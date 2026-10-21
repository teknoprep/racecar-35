"""AEMnet: the firmware decoder (src/aemnet.h), the bench encoder (tools/can_sim.py) and the
`validate` command, checked against each other and against AEM's documented layout.

Reference documents (AEM's own): "TA2 Infinity AEMnet Configuration" Rev A (AEMnet v150609)
and the 30-0300 X-Series UEGO gauge manual (doc 10-0300). The golden bytes below are worked
out by hand from those tables, not from the encoder.
"""
import os
import pathlib
import pty
import random
import re
import select
import shutil
import subprocess
import sys
import tempfile
import threading
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
HEADER = ROOT / "src" / "aemnet.h"
SIM = ROOT / "tools" / "can_sim.py"
sys.path.insert(0, str(ROOT / "tools"))
import can_sim  # noqa: E402

needs_gxx = pytest.mark.skipif(not shutil.which("g++"), reason="host C++ compiler not installed")

# ---------------------------------------------------------------- host decoder harness
# Speaks a tiny line protocol so Python can drive the REAL firmware decoder:
#   F <id hex> <ext 0|1> <payload hex> <now_ms>   decode one frame
#   R <now_ms>                                     expire, then print the AEMNET line fields
HARNESS = r'''
#include "%s"
#include <cstdio>
#include <cstring>
#include <cstdlib>
int main() {
    aemnet::State s;
    char line[256];
    while (fgets(line, sizeof line, stdin)) {
        if (line[0] == 'F') {
            unsigned long id; int ext; char hex[64]; unsigned long now;
            if (sscanf(line, "F %%lx %%d %%63s %%lu", &id, &ext, hex, &now) != 4) return 2;
            uint8_t b[8] = {0}; size_t n = strlen(hex) / 2;
            for (size_t i = 0; i < n && i < 8; i++) { char t[3] = {hex[2*i], hex[2*i+1], 0}; b[i] = (uint8_t)strtoul(t, 0, 16); }
            int used = aemnet::decode((uint32_t)id, ext != 0, b, (uint8_t)n, s, (uint32_t)now);
            printf("D %%d\n", used);
        } else if (line[0] == 'R') {
            unsigned long now; if (sscanf(line, "R %%lu", &now) != 1) return 3;
            aemnet::expire(s, (uint32_t)now, 2000);
            printf("AEMNET,%%ld,%%d,%%d,%%d,%%d,%%d,%%d,%%d,%%ld,%%u\n", (long)s.rpm, s.clt_f_x10, s.map_x10,
                   s.tps_x10, aemnet::afrX10(s), s.iat_f_x10, aemnet::batteryX10(s), s.oil_x10,
                   (long)aemnet::lambdaX10000(s), (unsigned)aemnet::uegoStatus(s));
        } else if (line[0] == 'Q') return 0;
        fflush(stdout);
    }
    return 0;
}
''' % HEADER


@pytest.fixture(scope="module")
def decoder(tmp_path_factory):
    if not shutil.which("g++"):
        pytest.skip("host C++ compiler not installed")
    d = tmp_path_factory.mktemp("aemnet")
    (d / "h.cpp").write_text(HARNESS)
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Wextra", "-Werror", "-fsanitize=undefined",
                    "-fno-sanitize-recover=all", str(d / "h.cpp"), "-o", str(d / "h")], check=True)
    return d / "h"


class Decoder:
    def __init__(self, exe):
        self.p = subprocess.Popen([str(exe)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)

    def frame(self, msg_id, ext, payload, now):
        self.p.stdin.write(f"F {msg_id:x} {1 if ext else 0} {bytes(payload).hex()} {now}\n")
        self.p.stdin.flush()
        return int(self.p.stdout.readline().split()[1])

    def report(self, now):
        self.p.stdin.write(f"R {now}\n")
        self.p.stdin.flush()
        vals = [int(x) for x in self.p.stdout.readline().strip().split(",")[1:]]
        return dict(zip(can_sim.AEMNET_LINE_FIELDS, vals))

    def close(self):
        try:
            self.p.stdin.write("Q\n")
            self.p.stdin.flush()
        except BrokenPipeError:
            pass
        self.p.wait(timeout=5)


# ---------------------------------------------------------------- golden bytes (from the docs)
def test_engine_frame_matches_the_ta2_table():
    # rpm 3000 / 0.39063 = 7679.9 -> 7680 = 0x1E00; load = 0; throttle 39 % / 0.0015259 = 25558.6
    # -> 25559 = 0x63D7; IAT 85 F = 29.4 C -> 29 = 0x1D; coolant 60 F = 15.6 C -> 16 = 0x10
    f = can_sim.aemnet_frames(rpm=3000, tps_pct=39.0, iat_f=85, clt_f=60, devices="ecu")
    assert f[can_sim.AEM_ID_ENGINE] == bytes.fromhex("1E00000063D71D10")


def test_lambda_and_pressure_frames_match_the_ta2_table():
    # AFR 14.65 = lambda 1.0 -> (1.0-0.5)/0.00390625 = 128 = 0x80 in bytes 0 AND 1; timing 24 deg
    # -> (24+17)/0.35156 = 116.6 -> 117 = 0x75; battery 13.8 V / 0.0002455 = 56211.8 -> 0xDB94
    f = can_sim.aemnet_frames(afr=14.65, adv_deg=24, batt_v=13.8, map_kpa=101.3, oil_psi=45,
                              devices="ecu")
    assert f[can_sim.AEM_ID_LAMBDA] == bytes.fromhex("8080000000" "75" "DB94")
    # MAP 101.3 kPa x10 = 1013 = 0x03F5; oil 45 psi / 0.580151 = 77.6 -> 78 = 0x4E; lambda target 0x80
    assert f[can_sim.AEM_ID_PRESSURE] == bytes.fromhex("03F50000" "4E" "80" "0000")


def test_uego_frame_matches_the_30_0300_table():
    # lambda 1.0 -> 10000 = 0x2710; O2 0 %; supply 13.8 V -> 138 = 0x8A; byte5 reserved;
    # byte6 = LSU4.9 (0x02) | Lambda Data Valid (0x80) = 0x82; byte7 no fault
    f = can_sim.aemnet_frames(afr=14.65, batt_v=13.8, devices="uego")
    assert f == {0x180: bytes.fromhex("27100000" "8A" "00" "82" "00")}
    # gauge menu "ID 16" = 0x18F; fault sets byte7 bit6 and clears valid
    f = can_sim.aemnet_frames(afr=14.65, devices="uego", uego_id=16, uego_state="fault")
    assert list(f) == [0x18F] and f[0x18F][6] == 0x02 and f[0x18F][7] == 0x40
    assert can_sim.aemnet_frames(devices="uego", uego_state="warmup")[0x180][6] == 0x02


def test_both_devices_and_ids():
    f = can_sim.aemnet_frames(devices="both", uego_id=3)
    assert sorted(f) == [0x182, can_sim.AEM_ID_ENGINE, can_sim.AEM_ID_LAMBDA, can_sim.AEM_ID_PRESSURE]
    assert all(len(p) == 8 for p in f.values())


# ---------------------------------------------------------------- decoder (real header) vs mirror
@needs_gxx
def test_firmware_decoder_agrees_with_the_python_mirror(decoder):
    """Thousands of random frame sets: the compiled firmware decoder and the Python mirror
    (which `validate` uses to predict the Teensy's line) must agree on every field."""
    rng = random.Random(1777)
    dec = Decoder(decoder)
    try:
        ids = [can_sim.AEM_ID_ENGINE, can_sim.AEM_ID_LAMBDA, can_sim.AEM_ID_PRESSURE, None]
        for k in range(3000):
            now = 10_000 + k * 5000          # 5 s apart: everything from the last set has expired
            frames = {}
            for i in ids:
                if rng.random() < 0.6:
                    mid = i if i is not None else 0x180 + rng.randrange(16)
                    payload = bytes(rng.randrange(256) for _ in range(8))
                    if i is None and rng.random() < 0.5:     # exercise the status flags
                        payload = payload[:6] + bytes([rng.choice([0, 0x02, 0x80, 0x82, 0xA2]),
                                                       rng.choice([0, 0x40])])
                    frames[mid] = payload
            dec.report(now)                    # expire the previous set
            for mid, payload in frames.items():
                assert dec.frame(mid, True, payload, now) == 1
            got = dec.report(now)
            want = can_sim.aemnet_line_from_frames(frames)
            if not frames:
                want = {k: -1 for k in can_sim.AEMNET_LINE_FIELDS}
                want["uego_status"] = 0
            assert got == want, f"set {k}: frames={ {hex(m): p.hex() for m, p in frames.items()} }"
    finally:
        dec.close()


@needs_gxx
def test_encoder_round_trips_through_the_firmware_decoder(decoder):
    """bench values -> encoder -> REAL decoder lands within each field's documented resolution."""
    dec = Decoder(decoder)
    try:
        now = 1000
        for t in range(0, 120, 3):
            v = can_sim.bench_values(float(t), "sweep")
            dec.report(now + 5000)
            now += 5000
            for mid, payload in can_sim.aemnet_frames(**v, devices="both").items():
                dec.frame(mid, True, payload, now)
            g = dec.report(now)
            assert abs(g["rpm"] - v["rpm"]) <= 1
            assert abs(g["tps_x10"] / 10 - v["tps_pct"]) <= 0.06
            assert abs(g["clt_f_x10"] / 10 - v["clt_f"]) <= 0.91      # whole degC = 1.8 F steps
            assert abs(g["iat_f_x10"] / 10 - v["iat_f"]) <= 0.91
            assert abs(g["map_x10"] / 10 - v["map_kpa"]) <= 0.051
            assert abs(g["oil_x10"] / 10 - v["oil_psi"]) <= 0.35       # half a 0.58 psi step + x10 rounding
            assert abs(g["bat_x10"] / 10 - v["batt_v"]) <= 0.051
            assert abs(g["afr_x10"] / 10 - v["afr"]) <= 0.051          # UEGO owns AFR: 0.0001 lambda
            assert g["uego_status"] == 1
    finally:
        dec.close()


@needs_gxx
def test_decoder_rules(decoder):
    dec = Decoder(decoder)
    try:
        eng = can_sim.aemnet_frames(rpm=4000, devices="ecu")[can_sim.AEM_ID_ENGINE]
        # an 11-bit frame with an AEMnet number is NOT AEMnet; neither is a short frame
        assert dec.frame(0x180, False, bytes(8), 100) == 0
        assert dec.frame(can_sim.AEM_ID_ENGINE, True, eng[:7], 100) == 0
        assert dec.frame(0x190, True, bytes(8), 100) == 0              # past "ID 16"
        assert dec.report(100)["rpm"] == -1
        assert dec.frame(can_sim.AEM_ID_ENGINE, True, eng, 100) == 1
        assert dec.report(100)["rpm"] == 4000
        assert dec.report(2100)["rpm"] == 4000                         # 2.0 s: still live
        assert dec.report(2101)["rpm"] == -1                           # >2.0 s: expired
        # temperatures: s8 degC extremes map to 18*C+320 exactly
        dec.frame(can_sim.AEM_ID_ENGINE, True, bytes([0, 0, 0, 0, 0, 0, 0x80, 0x7F]), 3000)
        r = dec.report(3000)
        assert (r["iat_f_x10"], r["clt_f_x10"]) == (-1984, 2606)
        # UEGO owns lambda: valid -> its value; warm-up / fault -> -1, never the ECU's lambda
        lam = can_sim.aemnet_frames(afr=13.0, devices="ecu")[can_sim.AEM_ID_LAMBDA]
        dec.frame(can_sim.AEM_ID_LAMBDA, True, lam, 4000)
        assert dec.report(4000)["uego_status"] == 0
        ecu_afr = dec.report(4000)["afr_x10"]
        assert abs(ecu_afr - 130) <= 1                                 # ECU lambda #1 (8-bit)
        for state, st, afr in (("valid", 1, 147), ("warmup", 2, -1), ("fault", 3, -1)):
            u = can_sim.aemnet_frames(afr=14.7, devices="uego", uego_state=state)[0x180]
            dec.frame(0x180, True, u, 4100)
            r = dec.report(4100)
            assert (r["uego_status"], r["afr_x10"]) == (st, afr), state
        # gauge gone -> the ECU's lambda again; battery falls back to the gauge supply
        assert dec.report(6200)["afr_x10"] == -1       # everything stale by now
        u = can_sim.aemnet_frames(batt_v=12.6, devices="uego")[0x180]
        dec.frame(0x180, True, u, 7000)
        assert dec.report(7000)["bat_x10"] == 126
    finally:
        dec.close()


# ---------------------------------------------------------------- slcan transport
class _FakeSer:
    def __init__(self):
        self.out = b""

    def write(self, b):
        self.out += b
        return len(b)


def test_send_many_writes_29_bit_frames_for_aemnet():
    bus = can_sim.SLCAN.__new__(can_sim.SLCAN)
    bus.ser = _FakeSer()
    bus.max_write_ms, bus.slow_writes = 0.0, 0
    frames, ext = can_sim.protocol_frames("aemnet", can_sim.bench_values(0.0))
    assert ext is True
    bus.send_many(frames, extended=ext)
    lines = bus.ser.out.split(b"\r")[:-1]
    assert len(lines) == 4 and all(l.startswith(b"T") for l in lines)
    assert lines[0].startswith(b"T01F0A0008")
    assert any(l.startswith(b"T000001808") for l in lines)
    # and the 11-bit protocols are unchanged
    bus.ser = _FakeSer()
    f, ext = can_sim.protocol_frames("megasquirt", can_sim.bench_values(0.0))
    bus.send_many(f, extended=ext)
    assert ext is False and bus.ser.out.startswith(b"t5E88")


def test_expected_lines_for_the_11_bit_protocols():
    v = dict(rpm=3650, clt_f=210, map_kpa=101.3, tps_pct=44.5, iat_f=95, afr=13.2, batt_v=14.1,
             oil_psi=55.5, adv_deg=20)
    f, _ = can_sim.protocol_frames("megasquirt", v)
    assert can_sim.expected_teensy_line("megasquirt", f) == ("ECU", {
        "rpm": 3650, "clt_f_x10": 2100, "map_x10": 1013, "tps_x10": 445, "afr_x10": 132,
        "iat_f_x10": 950, "bat_x10": 141, "oil_x10": -1})
    f, _ = can_sim.protocol_frames("rc35", v)
    assert can_sim.expected_teensy_line("rc35", f) == ("ECU", {
        "rpm": 3650, "clt_f_x10": 2100, "map_x10": 1013, "tps_x10": 445, "afr_x10": 132,
        "iat_f_x10": 950, "bat_x10": 141, "oil_x10": 555})


# ---------------------------------------------------------------- end to end, no hardware
FRAME_T = re.compile(rb"^T([0-9A-F]{8})(\d)([0-9A-F]*)$")


def _fake_teensy(decoder_exe, can_master, teensy_master, stop, corrupt=False):
    """Read 29-bit frames the tool sends to the fake CANable, decode them with the REAL firmware
    decoder, and print AEMNET lines on the fake Teensy's USB serial - like the firmware does."""
    dec = Decoder(decoder_exe)
    buf = b""
    t0 = time.monotonic()
    last = 0.0
    try:
        while not stop.is_set():
            r, _, _ = select.select([can_master], [], [], 0.01)
            now_ms = int((time.monotonic() - t0) * 1000) + 1
            if r:
                buf += os.read(can_master, 65536)
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    m = FRAME_T.match(line.strip())
                    if m:
                        dec.frame(int(m.group(1), 16), True, bytes.fromhex(m.group(3).decode()), now_ms)
            if time.monotonic() - last > 0.02:                # the firmware emits ~50-100 Hz
                last = time.monotonic()
                vals = dec.report(now_ms)
                if vals["uego_status"] or vals["rpm"] >= 0:
                    if corrupt and vals["rpm"] > 0:
                        vals["rpm"] += 1                       # a decoder bug the tool must catch
                    out = "AEMNET," + ",".join(str(vals[f]) for f in can_sim.AEMNET_LINE_FIELDS)
                    os.write(teensy_master, (out + "\r\n").encode())
    finally:
        dec.close()


def _run_validate(decoder_exe, extra=(), corrupt=False):
    cm, cs = pty.openpty()
    tm, ts = pty.openpty()
    stop = threading.Event()
    th = threading.Thread(target=_fake_teensy, args=(decoder_exe, cm, tm, stop, corrupt), daemon=True)
    th.start()
    try:
        p = subprocess.run([sys.executable, str(SIM), "validate", "-p", os.ttyname(cs),
                            "--teensy", os.ttyname(ts), "--hold", "1.2", "--settle", "0.5",
                            "--hz", "50", *extra],
                           capture_output=True, text=True, timeout=90)
    finally:
        stop.set()
        th.join(timeout=5)
        for fd in (cm, cs, tm, ts):
            os.close(fd)
    return p.returncode, p.stdout + p.stderr


@needs_gxx
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_validate_passes_against_the_real_decoder(decoder):
    rc, out = _run_validate(decoder)
    assert rc == 0, out
    assert "6/6 points passed" in out and "PASS" in out, out
    assert "uego-warmup" in out and "uego-fault" in out


@needs_gxx
@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_validate_fails_on_a_wrong_decode(decoder):
    rc, out = _run_validate(decoder, corrupt=True)
    assert rc == 1, out
    assert "MISMATCH" in out and "FAIL" in out, out


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_bench_aemnet_puts_only_29_bit_frames_on_the_wire():
    master, slave = pty.openpty()
    proc = subprocess.Popen([sys.executable, str(SIM), "bench", "--protocol", "aemnet", "--hz", "50",
                             "-p", os.ttyname(slave), "--duration", "1.5"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    buf, ids, std = b"", {}, 0
    deadline = time.time() + 15
    try:
        while time.time() < deadline and proc.poll() is None:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                buf += os.read(master, 65536)
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    line = line.strip()
                    m = FRAME_T.match(line)
                    if m:
                        ids[int(m.group(1), 16)] = ids.get(int(m.group(1), 16), 0) + 1
                    elif line[:1] == b"t":
                        std += 1
    finally:
        if proc.poll() is None:
            proc.terminate()
        out = proc.communicate(timeout=10)[0]
        os.close(master)
        os.close(slave)
    assert set(ids) == {can_sim.AEM_ID_ENGINE, can_sim.AEM_ID_LAMBDA, can_sim.AEM_ID_PRESSURE, 0x180}, out
    assert std == 0, "an 11-bit frame went out in AEMnet mode"
    assert min(ids.values()) >= 40, f"too few cycles: {ids}\n{out}"


# ---------------------------------------------------------------- the REAL SD sample writer
@needs_gxx
def test_session_writer_logs_aemnet_without_ever_dropping_a_sample(tmp_path):
    """writeSessionSample() extracted from src/main.cpp, compiled with stubs: every OTHER channel
    at its widest (the existing worst case) AND every AEMnet group live. The 640 B line cannot grow,
    so low-priority AEM groups must be left out - the sample itself must never be dropped, and the
    gauge's lambda (highest priority) must survive. The realistic MS3 + UEGO case logs all of it."""
    import json
    sys.path.insert(0, str(ROOT / "tests"))
    from test_wifi_only import TEENSY, block
    sig = "static void writeSessionSample("
    fn = block(TEENSY[TEENSY.index(sig, TEENSY.index(sig) + 1):], sig)
    src = (f'#include "{ROOT / "crowpanel-arduino/RaceDash/aem_afr.h"}"\n#include "{HEADER}"\n' + r'''
#include <cassert>
#include <stdarg.h>
#include <cstring>
#include <string>
#include <iostream>
constexpr size_t SESSION_LINE_CAP = 640;
constexpr uint32_t CAN_STALE_MS = 2000;
uint32_t millis() { return 10000; }
uint32_t micros() { return 10000000; }
uint32_t session_start_ms = 5000, session_start_unix = 1788966000;
uint32_t session_last_flush_ms = 10000, session_samples = 0, dbg_sdwr_max_us = 0;
bool session_file_open = true;
struct { bool aem_afr = true; bool vin_direct = true; } g_cfg;
int16_t vin_x10 = 138;
aemafr::Reading aem_reading;
struct { uint32_t last_ms = 10000; int tps_x10 = 999; int map_x10 = 1012;
         int iat_f_x10 = 882; int bat_x10 = 139; int afr_x10 = 147; int oil_x10 = 421; } can_ecu;
aemnet::State can_aem;
uint32_t bt_last_ms = 10000;
int bt_tps_x10 = 500, bt_spark_x10 = -35, bt_iat_f_x10 = 900, bt_volt_x10 = 141;
struct File { std::string bytes;
    int write(const uint8_t* p, size_t n) { bytes.append((const char*)p, n); return n; }
    void close() {} void sync() {} } session_file;
struct { void printf(const char*, ...) {} } Serial;
void emitSessionStatus(bool) {}
''' + fn + r'''
void sample() {   // the widest values the existing worst-case test uses
    writeSessionSample(3, 50, -89.999999f, -179.999999f, 249.5f, true, 299.9f, 359.9f,
                       65535, 30000, 30000, -2.0f, -2.0f, -2.0f, -250.0f, -250.0f, -250.0f, 999);
}
int main() {
    aem_reading = aemafr::fromMillivolts(3000);
    // 1. everything live, AEM at its widest: rpm 25600, temps -198.4, lambda1 1.4961, MAP 6553.5 ...
    can_aem.rpm = 25600; can_aem.tps_x10 = 1000; can_aem.iat_f_x10 = -1984; can_aem.clt_f_x10 = -1984;
    can_aem.lambda1_x10000 = 14961; can_aem.adv_x10 = -170; can_aem.bat_x10 = 161;
    can_aem.map_x10 = 32767; can_aem.oil_x10 = 1479;
    can_aem.engine_ms = can_aem.lambda_ms = can_aem.pressure_ms = 9990;
    can_aem.uego_lambda_x10000 = 65535; can_aem.uego_sysv_x10 = 255; can_aem.uego_flags6 = 0x82;
    can_aem.uego_flags7 = 0; can_aem.uego_index = 1; can_aem.uego_ms = 9995;
    sample();
    // 2. realistic: MS3 on the bus + the X-Series UEGO gauge only (no AEM ECU)
    can_aem = aemnet::State{};
    can_aem.uego_lambda_x10000 = 10034; can_aem.uego_sysv_x10 = 138; can_aem.uego_flags6 = 0x82;
    can_aem.uego_index = 1; can_aem.uego_ms = 9995;
    sample();
    // 3. gauge warming up: status logged, NO lambda/afr keys
    can_aem.uego_flags6 = 0x02;
    sample();
    std::cout << session_file.bytes;
}
''')
    (tmp_path / "w.cpp").write_text(src)
    subprocess.run(["g++", "-std=c++17", "-Wall", "-Werror", "-fsanitize=undefined",
                    "-fno-sanitize-recover=all", str(tmp_path / "w.cpp"), "-o", str(tmp_path / "w")],
                   check=True)
    out = subprocess.check_output([str(tmp_path / "w")], text=True)
    lines = out.splitlines()
    assert len(lines) == 3, "a sample was DROPPED (the line overflowed)"
    assert max(map(len, lines)) < 640
    full, ms3_uego, warm = (json.loads(l) for l in lines)
    # extreme case: highest priority kept, every AEM key that IS present is correct
    assert full["aem_lambda"] == 6.5535 and full["aem_afr"] == 96.0 and full["aem_uego"] == 1
    for k, v in (("aem_rpm", 25600), ("aem_coolant_f", -198.4), ("aem_map_kpa", 3276.7),
                 ("aem_oil_psi", 147.9), ("aem_lambda1", 1.4961), ("aem_batt_v", 16.1)):
        if k in full:
            assert full[k] == v, k
    # the MS3 keys and the AEM keys never merge
    assert full["afr_can"] == 14.7 and full["map_kpa"] == 101.2
    # realistic case: all of the gauge's keys, and the AEM ECU groups are simply absent
    assert ms3_uego["aem_lambda"] == 1.0034 and ms3_uego["aem_afr"] == 14.7 and ms3_uego["aem_uego"] == 1
    assert "aem_rpm" not in ms3_uego and "aem_map_kpa" not in ms3_uego
    assert warm["aem_uego"] == 2 and "aem_lambda" not in warm and "aem_afr" not in warm

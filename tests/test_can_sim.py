"""Bench CAN tool tests.

No CAN hardware needed: a **pty** stands in for the USB slcan adapter, so we can
measure the cadence the tool actually puts on the wire. That matters because the
old transmit loops called ``poll()`` — which used to block for the serial
timeout (0.2 s) on an idle bus — once per cycle, stretching every cycle and
delivering the injected values in bursts (the "RPM on the dash jumps around"
symptom). ``test_transmit_cadence_is_paced`` is the regression guard for that.
"""

import os
import pathlib
import pty
import re
import select
import struct
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SIM = ROOT / "tools" / "can_sim.py"
sys.path.insert(0, str(ROOT / "tools"))

import can_sim  # noqa: E402

FRAME_RE = re.compile(rb"t([0-9A-F]{3})([0-9])((?:[0-9A-F]{2})+)")


# --------------------------------------------------------------- pure encoding
def test_rc35_round_trip():
    frames = can_sim.rc35_frames(rpm=4321, clt_f=210, map_kpa=101.3, tps_pct=44.5,
                                 iat_f=120, afr=12.8, batt_v=11.9, oil_psi=68.4,
                                 adv_deg=-3, seq=7)
    assert set(frames) == set(can_sim.RC35_IDS), "both bench frames are required"
    core = can_sim.decode_rc35(can_sim.RC35_CORE_ID, frames[can_sim.RC35_CORE_ID])
    aux = can_sim.decode_rc35(can_sim.RC35_AUX_ID, frames[can_sim.RC35_AUX_ID])
    for want in ("RPM  4321", "TEMP 210 F", "IAT 120 F", "TPS  44.5 %"):
        assert want in core, f"{want!r} missing from {core!r}"
    for want in ("AFR 12.80", "VOLT  11.9 V", "OIL   68.4 PSI", "-3 BTDC"):
        assert want in aux, f"{want!r} missing from {aux!r}"
    assert len(frames[can_sim.RC35_CORE_ID]) == 8
    assert len(frames[can_sim.RC35_AUX_ID]) == 8
    # signed ignition advance must survive the round trip
    assert can_sim.rc35_frames(adv_deg=-12)[can_sim.RC35_AUX_ID][4] == 0xF4


def test_rc35_wire_bytes_are_frozen():
    """GOLDEN BYTES — the exact wire encodings pumpCAN() in src/main.cpp decodes.

    If this fails, one side of the contract moved: check the scaling block in
    can_sim.py against the CAN_BENCH_CORE_ID/AUX_ID cases in src/main.cpp before
    "fixing" the test.

    Scalings the firmware applies to these bytes:
      rpm u16 as-is | map kPa x10 as-is | tps x2 -> x10 (*5) | clt/iat °F -> x10
      afr x10 as-is | batt x10 V as-is | oil x10 PSI as-is
    """
    frames = can_sim.rc35_frames(rpm=3650, clt_f=210, map_kpa=101.3, tps_pct=44.5,
                                 iat_f=120, afr=12.8, batt_v=11.9, oil_psi=68.4,
                                 adv_deg=-3, seq=7)
    assert frames[0x700].hex().upper() == "0E4203F559D27807"
    assert frames[0x701].hex().upper() == "807702ACFD000000"
    assert struct.unpack(">H", frames[0x700][0:2])[0] == 3650     # rpm
    assert struct.unpack(">H", frames[0x700][2:4])[0] == 1013     # kPa x10
    assert frames[0x700][4] == 89                                 # 44.5 % -> x2
    assert frames[0x700][5] == 210                                # TEMP degF
    assert frames[0x700][6] == 120                                # IAT degF
    assert frames[0x700][7] == 7                                  # seq
    assert frames[0x701][0] == 128                                # AFR x10
    assert frames[0x701][1] == 119                                # V x10
    assert struct.unpack(">H", frames[0x701][2:4])[0] == 684       # PSI x10
    assert struct.unpack(">b", frames[0x701][4:5])[0] == -3        # advance, signed


def test_bench_values_are_continuous():
    """No random component: a 10 ms step must be tiny, so any jump seen on the
    dash is the transport/logger, not the data."""
    prev = can_sim.bench_values(0.0)
    for i in range(1, 200):
        cur = can_sim.bench_values(i * 0.01)
        assert abs(cur["rpm"] - prev["rpm"]) < 60, f"rpm step at t={i * 0.01}"
        assert abs(cur["clt_f"] - prev["clt_f"]) < 1.0
        assert abs(cur["oil_psi"] - prev["oil_psi"]) < 5.0
        prev = cur


def test_steady_profile_is_exactly_steady():
    vals = {can_sim.rpm_wave(t, "steady", 7000.0) for t in (0, 1, 5, 60)}
    assert vals == {7000.0}


# ----------------------------------------------------------- wire cadence (pty)
def _collect(cmd, seconds, hz_hint):
    """Run the tool against a pty and return (frames, cycles, walls, stderr)."""
    master, slave = pty.openpty()
    path = os.ttyname(slave)
    proc = subprocess.Popen(
        [sys.executable, str(SIM)] + cmd + ["-p", path, "--duration", str(seconds)],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    frames, walls, buf = [], [], b""
    try:
        deadline = time.time() + seconds + 12
        while time.time() < deadline:
            r, _, _ = select.select([master], [], [], 0.1)
            if r:
                chunk = os.read(master, 65536)
                if not chunk:
                    break
                buf += chunk
                now = time.time()
                while b"\r" in buf:
                    line, buf = buf.split(b"\r", 1)
                    m = FRAME_RE.match(line.strip() + b"\r")
                    if m:
                        frames.append((int(m.group(1), 16), bytes.fromhex(
                            m.group(3).decode())))
                        walls.append(now)
            if proc.poll() is not None:
                break
    finally:
        if proc.poll() is None:
            proc.terminate()
        out = proc.communicate(timeout=10)[0]
        os.close(master)
        os.close(slave)
    return frames, walls, out


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
@pytest.mark.parametrize("hz,expect_frames_per_cycle", [(100, 2)])
def test_transmit_cadence_is_paced(hz, expect_frames_per_cycle):
    """The bench frames must go out at the requested rate, evenly."""
    frames, walls, out = _collect(["bench", "--hz", str(hz), "--profile", "sweep"],
                                  seconds=2.0, hz_hint=hz)
    assert frames, f"nothing was transmitted\n{out}"
    # every cycle carries BOTH frames: all 7 channels + rpm, no MS3 framing
    core = sum(1 for i, _ in frames if i == can_sim.RC35_CORE_ID)
    aux = sum(1 for i, _ in frames if i == can_sim.RC35_AUX_ID)
    assert core == aux > 0, f"unbalanced cycles: core={core} aux={aux}\n{out}"
    assert not any(i in (0x5E8, 0x5E9, 0x5EA, 0x5EB) for i, _ in frames), \
        "MS3Pro framing must NOT be on the bus by default"
    # cadence: measure between cycles (0x700 only) over the middle of the run
    cyc = [w for i, w in zip((i for i, _ in frames), walls)
           if i == can_sim.RC35_CORE_ID]
    assert len(cyc) >= 5
    span = cyc[-1] - cyc[0]
    achieved = (len(cyc) - 1) / span if span > 0 else 0
    assert achieved > hz * 0.85, \
        f"achieved {achieved:.1f} cycles/s of {hz} (bursts?)\n{out}"
    gaps = [b - a for a, b in zip(cyc, cyc[1:])]
    worst = max(gaps)
    assert worst < 3.0 / hz, \
        f"worst cycle gap {worst * 1000:.1f} ms at {hz} Hz — not paced\n{out}"
    # and the values themselves arrive in order (seq increments, no repeats)
    seqs = [d[7] for i, d in frames if i == can_sim.RC35_CORE_ID]
    assert len(set(seqs)) == len(seqs), "duplicate cycles"


@pytest.mark.skipif(not hasattr(os, "openpty"), reason="needs a pty")
def test_ms3_compat_mode_is_also_paced():
    """The MS3 compatibility mode used the same blocking poll(), so it was the
    most badly affected: 4 frames x 50 Hz intended, but a 200 ms stall per cycle
    meant a few Hz of lumpy traffic."""
    frames, walls, out = _collect(["ms3", "--hz", "50"], seconds=2.0, hz_hint=50)
    assert frames, f"nothing was transmitted\n{out}"
    ids = {i for i, _ in frames}
    assert {0x5E8, 0x5E9, 0x5EA, 0x5EB} <= ids, f"missing MS3 frames: {ids}\n{out}"
    span = walls[-1] - walls[0]
    fps = (len(frames) - 1) / span if span > 0 else 0
    assert fps > 150, f"only {fps:.0f} frames/s (expect ~200) — poll() is blocking\n{out}"

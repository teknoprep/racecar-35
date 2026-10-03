#!/usr/bin/env python3
"""Racecar-35 bench CAN tool.

Drives a **CANable (slcan firmware)** over USB serial to either:

  * broadcast **RC35 bench frames** — fake TEMP / OIL / VOLT / AFR / IAT / MAP / TPS
    (+ RPM) at 100 Hz so the data logger can be tested on the bench with no engine
    running  (`bench`), or
  * listen to and decode whatever is really on a CAN bus, including a live MS3
    (`listen`).

The bench frames are ours, NOT MS3Pro framing:

    0x700 RC35_CORE  [0:2] rpm u16        [2:4] map x10 kPa   [4] tps x2 %
                     [5] clt/TEMP F       [6] iat F           [7] rolling seq
    0x701 RC35_AUX   [0] afr x10          [1] batt x10 V      [2:4] oil x10 PSI
                     [4] adv i8 BTDC      [5:8] reserved (0)

src/main.cpp pumpCAN() parses those two ids into the same struct as the MS3
frames, so the dash shows all seven channels with no MS3 on the bus at all.
MS3Pro "Simplified Dash" framing still exists here for working with a REAL MS3
(`ms3`, `coolant`, `afr`, `iat`) — it is compatibility code, not the fake-data
source:

    0x5E8  [0:2] map x10 kPa   [2:4] rpm   [4:6] clt x10 F   [6:8] tps x10 %
    0x5E9  [4:6] mat/IAT x10 F
    0x5EA  [0] afrtgt1  [1] AFR1 (single byte, x10: 147 = 14.7)
    0x5EB  [0:2] battery x10 V

Examples
--------
    python3 tools/can_sim.py bench  -p /dev/ttyACM0                 # fake data, 200 frames/s
    python3 tools/can_sim.py bench  --dry-run                       # print frames, no hardware
    python3 tools/can_sim.py bench  -p /dev/ttyACM0 --profile pull --hz 200
    python3 tools/can_sim.py bench  -p /dev/ttyACM0 --profile steady --rpm 7000
    python3 tools/can_sim.py probe                                  # find the adapter + version
    python3 tools/can_sim.py listen -p /dev/ttyACM0                 # decode the bus
    python3 tools/can_sim.py ms3    -p /dev/ttyACM0 --static         # REAL MS3 framing
    python3 tools/can_sim.py send   -p /dev/ttyACM0 --id 0x700 --data 0BB8000000000000

slcan bitrate code: S0=10k S1=20k S2=50k S3=100k S4=125k S5=250k S6=500k S7=800k S8=1M
"""

import argparse
import math
import re
import struct
import sys
import threading
import time

try:
    import serial
except ImportError:  # pragma: no cover
    sys.exit("pyserial missing: python3 -m pip install pyserial")

# ---------------------------------------------------------------------------
# MS3 Simplified Dash broadcast
# ---------------------------------------------------------------------------
BASE_ID = 0x5E8
MS3_IDS = (0x5E8, 0x5E9, 0x5EA, 0x5EB)   # the four the logger parses


def s16(v):
    return struct.pack(">h", max(-32768, min(32767, int(round(v)))))


def u16(v):
    return struct.pack(">H", max(0, min(65535, int(round(v)))))


# ---------------------------------------------------------------------------
# RC35 bench broadcast — THE DEFAULT FAKE-DATA SOURCE (not MS3Pro framing)
# ---------------------------------------------------------------------------
# The logger only ever understood MS3Pro "Simplified Dash" frames, which is why
# this tool used to impersonate an MS3Pro — and why OIL could never be sent: that
# broadcast has no oil channel at all. These two frames carry every channel the
# dash shows, in a layout that is OURS, not MegaSquirt's:
#
#   0x700 RC35_CORE  [0:2] rpm      u16   rpm          (1 rpm/LSB)
#                    [2:4] map_kpa  u16   kPa x10
#                    [4]   tps_pct  u8    % x2         (0..127.5)
#                    [5]   clt_f    u8    degF         (TEMP, 0..255 F)
#                    [6]   iat_f    u8    degF         (0..255 F)
#                    [7]   seq      u8    cycle counter (drop detection)
#
#   0x701 RC35_AUX   [0]   afr_x10  u8    AFR x10      (147 = 14.7)
#                    [1]   batt_x10 u8    V x10        (138 = 13.8 V)
#                    [2:4] oil_x10  u16   PSI x10      (450 = 45.0 PSI)
#                    [4]   adv_deg  i8    deg BTDC
#                    [5:8] reserved --    must be 0 (room to grow)
#
# Keep this table in sync with pumpCAN() in src/main.cpp and with the decoder
# below: rc35_frames() and decode_rc35() are the reference implementation.
RC35_CORE_ID = 0x700
RC35_AUX_ID = 0x701
RC35_IDS = (RC35_CORE_ID, RC35_AUX_ID)


def u8(v):
    return struct.pack(">B", max(0, min(255, int(round(v)))))


def i8(v):
    return struct.pack(">b", max(-128, min(127, int(round(v)))))


def rc35_frames(rpm=0.0, clt_f=180.0, map_kpa=100.0, tps_pct=0.0,
                iat_f=90.0, afr=14.7, batt_v=13.8, oil_psi=45.0,
                adv_deg=18.0, seq=0,
                core_id=RC35_CORE_ID, aux_id=RC35_AUX_ID):
    """Build the two RC35 bench frames — every channel, every cycle.

    The ids are parameters so the emulator can be pointed at whatever the current
    receiver expects (`bench --core-id/--aux-id`) without touching the layout.
    """
    core = (u16(rpm) + u16(map_kpa * 10) + u8(tps_pct * 2) + u8(clt_f)
            + u8(iat_f) + u8(seq))
    aux = (u8(afr * 10) + u8(batt_v * 10) + u16(oil_psi * 10) + i8(adv_deg)
           + b"\x00\x00\x00")
    return {core_id: core, aux_id: aux}


def decode_rc35(msg_id, data, core_id=RC35_CORE_ID, aux_id=RC35_AUX_ID):
    """Inverse of rc35_frames() — the receiver's parser must agree with this."""
    if msg_id == core_id and len(data) >= 8:
        rpm, mp = struct.unpack(">HH", data[0:4])
        return (f"RPM {rpm:5d}  MAP {mp / 10:6.1f} kPa  TPS {data[4] / 2:5.1f} %  "
                f"TEMP {data[5]:3d} F  IAT {data[6]:3d} F  seq {data[7]}")
    if msg_id == aux_id and len(data) >= 5:
        oil = struct.unpack(">H", data[2:4])[0]
        adv = struct.unpack(">b", data[4:5])[0]
        return (f"AFR {data[0] / 10:5.2f}  VOLT {data[1] / 10:5.1f} V  "
                f"OIL {oil / 10:6.1f} PSI  ADV {adv:3d} BTDC")
    return ""


def rpm_wave(t, profile="sweep", rpm=3000.0, lo=900.0, hi=6400.0):
    """Smooth, physically plausible RPM — NO random component.

    Jitter on the dash should come from the transport (or the logger's own
    sampling), never from the data, so a steady source can be told apart from a
    broken link.
    """
    if profile == "steady":
        return float(rpm)
    if profile == "pull":
        # 1st..5th gear WOT pull: ramp to the redline, shift, ramp again.
        seg = 3.6
        gears = ((2600, 6800), (3800, 6900), (4600, 7000),
                 (5200, 7100), (5600, 7200))
        p = t % (seg * len(gears))
        lo_g, hi_g = gears[int(p // seg) % len(gears)]
        return lo_g + (hi_g - lo_g) * ((p % seg) / seg)
    # sweep (default): one slow ~10 s sine, like a dyno pull
    return lo + (hi - lo) * (0.5 + 0.5 * math.sin(t * 0.6))


def coolant_at(t, cold=60.0, hot=228.0, ramp=90.0, hold=15.0):
    """Warm-up / cool-down cycle that crosses the dash's default 220 F warning."""
    seg = ramp + hold
    p = t % (2 * seg)
    if p < ramp:
        return cold + (hot - cold) * (p / ramp)
    if p < seg:
        return hot
    if p < seg + ramp:
        return hot - (hot - cold) * ((p - seg) / ramp)
    return cold


def bench_values(t, profile="sweep", rpm=3000.0):
    """One coherent snapshot: everything derived from load, so no channel
    contradicts another and nothing jumps discontinuously except a gear shift."""
    if profile == "chop":
        # Deliberately UNphysical: every channel flips hard between two values.
        # A plausible sweep is too slow to judge a screen's refresh rate — this
        # profile makes a 1 Hz display obvious and a live one instantaneous.
        hi = (t % 2.0) < 1.0
        return {
            "rpm": 6800.0 if hi else 1400.0,
            "tps_pct": 92.0 if hi else 8.0,
            "map_kpa": 99.0 if hi else 30.0,
            "oil_psi": 78.0 if hi else 24.0,
            "afr": 12.6 if hi else 15.4,
            "clt_f": 215.0 if (t % 6.0) < 3.0 else 165.0,
            "iat_f": 140.0 if hi else 75.0,
            "batt_v": 14.4 if hi else 12.4,
            "adv_deg": 32.0 if hi else 12.0,
        }
    r = rpm_wave(t, profile, rpm)
    load = max(0.0, min(1.0, (r - 900.0) / 5600.0))
    clt = coolant_at(t)
    return {
        "rpm": r,
        "tps_pct": 6.0 + 88.0 * load,
        "map_kpa": 28.0 + 72.0 * load,
        "oil_psi": 22.0 + 55.0 * load,
        "afr": 13.4 + 1.3 * (1.0 - load),
        "clt_f": clt,
        "iat_f": 70.0 + 40.0 * load + 0.12 * (clt - 60.0),
        "batt_v": 13.9 - 0.5 * load,
        "adv_deg": 30.0 - 16.0 * load,
    }


def fmt_values(v):
    return (f"RPM {v['rpm']:5.0f}  TEMP {v['clt_f']:5.1f}F  OIL {v['oil_psi']:5.1f}  "
            f"VOLT {v['batt_v']:5.2f}  AFR {v['afr']:5.2f}  IAT {v['iat_f']:5.1f}  "
            f"MAP {v['map_kpa']:5.1f}  TPS {v['tps_pct']:5.1f}")


def ms3_frames(rpm=0, clt_f=180.0, map_kpa=100.0, tps_pct=0.0,
               iat_f=90.0, afr=14.7, batt_v=13.8, adv_deg=0.0):
    """Build the four broadcast frames exactly as the MS3 broadcasts them."""
    return {
        0x5E8: s16(map_kpa * 10) + u16(rpm) + s16(clt_f * 10) + s16(tps_pct * 10),
        0x5E9: u16(0) + u16(0) + s16(iat_f * 10) + s16(adv_deg * 10),
        0x5EA: bytes([0x93, int(afr * 10) & 0xFF, 0x64, 0, 0, 0, 0, 0]),
        0x5EB: s16(batt_v * 10) + bytes([0, 0, 0, 0, 0, 0]),
    }


def coolant_frames(t, start=60.0, end=235.0, ramp=45.0, hold=10.0):
    """Emulate an MS3Pro coolant warm-up cycle.

    Cycles: ramp start->end over `ramp` s, hold `hold` s, ramp back down, hold.
    Everything else is plausible-but-static so the coolant channel is the only
    thing moving. Returns (clt_f, frames_dict, phase_name).
    """
    seg = ramp + hold
    cycle = 2 * seg
    p = t % cycle
    if p < ramp:
        f = p / ramp
        clt = start + (end - start) * f
        phase = f"WARMING {start:.0f}->{end:.0f}F"
    elif p < seg:
        clt = end
        phase = f"HOT HOLD {end:.0f}F (warning range)"
    elif p < seg + ramp:
        f = (p - seg) / ramp
        clt = end - (end - start) * f
        phase = f"COOLING {end:.0f}->{start:.0f}F"
    else:
        clt = start
        phase = f"COLD HOLD {start:.0f}F"
    iat = 70.0 + (clt - start) * 0.35          # intake picks up some heat
    batt = 13.9 - 0.5 * min(1.0, max(0.0, (clt - start) / (end - start)))
    frames = ms3_frames(rpm=900, clt_f=clt, map_kpa=32.0, tps_pct=0.0,
                        iat_f=iat, afr=14.7, batt_v=batt, adv_deg=15.0)
    return clt, frames, phase


def afr_wave(t, rich=10.8, stoich=14.7, lean=17.6, dfco=19.4, period=50.0):
    """Emulate a gasoline AFR trace that crosses both dashboard warning ends.

    One period: WOT-rich pull -> ramp to stoich -> lean cruise -> overrun (DFCO)
    very-lean spike -> recovery back to rich. Returns (afr, phase).

    AFR1 lives in 0x5EA **byte 1** as a SINGLE byte = AFR x10 (147 = 14.7), which
    is what the Teensy's pumpCAN() reads (afr_x10 = msg.buf[1]).
    """
    p = t % period
    if p < 18.0:                      # WOT pull, rich and steady
        afr = rich + 0.25 * math.sin(p * 3.0)
        phase = f"WOT RICH {rich:.1f}"
    elif p < 26.0:                    # spool down to cruise
        f = (p - 18.0) / 8.0
        afr = rich + (stoich - rich) * f
        phase = f"RAMP {rich:.1f}->{stoich:.1f}"
    elif p < 34.0:                    # lean cruise
        f = (p - 26.0) / 8.0
        afr = stoich + (lean - stoich) * f
        phase = f"LEAN CRUISE -> {lean:.1f}"
    elif p < 38.0:                    # overrun / decel fuel cut
        afr = dfco
        phase = f"DFCO LEAN SPIKE {dfco:.1f}"
    else:                             # back on the throttle
        f = (p - 38.0) / (period - 38.0)
        afr = dfco - (dfco - rich) * f
        phase = "RECOVERY -> RICH"
    return afr, phase


def iat_wave(t, ambient=70.0, hot=155.0, ramp=60.0, hold=12.0):
    """Emulate intake air temp (0x5E9 bytes 4-5, int16 degF x10).

    Heat-soak shape: rises from ambient toward a hot-bay soak value, holds, then
    falls back — the classic heat-soak-and-clear pattern a real IAT sees.
    """
    seg = ramp + hold
    cycle = 2 * seg
    p = t % cycle
    if p < ramp:
        f = p / ramp
        iat = ambient + (hot - ambient) * (1.0 - math.exp(-3.0 * f))   # exponential soak
        phase = f"HEAT SOAK {ambient:.0f}->{hot:.0f}F"
    elif p < seg:
        iat = hot + 1.5 * math.sin(p * 2.0)
        phase = f"HOT SOAK {hot:.0f}F"
    elif p < seg + ramp:
        f = (p - seg) / ramp
        iat = hot - (hot - ambient) * (1.0 - math.exp(-3.0 * f))
        phase = f"COOLING {hot:.0f}->{ambient:.0f}F"
    else:
        iat = ambient + 1.0 * math.sin(p)
        phase = f"AMBIENT {ambient:.0f}F"
    return iat, phase


def set_iat(frames, iat_f):
    """Overwrite the IAT field (0x5E9 bytes 4-5) inside a frame set."""
    fr = dict(frames)
    payload = bytearray(fr[0x5E9])
    b = s16(int(round(iat_f * 10)))
    payload[4:6] = b
    fr[0x5E9] = bytes(payload)
    return fr


def set_afr(frames, afr):
    """Overwrite the AFR1 byte inside a frame set."""
    fr = dict(frames)
    payload = bytearray(fr[0x5EA])
    payload[1] = int(round(afr * 10)) & 0xFF
    fr[0x5EA] = bytes(payload)
    return fr


def decode_ms3(msg_id, data):
    """Human-readable decode of the frames we care about (mirrors the firmware)."""
    n = len(data)
    if n < 2:
        return ""
    if msg_id == 0x5E8 and n >= 8:
        mp, rpm, clt, tps = struct.unpack(">hHhh", data[:8])
        return f"MAP {mp/10:6.1f} kPa  RPM {rpm:5d}  CLT {clt/10:6.1f} F  TPS {tps/10:5.1f} %"
    if msg_id == 0x5E9 and n >= 6:
        mat = struct.unpack(">h", data[4:6])[0]
        return f"IAT {mat/10:6.1f} F"
    if msg_id == 0x5EA and n >= 2:
        return f"AFR(1) {data[1]/10:5.2f}"
    if msg_id == 0x5EB and n >= 2:
        batt = struct.unpack(">h", data[0:2])[0]
        return f"BATT {batt/10:5.1f} V"
    return ""


# ---------------------------------------------------------------------------
# slcan transport
# ---------------------------------------------------------------------------
BITRATE_CODE = {"10k": "S0", "20k": "S1", "50k": "S2", "100k": "S3", "125k": "S4",
                "250k": "S5", "500k": "S6", "800k": "S7", "1m": "S8"}
BITRATE_BPS = {"10k": 10_000, "20k": 20_000, "50k": 50_000, "100k": 100_000,
               "125k": 125_000, "250k": 250_000, "500k": 500_000,
               "800k": 800_000, "1m": 1_000_000}


def bus_load_pct(frames_per_s, bitrate_bps=500_000, bits_per_frame=130):
    """Rough CAN bus utilisation (%).

    A standard 8-byte frame is ~111 bits + up to ~19 stuffing bits; this uses the
    pessimistic 130 so the number flatters nothing. At 500 kbit/s ~3800 frames/s
    is the theoretical ceiling, so a few hundred frames/s is a few percent — the
    slcan **USB serial** link, not the bus, is what caps the emulator.
    """
    return (frames_per_s * bits_per_frame / float(bitrate_bps)) * 100.0


class SLCAN:
    def __init__(self, port, bitrate="500k", serial_baud=115200, verbose=True):
        self.verbose = verbose
        # --- adapter-side transmit accounting ------------------------------------
        # LAWICEL-class slcan answers each transmit with \r (accepted) or \x07 BEL
        # (no ACK / bus error). Some firmwares (CANable 2.x "Slcan: 100") answer
        # NOTHING at all — then a TX/ACK failure is invisible here and the only
        # proof is the RECEIVER's own frame counter (see PeerReader / --peer-port).
        self.tx_ok       = 0     # bare \r seen  -> adapter accepted a transmit
        self.tx_err      = 0     # \x07 seen     -> adapter reported a TX failure
        self.tx_err_prev = 0
        self.slow_writes = 0     # writes that took >5 ms (adapter stalling us)
        self.max_write_ms = 0.0
        self.ser = serial.Serial(port, serial_baud, timeout=0.2)
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.write(b"\r")            # make sure the adapter is in command state
        time.sleep(0.1)
        self.ser.reset_input_buffer()
        self.version = self._cmd(b"V\r", 0.4).strip()
        if self.verbose:
            print(f"[slcan] {port} @{serial_baud}  version={self.version!r}")
        self.ser.write(BITRATE_CODE[bitrate].encode() + b"\r")
        time.sleep(0.2)
        self.ser.reset_input_buffer()
        self.ser.write(b"O\r")           # open the bus
        time.sleep(0.3)
        resp = self.ser.read(8)
        if self.verbose:
            print(f"[slcan] bitrate {bitrate} ({BITRATE_CODE[bitrate]}), open -> {resp!r}")
        self.buf = b""

    def _cmd(self, raw, wait):
        self.ser.write(raw)
        time.sleep(wait)
        return self.ser.read(64).decode(errors="replace")

    def send(self, msg_id, data, extended=False):
        payload = bytes(data)
        if extended:
            frame = b"T%08X%d%s\r" % (msg_id, len(payload), payload.hex().upper().encode())
        else:
            frame = b"t%03X%d%s\r" % (msg_id, len(payload), payload.hex().upper().encode())
        self.ser.write(frame)

    def send_many(self, frames):
        """Write a whole cycle (all of its frames) in ONE serial write.

        One syscall per cycle instead of one per frame: larger, fewer writes are
        what keeps the injected cadence even on a USB CDC adapter. The write is
        timed — a write that blocks means the adapter has stopped draining us,
        which is an adapter-side failure signal that works even on firmwares that
        report nothing about transmits.
        """
        out = bytearray()
        for msg_id, payload in frames.items():
            payload = bytes(payload)
            out += b"t%03X%d%s\r" % (msg_id, len(payload),
                                      payload.hex().upper().encode())
        t0 = time.time()
        n = self.ser.write(bytes(out))
        ms = (time.time() - t0) * 1000.0
        if ms > self.max_write_ms:
            self.max_write_ms = ms
        if ms > 5.0:
            self.slow_writes += 1
        return n

    def poll(self):
        """Yield every complete frame received since the last call — NEVER blocks.

        ⚠️ This used to call ser.read(4096) with the port's 0.2 s timeout, so on
        an idle bus it stalled the caller for 200 ms. Every paced transmit loop
        calls poll() once per cycle, so each cycle could take 10x its period and
        the injected values came out in BURSTS — the "the RPM on the dash jumps
        around" bug. Read only what has already arrived.
        """
        try:
            avail = self.ser.in_waiting
        except OSError:
            avail = 0
        data = self.ser.read(avail) if avail else b""
        if data:
            self.buf += data
        while b"\r" in self.buf:
            line, self.buf = self.buf.split(b"\r", 1)
            line = line.strip()
            if not line:
                self.tx_ok += 1          # a bare CR = "transmit accepted"
                continue
            ch = line[:1]
            if ch == b"\x07":
                # BEL = the adapter could NOT put the frame on the bus: nobody
                # ACKed it (no second node, wrong bitrate, bus-off, wiring) or a
                # bus error occurred. This is the signal worth counting.
                self.tx_err += 1
                if self.verbose:
                    print("[slcan] \x07 TX FAILURE reported by the adapter "
                          "(no ACK / bus error)")
                continue
            try:
                if ch in (b"t", b"r"):                      # std data / remote
                    msg_id = int(line[1:4], 16)
                    dlc = int(line[4:5], 16)
                    payload = bytes.fromhex(line[5:5 + dlc * 2].decode())
                    yield msg_id, payload, False, ch == b"r"
                elif ch in (b"T", b"R"):                    # ext data / remote
                    msg_id = int(line[1:9], 16)
                    dlc = int(line[9:10], 16)
                    payload = bytes.fromhex(line[10:10 + dlc * 2].decode())
                    yield msg_id, payload, True, ch == b"R"
                else:
                    if self.verbose:
                        print(f"[slcan] <{line!r}>")
            except (ValueError, IndexError):
                if self.verbose:
                    print(f"[slcan] unparsed {line!r}")

    def close(self):
        try:
            self.ser.write(b"C\r")
            time.sleep(0.1)
        except Exception:
            pass
        self.ser.close()


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------
def _bench_cycle(bus, t, profile, rpm, seq, core_id, aux_id):
    """One whole cycle: build every channel, one serial write."""
    bus.send_many(rc35_frames(**bench_values(t, profile, rpm), seq=seq & 0xFF,
                             core_id=core_id, aux_id=aux_id))


def bench_autotune(bus, args, seconds_per_step=1.5, ceiling=2000.0):
    """Ramp the cycle rate until the link can't keep up; report the real ceiling.

    The CAN bus is not the limit (500 kbit/s carries ~3800 frames/s, and two
    channels at a few hundred cycles/s is a few percent). The **slcan USB serial
    link** is: every frame goes out as ~25 ASCII bytes. So the honest answer to
    "how fast can this go" is a measurement, not a constant.
    """
    print("[bench] --autotune: ramping the cycle rate until the link degrades")
    bps = BITRATE_BPS.get(args.bitrate, 500_000)
    hz = float(args.hz)
    best = 0.0
    while hz <= ceiling:
        if bus_load_pct(hz * 2, bps) > args.max_load:
            print(f"    stopped at {hz:.0f} Hz: {bus_load_pct(hz * 2, bps):.0f}% bus load "
                  f"exceeds --max-load {args.max_load:.0f}% — a saturated bus starts "
                  f"starving the receiver, which is the bug you are trying to test for")
            break
        t0 = time.time()
        n = 0
        last = t0
        worst = 0.0
        qmax = 0
        while time.time() - t0 < seconds_per_step:
            t = time.time() - t0
            _bench_cycle(bus, t, args.profile, args.rpm, n, args.core_id, args.aux_id)
            n += 1
            now = time.time()
            if n > 1:
                worst = max(worst, now - last)
            last = now
            try:
                qmax = max(qmax, bus.ser.out_waiting)
            except OSError:
                pass
            d = (t0 + n / hz) - time.time()
            if d > 0:
                time.sleep(d)
        el = time.time() - t0
        achieved = n / el if el > 0 else 0
        ok = achieved >= hz * 0.90 and qmax < 256
        print(f"    asked {hz:6.0f} Hz -> got {achieved:6.0f} cycles/s "
              f"({achieved * 2:5.0f} frames/s), worst gap {worst * 1000:5.1f} ms, "
              f"tx queue max {qmax:4d} B   {'ok' if ok else 'TOO FAST'}")
        if not ok:
            break
        best = hz
        hz = hz * 2 if hz >= 100 else hz + 50
    print(f"[bench] sustainable: ~{best:.0f} Hz cycles = {best * 2:.0f} frames/s "
          f"(bus load ≈ {bus_load_pct(best * 2, bps):.1f}% at {args.bitrate}).")
    print("[bench] If that is lower than expected, the slcan serial link is the "
          "bottleneck, not the bus — try a higher --serial-baud.")
    return best


class PeerReader(threading.Thread):
    """Read the RECEIVER's USB serial (the logger) and keep its frame counters.

    This is the only way to separate "the adapter never got the frames onto the
    bus" from "the frames arrived but were not ACKed / not exposed" when the
    slcan firmware reports nothing about transmits. The logger prints
    `BENCH frames/s=<n> ...` (firmware >= 0.1.159) and `CANDIAG,<frames/s>,<total>,...`
    once a second; both are parsed here.
    """
    RE_BENCH = re.compile(r"BENCH frames/s=(\d+)")
    RE_CANDIAG = re.compile(r"CANDIAG,(\d+),(\d+),")

    def __init__(self, port, baud=115200, verbose=True):
        super().__init__(daemon=True)
        self.port = port
        self.baud = baud
        self.verbose = verbose
        self.bench_fps = None      # frames/s the RECEIVER counted (None = no line yet)
        self.bench_oil = None
        self.candiag_fps = None
        self.lines = 0
        self.err = None
        self._stop = threading.Event()
        self.ser = None

    def run(self):
        try:
            self.ser = serial.Serial(self.port, self.baud, timeout=0.5)
        except Exception as exc:                      # noqa: BLE001
            self.err = str(exc)
            return
        while not self._stop.is_set():
            try:
                raw = self.ser.readline()
            except Exception as exc:                  # noqa: BLE001
                self.err = str(exc)
                return
            if not raw:
                continue
            self.lines += 1
            line = raw.decode(errors="replace")
            m = self.RE_BENCH.search(line)
            if m:
                self.bench_fps = int(m.group(1))
                om = re.search(r"oil=(-?\d+)", line)
                if om:
                    self.bench_oil = int(om.group(1))
            m = self.RE_CANDIAG.search(line)
            if m:
                self.candiag_fps = int(m.group(1))

    def stop(self):
        self._stop.set()
        if self.ser is not None:
            try:
                self.ser.close()
            except Exception:                         # noqa: BLE001
                pass

    def summary(self):
        if self.err:
            return f"peer {self.port}: {self.err}"
        if self.lines == 0:
            return f"peer {self.port}: silent (no serial output)"
        if self.bench_fps is None:
            return (f"peer {self.port}: {self.lines} lines, NO BENCH line "
                    f"(firmware older than 0.1.159, or wrong port)")
        extra = f", oil={self.bench_oil}" if self.bench_oil is not None else ""
        return (f"peer {self.port}: BENCH {self.bench_fps}/s{extra}"
                + (f", CANDIAG {self.candiag_fps}/s" if self.candiag_fps is not None else ""))


def cmd_bench(args):
    """Fast, steady RC35 bench broadcast — every channel, every cycle.

    Paced on an absolute schedule (no drift accumulation), one serial write per
    cycle, and it reports the ACHIEVED rate + worst gap + adapter TX backlog once
    a second, so a jitter problem can be pinned on the transport instead of
    guessed at.
    """
    period = 1.0 / args.hz
    bps = BITRATE_BPS.get(args.bitrate, 500_000)
    nframes = 2
    if not args.dry_run and args.autotune:
        bus = SLCAN(args.port, args.bitrate, serial_baud=args.serial_baud)
        try:
            bench_autotune(bus, args)
        finally:
            bus.close()
        return
    if args.dry_run:
        print(f"[bench] --dry-run: {nframes} frames/cycle at {args.hz:g} Hz "
              f"= {args.hz * nframes:g} frames/s. No serial port opened.\n")
        for k in (0, 5, 50):
            t = k * period
            v = bench_values(t, args.profile, args.rpm)
            print(f"  t={t:5.2f}s  {fmt_values(v)}")
            for mid, payload in rc35_frames(**v, seq=k & 0xFF,
                                            core_id=args.core_id,
                                            aux_id=args.aux_id).items():
                print(f"    0x{mid:03X} [{len(payload)}] {payload.hex().upper():<18}"
                      f"-> {decode_rc35(mid, payload, args.core_id, args.aux_id)}")
        return

    bus = SLCAN(args.port, args.bitrate, serial_baud=args.serial_baud)
    print(f"[bench] RC35 bench frames 0x{args.core_id:03X} + 0x{args.aux_id:03X} at "
          f"{args.hz:g} Hz cycles = {args.hz * nframes:g} frames/s "
          f"(bus load ≈ {bus_load_pct(args.hz * nframes, bps):.1f}% at {args.bitrate})")
    print("[bench] channels every cycle: RPM TEMP OIL VOLT AFR IAT MAP TPS (+ ADV)")
    print(f"[bench] profile: {args.profile}"
          + (f" (rpm {args.rpm:g})" if args.profile == 'steady' else ""))
    print("[bench] the dash's own monitor block repaints at ~10 Hz, so past ~20 Hz")
    print("[bench] cycles the SCREEN cannot show it — the wire still carries it.")
    print("[bench] Ctrl-C to stop.\n")
    req_load = bus_load_pct(args.hz * nframes, bps)
    if req_load > args.max_load:
        print(f"[bench] ⚠️  {req_load:.0f}% bus load exceeds --max-load {args.max_load:.0f}%: "
              f"a saturated bus can starve the receiver's loop, which then LOOKS like "
              f"a slow display. Lower --hz to test the display, raise it only to stress it.\n")
    peer = None
    if getattr(args, "peer_port", None):
        peer = PeerReader(args.peer_port, getattr(args, "peer_baud", 115200))
        peer.start()
        print(f"[bench] watching the receiver on {args.peer_port} for its BENCH/CANDIAG counters\n")
    t0 = time.time()
    n = 0
    recent = []
    last_report = t0
    try:
        while True:
            t = time.time() - t0
            v = bench_values(t, args.profile, args.rpm)
            _bench_cycle(bus, t, args.profile, args.rpm, n, args.core_id, args.aux_id)
            n += 1
            now = time.time()
            recent.append(now)
            if now - last_report >= 1.0:
                while recent and now - recent[0] > 1.0:
                    recent.pop(0)
                gaps = [b - a for a, b in zip(recent, recent[1:])]
                worst = max(gaps) * 1000.0 if gaps else 0.0
                try:
                    backlog = bus.ser.out_waiting
                except OSError:
                    backlog = -1
                print(f"  t={t:6.1f}s  {len(recent):3d} cycles/s  "
                      f"worst gap {worst:5.1f} ms  tx queue {backlog:4d} B  "
                      f"load {bus_load_pct(len(recent) * nframes, bps):4.1f}%  "
                      f"tx fail {bus.tx_err:4d} ({bus.tx_err - bus.tx_err_prev}/s)")
                if bus.tx_err > bus.tx_err_prev:
                    print("  ⚠️  adapter-reported TX failures are RISING: frames are not "
                          "being ACKed (no second node / termination / bitrate / bus-off)")
                bus.tx_err_prev = bus.tx_err
                print(f"           {fmt_values(v)}")
                if peer is not None:
                    print(f"           {peer.summary()}")
                last_report = now
            for msg_id, payload, ext, remote in bus.poll():
                print(f"  [rx] 0x{msg_id:03X} [{len(payload)}] {payload.hex().upper()}"
                      f"  {decode_rc35(msg_id, payload, args.core_id, args.aux_id)}"
                      f"{decode_ms3(msg_id, payload)}")
            if args.duration and t > args.duration:
                break
            nxt = t0 + n * period
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
        if peer is not None:
            peer.stop()
        el = time.time() - t0
        if el > 0:
            print(f"\n[bench] sent {n} cycles ({n * nframes} frames) in {el:.1f}s = "
                  f"{n / el:.1f} cycles/s, {n * nframes / el:.0f} frames/s")
            print(f"[bench] adapter: tx_ok={bus.tx_ok} (accepted) tx_fail={bus.tx_err} "
                  f"slow_writes={bus.slow_writes} max_write={bus.max_write_ms:.1f} ms")
            if bus.tx_ok == 0 and bus.tx_err == 0 and bus.slow_writes == 0:
                print("[bench] NOTE: this slcan firmware reports NOTHING per transmit "
                      "(no CR, no 0x07 BEL) — verified on a lone-node bus, where every "
                      "frame must fail. So a TX/ACK failure is INVISIBLE from the "
                      "adapter side here; the receiver's own counter is the proof.")
            elif bus.tx_err:
                print("[bench] VERDICT: the adapter reported TX failures → the frames are "
                      "NOT reaching the bus: no ACKing node (logger unpowered/unwired), "
                      "termination, bitrate, or bus-off.")
            if peer is not None:
                print(f"[bench] {peer.summary()}")
                if peer.err or peer.lines == 0:
                    print("[bench] VERDICT: receiver serial is unreadable — fix the peer "
                          "port/baud before concluding anything about the bus.")
                elif peer.bench_fps is None:
                    print("[bench] VERDICT: receiver serial works but printed no BENCH "
                          "line — its firmware predates 0.1.159 (no RC35 parser).")
                elif peer.bench_fps == 0:
                    print("[bench] VERDICT: receiver counts ZERO frames while we transmit → "
                          "the frames never got onto the bus (or were never ACKed): "
                          "check the logger is powered, CANH/CANL, 120Ω termination, "
                          "common ground and 500k on BOTH nodes.")
                else:
                    print(f"[bench] VERDICT: receiver counted ~{peer.bench_fps} frames/s → "
                          "frames left the adapter AND were ACKed. Anything still missing "
                          "on the dash is downstream (per-item source setting / display).")
            elif bus.tx_ok == 0 and bus.tx_err == 0:
                print("[bench] VERDICT: unknown — re-run with --peer-port <logger port> so "
                      "the receiver's counter can prove the bus side.")
            if args.duration and abs(n / el - args.hz) / args.hz > 0.1:
                print(f"[bench] WARNING: achieved {n / el:.1f} Hz vs requested {args.hz:g} Hz "
                      f"— lower --hz, or raise --serial-baud if the adapter allows it")


def cmd_probe(args):
    import glob
    candidates = args.port and [args.port] or sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*"))
    if not candidates:
        return print("no /dev/ttyACM* or /dev/ttyUSB* found — is the CANable plugged in?")
    for port in candidates:
        for baud in (115200, 1000000, 921600, 57600):
            try:
                ser = serial.Serial(port, baud, timeout=0.3)
                time.sleep(0.2)
                ser.reset_input_buffer()
                ser.write(b"V\r")
                time.sleep(0.4)
                ver = ser.read(32)
                mac = b""
                ser.write(b"v\r")          # 'v' = hardware/firmware version string
                time.sleep(0.4)
                mac = ser.read(64)
                ser.close()
                if ver.strip():
                    print(f"{port}: slcan version {ver!r} at {baud} baud   extra={mac!r}")
                    break
            except Exception as exc:
                print(f"{port} @{baud}: {exc}")
        else:
            print(f"{port}: no slcan response at any tried baud rate")


def cmd_listen(args):
    bus = SLCAN(args.port, args.bitrate)
    print("[listen] Ctrl-C to stop. Anything on the bus is decoded below.\n")
    seen = {}
    t0 = time.time()
    last_report = t0
    try:
        while True:
            for msg_id, payload, ext, remote in bus.poll():
                seen[msg_id] = seen.get(msg_id, 0) + 1
                extra = decode_rc35(msg_id, payload) or decode_ms3(msg_id, payload)
                print(f"  {msg_id:04X}{'x' if ext else ' '} [{len(payload)}] {payload.hex().upper():<16}"
                      f" {extra}{'  (REMOTE)' if remote else ''}")
            time.sleep(0.002)          # poll() is non-blocking now; don't spin a core
            if args.duration and time.time() - t0 > args.duration:
                break
            now = time.time()
            if now - last_report >= 5.0:
                last_report = now
                rate = sum(seen.values()) / max(0.001, now - t0)
                print(f"\n--- {now-t0:5.1f}s: {sum(seen.values())} frames, {rate:6.1f} fps, "
                      f"ids={{{', '.join(f'{k:04X}:{v}' for k, v in sorted(seen.items()))}}}\n")
                if now - t0 > 3 and not seen:
                    print("    (bus silent — nothing is transmitting, or no ACK from any node)")
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()


def cmd_ms3(args):
    ALL_CH = ('rpm', 'clt', 'afr', 'iat', 'map', 'tps', 'batt', 'adv')
    sel = {c.strip().lower() for c in args.channels.split(',') if c.strip()}
    if 'all' in sel or not sel:
        sel = set(ALL_CH)
    bad = sel - set(ALL_CH)
    if bad:
        sys.exit("unknown channel(s): %s  (known: %s, all)"
                 % (', '.join(sorted(bad)), ', '.join(ALL_CH)))
    args.channels_set = sel
    bus = SLCAN(args.port, args.bitrate)
    print(f"[ms3] broadcasting fake MS3 Simplified Dash at {args.hz} Hz on 0x5E8..0x5EB"
          f"{' (static)' if args.static else ' (sweeping)'}")
    print(f"[ms3] animating: {', '.join(sorted(args.channels_set))}"
          f"  — every channel is on the bus; the rest transmit steady")
    if args.duration:
        print(f"[ms3] stopping after {args.duration}s")
    print("[ms3] Ctrl-C to stop\n")
    period = 1.0 / args.hz
    t0 = time.time()
    n = 0
    try:
        while True:
            t = time.time() - t0
            # Every channel is transmitted on every cycle so any monitor row the
            # driver enables holds a real number. `--channels` picks which ones
            # ANIMATE; the rest are held at a plausible steady value.
            moving = args.channels_set
            wave = {
                'rpm':  900 + 5500 * (0.5 + 0.5 * math.sin(t * 0.7)),
                'clt':  150 + 70 * (0.5 + 0.5 * math.sin(t * 0.13)),
                'tps':  50 * (0.5 + 0.5 * math.sin(t * 0.7)),
                'map':  30 + 70 * (0.5 + 0.5 * math.sin(t * 0.7)),
                'afr':  14.7 - 2.2 * (0.5 + 0.5 * math.sin(t * 0.31)),
                'batt': 13.8 - 0.6 * (0.5 + 0.5 * math.sin(t * 0.07)),
                'iat':  90 + 25 * (0.5 + 0.5 * math.sin(t * 0.05)),
                'adv':  12 + 14 * (0.5 + 0.5 * math.sin(t * 0.7)),
            }
            hold = {'rpm': 1800.0, 'clt': 190.0, 'tps': 20.0, 'map': 45.0,
                    'afr': 14.7, 'batt': 13.9, 'iat': 100.0, 'adv': 20.0}
            if args.static:
                v = {'rpm': args.rpm, 'clt': args.clt, 'afr': args.afr, 'tps': 25.0,
                     'map': 100.0, 'iat': 95.0, 'batt': 13.8, 'adv': 18.0}
            else:
                v = {k: (wave[k] if k in moving else hold[k]) for k in wave}
            for msg_id, payload in ms3_frames(v['rpm'], v['clt'], v['map'], v['tps'],
                                               v['iat'], v['afr'], v['batt'], v['adv']).items():
                bus.send(msg_id, payload)
            n += 1
            if n % args.hz == 0:
                el = time.time() - t0
                shown = '  '.join(f"{k.upper()} {v[k]:.1f}{'*' if k in moving else ''}"
                                  for k in ('rpm', 'clt', 'map', 'tps', 'iat', 'afr', 'batt', 'adv'))
                print(f"  t={el:6.1f}s  {shown}")
            for msg_id, payload, ext, remote in bus.poll():
                extra = decode_ms3(msg_id, payload)
                print(f"  [rx] {msg_id:04X} [{len(payload)}] {payload.hex().upper()} {extra}")
            if args.duration and time.time() - t0 > args.duration:
                break
            nxt = t0 + n * period
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
        el = time.time() - t0
        print(f"\n[ms3] sent {n} broadcast cycles in {el:.1f}s")


def cmd_coolant(args):
    """Emulate MS3Pro coolant temp: warm-up ramp with hot/cold holds."""
    bus = SLCAN(args.port, args.bitrate)
    print(f"[coolant] emulating MS3Pro coolant on 0x5E8 bytes 4-5 (degF x10, big-endian)")
    print(f"[coolant] {args.start:.0f}F -> {args.end:.0f}F over {args.ramp:.0f}s, "
          f"{args.hold:.0f}s holds, at {args.hz:.0f} Hz")
    if args.duration:
        print(f"[coolant] stopping after {args.duration}s")
    print("[coolant] Ctrl-C to stop\n")
    period = 1.0 / args.hz
    t0 = time.time()
    n = 0
    last_phase = None
    last_print = 0.0
    try:
        while True:
            t = time.time() - t0
            clt, frames, phase = coolant_frames(t, args.start, args.end, args.ramp, args.hold)
            if args.afr:
                a, aph = afr_wave(t)
                frames = set_afr(frames, a)
                phase = f"{phase} | AFR {a:.2f} ({aph})"
            if args.iat:
                i, iph = iat_wave(t)
                frames = set_iat(frames, i)
                phase = f"{phase} | IAT {i:.0f}F ({iph})"
            for msg_id, payload in frames.items():
                bus.send(msg_id, payload)
            n += 1
            now = time.time()
            if phase != last_phase or now - last_print >= 1.0:
                last_print = now
                raw = frames[0x5E8][4:6].hex().upper()
                print(f"  t={t:6.1f}s  CLT {clt:6.1f} F  (0x5E8[4:6]=0x{raw} = "
                      f"{int(raw,16)} x10)   {phase}")
                last_phase = phase
            for msg_id, payload, ext, remote in bus.poll():
                extra = decode_ms3(msg_id, payload)
                print(f"  [rx] {msg_id:04X} [{len(payload)}] {payload.hex().upper()} {extra}")
            if args.duration and now - t0 > args.duration:
                break
            nxt = t0 + n * period
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
        print(f"\n[coolant] sent {n} broadcast cycles in {time.time()-t0:.1f}s")


def cmd_afr(args):
    """Emulate MS3Pro AFR (0x5EA byte 1, single byte = AFR x10)."""
    bus = SLCAN(args.port, args.bitrate)
    print("[afr] emulating MS3Pro AFR on 0x5EA byte 1 (single byte, AFR x10: 147 = 14.7)")
    print(f"[afr] WOT-rich {args.rich:.1f} -> lean cruise {args.lean:.1f} -> "
          f"DFCO {args.dfco:.1f}, coolant held at {args.clt:.0f}F, at {args.hz:.0f} Hz")
    if args.duration:
        print(f"[afr] stopping after {args.duration}s")
    print("[afr] Ctrl-C to stop\n")
    period = 1.0 / args.hz
    t0 = time.time()
    n = 0
    last_phase, last_print = None, 0.0
    try:
        while True:
            t = time.time() - t0
            afr, phase = afr_wave(t, args.rich, 14.7, args.lean, args.dfco)
            frames = ms3_frames(rpm=2200, clt_f=args.clt, map_kpa=45.0, tps_pct=30.0,
                                iat_f=95.0, afr=afr, batt_v=13.9, adv_deg=22.0)
            for msg_id, payload in frames.items():
                bus.send(msg_id, payload)
            n += 1
            now = time.time()
            if phase != last_phase or now - last_print >= 1.0:
                last_print = now
                b = frames[0x5EA][1]
                print(f"  t={t:6.1f}s  AFR {afr:6.2f}  (0x5EA[1]=0x{b:02X} = {b} x10)   {phase}")
                last_phase = phase
            for msg_id, payload, ext, remote in bus.poll():
                extra = decode_ms3(msg_id, payload)
                print(f"  [rx] {msg_id:04X} [{len(payload)}] {payload.hex().upper()} {extra}")
            if args.duration and now - t0 > args.duration:
                break
            nxt = t0 + n * period
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
        print(f"\n[afr] sent {n} broadcast cycles in {time.time()-t0:.1f}s")


def cmd_iat(args):
    """Emulate MS3Pro intake air temp: heat-soak rise and cool-down."""
    bus = SLCAN(args.port, args.bitrate)
    print("[iat] emulating MS3Pro IAT on 0x5E9 bytes 4-5 (int16, degF x10, big-endian)")
    print(f"[iat] {args.ambient:.0f}F -> {args.hot:.0f}F soak over {args.ramp:.0f}s, "
          f"{args.hold:.0f}s holds, at {args.hz:.0f} Hz")
    if args.duration:
        print(f"[iat] stopping after {args.duration}s")
    print("[iat] Ctrl-C to stop\n")
    period = 1.0 / args.hz
    t0 = time.time()
    n = 0
    last_phase, last_print = None, 0.0
    try:
        while True:
            t = time.time() - t0
            iat, phase = iat_wave(t, args.ambient, args.hot, args.ramp, args.hold)
            frames = ms3_frames(rpm=2200, clt_f=195.0, map_kpa=45.0, tps_pct=25.0,
                                iat_f=iat, afr=14.1, batt_v=13.9, adv_deg=24.0)
            for msg_id, payload in frames.items():
                bus.send(msg_id, payload)
            n += 1
            now = time.time()
            if phase != last_phase or now - last_print >= 1.0:
                last_print = now
                raw = frames[0x5E9][4:6].hex().upper()
                print(f"  t={t:6.1f}s  IAT {iat:6.1f} F  (0x5E9[4:6]=0x{raw} = "
                      f"{int(raw,16)} x10)   {phase}")
                last_phase = phase
            for msg_id, payload, ext, remote in bus.poll():
                extra = decode_ms3(msg_id, payload)
                print(f"  [rx] {msg_id:04X} [{len(payload)}] {payload.hex().upper()} {extra}")
            if args.duration and now - t0 > args.duration:
                break
            nxt = t0 + n * period
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
    except KeyboardInterrupt:
        pass
    finally:
        bus.close()
        print(f"\n[iat] sent {n} broadcast cycles in {time.time()-t0:.1f}s")


def cmd_send(args):
    bus = SLCAN(args.port, args.bitrate)
    payload = bytes.fromhex(args.data)
    print(f"[send] id=0x{args.id:03X} data={payload.hex().upper()}")
    for _ in range(args.count):
        bus.send(args.id, payload)
        time.sleep(args.interval)
    time.sleep(0.5)
    got = list(bus.poll())
    if got:
        print("[send] frames seen while sending:")
        for msg_id, pl, ext, remote in got:
            print(f"   {msg_id:04X} [{len(pl)}] {pl.hex().upper()}")
    else:
        print("[send] nothing seen on the bus while sending — if this was a transmit test,")
        print("       no ACK came back (no other node, wrong bitrate, or wiring).")
    bus.close()


def main():
    p = argparse.ArgumentParser(
        description="Racecar-35 CANable slcan tool — RC35 bench frames (fake TEMP/OIL/VOLT/AFR/IAT/MAP/TPS) "
                    "or bus listen/decode")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-p", "--port", default="/dev/ttyACM0")
        sp.add_argument("-b", "--bitrate", default="500k", choices=sorted(BITRATE_CODE))

    sp = sub.add_parser("bench", help="fake data for the bench: RC35 frames with every channel, fast + steady")
    common(sp)
    sp.add_argument("--hz", type=float, default=200.0,
                    help="cycles/s; 2 frames per cycle, so 200 = 400 frames/s (default 200)")
    sp.add_argument("--serial-baud", type=int, default=115200,
                    help="USB serial baud to the adapter (default 115200; USB CDC usually "
                         "ignores it and runs at USB speed — measure with --autotune)")
    sp.add_argument("--core-id", type=lambda x: int(x, 0), default=RC35_CORE_ID,
                    help="frame id for the CORE frame (default 0x700)")
    sp.add_argument("--aux-id", type=lambda x: int(x, 0), default=RC35_AUX_ID,
                    help="frame id for the AUX frame (default 0x701)")
    sp.add_argument("--autotune", action="store_true",
                    help="ramp the rate and MEASURE the sustainable ceiling, then exit")
    sp.add_argument("--peer-port", default=None,
                    help="the RECEIVER's serial port (the logger). Its 1 Hz "
                         "'BENCH frames/s=' / CANDIAG counters are cross-checked "
                         "against what we transmit, which is the ONLY way to prove "
                         "a TX/ACK fault with a firmware that reports nothing")
    sp.add_argument("--peer-baud", type=int, default=115200,
                    help="baud for --peer-port (default 115200)")
    sp.add_argument("--max-load", type=float, default=60.0,
                    help="stop ramping at this %% bus load (default 60; a saturated bus "
                         "starves the receiver and looks like a slow display)")
    sp.add_argument("--profile", default="sweep", choices=("sweep", "steady", "pull", "chop"),
                    help="sweep = one slow sine (default); steady = fixed --rpm; "
                         "pull = gear-by-gear WOT; chop = hard steps on every channel "
                         "(use this to SEE the display refresh rate)")
    sp.add_argument("--rpm", type=float, default=3000.0, help="RPM for --profile steady")
    sp.add_argument("--duration", type=float, default=0)
    sp.add_argument("--dry-run", action="store_true",
                    help="print the encoded frames + their decode, touch no hardware")
    sp.set_defaults(func=cmd_bench)

    sp = sub.add_parser("probe", help="find the slcan adapter and print its version")
    sp.add_argument("-p", "--port", default=None)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("listen", help="decode everything on the bus")
    common(sp)
    sp.add_argument("-d", "--duration", type=float, default=0)
    sp.set_defaults(func=cmd_listen)

    sp = sub.add_parser("ms3", help="compatibility: broadcast real MS3Pro Simplified Dash frames")
    common(sp)
    sp.add_argument("--hz", type=float, default=50.0)
    sp.add_argument("--duration", type=float, default=0)
    sp.add_argument("--static", action="store_true", help="hold constant values instead of sweeping")
    sp.add_argument("--channels", default="all",
                    help="comma list to ANIMATE: rpm,clt,afr,iat,map,tps,batt,adv (default all; others still transmit)")
    sp.add_argument("--rpm", type=float, default=3000)
    sp.add_argument("--clt", type=float, default=185)
    sp.add_argument("--afr", type=float, default=14.7)
    sp.set_defaults(func=cmd_ms3)

    sp = sub.add_parser("coolant", help="emulate MS3Pro coolant temp (warm-up / cool-down cycles)")
    common(sp)
    sp.add_argument("--start", type=float, default=60.0, help="cold coolant temp, degF")
    sp.add_argument("--end", type=float, default=235.0, help="hot coolant temp, degF (default is past the dash warning)")
    sp.add_argument("--ramp", type=float, default=45.0, help="seconds to ramp between the two")
    sp.add_argument("--hold", type=float, default=10.0, help="seconds to hold at each end")
    sp.add_argument("--hz", type=float, default=50.0)
    sp.add_argument("--duration", type=float, default=0)
    sp.add_argument("--afr", action="store_true", help="also drive the AFR channel (0x5EA byte 1)")
    sp.add_argument("--iat", action="store_true", help="also drive the IAT channel (0x5E9 bytes 4-5)")
    sp.set_defaults(func=cmd_coolant)

    sp = sub.add_parser("iat", help="emulate MS3Pro intake air temp (0x5E9 bytes 4-5) heat soak")
    common(sp)
    sp.add_argument("--ambient", type=float, default=70.0, help="cold/ambient IAT, degF")
    sp.add_argument("--hot", type=float, default=155.0, help="heat-soaked IAT, degF")
    sp.add_argument("--ramp", type=float, default=60.0, help="seconds for the soak (and the cool-down)")
    sp.add_argument("--hold", type=float, default=12.0, help="seconds held at each end")
    sp.add_argument("--hz", type=float, default=50.0)
    sp.add_argument("--duration", type=float, default=0)
    sp.set_defaults(func=cmd_iat)

    sp = sub.add_parser("afr", help="emulate MS3Pro AFR (0x5EA byte 1) with rich/lean excursions")
    common(sp)
    sp.add_argument("--rich", type=float, default=10.8, help="WOT rich AFR")
    sp.add_argument("--lean", type=float, default=17.6, help="lean-cruise AFR")
    sp.add_argument("--dfco", type=float, default=19.4, help="overrun/decel-cut AFR spike")
    sp.add_argument("--clt", type=float, default=185.0, help="coolant temp to report alongside")
    sp.add_argument("--hz", type=float, default=50.0)
    sp.add_argument("--duration", type=float, default=0)
    sp.set_defaults(func=cmd_afr)

    sp = sub.add_parser("send", help="send one raw frame repeatedly")
    common(sp)
    sp.add_argument("--id", type=lambda x: int(x, 0), default=BASE_ID)
    sp.add_argument("--data", default="0300000000000000")
    sp.add_argument("--count", type=int, default=10)
    sp.add_argument("--interval", type=float, default=0.1)
    sp.set_defaults(func=cmd_send)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

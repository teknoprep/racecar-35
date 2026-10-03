#!/usr/bin/env python3
"""One-shot CAN bench test for the Racecar-35 data logger.

Answers the only question that matters right now: **does the Teensy's CAN receiver
actually see frames on the bus?** Two phases:

  1. PASSIVE — listen on the CANable for a few seconds. Tells us whether anything
     (MS3 or otherwise) is transmitting at all, and whether frames are being ACKed.
  2. INJECT — broadcast RC35 bench frames (0x700/0x701 @100 Hz: rpm/temp/oil/volt/
     afr/iat/map/tps) with the CANable while reading the Teensy's USB serial for
     its 1 Hz CANDIAG + BENCH lines. `--frames ms3` injects MS3Pro Simplified Dash
     (0x5E8..0x5EB) instead, for working with REAL MS3 framing.

Verdict rules (CANDIAG frames/s over the last second):
  * frames/s >= 50 and base_hits > 0   -> the logger's CAN RX works.
  * some frames, then 0, ACK_ERR/state  -> the node stopped ACKing or bus-off'd.
  * frames/s == 0 the whole time        -> war/baud/pins, or a latched bus-off.

Usage:
    python3 tools/can_bench_test.py                     # auto-detect both ports
    python3 tools/can_bench_test.py --can /dev/ttyACM0 --teensy /dev/ttyACM1
    python3 tools/can_bench_test.py --seconds 30 --no-inject    # passive only
"""

import argparse
import glob
import re
import sys
import threading
import time

sys.path.insert(0, __file__.rsplit("/", 1)[0])
from can_sim import (SLCAN, ms3_frames, coolant_frames, afr_wave, set_afr,   # noqa: E402
                     iat_wave, set_iat, bench_values, rc35_frames,
                     decode_rc35, RC35_CORE_ID, RC35_AUX_ID)

try:
    import serial
except ImportError:
    sys.exit("pyserial missing")


# ---------------------------------------------------------------------------
def identify_ports():
    """Return (canable, others). The CANable answers 'V\\r' with a version banner."""
    canable, others = None, []
    for port in sorted(glob.glob("/dev/ttyACM*") + glob.glob("/dev/ttyUSB*")):
        try:
            ser = serial.Serial(port, 115200, timeout=0.3)
            time.sleep(0.25)
            ser.reset_input_buffer()
            ser.write(b"V\r")
            time.sleep(0.4)
            resp = ser.read(96)
            ser.close()
        except Exception:
            continue
        if b"Board:" in resp or b"Slcan" in resp or b"slcan" in resp.lower():
            canable = port
            print(f"[port] {port} = CANable (slcan adapter)")
        else:
            others.append(port)
            print(f"[port] {port} = candidate data logger (Teensy)")
    return canable, others


CANDIAG = re.compile(
    rb"CANDIAG,(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")


class TeensyReader(threading.Thread):
    """Reads the Teensy's USB serial and keeps the latest CANDIAG values."""

    def __init__(self, port, verbose=True):
        super().__init__(daemon=True)
        self.port = port
        self.verbose = verbose
        self.ser = None
        self.samples = []          # (t, frames_s, total, base_hits, dup_pct, ack_err, txerr, rxerr)
        self.other_lines = []
        self._stop = False
        self.dropped = 0           # bytes of non-CANDIAG traffic noticed

    def run(self):
        try:
            self.ser = serial.Serial(self.port, 115200, timeout=0.2)
        except Exception as exc:
            print(f"[teensy] cannot open {self.port}: {exc}")
            return
        buf = b""
        while not self._stop:
            try:
                data = self.ser.read(512)
            except Exception:
                break
            if not data:
                continue
            buf += data
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                m = CANDIAG.search(line)
                if m:
                    vals = [int(x) for x in m.groups()]
                    self.samples.append((time.time(),) + tuple(vals))
                    if self.verbose:
                        print(f"[teensy] frames/s={vals[0]:<4} total={vals[1]:<7} "
                              f"base_hits={vals[2]:<4} dup={vals[3]}% ACK_ERR={vals[4]} "
                              f"TXerr={vals[5]} RXerr={vals[6]}")
                else:
                    t = line.strip()
                    if t:
                        self.other_lines.append(t)
                        if self.verbose and not t.startswith((b"GPS,", b"ENG,", b"ECU,",
                                                              b"IMU,", b"HLTH,", b"TIME,",
                                                              b"SD,", b"CLD,")):
                            print(f"[teensy] {t[:140].decode(errors='replace')}")
        try:
            self.ser.close()
        except Exception:
            pass

    def stop(self):
        self._stop = True


# ---------------------------------------------------------------------------
def phase_passive(can_port, bitrate, seconds):
    print(f"\n=== PHASE 1: passive listen on {can_port} for {seconds}s ===")
    print("(is anything on this bus talking at all?)")
    bus = SLCAN(can_port, bitrate, verbose=False)
    counts = {}
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            for msg_id, payload, ext, remote in bus.poll():
                counts[msg_id] = counts.get(msg_id, 0) + 1
            time.sleep(0.02)
    finally:
        bus.close()
    total = sum(counts.values())
    if not total:
        print("  -> BUS IS SILENT: no node transmitted during the listen window.")
        print("     (expected if nothing but the CANable is attached; if the MS3 was")
        print("      powered and broadcasting, this means nothing reaches this board.)")
    else:
        print(f"  -> {total} frames, {total/seconds:.1f} fps, ids: "
              f"{{{', '.join(f'0x{k:03X}:{v}' for k, v in sorted(counts.items()))}}}")
        for i in sorted(counts):
            if i in (0x5E8, 0x5E9, 0x5EA, 0x5EB):
                print(f"     0x{i:03X} is an MS3 Simplified-Dash id -> the logger should see it")
    return total, counts


def phase_inject(can_port, bitrate, seconds, teensy, wave='sweep', frames='rc35'):
    print(f"\n=== PHASE 2: injecting {frames.upper()} bench frames for {seconds}s ===")
    print("(the Teensy's CANDIAG / BENCH lines are printed below as they arrive)")
    bus = SLCAN(can_port, bitrate, verbose=False)
    stop = threading.Event()

    def broadcaster():
        t0 = time.time()
        n = 0
        period = 1.0 / 50.0
        while not stop.is_set():
            t = time.time() - t0
            import math
            if frames == 'rc35':
                # The fake-data path: every channel, every cycle, no MS3 framing.
                bus.send_many(rc35_frames(**bench_values(t, 'sweep'), seq=n & 0xFF))
            elif wave in ('coolant', 'afr', 'iat', 'both', 'all'):
                clt, fr, phase = coolant_frames(t)
                if wave in ('afr', 'both', 'all'):
                    a, _ = afr_wave(t)
                    fr = set_afr(fr, a)
                if wave in ('iat', 'all'):
                    i, _ = iat_wave(t)
                    fr = set_iat(fr, i)
                for mid, payload in fr.items():
                    bus.send(mid, payload)
            else:
                rpm = 900 + 5500 * (0.5 + 0.5 * math.sin(t * 0.7))
                clt = 150 + 70 * (0.5 + 0.5 * math.sin(t * 0.13))
                tps = 50 * (0.5 + 0.5 * math.sin(t * 0.7))
                afr = 14.7 - 2.2 * (0.5 + 0.5 * math.sin(t * 0.31))
                for mid, payload in ms3_frames(rpm, clt, 30 + 70 * (0.5 + 0.5 * math.sin(t * 0.7)),
                                                tps, 95, afr, 13.8, 18).items():
                    bus.send(mid, payload)
            n += 1
            # watch what (if anything) comes back — poll() never blocks (see can_sim)
            for msg_id, payload, ext, remote in bus.poll():
                print(f"  [bus rx] 0x{msg_id:03X} {payload.hex().upper()}")
            nxt = t0 + n * period
            d = nxt - time.time()
            if d > 0:
                time.sleep(d)
        return n

    th = threading.Thread(target=broadcaster, daemon=True)
    th.start()
    t0 = time.time()
    try:
        while time.time() - t0 < seconds:
            time.sleep(0.2)
    finally:
        stop.set()
        th.join(timeout=2)
        bus.close()
    return


def verdict(teensy):
    samples = [s for s in teensy.samples if True]
    if not samples:
        print("\n=== VERDICT: NO CANDIAG SEEN ===")
        print("The Teensy never reported CAN health. Either it is not connected/running the")
        print("dash firmware, or its USB serial is on a different port than assumed.")
        print(f"  other lines seen: {len(teensy.other_lines)}")
        for l in teensy.other_lines[-5:]:
            print(f"    {l[:120].decode(errors='replace')}")
        return
    # look at the injection window (the last samples)
    fps = [s[1] for s in samples]
    base = [s[3] for s in samples]
    ack = [s[5] for s in samples]
    txerr = [s[6] for s in samples]
    rxerr = [s[7] for s in samples]
    best, worst = max(fps), min(fps)
    print("\n=== VERDICT ===")
    print(f"  CANDIAG samples: {len(samples)}   frames/s min={worst} max={best}")
    print(f"  base_hits max={max(base)}  ACK_ERR max={max(ack)}  "
          f"TXerr max={max(txerr)}  RXerr max={max(rxerr)}")
    if max(base) > 0 and best >= 50:
        print("  => THE LOGGER'S CAN RECEIVE WORKS. Frames are arriving and parsing.")
        print("     If the car still shows nothing, the problem is on the MS3/wiring side")
        print("     (not broadcasting Simplified Dash, wrong base id, or query mode).")
    elif best > 0 and fps[-1] == 0:
        print("  => RECEIVED A BURST THEN STOPPED. This is the classic latched bus-off /")
        print("     lost-ACK signature: the controller died and has no recovery path.")
    elif max(ack) > 0 or max(txerr) > 0:
        print("  => ERRORS BUT NO FRAMES. ACK errors on the bus: our node is not being")
        print("     ACKed, or is not ACKing. Check the transceiver, ground and termination.")
    else:
        print("  => NOTHING RECEIVED AND NO ERRORS. Wiring/baud/pin path, or the controller")
        print("     is already bus-off from an earlier event (it never recovers without a reboot).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--can", default=None, help="CANable port (auto-detected)")
    ap.add_argument("--teensy", default=None, help="Teensy/data-logger port (auto-detected)")
    ap.add_argument("--bitrate", default="500k")
    ap.add_argument("--seconds", type=float, default=20.0)
    ap.add_argument("--passive-only", action="store_true")
    ap.add_argument("--no-inject", action="store_true")
    ap.add_argument("--wave", default="sweep", choices=("sweep", "coolant", "afr", "iat", "both", "all"),
                    help="what to inject: general sweep, or the MS3Pro coolant warm-up")
    ap.add_argument("--frames", default="rc35", choices=("rc35", "ms3"),
                    help="rc35 = RC35 bench frames 0x700/0x701 (default, every channel incl. OIL); "
                         "ms3 = MS3Pro Simplified Dash 0x5E8..0x5EB")
    a = ap.parse_args()

    canable, others = identify_ports()
    can_port = a.can or canable
    if not can_port:
        return print("No CANable found (nothing answered the slcan 'V' command).")
    teensy_port = a.teensy or (others[0] if others else None)

    total, counts = phase_passive(can_port, a.bitrate, 5.0)
    if a.passive_only:
        return

    teensy = None
    if teensy_port and not a.no_inject:
        teensy = TeensyReader(teensy_port)
        teensy.start()
        time.sleep(1.0)          # let it settle + see a baseline CANDIAG
    elif not a.no_inject:
        print("\n[warn] no Teensy port detected — running injection without reading CANDIAG")

    phase_inject(can_port, a.bitrate, a.seconds, teensy, a.wave, a.frames)

    if teensy:
        time.sleep(1.5)
        teensy.stop()
        teensy.join(timeout=2)
        verdict(teensy)
    print("\ndone.")


if __name__ == "__main__":
    main()

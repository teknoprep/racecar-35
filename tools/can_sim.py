#!/usr/bin/env python3
"""Racecar-35 bench CAN tool.

Drives a **CANable (slcan firmware)** over USB serial to either:

  * broadcast fake **MS3Pro "Simplified Dash Broadcasting"** frames at 50 Hz, so the
    Teensy data logger can be tested on the bench with no engine running, or
  * listen to and decode whatever is really on a CAN bus (including a live MS3).

The Teensy firmware reads MS3 Simplified Dash on CAN1 (500 kbit/s) at base id
**0x5E8** (1512). Byte layout (see src/main.cpp pumpCAN() and the MegaSquirt CAN
Broadcast spec):

    0x5E8  [0:2] map x10 kPa   [2:4] rpm   [4:6] clt x10 F   [6:8] tps x10 %
    0x5E9  [4:6] mat/IAT x10 F
    0x5EA  [0] afrtgt1  [1] AFR1 (single byte, x10: 147 = 14.7)
    0x5EB  [0:2] battery x10 V

Examples
--------
    python3 tools/can_sim.py probe                     # find the slcan adapter + its version
    python3 tools/can_sim.py listen -p /dev/ttyACM0    # decode everything on the bus
    python3 tools/can_sim.py ms3    -p /dev/ttyACM0    # fake MS3 broadcast, values sweeping
    python3 tools/can_sim.py ms3    -p /dev/ttyACM0 --rpm 6500 --clt 210 --afr 12.8 --static
    python3 tools/can_sim.py send   -p /dev/ttyACM0 --id 0x5E8 --data 0300000000000000

slcan bitrate code: S0=10k S1=20k S2=50k S3=100k S4=125k S5=250k S6=500k S7=800k S8=1M
"""

import argparse
import math
import struct
import sys
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


class SLCAN:
    def __init__(self, port, bitrate="500k", serial_baud=115200, verbose=True):
        self.verbose = verbose
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

    def poll(self):
        """Yield every complete frame received since the last call."""
        data = self.ser.read(4096)
        if data:
            self.buf += data
        while b"\r" in self.buf:
            line, self.buf = self.buf.split(b"\r", 1)
            line = line.strip()
            if not line:
                continue
            ch = line[:1]
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
                elif line.startswith(b"\x07"):
                    print("[slcan] adapter reported an ERROR (likely no ACK on the bus)")
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
                extra = decode_ms3(msg_id, payload)
                print(f"  {msg_id:04X}{'x' if ext else ' '} [{len(payload)}] {payload.hex().upper():<16}"
                      f" {extra}{'  (REMOTE)' if remote else ''}")
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
    bus = SLCAN(args.port, args.bitrate)
    print(f"[ms3] broadcasting fake MS3 Simplified Dash at {args.hz} Hz on 0x5E8..0x5EB"
          f"{' (static)' if args.static else ' (sweeping)'}")
    if args.duration:
        print(f"[ms3] stopping after {args.duration}s")
    print("[ms3] Ctrl-C to stop\n")
    period = 1.0 / args.hz
    t0 = time.time()
    n = 0
    try:
        while True:
            t = time.time() - t0
            if args.static:
                rpm, clt, tps, afr, batt, iat, mapk, adv = (
                    args.rpm, args.clt, 25.0, args.afr, 13.8, 95.0, 100.0, 18.0)
            else:
                # a slow, obviously-varying sweep so the dash shows movement
                rpm = 900 + 5500 * (0.5 + 0.5 * math.sin(t * 0.7))
                clt = 150 + 70 * (0.5 + 0.5 * math.sin(t * 0.13))
                tps = 50 * (0.5 + 0.5 * math.sin(t * 0.7))
                mapk = 30 + 70 * (0.5 + 0.5 * math.sin(t * 0.7))
                afr = 14.7 - 2.2 * (0.5 + 0.5 * math.sin(t * 0.31))
                batt = 13.8 - 0.6 * (0.5 + 0.5 * math.sin(t * 0.07))
                iat = 90 + 25 * (0.5 + 0.5 * math.sin(t * 0.05))
                adv = 12 + 14 * (0.5 + 0.5 * math.sin(t * 0.7))
            for msg_id, payload in ms3_frames(rpm, clt, mapk, tps, iat, afr, batt, adv).items():
                bus.send(msg_id, payload)
            n += 1
            if n % args.hz == 0:
                el = time.time() - t0
                print(f"  t={el:6.1f}s  RPM {rpm:5.0f}  CLT {clt:5.1f}F  MAP {mapk:5.1f}kPa  "
                      f"TPS {tps:4.1f}%  IAT {iat:5.1f}F  AFR {afr:5.2f}  BATT {batt:4.1f}V")
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
    p = argparse.ArgumentParser(description="Racecar-35 CANable slcan tool (fake MS3 broadcast / bus listener)")
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp):
        sp.add_argument("-p", "--port", default="/dev/ttyACM0")
        sp.add_argument("-b", "--bitrate", default="500k", choices=sorted(BITRATE_CODE))

    sp = sub.add_parser("probe", help="find the slcan adapter and print its version")
    sp.add_argument("-p", "--port", default=None)
    sp.set_defaults(func=cmd_probe)

    sp = sub.add_parser("listen", help="decode everything on the bus")
    common(sp)
    sp.add_argument("-d", "--duration", type=float, default=0)
    sp.set_defaults(func=cmd_listen)

    sp = sub.add_parser("ms3", help="broadcast fake MS3 Simplified Dash frames")
    common(sp)
    sp.add_argument("--hz", type=float, default=50.0)
    sp.add_argument("--duration", type=float, default=0)
    sp.add_argument("--static", action="store_true", help="hold constant values instead of sweeping")
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

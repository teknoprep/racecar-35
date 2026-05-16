#!/usr/bin/env python3
"""Push a local Teensy Intel HEX image into racecar-35 FlasherX over USB CDC.

Requires Teensy firmware that understands the developer command:

    USBFWUPDATE

Protocol:
  host -> Teensy: USBFWUPDATE\n
  Teensy -> host: FW,READY,<buffer_size>\n
  For each Intel HEX line:
    host -> Teensy: :...\n
    Teensy -> host: A\n                    (or FW,ERR,<reason>\n)

  After EOF and target checks:
    Teensy -> host: FW,COMMITTING\n
Then flash_move() reboots the Teensy.

This bypasses GitHub, CrowPanel WiFi, and the CrowPanel->Teensy UART bridge.
It is intended for fast FlasherX/protocol testing from the development laptop.
"""

from __future__ import annotations

import argparse
import pathlib
import re
import sys
import time
from typing import Optional

import serial


def read_line(ser: serial.Serial, timeout: float) -> Optional[str]:
    end = time.monotonic() + timeout
    buf = bytearray()
    while time.monotonic() < end:
        try:
            b = ser.read(1)
        except (serial.SerialException, OSError):
            raise
        if not b:
            continue
        c = b[0]
        if c == 13:  # \r
            continue
        if c == 10:  # \n
            return buf.decode(errors="replace")
        buf.append(c)
        if len(buf) > 512:
            s = buf.decode(errors="replace")
            buf.clear()
            return s
    if buf:
        return buf.decode(errors="replace")
    return None


def wait_for_prefix(ser: serial.Serial, prefix: str, timeout: float) -> str:
    end = time.monotonic() + timeout
    last = ""
    while time.monotonic() < end:
        line = read_line(ser, min(0.5, max(0.01, end - time.monotonic())))
        if line is None:
            continue
        if line:
            print(f"<- {line}")
            last = line
        if line.startswith(prefix):
            return line
        if line.startswith("FW,ERR"):
            raise RuntimeError(line)
    raise TimeoutError(f"timeout waiting for {prefix!r}; last={last!r}")


def parse_hex_version(hex_path: pathlib.Path) -> Optional[str]:
    # Cheap Intel HEX decoder sufficient for searching printable strings.
    base = 0
    mem = bytearray()
    for raw in hex_path.read_text(errors="replace").splitlines():
        if not raw.startswith(":") or len(raw) < 11:
            continue
        try:
            n = int(raw[1:3], 16)
            addr = int(raw[3:7], 16)
            typ = int(raw[7:9], 16)
            data = bytes.fromhex(raw[9 : 9 + n * 2])
        except ValueError:
            continue
        if typ == 0:
            a = base + addr
            e = a + n
            if e > len(mem):
                mem.extend(b"\xff" * (e - len(mem)))
            mem[a:e] = data
        elif typ == 1:
            break
        elif typ == 4 and len(data) == 2:
            base = int.from_bytes(data, "big") << 16
        elif typ == 2 and len(data) == 2:
            base = int.from_bytes(data, "big") << 4
    blob = bytes(mem)
    m = re.search(rb"firmware v([0-9]+\.[0-9]+\.[0-9]+)", blob)
    if m:
        return m.group(1).decode()
    # fallback: first semver-looking string near our firmware text
    m = re.search(rb"\b([0-9]+\.[0-9]+\.[0-9]+)\b", blob)
    return m.group(1).decode() if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("hex", nargs="?", default=".pio/build/teensy41/firmware.hex",
                    help="Intel HEX to send (default: .pio/build/teensy41/firmware.hex)")
    ap.add_argument("--port", default="/dev/ttyACM0", help="Teensy USB CDC port")
    ap.add_argument("--baud", type=int, default=115200, help="USB CDC nominal baud")
    ap.add_argument("--ready-timeout", type=float, default=8.0)
    ap.add_argument("--ack-timeout", type=float, default=5.0)
    ap.add_argument("--commit-timeout", type=float, default=20.0)
    ap.add_argument("--no-verify-version", action="store_true")
    args = ap.parse_args()

    hex_path = pathlib.Path(args.hex)
    if not hex_path.exists():
        raise SystemExit(f"HEX not found: {hex_path}")
    lines = [ln.strip() for ln in hex_path.read_text(errors="replace").splitlines() if ln.strip()]
    if not lines or not lines[-1].startswith(":00000001"):
        raise SystemExit("HEX does not appear to end with Intel HEX EOF record")

    expected_ver = parse_hex_version(hex_path)
    print(f"HEX: {hex_path}  lines={len(lines)}  expected_ver={expected_ver or '?'}")
    print(f"Opening {args.port}...")
    ser = serial.Serial(args.port, args.baud, timeout=0.05, write_timeout=5)
    time.sleep(0.2)
    ser.reset_input_buffer()

    # Show current version if available.
    ser.write(b"VER?\n")
    ser.flush()
    try:
        line = wait_for_prefix(ser, "VER,teensy,", 2.0)
        print(f"Current: {line}")
    except Exception as e:
        print(f"Current version query skipped/failed: {e}")

    print("Starting USBFWUPDATE...")
    ser.reset_input_buffer()
    ser.write(b"USBFWUPDATE\n")
    ser.flush()
    ready = wait_for_prefix(ser, "FW,READY", args.ready_timeout)
    print(f"Ready: {ready}")

    t0 = time.monotonic()
    last_print = t0
    for idx, line in enumerate(lines, 1):
        ser.write(line.encode("ascii") + b"\n")
        ser.flush()
        while True:
            resp = read_line(ser, args.ack_timeout)
            if resp is None:
                raise TimeoutError(f"ACK timeout at line {idx}/{len(lines)}")
            if resp == "A":
                break
            if resp.startswith("FW,ERR"):
                raise RuntimeError(resp)
            # USB diagnostics share the same stream; print and keep waiting.
            if resp:
                print(f"<- {resp}")
        now = time.monotonic()
        if now - last_print >= 1.0 or idx == len(lines):
            pct = idx * 100.0 / len(lines)
            print(f"sent {idx:5d}/{len(lines)} lines  {pct:5.1f}%")
            last_print = now

    print("HEX EOF ACKed; waiting for FW,COMMITTING...")
    try:
        commit = wait_for_prefix(ser, "FW,COMMITTING", args.commit_timeout)
        print(f"{commit}")
    except (serial.SerialException, OSError) as e:
        print(f"Serial disconnected during commit/reboot: {e}")
    except TimeoutError as e:
        print(f"WARNING: {e}")

    elapsed = time.monotonic() - t0
    print(f"Transfer phase elapsed: {elapsed:.1f}s")
    print("Teensy should reboot now. If needed, verify on STATUS or with USB serial after reconnect.")
    ser.close()
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(130)

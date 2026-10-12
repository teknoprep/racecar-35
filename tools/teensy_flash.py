#!/usr/bin/env python3
"""Flash a Teensy 4.1 from Linux, including a factory-fresh (RawHID) unit.

`teensy_loader_cli` can only soft-reboot a Teensy that is already running
*Teensyduino Serial* firmware (VID:PID 16c0:0483) - its `-s` path hard-codes that
PID. A brand-new Teensy, or any board flashed with USB Type = Raw HID, enumerates
as 16c0:0486 (HID + SEREMU) and the loader just prints

    Error opening USB device: No error
    Waiting for Teensy device...
     (hint: press the reset button)

This script fills that gap: it sends the SEREMU feature report the Teensy core
handles in `endpoint0_complete()` (usb.c: wValue 0x0300, wLength 4, data
A9 45 C2 6B) through hidraw, waits for the HalfKay bootloader (16c0:0478) to
appear, then runs teensy_loader_cli. No button press needed.

Usage: tools/teensy_flash.py [firmware.hex]
       (default: .pio/build/teensy41/firmware.hex)
"""
import fcntl
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HEX = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / '.pio/build/teensy41/firmware.hex'
MCU = 'TEENSY41'
HIDIOCSFEATURE = lambda n: 0xC0000000 | (n << 16) | (ord('H') << 8) | 0x06

TEENSY_VID = 0x16C0


def devices():
    """[(pid, product)] for every 16c0 device on the bus."""
    out = []
    for d in Path('/sys/bus/usb/devices').glob('*'):
        try:
            if int((d / 'idVendor').read_text().strip(), 16) != TEENSY_VID:
                continue
            pid = int((d / 'idProduct').read_text().strip(), 16)
        except OSError:
            continue
        try:                              # the bootloader has no product string
            name = (d / 'product').read_text().strip()
        except OSError:
            name = '(bootloader)'
        out.append((pid, name))
    return out


def hidraw_nodes(pid):
    nodes = []
    for h in sorted(Path('/dev').glob('hidraw*')):
        try:
            info = subprocess.run(['udevadm', 'info', '-q', 'property', '-n', str(h)],
                                  capture_output=True, text=True).stdout
        except OSError:
            continue
        if 'ID_VENDOR_ID=16c0' in info and f'ID_MODEL_ID={pid:04x}' in info:
            nodes.append(h)
    return nodes


def send_seremu_reboot():
    """Ask a running RawHID Teensy to jump into the HalfKay bootloader."""
    for node in hidraw_nodes(0x0486):
        try:
            fd = os.open(str(node), os.O_RDWR)
        except OSError:
            continue
        try:
            # leading byte = HID report number 0; the kernel strips it, so the
            # device sees SET_REPORT(wValue=0x0300, wLength=4) = A9 45 C2 6B
            fcntl.ioctl(fd, HIDIOCSFEATURE(5), b'\x00\xa9\x45\xc2\x6b')
            print(f'[teensy] sent SEREMU reboot report via {node}')
        except OSError as e:
            print(f'[teensy] {node}: {e}')
        finally:
            os.close(fd)
    for _ in range(30):                       # up to 3 s
        if any(pid == 0x0478 for pid, _ in devices()):
            print('[teensy] HalfKay bootloader up')
            return True
        time.sleep(0.1)
    return False


def main():
    if not HEX.is_file():
        sys.exit(f'no firmware hex at {HEX} - build it with `pio run` first')

    here = devices()
    print('[teensy] on the bus:', ', '.join(f'{p:04x} {n}' for p, n in here) or 'nothing')
    pids = {p for p, _ in here}

    if 0x0478 in pids:
        rc = flash_and_wait()
    elif 0x0483 in pids:
        # Running *our* firmware (USB Type = Serial): teensy_loader_cli's own
        # soft reboot (CDC line coding 134) works, so let it do the whole job.
        print('[teensy] CDC firmware running - soft-rebooting via the loader')
        rc = flash_and_wait(['-s'])
    elif 0x0486 in pids:
        # Factory-fresh / Raw HID firmware: no CDC, so use the SEREMU report.
        if not send_seremu_reboot():
            sys.exit('[teensy] could not reach the bootloader - press the reset button '
                     'and re-run, or hold it while this waits')
        rc = flash_and_wait()
    else:
        sys.exit('[teensy] no Teensy (16c0:xxxx) on the bus - is it plugged in?')
    if rc:
        sys.exit(rc)


def flash_and_wait(extra=()):
    # The `-s` (soft reboot) path is two-phase: reboot, then re-find the device and
    # write. That second phase intermittently loses its USB handle and fails with
    # "error writing to Teensy" - but it has already put the board in HalfKay, so a
    # plain retry succeeds. Retry rather than making the operator press the button.
    for attempt in (1, 2):
        args = ['teensy_loader_cli', f'--mcu={MCU}', '-w', '-v', *extra, str(HEX)]
        rc = subprocess.run(args).returncode
        if rc == 0:
            break
        if attempt == 2:
            return rc
        print('[teensy] write failed - the board is in HalfKay now, retrying')
        time.sleep(1)
    for _ in range(100):                      # wait for the new firmware to come up
        if any(pid != 0x0478 for pid, _ in devices()):
            time.sleep(1.5)
            print('[teensy] running again:', ', '.join(f'{p:04x} {n}' for p, n in devices()))
            return 0
        time.sleep(0.1)
    print('[teensy] flashed, but no device re-appeared within 10 s')
    return 0


if __name__ == '__main__':
    main()

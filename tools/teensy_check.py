#!/usr/bin/env python3
"""Bench check for a Teensy 4.1 running the racecar-35 firmware: reboot it and
report what the boot banner + the live telemetry say about the sensors.

Answers the two questions that come up every time a unit is built or swapped:

  * **IMU** - is the MPU-6050 detected (`MPU-6050 ready` vs `NOT found on Wire`)
    and are the values live (accel magnitude ~1.00 g at rest, gyro ~0)?
  * **GPS** - did the u-blox connect, at what baud, is the UBX PVT stream being
    parsed fresh (the GPS line's last field: 2 = OK / 3 = STALE / 0 = OFF), and
    does it have a satellite fix (fix type + sats)?

It reboots (never flashes) so the boot banner is captured, reads the raw tty, and
exits non-zero if the IMU is missing, the GPS link is down, or no telemetry
arrives at all. A GPS with no fix/sats but status 2 is a PASS: the link and the
data are fine, there is just no sky view (say so in the output).

Usage: tools/teensy_check.py [--port /dev/ttyACM0] [--seconds 8]
"""
import argparse
import glob
import os
import re
import subprocess
import sys
import termios
import time

ap = argparse.ArgumentParser()
ap.add_argument('--port', default=None,
                help='serial port (default: the Teensy by USB id, else the first /dev/ttyACM*)')
ap.add_argument('--seconds', type=float, default=8.0)
ap.add_argument('--no-reboot', action='store_true',
                help='just read the port (the boot banner will be missing)')
args = ap.parse_args()


def find_port():
    """The tty number is NOT stable: re-enumerating after a reboot or a flash can
    leave the board on ttyACM1, while platformio.ini's monitor_port says ttyACM0.
    The by-id symlink follows the board's USB serial number, so use that."""
    ids = sorted(glob.glob('/dev/serial/by-id/usb-Teensyduino_*-if00'))
    if ids:
        return os.path.realpath(ids[0])
    acms = sorted(glob.glob('/dev/ttyACM*'))
    if acms:
        return acms[0]
    sys.exit('no Teensy serial port found - is the board plugged in and running firmware?')


if not args.port:
    args.port = find_port()
args.explicit_port = any(a.startswith('--port') for a in sys.argv)

BOOT_BANNER = re.compile(rb'^(\[?[^,]*?boot[^\n]*|u-blox[^\n]*|\[gps\][^\n]*|MPU-6050[^\n]*|'
                         rb'IMU cal[^\n]*|\[sd\][^\n]*|\[boot\][^\n]*|FreqMeasureMulti[^\n]*)$',
                         re.M)


def read_port(path, seconds, explicit):
    """Open the port RAW. Opening a tty with the default termios turns ECHO on,
    and a serial tty ECHOes everything the device sends straight back at it -
    the Teensy then sees its own telemetry as a command and floods
    '[usb] unknown cmd'. Never read this port without clearing ECHO.

    A reboot re-enumerates the board onto a new /dev node; the OLD fd does not
    error, it just goes silent - so give up on a quiet fd and re-resolve."""
    deadline = time.time() + seconds
    buf = b''
    while time.time() < deadline:
        port = path if explicit else find_port()
        try:
            fd = os.open(port, os.O_RDONLY | os.O_NONBLOCK)
        except OSError:
            time.sleep(0.15)
            continue
        try:
            a = termios.tcgetattr(fd)
            a[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN)
            a[0] &= ~(termios.IXON | termios.IXOFF | termios.ICRNL | termios.INLCR)
            a[6][termios.VMIN] = 0
            a[6][termios.VTIME] = 1
            termios.tcsetattr(fd, termios.TCSANOW, a)
            last = time.time()
            while time.time() < deadline:
                try:
                    d = os.read(fd, 8192)
                except BlockingIOError:
                    d = b''
                except OSError:
                    break                       # device went away
                if d:
                    buf += d
                    last = time.time()
                elif time.time() - last > 2.0:
                    break                       # re-enumerated: this fd is dead
        finally:
            try:
                os.close(fd)
            except OSError:
                pass
    return buf


def soft_reboot_later(seconds):
    """Reboot into HalfKay (no programming) in the background, so the capture
    catches the fresh boot banner. -b = bootloader only."""
    if args.no_reboot:
        return None
    time.sleep(seconds)
    return subprocess.Popen(['teensy_loader_cli', '--mcu=TEENSY41', '-w', '-s', '-b', '-v'],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


probe = soft_reboot_later(1.5)          # let the port open first
data = read_port(args.port, args.seconds, explicit=bool(args.explicit_port))
if probe:
    try:
        probe.wait(timeout=20)
    except subprocess.TimeoutExpired:
        probe.kill()

lines = data.split(b'\n')
text = data.decode('utf-8', 'replace')
fail = []

# --- boot banner -------------------------------------------------------------
banner = [l.decode('utf-8', 'replace') for l in lines if BOOT_BANNER.match(l)]
fw = re.search(r'boot, firmware v([0-9.]+)', text)
print(f'firmware : {fw.group(1) if fw else "unknown (no boot banner captured)"}  [{args.port}]')

# --- IMU ---------------------------------------------------------------------
imu = [l.decode() for l in lines if l.startswith(b'IMU,')]
if 'MPU-6050 NOT found on Wire' in text:
    print('IMU      : NOT DETECTED  <-- check SDA=18, SCL=19, AD0->GND, 3.3 V')
    fail.append('IMU not detected')
elif 'MPU-6050 ready' in text or imu:
    cal = re.search(r'IMU calibrated: gyro bias\[([^\]]*)\] dps, \|a\|=([0-9.]+) g', text)
    if imu:
        vals = [[float(x) for x in l.split(',')[1:4]] for l in imu]
        mags = [sum(v * v for v in a) ** 0.5 for a in vals]
        print(f'IMU      : OK  {len(imu)} samples, accel magnitude '
              f'{min(mags):.2f}-{max(mags):.2f} g (1.00 = calibrated at rest)')
    else:
        print('IMU      : detected at boot but no IMU lines in the capture')
        fail.append('no IMU telemetry')
    if cal:
        print(f'           cal: gyro bias[{cal.group(1)}] dps, measured |a|={cal.group(2)} g')
    if 'IMU cal ABORTED' in text:
        print('           WARNING: calibration was aborted (device was moving)')
else:
    print('IMU      : no verdict (short capture, no boot banner) - rerun without --no-reboot')

# --- GPS ---------------------------------------------------------------------
baud = re.search(r'u-blox connected @ (\d+) baud', text)
gps = [l.decode() for l in lines if l.startswith(b'GPS,')]
stats = [int(l.rsplit(',', 1)[1].split()[0]) for l in gps if l.count(',') >= 7]
if 'u-blox NOT detected' in text:
    print('GPS      : NOT DETECTED on Serial2 (RX 7 / TX 8) - module missing, unpowered, '
          'or wired to the wrong pins')
    fail.append('GPS not detected')
else:
    fix_sats = sorted({(int(l.split(',')[1]), int(l.split(',')[2])) for l in gps})
    print(f'GPS      : link OK{" @ " + baud.group(1) + " baud" if baud else ""}, '
          f'{len(gps)} lines, status {sorted(set(stats)) if stats else "?"} '
          f'(2 = fresh PVT, 3 = STALE, 0 = off)')
    if fix_sats:
        print(f'           fix/sats seen: {fix_sats}')
    if stats and set(stats) == {2}:
        if all(f == 0 and s == 0 for f, s in fix_sats):
            print('           link + PVT stream are healthy; NO satellite fix yet '
                  '(no antenna / no sky view - take it outside to confirm)')
        else:
            print('           HAS A FIX')
    elif stats:
        print('           <-- PVT stream is not fresh: check the sky view and the '
              'Serial2 wiring, then the RX-buffer note in CLAUDE.md')
        fail.append('GPS PVT stale')
    elif not gps:
        print('           <-- connected but no GPS telemetry captured')
        fail.append('no GPS telemetry')

# --- SD ----------------------------------------------------------------------
sd = re.search(r'\[sd\] READY\s+total=(\d+)MB free=(\d+)MB', text)
if sd:
    print(f'SD card  : READY  {sd.group(1)} MB total, {sd.group(2)} MB free')
elif 'SD' in text:
    print('SD card  : present but not READY (see the [sd] lines above)')

other = [l for l in banner if l.startswith('[')]
if other:
    print('\nboot lines:')
    for l in other:
        print('  ' + l)

if fail:
    print('\nFAIL: ' + '; '.join(fail))
    sys.exit(1)
print('\nPASS')

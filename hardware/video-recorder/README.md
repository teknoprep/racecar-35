# Dual USB camera video box — Raspberry Pi 5

**Locked hardware: Raspberry Pi 5** (you already have them). This is a
**separate box**. The Teensy breadboard only gains a 3-pin 3.3 V UART header.
USB cameras and the stick plug into the Pi, never into the Teensy.

```
Pi 5 box                                         Teensy breadboard
+--------------------------------------+         +------------------+
| USB2: ELP front 1080p MJPEG          |  UART   | pin 1 TX1 ------> Pi GPIO15 RX (hdr 10)
| USB2: ELP rear  1080p MJPEG (opt)    | 115200  | pin 0 RX1 <------ Pi GPIO14 TX (hdr 8)
| USB3: thumb drive / T7               | 3-wire  | GND ------------- GND (hdr 6)
| ffmpeg: 1080p front + rear PIP BR    |         |                  |
|         + HUD from UART stats        |         | CrowPanel START  |
| own 12V → 5V 5A USB-C (not Teensy)   |         | still owns REC   |
+--------------------------------------+         +------------------+
```

Screen START/STOP already sends `REC,1` / `REC,0` to the Teensy. Firmware
forwards those to this box **only when Settings → Video interconnect is ON**.
The same UART carries overlay stats. Files stay on the USB stick — not on
Teensy SD.

## Why Pi 5 is enough (and what it is not)

You do **not** encode two 1080p files. The Pi:

1. Pulls **MJPEG** 1080p30 from each ELP (already compressed on the camera).
2. Decodes both, scales rear to 480×270, PIP bottom-right.
3. Burns HUD text onto **one** 1920×1080 frame.
4. Software-encodes that **one** stream with `libx264 veryfast` (~8 Mbps, ~4 GB/h).

Pi 5 has **no** H.264 hardware encoder. One 1080p30 x264 + dual MJPEG decode
is a normal A76 workload. Dual *independent* 1080p encodes would not be.

Must-haves on Pi 5 or USB/cameras starve and frames drop:

- **Official active cooler** (or equivalent fan). Not optional in a car.
- **5 V 5 A** on USB-C. A Pi 4 3 A supply will brown out under REC.
- `/boot/firmware/config.txt` must contain `usb_max_current_enable=1`
  (without PD handshake, Pi 5 caps USB peripherals at ~600 mA — two cameras
  plus a stick exceed that).

## What you still need (you have the Pi 5s)

| Qty | What | Notes |
| ---:| --- | --- |
| 1 | Pi 5 you already have + **active cooler** | 4 GB is enough |
| 2 | [ELP 1080p USB varifocal](https://www.amazon.com/ELP-Varifocal-Definition-Android-Industrial/dp/B07D57PQB7/) | Front / rear. UVC MJPEG only — never YUYV |
| 1 | Samsung BAR Plus 128 GB **or** T7 SSD | USB 3. Cheap sticks drop frames. exFAT |
| 1 | **12 V → 5 V 5 A USB-C** buck | Fuse 5 A on 12 V. **Do not** use Teensy 5 V |
| 1 | 3-wire 22 AWG + 2× 1 kΩ | Teensy 0/1/GND ↔ Pi header 8/10/6 |
| 1–2 | USB 2.0 **active** 5 m extension if a camera won’t reach | Passive USB 2 dies ~3–5 m |

Ports: FRONT → USB 2.0, REAR → USB 2.0, stick → USB 3.0, power → USB-C.

## Breadboard UART (3.3 V only)

Both ends are 3.3 V. **Never** CrowPanel J10 (5 V). Common GND is mandatory.

```
Teensy 4.1                         Pi 5 40-pin
pin 1  TX1  ── 1 kΩ ──>            GPIO15 RXD  header pin 10
pin 0  RX1  <── 1 kΩ ──            GPIO14 TXD  header pin 8
GND         ──────────             GND         header pin 6
```

Pi 5 also has a tiny 3-pin JST UART by the RTC battery — ignore it; use the
40-pin so you can use DuPont/breadboard wire.

Firmware: Teensy `Serial1` 115200 8N1. Dash NVS `viden`, `CFG,viden,0|1`.
Breadboard UART header is **J3 VIDEO** — see [hardware/breadboard](../breadboard/).

## Protocol (Teensy Serial1 ↔ Pi)

`\n`-terminated.

Teensy → Pi:

```
VIDEN,1
TRACK,Summit Point
REC,1
HUD,<rpm>,<mph_x10>,<lap>,<last_ms>,<pred_ms>,<best_ms>,<oil_x10>,<clt_x10>,<afr_x10>,<lat>,<lon>
REC,0
VIDEN,0
```

`HUD` ~10 Hz while interconnect is on and a session is running. Times are
milliseconds; `-1` = unknown. Dash sends `HUDLAP,...` to the Teensy so PRED
matches the screen.

Pi → Teensy (forwarded to Settings → **Video box**):

```
VID,HELLO,<ver>
VID,READY,<ncam>,<usb_ok>
VID,REC,1,<filename>
VID,REC,0
VID,ERR,<reason>          nousb / nocam / ffmpeg / ...
```

No second record button. START on the dash is the only trigger.

## Pi 5 image (once cameras + stick are on the desk)

1. Raspberry Pi OS **Lite 64-bit**, SSH on. Boot.
2. `sudo raspi-config` → Interface Options → Serial Port:
   login shell **No**, serial hardware **Yes**.
3. Append to `/boot/firmware/config.txt`:

   ```
   dtparam=uart0=on
   usb_max_current_enable=1
   ```

   Reboot. `ls -l /dev/serial0 /dev/ttyAMA0` — serial0 should point at a real UART.

4. `sudo apt update && sudo apt install -y ffmpeg v4l-utils python3-serial fonts-dejavu-core exfatprogs`
5. Copy this directory to `/home/pi/racecar-video/`.
   `cp config.env.example config.env` and edit if needed.
6. Stick: `sudo mkfs.exfat /dev/sda1` if needed, mount at `/media/usb`
   (see `racecar-video.service`).
7. Cameras: `v4l2-ctl --list-devices`. Set `FRONT=` / `BACK=` in `config.env`
   if the ports come up swapped. **Force MJPEG** — YUYV 1080p will fail.
8. Bench: `python3 recorder.py` then `REC,1` on the UART. Confirm an `.mp4`.
   `REC,0` finalizes. Files are **fragmented MP4** so a master-cut is still
   playable. 15 s UART silence while recording stops the file.
9. `sudo cp racecar-video.service /etc/systemd/system/ && sudo systemctl enable --now racecar-video`

`recorder.py` uses stock ffmpeg `libx264` (no Rockchip MPP, no Pi-4 v4l2m2m).

## Not doing

- Video on the CrowPanel / a second screen UART (no free RGB pins)
- Recording video onto Teensy SD
- Live preview on the dash
- Rev C PCB — parked; this is a box + one header
- Pi updates via dash OTA — scp/git until we decide otherwise

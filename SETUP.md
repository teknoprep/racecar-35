# racecar-35 — Dev Environment Setup

This document is written for an AI coding agent (Claude Code, Cursor, etc.) to drive a clean install of the toolchains needed to build and flash this repo on a fresh machine. Both **Windows** and **Linux** are supported. macOS is not covered but mostly mirrors Linux.

The repo has **two MCUs with two separate toolchains**, both of which must be installed:

| Target | MCU | Toolchain | Source location |
| --- | --- | --- | --- |
| Trunk | Teensy 4.1 | **PlatformIO** | [src/main.cpp](src/main.cpp) |
| Cabin dash | CrowPanel ESP32-S3 V3.0 | **arduino-cli** + `esp32:esp32@2.0.14` | [crowpanel-arduino/RaceDash/RaceDash.ino](crowpanel-arduino/RaceDash/RaceDash.ino) |

⚠️ **Do not try to build the CrowPanel sketch with PlatformIO.** PIO's `espressif32@^6.7.0` bootloader boot-loops on the V3.0 board. Use arduino-cli only. See [CLAUDE.md](CLAUDE.md) for the full reasoning.

---

## 0. Prerequisites (both OSes)

- **Git**
  - Windows: `winget install Git.Git` (or [git-scm.com](https://git-scm.com/))
  - Linux (Debian/Ubuntu): `sudo apt install git`
  - Linux (Fedora/RHEL): `sudo dnf install git`
- **Python ≥ 3.9** (for PlatformIO)
  - Windows: `winget install Python.Python.3.12`
  - Linux: usually preinstalled; otherwise `sudo apt install python3 python3-pip python3-venv`
- **A USB cable for each board** (CrowPanel uses CH340 over USB-C; Teensy uses USB micro-B native CDC)

Clone the repo:

```bash
git clone <repo-url> racecar-35
cd racecar-35
```

---

## 1. Install PlatformIO (for Teensy 4.1)

PlatformIO is OS-portable. Either the VS Code extension or the CLI works; the CLI is what the build/flash commands assume.

### Windows
```powershell
python -m pip install --user platformio
```
PIO ends up at `%USERPROFILE%\.platformio\penv\Scripts\platformio.exe`. The commands in [CLAUDE.md](CLAUDE.md) use this absolute path.

### Linux
```bash
python3 -m pip install --user platformio
# Add to PATH if not already:
echo 'export PATH="$HOME/.local/bin:$PATH"' >> ~/.bashrc
source ~/.bashrc
```
PIO ends up on PATH as `pio` / `platformio`.

### Teensy-specific extras (Linux only)
Linux needs a udev rule so non-root users can flash the Teensy:
```bash
sudo wget -O /etc/udev/rules.d/00-teensy.rules https://www.pjrc.com/teensy/00-teensy.rules
sudo udevadm control --reload-rules
sudo udevadm trigger
```
Also add your user to `dialout` (covers serial AND Teensy programming port):
```bash
sudo usermod -aG dialout $USER
# log out and back in for this to take effect
```

### Verify
```bash
# Windows
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" --version
# Linux
pio --version
```
First-time `pio run` will auto-download the Teensy GCC toolchain and required libraries (~5 min, one-time).

---

## 2. Install arduino-cli (for CrowPanel ESP32-S3)

### Windows
The Arduino IDE 2.x installer ships `arduino-cli.exe` inside its install dir:
```
C:\Users\<you>\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe
```
Either install Arduino IDE 2.x (`winget install ArduinoSA.IDE.unstable`) and use that path, **or** download the standalone CLI:
```powershell
# Standalone (recommended for headless / scripted use)
Invoke-WebRequest -Uri "https://downloads.arduino.cc/arduino-cli/arduino-cli_latest_Windows_64bit.zip" -OutFile arduino-cli.zip
Expand-Archive arduino-cli.zip -DestinationPath "$env:USERPROFILE\arduino-cli"
# Add $env:USERPROFILE\arduino-cli to PATH
```

### Linux
```bash
curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=$HOME/.local/bin sh
```
(Or use a distro package if one is current — most are stale.)

### Configure arduino-cli
```bash
# Create the config file if it doesn't exist
arduino-cli config init

# Add the ESP32 board package URL
arduino-cli config add board_manager.additional_urls https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json

# Update the index
arduino-cli core update-index

# Install the EXACT version. 2.0.14 is required — newer versions have not been validated against the V3.0 board's PCA9557 / GT911 init sequence.
arduino-cli core install esp32:esp32@2.0.14
```

### Point arduino-cli at this project's libraries directory
The CrowPanel build depends on a bundled **PCA9557 library** that is not in the Arduino registry. It lives at [_vendor/CrowPanel-ESP32-Display-Course-File/CrowPanel_ESP32_Tutorial/Code/7.0 v3.0 touch new code/File/PCA9557/](_vendor/CrowPanel-ESP32-Display-Course-File/CrowPanel_ESP32_Tutorial/Code/7.0%20v3.0%20touch%20new%20code/File/PCA9557/).

Find your `directories.user` path:
```bash
arduino-cli config dump | grep -i user
```
Default locations:
- **Windows:** `C:\Users\<you>\Documents\Arduino\` (or wherever your Documents folder redirects — on this dev machine it's OneDrive: `C:\Users\<you>\OneDrive - <Org>\Documents\Arduino\`)
- **Linux:** `~/Arduino/`

Copy the bundled PCA9557 library into `<directories.user>/libraries/PCA9557/`:

**Windows (PowerShell):**
```powershell
$src = ".\_vendor\CrowPanel-ESP32-Display-Course-File\CrowPanel_ESP32_Tutorial\Code\7.0 v3.0 touch new code\File\PCA9557"
$dst = (arduino-cli config dump | Select-String 'user:').ToString().Split(':',2)[1].Trim() + "\libraries\PCA9557"
New-Item -ItemType Directory -Force -Path $dst | Out-Null
Copy-Item "$src\*" $dst -Recurse -Force
```

**Linux:**
```bash
src="./_vendor/CrowPanel-ESP32-Display-Course-File/CrowPanel_ESP32_Tutorial/Code/7.0 v3.0 touch new code/File/PCA9557"
dst="$HOME/Arduino/libraries/PCA9557"
mkdir -p "$dst"
cp -r "$src"/* "$dst"/
```

The PCA9557 directory must contain a `library.properties` file. If it's missing, see [CLAUDE.md](CLAUDE.md) — the agent should hand-write one.

### Other libraries the CrowPanel sketch uses
TFT_eSPI, LovyanGFX, etc. are pulled in as needed. Install via:
```bash
arduino-cli lib install "TFT_eSPI"
```
Re-run `arduino-cli compile` (below) and install whatever it complains about until it links cleanly.

---

## 3. Identify serial ports

Both boards must be plugged in to flash, but **NOT at the same time during a CrowPanel flash** (see warning below).

### Windows
```powershell
[System.IO.Ports.SerialPort]::GetPortNames()
# Or check Device Manager → Ports (COM & LPT)
```
- **CrowPanel** enumerates as a `USB-SERIAL CH340` device → typically `COM3`
- **Teensy 4.1** enumerates as `USB Serial Device` (native CDC) → typically `COM4`

### Linux
```bash
# Plug in one board at a time and watch:
dmesg -w
# Or list current:
ls /dev/serial/by-id/
```
- **CrowPanel** → `/dev/ttyUSB0` (CH340, kernel built-in)
- **Teensy 4.1** → `/dev/ttyACM0` (native CDC)

Update [platformio.ini](platformio.ini) `monitor_port =` to match the Teensy's port if it differs from `COM4`.

---

## 4. Build & flash

### Teensy (both OSes — the only difference is the `pio` binary path)

**Windows:**
```powershell
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" run -t upload
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" device monitor
```

**Linux:**
```bash
pio run -t upload
pio device monitor
```

The Teensy upload uses the GUI loader (`teensy-gui`) which pops a window asking you to press the reset button if it's not already in HID-bootloader mode.

### CrowPanel — ⚠️ READ THIS BEFORE FLASHING

**Disconnect the Teensy↔CrowPanel UART jumpers (GPIO 43/44) before flashing the CrowPanel.** UART0 is shared with the CH340 used for upload; Teensy contention silently corrupts the flash, or you get `The serial TX path seems to be down` from esptool.

The exact FQBN is mandatory — every option matters:
```
esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=4M,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none
```

**Windows:**
```powershell
$cli = "C:\Users\ChrisRawlings\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
# Or if installed standalone:
# $cli = "arduino-cli"
$fqbn = "esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=4M,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none"
$sketch = ".\crowpanel-arduino\RaceDash"
& $cli compile --fqbn $fqbn $sketch
& $cli upload  --fqbn $fqbn -p COM3 $sketch
```

**Linux:**
```bash
fqbn="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=4M,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none"
sketch="./crowpanel-arduino/RaceDash"
arduino-cli compile --fqbn "$fqbn" "$sketch"
arduino-cli upload  --fqbn "$fqbn" -p /dev/ttyUSB0 "$sketch"
```

To watch the CrowPanel serial output after flash:
```bash
# Windows
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" device monitor --port COM3 --baud 115200
# Linux
pio device monitor --port /dev/ttyUSB0 --baud 115200
# Or any terminal: minicom, screen, picocom, etc.
```

---

## 5. Smoke tests

After both boards are flashed:

1. **Teensy alone** — open serial monitor at 115200, you should see `GPS,...` lines (even with `gps_status=0` if antenna unplugged) and a 1 Hz heartbeat.
2. **CrowPanel alone** — display should boot to the dash page, show `RPM 0`, `0.0 mph`, `FIX 0`, `SATS 0`. Touch START → track picker should open.
3. **Both connected via UART jumpers** (only after each is verified solo) — dash should start showing real GPS/RPM values from the Teensy. Tapping START on the dash should make the Teensy serial monitor print `[dash] REC=1 ...`.

---

## 6. Common install-time problems

| Symptom | Cause | Fix |
| --- | --- | --- |
| `arduino-cli: command not found` after install on Linux | `$HOME/.local/bin` not on PATH | `export PATH="$HOME/.local/bin:$PATH"` in `~/.bashrc` |
| `Permission denied` opening `/dev/ttyUSB0` on Linux | User not in `dialout` group | `sudo usermod -aG dialout $USER` + relogin |
| Teensy upload hangs | Teensy not in bootloader mode | Press the white button on top of the Teensy |
| CrowPanel upload: `The serial TX path seems to be down` | UART jumpers to Teensy still connected | Unplug jumpers, retry |
| CrowPanel boots but display is black | Stale arduino-esp32 version installed | Verify `arduino-cli core list` shows `esp32:esp32 2.0.14` exactly — `arduino-cli core install esp32:esp32@2.0.14` if not |
| `PCA9557.h: No such file or directory` | Bundled library not copied to user libs dir | Re-run section 2's PCA9557 copy step; verify path with `arduino-cli config dump \| grep user` |
| PIO compile fails on Linux with `libudev not found` | Missing build dep | `sudo apt install libudev-dev` |

---

## 7. Things to NOT do (project-specific footguns)

These are documented in [CLAUDE.md](CLAUDE.md) but worth surfacing to anyone setting up fresh:

- **Don't create a PlatformIO env for the CrowPanel.** The PIO bootloader boot-loops the V3.0 board. arduino-cli only.
- **Don't flash the CrowPanel with the Teensy↔CrowPanel UART wires connected.** Disconnect, flash, reconnect.
- **Don't install `esp32:esp32` at a version other than `2.0.14`** without testing the full PCA9557 + GT911 + 15 MHz pclk init sequence end-to-end.
- **Don't edit code under [crowpanel-baseline/](crowpanel-baseline/) or [crowpanel-ui/](crowpanel-ui/)** — those are dead PIO experiments kept for reference only. Live CrowPanel code is [crowpanel-arduino/RaceDash/RaceDash.ino](crowpanel-arduino/RaceDash/RaceDash.ino).

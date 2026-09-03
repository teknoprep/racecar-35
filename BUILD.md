# BUILD.md — full build requirements for racecar-35

This documents **everything** required to build all four release artifacts from a clean
machine, plus the exact commands that produce them. It is the operational companion to the
release contract in [CLAUDE.md](CLAUDE.md) ("STANDING ORDERS" / "OTA release").

There are **two independent toolchains** — they are NOT interchangeable (see CLAUDE.md):

| Artifact(s) | Toolchain | Source |
| --- | --- | --- |
| `teensy41-dash.hex` | **PlatformIO** (`platform = teensy`, `board = teensy41`) | `src/main.cpp`, `platformio.ini` |
| `crowpanel7-dash.bin`, `crowpanel5-dash.bin`, `crowpanel5adv-dash.bin`, `crowpanel7adv-dash.bin` | **arduino-cli** + `esp32:esp32@2.0.14` | `crowpanel-arduino/RaceDash/` |

> The three CrowPanel bins are one source (`RaceDash.ino`) built three times with a different
> `-DDASH_BOARD` and a per-board `FlashSize` (Advance = `16M`, both Basics = `4M`).

---

## 0. Host prerequisites

- Linux x86-64 (this was built on one; macOS works with path changes).
- **`python3`** on `PATH`. Note: a stock python3 may ship **without `pip`/`ensurepip`** — the
  steps below bootstrap pip manually with `get-pip.py`, so that's fine.
- `curl`, `tar`, `unzip`, `git`, `sha256sum`, `stat`.
- Internet access to GitHub + PyPI + the Espressif package index on first setup.
- **~2–3 GB free disk** for the two toolchains + cores.

### ⚠️ Where to install the tools

On this machine `~/chrisrawlings/…` (the repo lives under
`~/chrisrawlings/Documents/vscode/racecar-35`) is **OneDrive-synced** — do NOT put toolchains,
caches, or venvs there (sync churn + huge upload). Install everything to a **non-synced**
location. This doc uses:

```
/home/chris/racecar-tools/          # all build tooling lives here (NOT synced)
  bin/arduino-cli                    # arduino-cli binary
  arduino-cli.yaml                   # config: data/downloads/user dirs -> adata/
  adata/                             # esp32 core + libraries (arduino-cli data dir)
  pylibs/                            # pyserial (pure-python) on PYTHONPATH for esptool
  pio-venv/                          # python venv holding PlatformIO
  pio-core/                          # PLATFORMIO_CORE_DIR (teensy platform + packages)
```

---

## 1. arduino-cli + ESP32 core (the three CrowPanel bins)

### 1a. Install arduino-cli
```bash
mkdir -p /home/chris/racecar-tools/bin && cd /home/chris/racecar-tools
curl -fsSL https://github.com/arduino/arduino-cli/releases/download/v1.0.4/arduino-cli_1.0.4_Linux_64bit.tar.gz -o acli.tgz
tar xzf acli.tgz -C bin arduino-cli
./bin/arduino-cli version   # -> 1.0.4
```

### 1b. Config file (keeps all data OFF the synced tree)
`/home/chris/racecar-tools/arduino-cli.yaml`:
```yaml
directories:
  data: /home/chris/racecar-tools/adata/data
  downloads: /home/chris/racecar-tools/adata/downloads
  user: /home/chris/racecar-tools/adata/user
board_manager:
  additional_urls:
    - https://raw.githubusercontent.com/espressif/arduino-esp32/gh-pages/package_esp32_index.json
```

### 1c. Install the ESP32 core — **pinned to 2.0.14** (newer boot-loops this board; see CLAUDE.md)
```bash
CLI="./bin/arduino-cli --config-file /home/chris/racecar-tools/arduino-cli.yaml"
$CLI core update-index
$CLI core install esp32:esp32@2.0.14
```

### 1d. Libraries
Two from the registry, one bundled in `_vendor/`:
```bash
LIBDIR=/home/chris/racecar-tools/adata/user/libraries
$CLI lib install "LovyanGFX"     # 1.2.24 verified
$CLI lib install "TAMC_GT911"    # 1.0.2 verified — GT911 touch on Wire (see CLAUDE.md)

# PCA9557 is NOT in the registry — copy it from the vendor course files + add library.properties
V=_vendor/CrowPanel-ESP32-Display-Course-File/CrowPanel_ESP32_Tutorial/File/PCA9557
mkdir -p "$LIBDIR/PCA9557"
cp "$V/PCA9557.cpp" "$V/PCA9557.h" "$LIBDIR/PCA9557/"
cat > "$LIBDIR/PCA9557/library.properties" <<'EOF'
name=PCA9557
version=1.0.0
author=Elecrow
maintainer=Elecrow
sentence=PCA9557 IO expander (bundled from CrowPanel vendor course files)
paragraph=
category=Device Control
url=
architectures=*
EOF
```
(paths above are relative to the repo root.)

### 1e. pyserial for esptool (the non-obvious gotcha)
The esp32 core's post-compile image step runs `esptool.py`, which `import serial`. A stock
python3 without pip can't provide it, so drop the **pure-python** pyserial wheel onto
`PYTHONPATH` and export it for every compile:
```bash
cd /home/chris/racecar-tools && mkdir -p pylibs
curl -fsSL https://files.pythonhosted.org/packages/07/bc/587a445451b253b285629263eb51c2d8e9bcea4fc97826266d186f96f558/pyserial-3.5-py2.py3-none-any.whl -o pyserial.whl
unzip -oq pyserial.whl -d pylibs
PYTHONPATH=/home/chris/racecar-tools/pylibs python3 -c "import serial; print(serial.__version__)"  # 3.5
```
Symptom if you skip this: build fails with `ModuleNotFoundError: No module named 'serial'` at
the "Creating esp32s3 image" step (compile itself is fine — it dies in the image tool).

### 1f. Build the three dash bins
```bash
cd <repo root>
export PYTHONPATH=/home/chris/racecar-tools/pylibs          # <-- required (1e)
CLI="/home/chris/racecar-tools/bin/arduino-cli --config-file /home/chris/racecar-tools/arduino-cli.yaml"
base="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none"
# NimBLE size trim (v0.1.109): we are BLE central+observer only — compiling out the
# peripheral/broadcaster roles + single connection saves ~15.5 KB of the 1.31 MB OTA slot.
# Must go to BOTH c and cpp flags (the NimBLE host core is C).
trim="-DCONFIG_BT_NIMBLE_ROLE_PERIPHERAL_DISABLED -DCONFIG_BT_NIMBLE_ROLE_BROADCASTER_DISABLED -DCONFIG_BT_NIMBLE_MAX_CONNECTIONS=1"

# 7" Basic (crowpanel7), 4M
$CLI compile --fqbn "${base},FlashSize=4M"  --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=7 $trim"  --build-property "compiler.c.extra_flags=$trim" --build-path /tmp/rd7_build   --output-dir /tmp/rd7_out   crowpanel-arduino/RaceDash
# 5" Basic (crowpanel5), 4M
$CLI compile --fqbn "${base},FlashSize=4M"  --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=5 $trim"  --build-property "compiler.c.extra_flags=$trim" --build-path /tmp/rd5_build   --output-dir /tmp/rd5_out   crowpanel-arduino/RaceDash
# 5" Advance (crowpanel5adv), 16M
$CLI compile --fqbn "${base},FlashSize=16M" --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=51 $trim" --build-property "compiler.c.extra_flags=$trim" --build-path /tmp/rdadv_build --output-dir /tmp/rdadv_out crowpanel-arduino/RaceDash
# 7" Advance (crowpanel7adv), 16M  (v0.1.145+; same electrical config as the 5" Advance)
$CLI compile --fqbn "${base},FlashSize=16M" --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=71 $trim" --build-property "compiler.c.extra_flags=$trim" --build-path /tmp/rd7adv_build --output-dir /tmp/rd7adv_out crowpanel-arduino/RaceDash
```
**A fresh `--build-path` compiles the whole ESP32 core (~10+ min).** Run first-time builds
of a new variant in the background (`nohup … &`) or they'll blow past an agent's command
timeout; subsequent builds in the same path take under a minute.
Rules (from CLAUDE.md): use `compiler.cpp.extra_flags` (NOT `build.extra_flags`, which carries
the required USB-mode flags), and a **distinct `--build-path` per board** so the `-DDASH_BOARD`
define can't cross-contaminate the cache. The APP binary is `RaceDash.ino.bin` in each
`--output-dir`.

**Flash-size ceiling — what actually limits us.** The binding limit is the **A/B OTA app slot
in the partition table** (PartitionScheme=default ⇒ 1,310,720 B per slot), NOT total flash and
NOT the OTA transport. The partition table is written ONLY during a USB flash — OTA can never
change it — so **1.31 MB stays the release ceiling until every deployed 4M panel has been
bench-flashed**. The shipped `.bin` is partition-agnostic (ESP32 apps are MMU-mapped; the same
binary runs from either slot on either table), so the release builds above keep
`PartitionScheme=default` harmlessly. **Policy: any USB (bench) flash of a panel should use
`PartitionScheme=min_spiffs`** — same A/B OTA layout but SPIFFS (which we never use; settings
live in NVS) shrinks from 1.4 MB to 128 KB, growing each app slot to 1,966,080 B (+50%
headroom). Units migrate opportunistically as they visit the bench; an oversized image sent to
a not-yet-migrated unit fails cleanly (its `Update.begin()` checks its own table — no brick).

---

## 2. PlatformIO (the Teensy hex)

### 2a. Bootstrap pip into a venv, then install PlatformIO
Stock python3 here has no `ensurepip`, so create the venv `--without-pip` and bootstrap:
```bash
cd /home/chris/racecar-tools
python3 -m venv --without-pip pio-venv
curl -fsSL https://bootstrap.pypa.io/get-pip.py -o get-pip.py
./pio-venv/bin/python get-pip.py
./pio-venv/bin/pip install platformio        # 6.1.19 verified
./pio-venv/bin/pio --version
```

### 2b. Build (core dir kept off the synced tree)
```bash
cd <repo root>
export PLATFORMIO_CORE_DIR=/home/chris/racecar-tools/pio-core
/home/chris/racecar-tools/pio-venv/bin/pio run     # -> .pio/build/teensy41/firmware.hex
```
First run downloads the `teensy` platform + toolchain into `pio-core/`. `platformio.ini` pins
`monitor_port = COM4` (Windows) — harmless on Linux; monitor via `-t monitor` with the right
`/dev/tty*` if needed. Do **not** add a CrowPanel env to `platformio.ini` (PIO boot-loops that
board — see CLAUDE.md).

---

## 3. Assemble the release (the four artifacts + manifest)

```bash
cd <repo root>
cp .pio/build/teensy41/firmware.hex firmware/teensy41-dash.hex
cp /tmp/rd7_out/RaceDash.ino.bin    firmware/crowpanel7-dash.bin
cp /tmp/rd5_out/RaceDash.ino.bin    firmware/crowpanel5-dash.bin
cp /tmp/rdadv_out/RaceDash.ino.bin  firmware/crowpanel5adv-dash.bin
cp /tmp/rd7adv_out/RaceDash.ino.bin firmware/crowpanel7adv-dash.bin

sha256sum firmware/teensy41-dash.hex firmware/crowpanel7-dash.bin firmware/crowpanel5-dash.bin firmware/crowpanel5adv-dash.bin firmware/crowpanel7adv-dash.bin
stat -c'%n %s' firmware/teensy41-dash.hex firmware/crowpanel7-dash.bin firmware/crowpanel5-dash.bin firmware/crowpanel5adv-dash.bin firmware/crowpanel7adv-dash.bin
```
Then hand-edit `firmware/manifest.json`: set **every** `version` to the new number, paste the
recomputed `sha256` + `size` into each entry, keep each `board` field, and keep the legacy
`crowpanel` entry mirroring `crowpanel7` (same sha/size/version). A stale sha aborts OTA on the
device.

**Version bump (do FIRST, before building):** both defines must match:
```bash
NEW=0.1.69
sed -i "s/#define FIRMWARE_VERSION .*/#define FIRMWARE_VERSION \"$NEW\"/" src/main.cpp
sed -i "s/#define FIRMWARE_VERSION .*/#define FIRMWARE_VERSION \"$NEW\"/" crowpanel-arduino/RaceDash/RaceDash.ino
```

---

## 4. Flashing (optional — OTA is the normal path)

- **CrowPanel over USB**: disconnect the Teensy↔CrowPanel UART jumpers first (shared with the
  CH340). ALWAYS `esptool flash_id` to confirm 16M=Advance vs 4M=Basic and flash the matching
  bin, then read the boot banner @ 921600 to confirm the `crowpanel-…` board id + version. See
  CLAUDE.md "ALWAYS verify which board is connected".
- **Teensy over USB**: `pio run -t upload` (HalfKay; press the button if "Error opening USB
  device").
- **OTA**: bump + rebuild all four + publish `manifest.json` (below); devices self-update.

---

## 5. Publish

```bash
git add src/main.cpp crowpanel-arduino/RaceDash/RaceDash.ino crowpanel-arduino/RaceDash/board_config.h firmware/ BUILD.md CLAUDE.md
git commit -m "Release vX.Y.Z: <what changed>"
git push origin main
curl -s https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/manifest.json   # verify
```
Auth: the remote is HTTPS token auth (`https://github.com/teknoprep/racecar-35.git`). On the
original build host `$HOME` was unset, so git ops needed a `HOME=/root` prefix (store helper) or
an explicit token URL. On this host `$HOME=/home/chris`; use whatever supplies the token.

---

## Publishing OTA to the server (racecar.api.blueuc.com) — the normal path (v0.1.74+)

Since **v0.1.73** the dash checks our OWN server for updates, not GitHub. This means a future
agent can ship an OTA **entirely from this build host with no laptop** — build the four
artifacts, then push straight to the server, which serves them `no-store` (instantly fresh, no
CDN lag). See [server/README.md](server/README.md) for the endpoint reference.

### The firmware-upload API key (already provisioned)
- **Value** lives OFF the repo at **`/home/chris/racecar-tools/secrets/racecar_api_key.env`**
  (mode 600, outside the git tree — never committed). Read it with `grep RACECAR_API_KEY <file>`
  or `grep -oE '[0-9a-f]{64}' <file>`.
- The SAME value is set on the server as env **`RACECAR_FIRMWARE_KEY`** (gates `POST
  /firmware/upload`). It is DELIBERATELY separate from `RACECAR_API_KEY` (session-upload auth) —
  reusing one key for both previously 401'd every dash session upload mid-stream.
- The server is reachable over public HTTPS from this host; no SSH needed to publish.

### Publish command (after building all four — sections 1–2 above put them in `firmware/`)
```bash
export RACECAR_API_KEY=$(grep -oE '[0-9a-f]{64}' /home/chris/racecar-tools/secrets/racecar_api_key.env)
./server/publish_firmware.sh 0.1.NN https://racecar.api.blueuc.com
```
`publish_firmware.sh` uploads the four binaries then a server-pointed manifest (artifact URLs =
`<base>/firmware/<file>`, every `version`=the arg, legacy `crowpanel` alias = `crowpanel7`) and
prints the live manifest to verify. The script sends `X-API-Key: $RACECAR_API_KEY` — that env
var is the FIRMWARE key here, matching the server's `RACECAR_FIRMWARE_KEY` (naming is a bit
confusing: the script variable is `RACECAR_API_KEY` but it must hold the firmware key).

### Verify a server release
```bash
curl -s https://racecar.api.blueuc.com/firmware/manifest.json | grep -E '"version"|"url"'
curl -s https://racecar.api.blueuc.com/firmware/list                 # name+size+sha256 of all
# hash-check one artifact against the manifest:
curl -s https://racecar.api.blueuc.com/firmware/crowpanel5adv-dash.bin | sha256sum
```

### GitHub is now only the transition channel (frozen at 0.1.73)
GitHub `firmware/manifest.json` stays at **0.1.73** (the build whose `OTA_MANIFEST_URL` is the
server) so any panel still on ≤0.1.72 can OTA that far from GitHub and then hop to the server.
**0.1.74+ ship server-only** via the script above; do NOT add them to the GitHub manifest, and
keep the committed GitHub `firmware/` binaries at the 0.1.73 build (see the racy-index note
below). You still commit the SOURCE (both `FIRMWARE_VERSION` defines + `RaceDash.ino`) to GitHub
for provenance on every release.

### ⚠ Networked-FS racy git index (bites every release)
This working tree is on a filesystem whose stat-cache confuses git: an artifact that changed but
kept the **same byte length** (the teensy `.hex` is always 610714 B; the manifest is a fixed
size) gets treated as “clean” and silently NOT committed. Symptoms: `git show HEAD:firmware/x`
differs from the working file while `git status` says clean. **Always** stage firmware/manifest
changes with an index eviction:
```bash
git rm --cached firmware/teensy41-dash.hex firmware/manifest.json ... 2>/dev/null; git add firmware/ ...
git cat-file -p :firmware/manifest.json | grep -m1 version   # CONFIRM the staged blob is right
```
And for GitHub releases, pin the manifest's artifact URLs to the commit SHA (immutable path →
GitHub CDN can never serve a stale bin): commit artifacts first, `X=$(git rev-parse HEAD)`,
`sed -i "s#/main/firmware#/$X/firmware#g" firmware/manifest.json`, evict+add+commit+push.

## Verified versions (this build, 2026-07-01)

| Tool / lib | Version |
| --- | --- |
| arduino-cli | 1.0.4 |
| esp32:esp32 core | 2.0.14 (pinned) |
| LovyanGFX | 1.2.24 |
| TAMC_GT911 | 1.0.2 |
| PCA9557 | bundled (`_vendor/…/File/PCA9557`) |
| pyserial (for esptool) | 3.5 |
| PlatformIO Core | 6.1.19 |
| teensy platform | installed on first `pio run` |

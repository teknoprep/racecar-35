# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this project is

`racecar-35` is a two-MCU dash for a track car, with the **driver-facing display in the cabin** and **all sensing + connectivity in the trunk** so we run only one UART wire end-to-end.

```
Cabin (driver)                   Trunk (data + connectivity)
+-------------------+           +-----------------------------+
|  CrowPanel ESP32  | ---UART-> |  Teensy 4.1                 |
|  - dash UI        |           |  - GPS (NEO-M9N) @ 25 Hz    |
|  - settings page  |           |  - tach in (opto, pin 9)    |
|  - track picker   |           |  - MS3Pro CAN (CAN1, 22/23) |
|  - touch (GT911)  |           |  - W5500 ethernet (SPI0)    |
|  - keyboards      |           |  - SD card (built-in slot)  |
|  - REC commands ──+--UART---->|  - IMU (MPU-6050, Wire)     |
+-------------------+           |  - cloud uploader + queue   |
                                |  - NTP from 0.pool.ntp.org  |
                                +-----------------------------+
```

UART is **bidirectional**:
- Teensy → CrowPanel: `GPS,...`, `ENG,...`, `ECU,...`, `IMU,...`, `SD,...`, `CLD,...`, `CANSNIFF,...` lines (telemetry + status, 25 Hz with 1 Hz heartbeat)
- CrowPanel → Teensy: `REC,<0|1>`, `TRACK,<name>`, `CFG,<k>,<v>`, `CANSNIFF,<0|1>`, etc. (control)

> Most of the once-"planned" trunk features (W5500, SD logging, cloud queue, IMU, NTP) are
> **implemented now**. The newest additions (this work cycle) are the **MS3Pro CAN bus**
> (RPM/coolant/AFR) and a **CAN sniffer** for reverse-engineering the ECU broadcast layout.

[README.md](README.md) has the original wiring table — **architecture has expanded since**: there's now a planned W5500 ethernet module on the Teensy SPI0, and the dash sends control commands back to the Teensy.

## Critical: two MCUs, two build systems — and they are NOT interchangeable

| Side | Toolchain | Why |
| --- | --- | --- |
| Teensy 4.1 ([src/main.cpp](src/main.cpp), [platformio.ini](platformio.ini)) | **PlatformIO** | Works fine. |
| CrowPanel ESP32-S3 V3.0 ([crowpanel-arduino/RaceDash/RaceDash.ino](crowpanel-arduino/RaceDash/RaceDash.ino)) | **arduino-cli** with `esp32:esp32@2.0.14` | PIO's `espressif32@^6.7.0` bootloader **boot-loops on this specific board** with `rst:0x3 (RTC_SW_SYS_RST)` before user code runs. Do not waste time re-trying PIO for the CrowPanel. The arduino-cli toolchain produces a bootloader that does work. |

Directories `crowpanel-ui/` and `crowpanel-baseline/` are **dead PIO experiments** kept for reference only. Live CrowPanel code is `crowpanel-arduino/RaceDash/`.

## Build & flash commands

### Teensy (PlatformIO)
```powershell
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" run -t upload   # build + flash
& "$env:USERPROFILE\.platformio\penv\Scripts\platformio.exe" device monitor  # USB serial (COM4)
```
`monitor_port = COM4` is pinned in [platformio.ini](platformio.ini) so the monitor doesn't grab the CrowPanel's CH340 instead.

### CrowPanel (arduino-cli)
The CLI ships with Arduino IDE 2.x at `C:\Users\ChrisRawlings\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe`. The exact FQBN matters — every option is required:

```
esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,FlashSize=4M,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none
```

```powershell
$cli = "C:\Users\ChrisRawlings\AppData\Local\Programs\Arduino IDE\resources\app\lib\backend\resources\arduino-cli.exe"
& $cli compile --fqbn $fqbn $sketch
& $cli upload  --fqbn $fqbn -p COM3 $sketch
```

**Multi-board (one source tree, THREE display panels).** `RaceDash.ino` is panel-agnostic; the
only board-specific values (RGB pin map, panel timing + sync polarities, touch I2C pins,
backlight method, LCD-reset method) live in `crowpanel-arduino/RaceDash/board_config.h`,
selected at compile time by `-DDASH_BOARD`. **Each panel also needs its own FlashSize** in the
FQBN:

| `-DDASH_BOARD` | board id | panel | FlashSize | notes |
| --- | --- | --- | --- | --- |
| `7` (default) | `crowpanel7` | CrowPanel 7.0" V3.0 (Basic RGB) | `4M` | PCA9557 reset, touch 19/20, GPIO2 PWM backlight |
| `5` | `crowpanel5` | CrowPanel 5.0" V3.0 (Basic RGB) | `4M` | same family as 7", different pin map/porches |
| `51` | `crowpanel5adv` | CrowPanel **Advance** 5.0" (HMI IPS, N16R8) | `16M` | inverted sync polarity, touch 15/16, **I2C 0x30 coprocessor** backlight, NO PCA9557, GPIO 38 is an RGB data pin |

Use `compiler.cpp.extra_flags` (empty by default), NOT `build.extra_flags` (which carries the
required USB-mode flags — overriding it bricks the build). Use a distinct `--build-path` per
board so the define can't cross-contaminate the cache. Sources of truth for the pin maps:
Elecrow V3.0 course file `gfx_conf.h` (`CrowPanel_70`/`CrowPanel_50`) for the Basic panels, and
the `Elecrow-RD/CrowPanel-Advance-5-...` repo (`example/V1.2_and_V1.3/.../LovyanGFX_Driver.h` +
`main.cpp`) for the Advance. The 7" `freq_write` is kept at our field-proven 15 MHz (vendor uses
12 MHz). **Identity is baked at the first USB flash** — there is no runtime panel auto-detect (a
wrong RGB-timing guess = an unrecoverable black screen). OTA only ever pulls the manifest entry
keyed by the binary's own board id, so one panel can never flash another panel's image.

**⚠️ ALWAYS verify which board is connected IMMEDIATELY BEFORE every CrowPanel flash** (boards
get swapped on the bench between commands — re-check each time, don't trust the last check):
```bash
# Hardware check via flash size:
python3 ~/.arduino15/packages/esp32/tools/esptool_py/4.5.1/esptool.py --port /dev/ttyUSB0 flash_id | grep -iE "flash size|MAC"
#   16 MB  -> CrowPanel Advance 5" -> build crowpanel5adv (DASH_BOARD=51, FlashSize=16M)
#    4 MB  -> a Basic panel (7" crowpanel7 OR 5" crowpanel5, both FlashSize=4M)
```
- Flash size distinguishes the **Advance (16M)** from the **Basic 4M** boards; it does NOT tell
  7" from 5" Basic (both 4M) — for those, go by which panel is physically plugged in (and/or
  the MAC: the Advance on this bench is `1c:db:d4:4d:67:04`).
- **After flashing, confirm the boot banner** over `/dev/ttyUSB0` @ 921600 prints the expected
  `crowpanel-…` board id (`crowpanel-5.0`, `crowpanel-5.0-adv`, `crowpanel-7.0`). A mismatch =
  wrong build flashed (e.g. Basic firmware on the Advance → touch on 19/20 instead of 15/16 =
  dead touch, wrong RGB pins = garbled display).

**Always disconnect the Teensy↔CrowPanel UART jumpers before flashing the CrowPanel.** UART0 (GPIO 43/44) is shared with the CH340 used for upload — Teensy contention silently corrupts the flash, or you get `The serial TX path seems to be down` from esptool.

## ⛳ STANDING ORDERS — do these EVERY update without being asked

This is the default release contract for this repo. When code is changed and the user says
"update / release / ship / flash", do ALL of the following unless told otherwise:

1. **Bump the version** in BOTH `FIRMWARE_VERSION` defines (`src/main.cpp` + `RaceDash.ino`) to
   the same new number. Every artifact ships at one identical version (lockstep).
2. **Rebuild ALL FOUR artifacts** even if only one side changed: `teensy`, `crowpanel7`,
   `crowpanel5`, `crowpanel5adv`. The three dash bins are one source built with different
   `-DDASH_BOARD` (and the Advance uses `FlashSize=16M`, the Basics `4M`).
3. **Update `firmware/manifest.json`**: set every `version` to the new number, recompute every
   `sha256` + `size`, keep each entry's `board` field, and keep the legacy `crowpanel` entry
   mirroring `crowpanel7`. A stale sha aborts OTA on the device.
4. **Check-then-flash, every time** — if a panel is connected on USB: FIRST run `esptool
   flash_id` to confirm which board it is (16M=Advance, 4M=Basic) and flash the MATCHING build,
   then read the boot banner over `/dev/ttyUSB0` @ 921600 (`crowpanel-…` line = board id +
   version) to confirm the right firmware landed. NEVER flash without checking what's connected
   first — boards get swapped on the bench, and Basic firmware on the Advance kills touch +
   garbles the display. Confirm new UI with the user when the serial log alone can't.
5. **Commit + push** to `origin/main` (HTTPS token; `HOME=/root` or an explicit token URL), then
   curl the raw manifest to confirm GitHub serves the new version + matching hashes.
6. **Keep CLAUDE.md current** — if the change adds a board, a setting/NVS key, a wire-protocol
   line, or a maintenance step, update the relevant section here in the same commit.
7. **board_config.h is the seam** — anything panel-specific goes there or behind a
   `#if DASH_IS_ADVANCE` guard, never a forked copy of `RaceDash.ino`.
8. **NVS keys are short + append-only** — add new `Preferences` keys to BOTH `loadSettings()`
   and `saveSettings()`; never repurpose an existing key's meaning.

## OTA is now served from OUR server (racecar.api.blueuc.com), not GitHub (v0.1.74+)

As of **v0.1.73** the dash `OTA_MANIFEST_URL` points at
`https://racecar.api.blueuc.com/firmware/manifest.json` (see `RaceDash.ino`).
The FastAPI server (`server/app/main.py`) hosts the OTA feed:
`GET /firmware/manifest.json` (served **`Cache-Control: no-store`** → instantly
fresh, no CDN lag), `GET /firmware/{file}`, and `POST /firmware/upload?name=<f>`
(gated by `X-API-Key` == the server's `RACECAR_API_KEY`). Files live in
`RACECAR_DATA_DIR/firmware/`.

**Why:** `raw.githubusercontent.com` is fronted by Fastly, which ignores
query-string cache-busting AND client `no-cache` headers and serves ~5 min
stale after a push — so a freshly published version wasn't visible to devices
for minutes. The server's `no-store` fixes that outright.

**Publishing a server release** (after building all four — see [BUILD.md](BUILD.md)):
```bash
export RACECAR_API_KEY=$(grep -oE '[0-9a-f]{64}' /home/chris/racecar-tools/secrets/racecar_api_key.env)
./server/publish_firmware.sh <version> https://racecar.api.blueuc.com
```
That uploads the four binaries then a server-pointed manifest (artifact URLs =
`<base>/firmware/<file>`, legacy `crowpanel` alias = `crowpanel7`) and verifies. **A future
agent can ship OTA entirely from this build host — no laptop, no SSH** (the server is reached
over public HTTPS).

**Keys & server env (the auth landmine — read this):** firmware-upload auth and dash
session-upload auth are DELIBERATELY separate. Reusing one key 401'd every dash session upload
mid-stream (the “uploads die at ~70 lines” bug).
- `POST /firmware/upload` is gated by server env **`RACECAR_FIRMWARE_KEY`**. Its value is the
  64-hex key stored OFF the repo at **`/home/chris/racecar-tools/secrets/racecar_api_key.env`**
  (never committed). `publish_firmware.sh` sends it as `X-API-Key` (via the `RACECAR_API_KEY`
  shell var — confusingly named, but it must hold the FIRMWARE key).
- `POST /upload` (dash sessions) is gated by **`RACECAR_API_KEY`** (leave EMPTY = open dev mode,
  which is how it ships) OR any valid per-user account key (the dash sends its account key as
  `X-API-Key`). Setting `RACECAR_API_KEY` does NOT break dash uploads anymore (server accepts
  per-user keys), but keep firmware on its own `RACECAR_FIRMWARE_KEY`.

**Server box + one-click update (v: admin update button).** The repo on the server host lives at
**`/docker/racecar.api.blueuc.com`** (so `server/` = `/docker/racecar.api.blueuc.com/server`,
data volume = `.../server/data`). The `/admin` header has an **update server** button, but the
app runs INSIDE the container and cannot rebuild itself (no docker socket, no git checkout, and
`compose up --build` would kill the request). So it's split: `POST /admin/update` writes
`<data>/update_request.json`; **`server/host_updater.sh`** runs on the HOST from a systemd timer,
executes `git pull && docker compose -f docker-compose.prod.yml up -d --build`, and reports
`pulling/building/done/failed` via `<data>/update_status.json`. The button polls
`GET /admin/update/status` and declares success when `running_since` (`_PROC_START`) JUMPS — a
rebuild replaces the process, which is proof the update landed. The script is **self-locating**
(it derives repo/data/compose paths from its own location), consumes the request BEFORE running
so a failure can't loop, and uses `flock`. Install once, in place — do NOT copy it to
/usr/local/bin (that breaks self-location):
```bash
cd /docker/racecar.api.blueuc.com && sudo ./server/host_updater.sh --install   # systemd timer, 30 s
./server/host_updater.sh --status     # show detected paths + last result
./server/host_updater.sh --now        # force an update now
```

**Redeploying the server** (only when `server/app/main.py` etc. change; on the box hosting
`racecar.api.blueuc.com`, external nginx `proxy_default` already routes `/firmware/*`):
```bash
cd /path/to/racecar-35 && git pull origin main
cd server && docker compose -f docker-compose.prod.yml up -d --build
curl -s https://racecar.api.blueuc.com/firmware/list      # {"firmware":[...]}  = live
```
Data (incl. `firmware/`) persists in the `./data` volume across rebuilds.

### Admin: reassign a session to another user

Sessions live at `/data/sessions/<safe_name(email)>/<file>.ndjson`. An **admin** can move a
session to a different account (e.g. “someone drove my car — give them the data”) from the
**review page header**: a `reassign` control (target dropdown, admin-only, shown after `/me`
reports `is_admin`) POSTs `/admin/sessions/move {user, filename, target}`. The handler
`shutil.move`s the `.ndjson` into `session_dir_for(target)` **and moves the matching
`ai_history/<user>/<file>.json` alongside it**, tidies empty source dirs, and refuses (409) if
the target already has a same-named session (never silently overwrites). Targets come from
`GET /admin/sessions/targets`: for an **admin/view-all** viewer = `allowed_emails()` (known
accounts) ∪ every session-owning dir (orphan owners by slug), deduped + alphabetical; for a
**regular** viewer = only the dirs they may view. The review-page combobox is a custom
filterable dropdown (click = full list, type = substring filter, ↑↓/Enter/Esc). On success the
browser redirects to the new `/review/<new_slug>/<file>` URL. `admin_move_session` is
`require_admin`-gated; the targets list is `require_web_user` + view-scoped.
(NOTE: there is no `all_known_users()` — the helper is `allowed_emails()`; using the wrong name
500s the endpoint → empty combobox → “no users found”.)

### Admin: impersonate a user + login audit + activity report

**Impersonation.** From the `/admin` user list, each non-self row has an **impersonate** button
(`POST /admin/impersonate {email}`). It re-issues the signed session cookie with `imp=<target>`
+ `imp_by=<admin>` (base fields stay the real admin). `current_user()` then reports the target
for ALL view/authorization logic (so the admin literally browses as that user and temporarily
loses admin powers). A global `@app.middleware("http")` injects a **fixed red badge (bottom-
right, every HTML page)**; clicking it → confirm → `GET /impersonate/stop` restores the admin
from the cookie's base fields. `_session_payload()` reads the raw cookie incl. `imp`/`imp_by`;
`current_user()` is the effective (possibly impersonated) user. Only real admins can START
(require_admin runs before impersonation is active); self-impersonation is rejected.

**Login audit.** Every real OAuth sign-in calls `record_login()` → appends `{ts,email,ip,ua,event}`
to `/data/logins/<safe_name(email)>.jsonl` (per-user, append-only; `x-forwarded-for` aware).
Admin rows have a **history** button → `/admin/user/<email>/history` (page) / `/admin/user/<email>/logins`
(JSON). Impersonation is NOT logged as the target's login (it's an admin action).

**Report.** `/admin` header **report** button → `/admin/report`: metric cards (tracked users,
total sign-ins, active 7d/30d, sign-ins 7d/30d, new users 30d) + a per-user table (total / 30d /
7d logins, distinct active days, first & last seen) built from `login_stats_all()`.

### S/F line picker on the server (`/tools/sfpicker`, admin-page link)
Served copy of `tools/track_sf_picker.html` living at `server/app/sf_picker.html` (the Docker
image only ships `app/`). Login-gated. Satellite imagery + 2-click S/F line + centre; **track
dropdown embeds the CURRENT firmware `TRACKS[]`** (all rows incl. hidden aux variants) and
draws the baked S/F in RED (line, or point + dashed 75 m radius circle = "needs a line!") so a
misplaced pin is obvious; blue circle = auto-select radius; output line pre-fills the selected
track's name. **⚠️ When `TRACKS[]` changes in RaceDash.ino, regenerate the picker's embedded
JS copy in the same commit** (parse rows → JSON → `const TRACKS = [...]` in sf_picker.html).
Workflow: user picks the line → pastes the generated row into chat → coords get baked into
firmware (keep the existing facility centre/radius; only the sf endpoints change).

### Combine sessions + YouTube overlay viewer (server)

- **Combine**: index page has a checkbox column + `combine selected` toolbar button →
  `POST /sessions/combine {user, files[≥2]}` concatenates the files in session-id (time)
  order into a new `<sid>_<track>-combined.ndjson` (originals kept; absolute `t`
  timestamps make plain concatenation valid). Owner-or-admin (`gate_delete_dir`).
- **Video link**: review-page header takes a YouTube URL → `POST /sessions/<u>/<f>/video`
  (owner-or-admin) stores a sidecar `/data/video_meta/<user>/<file>.json`
  `{id,url,offset_ms}`; server parses the id from any URL shape (`_parse_youtube_id`).
  `GET …/video` is view-gated. Linking reveals a **▶ overlay** button.
- **Overlay viewer** `GET /overlay/<u>/<f>` (`_OVERLAY_HTML`): full-screen YouTube IFrame +
  HTML HUD on top — big MPH, RPM bar, lat/long g, canvas track map with live dot, lap # +
  running lap time + last/best (reuses `/laps`). Sync model: `data_rel_s = video_s +
  offset_ms/1000`. Controls (auto-hide bar): coarse slider ±5 min, fine nudges
  ±0.05/1/10 s, **SYNC @ LAUNCH** (scrub video to the car launching, click — offset pinned
  to the data's first sustained >15 mph), SAVE persists. HUD ticks 20 Hz off
  `player.getCurrentTime()` + binary-search sample lookup.
- **Public share links** (`/shared/<token>`): review-page **share** button (visible once a
  video is linked) mints an opaque token (`/data/shares/<token>.json` → {user, filename});
  `GET /shared/<token>` serves the overlay **read-only** (`__RO__=1` hides all sync/save UI)
  and `/shared/<token>/{data,laps,video}` feed it with **no auth**. Read-only is structural:
  everything under /shared is GET-only and all mutating endpoints stay behind owner-or-admin
  auth. Create/read/revoke of the token itself = owner-or-admin (`gate_delete_dir`); revoke
  kills the link instantly (404). Data/laps logic shared with the authed routes via
  `_session_data_payload()`/`_laps_payload()`.
- **Delete is owner-only however you authenticate** (security fix): per-user API keys now
  pass `can_delete_dir(key_owner, dir)` — a viewer's own key can no longer delete other
  users' sessions. Firmware/master key stays exempt (dash deletes its own uploads).

### AI corner analysis on the review page (v-server, Open WebUI @ ai.blueuc.com)

The review page (`/review/<user>/<file>`) has an **AI Corner Analysis** card: the user clicks
**circle a section**, lassos a corner (or a set of corners) on the Leaflet track map, and the
telemetry inside that polygon — **across every lap** — is analyzed by an LLM for coaching (entry/
exit speed, brake zones, best line, consistency). Server side (`server/app/main.py`):
- `POST /sessions/<u>/<f>/ai` body `{prompt, region:{points:[[lat,lon],…]}, model?}`. Runs
  `_region_metrics()` (point-in-poly filter + per-lap entry/min/exit/max speed, time, distance,
  peak lateral/longitudinal g, max rpm using the SAME `_detect_laps` boundaries the review UI
  uses), builds a race-engineer prompt, and calls Open WebUI's OpenAI-compatible
  `POST {base}/api/chat/completions` (Bearer key). Returns `{ok, model, metrics, answer}`.
- `GET /ai/models` → `{enabled, default, models[]}` feeds the review UI's model dropdown.
- **Config is env-only** (no rebuild, just `docker compose … up -d`): `RACECAR_AI_API_KEY`
  (blank → the whole AI card is hidden), `RACECAR_AI_BASE_URL` (default `https://ai.blueuc.com`),
  **`RACECAR_AI_MODEL` = the DEFAULT model id** (Open WebUI hosts many models, so this is
  required to name the fallback; users can override per-question from the dropdown),
  `RACECAR_AI_TIMEOUT_SECONDS` (120). Keys documented in `server/.env.example` + `server/README.md`.
- **AI cost is captured + ADMIN-ONLY visible**: `_ai_chat` returns `(answer, model, usage)` —
  usage harvested from the payload `usage` block AND the Open WebUI `<details>` footer (parsed
  BEFORE stripping). Stored in every history entry; `_hist_public()` strips it for non-admins
  (`_req_is_admin`; dev mode = admin). Review meta line + lineview cost line show `$x · N tok`.
- **Lineview AI commentary**: the ideal line is pure DATA (fastest real traverse); `POST
  /sessions/{u}/{f}/lines/ai` layers the model on top (shared `_rank_lines()` with /lines —
  compact metric table in, ≤180-word markdown out), appended to the session AI history. Popout
  button "AI: analyze this line".
- **Answers render as real Markdown** (review page `md()`): tables (thead + numeric cells
  right-aligned, striped/hover rows), `##`→h2 / `###`+→h3, ul/ol lists, hr, paragraphs,
  bold/italic/code — escape-first so the model can't inject markup. The system prompt asks the
  model for clean Markdown tables. JS golden-tested via node against the AST-evaluated page
  string (the `\\` in main.py are Python escapes — test the EVALUATED string, not the file).
- The lasso disables `map.dragging` while drawing; client-side point-in-poly shows a live
  “N points in region” count before asking. No new Python deps (uses stdlib `urllib`).
- **Cross-session references + racing-line popout (lineview).** Circling a region now also
  mines up to 12 most-recent sessions on the SAME track (`_track_key()` from the filename,
  '-combined' aware) via `_lap_library()` → `_region_traverses()` (per-lap region pass, window
  extended ~4 s before/2.5 s after so pre-region braking is captured; brake point = walk back up
  the smoothed decel slope from the apex; respects lap exclusions): up to **10 FASTER + 10
  SIMILAR** reference laps are embedded in the AI prompt as comparison tables (body
  `{refs:false}` opts out). **`POST /sessions/{u}/{f}/lines` + `GET /lineview/{u}/{f}`**
  (view-gated, `?pts=lat,lon|…`) = popout satellite window: IDEAL line (fastest REAL traverse
  on record) in green w/ mph labels + BRAKE/APEX/THROTTLE markers, your session-best in blue w/
  your brake point, grey refs, delta card + table. Review page **"ideal line ↗"** button (next
  to the lasso) opens it with the current polygon.
- **Persistent per-session history + cascade delete:** every Q&A is appended to
  `RACECAR_DATA_DIR/ai_history/<user>/<sessionfile>.json` and rendered as a collapsible list
  (newest first; per-item *show region* redraws the polygon, *delete* = `POST .../ai/delete`).
  `GET .../ai/history` loads it on page open. **`delete_session()` calls `_ai_history_delete_file()`
  so deleting a session wipes its AI history too.** Open WebUI appends a `<details>` cost/token
  footer (admin-only) to replies — `_ai_chat()` strips ALL `<details>…</details>` blocks before
  storing/returning, so only coaching text is kept.

**git push from this host:** remote is `https://github.com/teknoprep/racecar-35.git` (HTTPS token
auth). `$HOME=/home/chris` here; push with an explicit token URL
`https://teknoprep:<token>@github.com/teknoprep/racecar-35.git` (the token is supplied in chat by
the user each session — do not hardcode it into files).

**Two channels, deliberately:** the GitHub `firmware/manifest.json` is a **frozen
bridge** so any panel still on ≤0.1.72 can OTA from GitHub and then hop to the
server. The bridge was re-pinned from 0.1.73 to **Teensy 0.1.99 + dash 0.1.98**
(commit `a299699`, immutable pinned URLs): the 0.1.73 Teensy hex still had the
BROKEN FlasherX `FLASH_RESERVE` — a unit bridging through 0.1.73 would exit with
a Teensy whose updater can't accept any modern (≥~224 KB) image from the server
(`addr_too_large`, stuck forever). The 0.1.99 Teensy is the `-Os` rescue build
(204,800 B flash image — fits THROUGH the broken updater) that CONTAINS the
FLASH_RESERVE fix, so bridged units can then take current-size images. The dash
entries are the 0.1.98 bins (the a299699 rescue commit only rebuilt the Teensy)
— any ≥0.1.73 dash reads the server, which is all the bridge needs.
Everything current ships **server-only** via `publish_firmware.sh`; new versions
are NOT added to the GitHub manifest, and **the repo `firmware/` binaries must
STAY at the bridge build to match its frozen manifest — do NOT `git add
firmware/` in release commits** (pre-0.1.71 units fetch the main-branch paths
and sha-verify against the frozen manifest; committing new artifacts there
breaks them). The lockstep rules below still apply (bump BOTH
`FIRMWARE_VERSION` defines, build ALL FOUR, same version) — only the publish
destination changed.

## OTA release (GitHub path — used through v0.1.73; server path above supersedes it) — ⚠️ ALWAYS bump ALL FOUR artifacts to the SAME version

The dash OTA pulls `firmware/manifest.json` from GitHub raw and flashes whichever artifact's
manifest `version` is newer than what's installed. There are **FOUR** release artifacts —
`teensy`, `crowpanel7` (7" Basic), `crowpanel5` (5" Basic), `crowpanel5adv` (5" Advance IPS) —
plus a legacy `crowpanel` alias (below). **They MUST always be released at the SAME version
number, even if only one side's code changed.** If you touch *any* of `src/main.cpp`,
`RaceDash.ino`, or `board_config.h`, you rebuild *all four*, bump *both* `FIRMWARE_VERSION`
defines to the same number, and republish *all four* artifacts.

Each CrowPanel binary carries its board id (`crowpanel7`/`crowpanel5`/`crowpanel5adv`) baked in
at compile time, reads only the manifest entry keyed by that id, and refuses an entry whose
`board` field doesn't match (belt-and-suspenders against a mis-pointed URL). The legacy
`crowpanel` key mirrors `crowpanel7` so already-deployed 0.1.60 7" units (whose old firmware
reads the bare `crowpanel` key) can still OTA forward; keep it pointing at the 7" bin.

Full publish procedure (Linux host; `$HOME` is unset in this shell — prefix git ops with
`HOME=/root` OR push to an explicit token URL):
**⚠️ Size ceiling = the OTA app slot, not the hardware.** PartitionScheme=default gives two
1,310,720 B A/B app slots + 1.4 MB SPIFFS we never use. OTA can never rewrite the partition
table (USB flash only), so 1.31 MB is the release ceiling until deployed 4M panels get
bench-flashed. The `.bin` is partition-agnostic — release builds keep PartitionScheme=default.
**USB/bench flashes should use `PartitionScheme=min_spiffs`** (same A/B OTA, app slots grow to
1,966,080 B); an oversized OTA to a not-yet-migrated unit fails cleanly, no brick. Full
explanation in BUILD.md §1f.

**⚠️ Publishing now uses a STAGING dir** — `RACECAR_FW_DIR=/tmp/fwstage
./server/publish_firmware.sh <ver>` after copying fresh builds there. The repo
`firmware/` dir is the frozen GitHub bridge and must not be overwritten.

```bash
NEW=0.1.63   # pick the next version

# 1. bump BOTH version defines to the same number
sed -i "s/#define FIRMWARE_VERSION .*/#define FIRMWARE_VERSION \"$NEW\"/" src/main.cpp
sed -i "s/#define FIRMWARE_VERSION .*/#define FIRMWARE_VERSION \"$NEW\"/" crowpanel-arduino/RaceDash/RaceDash.ino

# 2. build Teensy -> firmware/teensy41-dash.hex
#    ⚠️ BUILD THE TEENSY *AFTER* THE sed VERSION BUMP. Bug that shipped 0.1.94:
#    the Teensy hex was copied from a build made BEFORE the bump, so the manifest
#    said 0.1.94 but the binary reported 0.1.93 -> the dash re-flashed the Teensy
#    forever (version never matched). ALWAYS verify the version string is
#    actually inside the hex before publishing (Intel HEX = hex-encoded, so you
#    MUST decode the data records first; a raw grep finds nothing):
#      python3 -c "d=bytearray()\nfor l in open('firmware/teensy41-dash.hex'):\n l=l.strip()\n if l[:1]!=':' or int(l[7:9],16)!=0: continue\n n=int(l[1:3],16)\n d+=bytes(int(l[9+i*2:11+i*2],16) for i in range(n))\nimport re;print(sorted(set(m.group().decode() for m in re.finditer(rb'0[.]1[.]\\d\\d',bytes(d)))))"
~/.local/bin/pio run && cp .pio/build/teensy41/firmware.hex firmware/teensy41-dash.hex

# 3. build ALL THREE dash variants (APP bin RaceDash.ino.bin). Distinct --build-path each.
#    NOTE the per-board FlashSize: Advance=16M, Basics=4M.
cli=~/.local/bin/arduino-cli
base="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=default,MSCOnBoot=default,DFUOnBoot=default,UploadMode=default,CPUFreq=240,FlashMode=qio,PartitionScheme=default,DebugLevel=none,PSRAM=opi,LoopCore=1,EventsCore=1,EraseFlash=none"
# NimBLE trim (v0.1.109): central+observer only; must go to BOTH c and cpp flags (~15.5 KB saved)
trim="-DCONFIG_BT_NIMBLE_ROLE_PERIPHERAL_DISABLED -DCONFIG_BT_NIMBLE_ROLE_BROADCASTER_DISABLED -DCONFIG_BT_NIMBLE_MAX_CONNECTIONS=1"
$cli compile --fqbn "${base},FlashSize=4M"  --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=7 $trim"  --build-property "compiler.c.extra_flags=$trim" --build-path /tmp/rd7_build  --output-dir /tmp/rd7_out  crowpanel-arduino/RaceDash
$cli compile --fqbn "${base},FlashSize=4M"  --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=5 $trim"  --build-property "compiler.c.extra_flags=$trim" --build-path /tmp/rd5_build  --output-dir /tmp/rd5_out  crowpanel-arduino/RaceDash
$cli compile --fqbn "${base},FlashSize=16M" --build-property "compiler.cpp.extra_flags=-DDASH_BOARD=51 $trim" --build-property "compiler.c.extra_flags=$trim" --build-path /tmp/rdadv_build --output-dir /tmp/rdadv_out crowpanel-arduino/RaceDash
cp /tmp/rd7_out/RaceDash.ino.bin   firmware/crowpanel7-dash.bin
cp /tmp/rd5_out/RaceDash.ino.bin   firmware/crowpanel5-dash.bin
cp /tmp/rdadv_out/RaceDash.ino.bin firmware/crowpanel5adv-dash.bin

# 4. update firmware/manifest.json: every version=$NEW, recompute every sha256+size, keep each
#    entry's "board" field, keep legacy "crowpanel" == "crowpanel7".
sha256sum firmware/teensy41-dash.hex firmware/crowpanel7-dash.bin firmware/crowpanel5-dash.bin firmware/crowpanel5adv-dash.bin
stat -c%s  firmware/teensy41-dash.hex firmware/crowpanel7-dash.bin firmware/crowpanel5-dash.bin firmware/crowpanel5adv-dash.bin

# 5. commit + push, then verify GitHub raw serves the new manifest + matching hashes
HOME=/root git add src/main.cpp crowpanel-arduino/RaceDash/RaceDash.ino crowpanel-arduino/RaceDash/board_config.h firmware/
HOME=/root git commit -m "Release v$NEW: <what changed>"
HOME=/root git push origin main
curl -s https://raw.githubusercontent.com/teknoprep/racecar-35/main/firmware/manifest.json
```
The manifest's version checks are independent in code, but **operationally they are
lockstep** — keep all four (+ the legacy alias) equal.

## Hardware constants that bit us repeatedly

- **CrowPanel rev:** V3.0 (silkscreened on the back). V3.0 has a **PCA9557 I²C IO expander at 0x18** that holds the LCD in reset until IO0 is driven high. V1.X / V2.X don't. Source per rev lives at `_vendor/CrowPanel-ESP32-Display-Course-File/CrowPanel_ESP32_Tutorial/Code/{V1.X,V2.X,7.0 v3.0 touch new code}/`. **V3.0 uses 15 MHz pclk** (V2.X uses 12 MHz — the configs look interchangeable but are not).
- **UART0 (GPIO 43/44) on the CrowPanel is shared with the CH340** used for USB upload.
- **Backlight (GPIO 2)** must be initialised after the PCA9557 init releases LCD reset. Pattern: `ledcSetup(1, 300, 8); ledcAttachPin(2, 1); ledcWrite(1, 0);` then `pinMode(2, OUTPUT); digitalWrite(2, LOW); delay(500); digitalWrite(2, HIGH);`. The LEDC setup is effectively a no-op once `pinMode` runs — backlight is GPIO HIGH.
- **GT911 capacitive touch — READ THE DEDICATED SECTION BELOW ("GT911 touch").** Short version: do **NOT** use LovyanGFX's built-in `Touch_GT911` — it runs on `I2C_NUM_1` and its init steals the GPIO 19/20 pin routing from `Wire`, killing touch. We drive the GT911 with **TAMC_GT911 on `Wire` (I2C_NUM_0)** instead, and the I²C address **varies by unit batch** (0x5D or 0x14) so we auto-detect it. SDA=19, SCL=20, RST not on a dedicated GPIO (GPIO 38 held low at boot per vendor).
- **Documents folder is OneDrive-redirected** to `C:\Users\ChrisRawlings\OneDrive - BlueCloud Consultants\Documents\` on this machine. Arduino libraries live there, NOT under `~/Documents/Arduino`. arduino-cli's `directories.user` is set to that OneDrive path.
- **PCA9557 library is not in the Arduino registry** — bundled in `_vendor/.../File/PCA9557/` and copied into the libraries dir with a hand-written `library.properties`.
- **COM port mapping on this machine:** COM3 = CrowPanel (CH340), COM4 = Teensy (USB-CDC native).
- **Teensy 4.1 pin assignments in use** — keep these straight before claiming a "free" pin:
  - **Serial2** (RX 7, TX 8): u-blox GNSS UART
  - **Serial3** (TX 14, RX 15): bidirectional dash link to CrowPanel UART0
  - **Pin 9**: tach input via opto (`FreqMeasureMulti` FlexPWM2_2_B input capture). Used only when `sensor_type == 0` (Direct); MS3Pro CAN supplies RPM when `== 1`. **⚠️ Do NOT use the plain `FreqMeasure` lib here** — on T4.x (`__IMXRT1062__`) it is hard-wired to **pin 22** (FlexPWM4 CH0-A, = our CAN1 TX) and silently ignores pin 9, so it reads 0 forever. `FreqMeasureMulti.begin(9)` is the only thing that actually captures on pin 9. (This bit us hard: looked like a wiring problem for ages.)
  - **CAN1** (TX 22, RX 23): **MS3Pro MegaSquirt CAN bus** via SN65HVD230 transceiver. See "MS3Pro CAN" section.
  - **Wire / I²C** (SDA 18, SCL 19): MPU-6050 IMU (AD0→GND ⇒ addr 0x68)
  - **A2** (pin 16): oil-pressure transducer (0.5–4.5 V via 10k/20k divider). **A3** (pin 17): coolant NTC thermistor (150 Ω pull-up). Used in Direct sensor mode.
  - **SPI0** (CS 10, MOSI 11, MISO 12, SCK 13): **W5500 ethernet** (when installed)
  - **Pin 5**: W5500 `/INT` (planned)
  - **Pin 6**: W5500 `/RESET` (planned)
  - **SDIO (built-in)**: SD card (pins are dedicated, not on the header)
- **FlasherX OTA staging vs Teensy 4.1 EEPROM emulation (v0.1.99).** PJRC's EEPROM emulation
  lives in flash `0x607C0000..0x607FF000` (63 sectors) and we write it nearly every boot (IMU
  cal). FlasherX's `firmware_buffer_init()` scans DOWN from the top for the first non-erased
  word — with the stock 16 KB `FLASH_RESERVE` it stopped at the topmost EEPROM record and
  squeezed the OTA staging buffer into the ~224 KB (shrinking) sliver above it; when the image
  outgrew that, Teensy OTA aborted with `FW,ERR,addr_too_large`. Fix in `lib/FlasherX/FlashTxx.h`:
  `FLASH_RESERVE = 64*FLASH_SECTOR_SIZE` (256 KB) so the buffer lands in the ~7.5 MB between code
  and the EEPROM region. (0.1.99 itself shipped `-Os`-trimmed to fit through the old broken
  updater; builds are back to `-O2` since 0.1.100.)
- **Optocoupler tach front-end** — PC817-class is fine for typical 4-cyl × 2-pulse-per-rev (≤ 270 Hz at 8 k RPM). The Teensy side needs a 4.7–10 kΩ pull-up from `3V3` to pin 9. Output is *inverted* but `FreqMeasureMulti` counts edges either way. **The signal at pin 9 must be a clean 0→3.3 V logic swing referenced to Teensy GND**: LOW must drop below ~0.8 V (opto transistor fully saturating) and HIGH above ~2.3 V, and it must never go negative. A symptom we hit: the opto not pulling fully low (line sitting ~1.5 V), which reads as a constant HIGH and RPM=0 even though a scope shows a waveform — verify the LOW level and a common ground, not just "there's a signal."

## Wire protocol (Teensy ↔ CrowPanel UART)

Bidirectional, 115200 8N1, line-oriented, `\n`-terminated.

Note: the live baud is **921600** (not 115200) for telemetry + file uploads — both sides must agree.

### Teensy → CrowPanel (telemetry + status)
Up to 25 Hz when GPS PVT is fresh; 1 Hz heartbeat fallback when not.
```
GPS,<fix>,<sats>,<lat_deg>,<lon_deg>,<speed_mph>,<heading_deg>,<gps_status>
ENG,<rpm>,<oil_psi_x10>,<coolant_f_x10>
ECU,<rpm>,<clt_f_x10>,<map_x10>,<tps_x10>,<afr_x10>,<iat_f_x10>,<bat_x10>
IMU,<ax>,<ay>,<az>,<gx>,<gy>,<gz>
SD,REC,<0|1>,<file>,<samples>   (+ SD,READY/FMT/NONE/ERR/ACTIVE status forms)
CLD,<live_ok>,<queue_depth>
CANSNIFF,<0|1>,<file>,<frames>  (CAN sniffer status / live frame count)
TIME,<unix_epoch>               (RTC, 1 Hz)
HLTH,<t_die_x10>,<t_mpu_x10>,<t_esp_x10>,<batt_x10>  (1 Hz device health; batt falls back to the BT dongle's ATRV when no MS3 CAN)
RST,teensy,<reason>             (once at boot: Teensy reset cause)
VER,teensy,<semver>
```
- **`RST,teensy,<reason>`** (once at boot): the Teensy's own reset cause, decoded from `SRC_SRSR`
  (`captureResetReason()`): `POR(power)` / `watchdog` / `lockup/swrst` / `OVERTEMP` / `reset-pin`
  / etc. Shown on the dash STATUS HEALTH bar as `Trst:<reason>` and stamped into the `.dbg` open
  line (`"reset":"..."`). Diagnoses the "Teensy stopped communicating" mystery: a `POR(power)`
  mid-session = the Teensy is **browning out**; `watchdog`/`lockup` = a firmware hang. (FlasherX
  OTA reboots show as `lockup/swrst`.)
- **`HLTH`** (1 Hz, ALWAYS, independent of recording/debug setting): heat/brownout
  diagnostics. `t_die`=Teensy i.MX RT1062 die temp (`tempmonGetTemp()`), `t_mpu`=MPU-6050
  temp (decoded from the IMU burst bytes 6-7 — enclosure ambient proxy), `t_esp`=dash ESP32-S3
  temp (relayed back from its `DTEMP` line), `batt`=MS3 CAN battery volts. All ×10; `-9999`
  temp / `-1` batt = n/a. Shown on the dash STATUS page HEALTH bar (RED >80 °C). Also folded
  into the `.dbg` health line (`t_die`/`t_mpu`/`t_esp`/`batt`). This exists to catch a thermal
  shutdown / brownout that kills the UART link.
- `gps_status`: `0=OFF`, `1=RAW`, `2=OK`, `3=STALE` (see `gpsStatus()`)
- **`ENG` rpm source depends on `sensor_type`**: Direct (0) = opto tach (FreqMeasure / `RPM_PULSES_PER_REV`); MegaSquirt (1) = MS3Pro CAN. `oil_psi_x10` is always the A2 ADC; `coolant_f_x10` is A3 NTC in Direct mode or CAN CLT in MS3 mode. The dash RPM bar always reads `eng.rpm`.
- **`ECU`** carries the full MS3Pro CAN dataset; the dash uses it for coolant/AFR/MAP/TPS when `sensor_type == 1`. `-1` in any field = not-received / fault → dash shows `---`.
- The dash parser tolerates short forms for back-compat (e.g. `ENG` with 1 or 3 fields).
- **Rate-limiting**: emit only when `myGNSS.getPVT(0)` returns true OR every 1 s. `getPVT(0)` is non-blocking — never use the blocking variant or the 25 Hz loop chokes.
- **GPS UART RX buffer (v0.1.66 fix) — DO NOT remove.** Serial2's default RX ring is only tens of bytes — SMALLER than one ~100-byte UBX-NAV-PVT frame. GPS is parsed by *polling* `getPVT()` in `loop()` (RPM is NOT — CAN + `FreqMeasureMulti` are interrupt/FIFO-driven). So any loop stall >~25 ms overflows the ring, desyncs the autoPVT stream, and with a sub-frame buffer it can NEVER re-sync → GPS "freezes at last position" (STALE) while RPM keeps running. The stalls come from **SD `sync()`/`write()` latency during recording** (this is why it only happened once a session started, after "half a lap or two"). Fix: `Serial2.addMemoryForRead(gpsRxBuf, 32768)` in `setup()` BEFORE `begin()` (mirrors the dash Serial3 buffer) — ~8.5 s of slack at 38400 baud so the parser rides through SD spikes. `addMemoryForRead()` is on the concrete `Serial2`, not the `HardwareSerial&` alias.
- **GPS module-reset forensics (v0.1.102).** Auto **UBX-NAV-STATUS** (every 5th nav solution)
  rides the PVT stream; its `msss` (ms since MODULE startup) feeds `navStatusCB()` — if msss goes
  BACKWARDS the u-blox rebooted (power glitch), counted in `gps_module_resets` and logged in the
  `.dbg` health line as `msss`/`mrst`. Requires `myGNSS.checkCallbacks()` in loop(). Re-asserted
  on every re-begin path. Context: the racing-only stales show `avail=0` (module silent, 51% of
  the 1783087863 session, 8-min outages, fine when parked) — mrst>0 = reboots (power/wiring),
  mrst==0 with msss advancing = module alive but mute (UART wire/antenna path).
- **Compressed uploads — "zblocks" (v0.1.127).** The dash→cloud socket is hard-capped ~70 KB/s
  by lwIP's `TCP_SND_BUF=5744` ÷ path RTT (compile-time, not fixable without a core rebuild), so
  the uploader now **deflate-compresses the HTTP body**: `zdeflate.h` (tiny raw-DEFLATE,
  fixed-Huffman + stored fallback, ~3 KB flash, 72 KB PSRAM workspace; host-verified against
  Python zlib, ~4-6× on session NDJSON) compresses 32 KB blocks on the NET TASK; body = frames
  `['Z','B', u32le raw_len, u32le comp_len, raw-deflate]`, header `X-Body-Format: zblocks`.
  **Negotiated per boot via `GET /caps`** (`{"zblocks":true}`) — old server → raw body, so
  update order never matters. Server `_zb_decode()` inflates in `_save_body` (zlib -15;
  malformed frame → 400 reject `zblocks: …`), logs `wire` vs `bytes` (ratio) in the upload
  event. Net effect: the socket carries ~4× more RAW telemetry → the UART wire is the upload
  bottleneck again (~2× end-to-end). **The Tools speed test gained a third pass** (`ram=zb`
  note): synthesizes telemetry-shaped NDJSON, pushes it through the SAME zdeflate+framing, and
  reports EFFECTIVE raw KB/s + ratio (`ZB <n> KB/s (<r>x)` on the Tools page; server logs
  `raw_bytes`/`raw_kbps`/`ratio` in the nettest event — `/nettest` stream-parses frame headers
  without inflating).
- **⚠️ SD garbage collection after a delete kills the NEXT file (fixed v0.1.136).** Bench-proven
  by tapping the dash's TX during a real upload: `Q,GET` → acks reach seq **26** → **90.8 s of
  total silence** → `DBG,uf_timeout state=3 stalled at 5679/0 B` → retry → **15,000 lines at
  250/s without a single pause**. The trigger is the PREVIOUS file's `Q,DEL`: deleting a
  multi-MB file makes the card run internal GC, which then blocks the next file's reads.
  Fixes: (1) **deletes are deferred to the END of the batch** (`uf.del_mask` bitmask,
  `ufFlushPendingDeletes()` at `ufStartCurrentFile()`'s exhaustion path) so GC is off the
  critical path — a lost delete is harmless because the server keys on session id with
  `mode='w'`; (2) the `UF_STREAMING` watchdog now **distinguishes the two stalls**: ring
  DRAINED + net task healthy = Teensy-side stall → retry after **20 s** (waiting does not help);
  ring FULL = network stall → keep the full 90 s patience (v0.1.115).
- **⚠️ Dash reboot → "never re-talks to the Teensy" (fixed v0.1.136).** It was never permanent,
  just 60-120 s: the recovery watchdog waited a **20 s** boot grace then retried every **15 s**
  (3-6 cycles). Compounding it, stray `Q,*` lines from a Teensy stuck in its BLOCKING ARQ
  retransmit loop (which emits NO telemetry) still "parsed", so `uart_last_ok_ms` stayed fresh
  and the watchdog never fired at all. Fixes: only **real telemetry** credits link health
  (`uartLineIsTelemetry()` — GPS/ENG/ECU/IMU/TIME/HLTH/SD/CLD/ETH/VER/RST); first probe at
  **4 s** then every **5 s** until telemetry appears (lazy 15 s once healthy); `Q,ABORT` is sent
  at boot AND on every watchdog poke (the Teensy's ack pump matches it, freeing a zombie stream
  instead of waiting out its 120 s patience); and the boot-time RX FIFO is drained because the
  ROM bootloader runs UART0 at 115200 while the Teensy is already sending at 921600.
- **⚠️ THE OTHER upload killer: a 17-line CFG burst every 5 s (fixed v0.1.135).** Found by
  putting a scope on the actual wire (`/dev/ttyUSB0` @921600 taps the dash's UART0 = the Teensy
  link): the dash was re-sending the ENTIRE config burst — 17 lines, ~500 bytes — **every 5
  seconds, forever**, and the corruption in the capture appeared ONLY inside those bursts
  (isolated `DTEMP`/`TCHrate` lines were always clean). CLAUDE.md has warned since v0.1.79 that
  a CFG burst mid-ARQ desyncs the Teensy's per-line ACK wait and aborts the transfer — the
  guard was `(uf.state != UF_IDLE) || (sl.state != SL_IDLE)`, which **lies**: the dash returns
  to `UF_IDLE` the instant an upload fails while the Teensy keeps retransmitting `Q,L` for up to
  120 s of ARQ patience. So every failed upload got carpet-bombed with CFG until the link
  wedged. Fix: resend is now **event-driven** — `RST,teensy,…` (Teensy announces its own reboot)
  sets `cfg_resend_req`, which is what the periodic blast was really approximating — with a slow
  60 s safety net, and gated on `q_activity_ms` (millis of the last `Q,*` line SEEN FROM the
  Teensy) being quiet for 10 s. Also deleted a leftover `TCHrate touched/s=…` debug printf that
  fired onto UART0 every single second. **Lesson: the dash's own state machine is not evidence
  that the link is free — only Teensy silence is.**
- **⚠️ THE upload killer: the compressor was starving the TLS handshake (fixed v0.1.134).**
  Field signature (0.1.133, RSSI -43, WiFi fine): `ufdiag st=3 net=3 rh=524234 rt=0 er=TCP
  connect failed` — ring FULL (512 KB) with `ring_tail == 0`, i.e. the net task never sent one
  body byte, and the server logged `rx=0`. Cause: `ufNetTask()` called `zbProbeCaps()` +
  `zbEnsure()` FIRST, taking **~73 KB of INTERNAL RAM** (40 KB hash workspace + 16 KB raw +
  17 KB out, internal since 0.1.128) plus a TLS connection for `/caps`, BEFORE
  `ufOpenStream()` ran the real handshake — and mbedtls needs tens of KB of *internal* heap for
  cert parse + TLS buffers. Marginal heap ⇒ intermittent: 22 MB uploads landed on a lucky
  retry, then stopped working entirely. Consequence chain: no socket → ring fills → dash stops
  acking (backpressure) → Teensy stalls → 90 s watchdog → whole-file retry → forever.
  Fix: **socket FIRST, compressor second** — `ufOpenStream()` no longer terminates the headers
  (caller appends `X-Body-Format` + the blank line), compression is only enabled when
  `heap_caps_get_free_size(MALLOC_CAP_INTERNAL) >= ZB_MIN_FREE_INTERNAL` (96 KB) *after* the
  handshake (else stream RAW — slower but always works, server accepts both), connect now
  **retries 3× with a fresh client each attempt** (a failed WiFiClientSecure can be left
  wedged), `zbFree()` runs at task exit so the next file's handshake gets the RAM back, and the
  ufdiag note carries `ih=<free internal KB>`.
  **Progress bar** now reads `ring_head` (bytes pulled off the car) not `ring_tail` (bytes
  ack'd by the socket) — ring_tail is 0 for the whole handshake and stays 0 forever on a
  connect failure, which is exactly the "stuck at 0 then jumps" the driver reported.
- **Upload speed: sliding-window ARQ (v0.1.102).** `handleQGet()` streams with a **go-back-N
  window (QGET_WIN=16→48 lines in flight since v0.1.127 — 16 was ~38 ms of pipe and drained
  faster than the dash's ack cadence, capping the UART hop ≈ 70 KB/s; 48 ≈ 120 ms rides through
  the dash's UI-loop ack latency, ~11 KB still ×3 under the 32 KB RX ring, cumulative ACKs)** instead of stop-and-wait — the old
  one-line-per-round-trip paid the dash's 5–30 ms UI-loop latency per ~220-byte line (20k-line
  session = many minutes); now it's wire-limited (~90 KB/s at 921600, 4 MB ≈ 1 min). The dash
  side needed NO protocol change (it already ACKs the highest CONTIGUOUS seq — a gap re-acks the
  last good one = natural NAK; 32 KB RX ring; window ~5 KB ≪ ring so TCP-flush backpressure just
  stalls the window). `qPumpAcksOnce()` keeps STATIC parse state — ack lines straddle calls now.
- **Sessions page "Upload (n)" (v0.1.102).** Third footer button uploads ONLY the checkbox-
  selected files (`ufStartSelected()` skips Q,LIST) — e.g. push a 300 KB `.dbg` up without
  waiting behind a 4 MB session. Same radio-handover guard as the dash UPLOAD button
  (`net_pending_sel`).
- **GPS dead-at-boot recovery + boot baud scan (v0.1.97 review fixes).** (1) If the boot-time
  connect fails, `gnss_lib_ok=false` used to mean NOTHING ever retried — GPS stayed OFF for the
  whole session (proven on track: two sessions with bogus 2019 epochs = no time source from
  power-on). `gpsStaleWatchdog()` now retries a dead-at-boot GPS every 30 s (bounded ~0.9 s;
  wider baud scan every 6th try) and re-asserts UBX/rate/autoPVT + `saveConfiguration()` on a
  late connect. (2) **Baud landmine**: `saveConfiguration()` (0.1.93) persists a dash-selected
  baud to MODULE flash, so the boot scan must include **230400/115200/460800** too — scanning
  only 38400+9600 left a module saved at 230400 permanently unreachable at every power-up.
- **RPM spike filter (v0.1.98)** — dash setting `RPM spike filter` (ENUM Off/Mild/Normal/Strong,
  NVS `rpmspk`, `CFG,rpmspk,<0-3>`, default Normal). Teensy `pumpTach()` stage 1.5: rejects tach
  pulses implying a faster-than-physics jump from the current filtered RPM (slew limit
  20000/10000/5000 RPM/s for Mild/Normal/Strong, allowance grows with time since last accepted
  pulse, floor 250 RPM). Kills noise BURSTS and plausible-value spikes that the 12 k absolute
  gate + median-of-3 let through. Escape hatch: after 120/200/300 ms of continuous rejection the
  filter resets and accepts the new level — a genuine fast shift lands within ~¼ s, so it can
  never lock out a real RPM change. Tach path only (MS3 CAN RPM is digital/CRC-protected).
- **GPS stale auto-recovery watchdog (v0.1.68).** Belt-and-suspenders net on top of the buffer:
  `gpsStaleWatchdog()` runs every `loop()` and, once the link is up, watches the age of the last
  fresh PVT. **LIGHT** (>2.5 s stale): flush the Serial2 RX ring (non-blocking) so the UBX parser
  drops corrupted/backlogged bytes and resyncs on the next LIVE frame — position jumps to NOW
  instead of replaying the stale backlog; rate-limited to once / 2 s. **HEAVY** (>10 s stale): the
  module itself may have glitched, so re-run `myGNSS.begin()` + re-assert `setUART1Output(UBX)` /
  `setNavigationFrequency` / `setAutoPVT` (blocking ~handshake but bounded; last resort, once / 10 s;
  the 32 KB ring covers the one-time stall). No-op until the first PVT, so it never trips during
  cold-start acquisition. Recovery events log `[gps] STALE …` / `[gps] re-begin …` to USB serial.
- **HEAVY re-begin bounded to ~1.3 s (v0.1.91).** The old "scan all 5 bauds + re-config at
  default ~1.1 s maxWaits" froze the loop **up to ~9.3 s per recovery** (0.1.88 debug logs). A
  glitched/reset module almost always returns at the SAME baud, so the HEAVY path now tries only
  the current baud (600 ms) + reasserts config at 250 ms maxWaits (~1.3 s); the full 5-baud scan
  runs only after 3 consecutive failures (rare, real baud change).
- **STALE ROOT CAUSE + fix (v0.1.93).** Debug logs show the module going **silent** (`avail=0`,
  not a baud/parser issue) for ~10 s repeatedly, then recovering — i.e. the module is **resetting**
  (most likely a **brownout**; same power thread as the BT reboot + Teensy comms death) and coming
  back in its DEFAULT **NMEA** mode, so the Teensy sees no UBX PVT until the watchdog re-asserts
  UBX. Fix: **`myGNSS.saveConfiguration()`** after the initial GPS setup (and after each live
  baud/Hz change) persists UBX-output + nav-rate + auto-PVT + baud to the module's **flash + BBR**,
  so a reset reboots STRAIGHT into UBX auto-PVT and resumes in ~1-2 s instead of ~10 s. Doesn't fix
  the underlying reset (that's power) but kills the long stale. **Confirm brownout via the v0.1.90
  `HLTH` battery voltage** on the STATUS page: if `Batt` dips when GPS goes stale, it's power.
- **GPS baud RAISED to 230400 (v0.1.83) — the real headroom fix.** 25 Hz UBX-NAV-PVT is ~25
  kbit/s; at **38400 that's ~65 % util → zero headroom**, so after any loop stall the backlog
  drains at only ~35 % spare and chronically lags = STALE. At **230400 it's ~11 %** → a full 32 KB
  backlog clears in <1 s. `setup()` connects at whatever baud the module is at (230400/38400/9600),
  then `setSerialRate(230400)` + re-`begin()`. **Live-selectable from the dash** (Settings → *GPS
  baud*, NVS `gpsbaud`, `CFG,gpsbaud,<n>`): the Teensy `applyGpsBaud()` switches the module,
  re-handshakes, and if that fails SCANS known bauds so a bad pick can't brick the link; it replies
  `GPSBAUD,<actual>,<ok>` which the dash shows in the *GPS link* INFO row (`230400 OK` / `NO DATA`).
- **On-SD debug log (v0.1.83) — to actually diagnose stale, not guess.** Each recording writes a
  companion `…​.dbg.ndjson` (same dir → `/queue/` when cloud → auto-uploads via `Q,LIST`, tagged
  `X-File-Kind: debug`, filed under `debug/<user>/` on the server). 1 Hz health line:
  `loop_ms` (worst loop-period = stall), `sdwr_ms` (worst SD write+sync), `fresh` (real PVT/s),
  `avail` (GPS UART backlog), `pvt_age`, `flush`/`rebegin` counts, `samp`. Writes are cached,
  synced only every 5 s so the log can't add the very SD latency it measures. **Server verdict:**
  `GET /sessions/<u>/<f>/debug` returns `_debug_diagnose()` — `fresh=0 & avail≈0` → PHYSICAL/module;
  `fresh=0 & avail high` → CODE/parser; `loop_ms`/`sdwr_ms` spikes → CODE/SD stall. Raw at
  `…/debug/raw`; session GPS-freeze summary at `…/gpsdiag`.

### CrowPanel → Teensy (control)
```
REC,<0|1>          # start/stop recording
TRACK,<name>       # set the current track name (sent immediately before REC,1)
CFG,<key>,<value>  # push settings (incl. srctyp, cloud, inet, and sf = active
                   #   track's S/F LINE: CFG,sf,aLat,aLon,bLat,bLon -> Teensy
                   #   stamps "lap":N into NDJSON via line-crossing).
                   #   NOTE: cl_strm REMOVED in v0.1.66 (live "stream to cloud"
                   #   deleted). Teensy ignores cl_strm if an old dash sends it.
CFG,dbg_on,<0|1>   # (part of CFG) debug logging master switch. 0 = Teensy writes
                   #   NO .dbg health log for a session. NVS key dbg2 on the dash
                   #   (renamed from dbg_on in v0.1.103 to force the new OFF default;
                   #   wire key stays dbg_on).
CFG,srctyp,<0|1|2> # sensor source: 0=Direct, 1=MegaSquirt, 2=Bluetooth OBD-II
DTEMP,<c>          # dash -> Teensy: dash ESP32-S3 die temp (1 Hz, for HLTH/.dbg)
BTD,<clt_f_x10>,<iat_f_x10>,<volt_x10>[,<tps_x10>,<spark_x10>]
                   # 1 Hz BLE-OBD relay while the dongle link is up: coolant/IAT (°F×10),
                   #   battery (V×10), + v0.1.109 throttle (%×10, PID 0111) and timing
                   #   advance (°BTDC×10, PID 010E — knock-retard proxy). Spark can be
                   #   legitimately negative: its no-data sentinel is -1000, others -1.
                   #   Teensy parses the 3-field short form from old dashes. Coolant feeds
                   #   the ENG line + NDJSON when srctyp==2 (fresh ≤10 s); volts back HLTH/
                   #   .dbg; tps/spark land in the session NDJSON as tps_pct/spark_deg
                   #   (tps_pct prefers MS3 CAN when live; spark is BT-only; keys omitted
                   #   when no live source — old parsers unaffected).
SETTIME,<unix>     # set Teensy RTC
TZ,<id>            # timezone id (for SD filenames / metadata)
SDFORMAT           # FAT32-format the SD card
CANSNIFF,<0|1>     # start/stop raw CAN capture to SD (see "CAN sniffer")
TESTSTART/TESTSTOP # synthetic-data session for pipeline testing
Q,LIST / Q,GET / Q,DEL / QUEUE,DRAIN / UPLOAD,CANCEL   # cloud queue control
                   # ⚠️ v0.1.115 RESILIENCE on top: paddock WiFi measurably drops to ZERO
                   #   throughput for 30 s+ windows (server log 07-17: 66 KB/s → 0 → fine).
                   #   Net task rides outages up to 90 s (write-stall cap, was 20 s);
                   #   UART watchdog is RING-AWARE (ring-full = we stopped acking on
                   #   purpose — not a Teensy failure — so it stays fed while the task
                   #   lives); Teensy ARQ patience 60×2 s = 120 s (must exceed the dash's
                   #   90 s); 2 automatic whole-file retries. STATUS page IP row + the
                   #   uf_stream_start DBG line now show WiFi RSSI (> -70 dBm good,
                   #   < -80 dBm = uploads will crawl — move the hotspot).
                   # ⚠️ UPLOAD ARCHITECTURE (v0.1.114): TRUE STREAMING via PSRAM ring +
                   #   dedicated net task. The loop (core 1) pushes Q,L lines into a
                   #   512 KB PSRAM ring and ONLY touches UART; ufNetTask (core 0) owns
                   #   the socket end-to-end — TCP/TLS connect, headers (chunked), body
                   #   chunks (≤16 KB), terminator, response — and drains the ring at
                   #   network speed. File streams car→cloud in ONE continuous pass at
                   #   min(UART, WiFi) rate; ring-full = don't-ack (Teensy ARQ paces the
                   #   wire), so neither link ever blocks the other and the TLS handshake
                   #   no longer stalls the UI. ⚠️ The net task must NEVER Serial.print
                   #   (UART0 = Teensy link; an interleaved print corrupts the Q,A ack
                   #   stream) — it reports via uf.net_err/net_state only, and
                   #   ufStopNetTask() MUST run before ufCloseTcp/ufFreeBuf (task owns
                   #   uf.tcp/uf.buf). Failure = whole-file retry once (ufFailOrRetry /
                   #   UF_RETRY_WAIT + Q,ABORT). openUploadModal() forces BLE off.
                   #   (v0.1.113's store-and-forward /stream append segments are gone;
                   #   the Teensy's Q,GET skip_lines support remains, unused.)
Q,GET,<name>[,<skip_lines>]   # skip_lines (v0.1.113) = segmented pull: Teensy does a
                   #   buffered line-skip, then streams with seq starting at skip+1 (acks
                   #   are absolute line numbers; Teensy inits last_acked=skip).
Q,ABORT            # dash aborts an in-flight Q,GET stream (v0.1.112). Teensy's ack pump
                   #   (qPumpAcksOnce) MUST match this BEFORE the bare "Q,A" prefix (else
                   #   it parses as ack-everything). Exits handleQGet fast, WITHOUT a
                   #   Q,ERR reply (a stray error would kill the dash's scheduled retry).
                   #   Teensy ack patience: 30×2 s = 60 s (was 8×2 s — too short).
FWUPDATE / VER?    # OTA + version query
```
Parsed in `handleDashCommand()` ([src/main.cpp](src/main.cpp)).

**Boot-time gotcha** (Teensy): calling `myGNSS.getFixType()` before the first successful `getPVT()` returns uninitialised heap memory. The emit path gates all PVT reads on `gnss_lib_ok && gnss_last_fresh_ms != 0`.

## Settings persistence (NVS via Arduino `Preferences`)

Namespace `"dash"`. Keys are short to fit NVS limits. Saved on every dash entry (when you swipe back from settings → dash). Full list:

| Key | Type | Purpose |
| --- | --- | --- |
| `rpm_min` / `rpm_max` | uint16 | RPM bar range |
| `alerts` | bool | Master alerts on/off |
| `a1_rpm` / `a1_col` / `a1_hz` | uint16/uint8/uint8 | Alert 1 threshold/colour/blink-Hz |
| `am_rpm` / `am_col` / `am_hz` | uint16/uint8/uint8 | MAX alert ditto |
| `rec_sd` / `rec_cl` | bool | Record-to-SD / record-to-cloud master switches |
| `cl_host` | string | Cloud DNS or IP |
| `cl_port` | uint16 | Cloud port. **Default 443** (HTTPS). Cycling `cl_proto` auto-snaps this to the protocol's well-known port (HTTP→80 / HTTPS→443 / FTP→21), still editable. The `racecar.api.blueuc.com` endpoint is HTTPS-only: port 80 just 301-redirects (which the dash won't follow), so HTTPS+443 is the only working combo. NOTE: existing units have the old `80` stored in NVS — change it on-device or it stays 80. |
| `cl_proto` | uint8 | 0=HTTP, 1=HTTPS, 2=FTP |
| ~~`cl_strm`~~ | — | **REMOVED in v0.1.66.** Live "stream to cloud" was deleted (not ready, and its mid-session POSTs stalled the loop → GPS STALE). Cloud recording is now **always After Race**. Old stored key is orphaned/harmless. |
| `cl_email` / `cl_key` | string | Cloud user email (X-User-Email) + API key (X-API-Key, masked). Migrated from legacy `cl_user`/`cl_pass`. |
| `autost` / `astmph` / `astsec` | bool/uint16/uint16 | **Auto-start recording** (v0.1.131): enable / speed threshold (mph) / **seconds the speed must be HELD continuously** (default 4, range 1-30). The dwell is the fix for spurious starts — the pre-0.1.131 code fired on the FIRST sample above the threshold, so one GPS speed spike or a squirt across the paddock opened a session. ANY dip below the threshold restarts the dwell. The START button turns amber and counts down (`AUTO 3`) while arming. |
| `auto_trk` | bool | Auto-select closest track on START (skip picker if a clear match exists) |
| `inet` | uint8 | Internet routing: 0=Ethernet (Teensy/W5500), 1=WiFi (CrowPanel) |
| `wssid` / `wpass` | string | WiFi SSID / PSK |
| `s_temp` / `t_warn` / `t_col` | bool/uint16/uint8 | Coolant show / warn-°F / warn-colour |
| `s_psi` / `p_warn` / `p_col` | bool/uint16/uint8 | Oil-PSI show / warn-PSI / warn-colour |
| `s_volt` / `v_warn` / `v_col` | bool/uint16/uint8 | **Voltage show / low-warn (V×10, default 128=12.8 V) / warn-colour (v0.1.110).** Source: BT ATRV (srctyp==2) or MS3 CAN bat (==1). Display AND warning are gated on `eng.rpm >= ENGINE_RUNNING_RPM` (500) — parked ignition-on reads ~12.4 V, which is normal, not a dying alternator. The VOLT line shares the AFR dash row (renders only when the AFR line doesn't). |
| `srctyp` | uint8 | **Sensor source: 0=Direct (opto tach + ADC), 1=MegaSquirt (CAN), 2=Bluetooth (BLE OBD-II dongle for slow readings)** |
| `bt_addr` / `bt_atype` / `bt_name` | string/uint8/string | **Paired BLE OBD-II (ELM327) dongle**: address `"aa:bb:.."`, BLE address type, friendly name. Used when `srctyp==2` to auto-reconnect on boot. Set from PAGE_BT_SCAN. |
| `btpid` | uint8 | **Mode-01 PID mapped to the COOLANT function** (default 0x05 = standard ECT). Set from PAGE_PID_SCAN (Sensor page → COOLANT PID button); decoded as A−40 °C. |
| `rpmppr` | uint16 | **Tach pulses/rev ×10** (Direct-mode RPM divider). 20=2.0. Sent to Teensy as `CFG,rpmppr,<x10>`; Teensy divides the opto-tach frequency by `rpmppr/10`. Ignored in MegaSquirt mode (RPM is straight from CAN). |
| `rpmspk` | uint8 | **RPM spike filter** 0=Off 1=Mild 2=Normal 3=Strong (default 2). Sent as `CFG,rpmspk,<v>`; Teensy slew-gates tach pulses (see "RPM spike filter" note above). |
| `gpsflt` | uint8 | **GPS drift filter** 0=Off 1=Mild 2=Normal 3=Strong (default 2, v0.1.119). Sent as `CFG,gpsflt,<v>`. Teensy `applyGpsDriftFilter()` at the emit choke point (dash display + lap timing + NDJSON all filtered): parked GPS wander is LATCHED — below the freeze speed (0.8/1.2/2.0 mph, ~0.5 s) position/heading hold and speed clamps to 0; thaws instantly above the hysteresis speed (1.8/2.5/3.5 mph) OR when the raw fix escapes 8/12/20 m from the latch point (catches speed-less creep). |
| `s_afr` / `afr_lo` / `afr_hi` / `afr_col` | bool/uint16/uint16/uint8 | AFR show / rich-warn×10 / lean-warn×10 / colour (MS3 mode only) |
| `tz` | uint8 | Timezone index into `TIMEZONES[]` |
| `lapov` | uint8 | **Finish-line lap-time popup duration** in seconds, 0–9 (default 3, 0 = off). Dash-only (no CFG). Settings → "Lap time popup (sec)". |
| `dbg2` | bool | **Debug logging master switch** (default **OFF** since v0.1.103 — diagnostic tool, enable when chasing a problem). Sent as `CFG,dbg_on,<0|1>`; when OFF the Teensy writes NO `.dbg` health log. Toggle: Settings → "Debug logging (SD)". Renamed from `dbg_on` (which had ON persisted on deployed units) so the new default takes effect everywhere; old key orphaned, never repurposed. |
| `sf_unk` | blob | **UNKNOWN-track S/F** (one `SfOverride`, v0.1.129) — the ONLY on-car S/F capture left. SET S/F (STATUS page / dash TRACK button while recording) works ONLY when `lapTrackIdx() < 0`; lap timing + Teensy `CFG,sf` stamping run against it at unmapped tracks. DELETE S/F clears it. |
| `sf_ovr` | blob | **Per-track start/finish overrides** — array of `{used,lat,lon,lat2,lon2}` (a LINE; v0.1.82 grew it from a point) sized `N_TRACKS`, keyed by `TRACKS[]` index. **⚠️ IGNORED since v0.1.129 for KNOWN tracks** — the baked S/F (web-managed via `/tools/sfpicker`) is the ONLY source; a stale on-device capture used to silently beat a freshly baked line and kill lap timing (the Summit Point incident). Blob still loaded/saved for back-compat, never consulted. On-car capture now exists ONLY for UNKNOWN tracks (`sf_unk` above). The dash LAP row shows a CYAN `SF <dist>` countdown (pre-arm, while recording) so a misplaced S/F is visible on lap 1. Historical: struct size changed in 0.1.82 (pre-0.1.82 blobs ignored once).** Set from the STATUS-page **SET START/FINISH** button (captures current GPS as that track's S/F line); `effectiveSf()` prefers it over the baked approximate `sf_lat/sf_lon`. **v0.1.112: a capture below 5 mph stores a POINT (radius method) — GPS heading is garbage at rest, so the old parked capture built a line pointing anywhere and silently killed lap detection for the whole track (the Thompson incident). Rolling capture (≥5 mph) builds the perpendicular line. The STATUS button label now shows live distance to the effective S/F (`custom`/`default`, meters); a maroon **CLR S/F** sub-button (only when an override exists) wipes a bad override trackside; `updateLapTimer()` emits a 20 s `DBG,lap trk=… ovr=… d_sf=…m armed=… laps=…` breadcrumb.** Loaded in `loadSettings()`, written by a dedicated `saveSfOverrides()` (NOT `saveSettings()`, since it's mutated from the status page, not the settings-save path). Blob is restored only if its byte length still matches `sizeof(sfOverride)` — **TRACKS[] is append-only** (inserting a track mid-array shifts existing overrides onto the wrong track). |

`clampInvariants()` enforces `rpm_min < rpm_max`, `alert1_rpm < alertmax_rpm`, `alert1_hz < alertmax_hz` after every mutation.

### Full-screen sensor warning flash (v0.1.110)
When a sensor warning is active on the dash page, the **whole screen flashes
the warn colour at 2 Hz with the warning NAME** (Font4×6, centered):
`OIL` (psi ≤ warn) > `TEMP` (coolant ≥ warn) > `VOLT` (low volts while
running) > `AFR` (out of band) — highest priority wins (`activeSensorWarning()`
next to `computeBgColor()`; it mirrors the per-line warn_active conditions).
The ON phase early-returns from `drawDashPage()` (same pattern as the lap
popup); the OFF phase renders the normal dash, so the **RPM shift flash shows
interleaved** (shift lights themselves are unchanged — colour-only, no label).
Transitions force a full repaint via `pageJustEntered`.

## Dash UI architecture (RaceDash.ino)

### Pages
| Page | Entered via | Purpose |
| --- | --- | --- |
| `PAGE_DASH` | swipe ←-direction from settings | RPM bar (top), HUGE Font7 speed (right side at x=600), HDG/LAT/LON left column, FIX/SATS/GPS right column, START/STOP button left of speed |
| `PAGE_SETTINGS` | swipe →-direction from dash | Scrollable list of settings rows |
| `PAGE_NUM_KB` | tap on cloud port value | Numeric keypad (3 cols × 4 rows + DONE/CANCEL) |
| `PAGE_TEXT_KB` | tap on cloud host / auth user / auth pass | Full lowercase keyboard (10 × 4 letters/digits + .-_/ + BACK/SPACE/DONE/CANCEL) |
| `PAGE_TRACK_PICKER` | tap START button (when not auto-confirming) | Modal list of tracks; closest GPS match auto-bumped to top with distance label |
| `PAGE_SENSOR` | tap the **Sensor data source** settings row | Dedicated picker: Direct / MegaSquirt / **Bluetooth** buttons (like the GPS page). In Bluetooth mode shows the paired OBD-II dongle + live BLE status + a SCAN button. DONE saves, CANCEL reverts. |
| `PAGE_BT_SCAN` | tap SCAN on PAGE_SENSOR | BLE scan for OBD-II dongles; tap a row to pair (saves `bt_addr`/`bt_atype`/`bt_name`, connects). Drag-scrollable. RESCAN / BACK. |
| `PAGE_PID_SCAN` | tap COOLANT PID on PAGE_SENSOR | Mode-01 PID scan (needs connected dongle + ignition); tap a row to map it as COOLANT (`btpid`). Drag-scrollable. RESCAN / BACK. |

### Bluetooth OBD-II (ELM327 BLE) — `sensor_type == 2`
`obd_ble.h` is a self-contained NimBLE client for a **BLE ELM327** dongle in the
car's OBD-II port. Used for **slow** readings only (coolant temp, IAT, module
voltage) — **NOT RPM** (RPM stays on the opto tach / MS3 CAN via the Teensy; BLE
is far too slow for the RPM bar). Hard rule (same lesson as the GPS-stale saga):
**ALL BLE work runs on a dedicated FreeRTOS task pinned to core 0** so blocking
calls (connect, GATT discovery, waiting for ELM `>` prompts, PID polling) can
never stall the 60 fps UI loop on core 1 — the UI only sets request flags and
reads plain volatile values. GATT layout is **auto-discovered**, and since the
v0.1.97 review fix the discovery **skips the standard GAP/GATT/DeviceInfo
services (0x1800/0x1801/0x180A) and prefers a service containing BOTH a
notify-ish char (RX) and a writable char (TX)** — the vendor-UART pattern every
BLE ELM327 uses. (The old "first notify anywhere" pick latched onto 0x1801's
indicate-capable *Service Changed* char, which enumerates before the vendor
services on nearly every dongle → subscribed to a dead char → ELM replies never
arrived → init timed out forever. Also fixed: write mode follows the char's
caps, subscribe uses notifications vs indications per the char, and
`openBtScan()` clears the crash-block so SCAN isn't a silent no-op after a
prior BLE crash.) ⚠️ The dongle MUST be a **BLE** ELM327 (e.g. Vgate iCar Pro
*BLE*) — the ESP32-S3 has no Classic BT/SPP, so a classic-only ELM327 will
never appear in a scan. ELM init: `ATZ ATE0 ATL0 ATS0
ATH0 ATSP0`, then (v0.1.106) a **`0100` protocol-lock probe with a 10 s budget**
— with ATSP0 the FIRST query triggers the ELM's bus-protocol search (3–8 s,
way past the normal poll timeout; without the probe the first coolant reads
all "time out" and BT looks dead) — then (v0.1.109) a **fast/slow poll split**
per `pollOnce()` pass (150 ms task delay): FAST every pass = `0111` throttle +
`010E` timing advance (the knock-retard proxy — race analysis channels; if the
car never answers one, 5 consecutive misses back it off to every 10th pass so
its 1.2 s timeout doesn't starve coolant on the ~4-6 queries/s K-line budget);
SLOW one-per-pass rotation = coolant (`01`+mapped `btpid`), `010F` IAT, `ATRV`
volts. Auto-reconnects on link loss. The dash
overrides its coolant readout with `obd::coolantF_x10()` when `sensor_type==2`,
relays the data to the Teensy as `BTD,...` (session logging + HLTH batt), and
the Sensor page shows coolant/IAT/volts plus the raw ELM reply
(`obd::lastResp()`: NODATA/UNABLETOCONNECT/SEARCHING) when the ECU isn't
answering. **Scan is ACTIVE again since v0.1.106** (names live in the scan
response; safe because the radio time-share keeps WiFi hard-off during scans),
results are RSSI-sorted (strongest first), and after connect the GAP Device
Name (0x1800/0x2A00 — mandatory on every BLE device) is read as a name
fallback and adopted into the saved pairing (`dashHealthTick`).
**Scan page scrolls (v0.1.107)**: vertical drag (constants `BT_ROW_Y0`/`BT_VIEW_H`
+ `bt_scan_scroll` live near `bt_scan_dirty` up top so `handleTouch()` can see
them; clip-rect render, scroll-aware hit test, ~30 Hz drag redraw cap).
**Reconnect hardening (v0.1.107)**: `setConnectTimeout(5)` (NimBLE default 30 s
wedged every retry — the "reconnect sucks" root cause), NimBLE client deleted +
recreated after 3 consecutive connect failures (a wedged client fails forever),
retry cadence 1.5 s (was 2.5–5.5 s), and a POLL **wedge watchdog**: connected
but d_last_ms stalled >20 s = hung ELM/GATT link (ATRV answers whenever the
dongle is alive, so a stall is a dead LINK not a sleeping ECU) → disconnect →
reconnect (ATZ hard-resets the ELM). **STATUS page (page 3) has a BT row** in
the LINK section: OFF (source≠BT) / "waiting (BT on at REC)" / state while
connecting / green `<name> <temp>F` when data flows / yellow `<name> no ECU
data`. **PID mapper (v0.1.108, PAGE_PID_SCAN)**: Sensor page → `COOLANT PID:
xx` button → scans the car's supported Mode-01 PIDs (0100/0120/... bitmask
walk, then one live sample each; supported-but-silent PIDs listed as "no
answer") into a drag-scrollable list — tap a row to map that PID to the
COOLANT function (NVS `btpid`, default 0x05 = standard ECT; applied via
`obd::setCoolantPid()` in `loadSettings()`). Decode assumes the 1-byte A−40 °C
temperature formula (true for 05 ECT / 0F IAT / 46 ambient / 5C oil temp).
The scan runs on the obd task (`g_req_pidscan`/`doPidScan()`), needs a
connected dongle + ignition ON, and refreshes `d_last_ms` at the end so the
wedge watchdog doesn't fire after a long scan. IAT polling stays hardcoded
0x0F. Generic multi-function mapping (RPM/MAP/etc from BT) is still future
work — only COOLANT is mappable today. ⚠️ Car-side
prerequisite: something must ANSWER OBD2 on the port — an MS3Pro needs
"OBD-II over CAN (ISO 15765)" enabled in TunerStudio; a 90–95 NA Miata has no
OBD2 at all.

**⚠️ WiFi + BLE may NEVER run at the same time (v0.1.101).** On this core
(arduino-esp32 2.0.14 / IDF 4.4, ESP32-S3) enabling the BT controller with
WiFi up **aborts in `coex_core_enable`**, and the WiFi `ppTask` can panic
(`pm_set_sleep_type`) when BLE flips coex state under it — both decoded from
live bench backtraces; confirmed by test (WiFi off → BT works). Fix: a radio
**time-share arbiter** in RaceDash.ino (`net_owner`: `NET_WIFI` /
`NET_TO_WIFI` / `NET_BT`; `btAcquireRadio()` / `btReleaseRadio()` /
`netOwnerTick()`; `wifiTick()` holds WiFi hard-off unless `NET_WIFI`).
**Policy: BT owns the radio ONLY while `recording`** (that's when coolant
matters) **or while the user is on PAGE_SENSOR/PAGE_BT_SCAN pairing** — all
paddock time is WiFi (uploads/OTA/NTP just work). `netOwnerTick()` watches the
`recording` edges: REC start → WiFi hard-off (synchronous) → BLE up + dongle
reconnect; REC stop → `obd::requestShutdown()` (disconnect + **full
`NimBLEDevice::deinit`** on the obd task — a mere disconnect leaves the
controller running and coex still asserts) → when `obd::isDown()` (12 s
failsafe) → WiFi reconnects. Tapping UPLOAD if BT somehow holds the radio
auto-hands to WiFi (pending-upload path) and back. Sequencing is everything:
WiFi.mode(WIFI_OFF) is synchronous and MUST precede any BLE init.

Other BLE hardening (v0.1.100/101): crash-forensics **phase breadcrumb** (NVS
`blediag`/`ph`: 1=init 2=scan 3=connect 4=connected; boot reader attributes
only crashy reset reasons — a power cycle while connected is ignored); scan is
**passive + ~20% duty** (interval 100 ms / window 20 ms, 8 s — NimBLE default
is a CONTINUOUS active scan, ~+90 mA; passive may list some dongles as
"(unnamed)" — still pairable by address); TX power 0 dBm for conn/adv/scan;
~64 KB free-internal-heap guard before controller init (parks the task with a
visible reason via `obd::lastErr()` instead of crashing). **NimBLE adds ~180 KB
of flash → the 4M dash binary is now ~94% of the 1.25 MB OTA slot (still fits;
watch this headroom before adding more).**

### Track picker
Pre-seeded with 15 common US road courses (`TRACKS[]` near the top of `RaceDash.ino`). To add tracks, extend that array (TODO: editable from settings).

The `Auto select by GPS` toggle determines whether the picker actually opens or auto-confirms the closest match in range:
- **ON** + GPS fix + a track within its `radius_km` → skip picker, immediate `TRACK,<name>` + `REC,1`
- **OFF** or no match → open picker; user taps a row, taps CONFIRM
- The synthetic `(no track / unknown)` row is always last in the list and emits `TRACK,UNKNOWN`
- Closest track is **highlighted green at the top** of the picker with a `closest · X.X km` distance label

### Track selection: sub-tracks (configs) with their own S/F; SELECTED track wins (v0.1.115)
**Summit Point is ONE picker entry with three sub-tracks** (configs Main / Jefferson /
Shenandoah). `TrackConfig` gained `sf_from`: a config may BORROW its S/F — baked coords AND
the SET START/FINISH override slot — from a **hidden aux TRACKS[] entry** (aux=1 = never
auto-picked, never listed; `Summit Point Jefferson`/`Summit Point Shenandoah` remain in the
array purely as S/F-storage tombstones so the append-only `sf_ovr` blob stays index-stable —
DO NOT remove/reorder). `sfStorageIdx(tIdx)` resolves (track, active config) → storage index
by NAME; `effectiveSf`/`effectiveSfLine`/SET/DELETE S/F/`sendSfToTeensy` all route through it.
Active config = `active_cfg_idx` (NVS `lcfg`), set by the config picker, reset to 0 on plain
track select. **AUTO flow never prompts**: a config track defaults to config 0 (Summit Point
Main); variants are chosen deliberately from the track picker (manual START still shows the
config picker). `lapTrackIdx()` = SELECTED track (within radius) else GPS-closest — drives
lap timing AND S/F set/delete (the old three-entry auto-pick flapped mid-lap and reset the
lap timer = the "PRED/LAP/DELTA dead at Summit Point" bug).

### S/F capture: dash-page button while recording + DELETE CUSTOM S/F (v0.1.115)
`captureSfHere()` (shared): parked <5 mph → POINT, rolling → perpendicular LINE, stored into
`sfStorageIdx()`'s slot + re-sent to the Teensy. **While RECORDING the dash TRACK button
becomes SET S/F** (two-tap: arm → "TAP AGAIN" → capture; green "S/F SET!" flash; the button
reverts to TRACK+picker when idle) — so the driver can set the line any time, e.g. rolling
across S/F on an out-lap. STATUS page: the old "CLR S/F" is now a wide maroon **DELETE CUSTOM
S/F** button (x180-404, only visible when an override exists).

### ⚠️ THE lap-timer killer: one bad GPS line wiped the whole timer (fixed v0.1.133)
Symptom: "40 laps before 1 registered" — while the SERVER and the SD NDJSON showed every lap
perfectly. That split is the whole clue: the Teensy logs locally, so only the DASH sees the
UART hop. Mechanism: a dropped/overflowed UART byte mangles one `GPS,` line → `toFloat()`
returns 0.0 → position `(0,0)` → outside every track radius → `lapTrackIdx()` = -1 →
`updateLapTimer()` did `lapTimer = LapTimer{}` **on the first bad sample**, wiping
`timing_started`/`lap_number`/ghost. A lap therefore only completed if an ENTIRE ~85 s lap
passed with ZERO corrupt samples. **Measured with the real firmware code on a real Thompson
trace: 1 bad line per ~100 s took 4 detected laps → 0.**
Fix (three layers, no geometry change):
1. `parseGpsLine()` parses into LOCALS then **sanity-gates** before committing to `g.*`:
   reject null-island (0,0), |lat|>90/|lon|>180, and >500 m teleports between consecutive
   fixes — the teleport check only applies while the last good fix is <2 s old, so it is
   self-healing after a real dropout and can never latch onto a dead position.
2. `updateLapTimer()` **debounces** the off-track clear: ~50 consecutive (~2 s) out-of-range
   fixes before wiping state.
3. Rejected samples never reach the crossing test, so `prev_lat/prev_lon` can't be poisoned.
Result on the same trace: **4/4 laps at every corruption rate up to 15 bad lines/min**, timer
resets 5 → 0, clean-trace lap times unchanged. Harness: `/tmp/extract_core.py` + a replay of a
real session pulled via `GET /admin/sessions/get` — **replay real data before theorising.**

### S/F crossing = PLANE + gate + interpolated instant (v0.1.130) — the "it doesn't see the line" fix
The old test was "does the path segment prev→cur INTERSECT the stored S/F segment?" A
web-picked line is only ~10 m long (you click the two edges of the stripe), so passing a few
metres wide of it MISSED the lap. Proven in simulation with the REAL firmware code on REAL
Summit geometry: **−4/−6/−8 m lateral offset = 0 of 5 laps detected**, +4 m @ 60 mph = flaky.
Now (`SfGate` / `buildSfGate` / `sfGateProject` / `sfGateCross`, mirrored on the Teensy in
`updateTeensyLap` + `buildSfGateT`):
- the S/F is an **infinite PLANE** through the line's midpoint; every fix computes the **signed
  distance** to it; a **sign change = crossing** (so lateral position no longer matters),
- **lateral gate** `SF_GATE_HALF_M = 25 m` (50 m wide, or the drawn line's own width if wider)
  keeps a parallel road / distant part of the circuit from triggering,
- **direction gate**: the travel direction through the plane is learned at the first crossing;
  later passes must match (a wide gate can otherwise catch the pit lane / wrong-way),
- the crossing **INSTANT is interpolated** between the two straddling fixes
  (`t_cross = t_prev + frac·Δt`), so lap time no longer quantizes to the 40 ms sample grid:
  measured error vs ground truth **2.5–9 ms mean** (was ±40 ms), and `lap_start_ms` is set to
  `t_cross`, not the sample time,
- **MIN_LAP_MS no longer applies to the FIRST crossing** — arming is not a lap, so starting a
  recording shortly before the line no longer silently costs a whole lap.
⚠️ `struct SfGate` is forward-declared at the top of RaceDash.ino (Arduino auto-prototype rule).
Harness: `/tmp/extract_core.py` pulls the REAL functions out of RaceDash.ino into a host test —
re-run it after touching the lap timer.

### Lap timer / predictive / delta / LAP COUNTER (S/F LINE-crossing, v0.1.82)
Lap timing runs in `RaceDash.ino` (`updateLapTimer()`) — **only WHILE RECORDING since v0.1.105**:
START resets `lapTimer` for a fresh session (rising-edge reset also avoids a stale `prev_gps_ms`
distance jump on restart); STOP freezes it and PRED/DELTA draw as grey `--` (the LAP row keeps
the last completed time as a static fact). Middle dash column shows `PRED` / `LAP`
(time) / `DELTA`; a **LAP COUNTER** (`"LAP N"`, yellow Font4) is drawn near the
speed, just above the FIX/SATS/GPS column (current lap; `LAP --` until the first
crossing).
- **Start/finish = LINE crossing (v0.1.82).** The S/F is a **2-point line**
  (endpoints A,B). `updateLapTimer()` keeps the previous GPS point and logs a lap
  the instant the path segment prev→cur **intersects** the S/F segment
  (`segmentsCross()`, planar w/ lon×cos lat) — precise + repeatable vs the old
  radius method. `MIN_LAP_MS` (15 s) floor. **Fallback:** a track/override with
  only a point (endpoint B = 0,0) still uses the legacy `LAP_RADIUS_KM` (75 m)
  radius method (`effectiveSfLine()` returns `hasLine=false`).
- **S/F line everywhere (all 4):** `TrackInfo` gained `sf_lat2/sf_lon2` (appended,
  aggregate zero-fill keeps old rows valid). The picker (`tools/track_sf_picker.html`)
  draws a 2-click line. **SET START/FINISH** now captures a point **+ heading** and
  synthesizes a perpendicular ±30 m line. The dash pushes the active line to the
  **Teensy** via `CFG,sf,aLat,aLon,bLat,bLon`; the Teensy runs the same crossing
  test and stamps `"lap":N` into each NDJSON sample (`updateTeensyLap`, reset per
  `openSession()`). The **server** `_detect_laps()` honors that `lap` field when
  present, else auto-detects via a perpendicular S/F line at the anchor
  (`_seg_cross`/`_sf_line_from`) instead of a radius.
- **Predictive = "ghost lap" method.** The session-best lap is snapshotted as a
  time-vs-distance table (`lapRefBt[]`, ~8 m buckets, `LAP_BUCKET_MI`). The live
  `liveDeltaMs()` compares the current lap's elapsed time to the ghost at the
  **same distance into the lap** (interpolated); `PRED = best_lap + delta`. Needs
  one complete lap to seed the ghost (lap 1 shows `--`). Bucket tables are kept
  **outside** the `LapTimer` struct so `lapTimer = LapTimer{}` stays a cheap
  scalar reset (a ~10 KB temporary on the loopTask stack would risk overflow).
- **Colours (PRED + DELTA): green/white/red** = faster / same (within
  `DELTA_SAME_MS` = 50 ms) / slower; grey `--` until a ghost lap exists.
- **Middle-column values are Font4** (v0.1.104, rows at 28 px pitch, sprites 150×28); labels
  stay Font2.
- **Finish-line lap-time popup (v0.1.104).** On lap completion the completed time is drawn HUGE
  (Font7×2, green if session best else yellow, with a "LAP n" caption) over everything BELOW the
  RPM bar + RPM number — the live RPM bar stays visible, and the **RPM alert flash takes
  precedence** (popup hides while `bg != TFT_BLACK` flashes and RE-ARMS when the flash ends —
  only natural expiry clears `lap_overlay_until_ms`). Duration = NVS `lapov` (0–9 s, default 3,
  0 = off). While the popup is up, `drawDashPage()` returns early (dash under it is frozen but
  fully covered); expiry sets `pageJustEntered = true` for a clean full repaint. Armed in
  `updateLapTimer()` at the lap-record point (`lap_overlay_*` globals next to `lapTimer`).
- The **baked `sf_lat/sf_lon` are approximate** — use the STATUS-page SET
  START/FINISH capture (NVS `sf_ovr`) to pin the real line per track on-site.

### Touch handling
Single touch handler at the top of `loop()` distinguishes:
- **Tap** (|dx|, |dy| < 30 px, < 600 ms): button press / row select / key press
- **Horizontal swipe** (|dx| > 120 px, |dy| < 100 px, < 800 ms): page navigation between dash and settings
- **Vertical drag** on PAGE_SETTINGS or PAGE_TRACK_PICKER: scroll content, header/footer stay sticky

Keyboard pages use a tap-only model (CANCEL is the way out, no swipe-back).

> Touch hardware is **TAMC_GT911 on Wire**, not LovyanGFX — see the **"GT911 touch"** section
> below. The 80 ms release debounce in `handleTouch()` is what makes swipes/drag work at all;
> don't remove it.

### Anti-flicker rendering rules — DO NOT remove
1. **Conditional redraws on dash page.** Each dynamic element caches its last-drawn value in `LastDrawn ld`. Drawing skips when the new value matches.
2. **`setTextColor(fg, bg)`** so each character cell paints its own background. No `fillRect → blanked frame → drawString` sequences ever.
3. **`setTextPadding()` for centred dynamic text** like the speed: clears any leftover from a longer previous draw atomically while keeping the visible digits centred at the draw position.
4. **Settings + picker pages: per-row repaint, NOT full-band wipe.** Full-band wipes (~624 KB to PSRAM) take ~12 ms and the LCD scan-out catches them mid-render → visible flashing. Instead we wipe only narrow strip bands (40 px each at the body's top and bottom edges, ~64 KB each = 1.3 ms) on scroll-position change. Tap mutations skip the wipe entirely.
5. **Scroll redraw rate cap = 30 Hz max** during active drag (`now - lastDraw >= 33 ms`). At ~200 Hz the LCD never gets a clean scan window between renders and tearing accumulates. 30 Hz gives the LCD two full scan periods per render.

The settings page uses a `settingsDirty` flag, picker uses `tp.dirty`. Drawing is skipped between mutations.

**Scroll flicker is reduced but not zero.** If a perfect fix is needed, the next step is a sprite back-buffer for each scrollable body — render to PSRAM sprite first, `pushSprite` once. That's deferred for now (current behaviour was deemed acceptable).

### Speed format
Speed renders at Font7 size 4 (~192 px tall) centred at x=600 (right side of screen, away from the START/STOP button on the left). **The decimal drops at >=100 mph** so 3-digit values stay narrow enough to fit the 400-px bg pad without clipping.

### Setting up new ENUM controls
Add to `enumValue()`, the appropriate `_NAMES[]` array, and the cycle case in `handleSettingsTap()` ENUM branch. `PROTOCOL_NAMES = {"HTTP", "HTTPS", "FTP"}`. (The `STREAM_NAMES` / `ST_CL_STREAM` Live-vs-AfterRace enum was removed in v0.1.66.)

## GT911 touch — the hard-won truth (do not regress this)

Touch broke completely on a new CrowPanel unit after flashing, while the factory firmware
touched fine and our firmware touched fine on a *different* unit. After exhaustive
bus-level instrumentation, the real causes and the working solution:

### What does NOT work
- **LovyanGFX's built-in `Touch_GT911` (on `I2C_NUM_1`, SDA/SCL 19/20).** `tft.begin()` runs
  its touch init which routes `I2C_NUM_1` onto GPIO 19/20 via the GPIO matrix — **overwriting
  `Wire`'s `I2C_NUM_0` output routing on the same pins**. After that, *all* I²C reads return
  nothing. Empirically: GT911 product-ID read returned `"911"` **before** `tft.begin()`, then
  `got=0` / `eot=5` (timeout) **after**. This is why the original `cfg.i2c_addr=0x14`,
  `I2C_NUM_1`, `bus_shared=true` config silently fails on some units.
- Lowering the RGB pixel clock (15→12 MHz) or the I²C clock (400→100 kHz) does **not** help —
  it's a pin-routing conflict, not bandwidth.

### What DOES work (current implementation)
Replicate the **vendor V3.0 demo**: LovyanGFX for the RGB **display only** (no touch instance
in the `LGFX` class), and **TAMC_GT911 on `Wire` (I2C_NUM_0)** for touch. Only one I²C
peripheral ever owns pins 19/20, so there's no routing theft and reads work even with the
RGB DMA running.

Key points in `RaceDash.ino`:
- `#include <TAMC_GT911.h>`; global `TAMC_GT911 ts(19, 20, (uint8_t)-1, (uint8_t)-1, 800, 480);`
  (INT and RST = `-1`; the board handles reset, GPIO 38 held low at boot per vendor).
- The `LGFX` class has **no `Touch_GT911 _touch_instance`** and no touch config block.
- `setup()`: `Wire.begin(19,20)` → PCA9557 → `tft.begin()` (RGB only) → **then** probe I²C for
  `0x5D` then `0x14` and `ts.begin(<detected addr>)`. **Address varies by unit batch** —
  auto-detect, never hardcode.
- `ts.setRotation(ROTATION_INVERTED)` gives raw coords (x=x, y=y). `ROTATION_NORMAL` mirrors
  to the opposite corner (looks like "touch doesn't work"). If a future unit reads mirrored,
  switch the rotation constant.
- **TAMC's `read()` has an array-overflow bug** if I²C returns garbage (`touches = buf & 0x0F`
  can be 15, writing past `points[5]`). It only bites when reads fail — which they won't with
  the correct single-bus setup.

### Swipe / drag debounce (also critical)
The GT911 clears its data-ready flag on each read, so `ts.isTouched` **flickers false between
the controller's sample periods (~5–10 ms)** while a finger is held. Without handling this,
every flicker looks like a finger-up: the swipe/drag state machine resets and movement never
accumulates past the 120 px threshold — **taps work, swipes/scroll don't.** Fix in
`handleTouch()`: an **80 ms release debounce** — hold the touch "down" (retaining last X/Y)
for up to `TOUCH_RELEASE_MS` after the last real sample. Do not remove this.

### Backup
The pre-touch-work dash is preserved at `crowpanel-arduino/RaceDash_v0139_orig/` so a screen
can be swapped/reverted easily.

## MS3Pro CAN (MegaSquirt RPM / coolant / AFR)

The Teensy reads the MS3Pro ECU over **CAN1 (TX 22, RX 23)** via an **SN65HVD230 ("VP230")**
3.3 V transceiver — **NOT** an MCP2551 (that's 5 V and would damage the Teensy). The blue
breakout has an **onboard 120 Ω terminator** (silkscreen `R2 = 121`), so no external resistor
is needed; it must sit at the **end** of the bus (Teensy ↔ MS3Pro = correct).

Wiring: `3V3→3.3V, GND→GND, CTX(TXD)→pin 22, CRX(RXD)→pin 23, CANH→MS3Pro CAN-H,
CANL→MS3Pro CAN-L`.

Software (`src/main.cpp`): `FlexCAN_T4<CAN1, RX_SIZE_256, TX_SIZE_16> Can1;` at
`CAN_BAUD = 500000`, `CAN_BASE_ID = 0x5E8` (1512, MS3 "Simplified Dash" base). `pumpCAN()` parses frames and `CAN_STALE_MS = 2000`
resets fields to `-1` if the bus goes silent. **FreqMeasureMulti (opto tach) is kept alongside CAN**
— both RPM sources are always running; `emitToDash()` picks one based on `sensor_type`.

### sensor_type switch (Direct / MegaSquirt / Bluetooth)
`Settings → Sensor data source` opens **PAGE_SENSOR** (NVS key `srctyp`, synced to Teensy via
`CFG,srctyp,<0|1|2>` in `sendCfgToTeensy()`):
- **0 = Direct**: RPM from opto tach (pin 9), coolant from A3 NTC. Oil PSI always from A2.
- **1 = MegaSquirt**: RPM + coolant + AFR + MAP + TPS + IAT + battery from CAN. AFR is only
  shown in MS3 mode. Oil PSI still from A2 (MS3Pro typically has no oil-pressure input).
- **2 = Bluetooth**: coolant (+IAT/voltage) from a **BLE OBD-II (ELM327) dongle** on the
  CrowPanel (see the PAGE_BT_SCAN / `obd_ble.h` section above). **RPM still comes from the
  Teensy opto tach** — the Teensy treats `srctyp==2` like Direct (`use_can = (srctyp==1) ||
  can_live`), and the dash just overrides its coolant readout with the OBD value.

### ✅ Broadcast byte offsets — VERIFIED (2026-06-03)
Layout confirmed against the **official MegaSquirt CAN Broadcast spec**
(`msextra.com/doc/pdf/Megasquirt_CAN_Broadcast.pdf`, "Simplified Dash Broadcasting") AND a
real sniffer capture: frame `0x5E8 = 03 FD 00 00 05 62 FF FF` decoded cleanly to MAP 102.1 kPa
/ RPM 0 / CLT 137.8 °F / TPS ~0 (a key-on, engine-off snapshot — that's why the payload was
static across the whole capture). Base ID is **1512 (0x5E8)**, NOT 1520/0x5F0 — sequential
addressing, big-endian, 500 kbps. `pumpCAN()` now parses:

| ID | bytes | field | scale |
|---|---|---|---|
| `0x5E8` | 0-1 | map | ÷10 kPa (int16) |
| `0x5E8` | 2-3 | **rpm** | ×1 (uint16) |
| `0x5E8` | 4-5 | **clt** | ÷10 °F (int16) |
| `0x5E8` | 6-7 | tps | ÷10 % (int16) |
| `0x5E9` | 4-5 | mat (IAT) | ÷10 °F (int16) |
| `0x5EA` | 1 | **AFR1** | ÷10 (uint8! 0-255 = 0.0-25.5) |
| `0x5EB` | 0-1 | batt | ÷10 V (int16) |

**AFR + AFR-target are single bytes**, not 16-bit (byte 0 = afrtgt1, byte 1 = AFR1). The rest
are 16-bit big-endian. TunerStudio setup: `CAN-Bus/Testmodes → Dash Broadcasting → Enable=On`,
`Automatic` mode (locks base to 1512, broadcasts 50 Hz). Temps assumed °F × 10 (depends on
TunerStudio units — re-verify if it's ever switched to °C).

> Note: only `0x5E8` was seen in the engine-off capture. The 1513-1516 (`0x5E9`-`0x5EC`)
> frames (IAT/AFR/batt) still want a **running-engine capture** to fully confirm, but they
> follow the same authoritative spec table above.

### Upload diagnostics (v0.1.119) — how to debug a wedged upload REMOTELY
- **Modal diag line** (under the progress bar): `S<state> N<net_state> rt=<sentKB> rh=<queuedKB> <err>`
  — S = UploadFlowState, N = net task (0 idle/1 running/2 done/3 failed). Photograph this.
- **`ufDiagReport()`**: on every retry/failure AND on success the dash fires a 0-byte POST
  /nettest with `X-Note: ufdiag <why> st=.. net=.. rh=.. rt=.. lr=.. er=.. f=..` — lands in the
  server upload event log (`ev=nettest`, note field) readable via `GET /admin/upload/log`.
- **Server `recv_error` now logs `bytes_received`** (body bytes before the client died):
  0 = body never started (client-side pre-write bug), large = mid-stream stall.
- Modal progress reads the VOLATILE `uf.ring_tail` directly during streaming (bytes on the
  wire), not the non-volatile relay.

## WiFi speed test (Tools page button 5, v0.1.116)

The upload-debugging discriminator: POSTs **2 MB of junk from PSRAM straight to the server's
`POST /nettest`** — no Teensy, no UART, no session files — so it measures the dash's radio +
TCP/TLS path and NOTHING else. Result shows on the button ("2MB in 3.2s = 640 KB/s  RSSI -67")
and **every run is recorded server-side** in the upload event log (`ev=nettest` with bytes/
seconds/kbps/RSSI/fw) — review later via `GET /admin/upload/log` (firmware-key gated).
Interpretation: hundreds of KB/s here but session uploads fail → transfer code's fault;
tens of KB/s (or STALLED) while a phone on the same AP is fast → the dash's RF path is
starved (RGB-panel EMI / weak AP — check the RSSI number, move the hotspot). Runs on a
core-0 task (`netTestTask`); blocked while an upload is active. Tools page is now FIVE
64 px buttons; the CAN health readout moved below button 5.

## CAN sniffer (Tools page → "Start CAN capture")

De-risks the byte-offset guessing: captures **every raw CAN frame** to SD for offline analysis.
- Dash: 4th button on `PAGE_TOOLS`. Tap toggles; sends `CANSNIFF,1` / `CANSNIFF,0`; shows live
  frame count. Greyed out without a mounted SD card.
- Teensy: `openCanSniff()` writes `/cansniff/cansniff_<unixtime>.csv`; `pumpCAN()` logs every
  frame (any ID) when active; 1 Hz flush + `CANSNIFF` status heartbeat back to the dash.
- CSV columns: `t_ms,id,ext,dlc,d0..d7` (data bytes in hex), e.g. `12,0x5F0,0,8,00,1A,0B,4E,...`.

Workflow: wire transceiver → enable TunerStudio broadcast → run engine → Tools → Start CAN
capture → vary RPM ~30 s → Stop → pull SD → decode which byte pairs track RPM/CLT/AFR → lock in
the real offsets in `pumpCAN()`.

## IMU (MPU-6050) — wiring + boot auto-calibration

GY-521 / MPU-6050 on the Teensy's **default `Wire` bus (I2C0) = pin 18 SDA, pin 19 SCL**,
400 kHz, address **0x68** (`AD0` → GND). Read as a raw 14-byte burst from `0x3B` at ~250 Hz,
averaged into each emit window (`readIMU()` / `flushImu()` in [src/main.cpp](src/main.cpp)),
emitted as the `IMU,ax,ay,az,gx,gy,gz` wire line. Accel ±2 g (16384 LSB/g), gyro ±250 °/s
(131 LSB/°s), DLPF 44 Hz (`CONFIG=0x03`).

**Wiring (the part that bit us — SDA/SCL get swapped, or wired to the wrong I2C pair):**

| GY-521 | → Teensy 4.1 | note |
| --- | --- | --- |
| VCC | **3.3V** | NOT 5V — Teensy GPIO isn't 5V-tolerant; the module's pull-ups reference VCC |
| GND | GND | common ground is mandatory |
| **SDA** | **pin 18** | I2C0 SDA (not 17=Wire1, not 25=Wire2) |
| **SCL** | **pin 19** | I2C0 SCL (not 16=Wire1, not 24=Wire2) |
| AD0 | GND | leaving it high/floating → addr 0x69 → "MPU-6050 NOT found" |

The Teensy 4.1 has **three** SDA/SCL pairs; only `Wire`=18/19 works here. Pins 16/17 (`Wire1`)
are already the oil/coolant ADC (A2/A3). Boot banner says exactly which way it went:
`MPU-6050 ready ...` (detected) vs `MPU-6050 NOT found on Wire (SDA=18, SCL=19)`.
**Detection is one-shot at boot** — if the MPU doesn't ACK that boot, `imu_present` stays false
and every `IMU` line is `0.00,...,0.0` for the whole session (the all-zeros signature = not
detected, NOT a calibration artifact). We saw intermittent non-detection on a jostled DuPont
jumper — if zeros show up sporadically, re-seat/solder the IMU header before suspecting code.

### Boot auto-calibration (`calibrateIMU()`) — gyro bias + accel scale, every boot
Runs once right after `setupIMU()` in `setup()`. **Chosen design (Option B):**
- **Gyro: full bias removal, every boot.** True at-rest value is 0,0,0, so bias = mean of the
  window, SUBTRACTED on every read. Re-done each boot because MPU-6050 gyro zero-rate drifts
  with temperature (a stored one-time cal would be wrong on the next cold start). We measured
  ~26 °/s bias on this unit → cal drives it to ~0.
- **Accel: scale-normalize to 1 g, every boot.** `a_scale = 1 / |a_measured|`, MULTIPLIED on
  every read so gravity reads exactly 1.00 g. **Orientation-independent** — it does NOT assume
  the car is level or that any particular axis is "up", so a boot on a slope is fine. It fixes
  the magnitude (we measured 1.41 g → 1.00 g) but does NOT remove per-axis offset (that needs a
  6-position cal — deliberately deferred; can be added later as a stored EEPROM cal + dash cmd).
- **Stillness gate (the critical safeguard).** ~0.5 s window; if any axis peak-to-peak exceeds
  `GYRO_STILL_DPS` (2 °/s) or `ACCEL_STILL_G` (0.10 g) the device is moving → the fresh cal is
  **REJECTED** and the last-good offsets stand. This stops a cal taken while idling/driving/
  bumped from corrupting the whole session. Banner prints `IMU cal ABORTED — device moving ...`
  when it trips.
- **EEPROM persistence.** Last-good offsets live in the Teensy's flash-emulated **EEPROM at
  address 0** (`ImuCalStore` = magic `0xCA15` + 3 gyro offsets + accel scale, 18 bytes).
  `loadImuCal()` runs first as the fallback; `saveImuCal()` (`EEPROM.put` == update) writes back
  only on an accepted cal. **EEPROM addr 0 is now reserved for IMU cal — nothing else on the
  Teensy uses EEPROM; if you add EEPROM storage, start past `sizeof(ImuCalStore)`.**
- **Bonus fix:** `readImuRaw()` buffers all 14 bytes before assembling the int16s, removing the
  old `(read()<<8)|read()` reliance on unspecified C++ operand evaluation order.

Verified at rest after flashing: `gyro ~0 °/s`, `|a| = 1.00 g`. If you ever need to wipe the
stored cal, reflash won't clear EEPROM — power-cycle with the IMU still and it self-recomputes,
or zero `ImuCalStore.magic`.

Still on the table (not done): lowering the on-chip DLPF (44 Hz → 10/21 Hz) for better
anti-aliasing before the 25 Hz decimation, and using the discarded MPU temperature bytes for
gyro temp-drift compensation. Heavy filtering / IMU↔GPS fusion is intended for the Docker
post-processing side, not the Teensy (keep the logged stream calibrated-but-raw).

## Libraries added this cycle
- **`TAMC_GT911`** (Arduino registry) — GT911 touch on Wire. Installed via
  `arduino-cli lib install "TAMC_GT911"`.
- **`FlexCAN_T4`** — ships with the Teensy core (no `lib_deps` entry needed). `FreqMeasureMulti`
  is also a Teensy-core lib.
- **`NimBLE-Arduino` v1.4.3** (Arduino registry) — lightweight BLE stack for the CrowPanel's
  BLE OBD-II client (`obd_ble.h`, `sensor_type==2`). Install the EXACT version:
  `arduino-cli lib install "NimBLE-Arduino@1.4.3"`. **DO NOT use 2.x** — NimBLE 2.x targets the
  arduino-esp32 **3.x** core (IDF 5); on our **2.0.14** core (IDF 4.4) it compiles but
  **crash-reboots the board on BLE init** (v0.1.90 shipped with 2.5.0 and did exactly this).
  1.4.3 is the battle-tested match for 2.0.14. (API differs from 2.x: scan `start()` returns
  results in SECONDS, `getDevice()` by value, `getServices/getCharacteristics` return POINTERS,
  `onDisconnect` has no reason arg, `setPower(esp_power_level_t)`.) Also: NimBLE init is done
  **inside the OBD task**, not the caller — running it on the shallow UI/tap-handler stack was a
  second crash vector. Chosen over stock Bluedroid because it's ~180 KB (vs ~400 KB) — the 4M
  dash binary is ~94 % of the 1.25 MB OTA slot. **The ESP32-S3 is BLE-only (no classic BT/SPP),
  so the OBD dongle MUST be a BLE ELM327** (e.g. Vgate iCar Pro BLE), not a classic one.

## Toolchain note (Linux build host)

> **Full from-scratch build setup + exact commands live in [BUILD.md](BUILD.md)** — install
> arduino-cli + `esp32:esp32@2.0.14` + libs (LovyanGFX/TAMC_GT911/bundled PCA9557) + the
> pyserial-on-PYTHONPATH gotcha for esptool, and PlatformIO in a venv. Keep tools OUT of the
> OneDrive-synced `chrisrawlings/` tree (this build used `/home/chris/racecar-tools/`).

This session built/flashed on Linux, not the Windows paths below:
- PlatformIO: `~/.local/bin/pio` (or `~/.local/share/pipx/venvs/platformio/bin/platformio`)
- arduino-cli: `~/.local/bin/arduino-cli`, `directories.user = ~/Arduino`
- Ports: **`/dev/ttyACM0` = Teensy**, **`/dev/ttyUSB0` = CrowPanel (CH340)**
- Teensy enumerates as HID/HalfKay for flashing (auto-detected by teensy-cli; press the
  button if "Error opening USB device").

### git push — `$HOME` is UNSET in this shell (the gotcha that bites every push)
The GitHub token is already stored at `/root/.git-credentials` with `helper = store` in
`/root/.gitconfig`. **But this shell starts with `$HOME` empty**, so git can't find either
file and you get `fatal: could not read Username for 'https://github.com'`. The fix is just
to set HOME for the git command:
```bash
HOME=/root git push origin main        # works — store helper supplies the token
HOME=/root git pull / fetch / etc.     # same for any auth'd git op
```
Remote is `https://github.com/teknoprep/racecar-35.git` (HTTPS, token auth). Don't bother
re-pasting the token or rewriting the remote URL — it's all configured; you only ever need
the `HOME=/root` prefix. (Or `export HOME=/root` once at the top of a session.)

## Arduino IDE auto-prototype gotcha when editing RaceDash.ino

Arduino IDE injects auto-generated function prototypes at the top of the translation unit, immediately after `#include` lines. Any type used in a function signature must be **declared above the first function**, or compilation fails with "X was not declared in this scope" on the auto-prototypes.

In RaceDash.ino: `enum SettingId`, `struct NumBounds`, and `struct KbKey` are forward-declared right after the includes for this reason. **`TrackPickerState` (and the `tp` global) is also defined early** — adjacent to the `TRACKS` table — so `handleTouch()` can reference `tp.scrollY` and `tp.dirty` for drag-scroll without ordering tricks.

`I2C_NUM_1` requires `#include <driver/i2c.h>` even though Wire.h transitively pulls it in some paths but not others.

## Planned: cloud upload pipeline (W5500 incoming)

When the W5500 module arrives, the **Teensy** side gets:

- **`NativeEthernet` lib** (paulstoffregen) for the W5500 — stable on Teensy 4.1
- **`OPEnSLab-OSU/SSLClient`** for HTTPS — wraps mbedtls, works with NativeEthernet
- **`bblanchon/ArduinoJson`** for the per-sample JSON serializer
- DHCP on boot, then NTP from `0.pool.ntp.org` once link-up
- Per-sample serializer producing **descriptive-key NDJSON** at 25 Hz:
  ```json
  {"t":1714942567.234,"fix":3,"sats":12,"lat":40.123456,"lon":-74.12345,
   "alt_m":123.4,"speed_mph":67.5,"heading_deg":123.4,"rpm":5800}
  ```
  (~220 bytes/sample × 25 Hz = ~5.5 KB/s = 20 MB/hr — fine over LTE/wired)
- **SD logging filename convention**: `/sessions/session_<unixtime>_<trackname>.ndjson`. Track name comes from the dash's `TRACK,<name>` line right before `REC,1`; falls back to `UNKNOWN` when not set.

### Cloud strategy — After Race only (live streaming removed v0.1.66)

Live "stream to cloud" (per-sample POST to `/stream` mid-session) was **deleted**
in v0.1.66: it wasn't ready, and its blocking POSTs stalled the Teensy loop long
enough to desync the GPS UART (see the GPS RX-buffer note above). The dash no
longer exposes a stream-mode setting and the Teensy has no live-POST code.

| | After Race (HTTP today; HTTPS/FTP NYI) |
| --- | --- |
| Endpoint | `http://<host>:<port>/upload` whole-file POST |
| Headers | `Content-Type: application/x-ndjson`, `X-API-Key`, `X-User-Email`, `X-Session-Id`, `X-Track-Name` |
| Flow | session file written into `/queue/` during recording (kill-switch insurance); uploaded after the session ends, **dash-driven** (`Q,LIST`/`Q,GET`/`Q,DEL`) or via the queue drain |
| Failure | file stays in `/queue/`, retried on the next dash-requested drain |

### Upload transfer path (Teensy → dash → cloud) — how the bytes actually move

The SD card + session files live on the **Teensy**; only the **dash** has WiFi. So an upload is a
two-hop relay: the Teensy streams the file to the dash over UART, and the dash POSTs it to the
cloud. Both hops and their hard-won fixes:

**Hop 1 — Teensy→dash UART, sequence-numbered stop-and-wait ARQ (v0.1.78, `handleQGet`):**
`Q,DATA,<name>,<size>` then `Q,L,<seq>,<line>` per line; the dash replies `Q,A,<seq>`. The
Teensy RETRANSMITS a line if its ack doesn't arrive (2 s × 15 tries); the dash APPLIES only
`seq == next_seq` and dedups resends. This is the fix that made uploads reliable — before it, a
SINGLE dropped/corrupted byte on the long 921600-baud cabin↔trunk wire aborted the whole file
(`Q,ERR,ack_timeout`) at a random line. Ends with `Q,EOF,<seq>`.

**Hop 2 — dash→cloud HTTPS.** TWO designs have existed; keep this note so it's revertible:
- **Whole-file cache (v0.1.76–0.1.78):** `Q,DATA` → `ps_malloc(size)`; each `Q,L` is memcpy'd into
  PSRAM (`UF_STREAMING`, instant ack, no network in the loop); on `Q,EOF` the complete buffer is
  POSTed with `Content-Length` in one shot (`UF_POSTING` → chunked `tcp->write` from the buffer).
  Modal is two-phase: "Copying to cache" (cyan) then "Uploading to cloud" (green). **Limit: the
  whole file must fit in PSRAM** — capped at `UF_MAX_FILE` (6 MB); the Advance has 8 MB PSRAM minus
  the ~768 KB framebuffer. A >6 MB session (>~18 min at 25 Hz) is rejected. **To revert to this,
  restore the `UF_STREAMING`=stage-to-PSRAM + `UF_POSTING`=post-buffer handlers.**
- **Bounded streaming (v0.1.79+, current):** `Q,DATA` opens the TLS socket immediately with
  `Transfer-Encoding: chunked` (no Content-Length needed — tolerant of CR-stripping size deltas),
  and each `Q,L` appends into a fixed ~128 KB PSRAM buffer that is flushed to the socket as one
  HTTP chunk whenever it fills; `Q,EOF` flushes the tail + writes the `0\r\n\r\n` terminator.
  **Flat memory → any session size.** Backpressure is implicit: while the dash blocks on a chunk
  flush it stops acking, and the Teensy's ARQ simply waits/retransmits (safe). Requires the ARQ
  (hop 1) to be reliable, which is why streaming only works from v0.1.78 onward.

Whichever hop-2 design: opening the socket, chunk/short-write retries, and the HTTP response
parse live in `ufOpenStream`/`ufFlushChunk`/`UF_STREAM_FINISH`. Do NOT reintroduce per-line
`CFG,...` config resends to the Teensy DURING a transfer (the loop gates on `uf.state==UF_IDLE`
&& `sl.state==SL_IDLE`) — a 12-line CFG burst mid-ARQ used to desync it.

### Outbound queue (offline / poor-connectivity tolerance)
`/queue/session_*.ndjson` on SD card. On every boot or link-up event:
1. Walk the queue oldest-first
2. Attempt upload of each file
3. Delete on success; leave for next time on failure
4. Cloud-recorded sessions are written straight into `/queue/` (so a mid-session power cut still leaves a valid, uploadable file)

This means the car can have zero connectivity at the track and still get all data — power on at home with internet and the dash flushes the backlog.

### NTP sync
On Teensy boot: DHCP → NTP query to `0.pool.ntp.org` → set the Teensy's RTC. GPS time is the secondary source (UTC from PVT). When neither NTP nor GPS-fix is available, fall back to `millis()`-relative timestamps until one becomes available.

## Layout

```
src/main.cpp                              Teensy: GNSS + tach + REC/TRACK consumer + (TODO) W5500 cloud client
platformio.ini                            Teensy build (do NOT add a CrowPanel env)
crowpanel-arduino/RaceDash/RaceDash.ino   CrowPanel: dash UI + settings + keyboards + track picker + GT911 touch (LIVE, panel-agnostic)
crowpanel-arduino/RaceDash/board_config.h Per-panel RGB pin map/timing/backlight/touch (DASH_BOARD 7|5); 7"=crowpanel7, 5"=crowpanel5
crowpanel-arduino/RaceDash/obd_ble.h      BLE OBD-II (ELM327) client — NimBLE on a core-0 task; coolant/IAT/voltage for sensor_type==2 (NOT RPM)
crowpanel-arduino/RaceDash_v0139_orig/    Pre-touch-rework backup of RaceDash (swap/revert screens easily)
crowpanel-arduino/PanelTest/PanelTest.ino Bare panel bring-up sketch — display only, NO touch (not a touch baseline)
crowpanel-baseline/                       Dead PIO experiments — do not touch
crowpanel-ui/                             Dead PIO experiments — do not touch
firmware/                                 OTA artifacts + manifest.json: teensy41-dash.hex, crowpanel7-dash.bin, crowpanel5-dash.bin
_vendor/CrowPanel-ESP32-Display-Course-File/   Elecrow's reference source, all revs (cloned for offline use; gfx_conf.h block CrowPanel_50 = the 5" pin map/timing)
LVGL_Library.pdf                          Generic upstream LVGL 9.0 docs (NOT Elecrow-specific, useless)
```

# racecar-35 cloud receiver

Tiny FastAPI service that accepts NDJSON session uploads from the race dash
and stores them on disk. Designed to run as a single Docker container behind
an nginx reverse proxy that terminates TLS — **this service speaks plain HTTP
only**, on port `8089`.

For initial LAN testing you can point the dash directly at this port over
plain HTTP without nginx in the path. Set Internet Protocol = HTTP and use the
LAN IP of the host running the container.

## Endpoints

| Method | Path                        | Purpose                                        |
| ------ | --------------------------- | ---------------------------------------------- |
| POST   | `/upload`                   | Whole-file AfterRace upload (NDJSON). Overwrites by `X-Session-Id` so retries are idempotent. |
| POST   | `/stream`                   | Live-stream append (NDJSON). Reserved for the future Ethernet-mode live path. |
| GET    | `/`                         | HTML index of the sessions you're allowed to see (see *Session visibility*). |
| GET    | `/sessions`                 | JSON listing of the sessions you're allowed to see. |
| GET    | `/sessions/<user>/<file>`   | Download one session file.                     |
| GET    | `/sessions/<user>/<file>/laps` | Auto-detected lap list + times for the review UI (JSON). Start/finish is detected from the GPS trace — no track table needed. |
| POST   | `/sessions/<user>/<file>/ai` | **AI corner analysis.** Body `{prompt, region:{points:[[lat,lon],…]}, model?}`. Extracts the telemetry inside the drawn polygon, computes per-lap metrics (entry/min/exit/max speed, time, distance, peak lateral/longitudinal g, max rpm), and asks the LLM for coaching. Returns `{ok, model, metrics, answer}`. Requires `RACECAR_AI_API_KEY`. |
| GET    | `/ai/models`                | `{enabled, default, models:[{id,name}]}` — the model picker for the review UI (fetched live from Open WebUI). |
| GET    | `/health`                   | Healthcheck for Docker / nginx. Returns `{"ok":true}`. |
| GET    | `/account`                  | Per-user account page (any signed-in user): view + copy + refresh your upload API key. |
| GET    | `/account/apikey`           | JSON `{email, api_key}` for the signed-in user (creates a key if missing). |
| POST   | `/account/apikey/refresh`   | Regenerate the signed-in user's API key. Old key stops working immediately. |
| GET    | `/admin`                    | Admin portal (admins only): add authorized Gmail accounts, grant/revoke admin. |
| GET    | `/admin/sessions/targets`   | Emails an admin may reassign a session to (all known accounts). Admins only. |
| POST   | `/admin/sessions/move`      | **Reassign a session to another user.** Body `{user, filename, target}` (target = destination email). Moves the `.ndjson` **and its AI history** into the target's dir; refuses if the target already has a same-named session. Admins only. |
| POST   | `/admin/users`              | Add an account or change its admin flag (JSON `{email, is_admin}`). Admins only. |
| POST   | `/admin/users/delete`       | Remove a managed account (JSON `{email}`). Admins only. |
| GET    | `/admin/user/<email>`       | Per-user screen: ALL USERS toggle + manage who this account can see. Admins only. |
| GET    | `/admin/canbus`             | CAN-capture portal: upload + list captures, links to review. Admins only. |
| POST   | `/admin/canbus/upload?name=` | Upload a CAN sniffer CSV (raw body). Admins only. |
| GET    | `/admin/canbus/<file>`      | Review a capture: per-ID stats + byte/word signal inspector. Admins only. |
| GET    | `/admin/canbus/<file>/data` | Parsed JSON (per-ID frame counts, byte ranges, downsampled series). Admins only. |
| GET    | `/admin/canbus/<file>/raw`  | Download the raw CSV. Admins only. |
| POST   | `/admin/canbus/<file>/delete` | Delete a capture. Admins only. |
| POST   | `/admin/users/visibility`   | Set the ALL USERS flag (JSON `{email, view_all}`). Admins only. |
| POST   | `/admin/users/grant`        | Let an account see another's sessions (JSON `{email, target}`). Admins only. |
| POST   | `/admin/users/revoke`       | Remove a granted account (JSON `{email, target}`). Admins only. |
| GET    | `/docs`                     | OpenAPI / Swagger UI for poking around.        |

## Access control / admin portal

There are three layers, lowest to highest precedence for *display*, but all
additive for *who may sign in*:

1. `RACECAR_ADMIN_EMAILS` (env) — **bootstrap admins**. Always allowed to sign
   in, always admin, and the only accounts that can use `/admin` on a fresh
   deploy. Editable only by changing `.env` + restarting.
2. `RACECAR_ALLOWED_EMAILS` (env) — optional legacy static allowlist (non-admin).
3. **Managed accounts** added from the `/admin` portal at runtime, persisted to
   `/data/users.json` (survives rebuilds; no redeploy needed to add a driver).

If **none** of the three are set, the server stays in open dev mode (any
verified Google account can sign in). Setting `RACECAR_ADMIN_EMAILS` activates
the allowlist: only listed/managed accounts can sign in from then on.

The `/admin` link appears in the header for admin accounts. From there an admin
can add a Gmail address (optionally as admin) and toggle/remove any
portal-managed account. Bootstrap admins show as `locked` and can't be edited
from the UI.

## Session visibility

The sessions index (`/` and `/sessions`) is **scoped per account**:

- Every account always sees **its own** uploaded sessions.
- **Admins** (bootstrap or portal-granted) see **everyone's** sessions.
- A non-admin account can be granted visibility of specific other accounts, or
  of **everyone**, from the admin portal.

In `/admin`, click a managed user (or its **manage** button) to open
`/admin/user/<email>`. That screen has:

- an **ALL USERS** checkbox — tick it and the account sees every user's
  sessions, with no list to maintain (`view_all` on the account record);
- a **can-view list** — type/pick another account's email and **add user** to
  share just that account's sessions; **remove** revokes it.

Visibility is enforced on the index, the JSON listing, downloads, the review
page, the parsed-data endpoint, and web deletes. Device uploads via `X-API-Key`
are unaffected. Settings persist in `/data/users.json` (fields `view_all` and
`can_view`).

## Per-user API keys

Every authorized account gets a unique 12-char API key, minted on first Google
login (or when an admin adds the account) and persisted in `/data/users.json`.
A user views/refreshes their own key from the **account** link in the header
(`/account`). Refreshing replaces the old key immediately.

Send the key as `X-API-Key` on uploads. When no `X-User-Email` header is
supplied, the upload is filed under the key owner's email automatically. This
is independent of the global `RACECAR_API_KEY` env var (which, when set, gates
all ingestion with a single shared secret).

## Request headers honored

These match the dash and Teensy firmware exactly:

```
Content-Type:  application/x-ndjson
X-API-Key:     <secret>           (optional; required if RACECAR_API_KEY is set)
X-User-Email:  user@example.com   (used to namespace files on disk)
X-Session-Id:  1714942567         (used in the saved filename; usually unix epoch)
X-Track-Name:  Lime_Rock_Park     (used in the saved filename)
```

## On-disk layout

```
/data/sessions/<email>/<session_id>_<track>.ndjson
```

Where:

- `<email>`, `<track>` are sanitized — anything outside `[A-Za-z0-9._@+-]` becomes `_`.
- `<session_id>` is whatever the dash put in `X-Session-Id` (the start unix epoch).

## Run with Docker

```bash
cp .env.example .env
# edit .env if you want to require an API key

docker compose up -d --build
docker compose logs -f
```

The container exposes port `8089` on the host. Data is persisted to `./data/`
on the host. Health probe runs every 30 s.

To check that everything is responsive:

```bash
curl http://localhost:8089/health
# {"ok":true,"service":"racecar-35 cloud","data_dir":"/data"}
```

## Configure the dash for this server

Settings → cloud rows on the dash, when **Internet mode = WiFi**:

```
Cloud host:   <LAN IP of the docker host>   e.g. 192.168.1.50
Cloud port:   8089
Protocol:     HTTP                          (HTTPS is the default; switch to HTTP for LAN test)
User email:   anything you want (used to namespace files)
API key:      match RACECAR_API_KEY, or any value if RACECAR_API_KEY is empty
Record cloud: ON
```

Then TOOLS → **Start test mode** → wait ~10 s → **Stop test mode**.

If everything is wired up correctly, you'll see (in `docker compose logs -f`):

```
INFO racecar.cloud: received 192.168.1.123 mode=w email=anon session=1714... track=TEST bytes=14352 lines=58 -> sessions/anon/1714..._TEST.ndjson
```

And opening `http://<host>:8089/` in a browser shows the new session in the table.

## AI corner analysis (review page)

The review page has an **AI Corner Analysis** card. Click **circle a section**,
then drag a loop around a corner (or a set of corners) on the track map — the
samples inside that polygon, **across every lap**, become the context for an LLM
coaching prompt. Preset buttons cover the common questions (brake zones, entry
speed, exit speed, best line, consistency) and there's a free-text box for
anything else.

How it works:
1. The browser sends the drawn polygon + question to `POST /sessions/<u>/<f>/ai`.
2. The server reprojects every sample through the same auto lap-detector the
   review page uses, keeps the points inside the polygon, and computes **per-lap
   metrics** for that section: entry / min / exit / max speed, time, distance,
   peak lateral & longitudinal g, max rpm.
3. Those metrics become a compact table in a race-engineer system prompt, sent to
   **Open WebUI** (`ai.blueuc.com`) via its OpenAI-compatible
   `POST /api/chat/completions` (Bearer key). The reply is rendered in the card.

**Config (`.env`):**

| Var | Purpose |
| --- | --- |
| `RACECAR_AI_API_KEY` | Open WebUI API key. **Blank = the AI card is hidden entirely.** |
| `RACECAR_AI_BASE_URL` | Open WebUI base (default `https://ai.blueuc.com`). |
| `RACECAR_AI_MODEL` | **Default model id.** Open WebUI hosts many models, so this names the one used when a request doesn't override it. Must match an id from `GET {base}/api/models` (e.g. `gpt-4o-mini`, `llama3.1:8b`). |
| `RACECAR_AI_TIMEOUT_SECONDS` | Upstream timeout (default 120). |

The review UI also fetches the live model list (`GET /ai/models`) into a
dropdown, so a user can override the default model per question; `RACECAR_AI_MODEL`
is the fallback. After setting these, recreate the container (`docker compose
… up -d`) — no rebuild needed since it's just env.

**Persistent history + cascade delete.** Every question + answer is saved per
session under `RACECAR_DATA_DIR/ai_history/<user>/<sessionfile>.json` and shown
as a collapsible list in the AI card (newest first; each item has *show region*
— redraws the polygon on the map — and *delete*). `GET .../ai/history` lists them,
`POST .../ai/delete {id}` removes one. **Deleting the session deletes its entire
AI history** (session `DELETE` / `/delete` cascade). Open WebUI's `<details>`
cost/token footer is stripped server-side (admin-only info) so only the coaching
text is stored/shown.

## Lap detection + review page

The review page (`/review/<user>/<file>`) shows a **lap table** and a
**delta-vs-best trace** in addition to the map / playback / G-meter.

Laps are detected server-side (`/sessions/<user>/<file>/laps`) straight from
the GPS trace — the cloud has no track table, so it **auto-detects the
start/finish line**: it anchors on the first moving fix, then closes a lap
each time the car returns within 40 m of that anchor (after leaving by >80 m),
guarded by a 20 s minimum lap time. Lap boundaries come back as
seconds-from-start so the browser can map them onto its sample array at any
stride. Constants live at the top of `_detect_laps()` in `app/main.py`
(`_LAP_RADIUS_KM`, `_LAP_MIN_SEC`, `_LAP_MOVING_MPH`).

On the page:
- the **lap table** lists every lap with its time, gap to best, and max speed;
  the fastest lap is highlighted.
- clicking a lap makes it the **primary lap**: it's highlighted on the map
  (the reference/ghost lap overlaid as a dashed green line), playback is
  scoped to just that lap, and the **delta trace** redraws — a
  distance-aligned time delta of the primary lap vs the reference lap (below
  zero / green = faster, above / red = slower).
- a **follow-dot** rides the delta trace in sync with playback (and with the
  map marker), so you can scrub to any corner and read whether you were up or
  down there. The live delta at the cursor is shown to the right of the chart.

### Comparison mode (incl. across sessions)

By default the reference lap is **this session's best lap**. You can instead
compare against **any lap from any session you're allowed to see** — e.g. an
admin diffing their own quick lap against another driver's lap in the same car:

- a **filterable session picker** (type to filter; backed by `/sessions`)
  chooses the comparison session;
- a **lap picker** then lists that session's laps, each labelled with its time
  and either `(fastest)` or the gap from that session's fastest lap;
- **best lap** resets back to this session's best.

Cross-session laps are fetched on demand (`/data` + `/laps`) and cached. The
delta is distance-aligned, so it only makes sense for laps of the **same
track**.

### Sync mode

A **sync** toggle controls how the comparison lap is sampled against the
primary lap during playback:

- **time** — the comparison car is shown at the **same elapsed time** into the
  lap, which is generally a *different* place on track, so it gets its own
  **red map dot** alongside the amber primary dot. Watch the gap open/close.
- **location** — the comparison is sampled at the **same place** on track (one
  dot), and its telemetry (speed / RPM / heading) is shown in **red** beneath
  the primary values so you can read "here I was 112 vs 118 mph" corner by
  corner.

The dash firmware does its own on-car lap timing (predictive lap time + live
delta vs the session best, shown in the middle column of the dash). The cloud
view is the post-session analysis counterpart.

## CAN-bus captures (admin)

The dash's CAN sniffer (Tools → Start CAN capture) writes a CSV per run
(`t_ms,id,ext,dlc,d0..d7`, data bytes in hex) to reverse-engineer the MS3Pro
broadcast byte layout. Instead of pulling the SD card, you can now upload that
CSV straight into the **admin → CAN captures** portal and analyse it in the
browser:

- **Upload** is a plain file picker (sent as a raw POST body — no extra deps).
  Files land in `RACECAR_DATA_DIR/canbus/<name>.csv`.
- The **review page** parses the capture server-side and shows, per CAN ID,
  the frame count + rate and each data byte's min/max/range. Pick an ID and:
  - the **bytes plot** draws d0–d7 over time (the changing bytes are enabled by
    default — a flat line means that byte never moved, a ramp means it carries
    a live signal);
  - the **16-bit word inspector** combines an adjacent byte pair (selectable
    start byte + big/little-endian) into a single value and plots it, so you
    can confirm e.g. "0x5F0 bytes 6–7 BE" ramps cleanly with RPM.

Capture while sweeping the throttle: the byte/word that tracks engine speed is
your RPM field. Note the ID + start byte + endianness and lock them into
`pumpCAN()` in `src/main.cpp`. Everything here is **admin-only** and the
filename is sanitised (no path traversal, single `.csv` on disk).

## Firmware / OTA hosting

The server also hosts the dash OTA feed, so updates don't depend on GitHub
raw's CDN (which ignores query-string cache-busting and serves ~5 min stale
after a push — the "new version isn't immediately available" problem).

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| GET | `/firmware/manifest.json` | none | Served **`Cache-Control: no-store`** → dash sees a new version instantly |
| GET | `/firmware/list` | none | JSON: each stored artifact's name + size + sha256 |
| GET | `/firmware/{file}` | none | Serves a stored `.bin`/`.hex` (device verifies sha256 from the manifest) |
| POST | `/firmware/upload?name=<file>` | `X-API-Key` | Body = raw artifact bytes. Only `.bin`/`.hex`/`.json` names allowed |

Stored under `RACECAR_DATA_DIR/firmware/` (survives container rebuilds). The
dash firmware's `OTA_MANIFEST_URL` points at `/firmware/manifest.json`; the
manifest's artifact URLs point back at `/firmware/<file>`.

### Publishing a release to the server

Build the four artifacts (see [../BUILD.md](../BUILD.md)), then:

```bash
RACECAR_API_KEY=<server RACECAR_API_KEY> \
  ./server/publish_firmware.sh 0.1.73 https://racecar.api.blueuc.com
```

This uploads all four binaries, then a **server-pointed manifest** (artifact
URLs = `<base>/firmware/<file>`, every `version` = the arg, legacy `crowpanel`
alias = `crowpanel7`), and prints the live manifest to verify. Binaries upload
first so the manifest never references a not-yet-present artifact.

### One-time transition off GitHub

Devices only start reading the server after they run firmware whose
`OTA_MANIFEST_URL` is the server. So the switch is a **single transition
release** published to BOTH places:

1. Rebuild + redeploy this container (so `/firmware/*` exists).
2. `publish_firmware.sh <ver>` → populate the server.
3. Cut the transition firmware (the one with the server `OTA_MANIFEST_URL`) to
   **GitHub** as usual, so devices still on the old GitHub-pointed firmware can
   OTA up to it one last time.
4. From then on, publish only with `publish_firmware.sh` — no CDN lag.

## Behind nginx (production)

Minimal nginx server block, assuming you've already got TLS termination on
some public hostname:

```nginx
server {
    listen 443 ssl http2;
    server_name racecar.api.blueuc.com;

    # ... ssl_certificate / ssl_certificate_key / etc ...

    client_max_body_size 96M;          # allow long sessions

    location / {
        proxy_pass http://127.0.0.1:9000;   # RACECAR_HOST_PORT (default 9000)
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_read_timeout 120s;
    }
}
```

uvicorn is started with `--proxy-headers --forwarded-allow-ips "*"` in the
Dockerfile so it will trust the `X-Forwarded-*` headers nginx sets.

## Local sanity test without the dash

```bash
echo '{"t":1,"speed_mph":42.0}' > /tmp/one.ndjson
curl -i -X POST http://localhost:8089/upload \
     -H 'Content-Type: application/x-ndjson' \
     -H 'X-User-Email: test@example.com' \
     -H 'X-Session-Id: 1714999999' \
     -H 'X-Track-Name: BenchTest' \
     --data-binary @/tmp/one.ndjson
# {"ok":true,"mode":"upload",...}

curl http://localhost:8089/sessions
# {"sessions":[{"user":"test_example.com","filename":"1714999999_BenchTest.ndjson",...}],"count":1}
```

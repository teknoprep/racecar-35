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
| GET    | `/`                         | HTML index of all saved sessions.              |
| GET    | `/sessions`                 | JSON listing of all saved sessions.            |
| GET    | `/sessions/<user>/<file>`   | Download one session file.                     |
| GET    | `/health`                   | Healthcheck for Docker / nginx. Returns `{"ok":true}`. |
| GET    | `/admin`                    | Admin portal (admins only): add authorized Gmail accounts, grant/revoke admin. |
| POST   | `/admin/users`              | Add an account or change its admin flag (JSON `{email, is_admin}`). Admins only. |
| POST   | `/admin/users/delete`       | Remove a managed account (JSON `{email}`). Admins only. |
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

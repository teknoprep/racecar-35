"""racecar-35 cloud receiver.

Single-purpose FastAPI service that accepts NDJSON session uploads from the
race dash and stores them on disk for later inspection. Designed to live
behind an nginx reverse proxy so this service speaks plain HTTP only.

Endpoints
---------
POST /upload   Whole-file AfterRace upload. Overwrites by session_id so
               retries are idempotent.
POST /stream   Live streaming append. Each request body is appended to the
               session file. Reserved for the future Ethernet-mode live
               streamer; in WiFi mode the dash uses /upload only.
GET  /         Tiny HTML index of every saved session.
GET  /sessions Same listing as JSON.
GET  /sessions/<user>/<file>  Download one session file.
GET  /health   Returns {"ok": true}; used by nginx / Docker healthcheck.

Headers honored (must match the dash firmware):
    X-API-Key       optional; if RACECAR_API_KEY is set in env, must match
    X-User-Email    used to namespace saved files
    X-Session-Id    used in filename (the recording's start unix epoch)
    X-Track-Name    used in filename (best-effort sanitized)
    Content-Type    expected: application/x-ndjson

Filesystem layout under RACECAR_DATA_DIR (default /data):
    /data/sessions/<email>/<session_id>_<track>.ndjson

Run locally with docker compose (see ../docker-compose.yml) or directly:
    uvicorn main:app --host 0.0.0.0 --port 8089 --proxy-headers
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = pathlib.Path(os.environ.get("RACECAR_DATA_DIR", "/data"))
API_KEY = os.environ.get("RACECAR_API_KEY", "").strip()
SERVICE_NAME = os.environ.get("RACECAR_SERVICE_NAME", "racecar-35 cloud")
MAX_BODY_BYTES = int(os.environ.get("RACECAR_MAX_BODY_BYTES", str(64 * 1024 * 1024)))

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "sessions").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("racecar.cloud")

app = FastAPI(title=SERVICE_NAME, version="0.1.0", docs_url="/docs")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# Note: '@' is intentionally NOT in the allowed set. Even though POSIX allows
# it in filenames, it's awkward in URLs (RFC 3986 reserves it for userinfo) and
# trips up some browsers/proxies when present in path segments. john@x.com
# becomes john_x.com on disk so download links work without %-encoding.
_safe_re = re.compile(r"[^A-Za-z0-9._+-]+")


def safe_name(s: Optional[str], default: str = "anon", maxlen: int = 96) -> str:
    """Reduce a header value to a filesystem-safe slug.

    The firmware url-encodes track names but we want a stable on-disk format,
    so collapse non-alphanumeric runs to underscores and clamp length.
    """
    s = (s or "").strip()
    if not s:
        return default
    out = _safe_re.sub("_", s).strip("_") or default
    return out[:maxlen]


def session_dir_for(email: str) -> pathlib.Path:
    """Per-user directory under sessions/, created on demand."""
    p = DATA_DIR / "sessions" / safe_name(email)
    p.mkdir(parents=True, exist_ok=True)
    return p


def authorize(x_api_key: Optional[str]) -> None:
    """Reject requests with a wrong API key. Empty config = allow all (dev)."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def parse_session_id(filename: str) -> Optional[int]:
    """Best-effort: extract the leading unix-epoch from <id>_<track>.ndjson."""
    m = re.match(r"^(\d+)_", filename)
    return int(m.group(1)) if m else None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": SERVICE_NAME, "data_dir": str(DATA_DIR)}


async def _save_body(
    request: Request,
    x_api_key: Optional[str],
    x_user_email: Optional[str],
    x_session_id: Optional[str],
    x_track_name: Optional[str],
    *,
    mode: str,
) -> JSONResponse:
    """Common body for /upload (mode='w') and /stream (mode='a')."""
    authorize(x_api_key)

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")

    email = safe_name(x_user_email)
    sid = safe_name(x_session_id, default="0")
    track = safe_name(x_track_name, default="UNKNOWN")
    filename = f"{sid}_{track}.ndjson"
    out_path = session_dir_for(email) / filename

    # Open in the requested mode. 'wb' overwrites (AfterRace whole-file POSTs
    # so retries are idempotent), 'ab' appends (live streaming).
    flags = "wb" if mode == "w" else "ab"
    with open(out_path, flags) as f:
        f.write(body)

    nl = body.count(b"\n")
    size = out_path.stat().st_size

    log.info(
        "received %s mode=%s email=%s session=%s track=%s bytes=%d lines=%d -> %s",
        request.client.host if request.client else "?",
        mode,
        email,
        sid,
        track,
        len(body),
        nl,
        out_path.relative_to(DATA_DIR),
    )

    return JSONResponse(
        {
            "ok": True,
            "mode": "upload" if mode == "w" else "stream",
            "path": str(out_path.relative_to(DATA_DIR)),
            "bytes_received": len(body),
            "lines_received": nl,
            "file_size_bytes": size,
            "ts": int(time.time()),
        }
    )


@app.post("/upload")
async def upload(
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_session_id: Optional[str] = Header(None),
    x_track_name: Optional[str] = Header(None),
) -> JSONResponse:
    return await _save_body(
        request,
        x_api_key,
        x_user_email,
        x_session_id,
        x_track_name,
        mode="w",
    )


@app.post("/stream")
async def stream(
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_session_id: Optional[str] = Header(None),
    x_track_name: Optional[str] = Header(None),
) -> JSONResponse:
    return await _save_body(
        request,
        x_api_key,
        x_user_email,
        x_session_id,
        x_track_name,
        mode="a",
    )


@app.get("/sessions")
async def list_sessions() -> dict:
    """JSON listing of saved sessions. Useful for tooling/cli inspection."""
    out = []
    sessions_root = DATA_DIR / "sessions"
    if sessions_root.exists():
        for user_dir in sorted(sessions_root.iterdir()):
            if not user_dir.is_dir():
                continue
            for f in sorted(user_dir.iterdir()):
                if not f.is_file() or not f.name.endswith(".ndjson"):
                    continue
                st = f.stat()
                out.append(
                    {
                        "user": user_dir.name,
                        "filename": f.name,
                        "session_id": parse_session_id(f.name),
                        "size_bytes": st.st_size,
                        "mtime": int(st.st_mtime),
                    }
                )
    return {"sessions": out, "count": len(out)}


def _resolve_session(user: str, filename: str) -> pathlib.Path:
    user = safe_name(user)
    filename = safe_name(filename, maxlen=256)
    p = DATA_DIR / "sessions" / user / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return p


@app.get("/sessions/{user}/{filename}")
async def download_session(user: str, filename: str) -> FileResponse:
    p = _resolve_session(user, filename)
    return FileResponse(
        p,
        media_type="application/x-ndjson",
        filename=p.name,
    )


@app.get("/sessions/{user}/{filename}/data")
async def session_data(
    user: str,
    filename: str,
    stride: int = Query(1, ge=1, le=100),
) -> JSONResponse:
    """Parsed NDJSON for the review UI.

    Returns every Nth sample (default every sample). The dash logs at 25 Hz, so
    a one-hour session is ~90 000 samples; the slider only needs maybe 5-10k
    points to feel smooth, so the client may request stride=10 for long files.

    Response shape:
        { "count": N, "samples": [ {t, lat, lon, speed_mph, ...}, ... ],
          "bounds": [[minLat,minLon],[maxLat,maxLon]] | null }
    """
    p = _resolve_session(user, filename)
    samples: list[dict] = []
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")
    has_geo = False
    with open(p, "rb") as f:
        for i, raw in enumerate(f):
            if i % stride != 0:
                continue
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw)
            except Exception:
                continue
            samples.append(obj)
            lat = obj.get("lat")
            lon = obj.get("lon")
            if (
                isinstance(lat, (int, float))
                and isinstance(lon, (int, float))
                and -90 <= lat <= 90
                and -180 <= lon <= 180
                and (lat or lon)
            ):
                has_geo = True
                if lat < min_lat: min_lat = lat
                if lat > max_lat: max_lat = lat
                if lon < min_lon: min_lon = lon
                if lon > max_lon: max_lon = lon
    bounds = (
        [[min_lat, min_lon], [max_lat, max_lon]] if has_geo else None
    )
    return JSONResponse(
        {"count": len(samples), "stride": stride, "bounds": bounds, "samples": samples}
    )


@app.get("/review/{user}/{filename}", response_class=HTMLResponse)
async def review(user: str, filename: str) -> str:
    p = _resolve_session(user, filename)
    sid = parse_session_id(p.name)
    when = (
        time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(sid)) if sid else "?"
    )
    return _REVIEW_HTML.replace("__USER__", safe_name(user)) \
                       .replace("__FILE__", p.name) \
                       .replace("__WHEN__", when)


# ---------------------------------------------------------------------------
# Minimal HTML index (intentionally dependency-free; just <table>).
# Behind nginx you can replace / augment this with a real UI later.
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Shared dark-dashboard CSS, used by index + review pages.
# Palette: near-black surfaces, off-white text, single saffron accent
# (#ffb020) that matches the dash RPM bar, mono numerics for telemetry.
# ---------------------------------------------------------------------------
_BASE_CSS = """
  :root {
    --bg: #0e1014; --surface: #181b22; --surface-2: #20242e;
    --line: #2a2f3a; --text: #e6e8ee; --muted: #8a92a3;
    --accent: #ffb020; --accent-dim: #6a4a10; --good: #6cd07a; --bad: #ff5d5d;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font: 14px/1.45 ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif; }
  a { color: var(--accent); text-decoration: none; }
  a:hover { text-decoration: underline; }
  header.app { display:flex; align-items:center; gap:16px; padding:14px 24px;
    border-bottom:1px solid var(--line); background:var(--surface); }
  header.app h1 { margin:0; font-size:16px; font-weight:600; letter-spacing:.04em; }
  header.app .dot { width:8px; height:8px; border-radius:50%; background:var(--accent);
    box-shadow:0 0 8px var(--accent); }
  header.app .crumbs { color:var(--muted); font-size:13px; }
  main { padding: 24px; max-width: 1400px; margin: 0 auto; }
  .mono, td.num { font-variant-numeric: tabular-nums;
    font-family: ui-monospace, "SF Mono", Menlo, Consolas, monospace; }
  input[type=text], input[type=search] {
    background: var(--surface); color: var(--text);
    border: 1px solid var(--line); border-radius: 6px;
    padding: 8px 12px; font-size: 14px; outline: none; width: 100%;
  }
  input[type=search]:focus { border-color: var(--accent); }
  .toolbar { display:flex; gap:12px; align-items:center; margin: 0 0 16px; }
  .toolbar .grow { flex: 1; }
  .pill { display:inline-block; padding:2px 8px; border-radius:999px;
    background: var(--surface-2); color: var(--muted); font-size: 12px; }
"""

_INDEX_HEAD = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>racecar-35 sessions</title>
<style>{_BASE_CSS}
 table {{ width: 100%; border-collapse: separate; border-spacing: 0;
   background: var(--surface); border: 1px solid var(--line); border-radius: 8px;
   overflow: hidden; }}
 th, td {{ padding: 10px 14px; font-size: 13px; text-align: left;
   border-bottom: 1px solid var(--line); }}
 th {{ background: var(--surface-2); color: var(--muted); font-weight: 600;
   text-transform: uppercase; letter-spacing: .06em; font-size: 11px; }}
 tbody tr:last-child td {{ border-bottom: none; }}
 tbody tr:hover {{ background: rgba(255,176,32,0.04); }}
 td.num {{ text-align: right; }}
 .empty {{ color: var(--muted); font-style: italic; padding: 24px; text-align:center; }}
 .summary {{ color: var(--muted); margin-top: 14px; font-size: 13px; }}
 .no-match {{ display:none; color: var(--muted); padding: 24px; text-align:center; }}
</style>
</head><body>
<header class="app"><span class="dot"></span><h1>racecar-35 cloud</h1>
  <span class="crumbs">sessions</span></header>
<main>
"""


def _human_bytes(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / 1024 / 1024:.2f} MB"


@app.get("/", response_class=HTMLResponse)
async def index() -> str:
    sessions_root = DATA_DIR / "sessions"
    rows: list[str] = []
    total = 0
    total_bytes = 0
    if sessions_root.exists():
        for user_dir in sorted(sessions_root.iterdir()):
            if not user_dir.is_dir():
                continue
            for f in sorted(user_dir.iterdir(), reverse=True):
                if not f.is_file() or not f.name.endswith(".ndjson"):
                    continue
                st = f.stat()
                sid = parse_session_id(f.name)
                when = (
                    time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(sid))
                    if sid
                    else "?"
                )
                size_str = _human_bytes(st.st_size)
                # Track name = filename middle bit: <sid>_<track>.ndjson
                track = f.name
                if track.endswith(".ndjson"):
                    track = track[:-7]
                track = re.sub(r"^\d+_", "", track) or "?"
                rows.append(
                    f"<tr><td>{user_dir.name}</td>"
                    f"<td class=mono>{when}</td>"
                    f"<td>{track}</td>"
                    f'<td class=mono><a href="/review/{user_dir.name}/{f.name}">{f.name}</a></td>'
                    f"<td class=num>{size_str}</td>"
                    f'<td><a href="/sessions/{user_dir.name}/{f.name}">download</a></td></tr>'
                )
                total += 1
                total_bytes += st.st_size

    if rows:
        body = (
            '<div class="toolbar"><div class="grow">'
            '<input type="search" id="q" placeholder="filter by user / track / date / filename\u2026" autofocus>'
            '</div><span class="pill" id="vis"></span></div>'
            "<table><thead><tr><th>user</th><th>started (UTC)</th>"
            "<th>track</th><th>filename</th><th>size</th><th></th></tr></thead><tbody id=\"rows\">"
            + "\n".join(rows)
            + "</tbody></table>"
            + '<div class="no-match" id="nomatch">no sessions match that filter.</div>'
            + f'<p class="summary">{total} session(s), {_human_bytes(total_bytes)} total</p>'
            + _INDEX_JS
        )
    else:
        body = '<p class="empty">no sessions uploaded yet.</p>'

    return _INDEX_HEAD + body + "</main></body></html>"

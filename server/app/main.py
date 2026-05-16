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

import logging
import os
import pathlib
import re
import time
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Request
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
_safe_re = re.compile(r"[^A-Za-z0-9._@+-]+")


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


@app.get("/sessions/{user}/{filename}")
async def download_session(user: str, filename: str) -> FileResponse:
    user = safe_name(user)
    filename = safe_name(filename, maxlen=256)
    p = DATA_DIR / "sessions" / user / filename
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(
        p,
        media_type="application/x-ndjson",
        filename=filename,
    )


# ---------------------------------------------------------------------------
# Minimal HTML index (intentionally dependency-free; just <table>).
# Behind nginx you can replace / augment this with a real UI later.
# ---------------------------------------------------------------------------
_INDEX_HEAD = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>racecar-35 cloud</title>
<style>
 body { font-family: -apple-system, system-ui, sans-serif; margin: 32px; color: #222; }
 h1   { font-weight: 600; }
 table{ border-collapse: collapse; }
 th, td { border: 1px solid #ccc; padding: 6px 12px; font-size: 14px; }
 th   { background: #f4f4f4; text-align: left; }
 td.num { text-align: right; font-variant-numeric: tabular-nums; }
 .empty { color: #888; font-style: italic; }
 a    { color: #06c; text-decoration: none; }
 a:hover { text-decoration: underline; }
</style>
</head><body>
<h1>racecar-35 sessions</h1>
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
                rows.append(
                    f"<tr><td>{user_dir.name}</td>"
                    f"<td>{when}</td>"
                    f"<td>{f.name}</td>"
                    f"<td class=num>{size_str}</td>"
                    f'<td><a href="/sessions/{user_dir.name}/{f.name}">download</a></td></tr>'
                )
                total += 1
                total_bytes += st.st_size

    if rows:
        body = (
            "<table><thead><tr><th>user</th><th>started (UTC)</th>"
            "<th>filename</th><th>size</th><th></th></tr></thead><tbody>"
            + "\n".join(rows)
            + "</tbody></table>"
            + f"<p>{total} session(s), {_human_bytes(total_bytes)} total</p>"
        )
    else:
        body = '<p class="empty">no sessions uploaded yet.</p>'

    return _INDEX_HEAD + body + "</body></html>"

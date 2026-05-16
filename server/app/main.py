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
GET  /         HTML index: search, manual validated upload, delete, review links.
GET  /sessions Same listing as JSON.
GET  /sessions/<user>/<file>  Download one session file.
DELETE /sessions/<user>/<file> Delete one session file (API key checked if set).
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

import base64
import hashlib
import hmac
import html
import json
import logging
import math
import os
import pathlib
import re
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

from fastapi import FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DATA_DIR = pathlib.Path(os.environ.get("RACECAR_DATA_DIR", "/data"))
API_KEY = os.environ.get("RACECAR_API_KEY", "").strip()
SERVICE_NAME = os.environ.get("RACECAR_SERVICE_NAME", "racecar-35 cloud")
MAX_BODY_BYTES = int(os.environ.get("RACECAR_MAX_BODY_BYTES", str(64 * 1024 * 1024)))

# Google OAuth is optional. If GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET are
# blank, the server stays in open dev mode. Once configured, all browser UI
# routes require Google sign-in; dash ingestion still uses X-API-Key.
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI", "").strip()
ALLOWED_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("RACECAR_ALLOWED_EMAILS", "").split(",")
    if e.strip()
}
SESSION_COOKIE = "racecar_session"
OAUTH_STATE_COOKIE = "racecar_oauth_state"
OAUTH_NEXT_COOKIE = "racecar_oauth_next"
COOKIE_SECURE = os.environ.get("RACECAR_COOKIE_SECURE", "0").lower() in {"1", "true", "yes", "on"}
SESSION_TTL_SECONDS = int(os.environ.get("RACECAR_SESSION_TTL_SECONDS", str(7 * 24 * 3600)))
SESSION_SECRET = os.environ.get("RACECAR_SESSION_SECRET", "").strip() or API_KEY or "racecar-35-dev-session-secret-change-me"

DATA_DIR.mkdir(parents=True, exist_ok=True)
(DATA_DIR / "sessions").mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("racecar.cloud")
if SESSION_SECRET == "racecar-35-dev-session-secret-change-me":
    log.warning("RACECAR_SESSION_SECRET is not set; OAuth sessions use a dev secret")

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


def oauth_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(data: str) -> str:
    mac = hmac.new(SESSION_SECRET.encode("utf-8"), data.encode("ascii"), hashlib.sha256).digest()
    return _b64url(mac)


def make_session_cookie(user: dict) -> str:
    now = int(time.time())
    payload = {
        "email": str(user.get("email", "")).lower(),
        "name": user.get("name") or user.get("email") or "",
        "picture": user.get("picture") or "",
        "sub": user.get("sub") or "",
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    raw = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return raw + "." + _sign(raw)


def current_user(request: Request) -> Optional[dict]:
    cookie = request.cookies.get(SESSION_COOKIE, "")
    if not cookie or "." not in cookie:
        return None
    raw, sig = cookie.rsplit(".", 1)
    if not hmac.compare_digest(_sign(raw), sig):
        return None
    try:
        payload = json.loads(_b64url_decode(raw))
    except Exception:
        return None
    if int(payload.get("exp", 0)) < int(time.time()):
        return None
    email = str(payload.get("email", "")).lower()
    if not email:
        return None
    return payload


def require_web_user(request: Request) -> Optional[dict]:
    """Require Google login when OAuth is configured; no-op in dev mode."""
    if not oauth_enabled():
        return None
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user


def login_redirect(request: Request) -> RedirectResponse:
    target = request.url.path
    if request.url.query:
        target += "?" + request.url.query
    return RedirectResponse("/login?" + urllib.parse.urlencode({"next": target}))


def authorize_api_or_user(request: Request, x_api_key: Optional[str]) -> Optional[dict]:
    """Allow valid dash API key OR logged-in Google user.

    If OAuth is not configured and RACECAR_API_KEY is blank, keep dev-mode
    compatibility and allow the operation.
    """
    if API_KEY and x_api_key == API_KEY:
        return None
    user = current_user(request) if oauth_enabled() else None
    if user:
        return user
    if API_KEY:
        raise HTTPException(status_code=401, detail="login or valid api key required")
    if oauth_enabled():
        raise HTTPException(status_code=401, detail="login required")
    return None


def oauth_redirect_uri(request: Request) -> str:
    if GOOGLE_REDIRECT_URI:
        return GOOGLE_REDIRECT_URI
    return str(request.url_for("auth_google_callback"))


def cookie_kwargs() -> dict:
    return {"httponly": True, "samesite": "lax", "secure": COOKIE_SECURE}


def _json_constant_error(name: str) -> None:
    """Reject Python json's non-standard NaN / Infinity extensions."""
    raise ValueError(f"invalid JSON constant {name}")


def _is_json_number(v: object) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(float(v))


_OPTIONAL_NUMERIC_FIELDS = {
    "fix", "sats", "alt_m", "speed_mph", "heading_deg", "rpm",
    "oil_psi", "coolant_f", "oil_psi_x10", "cool_f_x10",
    "ax", "ay", "az", "gx", "gy", "gz",
}


def validate_ndjson_body(body: bytes) -> dict:
    """Validate racecar session NDJSON before it is accepted.

    Rules are intentionally strict enough to catch accidental uploads (CSV,
    JSON arrays, browser error pages, partial files) but compatible with the
    firmware's descriptive-key NDJSON serializer:
      - non-empty UTF-8 text
      - one JSON object per non-empty line
      - every sample must have numeric finite t, lat, lon
      - lat/lon must be in normal WGS84 ranges
      - known telemetry numeric fields, when present, must be finite numbers
    """
    errors: list[str] = []
    warnings: list[str] = []
    samples = 0
    geo = 0
    first_t: Optional[float] = None
    last_t: Optional[float] = None

    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]

    try:
        lines = body.decode("utf-8").splitlines()
    except UnicodeDecodeError as e:
        raise HTTPException(
            status_code=422,
            detail={"message": "invalid NDJSON", "errors": [f"not UTF-8: {e}"]},
        )

    if not body.strip():
        raise HTTPException(
            status_code=422,
            detail={"message": "invalid NDJSON", "errors": ["file is empty"]},
        )

    for lineno, line in enumerate(lines, 1):
        raw = line.strip()
        if not raw:
            warnings.append(f"line {lineno}: blank line ignored")
            continue
        try:
            obj = json.loads(raw, parse_constant=_json_constant_error)
        except Exception as e:
            errors.append(f"line {lineno}: invalid JSON ({e})")
            if len(errors) >= 25:
                break
            continue
        if not isinstance(obj, dict):
            errors.append(f"line {lineno}: expected a JSON object, got {type(obj).__name__}")
            if len(errors) >= 25:
                break
            continue

        samples += 1

        t = obj.get("t")
        if not _is_json_number(t):
            errors.append(f"line {lineno}: missing/non-numeric t")
        else:
            tf = float(t)
            if first_t is None:
                first_t = tf
            if last_t is not None and tf < last_t:
                warnings.append(f"line {lineno}: timestamp moved backwards")
            last_t = tf

        lat = obj.get("lat")
        lon = obj.get("lon")
        if not _is_json_number(lat) or not _is_json_number(lon):
            errors.append(f"line {lineno}: missing/non-numeric lat/lon")
        else:
            latf = float(lat)
            lonf = float(lon)
            if not (-90 <= latf <= 90):
                errors.append(f"line {lineno}: lat out of range")
            if not (-180 <= lonf <= 180):
                errors.append(f"line {lineno}: lon out of range")
            geo += 1

        for field in _OPTIONAL_NUMERIC_FIELDS:
            if field in obj and not _is_json_number(obj[field]):
                errors.append(f"line {lineno}: {field} must be numeric")

        if len(errors) >= 25:
            break

    if samples == 0:
        errors.append("no JSON samples found")
    if geo == 0:
        errors.append("no valid lat/lon samples found")

    if errors:
        raise HTTPException(
            status_code=422,
            detail={"message": "invalid NDJSON", "errors": errors[:25], "warnings": warnings[:10]},
        )

    return {
        "samples": samples,
        "geo_samples": geo,
        "warnings": warnings[:10],
        "duration_s": (last_t - first_t) if first_t is not None and last_t is not None else 0,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login(request: Request, next: str = "/") -> Response:
    if not oauth_enabled():
        return HTMLResponse(_LOGIN_DISABLED_HTML)
    if current_user(request):
        return RedirectResponse(next if next.startswith("/") else "/")

    state = secrets.token_urlsafe(32)
    safe_next = next if next.startswith("/") and not next.startswith("//") else "/"
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": oauth_redirect_uri(request),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    }
    auth_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    page = _LOGIN_HTML.replace("__AUTH_URL__", html.escape(auth_url))
    resp = HTMLResponse(page)
    resp.set_cookie(OAUTH_STATE_COOKIE, state, max_age=600, **cookie_kwargs())
    resp.set_cookie(OAUTH_NEXT_COOKIE, safe_next, max_age=600, **cookie_kwargs())
    return resp


@app.get("/auth/google/callback")
async def auth_google_callback(
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
) -> Response:
    if error:
        return HTMLResponse(_LOGIN_ERROR_HTML.replace("__ERROR__", html.escape(error)), status_code=400)
    if not oauth_enabled():
        return HTMLResponse(_LOGIN_ERROR_HTML.replace("__ERROR__", "Google OAuth is not configured"), status_code=400)
    expected_state = request.cookies.get(OAUTH_STATE_COOKIE, "")
    if not code or not state or not expected_state or not hmac.compare_digest(state, expected_state):
        return HTMLResponse(_LOGIN_ERROR_HTML.replace("__ERROR__", "OAuth state mismatch; try again"), status_code=400)

    token_body = urllib.parse.urlencode({
        "code": code,
        "client_id": GOOGLE_CLIENT_ID,
        "client_secret": GOOGLE_CLIENT_SECRET,
        "redirect_uri": oauth_redirect_uri(request),
        "grant_type": "authorization_code",
    }).encode("utf-8")
    try:
        token_req = urllib.request.Request(
            "https://oauth2.googleapis.com/token",
            data=token_body,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(token_req, timeout=15) as r:
            token = json.loads(r.read().decode("utf-8"))
        access_token = token.get("access_token")
        if not access_token:
            raise RuntimeError("token response did not include access_token")

        user_req = urllib.request.Request(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        with urllib.request.urlopen(user_req, timeout=15) as r:
            user = json.loads(r.read().decode("utf-8"))
    except Exception as e:
        log.exception("google oauth failed")
        return HTMLResponse(
            _LOGIN_ERROR_HTML.replace("__ERROR__", html.escape(str(e))),
            status_code=502,
        )

    email = str(user.get("email", "")).lower()
    verified = user.get("email_verified") in (True, "true", "True", "1", 1)
    if not email or not verified:
        return HTMLResponse(_LOGIN_ERROR_HTML.replace("__ERROR__", "Google account email is not verified"), status_code=403)
    if ALLOWED_EMAILS and email not in ALLOWED_EMAILS:
        return HTMLResponse(_LOGIN_ERROR_HTML.replace("__ERROR__", f"{html.escape(email)} is not allowed"), status_code=403)

    next_url = request.cookies.get(OAUTH_NEXT_COOKIE, "/")
    if not next_url.startswith("/") or next_url.startswith("//"):
        next_url = "/"
    resp = RedirectResponse(next_url)
    resp.set_cookie(SESSION_COOKIE, make_session_cookie(user), max_age=SESSION_TTL_SECONDS, **cookie_kwargs())
    resp.delete_cookie(OAUTH_STATE_COOKIE)
    resp.delete_cookie(OAUTH_NEXT_COOKIE)
    return resp


@app.get("/logout")
async def logout() -> RedirectResponse:
    resp = RedirectResponse("/login")
    resp.delete_cookie(SESSION_COOKIE)
    resp.delete_cookie(OAUTH_STATE_COOKIE)
    resp.delete_cookie(OAUTH_NEXT_COOKIE)
    return resp


@app.get("/me")
async def me(request: Request) -> dict:
    user = current_user(request)
    return {"oauth_enabled": oauth_enabled(), "user": user}


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

    validation = validate_ndjson_body(body)

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

    nl = int(validation["samples"])
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
            "validation": validation,
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


@app.delete("/sessions/{user}/{filename}")
async def delete_session(
    user: str,
    filename: str,
    x_api_key: Optional[str] = Header(None),
) -> JSONResponse:
    authorize(x_api_key)
    p = _resolve_session(user, filename)
    size = p.stat().st_size
    rel = str(p.relative_to(DATA_DIR))
    p.unlink()
    try:
        p.parent.rmdir()  # tidy empty per-user directory
    except OSError:
        pass
    log.info("deleted session %s bytes=%d", rel, size)
    return JSONResponse({"ok": True, "deleted": rel, "bytes": size})


@app.post("/sessions/{user}/{filename}/delete")
async def delete_session_form(
    user: str,
    filename: str,
    x_api_key: Optional[str] = Header(None),
) -> JSONResponse:
    # Convenience alias for clients that can't send DELETE.
    return await delete_session(user, filename, x_api_key)


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
# HTML / CSS / JS for the index + review pages.
#
# The CSS variables in :root below are a direct mirror of the design tokens
# defined in /DESIGN.md (Google's DESIGN.md spec, name="Pit Wall"). If you
# edit one, edit the other.
#
#   token (DESIGN.md)            -> CSS variable
#   ---------------------------- -------------------
#   colors.primary               -> --primary
#   colors.bg                    -> --bg
#   colors.surface{,-2,-3}       -> --surface{,-2,-3}
#   colors.on-surface{,-muted}   -> --text / --muted
#   colors.good / error          -> --good / --bad
#   rounded.sm / md / full       -> --r-sm / --r-md / --r-full
#   spacing.sm/md/lg/xl          -> --sp-sm / --sp-md / --sp-lg / --sp-xl
#
# Inter + JetBrains Mono are loaded from Google Fonts; system fallbacks keep
# things sane if the page is offline.
# ---------------------------------------------------------------------------
_BASE_CSS = """
  /* ---- tokens (mirror of DESIGN.md Pit Wall) ------------------------- */
  :root {
    --primary:      #FFB020;
    --primary-hov:  #FFC04A;
    --on-primary:   #1A1300;
    --tertiary:     #6CD07A;
    --bg:           #0E1014;
    --surface:      #181B22;
    --surface-2:    #20242E;
    --surface-3:    #2A2F3A;
    --line:         #2A2F3A;
    --text:         #E6E8EE;
    --muted:        #8A92A3;
    --good:         #6CD07A;
    --bad:          #FF5D5D;
    --r-sm: 4px; --r-md: 8px; --r-lg: 12px; --r-full: 9999px;
    --sp-xs: 4px; --sp-sm: 8px; --sp-md: 16px; --sp-lg: 24px; --sp-xl: 32px;
    --ff-ui:  Inter, ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    --ff-mono: "JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; background: var(--bg); color: var(--text);
    font: 14px/1.45 var(--ff-ui); -webkit-font-smoothing: antialiased; }
  a { color: var(--primary); text-decoration: none; }
  a:hover { text-decoration: underline; }

  /* ---- typography roles --------------------------------------------- */
  .t-display   { font: 600 28px/1.1 var(--ff-ui); letter-spacing: -0.01em; }
  .t-headline  { font: 600 16px/1.2 var(--ff-ui); letter-spacing: 0.02em; }
  .t-label     { font: 600 11px/1 var(--ff-ui); letter-spacing: 0.08em;
                 text-transform: uppercase; color: var(--muted); }
  .t-tel-lg    { font: 600 36px/1 var(--ff-mono); font-feature-settings: 'tnum' 1, 'zero' 1; }
  .t-tel-md    { font: 500 18px/1.1 var(--ff-mono); font-feature-settings: 'tnum' 1, 'zero' 1; }
  .t-tel-sm    { font: 400 13px/1.3 var(--ff-mono); font-feature-settings: 'tnum' 1, 'zero' 1; }
  .mono, td.num { font-family: var(--ff-mono); font-variant-numeric: tabular-nums; }

  /* ---- header ------------------------------------------------------- */
  header.app { display:flex; align-items:center; gap: var(--sp-md);
    padding: 14px var(--sp-lg); border-bottom: 1px solid var(--line);
    background: var(--surface); }
  header.app h1 { margin:0; font: 600 14px/1 var(--ff-ui); letter-spacing: 0.08em;
    text-transform: uppercase; }
  header.app .dot { width:8px; height:8px; border-radius: var(--r-full);
    background: var(--primary); box-shadow: 0 0 8px var(--primary); }
  header.app .crumbs { color: var(--muted); font-size: 13px; }
  header.app .crumbs a { color: var(--muted); }
  header.app .crumbs a:hover { color: var(--text); }

  main { padding: var(--sp-lg); max-width: 1400px; margin: 0 auto; }

  /* ---- inputs ------------------------------------------------------- */
  input[type=text], input[type=search] {
    background: var(--surface); color: var(--text);
    border: 1px solid var(--line); border-radius: var(--r-sm);
    padding: 8px 12px; font: 14px var(--ff-ui); outline: none; width: 100%;
  }
  input[type=search]:focus { border-color: var(--primary); }

  /* ---- toolbar / pills ---------------------------------------------- */
  .toolbar { display:flex; gap: var(--sp-md); align-items:center; margin: 0 0 var(--sp-md); }
  .toolbar .grow { flex: 1; }
  .pill { display:inline-flex; align-items:center; padding: 4px 10px;
    border-radius: var(--r-full); background: var(--surface-2); color: var(--muted);
    font: 600 11px/1 var(--ff-ui); letter-spacing: 0.08em; text-transform: uppercase; }
  .pill.good { color: var(--good); }

  /* ---- buttons ------------------------------------------------------ */
  .btn { display: inline-flex; align-items:center; justify-content:center;
    gap: 6px; padding: 8px 14px; border-radius: var(--r-sm); border: 1px solid var(--line);
    background: var(--surface-2); color: var(--text); cursor: pointer;
    font: 600 11px/1 var(--ff-ui); letter-spacing: 0.08em; text-transform: uppercase; }
  .btn:hover { background: var(--surface-3); }
  .btn.primary { background: var(--primary); color: var(--on-primary); border-color: var(--primary); }
  .btn.primary:hover { background: var(--primary-hov); border-color: var(--primary-hov); }
"""

_FONTS_LINK = (
    '<link rel="preconnect" href="https://fonts.googleapis.com">'
    '<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>'
    '<link rel="stylesheet" '
    'href="https://fonts.googleapis.com/css2?'
    'family=Inter:wght@400;500;600&'
    'family=JetBrains+Mono:wght@400;500;600&display=swap">'
)

_INDEX_HEAD = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>racecar-35 sessions</title>
{_FONTS_LINK}
<style>{_BASE_CSS}
 table {{ width: 100%; border-collapse: separate; border-spacing: 0;
   background: var(--surface); border: 1px solid var(--line);
   border-radius: var(--r-md); overflow: hidden; }}
 th, td {{ padding: 12px 14px; font-size: 13px; text-align: left;
   border-bottom: 1px solid var(--line); }}
 th {{ background: var(--surface-2); color: var(--muted); font-weight: 600;
   text-transform: uppercase; letter-spacing: 0.08em; font-size: 11px; }}
 tbody tr:last-child td {{ border-bottom: none; }}
 tbody tr:hover {{ background: rgba(255,176,32,0.05); }}
 td.num {{ text-align: right; }}
 .empty {{ color: var(--muted); font-style: italic; padding: 24px; text-align:center; }}
 .summary {{ color: var(--muted); margin-top: var(--sp-md); font-size: 12px; }}
 .no-match {{ display:none; color: var(--muted); padding: 24px; text-align:center; }}
 .panel {{ background: var(--surface); border: 1px solid var(--line);
   border-radius: var(--r-md); padding: var(--sp-md); margin-bottom: var(--sp-md); }}
 .panel-head {{ display:flex; align-items:center; justify-content:space-between;
   gap: var(--sp-md); margin-bottom: var(--sp-md); }}
 .upload-grid {{ display:grid; grid-template-columns: 1.25fr 1fr 140px 1fr 1fr auto;
   gap: var(--sp-sm); align-items:end; }}
 .upload-grid label {{ display:block; color: var(--muted); font-size: 11px;
   text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 5px; }}
 .upload-grid input {{ min-width: 0; }}
 input[type=file] {{ width: 100%; color: var(--muted); font: 13px var(--ff-ui); }}
 input[type=file]::file-selector-button {{ margin-right: 10px; border: 1px solid var(--line);
   border-radius: var(--r-sm); background: var(--surface-2); color: var(--text);
   padding: 8px 12px; cursor: pointer; }}
 .upload-help {{ margin-top: var(--sp-sm); color: var(--muted); font-size: 12px; }}
 .upload-result {{ display:none; margin: var(--sp-md) 0 0; white-space: pre-wrap;
   background: var(--bg); border: 1px solid var(--line); border-radius: var(--r-sm);
   padding: var(--sp-sm); color: var(--muted); max-height: 140px; overflow:auto; }}
 .upload-result.ok {{ color: var(--good); }}
 .upload-result.bad {{ color: var(--bad); }}
 .btn.danger {{ color: var(--bad); }}
 .actions {{ display:flex; gap: var(--sp-sm); align-items:center; }}
 @media (max-width: 1100px) {{ .upload-grid {{ grid-template-columns: 1fr 1fr; }} }}
</style>
</head><body>
<header class="app"><span class="dot"></span><h1>racecar-35 \u00b7 pit wall</h1>
  <span class="crumbs">sessions</span></header>
<main>
"""

# Tiny dependency-free UI script: search/filter, browser-direct upload, delete.
_INDEX_JS = """
<script>
(function(){
  const $ = id => document.getElementById(id);
  const q = $('q');
  let rows = Array.from(document.querySelectorAll('#rows tr'));
  const vis = $('vis');
  const nomatch = $('nomatch');
  const result = $('uploadResult');

  function apiKey(){ return ($('apiKey')?.value || '').trim(); }
  function authHeaders(){ const k = apiKey(); return k ? {'X-API-Key': k} : {}; }
  function showResult(text, ok){
    if (!result) return;
    result.className = 'upload-result ' + (ok ? 'ok' : 'bad');
    result.style.display = 'block';
    result.textContent = text;
  }
  function render(){
    if (!q || !vis) return;
    rows = Array.from(document.querySelectorAll('#rows tr'));
    const terms = q.value.toLowerCase().split(/\\s+/).filter(Boolean);
    let shown = 0;
    for (const r of rows){
      const t = r.textContent.toLowerCase();
      const ok = terms.every(w => t.includes(w));
      r.style.display = ok ? '' : 'none';
      if (ok) shown++;
    }
    vis.textContent = shown + ' / ' + rows.length;
    if (nomatch) nomatch.style.display = shown === 0 && rows.length ? 'block' : 'none';
  }
  if (q) q.addEventListener('input', render);

  // Remember convenience fields locally in the browser only.
  for (const id of ['userEmail', 'apiKey']) {
    const el = $(id);
    if (!el) continue;
    const saved = localStorage.getItem('racecar.' + id);
    if (saved) el.value = saved;
    el.addEventListener('input', () => localStorage.setItem('racecar.' + id, el.value));
  }

  // Infer session id + track from common firmware filename:
  //   1714942567_LagunaSeca.ndjson
  const fileEl = $('sessionFile');
  if (fileEl) fileEl.addEventListener('change', () => {
    const f = fileEl.files && fileEl.files[0];
    if (!f) return;
    const base = f.name.replace(/\\.ndjson$/i, '');
    const m = base.match(/^(\\d+)[_-](.+)$/);
    if (m) {
      if (!$('sessionId').value) $('sessionId').value = m[1];
      if (!$('trackName').value) $('trackName').value = m[2];
    } else if (!$('trackName').value) {
      $('trackName').value = base || 'UNKNOWN';
    }
  });

  const form = $('uploadForm');
  if (form) form.addEventListener('submit', async ev => {
    ev.preventDefault();
    const f = fileEl.files && fileEl.files[0];
    if (!f) { showResult('Choose an .ndjson file first.', false); return; }
    const user = ($('userEmail').value || 'manual@upload.local').trim();
    const sid  = ($('sessionId').value || Math.floor(Date.now()/1000).toString()).trim();
    const trk  = ($('trackName').value || 'UNKNOWN').trim();
    try {
      showResult('Validating + uploading ' + f.name + ' ...', true);
      const body = await f.arrayBuffer();
      const headers = Object.assign({
        'Content-Type': 'application/x-ndjson',
        'X-User-Email': user,
        'X-Session-Id': sid,
        'X-Track-Name': trk
      }, authHeaders());
      const resp = await fetch('/upload', { method: 'POST', headers, body });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const d = data.detail || data;
        const errors = d.errors ? ('\n' + d.errors.join('\n')) : '';
        throw new Error((d.message || data.detail || ('HTTP ' + resp.status)) + errors);
      }
      const v = data.validation || {};
      showResult('OK: saved ' + data.path + '\n' + (v.samples || '?') + ' samples, '
                 + (v.geo_samples || '?') + ' GPS samples', true);
      setTimeout(() => location.reload(), 900);
    } catch(e) {
      showResult('Upload rejected: ' + e.message, false);
    }
  });

  document.addEventListener('click', async ev => {
    const btn = ev.target.closest('[data-delete]');
    if (!btn) return;
    const user = btn.dataset.user, file = btn.dataset.file;
    if (!confirm('Delete session permanently?\n\n' + user + '/' + file)) return;
    try {
      btn.disabled = true;
      const resp = await fetch('/sessions/' + encodeURIComponent(user) + '/' + encodeURIComponent(file), {
        method: 'DELETE', headers: authHeaders()
      });
      const data = await resp.json().catch(() => ({}));
      if (!resp.ok) throw new Error(data.detail || ('HTTP ' + resp.status));
      const tr = btn.closest('tr');
      if (tr) tr.remove();
      render();
    } catch(e) {
      alert('Delete failed: ' + e.message);
      btn.disabled = false;
    }
  });

  render();
})();
</script>
"""

_UPLOAD_PANEL_HTML = """
<section class="panel">
  <div class="panel-head">
    <div>
      <div class="t-label">Manual Session Upload</div>
      <div class="upload-help">Uploads are validated server-side before they are saved. Expected format: newline-delimited JSON, one telemetry object per line, with numeric <span class="mono">t</span>, <span class="mono">lat</span>, and <span class="mono">lon</span>.</div>
    </div>
    <span class="pill">.ndjson</span>
  </div>
  <form id="uploadForm" class="upload-grid">
    <div><label for="sessionFile">file</label><input id="sessionFile" type="file" accept=".ndjson,application/x-ndjson,text/plain"></div>
    <div><label for="userEmail">user email</label><input id="userEmail" type="text" placeholder="driver@example.com"></div>
    <div><label for="sessionId">session id</label><input id="sessionId" type="text" placeholder="unix epoch"></div>
    <div><label for="trackName">track</label><input id="trackName" type="text" placeholder="UNKNOWN"></div>
    <div><label for="apiKey">api key</label><input id="apiKey" type="text" placeholder="optional"></div>
    <button class="btn primary" type="submit">upload</button>
  </form>
  <pre id="uploadResult" class="upload-result"></pre>
</section>
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
                user_h = html.escape(user_dir.name)
                file_h = html.escape(f.name)
                track_h = html.escape(track)
                when_h = html.escape(when)
                rows.append(
                    f"<tr><td>{user_h}</td>"
                    f"<td class=mono>{when_h}</td>"
                    f"<td>{track_h}</td>"
                    f'<td class=mono><a href="/review/{user_h}/{file_h}">{file_h}</a></td>'
                    f"<td class=num>{size_str}</td>"
                    f'<td><div class="actions">'
                    f'<a class="btn" href="/sessions/{user_h}/{file_h}">download</a>'
                    f'<button class="btn danger" data-delete="1" data-user="{user_h}" data-file="{file_h}">delete</button>'
                    f'</div></td></tr>'
                )
                total += 1
                total_bytes += st.st_size

    if rows:
        listing = (
            '<div class="toolbar"><div class="grow">'
            '<input type="search" id="q" placeholder="filter by user / track / date / filename\u2026" autofocus>'
            '</div><span class="pill" id="vis"></span></div>'
            "<table><thead><tr><th>user</th><th>started (UTC)</th>"
            "<th>track</th><th>filename</th><th>size</th><th>actions</th></tr></thead><tbody id=\"rows\">"
            + "\n".join(rows)
            + "</tbody></table>"
            + '<div class="no-match" id="nomatch">no sessions match that filter.</div>'
            + f'<p class="summary">{total} session(s), {_human_bytes(total_bytes)} total</p>'
        )
    else:
        listing = '<p class="empty">no sessions uploaded yet.</p>'

    return _INDEX_HEAD + _UPLOAD_PANEL_HTML + listing + _INDEX_JS + "</main></body></html>"


# ---------------------------------------------------------------------------
# Review page. Two-pane layout (map left, telemetry tiles right), full-width
# scrub bar below. All visual choices follow /DESIGN.md "Pit Wall":
#   - saffron accent for the car dot, slider thumb, play button
#   - JetBrains Mono tnum for every changing numeral
#   - flat tonal layering, no shadows except the header status dot
#
# Leaflet is loaded from a CDN. We use CartoDB "dark matter" tiles which
# already match the dark surface palette without a custom tile server.
# ---------------------------------------------------------------------------
_REVIEW_HTML = (
    """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>review \u00b7 __FILE__</title>
""" + _FONTS_LINK + """
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>""" + _BASE_CSS + """
  .grid { display: grid; grid-template-columns: 1.4fr 1fr; gap: var(--sp-md); }
  @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }
  .card { background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--r-md); overflow: hidden; }
  .card-head { display:flex; justify-content:space-between; align-items:center;
    padding: 10px var(--sp-md); border-bottom: 1px solid var(--line);
    background: var(--surface); }
  .card-body { padding: var(--sp-md); }
  #map { height: 560px; width: 100%; background: var(--bg); }
  .leaflet-container { background: var(--bg); }
  .tiles { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-md); }
  .tile { background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--r-md); padding: var(--sp-md); }
  .tile.full { grid-column: 1 / -1; }
  .tile .label { color: var(--muted); margin-bottom: 6px;
    font: 600 11px/1 var(--ff-ui); letter-spacing: 0.08em; text-transform: uppercase; }
  .tile .val.accent { color: var(--primary); }
  .tile .unit { color: var(--muted); font: 500 13px var(--ff-mono); margin-left: 4px; }

  /* ---- scrub bar -------------------------------------------------- */
  .scrub { margin-top: var(--sp-md); padding: var(--sp-md); background: var(--surface);
    border: 1px solid var(--line); border-radius: var(--r-md); }
  .scrub-row { display:flex; align-items:center; gap: var(--sp-md); }
  .scrub-row .time { min-width: 140px; color: var(--muted); }
  .scrub-row .time .now { color: var(--text); }
  input[type=range].slider {
    -webkit-appearance: none; appearance: none;
    flex: 1; background: transparent; cursor: pointer;
  }
  input[type=range].slider:focus { outline: none; }
  input[type=range].slider::-webkit-slider-runnable-track {
    height: 4px; background: var(--surface-3); border-radius: var(--r-full);
  }
  input[type=range].slider::-moz-range-track {
    height: 4px; background: var(--surface-3); border-radius: var(--r-full);
  }
  input[type=range].slider::-webkit-slider-thumb {
    -webkit-appearance: none; width: 16px; height: 16px; border-radius: var(--r-full);
    background: var(--primary); border: none; margin-top: -6px;
    box-shadow: 0 0 0 2px var(--bg);
  }
  input[type=range].slider::-moz-range-thumb {
    width: 16px; height: 16px; border-radius: var(--r-full); background: var(--primary);
    border: none; box-shadow: 0 0 0 2px var(--bg);
  }

  .loading { padding: 32px; color: var(--muted); text-align: center; }
  .err { padding: 16px; color: var(--bad); }
</style>
</head><body>
<header class="app">
  <span class="dot"></span>
  <h1>racecar-35 \u00b7 pit wall</h1>
  <span class="crumbs"><a href="/">sessions</a> &rsaquo; __USER__ &rsaquo; <span class="mono">__FILE__</span></span>
  <span style="flex:1"></span>
  <span class="pill" id="started">__WHEN__</span>
  <span class="pill" id="count">\u2026</span>
  <a class="btn" href="/sessions/__USER__/__FILE__">download</a>
</header>
<main>
  <div id="loading" class="loading">loading session\u2026</div>
  <div id="app" style="display:none">
    <div class="grid">
      <div class="card">
        <div class="card-head"><span class="t-label">Track Map</span>
          <span class="t-label" id="gps-status">\u2014</span></div>
        <div id="map"></div>
      </div>
      <div class="tiles">
        <div class="tile full">
          <div class="label">Speed</div>
          <div><span class="t-tel-lg val accent" id="v-speed">\u2014</span><span class="unit">mph</span></div>
        </div>
        <div class="tile">
          <div class="label">RPM</div>
          <div><span class="t-tel-md val accent" id="v-rpm">\u2014</span></div>
        </div>
        <div class="tile">
          <div class="label">Heading</div>
          <div><span class="t-tel-md val" id="v-hdg">\u2014</span><span class="unit">\u00b0</span></div>
        </div>
        <div class="tile">
          <div class="label">Latitude</div>
          <div><span class="t-tel-sm val" id="v-lat">\u2014</span></div>
        </div>
        <div class="tile">
          <div class="label">Longitude</div>
          <div><span class="t-tel-sm val" id="v-lon">\u2014</span></div>
        </div>
        <div class="tile">
          <div class="label">Fix \u00b7 Sats</div>
          <div><span class="t-tel-md val" id="v-fix">\u2014</span></div>
        </div>
        <div class="tile">
          <div class="label">Altitude</div>
          <div><span class="t-tel-md val" id="v-alt">\u2014</span><span class="unit">m</span></div>
        </div>
      </div>
    </div>

    <div class="scrub">
      <div class="scrub-row">
        <button id="play" class="btn primary">play</button>
        <input id="slider" class="slider" type="range" min="0" max="0" value="0" step="1">
        <div class="time mono"><span class="now" id="t-now">0:00.0</span> / <span id="t-total">0:00.0</span></div>
      </div>
    </div>
  </div>
  <div id="err" class="err" style="display:none"></div>
</main>

<script>
(async function(){
  const USER='__USER__', FILE='__FILE__';
  const el = id => document.getElementById(id);
  const fmt = (v, d=1) => (v==null||!isFinite(v)) ? '\u2014' : Number(v).toFixed(d);
  const fmtInt = v => (v==null||!isFinite(v)) ? '\u2014' : String(Math.round(v));
  const fmtTime = s => {
    if (!isFinite(s) || s < 0) return '0:00.0';
    const m = Math.floor(s/60), r = s - m*60;
    return m + ':' + r.toFixed(1).padStart(4,'0');
  };
  const FIX_NAMES = ['no fix','dead reck','2D','3D','GNSS+DR','time only'];

  let resp, data;
  try {
    resp = await fetch('/sessions/' + encodeURIComponent(USER) + '/' + encodeURIComponent(FILE) + '/data');
    if (!resp.ok) throw new Error('HTTP ' + resp.status);
    data = await resp.json();
  } catch(e) {
    el('loading').style.display = 'none';
    el('err').style.display = 'block';
    el('err').textContent = 'failed to load: ' + e.message;
    return;
  }
  const S = data.samples || [];
  if (!S.length) {
    el('loading').textContent = 'session file is empty.';
    return;
  }
  el('loading').style.display = 'none';
  el('app').style.display = 'block';
  el('count').textContent = data.count + ' samples';

  // ---- Leaflet map (dark tiles match the surface palette) -------------
  const map = L.map('map', { zoomControl: true, attributionControl: true });
  L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png', {
    maxZoom: 19, attribution: '\u00a9 OpenStreetMap \u00a9 CARTO'
  }).addTo(map);

  // Track centerline = all samples that have a valid lat/lon.
  const latlngs = [];
  for (const s of S) {
    if (typeof s.lat === 'number' && typeof s.lon === 'number' && (s.lat || s.lon)) {
      latlngs.push([s.lat, s.lon]);
    }
  }
  let line=null, dot=null;
  if (latlngs.length) {
    line = L.polyline(latlngs, { color: '#FFB020', weight: 3, opacity: 0.4 }).addTo(map);
    dot  = L.circleMarker(latlngs[0], {
      radius: 7, color: '#1A1300', weight: 2, fillColor: '#FFB020', fillOpacity: 1
    }).addTo(map);
    if (data.bounds) map.fitBounds(data.bounds, { padding: [20,20] });
    else map.setView(latlngs[0], 15);
    el('gps-status').textContent = latlngs.length + ' fixes';
  } else {
    map.setView([0,0], 2);
    el('gps-status').textContent = 'no GPS fixes';
  }

  // ---- timestamps -----------------------------------------------------
  const t0 = S[0].t || 0;
  const tEnd = S[S.length-1].t || 0;
  const totalSec = Math.max(0, tEnd - t0);
  el('t-total').textContent = fmtTime(totalSec);

  // ---- slider + render ------------------------------------------------
  const slider = el('slider');
  slider.max = String(S.length - 1);

  function render(idx) {
    const s = S[idx];
    el('v-speed').textContent = (typeof s.speed_mph === 'number')
        ? (s.speed_mph >= 100 ? fmtInt(s.speed_mph) : fmt(s.speed_mph, 1)) : '\u2014';
    el('v-rpm').textContent   = fmtInt(s.rpm);
    el('v-hdg').textContent   = fmt(s.heading_deg, 0);
    el('v-lat').textContent   = fmt(s.lat, 6);
    el('v-lon').textContent   = fmt(s.lon, 6);
    el('v-alt').textContent   = fmt(s.alt_m, 1);
    const fix = s.fix, sats = s.sats;
    el('v-fix').textContent   = (fix==null) ? '\u2014'
      : (FIX_NAMES[fix] || ('fix '+fix)) + (sats!=null ? ' \u00b7 ' + sats : '');
    el('t-now').textContent   = fmtTime((s.t||t0) - t0);
    if (dot && typeof s.lat === 'number' && typeof s.lon === 'number' && (s.lat || s.lon)) {
      dot.setLatLng([s.lat, s.lon]);
    }
  }
  slider.addEventListener('input', () => render(Number(slider.value)));
  render(0);

  // ---- play / pause (real time relative to sample timestamps) ---------
  let playing = false, lastTick = 0, rafId = 0;
  const playBtn = el('play');
  function tick(now) {
    if (!playing) return;
    const dt = (now - lastTick) / 1000;
    lastTick = now;
    let idx = Number(slider.value);
    const cur = S[idx].t || t0;
    let next = idx;
    const target = cur + dt;
    while (next < S.length - 1 && (S[next+1].t || t0) <= target) next++;
    if (next >= S.length - 1) { stop(); slider.value = String(S.length - 1); render(S.length - 1); return; }
    if (next !== idx) { slider.value = String(next); render(next); }
    rafId = requestAnimationFrame(tick);
  }
  function start() {
    if (Number(slider.value) >= S.length - 1) slider.value = '0';
    playing = true; lastTick = performance.now();
    playBtn.textContent = 'pause';
    rafId = requestAnimationFrame(tick);
  }
  function stop() {
    playing = false; playBtn.textContent = 'play';
    if (rafId) cancelAnimationFrame(rafId);
  }
  playBtn.addEventListener('click', () => playing ? stop() : start());
})();
</script>
</body></html>
"""
)

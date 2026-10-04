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
import asyncio
import json
import logging
import math
import os
import pathlib
import re
import secrets
import difflib
import shutil
import struct
import threading
import time
import zlib
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
# Firmware-upload auth is DELIBERATELY separate from session-upload auth. If we
# reused RACECAR_API_KEY, setting it to lock down firmware uploads would also
# force every dash session upload to send that exact key (and 401 otherwise —
# which is precisely the bug that made uploads "die at ~250 lines"). Set
# RACECAR_FIRMWARE_KEY to gate POST /firmware/upload independently; it falls
# back to RACECAR_API_KEY only if unset.
FIRMWARE_KEY = os.environ.get("RACECAR_FIRMWARE_KEY", "").strip() or API_KEY
SERVICE_NAME = os.environ.get("RACECAR_SERVICE_NAME", "racecar-35 cloud")
# Wall-clock start of THIS process. The admin update button watches this: a
# successful rebuild replaces the process, so a jump here is proof the update
# actually landed (rather than trusting the host script's own status file).
_PROC_START = int(time.time())
MAX_BODY_BYTES = int(os.environ.get("RACECAR_MAX_BODY_BYTES", str(64 * 1024 * 1024)))

# ---- AI corner analysis (Open WebUI @ ai.blueuc.com, OpenAI-compatible) -----
# The review page can send the telemetry inside a user-drawn track region to an
# LLM for coaching feedback. We talk to Open WebUI's OpenAI-compatible API
# (POST {base}/api/chat/completions, Bearer key). Open WebUI hosts MANY models,
# so a default model id is REQUIRED (RACECAR_AI_MODEL) — it's the model used when
# the request doesn't name one. The review UI also fetches the live model list
# (GET {base}/api/models) so the user can override per-question from a dropdown.
AI_BASE_URL = os.environ.get("RACECAR_AI_BASE_URL", "https://ai.blueuc.com").strip().rstrip("/")
AI_API_KEY = os.environ.get("RACECAR_AI_API_KEY", "").strip()
AI_MODEL = os.environ.get("RACECAR_AI_MODEL", "").strip()   # default model id, e.g. "gpt-4o-mini"
AI_TIMEOUT = int(os.environ.get("RACECAR_AI_TIMEOUT_SECONDS", "120"))
# Sampling temperature. Newer models (e.g. Anthropic claude-sonnet-5) REJECT the
# temperature param outright, so we OMIT it unless RACECAR_AI_TEMPERATURE is set.
_ai_temp_raw = os.environ.get("RACECAR_AI_TEMPERATURE", "").strip()
AI_TEMPERATURE = float(_ai_temp_raw) if _ai_temp_raw else None
# Model ALLOWLIST. RACECAR_AI_MODELS is a CSV of model ids the UI may offer and
# the server will accept; if unset it falls back to just [RACECAR_AI_MODEL].
# When the allowlist is non-empty it is authoritative — the live 100+ model
# catalogue is NOT exposed and any other model id is rejected/forced to default.
# Leave BOTH unset only if you want the full live catalogue selectable.
AI_MODELS = [x.strip() for x in os.environ.get("RACECAR_AI_MODELS", "").split(",") if x.strip()]
if not AI_MODELS and AI_MODEL:
    AI_MODELS = [AI_MODEL]
# The default/preselected model: explicit RACECAR_AI_MODEL wins, else first of
# the allowlist, else empty (full-catalogue mode with no preselection).
AI_DEFAULT_MODEL = AI_MODEL or (AI_MODELS[0] if AI_MODELS else "")


# ---------------------------------------------------------------------------
# Basemap for the session map (review page / playback).
#
# ⚠️ CARTO's raster basemaps now REQUIRE an API key. Without one,
# basemaps.cartocdn.com serves a 256x256 "API KEY REQUIRED" placeholder tile
# instead of map data — two different tile coordinates come back byte-identical
# (verified), which is exactly the "the map says API KEY REQUIRED" symptom.
# So the default here is Esri's KEYLESS imagery: the same source the S/F picker
# (/tools/sfpicker), the standalone tools/track_sf_picker.html and the lineview
# popout already use, and it keeps detail to z19 over a race track.
#
# Override with env only — no rebuild needed, just `docker compose up -d`:
#   RACECAR_MAP_TILES=https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}
#   RACECAR_MAP_ATTRIB=Tiles (c) Esri
#   RACECAR_MAP_MAXZOOM=19
# BLANK means "use the default" (a blank var in .env must not kill the map);
# the literal RACECAR_MAP_TILES=none removes the basemap (traces on the surface).
# NOTE: Esri tile URLs are /{z}/{y}/{x} (row/col) — not /{z}/{x}/{y} — and the
# Canvas basemaps only have native data to z16, so over-zooming they upscale.
# ---------------------------------------------------------------------------
_DEFAULT_MAP_TILES = (
    "https://server.arcgisonline.com/ArcGIS/rest/services/"
    "World_Imagery/MapServer/tile/{z}/{y}/{x}"
)
_map_tiles_env = os.environ.get("RACECAR_MAP_TILES", "").strip()
if _map_tiles_env.lower() == "none":
    MAP_TILES = ""                              # explicit opt-out
else:
    MAP_TILES = _map_tiles_env or _DEFAULT_MAP_TILES
MAP_ATTRIB = os.environ.get("RACECAR_MAP_ATTRIB", "").strip() or (
    "Imagery \u00a9 Esri, Maxar, Earthstar Geographics"
)
_zoom_env = os.environ.get("RACECAR_MAP_MAXZOOM", "").strip()
try:
    MAP_MAXZOOM = int(_zoom_env) if _zoom_env else 19
except ValueError:
    MAP_MAXZOOM = 19

# ---------------------------------------------------------------------------
# Elevation for /track3d (the first-person 3D drive view).
#
# KEYLESS by design, like the imagery above: AWS's open "terrarium" terrain
# tiles (Mapzen/Amazon terrain, SRTM + others), PNG-encoded, no key, CORS-open.
# MapLibre decodes them with encoding="terrarium". Blank => the default;
# `none` => no 3D terrain at all (flat ground, the imagery still works).
#   RACECAR_MAP_DEM=https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png
#   RACECAR_MAP_DEM=none
# NOTE native data only goes to ~z15, so do not raise the maxzoom much beyond
# that — above it the tiles are upsampled (blocks below z13 are 2x2 pooled).
# ---------------------------------------------------------------------------
_DEFAULT_MAP_DEM = (
    "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
)
_dem_env = os.environ.get("RACECAR_MAP_DEM", "").strip()
MAP_DEM = "" if _dem_env.lower() == "none" else (_dem_env or _DEFAULT_MAP_DEM)
MAP_DEM_MAXZOOM = 15


def ai_enabled() -> bool:
    return bool(AI_API_KEY)


def ai_resolve_model(requested: Optional[str]) -> str:
    """Enforce the allowlist. Returns an allowed model id (or raises 503 if none
    is configured). A disallowed/blank request is forced to the default so a
    stale UI can never sneak a non-allowed model past the server."""
    req = (requested or "").strip()
    if AI_MODELS:
        return req if req in AI_MODELS else (AI_DEFAULT_MODEL or AI_MODELS[0])
    # Unrestricted mode: honor the request, else the default.
    return req or AI_DEFAULT_MODEL


# Per-session AI Q&A history lives in a parallel tree so it survives rebuilds
# alongside the session data and is trivially deleted with its session.
AI_HISTORY_DIR = DATA_DIR / "ai_history"
VIDEO_META_DIR = DATA_DIR / "video_meta"   # per-session YouTube link + sync offset
SHARE_DIR = DATA_DIR / "shares"            # public view-only overlay tokens
LAP_META_DIR = DATA_DIR / "lap_meta"       # per-session excluded-lap lists

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
# Bootstrap admins. These accounts can always sign in and always have admin
# rights, even before the on-disk users file exists. The admin portal lets
# them add more authorized accounts and grant/revoke admin to those accounts.
# Bootstrap admins themselves can only be changed by editing this env var.
ADMIN_EMAILS = {
    e.strip().lower()
    for e in os.environ.get("RACECAR_ADMIN_EMAILS", "").split(",")
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
# Persistent allowlist managed from the admin portal (separate from the static
# env vars above). Stored next to the session data so it survives rebuilds.
USERS_FILE = DATA_DIR / "users.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("racecar.cloud")
if SESSION_SECRET == "racecar-35-dev-session-secret-change-me":
    log.warning("RACECAR_SESSION_SECRET is not set; OAuth sessions use a dev secret")

app = FastAPI(title=SERVICE_NAME, version="0.1.0", docs_url="/docs")


# Floating "you are impersonating" badge injected into every HTML page while an
# admin is impersonating. Fixed bottom-right, always visible; click -> confirm ->
# /impersonate/stop restores the admin session.
_IMPERSONATE_BADGE = (
    "<div id=\"imp-badge\" onclick=\"if(confirm('Leave impersonation mode and "
    "return to your admin account?'))location.href='/impersonate/stop';\" "
    "style=\"position:fixed;right:18px;bottom:18px;z-index:2147483647;"
    "background:#FF5D5D;color:#1A1300;padding:11px 16px;border-radius:9999px;"
    "font:600 12px/1 Inter,system-ui,sans-serif;letter-spacing:.02em;cursor:pointer;"
    "box-shadow:0 6px 20px rgba(0,0,0,.5);user-select:none\" "
    "title=\"Click to leave impersonation mode\">"
    "\U0001F464 impersonating <b>__IMP__</b> \u2014 exit\u2715</div>"
)


@app.middleware("http")
async def _impersonation_badge_mw(request: Request, call_next):
    """Append the floating exit badge to HTML responses while impersonating."""
    resp = await call_next(request)
    try:
        payload = _session_payload(request)
        imp = payload.get("imp") if payload else None
        ctype = resp.headers.get("content-type", "")
        path = request.url.path
        if imp and ctype.startswith("text/html") and not path.startswith("/impersonate"):
            body = b""
            async for chunk in resp.body_iterator:
                body += chunk
            badge = _IMPERSONATE_BADGE.replace("__IMP__", html.escape(str(imp))).encode("utf-8")
            if b"</body>" in body:
                body = body.replace(b"</body>", badge + b"</body>", 1)
            else:
                body += badge
            headers = dict(resp.headers)
            headers.pop("content-length", None)
            return Response(content=body, status_code=resp.status_code,
                            headers=headers, media_type="text/html")
    except Exception:
        log.exception("impersonation badge injection failed")
    return resp


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


# --- login audit log (per-user append-only JSONL under /data/logins) --------
LOGIN_LOG_DIR = DATA_DIR / "logins"


def _login_log_path(email: str) -> pathlib.Path:
    return LOGIN_LOG_DIR / (safe_name(email) + ".jsonl")


def record_login(email: str, request: Request, event: str = "login") -> None:
    """Append one audit record for a sign-in (or other tracked event).
    Best-effort: never let logging break the auth flow."""
    try:
        email = (email or "").lower()
        if not email:
            return
        LOGIN_LOG_DIR.mkdir(parents=True, exist_ok=True)
        fwd = request.headers.get("x-forwarded-for", "")
        ip = (fwd.split(",")[0].strip() if fwd
              else (request.client.host if request.client else ""))
        ua = request.headers.get("user-agent", "")[:300]
        rec = {"ts": int(time.time()), "email": email, "ip": ip,
               "ua": ua, "event": event}
        with open(_login_log_path(email), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception:
        log.exception("login log write failed for %s", email)


def load_login_log(email: str, limit: int = 1000) -> list:
    """Recent login records for one user, newest first."""
    p = _login_log_path(email)
    out: list = []
    if p.exists():
        try:
            for line in p.read_text("utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
        except Exception:
            pass
    return out[-limit:][::-1]


def login_stats_all() -> list:
    """Per-user login metrics across everyone who has a login log. Rows:
    {email, logins, first_ts, last_ts, distinct_days, logins_7d, logins_30d}."""
    now = int(time.time())
    d7, d30 = now - 7 * 86400, now - 30 * 86400
    rows = []
    if not LOGIN_LOG_DIR.exists():
        return rows
    for p in LOGIN_LOG_DIR.glob("*.jsonl"):
        recs = []
        try:
            for line in p.read_text("utf-8").splitlines():
                line = line.strip()
                if line:
                    try:
                        recs.append(json.loads(line))
                    except Exception:
                        pass
        except Exception:
            continue
        if not recs:
            continue
        ts = [int(r.get("ts", 0)) for r in recs if r.get("ts")]
        email = recs[-1].get("email", p.stem)
        days = {time.strftime("%Y-%m-%d", time.gmtime(t)) for t in ts}
        rows.append({
            "email": email,
            "logins": len(recs),
            "first_ts": min(ts) if ts else 0,
            "last_ts": max(ts) if ts else 0,
            "distinct_days": len(days),
            "logins_7d": sum(1 for t in ts if t >= d7),
            "logins_30d": sum(1 for t in ts if t >= d30),
        })
    return rows


def authorize(x_api_key: Optional[str]) -> None:
    """Reject requests with a wrong API key. Empty config = allow all (dev)."""
    if API_KEY and x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="invalid api key")


def parse_session_id(filename: str) -> Optional[int]:
    """Best-effort: extract the leading unix-epoch from <id>_<track>.ndjson."""
    m = re.match(r"^(\d+)_", filename)
    return int(m.group(1)) if m else None


# Anything before 2000-01-01 or after 2100-01-01 is treated as not-a-real-epoch.
# Hit when the Teensy RTC was never set: session_start_unix lands on 0 or on a
# tiny millis()-style number, and the firmware sends X-Session-Id: 0 or 50000
# (or similar). Without a sanity check the index page rendered those as
# 1970-01-01 13:53:20 UTC which is useless. Inside this window we trust the
# value as-is; outside it we fall back to wall-clock time (upload time, or
# file mtime for pre-existing rows).
EPOCH_REASONABLE_MIN = 946684800        # 2000-01-01T00:00:00Z
EPOCH_REASONABLE_MAX = 4102444800       # 2100-01-01T00:00:00Z


def reasonable_epoch(value: Optional[int]) -> bool:
    return value is not None and EPOCH_REASONABLE_MIN <= value <= EPOCH_REASONABLE_MAX


def display_epoch_for(p: pathlib.Path) -> int:
    """Effective "started at" for a saved session file.

    Prefer the session_id encoded in the filename; if that's bogus (RTC not
    set on the firmware side), fall back to the file's mtime so the listing
    + review page never show 1970.
    """
    sid = parse_session_id(p.name)
    if reasonable_epoch(sid):
        return int(sid)  # type: ignore[arg-type]
    try:
        return int(p.stat().st_mtime)
    except OSError:
        return int(time.time())


def oauth_enabled() -> bool:
    return bool(GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET)


# ---------------------------------------------------------------------------
# Managed allowlist + admin model
#
# Two layers stack on top of each other:
#   1. Static env vars (RACECAR_ADMIN_EMAILS, RACECAR_ALLOWED_EMAILS) — these
#      are the bootstrap set and can only be changed by editing .env.
#   2. A JSON file (USERS_FILE) the admin portal reads + writes at runtime so
#      admins can add/remove authorized Google accounts without a redeploy.
#
# An account may sign in if the allowlist is "active" (any of the above is
# populated) and its email is in the union of all three sources. If nothing is
# configured, the server stays in open dev mode (any verified Google account).
# ---------------------------------------------------------------------------
def load_managed_users() -> dict:
    """Return {email: {email, is_admin, added_by, added_at}} from USERS_FILE."""
    try:
        with open(USERS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    users = data.get("users", []) if isinstance(data, dict) else []
    out: dict = {}
    for u in users:
        if not isinstance(u, dict):
            continue
        email = str(u.get("email", "")).strip().lower()
        if not email:
            continue
        can_view = u.get("can_view") or []
        if not isinstance(can_view, list):
            can_view = []
        out[email] = {
            "email": email,
            "is_admin": bool(u.get("is_admin")),
            "added_by": str(u.get("added_by") or ""),
            "added_at": int(u.get("added_at") or 0),
            "api_key": str(u.get("api_key") or ""),
            # Session-visibility scope:
            #   view_all  -> this account sees EVERY user's sessions.
            #   can_view  -> extra emails whose sessions this account may see
            #                (on top of its own). Admins implicitly see all.
            "view_all": bool(u.get("view_all")),
            "can_view": sorted({str(e).strip().lower() for e in can_view if str(e).strip()}),
        }
    return out


# Per-user API key for the dash firmware. 12 chars from an unambiguous-ish
# alphanumeric alphabet (62^12 keyspace). Each allowed account gets one on
# first login; it can be regenerated from the account page at any time.
_API_KEY_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"


def generate_api_key(n: int = 12) -> str:
    return "".join(secrets.choice(_API_KEY_ALPHABET) for _ in range(n))


def email_for_api_key(key: str) -> Optional[str]:
    """Return the owner email for a per-user API key, or None."""
    key = (key or "").strip()
    if not key:
        return None
    for email, u in load_managed_users().items():
        stored = str(u.get("api_key") or "")
        if stored and hmac.compare_digest(stored, key):
            return email
    return None


def ensure_user_record(email: str) -> dict:
    """Guarantee an allowed account has a persisted record + API key.

    Called on every successful login so a brand-new (or pre-existing env)
    account always has a 12-char key ready the first time it signs in.
    """
    email = (email or "").strip().lower()
    if not email:
        return {}
    users = load_managed_users()
    u = users.get(email)
    changed = False
    if u is None:
        u = {
            "email": email,
            "is_admin": False,
            "added_by": "auto (first login)",
            "added_at": int(time.time()),
            "api_key": generate_api_key(),
        }
        users[email] = u
        changed = True
    elif not u.get("api_key"):
        u["api_key"] = generate_api_key()
        users[email] = u
        changed = True
    if changed:
        save_managed_users(users)
    return u


def refresh_user_api_key(email: str) -> str:
    """Regenerate (or create) the API key for an account and persist it."""
    email = (email or "").strip().lower()
    users = load_managed_users()
    u = users.get(email) or {
        "email": email,
        "is_admin": False,
        "added_by": "auto (first login)",
        "added_at": int(time.time()),
    }
    u["api_key"] = generate_api_key()
    users[email] = u
    save_managed_users(users)
    return u["api_key"]


def save_managed_users(users: dict) -> None:
    """Atomically persist the managed users dict back to USERS_FILE."""
    payload = {"users": sorted(users.values(), key=lambda u: u["email"])}
    tmp = USERS_FILE.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2)
    tmp.replace(USERS_FILE)


def allowlist_active() -> bool:
    """True when sign-in is restricted to an explicit allowlist."""
    return bool(ADMIN_EMAILS or ALLOWED_EMAILS or load_managed_users())


def allowed_emails() -> set:
    """Union of every email permitted to sign in."""
    return set(ADMIN_EMAILS) | set(ALLOWED_EMAILS) | set(load_managed_users().keys())


def is_allowed_email(email: str) -> bool:
    email = (email or "").lower()
    if not allowlist_active():
        return True  # open dev mode
    return email in allowed_emails()


def is_admin_email(email: str) -> bool:
    email = (email or "").lower()
    if not email:
        return False
    if email in ADMIN_EMAILS:
        return True
    u = load_managed_users().get(email)
    return bool(u and u["is_admin"])


def user_sees_all(email: str) -> bool:
    """True if this account may see EVERY user's sessions (admin or view_all)."""
    email = (email or "").lower()
    if not email:
        return False
    if email in ADMIN_EMAILS:
        return True
    u = load_managed_users().get(email)
    return bool(u and (u["is_admin"] or u.get("view_all")))


def visible_dirnames_for(email: str) -> Optional[set]:
    """Sanitized session-dir names this account may view, or None for ALL.

    Always includes the account's own directory, plus every email in its
    can_view grant list. Admins / view_all accounts get None (= unrestricted).
    """
    if user_sees_all(email):
        return None
    email = (email or "").lower()
    names = {safe_name(email)} if email else set()
    u = load_managed_users().get(email)
    if u:
        for e in u.get("can_view") or []:
            names.add(safe_name(e))
    return names


def can_view_dir(email: str, dirname: str) -> bool:
    vis = visible_dirnames_for(email)
    return vis is None or dirname in vis


def gate_view_dir(request: Request, dirname: str) -> None:
    """Raise unless the signed-in user may view sessions under dirname.

    No-op in dev mode (OAuth off) so bench testing still sees everything.
    """
    if not oauth_enabled():
        return
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    if not can_view_dir(str(user.get("email", "")), dirname):
        raise HTTPException(status_code=403, detail="you don't have access to that user's sessions")


def can_delete_dir(email: str, dirname: str) -> bool:
    """Web-delete permission: your OWN sessions only, unless you're an admin.

    Being *granted* visibility of another account (can_view / view_all) lets
    you SEE that account's sessions but NOT delete them — deletes are
    destructive, so they stay owner-only. Admins (bootstrap or portal-granted)
    may delete anyone's.
    """
    email = (email or "").lower()
    if is_admin_email(email):
        return True
    return bool(email) and dirname == safe_name(email)


def gate_delete_dir(request: Request, dirname: str) -> None:
    """Raise unless the signed-in user may DELETE sessions under dirname.

    No-op in dev mode (OAuth off) so bench testing still works — mirrors
    gate_view_dir.
    """
    if not oauth_enabled():
        return
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    if not can_delete_dir(str(user.get("email", "")), dirname):
        raise HTTPException(status_code=403, detail="you can only delete your own sessions")


def require_admin(request: Request) -> dict:
    """Gate admin-portal routes: must be a logged-in admin Google account."""
    if not oauth_enabled():
        raise HTTPException(status_code=403, detail="admin portal requires Google OAuth to be configured")
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    if not is_admin_email(str(user.get("email", ""))):
        raise HTTPException(status_code=403, detail="admin access required")
    return user


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _b64url_decode(s: str) -> bytes:
    return base64.urlsafe_b64decode(s + "=" * (-len(s) % 4))


def _sign(data: str) -> str:
    mac = hmac.new(SESSION_SECRET.encode("utf-8"), data.encode("ascii"), hashlib.sha256).digest()
    return _b64url(mac)


def make_session_cookie(user: dict, imp: Optional[str] = None,
                        imp_by: Optional[str] = None) -> str:
    now = int(time.time())
    payload = {
        "email": str(user.get("email", "")).lower(),
        "name": user.get("name") or user.get("email") or "",
        "picture": user.get("picture") or "",
        "sub": user.get("sub") or "",
        "iat": now,
        "exp": now + SESSION_TTL_SECONDS,
    }
    # Impersonation: `imp` is the account the (admin) `imp_by` is viewing AS. The
    # base fields above stay the REAL admin so we can restore them on exit.
    if imp:
        payload["imp"] = str(imp).lower()
        payload["imp_by"] = str(imp_by or user.get("email", "")).lower()
    raw = _b64url(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    return raw + "." + _sign(raw)


def _session_payload(request: Request) -> Optional[dict]:
    """Verified raw cookie payload (INCLUDING imp/imp_by), or None. Use this when
    you need the impersonation fields; use current_user() for the effective user."""
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
    if not str(payload.get("email", "")).lower():
        return None
    return payload


def current_user(request: Request) -> Optional[dict]:
    payload = _session_payload(request)
    if not payload:
        return None
    imp = str(payload.get("imp", "")).lower()
    if imp:
        # Present as the impersonated account so ALL view/authorization logic
        # treats the request as that user; tag the real admin for the exit path.
        return {
            "email": imp,
            "name": imp,
            "picture": "",
            "sub": "",
            "iat": payload.get("iat", 0),
            "exp": payload.get("exp", 0),
            "impersonating": True,
            "real_admin": str(payload.get("imp_by", "")).lower(),
        }
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
    # v: every remaining channel the system can hold (see the firmware sample
    # writer). Null is still accepted (sensor absent/faulted); present = finite.
    "map_kpa", "iat_f", "batt_v", "afr_can", "oil_can_psi",
    "tps_pct", "spark_deg", "lap",
}


def validate_ndjson_body(body: bytes) -> dict:
    """Validate racecar session NDJSON before it is accepted.

    Rules are intentionally strict enough to catch accidental uploads (CSV,
    JSON arrays, browser error pages, partial files) but compatible with the
    firmware's descriptive-key NDJSON serializer:
      - non-empty UTF-8 text
      - one JSON object per non-empty line
      - every sample must have numeric finite t (or t_ms fallback), lat, lon
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
        if _is_json_number(t):
            tf = float(t)
        else:
            # If the Teensy's RTC/NTP was not set when recording started, the
            # firmware logs relative milliseconds as t_ms. Accept that for
            # upload validation so bench/test sessions are not rejected.
            t_ms = obj.get("t_ms")
            if _is_json_number(t_ms):
                tf = float(t_ms) / 1000.0
            else:
                errors.append(f"line {lineno}: missing/non-numeric t or t_ms")
                tf = None
        if tf is not None:
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
            # Firmware deliberately emits JSON null for faulted/absent analog
            # sensors (oil pressure, coolant, etc.). Treat null as "missing"
            # for optional telemetry; if a value is present, it must be finite.
            if field in obj and obj[field] is not None and not _is_json_number(obj[field]):
                errors.append(f"line {lineno}: {field} must be numeric or null")

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
    if not is_allowed_email(email):
        return HTMLResponse(_LOGIN_ERROR_HTML.replace("__ERROR__", f"{html.escape(email)} is not authorized. Ask an admin to add your account."), status_code=403)

    # Guarantee this account has a persisted record + per-user API key the
    # first time it signs in (bootstrap admins included).
    ensure_user_record(email)
    record_login(email, request)   # audit: track every real sign-in

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
    return {
        "oauth_enabled": oauth_enabled(),
        "user": user,
        "is_admin": is_admin_email(str((user or {}).get("email", ""))),
    }


# ---------------------------------------------------------------------------
# Per-user account page: view + refresh your own upload API key.
# Any signed-in account (not just admins) may use these.
# ---------------------------------------------------------------------------
def _require_account_user(request: Request) -> dict:
    """Signed-in user for account/API-key routes; 400 in dev (no OAuth)."""
    if not oauth_enabled():
        raise HTTPException(
            status_code=400,
            detail="per-user API keys require Google OAuth to be configured",
        )
    user = current_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="login required")
    return user


@app.get("/account", response_class=HTMLResponse)
async def account_page(request: Request) -> Response:
    if not oauth_enabled():
        return HTMLResponse(_ADMIN_DISABLED_HTML, status_code=400)
    user = current_user(request)
    if not user:
        return login_redirect(request)
    email = str(user.get("email", ""))
    rec = ensure_user_record(email)
    page = (_ACCOUNT_HTML
            .replace("__USER_CHIP__", _user_chip_html(user))
            .replace("__EMAIL__", html.escape(email))
            .replace("__APIKEY__", html.escape(str(rec.get("api_key") or ""))))
    return HTMLResponse(page)


@app.get("/account/apikey")
async def account_apikey(request: Request) -> dict:
    user = _require_account_user(request)
    rec = ensure_user_record(str(user.get("email", "")))
    return {"email": rec.get("email"), "api_key": rec.get("api_key")}


@app.post("/account/apikey/refresh")
async def account_apikey_refresh(request: Request) -> JSONResponse:
    user = _require_account_user(request)
    email = str(user.get("email", "")).lower()
    new_key = refresh_user_api_key(email)
    log.info("user %s refreshed their API key", email)
    return JSONResponse({"ok": True, "email": email, "api_key": new_key})


# ---------------------------------------------------------------------------
# Admin portal
# ---------------------------------------------------------------------------
def _admin_rows_html(self_email: str) -> str:
    self_email = (self_email or "").lower()

    def you_badge(em: str) -> str:
        return ' <span class="badge env">you</span>' if em == self_email else ''

    def imp_btn(em: str) -> str:
        if em == self_email:
            return ''
        return (f"<button class='btn' data-act='impersonate' "
                f"data-email='{html.escape(em)}'>impersonate</button>")

    def hist_btn(em: str) -> str:
        return (f"<button class='btn' data-act='history' "
                f"data-email='{html.escape(em)}'>history</button>")

    def vis_badge(view_all: bool, n: int) -> str:
        if view_all:
            return " <span class='badge admin'>sees all</span>"
        if n:
            return f" <span class='badge env'>sees {n}</span>"
        return ""

    def env_row(em: str, is_admin: bool) -> str:
        em_h = html.escape(em)
        role = ('<span class="badge admin">admin</span>' if is_admin
                else '<span class="badge">user</span>')
        # Admins implicitly see all; mark it.
        sees = " <span class='badge admin'>sees all</span>" if is_admin else ""
        return (f"<tr><td class=mono>{em_h}{you_badge(em)}</td>"
                f"<td>{role} <span class='badge env'>env</span>{sees}</td>"
                f"<td class=mono>.env</td>"
                f"<td><div class='row-actions'>{imp_btn(em)}{hist_btn(em)}"
                f"<span style='color:var(--muted)'>locked</span></div></td></tr>")

    def managed_row(em: str, u: dict) -> str:
        em_h = html.escape(em)
        is_admin = u["is_admin"]
        role = ('<span class="badge admin">admin</span>' if is_admin
                else '<span class="badge">user</span>')
        sees = (" <span class='badge admin'>sees all</span>" if is_admin
                else vis_badge(bool(u.get("view_all")), len(u.get("can_view") or [])))
        added_by = html.escape(u.get("added_by") or "")
        toggle_label = 'revoke admin' if is_admin else 'make admin'
        return (f"<tr><td class=mono><a href='/admin/user/{em_h}'>{em_h}</a>{you_badge(em)}</td>"
                f"<td>{role}{sees}</td>"
                f"<td class=mono>{added_by}</td>"
                f"<td><div class='row-actions'>"
                f"<a class='btn' href='/admin/user/{em_h}'>manage</a>"
                f"{imp_btn(em)}{hist_btn(em)}"
                f"<button class='btn' data-act='toggle' data-admin='{1 if is_admin else 0}' data-email='{em_h}'>{toggle_label}</button>"
                f"<button class='btn danger' data-act='remove' data-email='{em_h}'>remove</button>"
                f"</div></td></tr>")

    rows: list[str] = []
    seen: set = set()
    for em in sorted(ADMIN_EMAILS):
        rows.append(env_row(em, True))
        seen.add(em)
    for em in sorted(ALLOWED_EMAILS):
        if em in seen:
            continue
        rows.append(env_row(em, False))
        seen.add(em)
    for em, u in sorted(load_managed_users().items()):
        if em in seen:  # bootstrap env entry wins; don't double-list
            continue
        rows.append(managed_row(em, u))
    if not rows:
        return ('<tr><td colspan="4" class="empty" '
                'style="color:var(--muted);font-style:italic;text-align:center">'
                'no authorized accounts yet</td></tr>')
    return "\n".join(rows)


@app.get("/admin", response_class=HTMLResponse)
async def admin_page(request: Request) -> Response:
    if not oauth_enabled():
        return HTMLResponse(_ADMIN_DISABLED_HTML, status_code=400)
    user = current_user(request)
    if not user:
        return login_redirect(request)
    self_email = str(user.get("email", ""))
    if not is_admin_email(self_email):
        return HTMLResponse(
            _LOGIN_ERROR_HTML.replace("__ERROR__", "Admin access is required for this page."),
            status_code=403,
        )
    page = (_ADMIN_HTML
            .replace("__USER_CHIP__", _user_chip_html(user))
            .replace("__ROWS__", _admin_rows_html(self_email))
            .replace("__SELF__", html.escape(self_email.lower()))
            .replace("__HINT_NOW__", html.escape(HOST_UPDATE_HINT_NOW))
            .replace("__HINT_INSTALL__", html.escape(HOST_UPDATE_HINT_INSTALL)))
    return HTMLResponse(page)


@app.get("/admin/users")
async def admin_list_users(request: Request) -> dict:
    """JSON view of the access model (for tooling / debugging)."""
    require_admin(request)
    return {
        "bootstrap_admins": sorted(ADMIN_EMAILS),
        "env_allowed": sorted(ALLOWED_EMAILS),
        "managed": sorted(load_managed_users().values(), key=lambda u: u["email"]),
        "allowlist_active": allowlist_active(),
    }


@app.post("/admin/impersonate")
async def admin_impersonate_start(request: Request) -> JSONResponse:
    """Begin impersonating another user (admin only). Re-issues the session
    cookie with imp=<target>, imp_by=<admin>. From then on current_user() reports
    the target so the whole site behaves as them; the floating badge exits.
    Body JSON: {email}."""
    admin = require_admin(request)   # must be a REAL admin (not already impersonating)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    target = str(body.get("email", "")).strip().lower()
    if not target:
        raise HTTPException(status_code=400, detail="email required")
    admin_email = str(admin.get("email", "")).lower()
    if target == admin_email:
        raise HTTPException(status_code=400, detail="cannot impersonate yourself")
    resp = JSONResponse({"ok": True, "impersonating": target})
    resp.set_cookie(
        SESSION_COOKIE,
        make_session_cookie(admin, imp=target, imp_by=admin_email),
        max_age=SESSION_TTL_SECONDS, **cookie_kwargs(),
    )
    log.info("admin %s START impersonating %s", admin_email, target)
    return resp


@app.get("/impersonate/stop")
async def impersonate_stop(request: Request) -> Response:
    """Leave impersonation mode and restore the real admin session. GET so the
    floating badge can just navigate here. No-op (→ home) if not impersonating."""
    payload = _session_payload(request)
    resp = RedirectResponse("/admin")
    if payload and payload.get("imp"):
        # base fields in the cookie are still the real admin — restore them.
        resp = RedirectResponse("/admin")
        resp.set_cookie(
            SESSION_COOKIE,
            make_session_cookie({
                "email": payload.get("email", ""),
                "name": payload.get("name", ""),
                "picture": payload.get("picture", ""),
                "sub": payload.get("sub", ""),
            }),
            max_age=SESSION_TTL_SECONDS, **cookie_kwargs(),
        )
        log.info("admin %s STOP impersonating %s",
                 str(payload.get("imp_by", "")), str(payload.get("imp", "")))
    return resp


def _admin_shell(title: str, body_html: str) -> str:
    """Minimal admin sub-page wrapper (header + Pit Wall CSS)."""
    return (
        "<!doctype html><html lang=en><head><meta charset=utf-8>"
        f"<meta name=viewport content='width=device-width,initial-scale=1'>"
        f"<title>{html.escape(title)}</title>{_FONTS_LINK}<style>{_BASE_CSS}"
        ".metrics{display:flex;flex-wrap:wrap;gap:var(--sp-md);margin:0 0 var(--sp-lg)}"
        ".metric{background:var(--surface);border:1px solid var(--line);border-radius:var(--r-md);"
        "padding:var(--sp-md) var(--sp-lg);min-width:150px}"
        ".metric .v{font:600 28px/1 var(--ff-mono);color:var(--primary)}"
        ".metric .k{color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.08em;margin-top:6px}"
        "table.rep{width:100%;border-collapse:collapse}"
        "table.rep th,table.rep td{text-align:left;padding:8px 10px;border-bottom:1px solid var(--line);font-size:13px}"
        "table.rep th{color:var(--muted);text-transform:uppercase;font-size:11px;letter-spacing:.06em}"
        "table.rep td.mono{font-family:var(--ff-mono)}"
        ".ua{max-width:520px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:var(--muted)}"
        f"</style></head><body><header class=app><span class=dot></span>"
        f"<h1>racecar-35 \u00b7 admin</h1><span class=crumbs>&rsaquo; "
        f"<a href='/admin'>admin</a> &rsaquo; {html.escape(title)}</span>"
        f"<span style='flex:1'></span><a class=btn href='/admin'>back to admin</a></header>"
        f"<main>{body_html}</main></body></html>"
    )


@app.get("/admin/user/{email}/logins")
async def admin_user_logins(request: Request, email: str) -> JSONResponse:
    """JSON login history for one user (admin only)."""
    require_admin(request)
    return JSONResponse({"email": email.lower(), "logins": load_login_log(email.lower())})


@app.get("/admin/user/{email}/history", response_class=HTMLResponse)
async def admin_user_history(request: Request, email: str) -> Response:
    """Per-user login history page (admin only)."""
    if not oauth_enabled():
        return HTMLResponse(_ADMIN_DISABLED_HTML, status_code=400)
    u = current_user(request)
    if not u:
        return login_redirect(request)
    if not is_admin_email(str(u.get("email", ""))):
        return HTMLResponse(_LOGIN_ERROR_HTML.replace("__ERROR__", "Admin access required."),
                            status_code=403)
    email = email.lower()
    logins = load_login_log(email)
    rows = []
    for r in logins:
        when = time.strftime("%Y-%m-%d %H:%M:%S UTC", time.gmtime(int(r.get("ts", 0))))
        rows.append(
            f"<tr><td class=mono>{when}</td>"
            f"<td class=mono>{html.escape(str(r.get('ip','')))}</td>"
            f"<td class='mono ua' title=\"{html.escape(str(r.get('ua','')))}\">"
            f"{html.escape(str(r.get('ua','')))}</td>"
            f"<td>{html.escape(str(r.get('event','login')))}</td></tr>"
        )
    if not rows:
        rows = ["<tr><td colspan=4 style='color:var(--muted);text-align:center;font-style:italic'>"
                "no logins recorded yet</td></tr>"]
    body = (
        f"<div class=toolbar><h2 class=t-display style='margin:0'>{html.escape(email)}</h2></div>"
        f"<p style='color:var(--muted)'>{len(logins)} recorded sign-in(s), newest first.</p>"
        "<div class=card><div class=card-body><table class=rep><thead><tr>"
        "<th>When</th><th>IP</th><th>User agent</th><th>Event</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></div>"
    )
    return HTMLResponse(_admin_shell("login history", body))


@app.get("/admin/report", response_class=HTMLResponse)
async def admin_report(request: Request) -> Response:
    """Aggregate user-activity report (admin only)."""
    if not oauth_enabled():
        return HTMLResponse(_ADMIN_DISABLED_HTML, status_code=400)
    u = current_user(request)
    if not u:
        return login_redirect(request)
    if not is_admin_email(str(u.get("email", ""))):
        return HTMLResponse(_LOGIN_ERROR_HTML.replace("__ERROR__", "Admin access required."),
                            status_code=403)
    stats = login_stats_all()
    now = int(time.time())
    d7, d30 = now - 7 * 86400, now - 30 * 86400
    total_users = len(stats)
    total_logins = sum(s["logins"] for s in stats)
    active7 = sum(1 for s in stats if s["last_ts"] >= d7)
    active30 = sum(1 for s in stats if s["last_ts"] >= d30)
    new30 = sum(1 for s in stats if s["first_ts"] >= d30)
    logins7 = sum(s["logins_7d"] for s in stats)
    logins30 = sum(s["logins_30d"] for s in stats)

    def metric(v, k):
        return f"<div class=metric><div class=v>{v}</div><div class=k>{k}</div></div>"
    cards = (
        metric(total_users, "tracked users") + metric(total_logins, "total sign-ins")
        + metric(active7, "active (7d)") + metric(active30, "active (30d)")
        + metric(logins7, "sign-ins (7d)") + metric(logins30, "sign-ins (30d)")
        + metric(new30, "new users (30d)")
    )
    stats.sort(key=lambda s: s["last_ts"], reverse=True)
    rows = []
    for s in stats:
        first = time.strftime("%Y-%m-%d", time.gmtime(s["first_ts"])) if s["first_ts"] else "\u2014"
        last = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(s["last_ts"])) if s["last_ts"] else "\u2014"
        em_h = html.escape(s["email"])
        rows.append(
            f"<tr><td class=mono><a href='/admin/user/{em_h}/history'>{em_h}</a></td>"
            f"<td class=mono>{s['logins']}</td><td class=mono>{s['logins_30d']}</td>"
            f"<td class=mono>{s['logins_7d']}</td><td class=mono>{s['distinct_days']}</td>"
            f"<td class=mono>{first}</td><td class=mono>{last}</td></tr>"
        )
    if not rows:
        rows = ["<tr><td colspan=7 style='color:var(--muted);text-align:center;font-style:italic'>"
                "no login activity recorded yet</td></tr>"]
    body = (
        "<div class=toolbar><h2 class=t-display style='margin:0'>User activity report</h2></div>"
        f"<div class=metrics>{cards}</div>"
        "<div class=card><div class=card-body><table class=rep><thead><tr>"
        "<th>User</th><th>Total</th><th>30d</th><th>7d</th><th>Active days</th>"
        "<th>First seen</th><th>Last seen</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table></div></div>"
    )
    return HTMLResponse(_admin_shell("activity report", body))


@app.post("/admin/users")
async def admin_upsert_user(request: Request) -> JSONResponse:
    """Add an authorized account, or change its admin flag (idempotent upsert)."""
    admin = require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    email = str(body.get("email", "")).strip().lower()
    is_admin = bool(body.get("is_admin"))
    if not email or "@" not in email or " " in email:
        raise HTTPException(status_code=422, detail="a valid email is required")
    if email in ADMIN_EMAILS:
        raise HTTPException(status_code=409, detail="that account is a bootstrap admin (set in .env) and can't be edited here")
    users = load_managed_users()
    existing = users.get(email)
    users[email] = {
        "email": email,
        "is_admin": is_admin,
        "added_by": (existing or {}).get("added_by") or str(admin.get("email", "")).lower(),
        "added_at": (existing or {}).get("added_at") or int(time.time()),
        # Preserve any existing per-user key; mint one for brand-new accounts
        # so a driver added here has a usable key before their first login.
        "api_key": (existing or {}).get("api_key") or generate_api_key(),
    }
    save_managed_users(users)
    log.info("admin %s %s user %s (admin=%s)", admin.get("email"),
             "updated" if existing else "added", email, is_admin)
    return JSONResponse({"ok": True, "email": email, "is_admin": is_admin})


@app.post("/admin/users/delete")
async def admin_delete_user(request: Request) -> JSONResponse:
    admin = require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    email = str(body.get("email", "")).strip().lower()
    if email in ADMIN_EMAILS:
        raise HTTPException(status_code=409, detail="bootstrap admins can't be removed here (edit .env)")
    users = load_managed_users()
    if email not in users:
        raise HTTPException(status_code=404, detail="no such managed account")
    del users[email]
    save_managed_users(users)
    log.info("admin %s removed user %s", admin.get("email"), email)
    return JSONResponse({"ok": True, "removed": email})


# ---------------------------------------------------------------------------
# Per-user visibility management (admin only).
#   - view_all: ALL USERS checkbox -> this account sees every user's sessions.
#   - can_view: explicit list of other accounts whose sessions it may see.
# ---------------------------------------------------------------------------
def _upsert_managed_for_visibility(email: str, admin_email: str) -> dict:
    """Fetch (or create) a managed record so visibility can be stored on it."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise HTTPException(status_code=422, detail="a valid email is required")
    if email in ADMIN_EMAILS:
        raise HTTPException(status_code=409, detail="that account is a bootstrap admin and already sees every user")
    users = load_managed_users()
    u = users.get(email)
    if u is None:
        u = {
            "email": email, "is_admin": False,
            "added_by": (admin_email or "").lower(), "added_at": int(time.time()),
            "api_key": generate_api_key(), "view_all": False, "can_view": [],
        }
        users[email] = u
    return u


@app.post("/admin/users/visibility")
async def admin_set_visibility(request: Request) -> JSONResponse:
    """Toggle the ALL USERS (view_all) flag for a managed account."""
    admin = require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    email = str(body.get("email", "")).strip().lower()
    view_all = bool(body.get("view_all"))
    users = load_managed_users()
    u = _upsert_managed_for_visibility(email, str(admin.get("email", "")))
    users[email] = u
    u["view_all"] = view_all
    save_managed_users(users)
    log.info("admin %s set view_all=%s for %s", admin.get("email"), view_all, email)
    return JSONResponse({"ok": True, "email": email, "view_all": view_all})


@app.post("/admin/users/grant")
async def admin_grant_view(request: Request) -> JSONResponse:
    """Add one account to another account's can_view list."""
    admin = require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    email = str(body.get("email", "")).strip().lower()
    target = str(body.get("target", "")).strip().lower()
    if not target or "@" not in target:
        raise HTTPException(status_code=422, detail="a valid target email is required")
    if target == email:
        raise HTTPException(status_code=422, detail="an account already sees its own sessions")
    users = load_managed_users()
    u = _upsert_managed_for_visibility(email, str(admin.get("email", "")))
    users[email] = u
    cv = set(u.get("can_view") or [])
    cv.add(target)
    u["can_view"] = sorted(cv)
    save_managed_users(users)
    log.info("admin %s granted %s view of %s", admin.get("email"), email, target)
    return JSONResponse({"ok": True, "email": email, "can_view": u["can_view"]})


@app.post("/admin/users/revoke")
async def admin_revoke_view(request: Request) -> JSONResponse:
    """Remove one account from another account's can_view list."""
    admin = require_admin(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    email = str(body.get("email", "")).strip().lower()
    target = str(body.get("target", "")).strip().lower()
    users = load_managed_users()
    u = users.get(email)
    if u is None:
        raise HTTPException(status_code=404, detail="no such managed account")
    cv = [e for e in (u.get("can_view") or []) if e != target]
    u["can_view"] = cv
    users[email] = u
    save_managed_users(users)
    log.info("admin %s revoked %s view of %s", admin.get("email"), email, target)
    return JSONResponse({"ok": True, "email": email, "can_view": cv})


@app.get("/admin/user/{email}", response_class=HTMLResponse)
async def admin_user_detail(request: Request, email: str) -> Response:
    """Per-user screen: ALL USERS toggle + dynamic can_view grant list."""
    if not oauth_enabled():
        return HTMLResponse(_ADMIN_DISABLED_HTML, status_code=400)
    viewer = current_user(request)
    if not viewer:
        return login_redirect(request)
    if not is_admin_email(str(viewer.get("email", ""))):
        return HTMLResponse(
            _LOGIN_ERROR_HTML.replace("__ERROR__", "Admin access is required for this page."),
            status_code=403,
        )
    target = (email or "").strip().lower()
    is_boot_admin = target in ADMIN_EMAILS
    u = load_managed_users().get(target)
    is_admin = is_boot_admin or bool(u and u["is_admin"])
    view_all = bool(u and u.get("view_all"))
    can_view = list(u.get("can_view")) if u else []
    sees_all = is_admin or view_all

    # All known accounts (minus the target itself) for the picker.
    others = sorted(allowed_emails() - {target})
    options = "".join(f'<option value="{html.escape(e)}">' for e in others)

    if can_view:
        chips = "".join(
            f"<li class='mono'>{html.escape(e)}"
            f"<button class='btn danger' data-revoke='{html.escape(e)}'>remove</button></li>"
            for e in sorted(can_view)
        )
    else:
        chips = "<li class='empty' style='color:var(--muted);font-style:italic'>no extra users yet</li>"

    locked_note = ""
    if is_admin:
        locked_note = ("<p class='summary' style='color:var(--muted)'>This is an "
                       "admin account &mdash; it already sees every user's sessions.</p>")

    page = (_ADMIN_USER_HTML
            .replace("__USER_CHIP__", _user_chip_html(viewer))
            .replace("__EMAIL__", html.escape(target))
            .replace("__CHECKED__", "checked" if sees_all else "")
            .replace("__DISABLED__", "disabled" if is_admin else "")
            .replace("__GRANT_DISABLED__", "disabled" if sees_all else "")
            .replace("__OPTIONS__", options)
            .replace("__CHIPS__", chips)
            .replace("__LOCKED_NOTE__", locked_note))
    return HTMLResponse(page)


# ---------------------------------------------------------------------------
# CAN-bus captures (admin only).
#
# The dash's CAN sniffer writes CSV files (t_ms,id,ext,dlc,d0..d7) to reverse-
# engineer the MS3Pro broadcast byte layout. These endpoints let an admin
# upload one straight from the browser and inspect it: per-ID frame counts +
# rates, per-byte min/max/range, and downsampled byte time-series so you can
# eyeball which byte (or 16-bit word) tracks RPM/CLT/AFR.
# ---------------------------------------------------------------------------
def _canbus_dir() -> pathlib.Path:
    p = DATA_DIR / "canbus"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _canbus_save_name(raw_name: Optional[str]) -> str:
    raw = (raw_name or "capture").strip()
    for ext in (".csv", ".txt", ".log"):
        if raw.lower().endswith(ext):
            raw = raw[: -len(ext)]
            break
    return safe_name(raw, default="capture", maxlen=110) + ".csv"


def _resolve_can(file: str) -> pathlib.Path:
    f = re.sub(r"[^A-Za-z0-9._-]", "_", (file or "").strip()).lstrip(".")
    if not f.endswith(".csv"):
        f += ".csv"
    base = _canbus_dir().resolve()
    p = (base / f).resolve()
    if p.parent != base or not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return p


def _canbus_listing() -> list:
    out = []
    for f in _canbus_dir().iterdir():
        if not f.is_file() or not f.name.endswith(".csv"):
            continue
        st = f.stat()
        out.append({"filename": f.name, "size_bytes": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda c: c["mtime"], reverse=True)
    return out


def _canbus_rows_html() -> str:
    rows = []
    for c in _canbus_listing():
        fn = html.escape(c["filename"])
        when = time.strftime("%Y-%m-%d %H:%M", time.gmtime(c["mtime"]))
        kb = f"{c['size_bytes'] / 1024:.0f} KB"
        rows.append(
            f"<tr><td class=mono><a href='/admin/canbus/{fn}'>{fn}</a></td>"
            f"<td class=mono>{when} UTC</td><td class=mono>{kb}</td>"
            f"<td><div class='row-actions'>"
            f"<a class='btn' href='/admin/canbus/{fn}'>review</a>"
            f"<a class='btn' href='/admin/canbus/{fn}/raw'>download</a>"
            f"<button class='btn danger' data-act='del' data-file='{fn}'>delete</button>"
            f"</div></td></tr>"
        )
    if not rows:
        return ("<tr><td colspan=4 class=empty style='color:var(--muted);"
                "font-style:italic;text-align:center'>no CAN captures uploaded yet</td></tr>")
    return "\n".join(rows)


def _parse_can_id(tok: str):
    tok = (tok or "").strip()
    if not tok:
        return None
    try:
        if tok.lower().startswith("0x"):
            return int(tok, 16)
        return int(tok, 10)
    except ValueError:
        try:
            return int(tok, 16)
        except ValueError:
            return None


def _parse_can_csv(raw: bytes, max_points: int = 2000) -> dict:
    text = raw.decode("utf-8", "replace")
    ids: dict = {}
    total = 0
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if parts[0].lower() in ("t_ms", "t", "time", "timestamp"):
            continue  # header
        if len(parts) < 4:
            continue
        try:
            t_ms = int(float(parts[0]))
        except ValueError:
            continue
        idv = _parse_can_id(parts[1])
        if idv is None:
            continue
        try:
            dlc = int(parts[3])
        except ValueError:
            dlc = len(parts) - 4
        databytes = []
        for b in parts[4:12]:
            try:
                databytes.append(int(b, 16) & 0xFF)
            except ValueError:
                databytes.append(None)
        rec = ids.get(idv)
        if rec is None:
            rec = {"count": 0, "t": [], "b": [[] for _ in range(8)],
                   "dlc": dlc, "first": t_ms, "last": t_ms}
            ids[idv] = rec
        rec["count"] += 1
        rec["last"] = t_ms
        rec["t"].append(t_ms)
        for i in range(8):
            rec["b"][i].append(databytes[i] if i < len(databytes) else None)
        total += 1

    out_ids = []
    for idv, rec in ids.items():
        n = rec["count"]
        span_ms = rec["last"] - rec["first"]
        hz = round(n / (span_ms / 1000.0), 1) if span_ms > 0 else 0.0
        bytestats = []
        for i in range(8):
            vals = [v for v in rec["b"][i] if v is not None]
            if vals:
                bytestats.append({"i": i, "min": min(vals), "max": max(vals),
                                  "range": max(vals) - min(vals)})
            else:
                bytestats.append({"i": i, "min": None, "max": None, "range": 0})
        stride = max(1, n // max_points)
        out_ids.append({
            "id": idv, "id_hex": "0x%X" % idv, "count": n, "hz": hz, "dlc": rec["dlc"],
            "first_ms": rec["first"], "last_ms": rec["last"], "stride": stride,
            "bytes": bytestats,
            "t": rec["t"][::stride],
            "b": [rec["b"][i][::stride] for i in range(8)],
        })
    out_ids.sort(key=lambda r: r["count"], reverse=True)
    return {"frames": total, "n_ids": len(out_ids), "ids": out_ids}


def _require_admin_page(request: Request):
    """Admin gate that returns an HTML response (not JSON) for browser routes.
    Returns (user, None) when OK, or (None, Response) to short-circuit."""
    if not oauth_enabled():
        return None, HTMLResponse(_ADMIN_DISABLED_HTML, status_code=400)
    user = current_user(request)
    if not user:
        return None, login_redirect(request)
    if not is_admin_email(str(user.get("email", ""))):
        return None, HTMLResponse(
            _LOGIN_ERROR_HTML.replace("__ERROR__", "Admin access is required for this page."),
            status_code=403,
        )
    return user, None


@app.get("/admin/canbus", response_class=HTMLResponse)
async def canbus_page(request: Request) -> Response:
    user, resp = _require_admin_page(request)
    if resp:
        return resp
    return HTMLResponse(_CANBUS_HTML
                        .replace("__USER_CHIP__", _user_chip_html(user))
                        .replace("__ROWS__", _canbus_rows_html()))


@app.get("/admin/canbus/list")
async def canbus_list(request: Request) -> dict:
    require_admin(request)
    return {"captures": _canbus_listing()}


@app.post("/admin/canbus/upload")
async def canbus_upload(request: Request, name: str = Query("capture")) -> JSONResponse:
    require_admin(request)
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    fn = _canbus_save_name(name)
    out = _canbus_dir() / fn
    if out.exists():
        out = _canbus_dir() / f"{out.stem}_{int(time.time())}.csv"
    out.write_bytes(body)
    log.info("canbus upload %s bytes=%d -> %s", request.client.host if request.client else "?",
             len(body), out.name)
    return JSONResponse({"ok": True, "filename": out.name, "bytes": len(body)})


@app.get("/admin/canbus/{file}/data")
async def canbus_data(request: Request, file: str) -> JSONResponse:
    require_admin(request)
    p = _resolve_can(file)
    return JSONResponse(_parse_can_csv(p.read_bytes()))


@app.get("/admin/canbus/{file}/raw")
async def canbus_raw(request: Request, file: str) -> FileResponse:
    require_admin(request)
    p = _resolve_can(file)
    return FileResponse(p, media_type="text/csv", filename=p.name)


@app.post("/admin/canbus/{file}/delete")
async def canbus_delete(request: Request, file: str) -> JSONResponse:
    require_admin(request)
    p = _resolve_can(file)
    p.unlink()
    return JSONResponse({"ok": True})


@app.get("/admin/canbus/{file}", response_class=HTMLResponse)
async def canbus_review(request: Request, file: str) -> Response:
    user, resp = _require_admin_page(request)
    if resp:
        return resp
    p = _resolve_can(file)
    return HTMLResponse(_CAN_REVIEW_HTML
                        .replace("__USER_CHIP__", _user_chip_html(user))
                        .replace("__FILE__", html.escape(p.name)))


@app.get("/admin/debug/list")
async def admin_debug_list(request: Request,
                          x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    """List uploaded Teensy debug logs (firmware-key gated, so tooling/agents can
    pull them without a browser session)."""
    if not FIRMWARE_KEY or x_api_key != FIRMWARE_KEY:
        raise HTTPException(status_code=401, detail="firmware key required")
    root = DATA_DIR / "debug"
    out = []
    if root.exists():
        for ud in sorted(root.iterdir()):
            if ud.is_dir():
                for f in sorted(ud.iterdir()):
                    if f.is_file():
                        st = f.stat()
                        out.append({"user": ud.name, "file": f.name,
                                    "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return JSONResponse({"debug_files": out})


@app.get("/admin/debug/get")
async def admin_debug_get(request: Request, user: str, file: str,
                         x_api_key: Optional[str] = Header(None)) -> Response:
    """Raw contents of one uploaded debug log (firmware-key gated)."""
    if not FIRMWARE_KEY or x_api_key != FIRMWARE_KEY:
        raise HTTPException(status_code=401, detail="firmware key required")
    p = DATA_DIR / "debug" / safe_name(user) / safe_name(file, maxlen=256)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return Response(content=p.read_text("utf-8", "replace"), media_type="text/plain")


@app.get("/admin/sessions/list")
async def admin_sessions_list(request: Request,
                             x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    """List ALL session files (firmware-key gated) so tooling/agents can run
    remote forensics (e.g. test a baked S/F line against a real GPS trace)
    without a browser session. Mirrors /admin/debug/list."""
    if not FIRMWARE_KEY or x_api_key != FIRMWARE_KEY:
        raise HTTPException(status_code=401, detail="firmware key required")
    out = []
    root = DATA_DIR / "sessions"
    if root.exists():
        for ud in sorted(root.iterdir()):
            if ud.is_dir():
                for f in sorted(ud.iterdir()):
                    if f.is_file():
                        st = f.stat()
                        out.append({"user": ud.name, "file": f.name,
                                    "size": st.st_size, "mtime": int(st.st_mtime)})
    out.sort(key=lambda x: x["mtime"], reverse=True)
    return JSONResponse({"sessions": out})


@app.get("/admin/sessions/get")
async def admin_sessions_get(request: Request, user: str, file: str,
                            x_api_key: Optional[str] = Header(None)) -> Response:
    """Raw NDJSON of one session (firmware-key gated; forensics tooling)."""
    if not FIRMWARE_KEY or x_api_key != FIRMWARE_KEY:
        raise HTTPException(status_code=401, detail="firmware key required")
    p = DATA_DIR / "sessions" / safe_name(user) / safe_name(file, maxlen=256)
    if not p.exists() or not p.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(p, media_type="application/x-ndjson", filename=p.name)


@app.get("/admin/upload/log")
async def admin_upload_log(request: Request, n: int = 200,
                          x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    """Recent upload attempts (start/ok/recv_error/reject), newest last. Firmware-
    key gated so the agent can see WHY the dash's uploads fail from the server
    side even when the device can't upload its own debug log."""
    if not FIRMWARE_KEY or x_api_key != FIRMWARE_KEY:
        raise HTTPException(status_code=401, detail="firmware key required")
    out = []
    if UPLOAD_LOG.exists():
        for line in UPLOAD_LOG.read_text(errors="replace").splitlines()[-max(1, min(n, 1000)):]:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                pass
    return JSONResponse({"events": out})


@app.get("/tools/sfpicker", response_class=HTMLResponse)
async def sf_picker(request: Request) -> Response:
    """Satellite start/finish line picker (served copy of
    tools/track_sf_picker.html). Click two points across the S/F stripe +
    optionally the circuit centre; paste the generated TRACKS[] coords back
    into chat to get them baked into firmware. Login-gated like other pages."""
    if oauth_enabled() and not current_user(request):
        return login_redirect(request)
    p = pathlib.Path(__file__).parent / "sf_picker.html"
    if not p.exists():
        raise HTTPException(status_code=404, detail="picker not deployed")
    return HTMLResponse(p.read_text("utf-8"))


# ---------------------------------------------------------------------------
# AUTO-COACH CHECKLIST
# ---------------------------------------------------------------------------
# Every successful session upload kicks a BACKGROUND AI review that distils the
# session into 1-3 short, actionable checklist items. They live per-user, are
# de-duplicated against what's already open (so the same advice doesn't pile up
# session after session), and are tickable from BOTH the web and the dash.
# Checked items are kept for the web history but are NEVER sent to the dash.
#   GET  /coach/{user}/open   -> dash: open items only (compact)
#   GET  /coach/{user}        -> web JSON: open + done
#   POST /coach/{user}/done   -> {id} tick (accepts "by": display|web)
#   POST /coach/{user}/reopen -> {id} untick (web only)
# ---------------------------------------------------------------------------
COACH_DIR = DATA_DIR / "coach"
COACH_MAX_ITEMS_PER_SESSION = 3
COACH_SIM_THRESHOLD = 0.55      # >= this vs an OPEN item => treat as duplicate


def _coach_path(user: str) -> pathlib.Path:
    return COACH_DIR / (safe_name(user) + ".json")


def _coach_load(user: str) -> list:
    p = _coach_path(user)
    if p.exists():
        try:
            d = json.loads(p.read_text("utf-8"))
            return d if isinstance(d, list) else []
        except Exception:
            return []
    return []


def _coach_save(user: str, items: list) -> None:
    p = _coach_path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(items), "utf-8")
    tmp.replace(p)


_COACH_STOP = {"the", "a", "an", "to", "on", "in", "of", "and", "or", "your", "you",
               "is", "are", "for", "at", "be", "more", "less", "it", "with", "into",
               "through", "then", "but", "get", "keep", "try"}


def _coach_norm(s: str) -> list:
    toks = re.findall(r"[a-z0-9]+", (s or "").lower())
    return [t for t in toks if t not in _COACH_STOP]


def _coach_similar(a: str, b: str) -> float:
    """Similarity of two coaching lines. Jaccard on content words catches
    reworded advice ('brake later into T3' vs 'later braking for turn 3');
    SequenceMatcher catches near-identical phrasing. Take the max."""
    ta, tb = set(_coach_norm(a)), set(_coach_norm(b))
    if not ta or not tb:
        return 0.0
    jac = len(ta & tb) / float(len(ta | tb))
    seq = difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(jac, seq)


def _coach_add(user: str, texts: list, session: str, track: str) -> list:
    """Append new items, skipping anything similar to an already-OPEN item.
    Ticked items are ignored for dedupe on purpose: if the driver ticked it off
    and the habit came back, it SHOULD be raised again."""
    items = _coach_load(user)
    open_texts = [i.get("text", "") for i in items if not i.get("done")]
    added = []
    for t in texts[:COACH_MAX_ITEMS_PER_SESSION]:
        t = re.sub(r"\s+", " ", (t or "").strip())
        if len(t) < 6:
            continue
        if any(_coach_similar(t, o) >= COACH_SIM_THRESHOLD for o in open_texts):
            continue
        it = {"id": secrets.token_hex(6), "ts": int(time.time()), "text": t[:180],
              "session": session, "track": track,
              "done": False, "done_ts": None, "done_by": None}
        items.append(it)
        open_texts.append(t)
        added.append(it)
    if added:
        _coach_save(user, items)
    return added


def _coach_prefs_path(user: str) -> pathlib.Path:
    return COACH_DIR / (safe_name(user) + ".prefs.json")


def _coach_prefs(user: str) -> dict:
    """Per-user coach settings. auto=True => review every upload automatically.
    Default ON (that's the feature people expect); turn it off to keep the AI
    cost/latency on a manual, per-session click instead."""
    p = _coach_prefs_path(user)
    out = {"auto": True, "tz": ""}
    if p.exists():
        try:
            d = json.loads(p.read_text("utf-8"))
            if isinstance(d, dict):
                out["auto"] = bool(d.get("auto", True))
                out["tz"] = str(d.get("tz", ""))[:64]
        except Exception:
            pass
    return out


def _coach_set_prefs(user: str, auto: bool, tz: str = "") -> dict:
    p = _coach_prefs_path(user)
    p.parent.mkdir(parents=True, exist_ok=True)
    d = {"auto": bool(auto), "tz": str(tz or "")[:64]}
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(d), "utf-8")
    tmp.replace(p)
    return d


def _coach_has_for_session(user: str, filename: str) -> bool:
    """Has this session already been reviewed? (Any item, ticked or not, that
    came from it.) Used to keep the manual 'generate' button idempotent."""
    return any(i.get("session") == filename for i in _coach_load(user))


def _corner_table(samples: list, laps: list) -> list:
    """Corners of the BEST lap, numbered from S/F, with direction. Heading is
    computed from consecutive moving fixes; a corner = contiguous span where
    the cumulative heading change exceeds 30 deg. Geographic headings increase
    clockwise, so positive delta = RIGHT."""
    if not laps:
        return []
    rel, _ = _relative_seconds(samples)
    best = min(laps, key=lambda l: l["seconds"])
    idx = [i for i, sm in enumerate(samples)
           if best["t_start"] <= rel[i] <= best["t_end"]
           and isinstance(sm.get("lat"), (int, float))
           and isinstance(sm.get("speed_mph"), (int, float))
           and sm.get("speed_mph", 0) > 15]
    if len(idx) < 50:
        return []
    import math as _m
    hd = []
    for a, b in zip(idx, idx[1:]):
        sa, sb = samples[a], samples[b]
        dE = (sb["lon"] - sa["lon"]) * _m.cos(_m.radians(sa["lat"]))
        dN = sb["lat"] - sa["lat"]
        hd.append((_m.degrees(_m.atan2(dE, dN)), b))
    out = []
    acc = 0.0; start_i = None; min_mph = 1e9; n = 0
    for k in range(1, len(hd)):
        d = hd[k][0] - hd[k - 1][0]
        if d > 180: d -= 360
        if d < -180: d += 360
        if abs(d) > 1.0 and (acc == 0 or (d > 0) == (acc > 0)):
            if start_i is None: start_i = hd[k][1]; min_mph = 1e9
            acc += d
            min_mph = min(min_mph, samples[hd[k][1]].get("speed_mph", 999))
        else:
            if start_i is not None and abs(acc) >= 30:
                n += 1
                out.append(f"T{n} {'RIGHT' if acc > 0 else 'LEFT'} "
                           f"{abs(acc):.0f}deg min={min_mph:.0f}mph")
                if n >= 20: break
            acc = 0.0; start_i = None
    return out


def _coach_facts(user_dir: str, p: pathlib.Path) -> Optional[str]:
    """Compact fact sheet for the AI: lap times + consistency + the physical
    envelope. Deliberately small — this runs on every upload."""
    samples = _read_ndjson_samples(p)
    if not samples:
        return None
    info = _apply_lap_meta(_detect_laps(samples), user_dir, p.name)
    laps = info.get("laps") or []
    def nums(key):
        return [s[key] for s in samples
                if isinstance(s.get(key), (int, float))]
    spd = nums("speed_mph")
    ay = [abs(v) for v in nums("ay")]
    ax = [abs(v) for v in nums("ax")]
    rpm = nums("rpm")
    out = []
    if laps:
        ts = sorted(l["seconds"] for l in laps)
        best = ts[0]
        med = ts[len(ts) // 2]
        out.append(f"laps={len(laps)} best={best:.2f}s median={med:.2f}s "
                   f"worst={ts[-1]:.2f}s spread={ts[-1]-best:.2f}s")
        out.append("lap_times_s=" + ",".join(f"{l['seconds']:.2f}" for l in laps[:25]))
    else:
        out.append("laps=0 (no start/finish crossings detected)")
    if spd:
        out.append(f"speed_mph max={max(spd):.0f} min_moving="
                   f"{min([v for v in spd if v > 5] or [0]):.0f}")
    if ay:
        out.append(f"peak_lateral_g={max(ay):.2f}")
    if ax:
        out.append(f"peak_long_g={max(ax):.2f}")
    if rpm:
        out.append(f"max_rpm={int(max(rpm))}")
    out.append(f"samples={len(samples)}")
    corners = _corner_table(samples, laps)
    if corners:
        out.append("CORNERS of the best lap, numbered FROM START/FINISH "
                   "(T1 = first corner after S/F), direction included:")
        out.extend(corners)
    return "\n".join(out)


def _coach_analyze(user_dir: str, p: pathlib.Path, track: str,
                   force: bool = False) -> None:
    """Background worker: review one freshly-uploaded session and file 1-3
    checklist items. Never raises into the request path."""
    try:
        if not force and _coach_has_for_session(user_dir, p.name):
            return                      # already reviewed (e.g. w then resumed a)
        facts = _coach_facts(user_dir, p)
        if not facts:
            return
        system = (
            "You are a professional race engineer reviewing a driver's track "
            "session. Reply with ONLY 1 to 3 lines. Each line: '- ' then ONE "
            "specific, actionable instruction the driver can act on next "
            "session, at most 16 words, no explanation, no numbering, no "
            "preamble. Prefer the biggest time gain. When referring to a "
            "corner you MUST use the names from the CORNERS table exactly, "
            "e.g. 'T4 (left)' - T1 is the first corner after start/finish; "
            "never say vague things like 'near-stop point'. If the data is "
            "too thin, reply '- Not enough clean lap data to coach from'."
        )
        user_msg = f"Track: {track or 'unknown'}\n{facts}"
        answer, model, usage = _ai_chat(
            [{"role": "system", "content": system},
             {"role": "user", "content": user_msg}])
        texts = []
        for ln in (answer or "").splitlines():
            ln = ln.strip()
            m = re.match(r"^[-*\u2022]\s*(.+)$", ln) or re.match(r"^\d+[.)]\s*(.+)$", ln)
            if m:
                texts.append(m.group(1).strip())
        if not texts:
            # model ignored the format — take the first non-empty sentence
            first = next((l.strip() for l in (answer or "").splitlines() if l.strip()), "")
            if first:
                texts = [first]
        added = _coach_add(user_dir, texts, p.name, track or "")
        log.info("coach: %s %s -> %d new item(s) (model=%s)",
                 user_dir, p.name, len(added), model)
    except Exception as e:
        log.warning("coach analyze failed for %s/%s: %s", user_dir, p.name, e)


def _coach_kick(user_dir: str, p: pathlib.Path, track: str) -> None:
    """Fire the review on a daemon thread so the dash's upload response is not
    delayed by a 10-60 s model call. Respects the per-user auto setting — when
    off, nothing happens on upload and the driver generates it by hand from the
    review page instead."""
    if not ai_enabled():
        return
    if not _coach_prefs(user_dir).get("auto", True):
        log.info("coach: auto-review OFF for %s — skipping %s", user_dir, p.name)
        return
    try:
        threading.Thread(target=_coach_analyze, args=(user_dir, p, track),
                         daemon=True).start()
    except Exception as e:
        log.warning("coach thread spawn failed: %s", e)


def _coach_gate(request: Request, user: str, x_api_key: Optional[str]) -> str:
    """Who may read/modify a user's checklist: the firmware key, that user's own
    account key, or a logged-in web user allowed to view them."""
    dirname = safe_name(user)
    if FIRMWARE_KEY and x_api_key == FIRMWARE_KEY:
        return dirname
    if x_api_key:
        owner = email_for_api_key(x_api_key)
        if owner and safe_name(owner) == dirname:
            return dirname
    if oauth_enabled():
        u = current_user(request)
        if u and can_view_dir(str(u.get("email", "")), dirname):
            return dirname
        raise HTTPException(status_code=403, detail="not allowed")
    return dirname   # dev mode


@app.get("/coach/{user}/open")
async def coach_open(request: Request, user: str,
                     x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    """Open (unticked) items only — what the DASH shows. Checked items are
    never returned here, by design."""
    d = _coach_gate(request, user, x_api_key)
    items = [i for i in _coach_load(d) if not i.get("done")]
    items.sort(key=lambda i: i.get("ts", 0), reverse=True)
    return JSONResponse({"ok": True, "count": len(items),
                         "items": [{"id": i["id"], "text": i["text"],
                                    "track": i.get("track", ""), "ts": i.get("ts", 0)}
                                   for i in items[:12]]})


@app.get("/coach/{user}")
async def coach_all(request: Request, user: str,
                    x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    d = _coach_gate(request, user, x_api_key)
    items = _coach_load(d)
    items.sort(key=lambda i: (bool(i.get("done")), -i.get("ts", 0)))
    return JSONResponse({"ok": True, "items": items, "prefs": _coach_prefs(d)})


@app.post("/coach/{user}/prefs")
async def coach_set_prefs(request: Request, user: str,
                          x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    """Toggle automatic review-on-upload for this user. Body {auto: bool}."""
    d = _coach_gate(request, user, x_api_key)
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    cur = _coach_prefs(d)
    return JSONResponse({"ok": True, "prefs": _coach_set_prefs(
        d, bool(body.get("auto", cur["auto"])), str(body.get("tz", cur["tz"])))})


@app.post("/sessions/{user}/{filename}/coach")
async def session_coach_generate(request: Request, user: str, filename: str) -> JSONResponse:
    """Manually run the coach review for ONE session — the path used when
    auto-review is off (or when an upload predates the feature). Idempotent:
    refuses if this session already produced items, unless ?force=1."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    if not ai_enabled():
        raise HTTPException(status_code=503,
                            detail="AI is not configured (set RACECAR_AI_API_KEY)")
    p = _resolve_session(user, filename)
    d = safe_name(user)
    force = str(request.query_params.get("force", "")).strip() in ("1", "true", "yes")
    if not force and _coach_has_for_session(d, p.name):
        return JSONResponse({"ok": True, "already": True, "added": 0,
                            "detail": "this session has already been reviewed"})
    before = len(_coach_load(d))
    _coach_analyze(d, p, _track_key(p.name), force=True)   # synchronous + explicit
    items = _coach_load(d)
    added = [i for i in items if i.get("session") == p.name]
    return JSONResponse({"ok": True, "already": False,
                         "added": max(0, len(items) - before),
                         "items": [{"id": i["id"], "text": i["text"], "done": i.get("done", False)}
                                   for i in added]})


@app.post("/coach/{user}/done")
async def coach_done(request: Request, user: str,
                     x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    d = _coach_gate(request, user, x_api_key)
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    iid = str(body.get("id", "")).strip()
    by = "display" if str(body.get("by", "")).lower().startswith("disp") else "web"
    if not iid:
        raise HTTPException(status_code=400, detail="id required")
    items = _coach_load(d)
    hit = False
    for i in items:
        if i.get("id") == iid and not i.get("done"):
            i["done"] = True
            i["done_ts"] = int(time.time())
            i["done_by"] = by
            hit = True
    if hit:
        _coach_save(d, items)
    open_n = sum(1 for i in items if not i.get("done"))
    return JSONResponse({"ok": True, "changed": hit, "open": open_n})


@app.post("/coach/{user}/reopen")
async def coach_reopen(request: Request, user: str,
                       x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    d = _coach_gate(request, user, x_api_key)
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    iid = str(body.get("id", "")).strip()
    items = _coach_load(d)
    for i in items:
        if i.get("id") == iid:
            i["done"] = False
            i["done_ts"] = None
            i["done_by"] = None
    _coach_save(d, items)
    return JSONResponse({"ok": True})


# ---------------------------------------------------------------------------
# ADMIN: one-click server update
# ---------------------------------------------------------------------------
# ⚠️ The app runs INSIDE the container, so it cannot rebuild itself: no docker
# socket, no git checkout, and `compose up --build` would kill the process
# serving the request. Mounting docker.sock would also be root-equivalent on
# the host. So this endpoint only WRITES A REQUEST; a tiny host-side watcher
# (server/host_updater.sh, run from cron/systemd) performs the real command:
#     git pull && docker compose -f docker-compose.prod.yml up -d --build
# and writes progress back to update_status.json in the same volume.
UPDATE_REQ  = DATA_DIR / "update_request.json"
UPDATE_STAT = DATA_DIR / "update_status.json"

# Copy-pasteable repair hints for the admin UI. The container cannot run these
# itself (no docker socket / no git checkout) — they run on the SERVER HOST.
# Override the checkout path with RACECAR_HOST_REPO if yours differs.
_HOST_REPO = os.environ.get("RACECAR_HOST_REPO", "/docker/racecar.api.blueuc.com").strip()
HOST_UPDATE_HINT_NOW = f"cd {_HOST_REPO} && sudo ./server/host_updater.sh --now"
HOST_UPDATE_HINT_INSTALL = (
    f"cd {_HOST_REPO} && sudo ./server/host_updater.sh --install"
    "   # one-time: installs the systemd watcher so the button works"
)


@app.post("/admin/update")
async def admin_update(request: Request) -> JSONResponse:
    """Queue a server update for the host watcher to execute."""
    require_admin(request)
    u = current_user(request) if oauth_enabled() else None
    req = {"ts": int(time.time()),
           "by": str((u or {}).get("email", "dev")),
           "id": secrets.token_hex(6)}
    UPDATE_REQ.parent.mkdir(parents=True, exist_ok=True)
    tmp = UPDATE_REQ.with_suffix(".tmp")
    tmp.write_text(json.dumps(req), "utf-8")
    tmp.replace(UPDATE_REQ)
    log.info("admin update requested by %s (id=%s)", req["by"], req["id"])
    return JSONResponse({"ok": True, "queued": True, "request": req,
                         "note": "host watcher will run git pull + compose up -d --build"})


@app.get("/admin/update/status")
async def admin_update_status(request: Request) -> JSONResponse:
    """Progress written by the host watcher, plus whether a request is pending.
    After a successful rebuild this process is NEW, so `running_since` moving is
    itself proof the update landed.

    Also reports WHETHER THE WATCHER HAS EVER ANSWERED (watcher_ever) and the
    ages of the files, because 'queued… waiting for host watcher' forever is
    almost always the watcher never having been installed on the host — a
    silent failure the UI used to hide."""
    require_admin(request)
    st = {}
    if UPDATE_STAT.exists():
        try:
            st = json.loads(UPDATE_STAT.read_text("utf-8"))
        except Exception:
            st = {"state": "unreadable"}
    pending = None
    if UPDATE_REQ.exists():
        try:
            pending = json.loads(UPDATE_REQ.read_text("utf-8"))
        except Exception:
            pending = {"state": "unreadable"}
    now = int(time.time())

    def _age(p: pathlib.Path):
        try:
            return max(0, now - int(p.stat().st_mtime))
        except Exception:
            return None

    return JSONResponse({
        "ok": True, "status": st, "pending": pending,
        "running_since": _PROC_START, "now": now,
        "status_age_s": _age(UPDATE_STAT),
        "pending_age_s": _age(UPDATE_REQ),
        "watcher_ever": UPDATE_STAT.exists(),
        "hint_now": HOST_UPDATE_HINT_NOW,
        "hint_install": HOST_UPDATE_HINT_INSTALL,
    })


_KNOWN_TRACKS: list = []

@app.get("/tracks")
async def known_tracks(request: Request) -> JSONResponse:
    """Known track names for the rename dropdown — parsed once from the
    S/F picker's embedded firmware TRACKS[] (kept current by the release
    process), primary entries only."""
    global _KNOWN_TRACKS
    if not _KNOWN_TRACKS:
        try:
            import re as _re
            html_src = (pathlib.Path(__file__).parent / "sf_picker.html").read_text("utf-8")
            m = _re.search(r"const TRACKS = (\[.*?\]);", html_src, _re.S)
            if m:
                _KNOWN_TRACKS = sorted(t["name"] for t in json.loads(m.group(1))
                                       if not t.get("aux"))
        except Exception as e:
            log.warning("tracks parse failed: %s", e)
    return JSONResponse({"ok": True, "tracks": _KNOWN_TRACKS})


# ---------------------------------------------------------------------------
# Track geometry pre-render (app/trackprep.py)
#
# The 3D drive view can draw a synthetic constant-width ribbon from our own GPS
# alone. This is the upgrade: for a track we HAVE data for, bake what the world
# knows about it — the real width (measured from satellite imagery, or the
# OpenStreetMap width tag when a circuit is surveyed), real terrain (AWS DEM),
# and the satellite imagery itself as a ground texture — into one cached asset
# under DATA_DIR/tracks/.
#
# Assets are per TRACK (not per session), so preparing is a once-per-track cost
# and every later session on that track gets the better render for free.
# ---------------------------------------------------------------------------
TRACKS_DIR = DATA_DIR / "tracks"
PREP_VERSION = 2          # bump to invalidate every prepared asset (see below)
_PREP: dict = {}                      # slug -> {state, log[], started, ...}
_PREP_LOCK = threading.Lock()
_PREP_KEEP = 40                       # log lines kept per run


def _trackprep():
    """Import the pre-render module. It needs Pillow + numpy; callers turn the
    ImportError into a real message instead of a 500."""
    import importlib
    if __package__:
        return importlib.import_module(".trackprep", __package__)
    return importlib.import_module("trackprep")


def _seed_tracks() -> int:
    """Copy any shipped seed track into DATA_DIR/tracks (never overwriting one
    baked locally). Seeds are tracks we have already measured off real imagery,
    so the 3D view is worth looking at before anyone clicks 'prepare track'."""
    seed_dir = pathlib.Path(__file__).parent / "seed-tracks"
    if not seed_dir.is_dir():
        return 0
    try:
        TRACKS_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as e:                      # read-only data dir: not fatal
        log.warning("cannot seed tracks into %s: %s", TRACKS_DIR, e)
        return 0
    n = 0
    for src in sorted(seed_dir.glob("*.json")):
        dst = TRACKS_DIR / src.name
        if dst.exists():
            # Replace a STALE asset (an older prep_version, i.e. baked by a
            # method we know was wrong) with the shipped one, but never trample
            # a current locally-baked asset.
            try:
                have = json.loads(dst.read_text("utf-8"))
                if int(have.get("prep_version") or 0) >= PREP_VERSION:
                    continue
                log.info("replacing stale seeded track %s (prep_version %s)",
                         dst.name, have.get("prep_version"))
            except Exception:
                continue
        try:
            shutil.copy2(src, dst)
            tex = src.with_suffix(".jpg")
            if tex.is_file():
                shutil.copy2(tex, TRACKS_DIR / tex.name)
            n += 1
        except OSError as e:
            log.warning("seed track %s failed: %s", src.name, e)
    if n:
        log.info("seeded %d prepared track(s) into %s", n, TRACKS_DIR)
    return n


_seed_tracks()


def _track_asset_path(slug: str) -> pathlib.Path:
    return TRACKS_DIR / (safe_name(slug, default="") + ".json")


def _track_slug(track: str) -> str:
    try:
        return _trackprep().slugify(track)
    except Exception:
        return re.sub(r"[^a-z0-9]+", "-", (track or "").lower()).strip("-") or "track"


def _available_slugs() -> list:
    try:
        return sorted(f.stem for f in TRACKS_DIR.glob("*.json"))
    except OSError:
        return []


def _track_asset_for(track: str) -> Optional[dict]:
    """The prepared asset for a track name, if one exists.

    Track names come from session filenames and are typed by hand, so a session
    called "Summit Point Main" must still find a track prepared as "Summit
    Point". Resolution order:
      1. exact slug
      2. the slug with a trailing config/variant word dropped/changed
         (main, circuit, full, course, raceway...)
      3. the longest prepared slug that is a prefix of this one, or vice versa
         ("summit-point-main" -> "summit-point"), which is how one prepared
         circuit covers its sub-configs.
    """
    slug = _track_slug(track)
    p = _track_asset_path(slug)
    chosen = slug if p.is_file() else None
    if chosen is None:
        words = [w for w in slug.split("-") if w]
        drop = {"main", "circuit", "full", "course", "raceway", "track", "long"}
        for cut in range(len(words), 0, -1):
            cand = "-".join(words[:cut])
            if words[cut - 1] in drop or cut < len(words):
                if _track_asset_path(cand).is_file():
                    chosen = cand
                    break
        if chosen is None:
            # longest common prefix with an existing asset, either direction
            best = None
            for have in _available_slugs():
                if have == slug:
                    best = have
                    break
                if slug.startswith(have + "-") or have.startswith(slug + "-"):
                    if best is None or len(have) > len(best):
                        best = have
            chosen = best
    if not chosen:
        return None
    try:
        asset = json.loads(_track_asset_path(chosen).read_text("utf-8"))
        asset["slug"] = chosen
        if int(asset.get("prep_version") or 0) < PREP_VERSION:
            # Built by an older, wrong method (e.g. from a whole multi-lap
            # session, where seven laps superimposed read as a 31 km "circuit").
            # Treated as missing so the page re-prepares it.
            log.info("track asset %s is prep_version %s < %s: re-prepare",
                     chosen, asset.get("prep_version"), PREP_VERSION)
            return None
        if chosen != slug:
            log.info("track %r resolved to prepared asset %r", track, chosen)
        return asset
    except Exception as e:
        log.warning("track asset %s unreadable: %s", chosen, e)
        return None


def _session_lap_centreline(p: pathlib.Path, target: int = 4000):
    """The GPS line of ONE lap — the fastest logged lap when we can find it.

    Using the whole session is wrong: seven laps of a circuit are seven
    superimposed traces, so the "track" came out 31.8 km long on a 5.5 km course,
    the measured width averaged across all of them (8.5 m), and the imagery
    mosaic was smeared over the union of every line ever driven. One lap IS the
    circuit.
    """
    samples = _read_ndjson_samples(p)
    if len(samples) < 10:
        raise ValueError("session has no usable GPS fixes")
    rel, _basis = _relative_seconds(samples)
    win = None
    try:
        laps = _detect_laps(samples).get("laps") or []
        good = [lp for lp in laps if float(lp.get("seconds") or 0) > 20]
        if good:
            best = min(good, key=lambda lp: float(lp["seconds"]))
            win = (float(best["t_start"]), float(best["t_end"]))
    except Exception as e:
        log.warning("lap detection failed for %s: %s", p.name, e)
    rows = []
    for i, s in enumerate(samples):
        lat, lon = s.get("lat"), s.get("lon")
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and (lat or lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            continue
        if win and not (win[0] <= rel[i] <= win[1]):
            continue
        rows.append((float(lat), float(lon)))
    if len(rows) < 20:                       # no usable lap window: take everything
        rows = [(float(s["lat"]), float(s["lon"])) for s in samples
                if isinstance(s.get("lat"), (int, float))
                and isinstance(s.get("lon"), (int, float)) and (s.get("lat") or s.get("lon"))]
    if len(rows) < 10:
        raise ValueError("no usable GPS line in this session")
    step = max(1, len(rows) // target)
    return rows[::step]


def _prep_log(slug: str, msg: str) -> None:
    with _PREP_LOCK:
        st = _PREP.setdefault(slug, {})
        lg = st.setdefault("log", [])
        lg.append(msg)
        del lg[:-_PREP_KEEP]
    log.info("[trackprep %s] %s", slug, msg)


def _prep_run(slug: str, track: str, params: dict) -> None:
    """Background worker: build one track asset. Never raises out."""
    with _PREP_LOCK:
        _PREP.setdefault(slug, {})["state"] = "running"
    try:
        tp = _trackprep()
    except Exception as e:
        with _PREP_LOCK:
            _PREP[slug].update(state="failed", error=f"trackprep unavailable: {e}")
        return
    try:
        points, source, osm_id, osm_w = params["points"], params["source"], \
            params.get("osm_id"), params.get("osm_width_m")
        if len(points) < 20:
            raise ValueError("not enough GPS fixes to trace the track")
        asset = tp.build_asset(
            track, points, DATA_DIR,
            {"zoom": params.get("zoom", 18), "line_source": source,
             "osm_id": osm_id, "osm_width_m": osm_w,
             "prep_version": PREP_VERSION},
            log=lambda m: _prep_log(slug, m),
        )
        with _PREP_LOCK:
            _PREP[slug].update(state="done", finished=int(time.time()),
                               summary={"track": asset["track"], "stations": len(asset["line"]),
                                        "width_m": asset.get("width_osm_m") or asset.get("width_imagery_m"),
                                        "width_source": asset.get("width_source"),
                                        "has_texture": bool(asset.get("texture"))})
    except Exception as e:
        with _PREP_LOCK:
            _PREP[slug].update(state="failed", error=str(e)[:300],
                               finished=int(time.time()))


@app.get("/trackassets")
async def trackassets(request: Request) -> JSONResponse:
    """Tracks that have been pre-rendered, with what was learned about them."""
    require_web_user(request)
    out = []
    if TRACKS_DIR.is_dir():
        for f in sorted(TRACKS_DIR.glob("*.json")):
            try:
                a = json.loads(f.read_text("utf-8"))
            except Exception:
                continue
            out.append({"slug": f.stem, "track": a.get("track"),
                        "generated": a.get("generated"),
                        "width_m": a.get("width_osm_m") or a.get("width_imagery_m"),
                        "width_source": a.get("width_source"),
                        "stations": len(a.get("line") or []),
                        "length_m": a.get("length_m"),
                        "has_texture": bool(a.get("texture")),
                        "source": (a.get("source") or {}).get("line")})
    return JSONResponse({"ok": True, "tracks": out})


@app.get("/trackassets/{slug}/asset")
async def trackasset(request: Request, slug: str) -> JSONResponse:
    require_web_user(request)
    p = _track_asset_path(slug)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no prepared asset for this track")
    return JSONResponse(json.loads(p.read_text("utf-8")))


@app.get("/trackassets/{slug}/texture.jpg")
async def trackasset_texture(request: Request, slug: str) -> FileResponse:
    require_web_user(request)
    p = TRACKS_DIR / (safe_name(slug, default="") + ".jpg")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no texture")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/sessions/{user}/{filename}/track-asset")
async def session_track_asset(request: Request, user: str, filename: str) -> JSONResponse:
    """The prepared track asset for this session's track, or 404 with the slug it
    WOULD use (so the viewer can offer to prepare it)."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    slug = _track_slug(_track_key(p.name))
    asset = _track_asset_for(_track_key(p.name))
    if not asset:
        raise HTTPException(status_code=404, detail=json.dumps(
            {"missing": True, "track": _track_key(p.name), "slug": slug,
             "prepared": _available_slugs()[:40]}))
    return JSONResponse(asset)


@app.post("/sessions/{user}/{filename}/track-prep")
async def session_track_prep(request: Request, user: str, filename: str,
                             force: int = Query(0)) -> JSONResponse:
    """Kick off a pre-render for this session's track (owner-or-admin).

    Uses OUR driven line when the session has one (best: it is the line the car
    actually takes), and falls back to the OpenStreetMap `highway=raceway` way
    nearest the session's own coordinates when it does not."""
    require_web_user(request)
    gate_delete_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    track = _track_key(p.name)
    try:
        tp = _trackprep()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"trackprep unavailable: {e}")
    slug = _track_slug(track)
    if force:                            # re-bake even though an asset exists
        try:
            _track_asset_path(slug).unlink()
            (TRACKS_DIR / (slug + ".jpg")).unlink()
        except OSError:
            pass
    with _PREP_LOCK:
        cur = (_PREP.get(slug) or {}).get("state")
        if cur == "running":
            return JSONResponse({"ok": True, "slug": slug, "state": "running",
                                 "already": True})
        _PREP[slug] = {"state": "queued", "track": track, "started": int(time.time()),
                       "log": []}
    try:
        points = _session_lap_centreline(p)
        _prep_log(slug, "centreline from the fastest logged lap: %d points" % len(points))
        source, osm_id, osm_w = "session", None, None
        # Borrow OSM's surveyed width when we can identify which way we drove.
        # Matched by SHAPE (a session says "Thompson", OSM says "Road Course"),
        # and it is only a width: our own line stays the geometry.
        try:
            lat0 = sum(x[0] for x in points) / len(points)
            lon0 = sum(x[1] for x in points) / len(points)
            ways = tp.osm_raceways((lat0 - 0.06, lon0 - 0.08, lat0 + 0.06, lon0 + 0.08))
            way, dist = tp.osm_match_by_trace(points, ways, log=lambda m: _prep_log(slug, m))
            if way is not None:
                osm_id = way["id"]
                osm_w = way.get("width_m")
                source = "session+osm:%s" % way["id"]
                _prep_log(slug, "borrowed OSM width %s m from way %s (%.1f m from our line)"
                          % (osm_w, way["id"], dist or 0))
        except Exception as e:
            _prep_log(slug, "osm width lookup skipped: %s" % e)
    except Exception as e:
        _prep_log(slug, f"no usable session line ({e}); trying OpenStreetMap")
        try:
            samples = _read_ndjson_samples(p)
            lat = lon = None
            for s in samples:
                if isinstance(s.get("lat"), (int, float)) and (s.get("lat") or s.get("lon")):
                    lat, lon = float(s["lat"]), float(s["lon"])
                    break
            if lat is None:
                raise ValueError("session has no GPS fixes")
            bbox = (lat - 0.06, lon - 0.08, lat + 0.06, lon + 0.08)
            way = tp.osm_best(track, bbox)
            if not way:
                raise ValueError("no OSM raceway found near this session")
            points, source, osm_id, osm_w = way["points"], f"osm:{way['id']}", \
                way["id"], way.get("width_m")
        except Exception as e2:
            with _PREP_LOCK:
                _PREP[slug].update(state="failed", error=str(e2)[:300])
            raise HTTPException(status_code=422, detail=str(e2))
    threading.Thread(target=_prep_run, args=(slug, track,
                                             {"points": points, "source": source,
                                              "osm_id": osm_id, "osm_width_m": osm_w}),
                     daemon=True).start()
    return JSONResponse({"ok": True, "slug": slug, "state": "queued",
                         "line_source": source})


@app.get("/sessions/{user}/{filename}/track-prep/status")
async def session_track_prep_status(request: Request, user: str, filename: str) -> JSONResponse:
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    slug = _track_slug(_track_key(p.name))
    with _PREP_LOCK:
        st = dict(_PREP.get(slug) or {})
    if not st and _track_asset_for(_track_key(p.name)):
        st = {"state": "done", "already": True}
    return JSONResponse({"ok": True, "slug": slug, "prep": st})


@app.post("/sessions/{user}/{filename}/rename")
async def session_rename(request: Request, user: str, filename: str) -> JSONResponse:
    """Change a session's TRACK (renames the file to <sid>_<track>.ndjson and
    moves every sidecar with it: AI history, lap exclusions, video link, share
    tokens, coach items). Owner-or-admin."""
    require_web_user(request)
    gate_delete_dir(request, safe_name(user))
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    new_track = safe_name(str(body.get("track", "")).strip(), default="")
    if not new_track:
        raise HTTPException(status_code=400, detail="track required")
    p = _resolve_session(user, filename)
    d = safe_name(user)
    sid = p.name.split("_", 1)[0]
    suffix = "-combined.ndjson" if p.name.endswith("-combined.ndjson") else ".ndjson"
    new_name = f"{sid}_{new_track}{suffix}"
    if new_name == p.name:
        return JSONResponse({"ok": True, "file": p.name, "unchanged": True})
    dst = p.parent / new_name
    if dst.exists():
        raise HTTPException(status_code=409, detail=f"{new_name} already exists")
    shutil.move(str(p), str(dst))
    # sidecars — every one keyed by (user, filename)
    for fn in (_ai_history_path, _lap_meta_path, _video_meta_path):
        try:
            src = fn(d, p.name)
            if src.exists():
                dstm = fn(d, new_name)
                dstm.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(src), str(dstm))
        except Exception as e:
            log.warning("rename sidecar %s failed: %s", fn.__name__, e)
    try:   # share tokens reference the filename inside their json
        if SHARE_DIR.exists():
            for tf in SHARE_DIR.glob("*.json"):
                try:
                    td = json.loads(tf.read_text("utf-8"))
                    if td.get("user") == d and td.get("filename") == p.name:
                        td["filename"] = new_name
                        tf.write_text(json.dumps(td), "utf-8")
                except Exception:
                    pass
    except Exception as e:
        log.warning("rename shares failed: %s", e)
    try:   # coach items carry the session filename
        items = _coach_load(d)
        ch = False
        for i in items:
            if i.get("session") == p.name:
                i["session"] = new_name
                ch = True
        if ch:
            _coach_save(d, items)
    except Exception:
        pass
    log.info("renamed %s/%s -> %s", d, p.name, new_name)
    return JSONResponse({"ok": True, "file": new_name})


@app.get("/coach", response_class=HTMLResponse)
async def coach_page(request: Request) -> Response:
    """Driver checklist: open items with tick boxes + a ticked-off history.
    Ticking here also removes it from the dash (the dash only ever fetches
    open items)."""
    if oauth_enabled() and not current_user(request):
        return login_redirect(request)
    u = current_user(request) if oauth_enabled() else None
    email = str((u or {}).get("email", "")) or "dev"
    return HTMLResponse(_COACH_HTML.replace("__USER__", json.dumps(safe_name(email))))


_COACH_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Driver checklist — racecar-35</title>
<style>
 :root{--bg:#0E1014;--surface:#181B22;--line:#2A2F3A;--text:#E6E8EE;--muted:#8A92A3;
       --primary:#FFB020;--good:#6CD07A}
 *{box-sizing:border-box} body{margin:0;background:var(--bg);color:var(--text);
   font:15px/1.5 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:24px}
 h1{font-size:20px;margin:0 0 4px} p.sub{color:var(--muted);font-size:13px;margin:0 0 20px}
 .wrap{max-width:760px;margin:0 auto}
 .item{display:flex;gap:14px;align-items:flex-start;background:var(--surface);
   border:1px solid var(--line);border-radius:8px;padding:14px 16px;margin-bottom:10px}
 .item.done{opacity:.5}
 .cb{width:26px;height:26px;flex:0 0 auto;border:2px solid var(--primary);border-radius:6px;
   cursor:pointer;display:flex;align-items:center;justify-content:center;font-weight:700;
   color:#1A1300;background:transparent;font-size:17px;line-height:1}
 .item.done .cb{background:var(--good);border-color:var(--good)}
 .txt{flex:1} .meta{color:var(--muted);font-size:12px;margin-top:4px}
 h2{font-size:14px;color:var(--muted);text-transform:uppercase;letter-spacing:.08em;
   margin:28px 0 10px;border-bottom:1px solid var(--line);padding-bottom:6px}
 .empty{color:var(--muted);font-style:italic}
 a{color:var(--primary)}
</style></head><body><div class="wrap">
<h1>Driver checklist</h1>
<p class="sub">Written automatically by the AI review of each uploaded session.
Ticking an item here removes it from the dash — the dash only ever shows open items.
&nbsp; <a href="/">← sessions</a></p>
<div class="item" style="align-items:center">
  <div class="cb" id="autocb" title="review every upload automatically"></div>
  <div class="txt"><b>Auto-review every upload</b>
    <div class="meta" id="automsg">When off, nothing is created on upload — generate it by hand
    from a session's review page.</div></div>
</div>
<div class="item" style="align-items:center">
  <div class="txt"><b>Timezone</b>
  <div class="meta">Stored preference (pages render in your browser's local time by default).</div></div>
  <select id="tzsel" style="background:var(--bg);color:var(--text);border:1px solid var(--line);
    border-radius:6px;padding:8px">
    <option value="">(browser local)</option>
    <option>America/New_York</option><option>America/Chicago</option>
    <option>America/Denver</option><option>America/Phoenix</option>
    <option>America/Los_Angeles</option><option>UTC</option>
  </select>
</div>
<div id="open"></div>
<h2>Ticked off</h2>
<div id="done"></div>
</div>
<script>
const USER=__USER__;
const esc=s=>(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
function when(ts){ try{return new Date(ts*1000).toLocaleString();}catch(e){return '';} }
function row(i){
  const d=document.createElement('div');
  d.className='item'+(i.done?' done':'');
  d.innerHTML='<div class="cb">'+(i.done?'\\u2713':'')+'</div><div class="txt">'+esc(i.text)+
    '<div class="meta">'+esc(i.track||'')+' · '+when(i.ts)+
    (i.done?(' · ticked '+esc(i.done_by||'')+' '+when(i.done_ts)):'')+'</div></div>';
  d.querySelector('.cb').addEventListener('click', async ()=>{
    const url='/coach/'+encodeURIComponent(USER)+(i.done?'/reopen':'/done');
    await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},
                     body:JSON.stringify({id:i.id,by:'web'})});
    load();
  });
  return d;
}
let PREFS={auto:true};
const autocb=document.getElementById('autocb');
document.getElementById('tzsel').addEventListener('change', async (e)=>{
  await fetch('/coach/'+encodeURIComponent(USER)+'/prefs',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({tz:e.target.value})});
});
autocb.addEventListener('click', async ()=>{
  PREFS.auto=!PREFS.auto;
  await fetch('/coach/'+encodeURIComponent(USER)+'/prefs',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify({auto:PREFS.auto})});
  load();
});
async function load(){
  const r=await fetch('/coach/'+encodeURIComponent(USER));
  const j=await r.json();
  PREFS=j.prefs||{auto:true};
  try{ document.getElementById('tzsel').value = PREFS.tz||''; }catch(e){}
  autocb.textContent = PREFS.auto ? '\\u2713' : '';
  autocb.style.background = PREFS.auto ? 'var(--good)' : 'transparent';
  autocb.style.borderColor = PREFS.auto ? 'var(--good)' : 'var(--primary)';
  const o=document.getElementById('open'), dn=document.getElementById('done');
  o.innerHTML=''; dn.innerHTML='';
  const items=j.items||[];
  const op=items.filter(i=>!i.done), df=items.filter(i=>i.done);
  if(!op.length) o.innerHTML='<div class="empty">Nothing outstanding — upload a session and the AI will add items here.</div>';
  op.forEach(i=>o.appendChild(row(i)));
  if(!df.length) dn.innerHTML='<div class="empty">none yet</div>';
  df.forEach(i=>dn.appendChild(row(i)));
}
load();
</script></body></html>"""


@app.get("/caps")
async def caps() -> dict:
    """Server capability probe for the dash (public, static). The dash asks
    once per boot before its first upload; 'zblocks' advertises that /upload,
    /stream and /nettest decode the compressed body framing (v0.1.127).
    An old dash never asks; an old server 404s and the dash sends raw.

    'track3d' is a WEB-side marker, not a dash one: it tells you at a glance
    whether the running image is new enough to serve the 3D drive view
    (`curl <host>/caps`). Handy because the host watcher rebuilds in place and
    the browser may still be showing a cached page.
    `track3d_v` bumps when the 3D view itself changes materially, so a deploy is
    verifiable: 1 = the satellite/terrain variant, 2 = the DATA-ONLY driving
    render (no imagery at all) + the map-strip lasso and the AI card cleanup,
    3 = supersampled/anti-aliased render (no log depth buffer), clean kerb quads,
    rounder markers, auto-recentring free look and the position mini-map,
    4 = PREPARED TRACKS: real width measured from satellite imagery (or the OSM
    width tag), real terrain from the DEM, and the imagery itself draped as the
    ground — see app/trackprep.py and /trackassets.
    5 = corner brake boards (5 4 3 2 1), shipped seed tracks, OSM ways matched by
    the driven line's SHAPE, and the sessions list with a best-lap column.
    6 = road colour is the DRIVER'S INPUT (green accelerating / grey neither /
    red braking, scaled by longitudinal g), a plan view with a scale bar and a
    car arrow for the whole circuit, forgiving track-name lookup, and
    auto-prepare on first view of a track.
    7 = prepared tracks done right: OSM circuits STITCHED from many ways,
    centreline from the fastest single lap (not the whole session),
    self-calibrating imagery width (median+MAD, clamped to 8-15 m with the raw
    value kept), ground imagery sharing the road's uv frame, and a
    PREP_VERSION that invalidates wrongly-built assets.
    8 = a bake that would produce wallpaper now FAILS instead of publishing:
    duplicate-tile detection (a blocked/proxied tile source answers every URL
    with the same image), an asset validator (texture must cover the track, be
    >= 512 px and finer than 6 m/px, and the line must look like a circuit not
    laps), the viewer clamps the texture so it can never tile, anisotropy for
    crisp ground, and seeds replace a STALE asset (so a broken baked track is
    healed by the shipped one)."""
    return {"ok": True, "zblocks": True, "coach": True, "track3d": True,
            "track3d_v": 8}



def _zb_decode(data: bytes) -> bytes:
    """Decode a zblocks body: frames of ['Z','B', u32le raw_len, u32le comp_len,
    raw-deflate bytes] — each frame an independent ≤32 KB deflate stream
    (fixed-Huffman or stored, from the dash's zdeflate.h). Raises ValueError
    on any malformed frame so the caller can 400 with a useful reason."""
    out = bytearray()
    off = 0
    n = len(data)
    while off < n:
        if data[off:off + 2] != b"ZB" or off + 10 > n:
            raise ValueError(f"bad frame header at {off}")
        rl, cl = struct.unpack_from("<II", data, off + 2)
        if rl == 0 or rl > (1 << 20) or cl == 0 or cl > (1 << 20) or off + 10 + cl > n:
            raise ValueError(f"bad frame lengths at {off} (raw={rl} comp={cl})")
        d = zlib.decompressobj(-15)
        raw = d.decompress(data[off + 10:off + 10 + cl]) + d.flush()
        if len(raw) != rl:
            raise ValueError(f"frame at {off} inflated to {len(raw)}, expected {rl}")
        out += raw
        off += 10 + cl
    return bytes(out)


@app.post("/nettest")
async def nettest(
    request: Request,
    x_rssi: Optional[str] = Header(None),
    x_fw: Optional[str] = Header(None),
    x_note: Optional[str] = Header(None),
    x_tls: Optional[str] = Header(None),
) -> JSONResponse:
    """Raw throughput probe for the dash's 'WIFI SPEED TEST' (Tools page).
    Reads and DISCARDS the body, returns bytes + elapsed so the dash can
    display real end-to-end throughput with zero UART/session involvement —
    the discriminator between 'transfer code broken' and 'dash RF starved'.
    Unauthenticated (stores nothing but a log line). Every run is RECORDED in
    the upload event log (ev=nettest, with the dash's RSSI + fw version) so
    results can be reviewed later via GET /admin/upload/log."""
    client_host = request.client.host if request.client else "?"
    t0 = time.time()
    n = 0
    # zblocks pass (v0.1.127): stream-parse the frame headers to count RAW
    # bytes without buffering or inflating — the point is the ratio + the
    # effective raw throughput of the compressed upload path.
    zb = (request.headers.get("x-body-format") or "").strip().lower() == "zblocks"
    raw_n = 0
    _hdr = b""
    _skip = 0
    try:
        async for chunk in request.stream():
            n += len(chunk)
            if zb:
                mv = memoryview(chunk)
                while len(mv):
                    if _skip:
                        t = min(_skip, len(mv))
                        _skip -= t
                        mv = mv[t:]
                        continue
                    t = min(10 - len(_hdr), len(mv))
                    _hdr += bytes(mv[:t])
                    mv = mv[t:]
                    if len(_hdr) == 10:
                        if _hdr[:2] != b"ZB":       # not actually zblocks — stop parsing
                            zb = False
                            raw_n = 0
                            break
                        rl, cl = struct.unpack("<II", _hdr[2:])
                        raw_n += rl
                        _skip = cl
                        _hdr = b""
        err = ""
    except Exception as e:   # client vanished mid-test — log what we got
        err = f"{type(e).__name__}"
    dt = max(0.001, time.time() - t0)
    kbps = round(n / dt / 1024.0, 1)
    raw_kbps = round(raw_n / dt / 1024.0, 1) if raw_n else 0.0
    ratio = round(raw_n / n, 2) if (raw_n and n) else 0.0
    _upload_event({"ev": "nettest", "ip": client_host, "bytes": n,
                   "seconds": round(dt, 3), "kbps": kbps,
                   "raw_bytes": raw_n, "raw_kbps": raw_kbps, "ratio": ratio,
                   "rssi": (x_rssi or ""), "fw": (x_fw or ""),
                   "note": (x_note or ""), "tls_ms": (x_tls or ""),
                   "err": err})
    log.info("nettest %s: %d B in %.2fs = %.1f KB/s raw=%d (%.1f KB/s eff, %.2fx) rssi=%s fw=%s %s",
             client_host, n, dt, kbps, raw_n, raw_kbps, ratio, x_rssi, x_fw, err)
    return JSONResponse({"ok": not err, "bytes": n, "seconds": round(dt, 3),
                         "kbps": kbps, "raw_bytes": raw_n,
                         "raw_kbps": raw_kbps, "ratio": ratio})


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "service": SERVICE_NAME, "data_dir": str(DATA_DIR)}


# ---------------------------------------------------------------------------
# Firmware hosting (OTA)
# ---------------------------------------------------------------------------
# Serves the OTA manifest + artifacts to the dash so updates don't depend on
# GitHub raw's CDN. That CDN ignores query-string cache-busting AND client
# no-cache headers, and serves ~5 min stale after a push — which is exactly why
# a freshly published version "isn't immediately available" on the device.
#
# Here the manifest is served no-store, so a freshly uploaded version is visible
# to the dash immediately. Binaries are content-verified by the device against
# the manifest sha256, so there's no correctness risk even if a proxy caches one.
#
#   GET  /firmware/manifest.json      public; no-store (always fresh)
#   GET  /firmware/list               public JSON: name + size + sha256
#   GET  /firmware/{file}             public; serves a .bin/.hex/.json artifact
#   POST /firmware/upload?name=<f>    X-API-Key protected; body = raw artifact
#
# Storage: RACECAR_DATA_DIR/firmware/ (persists across container rebuilds).
_FW_ALLOWED_EXT = (".bin", ".hex", ".json")
_FW_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
}


def _firmware_dir() -> pathlib.Path:
    p = DATA_DIR / "firmware"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _resolve_firmware(file: str, must_exist: bool = True) -> pathlib.Path:
    """Sanitize + resolve a firmware artifact name inside the firmware dir."""
    f = re.sub(r"[^A-Za-z0-9._-]", "_", (file or "").strip()).lstrip(".")
    if not f.lower().endswith(_FW_ALLOWED_EXT):
        raise HTTPException(status_code=400, detail="only .bin/.hex/.json allowed")
    base = _firmware_dir().resolve()
    p = (base / f).resolve()
    if p.parent != base:
        raise HTTPException(status_code=400, detail="bad name")
    if must_exist and (not p.exists() or not p.is_file()):
        raise HTTPException(status_code=404, detail="not found")
    return p


def _fw_sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


@app.get("/firmware/manifest.json")
async def firmware_manifest() -> Response:
    p = _firmware_dir() / "manifest.json"
    if not p.exists():
        raise HTTPException(status_code=404, detail="no manifest uploaded")
    return Response(content=p.read_bytes(), media_type="application/json",
                    headers=_FW_NO_STORE)


@app.get("/firmware/list")
async def firmware_list() -> JSONResponse:
    items = []
    for p in sorted(_firmware_dir().glob("*")):
        if p.is_file() and p.suffix.lower() in _FW_ALLOWED_EXT:
            items.append({"name": p.name, "size": p.stat().st_size,
                          "sha256": _fw_sha256(p)})
    return JSONResponse({"firmware": items}, headers=_FW_NO_STORE)


@app.post("/firmware/upload")
async def firmware_upload(request: Request, name: str = Query(...),
                         x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    # Gated by the DEDICATED firmware key (RACECAR_FIRMWARE_KEY), never the
    # session RACECAR_API_KEY — see the FIRMWARE_KEY comment above.
    if FIRMWARE_KEY and x_api_key != FIRMWARE_KEY:
        raise HTTPException(status_code=401, detail="invalid firmware key")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")
    p = _resolve_firmware(name, must_exist=False)
    p.write_bytes(body)
    sha = hashlib.sha256(body).hexdigest()
    log.info("firmware upload %s bytes=%d sha=%s -> %s",
             request.client.host if request.client else "?", len(body), sha, p.name)
    return JSONResponse({"ok": True, "name": p.name, "size": len(body), "sha256": sha})


@app.get("/firmware/{file}")
async def firmware_get(file: str) -> FileResponse:
    p = _resolve_firmware(file)
    return FileResponse(p, media_type="application/octet-stream",
                        filename=p.name, headers=_FW_NO_STORE)


@app.delete("/firmware/{file}")
async def firmware_delete(file: str, request: Request,
                          x_api_key: Optional[str] = Header(None)) -> JSONResponse:
    """Remove a retired artifact from the OTA store (e.g. the Basic-panel bins
    after those boards were scrapped). Gated by the FIRMWARE key, same as
    upload. Refuses to delete the manifest itself — re-upload a new one via
    POST /firmware/upload?name=manifest.json instead, so devices never see a
    404 on the manifest."""
    if FIRMWARE_KEY and x_api_key != FIRMWARE_KEY:
        raise HTTPException(status_code=401, detail="invalid firmware key")
    if file == "manifest.json":
        raise HTTPException(status_code=400, detail="replace the manifest via upload, never delete it")
    p = _resolve_firmware(file)
    p.unlink()
    log.info("firmware delete %s -> %s",
             request.client.host if request.client else "?", p.name)
    return JSONResponse({"ok": True, "deleted": p.name})


# Every upload attempt (success OR failure) appends one JSON line here so we can
# diagnose the dash's flaky uploads from the RECEIVING end — crucial because when
# an upload fails the device can't send us its own debug log either. Pullable via
# GET /admin/upload/log (firmware-key gated). Capped so it can't grow unbounded.
UPLOAD_LOG = DATA_DIR / "upload_log.jsonl"

def _upload_event(d: dict) -> None:
    try:
        rec = {"ts": round(time.time(), 3), **d}
        UPLOAD_LOG.parent.mkdir(parents=True, exist_ok=True)
        if UPLOAD_LOG.exists() and UPLOAD_LOG.stat().st_size > 512 * 1024:
            lines = UPLOAD_LOG.read_text(errors="replace").splitlines()[-400:]
            UPLOAD_LOG.write_text("\n".join(lines) + "\n")
        with open(UPLOAD_LOG, "a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass



def _resolve_upload_target(x_user_email, x_session_id, x_track_name,
                           x_api_key, web_user, kind: str):
    """Filename derivation shared by /upload, /stream, partial-salvage and
    /upload/progress — MUST stay byte-identical across them or resume breaks."""
    key_email = email_for_api_key(x_api_key) if x_api_key else None
    email = safe_name(x_user_email or (web_user or {}).get("email") or key_email)
    sid_raw = (x_session_id or "").strip()
    sid_overridden = False
    try:
        sid_int = int(sid_raw) if sid_raw else 0
    except ValueError:
        sid_int = 0
    if not reasonable_epoch(sid_int):
        sid_int = int(time.time())
        sid_overridden = True
    sid = safe_name(str(sid_int), default=str(int(time.time())))
    raw_track = (x_track_name or "").strip()
    if raw_track.lower().endswith(".ndjson"):
        raw_track = raw_track[: -len(".ndjson")]
    if raw_track.startswith("session_"):
        raw_track = raw_track[len("session_"):]
    if raw_track.startswith(f"{sid}_"):
        raw_track = raw_track[len(sid) + 1:]
    if kind == "debug" and raw_track.lower().endswith(".dbg"):
        raw_track = raw_track[: -len(".dbg")]
    track = safe_name(raw_track, default="UNKNOWN")
    if kind == "debug":
        filename = f"{sid}_{track}.dbg.ndjson"
        out_path = DATA_DIR / "debug" / email / filename
    else:
        filename = f"{sid}_{track}.ndjson"
        out_path = session_dir_for(email) / filename
    return email, sid, sid_int, sid_overridden, track, filename, out_path


def _zb_decode_partial(data: bytes) -> bytes:
    """Best-effort zblocks decode: complete frames only, silently dropping a
    trailing truncated frame. For salvaging interrupted uploads."""
    out = bytearray()
    off = 0
    n = len(data)
    while off + 10 <= n:
        if data[off:off + 2] != b"ZB":
            break
        rl, cl = struct.unpack_from("<II", data, off + 2)
        if rl == 0 or cl == 0 or rl > (1 << 20) or cl > (1 << 20) or off + 10 + cl > n:
            break
        try:
            d = zlib.decompressobj(-15)
            raw = d.decompress(data[off + 10:off + 10 + cl]) + d.flush()
        except Exception:
            break
        if len(raw) != rl:
            break
        out += raw
        off += 10 + cl
    return bytes(out)


async def _save_body(
    request: Request,
    x_api_key: Optional[str],
    x_user_email: Optional[str],
    x_session_id: Optional[str],
    x_track_name: Optional[str],
    *,
    mode: str,
    kind: str = "",
) -> JSONResponse:
    """Common body for /upload (mode='w') and /stream (mode='a').
    kind='debug' files a companion GPS/health log under debug/<user>/ instead of
    overwriting the real session (same session-id+track)."""
    _client_host = request.client.host if request.client else "?"
    _upload_event({"ev": "start", "ip": _client_host, "kind": kind or "session",
                   "mode": mode, "session": x_session_id, "track": x_track_name,
                   "content_length": request.headers.get("content-length"),
                   "transfer_encoding": request.headers.get("transfer-encoding"),
                   "has_key": bool(x_api_key), "user": x_user_email})
    web_user = None
    if API_KEY and x_api_key != API_KEY:
        # Accept EITHER a logged-in web user OR a valid per-user API key
        # (the dash sends its account key as X-API-Key). Without the per-user
        # check, setting RACECAR_API_KEY for any reason would 401 every dash
        # upload whose key isn't the global one.
        web_user = current_user(request) if oauth_enabled() else None
        if not web_user and not (x_api_key and email_for_api_key(x_api_key)):
            raise HTTPException(status_code=401, detail="invalid api key")
    elif oauth_enabled():
        web_user = current_user(request)

    # Resolve the target BEFORE reading the body so an interrupted read can
    # still salvage what arrived (resume support, v: resume).
    email, sid, sid_int, sid_overridden, track, filename, out_path = \
        _resolve_upload_target(x_user_email, x_session_id, x_track_name,
                               x_api_key, web_user, kind)
    _zb_hdr = (request.headers.get("x-body-format") or "").strip().lower() == "zblocks"
    _rx = 0
    try:
        _chunks: list[bytes] = []
        async for _chunk in request.stream():
            _chunks.append(_chunk)
            _rx += len(_chunk)
        body = b"".join(_chunks)
    except Exception as e:
        # SALVAGE (v: resume): keep every complete line that made it. Without
        # this, a stalled UART meant the retry restarted from byte 0 forever —
        # a file that can't stream end-to-end in ONE pass could NEVER land.
        try:
            part = b"".join(_chunks)
            if _zb_hdr:
                part = _zb_decode_partial(part)
            nl_at = part.rfind(b"\n")
            part = part[:nl_at + 1] if nl_at >= 0 else b""
            if part and kind != "debug":
                out_path.parent.mkdir(parents=True, exist_ok=True)
                with open(out_path, "wb" if mode == "w" else "ab") as f:
                    f.write(part)
                _upload_event({"ev": "partial_saved", "ip": _client_host,
                               "session": sid, "track": track, "mode": mode,
                               "bytes": len(part), "lines": part.count(b"\n"),
                               "file_total": out_path.stat().st_size})
        except Exception as se:
            log.warning("partial salvage failed: %s", se)
        # Client aborted mid-stream (dropped WiFi, TLS reset, chunked framing
        # error). This is the smoking gun for "upload died partway" — and
        # bytes_received says HOW FAR it got before dying (0 = the body never
        # started; N = it flowed then stopped), which discriminates a
        # never-wrote client bug from a mid-stream stall.
        _upload_event({"ev": "recv_error", "ip": _client_host, "kind": kind or "session",
                       "session": x_session_id, "track": x_track_name,
                       "bytes_received": _rx,
                       "err": f"{type(e).__name__}: {e}"})
        raise HTTPException(status_code=400, detail=f"body read failed: {type(e).__name__}")
    if not body:
        _upload_event({"ev": "reject", "ip": _client_host, "kind": kind or "session",
                       "session": x_session_id, "reason": "empty body"})
        raise HTTPException(status_code=400, detail="empty body")
    # Compressed upload (v0.1.127): the dash only sends this header after the
    # /caps probe confirmed we decode it. Inflate to the raw NDJSON here so
    # everything downstream (validation, lap detection, storage) is unchanged.
    wire_len = len(body)
    if (request.headers.get("x-body-format") or "").strip().lower() == "zblocks":
        try:
            body = _zb_decode(body)
        except ValueError as e:
            _upload_event({"ev": "reject", "ip": _client_host, "kind": kind or "session",
                           "session": x_session_id, "reason": f"zblocks: {e}",
                           "bytes": wire_len})
            raise HTTPException(status_code=400, detail=f"zblocks decode failed: {e}")
    if len(body) > MAX_BODY_BYTES:
        _upload_event({"ev": "reject", "ip": _client_host, "kind": kind or "session",
                       "session": x_session_id, "reason": "too large", "bytes": len(body)})
        raise HTTPException(status_code=413, detail="body too large")

    # Debug logs are our own event NDJSON ({"ev":"h",...}) — they don't carry the
    # lat/lon/t sample keys the session validator requires, so skip it for them.
    if kind == "debug":
        validation = {"samples": body.count(b"\n")}
    else:
        validation = validate_ndjson_body(body)


    # (email/sid/track/out_path resolved above, before the body read.)
    # Open in the requested mode. 'wb' overwrites (AfterRace whole-file POSTs
    # so retries are idempotent), 'ab' appends (live streaming).
    flags = "wb" if mode == "w" else "ab"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, flags) as f:
        f.write(body)

    nl = int(validation["samples"])
    size = out_path.stat().st_size

    _upload_event({"ev": "ok", "ip": _client_host, "kind": kind or "session",
                   "session": sid, "track": track, "bytes": len(body), "lines": nl,
                   "wire": wire_len,   # < bytes when the body came in compressed
                   "path": str(out_path.relative_to(DATA_DIR))})
    # Auto-coach: review this session in the BACKGROUND (never delays the dash's
    # upload response) and file 1-3 de-duplicated checklist items.
    if kind != "debug":
        _coach_kick(email, out_path, track)   # ok on mode=a == resumed file COMPLETED
    log.info(
        "received %s mode=%s email=%s session=%s%s track=%s bytes=%d lines=%d -> %s",
        request.client.host if request.client else "?",
        mode,
        email,
        sid,
        " (server-clock)" if sid_overridden else "",
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
            "session_id": sid_int,
            "session_id_overridden": sid_overridden,
        }
    )


@app.get("/upload/progress")
async def upload_progress(
    request: Request,
    session: str = Query(""),
    track: str = Query(""),
    kind: str = Query(""),
    x_api_key: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
) -> JSONResponse:
    """RESUME support: the dash asks how many bytes/lines of a session the
    server already holds (whole-file POSTs + salvaged partials), then re-pulls
    from the Teensy with Q,GET,<file>,<skip_lines> and appends via /stream.
    Auth mirrors /upload (master key, per-user key, or logged-in web user)."""
    web_user = None
    if API_KEY and x_api_key != API_KEY:
        web_user = current_user(request) if oauth_enabled() else None
        if not web_user and not (x_api_key and email_for_api_key(x_api_key)):
            raise HTTPException(status_code=401, detail="invalid api key")
    elif oauth_enabled():
        web_user = current_user(request)
    _, _, _, _, _, filename, out_path = _resolve_upload_target(
        x_user_email, session, track, x_api_key, web_user,
        "debug" if kind == "debug" else "")
    if not out_path.exists():
        return JSONResponse({"ok": True, "exists": False, "bytes": 0, "lines": 0,
                             "file": filename})
    data_len = out_path.stat().st_size
    lines = 0
    with open(out_path, "rb") as f:
        while True:
            chunk = f.read(1 << 20)
            if not chunk:
                break
            lines += chunk.count(b"\n")
    return JSONResponse({"ok": True, "exists": True, "bytes": data_len,
                         "lines": lines, "file": filename})


@app.post("/upload")
async def upload(
    request: Request,
    x_api_key: Optional[str] = Header(None),
    x_user_email: Optional[str] = Header(None),
    x_session_id: Optional[str] = Header(None),
    x_track_name: Optional[str] = Header(None),
    x_file_kind: Optional[str] = Header(None),
) -> JSONResponse:
    return await _save_body(
        request,
        x_api_key,
        x_user_email,
        x_session_id,
        x_track_name,
        mode="w",
        kind=(x_file_kind or "").strip().lower(),
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
async def list_sessions(request: Request) -> dict:
    """JSON listing of saved sessions. Useful for tooling/cli inspection."""
    web_user = require_web_user(request)
    viewer_email = str((web_user or {}).get("email", ""))
    out = []
    sessions_root = DATA_DIR / "sessions"
    if sessions_root.exists():
        for user_dir in sorted(sessions_root.iterdir()):
            if not user_dir.is_dir():
                continue
            if oauth_enabled() and not can_view_dir(viewer_email, user_dir.name):
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
                        "display_epoch": display_epoch_for(f),
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


# ---------------------------------------------------------------------------
# Lap detection.
#
# The dash already knows each circuit's start/finish line, but the cloud has
# no track table — so we AUTO-DETECT the start/finish from the GPS trace
# itself (the user's "don't make me enter S/F by hand" ask). The method is
# the same family the dash firmware uses: pick an anchor point on track, then
# count each return to within R metres of it (after the car has left by >2R),
# guarded by a minimum lap time. Every crossing closes a lap.
# ---------------------------------------------------------------------------
_LAP_RADIUS_KM = 0.040       # 40 m start/finish detection radius
_LAP_MIN_SEC = 20.0          # ignore "crossings" sooner than this (pit crawl, noise)
_LAP_MOVING_MPH = 12.0       # anchor must be a point where the car is actually driving


def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    d2r = math.pi / 180.0
    dlat = (lat2 - lat1) * d2r
    dlon = (lon2 - lon1) * d2r
    a = (math.sin(dlat / 2.0) ** 2
         + math.cos(lat1 * d2r) * math.cos(lat2 * d2r) * math.sin(dlon / 2.0) ** 2)
    return 6371.0 * 2.0 * math.atan2(math.sqrt(a), math.sqrt(1.0 - a))


def _relative_seconds(samples: list) -> tuple:
    """Seconds-from-start for each sample, mirroring the review page's T[] logic
    (prefer epoch `t`, else `t_ms`/1000, else synthetic 25 Hz). Returns
    (rel_list, basis)."""
    raw: list = []
    first = None
    for s in samples:
        v = None
        t = s.get("t")
        if isinstance(t, (int, float)) and math.isfinite(t):
            v = float(t)
        else:
            tms = s.get("t_ms")
            if isinstance(tms, (int, float)) and math.isfinite(tms):
                v = float(tms) / 1000.0
        if v is not None and first is None:
            first = v
        raw.append(v)
    usable = first is not None
    if usable:
        last = first
        for i in range(len(raw)):
            if raw[i] is None:
                raw[i] = last
            else:
                last = raw[i]
        if not (raw and (raw[-1] - raw[0]) > 0.5):
            usable = False
    if not usable:
        raw = [i / 25.0 for i in range(len(samples))]
    t0 = raw[0] if raw else 0.0
    return [r - t0 for r in raw], ("epoch" if usable else "synthetic")


def _seg_cross(ax, ay, bx, by, cx, cy, dx, dy) -> bool:
    """True if segment A-B intersects segment C-D (planar)."""
    def cr(px, py, qx, qy, rx, ry):
        return (qx - px) * (ry - py) - (qy - py) * (rx - px)
    d1 = cr(cx, cy, dx, dy, ax, ay)
    d2 = cr(cx, cy, dx, dy, bx, by)
    d3 = cr(ax, ay, bx, by, cx, cy)
    d4 = cr(ax, ay, bx, by, dx, dy)
    return ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0))


def _sf_line_from(lat, lon, heading_deg, half_m=30.0):
    """Perpendicular start/finish line segment (2 endpoints) at a point, given
    the travel heading. Returns ((lat1,lon1),(lat2,lon2))."""
    h = math.radians(heading_deg or 0.0)
    # left of travel (E,N): rotate forward (sinH,cosH) by +90 -> (-cosH, sinH)
    lE, lN = -math.cos(h), math.sin(h)
    dlat = (half_m / 111320.0)
    dlon = (half_m / (111320.0 * max(0.1, math.cos(math.radians(lat)))))
    return ((lat + lN * dlat, lon + lE * dlon),
            (lat - lN * dlat, lon - lE * dlon))


def _detect_laps(samples: list) -> dict:
    rel, basis = _relative_seconds(samples)
    n = len(samples)

    def geo(i):
        s = samples[i]
        lat, lon = s.get("lat"), s.get("lon")
        if (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and (lat or lon) and -90 <= lat <= 90 and -180 <= lon <= 180):
            return lat, lon
        return None

    # -- Path 1: the Teensy stamped a lap counter into the stream. Trust it:
    #    lap boundaries are exactly where the integer `lap` value increments.
    has_lap_field = any(isinstance(s.get("lap"), int) for s in samples)
    sf_info = None
    if has_lap_field:
        crossings = []
        prev_lap = None
        for i in range(n):
            lp = samples[i].get("lap")
            if not isinstance(lp, int):
                continue
            if prev_lap is None:
                prev_lap = lp
                crossings.append(i)
            elif lp != prev_lap:
                crossings.append(i)
                prev_lap = lp
        if len(crossings) < 2:
            crossings = []
    else:
        crossings = []

    # -- Path 2: no lap field -> auto-detect via a start/finish LINE crossing.
    if not crossings:
        anchor = None
        for i in range(n):
            g = geo(i)
            if g is None:
                continue
            mph = samples[i].get("speed_mph")
            if isinstance(mph, (int, float)) and mph > _LAP_MOVING_MPH:
                anchor = (g[0], g[1], i)
                break
        if anchor is None:
            for i in range(n):
                g = geo(i)
                if g is not None:
                    anchor = (g[0], g[1], i)
                    break
        if anchor is None:
            return {"laps": [], "best_lap": None, "sf": None, "time_basis": basis}

        alat, alon, ai = anchor
        # Heading at the anchor: prefer the logged heading, else bearing to the
        # next distinct point, so the S/F line sits perpendicular to travel.
        hd = samples[ai].get("heading_deg")
        if not isinstance(hd, (int, float)):
            hd = 0.0
            for j in range(ai + 1, min(ai + 40, n)):
                g = geo(j)
                if g and (g[0] != alat or g[1] != alon):
                    hd = math.degrees(math.atan2(
                        math.sin(math.radians(g[1] - alon)) * math.cos(math.radians(g[0])),
                        math.cos(math.radians(alat)) * math.sin(math.radians(g[0]))
                        - math.sin(math.radians(alat)) * math.cos(math.radians(g[0]))
                        * math.cos(math.radians(g[1] - alon)))) % 360.0
                    break
        (l1lat, l1lon), (l2lat, l2lon) = _sf_line_from(alat, alon, hd)
        sf_info = {"lat1": l1lat, "lon1": l1lon, "lat2": l2lat, "lon2": l2lon,
                   "lat": alat, "lon": alon}

        crossings = [ai]
        last_cross_t = rel[ai]
        prev = geo(ai)
        for i in range(ai + 1, n):
            g = geo(i)
            if g is None:
                continue
            if (rel[i] - last_cross_t) >= _LAP_MIN_SEC and _seg_cross(
                    prev[1], prev[0], g[1], g[0],
                    l1lon, l1lat, l2lon, l2lat):
                crossings.append(i)
                last_cross_t = rel[i]
            prev = g

    laps = []
    for k in range(len(crossings) - 1):
        i0, i1 = crossings[k], crossings[k + 1]
        secs = rel[i1] - rel[i0]
        max_mph = 0.0
        for j in range(i0, i1 + 1):
            mph = samples[j].get("speed_mph")
            if isinstance(mph, (int, float)) and mph > max_mph:
                max_mph = mph
        laps.append({
            "lap": k + 1,
            "t_start": round(rel[i0], 3),
            "t_end": round(rel[i1], 3),
            "seconds": round(secs, 3),
            "ms": int(secs * 1000),
            "max_mph": round(max_mph, 1),
        })

    best = None
    best_secs = float("inf")
    for lp in laps:
        if lp["seconds"] < best_secs:
            best_secs = lp["seconds"]
            best = lp["lap"]
    return {
        "laps": laps,
        "best_lap": best,
        "sf": sf_info,
        "source": "teensy_lap_field" if has_lap_field and laps else "line_crossing",
        "time_basis": basis,
    }


# ---------------------------------------------------------------------------
# AI corner analysis helpers
# ---------------------------------------------------------------------------
def _point_in_poly(lat: float, lon: float, poly: list) -> bool:
    """Ray-casting point-in-polygon. poly = [[lat,lon], ...] (lon = x, lat = y)."""
    inside = False
    n = len(poly)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        yi, xi = poly[i][0], poly[i][1]
        yj, xj = poly[j][0], poly[j][1]
        if ((yi > lat) != (yj > lat)) and \
           (lon < (xj - xi) * (lat - yi) / ((yj - yi) or 1e-15) + xi):
            inside = not inside
        j = i
    return inside


def _region_metrics(samples: list, poly: list) -> dict:
    """Per-lap driving metrics for the samples that fall inside `poly`.

    Returns {laps:[{lap, n, entry_mph, min_mph, exit_mph, max_mph, seconds,
    dist_m, peak_lat_g, peak_long_g, max_rpm}], points_in_region, total_laps,
    best_lap}. Lap membership comes from the same auto start/finish detector the
    review UI uses, so a region can be compared apex-to-apex across every lap.
    """
    rel, _basis = _relative_seconds(samples)
    laps_info = _detect_laps(samples)
    laps = laps_info.get("laps", [])

    def lap_of(t: float):
        for lp in laps:
            if lp["t_start"] <= t <= lp["t_end"]:
                return lp["lap"]
        return None

    per: dict = {}
    total_pts = 0
    for i, s in enumerate(samples):
        lat, lon = s.get("lat"), s.get("lon")
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float))
                and (lat or lon)):
            continue
        if not _point_in_poly(lat, lon, poly):
            continue
        total_pts += 1
        lp = lap_of(rel[i])
        per.setdefault(lp, []).append(i)

    def num(s, k):
        v = s.get(k)
        return v if isinstance(v, (int, float)) else None

    out = []
    for lp in sorted(k for k in per.keys() if k is not None):
        idxs = per[lp]
        seg = [samples[i] for i in idxs]
        speeds = [num(s, "speed_mph") for s in seg]
        speeds = [v for v in speeds if v is not None]
        latg = [abs(num(s, "ay")) for s in seg if num(s, "ay") is not None]
        longg = [abs(num(s, "ax")) for s in seg if num(s, "ax") is not None]
        rpm = [num(s, "rpm") for s in seg if num(s, "rpm") is not None]
        dist_m = 0.0
        for a, b in zip(idxs, idxs[1:]):
            ga, gb = samples[a], samples[b]
            dist_m += _haversine_km(ga["lat"], ga["lon"], gb["lat"], gb["lon"]) * 1000.0
        entry = num(seg[0], "speed_mph")
        exit_ = num(seg[-1], "speed_mph")
        out.append({
            "lap": lp,
            "n": len(seg),
            "entry_mph": round(entry, 1) if entry is not None else None,
            "min_mph": round(min(speeds), 1) if speeds else None,
            "exit_mph": round(exit_, 1) if exit_ is not None else None,
            "max_mph": round(max(speeds), 1) if speeds else None,
            "seconds": round(rel[idxs[-1]] - rel[idxs[0]], 2),
            "dist_m": round(dist_m, 1),
            "peak_lat_g": round(max(latg), 2) if latg else None,
            "peak_long_g": round(max(longg), 2) if longg else None,
            "max_rpm": int(max(rpm)) if rpm else None,
        })
    return {
        "laps": out,
        "points_in_region": total_pts,
        "total_laps": len(laps),
        "best_lap": laps_info.get("best_lap"),
    }


# ---------------------------------------------------------------------------
# Cross-session lap library (v: lineview). When the driver circles a corner,
# mine ALL of their sessions on the SAME TRACK (any day) for laps through that
# region: up to 10 FASTER references + up to 10 SIMILAR-pace references feed
# the AI comparison, and /lines + /lineview render the fastest real line with
# brake/apex/throttle markers so the difference is visible, not just described.
# ---------------------------------------------------------------------------
def _track_key(filename: str) -> str:
    """'<sid>_<track>.ndjson' -> normalized track key ('-combined' stripped)."""
    stem = filename[:-7] if filename.endswith(".ndjson") else filename
    if "_" in stem:
        stem = stem.split("_", 1)[1]
    if stem.endswith("-combined"):
        stem = stem[: -len("-combined")]
    return stem.lower()


def _read_ndjson_samples(p: pathlib.Path) -> list:
    samples: list = []
    try:
        with open(p, "rb") as f:
            for raw in f:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    samples.append(json.loads(raw))
                except Exception:
                    continue
    except OSError:
        return []
    return samples


def _session_date_label(filename: str) -> str:
    try:
        sid = int(filename.split("_", 1)[0])
        if reasonable_epoch(sid):
            return time.strftime("%m/%d", time.localtime(sid))
    except Exception:
        pass
    return filename.split("_", 1)[0][:8]


def _region_traverses(samples: list, poly: list, user_dir: str, fname: str) -> list:
    """Per-lap passes through `poly` with brake/apex/throttle analysis and a
    decimated GPS trace (window EXTENDED before/after the region so braking
    that starts before the circled area is captured). Laps excluded on the
    review page (manual or auto <10s) are skipped — same rules as /laps."""
    rel, _basis = _relative_seconds(samples)
    laps_info = _detect_laps(samples)
    laps = laps_info.get("laps", [])
    if not laps:
        return []
    meta = _lap_meta(user_dir, fname)
    excl = set(meta["excluded"])
    incl = set(meta["included"])
    allowed = {}
    for lp in laps:
        n = int(lp.get("lap", 0))
        if n in excl:
            continue
        if float(lp.get("seconds", 0)) < LAP_AUTO_EXCLUDE_UNDER_S and n not in incl:
            continue
        allowed[n] = lp

    def lap_of(t: float):
        for lp in laps:
            if lp["t_start"] <= t <= lp["t_end"]:
                return lp["lap"]
        return None

    def num(s, k):
        v = s.get(k)
        return v if isinstance(v, (int, float)) else None

    # bbox prefilter: full point-in-poly only for the tiny fraction of a 90k-
    # sample session that's anywhere near the circled corner (12-session scans
    # would otherwise spend many seconds in the polygon test).
    blat0 = min(p[0] for p in poly); blat1 = max(p[0] for p in poly)
    blon0 = min(p[1] for p in poly); blon1 = max(p[1] for p in poly)
    per: dict = {}
    for i, s in enumerate(samples):
        lat, lon = s.get("lat"), s.get("lon")
        if not (isinstance(lat, (int, float)) and isinstance(lon, (int, float)) and (lat or lon)):
            continue
        if lat < blat0 or lat > blat1 or lon < blon0 or lon > blon1:
            continue
        if not _point_in_poly(lat, lon, poly):
            continue
        lp = lap_of(rel[i])
        if lp in allowed:
            per.setdefault(lp, []).append(i)

    date_lbl = _session_date_label(fname)
    out = []
    for n in sorted(per.keys()):
        idxs = per[n]
        # longest CONTIGUOUS run (a lasso over esses can clip a lap twice)
        runs, cur = [], [idxs[0]]
        for a, b in zip(idxs, idxs[1:]):
            if b - a <= 8:
                cur.append(b)
            else:
                runs.append(cur)
                cur = [b]
        runs.append(cur)
        run = max(runs, key=len)
        if len(run) < 4:
            continue
        first, last = run[0], run[-1]
        lp = allowed[n]
        # extend the window so pre-region braking / post-region acceleration is
        # visible (~4s before, ~2.5s after at 25 Hz), clamped to the lap.
        ext0 = first
        while ext0 > 0 and first - ext0 < 100 and rel[ext0 - 1] >= lp["t_start"]:
            ext0 -= 1
        ext1 = last
        while ext1 + 1 < len(samples) and ext1 - last < 60 and rel[ext1 + 1] <= lp["t_end"]:
            ext1 += 1
        win = list(range(ext0, ext1 + 1))
        spd = [num(samples[i], "speed_mph") or 0.0 for i in win]
        # 3-tap smoothing for the brake/throttle edge detectors
        sm = [spd[0]] + [(spd[k - 1] + spd[k] + spd[k + 1]) / 3.0
                         for k in range(1, len(spd) - 1)] + [spd[-1]]
        in0, in1 = first - ext0, last - ext0          # region span within win
        apex_k = min(range(in0, in1 + 1), key=lambda k: sm[k])
        bk = apex_k
        while bk > 0 and sm[bk - 1] > sm[bk] + 0.02:   # climb the decel slope
            bk -= 1
        tk = apex_k
        while tk + 1 < len(sm) and not (sm[tk + 1] > sm[tk] + 0.02):
            tk += 1
        if tk + 1 >= len(sm):
            tk = apex_k

        def pathm(k0, k1):
            d = 0.0
            for a, b in zip(win[k0:k1], win[k0 + 1:k1 + 1]):
                d += _haversine_km(samples[a]["lat"], samples[a]["lon"],
                                   samples[b]["lat"], samples[b]["lon"]) * 1000.0
            return d

        def ptinfo(k):
            s = samples[win[k]]
            return {"lat": round(s["lat"], 6), "lon": round(s["lon"], 6),
                    "mph": round(spd[k], 1)}

        step = max(1, (len(win) + 239) // 240)
        trace = []
        for k in range(0, len(win), step):
            s = samples[win[k]]
            trace.append([round(s["lat"], 6), round(s["lon"], 6), round(spd[k], 1)])
        brake = ptinfo(bk)
        brake["dist_to_apex_m"] = round(pathm(bk, apex_k), 0)
        out.append({
            "session": fname,
            "date": date_lbl,
            "lap": n,
            "label": f"{date_lbl} L{n}",
            "seconds": round(rel[last] - rel[first], 2),
            "entry_mph": round(spd[in0], 1),
            "min_mph": round(sm[apex_k], 1),
            "exit_mph": round(spd[in1], 1),
            "brake": brake if bk < apex_k else None,
            "apex": ptinfo(apex_k),
            "throttle": ptinfo(tk) if tk > apex_k else None,
            "trace": trace,
        })
    return out


def _lap_library(user_dir: str, current_path: pathlib.Path, poly: list,
                 max_sessions: int = 12) -> dict:
    """Gather region traverses from up to `max_sessions` of the user's most
    recent sessions on the SAME track and rank them against the current
    session: up to 10 FASTER + up to 10 SIMILAR-pace references."""
    sroot = DATA_DIR / "sessions" / user_dir
    tkey = _track_key(current_path.name)
    cands = []
    if sroot.exists():
        for f in sroot.iterdir():
            if (f.is_file() and f.name.endswith(".ndjson")
                    and not f.name.endswith(".dbg.ndjson")
                    and _track_key(f.name) == tkey
                    and f.name != current_path.name
                    and f.stat().st_size <= 60 * 1024 * 1024):
                cands.append(f)
    cands.sort(key=lambda f: f.stat().st_mtime, reverse=True)
    files = [current_path] + cands[: max(0, max_sessions - 1)]

    current: list = []
    pool: list = []
    scanned = 0
    for f in files:
        samples = _read_ndjson_samples(f)
        if not samples:
            continue
        trav = _region_traverses(samples, poly, user_dir, f.name)
        scanned += 1
        if f.name == current_path.name:
            current = trav
        else:
            pool.extend(trav)

    faster: list = []
    similar: list = []
    if current:
        cur_ts = sorted(t["seconds"] for t in current)
        cur_best = cur_ts[0]
        cur_med = cur_ts[len(cur_ts) // 2]
        faster = sorted([t for t in pool if t["seconds"] < cur_best - 0.005],
                        key=lambda t: t["seconds"])[:10]
        in_f = {(t["session"], t["lap"]) for t in faster}
        similar = sorted([t for t in pool if (t["session"], t["lap"]) not in in_f],
                         key=lambda t: abs(t["seconds"] - cur_med))[:10]
    return {"track": tkey, "sessions_scanned": scanned,
            "current": current, "faster": faster, "similar": similar}


def _refs_table(trs: list) -> list:
    rows = ["session | lap | time_s | entry_mph | min_mph | exit_mph | brake_mph | brake_m_before_apex"]
    for t in trs:
        b = t.get("brake") or {}
        rows.append(" | ".join(str(v if v is not None else "-") for v in (
            t["date"], t["lap"], t["seconds"], t["entry_mph"], t["min_mph"],
            t["exit_mph"], b.get("mph", "-"), b.get("dist_to_apex_m", "-"))))
    return rows


def _region_prompt(metrics: dict, question: str, lib: Optional[dict] = None) -> list:
    """Build the chat messages: a race-engineer system prompt + a compact
    per-lap metrics table (+ cross-session reference laps) + the question."""
    laps = metrics.get("laps", [])
    lines = [
        "Per-lap telemetry through the track section the driver circled on the map.",
        "Source: GPS + IMU logged at 25 Hz. Speeds in mph, distances in metres,",
        "g-forces in units of g (peak_lat_g = cornering load, peak_long_g =",
        "combined braking/acceleration load through the section).",
        "",
        "lap | entry_mph | min_mph | exit_mph | max_mph | time_s | dist_m | peak_lat_g | peak_long_g | max_rpm",
    ]
    for lp in laps:
        def c(v):
            return "-" if v is None else str(v)
        lines.append(" | ".join(c(lp[k]) for k in (
            "lap", "entry_mph", "min_mph", "exit_mph", "max_mph",
            "seconds", "dist_m", "peak_lat_g", "peak_long_g", "max_rpm")))
    if metrics.get("best_lap"):
        lines.append("")
        lines.append(f"Session's fastest overall lap (whole track): lap {metrics['best_lap']}.")
    lines.append(f"({metrics.get('points_in_region', 0)} GPS points fell inside the region "
                 f"across {len(laps)} laps.)")
    if lib and (lib.get("faster") or lib.get("similar")):
        lines.append("")
        lines.append(f"REFERENCE LAPS from the driver's OTHER sessions on this track "
                     f"({lib.get('sessions_scanned', 0)} sessions scanned), same circled "
                     f"region. brake_m_before_apex = metres before the min-speed point "
                     f"where sustained braking began.")
        if lib.get("faster"):
            lines.append("")
            lines.append(f"FASTER than this session's best through the region "
                         f"({len(lib['faster'])}):")
            lines.extend(_refs_table(lib["faster"]))
        if lib.get("similar"):
            lines.append("")
            lines.append(f"SIMILAR pace ({len(lib['similar'])}):")
            lines.extend(_refs_table(lib["similar"]))
        lines.append("")
        lines.append("When faster references exist, coach by DIRECT comparison: where do "
                     "they brake relative to this session (brake_m_before_apex and "
                     "brake_mph), how much more min/exit speed do they carry, and "
                     "quantify the time on offer through this section.")
    table = "\n".join(lines)
    system = (
        "You are a professional race engineer and driving coach analyzing "
        "telemetry from an amateur's track car. Be concise and concrete: give "
        "specific, actionable coaching (braking points, apex speed, throttle "
        "application, gear, line) grounded in the numbers provided. Compare the "
        "laps to each other, call out the best and worst, and quantify the time "
        "or speed on offer. Format the answer in clean Markdown: '##' section "
        "headings, bullet lists for coaching points, and proper Markdown tables "
        "(header row + '---' separator row) for any lap comparison — never "
        "ASCII-art or inline pipe lists. Bold the key numbers. If the data is "
        "insufficient to answer, say so plainly."
    )
    user = f"{question.strip() or 'Analyze this section and tell me how to be faster through it.'}\n\n{table}"
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _ai_chat(messages: list, model: Optional[str] = None) -> tuple:
    """Call the Open WebUI OpenAI-compatible chat endpoint. Returns
    (reply_text, model_used), or raises HTTPException on config/upstream errors."""
    if not AI_API_KEY:
        raise HTTPException(status_code=503,
                            detail="AI is not configured (set RACECAR_AI_API_KEY)")
    use_model = ai_resolve_model(model)   # allowlist-enforced
    if not use_model:
        raise HTTPException(status_code=503,
                            detail="No AI model selected and RACECAR_AI_MODEL is unset")
    payload_obj = {
        "model": use_model,
        "messages": messages,
        "stream": False,
    }
    # temperature is DEPRECATED / rejected by newer models (e.g. Anthropic
    # claude-sonnet-5 -> "temperature is deprecated for this model"), so only
    # send it when explicitly configured via RACECAR_AI_TEMPERATURE.
    if AI_TEMPERATURE is not None:
        payload_obj["temperature"] = AI_TEMPERATURE
    body = json.dumps(payload_obj).encode()
    req = urllib.request.Request(
        AI_BASE_URL + "/api/chat/completions",
        data=body,
        headers={
            "Authorization": "Bearer " + AI_API_KEY,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=AI_TIMEOUT) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:400] if e.fp else str(e)
        raise HTTPException(status_code=502, detail=f"AI upstream {e.code}: {detail}")
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"AI request failed: {e}")
    try:
        content = payload["choices"][0]["message"]["content"]
    except Exception:
        return json.dumps(payload)[:2000], use_model, {}
    # Cost/usage capture (v: admin cost display): the OpenAI-compatible payload
    # may carry a usage block, and Open WebUI appends a <details> cost/token
    # footer to the text — harvest BOTH before stripping the footer.
    usage = _ai_parse_usage(payload, content)
    # Open WebUI appends a collapsible <details> usage/cost/token footer (admin-
    # only info) to the reply. Strip EVERY such block anywhere in the text so the
    # review card shows only the coaching content.
    content = re.sub(r"<details>.*?</details>", "", content, flags=re.S | re.I).strip()
    return content, use_model, usage


def _ai_parse_usage(payload: dict, content: str) -> dict:
    """Harvest token counts + $ cost from the API usage block and/or the Open
    WebUI <details> footer. Best-effort — absent fields are simply omitted."""
    out: dict = {}
    u = payload.get("usage")
    if isinstance(u, dict):
        for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
            if isinstance(u.get(k), (int, float)):
                out[k] = int(u[k])
        for k in ("cost", "total_cost", "cost_usd"):
            if isinstance(u.get(k), (int, float)):
                out["cost_usd"] = round(float(u[k]), 6)
                break
    for m in re.finditer(r"<details>(.*?)</details>", content, re.S | re.I):
        txt = m.group(1)
        if "cost_usd" not in out:
            dm = re.search(r"\$\s*([0-9]+(?:\.[0-9]+)?)", txt)
            if dm:
                try:
                    out["cost_usd"] = round(float(dm.group(1)), 6)
                except ValueError:
                    pass
        if "total_tokens" not in out:
            tm = re.findall(r"([\d,]+)\s*(?:total\s*)?tokens", txt, re.I)
            if tm:
                try:
                    out["total_tokens"] = int(tm[-1].replace(",", ""))
                except ValueError:
                    pass
    return out


def _req_is_admin(request: Request) -> bool:
    """Is the signed-in viewer an admin? (dev mode / OAuth off = yes)."""
    if not oauth_enabled():
        return True
    u = current_user(request)
    return bool(u and is_admin_email(str(u.get("email", ""))))


def _hist_public(hist: list, admin: bool) -> list:
    """History as sent to the browser: usage/cost is ADMIN-ONLY."""
    if admin:
        return hist
    return [{k: v for k, v in e.items() if k != "usage"} for e in hist]


def _ai_history_path(user: str, session_name: str) -> pathlib.Path:
    """On-disk path for a session's AI conversation history
    (/data/ai_history/<user>/<sessionfile>.json). Keyed by the same safe user
    slug + resolved session filename so it maps 1:1 to the session."""
    d = AI_HISTORY_DIR / safe_name(user)
    return d / (safe_name(session_name) + ".json")


def _ai_history_load(user: str, session_name: str) -> list:
    p = _ai_history_path(user, session_name)
    if p.exists():
        try:
            data = json.loads(p.read_text("utf-8"))
            return data if isinstance(data, list) else []
        except Exception:
            return []
    return []


def _ai_history_append(user: str, session_name: str, entry: dict) -> list:
    p = _ai_history_path(user, session_name)
    p.parent.mkdir(parents=True, exist_ok=True)
    hist = _ai_history_load(user, session_name)
    hist.append(entry)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(hist), "utf-8")
    tmp.replace(p)   # atomic
    return hist


def _ai_history_delete_file(user: str, session_name: str) -> None:
    """Remove a session's entire AI history (called when the session is deleted)."""
    p = _ai_history_path(user, session_name)
    try:
        p.unlink()
    except FileNotFoundError:
        pass
    except OSError:
        pass
    try:
        p.parent.rmdir()   # tidy empty per-user history dir
    except OSError:
        pass


def _ai_model_list() -> list:
    """Models the UI may offer. If an allowlist is configured (RACECAR_AI_MODELS
    or RACECAR_AI_MODEL), it is authoritative and we do NOT expose the live
    100+ catalogue. Only with NO allowlist do we fetch the full list."""
    if not AI_API_KEY:
        return []
    if AI_MODELS:
        return [{"id": mid, "name": mid} for mid in AI_MODELS]
    req = urllib.request.Request(
        AI_BASE_URL + "/api/models",
        headers={"Authorization": "Bearer " + AI_API_KEY},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            payload = json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        log.warning("AI model list fetch failed: %s", e)
        return []
    data = payload.get("data", payload) if isinstance(payload, dict) else payload
    out = []
    if isinstance(data, list):
        for m in data:
            if isinstance(m, dict):
                mid = m.get("id") or m.get("name")
                if mid:
                    out.append({"id": mid, "name": m.get("name") or mid})
            elif isinstance(m, str):
                out.append({"id": m, "name": m})
    return out


@app.get("/sessions/{user}/{filename}")
async def download_session(request: Request, user: str, filename: str) -> FileResponse:
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    return FileResponse(
        p,
        media_type="application/x-ndjson",
        filename=p.name,
    )


@app.delete("/sessions/{user}/{filename}")
async def delete_session(
    request: Request,
    user: str,
    filename: str,
    x_api_key: Optional[str] = Header(None),
) -> JSONResponse:
    authorize_api_or_user(request, x_api_key)
    # Delete stays OWNER-ONLY (admins excepted) no matter HOW you authenticate.
    # Security fix: a PER-USER API key used to skip this gate entirely, so a
    # non-admin who was merely granted VIEW of another account could delete
    # that account's sessions by sending their own key. Now:
    #   - firmware/master key (API_KEY): device maintenance — allowed;
    #   - per-user key: only dirs that key's OWNER could web-delete
    #     (own dir, or anyone's if the key belongs to an admin);
    #   - web session: gate_delete_dir (owner or admin), as before.
    if x_api_key and x_api_key == API_KEY:
        pass
    elif x_api_key and email_for_api_key(x_api_key):
        key_email = str(email_for_api_key(x_api_key) or "").lower()
        if not can_delete_dir(key_email, safe_name(user)):
            raise HTTPException(status_code=403,
                                detail="you can only delete your own sessions")
    else:
        gate_delete_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    size = p.stat().st_size
    rel = str(p.relative_to(DATA_DIR))
    session_name = p.name
    p.unlink()
    try:
        p.parent.rmdir()  # tidy empty per-user directory
    except OSError:
        pass
    # Cascade: a deleted session takes its entire AI Q&A history with it.
    _ai_history_delete_file(user, session_name)
    log.info("deleted session %s bytes=%d (+ai history)", rel, size)
    return JSONResponse({"ok": True, "deleted": rel, "bytes": size})


@app.post("/sessions/{user}/{filename}/delete")
async def delete_session_form(
    request: Request,
    user: str,
    filename: str,
    x_api_key: Optional[str] = Header(None),
) -> JSONResponse:
    # Convenience alias for clients that can't send DELETE.
    return await delete_session(request, user, filename, x_api_key)


@app.post("/sessions/combine")
async def combine_sessions(request: Request) -> JSONResponse:
    """Merge 2+ session files (same user) into ONE new session file.

    Body: {"user": "<dir>", "files": ["<f1>.ndjson", "<f2>.ndjson", ...]}

    Files are concatenated in session-id (epoch) order — each file's lines are
    already time-ordered and carry absolute "t" timestamps, so plain
    concatenation yields a valid, monotonic combined session. The originals are
    left untouched; the result is a new "<sid>_<track>-combined.ndjson" (sid +
    track from the earliest file). Owner-or-admin only (writes into the user's
    dir — same rule as delete: view grants don't let you modify).
    """
    require_web_user(request)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="json body required")
    user = safe_name(str(body.get("user", "")))
    files = body.get("files")
    if not user or not isinstance(files, list) or len(files) < 2:
        raise HTTPException(status_code=400, detail="need user + at least 2 files")
    gate_delete_dir(request, user)
    paths = [_resolve_session(user, str(f)) for f in files]

    def _sid(p: pathlib.Path) -> int:
        m = re.match(r"^(\d+)_", p.name)
        return int(m.group(1)) if m else 0

    paths.sort(key=lambda p: (_sid(p), p.name))
    first = paths[0].name
    track = first[:-len(".ndjson")] if first.endswith(".ndjson") else first
    track = re.sub(r"^\d+_", "", track) or "UNKNOWN"
    sid = _sid(paths[0]) or int(time.time())
    base = f"{sid}_{track}-combined"
    out = DATA_DIR / "sessions" / user / (base + ".ndjson")
    n = 2
    while out.exists():
        out = DATA_DIR / "sessions" / user / f"{base}{n}.ndjson"
        n += 1
    total = 0
    with open(out, "wb") as w:
        for p in paths:
            last = b""
            with open(p, "rb") as f2:
                while True:
                    chunk = f2.read(1 << 20)
                    if not chunk:
                        break
                    w.write(chunk)
                    total += len(chunk)
                    last = chunk
            if last and not last.endswith(b"\n"):
                w.write(b"\n")
                total += 1
    log.info("combined %d sessions -> %s (%d bytes) for %s",
             len(paths), out.name, total, user)
    return JSONResponse({"ok": True, "filename": out.name,
                         "bytes": total, "files": len(paths)})


@app.get("/admin/sessions/targets")
async def admin_session_targets(request: Request) -> JSONResponse:
    """Users to offer as reassignment targets, alphabetical.

    - **Admin / view-all**: EVERYONE — all known accounts (admins + allowlist +
      managed) as emails, unioned with every user dir that holds sessions
      (orphan owners shown by slug), deduped.
    - **Regular user**: only the users they're allowed to view (their own dir +
      any can_view grants).

    (The reassign UI itself is admin-only, but the list honors view scope so it
    can be reused elsewhere without leaking who exists.)
    """
    web_user = require_web_user(request)
    viewer = str((web_user or {}).get("email", ""))
    root = DATA_DIR / "sessions"

    # Non-admin: restrict to the dirs this account may view.
    if oauth_enabled() and not user_sees_all(viewer):
        allowed = visible_dirnames_for(viewer)   # set of slugs, or None (=all)
        out = set()
        if viewer:
            out.add(viewer)
        if root.exists():
            for d in sorted(root.iterdir()):
                if d.is_dir() and any(d.iterdir()) and (allowed is None or d.name in allowed):
                    out.add(d.name)
        return JSONResponse({"targets": sorted(out, key=str.lower)})

    # Admin / view-all / dev-mode: everyone.
    emails = set(allowed_emails())
    known_slugs = {safe_name(e) for e in emails}
    if root.exists():
        for d in sorted(root.iterdir()):
            if d.is_dir() and d.name not in known_slugs and any(d.iterdir()):
                emails.add(d.name)   # orphan owner: a valid move target by slug
    return JSONResponse({"targets": sorted(emails, key=str.lower)})


@app.post("/admin/sessions/move")
async def admin_move_session(request: Request) -> JSONResponse:
    """Reassign a session (and its AI history) to another user. Admin only.

    Body JSON: {user, filename, target}  where `user`/`filename` identify the
    source session (same as the URL params) and `target` is the destination
    account EMAIL. Moves the .ndjson into the target's sessions dir and moves
    the matching ai_history file alongside it. Refuses if the target already
    has a session with the same name (so nothing is silently overwritten).
    """
    require_admin(request)
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    user = str(body.get("user", ""))
    filename = str(body.get("filename", ""))
    target = str(body.get("target", "")).strip().lower()
    if not target:
        raise HTTPException(status_code=400, detail="target (email) required")

    src = _resolve_session(user, filename)     # 404 if it doesn't exist
    dst_dir = session_dir_for(target)          # creates the target dir; slug = safe_name(target)
    dst = dst_dir / src.name
    if dst.resolve() == src.resolve():
        raise HTTPException(status_code=400, detail="source and target are the same user")
    if dst.exists():
        raise HTTPException(status_code=409,
                            detail=f"target already has a session named {src.name}")

    shutil.move(str(src), str(dst))
    # Move the AI Q&A history alongside the session (best-effort).
    src_hist = _ai_history_path(user, src.name)
    dst_hist = _ai_history_path(target, src.name)
    if src_hist.exists():
        dst_hist.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src_hist), str(dst_hist))
    # Tidy now-empty source dirs.
    for d in (src.parent, src_hist.parent):
        try:
            d.rmdir()
        except OSError:
            pass
    new_user = safe_name(target)
    log.info("admin moved session %s: %s -> %s", src.name, safe_name(user), new_user)
    return JSONResponse({
        "ok": True,
        "user": new_user,
        "filename": src.name,
        "review": f"/review/{new_user}/{src.name}",
    })


@app.get("/sessions/{user}/{filename}/data")
async def session_data(
    request: Request,
    user: str,
    filename: str,
    stride: int = Query(1, ge=1, le=100),
    target: int = Query(0, ge=0, le=200000),
) -> JSONResponse:
    """Parsed NDJSON for the review UI.

    The dash logs at 25 Hz, so a one-hour session is ~90 000 samples — shipping
    all of them as JSON is slow to serialize, transfer, and parse in the browser.
    The map/playback only needs ~10k points to look smooth, so the review UI
    passes `target` (desired sample count) and the server auto-picks a stride to
    hit it. Crucially this is a TWO-PASS read: pass 1 just COUNTS lines (no JSON
    parse), pass 2 parses only the ~target kept lines — so a 200 MB file costs
    ~target parses instead of millions. `stride` is still honored when `target`
    is 0 (back-compat / explicit control).

    Response shape:
        { "count": N, "total": M, "stride": S,
          "samples": [ {t, lat, lon, speed_mph, ...}, ... ],
          "bounds": [[minLat,minLon],[maxLat,maxLon]] | null }
    """
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)

    return JSONResponse(_session_data_payload(p, stride, target))


def _session_data_payload(p: pathlib.Path, stride: int, target: int) -> dict:
    """Core of /data — shared by the authenticated route and the public
    /shared/<token>/data route (view-only overlay links)."""
    total = 0
    eff_stride = max(1, stride)
    if target > 0:
        # Pass 1: count non-empty lines without parsing JSON (cheap).
        with open(p, "rb") as f:
            for raw in f:
                if raw.strip():
                    total += 1
        if total > target:
            eff_stride = math.ceil(total / target)

    samples: list[dict] = []
    min_lat = min_lon = float("inf")
    max_lat = max_lon = float("-inf")
    has_geo = False
    j = -1  # index over non-empty lines only
    with open(p, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            j += 1
            if j % eff_stride != 0:
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
    if target <= 0:
        total = j + 1
    bounds = (
        [[min_lat, min_lon], [max_lat, max_lon]] if has_geo else None
    )
    return {"count": len(samples), "total": total, "stride": eff_stride,
            "bounds": bounds, "samples": samples}


# ---------------------------------------------------------------------------
# Lap exclusion. A spurious S/F crossing (GPS jitter on the line) can log a
# garbage 0.06 s "lap" that poisons best-lap, deltas, PRED references and the
# AI metrics. Laps can be EXCLUDED: automatically (< 10 s = physically
# impossible) or manually from the review page. Exclusions live in a sidecar
# (/data/lap_meta/<user>/<file>.json: {"excluded":[n..], "included":[n..]});
# "included" whitelists a lap the auto rule would have dropped. Excluded laps
# keep their ORIGINAL numbers and are returned separately so the UI can show
# a restore control; /laps consumers (review, overlay, shared) all get the
# filtered view.
# ---------------------------------------------------------------------------
LAP_AUTO_EXCLUDE_UNDER_S = 10.0


def _lap_meta_path(user: str, session_name: str) -> pathlib.Path:
    return LAP_META_DIR / safe_name(user) / (safe_name(session_name) + ".json")


def _lap_meta(user: str, session_name: str) -> dict:
    p = _lap_meta_path(user, session_name)
    if p.exists():
        try:
            d = json.loads(p.read_text("utf-8"))
            return {"excluded": [int(x) for x in d.get("excluded", [])],
                    "included": [int(x) for x in d.get("included", [])]}
        except Exception:
            pass
    return {"excluded": [], "included": []}


def _apply_lap_meta(payload: dict, user: str, session_name: str) -> dict:
    laps = payload.get("laps") or []
    meta = _lap_meta(user, session_name)
    excl = set(meta["excluded"])
    incl = set(meta["included"])
    kept: list = []
    dropped: list = []
    for lp in laps:
        n = int(lp.get("lap", 0))
        auto = (float(lp.get("seconds", 0)) < LAP_AUTO_EXCLUDE_UNDER_S) and (n not in incl)
        if n in excl or auto:
            lp = dict(lp)
            lp["excluded_reason"] = "manual" if n in excl else "auto (<10s)"
            dropped.append(lp)
        else:
            kept.append(lp)
    best = None
    best_s = float("inf")
    for lp in kept:
        if lp["seconds"] < best_s:
            best_s = lp["seconds"]
            best = lp["lap"]
    payload["laps"] = kept
    payload["excluded_laps"] = dropped
    payload["best_lap"] = best
    return payload


# ---------------------------------------------------------------------------
# Best-lap summary cache for the sessions list.
#
# The list wants one number per session ("which one do I want to open?"), but
# computing it means reading the whole NDJSON and running lap detection — a few
# seconds on a 27 MB file. So: cache PER FILE, keyed on (mtime, size) on disk
# AND in memory, and never block the list request on a cold file. The list
# renders whatever is already cached and the page fills the rest in through
# /laps/summary, which computes a few at a time off the event loop.
# ---------------------------------------------------------------------------
LAPCACHE_DIR = DATA_DIR / "lapcache"
_LAP_SUMMARY_MEM: dict = {}          # (name, mtime, size) -> summary
_LAP_SUMMARY_LOCK = threading.Lock()
_LAP_SUMMARY_MAX_PER_CALL = 4        # uncached files one /laps/summary may do


def _lap_summary_cached(user: str, p: pathlib.Path) -> Optional[dict]:
    """Cached summary if (mtime, size) still match, else None."""
    try:
        st = p.stat()
    except OSError:
        return None
    key = (str(p), int(st.st_mtime), int(st.st_size))
    with _LAP_SUMMARY_LOCK:
        hit = _LAP_SUMMARY_MEM.get(key)
    if hit:
        return hit
    cp = LAPCACHE_DIR / safe_name(user) / (p.name + ".json")
    try:
        if cp.is_file():
            d = json.loads(cp.read_text("utf-8"))
            if d.get("mtime") == int(st.st_mtime) and d.get("size") == int(st.st_size):
                with _LAP_SUMMARY_LOCK:
                    _LAP_SUMMARY_MEM[key] = d
                return d
    except Exception:
        pass
    return None


def _lap_summary(user: str, p: pathlib.Path, force: bool = False) -> dict:
    """Compute (+ cache) the best lap of one session. Blocking: call it from a
    worker thread, never from the event loop."""
    if not force:
        hit = _lap_summary_cached(user, p)
        if hit:
            return hit
    st = p.stat()
    summary = {"mtime": int(st.st_mtime), "size": int(st.st_size),
               "computed": int(time.time()), "best_s": None, "best_lap": None,
               "laps": 0, "excluded": 0, "source": None, "error": None}
    try:
        payload = _apply_lap_meta(_laps_payload(p), user, p.name)
        best = payload.get("best_lap")
        laps = payload.get("laps") or []
        secs = None
        for lp in laps:
            if lp.get("lap") == best:
                secs = float(lp.get("seconds") or 0) or None
        summary.update(best_s=secs, best_lap=best, laps=len(laps),
                       excluded=len(payload.get("excluded_laps") or []),
                       source=payload.get("source"))
    except Exception as e:
        summary["error"] = str(e)[:200]
    cp = LAPCACHE_DIR / safe_name(user) / (p.name + ".json")
    try:
        cp.parent.mkdir(parents=True, exist_ok=True)
        tmp = cp.with_suffix(".tmp")
        tmp.write_text(json.dumps(summary), "utf-8")
        tmp.replace(cp)
    except OSError:
        pass
    with _LAP_SUMMARY_LOCK:
        _LAP_SUMMARY_MEM[(str(p), summary["mtime"], summary["size"])] = summary
    return summary


def _fmt_lap_s(secs) -> str:
    if not secs or not math.isfinite(float(secs)):
        return "—"
    secs = float(secs)
    m = int(secs // 60)
    r = secs - m * 60
    return (f"{m}:" + ("0" if r < 10 else "") + f"{r:.2f}") if m else f"{r:.2f}"


@app.get("/laps/summary")
async def laps_summary(request: Request, files: str = Query("")):
    """Best lap for a list of "user/filename" pairs (the sessions list fills its
    own cells with this). Cached summaries come back immediately; at most
    _LAP_SUMMARY_MAX_PER_CALL cold files are computed, off the event loop, and
    the page simply asks again for the rest."""
    require_web_user(request)
    out: dict = {}
    pending: list = []
    budget = _LAP_SUMMARY_MAX_PER_CALL
    for item in [x for x in (files or "").split(",") if x.strip()][:400]:
        if "/" not in item:
            continue
        u, _, fn = item.partition("/")
        u = safe_name(u)
        if oauth_enabled() and not can_view_dir(str((current_user(request) or {}).get("email", "")), u):
            continue
        try:
            p = _resolve_session(u, fn)
        except HTTPException:
            continue
        hit = _lap_summary_cached(u, p)
        if hit is not None:
            out[f"{u}/{fn}"] = hit
        elif budget > 0:
            budget -= 1
            pending.append((f"{u}/{fn}", u, p))
    if pending:
        loop = asyncio.get_running_loop()
        results = await asyncio.gather(
            *[loop.run_in_executor(None, _lap_summary, u, p) for _, u, p in pending])
        for (key, _, _), res in zip(pending, results):
            out[key] = res
    return JSONResponse({"ok": True, "laps": out})


def _laps_payload(p: pathlib.Path) -> dict:
    """Core of /laps — shared by the authenticated route and /shared/<token>/laps."""
    samples: list = []
    with open(p, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                samples.append(json.loads(raw))
            except Exception:
                continue
    return _detect_laps(samples)


@app.get("/sessions/{user}/{filename}/laps")
async def session_laps(request: Request, user: str, filename: str) -> JSONResponse:
    """Auto-detected laps for the review UI.

    Reads the session at full resolution (lap timing wants every fix, not the
    strided set the chart uses), auto-detects the start/finish line from the
    GPS trace, and returns per-lap times + the fastest lap. Lap boundaries are
    given as seconds-from-session-start so the client can map them onto its
    own sample array regardless of stride.
    """
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    return JSONResponse(_apply_lap_meta(_laps_payload(p), safe_name(user), p.name))


@app.post("/sessions/{user}/{filename}/laps/exclude")
async def set_lap_exclusion(request: Request, user: str, filename: str) -> JSONResponse:
    """Exclude / restore a lap. Body: {"lap": N, "exclude": true|false}.
    Restoring an auto-excluded (<10 s) lap whitelists it. Owner-or-admin."""
    require_web_user(request)
    gate_delete_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    try:
        body = await request.json()
        lap = int(body["lap"])
        exclude = bool(body.get("exclude", True))
    except Exception:
        raise HTTPException(status_code=400, detail="need {lap:int, exclude:bool}")
    u = safe_name(user)
    meta = _lap_meta(u, p.name)
    excl = set(meta["excluded"])
    incl = set(meta["included"])
    if exclude:
        excl.add(lap)
        incl.discard(lap)
    else:
        excl.discard(lap)
        incl.add(lap)   # whitelist so the auto rule can't re-drop it
    mp = _lap_meta_path(u, p.name)
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"excluded": sorted(excl), "included": sorted(incl)}), "utf-8")
    log.info("lap exclusion %s/%s lap=%d exclude=%s", u, p.name, lap, exclude)
    return JSONResponse(_apply_lap_meta(_laps_payload(p), u, p.name))


@app.get("/sessions/{user}/{filename}/gpsdiag")
async def session_gpsdiag(request: Request, user: str, filename: str) -> JSONResponse:
    """GPS-health diagnostics for a session (to debug 'GPS goes stale').
    Open this URL while logged in and paste the JSON. Reports fix distribution,
    frozen-position (stale) runs, inter-sample time gaps, and time-to-first-fix."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    ts = []; fixes = {}; lat = lon = None
    n = 0; first_fix_t = None; t0 = None
    stale_runs = []          # (start_t, dur_s, samples) for frozen-position runs
    cur_start = None; cur_n = 0; last_t = None
    gaps = []                # (t, dt) for dt > 0.5 s
    prev_lat = prev_lon = None
    with open(p, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            n += 1
            t = o.get("t")
            if not isinstance(t, (int, float)):
                tm = o.get("t_ms")
                t = (tm / 1000.0) if isinstance(tm, (int, float)) else None
            if t0 is None and t is not None:
                t0 = t
            rt = (t - t0) if (t is not None and t0 is not None) else None
            fx = o.get("fix")
            fixes[str(fx)] = fixes.get(str(fx), 0) + 1
            if first_fix_t is None and isinstance(fx, int) and fx >= 2 and rt is not None:
                first_fix_t = round(rt, 1)
            la, lo = o.get("lat"), o.get("lon")
            # time gap
            if last_t is not None and rt is not None and (rt - last_t) > 0.5:
                gaps.append([round(last_t, 1), round(rt - last_t, 2)])
            if rt is not None:
                last_t = rt
            # frozen-position run (identical lat/lon = stale)
            frozen = (la == prev_lat and lo == prev_lon and la is not None)
            if frozen:
                if cur_start is None:
                    cur_start = last_t; cur_n = 1
                else:
                    cur_n += 1
            else:
                if cur_start is not None and cur_n >= 3:
                    stale_runs.append([round(cur_start, 1),
                                       round((last_t or cur_start) - cur_start, 1), cur_n])
                cur_start = None; cur_n = 0
            prev_lat, prev_lon = la, lo
    if cur_start is not None and cur_n >= 3:
        stale_runs.append([round(cur_start, 1), round((last_t or cur_start) - cur_start, 1), cur_n])
    stale_runs.sort(key=lambda r: r[1], reverse=True)
    total_stale = round(sum(r[1] for r in stale_runs), 1)
    gaps.sort(key=lambda g: g[1], reverse=True)
    return JSONResponse({
        "samples": n,
        "duration_s": round(last_t, 1) if last_t else 0,
        "fix_histogram": fixes,
        "time_to_first_fix_s": first_fix_t,
        "stale_runs_count": len(stale_runs),
        "stale_seconds_total": total_stale,
        "longest_stale_runs": stale_runs[:10],   # [start_s, dur_s, n_samples]
        "biggest_time_gaps": gaps[:10],          # [at_s, gap_s]
        "note": "stale_run = consecutive samples with an identical frozen lat/lon",
    })


def _debug_path_for(user: str, session_filename: str) -> pathlib.Path:
    base = safe_name(session_filename, maxlen=256)
    if base.endswith(".ndjson"):
        base = base[: -len(".ndjson")]
    if base.endswith(".dbg"):
        base = base[: -len(".dbg")]
    return DATA_DIR / "debug" / safe_name(user) / (base + ".dbg.ndjson")


def _debug_diagnose(rows: list) -> list:
    """Turn the Teensy health lines into a plain-english verdict on GPS loss."""
    out = []
    zero = [r for r in rows if r.get("fresh") == 0]
    if not rows:
        return ["no health lines in the debug log"]
    if not zero:
        out.append("GPS produced a fresh fix every second — no stalls in this session.")
        return out
    # Classify each zero-PVT second by what else was happening.
    n_backlog = sum(1 for r in zero if (r.get("avail") or 0) > 400)
    n_nodata  = sum(1 for r in zero if (r.get("avail") or 0) <= 400)
    n_loop    = sum(1 for r in zero if (r.get("loop_ms") or 0) > 300)
    max_loop  = max((r.get("loop_ms") or 0) for r in rows)
    max_sdwr  = max((r.get("sdwr_ms") or 0) for r in rows)
    out.append(f"{len(zero)} second(s) had ZERO fresh GPS fixes.")
    if n_loop:
        out.append(f"CODE/SD: {n_loop} of those had a loop stall >300 ms — the loop "
                   f"blocked (worst loop {max_loop} ms, worst SD write {max_sdwr} ms), "
                   f"starving the GPS UART. Fix is on the Teensy (SD latency / loop).")
    if n_backlog:
        out.append(f"CODE/PARSER: {n_backlog} had GPS bytes BACKLOGGED (avail>400) but no "
                   f"parsed fix — data was arriving, the parser wasn't consuming it.")
    if n_nodata:
        out.append(f"PHYSICAL/MODULE: {n_nodata} had NO backlog and no fix — the module sent "
                   f"nothing (wiring, power, antenna, or baud too low for the nav rate).")
    return out


@app.get("/sessions/{user}/{filename}/debug")
async def session_debug(request: Request, user: str, filename: str) -> JSONResponse:
    """Parsed summary + verdict from the Teensy's on-SD debug log for a session
    (companion .dbg.ndjson uploaded alongside it). Open logged-in and read the
    'diagnosis'. 404 if the session predates the debug logger / wasn't a cloud rec."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _debug_path_for(user, filename)
    if not p.exists():
        raise HTTPException(status_code=404, detail="no debug log for this session")
    rows = []; events = []
    for line in p.read_text("utf-8", "replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        if o.get("ev") == "h":
            rows.append(o)
        else:
            events.append(o)

    def mx(k):
        vals = [r.get(k) for r in rows if isinstance(r.get(k), (int, float))]
        return max(vals) if vals else None
    zero_pvt = [r.get("t") for r in rows if r.get("fresh") == 0]
    return JSONResponse({
        "health_lines": len(rows),
        "max_loop_ms": mx("loop_ms"),
        "max_sdwr_ms": mx("sdwr_ms"),
        "max_avail_bytes": mx("avail"),
        "seconds_with_zero_pvt": len(zero_pvt),
        "zero_pvt_at_s": zero_pvt[:60],
        "total_flush_events": sum(r.get("flush", 0) or 0 for r in rows),
        "total_rebegin_events": sum(r.get("rebegin", 0) or 0 for r in rows),
        "events": events[:60],
        "diagnosis": _debug_diagnose(rows),
    })


@app.get("/sessions/{user}/{filename}/debug/raw")
async def session_debug_raw(request: Request, user: str, filename: str) -> Response:
    """Raw text of the on-SD debug log (for eyeballing every health line)."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _debug_path_for(user, filename)
    if not p.exists():
        raise HTTPException(status_code=404, detail="no debug log for this session")
    return Response(content=p.read_text("utf-8", "replace"), media_type="text/plain")


@app.get("/ai/models")
async def ai_models(request: Request) -> JSONResponse:
    """Model catalogue for the review UI's picker + the configured default."""
    require_web_user(request)
    return JSONResponse({
        "enabled": ai_enabled(),
        "default": AI_DEFAULT_MODEL,
        "models": _ai_model_list(),
    })


@app.post("/sessions/{user}/{filename}/ai")
async def session_ai(request: Request, user: str, filename: str) -> JSONResponse:
    """Analyze the telemetry inside a user-drawn track region with the LLM.

    Body JSON: {
        "prompt": "<question>",
        "region": {"points": [[lat,lon], ...]},   # polygon the user circled
        "model": "<optional model id override>"
    }
    Returns {ok, model, metrics, answer, entry, history}. Every Q&A is appended
    to the session's persistent history (deleted when the session is deleted).
    """
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    if not ai_enabled():
        raise HTTPException(status_code=503,
                            detail="AI is not configured (set RACECAR_AI_API_KEY)")
    p = _resolve_session(user, filename)
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    region = body.get("region") or {}
    poly = region.get("points") or []
    if not isinstance(poly, list) or len(poly) < 3:
        raise HTTPException(status_code=400,
                            detail="region.points must be a polygon of >=3 [lat,lon] pairs")
    try:
        poly = [[float(pt[0]), float(pt[1])] for pt in poly]
    except Exception:
        raise HTTPException(status_code=400, detail="region.points malformed")

    samples = []
    with open(p, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                samples.append(json.loads(raw))
            except Exception:
                continue
    metrics = _region_metrics(samples, poly)
    if not metrics.get("laps"):
        raise HTTPException(status_code=422,
                            detail="no lap data fell inside the selected region")
    # Cross-session references (other days, same track, same region) — on by
    # default; body {"refs": false} skips the extra session scans.
    lib = None
    if body.get("refs", True):
        try:
            lib = _lap_library(safe_name(user), p, poly)
        except Exception as e:
            log.warning("lap library failed for %s/%s: %s", user, filename, e)
    question = str(body.get("prompt", "")).strip()
    messages = _region_prompt(metrics, question, lib=lib)
    answer, used_model, usage = _ai_chat(messages, model=body.get("model"))

    entry = {
        "id": secrets.token_hex(8),
        "ts": int(time.time()),
        "question": question,
        "model": used_model,
        "answer": answer,
        "region": {"points": poly},
        "laps": len(metrics.get("laps", [])),
        "points_in_region": metrics.get("points_in_region", 0),
        "refs_faster": len((lib or {}).get("faster", [])),
        "refs_similar": len((lib or {}).get("similar", [])),
        "refs_sessions": (lib or {}).get("sessions_scanned", 0),
    }
    if usage:
        entry["usage"] = usage   # persisted; exposed to ADMIN viewers only
    history = _ai_history_append(user, p.name, entry)
    adm = _req_is_admin(request)
    return JSONResponse({
        "ok": True,
        "model": used_model,
        "metrics": metrics,
        "answer": answer,
        "entry": (entry if adm else {k: v for k, v in entry.items() if k != "usage"}),
        "history": _hist_public(history, adm),
        "refs": {"faster": len((lib or {}).get("faster", [])),
                 "similar": len((lib or {}).get("similar", [])),
                 "sessions": (lib or {}).get("sessions_scanned", 0)},
    })


@app.post("/sessions/{user}/{filename}/lines")
async def session_lines(request: Request, user: str, filename: str) -> JSONResponse:
    """Racing-line data for the /lineview popout. Body {region:{points}}.
    Returns the fastest REAL traverse of the region across all of the user's
    sessions on this track ('ideal' — achievable by construction: somebody
    drove it), the current session's best, and up to 4 further fast references
    — each with a GPS trace + brake/apex/throttle markers + speeds."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    poly = (body.get("region") or {}).get("points") or []
    if not isinstance(poly, list) or len(poly) < 3:
        raise HTTPException(status_code=400,
                            detail="region.points must be a polygon of >=3 [lat,lon] pairs")
    poly = [[float(pt[0]), float(pt[1])] for pt in poly]
    lib = _lap_library(safe_name(user), p, poly)
    ideal, your_best, refs = _rank_lines(lib)
    return JSONResponse({
        "ok": True,
        "track": lib.get("track"),
        "sessions_scanned": lib.get("sessions_scanned", 0),
        "ideal": ideal,
        "your_best": your_best,
        "refs": refs,
        "delta_s": round(your_best["seconds"] - ideal["seconds"], 2),
        "region": {"points": poly},
    })


def _rank_lines(lib: dict):
    """Shared by /lines and /lines/ai: (ideal, your_best, refs[≤4]) from the
    cross-session library. 422s when the region caught no laps."""
    cur = lib.get("current") or []
    if not cur:
        raise HTTPException(status_code=422,
                            detail="no lap data fell inside the selected region")
    your_best = min(cur, key=lambda t: t["seconds"])
    pool = cur + lib.get("faster", []) + lib.get("similar", [])
    seen = set()
    uniq = []
    for t in sorted(pool, key=lambda t: t["seconds"]):
        k = (t["session"], t["lap"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(t)
    ideal = uniq[0]
    refs = [t for t in uniq
            if (t["session"], t["lap"]) not in
               {(ideal["session"], ideal["lap"]), (your_best["session"], your_best["lap"])}][:4]
    return ideal, your_best, refs


def _line_row(t: dict) -> str:
    b = t.get("brake") or {}
    return " | ".join(str(v if v is not None else "-") for v in (
        t["label"], t["seconds"], t["entry_mph"], t["min_mph"], t["exit_mph"],
        b.get("mph", "-"), b.get("dist_to_apex_m", "-")))


@app.post("/sessions/{user}/{filename}/lines/ai")
async def session_lines_ai(request: Request, user: str, filename: str) -> JSONResponse:
    """AI commentary on the /lineview racing line: the geometry (fastest real
    traverse vs the driver's best) is computed HERE from data — the AI is then
    asked to interpret it (what the ideal does differently, concrete actions).
    Body {region:{points}, model?}. Appended to the session's AI history."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    if not ai_enabled():
        raise HTTPException(status_code=503,
                            detail="AI is not configured (set RACECAR_AI_API_KEY)")
    p = _resolve_session(user, filename)
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    poly = (body.get("region") or {}).get("points") or []
    if not isinstance(poly, list) or len(poly) < 3:
        raise HTTPException(status_code=400,
                            detail="region.points must be a polygon of >=3 [lat,lon] pairs")
    poly = [[float(pt[0]), float(pt[1])] for pt in poly]
    lib = _lap_library(safe_name(user), p, poly)
    ideal, your_best, refs = _rank_lines(lib)
    hdr = "who | time_s | entry_mph | min_mph | exit_mph | brake_mph | brake_m_before_apex"
    rows = [hdr, "IDEAL " + _line_row(ideal), "YOU " + _line_row(your_best)]
    rows += [_line_row(t) for t in refs]
    same = (ideal["session"] == your_best["session"] and ideal["lap"] == your_best["lap"])
    userq = (
        "The driver circled ONE section of the track. Below: the IDEAL line "
        "(the fastest REAL traverse of this section across all their sessions "
        "— achievable, somebody drove it), the driver's best this session, and "
        "further fast references. brake_m_before_apex = metres before the "
        "min-speed point where sustained braking began.\n\n"
        + "\n".join(rows) + "\n\n"
        + ("NOTE: the driver's best IS the ideal here — confirm what they're "
           "doing right and where the remaining margin might be.\n" if same else "")
        + "In <=180 words of clean Markdown: (1) one short paragraph on what "
          "the ideal does differently (braking point, apex speed, exit); "
          "(2) a compact Markdown table IDEAL vs YOU (time, min, exit, brake "
          "distance); (3) 2-4 bullet ACTIONS with concrete numbers."
    )
    system = (
        "You are a professional race engineer. Be concrete and numeric. "
        "Format in clean Markdown with a proper table (header + '---' row)."
    )
    answer, used_model, usage = _ai_chat(
        [{"role": "system", "content": system}, {"role": "user", "content": userq}],
        model=body.get("model"))
    entry = {
        "id": secrets.token_hex(8),
        "ts": int(time.time()),
        "question": "Ideal line — circled section (lineview)",
        "model": used_model,
        "answer": answer,
        "region": {"points": poly},
        "laps": len(lib.get("current", [])),
    }
    if usage:
        entry["usage"] = usage
    _ai_history_append(user, p.name, entry)
    adm = _req_is_admin(request)
    out = {"ok": True, "model": used_model, "answer": answer,
           "delta_s": round(your_best["seconds"] - ideal["seconds"], 2)}
    if adm and usage:
        out["usage"] = usage
    return JSONResponse(out)


@app.get("/lineview/{user}/{filename}", response_class=HTMLResponse)
async def lineview_page(request: Request, user: str, filename: str) -> Response:
    """Popout racing-line visualizer (satellite + fastest real line + brake/
    apex/throttle markers + speed labels vs your line). ?pts=lat,lon|lat,lon…"""
    if oauth_enabled() and not current_user(request):
        return login_redirect(request)
    gate_view_dir(request, safe_name(user))
    _resolve_session(user, filename)
    html_out = (_LINEVIEW_HTML
                .replace("__USER__", json.dumps(user))
                .replace("__FILE__", json.dumps(filename)))
    return HTMLResponse(html_out)


_LINEVIEW_HTML = """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Racing line — racecar-35</title>
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css">
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
  :root{--bg:#0E1014;--surface:#181B22;--line:#2A2F3A;--text:#E6E8EE;--muted:#8A92A3;
        --good:#6CD07A;--warn:#FFB020;--bad:#FF4D4D;--you:#4EA1FF;}
  *{box-sizing:border-box} html,body{margin:0;height:100%;background:var(--bg);color:var(--text);
    font:14px/1.45 Inter,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;}
  #wrap{display:grid;grid-template-columns:1fr 340px;height:100vh}
  #mcol{display:flex;flex-direction:column;height:100vh;min-width:0}
  #map{flex:1 1 auto;min-height:0}
  /* basemap switch — checkbox strip under the map; off = plain black */
  #map.nosat{background:#000}
  #map.nosat .leaflet-control-attribution{display:none}
  .mapopts{display:flex;align-items:center;gap:12px;flex-wrap:wrap;padding:6px 10px;
    border-top:1px solid var(--line);background:var(--surface)}
  .mapchk{display:flex;align-items:center;gap:8px;cursor:pointer;
    font:600 12px Inter,sans-serif;color:var(--text);white-space:nowrap;user-select:none}
  .mapchk input{width:15px;height:15px;margin:0;cursor:pointer;accent-color:var(--warn)}
  .mapchk .hint{color:var(--muted);font-weight:400;font-size:11px}
  .mapopts .sep{width:1px;height:20px;background:var(--line);flex:0 0 auto}
  a.mapbtn{display:inline-flex;align-items:center;background:var(--warn);color:#1A1300;
    text-decoration:none;border-radius:4px;padding:7px 12px;
    font:700 12px Inter,sans-serif;white-space:nowrap;cursor:pointer}
  a.mapbtn:hover{filter:brightness(1.08)}
  aside{padding:14px;overflow-y:auto;border-left:1px solid var(--line);background:var(--surface)}
  h1{font-size:15px;margin:0 0 8px} .muted{color:var(--muted);font-size:12px}
  .leg{display:flex;align-items:center;gap:8px;margin:6px 0;font-size:13px}
  .sw{width:26px;height:5px;border-radius:2px;flex:0 0 auto}
  .card{background:var(--bg);border:1px solid var(--line);border-radius:6px;padding:10px;margin:10px 0}
  .big{font-size:20px;font-weight:700}
  table{width:100%;border-collapse:collapse;font-size:12px;margin-top:6px}
  td,th{padding:3px 6px;border-bottom:1px solid var(--line);text-align:right}
  th:first-child,td:first-child{text-align:left}
  .spd-lbl{background:rgba(0,0,0,.65);color:#fff;font:600 10px Inter,sans-serif;
    padding:1px 3px;border-radius:3px;white-space:nowrap;border:1px solid rgba(255,255,255,.25)}
  .mk-lbl{background:rgba(0,0,0,.75);font:700 10px Inter,sans-serif;padding:2px 5px;
    border-radius:3px;white-space:nowrap}
  .btn{background:#20242E;color:var(--text);border:1px solid var(--line);border-radius:4px;
    padding:8px 12px;cursor:pointer;font:13px Inter,sans-serif;width:100%}
  .btn:disabled{opacity:.5;cursor:default}
  #aiOut{display:none;margin-top:8px;font-size:13px;line-height:1.5}
  #aiOut p{margin:6px 0}
  #aiOut h2{font-size:14px;margin:10px 0 4px;color:var(--warn);border-bottom:1px solid var(--line);padding-bottom:3px}
  #aiOut h3{font-size:13px;margin:8px 0 3px;color:var(--warn)}
  #aiOut ul,#aiOut ol{margin:6px 0;padding-left:20px}
  #aiOut li{margin:2px 0}
  #aiOut table{border-collapse:collapse;margin:8px 0;font:12px ui-monospace,Menlo,monospace}
  #aiOut th{background:var(--bg);color:var(--warn);font-weight:700;text-align:left;
    padding:4px 9px;border:1px solid var(--line);border-bottom:2px solid var(--warn);white-space:nowrap}
  #aiOut td{padding:4px 9px;border:1px solid var(--line)}
  #aiOut td.num{text-align:right;font-variant-numeric:tabular-nums}
  #aiOut tbody tr:nth-child(even) td{background:rgba(255,255,255,.03)}
  #aiCost{color:var(--muted);font-size:11px;margin-top:4px}
</style></head><body>
<div id="wrap">
  <div id="mcol">
    <div id="map"></div>
    <div id="mapopts" class="mapopts">
      <label class="mapchk" for="opt-sat">
        <input type="checkbox" id="opt-sat">
        <span>Satellite view</span>
      </label>
      <span class="sep"></span>
      <a id="map3d" class="mapbtn" target="_blank" href="#"
         title="first-person 3D drive view of this section over real terrain">\u25b6 3D drive view</a>
    </div>
  </div>
  <aside>
    <h1>Racing line — circled section</h1>
    <div class="muted" id="status">loading…</div>
    <div class="card" id="summary" style="display:none">
      <div class="big" id="delta"></div>
      <div class="muted" id="deltaSub"></div>
    </div>
    <div class="leg"><div class="sw" style="background:var(--good)"></div>ideal (fastest real lap through here)</div>
    <div class="leg"><div class="sw" style="background:var(--you)"></div>your best this session</div>
    <div class="leg"><div class="sw" style="background:#777"></div>other fast references</div>
    <div class="leg"><div style="width:12px;height:12px;border-radius:50%;background:var(--bad)"></div>brake point</div>
    <div class="leg"><div style="width:12px;height:12px;border-radius:50%;background:var(--warn)"></div>apex (min speed)</div>
    <div class="leg"><div style="width:12px;height:12px;border-radius:50%;background:var(--good)"></div>back to throttle</div>
    <div class="card"><table id="tbl"><thead><tr>
      <th>lap</th><th>time</th><th>entry</th><th>min</th><th>exit</th><th>brake m</th>
    </tr></thead><tbody></tbody></table>
    <div class="muted" id="scanned" style="margin-top:6px"></div></div>
    <div class="muted">Speed labels are mph along each line. “brake m” = metres before the
    apex where sustained braking began. The ideal line is a REAL lap — someone (you) drove
    it, so it's achievable.</div>
    <div class="card">
      <button id="aiBtn" class="btn">AI: analyze this line</button>
      <div id="aiOut"></div>
      <div id="aiCost"></div>
    </div>
  </aside>
</div>
<script>
(function(){
  const USER=__USER__, FILE=__FILE__;
  const q=new URLSearchParams(location.search);
  const pts=(q.get('pts')||'').split('|').map(s=>s.split(',').map(Number)).filter(a=>a.length===2&&isFinite(a[0])&&isFinite(a[1]));
  const map=L.map('map');
  // ---- basemap on/off: satellite tiles, or nothing but black. ------------
  // The checkbox sits directly under the map and the choice is remembered per
  // browser (same localStorage key as the review page). Tiles are additive, so
  // toggling never disturbs the ideal-line / reference / marker overlays.
  const map_el=document.getElementById('map');
  const sat_chk=document.getElementById('opt-sat');
  // "3D drive view" keeps the circled section: carry ?pts= straight through.
  (function(){
    var m3=document.getElementById('map3d');
    if(!m3) return;
    var raw=q.get('pts');
    m3.href='/track3d/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+
            (raw?('?pts='+encodeURIComponent(raw)):'');
  })();
  let sat_layer=null;
  function applySat(on){
    if(on&&!sat_layer){
      sat_layer=L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
        {maxZoom:20, attribution:'Imagery \u00a9 Esri'}).addTo(map);
    } else if(!on&&sat_layer){ map.removeLayer(sat_layer); sat_layer=null; }
    map_el.classList.toggle('nosat',!on);
    if(sat_chk) sat_chk.checked=!!on;
    try{ localStorage.setItem('rc5.sat', on?'1':'0'); }catch(e){}
  }
  let sat_on=true;
  try{ sat_on=localStorage.getItem('rc5.sat')!=='0'; }catch(e){}
  if(sat_chk) sat_chk.addEventListener('change',function(){ applySat(sat_chk.checked); });
  applySat(sat_on);
  const status=document.getElementById('status');
  if(pts.length<3){ status.textContent='no region — open this from the review page (circle a section → ideal line)'; map.setView([39,-77],5); return; }
  L.polygon(pts,{color:'#6CD07A',weight:1,fillOpacity:0.06,dashArray:'4 4'}).addTo(map);

  function speedColor(mph){ // blue slow -> red fast (relative-ish absolute scale)
    const t=Math.max(0,Math.min(1,(mph-30)/90));
    const r=Math.round(60+t*195), g=Math.round(120-40*Math.abs(t-0.5)*2+60*(1-t)), b=Math.round(220-200*t);
    return 'rgb('+r+','+Math.max(40,g)+','+b+')';
  }
  function drawTrace(t, color, weight, opacity, withLabels, labelEvery){
    const ll=t.trace.map(p=>[p[0],p[1]]);
    L.polyline(ll,{color:color,weight:weight,opacity:opacity}).addTo(map);
    if(withLabels){
      const step=labelEvery||Math.max(8,Math.floor(t.trace.length/12));
      for(let i=0;i<t.trace.length;i+=step){
        L.marker([t.trace[i][0],t.trace[i][1]],{interactive:false,icon:L.divIcon({className:'',
          html:'<div class="spd-lbl" style="border-color:'+color+'">'+Math.round(t.trace[i][2])+'</div>',
          iconAnchor:[10,-4]})}).addTo(map);
      }
    }
    return ll;
  }
  function marker(pt, color, text){
    if(!pt) return;
    L.circleMarker([pt.lat,pt.lon],{radius:7,color:'#000',weight:1.5,fillColor:color,fillOpacity:1}).addTo(map);
    L.marker([pt.lat,pt.lon],{interactive:false,icon:L.divIcon({className:'',
      html:'<div class="mk-lbl" style="color:'+color+'">'+text+'</div>', iconAnchor:[-10,8]})}).addTo(map);
  }
  fetch('/sessions/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+'/lines',{
    method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({region:{points:pts}})
  }).then(async r=>{
    const j=await r.json();
    if(!r.ok){ status.textContent='error: '+((j&&j.detail)||('HTTP '+r.status)); map.fitBounds(pts); return; }
    status.textContent='';
    for(const t of j.refs) drawTrace(t,'#777',2,0.55,false);
    const sameLap = j.ideal.session===j.your_best.session && j.ideal.lap===j.your_best.lap;
    let yb=null;
    if(!sameLap) yb=drawTrace(j.your_best,'#4EA1FF',3,0.85,true);
    const il=drawTrace(j.ideal,'#6CD07A',5,0.95,true);
    const b=j.ideal.brake;
    marker(b,'#FF4D4D','BRAKE '+(b?Math.round(b.mph):'')+' mph · '+(b?Math.round(b.dist_to_apex_m):'?')+' m → apex');
    marker(j.ideal.apex,'#FFB020','APEX '+Math.round(j.ideal.apex.mph)+' mph');
    marker(j.ideal.throttle,'#6CD07A','THROTTLE '+(j.ideal.throttle?Math.round(j.ideal.throttle.mph):'')+' mph');
    if(!sameLap && j.your_best.brake)
      marker(j.your_best.brake,'#4EA1FF','you brake · '+Math.round(j.your_best.brake.dist_to_apex_m)+' m');
    map.fitBounds(il.concat(yb||[]), {padding:[40,40]});
    const d=document.getElementById('delta'), ds=document.getElementById('deltaSub'),
          sm=document.getElementById('summary');
    sm.style.display='block';
    if(sameLap){ d.textContent='your lap IS the ideal here'; ds.textContent='fastest traverse on record: '+j.ideal.label+' · '+j.ideal.seconds+'s'; }
    else { d.textContent='-'+j.delta_s+'s on offer';
           ds.textContent='ideal: '+j.ideal.label+' ('+j.ideal.seconds+'s) vs your best this session: '+j.your_best.label+' ('+j.your_best.seconds+'s)'; }
    const tb=document.querySelector('#tbl tbody');
    const rows=[['IDEAL '+j.ideal.label,j.ideal],['YOU '+j.your_best.label,j.your_best]]
      .concat(j.refs.map(t=>[t.label,t]));
    for(const [nm,t] of rows){
      const tr=document.createElement('tr');
      tr.innerHTML='<td>'+nm+'</td><td>'+t.seconds+'</td><td>'+Math.round(t.entry_mph)+'</td>'+
        '<td>'+Math.round(t.min_mph)+'</td><td>'+Math.round(t.exit_mph)+'</td>'+
        '<td>'+(t.brake?Math.round(t.brake.dist_to_apex_m):'-')+'</td>';
      tb.appendChild(tr);
    }
    document.getElementById('scanned').textContent=j.sessions_scanned+' sessions scanned on this track';
  }).catch(e=>{ status.textContent='request failed: '+e.message; });

    function mdInline(s){
    return s.replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>')
            .replace(/(^|[^*])\\*([^*\\s][^*]*)\\*/g,'$1<em>$2</em>')
            .replace(/`([^`]+)`/g,'<code>$1</code>');
  }
  function md(t){
    const esc = (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    const L = esc.split(/\\r?\\n/);
    const out = [];
    const isNum = c => /^[-+]?\\$?\\d[\\d,]*\\.?\\d*\\s*(s|ms|mph|m|km|rpm|g|%)?$/i.test(c.trim());
    let i = 0;
    while (i < L.length){
      const ln = L[i];
      if (!ln.trim()){ i++; continue; }
      // table: | a | b | followed by |---|---|
      if (/^\\s*\\|.*\\|\\s*$/.test(ln) && i+1 < L.length && /^\\s*\\|[\\s:|-]+\\|\\s*$/.test(L[i+1])){
        const cells = r => r.trim().replace(/^\\|/,'').replace(/\\|$/,'').split('|').map(c=>c.trim());
        const head = cells(ln);
        const align = cells(L[i+1]).map(c => /^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'right' : '');
        let h = '<table><thead><tr>';
        head.forEach((c,k)=>{ h += '<th'+(align[k]?' style="text-align:'+align[k]+'"':'')+'>'+mdInline(c)+'</th>'; });
        h += '</tr></thead><tbody>';
        i += 2;
        while (i < L.length && /^\\s*\\|.*\\|\\s*$/.test(L[i])){
          h += '<tr>';
          cells(L[i]).forEach((c,k)=>{
            const cls = (align[k]==='right' || (!align[k] && isNum(c))) ? ' class="num"' : '';
            const st  = align[k]==='center' ? ' style="text-align:center"' : '';
            h += '<td'+cls+st+'>'+mdInline(c)+'</td>';
          });
          h += '</tr>'; i++;
        }
        out.push(h+'</tbody></table>');
        continue;
      }
      // heading
      const hm = ln.match(/^(#{1,6})\\s+(.+)$/);
      if (hm){ out.push((hm[1].length<=2?'<h2>':'<h3>')+mdInline(hm[2])+(hm[1].length<=2?'</h2>':'</h3>')); i++; continue; }
      // horizontal rule
      if (/^\\s*(-{3,}|\\*{3,}|_{3,})\\s*$/.test(ln)){ out.push('<hr>'); i++; continue; }
      // list (unordered or ordered)
      if (/^\\s*([-*+]|\\d+[.)])\\s+/.test(ln)){
        const ord = /^\\s*\\d+[.)]/.test(ln);
        let h = ord ? '<ol>' : '<ul>';
        while (i < L.length && /^\\s*([-*+]|\\d+[.)])\\s+/.test(L[i])){
          h += '<li>'+mdInline(L[i].replace(/^\\s*([-*+]|\\d+[.)])\\s+/,''))+'</li>'; i++;
        }
        out.push(h + (ord ? '</ol>' : '</ul>'));
        continue;
      }
      // paragraph: gather until blank/structural line
      let para = [ln];
      i++;
      while (i < L.length && L[i].trim()
             && !/^\\s*\\|.*\\|\\s*$/.test(L[i]) && !/^#{1,6}\\s+/.test(L[i])
             && !/^\\s*([-*+]|\\d+[.)])\\s+/.test(L[i]) && !/^\\s*-{3,}\\s*$/.test(L[i])){
        para.push(L[i]); i++;
      }
      out.push('<p>'+mdInline(para.join(' '))+'</p>');
    }
    return out.join('');
  }
  const aiBtn=document.getElementById('aiBtn'), aiOut=document.getElementById('aiOut'),
        aiCost=document.getElementById('aiCost');
  aiBtn.addEventListener('click', async ()=>{
    aiBtn.disabled=true; aiBtn.textContent='analyzing\u2026';
    try{
      const r=await fetch('/sessions/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+'/lines/ai',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({region:{points:pts}})});
      const j=await r.json();
      if(!r.ok){ aiOut.style.display='block'; aiOut.textContent='error: '+((j&&j.detail)||('HTTP '+r.status)); }
      else{
        aiOut.style.display='block'; aiOut.innerHTML=md(j.answer||'');
        let c='';
        if(j.usage){ if(j.usage.cost_usd!=null)c+='$'+(+j.usage.cost_usd).toFixed(4);
                     if(j.usage.total_tokens)c+=(c?' \u00b7 ':'')+j.usage.total_tokens+' tok'; }
        aiCost.textContent = c ? ('model '+j.model+' \u00b7 '+c) : (j.model?('model '+j.model):'');
      }
    }catch(e){ aiOut.style.display='block'; aiOut.textContent='request failed: '+e.message; }
    aiBtn.disabled=false; aiBtn.textContent='AI: analyze this line';
  });
})();
</script></body></html>"""


@app.get("/sessions/{user}/{filename}/ai/history")
async def session_ai_history(request: Request, user: str, filename: str) -> JSONResponse:
    """Persistent AI Q&A history for this session (newest handling is client-side)."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    return JSONResponse({"history": _hist_public(_ai_history_load(user, p.name),
                                                 _req_is_admin(request))})


@app.post("/sessions/{user}/{filename}/ai/delete")
async def session_ai_delete(request: Request, user: str, filename: str) -> JSONResponse:
    """Delete ONE AI Q&A entry (body {id}). Owner/admin gated like session delete."""
    require_web_user(request)
    gate_delete_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    try:
        body = json.loads((await request.body()).decode("utf-8", "replace") or "{}")
    except Exception:
        raise HTTPException(status_code=400, detail="invalid JSON body")
    eid = str(body.get("id", ""))
    hist = _ai_history_load(user, p.name)
    new = [e for e in hist if e.get("id") != eid]
    path = _ai_history_path(user, p.name)
    if new:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(new), "utf-8")
    else:
        _ai_history_delete_file(user, p.name)
    return JSONResponse({"ok": True, "history": new})


@app.get("/review/{user}/{filename}", response_class=HTMLResponse)
async def review(request: Request, user: str, filename: str) -> Response:
    if oauth_enabled() and not current_user(request):
        return login_redirect(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    when = time.strftime(
        "%Y-%m-%d %H:%M:%S UTC", time.gmtime(display_epoch_for(p))
    )
    return _REVIEW_HTML.replace("__USER__", safe_name(user)) \
                       .replace("__FILE__", p.name) \
                       .replace("__WHEN__", when) \
                       .replace("__MAP_TILES__", json.dumps(MAP_TILES)) \
                       .replace("__MAP_ATTRIB__", json.dumps(MAP_ATTRIB)) \
                       .replace("__MAP_MAXZOOM__", str(MAP_MAXZOOM))


@app.get("/map3d/{user}/{filename}", response_class=HTMLResponse)
async def map3d(request: Request, user: str, filename: str) -> Response:
    """The session in 3D the "map" way: satellite imagery draped over real
    terrain (RACECAR_MAP_DEM) with the driven line painted on the ground, a
    chase camera on the review page's playback clock, and a ?pts= ideal line.

    This is the imagery variant. The DATA-ONLY driver's view is /track3d, which
    is what the review page links to; this one stays reachable because seeing
    the same corner over real ground is occasionally what you want."""
    if oauth_enabled() and not current_user(request):
        return login_redirect(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    return _TRACK3DMAP_HTML.replace("__USER__", safe_name(user)) \
                        .replace("__FILE__", p.name) \
                        .replace("__MAP_TILES__", json.dumps(MAP_TILES)) \
                        .replace("__MAP_ATTRIB__", json.dumps(MAP_ATTRIB)) \
                        .replace("__MAP_MAXZOOM__", str(MAP_MAXZOOM)) \
                        .replace("__MAP_DEM__", json.dumps(MAP_DEM)) \
                        .replace("__MAP_DEM_MAXZOOM__", str(MAP_DEM_MAXZOOM))


@app.get("/track3d/{user}/{filename}", response_class=HTMLResponse)
async def track3d(request: Request, user: str, filename: str) -> Response:
    """First-person DRIVING view built from the data alone — no imagery, no map
    tiles, no terrain service: a road ribbon drawn around the line the car
    actually drove, on the logged altitude, with speed colouring, corner kerbs,
    brake/apex/throttle markers, an S/F gantry and a chase camera at eye height.
    The 25 Hz samples are smoothed and arc-length interpolated so it plays at
    60 fps. Same view gate as /review.

    Client-side only, from /sessions/<u>/<f>/{data,laps} (+ /lines when a lasso
    polygon arrives as ?pts=)."""
    if oauth_enabled() and not current_user(request):
        return login_redirect(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    return _TRACK3D_HTML.replace("__USER__", safe_name(user)) \
                        .replace("__FILE__", p.name)


# ---------------------------------------------------------------------------
# Session <-> YouTube video link (overlay viewer).
# Sidecar json at /data/video_meta/<user>/<file>.json:
#   {"id": "<yt id>", "url": "<as entered>", "offset_ms": <int>}
# offset semantics: data_time_rel_s = video_time_s + offset_ms/1000.
# ---------------------------------------------------------------------------
def _video_meta_path(user: str, session_name: str) -> pathlib.Path:
    return VIDEO_META_DIR / safe_name(user) / (safe_name(session_name) + ".json")


def _parse_youtube_id(s: str) -> Optional[str]:
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"[A-Za-z0-9_-]{8,16}", s):   # raw video id (11 typical)
        return s
    m = re.search(r"(?:[?&]v=|youtu\.be/|/embed/|/shorts/|/live/)([A-Za-z0-9_-]{6,16})", s)
    return m.group(1) if m else None


@app.get("/sessions/{user}/{filename}/video")
async def get_video_link(request: Request, user: str, filename: str) -> JSONResponse:
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    mp = _video_meta_path(user, p.name)
    meta = {}
    if mp.exists():
        try:
            meta = json.loads(mp.read_text("utf-8"))
        except Exception:
            meta = {}
    return JSONResponse({"id": meta.get("id"), "url": meta.get("url", ""),
                         "offset_ms": int(meta.get("offset_ms", 0) or 0)})


@app.post("/sessions/{user}/{filename}/video")
async def set_video_link(request: Request, user: str, filename: str) -> JSONResponse:
    """Link/unlink a YouTube video + store the data<->video sync offset.
    Owner-or-admin (same modify rule as delete). Empty url = unlink."""
    require_web_user(request)
    gate_delete_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=400, detail="json body required")
    url = str(body.get("url", "")).strip()
    offset_ms = int(body.get("offset_ms", 0) or 0)
    mp = _video_meta_path(user, p.name)
    if not url:
        if mp.exists():
            mp.unlink()
        return JSONResponse({"ok": True, "id": None})
    vid = _parse_youtube_id(url)
    if not vid:
        raise HTTPException(status_code=400, detail="couldn't parse a YouTube video id from that link")
    mp.parent.mkdir(parents=True, exist_ok=True)
    mp.write_text(json.dumps({"id": vid, "url": url, "offset_ms": offset_ms}), "utf-8")
    log.info("video link %s/%s -> %s offset=%dms", user, p.name, vid, offset_ms)
    return JSONResponse({"ok": True, "id": vid, "offset_ms": offset_ms})


@app.get("/overlay/{user}/{filename}", response_class=HTMLResponse)
async def overlay(request: Request, user: str, filename: str) -> Response:
    """Full-screen YouTube player + live telemetry HUD (speed / RPM / track
    map / laps) rendered as HTML over the video, driven by video time + the
    saved sync offset. Sync controls on-page (coarse slider + fine nudge +
    one-click 'launch' auto-sync); SAVE persists the offset."""
    if oauth_enabled() and not current_user(request):
        return login_redirect(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    u = safe_name(user)
    return HTMLResponse(_OVERLAY_HTML
                        .replace("__API__", f"/sessions/{u}/{p.name}")
                        .replace("__BACK__", f"/review/{u}/{p.name}")
                        .replace("__FILE__", p.name)
                        .replace("__RO__", "0"))


# ---------------------------------------------------------------------------
# Public view-only share links for the overlay.
#
# An opaque token (secrets.token_urlsafe) maps to {user, filename} via a json
# file in /data/shares/. The /shared/<token>/* routes need NO authentication
# and are ALL read-only GETs: the overlay page they serve hides every sync/
# save control (__RO__=1), and mutation endpoints (/video POST, share create/
# revoke, delete) remain behind the normal owner-or-admin auth — so a leaked
# link can only ever LOOK at this one session. Revoking deletes the token and
# kills the link immediately.
# ---------------------------------------------------------------------------
def _share_lookup(token: str) -> tuple[str, str]:
    tp = SHARE_DIR / (safe_name(token, maxlen=64) + ".json")
    if not tp.exists():
        raise HTTPException(status_code=404, detail="unknown or revoked share link")
    try:
        meta = json.loads(tp.read_text("utf-8"))
        return str(meta["user"]), str(meta["filename"])
    except Exception:
        raise HTTPException(status_code=404, detail="unknown or revoked share link")


def _share_token_for(user: str, filename: str) -> Optional[str]:
    if not SHARE_DIR.exists():
        return None
    for f in SHARE_DIR.glob("*.json"):
        try:
            meta = json.loads(f.read_text("utf-8"))
        except Exception:
            continue
        if meta.get("user") == user and meta.get("filename") == filename:
            return f.stem
    return None


@app.get("/sessions/{user}/{filename}/share")
async def get_share(request: Request, user: str, filename: str) -> JSONResponse:
    """Current share token for a session (owner-or-admin — the token IS access)."""
    require_web_user(request)
    gate_delete_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    tok = _share_token_for(safe_name(user), p.name)
    return JSONResponse({"token": tok, "url": (f"/shared/{tok}" if tok else None)})


@app.post("/sessions/{user}/{filename}/share")
async def create_share(request: Request, user: str, filename: str) -> JSONResponse:
    """Create (idempotent) a public view-only overlay link. Owner-or-admin."""
    require_web_user(request)
    gate_delete_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    u = safe_name(user)
    tok = _share_token_for(u, p.name)
    if not tok:
        tok = secrets.token_urlsafe(16)
        SHARE_DIR.mkdir(parents=True, exist_ok=True)
        (SHARE_DIR / (tok + ".json")).write_text(json.dumps({
            "user": u, "filename": p.name, "created": int(time.time()),
            "by": str((current_user(request) or {}).get("email", "")),
        }), "utf-8")
        log.info("share created %s/%s -> %s", u, p.name, tok)
    return JSONResponse({"token": tok, "url": f"/shared/{tok}"})


@app.post("/sessions/{user}/{filename}/share/revoke")
async def revoke_share(request: Request, user: str, filename: str) -> JSONResponse:
    require_web_user(request)
    gate_delete_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    u = safe_name(user)
    n = 0
    tok = _share_token_for(u, p.name)
    while tok:   # defensive: clear duplicates too
        (SHARE_DIR / (tok + ".json")).unlink(missing_ok=True)
        n += 1
        tok = _share_token_for(u, p.name)
    log.info("share revoked %s/%s (%d token(s))", u, p.name, n)
    return JSONResponse({"ok": True, "revoked": n})


@app.get("/shared/{token}", response_class=HTMLResponse)
async def shared_overlay(token: str) -> Response:
    """PUBLIC read-only overlay viewer. No auth; sync/save UI hidden."""
    user, filename = _share_lookup(token)
    p = _resolve_session(user, filename)
    return HTMLResponse(_OVERLAY_HTML
                        .replace("__API__", f"/shared/{safe_name(token, maxlen=64)}")
                        .replace("__BACK__", "#")
                        .replace("__FILE__", p.name)
                        .replace("__RO__", "1"))


@app.get("/shared/{token}/data")
async def shared_data(
    token: str,
    stride: int = Query(1, ge=1, le=100),
    target: int = Query(0, ge=0, le=200000),
) -> JSONResponse:
    user, filename = _share_lookup(token)
    p = _resolve_session(user, filename)
    return JSONResponse(_session_data_payload(p, stride, target))


@app.get("/shared/{token}/laps")
async def shared_laps(token: str) -> JSONResponse:
    user, filename = _share_lookup(token)
    p = _resolve_session(user, filename)
    return JSONResponse(_apply_lap_meta(_laps_payload(p), safe_name(user), p.name))


@app.get("/shared/{token}/video")
async def shared_video(token: str) -> JSONResponse:
    user, filename = _share_lookup(token)
    p = _resolve_session(user, filename)
    mp = _video_meta_path(user, p.name)
    meta = {}
    if mp.exists():
        try:
            meta = json.loads(mp.read_text("utf-8"))
        except Exception:
            meta = {}
    return JSONResponse({"id": meta.get("id"), "url": "",   # url withheld: id is enough to play
                         "offset_ms": int(meta.get("offset_ms", 0) or 0)})


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

_LOGIN_EXTRA_CSS = """
  body { min-height:100vh; display:grid; place-items:center; }
  .login-card { width:min(520px, calc(100vw - 48px)); background:var(--surface);
    border:1px solid var(--line); border-radius:var(--r-md); padding:var(--sp-xl); }
  .login-card h1 { margin:0 0 var(--sp-sm); }
  .login-card p { color:var(--muted); margin:0 0 var(--sp-lg); }
  .google { width:100%; padding:12px 16px; font-size:12px; }
  code { color:var(--primary); font-family:var(--ff-mono); }
  pre { white-space:pre-wrap; background:var(--bg); border:1px solid var(--line);
    border-radius:var(--r-sm); padding:var(--sp-md); color:var(--muted); }
"""

_LOGIN_HTML = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>sign in \u00b7 racecar-35</title>
{_FONTS_LINK}<style>{_BASE_CSS}{_LOGIN_EXTRA_CSS}</style></head><body>
  <section class="login-card">
    <div class="pill good">Google OAuth</div>
    <h1 class="t-display" style="margin-top:14px">racecar-35 pit wall</h1>
    <p>Sign in with a Google account to review, upload, and delete sessions.</p>
    <a class="btn primary google" href="__AUTH_URL__">sign in with Google</a>
  </section>
</body></html>"""

_LOGIN_DISABLED_HTML = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>OAuth not configured</title>
{_FONTS_LINK}<style>{_BASE_CSS}{_LOGIN_EXTRA_CSS}</style></head><body>
  <section class="login-card">
    <div class="pill">dev open</div>
    <h1 class="t-display" style="margin-top:14px">Google OAuth is not configured</h1>
    <p>The server is currently in open dev mode. To enable Google login, set these in <code>server/.env</code>, sync to <code>/tmp/server</code>, and rebuild.</p>
    <pre>GOOGLE_CLIENT_ID=...
GOOGLE_CLIENT_SECRET=...
GOOGLE_REDIRECT_URI=http://10.1.16.7:8089/auth/google/callback
RACECAR_SESSION_SECRET=make-a-long-random-string
# optional: restrict who can log in
RACECAR_ALLOWED_EMAILS=cm.rawlings@gmail.com</pre>
    <a class="btn primary" href="/">continue in dev mode</a>
  </section>
</body></html>"""

_LOGIN_ERROR_HTML = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>login failed</title>
{_FONTS_LINK}<style>{_BASE_CSS}{_LOGIN_EXTRA_CSS}</style></head><body>
  <section class="login-card">
    <div class="pill" style="color:var(--bad)">login failed</div>
    <h1 class="t-display" style="margin-top:14px">Could not sign in</h1>
    <p>__ERROR__</p>
    <a class="btn primary" href="/login">try again</a>
  </section>
</body></html>"""

_ADMIN_EXTRA_CSS = """
  main { padding: var(--sp-lg); max-width: 1100px; margin: 0 auto; }
  table { width:100%; border-collapse:separate; border-spacing:0;
    background:var(--surface); border:1px solid var(--line);
    border-radius:var(--r-md); overflow:hidden; }
  th,td { padding:12px 14px; font-size:13px; text-align:left;
    border-bottom:1px solid var(--line); }
  th { background:var(--surface-2); color:var(--muted); font-weight:600;
    text-transform:uppercase; letter-spacing:0.08em; font-size:11px; }
  tbody tr:last-child td { border-bottom:none; }
  tbody tr:hover { background: rgba(255,176,32,0.05); }
  .row-actions { display:flex; gap:var(--sp-sm); }
  .panel { background:var(--surface); border:1px solid var(--line);
    border-radius:var(--r-md); padding:var(--sp-md); margin-bottom:var(--sp-lg); }
  .add-grid { display:grid; grid-template-columns:1fr auto auto; gap:var(--sp-md);
    align-items:center; }
  @media (max-width:640px){ .add-grid { grid-template-columns:1fr; } }
  .chk { display:inline-flex; align-items:center; gap:8px; color:var(--muted);
    font:600 11px/1 var(--ff-ui); letter-spacing:0.08em; text-transform:uppercase;
    white-space:nowrap; cursor:pointer; }
  .badge { display:inline-flex; padding:3px 9px; border-radius:var(--r-full);
    font:600 10px/1.4 var(--ff-ui); letter-spacing:0.06em; text-transform:uppercase;
    background:var(--surface-2); color:var(--muted); }
  .badge.admin { background:rgba(255,176,32,0.15); color:var(--primary); }
  .badge.env { background:var(--surface-3); color:var(--muted); }
  .btn.danger { color:var(--bad); }
  .msg { display:none; margin-bottom:var(--sp-md); padding:10px var(--sp-md);
    border-radius:var(--r-sm); font-size:13px; }
  .msg.bad { display:block; background:rgba(255,93,93,0.1); color:var(--bad);
    border:1px solid rgba(255,93,93,0.3); }
"""

_ADMIN_HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>admin \u00b7 racecar-35</title>
""" + _FONTS_LINK + "<style>" + _BASE_CSS + _ADMIN_EXTRA_CSS + """</style></head><body>
<header class="app"><span class="dot"></span><h1>racecar-35 \u00b7 pit wall</h1>
  <span class="crumbs"><a href="/">sessions</a> &rsaquo; admin</span>
  <span style="flex:1"></span>
  <a class="btn" href="/coach">checklist</a>
  <a class="btn" href="/tools/sfpicker">S/F picker</a>
  <button class="btn" id="srvupd" title="git pull + docker compose up -d --build (executed by the host watcher)">update server</button>
  <span class="t-label" id="srvupdmsg" style="margin-right:var(--sp-md)"></span>
  <script>
  (function(){
    var b=document.getElementById('srvupd'), m=document.getElementById('srvupdmsg');
    if(!b) return;
    var poll=null, t0=0, clickedAt=0, DEADLINE=3600000;
    var NOW_CMD='__HINT_NOW__', INST_CMD='__HINT_INSTALL__';
    function fmt(s){ return s||''; }
    function age(s){ return (s===null||s===undefined) ? '' : ' (' + s + 's ago)'; }
    function stuck(j, elapsed){
      // The watcher has NEVER written a status file -> it is not installed.
      // That is the usual reason the button 'never works'.
      if(!j.watcher_ever) return true;
      // A request older than 45 s with the host still idle means nothing picked it up.
      var st=(j.status&&j.status.state)||'';
      if(elapsed>45 && j.pending && st!=='pulling' && st!=='building') return true;
      return false;
    }
    async function tick(){
      try{
        var r=await fetch('/admin/update/status'); var j=await r.json();
        var st=(j.status&&j.status.state)||'', pend=!!j.pending;
        var elapsed=(Date.now()-clickedAt)/1000;
        if(j.running_since && t0 && j.running_since>t0){
          m.style.color='#2e7d32';
          m.textContent='updated \u2713 server restarted';
          b.disabled=false; clearInterval(poll); poll=null; return;
        }
        if(st==='done' && !pend && j.status_age_s!==null && elapsed>j.status_age_s && j.status_age_s<600){
          m.style.color='#2e7d32';
          m.textContent='updated \u2713 host reported done';
          b.disabled=false; clearInterval(poll); poll=null; return;
        }
        if(st==='failed'){
          m.style.color='#c62828';
          m.textContent='update FAILED: '+fmt(j.status&&j.status.detail);
          b.disabled=false; clearInterval(poll); poll=null; return;
        }
        if(stuck(j, elapsed)){
          m.style.color='#c62828';
          m.textContent = 'no host watcher response \u2014 run this ON THE SERVER HOST:  '
            + (j.watcher_ever ? NOW_CMD : INST_CMD);
          b.disabled=false; clearInterval(poll); poll=null; return;
        }
        if(poll && Date.now()-clickedAt>DEADLINE){
          m.style.color='#c62828';
          m.textContent='still no response after 60 min \u2014 run: '+NOW_CMD;
          b.disabled=false; clearInterval(poll); poll=null; return;
        }
        m.style.color='';
        m.textContent = pend
          ? ('queued\u2026 waiting for host watcher' + (st?(' (host: '+fmt(st)+')'):''))
          : (st ? ('host: '+fmt(st)+age(j.status_age_s)) : 'queued\u2026');
      }catch(e){}
    }
    b.addEventListener('click', async function(){
      if(!confirm('Update the server?\\n\\ngit pull + docker compose up -d --build\\nThe site will restart.')) return;
      b.disabled=true; m.style.color=''; m.textContent='requesting\u2026';
      try{
        var s=await (await fetch('/admin/update/status')).json();
        t0=s.running_since||0;
        var r=await fetch('/admin/update',{method:'POST'});
        var j=await r.json();
        if(!r.ok){ m.style.color='#c62828'; m.textContent='error: '+((j&&j.detail)||r.status); b.disabled=false; return; }
        clickedAt=Date.now();
        m.textContent='queued\u2026';
        if(!poll) poll=setInterval(tick,3000);
        tick();
      }catch(e){ m.style.color='#c62828'; m.textContent='failed: '+e.message; b.disabled=false; }
    });
    // On load, if a request is already pending, say plainly whether the host has
    // ever answered instead of showing an open-ended spinner.
    tick();
  })();
  </script>
  <a class="btn" href="/admin/report" style="margin-right:var(--sp-md)">report</a>
  <a class="btn" href="/admin/canbus" style="margin-right:var(--sp-md)">CAN captures</a>__USER_CHIP__</header>
<main>
  <div id="msg" class="msg"></div>
  <section class="panel">
    <div class="t-label" style="margin-bottom:var(--sp-md)">Add authorized account</div>
    <div class="add-grid">
      <input id="newEmail" type="text" placeholder="name@gmail.com" autocomplete="off" spellcheck="false">
      <label class="chk"><input id="newAdmin" type="checkbox"> grant admin</label>
      <button class="btn primary" id="addBtn">add account</button>
    </div>
  </section>
  <table><thead><tr><th>email</th><th>role</th><th>added by</th><th>actions</th></tr></thead>
  <tbody id="rows">__ROWS__</tbody></table>
  <p class="summary" style="color:var(--muted);margin-top:var(--sp-md);font-size:12px">
    Bootstrap admins come from <span class="mono">RACECAR_ADMIN_EMAILS</span> in
    <span class="mono">.env</span> and can't be edited from here. Everyone else
    listed below can sign in with Google; "admin" accounts can also open this page.</p>
</main>
<script>
(function(){
  var self = "__SELF__";
  var msg = document.getElementById('msg');
  function show(t){ msg.className='msg bad'; msg.textContent=t; }
  async function post(url, payload){
    var r = await fetch(url, {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    var d = await r.json().catch(function(){return {};});
    if(!r.ok) throw new Error((d && d.detail) || ('HTTP '+r.status));
    return d;
  }
  document.getElementById('addBtn').addEventListener('click', async function(){
    var email=(document.getElementById('newEmail').value||'').trim().toLowerCase();
    var is_admin=document.getElementById('newAdmin').checked;
    if(!email || email.indexOf('@')<0){ show('Enter a valid email address.'); return; }
    try{ await post('/admin/users',{email:email,is_admin:is_admin}); location.reload(); }
    catch(e){ show('Add failed: '+e.message); }
  });
  document.getElementById('newEmail').addEventListener('keydown', function(e){
    if(e.key==='Enter') document.getElementById('addBtn').click();
  });
  document.addEventListener('click', async function(ev){
    var t=ev.target.closest('[data-act]'); if(!t) return;
    var email=t.dataset.email, act=t.dataset.act;
    try{
      if(act==='remove'){
        if(!confirm('Remove '+email+'?\\n\\nThey will no longer be able to sign in.')) return;
        await post('/admin/users/delete',{email:email});
      } else if(act==='toggle'){
        await post('/admin/users',{email:email, is_admin: t.dataset.admin!=='1'});
      } else if(act==='impersonate'){
        if(!confirm('Impersonate '+email+'?\\n\\nYou will browse the whole site AS this user. A red badge (bottom-right) exits impersonation.')) return;
        await post('/admin/impersonate',{email:email});
        location.href='/'; return;
      } else if(act==='history'){
        location.href='/admin/user/'+encodeURIComponent(email)+'/history'; return;
      }
      location.reload();
    }catch(e){ show('Action failed: '+e.message); }
  });
})();
</script>
</body></html>"""
)

_ADMIN_DISABLED_HTML = f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>admin unavailable</title>
{_FONTS_LINK}<style>{_BASE_CSS}{_LOGIN_EXTRA_CSS}</style></head><body>
  <section class="login-card">
    <div class="pill">dev open</div>
    <h1 class="t-display" style="margin-top:14px">Admin portal is unavailable</h1>
    <p>The admin portal needs Google OAuth configured. Set <code>GOOGLE_CLIENT_ID</code>,
       <code>GOOGLE_CLIENT_SECRET</code>, and at least one
       <code>RACECAR_ADMIN_EMAILS</code> entry in <code>server/.env</code>, then restart.</p>
    <a class="btn primary" href="/">back to sessions</a>
  </section>
</body></html>"""

_ACCOUNT_HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>account \u00b7 racecar-35</title>
""" + _FONTS_LINK + "<style>" + _BASE_CSS + _ADMIN_EXTRA_CSS + """
.key-row{display:flex;gap:var(--sp-sm);align-items:center;flex-wrap:wrap}
.key-row input{flex:1;min-width:220px;font-family:var(--mono,monospace);letter-spacing:1px}
</style></head><body>
<header class="app"><span class="dot"></span><h1>racecar-35 \u00b7 pit wall</h1>
  <span class="crumbs"><a href="/">sessions</a> &rsaquo; account</span>
  <span style="flex:1"></span>__USER_CHIP__</header>
<main>
  <div id="msg" class="msg"></div>
  <section class="panel">
    <div class="t-label" style="margin-bottom:var(--sp-md)">Signed in as</div>
    <p class="mono" style="margin:0 0 var(--sp-lg)">__EMAIL__</p>
    <div class="t-label" style="margin-bottom:var(--sp-md)">Your upload API key</div>
    <div class="key-row">
      <input id="apiKey" type="text" readonly value="__APIKEY__">
      <button class="btn" id="copyBtn">copy</button>
      <button class="btn danger" id="refreshBtn">refresh key</button>
    </div>
    <p class="summary" style="color:var(--muted);margin-top:var(--sp-md);font-size:12px">
      Send this as the <span class="mono">X-API-Key</span> header (or put it in the
      dash's <span class="mono">api key</span> field). Uploads with this key are filed
      under your account automatically. <b>Refreshing replaces the old key immediately</b>
      \u2014 any device still using the old value must be updated.</p>
  </section>
</main>
<script>
(function(){
  var msg=document.getElementById('msg');
  var input=document.getElementById('apiKey');
  function show(t,ok){ msg.className='msg '+(ok?'good':'bad'); msg.textContent=t; }
  async function post(url){
    var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
    var d=await r.json().catch(function(){return {};});
    if(!r.ok) throw new Error((d&&d.detail)||('HTTP '+r.status));
    return d;
  }
  document.getElementById('copyBtn').addEventListener('click',function(){
    input.select(); input.setSelectionRange(0,99999);
    navigator.clipboard.writeText(input.value).then(function(){show('Copied to clipboard.',true);},
      function(){ try{document.execCommand('copy'); show('Copied to clipboard.',true);}catch(e){show('Copy failed \u2014 select and copy manually.');} });
  });
  document.getElementById('refreshBtn').addEventListener('click',async function(){
    if(!confirm('Refresh your API key?\\n\\nThe current key stops working immediately and any device using it must be updated.')) return;
    try{ var d=await post('/account/apikey/refresh'); input.value=d.api_key; show('New API key generated.',true); }
    catch(e){ show('Refresh failed: '+e.message); }
  });
})();
</script>
</body></html>"""
)

_ADMIN_USER_HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>manage user \u00b7 racecar-35</title>
""" + _FONTS_LINK + "<style>" + _BASE_CSS + _ADMIN_EXTRA_CSS + """
.chk-big{display:flex;align-items:center;gap:var(--sp-sm);font-size:15px}
.grant-grid{display:flex;gap:var(--sp-sm);align-items:center;flex-wrap:wrap}
.grant-grid input{flex:1;min-width:220px}
ul.grants{list-style:none;padding:0;margin:var(--sp-md) 0 0}
ul.grants li{display:flex;align-items:center;justify-content:space-between;gap:var(--sp-sm);padding:8px 0;border-bottom:1px solid var(--surface-3)}
</style></head><body>
<header class="app"><span class="dot"></span><h1>racecar-35 \u00b7 pit wall</h1>
  <span class="crumbs"><a href="/">sessions</a> &rsaquo; <a href="/admin">admin</a> &rsaquo; manage user</span>
  <span style="flex:1"></span>__USER_CHIP__</header>
<main>
  <div id="msg" class="msg"></div>
  <section class="panel">
    <div class="t-label" style="margin-bottom:var(--sp-md)">Managing</div>
    <p class="mono" style="margin:0 0 var(--sp-lg)">__EMAIL__</p>
    <label class="chk-big">
      <input type="checkbox" id="viewAll" __CHECKED__ __DISABLED__>
      <span><b>ALL USERS</b> &mdash; this account sees every user's sessions
      (no need to add anyone).</span>
    </label>
    __LOCKED_NOTE__
  </section>
  <section class="panel">
    <div class="t-label" style="margin-bottom:var(--sp-md)">Users this account can see</div>
    <div class="grant-grid">
      <input id="target" list="known" type="text" placeholder="name@gmail.com" autocomplete="off" spellcheck="false" __GRANT_DISABLED__>
      <datalist id="known">__OPTIONS__</datalist>
      <button class="btn primary" id="grantBtn" __GRANT_DISABLED__>add user</button>
    </div>
    <ul class="grants" id="grants">__CHIPS__</ul>
    <p class="summary" style="color:var(--muted);margin-top:var(--sp-md);font-size:12px">
      An account always sees its own sessions. Add others here to share their
      sessions with this account, or tick <b>ALL USERS</b> above to skip the
      list entirely.</p>
  </section>
</main>
<script>
(function(){
  var EMAIL="__EMAIL__";
  var msg=document.getElementById('msg');
  function show(t,ok){ msg.className='msg '+(ok?'good':'bad'); msg.textContent=t; }
  async function post(url,payload){
    var r=await fetch(url,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
    var d=await r.json().catch(function(){return {};});
    if(!r.ok) throw new Error((d&&d.detail)||('HTTP '+r.status));
    return d;
  }
  var va=document.getElementById('viewAll');
  if(va && !va.disabled){
    va.addEventListener('change', async function(){
      try{ await post('/admin/users/visibility',{email:EMAIL,view_all:va.checked}); location.reload(); }
      catch(e){ show('Update failed: '+e.message); va.checked=!va.checked; }
    });
  }
  var grantBtn=document.getElementById('grantBtn');
  if(grantBtn && !grantBtn.disabled){
    grantBtn.addEventListener('click', async function(){
      var t=(document.getElementById('target').value||'').trim().toLowerCase();
      if(!t || t.indexOf('@')<0){ show('Enter a valid email address.'); return; }
      try{ await post('/admin/users/grant',{email:EMAIL,target:t}); location.reload(); }
      catch(e){ show('Add failed: '+e.message); }
    });
    document.getElementById('target').addEventListener('keydown',function(e){ if(e.key==='Enter') grantBtn.click(); });
  }
  document.addEventListener('click', async function(ev){
    var t=ev.target.closest('[data-revoke]'); if(!t) return;
    try{ await post('/admin/users/revoke',{email:EMAIL,target:t.dataset.revoke}); location.reload(); }
    catch(e){ show('Remove failed: '+e.message); }
  });
})();
</script>
</body></html>"""
)

_COMBINE_JS = """
<script>
/* ---- combine selected sessions into one file ------------------------- */
(function(){
  const btn = document.getElementById('combine-btn');
  if (!btn) return;
  btn.addEventListener('click', async function(){
    const boxes = Array.from(document.querySelectorAll('.cmb:checked'));
    if (boxes.length < 2){ alert('Select at least 2 sessions (checkboxes on the left).'); return; }
    const users = new Set(boxes.map(b=>b.dataset.user));
    if (users.size > 1){ alert('All selected sessions must belong to the SAME user.'); return; }
    const files = boxes.map(b=>b.dataset.file);
    if (!confirm('Combine '+files.length+' sessions into one new file?\\n\\n'+files.join('\\n')+
                 '\\n\\n(The originals are kept.)')) return;
    btn.disabled = true; btn.textContent = 'combining\u2026';
    try {
      const r = await fetch('/sessions/combine', {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({user: boxes[0].dataset.user, files})});
      const j = await r.json().catch(()=>({}));
      if (!r.ok) throw new Error((j&&j.detail)||('HTTP '+r.status));
      location.reload();
    } catch(e){
      alert('combine failed: '+e.message);
      btn.disabled = false; btn.textContent = 'combine selected';
    }
  });
})();
</script>
"""

_INDEX_HEAD = f"""<!doctype html>
<html lang="en"><head>
<meta charset="utf-8">
<title>racecar-35 sessions</title>
{_FONTS_LINK}
<style>{_BASE_CSS}
 /* The sessions list is a table of numbers: it wants the width. */
 main {{ max-width: 2400px; }}
 th.best {{ cursor: pointer; white-space: nowrap; }}
 th.best span {{ color: var(--muted); font-weight: 400; }}
 td.best {{ white-space: nowrap; font-variant-numeric: tabular-nums; }}
 td.best.sortkey {{ color: var(--primary); }}
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
  <span class="crumbs">sessions</span><span style="flex:1"></span>__USER_CHIP__</header>
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

  // ---- best lap per session -------------------------------------------
  // The list must never wait on lap detection (it reads whole session files),
  // so cells render "…", the server answers with whatever is already cached,
  // and a few cold ones get computed per request until the column is full.
  (function(){
    const cells = Array.from(document.querySelectorAll('td.best'));
    if (!cells.length) return;
    const byKey = new Map(cells.map(c => [c.dataset.best, c]));
    function fmt(s){ if (s == null || !isFinite(s) || s <= 0) return '\u2014';
      s = Number(s); const m = Math.floor(s/60), r = s - m*60;
      return m ? (m + ':' + (r < 10 ? '0' : '') + r.toFixed(2)) : r.toFixed(2); }
    let rounds = 0;
    async function fill(){
      const want = cells.filter(c => !c.dataset.done).map(c => c.dataset.best);
      if (!want.length || rounds > 12) return;
      rounds++;
      let any = false;
      for (let i = 0; i < want.length; i += 25){
        const batch = want.slice(i, i+25);
        try {
          const r = await fetch('/laps/summary?files=' + encodeURIComponent(batch.join(',')));
          if (!r.ok) continue;
          const j = await r.json();
          for (const k of batch){
            const c = byKey.get(k); if (!c || c.dataset.done) continue;
            const d = (j.laps || {})[k];
            if (!d) continue;                       // not computed yet: ask again
            c.textContent = fmt(d.best_s);
            c.title = (d.laps || 0) + ' laps' + (d.excluded ? ' (' + d.excluded + ' excluded)' : '')
                      + (d.source ? ' \u00b7 ' + d.source : '')
                      + (d.error ? ' \u00b7 ' + d.error : '');
            c.dataset.done = '1';
            c.dataset.secs = (d.best_s == null ? '' : d.best_s);
            any = true;
          }
        } catch(e){}
      }
      if (!any){ cells.forEach(c => { if (!c.dataset.done){ c.dataset.done='1';
        if (!c.textContent.trim() || c.textContent.trim() === '\u2026') c.textContent = '\u2014'; } }); }
      else setTimeout(fill, 400);
    }
    fill();

    // sort by best lap (click the header); un-timed sessions sort last
    const head = document.querySelector('th.best');
    if (head){
      let dir = 1;
      head.addEventListener('click', () => {
        const tb = document.getElementById('rows');
        const rows = Array.from(tb.querySelectorAll('tr'));
        rows.sort((a, b) => {
          const ka = a.querySelector('td.best'), kb = b.querySelector('td.best');
          const va = parseFloat(ka?.dataset.secs || ''), vb = parseFloat(kb?.dataset.secs || '');
          const na = isFinite(va), nb = isFinite(vb);
          if (na !== nb) return na ? -1 : 1;
          if (!na) return 0;
          return dir * (va - vb);
        });
        dir = -dir;
        rows.forEach(r => tb.appendChild(r));
        head.querySelector('span').textContent = dir > 0 ? '\u25b4' : '\u25be';
      });
    }
  })();

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
        const errors = d.errors ? ('\\n' + d.errors.join('\\n')) : '';
        throw new Error((d.message || data.detail || ('HTTP ' + resp.status)) + errors);
      }
      const v = data.validation || {};
      showResult('OK: saved ' + data.path + '\\n' + (v.samples || '?') + ' samples, '
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
    if (!confirm('Delete session permanently?\\n\\n' + user + '/' + file)) return;
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
    <div><label for="userEmail">user email</label><input id="userEmail" type="text" value="__CURRENT_EMAIL__" placeholder="driver@example.com"></div>
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


def _user_chip_html(user: Optional[dict]) -> str:
    if not oauth_enabled():
        return '<span class="pill">dev open</span>'
    if not user:
        return '<a class="btn primary" href="/login">sign in</a>'
    email = html.escape(str(user.get("email", "")))
    admin_link = ''
    if is_admin_email(str(user.get("email", ""))):
        admin_link = '<a class="btn" href="/admin">admin</a>'
    return f'<span class="pill good">{email}</span><a class="btn" href="/coach">checklist</a><a class="btn" href="/account">account</a>{admin_link}<a class="btn" href="/logout">logout</a>'


@app.get("/", response_class=HTMLResponse)
async def index(request: Request) -> Response:
    user = current_user(request)
    if oauth_enabled() and not user:
        return login_redirect(request)
    viewer_email = str((user or {}).get("email", ""))
    sessions_root = DATA_DIR / "sessions"
    rows: list[str] = []
    total = 0
    total_bytes = 0
    if sessions_root.exists():
        for user_dir in sorted(sessions_root.iterdir()):
            if not user_dir.is_dir():
                continue
            if oauth_enabled() and not can_view_dir(viewer_email, user_dir.name):
                continue
            # Delete is owner-only (admins excepted) even when you can VIEW
            # another user's sessions via a can_view / view_all grant.
            dir_can_delete = (not oauth_enabled()) or can_delete_dir(viewer_email, user_dir.name)
            for f in sorted(user_dir.iterdir(), reverse=True):
                if not f.is_file() or not f.name.endswith(".ndjson"):
                    continue
                st = f.stat()
                when = time.strftime(
                    "%Y-%m-%d %H:%M:%S UTC",
                    time.gmtime(display_epoch_for(f)),
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
                delete_btn = (
                    f'<button class="btn danger" data-delete="1" '
                    f'data-user="{user_h}" data-file="{file_h}">delete</button>'
                    if dir_can_delete else ""
                )
                cached = _lap_summary_cached(user_dir.name, f)
                best_str = (f'<span title="{cached.get("laps", 0)} laps">'
                            f"{_fmt_lap_s(cached.get('best_s'))}</span>"
                            if cached else "\u2026")
                rows.append(
                    f'<tr><td><input type="checkbox" class="cmb" '
                    f'data-user="{user_h}" data-file="{file_h}"></td>'
                    f"<td>{user_h}</td>"
                    f"<td class=mono>{when_h}</td>"
                    f"<td>{track_h}</td>"
                    f'<td class="num best" data-best="{user_h}/{file_h}">'
                    f"{best_str}</td>"
                    f'<td class=mono><a href="/review/{user_h}/{file_h}">{file_h}</a></td>'
                    f"<td class=num>{size_str}</td>"
                    f'<td><div class="actions">'
                    f'<a class="btn" href="/sessions/{user_h}/{file_h}">download</a>'
                    f'{delete_btn}'
                    f'</div></td></tr>'
                )
                total += 1
                total_bytes += st.st_size

    if rows:
        listing = (
            '<div class="toolbar"><div class="grow">'
            '<input type="search" id="q" placeholder="filter by user / track / date / filename\u2026" autofocus>'
            '</div><button id="combine-btn" class="btn" '
            'title="select 2+ sessions of the same user, oldest+newest are joined in time order">'
            'combine selected</button><span class="pill" id="vis"></span></div>'
            "<table><thead><tr><th></th><th>user</th><th>started (UTC)</th>"
            "<th>track</th><th>best lap <span>\u25b4\u25be</span></th>"
            "<th>filename</th><th>size</th><th>actions</th></tr></thead><tbody id=\"rows\">"
            + "\n".join(rows)
            + "</tbody></table>"
            + '<div class="no-match" id="nomatch">no sessions match that filter.</div>'
            + f'<p class="summary">{total} session(s), {_human_bytes(total_bytes)} total</p>'
        )
    else:
        listing = '<p class="empty">no sessions uploaded yet.</p>'

    current_email = html.escape((user or {}).get("email", ""))
    upload_panel = _UPLOAD_PANEL_HTML.replace("__CURRENT_EMAIL__", current_email)
    user_chip = _user_chip_html(user)
    return _INDEX_HEAD.replace("__USER_CHIP__", user_chip) + upload_panel + listing + _INDEX_JS + _COMBINE_JS + "</main></body></html>"


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
_CANBUS_HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>CAN captures \u00b7 racecar-35</title>
""" + _FONTS_LINK + "<style>" + _BASE_CSS + _ADMIN_EXTRA_CSS + """
  .msg.good { display:block; background:rgba(108,208,122,0.1); color:var(--good);
    border:1px solid rgba(108,208,122,0.3); }
  input[type=file] { color:var(--muted); font:13px var(--ff-ui); }
</style></head><body>
<header class="app"><span class="dot"></span><h1>racecar-35 \u00b7 pit wall</h1>
  <span class="crumbs"><a href="/">sessions</a> &rsaquo; <a href="/admin">admin</a> &rsaquo; CAN</span>
  <span style="flex:1"></span>__USER_CHIP__</header>
<main>
  <div id="msg" class="msg"></div>
  <section class="panel">
    <div class="t-label" style="margin-bottom:var(--sp-md)">Upload a CAN sniffer capture (.csv)</div>
    <div class="add-grid">
      <input id="canFile" type="file" accept=".csv,.txt,.log,text/csv">
      <button class="btn primary" id="upBtn">upload</button>
    </div>
    <p class="summary" style="color:var(--muted);margin-top:var(--sp-md);font-size:12px">
      Expected format from the dash sniffer (Tools \u2192 Start CAN capture):
      <span class="mono">t_ms,id,ext,dlc,d0..d7</span> \u2014 one CAN frame per line,
      data bytes in hex. Open a capture's <b>review</b> to find which byte/word tracks RPM.</p>
  </section>
  <table><thead><tr><th>capture</th><th>uploaded</th><th>size</th><th>actions</th></tr></thead>
  <tbody id="rows">__ROWS__</tbody></table>
</main>
<script>
(function(){
  var msg=document.getElementById('msg');
  function show(t,ok){ msg.className='msg '+(ok?'good':'bad'); msg.textContent=t; }
  document.getElementById('upBtn').addEventListener('click', async function(){
    var f=document.getElementById('canFile').files[0];
    if(!f){ show('Choose a .csv capture first.'); return; }
    show('Uploading '+f.name+'\u2026', true);
    try{
      var buf=await f.arrayBuffer();
      var r=await fetch('/admin/canbus/upload?name='+encodeURIComponent(f.name),
        {method:'POST', headers:{'Content-Type':'text/csv'}, body:buf});
      var d=await r.json().catch(function(){return {};});
      if(!r.ok) throw new Error((d&&d.detail)||('HTTP '+r.status));
      location.reload();
    }catch(e){ show('Upload failed: '+e.message); }
  });
  document.addEventListener('click', async function(ev){
    var t=ev.target.closest('[data-act=del]'); if(!t) return;
    var file=t.dataset.file;
    if(!confirm('Delete '+file+'?')) return;
    try{
      var r=await fetch('/admin/canbus/'+encodeURIComponent(file)+'/delete',{method:'POST'});
      if(!r.ok){ var d=await r.json().catch(function(){return {};}); throw new Error((d&&d.detail)||('HTTP '+r.status)); }
      location.reload();
    }catch(e){ show('Delete failed: '+e.message); }
  });
})();
</script>
</body></html>"""
)

# ---------------------------------------------------------------------------
# /track3d — first-person 3D drive view
#
# The 2D Leaflet map answers "where on the track". This answers "what does it
# look like from the seat": satellite imagery DRAPED over real terrain, the
# driven line painted on the ground, brake/apex/throttle markers, and a chase
# camera that drives the trace at eye height driven by the same playback clock
# as the review page.
#
# Ground = the SAME keyless Esri World Imagery the 2D map uses (a raster draped
# on the surface — this is not photogrammetry; true 3D buildings/trees would
# need a keyed source such as Google Photorealistic 3D Tiles). Elevation = the
# keyless AWS terrarium DEM (RACECAR_MAP_DEM, `none` to disable).
#
# ⚠️ Everything is a layer toggle on ONE map, so switching imagery/terrain on
# and off never rebuilds the camera or re-fetches the session:
#   imagery off + terrain on  = black ground with real hills (the lines read
#                               like a light table)
#   imagery on  + terrain off = flat satellite view (classic map in 3D)
#
# Phase 2 hook (already wired): a `?pts=` lasso polygon makes the page POST the
# SAME /sessions/<u>/<f>/lines endpoint the lineview popout uses and draw the
# IDEAL line (fastest real traverse on record) in green plus your session best
# in blue over the ground — "what we did" vs "what the data says is fastest",
# in the driver's view.
# ---------------------------------------------------------------------------
_TRACK3DMAP_HTML = (
    """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>3D drive \u00b7 __FILE__</title>
<link rel="stylesheet" href="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.css">
<script src="https://unpkg.com/maplibre-gl@4.7.1/dist/maplibre-gl.js"></script>
<style>
  :root { --bg:#0E1014; --surface:#181B22; --line:#2A2F3A; --text:#E6E8EE;
          --muted:#8A92A3; --good:#6CD07A; --warn:#FFB020; --bad:#FF4D4D;
          --you:#4EA1FF; }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--text);
    font: 13px/1.4 Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    overflow: hidden; }
  #map { position:absolute; inset:0 0 46px 0; background:#000; }
  #notice { position:absolute; left:50%; top:14px; transform:translateX(-50%);
    z-index:20; background:rgba(14,16,20,0.92); border:1px solid var(--line);
    border-radius:6px; padding:8px 14px; font:600 12px Inter,sans-serif;
    max-width:80vw; }
  #notice.err { border-color:var(--bad); color:#FFB0B0; }
  #hud { position:absolute; left:12px; top:12px; z-index:15; pointer-events:none;
    background:rgba(14,16,20,0.72); border:1px solid var(--line); border-radius:8px;
    padding:10px 14px; min-width:190px; }
  #hud .big { font:700 40px/1 ui-monospace, Menlo, monospace; letter-spacing:-1px; }
  #hud .unit { color:var(--muted); font:600 12px Inter,sans-serif; margin-left:4px; }
  #hud .row { display:flex; justify-content:space-between; gap:12px;
    font:600 12px ui-monospace, Menlo, monospace; margin-top:4px; }
  #hud .row span.k { color:var(--muted); font-weight:400; }
  #legend { position:absolute; right:12px; top:12px; z-index:15;
    pointer-events:none; background:rgba(14,16,20,0.72);
    border:1px solid var(--line); border-radius:8px; padding:8px 12px; }
  #legend .li { display:flex; align-items:center; gap:7px; margin:3px 0;
    font:500 11px Inter,sans-serif; white-space:nowrap; }
  #legend .sw { width:22px; height:4px; border-radius:2px; flex:0 0 auto; }
  #legend .dot { width:10px; height:10px; border-radius:50%; flex:0 0 auto;
    border:1.5px solid #000; }
  #bar { position:absolute; left:0; right:0; bottom:0; height:46px; z-index:20;
    display:flex; align-items:center; gap:10px; padding:0 12px; overflow-x:auto;
    background:var(--surface); border-top:1px solid var(--line); }
  #bar button { background:#20242E; color:var(--text); border:1px solid var(--line);
    border-radius:4px; padding:7px 12px; cursor:pointer; font:600 12px Inter,sans-serif;
    white-space:nowrap; }
  #bar button.on { background:var(--primary, #3B82F6); border-color:transparent;
    color:#fff; }
  #bar input[type=range] { flex:1 1 180px; min-width:120px; accent-color:var(--warn); }
  #bar label { display:flex; align-items:center; gap:6px; white-space:nowrap;
    font:600 12px Inter,sans-serif; cursor:pointer; color:var(--text); }
  #bar input[type=checkbox] { width:15px; height:15px; margin:0; cursor:pointer;
    accent-color:var(--warn); }
  #bar .meta { color:var(--muted); font:500 12px ui-monospace, Menlo, monospace;
    white-space:nowrap; }
  #bar select { background:#20242E; color:var(--text); border:1px solid var(--line);
    border-radius:4px; padding:6px 8px; font:600 12px Inter,sans-serif; }
  #bar a { color:var(--muted); text-decoration:none; white-space:nowrap;
    border:1px solid var(--line); border-radius:4px; padding:7px 10px;
    font:600 12px Inter,sans-serif; }
  #bar a:hover { color:var(--text); }
  .maplibregl-ctrl-bottom-left { bottom:52px; }
</style></head><body>
<div id="map"></div>
<div id="notice">loading session\u2026</div>
<div id="hud" style="display:none">
  <div><span class="big" id="h-mph">0</span><span class="unit">mph</span></div>
  <div class="row"><span class="k">rpm</span><span id="h-rpm">\u2014</span></div>
  <div class="row"><span class="k">lap</span><span id="h-lap">\u2014</span></div>
  <div class="row"><span class="k">lap time</span><span id="h-lapt">\u2014</span></div>
  <div class="row"><span class="k">altitude</span><span id="h-alt">\u2014</span></div>
</div>
<div id="legend">
  <div class="li"><span class="sw" style="background:#FFB020"></span>lap you are driving</div>
  <div class="li"><span class="sw" style="background:#6B7280"></span>rest of the session</div>
  <div class="li" id="lg-ideal" style="display:none"><span class="sw" style="background:#6CD07A"></span>ideal line (fastest real lap)</div>
  <div class="li" id="lg-you" style="display:none"><span class="sw" style="background:#4EA1FF"></span>your best line through it</div>
  <div class="li"><span class="dot" style="background:#FF4D4D"></span>brake</div>
  <div class="li"><span class="dot" style="background:#FFB020"></span>apex</div>
  <div class="li"><span class="dot" style="background:#6CD07A"></span>back to throttle</div>
</div>
<div id="bar">
  <button id="b-play">\u25b6 drive</button>
  <input id="b-scrub" type="range" min="0" max="1000" value="0" step="1">
  <span class="meta" id="b-clock">0:00 / 0:00</span>
  <select id="b-rate" title="playback speed">
    <option value="1">1\u00d7</option><option value="2">2\u00d7</option>
    <option value="4">4\u00d7</option><option value="8">8\u00d7</option>
    <option value="0.25">\u00bc\u00d7</option><option value="0.5">\u00bd\u00d7</option>
  </select>
  <label title="keep the camera behind the car"><input type="checkbox" id="b-follow" checked>follow</label>
  <label title="satellite imagery on or off (black ground)"><input type="checkbox" id="b-sat">satellite</label>
  <label title="real elevation from the DEM"><input type="checkbox" id="b-ter">terrain</label>
  <label title="camera height">zoom <input id="b-zoom" type="range" min="13" max="19.5" step="0.1" value="17.4" style="width:110px;flex:0 0 110px"></label>
  <a href="/review/__USER__/__FILE__">\u2190 pit wall</a>
</div>
<script>
(function () {
  "use strict";
  var USER = "__USER__", FILE = "__FILE__";
  var SAT = __MAP_TILES__, ATTRIB = __MAP_ATTRIB__, MAXZOOM = __MAP_MAXZOOM__;
  var DEM = __MAP_DEM__, DEMZ = __MAP_DEM_MAXZOOM__;
  var q = new URLSearchParams(location.search);
  var pts = (q.get("pts") || "").split("|").map(function (s) {
    return s.split(",").map(Number);
  }).filter(function (a) { return a.length === 2 && isFinite(a[0]) && isFinite(a[1]); });
  function el(id) { return document.getElementById(id); }
  function notice(msg, err) {
    var n = el("notice");
    n.style.display = "block";
    n.textContent = msg;
    n.className = err ? "err" : "";
  }
  function hideNotice() { el("notice").style.display = "none"; }
  if (!window.maplibregl) {
    notice("3D engine failed to load (unpkg.com blocked?)", true);
    return;
  }
  function pref(k, d) {
    try { var v = localStorage.getItem(k); return v === null ? d : v === "1"; }
    catch (e) { return d; }
  }
  function setPref(k, v) { try { localStorage.setItem(k, v ? "1" : "0"); } catch (e) {} }

  // ---- geometry helpers ---------------------------------------------------
  function hav(a, b) {
    var R = 6371008.8, p = Math.PI / 180;
    var dlat = (b[0] - a[0]) * p, dlon = (b[1] - a[1]) * p;
    var la1 = a[0] * p, la2 = b[0] * p;
    var h = Math.sin(dlat / 2) * Math.sin(dlat / 2) +
            Math.cos(la1) * Math.cos(la2) * Math.sin(dlon / 2) * Math.sin(dlon / 2);
    return 2 * R * Math.asin(Math.min(1, Math.sqrt(h)));
  }
  function bearing(a, b) {
    var p = Math.PI / 180;
    var y = Math.sin((b[1] - a[1]) * p) * Math.cos(b[0] * p);
    var x = Math.cos(a[0] * p) * Math.sin(b[0] * p) -
            Math.sin(a[0] * p) * Math.cos(b[0] * p) * Math.cos((b[1] - a[1]) * p);
    return (Math.atan2(y, x) / p + 360) % 360;
  }
  function fmtClock(s) {
    s = Math.max(0, Math.round(s));
    var m = Math.floor(s / 60);
    return m + ":" + ("0" + (s % 60)).slice(-2);
  }
  function fmtLap(s) {
    if (!isFinite(s) || s <= 0) return "\\u2014";
    var m = Math.floor(s / 60), r = s - m * 60;
    return (m ? m + ":" + (r < 10 ? "0" : "") : "") + r.toFixed(2);
  }
  function num(v, d) { return (typeof v === "number" && isFinite(v)) ? v.toFixed(d) : null; }

  var satOn = pref("rc3.sat", true) && !!SAT;
  var terOn = pref("rc3.ter", true) && !!DEM;
  var follow = true, playing = false, rate = 1;
  var zoom = 17.4, PITCH = 74, LOOKAHEAD_M = 22;

  // ---- style: one background + optional imagery + optional terrain --------
  var sources = {}, layers = [{
    id: "bg", type: "background", paint: { "background-color": "#000000" }
  }];
  if (SAT) {
    sources.sat = { type: "raster", tiles: [SAT], tileSize: 256,
                    maxzoom: MAXZOOM, attribution: ATTRIB };
    layers.push({ id: "sat", type: "raster", source: "sat",
                  layout: { visibility: satOn ? "visible" : "none" } });
  }
  if (DEM) {
    sources.dem = { type: "raster-dem", tiles: [DEM], tileSize: 256,
                    encoding: "terrarium", maxzoom: DEMZ };
  }
  if (!SAT) { el("b-sat").disabled = true; el("b-sat").parentNode.style.opacity = 0.4; }
  if (!DEM) { el("b-ter").disabled = true; el("b-ter").parentNode.style.opacity = 0.4; }
  el("b-sat").checked = satOn;
  el("b-ter").checked = terOn;

  var map = new maplibregl.Map({
    container: "map", style: { version: 8, sources: sources, layers: layers },
    center: [-77.0, 39.0], zoom: zoom, pitch: PITCH, bearing: 0, maxPitch: 85,
    attributionControl: { compact: true }
  });
  map.on("error", function (e) {
    // Tile/CDN errors are common (offline, blocked host). Keep them in the
    // console instead of throwing overlays over the view.
    if (e && e.error) console.warn("[track3d]", e.error.message || e.error);
  });

  // Car marker: a canvas-drawn arrow (no glyph server needed). Wrapped so a
  // missing 2d context degrades to "no car icon" instead of killing the view.
  (function () {
    var c = document.createElement("canvas");
    c.width = c.height = 64;
    var g = null;
    try { g = c.getContext("2d"); } catch (e) { g = null; }
    if (!g) return;
    g.fillStyle = "#FFB020"; g.strokeStyle = "#1A1300"; g.lineWidth = 4;
    g.beginPath(); g.moveTo(32, 5); g.lineTo(57, 57); g.lineTo(32, 45);
    g.lineTo(7, 57); g.closePath(); g.fill(); g.stroke();
    map.on("load", function () {
      try { map.addImage("car-arrow", c, { pixelRatio: 2 }); } catch (e) {}
    });
  })();

  // ---- session data -------------------------------------------------------
  var S = [], T = [], BEAR = [], LAPS = [], lapOf = [], tEnd = 0;
  var idx = 0, curTime = 0;

  function buildTimeline() {
    var first = null, i;
    for (i = 0; i < S.length; i++) {
      var s = S[i], v = null;
      if (typeof s.t === "number" && isFinite(s.t)) v = s.t;
      else if (typeof s.t_ms === "number" && isFinite(s.t_ms)) v = s.t_ms / 1000;
      if (v !== null && first === null) first = v;
      T.push(v);
    }
    if (first === null) {
      for (i = 0; i < S.length; i++) T[i] = i / 25;
    } else {
      var last = first;
      for (i = 0; i < T.length; i++) {
        if (T[i] === null) T[i] = last; else last = T[i];
        T[i] = T[i] - first;
      }
    }
    tEnd = T.length ? T[T.length - 1] : 0;
  }

  function buildBearings() {
    for (var i = 0; i < S.length; i++) {
      var hdg = S[i].heading_deg;
      // GPS heading is meaningless at rest, so derive the direction the car is
      // actually travelling from the trace; fall back to the reported heading.
      var a = null, b = null;
      for (var k = i - 1; k >= Math.max(0, i - 60); k--) {
        if (hav([S[i].lat, S[i].lon], [S[k].lat, S[k].lon]) > 3) { a = [S[k].lat, S[k].lon]; break; }
      }
      for (var j = i + 1; j <= Math.min(S.length - 1, i + 60); j++) {
        if (hav([S[i].lat, S[i].lon], [S[j].lat, S[j].lon]) > 3) { b = [S[j].lat, S[j].lon]; break; }
      }
      if (a && b) BEAR[i] = bearing(a, b);
      else if (i > 0) BEAR[i] = BEAR[i - 1];
      else if (typeof hdg === "number" && isFinite(hdg)) BEAR[i] = hdg;
      else BEAR[i] = 0;
    }
  }

  function buildLaps() {
    var i, L;
    for (i = 0; i < S.length; i++) lapOf[i] = 0;   // 0 = out-lap / before S/F
    for (var k = 0; k < LAPS.length; k++) {
      L = LAPS[k];
      for (i = 0; i < S.length; i++) {
        if (T[i] >= L.t_start && T[i] <= L.t_end) lapOf[i] = L.lap;
      }
    }
  }

  function idxAt(t) {
    var lo = 0, hi = T.length - 1;
    while (lo < hi) {
      var mid = (lo + hi + 1) >> 1;
      if (T[mid] <= t) lo = mid; else hi = mid - 1;
    }
    return lo;
  }

  // A point LOOKAHEAD_M metres down the trace from index i (the camera centres
  // there so the car sits low in the frame, the road ahead filling the view).
  function lookAhead(i, m) {
    var d = 0;
    for (var j = i + 1; j < S.length && j < i + 120; j++) {
      d += hav([S[j - 1].lat, S[j - 1].lon], [S[j].lat, S[j].lon]);
      if (d >= m) return [S[j].lat, S[j].lon];
    }
    return [S[i].lat, S[i].lon];
  }

  // Simple per-lap corner markers: apex = slowest point, brake = where the
  // sustained deceleration into that apex began, throttle = where speed starts
  // climbing again. Cheap, no server round-trip, and honest about what it is.
  function lapMarkers() {
    var out = [], cur = [], curLap = null, i;
    for (i = 0; i <= S.length; i++) {
      var lp = (i === S.length) ? null : lapOf[i];
      if (cur.length && (lp === null || lp !== curLap)) {
        if (cur.length > 20 && (curLap !== 0 || !LAPS.length)) {
          var f = markersFor(cur);
          if (f) out = out.concat(f);
        }
        cur = []; curLap = null;
      }
      if (lp === null) break;
      if (curLap === null) curLap = lp;
      cur.push(i);
    }
    return { type: "FeatureCollection", features: out };
  }
  function markersFor(idxs) {
    var best = -1, bestMph = Infinity, i;
    for (i = 0; i < idxs.length; i++) {
      var mph = S[idxs[i]].speed_mph;
      if (typeof mph !== "number" || !isFinite(mph)) continue;
      if (mph < bestMph) { bestMph = mph; best = i; }
    }
    if (best < 0) return null;
    var out = [], apex = idxs[best];
    out.push(pt(apex, "apex"));
    // walk back until the car was clearly faster and accelerating into here
    var brake = -1;
    for (i = best; i > 0; i--) {
      var a = S[idxs[i]].speed_mph, b = S[idxs[i - 1]].speed_mph;
      if (typeof a !== "number" || typeof b !== "number") break;
      if (b <= a) { brake = i; break; }
    }
    if (brake >= 0 && brake < best) out.push(pt(idxs[brake], "brake"));
    var thr = -1;
    for (i = best + 1; i < idxs.length; i++) {
      var c = S[idxs[i]].speed_mph;
      if (typeof c !== "number" || !isFinite(c)) continue;
      if (c > bestMph + 2) { thr = i; break; }
    }
    if (thr >= 0) out.push(pt(idxs[thr], "throttle"));
    return out;
  }
  function pt(i, kind) {
    return { type: "Feature", properties: { kind: kind },
             geometry: { type: "Point", coordinates: [S[i].lon, S[i].lat] } };
  }

  function trackFeatures() {
    var feats = [], cur = [], curLap = null, i;
    for (i = 0; i < S.length; i++) {
      var p = [S[i].lon, S[i].lat];
      if (typeof S[i].lat !== "number" || (S[i].lat === 0 && S[i].lon === 0)) continue;
      if (curLap === null) curLap = lapOf[i];
      if (lapOf[i] !== curLap) {
        if (cur.length > 1) feats.push(line(cur, curLap));
        cur = []; curLap = lapOf[i];
      }
      cur.push(p);
    }
    if (cur.length > 1) feats.push(line(cur, curLap === null ? 0 : curLap));
    return { type: "FeatureCollection", features: feats };
  }
  function line(coords, lap) {
    return { type: "Feature", properties: { lap: lap },
             geometry: { type: "LineString", coordinates: coords } };
  }

  function addLineLayers() {
    map.addSource("traces", { type: "geojson", data: trackFeatures() });
    map.addLayer({
      id: "trace-all", type: "line", source: "traces",
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#6B7280", "line-width": 2, "line-opacity": 0.55 }
    });
    map.addLayer({
      id: "trace-lap", type: "line", source: "traces",
      filter: ["==", ["get", "lap"], 0],
      layout: { "line-cap": "round", "line-join": "round" },
      paint: { "line-color": "#FFB020", "line-width": 4 }
    });
    var sf = LAPS.sf;
    if (sf && typeof sf.lat1 === "number" && typeof sf.lat2 === "number" &&
        (sf.lat1 || sf.lon1) && (sf.lat2 || sf.lon2)) {
      map.addSource("sf", { type: "geojson", data: {
        type: "Feature", geometry: { type: "LineString",
          coordinates: [[sf.lon1, sf.lat1], [sf.lon2, sf.lat2]] } } });
      map.addLayer({ id: "sf", type: "line", source: "sf",
        paint: { "line-color": "#E6E8EE", "line-width": 3, "line-opacity": 0.9 } });
    }
    map.addSource("mk", { type: "geojson", data: lapMarkers() });
    var mk = [["brake", "#FF4D4D"], ["apex", "#FFB020"], ["throttle", "#6CD07A"]];
    for (var i = 0; i < mk.length; i++) {
      map.addLayer({
        id: "mk-" + mk[i][0], type: "circle", source: "mk",
        filter: ["==", ["get", "kind"], mk[i][0]],
        paint: { "circle-radius": 7, "circle-color": mk[i][1],
                 "circle-stroke-color": "#000000", "circle-stroke-width": 2 }
      });
    }
    map.addSource("car", { type: "geojson", data: {
      type: "Feature", properties: { hdg: 0 },
      geometry: { type: "Point", coordinates: [S[0].lon, S[0].lat] } } });
    map.addLayer({
      id: "car", type: "symbol", source: "car",
      layout: { "icon-image": "car-arrow", "icon-size": 0.55,
                "icon-rotate": ["get", "hdg"], "icon-rotation-alignment": "map",
                "icon-allow-overlap": true, "icon-ignore-placement": true }
    });
  }

  function setLapHighlight(lap) {
    if (map.getLayer("trace-lap")) {
      map.setFilter("trace-lap", ["==", ["get", "lap"], lap || 0]);
    }
  }

  // ---- optional ideal / best lines from the lasso polygon -----------------
  function addIdealLines() {
    if (pts.length < 3) return;
    fetch("/sessions/" + encodeURIComponent(USER) + "/" + encodeURIComponent(FILE) + "/lines", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ region: { points: pts } })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok) return;
      var add = function (id, color, width, tr) {
        if (!tr || !tr.trace || tr.trace.length < 2) return;
        map.addSource(id, { type: "geojson", data: {
          type: "Feature", geometry: { type: "LineString",
            coordinates: tr.trace.map(function (p) { return [p[1], p[0]]; }) } } });
        map.addLayer({ id: id, type: "line", source: id,
          layout: { "line-cap": "round", "line-join": "round" },
          paint: { "line-color": color, "line-width": width, "line-opacity": 0.95 } });
        var b = tr.brake;
        if (b) map.addSource(id + "-b", { type: "geojson", data: {
          type: "Feature", geometry: { type: "Point", coordinates: [b.lon, b.lat] } } });
        if (b) map.addLayer({ id: id + "-b", type: "circle", source: id + "-b",
          paint: { "circle-radius": 8, "circle-color": color,
                   "circle-stroke-color": "#000000", "circle-stroke-width": 2 } });
      };
      var sameLap = j.ideal && j.your_best &&
        (j.ideal.session === j.your_best.session) && (j.ideal.lap === j.your_best.lap);
      add("ideal", "#6CD07A", 5, j.ideal);
      if (!sameLap) add("you", "#4EA1FF", 4, j.your_best);
      var any = map.getLayer("ideal");
      if (any) el("lg-ideal").style.display = "flex";
      if (map.getLayer("you")) el("lg-you").style.display = "flex";
      notice("ideal line: lap " + (j.ideal ? j.ideal.lap : "?") +
             (sameLap ? " (same as your best)" : " vs your best"));
      setTimeout(hideNotice, 4000);
    }).catch(function () {});
  }

  // ---- render loop --------------------------------------------------------
  // The body is wrapped so a bad sample (or a missing layer during a style
  // toggle) can never kill the animation loop: playback is what the driver is
  // watching, and a frame that cannot draw must not stop the next one.
  var renderErrLogged = false;
  function render() {
    try { renderBody(); }
    catch (e) {
      if (!renderErrLogged) {
        renderErrLogged = true;
        console.warn("[track3d] render:", e && e.message ? e.message : e);
      }
    }
  }
  function renderBody() {
    var i = idx, s = S[i];
    if (!s) return;
    var hdg = BEAR[i];
    if (map.getSource("car")) {
      map.getSource("car").setData({
        type: "Feature", properties: { hdg: hdg },
        geometry: { type: "Point", coordinates: [s.lon, s.lat] }
      });
    }
    el("h-mph").textContent = (typeof s.speed_mph === "number") ? Math.round(s.speed_mph) : "0";
    el("h-rpm").textContent = (typeof s.rpm === "number") ? s.rpm : "\\u2014";
    var lap = LAPS.filter(function (L) { return L.lap === lapOf[i]; })[0];
    el("h-lap").textContent = lapOf[i] ? lapOf[i] : "out";
    el("h-lapt").textContent = lap ? fmtLap(T[i] - lap.t_start) : "\\u2014";
    el("h-alt").textContent = num(s.alt_m, 0) === null ? "\\u2014" : num(s.alt_m, 0) + " m";
    if (follow) {
      var la = lookAhead(i, LOOKAHEAD_M);
      map.jumpTo({ center: [la[1], la[0]], bearing: hdg, pitch: PITCH, zoom: zoom });
    }
    setLapHighlight(lapOf[i]);
    el("b-scrub").value = String(tEnd ? Math.round(1000 * curTime / tEnd) : 0);
    el("b-clock").textContent = fmtClock(curTime) + " / " + fmtClock(tEnd);
  }

  var lastTs = 0;
  function frame(ts) {
    requestAnimationFrame(frame);   // scheduled FIRST so nothing below can stop it
    if (!lastTs) lastTs = ts;
    var dt = Math.min(0.25, (ts - lastTs) / 1000);
    lastTs = ts;
    if (playing && S.length) {
      curTime += dt * rate;
      if (curTime >= tEnd) { curTime = tEnd; playing = false; syncPlay(); }
      var ni = idxAt(curTime);
      if (ni !== idx) { idx = ni; }
      render();
    }
  }

  // ---- controls -----------------------------------------------------------
  function syncPlay() {
    el("b-play").textContent = playing ? "\\u23f8 pause" : "\\u25b6 drive";
    el("b-play").className = playing ? "on" : "";
  }
  function seek(t) { curTime = Math.max(0, Math.min(tEnd, t)); idx = idxAt(curTime); render(); }

  el("b-play").addEventListener("click", function () {
    playing = !playing;
    if (playing && curTime >= tEnd) curTime = 0;
    syncPlay();
  });
  el("b-scrub").addEventListener("input", function () {
    playing = false; syncPlay();
    seek(tEnd * (Number(el("b-scrub").value) / 1000));
  });
  el("b-rate").addEventListener("change", function () { rate = Number(el("b-rate").value) || 1; });
  el("b-follow").addEventListener("change", function () {
    follow = el("b-follow").checked;
    el("b-follow").parentNode.classList.toggle("on", follow);
    if (follow) render();
  });
  el("b-sat").addEventListener("change", function () {
    satOn = el("b-sat").checked; setPref("rc3.sat", satOn);
    if (map.getLayer("sat")) map.setLayoutProperty("sat", "visibility", satOn ? "visible" : "none");
  });
  el("b-ter").addEventListener("change", function () {
    terOn = el("b-ter").checked; setPref("rc3.ter", terOn);
    applyTerrain();
  });
  el("b-zoom").addEventListener("input", function () {
    zoom = Number(el("b-zoom").value); if (follow) render();
  });
  function applyTerrain() {
    if (!DEM) return;
    if (terOn) map.setTerrain({ source: "dem", exaggeration: 1.15 });
    else map.setTerrain(null);
  }
  // Dragging means "I want to look around" — release the chase camera.
  map.on("dragstart", function () {
    if (follow) { follow = false; el("b-follow").checked = false; }
  });
  map.on("pitchend", function () { PITCH = map.getPitch(); });
  map.on("zoomend", function () { zoom = map.getZoom(); el("b-zoom").value = String(zoom); });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === " ") { ev.preventDefault(); el("b-play").click(); }
    else if (ev.key === "ArrowRight") seek(curTime + 2);
    else if (ev.key === "ArrowLeft") seek(curTime - 2);
  });

  // ---- boot ---------------------------------------------------------------
  map.on("load", function () {
    applyTerrain();
    fetch("/sessions/" + encodeURIComponent(USER) + "/" + encodeURIComponent(FILE) +
          "/data?target=30000")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        S = (d.samples || []).filter(function (s) {
          return typeof s.lat === "number" && typeof s.lon === "number" &&
                 (s.lat || s.lon) && Math.abs(s.lat) <= 90;
        });
        if (S.length < 10) { notice("no GPS fixes in this session", true); return; }
        return fetch("/sessions/" + encodeURIComponent(USER) + "/" +
                     encodeURIComponent(FILE) + "/laps")
          .then(function (r) { return r.json(); })
          .catch(function () { return {}; })
          .then(function (lj) {
            LAPS = (lj && lj.laps) || [];
            buildTimeline();
            buildBearings();
            buildLaps();
            addLineLayers();
            addIdealLines();
            hideNotice();
            el("hud").style.display = "block";
            curTime = 0; idx = 0;
            render();
            requestAnimationFrame(frame);
          });
      })
      .catch(function (e) { notice("could not load session: " + e.message, true); });
  });
})();
</script>
</body></html>"""
)

_TRACK3D_HTML = (
    """<!doctype html>
<html lang="en"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>3D drive · __FILE__</title>
<style>
  :root { --bg:#0E1014; --surface:#181B22; --line:#2A2F3A; --text:#E6E8EE;
          --muted:#8A92A3; --good:#6CD07A; --warn:#FFB020; --bad:#FF4D4D; }
  * { box-sizing: border-box; }
  html, body { margin:0; height:100%; background:var(--bg); color:var(--text);
    font:13px/1.4 Inter, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
    overflow:hidden; }
  #view { position:absolute; inset:0 0 52px 0; display:block; width:100%; height:calc(100% - 52px); }
  #hud { position:absolute; left:14px; bottom:64px; z-index:10; pointer-events:none;
    background:rgba(14,16,20,0.62); border:1px solid var(--line); border-radius:10px;
    padding:10px 16px; min-width:228px; }
  #hud .spd { display:flex; align-items:flex-end; gap:6px; }
  #hud .spd .v { font:700 52px/0.9 ui-monospace, Menlo, Consolas, monospace; letter-spacing:-3px; }
  #hud .spd .u { color:var(--muted); font:600 12px Inter,sans-serif; padding-bottom:6px; }
  #hud .bar { height:5px; border-radius:3px; background:#252A33; margin:9px 0 7px; overflow:hidden; }
  #hud .bar i { display:block; height:100%; width:0%; background:var(--warn); }
  #hud .row { display:flex; justify-content:space-between; gap:14px;
    font:600 12px ui-monospace, Menlo, Consolas, monospace; margin-top:3px; }
  #hud .row .k { color:var(--muted); font-weight:400; }
  #legend { position:absolute; right:14px; top:14px; z-index:10; pointer-events:none;
    background:rgba(14,16,20,0.62); border:1px solid var(--line); border-radius:8px; padding:8px 12px; }
  #legend .li { display:flex; align-items:center; gap:7px; margin:3px 0;
    font:500 11px Inter,sans-serif; white-space:nowrap; }
  #legend .dot { width:10px; height:10px; border-radius:50%; flex:0 0 auto; border:1.5px solid #000; }
  #legend .sw { width:22px; height:4px; border-radius:2px; flex:0 0 auto; }
  /* plan view: where you are, and what is coming next */
  #mini { position:absolute; right:14px; bottom:64px; z-index:10; display:none;
    background:rgba(14,16,20,0.66); border:1px solid var(--line);
    border-radius:10px; pointer-events:none; }
  #scalebar { position:absolute; left:14px; bottom:12px; z-index:12; display:none;
    align-items:center; gap:8px; color:var(--text);
    font:600 11px ui-monospace, Menlo, Consolas, monospace; text-shadow:0 0 4px #000; }
  #scalebar i { display:block; height:10px; width:140px;
    border-left:2px solid #fff; border-right:2px solid #fff; border-bottom:2px solid #fff; }
  #notice { position:absolute; left:50%; top:16px; transform:translateX(-50%); z-index:20;
    background:rgba(14,16,20,0.94); border:1px solid var(--line); border-radius:6px;
    padding:9px 15px; font:600 12px Inter,sans-serif; max-width:78vw; }
  #notice.err { border-color:var(--bad); color:#FFB0B0; }
  #bar { position:absolute; left:0; right:0; bottom:0; height:52px; z-index:15;
    display:flex; align-items:center; gap:9px; padding:0 12px; overflow-x:auto;
    background:var(--surface); border-top:1px solid var(--line); }
  #bar button { background:#20242E; color:var(--text); border:1px solid var(--line);
    border-radius:4px; padding:8px 13px; cursor:pointer; font:600 12px Inter,sans-serif;
    white-space:nowrap; }
  #bar button.on { background:var(--warn); border-color:transparent; color:#1A1300; }
  #bar input[type=range] { accent-color:var(--warn); }
  #bar select { background:#20242E; color:var(--text); border:1px solid var(--line);
    border-radius:4px; padding:7px 8px; font:600 12px Inter,sans-serif; }
  #bar label { display:flex; align-items:center; gap:6px; white-space:nowrap; cursor:pointer;
    font:600 12px Inter,sans-serif; }
  #bar input[type=checkbox] { width:15px; height:15px; margin:0; accent-color:var(--warn); cursor:pointer; }
  #bar .meta { color:var(--muted); font:500 12px ui-monospace, Menlo, Consolas, monospace;
    white-space:nowrap; }
  #bar a { color:var(--muted); text-decoration:none; border:1px solid var(--line);
    border-radius:4px; padding:8px 10px; font:600 12px Inter,sans-serif; white-space:nowrap; }
  #bar a:hover { color:var(--text); }
  #bar .sep { width:1px; height:26px; background:var(--line); flex:0 0 auto; }
</style></head><body>
<canvas id="view"></canvas>
<div id="notice">loading session…</div>
<div id="hud" style="display:none">
  <div class="spd"><span class="v" id="h-mph">0</span><span class="u">mph</span></div>
  <div class="bar"><i id="h-bar"></i></div>
  <div class="row"><span class="k">rpm</span><span id="h-rpm">—</span></div>
  <div class="row"><span class="k">lap</span><span id="h-lap">—</span></div>
  <div class="row"><span class="k">lap time</span><span id="h-lapt">—</span></div>
  <div class="row"><span class="k">best</span><span id="h-best">—</span></div>
  <div class="row"><span class="k">altitude</span><span id="h-alt">—</span></div>
  <div class="row"><span class="k">long g</span><span id="h-g">—</span></div>
</div>
<div id="legend">
  <div class="li"><span class="sw" style="background:#5CE07F"></span>accelerating</div>
  <div class="li"><span class="sw" style="background:#767A85"></span>neither (steady / coasting)</div>
  <div class="li"><span class="sw" style="background:#FF4D4D"></span>braking</div>
  <div class="li" id="lg-g" style="color:var(--muted)">red deepens with g, green with rate</div>
  <div class="li" id="lg-ideal" style="display:none"><span class="sw" style="background:#6CD07A"></span>ideal line (fastest real lap)</div>
  <div class="li"><span class="dot" style="background:#FF4D4D"></span>brake</div>
  <div class="li"><span class="dot" style="background:#FFB020"></span>apex</div>
  <div class="li"><span class="dot" style="background:#6CD07A"></span>throttle</div>
  <div class="li" id="lg-corner" style="display:none"></div>
  <div class="li" id="lg-track" style="display:none"></div>
</div>
<canvas id="mini" width="200" height="200"></canvas>
<div id="scalebar"><i></i><span id="scale-txt">—</span></div>
<div id="bar">
  <button id="b-play">▶ drive</button>
  <input id="b-scrub" type="range" min="0" max="1000" value="0" step="1" style="flex:1 1 150px;min-width:100px">
  <span class="meta" id="b-clock">0:00.0</span>
  <select id="b-rate" title="playback speed">
    <option value="1">1×</option><option value="2">2×</option><option value="4">4×</option>
    <option value="0.25">¼×</option><option value="0.5">½×</option>
  </select>
  <select id="b-lap" title="which lap to drive"></select>
  <span class="sep"></span>
  <label title="smoothing window over the 25 Hz GPS — higher is smoother but rounds off real detail">
    <span class="meta">smooth</span>
    <input id="b-smooth" type="range" min="1" max="15" step="2" value="5" style="width:74px"></label>
  <label title="eye height above the track surface (scroll wheel too)">
    <span class="meta">eye</span>
    <input id="b-eye" type="range" min="0.5" max="4" step="0.05" value="1.15" style="width:74px"></label>
  <label title="width of the ribbon drawn around your line">
    <span class="meta">road</span>
    <input id="b-road" type="range" min="6" max="24" step="1" value="12" style="width:74px"></label>
  <span class="sep"></span>
  <select id="b-view" title="chase = driving view; plan = whole circuit from above, over the real imagery">
    <option value="chase">chase</option>
    <option value="plan">plan</option>
  </select>
  <label id="b-ground-lab" style="display:none" title="drape the prepared satellite imagery on the ground: real asphalt, kerbs, grass and run-off"><input type="checkbox" id="b-ground" checked>imagery ground</label>
  <button id="b-prep" style="display:none" type="button" title="pre-render this track from satellite imagery + OpenStreetMap on the server (once per track)">prepare track</button>
  <label title="road colour = your inputs, not your speed: green accelerating, grey neither, red braking (deeper with the g)"><input type="checkbox" id="b-speedcol" checked>accel / brake</label>
  <label title="render scale: higher supersamples the view, which is what removes jagged/crawling edges. Pick 1x if the GPU struggles"><span class="meta">sharp</span>
    <select id="b-scale"><option value="1">1×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label>
  <label><input type="checkbox" id="b-markers" checked>markers</label>
  <label title="numbered brake boards (5 4 3 2 1 = hundreds of metres) before the corners that need them — tight corners get the full ladder, gentle bends get none"><input type="checkbox" id="b-brakes" checked>brake boards</label>
  <label><input type="checkbox" id="b-ghost">other laps</label>
  <label><input type="checkbox" id="b-loop" checked>loop</label>
  <label title="lean the camera into corners (computed from the path curvature)"><input type="checkbox" id="b-bank" checked>bank</label>
  <span class="sep"></span>
  <a href="/review/__USER__/__FILE__">← pit wall</a>
  <a href="/map3d/__USER__/__FILE__" title="the same session on satellite imagery with 3D terrain">satellite map</a>
</div>
<script type="module">
import * as THREE from 'https://unpkg.com/three@0.160.0/build/three.module.js';

/* ===========================================================================
   /track3d — first-person DRIVING view, rendered from DATA ONLY.

   No imagery, no map tiles, no DEM. The road is a ribbon built from the line
   the car actually drove, laid on the elevation the log recorded (`alt_m`;
   flat for sessions that predate altitude logging). Everything on screen is
   derived from the session: the ribbon, its speed colouring, the corner kerbs,
   the brake/apex/throttle markers and the start/finish gantry.

   ⚠️ The track WIDTH is synthetic (a constant-width ribbon around the driven
   line) because we do not log track edges. The line, its shape, its corners,
   its elevation and its speeds are all real; the asphalt either side of it is
   a drawing. A "satellite map" variant of the same session lives at /map3d.

   The 25 Hz samples become a continuous path once, up front:
     moving average (GPS lateral noise is 1-2 m) -> centripetal Catmull-Rom
     -> arc-length table (with enough LUT divisions that the table is fine
     relative to the path, which is what makes the spacing even).
   At render time the playback clock maps to arc length with a monotonic
   Catmull-Rom IN TIME, clamped to the bracketing samples so the speed can
   neither stall nor overshoot; 60 fps motion out of 40 ms data.
   =========================================================================== */

(function () {
  "use strict";
  // Signals the (non-module) watchdog below that the CDN import succeeded: if
  // this never runs, the page shows a way out instead of a stuck "loading".
  window.__track3dReady = true;
  var USER = "__USER__", FILE = "__FILE__";
  var q = new URLSearchParams(location.search);
  var pts = (q.get("pts") || "").split("|").map(function (s) {
    return s.split(",").map(Number);
  }).filter(function (a) { return a.length === 2 && isFinite(a[0]) && isFinite(a[1]); });

  function el(id) { return document.getElementById(id); }
  function notice(msg, err) {
    var n = el("notice");
    n.style.display = "block";
    n.textContent = msg;
    n.className = err ? "err" : "";
  }
  function hideNotice() { el("notice").style.display = "none"; }
  function fmtClock(s) {
    if (!isFinite(s)) return "0:00.0";
    s = Math.max(0, s);
    var m = Math.floor(s / 60), r = s - m * 60;
    return m + ":" + (r < 10 ? "0" : "") + r.toFixed(1);
  }
  function fmtLap(s) {
    if (!isFinite(s) || s <= 0) return "—";
    var m = Math.floor(s / 60), r = s - m * 60;
    return (m ? m + ":" + (r < 10 ? "0" : "") : "") + r.toFixed(2);
  }

  /* =======================================================================
     1. PURE LAYER — data in, geometry out. No DOM, no GL. Exposed as RC3D so
        the host test (tests/) can drive it with real sessions and synthetic
        arcs; that is the only way to check this maths without a browser.
     ======================================================================= */
  var RC3D = {};
  var M_LAT = 111320;

  // WGS84 -> local metres. +x = east, -z = north, +y = up.
  RC3D.project = function (lat, lon, o) {
    return {
      x: (lon - o.lon) * M_LAT * Math.cos(o.lat * Math.PI / 180),
      y: 0,
      z: -(lat - o.lat) * M_LAT
    };
  };

  // Centred moving average, edge-clamped, one pass (no drift, no phase shift).
  RC3D.smooth = function (arr, win) {
    var r = Math.max(0, Math.floor((win - 1) / 2)), out, i, j, a, b, s, n;
    if (!r) return arr.slice();
    out = new Array(arr.length);
    for (i = 0; i < arr.length; i++) {
      a = Math.max(0, i - r); b = Math.min(arr.length - 1, i + r);
      s = 0; n = 0;
      for (j = a; j <= b; j++) { s += arr[j]; n++; }
      out[i] = s / n;
    }
    return out;
  };

  // Nulls (no fix / no altitude) carry the nearest real value, so a dropout
  // never becomes a 0 m cliff. Nothing real => `fallback` everywhere.
  RC3D.fillNulls = function (arr, fallback) {
    var out = new Array(arr.length), i, last = null, first = null;
    for (i = 0; i < arr.length; i++) if (first === null && isFinite(arr[i])) first = arr[i];
    if (first === null) first = fallback || 0;
    for (i = 0; i < arr.length; i++) {
      if (isFinite(arr[i])) { last = arr[i]; out[i] = arr[i]; }
      else out[i] = (last === null ? first : last);
    }
    return out;
  };

  // Epoch `t`, else relative `t_ms`, else synthetic 25 Hz; always shifted so
  // the first sample is 0 (the server's lap times use the same base).
  RC3D.timeline = function (samples) {
    var first = null, i, s, v;
    var raw = new Array(samples.length);
    for (i = 0; i < samples.length; i++) {
      s = samples[i]; v = null;
      if (typeof s.t === "number" && isFinite(s.t)) v = s.t;
      else if (typeof s.t_ms === "number" && isFinite(s.t_ms)) v = s.t_ms / 1000;
      if (v !== null && first === null) first = v;
      raw[i] = v;
    }
    if (first === null) { for (i = 0; i < samples.length; i++) raw[i] = i / 25; return raw; }
    var last = first;
    for (i = 0; i < raw.length; i++) {
      if (raw[i] === null) { raw[i] = last; } else { last = raw[i]; raw[i] -= first; }
    }
    return raw;
  };

  // The drivable path: smoothed positions, cumulative arc length, and a dense
  // (~denseStep m) centreline with its own arc-length table + XZ tangents.
  RC3D.buildPath = function (samples, opts) {
    opts = opts || {};
    var win = opts.smooth == null ? 5 : opts.smooth;
    var step = opts.denseStep || 1.0;
    var maxCp = opts.maxCurvePoints || 2400;
    var i, n = samples.length;
    var lat = new Array(n), lon = new Array(n), alt = new Array(n), mph = new Array(n);
    for (i = 0; i < n; i++) {
      var s = samples[i];
      lat[i] = (typeof s.lat === "number") ? s.lat : NaN;
      lon[i] = (typeof s.lon === "number") ? s.lon : NaN;
      alt[i] = (typeof s.alt_m === "number" && isFinite(s.alt_m)) ? s.alt_m : NaN;
      mph[i] = (typeof s.speed_mph === "number" && isFinite(s.speed_mph)) ? s.speed_mph : 0;
    }
    // origin = mean of the valid fixes (keeps the local projection tight)
    var sla = 0, slo = 0, na = 0;
    for (i = 0; i < n; i++)
      if (isFinite(lat[i]) && isFinite(lon[i]) && (lat[i] || lon[i])) { sla += lat[i]; slo += lon[i]; na++; }
    var o = na ? { lat: sla / na, lon: slo / na } : { lat: 0, lon: 0 };

    var X = new Array(n), Z = new Array(n);
    for (i = 0; i < n; i++) {
      var p = RC3D.project(isFinite(lat[i]) ? lat[i] : o.lat,
                           isFinite(lon[i]) ? lon[i] : o.lon, o);
      X[i] = p.x; Z[i] = p.z;
    }
    // Altitude is logged in metres MSL: reference it to the session minimum so
    // a mountain track is not floating at its real elevation above the ground
    // plane (and so float precision stays usable).
    var Y = RC3D.smooth(RC3D.fillNulls(alt, 0), Math.max(3, win + 4));
    var yMin = Infinity;
    for (i = 0; i < n; i++) if (Y[i] < yMin) yMin = Y[i];
    if (!isFinite(yMin)) yMin = 0;
    for (i = 0; i < n; i++) Y[i] -= yMin;
    // lateral smoothing is the whole point: GPS noise is 1-2 m at 25 Hz
    X = RC3D.smooth(X, win);
    Z = RC3D.smooth(Z, win);
    mph = RC3D.smooth(RC3D.fillNulls(mph, 0), Math.max(3, win));

    var t = RC3D.timeline(samples);

    // Longitudinal acceleration in g, from SPEED (not RPM): it is gear
    // independent, which is the whole point — 1st and 4th cannot be compared on
    // an rpm rate, and we do not log gear. Braking is the same measurement with
    // a negative sign, so the "how red" scale is a real g figure.
    var acc = new Array(n);
    for (i = 0; i < n; i++) {
      var i0 = Math.max(0, i - 1), i1 = Math.min(n - 1, i + 1);
      var dt = t[i1] - t[i0];
      acc[i] = dt > 0.001 ? (((mph[i1] - mph[i0]) * 0.44704) / dt) / 9.80665 : 0;
    }
    acc = RC3D.smooth(acc, Math.max(3, win));      // one more pass to kill 25 Hz noise
    var cum = new Array(n);
    cum[0] = 0;
    for (i = 1; i < n; i++) {
      var dx = X[i] - X[i - 1], dz = Z[i] - Z[i - 1];
      cum[i] = cum[i - 1] + Math.sqrt(dx * dx + dz * dz);
    }
    var total = cum[n - 1] || 1;

    // ---- dense centreline ------------------------------------------------
    // Control points are decimated (≤maxCurvePoints) so a 50 km session does
    // not build an enormous spline; the smoothing above already removed the
    // noise that decimation would otherwise expose. Guarded on THREE so a
    // partial/missing library degrades to the smoothed polyline below instead
    // of throwing.
    var canCurve = typeof THREE.Vector3 === "function" &&
                   typeof THREE.CatmullRomCurve3 === "function";
    var cp = [], every = Math.max(1, Math.ceil(n / maxCp));
    if (canCurve) {
      for (i = 0; i < n; i += every) cp.push(new THREE.Vector3(X[i], Y[i], Z[i]));
      if (cp.length > 1 && n > 1) cp.push(new THREE.Vector3(X[n - 1], Y[n - 1], Z[n - 1]));
    }

    var dx2 = [], dy2 = [], dz2 = [], ds2 = [], dTotal = 0;
    if (canCurve && cp.length > 1) {
      var curve = new THREE.CatmullRomCurve3(cp, false, "centripetal", 0.5);
      // getSpacedPoints() is arc-length parameterised through a LUT. The three
      // default (200 divisions for the WHOLE curve) would sample a 50 km path
      // every 250 m and return visibly uneven spacing, so scale it with the
      // control-point count.
      curve.arcLengthDivisions = Math.min(400000, Math.max(400, cp.length * 3));
      var count = Math.max(2, Math.min(200000, Math.ceil(total / step)));
      var spaced = curve.getSpacedPoints(count);
      for (i = 0; i < spaced.length; i++) {
        var v = spaced[i];
        dx2.push(v.x); dy2.push(v.y); dz2.push(v.z);
        if (i) {
          var ax = v.x - spaced[i - 1].x, ay = v.y - spaced[i - 1].y, az = v.z - spaced[i - 1].z;
          dTotal += Math.sqrt(ax * ax + ay * ay + az * az);
        }
        ds2.push(dTotal);
      }
    } else {
      for (i = 0; i < n; i++) { dx2.push(X[i]); dy2.push(Y[i]); dz2.push(Z[i]); ds2.push(cum[i]); }
      dTotal = total;
    }

    var tan = new Array(dx2.length);
    for (i = 0; i < dx2.length; i++) {
      var a = Math.max(0, i - 1), b = Math.min(dx2.length - 1, i + 1);
      var tx = dx2[b] - dx2[a], tz = dz2[b] - dz2[a];
      var L = Math.sqrt(tx * tx + tz * tz) || 1;
      tan[i] = [tx / L, tz / L];
    }
    return {
      o: o, n: n, x: X, y: Y, z: Z, t: t, speed: mph, accel: acc, cum: cum, total: total,
      yRef: yMin, dense: { x: dx2, y: dy2, z: dz2, s: ds2, total: dTotal, tan: tan }
    };
  };

  // Arc length at a time: a MONOTONE cubic (Fritsch-Carlson / PCHIP) through the
  // samples. A plain Catmull-Rom is C1 but not monotone - on real GPS, where the
  // per-sample spacing wobbles, its cubic can dip backwards mid-segment, which
  // shows up as a tiny stutter every 40 ms. The limiter forces the interpolant
  // to stay between its bracketing samples, so speed can neither stall nor
  // overshoot, and it is still C1 (no visible kinks).
  function _secant(t, cum, k) {
    var dt = t[k + 1] - t[k];
    return dt > 0 ? (cum[k + 1] - cum[k]) / dt : 0;
  }
  function _pchip(dA, dB, dtA, dtB) {
    if (!(dA > 0) && !(dA < 0)) return 0;      // 0 / NaN
    if (!(dB > 0) && !(dB < 0)) return 0;
    if (dA * dB <= 0) return 0;                // a turning point: slope 0 there
    var w1 = 2 * dtB + dtA, w2 = dtB + 2 * dtA;
    return (w1 + w2) / (w1 / dA + w2 / dB);
  }
  RC3D.sAtTime = function (path, tt) {
    var t = path.t, cum = path.cum, n = t.length, lo, hi, mid, i, dt, f, s;
    var dPrev, dSeg, dNext, dtPrev, dtNext, m0, m1, f2, f3;
    if (!n) return 0;
    if (tt <= t[0]) return 0;
    if (tt >= t[n - 1]) return path.total;
    lo = 0; hi = n - 1;
    while (lo < hi) { mid = (lo + hi + 1) >> 1; if (t[mid] <= tt) lo = mid; else hi = mid - 1; }
    i = Math.min(lo, n - 2);
    dt = t[i + 1] - t[i];
    if (!(dt > 0)) return cum[i];
    f = (tt - t[i]) / dt;
    dSeg = _secant(t, cum, i);
    dtPrev = i > 0 ? t[i] - t[i - 1] : dt;
    dPrev = i > 0 ? _secant(t, cum, i - 1) : dSeg;
    dtNext = i + 2 <= n - 1 ? t[i + 2] - t[i + 1] : dt;
    dNext = i + 2 <= n - 1 ? _secant(t, cum, i + 1) : dSeg;
    m0 = _pchip(dPrev, dSeg, dtPrev, dt);
    m1 = _pchip(dSeg, dNext, dt, dtNext);
    f2 = f * f; f3 = f2 * f;
    s = (2 * f3 - 3 * f2 + 1) * cum[i] + (f3 - 2 * f2 + f) * dt * m0 +
        (-2 * f3 + 3 * f2) * cum[i + 1] + (f3 - f2) * dt * m1;
    if (s < cum[i]) s = cum[i];          // belt-and-braces: the limiter already
    if (s > cum[i + 1]) s = cum[i + 1];  // guarantees this
    return s;
  };

  // Arc length -> point + tangent on the dense centreline.
  RC3D.pointAtS = function (path, s) {
    var d = path.dense, n = d.s.length, lo, hi, mid, i, seg, f, scale, target;
    if (!n) return { x: 0, y: 0, z: 0, tan: [0, -1], s: 0 };
    scale = path.total > 0 ? d.total / path.total : 1;
    target = Math.max(0, Math.min(d.total, s * scale));
    lo = 0; hi = n - 1;
    while (lo < hi) { mid = (lo + hi + 1) >> 1; if (d.s[mid] <= target) lo = mid; else hi = mid - 1; }
    i = Math.min(lo, n - 2);
    seg = d.s[i + 1] - d.s[i];
    f = seg > 0 ? (target - d.s[i]) / seg : 0;
    var t0 = d.tan[i], t1 = d.tan[i + 1];
    var tx = t0[0] + (t1[0] - t0[0]) * f, tz = t0[1] + (t1[1] - t0[1]) * f;
    var L = Math.sqrt(tx * tx + tz * tz) || 1;
    return {
      x: d.x[i] + (d.x[i + 1] - d.x[i]) * f,
      y: d.y[i] + (d.y[i + 1] - d.y[i]) * f,
      z: d.z[i] + (d.z[i + 1] - d.z[i]) * f,
      tan: [tx / L, tz / L], s: target, i: i
    };
  };

  // Speed at arc length s (nearest sample) — ribbon colour + HUD.
  RC3D.mphAtS = function (path, s) {
    var cum = path.cum, n = cum.length, lo, hi, mid;
    if (!n) return 0;
    lo = 0; hi = n - 1;
    while (lo < hi) { mid = (lo + hi + 1) >> 1; if (cum[mid] <= s) lo = mid; else hi = mid - 1; }
    return path.speed[lo] || 0;
  };

  // Longitudinal g at arc length s (nearest sample).
  RC3D.accelAtS = function (path, s) {
    var cum = path.cum, a = path.accel, n = cum.length, lo, hi, mid;
    if (!n || !a) return 0;
    lo = 0; hi = n - 1;
    while (lo < hi) { mid = (lo + hi + 1) >> 1; if (cum[mid] <= s) lo = mid; else hi = mid - 1; }
    return a[lo] || 0;
  };

  // Road colour = what the driver is doing, not how fast:
  //   accelerating -> GREEN  (brighter with the rate; any real rise is green)
  //   braking      -> RED    (deeper with the deceleration in g)
  //   neither      -> GREY   (steady throttle, coasting, a corner taken flat)
  // Braking intensity is real g, read off speed, so 0.6 g reads full red.
  // Green is deliberately floored: the car's acceleration is not reliably
  // measurable from 25 Hz speed (nor comparable between gears), so we claim
  // "accelerating" and scale only mildly, rather than inventing a percentage.
  RC3D.NEUTRAL_GREY = [0.46, 0.48, 0.52];
  RC3D.driveColour = function (aG) {
    var thr = 0.035, accelFull = 0.28, brakeFull = 0.6;
    if (aG > thr) {
      var t = Math.max(0.40, Math.min(1, (aG - thr) / (accelFull - thr)));
      return [0.20 + 0.06 * (1 - t), 0.42 + 0.52 * t, 0.24 + 0.08 * (1 - t)];
    }
    if (aG < -thr) {
      var u = Math.max(0.32, Math.min(1, (-aG - thr) / (brakeFull - thr)));
      return [0.38 + 0.58 * u, 0.13 + 0.12 * (1 - u), 0.13 + 0.08 * (1 - u)];
    }
    return RC3D.NEUTRAL_GREY.slice();
  };

  // Blue -> green -> yellow -> red across [lo, hi] mph.
  RC3D.speedColour = function (mph, lo, hi) {
    var stops = [[0.15, 0.44, 0.90], [0.35, 0.85, 0.55], [1.0, 0.82, 0.29], [1.0, 0.30, 0.30]];
    var t = Math.max(0, Math.min(1, (mph - lo) / Math.max(1, hi - lo)));
    var f = t * (stops.length - 1), i = Math.min(stops.length - 2, Math.floor(f)), u = f - i;
    return [stops[i][0] + (stops[i + 1][0] - stops[i][0]) * u,
            stops[i][1] + (stops[i + 1][1] - stops[i][1]) * u,
            stops[i][2] + (stops[i + 1][2] - stops[i][2]) * u];
  };

  // Road ribbon: two vertices per centreline point, ±width/2 along the normal.
  // (nx,nz) = (-tz, tx) is the RIGHT-hand normal for +x east / -z north.
  RC3D.ribbon = function (path, width, lift, opt) {
    var d = path.dense, n = d.x.length, i;
    var pos = new Float32Array(n * 6), sArr = new Float32Array(n * 2);
    var uv = opt && opt.uv ? new Float32Array(n * 4) : null;
    var hw = width / 2, lf = lift || 0;
    var half = (opt && opt.half) || null;      // per-station [left,right] metres
    for (i = 0; i < n; i++) {
      var tx = d.tan[i][0], tz = d.tan[i][1];
      var nx = -tz, nz = tx;
      var x = d.x[i], y = d.y[i] + lf, z = d.z[i];
      var hl = half ? half[0][i] : hw, hr = half ? half[1][i] : hw;
      if (hl == null || !isFinite(hl) || hl < 2) hl = hw;
      if (hr == null || !isFinite(hr) || hr < 2) hr = hw;
      // driver's LEFT is (tz,-tx) and the right is (-tz,tx) — the perpendicular,
      // which is also what the asset's left/right widths are measured along
      pos[i * 6] = x + tz * hl; pos[i * 6 + 1] = y; pos[i * 6 + 2] = z - tx * hl;
      pos[i * 6 + 3] = x - tz * hr; pos[i * 6 + 4] = y; pos[i * 6 + 5] = z + tx * hr;
      sArr[i * 2] = d.s[i]; sArr[i * 2 + 1] = d.s[i];
      if (uv) {
        // real-imagery UVs: metres -> lat/lon -> the asset texture's bounds
        var ll = RC3D.localToLatLon(x + tz * hl, z - tx * hl, opt.o);
        var rr = RC3D.localToLatLon(x - tz * hr, z + tx * hr, opt.o);
        uv[i * 4] = (ll[1] - opt.uv.west) / (opt.uv.east - opt.uv.west);
        uv[i * 4 + 1] = (ll[0] - opt.uv.south) / (opt.uv.north - opt.uv.south);
        uv[i * 4 + 2] = (rr[1] - opt.uv.west) / (opt.uv.east - opt.uv.west);
        uv[i * 4 + 3] = (rr[0] - opt.uv.south) / (opt.uv.north - opt.uv.south);
      }
    }
    var idx = new Uint32Array(Math.max(0, (n - 1) * 6));
    for (i = 0; i < n - 1; i++) {
      var a = i * 2, b = i * 2 + 1, c = (i + 1) * 2, e = (i + 1) * 2 + 1;
      idx[i * 6] = a; idx[i * 6 + 1] = b; idx[i * 6 + 2] = c;
      idx[i * 6 + 3] = b; idx[i * 6 + 4] = e; idx[i * 6 + 5] = c;
    }
    return { position: pos, index: idx, s: sArr, uv: uv, count: n };
  };

  RC3D.localToLatLon = function (x, z, o) {
    return [o.lat - z / M_LAT,
            o.lon + x / (M_LAT * Math.cos(o.lat * Math.PI / 180))];
  };

  // The asset's own terrain grid: a DEM mesh for the ground, so the road does
  // not float over a flat quad on a circuit with real relief.
  RC3D.demMesh = function (grid, proj_o, ref, texBounds) {
    if (!grid || !grid.values || grid.cols < 2 || grid.rows < 2) return null;
    var b = grid.bounds;                      // [south, west, north, east]
    var pos = [], uv = [], idx = [];
    var cols = grid.cols, rows = grid.rows, r, c;
    var vals = grid.values;
    var y0 = (ref == null) ? Infinity : ref, y1 = -Infinity;
    for (var k = 0; k < vals.length; k++) {
      if (vals[k] < y0) y0 = vals[k];
      if (vals[k] > y1) y1 = vals[k];
    }
    for (r = 0; r < rows; r++) {
      var lat = b[0] + (b[2] - b[0]) * (r / (rows - 1));
      for (c = 0; c < cols; c++) {
        var lon = b[1] + (b[3] - b[1]) * (c / (cols - 1));
        var p = RC3D.project(lat, lon, proj_o);
        pos.push(p.x, vals[r * cols + c] - y0, p.z);
        if (texBounds) {
          // UV through the TEXTURE's own bounds, not 0..1 over the grid: the
          // mosaic extends past the DEM bbox (whole tiles), so stretching the
          // image across the grid would shift the ground imagery off the road.
          uv.push((lon - texBounds.west) / (texBounds.east - texBounds.west),
                  (lat - texBounds.south) / (texBounds.north - texBounds.south));
        } else {
          uv.push(c / (cols - 1), r / (rows - 1));
        }
      }
    }
    for (r = 0; r < rows - 1; r++) {
      for (c = 0; c < cols - 1; c++) {
        var a = r * cols + c, b2 = a + 1, cc = a + cols, dd = cc + 1;
        idx.push(a, cc, b2, b2, cc, dd);
      }
    }
    return { position: new Float32Array(pos), uv: new Float32Array(uv),
             index: new Uint32Array(idx), yMin: y0, yMax: y1 };
  };

  // Nearest prepared-asset station for a local (x,z): gives the REAL width and
  // the DEM elevation at that point. One spatial hash, built once per track.
  RC3D.assetSampler = function (asset, proj_o) {
    if (!asset || !asset.line || asset.line.length < 3) return null;
    var cell = 25, grid = {}, i;
    var n = asset.line.length;
    var xs = new Float64Array(n), zs = new Float64Array(n);
    for (i = 0; i < n; i++) {
      var p = RC3D.project(asset.line[i][0], asset.line[i][1], proj_o);
      xs[i] = p.x; zs[i] = p.z;
      var key = Math.floor(p.x / cell) + ":" + Math.floor(p.z / cell);
      (grid[key] || (grid[key] = [])).push(i);
    }
    var width = asset.width_m || [], elev = [];
    for (i = 0; i < n; i++) {
      var z = asset.line[i][2];
      elev.push(typeof z === "number" && isFinite(z) ? z : null);
    }
    return function (x, z2) {
      var gx = Math.floor(x / cell), gz = Math.floor(z2 / cell);
      var best = -1, bd = 1e9;
      for (var a = -1; a <= 1; a++) {
        for (var b = -1; b <= 1; b++) {
          var bucket = grid[(gx + a) + ":" + (gz + b)];
          if (!bucket) continue;
          for (var k = 0; k < bucket.length; k++) {
            var j = bucket[k];
            var dx = xs[j] - x, dz = zs[j] - z2, d = dx * dx + dz * dz;
            if (d < bd) { bd = d; best = j; }
          }
        }
      }
      if (best < 0) return null;
      var w = width[best];
      return { i: best, dist: Math.sqrt(bd),
               width_m: (w > 2 && w < 40) ? w : null,
               elev_m: elev[best] };
    };
  };

  // Bilinear read of the asset's terrain grid — the SAME field the ground mesh
  // is built from, which is what keeps the road welded to the terrain instead
  // of a few metres off it (per-station DEM samples vs a coarse mesh disagreed
  // by up to 2.7 m on the Shenandoah fixture, and it would have shown).
  RC3D.demAt = function (grid, lat, lon) {
    var b = grid.bounds, cols = grid.cols, rows = grid.rows, v = grid.values;
    var fr = (lat - b[0]) / (b[2] - b[0]);
    var fc = (lon - b[1]) / (b[3] - b[1]);
    fr = Math.max(0, Math.min(1, fr));
    fc = Math.max(0, Math.min(1, fc));
    var r0 = Math.floor(fr * (rows - 1)), c0 = Math.floor(fc * (cols - 1));
    var r1 = Math.min(rows - 1, r0 + 1), c1 = Math.min(cols - 1, c0 + 1);
    var tr = fr * (rows - 1) - r0, tc = fc * (cols - 1) - c0;
    var v00 = v[r0 * cols + c0], v01 = v[r0 * cols + c1];
    var v10 = v[r1 * cols + c0], v11 = v[r1 * cols + c1];
    return v00 * (1 - tr) * (1 - tc) + v01 * (1 - tr) * tc +
           v10 * tr * (1 - tc) + v11 * tr * tc;
  };

  // Re-seat the path on the prepared terrain. Road AND ground mesh then share
  // one reference, so the road can never float. The HUD still shows the alt_m
  // the car logged — a different measurement, and it stays honest.
  RC3D.applyAssetElevation = function (path, grid, proj_o) {
    var d = path.dense, i, yref = Infinity, ys = new Float64Array(d.x.length);
    for (i = 0; i < d.x.length; i++) {
      var ll = RC3D.localToLatLon(d.x[i], d.z[i], proj_o);
      ys[i] = RC3D.demAt(grid, ll[0], ll[1]);
      if (ys[i] < yref) yref = ys[i];
    }
    if (!isFinite(yref)) return path.yRef;
    for (i = 0; i < d.x.length; i++) d.y[i] = ys[i] - yref;
    path.yRef = yref;
    return yref;
  };

  // Per-station left/right half-widths (metres) pulled from a prepared track
  // asset, by nearest asset station in a spatial hash. This is the difference
  // between a synthetic 12 m ribbon and the track's ACTUAL width.
  RC3D.widthSampler = function (asset, proj_o, fallback) {
    if (!asset || !asset.line || asset.line.length < 3) return null;
    var cell = 25, grid = {}, i;
    var xs = new Float64Array(asset.line.length), zs = new Float64Array(asset.line.length);
    for (i = 0; i < asset.line.length; i++) {
      var p = RC3D.project(asset.line[i][0], asset.line[i][1], proj_o);
      xs[i] = p.x; zs[i] = p.z;
      var key = Math.floor(p.x / cell) + ":" + Math.floor(p.z / cell);
      (grid[key] || (grid[key] = [])).push(i);
    }
    var width = asset.width_m || [];
    return function (x, z) {
      var gx = Math.floor(x / cell), gz = Math.floor(z / cell), best = -1, bd = 1e9;
      for (var a = -1; a <= 1; a++) {
        for (var b = -1; b <= 1; b++) {
          var bucket = grid[(gx + a) + ":" + (gz + b)];
          if (!bucket) continue;
          for (var k = 0; k < bucket.length; k++) {
            var j = bucket[k];
            var dx = xs[j] - x, dz = zs[j] - z, d = dx * dx + dz * dz;
            if (d < bd) { bd = d; best = j; }
          }
        }
      }
      if (best < 0) return fallback;
      var w = width[best];
      if (!(w > 2) || w > 40) return fallback;
      return w * (asset.width_scale || 1);
    };
  };

  // Kerb blocks on both edges where the path curves, alternating red/white —
  // this is what makes a data-only track read as a track. One clean QUAD per
  // block (aligned to the arc-length grid, one per edge), not per-centimetre
  // triangles: chunky overlapping geometry was visibly ragged along the edges.
  RC3D.kerbs = function (path, width, blockM) {
    var d = path.dense, n = d.x.length, i, side, base;
    var pos = [], col = [], red = [0.82, 0.24, 0.24], white = [0.90, 0.91, 0.94];
    var hw = width / 2, kw = 0.45, block = blockM || 2;
    if (n < 4) return { position: new Float32Array(0), colour: new Float32Array(0), count: 0 };
    var quad = function (x, z, nx, nz, tx, tz, half, c, y) {
      var ox = nx * hw, oz = nz * hw;
      var ix = nx * (hw + kw), iz = nz * (hw + kw);
      var p = [
        [x + ox - tx * half, z + oz - tz * half],
        [x + ox + tx * half, z + oz + tz * half],
        [x + ix + tx * half, z + iz + tz * half],
        [x + ix - tx * half, z + iz - tz * half]
      ];
      // two triangles, consistent winding (double-sided material anyway)
      [0, 1, 2, 0, 2, 3].forEach(function (k) {
        pos.push(p[k][0], y, p[k][1]);
        col.push(c[0], c[1], c[2]);
      });
    };
    var blockNo = 0, lastBlock = -1;
    for (i = 1; i < n - 1; i++) {
      base = Math.floor(d.s[i] / block);
      if (base === lastBlock) continue;
      lastBlock = base;
      var t0 = d.tan[i - 1], t1 = d.tan[i + 1];
      // curvature over the block: skip the straights entirely
      if (Math.abs(t0[0] * t1[1] - t0[1] * t1[0]) < 0.0016) { blockNo++; continue; }
      var c = (blockNo % 2) ? red : white;
      blockNo++;
      var tx = d.tan[i][0], tz = d.tan[i][1], nx = -tz, nz = tx;
      for (side = -1; side <= 1; side += 2) {
        quad(d.x[i], d.z[i], nx * side, nz * side, tx, tz, block / 2,
             c, d.y[i] + 0.045);
      }
    }
    return { position: new Float32Array(pos), colour: new Float32Array(col), count: pos.length / 3 };
  };

  // One consistent answer to "where am I right now": the camera pose, the
  // surface point, the speed and the lap progress, all from the same instant.
  // The renderer, the HUD and the mini-map all read THIS, so they cannot
  // disagree about the car's position.
  RC3D.frameState = function (path, tNow, lap, opt) {
    opt = opt || {};
    var tEnd = path.t.length ? path.t[path.t.length - 1] : 0;
    var tA = lap ? lap.t_start : 0;
    var tB = lap ? lap.t_end : tEnd;
    if (!(tB > tA)) tB = tA + 1;
    var t = Math.max(tA, Math.min(tB, tNow));
    var s = RC3D.sAtTime(path, t);
    var pos = RC3D.pointAtS(path, s);
    var mph = RC3D.mphAtS(path, s);
    var latG = opt.bank === false ? 0 : RC3D.latAccel(path, s, mph);
    var pose = RC3D.cameraPose(path, s, mph, { eye: opt.eye, latG: latG,
                                               bank: opt.bank !== false });
    return { t: t, s: s, mph: mph, pos: pos, eye: pose.eye, target: pose.target,
             roll: pose.roll, lead: pose.lead, latG: latG,
             progress: Math.max(0, Math.min(1, (t - tA) / (tB - tA))) };
  };

  // Free-look decay: the mouse gives a temporary look-around, and it eases back
  // to straight-ahead so the view can never be silently left pointing somewhere
  // other than where the car is going (the "it doesn't follow me" trap).
  RC3D.recentreLook = function (look, dt, on) {
    if (!on) return look;
    var k = Math.max(0, 1 - dt * 3.5);
    look.yaw *= k;
    look.pitch *= k;
    if (Math.abs(look.yaw) < 1e-4) look.yaw = 0;
    if (Math.abs(look.pitch) < 1e-4) look.pitch = 0;
    return look;
  };

  RC3D.indexOfTime = function (t, target) {
    var lo = 0, hi = t.length - 1, mid;
    if (!t.length) return 0;
    if (target <= t[0]) return 0;
    if (target >= t[hi]) return hi;
    while (lo < hi) { mid = (lo + hi + 1) >> 1; if (t[mid] <= target) lo = mid; else hi = mid - 1; }
    return lo;
  };

  // Per-lap corner markers from the logged speed trace: apex = slowest point of
  // the lap, brake = where the deceleration into it began, throttle = where the
  // speed starts climbing again. Cheap and honest about what it is.
  // ---- corners + brake boards -------------------------------------------
  // Real circuits mark the braking zone of a corner with numbered boards (5 4 3
  // 2 1 = hundreds of metres) and only bother where they are needed: a tight
  // corner gets the lot, a gentle bend gets none. Severity is the TOTAL heading
  // change through the corner, measured on the smoothed path, so it works on a
  // driven line or on an OSM-traced one.
  //
  //   >= 80 deg  ->  5 4 3 2 1   (hairpin / near-90: the full ladder)
  //   >= 62 deg  ->  3 2 1
  //   >= 45 deg  ->  2 1
  //   <  45 deg  ->  nothing (a kink needs no braking reference)
  RC3D.MARKER_TIERS = [[80, [500, 400, 300, 200, 100]],
                       [62, [300, 200, 100]],
                       [45, [200, 100]]];

  RC3D.corners = function (path, opt) {
    opt = opt || {};
    var d = path.dense, n = d.x.length, i;
    var minDeg = opt.min_deg == null ? 45 : opt.min_deg;
    var minRadius = opt.min_radius_m == null ? 260 : opt.min_radius_m;
    var r = Math.max(2, Math.round((opt.smooth_m == null ? 12 : opt.smooth_m) / 2));
    if (n < 8) return [];
    // cumulative compass heading (0 = north, +ve = clockwise = turning right)
    var a = new Float64Array(n), acc = 0, prev = Math.atan2(d.tan[0][0], -d.tan[0][1]);
    a[0] = 0;
    for (i = 1; i < n; i++) {
      var cur = Math.atan2(d.tan[i][0], -d.tan[i][1]);
      var diff = cur - prev;
      while (diff > Math.PI) diff -= 2 * Math.PI;
      while (diff < -Math.PI) diff += 2 * Math.PI;
      acc += diff;
      a[i] = acc;
      prev = cur;
    }
    var sm = new Float64Array(n);
    for (i = 0; i < n; i++) {
      var p0 = Math.max(0, i - r), p1 = Math.min(n - 1, i + r);
      sm[i] = (a[p1] - a[p0]) / Math.max(0.001, d.s[p1] - d.s[p0]);   // rad per metre
    }
    var kMax = 1 / minRadius, run = null, out = [];
    var mergeM = opt.merge_m == null ? 22 : opt.merge_m;
    for (i = 0; i < n; i++) {
      var k = sm[i], on = Math.abs(k) > kMax;
      if (on && !run) run = { i0: i, i1: i, sign: k > 0 ? 1 : -1, kbest: Math.abs(k), apex: i };
      else if (on && run) {
        run.i1 = i;
        if (Math.abs(k) > run.kbest) { run.kbest = Math.abs(k); run.apex = i; run.sign = k > 0 ? 1 : -1; }
      } else if (!on && run) {
        var gap = 0, j = i;
        while (j < n && Math.abs(sm[j]) <= kMax && d.s[j] - d.s[i] < mergeM) { j++; }
        if (j < n - 1 && Math.abs(sm[j]) > kMax) continue;   // same corner, keep going
        out.push(run);
        run = null;
      }
    }
    if (run) out.push(run);
    var corners = [];
    for (i = 0; i < out.length; i++) {
      var c = out[i];
      var deg = (a[c.i1] - a[c.i0]) * 180 / Math.PI;
      if (Math.abs(deg) < minDeg) continue;
      corners.push({ s0: d.s[c.i0], s1: d.s[c.i1], apex_s: d.s[c.apex],
                     i0: c.i0, i1: c.i1, deg: deg, dir: deg > 0 ? 1 : -1,
                     radius_m: Math.abs(deg) < 0.001 ? 1e6 : (d.s[c.i1] - d.s[c.i0]) /
                               (Math.abs(deg) * Math.PI / 180) });
    }
    return corners;
  };

  RC3D.brakeMarkers = function (path, corners, opt) {
    opt = opt || {};
    var minGapM = opt.min_gap_m == null ? 30 : opt.min_gap_m;
    var out = [], prevExit = -1e9, c, k, p;
    for (c = 0; c < corners.length; c++) {
      var C = corners[c], deg = Math.abs(C.deg), dists = null;
      for (p = 0; p < RC3D.MARKER_TIERS.length; p++) {
        if (deg >= RC3D.MARKER_TIERS[p][0]) { dists = RC3D.MARKER_TIERS[p][1]; break; }
      }
      if (dists) {
        for (k = 0; k < dists.length; k++) {
          var s = C.s0 - dists[k];
          if (s < 5) continue;                       // before the start of this lap
          if (s < prevExit + minGapM) continue;      // would sit inside the previous corner
          out.push({ s: s, m: dists[k], label: String(Math.round(dists[k] / 100)),
                     side: -C.dir, deg: deg });
        }
      }
      prevExit = C.s1;
    }
    return out;
  };

  RC3D.markers = function (path, laps, lapNo) {
    var out = [], t = path.t, i, lo = 0, hi = t.length - 1;
    if (laps && laps.length && lapNo) {
      var L = null;
      for (i = 0; i < laps.length; i++) if (laps[i].lap === lapNo) L = laps[i];
      if (L) { lo = RC3D.indexOfTime(t, L.t_start); hi = RC3D.indexOfTime(t, L.t_end); }
    }
    var best = -1, bestMph = Infinity;
    for (i = lo; i <= hi; i++) if (path.speed[i] < bestMph) { bestMph = path.speed[i]; best = i; }
    if (best < 0) return out;
    var put = function (i2, kind) {
      var p = RC3D.pointAtS(path, path.cum[i2]);
      out.push({ kind: kind, x: p.x, y: p.y, z: p.z, mph: path.speed[i2] });
    };
    put(best, "apex");
    var brake = -1;
    for (i = best; i > lo; i--) if (path.speed[i - 1] <= path.speed[i]) { brake = i; break; }
    if (brake > 0) put(brake, "brake");
    var thr = -1;
    for (i = best + 1; i <= hi; i++) if (path.speed[i] > bestMph + 3) { thr = i; break; }
    if (thr > 0) put(thr, "throttle");
    return out;
  };

  // Signed lateral acceleration (g) at arc length s: +ve = turning RIGHT.
  // (+x east / -z north means a right turn rotates the tangent with a positive
  // cross product, which is what the sign test in the host test pins down.)
  RC3D.latAccel = function (path, s, mph) {
    var a = RC3D.pointAtS(path, Math.max(0, s - 6));
    var b = RC3D.pointAtS(path, s + 6);
    var cross = a.tan[0] * b.tan[1] - a.tan[1] * b.tan[0];
    var dot = a.tan[0] * b.tan[0] + a.tan[1] * b.tan[1];
    var dTheta = Math.atan2(cross, dot);                 // +ve = turning right
    var radius = Math.abs(dTheta) < 1e-6 ? 1e6 : Math.abs(12 / dTheta) + 1;
    var v = mph * 0.44704;
    var g = (v * v / radius) / 9.80665;
    return (dTheta >= 0 ? 1 : -1) * Math.min(1.6, g);
  };

  // Eye + aim + roll for one instant. The look-ahead distance and the FOV both
  // grow with speed (that is most of the "this feels fast" cue); `latG` banks
  // the camera a little. `look` is whatever the user dragged in.
  RC3D.cameraPose = function (path, s, mph, opt) {
    opt = opt || {};
    var eyeH = opt.eye == null ? 1.15 : opt.eye;
    var lead = opt.lead == null ? Math.max(16, mph * 0.42) : opt.lead;
    var here = RC3D.pointAtS(path, s);
    var ahead = RC3D.pointAtS(path, s + lead);
    var roll = opt.bank === false ? 0
      : Math.max(-0.055, Math.min(0.055, (opt.latG || 0) * 0.03));
    return {
      eye: { x: here.x, y: here.y + eyeH, z: here.z },
      target: { x: ahead.x, y: ahead.y + eyeH * 0.72, z: ahead.z },
      roll: roll, tan: here.tan, lead: lead
    };
  };

  globalThis.RC3D = RC3D;
  if (globalThis.RC3D_NO_MAIN) return;      // host-test seam (tests/test_track3d.py)

  /* =======================================================================
     2. RENDER LAYER (three.js)
     ======================================================================= */
  var renderer, scene, camera, canvas, ground, mini, miniCtx;
  var playing = false, rate = 1, loopLap = true, bank = true, freeLook = false;
  var scale = 1.5;                       // supersample factor (set on boot)
  var S = [], PATH = null, BASE = null, LAPS = [], LAPNO = 0;
  var TA = 0, TB = 0, NOW = 0, SMIN = 0, SMAX = 0, cur = null;
  var meshes = { road: null, kerbs: null, markers: null, ghost: null, ideal: null,
                 gantry: null, ground: null, signs: null, car: null };
  var ASSET = null, TEX = null, assetSample = null;   // prepared-track data
  var look = { yaw: 0, pitch: 0 };
  var view = "chase";                                     // "chase" | "plan"
  var planZoom = 1;                                       // wheel zoom in plan view
  var realWidth = null;                                   // metres, from the asset
  var CORNERS = [];                                       // detected corners
  var opts = { smooth: 5, eye: 1.15, road: 12, speedColour: true, markers: true,
               ghost: false, ground: true, brakes: true };

  function tryRenderer() {
    try {
      canvas = el("view");
      // antialias:true is MSAA. NOTE: a logarithmic depth buffer (which this
      // scene does not need — everything sits within ~50 m of the camera) makes
      // several drivers cut MSAA and shimmer badly, so it is deliberately off.
      renderer = new THREE.WebGLRenderer({ canvas: canvas, antialias: true,
                                           powerPreference: "high-performance" });
      return true;
    } catch (e) {
      notice("this browser cannot do WebGL — the 3D drive view needs it", true);
      return false;
    }
  }

  function skyTexture() {
    var c = document.createElement("canvas");
    c.width = 8; c.height = 256;
    var g = null;
    try { g = c.getContext("2d"); } catch (e) { g = null; }
    if (!g) return null;
    var grad = g.createLinearGradient(0, 0, 0, 256);
    grad.addColorStop(0.00, "#04060A");
    grad.addColorStop(0.46, "#0A1220");
    grad.addColorStop(0.80, "#1E2C3A");
    grad.addColorStop(1.00, "#3C4D5B");
    g.fillStyle = grad;
    g.fillRect(0, 0, 8, 256);
    var tex = new THREE.CanvasTexture(c);
    tex.mapping = THREE.EquirectangularReflectionMapping;
    return tex;
  }

  function buildScene() {
    scene = new THREE.Scene();
    var sky = skyTexture();
    scene.background = sky || new THREE.Color(0x0A1220);
    scene.fog = new THREE.Fog(0x1E2C3A, 200, 1200);
    camera = new THREE.PerspectiveCamera(68, 1, 0.25, 3000);
    scene.add(new THREE.HemisphereLight(0xC3D6E8, 0x191C21, 1.1));
    var sun = new THREE.DirectionalLight(0xFFFFFF, 0.7);
    sun.position.set(-1, 2.4, 0.6);
    scene.add(sun);
    ground = new THREE.Mesh(
      new THREE.PlaneGeometry(80000, 80000),
      new THREE.MeshBasicMaterial({ color: 0x121519 })
    );
    ground.rotation.x = -Math.PI / 2;
    ground.position.y = -2;
    scene.add(ground);
  }

  function disposeMeshes() {
    Object.keys(meshes).forEach(function (k) {
      var m = meshes[k];
      if (!m) return;
      scene.remove(m);
      if (m.traverse) m.traverse(function (o) {
        if (o.geometry) o.geometry.dispose();
        if (o.material) o.material.dispose();
      });
      meshes[k] = null;
    });
  }

  function makeRoad(path, width, lift, colourBySpeed, solidColour, alpha, extra) {
    extra = extra || {};
    var r = RC3D.ribbon(path, width, lift, {
      half: extra.half || null,
      uv: extra.uvBounds || null,
      o: extra.o || (PATH && PATH.o)
    });
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(r.position, 3));
    var cols = new Float32Array(r.position.length);
    var lo = 1e9, hi = -1e9, i, mph, c;
    for (i = 0; i < r.s.length; i++) {
      mph = RC3D.mphAtS(path, r.s[i]);
      if (mph < lo) lo = mph;
      if (mph > hi) hi = mph;
    }
    for (i = 0; i < r.s.length; i++) {
      if (colourBySpeed) {
        // accel / brake / neither — the driver's input, not the speed
        c = RC3D.driveColour(RC3D.accelAtS(path, r.s[i]));
        if (extra.tex) {
          // over satellite imagery, blend toward the state colour instead of
          // replacing it, so the real surface stays visible underneath
          var mix = 0.62;
          c = [c[0] * mix + 0.42 * (1 - mix), c[1] * mix + 0.42 * (1 - mix),
               c[2] * mix + 0.42 * (1 - mix)];
        }
      } else {
        c = solidColour || [0.135, 0.145, 0.165];
      }
      cols[i * 3] = c[0]; cols[i * 3 + 1] = c[1]; cols[i * 3 + 2] = c[2];
    }
    geo.setAttribute("color", new THREE.BufferAttribute(cols, 3));
    if (r.uv) geo.setAttribute("uv", new THREE.BufferAttribute(r.uv, 2));
    geo.setIndex(new THREE.BufferAttribute(r.index, 1));
    geo.computeVertexNormals();
    var mo = { vertexColors: true, side: THREE.DoubleSide };
    if (extra.tex) {
      // satellite imagery of the ACTUAL track surface, sampled through the
      // asset's bounds; vertex colours tint it by speed
      mo.map = extra.tex;
    }
    var mat = new THREE.MeshLambertMaterial(mo);
    if (alpha != null && alpha < 1) { mat.transparent = true; mat.opacity = alpha; }
    return new THREE.Mesh(geo, mat);
  }

  // The prepared track's own terrain, textured with its imagery. This is the
  // "how big is the track" view: real asphalt, real kerbs, real grass, real
  // run-off, from the same imagery the width was measured off.
  function rebuildGround() {
    if (meshes.ground) {
      scene.remove(meshes.ground);
      meshes.ground.geometry.dispose();
      meshes.ground.material.dispose();
      meshes.ground = null;
    }
    if (!opts.ground || !ASSET || !ASSET.dem || !TEX) return;
    var m = RC3D.demMesh(ASSET.dem, PATH.o, PATH.yRef,
                         (ASSET.texture && ASSET.texture.bounds) || null);
    if (!m) return;
    var g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(m.position, 3));
    g.setAttribute("uv", new THREE.BufferAttribute(m.uv, 1));
    g.setIndex(new THREE.BufferAttribute(m.index, 1));
    g.computeVertexNormals();
    meshes.ground = new THREE.Mesh(g, new THREE.MeshLambertMaterial({
      map: TEX, side: THREE.DoubleSide
    }));
    scene.add(meshes.ground);
  }

  // Brake boards: the digit is drawn on a canvas (no font file, no glyph
  // server), the board stands on a post at the edge of the road on the OUTSIDE
  // of the corner, facing back at the oncoming car exactly like a real one.
  function digitTexture(text, bg, fg) {
    var c = document.createElement("canvas");
    c.width = 160; c.height = 160;
    var g = null;
    try { g = c.getContext("2d"); } catch (e) { g = null; }
    if (!g) return null;
    g.fillStyle = bg;
    g.fillRect(0, 0, 160, 160);
    g.strokeStyle = fg;
    g.lineWidth = 8;
    g.strokeRect(4, 4, 152, 152);
    g.fillStyle = fg;
    g.font = "bold 108px Inter, Arial, sans-serif";
    g.textAlign = "center";
    g.textBaseline = "middle";
    g.fillText(text, 80, 88);
    var t = new THREE.CanvasTexture(c);
    t.colorSpace = THREE.SRGBColorSpace;
    return t;
  }

  var signTex = null;
  function signTextures() {
    if (signTex) return signTex;
    signTex = {};
    ["1", "2", "3", "4", "5"].forEach(function (n) {
      signTex[n] = digitTexture(n, n === "1" ? "#C0392B" : "#F0A32A",
                                n === "1" ? "#FFFFFF" : "#101010");
    });
    return signTex;
  }

  function makeBrakeSigns(list, path, width) {
    if (!list || !list.length) return null;
    var tex = signTextures();
    var grp = new THREE.Group();
    var postMat = new THREE.MeshLambertMaterial({ color: 0x2A2F3A });
    var boardGeo = new THREE.PlaneGeometry(1.5, 1.5);
    var postGeo = new THREE.BoxGeometry(0.12, 1.5, 0.12);
    for (var i = 0; i < list.length; i++) {
      var mk = list[i];
      var p = RC3D.pointAtS(path, mk.s);
      var tx = p.tan[0], tz = p.tan[1];
      // driver's left is (tz,-tx); mk.side = -1 puts the board on the left
      var ox = (mk.side < 0) ? tz : -tz, oz = (mk.side < 0) ? -tx : tx;
      var off = width / 2 + 1.9;
      var x = p.x + ox * off, z = p.z + oz * off, y = p.y;
      var post = new THREE.Mesh(postGeo, postMat);
      post.position.set(x, y + 0.75, z);
      grp.add(post);
      var mat = new THREE.MeshBasicMaterial({ map: tex[mk.label] || null,
                                              side: THREE.DoubleSide });
      var board = new THREE.Mesh(boardGeo, mat);
      board.position.set(x, y + 2.25, z);
      board.rotation.y = Math.atan2(-tx, -tz);   // face the oncoming car
      grp.add(board);
    }
    return grp;
  }

  // In plan mode you are looking at the whole circuit, so there has to be a
  // "you are here": an arrow on the ribbon, amber, pointing along the tangent.
  function makeCarMarker() {
    var grp = new THREE.Group();
    var cone = new THREE.Mesh(new THREE.ConeGeometry(2.6, 7.0, 4),
                              new THREE.MeshBasicMaterial({ color: 0xFFB020 }));
    cone.rotation.x = Math.PI / 2;          // lie it down, pointing along +z
    grp.add(cone);
    var ring = new THREE.Mesh(new THREE.RingGeometry(3.6, 5.0, 28),
                              new THREE.MeshBasicMaterial({ color: 0xFFB020,
                                side: THREE.DoubleSide, transparent: true,
                                opacity: 0.85 }));
    ring.rotation.x = -Math.PI / 2;
    grp.add(ring);
    return grp;
  }

  function placeCar(p) {
    if (!meshes.car) return;
    var pos = RC3D.pointAtS(PATH, p);
    meshes.car.position.set(pos.x, pos.y + 0.6, pos.z);
    meshes.car.rotation.y = Math.atan2(pos.tan[0], pos.tan[1]);
    // never buried in a hill
    meshes.car.children[0].position.y = 0;
  }

  function makeKerbs(path, width) {
    var k = RC3D.kerbs(path, width, 3);
    if (!k.count) return null;
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(k.position, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(k.colour, 3));
    geo.computeVertexNormals();
    return new THREE.Mesh(geo, new THREE.MeshLambertMaterial({
      vertexColors: true, side: THREE.DoubleSide
    }));
  }

  function makeMarkers(list) {
    if (!list.length) return null;
    var grp = new THREE.Group();
    var colours = { brake: 0xFF4D4D, apex: 0xFFB020, throttle: 0x6CD07A };
    list.forEach(function (mk) {
      var c = colours[mk.kind] || 0xFFFFFF;
      // 20-sided cones + a touch of emissive: faceted, dark-shaded cones were
      // reading as aliased blobs at speed.
      var cone = new THREE.Mesh(new THREE.ConeGeometry(0.6, 1.6, 20),
                                new THREE.MeshLambertMaterial({ color: c,
                                  emissive: c, emissiveIntensity: 0.35 }));
      cone.position.set(mk.x, mk.y + 0.8, mk.z);
      grp.add(cone);
      var pole = new THREE.Mesh(new THREE.CylinderGeometry(0.06, 0.06, 8, 10),
                                new THREE.MeshBasicMaterial({ color: c }));
      pole.position.set(mk.x, mk.y + 4, mk.z);
      grp.add(pole);
    });
    return grp;
  }

  function makeGantry(sf) {
    if (!sf || typeof sf.lat1 !== "number" || typeof sf.lat2 !== "number") return null;
    var a = RC3D.project(sf.lat1, sf.lon1, PATH.o);
    var b = RC3D.project(sf.lat2, sf.lon2, PATH.o);
    var mx = (a.x + b.x) / 2, mz = (a.z + b.z) / 2;
    var dx = b.x - a.x, dz = b.z - a.z;
    var L = Math.sqrt(dx * dx + dz * dz) || 1;
    var half = Math.max(6, L / 2);
    var y0 = PATH.y.length ? PATH.y[PATH.y.length >> 1] : 0;
    var grp = new THREE.Group();
    var white = new THREE.MeshLambertMaterial({ color: 0xE6E8EE });
    var red = new THREE.MeshLambertMaterial({ color: 0xD23B3B });
    [-1, 1].forEach(function (side) {
      var post = new THREE.Mesh(new THREE.BoxGeometry(0.35, 7, 0.35), red);
      post.position.set(mx + (dx / L) * half * side, y0 + 3.5, mz + (dz / L) * half * side);
      grp.add(post);
    });
    var beam = new THREE.Mesh(new THREE.BoxGeometry(half * 2, 0.9, 0.6), white);
    beam.position.set(mx, y0 + 6.6, mz);
    beam.rotation.y = Math.atan2(dz / L, dx / L);
    grp.add(beam);
    return grp;
  }

  // Sub-path for one lap window, sharing the dense centreline of the session
  // path. Used for the road (so overlapping laps cannot z-fight) and ghosts.
  function slicePath(path, t0, t1) {
    var sA = RC3D.sAtTime(path, t0), sB = RC3D.sAtTime(path, t1);
    if (!(sB > sA + 5)) return null;
    var d = path.dense, i, cum = 0, prev = null;
    var out = { o: path.o, x: [], y: [], z: [], cum: [], speed: [], t: [], total: 0,
                dense: { x: [], y: [], z: [], s: [], tan: [], total: 0 } };
    for (i = 0; i < d.s.length; i++) {
      if (d.s[i] < sA || d.s[i] > sB) continue;
      if (prev) {
        var dx = d.x[i] - prev[0], dy = d.y[i] - prev[1], dz = d.z[i] - prev[2];
        cum += Math.sqrt(dx * dx + dy * dy + dz * dz);
      }
      out.dense.x.push(d.x[i]); out.dense.y.push(d.y[i]); out.dense.z.push(d.z[i]);
      out.dense.s.push(cum); out.dense.tan.push(d.tan[i]);
      prev = [d.x[i], d.y[i], d.z[i]];
    }
    if (out.dense.x.length < 3) return null;
    out.dense.total = cum;
    out.total = cum;
    for (i = 0; i < path.cum.length; i++) {
      if (path.cum[i] < sA || path.cum[i] > sB) continue;
      out.cum.push(path.cum[i] - sA);
      out.speed.push(path.speed[i]);
      out.t.push(path.t[i]);
      out.x.push(path.x[i]); out.y.push(path.y[i]); out.z.push(path.z[i]);
    }
    if (out.cum.length < 2) return null;
    return out;
  }

  function lapObj(lapNo) {
    var i, L = null;
    for (i = 0; i < LAPS.length; i++) if (LAPS[i].lap === lapNo) L = LAPS[i];
    return L;
  }

  function rebuild() {
    try {
      disposeMeshes();
      if (!PATH) return;
      var L = lapObj(LAPNO), base = PATH, sub = null;
      if (L) { sub = slicePath(PATH, L.t_start, L.t_end); if (sub) base = sub; }
      BASE = base;
      // Prepared track: real per-station widths and the satellite texture
      // draped through the asset's bounds. Without one, the synthetic ribbon
      // and the width slider (as before).
      var extra = null;
      if (ASSET && assetSample) {
        var nb = base.dense.x.length, hl = new Float64Array(nb), hr = new Float64Array(nb);
        var sum = 0, cnt = 0;
        for (var bi = 0; bi < nb; bi++) {
          var r2 = assetSample(base.dense.x[bi], base.dense.z[bi]);
          var w2 = (r2 && r2.width_m) ? r2.width_m : null;
          hl[bi] = w2 ? w2 / 2 : null;
          hr[bi] = w2 ? w2 / 2 : null;
          if (w2) { sum += w2; cnt++; }
        }
        realWidth = cnt ? sum / cnt : null;
        extra = { half: [hl, hr], o: PATH.o,
                  uvBounds: (ASSET.texture && TEX) ? ASSET.texture.bounds : null,
                  tex: TEX };
        if (el("b-road")) {           // the slider no longer decides the width
          el("b-road").disabled = true;
          var lab = el("b-road").parentNode;
          if (lab) lab.title = "width comes from the prepared track (" +
            (realWidth ? realWidth.toFixed(1) : "?") + " m)";
        }
      }
      meshes.road = makeRoad(base, opts.road, 0.03, opts.speedColour, null, 1, extra);
      scene.add(meshes.road);
      var useW = extra && realWidth ? realWidth : opts.road;
      meshes.kerbs = makeKerbs(base, useW);
      if (opts.brakes) {
        CORNERS = RC3D.corners(base, {});
        meshes.signs = makeBrakeSigns(RC3D.brakeMarkers(base, CORNERS, {}), base, useW);
        if (meshes.signs) scene.add(meshes.signs);
      } else {
        CORNERS = [];
      }
      if (meshes.kerbs) scene.add(meshes.kerbs);
      if (opts.markers) {
        meshes.markers = makeMarkers(RC3D.markers(PATH, LAPS, LAPNO));
        if (meshes.markers) scene.add(meshes.markers);
      }
      if (opts.ghost && LAPS.length > 1) {
        var grp = new THREE.Group();
        LAPS.forEach(function (LL) {
          if (LL.lap === LAPNO) return;
          var g = slicePath(PATH, LL.t_start, LL.t_end);
          if (g) grp.add(makeRoad(g, 1.8, 0.05, false, [0.32, 0.34, 0.38], 0.65));
        });
        meshes.ghost = grp;
        scene.add(grp);
      }
      if (!meshes.gantry && LAPS.sf) {
        meshes.gantry = makeGantry(LAPS.sf);
        if (meshes.gantry) scene.add(meshes.gantry);
      }
      rebuildGround();
      if (!meshes.car) {
        meshes.car = makeCarMarker();
        if (meshes.car) scene.add(meshes.car);
      }
      if (meshes.car) meshes.car.visible = (view === "plan");
    } catch (e) {
      console.warn("[track3d] rebuild:", e && e.message ? e.message : e);
    }
  }

  function resize() {
    var w = canvas.clientWidth || window.innerWidth;
    var h = canvas.clientHeight || (window.innerHeight - 52);
    // Supersampling: this scene is a few thousand triangles, so rendering above
    // the CSS resolution is cheap and it is what actually removes the crawling
    // edges (MSAA alone leaves thin, high-contrast ribbon edges sparkling).
    renderer.setPixelRatio(scale * Math.min(2, window.devicePixelRatio || 1));
    renderer.setSize(w, h, false);
    camera.aspect = w / Math.max(1, h);
    camera.updateProjectionMatrix();
  }

  // Plan view = the whole circuit from above, over the prepared imagery, with a
  // scale bar. This is the "how big is the track" answer: real surface, real
  // width, real corner boards, to scale.
  var PLAN = { up: null };
  function planFrame() {
    var b = null, i;
    if (ASSET && ASSET.dem && ASSET.dem.bounds) b = ASSET.dem.bounds;      // [S,W,N,E]
    if (!b && ASSET && ASSET.bbox) b = ASSET.bbox;
    var minX, maxX, minZ, maxZ, cx, cz, span, y0;
    if (b) {
      var p1 = RC3D.project(b[0], b[1], PATH.o), p2 = RC3D.project(b[2], b[3], PATH.o);
      minX = Math.min(p1.x, p2.x); maxX = Math.max(p1.x, p2.x);
      minZ = Math.min(p1.z, p2.z); maxZ = Math.max(p1.z, p2.z);
      y0 = 0;
    } else {
      minX = maxX = PATH.dense.x[0]; minZ = maxZ = PATH.dense.z[0];
      y0 = PATH.dense.y[0];
      for (i = 0; i < PATH.dense.x.length; i++) {
        minX = Math.min(minX, PATH.dense.x[i]); maxX = Math.max(maxX, PATH.dense.x[i]);
        minZ = Math.min(minZ, PATH.dense.z[i]); maxZ = Math.max(maxZ, PATH.dense.z[i]);
        y0 = Math.min(y0, PATH.dense.y[i]);
      }
    }
    cx = (minX + maxX) / 2; cz = (minZ + maxZ) / 2;
    span = Math.max(maxX - minX, maxZ - minZ, 60);
    var fov = camera.fov * Math.PI / 180;
    var h = (span / 2) / Math.tan(fov / 2) * 1.12 / planZoom;   // fit, with a margin
    PLAN.up = null;
    return { c: { x: cx, y: y0, z: cz }, h: h, span: span };
  }

  function updateScaleBar(spanMeters, h) {
    var bar = el("scalebar"), txt = el("scale-txt");
    if (!bar) return;
    if (view !== "plan") { bar.style.display = "none"; return; }
    bar.style.display = "flex";
    // metres per pixel at the ground plane for a top-down view
    var hpx = canvas.clientHeight || 1;
    var mPerPx = (2 * h * Math.tan(camera.fov * Math.PI / 360)) / hpx;
    var want = 100;                                   // aim for a ~100 m bar
    var px = want / mPerPx;
    while (px > 260) { want /= 2; px = want / mPerPx; }
    while (px < 60) { want *= 2; px = want / mPerPx; }
    bar.firstChild.style.width = Math.round(px) + "px";
    txt.textContent = want >= 1000 ? (want / 1000) + " km" : want + " m";
  }

  function render() {
    if (!PATH) return;
    var L = lapObj(LAPNO);
    var st = RC3D.frameState(PATH, NOW, L, { eye: opts.eye, bank: bank });
    var eye, tgt;
    if (view === "plan") {
      var pf = planFrame();
      eye = new THREE.Vector3(pf.c.x, pf.c.y + pf.h, pf.c.z + 0.02);
      tgt = new THREE.Vector3(pf.c.x, pf.c.y, pf.c.z);
      updateScaleBar(pf.span, pf.h);
    } else {
      eye = new THREE.Vector3(st.eye.x, st.eye.y, st.eye.z);
      tgt = new THREE.Vector3(st.target.x, st.target.y, st.target.z);
    }
    if (look.yaw || look.pitch) {
      // temporary look-around: rotate the aim, the eye stays on the car
      var dir = tgt.clone().sub(eye).normalize();
      dir.applyEuler(new THREE.Euler(0, look.yaw, 0));
      dir.y += look.pitch;
      tgt = eye.clone().add(dir.normalize().multiplyScalar(st.lead));
    }
    camera.position.copy(eye);
    if (view === "plan") {
      camera.up.set(0, 0, -1);            // north (-z) up, like a track map
      camera.lookAt(tgt);
      camera.fov = 58;
    } else {
      camera.up.set(0, 1, 0);
      camera.lookAt(tgt);
      if (st.roll) camera.rotateZ(st.roll);
      camera.fov = Math.max(60, Math.min(84, 66 + st.mph * 0.09));
    }
    camera.updateProjectionMatrix();
    renderer.render(scene, camera);

    el("h-mph").textContent = Math.round(st.mph);
    el("h-rpm").textContent = (cur && typeof cur.rpm === "number") ? cur.rpm : "—";
    el("h-lap").textContent = LAPNO ? LAPNO : "whole session";
    el("h-lapt").textContent = L ? fmtLap(NOW - L.t_start) : fmtLap(NOW);
    el("h-best").textContent = L && L.seconds ? fmtLap(L.seconds) : "—";
    el("h-alt").textContent = (cur && typeof cur.alt_m === "number")
      ? Math.round(cur.alt_m) + " m" : "—";
    if (meshes.car) {
      meshes.car.visible = (view === "plan");
      if (view === "plan") placeCar(st.s);
    }
    var g = RC3D.accelAtS(PATH, st.s);
    if (el("h-g")) {
      el("h-g").textContent = (g >= 0 ? "+" : "") + g.toFixed(2) + " g";
      el("h-g").style.color = (g > 0.035) ? "#5CE07F"
        : (g < -0.035 ? "#FF6B6B" : "var(--muted)");
    }
    el("h-bar").style.width = Math.min(100, (st.mph / 160) * 100) + "%";
    if (el("lg-corner")) {
      if (CORNERS.length) {
        var tight = CORNERS.filter(function (c) { return Math.abs(c.deg) >= 45; }).length;
        el("lg-corner").style.display = "flex";
        el("lg-corner").textContent = CORNERS.length + " corners" +
          (tight ? " · " + tight + " with brake boards" : " · none need boards");
      } else {
        el("lg-corner").style.display = "none";
      }
    }
    el("b-clock").textContent = fmtClock(NOW - TA) + " / " + fmtClock(TB - TA);
    el("b-scrub").value = String(TB > TA ? Math.round(1000 * (NOW - TA) / (TB - TA)) : 0);
    drawMini(st.s);
  }

  /* ---- mini-map: an unambiguous "you are HERE" ---------------------------
     The chase camera already puts you on the line, but a small plan view makes
     the position, the lap direction and what is coming next obvious — and it
     proves at a glance that what is being drawn IS the lap you are driving. */
  var miniFit = null;
  function miniPrepare() {
    var base = BASE || PATH;
    if (!base) return;
    var d = base.dense, i;
    var minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity;
    for (i = 0; i < d.x.length; i++) {
      if (d.x[i] < minX) minX = d.x[i];
      if (d.x[i] > maxX) maxX = d.x[i];
      if (d.z[i] < minZ) minZ = d.z[i];
      if (d.z[i] > maxZ) maxZ = d.z[i];
    }
    if (!isFinite(minX)) return;
    miniFit = { minX: minX, minZ: minZ, w: Math.max(1, maxX - minX),
                h: Math.max(1, maxZ - minZ), base: base };
  }
  function drawMini(s) {
    if (!miniCtx || !miniFit || !PATH) return;
    var W = mini.width, H = mini.height, pad = 8;
    var k = Math.min((W - pad * 2) / miniFit.w, (H - pad * 2) / miniFit.h);
    var ox = (W - miniFit.w * k) / 2, oz = (H - miniFit.h * k) / 2;
    var px = function (x) { return ox + (x - miniFit.minX) * k; };
    var pz = function (z) { return oz + (z - miniFit.minZ) * k; };
    var d = miniFit.base.dense, i;
    miniCtx.clearRect(0, 0, W, H);
    miniCtx.lineWidth = 3;
    miniCtx.strokeStyle = "#3A4150";
    miniCtx.beginPath();
    for (i = 0; i < d.x.length; i += 3) {
      if (i === 0) miniCtx.moveTo(px(d.x[i]), pz(d.z[i]));
      else miniCtx.lineTo(px(d.x[i]), pz(d.z[i]));
    }
    miniCtx.stroke();
    var sf = LAPS.sf;
    if (sf && typeof sf.lat1 === "number" && typeof sf.lat2 === "number") {
      var a = RC3D.project(sf.lat1, sf.lon1, PATH.o);
      var b = RC3D.project(sf.lat2, sf.lon2, PATH.o);
      miniCtx.strokeStyle = "#E6E8EE";
      miniCtx.lineWidth = 2;
      miniCtx.beginPath();
      miniCtx.moveTo(px(a.x), pz(a.z));
      miniCtx.lineTo(px(b.x), pz(b.z));
      miniCtx.stroke();
    }
    var p = RC3D.pointAtS(PATH, s);
    var hdg = p.tan;
    miniCtx.fillStyle = "#FFB020";
    miniCtx.beginPath();
    miniCtx.arc(px(p.x), pz(p.z), 5, 0, Math.PI * 2);
    miniCtx.fill();
    // a short heading whisker so the direction of travel is obvious
    miniCtx.strokeStyle = "#FFB020";
    miniCtx.lineWidth = 3;
    miniCtx.beginPath();
    miniCtx.moveTo(px(p.x), pz(p.z));
    miniCtx.lineTo(px(p.x + hdg[0] * 14), pz(p.z + hdg[1] * 14));
    miniCtx.stroke();
  }

  function advance(dt) {
    NOW += dt * rate;
    if (NOW >= TB) {
      if (loopLap) NOW = TA + ((NOW - TA) % Math.max(0.05, TB - TA));
      else { NOW = TB; playing = false; syncPlay(); }
    }
    if (NOW < TA) NOW = TA;
    cur = S[RC3D.indexOfTime(PATH.t, NOW)] || null;
  }

  function syncPlay() {
    el("b-play").textContent = playing ? "❚❚ pause" : "▶ drive";
    el("b-play").className = playing ? "on" : "";
  }

  var lastTs = 0;
  function frame(ts) {
    requestAnimationFrame(frame);            // scheduled FIRST: nothing below can stop it
    if (!lastTs) lastTs = ts;
    var dt = Math.min(0.12, (ts - lastTs) / 1000);   // a stalled tab must not teleport
    lastTs = ts;
    if (!PATH) return;
    if (playing) advance(dt);
    RC3D.recentreLook(look, dt, !freeLook);
    try { render(); }
    catch (e) {
      if (!frame.warned) { frame.warned = true; console.warn("[track3d] render:", e && e.message ? e.message : e); }
    }
  }

  function setLap(lapNo) {
    LAPNO = lapNo || 0;
    var L = lapObj(LAPNO);
    TA = L ? L.t_start : (PATH.t[0] || 0);
    TB = L ? L.t_end : (PATH.t[PATH.t.length - 1] || 0);
    if (!(TB > TA)) TB = TA + 1;
    NOW = TA;
    look.yaw = 0; look.pitch = 0;      // a lap change always re-centres the view
    rebuild();
    miniPrepare();
    render();
  }

  function fillLapSelect() {
    var sel = el("b-lap"), i;
    sel.innerHTML = "";
    if (!LAPS.length) {
      var o0 = document.createElement("option");
      o0.value = "0"; o0.textContent = "whole session";
      sel.appendChild(o0);
      sel.disabled = true;
      return;
    }
    var best = null;
    for (i = 0; i < LAPS.length; i++)
      if (LAPS[i].seconds && (best === null || LAPS[i].seconds < best)) best = LAPS[i].seconds;
    for (i = 0; i < LAPS.length; i++) {
      var o = document.createElement("option");
      o.value = String(LAPS[i].lap);
      o.textContent = "lap " + LAPS[i].lap + "  " + fmtLap(LAPS[i].seconds) +
        (LAPS[i].seconds === best ? "  ★" : "");
      sel.appendChild(o);
    }
    sel.value = String(LAPS[0].lap);
    for (i = 0; i < LAPS.length; i++) if (LAPS[i].seconds === best) sel.value = String(LAPS[i].lap);
  }

  function wire() {
    el("b-play").addEventListener("click", function () {
      playing = !playing;
      if (playing && NOW >= TB) NOW = TA;
      syncPlay();
    });
    el("b-scrub").addEventListener("input", function () {
      playing = false; syncPlay();
      NOW = TA + (TB - TA) * (Number(el("b-scrub").value) / 1000);
      cur = S[RC3D.indexOfTime(PATH.t, NOW)] || null;
    });
    el("b-rate").addEventListener("change", function () { rate = Number(el("b-rate").value) || 1; });
    el("b-lap").addEventListener("change", function () {
      setLap(Number(el("b-lap").value));
      playing = true; syncPlay();
    });
    el("b-smooth").addEventListener("change", function () {
      opts.smooth = Number(el("b-smooth").value);
      PATH = RC3D.buildPath(S, { smooth: opts.smooth, denseStep: 1 });
      TA = PATH.t[0]; TB = PATH.t[PATH.t.length - 1];
      setLap(LAPNO);
    });
    el("b-eye").addEventListener("input", function () { opts.eye = Number(el("b-eye").value); });
    el("b-road").addEventListener("input", function () {
      opts.road = Number(el("b-road").value); rebuild();
    });
    el("b-speedcol").addEventListener("change", function () {
      opts.speedColour = el("b-speedcol").checked; rebuild();
    });
    el("b-markers").addEventListener("change", function () {
      opts.markers = el("b-markers").checked; rebuild();
    });
    el("b-ghost").addEventListener("change", function () {
      opts.ghost = el("b-ghost").checked; rebuild();
    });
    el("b-loop").addEventListener("change", function () { loopLap = el("b-loop").checked; });
    el("b-bank").addEventListener("change", function () { bank = el("b-bank").checked; });
    if (el("b-view")) el("b-view").addEventListener("change", function () {
      view = el("b-view").value === "plan" ? "plan" : "chase";
      if (view === "chase") { look.yaw = 0; look.pitch = 0; }
      render();
    });
    if (el("b-brakes")) el("b-brakes").addEventListener("change", function () {
      opts.brakes = el("b-brakes").checked; rebuild();
    });
    if (el("b-ground")) el("b-ground").addEventListener("change", function () {
      opts.ground = el("b-ground").checked; rebuildGround();
    });
    if (el("b-prep")) el("b-prep").addEventListener("click", prepareTrack);
    if (el("b-scale")) {
      el("b-scale").value = String(scale);
      el("b-scale").addEventListener("change", function () {
        scale = Number(el("b-scale").value) || 1;
        resize();
      });
    }

    var dragging = false, lx = 0, ly = 0;
    canvas.addEventListener("mousedown", function (e) {
      dragging = true; freeLook = true; lx = e.clientX; ly = e.clientY;
    });
    window.addEventListener("mouseup", function () {
      // released -> the view eases back to following the car by itself
      dragging = false; freeLook = false;
    });
    window.addEventListener("mousemove", function (e) {
      if (!dragging) return;
      look.yaw -= (e.clientX - lx) * 0.004;
      look.pitch = Math.max(-0.5, Math.min(0.5, look.pitch + (e.clientY - ly) * 0.02));
      lx = e.clientX; ly = e.clientY;
    });
    canvas.addEventListener("wheel", function (e) {
      e.preventDefault();
      if (view === "plan") {
        planZoom = Math.max(0.35, Math.min(6, planZoom * (e.deltaY > 0 ? 0.88 : 1.14)));
        return;
      }
      opts.eye = Math.max(0.5, Math.min(6, opts.eye + (e.deltaY > 0 ? 0.15 : -0.15)));
      el("b-eye").value = String(opts.eye);
    }, { passive: false });
    document.addEventListener("keydown", function (e) {
      if (e.key === " ") { e.preventDefault(); el("b-play").click(); }
      else if (e.key === "ArrowRight") { playing = false; syncPlay(); NOW = Math.min(TB, NOW + 1); advance(0); }
      else if (e.key === "ArrowLeft") { playing = false; syncPlay(); NOW = Math.max(TA, NOW - 1); advance(0); }
      else if (e.key === "ArrowUp") { opts.eye = Math.min(6, opts.eye + 0.2); el("b-eye").value = String(opts.eye); }
      else if (e.key === "ArrowDown") { opts.eye = Math.max(0.5, opts.eye - 0.2); el("b-eye").value = String(opts.eye); }
    });
    window.addEventListener("resize", resize);
  }

  // The AI/lineview ideal line, when a lasso polygon came in: the fastest REAL
  // traverses of that section, drawn as a slim ribbon on the ground.
  function addIdeal() {
    if (pts.length < 3) return;
    fetch("/sessions/" + encodeURIComponent(USER) + "/" + encodeURIComponent(FILE) + "/lines", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ region: { points: pts } })
    }).then(function (r) { return r.json(); }).then(function (j) {
      if (!j || !j.ok || !j.ideal || !j.ideal.trace || j.ideal.trace.length < 3) return;
      var samples = j.ideal.trace.map(function (p) {
        return { lat: p[0], lon: p[1], speed_mph: p[2] || 0 };
      });
      var sub = RC3D.buildPath(samples, { smooth: 7, denseStep: 1.5 });
      var mesh = makeRoad(sub, 0.6, 0.14, false, [0.42, 0.82, 0.48], 1);
      meshes.ideal = mesh;
      scene.add(mesh);
      el("lg-ideal").style.display = "flex";
      notice("ideal line loaded — fastest real lap through the circled section");
      setTimeout(hideNotice, 3500);
    }).catch(function () {});
  }

  // ---- prepared track (real width / real terrain / real imagery) ----------
  var assetUrl = "/sessions/" + encodeURIComponent(USER) + "/" +
                 encodeURIComponent(FILE) + "/track-asset";
  var prepUrl = "/sessions/" + encodeURIComponent(USER) + "/" +
                encodeURIComponent(FILE) + "/track-prep";
  var prepPoll = null;

  function assetProblem(asset) {
    // Mirrors the server-side validator: never draw wallpaper at the driver.
    var t = asset.texture;
    if (!t) return null;                       // geometry-only is fine
    var b = t.bounds;
    var mpp = 111320.0;
    var midLat = (b.north + b.south) / 2;
    var w = (b.east - b.west) * mpp * Math.cos(midLat * Math.PI / 180);
    var hgt = (b.north - b.south) * mpp;
    if (!(w > 300 && hgt > 300)) {
      return "its imagery covers only " + Math.round(w) + "x" + Math.round(hgt) +
             " m, so the ground would be stretched into wallpaper";
    }
    var px = t.px || [0, 0];
    if (px[0] < 512 || px[1] < 512) {
      return "its imagery is only " + px[0] + "x" + px[1] + " px";
    }
    if (Math.max(w / px[0], hgt / px[1]) > 6) {
      return "its imagery is too coarse (" +
             (Math.max(w / px[0], hgt / px[1])).toFixed(1) + " m/px) to show a track";
    }
    return null;
  }

  function applyAsset(asset) {
    var problem = asset ? assetProblem(asset) : null;
    if (problem) {
      notice("the prepared data for this track is unusable: " + problem +
             " \u2014 re-baking it from imagery\u2026");
      ASSET = null;
      assetSample = null;
      var btn = el("b-prep");
      if (btn) {
        btn.style.display = "inline-block";
        btn.textContent = "re-prepare track";
      }
      prepareTrack(true, true);
      return;
    }
    ASSET = asset;
    if (!asset || !PATH) return;
    try {
      assetSample = RC3D.assetSampler(asset, PATH.o);
      if (asset.dem) RC3D.applyAssetElevation(PATH, asset.dem, PATH.o);
    } catch (e) {
      console.warn("[track3d] asset apply:", e && e.message ? e.message : e);
    }
    // A prepared track is worth SHOWING first: open on the whole circuit over
    // the real imagery (that is the "how big is the track" view), one click from
    // the driving view. Without imagery there is nothing to see from above, so
    // stay in the car.
    if (asset.texture && view === "chase" && !applyAsset.choseView) {
      applyAsset.choseView = true;
      view = "plan";
      if (el("b-view")) el("b-view").value = "plan";
      notice("plan view: whole circuit over the real imagery (" +
             (asset.length_m ? (asset.length_m / 1000).toFixed(2) + " km" : "?") +
             ") — switch view to \u201cchase\u201d to drive it");
      setTimeout(hideNotice, 6000);
    }
    var attr = (asset.texture && asset.texture.attrib) || "";
    var src = (asset.source && asset.source.line) || "?";
    el("lg-track").style.display = "flex";
    if (!applyAsset.announced && !window.__rc3AssetNote) {
      window.__rc3AssetNote = true;
      notice("prepared track: " + asset.track +
             (asset.length_m ? " \u00b7 " + (asset.length_m / 1000).toFixed(2) + " km" : "") +
             " \u00b7 real width + terrain from imagery");
      setTimeout(hideNotice, 4000);
    }
    el("lg-track").textContent = asset.track +
      (asset.length_m ? " (" + (asset.length_m / 1000).toFixed(2) + " km)" : "") +
      " — " + (asset.width_osm_m || asset.width_imagery_m || "?") + " m wide (" +
      (asset.width_source || "?") + "), line from " + src;
    var note = el("notice");
    if (note && asset.width_agreement === false) {
      notice("track " + asset.track + ": imagery says " + asset.width_imagery_m +
             " m but OSM says " + asset.width_osm_m + " m — using the tag");
      setTimeout(hideNotice, 5000);
    }
    if (asset.texture) {
      el("b-ground-lab").style.display = "flex";
      var url = "/trackassets/" + encodeURIComponent(asset.slug) + "/texture.jpg";
      new THREE.TextureLoader().load(url, function (t) {
        t.colorSpace = THREE.SRGBColorSpace;
        // Clamp, never repeat: if anything about the asset's bounds is off, a
        // clamped texture smears at worst, while a repeating one turns the
        // ground into wallpaper. Anisotropy keeps the ground crisp where it
        // meets the horizon instead of turning to mush.
        t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
        t.generateMipmaps = true;
        t.minFilter = THREE.LinearMipmapLinearFilter;
        t.magFilter = THREE.LinearFilter;
        try {
          t.anisotropy = renderer.capabilities.getMaxAnisotropy();
        } catch (e) {}
        t.needsUpdate = true;
        TEX = t;
        rebuild();
      }, undefined, function () {
        console.warn("[track3d] texture failed to load");
      });
      if (attr) {
        el("lg-track").textContent += " · " + attr;
      }
    }
    rebuild();
  }

  function loadAsset() {
    return fetch(assetUrl).then(function (r) {
      if (r.ok) return r.json().then(applyAsset);
      if (r.status !== 404) return null;
      return r.json().then(function (j) {
        var d = j && j.detail;
        if (typeof d === "string") { try { d = JSON.parse(d); } catch (e) { d = null; } }
        if (!d || !d.missing) return;
        var b = el("b-prep");
        b.style.display = "inline-block";
        b.textContent = "prepare track (" + d.track + ")";
        b.title = "pre-render " + d.track + " from satellite imagery + OSM";
        // No prepared track for this circuit yet: do NOT just sit on a
        // synthetic ribbon and hope somebody clicks. Bake it now (once per
        // track, cached) and tell the driver what is happening.
        notice("no prepared track for \u201c" + d.track +
               "\u201d — preparing it from satellite imagery (one-off, ~20 s)\u2026");
        prepareTrack(true);
      }).catch(function () {});
    }).catch(function () {});
  }

  function prepareTrack(auto, force) {
    var b = el("b-prep");
    b.disabled = true;
    b.textContent = "preparing…";
    fetch(prepUrl + (force ? "?force=1" : ""), { method: "POST" }).then(function (r) {
      return r.json().then(function (j) {
        if (!r.ok) throw new Error((j && j.detail) || ("HTTP " + r.status));
        pollPrep();
      });
    }).catch(function (e) {
      b.disabled = false;
      b.textContent = "prepare track";
      notice("prepare failed: " + e.message, true);
    });
  }

  function pollPrep() {
    var b = el("b-prep");
    fetch(prepUrl + "/status").then(function (r) { return r.json(); }).then(function (j) {
      var st = (j && j.prep) || {};
      if (st.state === "done") {
        b.textContent = "prepared ✓";
        notice("track prepared — reloading the imagery");
        setTimeout(function () { location.reload(); }, 900);
        return;
      }
      if (st.state === "failed") {
        b.disabled = false;
        b.textContent = "retry prepare";
        notice("prepare failed: " + (st.error || "unknown") +
               (st.error && /GPS|osm|raceway/i.test(st.error)
                 ? " — this circuit may not exist in OpenStreetMap" : ""), true);
        return;
      }
      b.textContent = "preparing… (" + (st.state || "?") + ")";
      prepPoll = setTimeout(pollPrep, 3000);
    }).catch(function () { prepPoll = setTimeout(pollPrep, 5000); });
  }

  function start() {
    if (!tryRenderer()) return;
    buildScene();
    // supersample by default: 1.5x on a standard display, 2x on a hi-dpi one.
    // The scene is a few thousand triangles, so this is cheap and it is the
    // difference between "jagged everywhere" and a clean edge.
    scale = Math.min(2, Math.max(1.5, window.devicePixelRatio || 1));
    mini = el("mini");
    if (mini) { try { miniCtx = mini.getContext("2d"); } catch (e) { miniCtx = null; } }
    resize();
    wire();
    fetch("/sessions/" + encodeURIComponent(USER) + "/" + encodeURIComponent(FILE) +
          "/data?target=30000")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        S = (d.samples || []).filter(function (s) {
          return typeof s.lat === "number" && typeof s.lon === "number" &&
                 (s.lat || s.lon) && Math.abs(s.lat) <= 90 && Math.abs(s.lon) <= 180;
        });
        if (S.length < 20) { notice("no GPS fixes in this session", true); return; }
        return fetch("/sessions/" + encodeURIComponent(USER) + "/" +
                     encodeURIComponent(FILE) + "/laps")
          .then(function (r) { return r.json(); })
          .catch(function () { return {}; })
          .then(function (lj) {
            LAPS = (lj && lj.laps) || [];
            PATH = RC3D.buildPath(S, { smooth: opts.smooth, denseStep: 1 });
            fillLapSelect();
            hideNotice();
            el("hud").style.display = "block";
            if (el("mini")) el("mini").style.display = "block";
            setLap(Number(el("b-lap").value) || 0);
            loadAsset().then(function () { miniPrepare(); render(); });
            addIdeal();
            playing = true;
            syncPlay();
            requestAnimationFrame(frame);
          });
      })
      .catch(function (e) { notice("could not load session: " + e.message, true); });
  }

  start();
})();
</script>
<script>
// Classic script, so it runs even when the module's CDN import fails outright
// (no ES module = nothing inside it executes at all). Without this, a blocked
// unpkg.com leaves the viewer staring at "loading session…" forever.
setTimeout(function () {
  if (window.__track3dReady) return;
  var n = document.getElementById('notice');
  if (!n) return;
  n.className = 'err';
  n.style.display = 'block';
  n.innerHTML = '3D engine failed to load (unpkg.com blocked?) — ' +
    '<a href="/map3d/__USER__/__FILE__">satellite map view</a> · ' +
    '<a href="/review/__USER__/__FILE__">pit wall</a>';
}, 7000);
</script>
</body></html>
"""
)


_CAN_REVIEW_HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>CAN \u00b7 __FILE__</title>
""" + _FONTS_LINK + "<style>" + _BASE_CSS + _ADMIN_EXTRA_CSS + """
  main { max-width: 1280px; }
  .can-grid { display:grid; grid-template-columns: 320px 1fr; gap:var(--sp-md); align-items:start; }
  @media (max-width:980px){ .can-grid { grid-template-columns:1fr; } }
  .card { background:var(--surface); border:1px solid var(--line);
    border-radius:var(--r-md); overflow:hidden; }
  .card-head { display:flex; justify-content:space-between; align-items:center;
    padding:10px var(--sp-md); border-bottom:1px solid var(--line); }
  .card-body { padding:var(--sp-md); }
  table.idt { border:none; border-radius:0; }
  table.idt td, table.idt th { padding:9px 14px; }
  table.idt tbody tr { cursor:pointer; }
  table.idt tbody tr.sel { background:rgba(255,176,32,0.14); }
  .bytechips { display:flex; flex-wrap:wrap; gap:6px; margin:8px 0 12px; }
  .bchip { display:inline-flex; align-items:center; gap:6px; cursor:pointer;
    padding:4px 9px; border-radius:var(--r-full); border:1px solid var(--line);
    background:var(--surface-2); color:var(--muted); font:600 11px var(--ff-mono);
    opacity:0.45; }
  .bchip.on { opacity:1; color:var(--text); }
  .bchip i { width:9px; height:9px; border-radius:2px; display:inline-block; }
  .bchip small { color:var(--muted); font-weight:400; }
  canvas.plot { width:100%; height:auto; background:var(--bg);
    border:1px solid var(--line); border-radius:var(--r-sm); display:block; }
  .wctl { display:flex; align-items:center; gap:12px; flex-wrap:wrap; margin:8px 0; }
  .wctl label { color:var(--muted); font:600 11px/1 var(--ff-ui);
    letter-spacing:0.06em; text-transform:uppercase; display:flex; align-items:center; gap:6px; }
  .wctl select { background:var(--surface-2); color:var(--text);
    border:1px solid var(--line); border-radius:var(--r-sm); padding:6px 8px; font:13px var(--ff-ui); }
  .loading { padding:32px; color:var(--muted); text-align:center; }
  .err { padding:16px; color:var(--bad); }
</style></head><body>
<header class="app"><span class="dot"></span><h1>racecar-35 \u00b7 pit wall</h1>
  <span class="crumbs"><a href="/">sessions</a> &rsaquo; <a href="/admin">admin</a> &rsaquo;
    <a href="/admin/canbus">CAN</a> &rsaquo; <span class="mono">__FILE__</span></span>
  <span style="flex:1"></span>
  <a class="btn" href="/admin/canbus/__FILE__/raw" style="margin-right:var(--sp-md)">download</a>__USER_CHIP__</header>
<main>
  <div id="loading" class="loading">parsing capture\u2026</div>
  <div id="app" style="display:none">
    <div class="can-grid">
      <div class="card">
        <div class="card-head"><span class="t-label">CAN IDs</span>
          <span class="t-label" id="sum">\u2014</span></div>
        <table class="idt"><thead><tr><th>ID</th><th>frames</th><th>Hz</th><th>dlc</th></tr></thead>
          <tbody id="idrows"></tbody></table>
      </div>
      <div class="card">
        <div class="card-head"><span class="t-label">Signal inspector</span>
          <span class="t-label" id="sel">\u2014</span></div>
        <div class="card-body">
          <div class="t-label" style="margin-bottom:4px">Bytes d0\u2013d7 over time \u2014 click a chip to toggle (changing bytes on by default)</div>
          <div class="bytechips" id="chips"></div>
          <canvas id="bytes" class="plot" width="900" height="280"></canvas>
          <div style="height:16px"></div>
          <div class="t-label">16-bit word inspector \u2014 find RPM / CLT / AFR</div>
          <div class="wctl">
            <label>start byte <select id="wstart"></select></label>
            <label>order <select id="worder">
              <option value="be">big-endian</option>
              <option value="le">little-endian</option></select></label>
            <span id="wstat" class="mono" style="color:var(--muted)"></span>
          </div>
          <canvas id="word" class="plot" width="900" height="220"></canvas>
        </div>
      </div>
    </div>
    <p class="summary" style="color:var(--muted);margin-top:var(--sp-md);font-size:12px">
      Tip: capture while sweeping RPM. The byte (or 16-bit word) whose line ramps with
      engine speed is your RPM field \u2014 note the <b>ID</b>, <b>start byte</b>, and
      <b>endianness</b>, then lock them into <span class="mono">pumpCAN()</span> in
      <span class="mono">src/main.cpp</span>. The standard MS3 guess is 0x5F0 bytes 6\u20137 BE.</p>
  </div>
  <div id="err" class="err" style="display:none"></div>
</main>
<script>
(async function(){
  const FILE='__FILE__'; const el=id=>document.getElementById(id);
  let data;
  try{
    const r=await fetch('/admin/canbus/'+encodeURIComponent(FILE)+'/data');
    if(!r.ok) throw new Error('HTTP '+r.status);
    data=await r.json();
  }catch(e){ el('loading').style.display='none'; el('err').style.display='block';
    el('err').textContent='failed to load: '+e.message; return; }
  el('loading').style.display='none'; el('app').style.display='block';
  el('sum').textContent=data.frames+' frames \u00b7 '+data.n_ids+' IDs';
  if(!data.ids.length){ el('sel').textContent='no frames parsed'; return; }
  const COLORS=['#FFB020','#6CD07A','#5AC8FA','#FF5D5D','#C792EA','#FFD166','#1ABC9C','#EF476F'];
  const idrows=el('idrows');
  let sel=null, enabled=[true,true,true,true,true,true,true,true];
  const fmtHz=h=>h?h.toFixed(0):'\u2014';
  function axes(ctx,W,H,pad){ ctx.fillStyle='#0E1014'; ctx.fillRect(0,0,W,H);
    ctx.strokeStyle='rgba(255,255,255,0.12)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(pad,H-18); ctx.lineTo(W-6,H-18); ctx.stroke(); }
  function drawBytes(){
    const cv=el('bytes'),ctx=cv.getContext('2d'),W=cv.width,H=cv.height,pad=30;
    axes(ctx,W,H,pad); if(!sel) return;
    const t=sel.t,n=t.length; if(!n) return;
    const t0=t[0],t1=t[n-1]||t0+1;
    const X=k=>pad+(W-pad-6)*(t[k]-t0)/Math.max(1,(t1-t0));
    const Y=v=>10+(H-28)*(1-v/255);
    ctx.fillStyle='#8A92A3'; ctx.font='10px monospace'; ctx.textAlign='left';
    [0,128,255].forEach(v=>{ const y=Y(v); ctx.strokeStyle='rgba(255,255,255,0.06)';
      ctx.beginPath(); ctx.moveTo(pad,y); ctx.lineTo(W-6,y); ctx.stroke();
      ctx.fillStyle='#8A92A3'; ctx.fillText(String(v),2,y+3); });
    for(let i=0;i<8;i++){ if(!enabled[i])continue; const b=sel.b[i];
      ctx.strokeStyle=COLORS[i]; ctx.lineWidth=1.5; ctx.beginPath(); let started=false;
      for(let k=0;k<n;k++){ const v=b[k]; if(v==null){started=false;continue;}
        const px=X(k),py=Y(v); if(started)ctx.lineTo(px,py); else {ctx.moveTo(px,py);started=true;} }
      ctx.stroke(); }
  }
  function drawWord(){
    const cv=el('word'),ctx=cv.getContext('2d'),W=cv.width,H=cv.height,pad=46;
    axes(ctx,W,H,pad); if(!sel) return;
    const s=Number(el('wstart').value), be=el('worder').value==='be';
    const t=sel.t,n=t.length, hi=sel.b[s], lo=sel.b[s+1];
    const vals=new Array(n); let mn=Infinity,mx=-Infinity;
    for(let k=0;k<n;k++){ const a=hi[k],c=lo[k];
      if(a==null||c==null){vals[k]=null;continue;}
      const v= be ? (a*256+c) : (c*256+a); vals[k]=v;
      if(v<mn)mn=v; if(v>mx)mx=v; }
    if(mn===Infinity){ el('wstat').textContent='d'+s+'\u00b7d'+(s+1)+': no data'; return; }
    el('wstat').textContent='d'+s+'\u00b7d'+(s+1)+' '+(be?'BE':'LE')+'  \u00b7  range '+mn+'\u2013'+mx;
    const t0=t[0],t1=t[n-1]||t0+1, span=Math.max(1,mx-mn);
    const X=k=>pad+(W-pad-6)*(t[k]-t0)/Math.max(1,(t1-t0));
    const Y=v=>10+(H-28)*(1-(v-mn)/span);
    ctx.fillStyle='#8A92A3'; ctx.font='10px monospace'; ctx.textAlign='left';
    ctx.fillText(String(mx),2,14); ctx.fillText(String(mn),2,H-22);
    ctx.strokeStyle='#FFB020'; ctx.lineWidth=1.8; ctx.beginPath(); let started=false;
    for(let k=0;k<n;k++){ const v=vals[k]; if(v==null){started=false;continue;}
      const px=X(k),py=Y(v); if(started)ctx.lineTo(px,py); else {ctx.moveTo(px,py);started=true;} }
    ctx.stroke();
  }
  function selectId(rec){
    sel=rec;
    [...idrows.children].forEach(tr=>tr.classList.toggle('sel', tr.dataset.id===rec.id_hex));
    el('sel').textContent=rec.id_hex+' \u00b7 dlc '+rec.dlc+' \u00b7 '+rec.count+' frames \u00b7 '+fmtHz(rec.hz)+' Hz';
    enabled=rec.bytes.map(b=>b.range>0);
    if(!enabled.some(x=>x)) enabled=enabled.map(()=>true);
    const chips=el('chips'); chips.innerHTML='';
    rec.bytes.forEach((b,i)=>{
      const c=document.createElement('span'); c.className='bchip'+(enabled[i]?' on':'');
      c.innerHTML='<i style="background:'+COLORS[i]+'"></i>d'+i+' <small>'+
        (b.min==null?'\u2014':b.min+'\u2013'+b.max)+'</small>';
      c.addEventListener('click',()=>{ enabled[i]=!enabled[i]; c.classList.toggle('on',enabled[i]); drawBytes(); });
      chips.appendChild(c);
    });
    const ws=el('wstart'); ws.innerHTML='';
    for(let s=0;s<7;s++){ const o=document.createElement('option'); o.value=String(s);
      o.textContent='d'+s+'\u00b7d'+(s+1); ws.appendChild(o); }
    let bestS=0,bestR=-1;
    for(let s=0;s<7;s++){ const rr=(rec.bytes[s].range||0)+(rec.bytes[s+1].range||0);
      if(rr>bestR){bestR=rr;bestS=s;} }
    ws.value=String(bestS);
    drawBytes(); drawWord();
  }
  el('wstart').addEventListener('change', drawWord);
  el('worder').addEventListener('change', drawWord);
  for(const rec of data.ids){
    const tr=document.createElement('tr'); tr.dataset.id=rec.id_hex;
    tr.innerHTML='<td class=mono>'+rec.id_hex+'</td><td class=mono>'+rec.count+
      '</td><td class=mono>'+fmtHz(rec.hz)+'</td><td class=mono>'+rec.dlc+'</td>';
    tr.addEventListener('click',()=>selectId(rec));
    idrows.appendChild(tr);
  }
  selectId(data.ids[0]);
})();
</script>
</body></html>"""
)

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
  /* Basemap switch: checkbox strip directly UNDER the map. Off = no tiles at
     all, just a black surface — what you want when the imagery fights the
     racing lines. Remembered per browser (localStorage, shared with the
     lineview popout). */
  #map.nosat { background: #000; }
  #map.nosat .leaflet-control-attribution { display: none; }
  .mapopts { display: flex; align-items: center; gap: 12px; flex-wrap: wrap;
    padding: 8px var(--sp-md); border-top: 1px solid var(--line);
    background: var(--surface); }
  .mapchk { display: flex; align-items: center; gap: 8px; cursor: pointer;
    font: 600 12px var(--ff-ui); color: var(--text); white-space: nowrap;
    user-select: none; }
  .mapchk input { width: 16px; height: 16px; margin: 0; cursor: pointer;
    accent-color: var(--primary); }
  .mapopts .hint { color: var(--muted); font-weight: 400; font-size: 11px; }
  .mapopts .sep { width: 1px; height: 20px; background: var(--line); flex: 0 0 auto; }
  a.mapbtn, button.mapbtn { display:inline-flex; align-items:center; gap:6px;
    background: var(--primary, #3B82F6); color:#fff; text-decoration:none;
    border:1px solid transparent; border-radius: var(--r-md); padding:7px 12px;
    font:600 12px var(--ff-ui); white-space:nowrap; cursor:pointer; }
  a.mapbtn:hover, button.mapbtn:hover { filter: brightness(1.08); }
  button.mapbtn.ghost { background: transparent; color: var(--text); border-color: var(--line); }
  button.mapbtn.ghost:hover { border-color: var(--muted); filter:none; }
  button.mapbtn.ghost.active { background: var(--primary, #3B82F6); color:#fff;
    border-color: transparent; }
  button.mapbtn:disabled { opacity:.5; cursor:default; }
  /* On-map key for the two cars: amber = the lap being viewed, red = the
     comparison lap (its ghost LINE + its dot). Sits inside #map, which Leaflet
     makes position:relative, so it overlays the tiles. */
  .dotlegend { position: absolute; left: 10px; bottom: 10px; z-index: 1000;
    pointer-events: none; background: rgba(14,16,20,0.80);
    border: 1px solid var(--line); border-radius: var(--r-md);
    padding: 7px 10px; font: 600 11px/1.7 var(--ff-mono); color: var(--text); }
  .dotlegend .dl-row { display: flex; align-items: center; gap: 7px; white-space: nowrap; }
  .dotlegend .dl-dot { width: 10px; height: 10px; border-radius: var(--r-full);
    border: 2px solid #1A1300; flex: 0 0 auto; }
  .dotlegend .dl-line { width: 26px; border-top: 2px dashed #FF7A7A;
    opacity: 0.85; flex: 0 0 auto; }
  .dotlegend .dl-note { color: var(--muted); font-weight: 400; }
  .tiles { display: grid; grid-template-columns: 1fr 1fr; gap: var(--sp-md); }
  .tile { background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--r-md); padding: var(--sp-md); }
  .tile.full { grid-column: 1 / -1; }
  .tile .label { color: var(--muted); margin-bottom: 6px;
    font: 600 11px/1 var(--ff-ui); letter-spacing: 0.08em; text-transform: uppercase; }
  .tile .val.accent { color: var(--primary); }
  .tile .unit { color: var(--muted); font: 500 13px var(--ff-mono); margin-left: 4px; }
  /* "all channels logged" table (v0.1.171) — every numeric telemetry key the
     session actually carries, with n/min/avg/max. A missing row means the
     source was not live, which is itself the diagnostic. */
  .chtab { width: 100%; border-collapse: collapse; font: 500 12px var(--ff-mono); }
  .chtab th { text-align: right; color: var(--muted); font: 600 10px var(--ff-ui);
    letter-spacing: 0.08em; text-transform: uppercase; padding: 4px 6px;
    border-bottom: 1px solid var(--line); white-space: nowrap; }
  .chtab th:first-child, .chtab td:first-child { text-align: left; }
  .chtab td { text-align: right; padding: 3px 6px;
    border-bottom: 1px solid rgba(255,255,255,0.06);
    font-variant-numeric: tabular-nums; }
  .chtab tr:hover td { background: rgba(255,255,255,0.03); }
  .chtab td.ch-name { color: var(--text); }
  .chtab td.ch-unit { color: var(--muted); }

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

  /* ---- G-meter --------------------------------------------------- */
  .gmeter-wrap { display: grid; grid-template-columns: minmax(0, 1fr) minmax(0, 1.2fr);
    gap: var(--sp-md); align-items: center; }
  @media (max-width: 720px) { .gmeter-wrap { grid-template-columns: 1fr; } }
  .gmeter { position: relative; width: 100%; max-width: 280px; aspect-ratio: 1 / 1;
    margin: 0 auto; background: var(--bg); border: 1px solid var(--line);
    border-radius: var(--r-md); overflow: hidden; }
  .gmeter canvas { position: absolute; inset: 0; width: 100%; height: 100%; }
  .gmeter .gdot { position: absolute; width: 12px; height: 12px;
    margin: -6px 0 0 -6px; border-radius: var(--r-full);
    background: var(--primary); box-shadow: 0 0 0 2px var(--bg);
    transition: left 60ms linear, top 60ms linear; }
  .gmeter .gaxis { position: absolute; color: var(--muted); font: 600 10px/1 var(--ff-ui);
    letter-spacing: 0.08em; text-transform: uppercase; pointer-events: none; }
  .gmeter .gaxis.top    { top: 6px;    left: 50%; transform: translateX(-50%); }
  .gmeter .gaxis.bot    { bottom: 6px; left: 50%; transform: translateX(-50%); }
  .gmeter .gaxis.left   { left: 6px;   top: 50%;  transform: translateY(-50%); }
  .gmeter .gaxis.right  { right: 6px;  top: 50%;  transform: translateY(-50%); }
  .gstats { display: grid; grid-template-columns: 1fr 1fr; gap: 10px; }
  .gstat { padding: 10px 12px; background: var(--surface-2); border-radius: var(--r-sm);
    border: 1px solid var(--line); }
  .gstat .label { color: var(--muted);
    font: 600 10px/1 var(--ff-ui); letter-spacing: 0.08em; text-transform: uppercase;
    margin-bottom: 4px; }
  .gstat .v { font: 600 18px/1.1 var(--ff-mono); }
  .gstat .v.accent { color: var(--primary); }

  /* ---- laps + delta ---------------------------------------------- */
  .lapcard { margin-top: var(--sp-md); }
  .lap-body { display: grid; grid-template-columns: 340px 1fr; gap: var(--sp-md); }
  @media (max-width: 820px) { .lap-body { grid-template-columns: 1fr; } }
  .lap-table-wrap { max-height: 300px; overflow-y: auto;
    border: 1px solid var(--line); border-radius: var(--r-sm); }
  table.laptable { width: 100%; border-collapse: collapse; font: 13px var(--ff-mono); }
  table.laptable th { position: sticky; top: 0; background: var(--surface-2);
    color: var(--muted); text-align: right; padding: 8px 12px;
    font: 600 10px/1 var(--ff-ui); letter-spacing: 0.07em; text-transform: uppercase; }
  table.laptable th:first-child { text-align: left; }
  table.laptable td { padding: 7px 12px; border-top: 1px solid var(--line); text-align: right; }
  table.laptable td:first-child { text-align: left; color: var(--muted); }
  table.laptable tbody tr { cursor: pointer; }
  table.laptable tbody tr:hover { background: var(--surface-2); }
  table.laptable tbody tr.sel { background: rgba(255,176,32,0.14); }
  table.laptable tbody tr.best td { color: var(--good); }
  table.laptable td.gap { color: var(--muted); }
  .lap-delta { display: flex; flex-direction: column; }
  .lap-delta canvas { width: 100%; height: auto; background: var(--bg);
    border: 1px solid var(--line); border-radius: var(--r-sm); display: block; }
  .cmp-row { display: grid; grid-template-columns: 1fr 170px; gap: 8px;
    margin-bottom: 8px; }
  .cmp-row2 { display: flex; align-items: center; gap: 8px; margin-bottom: 8px; }
  .cmp-syncl { color: var(--muted); font: 600 11px/1 var(--ff-ui);
    letter-spacing: 0.06em; text-transform: uppercase;
    display: flex; align-items: center; gap: 6px; }
  @media (max-width: 560px) { .cmp-row { grid-template-columns: 1fr; } }
  .cmp-input { background: var(--surface-2); color: var(--text);
    border: 1px solid var(--line); border-radius: var(--r-sm);
    padding: 7px 9px; font: 13px var(--ff-ui); min-width: 0; }
  .cmp-sub { color: var(--bad); font: 600 12px/1 var(--ff-mono);
    margin-top: 5px; min-height: 13px; }
  /* ---- AI corner analysis ---------------------------------------- */
  /* Composer layout: section tools on one line, the model picker as a
     labelled field (it is a setting, not an action), the quick-ask chips on
     their own row, then the prompt + ask button as one unit. */
  .ai-tool { display:flex; flex-wrap:wrap; align-items:center; gap:8px;
    margin: 0 0 var(--sp-sm); }
  .ai-tool .btn { padding: 7px 11px; }
  .ai-tool-seg { display:flex; align-items:center; gap:6px;
    padding-right:8px; border-right:1px solid var(--line); }
  .ai-tool-seg:last-of-type { padding-right:0; border-right:0; }
  .ai-tool .spacer { flex:1 1 auto; }
  .ai-field { display:flex; align-items:center; gap:7px; color: var(--muted);
    font: 600 11px var(--ff-ui); letter-spacing:0.06em; text-transform:uppercase; }
  .ai-quick { display:flex; flex-wrap:wrap; align-items:center; gap:6px;
    margin: 0 0 var(--sp-sm); }
  .ai-quick .lab { color: var(--muted); font: 600 11px var(--ff-ui);
    letter-spacing:0.06em; text-transform:uppercase; margin-right:2px; }
  .ai-preset { padding: 6px 10px; }
  .ai-compose { display:flex; align-items:flex-end; gap:8px; margin: 0 0 6px; }
  .ai-prompt { flex:1; background: var(--surface); color: var(--text);
    border: 1px solid var(--line); border-radius: var(--r-sm); padding: 9px 12px;
    font: 14px var(--ff-ui); outline: none; resize: vertical; }
  .ai-prompt:focus { border-color: var(--primary); }
  .ai-compose .btn { padding: 10px 16px; white-space:nowrap; }
  .ai-statusline { min-height: 15px; color: var(--muted);
    font: 500 11px var(--ff-mono); margin: 0 0 var(--sp-sm); }
  .ai-answer { margin-top: var(--sp-sm); padding: var(--sp-md); background: var(--surface);
    border: 1px solid var(--line); border-radius: var(--r-sm); white-space: pre-wrap;
    line-height: 1.5; max-height: 460px; overflow-y: auto; }
  .ai-answer h1,.ai-answer h2,.ai-answer h3 { font-size: 15px; margin: 10px 0 4px; color: var(--primary); }
  .ai-answer code { font-family: var(--ff-mono); color: var(--primary); }
  .ai-answer strong { color: var(--text); }
  .ai-history { margin-top: var(--sp-sm); display:flex; flex-direction:column; gap:10px; }
  /* One card per Q&A, with a left accent rail so consecutive answers read as
     separate objects instead of one long scroll. */
  .ai-hist-item { border:1px solid var(--line); border-left:3px solid var(--primary);
    border-radius: var(--r-sm); background: var(--surface); overflow:hidden; }
  .ai-hist-head { display:flex; align-items:flex-start; gap:10px; padding:11px 13px;
    cursor:pointer; }
  .ai-hist-head:hover { background: var(--surface-2); }
  .ai-hist-caret { flex:0 0 auto; color: var(--muted); font: 700 11px var(--ff-mono);
    line-height:1.7; transition: transform 120ms ease; }
  .ai-hist-item.open .ai-hist-caret { transform: rotate(90deg); color: var(--primary); }
  .ai-hist-txt { display:flex; flex-direction:column; gap:3px; min-width:0; flex:1; }
  .ai-hist-q { color: var(--text); font-weight:600; overflow:hidden;
    text-overflow:ellipsis; white-space:nowrap; }
  .ai-hist-item.open .ai-hist-q { white-space:normal; }
  .ai-hist-meta { display:flex; flex-wrap:wrap; gap:8px; align-items:center;
    color: var(--muted); font: 400 11px var(--ff-mono); }
  .ai-hist-qn { color: var(--primary); font-weight:700; }
  .ai-hist-body { display:none; padding: 12px 13px 14px;
    border-top:1px solid var(--line); white-space:normal; line-height:1.55; }
  .ai-hist-item.open .ai-hist-body { display:block; }
  .ai-hist-foot { display:none; gap:8px; padding: 9px 13px;
    border-top:1px solid var(--line); background: var(--bg); }
  .ai-hist-item.open .ai-hist-foot { display:flex; }
  .ai-hist-foot .btn { padding: 5px 10px; font-size: 12px; }
  .ai-hist-foot .ai-del-btn { margin-left:auto; color: #FFB0B0; }
  .ai-hist-body code { font-family: var(--ff-mono); color: var(--primary); }
  .ai-hist-body strong { color: var(--text); }
  /* Rendered-markdown building blocks (AI answers) */
  .ai-hist-body p { margin: 6px 0; }
  .ai-hist-body h2 { font-size: 16px; margin: 14px 0 6px; color: var(--primary);
    border-bottom: 1px solid var(--line); padding-bottom: 4px; }
  .ai-hist-body h3 { font-size: 14px; margin: 12px 0 4px; color: var(--primary); }
  .ai-hist-body ul, .ai-hist-body ol { margin: 6px 0; padding-left: 22px; }
  .ai-hist-body li { margin: 3px 0; }
  .ai-hist-body hr { border: 0; border-top: 1px solid var(--line); margin: 10px 0; }
  .ai-hist-body table { border-collapse: collapse; margin: 8px 0; width: auto;
    font: 12.5px var(--ff-mono); }
  .ai-hist-body th { background: var(--bg); color: var(--primary); font-weight: 700;
    text-align: left; padding: 6px 12px; border: 1px solid var(--line);
    border-bottom: 2px solid var(--primary); white-space: nowrap; }
  .ai-hist-body td { padding: 5px 12px; border: 1px solid var(--line); color: var(--text); }
  .ai-hist-body td.num { text-align: right; font-variant-numeric: tabular-nums; }
  .ai-hist-body tbody tr:nth-child(even) td { background: rgba(255,255,255,0.03); }
  .ai-hist-body tbody tr:hover td { background: rgba(255,176,32,0.07); }
  /* ---- filterable combobox (admin reassign) ---------------------- */
  .combo { position: relative; display: inline-block; }
  .combo-list { position: absolute; top: calc(100% + 4px); left: 0; z-index: 1000;
    min-width: 240px; max-height: 320px; overflow-y: auto; background: var(--surface-2);
    border: 1px solid var(--line); border-radius: var(--r-sm);
    box-shadow: 0 8px 24px rgba(0,0,0,0.45); }
  .combo-opt { padding: 8px 12px; cursor: pointer; color: var(--text);
    font: 13px var(--ff-mono); white-space: nowrap; }
  .combo-opt:hover, .combo-opt.active { background: var(--surface-3); color: var(--primary); }
  .combo-empty { padding: 8px 12px; color: var(--muted); font-size: 12px; }
  .leaflet-crosshair, .leaflet-crosshair .leaflet-interactive { cursor: crosshair !important; }

  .delta-wrap { position: relative; }
  .delta-cursor { position: absolute; width: 11px; height: 11px;
    margin: -6px 0 0 -6px; border-radius: var(--r-full);
    background: var(--good); box-shadow: 0 0 0 2px var(--bg);
    pointer-events: none; transition: left 60ms linear, top 60ms linear; }
</style>
</head><body>
<header class="app">
  <span class="dot"></span>
  <h1>racecar-35 \u00b7 pit wall</h1>
  <span class="crumbs"><a href="/">sessions</a> &rsaquo; __USER__ &rsaquo; <span class="mono">__FILE__</span></span>
  <span style="flex:1"></span>
  <span class="pill" id="started">__WHEN__</span>
  <span class="pill" id="count">\u2026</span>
  <span id="admin-move" style="display:none;align-items:center;gap:6px">
    <span class="combo">
      <input id="move-target" class="cmp-input" type="text"
             placeholder="reassign to user…" autocomplete="off"
             style="width:auto;min-width:210px">
      <div id="move-list" class="combo-list" style="display:none"></div>
    </span>
    <button id="move-btn" class="btn">reassign</button>
  </span>
  <a class="btn" href="/sessions/__USER__/__FILE__">download</a>
  <input id="yt-url" class="cmp-input" type="text" placeholder="YouTube link…"
         autocomplete="off" style="width:190px">
  <button id="yt-save" class="btn">link video</button>
  <a id="yt-view" class="btn" target="_blank" style="display:none">&#9654; overlay</a>
  <button id="yt-share" class="btn" style="display:none">share</button>
</header>
<main>
  <div id="loading" class="loading">loading session\u2026</div>
  <div id="app" style="display:none">
    <div class="grid">
      <div class="card">
        <div class="card-head"><span class="t-label">Track Map</span>
          <span class="t-label" id="gps-status">\u2014</span></div>
        <div id="map">
          <div class="dotlegend" id="dotlegend">
            <div class="dl-row"><span class="dl-dot" style="background:#FFB020"></span><span id="dl-you">your lap (amber)</span></div>
            <div class="dl-row" id="dl-refrow"><span class="dl-line"></span><span class="dl-dot" style="background:#FF5D5D"></span><span id="dl-ref">comparison lap (red)</span></div>
            <div class="dl-row dl-note" id="dl-note"></div>
          </div>
        </div>
        <div id="mapopts" class="mapopts">
          <label class="mapchk" for="opt-sat">
            <input type="checkbox" id="opt-sat">
            <span>Satellite view</span>
          </label>
          <span class="sep"></span>
          <a id="map3d" class="mapbtn" target="_blank" href="/track3d/__USER__/__FILE__"
             title="first-person driving view built from the data alone: your line as a road ribbon on the logged elevation, speed-coloured, with brake/apex/throttle markers">\u25b6 3D drive view</a>
          <button id="map-circle" class="mapbtn ghost" type="button"
                  title="drag on the map to mark a corner or section — it feeds the 3D view and the ideal line">\u25cb circle a section</button>
          <span class="sep"></span>
          <span class="hint" id="map-region">no region selected</span>
          <span class="hint">\u00b7 uncheck satellite for a plain black map</span>
        </div>
      </div>
      <div class="tiles">
        <div class="tile full">
          <div class="label">Speed</div>
          <div><span class="t-tel-lg val accent" id="v-speed">\u2014</span><span class="unit">mph</span></div>
          <div class="cmp-sub" id="c-speed"></div>
        </div>
        <div class="tile">
          <div class="label">RPM</div>
          <div><span class="t-tel-md val accent" id="v-rpm">\u2014</span></div>
          <div class="cmp-sub" id="c-rpm"></div>
        </div>
        <div class="tile">
          <div class="label">Heading</div>
          <div><span class="t-tel-md val" id="v-hdg">\u2014</span><span class="unit">\u00b0</span></div>
          <div class="cmp-sub" id="c-hdg"></div>
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
        <div class="tile full">
          <div class="label">G-Meter</div>
          <div class="gmeter-wrap">
            <div class="gmeter" id="gmeter">
              <canvas id="gtrail" width="560" height="560"></canvas>
              <div class="gaxis top">brake</div>
              <div class="gaxis bot">accel</div>
              <div class="gaxis left">left</div>
              <div class="gaxis right">right</div>
              <div class="gdot" id="gdot" style="left:50%;top:50%"></div>
            </div>
            <div class="gstats">
              <div class="gstat"><div class="label">Lateral</div>
                <div class="v accent" id="v-glat">\u2014</div></div>
              <div class="gstat"><div class="label">Long.</div>
                <div class="v accent" id="v-glong">\u2014</div></div>
              <div class="gstat"><div class="label">Vertical</div>
                <div class="v" id="v-gvert">\u2014</div></div>
              <div class="gstat"><div class="label">Peak |G|</div>
                <div class="v" id="v-gpeak">\u2014</div></div>
            </div>
          </div>
        </div>
        <div class="tile full">
          <div class="label">All channels logged <span class="t-label" id="ch-note"></span></div>
          <table class="chtab">
            <thead><tr><th>channel</th><th>unit</th><th>n</th><th>min</th><th>avg</th><th>max</th></tr></thead>
            <tbody id="chtab"></tbody>
          </table>
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

    <div class="card lapcard" id="lapcard" style="display:none">
      <div class="card-head"><span class="t-label">Laps</span>
        <span class="t-label" id="lap-sub">—</span></div>
      <div class="card-body lap-body">
        <div class="lap-table-wrap">
          <table class="laptable">
            <thead><tr><th>Lap</th><th>Time</th><th>+/−</th><th>Max</th><th></th></tr></thead>
            <tbody id="lap-rows"></tbody>
          </table>
        </div>
        <div class="lap-delta">
          <div class="cmp-row">
            <input id="cmp-session" class="cmp-input" list="cmp-sessions"
                   placeholder="compare vs another session (type to filter)">
            <datalist id="cmp-sessions"></datalist>
            <select id="cmp-lap" class="cmp-input"></select>
          </div>
          <div class="cmp-row2">
            <label class="cmp-syncl">sync
              <select id="sync-mode" class="cmp-input">
                <option value="time">time</option>
                <option value="loc">location</option>
              </select>
            </label>
            <span style="flex:1"></span>
            <button id="cmp-reset" class="btn">best lap</button>
          </div>
          <div class="t-label" style="margin:2px 0 6px">Delta
            <span id="delta-sel" style="color:var(--muted)"></span>
            <span id="delta-live" class="mono" style="float:right"></span></div>
          <div class="delta-wrap">
            <canvas id="deltacanv" width="760" height="200"></canvas>
            <div class="delta-cursor" id="delta-cursor" style="display:none"></div>
          </div>
        </div>
      </div>
    </div>

    <div class="card aicard" id="aicard" style="display:none">
      <div class="card-head"><span class="t-label">AI Corner Analysis</span>
        <span class="t-label" id="ai-region">no region selected</span></div>
      <div class="card-body">
        <div class="ai-tool">
          <span class="ai-tool-seg">
            <button id="ai-draw" class="btn" type="button">○ circle a section</button>
            <button id="ai-clear" class="btn" type="button">clear</button>
          </span>
          <span class="ai-tool-seg">
            <button id="ai-line" class="btn" type="button" title="popout: fastest real line through this section vs yours, with brake/apex/throttle markers and speed labels">ideal line ↗</button>
            <button id="ai-coach" class="btn" type="button" title="run the whole-session review that normally happens automatically on upload, and file 1-3 checklist items">checklist</button>
          </span>
          <span class="spacer"></span>
          <label class="ai-field">model
            <select id="ai-model" class="cmp-input" style="width:auto;min-width:150px"></select>
          </label>
        </div>
        <div class="ai-quick">
          <span class="lab">quick asks</span>
          <button class="btn ai-preset" type="button" data-q="Analyze the braking zone for this section: where should I brake, how hard, and how consistent am I lap to lap?">brake zones</button>
          <button class="btn ai-preset" type="button" data-q="How is my corner entry speed through this section, and where can I carry more speed in?">entry speed</button>
          <button class="btn ai-preset" type="button" data-q="How is my corner exit and throttle application through this section? Where am I losing exit speed?">exit speed</button>
          <button class="btn ai-preset" type="button" data-q="What is the fastest line through this section and how does my best lap compare to the others here?">best line</button>
          <button class="btn ai-preset" type="button" data-q="How consistent am I through this section lap to lap, and which lap was best and why?">consistency</button>
        </div>
        <div class="ai-compose">
          <textarea id="ai-prompt" class="ai-prompt" rows="2"
            placeholder="Ask about the section you circled — entry/exit speed, brake points, best line, consistency…"></textarea>
          <button id="ai-ask" class="btn primary" type="button">ask ai</button>
        </div>
        <div class="ai-statusline"><span id="ai-status"></span></div>
        <div id="ai-history" class="ai-history"></div>
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
    resp = await fetch('/sessions/' + encodeURIComponent(USER) + '/' + encodeURIComponent(FILE) + '/data?target=12000');
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
  el('count').textContent = (data.total && data.total > data.count)
      ? (data.count + ' of ' + data.total + ' samples')
      : (data.count + ' samples');

  // ---- every channel the session actually carries ------------------------
  // The firmware logs all of its live sources (speed, rpm, coolant, oil,
  // altitude, MAP, IAT, battery, CAN AFR, bench oil, IMU, throttle, timing
  // advance, AEM AFR/lambda). Rather than one tile per channel this lists
  // whatever is PRESENT with n/min/avg/max — a row that is missing tells you
  // that source was not live (or is not wired), which is the useful answer.
  (function () {
    const UNITS = { speed_mph: 'mph', rpm: 'rpm', heading_deg: 'deg', alt_m: 'm',
      coolant_f: 'F', oil_psi: 'psi', oil_can_psi: 'psi', afr: 'AFR', afr_can: 'AFR',
      lambda: 'lambda', afr_v: 'mV', afr_status: '0-3', iat_f: 'F', map_kpa: 'kPa',
      tps_pct: '%', spark_deg: 'deg BTDC', batt_v: 'V', ax: 'g', ay: 'g', az: 'g',
      gx: 'deg/s', gy: 'deg/s', gz: 'deg/s', lap: '#' };
    const NAMES = { speed_mph: 'speed', rpm: 'rpm', heading_deg: 'heading',
      alt_m: 'altitude (GPS, MSL)', coolant_f: 'coolant',
      oil_psi: 'oil pressure (direct ADC)', oil_can_psi: 'oil pressure (CAN)',
      afr: 'AFR (AEM gauge)', afr_can: 'AFR (MS3 CAN)', lambda: 'lambda (AEM)',
      afr_v: 'AFR gauge voltage', afr_status: 'AFR status',
      iat_f: 'intake air temp', map_kpa: 'manifold pressure', tps_pct: 'throttle',
      spark_deg: 'timing advance', batt_v: 'battery', lap: 'lap counter',
      ax: 'accel x', ay: 'accel y', az: 'accel z',
      gx: 'gyro x', gy: 'gyro y', gz: 'gyro z' };
    // lat/lon/fix/sats have their own tiles; t is the clock.
    const SKIP = { t: 1, t_ms: 1, lat: 1, lon: 1, fix: 1, sats: 1 };
    const ORDER = ['speed_mph', 'rpm', 'heading_deg', 'alt_m', 'coolant_f', 'oil_psi',
      'oil_can_psi', 'afr', 'afr_can', 'lambda', 'afr_v', 'afr_status', 'iat_f',
      'map_kpa', 'tps_pct', 'spark_deg', 'batt_v', 'lap', 'ax', 'ay', 'az',
      'gx', 'gy', 'gz'];
    // lambda carries four decimals — rounding it to 1 makes every row read 1.0
    const DEC = { lambda: 4, afr: 2, afr_can: 2, afr_v: 0, afr_status: 0, lap: 0,
      rpm: 0, batt_v: 2, ax: 2, ay: 2, az: 2 };
    const keys = [], seen = {};
    for (const s of S) {
      for (const k in s) {
        if (SKIP[k] || seen[k]) continue;
        if (typeof s[k] !== 'number') continue;   // null == sensor absent
        seen[k] = 1; keys.push(k);
      }
    }
    if (!keys.length) { el('ch-note').textContent = 'none in this session'; return; }
    keys.sort(function (a, b) {
      const ia = ORDER.indexOf(a), ib = ORDER.indexOf(b);
      return (ia < 0 ? 999 : ia) - (ib < 0 ? 999 : ib) || a.localeCompare(b);
    });
    const tb = el('chtab');
    let shown = 0;
    for (const k of keys) {
      let n = 0, mn = Infinity, mx = -Infinity, sum = 0;
      for (const s of S) {
        const v = s[k];
        if (typeof v !== 'number' || !isFinite(v)) continue;
        n++; sum += v; if (v < mn) mn = v; if (v > mx) mx = v;
      }
      if (!n) continue;
      shown++;
      const dc = DEC[k] == null ? 1 : DEC[k];
      const tr = document.createElement('tr');
      tr.innerHTML = '<td class="ch-name">' + (NAMES[k] || k) + '</td>' +
        '<td class="ch-unit">' + (UNITS[k] || '') + '</td>' +
        '<td>' + n + '</td><td>' + fmt(mn, dc) + '</td><td>' +
        fmt(sum / n, Math.max(dc, 2)) + '</td><td>' + fmt(mx, dc) + '</td>';
      tb.appendChild(tr);
    }
    el('ch-note').textContent = shown + ' channels \u00b7 n / min / avg / max';
  })();

  // ---- Leaflet map (basemap from the server config; default = keyless Esri
  // imagery. CARTO's dark tiles now need an API key and would render a
  // "API KEY REQUIRED" placeholder here.) ---------------------------------
  const map = L.map('map', { zoomControl: true, attributionControl: true });
  // ---- basemap on/off ---------------------------------------------------
  // Satellite tiles, or nothing but a black surface. The checkbox lives in
  // the strip directly under the map; the choice is remembered per browser.
  // Tiles are an additive layer, so toggling never disturbs the trace
  // polylines / lap markers / lasso polygon drawn on top of them.
  const sat_chk = el('opt-sat');
  let sat_layer = null;
  function applySat(on) {
    if (on && __MAP_TILES__ && !sat_layer) {
      sat_layer = L.tileLayer(__MAP_TILES__, {
        maxZoom: __MAP_MAXZOOM__, attribution: __MAP_ATTRIB__
      }).addTo(map);
    } else if (!on && sat_layer) {
      map.removeLayer(sat_layer);
      sat_layer = null;
    }
    document.getElementById('map').classList.toggle('nosat', !on);
    if (sat_chk) sat_chk.checked = !!on;
    try { localStorage.setItem('rc5.sat', on ? '1' : '0'); } catch (e) {}
  }
  if (!__MAP_TILES__) {
    const mo = el('mapopts');
    if (mo) mo.style.display = 'none';   // server ships no basemap at all
  } else {
    let sat_on = true;                   // default: satellite ON
    try { sat_on = localStorage.getItem('rc5.sat') !== '0'; } catch (e) {}
    if (sat_chk) sat_chk.addEventListener('change',
      function () { applySat(sat_chk.checked); });
    applySat(sat_on);
  }

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

  // ---- timestamps (normalize: prefer epoch t, else t_ms, else synthetic) ----
  // Real timestamps come through as `t` (unix epoch seconds). Bench/test
  // sessions captured before NTP/RTC sync use `t_ms` (relative ms since
  // session start). When NEITHER is usable we fall back to synthetic 25 Hz
  // time so playback still tracks the sampled cadence instead of jumping
  // to the end in a single frame.
  const SAMPLE_HZ_DEFAULT = 25;
  const T = new Array(S.length);
  let firstT = null;
  for (let i = 0; i < S.length; i++) {
    const s = S[i];
    let v = null;
    if (typeof s.t === 'number' && isFinite(s.t))         v = s.t;
    else if (typeof s.t_ms === 'number' && isFinite(s.t_ms)) v = s.t_ms / 1000;
    if (v != null && firstT == null) firstT = v;
    T[i] = v;
  }
  let usableTime = (firstT != null);
  if (usableTime) {
    // anchor to first usable sample, fill gaps by linear interpolation
    let last = firstT;
    for (let i = 0; i < S.length; i++) {
      if (T[i] == null) T[i] = last;
      else last = T[i];
    }
    const span = T[S.length-1] - T[0];
    if (!(span > 0.5)) usableTime = false;   // single moment / corrupt
  }
  if (!usableTime) {
    for (let i = 0; i < S.length; i++) T[i] = i / SAMPLE_HZ_DEFAULT;
  }
  const T0 = T[0];
  const TEND = T[S.length-1];
  const totalSec = Math.max(0, TEND - T0);
  el('t-total').textContent = fmtTime(totalSec) + (usableTime ? '' : ' (est)');

  // ---- G-meter setup -------------------------------------------------
  // IMU axes (firmware): +ax = forward, +ay = right-positive lateral push,
  // +az = up. On a g-g plot we want brake-up / accel-down / right-positive
  // lateral, so we plot (lat=-ay, long=-ax). Vertical (az) subtract 1 g for
  // gravity to show vertical perturbation only.
  const gcanv = el('gtrail');
  const gctx  = gcanv.getContext('2d');
  const G_MAX = 2.0;   // canvas edges = +/- 2 g
  const gdot  = el('gdot');

  // pre-compute per-sample g-values once
  const GLat  = new Array(S.length);
  const GLong = new Array(S.length);
  const GVert = new Array(S.length);
  let peakG = 0;
  let haveImu = false;
  for (let i = 0; i < S.length; i++) {
    const s = S[i];
    const lat  = (typeof s.ay === 'number' && isFinite(s.ay)) ? -s.ay : null;
    const lng  = (typeof s.ax === 'number' && isFinite(s.ax)) ? -s.ax : null;
    const vert = (typeof s.az === 'number' && isFinite(s.az)) ? (s.az - 1) : null;
    GLat[i]  = lat;
    GLong[i] = lng;
    GVert[i] = vert;
    if (lat != null || lng != null) haveImu = true;
    if (lat != null && lng != null) {
      const mag = Math.sqrt(lat*lat + lng*lng);
      if (mag > peakG) peakG = mag;
    }
  }
  el('v-gpeak').textContent = haveImu ? (peakG.toFixed(2) + ' g') : '\u2014';

  function drawGTrail() {
    const W = gcanv.width, H = gcanv.height;
    gctx.clearRect(0, 0, W, H);
    const cx = W/2, cy = H/2;
    const r1g = (W/2) / G_MAX;
    // grid: concentric circles at 0.5g, 1.0g, 1.5g + cross hairs
    gctx.strokeStyle = 'rgba(255,255,255,0.07)';
    gctx.lineWidth = 1;
    for (let g = 0.5; g <= G_MAX - 0.001; g += 0.5) {
      gctx.beginPath(); gctx.arc(cx, cy, g * r1g, 0, Math.PI*2); gctx.stroke();
    }
    gctx.beginPath();
    gctx.moveTo(0, cy); gctx.lineTo(W, cy);
    gctx.moveTo(cx, 0); gctx.lineTo(cx, H);
    gctx.stroke();
    // 1 g reference ring brighter
    gctx.strokeStyle = 'rgba(255,176,32,0.25)';
    gctx.beginPath(); gctx.arc(cx, cy, 1.0 * r1g, 0, Math.PI*2); gctx.stroke();
    // historical g-g points
    if (haveImu) {
      gctx.fillStyle = 'rgba(255,176,32,0.18)';
      for (let i = 0; i < S.length; i++) {
        const lat = GLat[i], lng = GLong[i];
        if (lat == null || lng == null) continue;
        const px = cx + Math.max(-G_MAX, Math.min(G_MAX, lat)) * r1g;
        const py = cy - Math.max(-G_MAX, Math.min(G_MAX, lng)) * r1g;
        gctx.fillRect(px - 1, py - 1, 2, 2);
      }
    }
  }
  drawGTrail();

  function placeGDotVal(lat, lng) {
    if (lat == null || lng == null) { gdot.style.display = 'none'; return; }
    gdot.style.display = '';
    // map [-G_MAX, +G_MAX] -> [0%, 100%]
    const xPct = ((Math.max(-G_MAX, Math.min(G_MAX, lat))) / G_MAX) * 50 + 50;
    const yPct = 50 - ((Math.max(-G_MAX, Math.min(G_MAX, lng))) / G_MAX) * 50;
    gdot.style.left = xPct.toFixed(2) + '%';
    gdot.style.top  = yPct.toFixed(2) + '%';
  }
  function placeGDot(idx) { placeGDotVal(GLat[idx], GLong[idx]); }

  // ---- slider + render ------------------------------------------------
  const slider = el('slider');
  slider.max = String(S.length - 1);

  // shared lap / comparison state, declared before render() so the playback
  // path can touch it without a temporal-dead-zone error (it is null until
  // laps load, and every consumer guards on that).
  let deltaState = null, lapWindow = null, currentRef = null, primLap = null;
  let syncMode = 'time', compDot = null, compDotOn = false;

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
    el('t-now').textContent   = fmtTime(T[idx] - T0);
    if (dot && typeof s.lat === 'number' && typeof s.lon === 'number' && (s.lat || s.lon)) {
      dot.setLatLng([s.lat, s.lon]);
    }
    el('v-glat').textContent  = (GLat[idx]  == null) ? '\u2014' : GLat[idx].toFixed(2)  + ' g';
    el('v-glong').textContent = (GLong[idx] == null) ? '\u2014' : GLong[idx].toFixed(2) + ' g';
    el('v-gvert').textContent = (GVert[idx] == null) ? '\u2014' : GVert[idx].toFixed(2) + ' g';
    placeGDot(idx);
    placeDeltaCursor(idx);
    updateCompReadouts(idx);
  }
  slider.addEventListener('input', () => render(Number(slider.value)));
  render(0);

  // ---- interpolated render for smooth playback ------------------------
  // The /data feed is downsampled for fast load (a long session may be only a
  // few Hz), so snapping the follow-dot to discrete samples looks jerky. During
  // playback we render at the real clock `target` (seconds) and LERP position +
  // readouts between the two bracketing samples, so motion stays smooth at 60fps
  // no matter the sample rate. Scrubbing still uses the exact-sample render().
  const lerpN = (a, b, f) =>
    (typeof a === 'number' && isFinite(a) && typeof b === 'number' && isFinite(b))
      ? a + (b - a) * f : (typeof a === 'number' ? a : b);
  function lerpHeading(a, b, f) {
    if (typeof a !== 'number') return b;
    if (typeof b !== 'number') return a;
    let d = ((b - a + 540) % 360) - 180;   // shortest way round the compass
    return (a + d * f + 360) % 360;
  }
  function renderAt(target) {
    const endIdx = S.length - 1;
    let i0 = Number(slider.value);
    while (i0 < endIdx && T[i0 + 1] <= target) i0++;
    while (i0 > 0 && T[i0] > target) i0--;
    const i1 = Math.min(i0 + 1, endIdx);
    const span = (T[i1] - T[i0]) || 1;
    let f = (target - T[i0]) / span;
    if (!isFinite(f) || f < 0) f = 0; else if (f > 1) f = 1;
    const a = S[i0], b = S[i1];
    const speed = lerpN(a.speed_mph, b.speed_mph, f);
    el('v-speed').textContent = (typeof speed === 'number')
        ? (speed >= 100 ? fmtInt(speed) : fmt(speed, 1)) : '\u2014';
    el('v-rpm').textContent = fmtInt(lerpN(a.rpm, b.rpm, f));
    el('v-hdg').textContent = fmt(lerpHeading(a.heading_deg, b.heading_deg, f), 0);
    const lat = lerpN(a.lat, b.lat, f), lon = lerpN(a.lon, b.lon, f);
    el('v-lat').textContent = fmt(lat, 6);
    el('v-lon').textContent = fmt(lon, 6);
    el('v-alt').textContent = fmt(lerpN(a.alt_m, b.alt_m, f), 1);
    const fix = a.fix, sats = a.sats;
    el('v-fix').textContent = (fix == null) ? '\u2014'
      : (FIX_NAMES[fix] || ('fix ' + fix)) + (sats != null ? ' \u00b7 ' + sats : '');
    el('t-now').textContent = fmtTime(target - T0);
    if (dot && typeof lat === 'number' && typeof lon === 'number' && (lat || lon)) {
      dot.setLatLng([lat, lon]);
    }
    const gl = lerpN(GLat[i0], GLat[i1], f);
    const gL = lerpN(GLong[i0], GLong[i1], f);
    const gv = lerpN(GVert[i0], GVert[i1], f);
    el('v-glat').textContent  = (gl == null) ? '\u2014' : gl.toFixed(2) + ' g';
    el('v-glong').textContent = (gL == null) ? '\u2014' : gL.toFixed(2) + ' g';
    el('v-gvert').textContent = (gv == null) ? '\u2014' : gv.toFixed(2) + ' g';
    placeGDotVal(gl, gL);
    placeDeltaCursor(i0, i0 + f);
    updateCompReadouts(i0, i0 + f);
  }

  // ---- play / pause ---------------------------------------------------
  // Drives the slider in real time, paced by the normalized T[] array so
  // both real-clock and synthetic-25Hz files play at the right cadence.
  let playing = false, playT = 0, lastTick = 0, rafId = 0;
  const playBtn = el('play');
  function tick(now) {
    if (!playing) return;
    const dt = (now - lastTick) / 1000;
    lastTick = now;
    playT += dt;
    const target = T0 + playT;
    // when a lap is selected, playback is scoped to that lap so the
    // follow-dot sweeps exactly one lap; otherwise it runs the whole session.
    const endIdx = lapWindow ? lapWindow.i1 : S.length - 1;
    let next = Number(slider.value);
    while (next < endIdx && T[next+1] <= target) next++;
    if (target >= T[endIdx] || next >= endIdx) {
      slider.value = String(endIdx); render(endIdx); stop(); return;
    }
    slider.value = String(next);
    renderAt(target);         // smooth interpolated frame
    rafId = requestAnimationFrame(tick);
  }
  function start() {
    const endIdx = lapWindow ? lapWindow.i1 : S.length - 1;
    const startIdx = lapWindow ? lapWindow.i0 : 0;
    let idx = Number(slider.value);
    if (idx >= endIdx) { idx = startIdx; slider.value = String(startIdx); render(startIdx); }
    playT = T[idx] - T0;
    playing = true; lastTick = performance.now();
    playBtn.textContent = 'pause';
    rafId = requestAnimationFrame(tick);
  }
  function stop() {
    playing = false; playBtn.textContent = 'play';
    if (rafId) cancelAnimationFrame(rafId);
  }
  playBtn.addEventListener('click', () => playing ? stop() : start());

  // ---- laps + comparison + delta --------------------------------------
  // relT[i] = seconds-from-start, the basis /laps returns t_start/t_end in,
  // so a lap window maps straight onto a sample array. selfCtx wraps THIS
  // session; comparison laps from OTHER sessions get their own ctx, fetched
  // on demand and cached, so we can diff against another driver's lap.
  const relT = T.map(t => t - T0);
  const selfCtx = { S: S, relT: relT };
  function hav(a, b){
    const R = 6371000, d2r = Math.PI/180;
    const dLat = (b[0]-a[0])*d2r, dLon = (b[1]-a[1])*d2r;
    const s = Math.sin(dLat/2)**2 +
              Math.cos(a[0]*d2r)*Math.cos(b[0]*d2r)*Math.sin(dLon/2)**2;
    return R*2*Math.atan2(Math.sqrt(s), Math.sqrt(1-s));
  }
  function buildRelT(samples){
    const arr = new Array(samples.length); let first=null;
    for (let i=0;i<samples.length;i++){
      const s=samples[i]; let v=null;
      if (typeof s.t==='number' && isFinite(s.t)) v=s.t;
      else if (typeof s.t_ms==='number' && isFinite(s.t_ms)) v=s.t_ms/1000;
      if (v!=null && first==null) first=v; arr[i]=v;
    }
    let usable = first!=null;
    if (usable){ let last=first;
      for (let i=0;i<arr.length;i++){ if (arr[i]==null) arr[i]=last; else last=arr[i]; }
      if (!(arr.length && arr[arr.length-1]-arr[0] > 0.5)) usable=false;
    }
    if (!usable) for (let i=0;i<arr.length;i++) arr[i]=i/25;
    const t0 = arr.length ? arr[0] : 0;
    return arr.map(t=>t-t0);
  }
  function ctxIdxAt(ctx, relSec){
    const r=ctx.relT, last=ctx.S.length-1;
    if (last<0) return 0;
    if (relSec<=r[0]) return 0;
    if (relSec>=r[last]) return last;
    let lo=0, hi=last;
    while (lo<hi){ const m=(lo+hi)>>1; if (r[m]<relSec) lo=m+1; else hi=m; }
    return lo;
  }
  function lapFmt(sec){
    if (!isFinite(sec)) return '\u2014';
    const m=Math.floor(sec/60), r=sec-m*60;
    return m+':'+r.toFixed(2).padStart(5,'0');
  }
  function ctxSeg(ctx, lap){
    const seg=[]; const i0=ctxIdxAt(ctx,lap.t_start), i1=ctxIdxAt(ctx,lap.t_end);
    for (let i=i0;i<=i1;i++){ const s=ctx.S[i];
      if (typeof s.lat==='number' && typeof s.lon==='number' && (s.lat||s.lon)) seg.push([s.lat,s.lon]); }
    return seg;
  }
  // cumulative distance + time-into-lap, sample-aligned to i0..i1
  function ctxSeries(ctx, lap){
    const i0=ctxIdxAt(ctx,lap.t_start), i1=ctxIdxAt(ctx,lap.t_end);
    const dist=[], tm=[]; let d=0, prev=null;
    for (let i=i0;i<=i1;i++){ const s=ctx.S[i];
      if (typeof s.lat==='number' && typeof s.lon==='number' && (s.lat||s.lon)){
        const cur=[s.lat,s.lon]; if (prev) d+=hav(prev,cur); prev=cur; }
      dist.push(d); tm.push(ctx.relT[i]-lap.t_start);
    }
    return {dist, tm};
  }
  function interpTime(series, dq){
    const {dist, tm}=series, last=dist.length-1;
    if (last<0) return 0;
    if (dq<=dist[0]) return tm[0];
    if (dq>=dist[last]) return tm[last];
    let lo=0, hi=last;
    while (lo<hi){ const m=(lo+hi)>>1; if (dist[m]<dq) lo=m+1; else hi=m; }
    const i=Math.max(1,lo); const d0=dist[i-1], d1=dist[i];
    if (d1===d0) return tm[i-1];
    return tm[i-1]+(tm[i]-tm[i-1])*(dq-d0)/(d1-d0);
  }
  // a reference (ghost) lap, with its sample window + series precomputed
  function makeRef(ctx, lap, label){
    return { ctx, lap, label,
             i0: ctxIdxAt(ctx, lap.t_start), i1: ctxIdxAt(ctx, lap.t_end),
             series: ctxSeries(ctx, lap) };
  }
  // (refIdxByTime / refIdxByDist lived here: integer-index lookups. They were the
  //  reason the red comparison dot stepped between samples — replaced by
  //  idxFrac/seriesAt/refPosAt below, which interpolate.)

  // ---- sub-sample interpolation ---------------------------------------
  // Playback renders at 60 fps but the sample stream is much coarser (a long
  // session loads at only a few Hz), so ANYTHING driven by a raw integer index
  // visibly steps — that was the jerky red comparison dot. idxFrac returns a
  // FRACTIONAL index for a value in a monotonic array; seriesAt lerps a series
  // value at that index; refPosAt lerps a ghost lap's lat/lon.
  function idxFrac(arr, v){
    const last=arr.length-1;
    if (last<0) return 0;
    if (!(v>arr[0])) return 0;
    if (v>=arr[last]) return last;
    let lo=0, hi=last;
    while (lo<hi){ const m=(lo+hi)>>1; if (arr[m]<v) lo=m+1; else hi=m; }
    const i=Math.max(1,lo), a=arr[i-1], b=arr[i];
    return (b>a) ? (i-1)+(v-a)/(b-a) : (i-1);
  }
  function seriesAt(series, fi, key){
    const arr=series[key], n=arr.length-1;
    if (n<0) return null;
    const c=Math.max(0,Math.min(n,fi));
    const k0=Math.floor(c), k1=Math.min(k0+1,n);
    return lerpN(arr[k0], arr[k1], c-k0);
  }
  function refPosAt(ref, fi){
    const n=ref.i1-ref.i0;
    if (n<0) return null;
    const c=Math.max(0,Math.min(n,fi)), k0=Math.floor(c);
    const A=ref.ctx.S[ref.i0+k0], B=ref.ctx.S[ref.i0+Math.min(k0+1,n)] || A;
    if (!A) return null;
    const lat=lerpN(A.lat, B.lat, c-k0), lon=lerpN(A.lon, B.lon, c-k0);
    if (typeof lat!=='number' || typeof lon!=='number' || !(lat||lon)) return null;
    return [lat, lon];
  }

  // ---- map ghost lines + comparison dot ------------------------------
  let selLine=null, refLine=null;
  const dcanv = el('deltacanv');
  function showCompDot(pos){
    if (!compDot) compDot=L.circleMarker(pos,
      {radius:6, color:'#3a0d0d', weight:2, fillColor:'#FF5D5D', fillOpacity:1});
    if (!compDotOn){ compDot.addTo(map); compDotOn=true; }
    compDot.setLatLng(pos);
  }
  function hideCompDot(){ if (compDot && compDotOn){ map.removeLayer(compDot); compDotOn=false; } }
  function highlight(primLapArg, ref){
    if (selLine) map.removeLayer(selLine);
    if (refLine) map.removeLayer(refLine);
    const sameLap = ref && ref.ctx===selfCtx && ref.lap.lap===primLapArg.lap;
    if (ref && !sameLap){
      // The ghost lap is the SAME car the red dot follows, so draw it in the
      // same red — light + semi-transparent + dashed, so it reads as "not you"
      // and still loses to the amber line (weight 4, solid) underneath it.
      refLine=L.polyline(ctxSeg(ref.ctx, ref.lap),
        {color:'#FF7A7A', weight:2, opacity:0.6, dashArray:'5 7'}).addTo(map);
    }
    const seg=ctxSeg(selfCtx, primLapArg);
    selLine=L.polyline(seg, {color:'#FFB020', weight:4, opacity:0.95}).addTo(map);
    if (seg.length) map.fitBounds(selLine.getBounds(), {padding:[20,20]});
    updateDotLegend();
  }

  // ---- on-map key: which dot/line is which car ------------------------
  function updateDotLegend(){
    const you=el('dl-you'), ref=el('dl-ref'), row=el('dl-refrow'), note=el('dl-note');
    if (!you) return;
    you.textContent = primLap ? ('lap '+primLap.lap+'  \u2014 the lap you are viewing')
                              : 'the lap you are viewing';
    const sameLap = !!(currentRef && primLap && currentRef.ctx===selfCtx
                       && currentRef.lap.lap===primLap.lap);
    const showRef = !!(currentRef && primLap && !sameLap);
    if (row) row.style.display = showRef ? '' : 'none';
    if (ref && showRef) ref.textContent = currentRef.label+'  \u2014 the other car';
    if (note){
      if (sameLap) note.textContent = 'no second line: it is the same lap';
      else if (showRef && syncMode!=='time') note.textContent = 'same-place sync: line shown, dot hidden';
      else note.textContent = '';
    }
  }

  // ---- delta chart + follow-cursor -----------------------------------
  function drawDelta(primLapArg, ref){
    const ctx=dcanv.getContext('2d'), W=dcanv.width, H=dcanv.height;
    ctx.clearRect(0,0,W,H);
    ctx.fillStyle='#0E1014'; ctx.fillRect(0,0,W,H);
    const sa=ctxSeries(selfCtx, primLapArg);
    const sb=ref ? ctxSeries(ref.ctx, ref.lap) : sa;
    const maxD=Math.min(sa.dist[sa.dist.length-1]||0, sb.dist[sb.dist.length-1]||0);
    const N=240, dv=[]; let dmin=Infinity, dmax=-Infinity;
    if (maxD>0){
      for (let k=0;k<N;k++){
        const dq=maxD*k/(N-1);
        const v=interpTime(sa,dq)-interpTime(sb,dq);
        dv.push(v); if (v<dmin)dmin=v; if (v>dmax)dmax=v;
      }
    }
    if (!isFinite(dmin)){ dmin=-0.1; dmax=0.1; }
    const pad=Math.max(0.15,(dmax-dmin)*0.15);
    const lo=Math.min(dmin,-0.05)-pad, hi=Math.max(dmax,0.05)+pad;
    const X=k=>34+(W-44)*k/(N-1);
    const Y=v=>10+(H-28)*(1-(v-lo)/(hi-lo));
    ctx.strokeStyle='rgba(255,255,255,0.22)'; ctx.lineWidth=1;
    ctx.beginPath(); ctx.moveTo(34,Y(0)); ctx.lineTo(W-10,Y(0)); ctx.stroke();
    if (maxD>0){
      ctx.lineWidth=2; ctx.strokeStyle='#FFB020'; ctx.beginPath();
      for (let k=0;k<N;k++){ const px=X(k), py=Y(dv[k]); k?ctx.lineTo(px,py):ctx.moveTo(px,py); }
      ctx.stroke();
    }
    ctx.fillStyle='#8A92A3'; ctx.font='11px monospace'; ctx.textAlign='left';
    ctx.fillText('+'+hi.toFixed(2), 2, 14);
    ctx.fillText(lo.toFixed(2), 2, H-6);
    if (maxD>0){
      const fin=dv[N-1];
      ctx.fillStyle = fin<=0 ? '#6CD07A' : '#FF5D5D';
      ctx.font='600 14px monospace'; ctx.textAlign='right';
      ctx.fillText((fin<=0?'':'+')+fin.toFixed(2)+'s', W-12, 18);
    }
    deltaState={ i0:ctxIdxAt(selfCtx,primLapArg.t_start), i1:ctxIdxAt(selfCtx,primLapArg.t_end),
                 primSeries:sa, refSeries:sb, maxD, lo, hi };
    placeDeltaCursor(Number(slider.value));
  }
  function placeDeltaCursor(idx, fidx){
    const cur=el('delta-cursor'); const live=el('delta-live');
    if (!cur) return;
    if (!deltaState || idx<deltaState.i0 || idx>deltaState.i1 || !(deltaState.maxD>0)){
      cur.style.display='none'; if (live) live.textContent=''; return;
    }
    const k=((typeof fidx==='number') ? fidx : idx)-deltaState.i0;
    const dq=seriesAt(deltaState.primSeries, k, 'dist');
    const tmK=seriesAt(deltaState.primSeries, k, 'tm');
    if (dq==null || tmK==null){ cur.style.display='none'; return; }
    const v=tmK-interpTime(deltaState.refSeries, dq);
    const W=dcanv.width, H=dcanv.height;
    const px=34+(W-44)*Math.min(1, dq/deltaState.maxD);
    const py=10+(H-28)*(1-(v-deltaState.lo)/(deltaState.hi-deltaState.lo));
    cur.style.display='';
    cur.style.left=(px/W*100).toFixed(2)+'%';
    cur.style.top=(py/H*100).toFixed(2)+'%';
    cur.style.background = v<=0 ? '#6CD07A' : '#FF5D5D';
    if (live){ live.textContent=(v<=0?'':'+')+v.toFixed(2)+'s';
      live.style.color = v<=0 ? '#6CD07A' : '#FF5D5D'; }
  }

  // ---- comparison telemetry (red sub-values) + sync mode -------------
  function setCompTiles(s){
    const set=(id,txt)=>{ const e=el(id); if (e) e.textContent = txt||''; };
    if (!s){ set('c-speed',''); set('c-rpm',''); set('c-hdg',''); return; }
    set('c-speed', (s.speed_mph!=null) ?
      ('vs '+(s.speed_mph>=100?fmtInt(s.speed_mph):fmt(s.speed_mph,1))+' mph') : '');
    set('c-rpm', (s.rpm!=null) ? ('vs '+fmtInt(s.rpm)) : '');
    set('c-hdg', (s.heading_deg!=null) ? ('vs '+fmt(s.heading_deg,0)+'\u00b0') : '');
  }
  function updateCompReadouts(idx, fidx){
    const active = currentRef && primLap && lapWindow && deltaState
      && idx>=lapWindow.i0 && idx<=lapWindow.i1
      && !(currentRef.ctx===selfCtx && currentRef.lap.lap===primLap.lap);
    if (!active){ hideCompDot(); setCompTiles(null); return; }
    // fidx = FRACTIONAL index into S[] while playing; idx is the sample index
    // when scrubbing. Interpolating here is what keeps the red dot smooth.
    const k = ((typeof fidx==='number') ? fidx : idx) - lapWindow.i0;
    const tInto = seriesAt(deltaState.primSeries, k, 'tm');
    const dq    = seriesAt(deltaState.primSeries, k, 'dist');
    if (tInto==null || dq==null){ hideCompDot(); setCompTiles(null); return; }
    let rkF;
    if (syncMode==='time'){
      // same elapsed time into the lap -> generally a DIFFERENT place; show
      // where the comparison car was at this instant with its own red dot.
      rkF=idxFrac(currentRef.series.tm, tInto);
      const pos=refPosAt(currentRef, rkF);
      if (pos) showCompDot(pos); else hideCompDot();
    } else {
      // same place on track -> one dot; compare the telemetry at this spot.
      rkF=idxFrac(currentRef.series.dist, dq);
      hideCompDot();
    }
    setCompTiles(currentRef.ctx.S[currentRef.i0+Math.round(rkF)]);
  }

  // ---- selection state ------------------------------------------------
  let defaultRef=null, selfLaps=[], selfBest=null;
  function setRef(ref){
    currentRef=ref;
    if (!primLap) return;
    el('delta-sel').textContent='\u00b7 lap '+primLap.lap+' vs '+(ref?ref.label:'\u2014');
    highlight(primLap, ref);
    drawDelta(primLap, ref);
    updateDotLegend();
    render(Number(slider.value));
  }
  function selectPrimary(lap, rowsEl){
    primLap=lap;
    if (rowsEl) [...rowsEl.children].forEach(tr =>
      tr.classList.toggle('sel', Number(tr.dataset.lap)===lap.lap));
    lapWindow={ i0:ctxIdxAt(selfCtx, lap.t_start), i1:ctxIdxAt(selfCtx, lap.t_end) };
    el('delta-sel').textContent='\u00b7 lap '+lap.lap+' vs '+(currentRef?currentRef.label:'\u2014');
    highlight(lap, currentRef);
    drawDelta(lap, currentRef);
    updateDotLegend();
    slider.value=String(lapWindow.i0); render(lapWindow.i0);
  }

  // ---- comparison session / lap pickers ------------------------------
  const compCache={}; let compSessions={}, cmpEntry=null, cmpSess=null;
  function enc(s){ return encodeURIComponent(s); }
  async function loadCompSession(user, file){
    if (user===USER && file===FILE) return {ctx:selfCtx, laps:selfLaps, best:selfBest};
    const key=user+'/'+file;
    if (compCache[key]) return compCache[key];
    const [dr, lr]=await Promise.all([
      fetch('/sessions/'+enc(user)+'/'+enc(file)+'/data').then(r=>r.json()),
      fetch('/sessions/'+enc(user)+'/'+enc(file)+'/laps').then(r=>r.json())
    ]);
    const cs=dr.samples||[];
    const ctx={ S:cs, relT:buildRelT(cs) };
    const laps=lr.laps||[];
    const best=laps.find(l=>l.lap===lr.best_lap) || laps[0] || null;
    const entry={ ctx, laps, best };
    compCache[key]=entry; return entry;
  }
  function fillLapPicker(entry){
    const sel=el('cmp-lap'); sel.innerHTML='';
    if (!entry || !entry.laps.length) return;
    const best=entry.best;
    for (const lap of entry.laps){
      const o=document.createElement('option'); o.value=String(lap.lap);
      const gap=lap.seconds-(best?best.seconds:lap.seconds);
      o.textContent='Lap '+lap.lap+' \u2014 '+lapFmt(lap.seconds)+
        (best && lap.lap===best.lap ? ' (fastest)' : ' (+'+gap.toFixed(2)+')');
      sel.appendChild(o);
    }
    if (best) sel.value=String(best.lap);
  }
  function applyCompLap(){
    if (!cmpEntry) return;
    const lapNo=Number(el('cmp-lap').value);
    const lap=cmpEntry.laps.find(l=>l.lap===lapNo); if (!lap) return;
    const sameSelf=(cmpSess.user===USER && cmpSess.file===FILE);
    const who=cmpSess.user.split('@')[0].split('_')[0];
    const label=sameSelf ? ('lap '+lap.lap) : (who+' lap '+lap.lap);
    setRef(makeRef(cmpEntry.ctx, lap, label));
  }
  async function loadCompList(){
    let j;
    try { const r=await fetch('/sessions'); if (!r.ok) return; j=await r.json(); }
    catch(e){ return; }
    const dl=el('cmp-sessions'); dl.innerHTML=''; compSessions={};
    for (const s of (j.sessions||[])){
      const ep=(s.display_epoch||s.mtime||0)*1000;
      const date=ep ? new Date(ep).toISOString().slice(0,16).replace('T',' ') : '';
      const label=s.user+' \u00b7 '+date+' \u00b7 '+s.filename.replace(/\\.ndjson$/,'');
      compSessions[label]={ user:s.user, file:s.filename };
      const o=document.createElement('option'); o.value=label; dl.appendChild(o);
    }
  }
  el('cmp-session').addEventListener('change', async ()=>{
    const val=el('cmp-session').value.trim();
    const sess=compSessions[val];
    el('cmp-lap').innerHTML='';
    if (!sess) return;
    try { cmpEntry=await loadCompSession(sess.user, sess.file); cmpSess=sess; }
    catch(e){ return; }
    fillLapPicker(cmpEntry);
    applyCompLap();
  });
  el('cmp-lap').addEventListener('change', applyCompLap);
  el('cmp-reset').addEventListener('click', ()=>{
    el('cmp-session').value=''; el('cmp-lap').innerHTML='';
    cmpEntry=null; cmpSess=null;
    setRef(defaultRef);
  });
  el('sync-mode').addEventListener('change', ()=>{
    syncMode=el('sync-mode').value;
    render(Number(slider.value));
    updateDotLegend();
  });

  // ---- load this session's laps + wire the table ---------------------
  (async function loadLaps(){
    let lr;
    try {
      const r=await fetch('/sessions/'+enc(USER)+'/'+enc(FILE)+'/laps');
      if (!r.ok) return; lr=await r.json();
    } catch(e){ return; }
    const laps=lr.laps||[];
    if (!laps.length) return;
    selfLaps=laps;
    const bestLap=laps.find(l=>l.lap===lr.best_lap) || laps[0];
    selfBest=bestLap;
    defaultRef=makeRef(selfCtx, bestLap, 'best lap (lap '+bestLap.lap+')');
    currentRef=defaultRef;
    el('lapcard').style.display='';
    el('lap-sub').textContent=laps.length+' laps \u00b7 best '+lapFmt(bestLap.seconds);
    const rows=el('lap-rows'); rows.innerHTML='';
    for (const lap of laps){
      const tr=document.createElement('tr'); tr.dataset.lap=lap.lap;
      if (lap.lap===bestLap.lap) tr.classList.add('best');
      const gap=lap.seconds-bestLap.seconds;
      const gapTxt=(lap.lap===bestLap.lap) ? 'best' : '+'+gap.toFixed(2);
      tr.innerHTML='<td>'+lap.lap+'</td><td>'+lapFmt(lap.seconds)+
        '</td><td class="gap">'+gapTxt+'</td><td>'+
        (lap.max_mph!=null?Math.round(lap.max_mph):'\u2014')+'</td>'+
        '<td><button class="btn" data-xlap="'+lap.lap+'" '+
        'title="exclude this lap from best/deltas" '+
        'style="padding:1px 7px;line-height:1.1">\u2715</button></td>';
      tr.addEventListener('click', ()=>selectPrimary(lap, rows));
      rows.appendChild(tr);
    }
    // Excluded laps (garbage crossings, or manually dropped): greyed, with a
    // restore control. They keep their original numbers.
    for (const lap of (lr.excluded_laps||[])){
      const tr=document.createElement('tr');
      tr.style.opacity='0.45';
      tr.innerHTML='<td>'+lap.lap+'</td><td>'+lapFmt(lap.seconds)+
        '</td><td class="gap">excluded ('+(lap.excluded_reason||'')+')</td><td>'+
        (lap.max_mph!=null?Math.round(lap.max_mph):'\u2014')+'</td>'+
        '<td><button class="btn" data-rlap="'+lap.lap+'" title="restore this lap" '+
        'style="padding:1px 7px;line-height:1.1">\u21a9</button></td>';
      rows.appendChild(tr);
    }
    rows.addEventListener('click', async (ev)=>{
      const xb=ev.target.closest('[data-xlap]'), rb=ev.target.closest('[data-rlap]');
      if (!xb && !rb) return;
      ev.stopPropagation();
      const lap=parseInt((xb||rb).dataset.xlap||(xb||rb).dataset.rlap,10);
      try {
        const r=await fetch('/sessions/'+enc(USER)+'/'+enc(FILE)+'/laps/exclude',{
          method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({lap, exclude:!!xb})});
        if (!r.ok){ const j=await r.json().catch(()=>({}));
          alert('lap update failed: '+((j&&j.detail)||('HTTP '+r.status))); return; }
        location.reload();
      } catch(e){ alert('lap update failed: '+e.message); }
    });
    if ((lr.excluded_laps||[]).length)
      el('lap-sub').textContent += ' \u00b7 '+lr.excluded_laps.length+' excluded';
    selectPrimary(bestLap, rows);
    loadCompList();
  })();

  // ---- AI corner analysis ------------------------------------------
  (function(){
    const card = el('aicard'); if (!card) return;
    let poly = null, pts = [], drawing = false;
    // The lasso is a MAP feature (it feeds the ideal line AND the 3D drive
    // view), so it is driven from two places: the strip under the map and the
    // AI card. Both buttons mirror the same state, and the AI card can be
    // hidden entirely (no AI key on the server) without killing the lasso.
    const drawBtn = el('ai-draw'), mapDraw = el('map-circle'),
          mapInfo = el('map-region'), clearBtn = el('ai-clear'),
          info = el('ai-region'), status = el('ai-status'),
          histEl = el('ai-history'), modelSel = el('ai-model');

    function inPoly(lat, lon, P){
      let inside=false;
      for (let i=0,j=P.length-1;i<P.length;j=i++){
        const yi=P[i][0],xi=P[i][1],yj=P[j][0],xj=P[j][1];
        if (((yi>lat)!==(yj>lat)) &&
            (lon < (xj-xi)*(lat-yi)/((yj-yi)||1e-15)+xi)) inside=!inside;
      }
      return inside;
    }
    function regionInfo(){
      let txt;
      if (pts.length<3){ txt = 'no region selected'; }
      else {
        let n=0;
        for (const s of S){
          if (typeof s.lat==='number' && typeof s.lon==='number' && (s.lat||s.lon)
              && inPoly(s.lat, s.lon, pts)) n++;
        }
        txt = n+' points in region';
      }
      info.textContent = txt;
      if (mapInfo) mapInfo.textContent = txt;
    }
    function setDraw(on){
      drawing=on;
      [drawBtn, mapDraw].forEach(b=>{
        if (!b) return;
        b.classList.toggle('primary', on);
        b.classList.toggle('active', on);
        b.textContent = on ? 'drag on the map…' : '○ circle a section';
      });
      const c = map.getContainer();
      if (on){ map.dragging.disable(); c.classList.add('leaflet-crosshair'); }
      else   { map.dragging.enable();  c.classList.remove('leaflet-crosshair'); }
    }
    function onMove(e){ pts.push([e.latlng.lat, e.latlng.lng]); if(poly) poly.setLatLngs(pts); }
    drawBtn.addEventListener('click', ()=> setDraw(!drawing));
    if (mapDraw) mapDraw.addEventListener('click', ()=> setDraw(!drawing));
    clearBtn.addEventListener('click', ()=>{
      if (poly){ map.removeLayer(poly); poly=null; } pts=[]; regionInfo();
    });
    map.on('mousedown', e=>{
      if (!drawing) return;
      pts=[[e.latlng.lat, e.latlng.lng]];
      if (poly){ map.removeLayer(poly); poly=null; }
      poly = L.polygon([], {color:'#6CD07A', weight:2, fillColor:'#6CD07A', fillOpacity:0.15}).addTo(map);
      map.on('mousemove', onMove);
    });
    map.on('mouseup', ()=>{
      if (!drawing) return;
      map.off('mousemove', onMove);
      setDraw(false);
      if (pts.length<3){ if(poly){map.removeLayer(poly);poly=null;} pts=[]; }
      regionInfo();
    });

    // Markdown -> HTML (block-level: tables, headings, lists, hr, paragraphs;
    // inline: bold, italic, code). Escapes FIRST, so the AI can never inject
    // markup. Tables get thead/tbody + numeric cells right-aligned.
    function mdInline(s){
      return s.replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>')
              .replace(/(^|[^*])\\*([^*\\s][^*]*)\\*/g,'$1<em>$2</em>')
              .replace(/`([^`]+)`/g,'<code>$1</code>');
    }
    function md(t){
      const esc = (t||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      const L = esc.split(/\\r?\\n/);
      const out = [];
      const isNum = c => /^[-+]?\\$?\\d[\\d,]*\\.?\\d*\\s*(s|ms|mph|m|km|rpm|g|%)?$/i.test(c.trim());
      let i = 0;
      while (i < L.length){
        const ln = L[i];
        if (!ln.trim()){ i++; continue; }
        // table: | a | b | followed by |---|---|
        if (/^\\s*\\|.*\\|\\s*$/.test(ln) && i+1 < L.length && /^\\s*\\|[\\s:|-]+\\|\\s*$/.test(L[i+1])){
          const cells = r => r.trim().replace(/^\\|/,'').replace(/\\|$/,'').split('|').map(c=>c.trim());
          const head = cells(ln);
          const align = cells(L[i+1]).map(c => /^:-+:$/.test(c) ? 'center' : /-+:$/.test(c) ? 'right' : '');
          let h = '<table><thead><tr>';
          head.forEach((c,k)=>{ h += '<th'+(align[k]?' style="text-align:'+align[k]+'"':'')+'>'+mdInline(c)+'</th>'; });
          h += '</tr></thead><tbody>';
          i += 2;
          while (i < L.length && /^\\s*\\|.*\\|\\s*$/.test(L[i])){
            h += '<tr>';
            cells(L[i]).forEach((c,k)=>{
              const cls = (align[k]==='right' || (!align[k] && isNum(c))) ? ' class="num"' : '';
              const st  = align[k]==='center' ? ' style="text-align:center"' : '';
              h += '<td'+cls+st+'>'+mdInline(c)+'</td>';
            });
            h += '</tr>'; i++;
          }
          out.push(h+'</tbody></table>');
          continue;
        }
        // heading
        const hm = ln.match(/^(#{1,6})\\s+(.+)$/);
        if (hm){ out.push((hm[1].length<=2?'<h2>':'<h3>')+mdInline(hm[2])+(hm[1].length<=2?'</h2>':'</h3>')); i++; continue; }
        // horizontal rule
        if (/^\\s*(-{3,}|\\*{3,}|_{3,})\\s*$/.test(ln)){ out.push('<hr>'); i++; continue; }
        // list (unordered or ordered)
        if (/^\\s*([-*+]|\\d+[.)])\\s+/.test(ln)){
          const ord = /^\\s*\\d+[.)]/.test(ln);
          let h = ord ? '<ol>' : '<ul>';
          while (i < L.length && /^\\s*([-*+]|\\d+[.)])\\s+/.test(L[i])){
            h += '<li>'+mdInline(L[i].replace(/^\\s*([-*+]|\\d+[.)])\\s+/,''))+'</li>'; i++;
          }
          out.push(h + (ord ? '</ol>' : '</ul>'));
          continue;
        }
        // paragraph: gather until blank/structural line
        let para = [ln];
        i++;
        while (i < L.length && L[i].trim()
               && !/^\\s*\\|.*\\|\\s*$/.test(L[i]) && !/^#{1,6}\\s+/.test(L[i])
               && !/^\\s*([-*+]|\\d+[.)])\\s+/.test(L[i]) && !/^\\s*-{3,}\\s*$/.test(L[i])){
          para.push(L[i]); i++;
        }
        out.push('<p>'+mdInline(para.join(' '))+'</p>');
      }
      return out.join('');
    }

    async function loadModels(){
      try {
        const r = await fetch('/ai/models'); const j = await r.json();
        if (!j.enabled){ card.style.display='none'; return; }
        card.style.display='';
        modelSel.innerHTML='';
        const list = (j.models||[]);
        if (!list.length && j.default) list.push({id:j.default, name:j.default});
        for (const m of list){
          const o=document.createElement('option'); o.value=m.id; o.textContent=m.name;
          if (m.id===j.default) o.selected=true; modelSel.appendChild(o);
        }
        if (!list.length){ const o=document.createElement('option'); o.textContent='(server default)'; o.value=''; modelSel.appendChild(o); }
        // Single allowed model -> hide the picker (nothing to choose).
        const lbl = modelSel.closest('label'); if (lbl) lbl.style.display = (list.length<=1 ? 'none' : '');
        loadHistory();
      } catch(e){ card.style.display='none'; }
    }

    const esc = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
    function fmtWhen(ts){
      try { return new Date(ts*1000).toLocaleString(); } catch(e){ return ''; }
    }
    // Render the persistent Q&A list, newest first; first item expanded.
    // One card each, with a numbered badge, a caret, the answer in its own
    // bordered block and the actions on a footer bar — consecutive answers used
    // to run together because the action row had no styling at all.
    function renderHistory(list){
      const items = (list||[]).slice().reverse();
      const total = items.length;
      histEl.innerHTML = '';
      items.forEach((e, k)=>{
        const div = document.createElement('div');
        div.className = 'ai-hist-item' + (k===0 ? ' open' : '');
        const q = esc(e.question || '(no question)');
        // usage/cost is only present for ADMIN viewers (server-gated)
        let cost = '';
        if (e.usage){
          if (e.usage.cost_usd != null) cost += '$' + (+e.usage.cost_usd).toFixed(4);
          if (e.usage.total_tokens) cost += (cost?' · ':'') + e.usage.total_tokens + ' tok';
        }
        const meta = [
          esc(e.model||''), (e.laps||0)+' laps', cost, fmtWhen(e.ts)
        ].filter(Boolean).join(' · ');
        div.innerHTML =
          '<div class="ai-hist-head">'+
            '<span class="ai-hist-caret">▶</span>'+
            '<span class="ai-hist-txt">'+
              '<span class="ai-hist-q">'+q+'</span>'+
              '<span class="ai-hist-meta"><span class="ai-hist-qn">Q'+(total-k)+'</span>'+
                '<span>'+meta+'</span></span>'+
            '</span>'+
          '</div>'+
          '<div class="ai-hist-body">'+md(e.answer||'')+'</div>'+
          '<div class="ai-hist-foot">'+
            '<button class="btn ai-region-btn" type="button">show region</button>'+
            '<button class="btn ai-del-btn" type="button">delete</button>'+
          '</div>';
        div.querySelector('.ai-hist-head').addEventListener('click', ()=> div.classList.toggle('open'));
        div.querySelector('.ai-region-btn').addEventListener('click', ()=> showRegion(e.region && e.region.points));
        div.querySelector('.ai-del-btn').addEventListener('click', ()=> delEntry(e.id));
        histEl.appendChild(div);
      });
    }
    function showRegion(P){
      if (!P || P.length<3) return;
      pts = P.map(x=>[x[0],x[1]]);
      if (poly){ map.removeLayer(poly); poly=null; }
      poly = L.polygon(pts, {color:'#6CD07A', weight:2, fillColor:'#6CD07A', fillOpacity:0.15}).addTo(map);
      map.fitBounds(poly.getBounds(), {padding:[40,40]});
      regionInfo();
    }
    async function delEntry(id){
      if (!confirm('Delete this AI question and its answer?')) return;
      try {
        const r = await fetch('/sessions/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+'/ai/delete', {
          method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({id})
        });
        const j = await r.json();
        if (r.ok) renderHistory(j.history);
      } catch(e){}
    }
    async function loadHistory(){
      try {
        const r = await fetch('/sessions/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+'/ai/history');
        const j = await r.json();
        renderHistory(j.history);
      } catch(e){}
    }

    async function ask(){
      if (pts.length<3){ status.textContent='circle a section of track first'; return; }
      const q = (el('ai-prompt').value||'').trim();
      status.textContent='analyzing…';
      el('ai-ask').disabled=true;
      try {
        const r = await fetch('/sessions/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+'/ai', {
          method:'POST', headers:{'Content-Type':'application/json'},
          body: JSON.stringify({ prompt:q, model:modelSel.value, region:{points:pts} })
        });
        const j = await r.json();
        if (!r.ok){ status.textContent='error: '+((j&&j.detail)||('HTTP '+r.status)); el('ai-ask').disabled=false; return; }
        status.textContent = 'model: '+j.model;
        el('ai-prompt').value='';
        renderHistory(j.history);
      } catch(e){ status.textContent='request failed: '+e.message; }
      el('ai-ask').disabled=false;
    }
    el('ai-ask').addEventListener('click', ask);
    // Manual whole-session coach review — the path used when auto-review is
    // off, or for sessions uploaded before the feature existed. Idempotent
    // server-side: it reports "already reviewed" rather than duplicating.
    el('ai-coach').addEventListener('click', async ()=>{
      const b=el('ai-coach'); b.disabled=true; const old=b.textContent; b.textContent='reviewing\u2026';
      try{
        const r=await fetch('/sessions/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+'/coach?force=1',
                            {method:'POST'});
        const j=await r.json();
        if(!r.ok) status.textContent='checklist error: '+((j&&j.detail)||('HTTP '+r.status));
        else if(j.already) status.textContent='already reviewed — see the checklist';
        else status.textContent='checklist: '+(j.added||0)+' item(s) added';
      }catch(e){ status.textContent='checklist failed: '+e.message; }
      b.disabled=false; b.textContent=old;
    });
    el('ai-line').addEventListener('click', ()=>{
      if (pts.length<3){ status.textContent='circle a section of track first'; return; }
      // Decimate: hundreds of lasso points blow the server's request-line
      // limit ('request too large'). ~50 vertices keeps the polygon shape
      // and the URL ~1.5 KB.
      const step = Math.max(1, Math.ceil(pts.length/50));
      const dec = pts.filter((_,i)=> i%step===0 );
      const enc = dec.map(p=>p[0].toFixed(6)+','+p[1].toFixed(6)).join('|');
      window.open('/lineview/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+
                  '?pts='+encodeURIComponent(enc), 'lineview',
                  'width=1200,height=850,menubar=no,toolbar=no');
    });
    // 3D drive view: the href alone (no polygon) is a whole-session drive.
    // With a circled section we pass the SAME decimated polygon, so the 3D
    // window can draw the ideal line / your best line on the ground next to
    // the line you actually drove — the "us vs the computer" comparison.
    el('map3d').addEventListener('click', (ev)=>{
      if (pts.length<3) return;
      ev.preventDefault();
      const step = Math.max(1, Math.ceil(pts.length/50));
      const enc = pts.filter((_,i)=> i%step===0 )
                     .map(p=>p[0].toFixed(6)+','+p[1].toFixed(6)).join('|');
      window.open('/track3d/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+
                  '?pts='+encodeURIComponent(enc), 'track3d');
    });
    document.querySelectorAll('.ai-preset').forEach(b=>{
      b.addEventListener('click', ()=>{ el('ai-prompt').value=b.dataset.q; ask(); });
    });
    loadModels();

    // ---- session tools: change TRACK + this session's coach checklist ----
    (function(){
      // Sits as its own toolbar row directly under the AI toolbar (the AI card's
      // structure is tool rows on top, quick asks + composer + history below).
      var row=document.createElement('div'); row.className='ai-tool';
      row.innerHTML='<label class="ai-field">track '+
        '<select id="trk-sel" class="cmp-input" style="width:auto;min-width:170px"></select></label>'+
        '<input id="trk-custom" class="cmp-input" style="display:none;width:170px" placeholder="custom track name">'+
        '<button id="trk-save" class="btn" type="button">rename session</button>'+
        '<span class="t-label" id="trk-msg"></span>';
      var tool=el('ai-draw').closest('.ai-tool');
      var anchor = tool && tool.parentNode ? tool : el('ai-draw').parentNode;
      anchor.parentNode.insertBefore(row, anchor.nextSibling);
      var chd=document.createElement('div'); chd.id='sess-coach';
      anchor.parentNode.insertBefore(chd, row.nextSibling);
      var sel=document.getElementById('trk-sel'), cus=document.getElementById('trk-custom'),
          msg=document.getElementById('trk-msg');
      fetch('/tracks').then(function(r){return r.json();}).then(function(j){
        (j.tracks||[]).forEach(function(t){ var o=document.createElement('option');
          o.value=t; o.textContent=t; sel.appendChild(o); });
        var o=document.createElement('option'); o.value='__custom__'; o.textContent='(custom...)';
        sel.appendChild(o);
        var curTrack=FILE.replace(/^[0-9]+_/,'').replace(/(-combined)?[.]ndjson$/,'').replace(/_/g,' ');
        for(var i=0;i<sel.options.length;i++)
          if(sel.options[i].value.toLowerCase().replace(/_/g,' ')===curTrack.toLowerCase()){sel.selectedIndex=i;break;}
      });
      sel.addEventListener('change', function(){ cus.style.display = sel.value==='__custom__' ? '' : 'none'; });
      document.getElementById('trk-save').addEventListener('click', function(){
        var t = sel.value==='__custom__' ? cus.value.trim() : sel.value;
        if(!t){ msg.textContent='pick a track'; return; }
        msg.textContent='renaming...';
        fetch('/sessions/'+encodeURIComponent(USER)+'/'+encodeURIComponent(FILE)+'/rename',
              {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({track:t})})
        .then(function(r){return r.json().then(function(j){return {ok:r.ok,j:j};});})
        .then(function(x){
          if(!x.ok){ msg.textContent='error: '+(x.j.detail||'failed'); return; }
          if(x.j.unchanged){ msg.textContent='already that track'; return; }
          location.href='/review/'+encodeURIComponent(USER)+'/'+encodeURIComponent(x.j.file);
        }).catch(function(e){ msg.textContent='failed: '+e.message; });
      });
      function loadSessCoach(){
        fetch('/coach/'+encodeURIComponent(USER)).then(function(r){return r.json();}).then(function(j){
          var items=(j.items||[]).filter(function(i){return i.session===FILE;});
          if(!items.length){ chd.innerHTML=''; return; }
          var h='<div class="t-label" style="margin:6px 0 4px">CHECKLIST FROM THIS SESSION</div>';
          items.forEach(function(i){
            h+='<div style="display:flex;gap:10px;align-items:center;margin:4px 0">'+
               '<button class="btn sc-tick" data-id="'+i.id+'" '+(i.done?'disabled':'')+
               ' style="min-width:34px">'+(i.done?'\u2713':'\u25a1')+'</button>'+
               '<span style="'+(i.done?'opacity:.5':'')+'">'+
               i.text.replace(/&/g,'&amp;').replace(/</g,'&lt;')+'</span></div>';
          });
          chd.innerHTML=h;
          chd.querySelectorAll('.sc-tick').forEach(function(b){
            b.addEventListener('click', function(){
              fetch('/coach/'+encodeURIComponent(USER)+'/done',{method:'POST',
                headers:{'Content-Type':'application/json'},
                body:JSON.stringify({id:b.dataset.id,by:'web'})}).then(loadSessCoach);
            });
          });
        }).catch(function(e){});
      }
      loadSessCoach();
    })();
  })();
})();
</script>
<script>
// Admin-only: reassign this session (and its AI history) to another user.
(async function(){
  const U='__USER__', F='__FILE__';
  let me;
  try { me = await (await fetch('/me')).json(); } catch(e){ return; }
  if (!me || !me.is_admin) return;
  const wrap=document.getElementById('admin-move');
  const sel=document.getElementById('move-target');
  const list=document.getElementById('move-list');
  const btn=document.getElementById('move-btn');
  if (!wrap||!sel||!list||!btn) return;
  let targets=[];
  try {
    const t = await (await fetch('/admin/sessions/targets')).json();
    targets = (t.targets||[]).slice().sort((a,b)=>a.toLowerCase().localeCompare(b.toLowerCase()));
  } catch(e){}
  wrap.style.display='inline-flex';

  // Custom filterable dropdown: click shows the full alphabetical list; typing
  // filters (substring, case-insensitive); click a row or Enter to pick.
  let active=-1, shown=[];
  function renderList(){
    const q=(sel.value||'').trim().toLowerCase();
    shown = q ? targets.filter(x=>x.toLowerCase().includes(q)) : targets.slice();
    list.innerHTML='';
    if (!shown.length){
      const d=document.createElement('div'); d.className='combo-empty';
      d.textContent = targets.length ? 'no match — type a full email to add' : 'no users found';
      list.appendChild(d);
    } else {
      shown.forEach((em,i)=>{
        const d=document.createElement('div'); d.className='combo-opt'+(i===active?' active':'');
        d.textContent=em;
        d.addEventListener('mousedown', ev=>{ ev.preventDefault(); sel.value=em; hide(); });
        list.appendChild(d);
      });
    }
    list.style.display='';
  }
  function hide(){ list.style.display='none'; active=-1; }
  sel.addEventListener('focus', ()=>{ active=-1; renderList(); });
  sel.addEventListener('input', ()=>{ active=-1; renderList(); });
  sel.addEventListener('keydown', ev=>{
    if (list.style.display==='none') return;
    if (ev.key==='ArrowDown'){ ev.preventDefault(); active=Math.min(active+1, shown.length-1); renderList(); }
    else if (ev.key==='ArrowUp'){ ev.preventDefault(); active=Math.max(active-1, 0); renderList(); }
    else if (ev.key==='Enter'){ if (active>=0 && shown[active]){ ev.preventDefault(); sel.value=shown[active]; hide(); } }
    else if (ev.key==='Escape'){ hide(); }
  });
  document.addEventListener('click', ev=>{ if (!wrap.contains(ev.target)) hide(); });

  btn.addEventListener('click', async ()=>{
    const target=(sel.value||'').trim().toLowerCase();
    if (!target){ sel.focus(); return; }
    // Allow either a known target (email or slug) or a fresh, valid email.
    const known = targets.some(x=>x.toLowerCase()===target);
    if (!known && (target.indexOf('@')<0 || target.indexOf('.')<0)){
      alert('Pick a user from the list, or type a valid email address.'); sel.focus(); return;
    }
    if (!confirm('Move this session (and its AI history) to '+target+'?\\nThe URL will change to that user.')) return;
    btn.disabled=true; btn.textContent='moving\u2026';
    try {
      const r=await fetch('/admin/sessions/move', {method:'POST',headers:{'Content-Type':'application/json'},
        body: JSON.stringify({user:U, filename:F, target})});
      const j=await r.json();
      if (r.ok && j.review){ location.href=j.review; return; }
      alert('move failed: '+((j&&j.detail)||('HTTP '+r.status)));
    } catch(e){ alert('move failed: '+e.message); }
    btn.disabled=false; btn.textContent='reassign';
  });
})();
</script>
<script>
/* ---- YouTube link + overlay viewer ---------------------------------- */
(async function(){
  const enc = encodeURIComponent;
  const url = document.getElementById('yt-url');
  const save = document.getElementById('yt-save');
  const view = document.getElementById('yt-view');
  const share = document.getElementById('yt-share');
  const base  = '/sessions/'+enc('__USER__')+'/'+enc('__FILE__')+'/video';
  const sbase = '/sessions/'+enc('__USER__')+'/'+enc('__FILE__')+'/share';
  let offset_ms = 0;
  let shareTok = null;
  function reflectShare(){
    share.textContent = shareTok ? 'shared \u2713' : 'share';
  }
  function reflect(j){
    offset_ms = (j&&j.offset_ms)||0;
    if (j && j.id){
      url.value = j.url || j.id;
      view.style.display = '';
      view.href = '/overlay/__USER__/__FILE__';
      save.textContent = 'update';
      share.style.display = '';
    } else {
      view.style.display = 'none';
      share.style.display = 'none';
      save.textContent = 'link video';
    }
  }
  try { reflect(await (await fetch(base)).json()); } catch(e){}
  // Share state is owner-or-admin; a 403 just means the button acts as create-on-click.
  try { const r=await fetch(sbase); if (r.ok){ shareTok=(await r.json()).token; reflectShare(); } } catch(e){}
  share.addEventListener('click', async ()=>{
    if (shareTok){
      const full = location.origin + '/shared/' + shareTok;
      const revoke = !prompt('PUBLIC view-only overlay link (anyone with it can watch \u2014 nothing can be changed):\\n\\nCopy it, or clear this box and press OK to REVOKE the link.', full);
      if (revoke && confirm('Revoke the public link? It stops working immediately.')){
        try {
          const r=await fetch(sbase+'/revoke',{method:'POST'});
          if (r.ok){ shareTok=null; reflectShare(); }
        } catch(e){}
      }
      return;
    }
    try {
      const r=await fetch(sbase,{method:'POST'});
      const j=await r.json().catch(()=>({}));
      if (!r.ok) throw new Error((j&&j.detail)||('HTTP '+r.status));
      shareTok=j.token; reflectShare();
      prompt('PUBLIC view-only overlay link created \u2014 copy it:', location.origin+j.url);
    } catch(e){ alert('share failed: '+e.message); }
  });
  save.addEventListener('click', async ()=>{
    save.disabled = true;
    try {
      const r = await fetch(base, {method:'POST',
        headers:{'Content-Type':'application/json'},
        body: JSON.stringify({url: url.value.trim(), offset_ms})});
      const j = await r.json().catch(()=>({}));
      if (!r.ok) throw new Error((j&&j.detail)||('HTTP '+r.status));
      reflect(await (await fetch(base)).json());
    } catch(e){ alert('video link failed: '+e.message); }
    save.disabled = false;
  });
})();
</script>
</body></html>
"""
)


# ---------------------------------------------------------------------------
# Overlay viewer: full-screen YouTube + HTML telemetry HUD.
# Self-contained page (no Leaflet — the track map is a plain canvas drawn from
# the GPS trace). Sync model: data_rel_seconds = video_seconds + offset.
# Controls: coarse slider (±5 min), fine nudge buttons (0.05/1/10 s), a
# one-click "SYNC @ LAUNCH" auto-helper (scrub the video to the moment the car
# starts moving, click — offset is computed from the data's launch instant),
# and SAVE (persists to the /video sidecar). HUD ticks at 20 Hz off
# player.getCurrentTime(), samples looked up by binary search.
# ---------------------------------------------------------------------------
_OVERLAY_HTML = (
    """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>overlay · __FILE__</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  html,body { margin:0; padding:0; background:#000; height:100%; overflow:hidden;
    font-family: system-ui, sans-serif; }
  #stage { position:fixed; inset:0; background:#000; }
  #yt, #ytwrap { position:absolute; inset:0; }
  #ytwrap iframe { width:100%; height:100%; }
  .hud { position:absolute; pointer-events:none; z-index:5;
    text-shadow: 0 1px 3px rgba(0,0,0,0.9); color:#fff; }
  #tmap { position:absolute; left:16px; top:16px; z-index:5; pointer-events:none;
    background: rgba(0,0,0,0.35); border-radius:10px; }
  #speedbox { left:16px; bottom:76px; text-align:left; }
  #spd { font: 700 84px/0.95 'JetBrains Mono', monospace; letter-spacing:-2px; }
  #spd .u { font: 600 20px/1 system-ui; color:#FFB020; margin-left:6px; }
  #rpmbar { width:260px; height:14px; background:rgba(255,255,255,0.15);
    border-radius:7px; margin-top:8px; overflow:hidden; }
  #rpmfill { height:100%; width:0%; background:#FFB020; border-radius:7px; }
  #rpmtxt { font: 600 15px 'JetBrains Mono', monospace; margin-top:4px; color:#ddd; }
  #gtxt { font: 600 14px 'JetBrains Mono', monospace; margin-top:2px; color:#9ad; }
  #lapbox { right:16px; top:16px; text-align:right; }
  #lapbox .n  { font: 700 26px 'JetBrains Mono', monospace; color:#FFB020; }
  #lapbox .t  { font: 700 44px 'JetBrains Mono', monospace; }
  #lapbox .s  { font: 600 15px 'JetBrains Mono', monospace; color:#bbb; margin-top:2px; }
  #bar { position:absolute; left:0; right:0; bottom:0; z-index:10;
    display:flex; gap:8px; align-items:center; flex-wrap:wrap;
    padding:10px 14px; background:rgba(10,10,12,0.85);
    transition:opacity .25s; }
  #bar.hidden { opacity:0; pointer-events:none; }
  #bar button, #bar a { background:#222; color:#eee; border:1px solid #444;
    border-radius:6px; padding:7px 11px; font:600 13px system-ui; cursor:pointer;
    text-decoration:none; }
  #bar button.acc { background:#5a4200; border-color:#FFB020; color:#ffd77a; }
  #bar .off { font: 700 15px 'JetBrains Mono', monospace; color:#FFB020;
    min-width:86px; text-align:center; }
  #bar input[type=range] { flex:1; min-width:120px; accent-color:#FFB020; }
  #msg { position:absolute; left:50%; top:40%; transform:translate(-50%,-50%);
    color:#eee; font:600 18px system-ui; z-index:20; text-align:center;
    background:rgba(0,0,0,0.7); padding:18px 26px; border-radius:10px; display:none; }
  #toast { position:absolute; left:50%; bottom:74px; transform:translateX(-50%);
    color:#0d0; font:600 14px system-ui; z-index:20; display:none;
    background:rgba(0,0,0,0.75); padding:8px 14px; border-radius:8px; }
</style></head>
<body>
<div id="stage">
  <div id="ytwrap"><div id="yt"></div></div>
  <canvas id="tmap" width="240" height="240"></canvas>
  <div class="hud" id="speedbox">
    <div id="spd">--<span class="u">MPH</span></div>
    <div id="rpmbar"><div id="rpmfill"></div></div>
    <div id="rpmtxt">-- RPM</div>
    <div id="gtxt"></div>
  </div>
  <div class="hud" id="lapbox">
    <div class="n" id="lapn">LAP –</div>
    <div class="t" id="lapt">--:--.-</div>
    <div class="s" id="lapl">LAST --:--.-</div>
    <div class="s" id="lapb">BEST --:--.-</div>
  </div>
  <div id="bar">
    <button id="pp">play</button>
    <button id="fs">fullscreen</button>
    <span class="synclbl" style="color:#888;font:600 12px system-ui">SYNC</span>
    <button data-n="-10">-10s</button>
    <button data-n="-1">-1s</button>
    <button data-n="-0.05">-.05</button>
    <span class="off" id="off">+0.00s</span>
    <button data-n="0.05">+.05</button>
    <button data-n="1">+1s</button>
    <button data-n="10">+10s</button>
    <input type="range" id="coarse" min="-300" max="300" step="0.1" value="0">
    <button id="launch" class="acc" title="Scrub the video to the moment the car starts moving, then click">SYNC @ LAUNCH</button>
    <button id="save" class="acc">SAVE</button>
    <a href="__BACK__" id="backlink">back</a>
  </div>
  <div id="msg"></div>
  <div id="toast"></div>
</div>
<script>
(function(){
  // API base: '/sessions/<user>/<file>' (authed) or '/shared/<token>' (public
  // view-only link — RO hides every control that could change anything).
  const API='__API__', RO=('__RO__'==='1');
  const el = id => document.getElementById(id);
  if (RO){
    ['launch','save','coarse','off','backlink'].forEach(id=>{ const x=el(id); if(x) x.style.display='none'; });
    document.querySelectorAll('#bar [data-n], #bar .synclbl').forEach(x=>x.style.display='none');
  }
  let S=[], T=[], laps=[], bestLapS=null, meta={}, offset=0, player=null, ready=false;
  let rpmMax=8000, bounds=null;

  function fmtLap(sec){
    if (!(sec>0)) return '--:--.-';
    const m=Math.floor(sec/60), r=sec-m*60;
    return m+':'+(r<10?'0':'')+r.toFixed(1);
  }
  function fmtOff(){ return (offset>=0?'+':'')+offset.toFixed(2)+'s'; }
  function toast(t){ const x=el('toast'); x.textContent=t; x.style.display='block';
    clearTimeout(x._t); x._t=setTimeout(()=>x.style.display='none', 2500); }

  async function boot(){
    let d, l, v;
    try {
      [d,l,v] = await Promise.all([
        fetch(API+'/data?target=20000').then(r=>r.json()),
        fetch(API+'/laps').then(r=>r.json()),
        fetch(API+'/video').then(r=>r.json()),
      ]);
    } catch(e){
      el('msg').style.display='block';
      el('msg').textContent='failed to load session data: '+e.message;
      return;
    }
    meta = v||{};
    if (!meta.id){
      el('msg').style.display='block';
      el('msg').innerHTML='No video linked to this session yet.<br>' +
        'Go back to the review page and paste a YouTube link.';
      return;
    }
    offset = (meta.offset_ms||0)/1000;
    el('off').textContent = fmtOff();
    el('coarse').value = Math.max(-300, Math.min(300, offset));
    S = d.samples||[]; bounds = d.bounds;
    laps = (l&&l.laps)||[];
    if (l&&l.best_lap){ const b=laps.find(x=>x.lap===l.best_lap); if(b) bestLapS=b.seconds; }
    // normalize timestamps -> rel seconds (epoch t, else t_ms, else 25 Hz synthetic)
    let t0=null;
    T = new Array(S.length);
    for (let i=0;i<S.length;i++){
      const s=S[i]; let v2=null;
      if (typeof s.t==='number' && s.t>946684800) v2=s.t;
      else if (typeof s.t_ms==='number') v2=s.t_ms/1000;
      if (t0===null && v2!==null) t0=v2;
      if (v2!==null && t0!==null) T[i]=v2-t0;
      else T[i]=i? T[i-1]+0.04 : 0;   // synthetic 25 Hz fallback
    }
    let mr=0; for (const s of S) if (s.rpm>mr) mr=s.rpm;
    rpmMax = Math.max(1000, Math.ceil(mr/1000)*1000);
    drawTrack();
    // YouTube IFrame API
    const tag=document.createElement('script');
    tag.src='https://www.youtube.com/iframe_api';
    document.head.appendChild(tag);
    window.onYouTubeIframeAPIReady = function(){
      player = new YT.Player('yt', {
        videoId: meta.id, width:'100%', height:'100%',
        playerVars:{rel:0, modestbranding:1, playsinline:1, controls:1},
        events:{ onReady: ()=>{ ready=true; }, onStateChange: st=>{
          el('pp').textContent = (st.data===1)?'pause':'play'; } }
      });
    };
  }

  // ---- track map canvas ------------------------------------------------
  let proj=null;
  function drawTrack(){
    const c=el('tmap'), ctx=c.getContext('2d');
    ctx.clearRect(0,0,c.width,c.height);
    const pts=[];
    for (const s of S) if (typeof s.lat==='number'&&typeof s.lon==='number'&&(s.lat||s.lon)) pts.push([s.lat,s.lon]);
    if (pts.length<10){ c.style.display='none'; return; }
    let mnLa=1e9,mxLa=-1e9,mnLo=1e9,mxLo=-1e9;
    for (const p of pts){ if(p[0]<mnLa)mnLa=p[0]; if(p[0]>mxLa)mxLa=p[0];
      if(p[1]<mnLo)mnLo=p[1]; if(p[1]>mxLo)mxLo=p[1]; }
    const cosl=Math.cos(mnLa*Math.PI/180);
    const w=(mxLo-mnLo)*cosl, h=(mxLa-mnLa);
    const sc=Math.min((c.width-24)/(w||1e-9),(c.height-24)/(h||1e-9));
    proj = (la,lo)=>[12+((lo-mnLo)*cosl)*sc, c.height-12-((la-mnLa))*sc];
    ctx.strokeStyle='rgba(255,176,32,0.85)'; ctx.lineWidth=2.5; ctx.beginPath();
    for (let i=0;i<pts.length;i++){ const q=proj(pts[i][0],pts[i][1]);
      if(i)ctx.lineTo(q[0],q[1]); else ctx.moveTo(q[0],q[1]); }
    ctx.stroke();
  }
  function drawDot(la,lo){
    if(!proj) return;
    drawTrack();
    const c=el('tmap'), ctx=c.getContext('2d'); const q=proj(la,lo);
    ctx.fillStyle='#fff'; ctx.strokeStyle='#000'; ctx.lineWidth=2;
    ctx.beginPath(); ctx.arc(q[0],q[1],6,0,7); ctx.fill(); ctx.stroke();
  }

  function idxAt(t){
    let lo=0, hi=T.length-1;
    if (!T.length || t<=T[0]) return 0;
    if (t>=T[hi]) return hi;
    while (hi-lo>1){ const m=(lo+hi)>>1; if (T[m]<=t) lo=m; else hi=m; }
    return lo;
  }

  // ---- HUD tick ----------------------------------------------------------
  setInterval(function(){
    if (!ready || !S.length || !player || !player.getCurrentTime) return;
    const vt = player.getCurrentTime()||0;
    const dt = vt + offset;
    const i = idxAt(dt);
    const s = S[i];
    const inRange = dt>=T[0]-2 && dt<=T[T.length-1]+2;
    el('spd').innerHTML = (inRange && typeof s.speed_mph==='number' ? Math.round(s.speed_mph) : '--')
                          + '<span class="u">MPH</span>';
    const rpm = (inRange && typeof s.rpm==='number') ? s.rpm : 0;
    el('rpmfill').style.width = Math.min(100, rpm*100/rpmMax)+'%';
    el('rpmtxt').textContent = (inRange&&rpm? rpm : '--')+' RPM';
    if (inRange && typeof s.ax==='number' && typeof s.ay==='number')
      el('gtxt').textContent = 'LAT '+Math.abs(s.ay).toFixed(2)+'g   LON '+Math.abs(s.ax).toFixed(2)+'g';
    if (inRange && typeof s.lat==='number' && (s.lat||s.lon)) drawDot(s.lat, s.lon);
    // laps
    let cur=null, last=null;
    for (const lp of laps){
      if (dt>=lp.t_start && dt<lp.t_end){ cur=lp; break; }
      if (dt>=lp.t_end) last=lp;
    }
    if (cur){
      el('lapn').textContent='LAP '+cur.lap;
      el('lapt').textContent=fmtLap(dt-cur.t_start);
    } else {
      el('lapn').textContent='LAP –';
      el('lapt').textContent='--:--.-';
    }
    el('lapl').textContent='LAST '+fmtLap(last?last.seconds:0);
    el('lapb').textContent='BEST '+fmtLap(bestLapS||0);
  }, 50);

  // ---- controls -----------------------------------------------------------
  function setOffset(v){
    offset=v;
    el('off').textContent=fmtOff();
    el('coarse').value=Math.max(-300,Math.min(300,offset));
  }
  document.querySelectorAll('#bar button[data-n]').forEach(b=>{
    b.addEventListener('click',()=>setOffset(offset+parseFloat(b.dataset.n)));
  });
  el('coarse').addEventListener('input',()=>setOffset(parseFloat(el('coarse').value)));
  el('pp').addEventListener('click',()=>{
    if (!player) return;
    (player.getPlayerState()===1)?player.pauseVideo():player.playVideo();
  });
  el('fs').addEventListener('click',()=>{
    const st=el('stage');
    if (document.fullscreenElement) document.exitFullscreen();
    else st.requestFullscreen && st.requestFullscreen();
  });
  el('launch').addEventListener('click',()=>{
    // Auto-sync helper: find the data's LAUNCH (first sustained >15 mph) and
    // pin it to the video's current position.
    if (!S.length || !player) return;
    let li=-1;
    for (let i=0;i<S.length-5;i++){
      if (S[i].speed_mph>15 && S[i+3]&&S[i+3].speed_mph>12 && S[i+5]&&S[i+5].speed_mph>12){ li=i; break; }
    }
    if (li<0){ toast('no launch found in data (never above 15 mph?)'); return; }
    setOffset(T[li] - (player.getCurrentTime()||0));
    toast('synced: data launch = this video moment. Fine-tune then SAVE.');
  });
  el('save').addEventListener('click',async ()=>{
    if (RO) return;
    try{
      const r=await fetch(API+'/video',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:meta.url||meta.id, offset_ms:Math.round(offset*1000)})});
      if(!r.ok) throw new Error('HTTP '+r.status);
      toast('sync saved');
    }catch(e){ toast('save failed: '+e.message); }
  });
  // auto-hide the control bar
  let hideT=null;
  function poke(){ el('bar').classList.remove('hidden');
    clearTimeout(hideT); hideT=setTimeout(()=>el('bar').classList.add('hidden'), 3000); }
  document.addEventListener('mousemove',poke);
  document.addEventListener('touchstart',poke);
  poke();

  boot();
})();
</script>
</body></html>"""
)

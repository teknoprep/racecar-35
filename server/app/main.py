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

# ---------------------------------------------------------------------------
# WHICH BUILD IS RUNNING (the admin page shows this next to its update button)
# ---------------------------------------------------------------------------
# The server has no semver of its own: it IS the repo commit it was built from
# (`git pull && docker compose up -d --build`), so the honest identity is that
# commit. Two sources, in order of trust:
#   1. RACECAR_BUILD_* — baked into the image at BUILD time (Dockerfile ARG,
#      fed by server/host_updater.sh exporting the values just before compose).
#      TRUTHFUL: it describes THIS image whatever happens to the checkout next.
#   2. the host checkout mounted read-only at /repo (compose `../:/repo:ro`),
#      parsed straight out of .git with no git binary. Needed because it also
#      works on the very FIRST deploy of this feature, when the updater script
#      running on the host is still the old one and exports nothing. It
#      describes the CHECKOUT, which can be NEWER than the image (someone
#      pulled without rebuilding), so `source` says which one won and
#      `deploy_pending` flags that case.
# Neither present is not an error — the version simply reads "unknown".
BUILD_SHA = (os.environ.get("RACECAR_BUILD_SHA") or "").strip()
BUILD_SUBJECT = (os.environ.get("RACECAR_BUILD_SUBJECT") or "").strip()
BUILD_TIME = (os.environ.get("RACECAR_BUILD_TIME") or "").strip()
REPO_MOUNT = pathlib.Path(os.environ.get("RACECAR_REPO_MOUNT", "/repo"))


def _repo_dotgit(repo: pathlib.Path) -> pathlib.Path:
    """The real .git dir for `repo` (honours a `.git` FILE, i.e. a worktree)."""
    d = repo / ".git"
    try:
        if d.is_file():
            txt = d.read_text("utf-8").strip()
            if txt.startswith("gitdir:"):
                p = pathlib.Path(txt.split(":", 1)[1].strip())
                return p if p.is_absolute() else (repo / p)
    except Exception:
        pass
    return d


def _git_head(repo: pathlib.Path) -> str:
    """HEAD commit sha out of a .git dir — ref file, packed-refs, or detached."""
    git = _repo_dotgit(repo)
    try:
        head = (git / "HEAD").read_text("utf-8").strip()
    except Exception:
        return ""
    if not head.startswith("ref:"):
        return head if re.fullmatch(r"[0-9a-f]{40}", head) else ""  # detached
    ref = head.split(":", 1)[1].strip()
    try:
        sha = (git / ref).read_text("utf-8").strip()
        if re.fullmatch(r"[0-9a-f]{40}", sha):
            return sha
    except Exception:
        pass
    try:                                    # packed refs, after a gc/fetch
        for line in (git / "packed-refs").read_text("utf-8").splitlines():
            line = line.strip()
            if not line or line[0] in "#^":
                continue
            sha, _, name = line.partition(" ")
            if name.strip() == ref and re.fullmatch(r"[0-9a-f]{40}", sha.strip()):
                return sha.strip()
    except Exception:
        pass
    return ""


def _git_subject(repo: pathlib.Path, sha: str) -> str:
    """First line of a commit message, decompressed straight from the object
    store (the image has no git binary and no network). Empty when the object
    is packed rather than loose — the sha alone is then still reported."""
    if not re.fullmatch(r"[0-9a-f]{40}", sha or ""):
        return ""
    try:
        raw = zlib.decompress(
            (_repo_dotgit(repo) / "objects" / sha[:2] / sha[2:]).read_bytes())
    except Exception:
        return ""
    try:
        msg = raw.split(b"\x00", 1)[1].split(b"\n\n", 1)[1]
        return msg.decode("utf-8", "replace").splitlines()[0].strip()
    except Exception:
        return ""


def server_version() -> dict:
    """Identity of the RUNNING server build (see the notes above BUILD_SHA)."""
    sha, subject, built, source = BUILD_SHA, BUILD_SUBJECT, BUILD_TIME, "image"
    repo_sha = _git_head(REPO_MOUNT)
    if not sha:
        sha, source = repo_sha, "repo"
        subject = _git_subject(REPO_MOUNT, repo_sha)
        built = ""
    pending = bool(source == "image" and repo_sha and repo_sha != sha)
    short = sha[:7]
    # The label sits in the admin header, so keep it short; the full subject
    # stays in `subject` (and the page's tooltip).
    label = subject if len(subject) <= 48 else subject[:47].rstrip() + "\u2026"
    display = " \u00b7 ".join(x for x in (short, label) if x) or "unknown"
    return {
        "ok": True,
        "sha": sha, "short": short, "subject": subject, "built": built,
        "source": source, "display": display,
        "repo_sha": repo_sha, "repo_short": repo_sha[:7],
        "deploy_pending": pending,
        "started": _PROC_START,
        "uptime_s": max(0, int(time.time()) - _PROC_START),
    }

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
    # synchronous + explicit, but OFF the event loop: the model call can take
    # RACECAR_AI_TIMEOUT_SECONDS and would freeze the whole server meanwhile
    await asyncio.to_thread(lambda: _coach_analyze(d, p, _track_key(p.name), force=True))
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
        # The RUNNING build — the admin page shows this next to the button and
        # re-reads it after the restart, which is how "what version are we on?"
        # gets answered without a second endpoint or a page reload.
        "version": server_version(),
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


_ENRICHING: set = set()
_ENRICH_LOCK = threading.Lock()


def _kick_enrich(asset: dict) -> None:
    """An asset baked before OSM features / lidar terrain existed gets them in
    the background (network, tens of seconds), once per slug per process; the
    viewer uses whatever is there now and picks the rest up next load.
    enrich_asset() itself backs off for 6 h after a failed fetch."""
    try:
        _tp = _trackprep()
        slug = str(asset.get("slug") or "")
        if not slug or int((asset.get("enrich") or {}).get("v") or 0) >= _tp.ENRICH_VERSION:
            return
        path = _track_asset_path(slug)
        if not path.is_file():
            return
        with _ENRICH_LOCK:
            if slug in _ENRICHING:
                return
            _ENRICHING.add(slug)
    except Exception:
        return

    def run() -> None:
        try:
            _tp.enrich_asset(path, DATA_DIR / "tilecache",
                             log=lambda m: log.info("enrich %s", m))
        except Exception as e:                     # never kills the server
            log.warning("enrich %s failed: %s", slug, e)
    threading.Thread(target=run, name=f"enrich-{slug}", daemon=True).start()


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
            # ...or one that predates the shipped seed's enrichment (OSM
            # features, lidar terrain, imagery-centred line)
            try:
                have = json.loads(dst.read_text("utf-8"))
                seed = json.loads(src.read_text("utf-8"))
                h_en = int((have.get("enrich") or {}).get("v") or 0)
                s_en = int((seed.get("enrich") or {}).get("v") or 0)
                current = int(have.get("prep_version") or 0) >= PREP_VERSION
                # a current local bake NEWER than the seed is kept even when it
                # is not enriched yet (Overpass down at bake time): the
                # background enrichment fills it in, the seed would lose it
                newer = int(have.get("generated") or 0) > int(seed.get("generated") or 0)
                if current and (h_en >= s_en or newer):
                    continue
                log.info("replacing stale seeded track %s (prep_version %s, enrich %s)",
                         dst.name, have.get("prep_version"), h_en)
            except Exception:
                continue
        try:
            # side files first, the JSON that points at them last
            tex = src.with_suffix(".jpg")
            if tex.is_file():
                shutil.copy2(tex, TRACKS_DIR / tex.name)
            for extra in (src.with_suffix(".dem.bin"), src.with_suffix(".demfar.bin"),
                          src.with_suffix(".ground.jpg")):
                if extra.is_file():
                    shutil.copy2(extra, TRACKS_DIR / extra.name)
            shutil.copy2(src, dst)
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
        if not asset.get("landcover") and asset.get("texture"):
            # Baked before land cover existed: classify its own texture (offline,
            # well under a second, once) so the 3D view plants trees only where
            # the imagery shows woods and shades paddocks/woods on the ground.
            try:
                healed = _trackprep().ensure_landcover(
                    _track_asset_path(chosen), log=lambda m: log.info("%s", m))
                if healed:
                    healed["slug"] = chosen
                    asset = healed
            except Exception as e:
                log.warning("land cover for %s failed: %s", chosen, e)
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
        good = [lp for lp in laps if float(lp.get("seconds") or 0) > 20
                and not lp.get("out_lap") and not lp.get("partial")]
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


@app.get("/trackassets/{slug}/ground.jpg")
async def trackasset_ground(request: Request, slug: str) -> FileResponse:
    """Imagery of the whole FACILITY (every layout, paddock, run-off), coarser
    than texture.jpg: the 3D view tints its ground with it."""
    require_web_user(request)
    p = TRACKS_DIR / (safe_name(slug, default="") + ".ground.jpg")
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no ground image")
    return FileResponse(p, media_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/trackassets/{slug}/dem/{which}")
async def trackasset_dem(request: Request, slug: str, which: str) -> FileResponse:
    """The asset's high-resolution terrain grids (uint16 LE, south row first;
    the asset's dem_hr / dem_far metadata says how to decode them)."""
    require_web_user(request)
    if which not in ("hr", "far"):
        raise HTTPException(status_code=404, detail="no such grid")
    p = _track_asset_path(slug)
    if not p.is_file():
        raise HTTPException(status_code=404, detail="no prepared asset for this track")
    try:
        meta = (json.loads(p.read_text("utf-8")).get("dem_hr" if which == "hr" else "dem_far")
                or {})
    except Exception:
        meta = {}
    name = safe_name(str(meta.get("file") or ""), default="")
    f = TRACKS_DIR / name
    if not name or not f.is_file():
        raise HTTPException(status_code=404, detail="no terrain grid")
    return FileResponse(f, media_type="application/octet-stream",
                        headers={"Cache-Control": "public, max-age=86400"})


@app.get("/sessions/{user}/{filename}/track-asset")
async def session_track_asset(request: Request, user: str, filename: str) -> JSONResponse:
    """The prepared track asset for this session's track, or 404 with the slug it
    WOULD use (so the viewer can offer to prepare it)."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    slug = _track_slug(_track_key(p.name))
    # off the event loop: the first request for an older asset classifies its
    # imagery (land cover) before answering
    asset = await asyncio.to_thread(_track_asset_for, _track_key(p.name))
    if asset:
        _kick_enrich(asset)
    if not asset:
        raise HTTPException(status_code=404, detail=json.dumps(
            {"missing": True, "track": _track_key(p.name), "slug": slug,
             "prepared": _available_slugs()[:40]}))
    return JSONResponse(asset)


@app.post("/sessions/{user}/{filename}/track-prep")
def session_track_prep(request: Request, user: str, filename: str,
                             force: int = Query(0)) -> JSONResponse:
    """Kick off a pre-render for this session's track (owner-or-admin).

    A plain `def` on purpose: FastAPI runs it on the thread pool. It reads the
    session and asks Overpass for the nearby raceways (several mirrors, long
    timeouts) - as an `async def` that froze the WHOLE server, dash uploads
    included, for as long as Overpass took to answer.

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
        st0 = _PREP.get(slug) or {}
        cur = st0.get("state")
        # "queued" = another request is still reading the session / asking
        # Overpass (this handler runs on the thread pool, so two can overlap);
        # a queued entry older than 5 min is a request that died, not a job
        if cur == "running" or (cur == "queued" and
                                time.time() - (st0.get("started") or 0) < 300):
            return JSONResponse({"ok": True, "slug": slug, "state": cur,
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
            # only a width: do not wait out every Overpass mirror's 90 s for it
            ways = tp.osm_raceways((lat0 - 0.06, lon0 - 0.08, lat0 + 0.06, lon0 + 0.08), timeout=25)
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
    if not st and await asyncio.to_thread(_track_asset_for, _track_key(p.name)):
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


# Bumps whenever the 3D drive view changes materially (documented at length in
# the /caps docstring below). Kept as a constant because /caps and /version both
# report it — two literals would drift.
TRACK3D_V = 13


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
    9 = simulated (procedural) track dressing: canvas-generated tarmac /
    grass / gravel / Armco textures, Armco barriers outside every corner, trees
    scattered from the terrain grid, corner labels (T1, T2...) in plan view, the
    driven line drawn on the tarmac, and the brake/accel shading as a separate
    TRANSLUCENT wash that gets brighter with harder braking. The ground mode is
    now a choice: simulated / satellite / none. No giant ground plane (its own
    edges were the faceted 'ceiling' on the horizon).
    8 = a bake that would produce wallpaper now FAILS instead of publishing:
    duplicate-tile detection (a blocked/proxied tile source answers every URL
    with the same image), an asset validator (texture must cover the track, be
    >= 512 px and finer than 6 m/px, and the line must look like a circuit not
    laps), the viewer clamps the texture so it can never tile, anisotropy for
    crisp ground, and seeds replace a STALE asset (so a broken baked track is
    healed by the shipped one).
    10 = trees are NEVER on (or over) any part of the circuit (they were offset
    along the tangent, i.e. onto the road), and only where the imagery's land
    cover says woods; a daylight sky + sun + shadows; terrain-following ground
    flattened under the road (it used to float over a flat disc on a hilly
    track) and shaded by land cover; kerbs, white edge lines, gravel traps,
    Armco that never crosses another section; a chevron driving line coloured
    by the driver's input; markers painted on the road, not poles in it.
    11/12 = see CLAUDE.md (the real place; the whole facility network).
    13 = the REAL track surface: every layout's edges traced in 2-D off the
    imagery (trackprep.trace_edges, network v2) instead of a squeezed
    near-constant width; straight centrelines; the road widened wherever the
    session's own fixes ran past an edge, so the driving line is on the tarmac
    (Summit Point: 65 % of fixes on the old road, 99 % now)."""
    v = server_version()
    return {"ok": True, "zblocks": True, "coach": True, "track3d": True,
            "track3d_v": TRACK3D_V,
            # Which build is RUNNING (the same identity the admin page shows
            # next to its update button, and what `curl <host>/version`
            # reports) — so one probe answers both "is this image new enough?"
            # and "which commit is it?". Extra keys: an old dash only looks for
            # its own capability flags here.
            "server": v["display"], "server_sha": v["short"],
            "server_source": v["source"], "deploy_pending": v["deploy_pending"]}



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


@app.get("/version")
async def version_info() -> dict:
    """PUBLIC: which build is RUNNING.

    The admin page's "update server" button shows exactly this next to itself
    (it is also carried by GET /admin/update/status, which that button already
    polls). Public and tiny so a deploy is verifiable with one curl:

        curl -s https://racecar.api.blueuc.com/version

    `sha`/`display` are the commit this IMAGE was built from when the host
    updater baked it in (`source: "image"`); otherwise they come from the host
    checkout mounted read-only at /repo (`source: "repo"`). `deploy_pending`
    is true when the checkout has moved past the image — i.e. someone pulled
    without rebuilding, so the button still has work to do.
    """
    v = server_version()
    v["track3d_v"] = TRACK3D_V
    v["service"] = SERVICE_NAME
    return v


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
    # Path 1 produced the boundaries: only then do the stamped values describe
    # them (a stream stamped lap 0 throughout - an S/F that was never crossed -
    # falls through to Path 2, whose laps are real)
    from_field = bool(crossings)

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

    # The Teensy stamps lap 0 from REC until the first S/F crossing: that
    # segment starts wherever recording began (the pits, mid-track), so it is an
    # out-lap, never a timed lap - and usually SHORTER than a real one, which
    # made it the session's "best" (Watkins Glen: a 1:39 out-lap over 1:46 laps).
    # Flagged rather than dropped so lap numbers - and the exclusions stored
    # against them - stay what they were. A combined file restarts at lap 0
    # partway through: that segment is an out-lap too, and the one BEFORE it
    # ends at the counter reset (end of the first file), not at a crossing.
    def _stamped(i):
        v = samples[i].get("lap")
        return v if isinstance(v, int) else None
    laps = []
    for k in range(len(crossings) - 1):
        i0, i1 = crossings[k], crossings[k + 1]
        secs = rel[i1] - rel[i0]
        max_mph = 0.0
        for j in range(i0, i1 + 1):
            mph = samples[j].get("speed_mph")
            if isinstance(mph, (int, float)) and mph > max_mph:
                max_mph = mph
        lp = {
            "lap": k + 1,
            "t_start": round(rel[i0], 3),
            "t_end": round(rel[i1], 3),
            "seconds": round(secs, 3),
            "ms": int(secs * 1000),
            "max_mph": round(max_mph, 1),
        }
        if from_field:
            a, b = _stamped(i0), _stamped(i1)
            if a == 0:
                lp["out_lap"] = True
            elif a is not None and b is not None and b < a:
                lp["partial"] = True
        laps.append(lp)

    best = None
    best_secs = float("inf")
    for lp in laps:
        if lp.get("out_lap") or lp.get("partial"):
            continue
        if lp["seconds"] < best_secs:
            best_secs = lp["seconds"]
            best = lp["lap"]
    return {
        "laps": laps,
        "best_lap": best,
        "sf": sf_info,
        "source": "teensy_lap_field" if from_field and laps else "line_crossing",
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
    # an out-lap / a lap cut by a file join is not a lap to compare
    laps = [lp for lp in laps_info.get("laps", [])
            if not lp.get("out_lap") and not lp.get("partial")]

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
        if ((float(lp.get("seconds", 0)) < LAP_AUTO_EXCLUDE_UNDER_S or lp.get("out_lap")
                or lp.get("partial")) and n not in incl):
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
    fixes: int = Query(0, ge=0, le=1),
) -> JSONResponse:
    """Parsed NDJSON for the review UI.

    `fixes=1` keeps one row per GPS FIX before thinning: the logger emits up to
    100 Hz and repeats the same 25 Hz fix (identical lat/lon/speed) on several
    rows, so a plain stride both keeps repeats and drops real fixes unevenly -
    which is what made speed-derived acceleration (the 3D view's brake/throttle
    colouring) read multiple g on real data.

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

    if fixes:
        return JSONResponse(await asyncio.to_thread(_session_fix_payload, p, target))
    return JSONResponse(_session_data_payload(p, stride, target))


_FIX_NUM = rb'(-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?)'
_FIX_LAT_RE = re.compile(rb'"lat"\s*:\s*' + _FIX_NUM)
_FIX_LON_RE = re.compile(rb'"lon"\s*:\s*' + _FIX_NUM)
_FIX_SPD_RE = re.compile(rb'"speed_mph"\s*:\s*' + _FIX_NUM)


def _fix_key(raw: bytes):
    """(lat, lon, speed|None) of a raw NDJSON row without parsing it, or None
    when the row carries no numeric fix."""
    m1, m2 = _FIX_LAT_RE.search(raw), _FIX_LON_RE.search(raw)
    if not m1 or not m2:
        return None
    try:
        lat, lon = float(m1.group(1)), float(m2.group(1))
        m3 = _FIX_SPD_RE.search(raw)
        spd = float(m3.group(1)) if m3 else None
    except ValueError:
        return None
    return (lat, lon, spd)


def _session_fix_payload(p: pathlib.Path, target: int) -> dict:
    """/data?fixes=1: every row whose (lat, lon, speed_mph) differs from the
    previous KEPT fix, then an even stride down to `target`. The first
    appearance of a fix is kept (the repeats carry later emit times). Same rule
    as the viewer's RC3D.cleanFixes: a repeat needs a numeric speed, and rows
    without a fix are kept and never become the reference.

    Two passes so a 200 MB session never sits in memory: pass 1 keeps only the
    byte offsets of the rows to keep (the key comes out of the raw bytes by
    regex), pass 2 parses just the strided rows."""
    offsets: list[int] = []
    last = None
    pos = 0
    with open(p, "rb") as f:
        for raw in f:
            here = pos
            pos += len(raw)
            line = raw.strip()
            if not line or line[:1] != b"{":
                continue
            key = _fix_key(line)
            if key is not None:
                if (last is not None and key[2] is not None and
                        key[0] == last[0] and key[1] == last[1] and key[2] == last[2]):
                    continue
                last = key
            offsets.append(here)
        total = len(offsets)
        eff = max(1, math.ceil(total / target)) if target > 0 and total > target else 1
        rows: list[dict] = []
        for off in offsets[::eff]:
            f.seek(off)
            try:
                obj = json.loads(f.readline())
            except Exception:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    samples = rows
    lats = [r["lat"] for r in samples if isinstance(r.get("lat"), (int, float))
            and isinstance(r.get("lon"), (int, float)) and (r["lat"] or r["lon"])
            and -90 <= r["lat"] <= 90 and -180 <= r["lon"] <= 180]
    lons = [r["lon"] for r in samples if isinstance(r.get("lat"), (int, float))
            and isinstance(r.get("lon"), (int, float)) and (r["lat"] or r["lon"])
            and -90 <= r["lat"] <= 90 and -180 <= r["lon"] <= 180]
    bounds = [[min(lats), min(lons)], [max(lats), max(lons)]] if lats else None
    return {"count": len(samples), "total": total, "stride": eff,
            "bounds": bounds, "samples": samples, "fixes": True}


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
        short = float(lp.get("seconds", 0)) < LAP_AUTO_EXCLUDE_UNDER_S
        auto = (short or bool(lp.get("out_lap")) or bool(lp.get("partial"))) and (n not in incl)
        if n in excl or auto:
            lp = dict(lp)
            lp["excluded_reason"] = ("manual" if n in excl else
                                     "auto (<10s)" if short else
                                     "out lap" if lp.get("out_lap") else "partial")
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
_LAP_RULES_V = 2                     # bump when lap detection/exclusion rules change
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
            if (d.get("mtime") == int(st.st_mtime) and d.get("size") == int(st.st_size)
                    and d.get("v") == _LAP_RULES_V):
                with _LAP_SUMMARY_LOCK:
                    _LAP_SUMMARY_MEM[key] = d
                return d
    except Exception:
        pass
    return None


def _lap_summary_invalidate(user: str, p: pathlib.Path) -> None:
    """Drop the cached best lap of one session (its lap exclusions changed:
    the file's mtime/size did not, so the cache key cannot notice)."""
    with _LAP_SUMMARY_LOCK:
        for k in [k for k in _LAP_SUMMARY_MEM if k[0] == str(p)]:
            _LAP_SUMMARY_MEM.pop(k, None)
    try:
        (LAPCACHE_DIR / safe_name(user) / (p.name + ".json")).unlink()
    except OSError:
        pass


def _lap_summary(user: str, p: pathlib.Path, force: bool = False) -> dict:
    """Compute (+ cache) the best lap of one session. Blocking: call it from a
    worker thread, never from the event loop."""
    if not force:
        hit = _lap_summary_cached(user, p)
        if hit:
            return hit
    st = p.stat()
    summary = {"mtime": int(st.st_mtime), "size": int(st.st_size), "v": _LAP_RULES_V,
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
    _lap_summary_invalidate(u, p)
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

    # every slow step (the session read, the cross-session lap library, the
    # model's answer - up to RACECAR_AI_TIMEOUT_SECONDS) runs OFF the event
    # loop: on it, one question froze the whole server, dash uploads included
    def _metrics_from_file() -> dict:
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
        return _region_metrics(samples, poly)
    metrics = await asyncio.to_thread(_metrics_from_file)
    if not metrics.get("laps"):
        raise HTTPException(status_code=422,
                            detail="no lap data fell inside the selected region")
    # Cross-session references (other days, same track, same region) — on by
    # default; body {"refs": false} skips the extra session scans.
    lib = None
    if body.get("refs", True):
        try:
            lib = await asyncio.to_thread(_lap_library, safe_name(user), p, poly)
        except Exception as e:
            log.warning("lap library failed for %s/%s: %s", user, filename, e)
    question = str(body.get("prompt", "")).strip()
    messages = _region_prompt(metrics, question, lib=lib)
    answer, used_model, usage = await asyncio.to_thread(
        lambda: _ai_chat(messages, model=body.get("model")))

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
    lib = await asyncio.to_thread(_lap_library, safe_name(user), p, poly)   # off the event loop
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
    lib = await asyncio.to_thread(_lap_library, safe_name(user), p, poly)   # off the event loop
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
    answer, used_model, usage = await asyncio.to_thread(       # off the event loop
        lambda: _ai_chat([{"role": "system", "content": system}, {"role": "user", "content": userq}],
                         model=body.get("model")))
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
  <span class="t-label" id="srvupdmsg" style="margin-right:var(--sp-md)" title="">&#8230;</span>
  <script>
  (function(){
    var b=document.getElementById('srvupd'), m=document.getElementById('srvupdmsg');
    if(!b) return;
    var poll=null, t0=0, clickedAt=0, doneSeenAt=0, DEADLINE=3600000;
    var NOW_CMD='__HINT_NOW__', INST_CMD='__HINT_INSTALL__';
    function fmt(s){ return s||''; }
    function age(s){ return (s===null||s===undefined) ? '' : ' (' + s + 's ago)'; }
    // --- the RUNNING build: the answer to "what version are we on?" -----------
    // `j.version` comes from the server (see server_version() in main.py): the
    // commit THIS image was built from, or the host checkout when the image
    // carries no build stamp. `deploy_pending` = the checkout has moved on
    // since the image was built (someone pulled without rebuilding).
    function ver(j){ var v=(j&&j.version)||{}; return v.display||v.short||'unknown'; }
    function verTip(j){
      var v=(j&&j.version)||{}, t=[];
      t.push('running build: '+(v.display||'unknown'));
      if(v.sha) t.push('commit: '+v.sha);
      if(v.source) t.push('source: '+(v.source==='image'
        ? 'baked into this image at build time'
        : 'host checkout (.git) \u2014 this image has no build stamp'));
      if(v.subject) t.push('message: '+v.subject);
      if(v.built) t.push('built: '+v.built);
      if(v.deploy_pending) t.push('the checkout is on '+(v.repo_short||'?')+' \u2014 press "update server" to deploy it');
      if(typeof v.uptime_s==='number') t.push('this process has been up '+v.uptime_s+'s');
      var st=(j&&j.status)||{};
      if(st.state) t.push('host watcher: '+st.state+(st.detail?(' \u2014 '+st.detail):'')+age(j.status_age_s));
      else if(j && !j.watcher_ever) t.push('host watcher has never reported \u2014 on the server host run: '+INST_CMD);
      return t.join('\\n');
    }
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
        // While OUR update runs, only status the host wrote AFTER the click
        // counts: until it reports, the file still holds the PREVIOUS run's
        // result, and a stale 'done'/'failed' there used to end the poll on
        // the first tick. Both sides are durations on their own clocks, so
        // there is no clock-skew problem in comparing them.
        if(poll && !(typeof j.status_age_s==='number' && j.status_age_s<=elapsed)) st='';
        if(j.running_since && t0 && j.running_since>t0){
          // The PROCESS changed -> the rebuild landed. Report the version it is
          // running now, which is exactly what the button exists to answer.
          m.style.color='#2e7d32'; m.title=verTip(j);
          m.textContent='v '+ver(j)+' \u2713 updated';
          b.disabled=false; clearInterval(poll); poll=null; return;
        }
        if(poll && st==='done' && !pend){
          // The host said done, but this is still the OLD process: the new
          // container is starting. The running_since jump above is the only
          // proof it landed, so keep polling for it \u2014 for 90 s, after which
          // the idle branch below reports whatever is running.
          if(!doneSeenAt) doneSeenAt=Date.now();
          if(Date.now()-doneSeenAt<90000){
            m.style.color='#2e7d32'; m.title=verTip(j);
            m.textContent='updated \u2713 host reported done, restarting\u2026';
            return;
          }
        }
        if(poll && st==='failed'){
          m.style.color='#c62828'; m.title=verTip(j);
          m.textContent='update FAILED: '+fmt(j.status&&j.status.detail);
          b.disabled=false; clearInterval(poll); poll=null; return;
        }
        // Only an update in progress (ours, or a request already pending on
        // page load) can be 'stuck'; otherwise the label stays the version.
        if((poll || pend) && stuck(j, elapsed)){
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
        if(pend || st==='pulling' || st==='building' || (poll && !st)){
          // In flight: the host's own words, not the version. (poll && !st) =
          // the request was consumed but the host has not reported yet.
          m.style.color=''; m.title=verTip(j);
          m.textContent = pend ? 'queued\u2026 waiting for host watcher'
                        : st ? ('host: '+fmt(st)+age(j.status_age_s))
                        : 'host picked it up\u2026';
          return;
        }
        // Idle: the label IS the version. A finished update stops polling here.
        // An old failure is a red suffix, not a replacement (details on hover).
        if(poll){ b.disabled=false; clearInterval(poll); poll=null; }
        var bad = (st==='failed');
        m.style.color = bad ? '#c62828' : ''; m.title=verTip(j);
        m.textContent = 'v ' + ver(j)
          + (j.version && j.version.deploy_pending ? ' \u2014 checkout is newer, press update'
             : bad ? ' \u2014 last update FAILED (hover for details)' : '');
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
        clickedAt=Date.now(); doneSeenAt=0;
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
    background:rgba(14,16,20,0.62); border:1px solid var(--line); border-radius:8px; padding:8px 12px;
    max-width:min(470px, calc(100vw - 48px)); box-sizing:border-box; }
  #legend #lg-track, #legend #lg-src, #legend #lg-corner { white-space:pre-line; line-height:1.35; }
  #legend #lg-src { color:#AEB6C2; }
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
  <div class="li"><span class="sw" style="background:#5CE07F"></span>on the throttle</div>
  <div class="li"><span class="sw" style="background:#FBC72E"></span>coasting (off throttle, off brake)</div>
  <div class="li"><span class="sw" style="background:#FF4D4D"></span>braking (brighter = harder)</div>
  <div class="li" id="lg-ideal" style="display:none"><span class="sw" style="background:#3FD8FF"></span>ideal line (fastest real lap)</div>
  <div class="li"><span class="sw" style="background:#FF3B30"></span>BRAKE bar: where you braked (+m = later than your best lap)</div>
  <div class="li"><span class="sw" style="background:repeating-linear-gradient(90deg,#fff 0 6px,transparent 6px 10px)"></span>your best lap's brake point</div>
  <div class="li"><span class="dot" style="background:#FFB020"></span>MIN: slowest point \u00b7 <span class="dot" style="background:#34D058;margin-left:6px"></span>GAS: back on the throttle</div>
  <div class="li" id="lg-corner" style="display:none"></div>
  <div class="li" id="lg-track" style="display:none"></div>
  <div class="li" id="lg-src" style="display:none"></div>
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
  <label id="b-ground-lab" title="ground surface: simulated asphalt/grass/kerbs drawn procedurally, the prepared satellite imagery, or nothing"><span class="meta">ground</span>
    <select id="b-groundsel"><option value="sim">simulated</option><option value="satellite">satellite</option><option value="none">none</option></select></label>
  <button id="b-prep" style="display:none" type="button" title="pre-render this track from satellite imagery + OpenStreetMap on the server (once per track)">prepare track</button>
  <label title="road colour = your inputs, not your speed: green accelerating, grey neither, red braking (deeper with the g)"><input type="checkbox" id="b-speedcol" checked>accel / brake</label>
  <label title="render scale: higher supersamples the view, which is what removes jagged/crawling edges. Pick 1x if the GPU struggles"><span class="meta">sharp</span>
    <select id="b-scale"><option value="1">1×</option><option value="1.5">1.5×</option><option value="2">2×</option></select></label>
  <label><input type="checkbox" id="b-markers" checked>markers</label>
  <label title="numbered brake boards (5 4 3 2 1 = hundreds of metres) before the corners that need them — tight corners get the full ladder, gentle bends get none"><input type="checkbox" id="b-brakes" checked>brake boards</label>
  <label title="every other layout of the facility (other circuits, links, pit lanes, kart track) from OpenStreetMap + the imagery - shown, never driven on"><input type="checkbox" id="b-net" checked>all layouts</label>
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

  // ---- driver input from real logger data ------------------------------
  // The logger often writes the SAME GPS fix twice (a repeat row ~40 ms after
  // the fix, then the next fix 1 ms later). Speed differentiated across that
  // 1 ms step reads as several g, and the repeated position makes time ->
  // distance stutter. A row is a repeat when lat, lon AND speed_mph all equal
  // the previous KEPT row's (exact, all finite). Rows without a numeric fix are
  // kept untouched. Returns the kept indices (cleanFixes maps them to rows).
  function _cleanIdx(samples) {
    var keep = [], last = null, i, s;
    for (i = 0; i < samples.length; i++) {
      s = samples[i];
      if (!s || typeof s.lat !== "number" || typeof s.lon !== "number" ||
          !isFinite(s.lat) || !isFinite(s.lon)) { keep.push(i); continue; }
      if (last && s.lat === last.lat && s.lon === last.lon &&
          typeof s.speed_mph === "number" && isFinite(s.speed_mph) &&
          s.speed_mph === last.speed_mph) continue;
      keep.push(i);
      last = s;
    }
    return keep;
  }
  RC3D.cleanFixes = function (samples) {
    return _cleanIdx(samples).map(function (k) { return samples[k]; });
  };

  function _num(v) { return typeof v === "number" && isFinite(v); }

  // Solve a small dense linear system (Gaussian elimination, partial pivot).
  // null when singular.
  function _solve(A, b) {
    var n = b.length, i, j, k, M = [];
    for (i = 0; i < n; i++) M.push(A[i].slice().concat([b[i]]));
    for (k = 0; k < n; k++) {
      var p = k;
      for (i = k + 1; i < n; i++) if (Math.abs(M[i][k]) > Math.abs(M[p][k])) p = i;
      if (Math.abs(M[p][k]) < 1e-12) return null;
      var tmp = M[k]; M[k] = M[p]; M[p] = tmp;
      for (i = k + 1; i < n; i++) {
        var f = M[i][k] / M[k][k];
        for (j = k; j <= n; j++) M[i][j] -= f * M[k][j];
      }
    }
    var x = new Array(n);
    for (i = n - 1; i >= 0; i--) {
      var s = M[i][n];
      for (j = i + 1; j < n; j++) s -= M[i][j] * x[j];
      x[i] = s / M[i][i];
    }
    return x;
  }

  // Longitudinal g per sample. GPS: the slope of speed (m/s) by a local LINEAR
  // REGRESSION over +/-0.25 s of time (>= 3 points, widened to the nearest
  // neighbours when sparse) - a regression in TIME is what makes it robust to
  // the logger's irregular dt. IMU: the mounting is unknown, so (ax, ay, az, 1)
  // is least-squares fitted to the GPS g on the moving samples; when the fit is
  // good (Pearson r >= 0.85 - real sessions read 0.91-0.95, a junk IMU ~0)
  // the answer is the mean of the two (the IMU has no GPS lag/quantisation).
  // Returns a Float64Array carrying .source ("gps" | "gps+imu") and .imuR.
  RC3D.longG = function (t, mph, samples, opt) {
    opt = opt || {};
    var n = t.length, W = opt.win == null ? 0.25 : opt.win;
    var g = new Float64Array(n), v = new Float64Array(n), i, j, lo = 0, hi = 0;
    for (i = 0; i < n; i++) v[i] = (_num(mph[i]) ? mph[i] : 0) * 0.44704;
    for (i = 0; i < n; i++) {
      while (lo < i && t[lo] < t[i] - W) lo++;
      if (hi < i) hi = i;
      while (hi + 1 < n && t[hi + 1] <= t[i] + W) hi++;
      var a = lo, b = hi;
      while (b - a + 1 < 3) {                      // too sparse: nearest neighbours
        var ca = a > 0 ? t[i] - t[a - 1] : Infinity;
        var cb = b < n - 1 ? t[b + 1] - t[i] : Infinity;
        if (ca === Infinity && cb === Infinity) break;
        if (ca <= cb) a--; else b++;
      }
      if (b - a + 1 < 3 || !(t[b] - t[a] > 0) || t[b] - t[a] > 2.0) { g[i] = 0; continue; }
      var st = 0, sv = 0, m = b - a + 1;
      for (j = a; j <= b; j++) { st += t[j] - t[i]; sv += v[j]; }
      var tm = st / m, vm = sv / m, num = 0, den = 0;
      for (j = a; j <= b; j++) {
        var dt = t[j] - t[i] - tm;
        num += dt * (v[j] - vm); den += dt * dt;
      }
      g[i] = den > 0 ? (num / den) / 9.80665 : 0;
    }
    g.source = "gps"; g.imuR = null;
    if (!samples || samples.length !== n || n < 50) return g;
    // ---- IMU fusion, gated on the fit quality ----
    var have = 0;
    for (i = 0; i < n; i++) {
      var s0 = samples[i];
      if (s0 && _num(s0.ax) && _num(s0.ay) && _num(s0.az)) have++;
    }
    if (have < 0.6 * n) return g;
    var Wi = opt.imuWin == null ? 0.12 : opt.imuWin;
    var ok = new Uint8Array(n), raw = [new Float64Array(n), new Float64Array(n), new Float64Array(n)];
    var keys = ["ax", "ay", "az"], k;
    for (i = 0; i < n; i++) {
      var s1 = samples[i];
      if (s1 && _num(s1.ax) && _num(s1.ay) && _num(s1.az)) {
        ok[i] = 1; raw[0][i] = s1.ax; raw[1][i] = s1.ay; raw[2][i] = s1.az;
      }
    }
    // time-window moving average of the IMU (prefix sums over valid rows)
    var P = [new Float64Array(n + 1), new Float64Array(n + 1), new Float64Array(n + 1)];
    var C = new Float64Array(n + 1);
    for (i = 0; i < n; i++) {
      C[i + 1] = C[i] + ok[i];
      for (k = 0; k < 3; k++) P[k][i + 1] = P[k][i] + (ok[i] ? raw[k][i] : 0);
    }
    var imu = [new Float64Array(n), new Float64Array(n), new Float64Array(n)];
    var iok = new Uint8Array(n);
    lo = 0; hi = 0;
    for (i = 0; i < n; i++) {
      while (lo < i && t[lo] < t[i] - Wi) lo++;
      if (hi < i) hi = i;
      while (hi + 1 < n && t[hi + 1] <= t[i] + Wi) hi++;
      var cnt = C[hi + 1] - C[lo];
      if (!ok[i] || cnt < 1) continue;
      iok[i] = 1;
      for (k = 0; k < 3; k++) imu[k][i] = (P[k][hi + 1] - P[k][lo]) / cnt;
    }
    var A = [[0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0], [0, 0, 0, 0]], B = [0, 0, 0, 0];
    var rows = 0, row = [0, 0, 0, 1], r2, c2;
    for (i = 0; i < n; i++) {
      if (!iok[i] || !(v[i] > 15 * 0.44704) || !(Math.abs(g[i]) < 1.5)) continue;
      row[0] = imu[0][i]; row[1] = imu[1][i]; row[2] = imu[2][i];
      for (r2 = 0; r2 < 4; r2++) {
        B[r2] += row[r2] * g[i];
        for (c2 = 0; c2 < 4; c2++) A[r2][c2] += row[r2] * row[c2];
      }
      rows++;
    }
    if (rows < (opt.minFitRows || 250)) return g;
    var cf = _solve(A, B);
    if (!cf) return g;
    var sp = 0, sy = 0, spp = 0, syy = 0, spy = 0;
    for (i = 0; i < n; i++) {
      if (!iok[i] || !(v[i] > 15 * 0.44704) || !(Math.abs(g[i]) < 1.5)) continue;
      var pr = cf[0] * imu[0][i] + cf[1] * imu[1][i] + cf[2] * imu[2][i] + cf[3];
      sp += pr; sy += g[i]; spp += pr * pr; syy += g[i] * g[i]; spy += pr * g[i];
    }
    var cov = spy - sp * sy / rows, vp = spp - sp * sp / rows, vy = syy - sy * sy / rows;
    var r = (vp > 0 && vy > 0) ? cov / Math.sqrt(vp * vy) : 0;
    g.imuR = r;
    if (!(r >= (opt.minR == null ? 0.85 : opt.minR))) return g;
    var out = new Float64Array(n);
    for (i = 0; i < n; i++) {
      out[i] = iok[i]
        ? 0.5 * (g[i] + cf[0] * imu[0][i] + cf[1] * imu[1][i] + cf[2] * imu[2][i] + cf[3])
        : g[i];
    }
    out.source = "gps+imu"; out.imuR = r; out.imuCoef = cf;
    return out;
  };

  // What a light car (Miata) does with NO input at a given speed: rolling +
  // engine braking (~0.045 g) plus aero drag growing with v^2 (~0.14 g in
  // total at 100 mph). Throttle and brake are judged RELATIVE to this - a fixed
  // "g < 0 = braking" calls every coast at speed a brake and full throttle
  // near top speed (g ~ 0) nothing at all.
  RC3D.coastG = function (mph) {
    var k = (mph || 0) / 100;
    return -(0.045 + 0.095 * k * k);
  };
  RC3D.INPUT = {
    thrIn: 0.06, thrOut: 0.02, brkIn: 0.16, brkOut: 0.07, minMph: 12,
    brakeMin: 0.25, shiftMax: 0.45, throttleMin: 0.3, shiftRpmMax: 1.2, shiftRpmDrop: 0.12,
    thrFull: 0.35, brkFull: 0.9, floor: 0.15
  };
  // Per sample: state 1 = throttle, 0 = coast, -1 = brake, level 0..1.
  // Hysteresis around the coast curve, then time-based cleanup: a brake blip
  // under 0.25 s is coast, a coast gap under 0.45 s between throttle is a gear
  // change (throttle), a throttle blip under 0.3 s is coast.
  RC3D.inputStates = function (path, opt) {
    var K = {}, key;
    for (key in RC3D.INPUT) K[key] = RC3D.INPUT[key];
    if (opt) for (key in opt) K[key] = opt[key];
    var a = path.accel || [], sp = path.speed || [], t = path.t || [];
    var n = t.length, state = new Int8Array(n), level = new Float32Array(n), i, st = 0;
    var rpm = (path.rpm && path.rpm.length === n) ? path.rpm : null;
    for (i = 0; i < n; i++) {
      var g = a[i] || 0, c = RC3D.coastG(sp[i]);
      if (st === 1 && g < c + K.thrOut) st = 0;
      else if (st === -1 && g > c - K.brkOut) st = 0;
      if (st === 0) {
        if (g < c - K.brkIn) st = -1;
        else if (g > c + K.thrIn) st = 1;
      }
      if (st === 1 && !(sp[i] >= K.minMph)) st = 0;
      state[i] = st;
    }
    function runs() {
      var out = [], s0 = 0;
      for (var j = 1; j <= n; j++) {
        if (j === n || state[j] !== state[s0]) {
          var tEnd = j < n ? t[j] : t[n - 1];
          out.push({ a: s0, b: j - 1, v: state[s0], dur: tEnd - t[s0] });
          s0 = j;
        }
      }
      return out;
    }
    function fill(r, v) { for (var j = r.a; j <= r.b; j++) state[j] = v; }
    if (n) {
      var R = runs(), q;
      for (q = 0; q < R.length; q++) if (R[q].v === -1 && R[q].dur < K.brakeMin) fill(R[q], 0);
      R = runs();
      for (q = 1; q < R.length - 1; q++) {
        if (R[q].v !== 0 || R[q - 1].v !== 1 || R[q + 1].v !== 1) continue;
        if (R[q].dur < K.shiftMax) { fill(R[q], 1); continue; }
        // an upshift seen in the logged rpm: the regression window smears the
        // dip of a real (~0.4 s) shift to ~0.6-0.9 s, so rpm decides those
        if (rpm && R[q].dur < K.shiftRpmMax) {
          var rb = rpm[Math.max(0, R[q].a - 2)], ra = rpm[Math.min(n - 1, R[q].b + 2)];
          if (rb > 1500 && ra > 0 && ra < rb * (1 - K.shiftRpmDrop)) fill(R[q], 1);
        }
      }
      R = runs();
      for (q = 0; q < R.length; q++) {
        if (R[q].v !== 1 || R[q].dur >= K.throttleMin) continue;
        if ((q === 0 || R[q - 1].v !== 1) && (q === R.length - 1 || R[q + 1].v !== 1)) fill(R[q], 0);
      }
    }
    for (i = 0; i < n; i++) {
      var g2 = a[i] || 0, c2 = RC3D.coastG(sp[i]);
      if (state[i] === 1) level[i] = Math.max(K.floor, Math.min(1, (g2 - c2) / K.thrFull));
      else if (state[i] === -1) level[i] = Math.max(K.floor, Math.min(1, (c2 - g2) / K.brkFull));
    }
    return { state: state, level: level };
  };
  // Road colour from the classified input: throttle GREEN (brighter with
  // level), brake RED (deeper/brighter with level), coast AMBER (constant).
  RC3D.COAST_AMBER = [0.98, 0.78, 0.18];
  RC3D.inputColour = function (state, level) {
    var l = Math.max(0, Math.min(1, level || 0)), k;
    if (state > 0) { k = 0.45 + 0.55 * l; return [0.10 * k, k, 0.18 * k]; }
    if (state < 0) { k = 0.5 + 0.5 * l; return [k, 0.08 * k, 0.10 * k]; }
    return RC3D.COAST_AMBER.slice();
  };

  // ---- where the car REALLY was: Kalman filter + RTS smoother -------------
  // A moving average of 25 Hz GPS wobbles (0.3-1 m fix noise) and cuts
  // corners (it averages points on an arc, so it sits inside it). The u-blox
  // Doppler speed and course over ground are far better than the fixes, so the
  // state [x, z, vx, vz] (constant velocity, white-acceleration PSD q) takes
  // the fixes (sigma posSigma) AND the velocity vector (speed + heading, only
  // above minSpeed, anisotropic: along-track alongSigma, cross-track
  // v*headingSigma) and is smoothed forward + backward (Rauch-Tung-Striebel),
  // which removes the filter's lag. Slow/parked rows get a weak zero-velocity
  // prior (heading is garbage there). dt <= 0 is clamped; a gap > opt.gap
  // seconds is NOT propagated across - the covariance is blown up so each side
  // stands alone. Plain 4x4 arithmetic into typed arrays: 40k rows in ~ms.
  // Returns {x, z (Float64Array, local metres), nVel, nPos, ok} or null when
  // there are fewer than 20 fixes.
  function _kfPredict(P, dt, q) {
    // P <- F P F^T + Q, F = [[I, dt I], [0, I]]
    var r, c;
    for (c = 0; c < 4; c++) { P[c] += dt * P[8 + c]; P[4 + c] += dt * P[12 + c]; }
    for (r = 0; r < 4; r++) { P[r * 4] += dt * P[r * 4 + 2]; P[r * 4 + 1] += dt * P[r * 4 + 3]; }
    var q3 = q * dt * dt * dt / 3, q2 = q * dt * dt / 2, q1 = q * dt;
    P[0] += q3; P[5] += q3; P[10] += q1; P[15] += q1;
    P[2] += q2; P[8] += q2; P[7] += q2; P[13] += q2;
  }
  function _kfUpdate(xs, P, i0, i1, y0, y1, r00, r01, r11, tmp) {
    var s00 = P[i0 * 5] + r00, s01 = P[i0 * 4 + i1] + r01, s11 = P[i1 * 5] + r11;
    var det = s00 * s11 - s01 * s01, r, c;
    if (!(det > 1e-12)) return false;
    var a00 = s11 / det, a01 = -s01 / det, a11 = s00 / det;
    y0 -= xs[i0]; y1 -= xs[i1];
    for (c = 0; c < 4; c++) { tmp[c] = P[i0 * 4 + c]; tmp[4 + c] = P[i1 * 4 + c]; }
    for (r = 0; r < 4; r++) {
      var k0 = P[r * 4 + i0] * a00 + P[r * 4 + i1] * a01;
      var k1 = P[r * 4 + i0] * a01 + P[r * 4 + i1] * a11;
      xs[r] += k0 * y0 + k1 * y1;
      for (c = 0; c < 4; c++) P[r * 4 + c] -= k0 * tmp[c] + k1 * tmp[4 + c];
    }
    for (r = 0; r < 4; r++) for (c = r + 1; c < 4; c++) {
      var m = (P[r * 4 + c] + P[c * 4 + r]) / 2;
      P[r * 4 + c] = m; P[c * 4 + r] = m;
    }
    return true;
  }
  // Solve A y = b for a symmetric positive-definite 4x4 (Cholesky, in place:
  // A is destroyed, b becomes y). false when not positive-definite.
  function _chol4(A, b) {
    var i, j, k, s;
    for (j = 0; j < 4; j++) {
      s = A[j * 5];
      for (k = 0; k < j; k++) s -= A[j * 4 + k] * A[j * 4 + k];
      if (!(s > 1e-18)) return false;
      A[j * 5] = Math.sqrt(s);
      for (i = j + 1; i < 4; i++) {
        s = A[i * 4 + j];
        for (k = 0; k < j; k++) s -= A[i * 4 + k] * A[j * 4 + k];
        A[i * 4 + j] = s / A[j * 5];
      }
    }
    for (i = 0; i < 4; i++) {
      s = b[i];
      for (k = 0; k < i; k++) s -= A[i * 4 + k] * b[k];
      b[i] = s / A[i * 5];
    }
    for (i = 3; i >= 0; i--) {
      s = b[i];
      for (k = i + 1; k < 4; k++) s -= A[k * 4 + i] * b[k];
      b[i] = s / A[i * 5];
    }
    return true;
  }
  RC3D.kalmanPath = function (samples, o, opt) {
    opt = opt || {};
    var n = samples.length, i, k, c, s;
    var q = opt.q == null ? 10 : opt.q;
    var ps = opt.posSigma == null ? 1.0 : opt.posSigma, R0 = ps * ps;
    var sa = opt.alongSigma == null ? 0.3 : opt.alongSigma;
    var sh = (opt.headingSigmaDeg == null ? 1.5 : opt.headingSigmaDeg) * Math.PI / 180;
    var vMin = opt.minSpeed == null ? 4 : opt.minSpeed;
    var gapS = opt.gap == null ? 1.0 : opt.gap;
    var DT_MIN = 1e-3;
    var mx = new Float64Array(n), mz = new Float64Array(n), hasP = new Uint8Array(n);
    var vx = new Float64Array(n), vz = new Float64Array(n);
    var w00 = new Float64Array(n), w01 = new Float64Array(n), w11 = new Float64Array(n);
    var hasV = new Uint8Array(n), nPos = 0, nVel = 0, first = -1;
    if (!o) {
      var sla = 0, slo = 0, na = 0;
      for (i = 0; i < n; i++) {
        s = samples[i];
        if (s && _num(s.lat) && _num(s.lon) && (s.lat || s.lon)) { sla += s.lat; slo += s.lon; na++; }
      }
      o = na ? { lat: sla / na, lon: slo / na } : { lat: 0, lon: 0 };
    }
    for (i = 0; i < n; i++) {
      s = samples[i];
      if (!s) continue;
      if (_num(s.lat) && _num(s.lon) && (s.lat || s.lon)) {
        var pp = RC3D.project(s.lat, s.lon, o);
        mx[i] = pp.x; mz[i] = pp.z; hasP[i] = 1; nPos++;
        if (first < 0) first = i;
      }
      if (_num(s.speed_mph)) {
        var v = Math.abs(s.speed_mph) * 0.44704;
        if (v > vMin && _num(s.heading_deg)) {
          var h = s.heading_deg * Math.PI / 180, sn = Math.sin(h), cs = Math.cos(h);
          var sc = Math.max(0.05, v * sh), A2 = sa * sa, C2 = sc * sc;
          vx[i] = v * sn; vz[i] = -v * cs;
          w00[i] = A2 * sn * sn + C2 * cs * cs;
          w01[i] = sn * cs * (C2 - A2);
          w11[i] = A2 * cs * cs + C2 * sn * sn;
          hasV[i] = 1; nVel++;
        } else if (v <= vMin) {
          // slow / parked: heading is noise, speed says "barely moving"
          var sz = Math.max(0.5, 1.5 * v);
          w00[i] = sz * sz; w01[i] = 0; w11[i] = sz * sz;
          hasV[i] = 2;
        }
      }
    }
    if (nPos < 20) return null;
    var t = RC3D.timeline(samples);
    var XF = new Float64Array(4 * n), PF = new Float64Array(16 * n);
    var DT = new Float64Array(n), GAP = new Uint8Array(n);
    var xs = new Float64Array(4), P = new Float64Array(16), tmp = new Float64Array(8);
    xs[0] = mx[first]; xs[1] = mz[first];
    if (hasV[first] === 1) { xs[2] = vx[first]; xs[3] = vz[first]; }
    P[0] = P[5] = 1e4; P[10] = P[15] = 1e3;
    for (i = 0; i < n; i++) {
      if (i) {
        var dt = t[i] - t[i - 1];
        if (!(dt > DT_MIN)) dt = DT_MIN;
        DT[i] = dt;
        if (dt > gapS) {
          // a long gap: do not extrapolate a corner into a straight line -
          // keep the state, forget how sure we were
          GAP[i] = 1;
          for (c = 0; c < 16; c++) P[c] = 0;
          P[0] = P[5] = 1e6; P[10] = P[15] = 1e4;
        } else {
          xs[0] += dt * xs[2]; xs[1] += dt * xs[3];
          _kfPredict(P, dt, q);
        }
      }
      if (hasP[i]) _kfUpdate(xs, P, 0, 1, mx[i], mz[i], R0, 0, R0, tmp);
      if (hasV[i] === 1) _kfUpdate(xs, P, 2, 3, vx[i], vz[i], w00[i], w01[i], w11[i], tmp);
      else if (hasV[i] === 2) _kfUpdate(xs, P, 2, 3, 0, 0, w00[i], 0, w11[i], tmp);
      for (c = 0; c < 4; c++) XF[i * 4 + c] = xs[c];
      for (c = 0; c < 16; c++) PF[i * 16 + c] = P[c];
    }
    // Rauch-Tung-Striebel: x_k|n = x_k|k + P_k F^T Pp^-1 (x_k+1|n - F x_k|k)
    var outX = new Float64Array(n), outZ = new Float64Array(n), d = new Float64Array(4);
    var nx0 = XF[(n - 1) * 4], nx1 = XF[(n - 1) * 4 + 1], nx2 = XF[(n - 1) * 4 + 2], nx3 = XF[(n - 1) * 4 + 3];
    outX[n - 1] = nx0; outZ[n - 1] = nx1;
    for (k = n - 2; k >= 0; k--) {
      var b = k * 4, dtk = DT[k + 1];
      var f0 = XF[b], f1 = XF[b + 1], f2 = XF[b + 2], f3 = XF[b + 3];
      if (!GAP[k + 1]) {
        for (c = 0; c < 16; c++) P[c] = PF[k * 16 + c];
        _kfPredict(P, dtk, q);
        d[0] = nx0 - (f0 + dtk * f2); d[1] = nx1 - (f1 + dtk * f3);
        d[2] = nx2 - f2; d[3] = nx3 - f3;
        if (_chol4(P, d)) {
          // g = F^T y, then x += P_k g
          var g0 = d[0], g1 = d[1], g2 = d[2] + dtk * d[0], g3 = d[3] + dtk * d[1], pb = k * 16;
          f0 += PF[pb] * g0 + PF[pb + 1] * g1 + PF[pb + 2] * g2 + PF[pb + 3] * g3;
          f1 += PF[pb + 4] * g0 + PF[pb + 5] * g1 + PF[pb + 6] * g2 + PF[pb + 7] * g3;
          var u2 = PF[pb + 8] * g0 + PF[pb + 9] * g1 + PF[pb + 10] * g2 + PF[pb + 11] * g3;
          var u3 = PF[pb + 12] * g0 + PF[pb + 13] * g1 + PF[pb + 14] * g2 + PF[pb + 15] * g3;
          f2 += u2; f3 += u3;
        }
      }
      if (!(isFinite(f0) && isFinite(f1) && isFinite(f2) && isFinite(f3))) {
        f0 = XF[b]; f1 = XF[b + 1]; f2 = XF[b + 2]; f3 = XF[b + 3];
      }
      nx0 = f0; nx1 = f1; nx2 = f2; nx3 = f3;
      outX[k] = f0; outZ[k] = f1;
    }
    return { x: outX, z: outZ, nVel: nVel, nPos: nPos, ok: true };
  };

  // The drivable path: smoothed positions, cumulative arc length, and a dense
  // (~denseStep m) centreline with its own arc-length table + XZ tangents.
  // Repeated logger rows are dropped first (opts.keepRepeats opts out); the
  // rows actually used are path.samples, path.srcIndex maps them back to the
  // caller's array - index path.t / path.speed with THOSE, not the input.
  RC3D.buildPath = function (samples, opts) {
    opts = opts || {};
    var srcIdx = null;
    if (!opts.keepRepeats) {
      srcIdx = _cleanIdx(samples);
      if (srcIdx.length !== samples.length) {
        var orig = samples;
        samples = srcIdx.map(function (k) { return orig[k]; });
      }
    }
    var win = opts.smooth == null ? 5 : opts.smooth;
    var step = opts.denseStep || 1.0;
    var maxCp = opts.maxCurvePoints || 2400;
    var i, n = samples.length;
    var lat = new Array(n), lon = new Array(n), alt = new Array(n), mph = new Array(n), mphRaw;
    var rpmA = new Float64Array(n);
    for (i = 0; i < n; i++) {
      var s = samples[i];
      lat[i] = (typeof s.lat === "number") ? s.lat : NaN;
      lon[i] = (typeof s.lon === "number") ? s.lon : NaN;
      alt[i] = (typeof s.alt_m === "number" && isFinite(s.alt_m)) ? s.alt_m : NaN;
      rpmA[i] = (typeof s.rpm === "number" && isFinite(s.rpm)) ? s.rpm : NaN;
      mph[i] = (typeof s.speed_mph === "number" && isFinite(s.speed_mph)) ? s.speed_mph : 0;
    }
    // origin = mean of the valid fixes (keeps the local projection tight)
    var sla = 0, slo = 0, na = 0;
    for (i = 0; i < n; i++)
      if (isFinite(lat[i]) && isFinite(lon[i]) && (lat[i] || lon[i])) { sla += lat[i]; slo += lon[i]; na++; }
    var o = opts.o || (na ? { lat: sla / na, lon: slo / na } : { lat: 0, lon: 0 });

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
    // lateral smoothing is the whole point: GPS noise is 1-2 m at 25 Hz.
    // With real course-over-ground data (at least half the moving rows carry a
    // heading) the Kalman/RTS path puts the car where it really was across the
    // track; the smooth slider maps onto how far a single fix is trusted.
    // Otherwise (old logs, synthetic rows) the centred moving average.
    var posSource = "smooth";
    if (opts.kalman !== false) {
      var nMov = 0, nHd = 0;
      for (i = 0; i < n; i++) {
        var sk = samples[i];
        if (sk && _num(sk.speed_mph) && Math.abs(sk.speed_mph) * 0.44704 > 4) {
          nMov++;
          if (_num(sk.heading_deg)) nHd++;
        }
      }
      if (nMov > 0 && nHd * 2 >= nMov) {
        var psg = Math.min(3, Math.max(0.5, 0.5 + 0.125 * (Math.max(1, win) - 1)));
        var kp = RC3D.kalmanPath(samples, o, { posSigma: psg });
        if (kp) { X = Array.from(kp.x); Z = Array.from(kp.z); posSource = "kalman"; }
      }
    }
    if (posSource === "smooth") {
      X = RC3D.smooth(X, win);
      Z = RC3D.smooth(Z, win);
    }
    mphRaw = mph.slice();
    mph = RC3D.smooth(RC3D.fillNulls(mph, 0), Math.max(3, win));

    var t = RC3D.timeline(samples);

    // Longitudinal acceleration in g, from SPEED (not RPM): it is gear
    // independent, which is the whole point — 1st and 4th cannot be compared on
    // an rpm rate, and we do not log gear. A time regression on the raw speed
    // (fused with the IMU when that fits; see longG), on de-duplicated rows.
    var acc = RC3D.longG(t, mphRaw, samples);
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
    var cp = [], cpIdx = [], every = Math.max(1, Math.ceil(n / maxCp));
    if (canCurve) {
      for (i = 0; i < n; i += every) { cp.push(new THREE.Vector3(X[i], Y[i], Z[i])); cpIdx.push(i); }
      if (cp.length > 1 && n > 1 && cpIdx[cpIdx.length - 1] !== n - 1) {
        cp.push(new THREE.Vector3(X[n - 1], Y[n - 1], Z[n - 1])); cpIdx.push(n - 1);
      }
    }

    var dx2 = [], dy2 = [], dz2 = [], ds2 = [], dTotal = 0, knC = null, knS = null;
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
      // Knots: (sample arc length, spline arc length) at every control point.
      // The two lengths drift apart along a session (the spline cuts the
      // corners of the decimated fixes): one global ratio put the car 180 m
      // from where it was by lap 8 at Summit Point. Convert piecewise instead.
      if (typeof curve.getLengths === "function") {
        var LL = curve.getLengths(curve.arcLengthDivisions), DV = LL.length - 1,
            Lend = LL[DV] || 1, NK = cpIdx.length;
        knC = new Float64Array(NK); knS = new Float64Array(NK);
        for (i = 0; i < NK; i++) {
          var u = NK > 1 ? i / (NK - 1) * DV : 0, j = Math.min(DV, Math.floor(u)), fr = u - j;
          var len = j >= DV ? LL[DV] : LL[j] + (LL[j + 1] - LL[j]) * fr;
          knC[i] = cum[cpIdx[i]];
          knS[i] = Math.max(i ? knS[i - 1] : 0, len * dTotal / Lend);
        }
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
    var out = {
      o: o, n: n, x: X, y: Y, z: Z, t: t, speed: mph, accel: acc, cum: cum, total: total,
      accelSource: acc.source, imuR: acc.imuR, samples: samples, rpm: rpmA,
      srcIndex: srcIdx, positionSource: posSource,
      yRef: yMin, dense: { x: dx2, y: dy2, z: dz2, s: ds2, total: dTotal, tan: tan,
                           knC: knC, knS: knS }
    };
    out.input = RC3D.inputStates(out);
    return out;
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

  // Sample arc length (path.cum, what sAtTime returns) <-> spline arc length
  // (path.dense.s). Through the knots buildPath records when it has them,
  // else one ratio (a path built with no spline: the two are the same).
  function _knotMap(A, B, v) {
    var n = A.length, lo = 0, hi = n - 1, mid;
    if (v <= A[0]) return B[0] + (v - A[0]);
    if (v >= A[n - 1]) return B[n - 1] + (v - A[n - 1]);
    while (lo < hi) { mid = (lo + hi + 1) >> 1; if (A[mid] <= v) lo = mid; else hi = mid - 1; }
    var i = Math.min(lo, n - 2), da = A[i + 1] - A[i];
    return B[i] + (da > 0 ? (v - A[i]) / da : 0) * (B[i + 1] - B[i]);
  }
  RC3D.cumToDense = function (path, s) {
    var d = path.dense;
    if (d && d.knC && d.knC.length > 1) return _knotMap(d.knC, d.knS, s);
    return (path.total > 0 && d && d.total > 0) ? s * d.total / path.total : s;
  };
  RC3D.denseToCum = function (path, sd) {
    var d = path.dense;
    if (d && d.knC && d.knC.length > 1) return _knotMap(d.knS, d.knC, sd);
    return (path.total > 0 && d && d.total > 0) ? sd * path.total / d.total : sd;
  };

  // Arc length -> point + tangent on the dense centreline.
  RC3D.pointAtS = function (path, s) {
    var d = path.dense, n = d.s.length, lo, hi, mid, i, seg, f, target;
    if (!n) return { x: 0, y: 0, z: 0, tan: [0, -1], s: 0 };
    target = Math.max(0, Math.min(d.total, RC3D.cumToDense(path, s)));
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
  // The wash is TRANSLUCENT and its BRIGHTNESS carries the intensity, so harder
  // braking glows brighter red and harder acceleration brighter green.
  RC3D.driveColour = function (aG) {
    var thr = 0.03, accelFull = 0.30, brakeFull = 0.7;
    var t, k;
    if (aG > thr) {
      t = Math.max(0.18, Math.min(1, (aG - thr) / (accelFull - thr)));
      k = 0.45 + 0.55 * t;                      // 0.45 -> 1.0
      return [0.10 * k, k, 0.18 * k];
    }
    if (aG < -thr) {
      t = Math.max(0.18, Math.min(1, (-aG - thr) / (brakeFull - thr)));
      k = 0.45 + 0.55 * t;
      return [k, 0.08 * k, 0.10 * k];
    }
    return RC3D.NEUTRAL_GREY.slice();
  };
  RC3D.driveIntensity = function (aG) {
    // 0 = neither, 1 = maximum brake/accel: drives the wash's opacity
    var thr = 0.03, full = aG < 0 ? 0.7 : 0.30;
    var a = Math.abs(aG);
    if (a < thr) return 0;
    return Math.min(1, (a - thr) / (full - thr));
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
    var uv = (opt && (opt.uv || opt.worldUV)) ? new Float32Array(n * 4) : null;
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
      if (uv && opt.worldUV) {
        // procedural-surface UVs in WORLD METRES: u along the ribbon, v across.
        // A tarmac texture must not stretch with track width or length.
        uv[i * 4] = d.s[i] / opt.worldUV;
        uv[i * 4 + 1] = 0;
        uv[i * 4 + 2] = d.s[i] / opt.worldUV;
        uv[i * 4 + 3] = (hl + hr) / opt.worldUV;
        continue;
      }
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
      // an S-bend is two corners: a change of direction ends the run (merged
      // as one, a left and a right cancel out and the whole set vanished)
      if (on && run && (k > 0 ? 1 : -1) !== run.dir0) { out.push(run); run = null; }
      if (on && !run) run = { i0: i, i1: i, sign: k > 0 ? 1 : -1, dir0: k > 0 ? 1 : -1,
                              kbest: Math.abs(k), apex: i };
      else if (on && run) {
        run.i1 = i;
        if (Math.abs(k) > run.kbest) { run.kbest = Math.abs(k); run.apex = i; run.sign = k > 0 ? 1 : -1; }
      } else if (!on && run) {
        var gap = 0, j = i;
        while (j < n && Math.abs(sm[j]) <= kMax && d.s[j] - d.s[i] < mergeM) { j++; }
        // a short gap before more of the SAME direction: same corner, keep going
        if (j < n - 1 && Math.abs(sm[j]) > kMax && (sm[j] > 0 ? 1 : -1) === run.dir0) continue;
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

  // ---- road distance field ------------------------------------------------
  // A raster (`cell` metres) over `box` that holds, per node, the nearest dense
  // centreline station. Built by feature propagation (one forward and one
  // backward raster pass), so every tree, ground vertex, gravel strip and
  // barrier post can ask "how far am I from the road, and WHICH bit of road"
  // in O(1). nearest() then refines along the centreline, so the answer is
  // exact where it matters (near the road).
  RC3D.roadField = function (dense, box, cell) {
    cell = cell || 4;
    var X = dense.x, Z = dense.z, n = X.length;
    var win = Math.max(12, Math.ceil(cell * 3));      // refine reach, in ~1 m stations
    var cols = Math.max(2, Math.ceil((box.maxX - box.minX) / cell) + 1);
    var rows = Math.max(2, Math.ceil((box.maxZ - box.minZ) / cell) + 1);
    var N = cols * rows, idx = new Int32Array(N), dist = new Float32Array(N);
    var i, r, c, k;
    for (k = 0; k < N; k++) { idx[k] = -1; dist[k] = 1e9; }
    for (i = 0; i < n; i++) {
      c = Math.round((X[i] - box.minX) / cell);
      r = Math.round((Z[i] - box.minZ) / cell);
      if (c < 0 || r < 0 || c >= cols || r >= rows) continue;
      k = r * cols + c;
      var ex = X[i] - (box.minX + c * cell), ez = Z[i] - (box.minZ + r * cell);
      var ed = Math.sqrt(ex * ex + ez * ez);
      if (ed < dist[k]) { dist[k] = ed; idx[k] = i; }
    }
    var relax = function (k0, kn, x, z) {
      var j = idx[kn];
      if (j < 0) return;
      var dx = X[j] - x, dz = Z[j] - z, dd = Math.sqrt(dx * dx + dz * dz);
      if (dd < dist[k0]) { dist[k0] = dd; idx[k0] = j; }
    };
    var x0, z0;
    for (r = 0; r < rows; r++) {
      z0 = box.minZ + r * cell;
      for (c = 0; c < cols; c++) {
        k = r * cols + c; x0 = box.minX + c * cell;
        if (c > 0) relax(k, k - 1, x0, z0);
        if (r > 0) {
          relax(k, k - cols, x0, z0);
          if (c > 0) relax(k, k - cols - 1, x0, z0);
          if (c < cols - 1) relax(k, k - cols + 1, x0, z0);
        }
      }
      for (c = cols - 2; c >= 0; c--) {
        k = r * cols + c;
        relax(k, k + 1, box.minX + c * cell, z0);
      }
    }
    for (r = rows - 1; r >= 0; r--) {
      z0 = box.minZ + r * cell;
      for (c = cols - 1; c >= 0; c--) {
        k = r * cols + c; x0 = box.minX + c * cell;
        if (c < cols - 1) relax(k, k + 1, x0, z0);
        if (r < rows - 1) {
          relax(k, k + cols, x0, z0);
          if (c < cols - 1) relax(k, k + cols + 1, x0, z0);
          if (c > 0) relax(k, k + cols - 1, x0, z0);
        }
      }
      for (c = 1; c < cols; c++) {
        k = r * cols + c;
        relax(k, k - 1, box.minX + c * cell, z0);
      }
    }
    var cellOf = function (x, z) {
      var cc = Math.round((x - box.minX) / cell), rr = Math.round((z - box.minZ) / cell);
      if (cc < 0) cc = 0; else if (cc >= cols) cc = cols - 1;
      if (rr < 0) rr = 0; else if (rr >= rows) rr = rows - 1;
      return rr * cols + cc;
    };
    var outside = function (x, z) {
      var ox = Math.max(0, box.minX - x, x - box.maxX);
      var oz = Math.max(0, box.minZ - z, z - box.maxZ);
      return Math.sqrt(ox * ox + oz * oz);
    };
    return {
      box: box, cell: cell, cols: cols, rows: rows,
      // cheap, raster-accurate (~cell/2) distance to the centreline
      approx: function (x, z) { return dist[cellOf(x, z)] + outside(x, z); },
      // exact nearest station: the raster's guesses from the 3x3 cells around
      // the point (where two sections meet, neighbouring cells disagree about
      // which one is nearer), each refined along the centreline
      nearest: function (x, z) {
        var cc = Math.round((x - box.minX) / cell), rr = Math.round((z - box.minZ) / cell);
        var best = -1, bd = Infinity, seen = [], dr, dc, a;
        for (dr = -1; dr <= 1; dr++) {
          var r2 = Math.max(0, Math.min(rows - 1, rr + dr));
          for (dc = -1; dc <= 1; dc++) {
            var c2 = Math.max(0, Math.min(cols - 1, cc + dc));
            var j = idx[r2 * cols + c2];
            if (j < 0) continue;
            var dup = false;
            for (a = 0; a < seen.length; a++) if (Math.abs(seen[a] - j) <= 8) { dup = true; break; }
            if (dup) continue;
            seen.push(j);
            var a0 = Math.max(0, j - win), a1 = Math.min(n - 1, j + win);
            for (a = a0; a <= a1; a++) {
              var dx = X[a] - x, dz = Z[a] - z, dd = dx * dx + dz * dz;
              if (dd < bd) { bd = dd; best = a; }
            }
          }
        }
        return best < 0 ? null : { i: best, d: Math.sqrt(bd) };
      }
    };
  };

  // Bounding box of a dense centreline, grown by `margin` metres.
  RC3D.denseBox = function (dense, margin) {
    var minX = Infinity, maxX = -Infinity, minZ = Infinity, maxZ = -Infinity, i;
    for (i = 0; i < dense.x.length; i++) {
      if (dense.x[i] < minX) minX = dense.x[i];
      if (dense.x[i] > maxX) maxX = dense.x[i];
      if (dense.z[i] < minZ) minZ = dense.z[i];
      if (dense.z[i] > maxZ) maxZ = dense.z[i];
    }
    margin = margin || 0;
    return { minX: minX - margin, maxX: maxX + margin, minZ: minZ - margin, maxZ: maxZ + margin };
  };

  // Terrain WITHOUT a prepared DEM: a smooth surface through the road's own
  // logged elevation (Gaussian-weighted, ~40 m), easing to the session mean far
  // away. Near any section of road it IS that road's height, so two sections at
  // different heights meet in a slope, never a cliff.
  RC3D.pathTerrain = function (dense) {
    var n = dense.x.length, i, mean = 0, cell = 40, grid = new Map();
    for (i = 0; i < n; i++) mean += dense.y[i];
    mean = n ? mean / n : 0;
    var key = function (gx, gz) { return (gx + 50000) * 100000 + (gz + 50000); };
    for (i = 0; i < n; i += 2) {
      var kk = key(Math.floor(dense.x[i] / cell), Math.floor(dense.z[i] / cell));
      var b = grid.get(kk);
      if (!b) { b = []; grid.set(kk, b); }
      b.push(i);
    }
    // the BROAD field (sigma ~260 m) that the near field eases into: a coarse
    // grid, so far from the road the ground is a gentle regional surface and
    // never a jump to one flat "session mean"
    var fb = RC3D.denseBox(dense, 1600);
    var ext = Math.max(fb.maxX - fb.minX, fb.maxZ - fb.minZ);
    var G = Math.max(80, ext / 90);
    var gc = Math.ceil((fb.maxX - fb.minX) / G) + 1, gr = Math.ceil((fb.maxZ - fb.minZ) / G) + 1;
    var far = new Float32Array(gc * gr), sigF = 2 * 260 * 260;
    var stride = Math.max(1, Math.floor(n / 600)), r, c;
    for (r = 0; r < gr; r++) {
      for (c = 0; c < gc; c++) {
        var fx = fb.minX + c * G, fz = fb.minZ + r * G, fw = 1e-4, fy = 1e-4 * mean;
        for (i = 0; i < n; i += stride) {
          var ex = dense.x[i] - fx, ez = dense.z[i] - fz;
          var ww = Math.exp(-(ex * ex + ez * ez) / sigF);
          fw += ww; fy += ww * dense.y[i];
        }
        far[r * gc + c] = fy / fw;
      }
    }
    var farAt = function (x, z) {
      var u = Math.max(0, Math.min(gc - 1.001, (x - fb.minX) / G));
      var v = Math.max(0, Math.min(gr - 1.001, (z - fb.minZ) / G));
      var c0 = Math.floor(u), r0 = Math.floor(v), tu = u - c0, tv = v - r0;
      var a00 = far[r0 * gc + c0], a01 = far[r0 * gc + c0 + 1];
      var a10 = far[(r0 + 1) * gc + c0], a11 = far[(r0 + 1) * gc + c0 + 1];
      return a00 * (1 - tu) * (1 - tv) + a01 * tu * (1 - tv) + a10 * (1 - tu) * tv + a11 * tu * tv;
    };
    var sig2 = 2 * 40 * 40, R = 120, cut = Math.exp(-(R * R) / sig2);
    return function (x, z) {
      var gx = Math.floor(x / cell), gz = Math.floor(z / cell), sw = 0, sy = 0, a, cc, k;
      for (a = -3; a <= 3; a++) {
        for (cc = -3; cc <= 3; cc++) {
          var bucket = grid.get(key(gx + a, gz + cc));
          if (!bucket) continue;
          for (k = 0; k < bucket.length; k++) {
            var j = bucket[k], dx = dense.x[j] - x, dz = dense.z[j] - z;
            // the window always reaches >= R (= 3 cells), and the weight is
            // exactly 0 at R, so a bucket entering/leaving changes nothing
            var w = Math.exp(-(dx * dx + dz * dz) / sig2) - cut;
            if (w <= 0) continue;
            sw += w; sy += w * dense.y[j];
          }
        }
      }
      var wf = 0.4;                       // the regional surface, blended in as the road fades
      return (sy + wf * farAt(x, z)) / (sw + wf);
    };
  };

  // Ground height at (x,z): the terrain, FLATTENED to just under the road across
  // the road plus a shoulder, then eased back to the terrain over `blend` m.
  // The road can neither float above the grass nor be buried in a hillside.
  RC3D.groundField = function (dense, field, opt) {
    opt = opt || {};
    var hw = opt.halfWidth || function () { return 6; };
    var terrain = opt.terrain || function () { return 0; };
    var drop = opt.drop == null ? 0.06 : opt.drop;
    var shoulder = opt.shoulder == null ? 3 : opt.shoulder;
    var blend = opt.blend == null ? 30 : opt.blend;
    var reach = (opt.maxHalf || 15) + shoulder + blend;
    return function (x, z) {
      var t = terrain(x, z);
      if (field.approx(x, z) > reach + field.cell) return t;
      var q = field.nearest(x, z);
      if (!q) return t;
      var ry = dense.y[q.i] - drop, edge = hw(q.i) + shoulder;
      if (q.d <= edge) return ry;
      var u = (q.d - edge) / blend;
      if (u >= 1) return t;
      u = u * u * (3 - 2 * u);
      return ry + (t - ry) * u;
    };
  };

  RC3D.unrle = function (rle) {
    var out = [], num = "", i, ch;
    for (i = 0; i < rle.length; i++) {
      ch = rle.charAt(i);
      if (ch >= "0" && ch <= "9") num += ch;
      else { out.push(new Array((parseInt(num || "1", 10)) + 1).join(ch)); num = ""; }
    }
    return out.join("");
  };

  // Smooth value noise in [0,1], deterministic per seed, `scale` metres/lattice.
  RC3D.valueNoise = function (seed, scale) {
    var h0 = 2166136261, i;
    seed = String(seed || "rc");
    for (i = 0; i < seed.length; i++) { h0 ^= seed.charCodeAt(i); h0 = Math.imul(h0, 16777619) >>> 0; }
    var lat = function (ix, iz) {
      var h = (Math.imul(ix, 374761393) + Math.imul(iz, 668265263) + h0) | 0;
      h = Math.imul(h ^ (h >>> 13), 1274126177);
      return ((h ^ (h >>> 16)) >>> 0) / 4294967296;
    };
    return function (x, z) {
      var fx = x / scale, fz = z / scale, ix = Math.floor(fx), iz = Math.floor(fz);
      var tx = fx - ix, tz = fz - iz;
      tx = tx * tx * (3 - 2 * tx); tz = tz * tz * (3 - 2 * tz);
      var a = lat(ix, iz), b = lat(ix + 1, iz), c = lat(ix, iz + 1), d = lat(ix + 1, iz + 1);
      return a + (b - a) * tx + (c - a) * tz + (a - b - c + d) * tx * tz;
    };
  };

  // Where the trees go. With imagery land cover: only in cells the imagery
  // calls WOODS. Without it: clumps from smooth noise, well back from the
  // road. Either way NO tree may stand on, or overhang, any part of the
  // circuit: canopy edge >= `gap` metres beyond the road edge of the NEAREST
  // section (not just the section the tree was generated from), which is what
  // keeps them out of the infield on a circuit that folds back on itself.
  RC3D.treeSpots = function (dense, field, opt) {
    opt = opt || {};
    var rnd = opt.rnd || Math.random;
    var hw = opt.halfWidth || function () { return 6; };
    var gap = opt.gap == null ? 9 : opt.gap;
    var maxN = opt.max || 8000, maxD = opt.maxDist || 700;
    var cand = [], i, r, c;
    var density = function (d) {
      if (d < 260) return 0.5;
      if (d < 520) return 0.2;
      return 0.07;
    };
    var lc = opt.landcover, o = opt.o;
    if (lc && lc.codes && o) {
      var S = lc.bounds[0], W = lc.bounds[1], N = lc.bounds[2], E = lc.bounds[3];
      var pSW = RC3D.project(S, W, o), pNE = RC3D.project(N, E, o);
      var cw = (pNE.x - pSW.x) / lc.cols, ch = (pSW.z - pNE.z) / lc.rows;
      for (r = 0; r < lc.rows; r++) {
        for (c = 0; c < lc.cols; c++) {
          if (lc.codes.charAt(r * lc.cols + c) !== "w") continue;
          var x = pSW.x + (c + 0.5) * cw, z = pNE.z + (r + 0.5) * ch;
          var dA = field.approx(x, z);
          if (dA > maxD || dA < 8) continue;
          if (rnd() > density(dA)) continue;
          cand.push({ x: x + (rnd() - 0.5) * cw, z: z + (rnd() - 0.5) * ch, d: dA });
        }
      }
    } else {
      var noise = RC3D.valueNoise(opt.seed || "trees", 110);
      var fine = RC3D.valueNoise((opt.seed || "trees") + "f", 33);
      var b = field.box, step = 7;
      for (var zz = b.minZ; zz <= b.maxZ; zz += step) {
        for (var xx = b.minX; xx <= b.maxX; xx += step) {
          var d2 = field.approx(xx, zz);
          if (d2 < 30 || d2 > maxD) continue;
          var v = 0.7 * noise(xx, zz) + 0.3 * fine(xx, zz);
          if (v < 0.58) continue;                 // clearings / open grass
          if (rnd() > density(d2) * 1.4) continue;
          cand.push({ x: xx + (rnd() - 0.5) * step, z: zz + (rnd() - 0.5) * step, d: d2 });
        }
      }
    }
    // individually mapped trees (OpenStreetMap natural=tree / tree rows)
    (opt.extra || []).forEach(function (p) {
      var dE = field.approx(p.x, p.z);
      if (dE <= maxD) cand.push({ x: p.x, z: p.z, d: dE });
    });
    var kindNoise = RC3D.valueNoise((opt.seed || "trees") + "k", 140);
    var out = [];
    for (i = 0; i < cand.length; i++) {
      var p = cand[i];
      var conifer = kindNoise(p.x, p.z) + (rnd() - 0.5) * 0.35 < (opt.coniferShare || 0.42);
      var h = conifer ? 12 + rnd() * 11 : 10 + rnd() * 8;
      var w = conifer ? h * (0.62 + rnd() * 0.22) : h * (0.95 + rnd() * 0.35);
      var canopy = (conifer ? 0.45 : 0.41) * w;          // the drawn mesh, jitter included
      var q = field.nearest(p.x, p.z);
      if (q && q.d - hw(q.i) - canopy < gap) continue;     // on / over the circuit
      out.push({ x: p.x, z: p.z, h: h, w: w, canopy: canopy, kind: conifer ? 0 : 1,
                 d: p.d, rot: rnd() * Math.PI * 2, tint: rnd(), prio: p.d + rnd() * 60 });
    }
    if (out.length > maxN) {
      out.sort(function (a, b2) { return a.prio - b2.prio; });
      out.length = maxN;
    }
    return out;
  };

  // ---- braking zones and lifts, from the classified input ------------------
  // One entry per braking zone (brake runs merged when < mergeGap s apart; the
  // zone must drop >= minDrop mph) and per LIFT (a coast stretch between
  // throttle with no brake that drops >= liftDrop mph). min = slowest point
  // between the zone start and the first throttle after it (capped +8 s);
  // throttle = first throttle sample at/after the min (else release_i).
  // *_s are SAMPLE arc length (path.cum). Sorted by brake_i.
  RC3D.cornerEvents = function (path, opt) {
    opt = opt || {};
    var n = path.t.length, t = path.t, sp = path.speed, cum = path.cum, a = path.accel || [];
    var inp = path.input || RC3D.inputStates(path), st = inp.state;
    var i0 = Math.max(0, opt.i0 == null ? 0 : opt.i0);
    var i1 = Math.min(n - 1, opt.i1 == null ? n - 1 : opt.i1);
    var mergeGap = opt.mergeGap == null ? 0.6 : opt.mergeGap;
    var minDrop = opt.minDrop == null ? 5 : opt.minDrop;
    var liftDrop = opt.liftDrop == null ? 6 : opt.liftDrop;
    var capS = opt.cap_s == null ? 8 : opt.cap_s;
    var out = [], i, j;
    if (!(i1 > i0)) return out;
    function finish(kind, b, rel) {
      // slowest point up to the first throttle sample after the zone
      var end = rel, tCap = t[rel] + capS;
      while (end < i1 && st[end + 1] !== 1 && t[end + 1] <= tCap) end++;
      if (end < i1 && t[end + 1] <= tCap) end++;
      var mi = b, peak = 0, k;
      for (k = b; k <= end; k++) if (sp[k] < sp[mi]) mi = k;
      for (k = b; k <= rel; k++) if (-(a[k] || 0) > peak) peak = -(a[k] || 0);
      var thr = -1;
      for (k = mi; k <= i1 && t[k] <= tCap; k++) if (st[k] === 1) { thr = k; break; }
      if (thr < 0) thr = rel;
      var entry = sp[b];
      for (k = b; k >= i0 && t[b] - t[k] <= 1.0; k--) if (sp[k] > entry) entry = sp[k];
      return { kind: kind, brake_i: b, brake_s: cum[b], brake_mph: sp[b],
               release_i: rel, min_i: mi, min_s: cum[mi], min_mph: sp[mi],
               throttle_i: thr, throttle_s: cum[thr], throttle_mph: sp[thr],
               peak_g: peak, entry_mph: entry };
    }
    // brake zones
    i = i0;
    while (i <= i1) {
      if (st[i] !== -1) { i++; continue; }
      var b = i, rel = i;
      for (;;) {
        while (rel + 1 <= i1 && st[rel + 1] === -1) rel++;
        j = rel + 1;
        while (j <= i1 && st[j] !== -1 && t[j] - t[rel] < mergeGap) j++;
        if (j <= i1 && st[j] === -1 && t[j] - t[rel] < mergeGap) { rel = j; continue; }
        break;
      }
      var ev = finish("brake", b, rel);
      if (ev.brake_mph - ev.min_mph >= minDrop) out.push(ev);
      i = rel + 1;
    }
    // lifts: maximal non-throttle stretches containing no brake
    i = i0;
    while (i <= i1) {
      if (st[i] !== 0) { i++; continue; }
      var c0 = i, c1 = i, braked = false;
      while (c1 + 1 <= i1 && st[c1 + 1] !== 1) { c1++; if (st[c1] === -1) braked = true; }
      if (!braked && (c0 === i0 || st[c0 - 1] === 1)) {
        var lv = finish("lift", c0, c1);
        if (lv.brake_mph - lv.min_mph >= liftDrop) out.push(lv);
      }
      i = c1 + 1;
    }
    out.sort(function (p, q) { return p.brake_i - q.brake_i; });
    return out;
  };

  // ---- track shape: a polyline in the local metric frame ----------------
  // Lines are two parallel arrays (x[], z[]) in the RC3D.project frame (+x
  // east, -z north). _lineIndex buckets the SEGMENTS in a grid hash so the
  // nearest point on the polyline is cheap; `signed` is the lateral offset
  // along the right-hand normal (-tz, tx), the ribbon's convention.
  function _lineIndex(lx, lz, cell) {
    var n = lx.length, grid = {}, i;
    cell = cell || 20;
    function key(cx, cz) { return cx + "," + cz; }
    for (i = 0; i < n - 1; i++) {
      var ax = Math.floor(Math.min(lx[i], lx[i + 1]) / cell), bx = Math.floor(Math.max(lx[i], lx[i + 1]) / cell);
      var az = Math.floor(Math.min(lz[i], lz[i + 1]) / cell), bz = Math.floor(Math.max(lz[i], lz[i + 1]) / cell);
      for (var cx = ax; cx <= bx; cx++) for (var cz = az; cz <= bz; cz++) {
        var k = key(cx, cz);
        (grid[k] || (grid[k] = [])).push(i);
      }
    }
    return {
      nearest: function (x, z, maxD) {
        var r = Math.max(1, Math.ceil(maxD / cell)), gx = Math.floor(x / cell), gz = Math.floor(z / cell);
        var best = null, bd = maxD * maxD, cx, cz, q;
        for (cx = gx - r; cx <= gx + r; cx++) for (cz = gz - r; cz <= gz + r; cz++) {
          var L = grid[key(cx, cz)];
          if (!L) continue;
          for (q = 0; q < L.length; q++) {
            var s = L[q], dx = lx[s + 1] - lx[s], dz = lz[s + 1] - lz[s];
            var ll = dx * dx + dz * dz, f = ll > 0 ? ((x - lx[s]) * dx + (z - lz[s]) * dz) / ll : 0;
            f = Math.max(0, Math.min(1, f));
            var px = lx[s] + dx * f, pz = lz[s] + dz * f;
            var d2 = (x - px) * (x - px) + (z - pz) * (z - pz);
            if (d2 <= bd) {
              bd = d2;
              var len = Math.sqrt(ll) || 1, nx = -dz / len, nz = dx / len;
              best = { d: Math.sqrt(d2), px: px, pz: pz, nx: nx, nz: nz, seg: s, f: f,
                       i: f < 0.5 ? s : s + 1,
                       signed: (x - px) * nx + (z - pz) * nz };
            }
          }
        }
        return best;
      }
    };
  }
  RC3D.lineIndex = _lineIndex;

  // The rigid TRANSLATION {dx, dz} to apply to the LINE (e.g. a prepared
  // track's centreline) so the logged fixes sit on it. Point-to-line ICP,
  // translation only: per fix the nearest point p + normal n on the line,
  // residual e = n.(q - (p + T)); fixes > maxD (25 m) away are ignored, Huber
  // weights (k = 3 m) tame the rest; 2x2 normal equations per iteration.
  // A racing line sits up to +/- half a track width off the centre, but around
  // a closed circuit those offsets point every way and cancel in the sum.
  // medDist = median |distance| after; inFrac = share within 10 m after.
  RC3D.registerLine = function (lineX, lineZ, fixX, fixZ, opt) {
    opt = opt || {};
    var maxD = opt.maxDist || 25, hub = opt.huber || 3, iters = opt.iters || 4;
    var maxN = opt.maxFixes || 3000, nf = fixX.length, i, it;
    var res = { dx: 0, dz: 0, medDist: Infinity, inFrac: 0, used: 0 };
    if (lineX.length < 2 || !nf) return res;
    var idx = _lineIndex(lineX, lineZ, maxD), step = Math.max(1, Math.ceil(nf / maxN));
    var qx = [], qz = [];
    for (i = 0; i < nf; i += step) if (isFinite(fixX[i]) && isFinite(fixZ[i])) { qx.push(fixX[i]); qz.push(fixZ[i]); }
    var Tx = 0, Tz = 0;   // applied to the LINE
    for (it = 0; it < iters; it++) {
      var a11 = 0, a12 = 0, a22 = 0, b1 = 0, b2 = 0, used = 0;
      for (i = 0; i < qx.length; i++) {
        var h = idx.nearest(qx[i] - Tx, qz[i] - Tz, maxD);
        if (!h) continue;
        var e = h.signed;                       // n.(q - (p + T))
        var w = Math.abs(e) <= hub ? 1 : hub / Math.abs(e);
        a11 += w * h.nx * h.nx; a12 += w * h.nx * h.nz; a22 += w * h.nz * h.nz;
        b1 += w * h.nx * e; b2 += w * h.nz * e;
        used++;
      }
      var det = a11 * a22 - a12 * a12;
      res.used = used;
      if (used < 3 || !(Math.abs(det) > 1e-9 * Math.max(1, used * used))) break;
      // moving the line by dT changes e by -n.dT: solve sum w n n^T dT = sum w n e
      Tx += (a22 * b1 - a12 * b2) / det;
      Tz += (a11 * b2 - a12 * b1) / det;
    }
    var ds = [], inside = 0;
    for (i = 0; i < qx.length; i++) {
      var h2 = idx.nearest(qx[i] - Tx, qz[i] - Tz, maxD);
      var d = h2 ? h2.d : Infinity;
      ds.push(d);
      if (d <= 10) inside++;
    }
    ds.sort(function (p, q) { return p - q; });
    res.dx = Tx; res.dz = Tz;
    res.medDist = ds.length ? ds[ds.length >> 1] : Infinity;
    res.inFrac = qx.length ? inside / qx.length : 0;
    return res;
  };

  // A cleaner track shape from ALL laps when there is no prepared track:
  // reference = the fastest complete lap's dense slice; every OTHER complete
  // lap's fixes are projected onto it (nearest station, within 15 m), the
  // per-2 m-bin MEDIAN lateral offset is smoothed (~15 m) and the reference
  // shifted by it. Fewer than 2 laps -> the reference itself. {x:[], z:[]}.
  RC3D.consensusLine = function (path, laps, opt) {
    opt = opt || {};
    var binM = opt.bin_m || 2, near = opt.near_m || 15, smM = opt.smooth_m || 15;
    var tEnd = path.t.length ? path.t[path.t.length - 1] : 0, i, k;
    var good = (laps || []).filter(function (L) {
      return L && isFinite(L.t_start) && isFinite(L.t_end) && L.t_end > L.t_start + 5 &&
             L.t_start >= -0.5 && L.t_end <= tEnd + 0.5;
    });
    var d = path.dense;
    var best = null;
    good.forEach(function (L) {
      var secs = isFinite(L.seconds) && L.seconds > 0 ? L.seconds : L.t_end - L.t_start;
      if (!best || secs < best.secs) best = { L: L, secs: secs };
    });
    var rx = [], rz = [], rs = [];
    if (best) {
      var dA = RC3D.cumToDense(path, RC3D.sAtTime(path, best.L.t_start)),
          dB = RC3D.cumToDense(path, RC3D.sAtTime(path, best.L.t_end));
      for (i = 0; i < d.s.length; i++) {
        if (d.s[i] < dA || d.s[i] > dB) continue;
        rx.push(d.x[i]); rz.push(d.z[i]); rs.push(d.s[i] - dA);
      }
    }
    if (rx.length < 4) return { x: d.x.slice(), z: d.z.slice(), whole: true };   // no usable lap
    var refOut = { x: rx.slice(), z: rz.slice() };
    if (good.length < 2) return refOut;
    var idx = _lineIndex(rx, rz, near), total = rs[rs.length - 1];
    var nb = Math.max(1, Math.ceil(total / binM) + 1), bins = [];
    for (k = 0; k < nb; k++) bins.push([]);
    good.forEach(function (L) {
      if (L === best.L) return;
      var a = RC3D.indexOfTime(path.t, L.t_start), b = RC3D.indexOfTime(path.t, L.t_end);
      for (var j = a; j <= b; j++) {
        var h = idx.nearest(path.x[j], path.z[j], near);
        if (!h) continue;
        var s = rs[h.seg] + (rs[h.seg + 1] - rs[h.seg]) * h.f;
        bins[Math.min(nb - 1, Math.max(0, Math.round(s / binM)))].push(h.signed);
      }
    });
    var off = new Float64Array(nb), have = new Uint8Array(nb), any = false;
    for (k = 0; k < nb; k++) {
      var B = bins[k];
      if (!B.length) continue;
      B.sort(function (p, q) { return p - q; });
      off[k] = B.length % 2 ? B[B.length >> 1] : 0.5 * (B[B.length / 2 - 1] + B[B.length / 2]);
      have[k] = 1; any = true;
    }
    if (!any) return refOut;
    // empty bins: linear between their neighbours (flat at the ends)
    var last = -1;
    for (k = 0; k < nb; k++) {
      if (!have[k]) continue;
      if (last < 0) { for (var e0 = 0; e0 < k; e0++) off[e0] = off[k]; }
      else for (var m = last + 1; m < k; m++) off[m] = off[last] + (off[k] - off[last]) * (m - last) / (k - last);
      last = k;
    }
    for (k = last + 1; k < nb; k++) off[k] = off[last];
    // ~15 m moving average; wraps when the reference closes on itself
    var closed = Math.hypot(rx[0] - rx[rx.length - 1], rz[0] - rz[rz.length - 1]) < 30;
    var r = Math.max(1, Math.round(smM / binM / 2)), sm = new Float64Array(nb);
    for (k = 0; k < nb; k++) {
      var sum = 0, cnt = 0;
      for (var q = k - r; q <= k + r; q++) {
        var qq = q;
        if (closed) qq = ((q % nb) + nb) % nb;
        else if (q < 0 || q >= nb) continue;
        sum += off[qq]; cnt++;
      }
      sm[k] = sum / cnt;
    }
    var ox = [], oz = [];
    for (i = 0; i < rx.length; i++) {
      var a0 = Math.max(0, i - 1), b0 = Math.min(rx.length - 1, i + 1);
      var tx = rx[b0] - rx[a0], tz = rz[b0] - rz[a0], tl = Math.hypot(tx, tz) || 1;
      var fb = rs[i] / binM, k0 = Math.min(nb - 1, Math.floor(fb)), k1 = Math.min(nb - 1, k0 + 1);
      var o2 = sm[k0] + (sm[k1] - sm[k0]) * (fb - k0);
      ox.push(rx[i] - (tz / tl) * o2);          // right-hand normal (-tz, tx)
      oz.push(rz[i] + (tx / tl) * o2);
    }
    return { x: ox, z: oz };
  };

  // Fixes that wander just past the road edge (GPS noise on a car using all
  // the track) are pulled back inside: lateral distance between (half - 0.4)
  // and (half + 4.0) m -> +/-(half - 0.4). Farther fixes (pit lane, run-off)
  // are untouched. halfAt(station index) = the half width there. `o` = the
  // RC3D.project origin of the line. Returns {samples: copy, moved: count}.
  // halfAt(i) -> a half width, or [left, right] for an asymmetric road
  // (lineIndex's `signed` is + to the RIGHT of travel: (-dz, dx) is the
  // ribbon's right-hand perpendicular)
  RC3D.snapSamples = function (samples, o, lineX, lineZ, halfAt, opt) {
    opt = opt || {};
    var inset = opt.inset == null ? 0.4 : opt.inset, outer = opt.outer == null ? 4.0 : opt.outer;
    var out = new Array(samples.length), moved = 0, i;
    if (lineX.length < 2) return { samples: samples.slice(), moved: 0 };
    var idx = _lineIndex(lineX, lineZ, 20);
    for (i = 0; i < samples.length; i++) {
      var s = samples[i];
      out[i] = s;
      if (!s || !_num(s.lat) || !_num(s.lon)) continue;
      var p = RC3D.project(s.lat, s.lon, o);
      var h = idx.nearest(p.x, p.z, 60);
      if (!h) continue;
      var hv = halfAt ? halfAt(h.i) : 5;
      var half = (typeof hv === "number") ? hv : (h.signed >= 0 ? hv[1] : hv[0]);
      if (!(half > inset)) continue;
      var ad = Math.abs(h.signed), target = half - inset;
      if (!(ad > target) || ad > half + outer) continue;
      var sg = h.signed >= 0 ? 1 : -1, shift = sg * target - h.signed;
      var ll = RC3D.localToLatLon(p.x + h.nx * shift, p.z + h.nz * shift, o);
      var c = {}, key;
      for (key in s) c[key] = s[key];
      c.lat = ll[0]; c.lon = ll[1];
      out[i] = c;
      moved++;
    }
    return { samples: out, moved: moved };
  };


  // Bridge the unmeasured stations of a per-station array (have[i] = 0) by
  // linear interpolation between their measured neighbours (round the seam
  // of a closed loop). Nothing measured -> left as is.
  RC3D.fillGaps = function (arr, have, closed) {
    var n = arr.length, idx = [], i, k;
    for (i = 0; i < n; i++) if (have[i]) idx.push(i);
    if (!idx.length || idx.length === n) return arr;
    var m = idx.length;
    for (k = 0; k < m; k++) {
      var a = idx[k], b = idx[(k + 1) % m], last = (k === m - 1);
      if (last && !closed) break;
      var gap = last ? (n - a + b) : (b - a);
      for (var g = 1; g < gap; g++) {
        var j = (a + g) % n, f = g / gap;
        arr[j] = arr[a] + (arr[b] - arr[a]) * f;
      }
    }
    if (!closed) {
      for (i = 0; i < idx[0]; i++) arr[i] = arr[idx[0]];
      for (i = idx[m - 1] + 1; i < n; i++) arr[i] = arr[idx[m - 1]];
    }
    return arr;
  };

  // The car was ON the road. Widen [hl, hr] (in place) wherever the session's
  // own fixes run past an edge: per ~4 m of track, the 97th percentile of the
  // fixes' lateral offset on each side + `margin`, spread over ~10 m. Fixes
  // more than `maxOver` metres past the edge are the pit lane or a real trip
  // through the grass, and never widen anything. Slow fixes (< 15 mph:
  // paddock, pit box) are ignored. Returns {inside, insideAfter, widened}
  // = shares of the fixes on the road before / after, stations widened.
  RC3D.containEnvelope = function (T, hl, hr, samples, o, opt) {
    opt = opt || {};
    var margin = opt.margin == null ? 0.5 : opt.margin, maxOver = opt.maxOver || 7;
    var n = T.dense.x.length, BIN = 4, nbin = Math.ceil(n / BIN), i;
    if (n < 10 || !o) return null;
    var idx = _lineIndex(T.dense.x, T.dense.z, 20);
    var L = [], R = [], tot = 0, inside = 0;
    for (i = 0; i < nbin; i++) { L.push([]); R.push([]); }
    for (i = 0; i < samples.length; i++) {
      var s = samples[i];
      if (!s || !_num(s.lat) || !_num(s.lon) || !(s.speed_mph > 15)) continue;
      var p = RC3D.project(s.lat, s.lon, o), h = idx.nearest(p.x, p.z, 30);
      if (!h) continue;
      var right = h.signed >= 0, edge = right ? hr[h.i] : hl[h.i], ad = Math.abs(h.signed);
      if (ad > edge + maxOver) continue;
      tot++;
      if (ad <= edge) inside++;
      (right ? R : L)[Math.floor(h.i / BIN)].push(ad);
    }
    var p97 = function (a) {
      if (a.length < 6) return 0;
      a.sort(function (x, y) { return x - y; });
      return a[Math.min(a.length - 1, Math.floor(a.length * 0.97))];
    };
    var eL = new Float64Array(n), eR = new Float64Array(n);
    for (i = 0; i < n; i++) {
      var bi = Math.floor(i / BIN);
      eL[i] = p97(L[bi]); eR[i] = p97(R[bi]);
    }
    // spread: a running max over +-5 m, then a 9 m mean (no notches)
    var spread = function (e) {
      var mx = new Float64Array(n), k, j;
      for (k = 0; k < n; k++) {
        var v = 0;
        for (j = -5; j <= 5; j++) {
          var q = k + j;
          if (T.closed) q = (q + n) % n; else if (q < 0 || q >= n) continue;
          if (e[q] > v) v = e[q];
        }
        mx[k] = v;
      }
      return RC3D.smooth(Array.prototype.slice.call(mx), 9);
    };
    var sL = spread(eL), sR = spread(eR), widened = 0;
    for (i = 0; i < n; i++) {
      var nl = sL[i] > 0 ? sL[i] + margin : 0, nr = sR[i] > 0 ? sR[i] + margin : 0, w = false;
      if (nl > hl[i]) { hl[i] = nl; w = true; }
      if (nr > hr[i]) { hr[i] = nr; w = true; }
      if (w) widened++;
    }
    var after = 0;
    for (i = 0; i < samples.length; i++) {
      var s2 = samples[i];
      if (!s2 || !_num(s2.lat) || !_num(s2.lon) || !(s2.speed_mph > 15)) continue;
      var p2 = RC3D.project(s2.lat, s2.lon, o), h2 = idx.nearest(p2.x, p2.z, 30);
      if (!h2) continue;
      var e2 = h2.signed >= 0 ? hr[h2.i] : hl[h2.i];
      if (Math.abs(h2.signed) > e2 + maxOver) continue;
      if (Math.abs(h2.signed) <= e2) after++;
    }
    return { inside: tot ? inside / tot : 1, insideAfter: tot ? after / tot : 1,
             widened: widened / n, fixes: tot };
  };

  // The inside edge of a tight corner must stay inside the corner's own
  // centre of curvature, or the ribbon folds over itself (a black bow-tie at
  // a hairpin). Clamps the INSIDE half width to 85 % of the local radius.
  RC3D.foldGuard = function (T, hl, hr) {
    var d = T.dense, n = d.x.length, i, K = 4, clamped = 0;
    for (i = 0; i < n; i++) {
      var a = i - K, b = i + K;
      if (T.closed) { a = (a + n) % n; b = b % n; }
      else { a = Math.max(0, a); b = Math.min(n - 1, b); }
      var t1 = d.tan[a], t2 = d.tan[b];
      var ds = Math.abs((T.closed && b < a) ? (d.s[n - 1] - d.s[a] + d.s[b]) : (d.s[b] - d.s[a])) || 1;
      var cr = t1[0] * t2[1] - t1[1] * t2[0], dt = Math.asin(Math.max(-1, Math.min(1, cr)));
      if (Math.abs(dt) < 1e-4) continue;
      var r = ds / Math.abs(dt), lim = 0.85 * r;
      // the tangent turns TOWARDS the inside: left perpendicular is (tz, -tx)
      var lx = t1[1], lz = -t1[0], toLeft = ((t2[0] - t1[0]) * lx + (t2[1] - t1[1]) * lz) > 0;
      if (toLeft && hl[i] > lim) { hl[i] = Math.max(1.5, lim); clamped++; }
      if (!toLeft && hr[i] > lim) { hr[i] = Math.max(1.5, lim); clamped++; }
    }
    return clamped;
  };

  // ---- prepared-track terrain grids (lidar / far) --------------------------
  // dem_hr / dem_far bins: uint16 little-endian, value = base + q * scale,
  // row-major SOUTH row first, samples are NODES (row r sits at
  // lat = S + (N - S) * r / (rows - 1)) - the same layout RC3D.demAt reads.
  RC3D.decodeDem = function (meta, buf) {
    if (!meta || !buf || !meta.bounds) return null;
    var cols = meta.cols | 0, rows = meta.rows | 0;
    // exactly the size the metadata promises: a grid from another bake (a
    // cached or half-replaced file) would otherwise decode as wrong terrain
    if (cols < 2 || rows < 2 || buf.byteLength !== cols * rows * 2) return null;
    var dv = new DataView(buf), n = cols * rows, v = new Float32Array(n), i;
    var base = Number(meta.base) || 0, sc = Number(meta.scale) || 0.05;
    for (i = 0; i < n; i++) v[i] = base + dv.getUint16(i * 2, true) * sc;
    return { cols: cols, rows: rows, bounds: meta.bounds.slice(), values: v,
             cell_m: meta.cell_m, source: meta.source };
  };

  // Several grids, FINEST FIRST: a point reads the finest grid that covers it,
  // eased into the next one over that grid's outer `blend` cells, so there is
  // no step where the 3 m lidar tile ends and the 30 m far grid takes over.
  RC3D.demStack = function (grids, blend) {
    var gs = (grids || []).filter(function (g) {
      return g && g.values && g.cols > 1 && g.rows > 1 && g.bounds;
    });
    if (!gs.length) return null;
    var bl = blend == null ? 6 : blend;
    var inside = function (g, lat, lon) {
      var b = g.bounds;
      return lat >= b[0] && lat <= b[2] && lon >= b[1] && lon <= b[3];
    };
    var at = function (k, lat, lon) {
      var g = gs[k], b = g.bounds;
      if (k === gs.length - 1) return RC3D.demAt(g, lat, lon);
      if (!inside(g, lat, lon)) return at(k + 1, lat, lon);
      var fr = (lat - b[0]) / (b[2] - b[0]), fc = (lon - b[1]) / (b[3] - b[1]);
      var e = Math.min(fr * (g.rows - 1), (1 - fr) * (g.rows - 1),
                       fc * (g.cols - 1), (1 - fc) * (g.cols - 1));
      var v = RC3D.demAt(g, lat, lon);
      if (e >= bl) return v;
      var w = e / bl;
      w = w * w * (3 - 2 * w);
      return v * w + at(k + 1, lat, lon) * (1 - w);
    };
    var fn = function (lat, lon) { return at(0, lat, lon); };
    fn.grids = gs;
    fn.covers = function (lat, lon) {
      for (var k = 0; k < gs.length; k++) if (inside(gs[k], lat, lon)) return true;
      return false;
    };
    return fn;
  };

  // Move every coordinate of a prepared asset by (dLat, dLon). The asset is
  // registered onto the session's GPS frame ONCE, and everything drawn from it
  // - centreline, imagery, land cover, terrain, OSM features - moves together,
  // so a fence can never end up on the tarmac because only the road moved.
  RC3D.shiftAsset = function (asset, dLat, dLon) {
    if (!asset || (!dLat && !dLon)) return asset;
    var out = Object.assign({}, asset);
    var sp = function (p) { var q = p.slice(); q[0] += dLat; q[1] += dLon; return q; };
    var sb = function (b) { return [b[0] + dLat, b[1] + dLon, b[2] + dLat, b[3] + dLon]; };
    if (asset.line) out.line = asset.line.map(sp);
    if (asset.bbox) out.bbox = sb(asset.bbox);
    if (asset.centre) out.centre = [asset.centre[0] + dLat, asset.centre[1] + dLon];
    if (asset.dem && asset.dem.bounds) out.dem = Object.assign({}, asset.dem, { bounds: sb(asset.dem.bounds) });
    ["dem_hr", "dem_far"].forEach(function (k) {
      if (asset[k] && asset[k].bounds) out[k] = Object.assign({}, asset[k], { bounds: sb(asset[k].bounds) });
    });
    if (asset.landcover && asset.landcover.bounds) {
      out.landcover = Object.assign({}, asset.landcover, { bounds: sb(asset.landcover.bounds) });
    }
    ["texture", "ground"].forEach(function (k) {
      if (!asset[k] || !asset[k].bounds) return;
      var tb = asset[k].bounds;
      out[k] = Object.assign({}, asset[k], { bounds: {
        south: tb.south + dLat, north: tb.north + dLat, west: tb.west + dLon, east: tb.east + dLon } });
    });
    if (asset.network && asset.network.chains) {
      out.network = Object.assign({}, asset.network, {
        chains: asset.network.chains.map(function (c) {
          var c2 = Object.assign({}, c); c2.p = (c.p || []).map(sp); return c2;
        }) });
    }
    if (asset.features) {
      var F = {};
      Object.keys(asset.features).forEach(function (k) {
        var v = asset.features[k];
        if (!Array.isArray(v)) { F[k] = v; return; }
        F[k] = v.map(function (e) {
          if (Array.isArray(e) && typeof e[0] === "number") return sp(e);   // a point
          if (Array.isArray(e)) return e.map(sp);                            // a ring
          if (e && e.p) { var c = Object.assign({}, e); c.p = e.p.map(sp); return c; }
          return e;
        });
      });
      out.features = F;
    }
    out._shift = [dLat, dLon];
    return out;
  };

  // A dense path along an arbitrary line - the prepared track's centreline or
  // the consensus of the laps - in the same shape the road, kerbs and dressing
  // are built from ({dense: {x, y, z, s, tan, total}}, total == dense.total so
  // denseToCum is the identity). closed = a circuit: the spline wraps and so do
  // the tangents, so there is no seam or kink at the start/finish.
  RC3D.linePath = function (xs, zs, ys, opt) {
    opt = opt || {};
    var n = xs.length, i, step = opt.step || 1, closed = !!opt.closed;
    while (closed && n > 4 &&
           Math.sqrt((xs[n - 1] - xs[0]) * (xs[n - 1] - xs[0]) +
                     (zs[n - 1] - zs[0]) * (zs[n - 1] - zs[0])) < 1.5) n--;
    var dx = [], dy = [], dz = [], ds = [], total = 0;
    var canCurve = typeof THREE.Vector3 === "function" &&
                   typeof THREE.CatmullRomCurve3 === "function";
    if (canCurve && n > 3) {
      var cp = [];
      for (i = 0; i < n; i++) cp.push(new THREE.Vector3(xs[i], ys ? ys[i] : 0, zs[i]));
      var curve = new THREE.CatmullRomCurve3(cp, closed, "centripetal", 0.5);
      curve.arcLengthDivisions = Math.min(400000, Math.max(400, cp.length * 4));
      var L = curve.getLength();
      var count = Math.max(8, Math.min(200000, Math.ceil(L / step)));
      var pts = curve.getSpacedPoints(count);
      for (i = 0; i < pts.length; i++) {
        if (i) {
          var ax = pts[i].x - pts[i - 1].x, ay = pts[i].y - pts[i - 1].y, az = pts[i].z - pts[i - 1].z;
          total += Math.sqrt(ax * ax + ay * ay + az * az);
        }
        dx.push(pts[i].x); dy.push(pts[i].y); dz.push(pts[i].z); ds.push(total);
      }
    } else {
      for (i = 0; i < n; i++) {
        if (i) total += Math.sqrt((xs[i] - xs[i - 1]) * (xs[i] - xs[i - 1]) +
                                  (zs[i] - zs[i - 1]) * (zs[i] - zs[i - 1]));
        dx.push(xs[i]); dy.push(ys ? ys[i] : 0); dz.push(zs[i]); ds.push(total);
      }
      if (closed && n > 2) {
        total += Math.sqrt((xs[0] - xs[n - 1]) * (xs[0] - xs[n - 1]) + (zs[0] - zs[n - 1]) * (zs[0] - zs[n - 1]));
        dx.push(xs[0]); dy.push(ys ? ys[0] : 0); dz.push(zs[0]); ds.push(total);
      }
    }
    var m = dx.length, tan = new Array(m);
    for (i = 0; i < m; i++) {
      var a = i - 1, b = i + 1;
      if (closed && m > 3) {             // last point == first point
        if (a < 0) a = m - 2;
        if (b > m - 1) b = 1;
      } else {
        a = Math.max(0, a); b = Math.min(m - 1, b);
      }
      var tx = dx[b] - dx[a], tz = dz[b] - dz[a], Lt = Math.sqrt(tx * tx + tz * tz) || 1;
      tan[i] = [tx / Lt, tz / Lt];
    }
    return { o: opt.o || null, closed: closed, total: total, n: m,
             x: dx, y: dy, z: dz, cum: ds.slice(), speed: [], accel: [], t: [],
             dense: { x: dx, y: dy, z: dz, s: ds, tan: tan, total: total } };
  };

  // A lap's line runs a few metres past its own start (S/F is crossed between
  // two fixes; a consensus window is a lap and a bit). Closed as a loop, that
  // overlap folds back on itself: a 180-degree cusp at the seam that reads as
  // a hairpin to everything downstream. Cut the tail where it passes the start
  // (and the head where the tail starts, if the line begins early).
  RC3D.trimLoop = function (x, z, opt) {
    opt = opt || {};
    var n = x.length, win = opt.win_m || 150, i, k;
    if (n < 10) return { x: x.slice(), z: z.slice() };
    var cum = [0];
    for (i = 1; i < n; i++) cum.push(cum[i - 1] + Math.hypot(x[i] - x[i - 1], z[i] - z[i - 1]));
    var tot = cum[n - 1];
    if (tot < 4 * win) return { x: x.slice(), z: z.slice() };
    var dir = function (j) {
      var a = Math.max(0, j - 2), b = Math.min(n - 1, j + 2);
      var tx = x[b] - x[a], tz = z[b] - z[a], L = Math.hypot(tx, tz) || 1;
      return [tx / L, tz / L];
    };
    // the tail point nearest the start (same direction of travel)
    var d0 = dir(0), bestB = n - 1, bestD = Math.hypot(x[n - 1] - x[0], z[n - 1] - z[0]);
    for (k = n - 1; k > 0 && cum[k] > tot - win; k--) {
      var dk = dir(k);
      if (dk[0] * d0[0] + dk[1] * d0[1] < 0.7) continue;
      var dd = Math.hypot(x[k] - x[0], z[k] - z[0]);
      if (dd < bestD) { bestD = dd; bestB = k; }
    }
    // a tail that stops short of the start can leave the head overlapping
    var dn = dir(bestB), bestA = 0, bestD2 = Math.hypot(x[bestB] - x[0], z[bestB] - z[0]);
    for (k = 0; k < n && cum[k] < win; k++) {
      var dk2 = dir(k);
      if (dk2[0] * dn[0] + dk2[1] * dn[1] < 0.7) continue;
      var dd2 = Math.hypot(x[bestB] - x[k], z[bestB] - z[k]);
      if (dd2 < bestD2 - 0.01) { bestD2 = dd2; bestA = k; }
    }
    // drop the joining point itself when it sits on top of the start
    var end = bestB;
    if (end > bestA + 1 && Math.hypot(x[end] - x[bestA], z[end] - z[bestA]) < 0.5) end--;
    return { x: x.slice(bestA, end + 1), z: z.slice(bestA, end + 1), cutHead: bestA, cutTail: n - 1 - end };
  };

  // A track shape from what was DRIVEN (cx, cz: the laps' consensus), pulled
  // onto the prepared line (lx, lz: OpenStreetMap re-centred on the imagery)
  // wherever the two are the same piece of road - within `near` metres and
  // running the same way. A link the prepared layout does not have (the short
  // course at Watkins Glen, a chicane bypass) keeps the consensus shape; the
  // pull fades in and out over ~`blend` metres so there is no kink where it
  // starts. Returns {x, z, matched} (matched = share of stations pulled).
  RC3D.blendOnto = function (cx, cz, lx, lz, opt) {
    opt = opt || {};
    var near = opt.near || 8, blendM = opt.blend || 40, n = cx.length, i;
    // opt.index: a ready index (e.g. RC3D.multiIndex over a whole network).
    // OSM ways run either way round, so by default a parallel line matches in
    // both directions; opt.directed demands the same direction of travel.
    var idx = opt.index || RC3D.lineIndex(lx, lz, Math.max(near, 10));
    var directed = !!opt.directed, src = new Array(n);
    var ox = new Float64Array(n), oz = new Float64Array(n), w = new Float64Array(n), hit = 0;
    for (i = 0; i < n; i++) {
      var a = Math.max(0, i - 1), b = Math.min(n - 1, i + 1);
      var tx = cx[b] - cx[a], tz = cz[b] - cz[a], L = Math.sqrt(tx * tx + tz * tz) || 1;
      var h = idx.nearest(cx[i], cz[i], near);
      if (!h) continue;
      // the line's own direction at that point is (nz, -nx) (lineIndex's
      // normal is the direction turned left)
      var dot = (h.nz * tx - h.nx * tz) / L;
      if ((directed ? dot : Math.abs(dot)) < 0.9) continue;
      ox[i] = h.px - cx[i]; oz[i] = h.pz - cz[i]; w[i] = 1; hit++;
      src[i] = h;
    }
    // stations are ~1 m apart: a moving window of `blend` stations
    var half = Math.max(1, Math.round(blendM / 2));
    var pre = function (arr) { var o = new Float64Array(n + 1); for (var k = 0; k < n; k++) o[k + 1] = o[k] + arr[k]; return o; };
    var pw = pre(w), px = pre(Array.prototype.map.call(w, function (v, k) { return v * ox[k]; })),
        pz = pre(Array.prototype.map.call(w, function (v, k) { return v * oz[k]; }));
    var outX = new Array(n), outZ = new Array(n);
    for (i = 0; i < n; i++) {
      var a0 = Math.max(0, i - half), b0 = Math.min(n, i + half + 1), cnt = b0 - a0;
      var ww = (pw[b0] - pw[a0]);
      var k2 = ww / cnt;                           // share matched in the window
      var f = Math.max(0, Math.min(1, (k2 - 0.25) / 0.5));
      f = f * f * (3 - 2 * f);
      var mx = ww > 0 ? (px[b0] - px[a0]) / ww : 0, mz = ww > 0 ? (pz[b0] - pz[a0]) / ww : 0;
      // inside a matched stretch use the station's own exact offset
      if (w[i] && k2 > 0.9) { mx = ox[i]; mz = oz[i]; }
      outX[i] = cx[i] + mx * f; outZ[i] = cz[i] + mz * f;
    }
    return { x: outX, z: outZ, matched: n ? hit / n : 0, src: src };
  };

  // ---- the facility's whole track network (asset.network, every layout) ----
  // OSM raceway chains re-centred on the imagery, with measured half widths
  // per point ("left" = left of the direction the chain's points run). Area
  // polygons are skipped. -> [{x, z, hl, hr, kind, closed, names, len}]
  RC3D.networkLines = function (asset, o) {
    var net = asset && asset.network, out = [];
    if (!net || !net.chains) return out;
    net.chains.forEach(function (c) {
      if (!c || c.kind === "area" || !c.p || c.p.length < 2) return;
      var x = [], z = [], hl = [], hr = [], k, len = 0, w0 = (c.w > 0 ? c.w : 9) / 2;
      for (k = 0; k < c.p.length; k++) {
        var q = RC3D.project(c.p[k][0], c.p[k][1], o);
        if (k) len += Math.hypot(q.x - x[k - 1], q.z - z[k - 1]);
        x.push(q.x); z.push(q.z);
        var h = c.hw && c.hw[k];
        hl.push(h && h[0] > 0.5 ? h[0] : w0);
        hr.push(h && h[1] > 0.5 ? h[1] : w0);
      }
      out.push({ x: x, z: z, hl: hl, hr: hr, kind: c.kind || "circuit", closed: !!c.closed,
                 names: c.names || [], len: len });
    });
    return out;
  };

  // nearest point over SEVERAL polylines. opt.bias(line) adds metres to a
  // line's distance (a pit lane loses a tie with the circuit beside it);
  // opt.skip(line) leaves a line out. Result as lineIndex's, plus `line`
  // (its index) and `eff` (the biased distance).
  RC3D.multiIndex = function (lines, cell, opt) {
    opt = opt || {};
    cell = cell || 20;
    var parts = lines.map(function (L, k) {
      return (opt.skip && opt.skip(L, k)) ? null : RC3D.lineIndex(L.x, L.z, cell);
    });
    var bias = lines.map(function (L, k) { return opt.bias ? (opt.bias(L, k) || 0) : 0; });
    return {
      nearest: function (x, z, maxD) {
        var best = null, k;
        for (k = 0; k < parts.length; k++) {
          if (!parts[k] || bias[k] >= maxD) continue;
          var h = parts[k].nearest(x, z, maxD - bias[k]);
          if (!h) continue;
          var eff = h.d + bias[k];
          if (!best || eff < best.eff) { h.line = k; h.eff = eff; best = h; }
        }
        return best;
      }
    };
  };

  // [left, right] half widths of network line h.line at hit h, as seen
  // travelling along (tx, tz): a chain drawn the other way round swaps sides
  RC3D.networkHalfAt = function (lines, h, tx, tz) {
    var L = lines[h.line], a = h.seg, b = Math.min(L.x.length - 1, a + 1), f = h.f || 0;
    var hl = L.hl[a] + (L.hl[b] - L.hl[a]) * f, hr = L.hr[a] + (L.hr[b] - L.hr[a]) * f;
    var lx = L.x[b] - L.x[a], lz = L.z[b] - L.z[a];
    return (lx * tx + lz * tz) >= 0 ? [hl, hr] : [hr, hl];
  };

  // Burn OpenStreetMap areas into a land-cover grid (rows NORTH first, codes
  // p paved / g grass / w woods / o other). With imagery land cover the
  // imagery wins where it is sure it is paved (OSM woods can be years out of
  // date); without it the grid is made from OSM alone over `opt.bounds`
  // [S, W, N, E], grass everywhere else.
  RC3D.burnLandcover = function (lc, features, opt) {
    opt = opt || {};
    if (!features) return lc;
    var base = lc;
    if (!base) {
      var B = opt.bounds;
      if (!B) return null;
      var latMid = (B[0] + B[2]) / 2, M = 111320;
      var wM = (B[3] - B[1]) * M * Math.cos(latMid * Math.PI / 180), hM = (B[2] - B[0]) * M;
      var cell = Math.max(opt.cell || 4, Math.sqrt(wM * hM / (opt.maxCells || 1500000)));
      var cols = Math.max(2, Math.round(wM / cell)), rows = Math.max(2, Math.round(hM / cell));
      base = { cols: cols, rows: rows, bounds: B.slice(), cell_m: cell, _codes: null, osmOnly: true };
      var g = new Uint8Array(cols * rows);
      g.fill(103);                                   // 'g'
      base._grid = g;
    }
    var cols2 = base.cols, rows2 = base.rows, b = base.bounds;
    var grid = base._grid;
    if (!grid) {
      grid = new Uint8Array(cols2 * rows2);
      var codes = base._codes || "";
      for (var q = 0; q < grid.length; q++) grid[q] = q < codes.length ? codes.charCodeAt(q) : 103;
    } else {
      grid = new Uint8Array(grid);
    }
    var fromImagery = !base.osmOnly;
    var fill = function (ring, code) {
      if (!ring || ring.length < 3) return 0;
      var xs = [], ys = [], k, r, c, nb = 0;
      var minR = Infinity, maxR = -Infinity;
      for (k = 0; k < ring.length; k++) {
        xs.push((ring[k][1] - b[1]) / (b[3] - b[1]) * cols2);
        var yy = (b[2] - ring[k][0]) / (b[2] - b[0]) * rows2;
        ys.push(yy);
        if (yy < minR) minR = yy;
        if (yy > maxR) maxR = yy;
      }
      var r0 = Math.max(0, Math.floor(minR)), r1 = Math.min(rows2 - 1, Math.ceil(maxR));
      for (r = r0; r <= r1; r++) {
        var cy = r + 0.5, cross = [];
        for (k = 0; k < xs.length; k++) {
          var k2 = (k + 1) % xs.length;
          var y0 = ys[k], y1 = ys[k2];
          if ((y0 <= cy && y1 > cy) || (y1 <= cy && y0 > cy)) {
            cross.push(xs[k] + (cy - y0) / (y1 - y0) * (xs[k2] - xs[k]));
          }
        }
        cross.sort(function (a2, b2) { return a2 - b2; });
        for (k = 0; k + 1 < cross.length; k += 2) {
          var c0 = Math.max(0, Math.ceil(cross[k] - 0.5)), c1 = Math.min(cols2 - 1, Math.floor(cross[k + 1] - 0.5));
          for (c = c0; c <= c1; c++) {
            var at = r * cols2 + c, cur = grid[at];
            if (fromImagery && code === 119 && cur === 112) continue;   // imagery says paved
            grid[at] = code;
            nb++;
          }
        }
      }
      return nb;
    };
    var F = features, cnt = 0;
    if (!fromImagery) {
      (F.farmland || []).forEach(function (rg) { cnt += fill(rg, 103); });
      (F.grass || []).forEach(function (rg) { cnt += fill(rg, 103); });
    }
    (F.woods || []).forEach(function (rg) { cnt += fill(rg, 119); });
    (F.scrub || []).forEach(function (rg) { cnt += fill(rg, 119); });
    (F.parking || []).forEach(function (rg) { cnt += fill(rg, 112); });
    (F.paved || []).forEach(function (rg) { cnt += fill(rg, 112); });
    (F.water || []).forEach(function (rg) { cnt += fill(rg, 111); });
    var s = "";
    var CH = 8192;
    for (var i0 = 0; i0 < grid.length; i0 += CH) {
      s += String.fromCharCode.apply(null, grid.subarray(i0, Math.min(grid.length, i0 + CH)));
    }
    return { cols: cols2, rows: rows2, bounds: b.slice(), cell_m: base.cell_m,
             _codes: s, _grid: grid, osmOnly: !fromImagery, burned: cnt };
  };

  // Resample a polyline every `step` metres and cut it wherever it comes
  // within `clear(i)` metres of the circuit: an OSM service road that joins the
  // track, a fence along the edge, a pit lane - none of them may draw over the
  // tarmac. near(x, z) -> {d, i} is the circuit's distance field.
  RC3D.cutNearRoad = function (pts, step, near, clear) {
    var out = [], cur = [], k, j;
    var push = function (x, z) {
      var q = near ? near(x, z) : null;
      if (q && q.d < clear(q.i)) {
        if (cur.length > 1) out.push(cur);
        cur = [];
        return;
      }
      cur.push([x, z]);
    };
    for (k = 0; k + 1 < pts.length; k++) {
      var ax = pts[k][0], az = pts[k][1], bx = pts[k + 1][0], bz = pts[k + 1][1];
      var L = Math.sqrt((bx - ax) * (bx - ax) + (bz - az) * (bz - az));
      var nseg = Math.max(1, Math.ceil(L / step));
      for (j = 0; j < nseg; j++) push(ax + (bx - ax) * j / nseg, az + (bz - az) * j / nseg);
    }
    if (pts.length) push(pts[pts.length - 1][0], pts[pts.length - 1][1]);
    if (cur.length > 1) out.push(cur);
    return out;
  };

  // Brake boards where the DATA says a corner needs braking. Corners come from
  // the track's own geometry (stable lap to lap); `zones` are the braking
  // zones of every complete lap mapped onto the track ({s: track arc length of
  // the slowest point, entry_mph, lap}). A corner gets boards when at least
  // `share` of the laps brake into it; they count back from its turn-in at
  // 100 m steps, as many as the straight before it allows (5..1 only on a
  // long, fast approach). Without laps: the geometry tiers (brakeMarkers).
  RC3D.boardsFromData = function (T, corners, zones, opt) {
    opt = opt || {};
    var nLaps = Math.max(1, opt.laps || 1), share = opt.share == null ? 0.4 : opt.share;
    var total = T.dense.total, closed = !!T.closed, out = [], c, k;
    var rel = function (s, ref) {           // s - ref, wrapped on a circuit
      var v = s - ref;
      if (closed && total > 0) {
        while (v > total / 2) v -= total;
        while (v < -total / 2) v += total;
      }
      return v;
    };
    for (c = 0; c < corners.length; c++) {
      var C = corners[c], laps = {}, entry = 0, nz = 0;
      for (k = 0; k < zones.length; k++) {
        var z = zones[k];
        var r0 = rel(z.s, C.s0), r1 = rel(z.s, C.s1);
        if (r0 >= -70 && r1 <= 40) {
          laps[z.lap] = true;
          entry = Math.max(entry, z.entry_mph || 0);
          nz++;
        }
      }
      var nl = Object.keys(laps).length;
      if (!nz || nl / nLaps < share) continue;
      var prev = corners[(c - 1 + corners.length) % corners.length];
      var straight;
      if (corners.length < 2) straight = closed ? total - (C.s1 - C.s0) : C.s0;
      else if (c === 0 && !closed) straight = C.s0;
      else straight = rel(C.s0, prev.s1);
      if (!(straight > 0)) straight = closed ? straight + total : 0;
      var dists;
      if (straight >= 520 && entry >= 100) dists = [500, 400, 300, 200, 100];
      else if (straight >= 320) dists = [300, 200, 100];
      else if (straight >= 220) dists = [200, 100];
      else if (straight >= 130) dists = [100];
      else continue;
      for (k = 0; k < dists.length; k++) {
        var s = C.s0 - dists[k];
        if (closed) { while (s < 0) s += total; }
        else if (s < 5) continue;
        out.push({ s: s, m: dists[k], label: String(Math.round(dists[k] / 100)),
                   side: -C.dir, deg: Math.abs(C.deg), corner: c, laps: nl, entry_mph: entry });
      }
    }
    return out;
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
                 gantry: null, ground: null, signs: null, car: null,
                 barriers: null, trees: null, labels: null, wash: null,
                 line: null, edges: null, buildings: null, water: null,
                 roads: null, fences: null, events: null, network: null };
  var ASSET = null, TEX = null, assetSample = null;   // prepared-track data
  var BOOTED = false;                                 // first world build done
  var look = { yaw: 0, pitch: 0 };
  var view = "chase";                                     // "chase" | "plan"
  var planZoom = 1;                                       // wheel zoom in plan view
  var realWidth = null;                                   // metres, from the asset
  var CORNERS = [];                                       // detected corners
  var TRACK_SEED = "track";                               // stable per-track scatter
  var opts = { smooth: 5, eye: 1.15, road: 12, speedColour: true, markers: true,
               ghost: false, ground: "sim", brakes: true, dressing: true, network: true };

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

  // ---- sky + light ----------------------------------------------------------
  // One sun, used three times so they agree: the sky gradient's glow, the
  // directional light, and the shadows it casts.
  var SUN_DIR = null, sun = null, hemi = null;
  var HAZE = 0xC4D3DF;                   // horizon haze = fog colour

  function skyTexture() {
    var W = 2048, H = 1024;
    var c = document.createElement("canvas");
    c.width = W; c.height = H;
    var g = null;
    try { g = c.getContext("2d"); } catch (e) { g = null; }
    if (!g) return null;
    // zenith -> horizon (row H/2) -> below the horizon is all haze, so the
    // fogged far terrain meets the sky without a seam
    var grad = g.createLinearGradient(0, 0, 0, H);
    grad.addColorStop(0.00, "#2B63A8");
    grad.addColorStop(0.22, "#3F7DC2");
    grad.addColorStop(0.40, "#86AFD6");
    grad.addColorStop(0.485, "#BCD0E0");
    grad.addColorStop(0.50, "#C4D3DF");
    grad.addColorStop(1.00, "#B7C6D2");
    g.fillStyle = grad;
    g.fillRect(0, 0, W, H);
    // sun glow, at the same direction as the light
    var su = Math.atan2(SUN_DIR.z, SUN_DIR.x) / (2 * Math.PI) + 0.5;
    var sv = Math.asin(SUN_DIR.y) / Math.PI + 0.5;
    var sx = su * W, sy = (1 - sv) * H;
    [[sx, 1], [sx - W, 1], [sx + W, 1]].forEach(function (p) {
      var rg = g.createRadialGradient(p[0], sy, 0, p[0], sy, 260);
      rg.addColorStop(0, "rgba(255,250,235,0.95)");
      rg.addColorStop(0.06, "rgba(255,246,220,0.75)");
      rg.addColorStop(0.3, "rgba(255,240,210,0.18)");
      rg.addColorStop(1, "rgba(255,240,210,0)");
      g.fillStyle = rg;
      g.fillRect(p[0] - 260, sy - 260, 520, 520);
    });
    // soft fair-weather clouds above the horizon, flatter towards it
    var rnd = seededRandom("clouds");
    for (var k = 0; k < 70; k++) {
      var cx = rnd() * W, cy = H * (0.16 + rnd() * 0.31);
      var near = (cy / H - 0.16) / 0.31;                // 0 high .. 1 at horizon
      var rx = 40 + rnd() * 120 * (1 - near * 0.5), ry = rx * (0.28 - near * 0.16);
      for (var p2 = 0; p2 < 7; p2++) {
        var ox = (rnd() - 0.5) * rx * 1.6, oy = (rnd() - 0.5) * ry * 0.9;
        var rr = rx * (0.35 + rnd() * 0.45);
        [0, -W, W].forEach(function (wrap) {
          var px = cx + ox + wrap, py = cy + oy;
          if (px < -rr * 2 || px > W + rr * 2) return;
          g.save();
          g.translate(px, py);
          g.scale(1, ry / rx);
          var cg = g.createRadialGradient(0, 0, 0, 0, 0, rr);
          cg.addColorStop(0, "rgba(255,255,255," + (0.36 - near * 0.14) + ")");
          cg.addColorStop(0.6, "rgba(250,252,255," + (0.16 - near * 0.06) + ")");
          cg.addColorStop(1, "rgba(250,252,255,0)");
          g.fillStyle = cg;
          g.beginPath(); g.arc(0, 0, rr, 0, Math.PI * 2); g.fill();
          g.restore();
        });
      }
    }
    var tex = new THREE.CanvasTexture(c);
    tex.mapping = THREE.EquirectangularReflectionMapping;
    tex.colorSpace = THREE.SRGBColorSpace;
    return tex;
  }

  function buildScene() {
    scene = new THREE.Scene();
    SUN_DIR = new THREE.Vector3(-0.55, 0.62, 0.42).normalize();
    var sky = skyTexture();
    scene.background = sky || new THREE.Color(HAZE);
    scene.fog = new THREE.Fog(HAZE, 350, 3400);
    camera = new THREE.PerspectiveCamera(68, 1, 0.4, 9000);
    renderer.toneMapping = THREE.ACESFilmicToneMapping;
    renderer.toneMappingExposure = 1.05;
    renderer.shadowMap.enabled = true;
    renderer.shadowMap.type = THREE.PCFSoftShadowMap;
    hemi = new THREE.HemisphereLight(0xD6E6F5, 0x5E6B44, 1.05);
    scene.add(hemi);
    sun = new THREE.DirectionalLight(0xFFF2DE, 3.1);
    sun.castShadow = true;
    sun.shadow.mapSize.set(2048, 2048);
    sun.shadow.bias = -0.0004;
    sun.shadow.normalBias = 0.04;
    var sc = sun.shadow.camera;
    sc.left = -95; sc.right = 95; sc.top = 95; sc.bottom = -95; sc.near = 1; sc.far = 900;
    sc.updateProjectionMatrix();
    scene.add(sun);
    scene.add(sun.target);
  }

  // The shadow map follows the camera: a 190 m box just ahead of the car in
  // the driving view, the whole circuit (softer) from above.
  function placeSun(focus, span) {
    if (!sun) return;
    var sc = sun.shadow.camera, half = Math.max(95, span / 2);
    if (sc.right !== half) {
      sc.left = -half; sc.right = half; sc.top = half; sc.bottom = -half;
      sc.far = Math.max(900, half * 4);
      sc.updateProjectionMatrix();
    }
    sun.target.position.copy(focus);
    sun.position.copy(focus).addScaledVector(SUN_DIR, Math.max(400, half * 2));
    sun.target.updateMatrixWorld();
  }

  function disposeMeshes() {
    Object.keys(meshes).forEach(function (k) {
      var m = meshes[k];
      if (!m) return;
      scene.remove(m);
      if (m.traverse) m.traverse(function (o) {
        if (o.geometry && !(o.geometry.userData && o.geometry.userData.shared)) o.geometry.dispose();
        if (o.material && o.material.userData && o.material.userData.owned) {
          o.material.userData.owned.forEach(function (t) { if (t) t.dispose(); });
        }
        if (o.material) {
          if (Array.isArray(o.material)) o.material.forEach(function (mm) { mm.dispose(); });
          else o.material.dispose();
        }
        if (o.dispose && o.isInstancedMesh) o.dispose();
      });
      meshes[k] = null;
    });
  }

  /* ---- procedural textures ------------------------------------------------
     Simulated surfaces instead of a photographic drape: real circuits are
     asphalt + painted kerbs + grass + gravel + Armco + trees, and every one of
     those is sharper drawn from noise than from a satellite pixel that is 1 m
     wide at best. Deterministic (seeded) and TILEABLE: every blob is drawn at
     its wrapped positions too, so the repeat has no seam. */
  function noiseCanvas(px, base, grain, blob, seed, opt) {
    opt = opt || {};
    var c = document.createElement("canvas");
    c.width = c.height = px;
    var g = null;
    try { g = c.getContext("2d"); } catch (e) { g = null; }
    if (!g) return null;
    var rnd = (function (s) {
      return function () { s = (s * 1103515245 + 12345) & 0x7fffffff; return s / 0x7fffffff; };
    })(seed || 7);
    g.fillStyle = "rgb(" + base.join(",") + ")";
    g.fillRect(0, 0, px, px);
    var i, spread = opt.blobSpread || 22, gs = opt.grainSpread || 46;
    var wrapDraw = function (x, y, r, fn) {
      for (var a = -1; a <= 1; a++) for (var b = -1; b <= 1; b++) {
        var xx = x + a * px, yy = y + b * px;
        if (xx + r < 0 || xx - r > px || yy + r < 0 || yy - r > px) continue;
        fn(xx, yy);
      }
    };
    for (i = 0; i < blob; i++) {                     // soft patches
      var x = rnd() * px, y = rnd() * px, r = px * (0.04 + rnd() * 0.14);
      var d = Math.round((rnd() - 0.5) * spread);
      var tint = opt.tint ? opt.tint(rnd) : [0, 0, 0];
      wrapDraw(x, y, r, function (xx, yy) {
        var grd = g.createRadialGradient(xx, yy, 0, xx, yy, r);
        grd.addColorStop(0, "rgba(" + (base[0] + d + tint[0]) + "," + (base[1] + d + tint[1]) +
                            "," + (base[2] + d + tint[2]) + ",0.5)");
        grd.addColorStop(1, "rgba(" + (base[0] + d) + "," + (base[1] + d) + "," + (base[2] + d) + ",0)");
        g.fillStyle = grd;
        g.beginPath(); g.arc(xx, yy, r, 0, Math.PI * 2); g.fill();
      });
    }
    var gw = opt.grainW || 1, gh = opt.grainH || 1;
    for (i = 0; i < grain; i++) {                    // aggregate / blades
      var x2 = rnd() * px, y2 = rnd() * px;
      var v = Math.round((rnd() - 0.5) * gs);
      var t2 = opt.grainTint ? opt.grainTint(rnd) : [0, 0, 0];
      g.fillStyle = "rgba(" + Math.max(0, base[0] + v + t2[0]) + "," +
                    Math.max(0, base[1] + v + t2[1]) + "," +
                    Math.max(0, base[2] + v + t2[2]) + "," + (opt.grainAlpha || 0.55) + ")";
      g.fillRect(x2, y2, gw, gh);
    }
    return c;
  }

  function finishTex(c, srgb) {
    var t = new THREE.CanvasTexture(c);
    t.wrapS = t.wrapT = THREE.RepeatWrapping;
    if (srgb !== false) t.colorSpace = THREE.SRGBColorSpace;
    try { t.anisotropy = renderer.capabilities.getMaxAnisotropy(); } catch (e) {}
    return t;
  }

  function surfaceTexture(kind) {
    var c = null;
    if (kind === "asphalt") {
      c = noiseCanvas(512, [62, 63, 66], 26000, 40, 11,
                      { blobSpread: 14, grainSpread: 40, grainAlpha: 0.5 });
    } else if (kind === "grass") {
      c = noiseCanvas(512, [84, 116, 54], 34000, 46, 23,
                      { blobSpread: 26, grainSpread: 50, grainW: 1, grainH: 3,
                        tint: function (r) { return [Math.round((r() - 0.5) * 18), 0, -6]; },
                        grainTint: function (r) { return [Math.round((r() - 0.4) * 16), 8, 0]; } });
    } else if (kind === "gravel") {
      c = noiseCanvas(512, [168, 154, 128], 42000, 20, 31,
                      { blobSpread: 18, grainSpread: 70, grainW: 2, grainH: 2, grainAlpha: 0.7 });
    } else if (kind === "dirt") {
      c = noiseCanvas(256, [132, 116, 88], 9000, 24, 37, { blobSpread: 26 });
    } else if (kind === "macro") {
      c = noiseCanvas(256, [128, 128, 128], 0, 160, 53, { blobSpread: 150 });
      return c ? finishTex(c, false) : null;
    } else if (kind === "armco") {
      c = document.createElement("canvas");
      c.width = 64; c.height = 64;
      var g = c.getContext("2d");
      // a W-beam: two bright ridges with a shadowed channel between
      var grd = g.createLinearGradient(0, 0, 0, 64);
      grd.addColorStop(0.00, "#8C9298"); grd.addColorStop(0.18, "#DDE2E6");
      grd.addColorStop(0.34, "#9AA0A6"); grd.addColorStop(0.50, "#5E646A");
      grd.addColorStop(0.66, "#9AA0A6"); grd.addColorStop(0.82, "#DDE2E6");
      grd.addColorStop(1.00, "#8C9298");
      g.fillStyle = grd; g.fillRect(0, 0, 64, 64);
      g.fillStyle = "rgba(60,64,70,0.55)";
      g.fillRect(0, 0, 2, 64);                     // the joint between rails
    } else if (kind === "kerb") {
      c = document.createElement("canvas");
      c.width = 64; c.height = 16;
      var k = c.getContext("2d");
      k.fillStyle = "#D8262A"; k.fillRect(0, 0, 32, 16);
      k.fillStyle = "#F2F2EE"; k.fillRect(32, 0, 32, 16);
      k.fillStyle = "rgba(0,0,0,0.2)"; k.fillRect(0, 0, 64, 3);     // outer lip shade (v=1)
    }
    if (!c) return null;
    return finishTex(c);
  }

  var TEXS = null;
  function surfaces() {
    if (!TEXS) {
      TEXS = {};
      ["asphalt", "grass", "gravel", "dirt", "macro", "armco", "kerb"].forEach(function (k) {
        TEXS[k] = surfaceTexture(k);
      });
    }
    return TEXS;
  }

  function seededRandom(seedStr) {
    var h = 2166136261, i;
    for (i = 0; i < seedStr.length; i++) {
      h ^= seedStr.charCodeAt(i);
      h = (h * 16777619) >>> 0;
    }
    return function () {
      h = (h * 1664525 + 1013904223) >>> 0;
      return h / 4294967296;
    };
  }

  /* ---- the world model for one rebuild ----------------------------------
     WORLD = the road distance field, half-widths, terrain and ground height,
     built once per rebuild and shared by every piece of trackside dressing, so
     the grass, the gravel, the barriers and the trees all agree on where the
     road is and how high the ground is. */
  var WORLD = null, LC = null;

  function decodeLandcover(asset) {
    if (!asset || !asset.landcover || !asset.landcover.rle) return null;
    var lc = asset.landcover;
    if (lc._codes) return lc;
    try {
      var codes = RC3D.unrle(lc.rle);
      if (codes.length !== lc.cols * lc.rows) return null;
      lc._codes = codes;
      return lc;
    } catch (e) { return null; }
  }

  function buildWorld(base, half) {
    var d = base.dense, n = d.x.length;
    var hwDefault = (realWidth || opts.road) / 2;
    var hw = function (i) {
      if (!half) return hwDefault;
      var a = half[0][i], b = half[1][i];
      a = (a > 1) ? a : hwDefault; b = (b > 1) ? b : hwDefault;
      return Math.max(a, b);
    };
    var maxHalf = hwDefault;
    if (half) for (var i = 0; i < n; i += 5) maxHalf = Math.max(maxHalf, hw(i));
    var box = RC3D.denseBox(d, 760);
    // a road drive or a 20 km session must not allocate a 4 m raster over the
    // county: cells (and ground steps) grow with the extent
    var ext = Math.max(box.maxX - box.minX, box.maxZ - box.minZ);
    var field = RC3D.roadField(d, box, Math.max(4, Math.ceil(ext / 1800)));
    var terrain;
    if (DEMFN && demCoversTrack()) {
      var ref = YREF;
      terrain = function (x, z) {
        var ll = RC3D.localToLatLon(x, z, PATH.o);
        return DEMFN(ll[0], ll[1]) - ref;
      };
    } else {
      terrain = RC3D.pathTerrain(d);
    }
    var trackGround = RC3D.groundField(d, field, { halfWidth: hw, terrain: terrain,
                                                  maxHalf: maxHalf, drop: 0.1, shoulder: 4 });
    // every other layout of the facility, seated on that ground and flattening
    // it in turn
    var net = null;
    if (opts.network && NET.length) {
      try { net = buildNet({ d: d, field: field, hw: hw }, trackGround); }
      catch (e) { console.warn("[track3d] network:", e && e.message ? e.message : e); net = null; }
    }
    var groundY = net ? net.groundY : trackGround;
    // fine cells across the circuit you drove, twice as coarse over the rest
    // of the facility, then growing out to the fogged horizon
    var tb = RC3D.denseBox(d, 320), gb = tb;
    if (net) {
      gb = { minX: Math.min(tb.minX, net.box.minX - 120), maxX: Math.max(tb.maxX, net.box.maxX + 120),
             minZ: Math.min(tb.minZ, net.box.minZ - 120), maxZ: Math.max(tb.maxZ, net.box.maxZ + 120) };
    }
    var gstep = Math.max(5, ext / 560);
    var xs = axisNodes(tb.minX, tb.maxX, gstep, 6000, gb.minX, gb.maxX, gstep * 2);
    var zs = axisNodes(tb.minZ, tb.maxZ, gstep, 6000, gb.minZ, gb.maxZ, gstep * 2);
    var nodeY = new Map();
    var nodeAt = function (c, r) {
      var k = r * xs.length + c, v = nodeY.get(k);
      if (v === undefined) { v = groundY(xs[c], zs[r]); nodeY.set(k, v); }
      return v;
    };
    var find = function (a, v) {
      var lo = 0, hi = a.length - 2, mid;
      if (v <= a[0]) return 0;
      if (v >= a[a.length - 1]) return a.length - 2;
      while (lo < hi) { mid = (lo + hi + 1) >> 1; if (a[mid] <= v) lo = mid; else hi = mid - 1; }
      return lo;
    };
    // the height of the ground MESH (its triangles, its diagonal), not the
    // ideal field: gravel, posts, trees and boards stand on what is drawn
    var surfaceY = function (x, z) {
      var c = find(xs, x), r = find(zs, z);
      var u = (x - xs[c]) / (xs[c + 1] - xs[c]), v = (z - zs[r]) / (zs[r + 1] - zs[r]);
      u = Math.max(0, Math.min(1, u)); v = Math.max(0, Math.min(1, v));
      var y1 = nodeAt(c + 1, r), y2 = nodeAt(c, r + 1);
      if (u + v <= 1) {
        var y0 = nodeAt(c, r);
        return y0 + u * (y1 - y0) + v * (y2 - y0);
      }
      var y3 = nodeAt(c + 1, r + 1);
      return y3 + (1 - u) * (y2 - y3) + (1 - v) * (y1 - y3);
    };
    // distance from (x, z) to the nearest road EDGE of any layout (negative =
    // on the tarmac): what OSM roads, fences and trees keep clear of
    var edgeDist = function (x, z) {
      var best, q, a = field.approx(x, z);
      if (a < maxHalf + 60) { q = field.nearest(x, z); best = q ? q.d - hw(q.i) : a - maxHalf; }
      else best = a - maxHalf;
      if (net) {
        var a2 = net.field.approx(x, z);
        if (a2 < net.maxHalf + 60) {
          q = net.field.nearest(x, z);
          if (q) best = Math.min(best, q.d - net.hw(q.i));
        } else best = Math.min(best, a2 - net.maxHalf);
      }
      return best;
    };
    // the track's own edge lines and kerbs stop at the mouth of a pit lane or
    // a link (a point on another layout's tarmac)
    var veto = net ? function (x, z) {
      if (net.field.approx(x, z) > net.maxHalf + 4) return false;
      var q = net.field.nearest(x, z);
      return !!(q && q.d < net.hw(q.i) - 0.15);
    } : null;
    return { base: base, d: d, hw: hw, half: half, field: field, box: box,
             terrain: terrain, groundY: surfaceY, idealY: groundY, maxHalf: maxHalf,
             xs: xs, zs: zs, nodeAt: nodeAt, net: net, edgeDist: edgeDist, veto: veto };
  }

  // The network minus the stretches that ARE the track (on its tarmac and
  // running parallel): what is left is every other layout, pit lane and kart
  // track. Each piece runs 2 stations on into the cut so it tucks under the
  // track's tarmac at a junction instead of stopping short of it. Heights come
  // from the track's ground (terrain, flattened at the track), so a pit lane or
  // a link meets the circuit at the circuit's height.
  function buildNet(W0, trackGround) {
    var pieces = [], X = [], Z = [], Y = [], HW = [], PI = [], k;
    NET.forEach(function (L, li) {
      var n = L.x.length, keep = new Uint8Array(n);
      for (k = 0; k < n; k++) {
        var a = Math.max(0, k - 1), b = Math.min(n - 1, k + 1);
        var tx = L.x[b] - L.x[a], tz = L.z[b] - L.z[a], tl = Math.hypot(tx, tz) || 1;
        var same = false;
        if (W0.field.approx(L.x[k], L.z[k]) < 40) {
          var q = W0.field.nearest(L.x[k], L.z[k]);
          same = !!q && q.d < W0.hw(q.i) + 1.5 &&
                 Math.abs((W0.d.tan[q.i][0] * tx + W0.d.tan[q.i][1] * tz) / tl) > 0.85;
        }
        keep[k] = same ? 0 : 1;
      }
      var runs = [], s0 = -1;
      for (k = 0; k <= n; k++) {
        if (k < n && keep[k]) { if (s0 < 0) s0 = k; }
        else if (s0 >= 0) { runs.push([s0, k - 1]); s0 = -1; }
      }
      var whole = runs.length === 1 && runs[0][0] === 0 && runs[0][1] === n - 1;
      // a closed chain (no repeated end point) whose kept stretch runs through
      // its seam is ONE piece, not two with a gap in the edge lines
      if (L.closed && !whole && runs.length > 1 && runs[0][0] === 0 &&
          runs[runs.length - 1][1] === n - 1) {
        var last = runs.pop();
        runs[0] = [last[0], runs[0][1] + n];
      }
      runs.forEach(function (r) {
        var a = r[0] - 2, b = r[1] + 2, j, ids = [];
        if (!L.closed || whole) { a = Math.max(0, a); b = Math.min(n - 1, b); }
        for (j = a; j <= b && ids.length < n; j++) ids.push(((j % n) + n) % n);
        if (ids.length < 4) return;
        var xs = ids.map(function (q) { return L.x[q]; }), zs = ids.map(function (q) { return L.z[q]; });
        var HL = ids.map(function (q) { return L.hl[q]; }), HR = ids.map(function (q) { return L.hr[q]; });
        var cum = [0];
        for (j = 1; j < xs.length; j++) cum.push(cum[j - 1] + Math.hypot(xs[j] - xs[j - 1], zs[j] - zs[j - 1]));
        if (cum[cum.length - 1] < 12) return;
        var ys = RC3D.smooth(xs.map(function (x, jj) { return trackGround(x, zs[jj]); }), 7);
        var P = RC3D.linePath(xs, zs, ys, { closed: whole && L.closed, step: 1, o: PATH.o });
        var D = P.dense, m = D.x.length, hl = new Float64Array(m), hr = new Float64Array(m);
        var sc = cum[cum.length - 1] / (D.total || 1), seg = 0;
        for (j = 0; j < m; j++) {
          var sv = D.s[j] * sc;
          while (seg < cum.length - 2 && cum[seg + 1] < sv) seg++;
          var s1 = Math.min(seg + 1, cum.length - 1);
          var f = cum[s1] > cum[seg] ? Math.max(0, Math.min(1, (sv - cum[seg]) / (cum[s1] - cum[seg]))) : 0;
          hl[j] = HL[seg] + (HL[s1] - HL[seg]) * f;
          hr[j] = HR[seg] + (HR[s1] - HR[seg]) * f;
          X.push(D.x[j]); Z.push(D.z[j]); Y.push(D.y[j]); HW.push(Math.max(hl[j], hr[j]));
          PI.push(pieces.length);
        }
        pieces.push({ path: P, hl: hl, hr: hr, kind: L.kind, names: L.names, line: li });
      });
    });
    if (!X.length) return null;
    var nd = { x: X, z: Z, y: Y }, box = RC3D.denseBox(nd, 0);
    var nbox = RC3D.denseBox(nd, 120), ext = Math.max(nbox.maxX - nbox.minX, nbox.maxZ - nbox.minZ);
    var nfield = RC3D.roadField(nd, nbox, Math.max(4, Math.ceil(ext / 1800)));
    var nhw = function (i) { return HW[i]; }, maxH = 0;
    for (k = 0; k < HW.length; k++) if (HW[k] > maxH) maxH = HW[k];
    var gy = RC3D.groundField(nd, nfield, { halfWidth: nhw, terrain: trackGround,
                                           maxHalf: maxH, drop: 0.1, shoulder: 3 });
    return { pieces: pieces, d: nd, field: nfield, hw: nhw, pieceOf: PI, maxHalf: maxH,
             groundY: gy, box: box };
  }

  // the other layouts: tarmac at their measured width, edge lines that stop
  // where they would cross another road, kerbs through their bends (not on
  // pit lanes or ovals). Drawn a few cm under the track at every junction.
  function makeNetwork(W) {
    var net = W.net;
    if (!net || !net.pieces.length) return null;
    var grp = new THREE.Group();
    net.pieces.forEach(function (pc, k) {
      var D = pc.path.dense;
      var veto = function (x, z) {
        if (W.field.approx(x, z) < W.maxHalf + 6) {
          var q = W.field.nearest(x, z);
          if (q && q.d < W.hw(q.i) - 0.15) return true;
        }
        var q2 = net.field.nearest(x, z);
        return !!(q2 && net.pieceOf[q2.i] !== k && q2.d < net.hw(q2.i) - 0.15);
      };
      grp.add(makeRoad(pc.path, 9, 0.02, false, null, 1,
                       { asphalt: true, half: [pc.hl, pc.hr], o: PATH.o }));
      var pw = { d: D, half: [pc.hl, pc.hr], veto: veto,
                 hw: function (i) { return Math.max(pc.hl[i], pc.hr[i]); } };
      var el2 = makeEdgeLines(pw);
      if (el2) grp.add(el2);
      if (pc.kind !== "pit" && pc.kind !== "oval" && D.x.length > 20) {
        pw.field = RC3D.roadField(D, RC3D.denseBox(D, 40), 4);
        var kb = makeKerbs(pw);
        if (kb) grp.add(kb);
      }
    });
    grp.userData.count = net.pieces.length;
    return grp;
  }

  // A strip along the centreline between two lateral offsets (metres; + is the
  // driver's LEFT), for stations i0..i1. yAt(i, x, z, edge) gives the height.
  // ok(i) may veto a station (the strip simply breaks there).
  function stripGeo(d, i0, i1, offA, offB, yAt, uScale, ok, rows) {
    var pos = [], uv = [], idx = [], prevOk = false, nv = 0, i, k;
    var R = Math.max(1, rows || 1), per = R + 1;
    for (i = i0; i <= i1; i++) {
      var good = !ok || ok(i);
      if (!good) { prevOk = false; continue; }
      var tx = d.tan[i][0], tz = d.tan[i][1];
      var a = offA(i), b = offB(i);
      var u = d.s[i] / uScale;
      for (k = 0; k <= R; k++) {
        var f = k / R, o = a + (b - a) * f;
        var x = d.x[i] + tz * o, z = d.z[i] - tx * o;
        pos.push(x, yAt(i, x, z, f), z);
        uv.push(u, f);
      }
      if (prevOk) {
        for (k = 0; k < R; k++) {
          var p0 = nv - per + k, p1 = p0 + 1, p2 = nv + k, p3 = p2 + 1;
          idx.push(p0, p2, p1, p1, p2, p3);
        }
      }
      nv += per;
      prevOk = true;
    }
    if (!idx.length) return null;
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    geo.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
    geo.setIndex(idx);
    geo.computeVertexNormals();
    return geo;
  }

  // a station "belongs" to the section it was generated from only if no OTHER
  // section of the circuit is nearer — the guard that stops gravel and Armco
  // from being laid across the infield straight next door
  function ownSection(W, i, x, z, slack) {
    var q = W.field.nearest(x, z);
    if (!q) return true;
    if (Math.abs(q.i - i) < 80) return true;
    // the same piece of road driven on another lap (whole-session view):
    // parallel and within a few metres of this station
    var d = W.d, ti = d.tan[i], tq = d.tan[q.i];
    var ddx = d.x[q.i] - d.x[i], ddz = d.z[q.i] - d.z[i];
    if (ti[0] * tq[0] + ti[1] * tq[1] > 0.9 && ddx * ddx + ddz * ddz < 25) return true;
    return q.d - W.hw(q.i) > (slack == null ? 4 : slack);
  }

  /* ---- ground ---------------------------------------------------------------
     A terrain mesh: fine (6 m) across the circuit, growing geometrically out to
     the fogged horizon. Heights from the DEM (or the road's own elevation),
     flattened under the road. Shaded by a splat of the imagery's land cover —
     grass, woods floor, paved (paddock, other circuits), dirt — with grass
     forced near the road so a misregistered image cannot paint a ghost road. */
  // fine `step` over [min, max]; optionally `midStep` out to [lo, hi] (the
  // rest of the facility); then geometric growth to `outer` beyond that
  function axisNodes(min, max, step, outer, lo, hi, midStep) {
    var a = [], v;
    for (v = min; v < max; v += step) a.push(v);
    a.push(max);
    var left = [], right = [], s = step;
    v = min;
    if (lo != null && lo < min - midStep) {
      while (v - midStep > lo) { v -= midStep; left.push(v); }
      s = midStep;
    }
    var edgeL = v;
    while (v > edgeL - outer) { s *= 1.2; v -= s; left.push(v); }
    s = step; v = max;
    if (hi != null && hi > max + midStep) {
      while (v + midStep < hi) { v += midStep; right.push(v); }
      s = midStep;
    }
    var edgeR = v;
    while (v < edgeR + outer) { s *= 1.2; v += s; right.push(v); }
    return left.reverse().concat(a, right);
  }

  function splatTexture(lc) {
    var c = document.createElement("canvas");
    c.width = lc.cols; c.height = lc.rows;
    var g = null;
    try { g = c.getContext("2d"); } catch (e) { g = null; }
    if (!g) return null;
    var img = g.createImageData(lc.cols, lc.rows), px = img.data, i, ch;
    for (i = 0; i < lc.cols * lc.rows; i++) {
      ch = lc._codes.charCodeAt(i);
      px[i * 4] = ch === 112 ? 255 : 0;          // p: paved
      px[i * 4 + 1] = ch === 119 ? 255 : 0;      // w: woods
      px[i * 4 + 2] = ch === 111 ? 255 : 0;      // o: other (dirt, sand, water)
      px[i * 4 + 3] = 255;
    }
    g.putImageData(img, 0, 0);
    var t = new THREE.CanvasTexture(c);
    t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
    t.colorSpace = THREE.NoColorSpace;
    t.minFilter = THREE.LinearFilter;
    t.generateMipmaps = false;
    return t;
  }

  // uniform vec4 (x_west, 1/width, z_south, 1/height) mapping local metres to
  // the uv of something laid north-up between [S,W,N,E]
  function boundsXf(S, Wd, N, E) {
    var a = RC3D.project(S, Wd, PATH.o), b = RC3D.project(N, E, PATH.o);
    return new THREE.Vector4(a.x, 1 / Math.max(1e-6, b.x - a.x),
                             a.z, 1 / Math.max(1e-6, a.z - b.z));
  }

  function groundMaterial(W) {
    var T = surfaces();
    var u = {
      rcGrass: { value: T.grass }, rcPaved: { value: T.asphalt },
      rcDirt: { value: T.dirt }, rcMacro: { value: T.macro },
      rcSplat: { value: null }, rcSplatXf: { value: new THREE.Vector4() },
      rcHasSplat: { value: 0 },
      rcSat: { value: null }, rcSatXf: { value: new THREE.Vector4() }, rcUseSat: { value: 0 },
      rcImg: { value: null }, rcImgXf: { value: new THREE.Vector4() }, rcUseImg: { value: 0 }
    };
    // the facility imagery as the ground's COLOUR (where it is mown, worn, dirt,
    // paddock, run-off), the procedural textures as its close-up DETAIL
    // (satellite mode: the same imagery, uncorrected, wherever the track's
    // own sharper texture does not reach - the rest of the facility)
    if ((opts.ground === "sim" || opts.ground === "satellite") &&
        GROUND_TEX && ASSET && ASSET.ground && ASSET.ground.bounds) {
      var gbd = ASSET.ground.bounds;
      u.rcImg.value = GROUND_TEX;
      u.rcImgXf.value = boundsXf(gbd.south, gbd.west, gbd.north, gbd.east);
      u.rcUseImg.value = opts.ground === "satellite" ? 2 : 1;
    }
    if (LC) {
      var st = splatTexture(LC);
      if (st) {
        u.rcSplat.value = st;
        u.rcSplatXf.value = boundsXf(LC.bounds[0], LC.bounds[1], LC.bounds[2], LC.bounds[3]);
        u.rcHasSplat.value = 1;
      }
    }
    if (opts.ground === "satellite" && TEX && ASSET && ASSET.texture) {
      var tb = ASSET.texture.bounds;
      u.rcSat.value = TEX;
      u.rcSatXf.value = boundsXf(tb.south, tb.west, tb.north, tb.east);
      u.rcUseSat.value = 1;
    }
    var mat = new THREE.MeshLambertMaterial({ color: 0xFFFFFF });
    mat.onBeforeCompile = function (sh) {
      Object.keys(u).forEach(function (k) { sh.uniforms[k] = u[k]; });
      sh.vertexShader = sh.vertexShader
        .replace("#include <common>",
          "#include <common>\\n attribute float aNear;\\n varying vec2 vRcXZ;\\n varying float vRcNear;")
        .replace("#include <begin_vertex>",
          "#include <begin_vertex>\\n vRcXZ = (modelMatrix * vec4(transformed, 1.0)).xz;\\n vRcNear = aNear;");
      sh.fragmentShader = sh.fragmentShader
        .replace("#include <common>", [
          "#include <common>",
          "uniform sampler2D rcGrass; uniform sampler2D rcPaved; uniform sampler2D rcDirt;",
          "uniform sampler2D rcMacro; uniform sampler2D rcSplat; uniform sampler2D rcSat;",
          "uniform vec4 rcSplatXf; uniform vec4 rcSatXf;",
          "uniform float rcHasSplat; uniform float rcUseSat;",
          "uniform sampler2D rcImg; uniform vec4 rcImgXf; uniform float rcUseImg;",
          "varying vec2 vRcXZ; varying float vRcNear;"].join("\\n"))
        .replace("#include <map_fragment>", [
          "vec3 rcG = texture2D(rcGrass, vRcXZ * 0.22).rgb;",
          "vec3 rcG2 = texture2D(rcGrass, vRcXZ * 0.031 + 0.5).rgb;",
          "float rcM = texture2D(rcMacro, vRcXZ * 0.0036).r;",
          "float rcM2 = texture2D(rcMacro, vRcXZ * 0.017 + 0.3).r;",
          "vec3 rcGrassC = mix(rcG, rcG2, 0.35) * (0.80 + 0.42 * rcM) * (0.9 + 0.2 * rcM2);",
          "rcGrassC *= mix(vec3(1.0), vec3(1.07, 1.02, 0.86), smoothstep(0.55, 0.85, rcM));",
          "vec3 rcCol = rcGrassC;",
          "if (rcHasSplat > 0.5) {",
          "  vec2 suv = vec2((vRcXZ.x - rcSplatXf.x) * rcSplatXf.y, (rcSplatXf.z - vRcXZ.y) * rcSplatXf.w);",
          "  if (suv.x > 0.0 && suv.x < 1.0 && suv.y > 0.0 && suv.y < 1.0) {",
          "    vec3 s = texture2D(rcSplat, suv).rgb;",
          "    float rcEdge = min(min(suv.x, 1.0 - suv.x), min(suv.y, 1.0 - suv.y));",
          "    float k = clamp(vRcNear, 0.0, 1.0) * smoothstep(0.0, 0.06, rcEdge);",
          "    float wp = s.r * k, ww = s.g * k, wo = s.b * k;",
          "    float wg = max(0.0, 1.0 - wp - ww - wo);",
          "    vec3 paved = mix(texture2D(rcPaved, vRcXZ * 0.15).rgb, vec3(0.16, 0.16, 0.155), 0.35) * 1.7;",
          "    vec3 dirt = texture2D(rcDirt, vRcXZ * 0.2).rgb;",
          "    vec3 floorC = rcGrassC * vec3(0.50, 0.56, 0.40);",
          "    rcCol = rcGrassC * wg + paved * wp + floorC * ww + dirt * wo;",
          "  }",
          "}",
          "if (rcUseImg > 0.5) {",
          "  vec2 iuv = vec2((vRcXZ.x - rcImgXf.x) * rcImgXf.y, (rcImgXf.z - vRcXZ.y) * rcImgXf.w);",
          "  if (iuv.x > 0.0 && iuv.x < 1.0 && iuv.y > 0.0 && iuv.y < 1.0) {",
          "    float ie = min(min(iuv.x, 1.0 - iuv.x), min(iuv.y, 1.0 - iuv.y));",
          "    vec3 im = texture2D(rcImg, iuv).rgb;",
          "    if (rcUseImg > 1.5) {",
          "      rcCol = mix(rcCol, im, smoothstep(0.0, 0.08, ie));",
          "    } else {",
          "      float dl = dot(rcG, vec3(0.333)) / max(0.04, dot(rcG2, vec3(0.333)));",
          "      float il = dot(im, vec3(0.299, 0.587, 0.114));",
          "      im = mix(vec3(il), im, 1.18) * 1.32;",
          "      vec3 hyb = im * mix(1.0, clamp(dl, 0.55, 1.5), 0.7) * (0.9 + 0.2 * rcM2);",
          "      float kk = 0.82 * smoothstep(0.0, 0.16, ie) * mix(0.45, 1.0, clamp(vRcNear, 0.0, 1.0));",
          "      rcCol = mix(rcCol, hyb, kk);",
          "    }",
          "  }",
          "}",
          "if (rcUseSat > 0.5) {",
          "  vec2 tuv = vec2((vRcXZ.x - rcSatXf.x) * rcSatXf.y, (rcSatXf.z - vRcXZ.y) * rcSatXf.w);",
          "  if (tuv.x > 0.0 && tuv.x < 1.0 && tuv.y > 0.0 && tuv.y < 1.0) {",
          "    rcCol = texture2D(rcSat, tuv).rgb;",
          "  }",
          "}",
          "diffuseColor.rgb *= rcCol;"].join("\\n"));
    };
    mat.customProgramCacheKey = function () { return "rc-ground-v2"; };
    mat.userData.owned = [u.rcSplat.value];
    return mat;
  }

  function buildGround(W) {
    var xs = W.xs, zs = W.zs;
    var nx = xs.length, nz = zs.length, r, c, k = 0;
    var pos = new Float32Array(nx * nz * 3), near = new Float32Array(nx * nz);
    var nearEdge = 12;                     // landcover is ignored this close to the road edge
    for (r = 0; r < nz; r++) {
      for (c = 0; c < nx; c++) {
        var x = xs[c], z = zs[r], y;
        var a = W.field.approx(x, z), nr = 1;
        if (a < W.maxHalf + 60) {
          var q = W.field.nearest(x, z);
          if (q) nr = Math.max(0, Math.min(1, (q.d - W.hw(q.i) - nearEdge) / 14));
        }
        y = W.nodeAt(c, r);
        pos[k * 3] = x; pos[k * 3 + 1] = y; pos[k * 3 + 2] = z;
        near[k] = nr;
        k++;
      }
    }
    var idx = new Uint32Array((nx - 1) * (nz - 1) * 6), m = 0;
    for (r = 0; r < nz - 1; r++) {
      for (c = 0; c < nx - 1; c++) {
        var i0 = r * nx + c, i1 = i0 + 1, i2 = i0 + nx, i3 = i2 + 1;
        idx[m++] = i0; idx[m++] = i2; idx[m++] = i1;
        idx[m++] = i1; idx[m++] = i2; idx[m++] = i3;
      }
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    geo.setAttribute("aNear", new THREE.BufferAttribute(near, 1));
    geo.setIndex(new THREE.BufferAttribute(idx, 1));
    geo.computeVertexNormals();
    var mesh = new THREE.Mesh(geo, groundMaterial(W));
    mesh.receiveShadow = true;
    return mesh;
  }

  /* ---- road, lines, kerbs ---------------------------------------------------*/
  function makeRoad(path, width, lift, colourBySpeed, solidColour, alpha, extra) {
    extra = extra || {};
    var r = RC3D.ribbon(path, width, lift, {
      half: extra.half || null,
      uv: extra.uvBounds || null,
      worldUV: extra.asphalt ? 7 : 0,       // one tarmac tile per 7 m
      o: extra.o || (PATH && PATH.o)
    });
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(r.position, 3));
    var cols = new Float32Array(r.s.length * 3), i, c;
    for (i = 0; i < r.s.length; i++) {
      c = colourBySpeed ? RC3D.driveColour(RC3D.accelAtS(path, denseToCum(path, r.s[i])))
                        : (solidColour || [0.135, 0.145, 0.165]);
      cols[i * 3] = c[0]; cols[i * 3 + 1] = c[1]; cols[i * 3 + 2] = c[2];
    }
    geo.setAttribute("color", new THREE.BufferAttribute(cols, 3));
    if (r.uv) geo.setAttribute("uv", new THREE.BufferAttribute(r.uv, 2));
    geo.setIndex(new THREE.BufferAttribute(r.index, 1));
    geo.computeVertexNormals();
    var mo = { vertexColors: true, side: THREE.DoubleSide };
    if (extra.asphalt) {
      var at = surfaces().asphalt;
      if (at) { mo.map = at; mo.vertexColors = false; }
    }
    if (extra.tex) mo.map = extra.tex;
    var mat = new THREE.MeshLambertMaterial(mo);
    if (extra.asphalt || extra.tex) {
      mat.polygonOffset = true; mat.polygonOffsetFactor = -1; mat.polygonOffsetUnits = -2;
    }
    if (alpha != null && alpha < 1) { mat.transparent = true; mat.opacity = alpha; }
    var mesh = new THREE.Mesh(geo, mat);
    mesh.receiveShadow = true;
    return mesh;
  }

  var LINE_TEX = null;
  function lineTexture() {
    // the driving line's alpha: soft edges across, a chevron every 2.5 m along
    if (LINE_TEX) return LINE_TEX;
    var c = document.createElement("canvas"), S = 256;
    c.width = S; c.height = S;
    var g = c.getContext("2d"), y, x;
    var img = g.createImageData(S, S), px = img.data;
    var sm = function (e0, e1, v) { var t = Math.max(0, Math.min(1, (v - e0) / (e1 - e0))); return t * t * (3 - 2 * t); };
    for (y = 0; y < S; y++) {
      for (x = 0; x < S; x++) {
        var v = (y + 0.5) / S;                           // across the line
        var edge = sm(0, 0.26, Math.min(v, 1 - v));
        var u = (x + 0.5) / S;                           // along the line
        var dv = Math.abs(v - 0.5) * 2;
        var ch = ((u + dv * 0.3) % 1);
        var chev = sm(0.5, 0.56, ch) * (1 - sm(0.74, 0.8, ch));
        var a = edge * (0.5 + 0.5 * chev);
        var o2 = (y * S + x) * 4;
        px[o2] = px[o2 + 1] = px[o2 + 2] = Math.round(a * 255);
        px[o2 + 3] = 255;
      }
    }
    g.putImageData(img, 0, 0);
    LINE_TEX = new THREE.CanvasTexture(c);
    LINE_TEX.wrapS = THREE.RepeatWrapping;
    LINE_TEX.wrapT = THREE.ClampToEdgeWrapping;
    try { LINE_TEX.anisotropy = renderer.capabilities.getMaxAnisotropy(); } catch (e) {}
    return LINE_TEX;
  }

  // The driving line (chase view): a soft ribbon with chevrons, coloured by the
  // driver's input — green on the throttle, red on the brakes (brighter with
  // the g), pale when neither. The wash (plan view): the same colours across
  // the whole road width, transparent where nothing is happening.
  function makeInputRibbon(path, width, lift, mode, half) {
    var r = RC3D.ribbon(path, width, lift, { worldUV: mode === "line" ? width : 0,
                                             half: mode === "wash" ? half : null });
    var n = r.s.length, cols = new Float32Array(n * 4), i;
    var cum = path.cum, nc = cum.length, inp = path.input;
    var at = function (sc) {                 // nearest sample at sample arc length sc
      var lo = 0, hi = nc - 1, mid;
      while (lo < hi) { mid = (lo + hi + 1) >> 1; if (cum[mid] <= sc) lo = mid; else hi = mid - 1; }
      return lo;
    };
    for (i = 0; i < n; i++) {
      var k = nc ? at(denseToCum(path, r.s[i])) : 0;
      var st = inp && inp.state.length ? inp.state[k] : 0, lv = inp && inp.level.length ? inp.level[k] : 0;
      var c = opts.speedColour ? RC3D.inputColour(st, lv) : [0.86, 0.91, 1.0];
      var a;
      if (mode === "wash") {
        a = !opts.speedColour ? 0 : st > 0 ? 0.22 + 0.42 * lv : st < 0 ? 0.38 + 0.5 * lv : 0.42;
      } else {
        a = !opts.speedColour ? 0.4 : st > 0 ? 0.55 + 0.35 * lv : st < 0 ? 0.62 + 0.33 * lv : 0.78;
      }
      cols[i * 4] = c[0]; cols[i * 4 + 1] = c[1]; cols[i * 4 + 2] = c[2]; cols[i * 4 + 3] = a;
    }
    var geo = new THREE.BufferGeometry();
    geo.setAttribute("position", new THREE.BufferAttribute(r.position, 3));
    geo.setAttribute("color", new THREE.BufferAttribute(cols, 4));
    if (r.uv && mode === "line") {
      // u along in units of 2.5 m per chevron
      for (i = 0; i < n; i++) r.uv[i * 2] = r.uv[i * 2] * width / 2.5;
      geo.setAttribute("uv", new THREE.BufferAttribute(r.uv, 2));
    }
    geo.setIndex(new THREE.BufferAttribute(r.index, 1));
    var mat = new THREE.MeshBasicMaterial({
      vertexColors: true, transparent: true, depthWrite: false, side: THREE.DoubleSide,
      alphaMap: mode === "line" ? lineTexture() : null,
      polygonOffset: true, polygonOffsetFactor: -2, polygonOffsetUnits: -4
    });
    mat.toneMapped = false;
    if (mode === "line") {
      mat.onBeforeCompile = function (sh) {
        sh.fragmentShader = sh.fragmentShader.replace("#include <alphamap_fragment>",
          "#include <alphamap_fragment>\\n#ifdef USE_FOG\\n diffuseColor.a *= smoothstep(1.2, 6.5, vFogDepth);\\n#endif");
      };
      mat.customProgramCacheKey = function () { return "rc-line-v1"; };
    }
    var mesh = new THREE.Mesh(geo, mat);
    mesh.renderOrder = 2;
    return mesh;
  }

  function overlayMat(opt) {
    opt.side = THREE.DoubleSide;
    var m = new THREE.MeshLambertMaterial(opt);
    m.polygonOffset = true; m.polygonOffsetFactor = -1; m.polygonOffsetUnits = -3;
    return m;
  }

  // painted white track-limit lines along both edges
  function makeEdgeLines(W) {
    var d = W.d, n = d.x.length, grp = new THREE.Group();
    var mat = overlayMat({ color: 0xE9EBEC });
    [1, -1].forEach(function (side) {
      var edge = function (i) {
        var h = W.half ? (side > 0 ? W.half[0][i] : W.half[1][i]) : null;
        return (h > 1 ? h : W.hw(i));
      };
      var okE = W.veto ? function (i) {
        var o = side * (edge(i) - 0.2);
        return !W.veto(d.x[i] + d.tan[i][1] * o, d.z[i] - d.tan[i][0] * o);
      } : null;
      var geo = stripGeo(d, 0, n - 1,
        function (i) { return side * (edge(i) - 0.32); },
        function (i) { return side * (edge(i) - 0.08); },
        function (i) { return d.y[i] + 0.045; }, 10, okE);
      if (geo) { var m = new THREE.Mesh(geo, mat); m.receiveShadow = true; grp.add(m); }
    });
    return grp;
  }

  // red/white kerbs through every bend tighter than ~600 m radius, both edges,
  // 1.1 m wide, as continuous striped strips (not per-block quads)
  function makeKerbs(W) {
    var d = W.d, n = d.x.length, i, on = new Uint8Array(n);
    if (n < 10) return null;
    // real circuits kerb their CORNERS (radius under ~250 m), not the gently
    // curving straights between them; tangents +-8 stations apart (a 16 m span)
    // so the centreline's centimetre wiggles never read as a bend
    for (i = 8; i < n - 8; i++) {
      var t0 = d.tan[i - 8], t1 = d.tan[i + 8];
      var ds = Math.max(0.5, d.s[i + 8] - d.s[i - 8]);
      if (Math.abs(t0[0] * t1[1] - t0[1] * t1[0]) / ds > 1 / 250) on[i] = 1;
    }
    // dilate 6 m, drop runs shorter than 10 m
    var dil = new Uint8Array(n), j;
    for (i = 0; i < n; i++) if (on[i]) for (j = Math.max(0, i - 6); j <= Math.min(n - 1, i + 6); j++) dil[j] = 1;
    var runs = [], s0 = -1;
    for (i = 0; i <= n; i++) {
      if (i < n && dil[i]) { if (s0 < 0) s0 = i; }
      else if (s0 >= 0) { if (d.s[i - 1] - d.s[s0] >= 10) runs.push([s0, i - 1]); s0 = -1; }
    }
    if (!runs.length) return null;
    var grp = new THREE.Group();
    var mat = overlayMat({ map: surfaces().kerb });
    runs.forEach(function (run) {
      [1, -1].forEach(function (side) {
        var edge = function (i2) {
          var h = W.half ? (side > 0 ? W.half[0][i2] : W.half[1][i2]) : null;
          return (h > 1 ? h : W.hw(i2));
        };
        var geo = stripGeo(d, run[0], run[1],
          function (i2) { return side * (edge(i2) - 0.05); },
          function (i2) { return side * (edge(i2) + 1.05); },
          function (i2, x, z, e) { return d.y[i2] + (e ? 0.08 : 0.05); }, 2.4,
          function (i2) {
            var px = d.x[i2] + d.tan[i2][1] * side * (edge(i2) + 1.0);
            var pz = d.z[i2] - d.tan[i2][0] * side * (edge(i2) + 1.0);
            if (W.veto) {
              var ix = d.x[i2] + d.tan[i2][1] * side * edge(i2);
              var iz = d.z[i2] - d.tan[i2][0] * side * edge(i2);
              if (W.veto(px, pz) || W.veto(ix, iz)) return false;
            }
            return ownSection(W, i2, px, pz, 0.5);
          });
        if (geo) {
          var m = new THREE.Mesh(geo, mat);
          m.receiveShadow = true;
          grp.add(m);
        }
      });
    });
    return grp;
  }

  /* ---- run-off: gravel traps + Armco ---------------------------------------
     On the OUTSIDE of every real corner (the turn direction says which side):
     a gravel trap that runs on past the exit, the way a car that does not
     make the corner would go, and Armco behind it. Never laid over another
     section of the circuit. */
  function makeRunoff(W, corners, noGravel) {
    var d = W.d, n = d.x.length, grp = new THREE.Group();
    if (!corners || !corners.length) return grp;
    var T = surfaces();
    var gravelMat = new THREE.MeshLambertMaterial({ map: T.gravel, side: THREE.DoubleSide });
    gravelMat.polygonOffset = true; gravelMat.polygonOffsetFactor = -1; gravelMat.polygonOffsetUnits = -2;
    var armcoMat = new THREE.MeshLambertMaterial({ map: T.armco, side: THREE.DoubleSide });
    var posts = [];
    corners.forEach(function (C) {
      var side = C.dir > 0 ? 1 : -1;          // outside of a right-hander is the LEFT (+)
      var big = Math.abs(C.deg) >= 60 && !noGravel;
      var iA = Math.max(0, C.i0 - 10), iB = Math.min(n - 1, C.i1 + (big ? 45 : 12));
      var inner = function (i) { return W.hw(i) + 3.2; };
      var depth = function (i) {
        var into = (d.s[i] - d.s[iA]), left = (d.s[iB] - d.s[i]);
        var ramp = Math.min(1, into / 14, left / 22);
        return big ? 3 + 11 * Math.max(0, ramp) : 0;
      };
      var okAt = function (off) {
        return function (i) {
          var o = side * off(i);
          var x = d.x[i] + d.tan[i][1] * o, z = d.z[i] - d.tan[i][0] * o;
          return ownSection(W, i, x, z, 6);
        };
      };
      if (big) {
        var outer = function (i) { return inner(i) + depth(i); };
        var gGeo = stripGeo(d, iA, iB,
          function (i) { return side * inner(i); },
          function (i) { return side * outer(i); },
          function (i, x, z) { return W.groundY(x, z) + 0.05; }, 4, okAt(outer), 5);
        if (gGeo) {
          // gravel uv in world metres (u along, v across)
          var uv = gGeo.attributes.uv, p = gGeo.attributes.position;
          for (var q = 0; q < uv.count; q++) uv.setXY(q, p.getX(q) / 4, p.getZ(q) / 4);
          var gm = new THREE.Mesh(gGeo, gravelMat);
          gm.receiveShadow = true;
          grp.add(gm);
        }
      }
      var barrierOff = function (i) { return inner(i) + (big ? depth(i) + 2.5 : 4); };
      var aGeo = stripGeo(d, iA, iB,
        function (i) { return side * barrierOff(i); },
        function (i) { return side * barrierOff(i); },
        function (i, x, z, e) { return W.groundY(x, z) + (e ? 0.82 : 0.45); }, 4,
        okAt(barrierOff));
      if (aGeo) {
        // a zero-width strip has both vertices at the same x/z: that is the
        // rail face (two heights), lit from either side
        var am = new THREE.Mesh(aGeo, armcoMat);
        am.castShadow = true;
        am.receiveShadow = true;
        grp.add(am);
        for (var i = iA; i <= iB; i += 4) {
          if (!okAt(barrierOff)(i)) continue;
          var o = side * (barrierOff(i) + 0.12);
          var x = d.x[i] + d.tan[i][1] * o, z = d.z[i] - d.tan[i][0] * o;
          posts.push([x, W.groundY(x, z), z]);
        }
      }
    });
    if (posts.length) {
      var pm = new THREE.InstancedMesh(new THREE.BoxGeometry(0.14, 0.95, 0.14),
        new THREE.MeshLambertMaterial({ color: 0x6A7078 }), posts.length);
      var mx = new THREE.Matrix4();
      posts.forEach(function (p, k) { mx.makeTranslation(p[0], p[1] + 0.47, p[2]); pm.setMatrixAt(k, mx); });
      pm.castShadow = true;
      pm.computeBoundingSphere();
      grp.add(pm);
    }
    return grp;
  }

  /* ---- trees ------------------------------------------------------------------
     Instanced, in 320 m chunks (so the camera and the shadow map only draw the
     chunks they can see). Two species built from merged primitives with baked
     vertex colours; each instance gets its own size, turn and tint. */
  function partGeo(geo, colour, ox, oy, oz, sx, sy, sz, jitter, seed, radial) {
    var g = geo.index ? geo.toNonIndexed() : geo;
    var p = g.attributes.position, i;
    if (jitter) {
      var rnd = seededRandom(seed || "j");
      var map = {};
      for (i = 0; i < p.count; i++) {
        var key = p.getX(i).toFixed(3) + "," + p.getY(i).toFixed(3) + "," + p.getZ(i).toFixed(3);
        if (!map[key]) map[key] = 1 + (rnd() - 0.5) * jitter;
        p.setXYZ(i, p.getX(i) * map[key], p.getY(i) * map[key], p.getZ(i) * map[key]);
      }
    }
    if (radial) {
      // a lumpy blob still shades like a ball of leaves, not a faceted rock
      var nn = g.attributes.normal, v3 = new THREE.Vector3();
      for (i = 0; i < p.count; i++) {
        v3.set(p.getX(i), p.getY(i) * 1.3, p.getZ(i)).normalize();
        nn.setXYZ(i, v3.x, v3.y, v3.z);
      }
    }
    g.scale(sx, sy, sz);
    g.translate(ox, oy, oz);
    var col = new Float32Array(p.count * 3), cc = new THREE.Color(colour);
    var y0 = Infinity, y1 = -Infinity;
    for (i = 0; i < p.count; i++) { y0 = Math.min(y0, p.getY(i)); y1 = Math.max(y1, p.getY(i)); }
    for (i = 0; i < p.count; i++) {
      // a little self-shadowing: darker low on the part, lighter at the top
      var k = 0.78 + 0.32 * (p.getY(i) - y0) / Math.max(1e-3, y1 - y0);
      col[i * 3] = cc.r * k; col[i * 3 + 1] = cc.g * k; col[i * 3 + 2] = cc.b * k;
    }
    g.setAttribute("color", new THREE.BufferAttribute(col, 3));
    return g;
  }

  function mergeGeos(parts) {
    var total = 0, i, off = 0;
    parts.forEach(function (g) { total += g.attributes.position.count; });
    var pos = new Float32Array(total * 3), nor = new Float32Array(total * 3), col = new Float32Array(total * 3);
    parts.forEach(function (g) {
      pos.set(g.attributes.position.array, off * 3);
      nor.set(g.attributes.normal.array, off * 3);
      col.set(g.attributes.color.array, off * 3);
      off += g.attributes.position.count;
    });
    var out = new THREE.BufferGeometry();
    out.setAttribute("position", new THREE.BufferAttribute(pos, 3));
    out.setAttribute("normal", new THREE.BufferAttribute(nor, 3));
    out.setAttribute("color", new THREE.BufferAttribute(col, 3));
    return out;
  }

  var TREE_GEO = null;
  function treeGeometries() {
    // unit tree: height 1, base at y=0; instance scale = (width, height, width)
    if (TREE_GEO) return TREE_GEO;
    var bark = 0x4B3A2A;
    var conifer = mergeGeos([
      partGeo(new THREE.CylinderGeometry(0.03, 0.045, 0.3, 6), bark, 0, 0.15, 0, 1, 1, 1),
      partGeo(new THREE.ConeGeometry(0.42, 0.42, 9), 0x2D4F2C, 0, 0.36, 0, 1, 1, 1, 0.12, "c1"),
      partGeo(new THREE.ConeGeometry(0.33, 0.36, 9), 0x31572F, 0, 0.58, 0, 1, 1, 1, 0.12, "c2"),
      partGeo(new THREE.ConeGeometry(0.22, 0.30, 8), 0x386335, 0, 0.79, 0, 1, 1, 1, 0.1, "c3"),
      partGeo(new THREE.ConeGeometry(0.1, 0.16, 7), 0x3E6B39, 0, 0.94, 0, 1, 1, 1, 0, "c4")
    ]);
    var broad = mergeGeos([
      partGeo(new THREE.CylinderGeometry(0.035, 0.06, 0.48, 6), bark, 0, 0.24, 0, 1, 1, 1),
      partGeo(new THREE.IcosahedronGeometry(0.3, 1), 0x3E6A2E, 0, 0.64, 0, 1.1, 0.95, 1.1, 0.22, "b1", true),
      partGeo(new THREE.IcosahedronGeometry(0.22, 1), 0x4A7835, 0.16, 0.76, 0.05, 1, 0.9, 1, 0.22, "b2", true),
      partGeo(new THREE.IcosahedronGeometry(0.2, 1), 0x426F31, -0.14, 0.72, -0.1, 1, 0.9, 1, 0.22, "b3", true),
      partGeo(new THREE.IcosahedronGeometry(0.17, 1), 0x527F3A, 0.0, 0.88, 0.0, 1, 0.85, 1, 0.2, "b4", true)
    ]);
    conifer.userData.shared = broad.userData.shared = true;
    TREE_GEO = [conifer, broad];
    return TREE_GEO;
  }

  function buildTrees(W, seedStr) {
    var grp = new THREE.Group();
    var spots = RC3D.treeSpots(W.d, W.field, {
      rnd: seededRandom(seedStr || "track"), halfWidth: W.hw, gap: 17,
      landcover: LC ? { codes: LC._codes, cols: LC.cols, rows: LC.rows, bounds: LC.bounds } : null,
      o: PATH.o, seed: seedStr || "track", max: 9000, maxDist: 760,
      extra: osmTreeSpots(ASSET && ASSET.features)
    });
    if (W.net) {
      // the same clearance from every other layout, pit lane and kart track
      spots = spots.filter(function (t) { return W.edgeDist(t.x, t.z) > 14; });
    }
    var geos = treeGeometries();
    var mat = new THREE.MeshLambertMaterial({ vertexColors: true });
    var chunks = {}, CH = 320;
    spots.forEach(function (t) {
      var key = Math.floor(t.x / CH) + ":" + Math.floor(t.z / CH) + ":" + t.kind;
      (chunks[key] || (chunks[key] = [])).push(t);
    });
    var m4 = new THREE.Matrix4(), q = new THREE.Quaternion(), sc = new THREE.Vector3();
    var pv = new THREE.Vector3(), up = new THREE.Vector3(0, 1, 0), col = new THREE.Color();
    Object.keys(chunks).forEach(function (key) {
      var list = chunks[key], kind = list[0].kind;
      var im = new THREE.InstancedMesh(geos[kind], mat, list.length);
      list.forEach(function (t, k) {
        var y = W.groundY(t.x, t.z) - 0.3;
        q.setFromAxisAngle(up, t.rot);
        sc.set(t.w, t.h, t.w);
        pv.set(t.x, y, t.z);
        m4.compose(pv, q, sc);
        im.setMatrixAt(k, m4);
        // seasonal-ish variety: some olive, some deep green, some lighter
        var v = t.tint;
        col.setRGB(0.82 + 0.3 * v, 0.86 + 0.22 * v, 0.78 + 0.18 * (1 - v));
        im.setColorAt(k, col);
      });
      im.instanceMatrix.needsUpdate = true;
      if (im.instanceColor) im.instanceColor.needsUpdate = true;
      im.castShadow = true;
      im.computeBoundingSphere();
      grp.add(im);
    });
    grp.userData.count = spots.length;
    return grp;
  }

  var LABEL_TEX = {};
  function buildCornerLabels(base, corners) {
    var grp = new THREE.Group();
    var tex = LABEL_TEX;
    for (var i = 0; i < corners.length; i++) {
      var label = "T" + (i + 1);
      if (!tex[label]) {
        var c = document.createElement("canvas");
        c.width = c.height = 128;
        var g = null;
        try { g = c.getContext("2d"); } catch (e) { g = null; }
        if (!g) continue;
        g.fillStyle = "rgba(14,16,20,0.72)";
        g.fillRect(0, 0, 128, 128);
        g.strokeStyle = "#E6E8EE";
        g.lineWidth = 6;
        g.strokeRect(4, 4, 120, 120);
        g.fillStyle = "#E6E8EE";
        g.font = "bold 72px Inter, Arial, sans-serif";
        g.textAlign = "center"; g.textBaseline = "middle";
        g.fillText(label, 64, 70);
        var t = new THREE.CanvasTexture(c);
        t.colorSpace = THREE.SRGBColorSpace;
        tex[label] = t;
      }
      var p = RC3D.pointAtS(base, denseToCum(base, corners[i].apex_s));
      var spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex[label],
                                                            depthTest: false, toneMapped: false }));
      spr.scale.set(26, 26, 1);
      spr.position.set(p.x, p.y + 14, p.z);
      grp.add(spr);
    }
    return grp;
  }

  // The prepared track's own terrain grid must actually cover the circuit
  // before anything is built from it (a bad asset once floated its terrain
  // ABOVE the track: a dark faceted ceiling in the driving view).
  function demCoversTrack() {
    return !!(DEMFN && TRACK_INFO && TRACK_INFO.demOK);
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
      signTex[n] = digitTexture(n, n === "1" ? "#C0392B" : "#F4F4F1",
                                n === "1" ? "#FFFFFF" : "#111214");
    });
    return signTex;
  }

  function makeBrakeSigns(list, path, W) {
    if (!list || !list.length) return null;
    var tex = signTextures();
    var grp = new THREE.Group();
    var postMat = new THREE.MeshLambertMaterial({ color: 0x2A2F3A });
    var backMat = new THREE.MeshLambertMaterial({ color: 0x30353C });
    // bigger than life (real boards are ~1.2 m): read from the chase camera
    // at 100 m, they have to be
    var boardGeo = new THREE.PlaneGeometry(2.4, 2.4);
    var postGeo = new THREE.BoxGeometry(0.16, 2.0, 0.16);
    for (var i = 0; i < list.length; i++) {
      var mk = list[i];
      var p = RC3D.pointAtS(path, denseToCum(path, mk.s));
      var tx = p.tan[0], tz = p.tan[1];
      // driver's left is (tz,-tx); mk.side = -1 puts the board on the left
      var ox = (mk.side < 0) ? tz : -tz, oz = (mk.side < 0) ? -tx : tx;
      var off = (W ? W.hw(p.i || 0) : 6) + 3.2;
      var x = p.x + ox * off, z = p.z + oz * off;
      var y = W ? W.groundY(x, z) : p.y;
      var post = new THREE.Mesh(postGeo, postMat);
      post.position.set(x, y + 1.0, z);
      post.castShadow = true;
      grp.add(post);
      var mat = new THREE.MeshBasicMaterial({ map: tex[mk.label] || null });
      mat.toneMapped = false;
      var board = new THREE.Mesh(boardGeo, mat);
      board.position.set(x, y + 3.1, z);
      board.rotation.y = Math.atan2(-tx, -tz);   // face the oncoming car
      board.castShadow = true;
      grp.add(board);
      var back = new THREE.Mesh(boardGeo, backMat);
      back.position.copy(board.position);
      back.rotation.y = board.rotation.y + Math.PI;
      grp.add(back);
    }
    return grp;
  }

  // In plan mode you are looking at the whole circuit, so there has to be a
  // "you are here": an arrow on the ribbon, amber, pointing along the tangent.
  function makeCarMarker() {
    var grp = new THREE.Group();
    var cone = new THREE.Mesh(new THREE.ConeGeometry(2.6, 7.0, 4),
                              new THREE.MeshBasicMaterial({ color: 0xFFB020, toneMapped: false }));
    cone.rotation.x = Math.PI / 2;          // lie it down, pointing along +z
    grp.add(cone);
    var ring = new THREE.Mesh(new THREE.RingGeometry(3.6, 5.0, 28),
                              new THREE.MeshBasicMaterial({ color: 0xFFB020,
                                side: THREE.DoubleSide, transparent: true,
                                opacity: 0.85, toneMapped: false }));
    ring.rotation.x = -Math.PI / 2;
    grp.add(ring);
    return grp;
  }

  function placeCar(p) {
    if (!meshes.car) return;
    var pos = RC3D.pointAtS(PATH, p);
    meshes.car.position.set(pos.x, pos.y + 0.6, pos.z);
    meshes.car.rotation.y = Math.atan2(pos.tan[0], pos.tan[1]);
    meshes.car.children[0].position.y = 0;
  }

  // brake / apex / throttle: painted on the road (a disc on the line) with a
  // marker post at the edge of the road on the INSIDE of the bend — never a
  // pole standing in the middle of the racing line
  function makeMarkers(list, W) {
    if (!list.length) return null;
    var grp = new THREE.Group();
    var colours = { brake: 0xFF4D4D, apex: 0xFFB020, throttle: 0x6CD07A };
    list.forEach(function (mk) {
      var c = colours[mk.kind] || 0xFFFFFF;
      var disc = new THREE.Mesh(new THREE.RingGeometry(0.55, 1.15, 32),
        new THREE.MeshBasicMaterial({ color: c, transparent: true, opacity: 0.85,
          depthWrite: false, polygonOffset: true, polygonOffsetFactor: -3,
          polygonOffsetUnits: -6, toneMapped: false }));
      disc.rotation.x = -Math.PI / 2;
      disc.position.set(mk.x, mk.y + 0.1, mk.z);
      disc.renderOrder = 3;
      grp.add(disc);
      if (!W) return;
      var q = W.field.nearest(mk.x, mk.z);
      if (!q) return;
      var i = q.i, tx = W.d.tan[i][0], tz = W.d.tan[i][1];
      var s = denseToCum(W.base, W.d.s[i]);
      var a = RC3D.pointAtS(W.base, Math.max(0, s - 8)), b = RC3D.pointAtS(W.base, s + 8);
      var turnRight = (a.tan[0] * b.tan[1] - a.tan[1] * b.tan[0]) >= 0;
      var off = (W.hw(i) + 1.6) * (turnRight ? -1 : 1);     // inside of the bend
      var x = W.d.x[i] + tz * off, z = W.d.z[i] - tx * off, y = W.groundY(x, z);
      var pole = new THREE.Mesh(new THREE.CylinderGeometry(0.05, 0.05, 2.2, 8),
                                new THREE.MeshLambertMaterial({ color: 0xE6E8EE }));
      pole.position.set(x, y + 1.1, z);
      pole.castShadow = true;
      grp.add(pole);
      var cone = new THREE.Mesh(new THREE.ConeGeometry(0.32, 0.7, 16),
                                new THREE.MeshLambertMaterial({ color: c, emissive: c,
                                                                emissiveIntensity: 0.45 }));
      cone.position.set(x, y + 2.55, z);
      cone.rotation.x = Math.PI;
      grp.add(cone);
    });
    return grp;
  }

  function checkerTexture(cols, rows, w, h, text) {
    var c = document.createElement("canvas");
    c.width = w; c.height = h;
    var g = c.getContext("2d"), r, k;
    var cw = w / cols, ch = h / rows;
    for (r = 0; r < rows; r++) for (k = 0; k < cols; k++) {
      g.fillStyle = ((r + k) % 2) ? "#111214" : "#F1F2F3";
      g.fillRect(k * cw, r * ch, cw + 1, ch + 1);
    }
    if (text) {
      g.fillStyle = "rgba(10,12,16,0.86)";
      g.fillRect(w * 0.2, h * 0.14, w * 0.6, h * 0.72);
      g.fillStyle = "#F1F2F3";
      g.font = "bold " + Math.round(h * 0.42) + "px Inter, Arial, sans-serif";
      g.textAlign = "center"; g.textBaseline = "middle";
      g.fillText(text, w / 2, h / 2 + 2);
    }
    var t = new THREE.CanvasTexture(c);
    t.colorSpace = THREE.SRGBColorSpace;
    try { t.anisotropy = renderer.capabilities.getMaxAnisotropy(); } catch (e) {}
    return t;
  }

  // start/finish: a chequered stripe painted across the road plus a gantry
  // whose legs stand off the edges of the road, square to the track
  // `atStart`: laps stamped by the logger carry no S/F geometry, but a lap
  // slice STARTS on the line, so its first station is the crossing
  function makeGantry(sf, W, startXZ) {
    if (!W) return null;
    var i, q;
    if (sf && typeof sf.lat1 === "number" && typeof sf.lat2 === "number") {
      var a = RC3D.project(sf.lat1, sf.lon1, PATH.o);
      var b = RC3D.project(sf.lat2, sf.lon2, PATH.o);
      q = W.field.nearest((a.x + b.x) / 2, (a.z + b.z) / 2);
      if (!q || q.d > 40) return null;
      i = q.i;
    } else if (startXZ) {
      q = W.field.nearest(startXZ[0], startXZ[1]);
      if (!q || q.d > 30) return null;
      i = Math.max(1, Math.min(W.d.x.length - 2, q.i));
    } else {
      return null;
    }
    var d = W.d, tx = d.tan[i][0], tz = d.tan[i][1];
    var hwL = W.half && W.half[0][i] > 1 ? W.half[0][i] : W.hw(i);
    var hwR = W.half && W.half[1][i] > 1 ? W.half[1][i] : W.hw(i);
    var grp = new THREE.Group();
    // the painted line: 1.2 m along the track, edge to edge
    var j0 = Math.max(0, i - 1), j1 = Math.min(d.x.length - 1, i + 1);
    var paint = stripGeo(d, j0, j1, function () { return hwL; }, function () { return -hwR; },
                         function (k) { return d.y[k] + 0.05; }, 1, null);
    if (paint) {
      var uv = paint.attributes.uv;
      for (var v = 0; v < uv.count; v++) uv.setXY(v, (v >> 1) / 2, v % 2);
      var pm = overlayMat({ map: checkerTexture(2, 12, 64, 384) });
      pm.userData.owned = [pm.map];
      var pmesh = new THREE.Mesh(paint, pm);
      pmesh.receiveShadow = true;
      grp.add(pmesh);
    }
    var y0 = d.y[i];
    var red = new THREE.MeshLambertMaterial({ color: 0xC9302C });
    var steel = new THREE.MeshLambertMaterial({ color: 0x3A3F47 });
    var span = hwL + hwR + 4;
    var cx = d.x[i] + tz * (hwL - hwR) / 2, cz = d.z[i] - tx * (hwL - hwR) / 2;
    [-1, 1].forEach(function (s) {
      var off = s * span / 2;
      var leg = new THREE.Mesh(new THREE.BoxGeometry(0.5, 7.4, 0.5), red);
      leg.position.set(cx + tz * off, y0 + 3.7, cz - tx * off);
      leg.castShadow = true;
      grp.add(leg);
    });
    var beam = new THREE.Mesh(new THREE.BoxGeometry(span + 0.5, 0.5, 0.6), steel);
    beam.position.set(cx, y0 + 7.1, cz);
    beam.rotation.y = Math.atan2(-tx, -tz);
    beam.castShadow = true;
    grp.add(beam);
    var bannerTex = checkerTexture(Math.round(span * 1.2), 2, 1024, 96, "START / FINISH");
    var bannerMat = new THREE.MeshLambertMaterial({ map: bannerTex, side: THREE.DoubleSide });
    bannerMat.userData.owned = [bannerTex];
    var banner = new THREE.Mesh(new THREE.PlaneGeometry(span - 0.6, 1.3), bannerMat);
    banner.position.set(cx, y0 + 6.15, cz);
    banner.rotation.y = Math.atan2(-tx, -tz);
    banner.castShadow = true;
    grp.add(banner);
    return grp;
  }


  /* ---- the track vs the line driven on it ----------------------------------
     The ROAD is the circuit: the prepared centreline (OpenStreetMap, re-centred
     on the imagery's tarmac) registered onto this session's GPS - or, without
     one, the consensus of every lap. The DRIVEN line is the GPS, kept on that
     road. Built on boot, when the prepared track arrives and when the
     smoothing changes. */
  var RAW_ASSET = null, TRACK = null, TRACK_HALF = null, TRACK_INFO = null;
  // the facility's whole network (every layout, pit lanes, kart track) in the
  // session frame, and an index over it; GROUND_TEX = imagery of the facility
  var NET = [], NET_IDX = null, GROUND_TEX = null, GROUND_TOK = null;
  var netBias = function (L) { return L.kind === "pit" ? 6 : (L.kind === "kart" ? 8 : 0); };
  var DEM_HR = null, DEM_FAR = null, DEMFN = null, YREF = 0, EVENTS = null, BOARDS = [];

  function shiftGrid(g, dLat, dLon) {
    if (!g) return g;
    var b = g.bounds;
    return Object.assign({}, g, { bounds: [b[0] + dLat, b[1] + dLon, b[2] + dLat, b[3] + dLon] });
  }

  function loadDems(asset) {
    var get = function (which, meta) {
      if (!meta || !meta.file || !asset.slug) return Promise.resolve(null);
      // the file behind this URL is rewritten by re-bakes/enrichment: version
      // the URL by what decodes it, so a cached copy is never a different grid
      var ver = [meta.cols, meta.rows, meta.base, meta.scale,
                 (asset.enrich && asset.enrich.at) || asset.generated || 0].join("-");
      return fetch("/trackassets/" + encodeURIComponent(asset.slug) + "/dem/" + which +
                   "?v=" + encodeURIComponent(ver))
        .then(function (r) { return r.ok ? r.arrayBuffer() : null; })
        .then(function (buf) { return buf ? RC3D.decodeDem(meta, buf) : null; })
        .catch(function () { return null; });
    };
    return Promise.all([get("hr", asset.dem_hr), get("far", asset.dem_far)])
      .then(function (g) { DEM_HR = g[0]; DEM_FAR = g[1]; });
  }

  // per-station [left, right] half widths of a track path: the prepared
  // network's traced edges (the real surface, station by station), widened
  // wherever this session's own driving shows the tarmac is wider, else the
  // width slider. Gaps are bridged from their measured neighbours.
  function trackHalfWidths(T) {
    var nb = T.dense.x.length, hl = new Float64Array(nb), hr = new Float64Array(nb);
    var have = new Uint8Array(nb), sum = 0, cnt = 0, bi;
    var fallback = opts.road / 2;
    if (NET_IDX) {
      // the layout the track runs on, measured edge by edge off the imagery
      for (bi = 0; bi < nb; bi++) {
        var tn = T.dense.tan[bi], hn = NET_IDX.nearest(T.dense.x[bi], T.dense.z[bi], 5);
        if (!hn || Math.abs(hn.nz * tn[0] - hn.nx * tn[1]) < 0.8) continue;
        var lr = RC3D.networkHalfAt(NET, hn, tn[0], tn[1]);
        hl[bi] = lr[0]; hr[bi] = lr[1]; have[bi] = 1; sum += lr[0] + lr[1]; cnt++;
      }
    }
    if (cnt < nb * 0.3 && ASSET && assetSample) {
      sum = 0; cnt = 0;
      for (bi = 0; bi < nb; bi++) {
        var r2 = assetSample(T.dense.x[bi], T.dense.z[bi]);
        var w2 = (r2 && r2.width_m && r2.dist < 25) ? r2.width_m : 0;
        hl[bi] = hr[bi] = w2 / 2;
        have[bi] = w2 ? 1 : 0;
        if (w2) { sum += w2; cnt++; }
      }
    }
    var real = cnt > nb * 0.3 ? sum / cnt : null;
    if (real) {
      RC3D.fillGaps(hl, have, T.closed);
      RC3D.fillGaps(hr, have, T.closed);
      // light smoothing only (9 m): these are MEASURED per station. The old
      // two passes of 31 m flattened every circuit to one number.
      var a1 = RC3D.smooth(Array.prototype.slice.call(hl), 9);
      var a2 = RC3D.smooth(Array.prototype.slice.call(hr), 9);
      for (bi = 0; bi < nb; bi++) { hl[bi] = a1[bi]; hr[bi] = a2[bi]; }
    } else {
      for (bi = 0; bi < nb; bi++) hl[bi] = hr[bi] = fallback;
    }
    // the car WAS on the tarmac: where the session's fixes run past an edge
    // (GPS vs imagery registration, an edge the imagery could not see) the
    // road is wider there - never the driving line on the grass
    var env = null;
    if (real && S.length) {
      env = RC3D.containEnvelope(T, hl, hr, S, T.o, { margin: 0.5, maxOver: 7 });
    }
    // and the inside edge of a tight corner must never fold over itself
    RC3D.foldGuard(T, hl, hr);
    return { half: [hl, hr], real: real, env: env };
  }

  // Never let the track shape take the viewer down: the full build (asset
  // registration, blend, consensus, snapping) falls back to the plain driven
  // line - no asset, no DEM, no snapping - if anything in it throws.
  function setupTrack() {
    try { setupTrackFrom(false); return; }
    catch (e) {
      console.warn("[track3d] track setup failed (" + (e && e.message ? e.message : e) +
                   "); drawing the driven line instead");
    }
    setupTrackFrom(true);
    if (TRACK_INFO) TRACK_INFO.fallback = true;
  }

  function setupTrackFrom(plain) {
    if (!S.length) return;
    var P0 = RC3D.buildPath(S, { smooth: opts.smooth, denseStep: 1 });
    var o = P0.o, i;
    ASSET = plain ? null : RAW_ASSET;
    TRACK_INFO = { source: "laps" };
    var demHr = plain ? null : DEM_HR, demFar = plain ? null : DEM_FAR, line = null, closed = false;
    var CL = null;                              // the laps' consensus, computed once
    var consensus = function () { return CL || (CL = RC3D.consensusLine(P0, LAPS)); };
    if (!plain && RAW_ASSET && RAW_ASSET.line && RAW_ASSET.line.length > 20) {
      var lx = [], lz = [];
      RAW_ASSET.line.forEach(function (p) {
        var q = RC3D.project(p[0], p[1], o); lx.push(q.x); lz.push(q.z);
      });
      var fx = [], fz = [];
      for (i = 0; i < P0.n; i++) if (P0.speed[i] > 20) { fx.push(P0.x[i]); fz.push(P0.z[i]); }
      var reg = fx.length > 50 ? RC3D.registerLine(lx, lz, fx, fz, {}) : null;
      var sh = reg ? Math.sqrt(reg.dx * reg.dx + reg.dz * reg.dz) : Infinity;
      if (reg && reg.inFrac >= 0.6 && reg.medDist <= 6 && sh <= 30) {
        var dLat = -reg.dz / M_LAT, dLon = reg.dx / (M_LAT * Math.cos(o.lat * Math.PI / 180));
        ASSET = RC3D.shiftAsset(RAW_ASSET, dLat, dLon);
        demHr = shiftGrid(DEM_HR, dLat, dLon);
        demFar = shiftGrid(DEM_FAR, dLat, dLon);
        line = { x: lx.map(function (v) { return v + reg.dx; }),
                 z: lz.map(function (v) { return v + reg.dz; }) };
        var e0 = Math.sqrt((line.x[0] - line.x[line.x.length - 1]) * (line.x[0] - line.x[line.x.length - 1]) +
                           (line.z[0] - line.z[line.z.length - 1]) * (line.z[0] - line.z[line.z.length - 1]));
        closed = e0 < 40 && (RAW_ASSET.length_m || 1000) > 500;
        TRACK_INFO = { source: "asset", shift: Math.round(sh * 10) / 10,
                       medDist: Math.round(reg.medDist * 10) / 10,
                       inFrac: Math.round(reg.inFrac * 100) };
        // Which layout was DRIVEN? A prepared Grand Prix line under a short-
        // course session would leave the car on the grass through the link:
        // if the laps leave the prepared line anywhere, the track follows the
        // laps there and the prepared line everywhere else.
        if (LAPS.length) {
          var cl0 = consensus(), aidx = RC3D.lineIndex(line.x, line.z, 20);
          var off0 = 0, tot0 = 0;
          for (i = 0; i < cl0.x.length; i += 3) {
            tot0++;
            if (!aidx.nearest(cl0.x[i], cl0.z[i], 12)) off0++;
          }
          var cover = tot0 ? 1 - off0 / tot0 : 1;
          TRACK_INFO.cover = Math.round(cover * 100);
          if (cover < 0.97 && cl0.x.length > 50 && !cl0.whole) {
            var bl = RC3D.blendOnto(cl0.x, cl0.z, line.x, line.z, { near: 8, blend: 40 });
            var nC = bl.x.length;
            var e2 = Math.sqrt((bl.x[0] - bl.x[nC - 1]) * (bl.x[0] - bl.x[nC - 1]) +
                               (bl.z[0] - bl.z[nC - 1]) * (bl.z[0] - bl.z[nC - 1]));
            line = { x: bl.x, z: bl.z };
            closed = e2 < 30;
            TRACK_INFO.source = "blend";
            TRACK_INFO.matched = Math.round(bl.matched * 100);
            TRACK_INFO.laps = LAPS.length;
          }
        }
      } else if (reg) {
        TRACK_INFO = { source: "laps", rejected: true,
                       medDist: Math.round(reg.medDist * 10) / 10,
                       inFrac: Math.round(reg.inFrac * 100) };
      }
    }
    assetSample = ASSET ? RC3D.assetSampler(ASSET, o) : null;
    DEMFN = plain ? null : RC3D.demStack([demHr, demFar, ASSET && ASSET.dem]);

    // The WHOLE facility: every layout OpenStreetMap knows, re-centred on the
    // imagery. The track you drove is your laps' route THROUGH that network,
    // on the real centrelines wherever they agree - never a road built from
    // your own average line (that drew you dead centre the whole lap).
    // (not when the prepared track was REJECTED as a different place/layout:
    // its network would sit unregistered beside the track)
    NET = (!plain && ASSET && ASSET.network && !TRACK_INFO.rejected) ? RC3D.networkLines(ASSET, o) : [];
    NET_IDX = NET.length ? RC3D.multiIndex(NET, 20, { bias: netBias }) : null;
    if (NET_IDX) {
      // only REAL laps are routed: with none, the "consensus" is the whole
      // session (every lap stacked, pit lane, paddock) - never a track
      var clN = consensus();
      if (clN.x.length > 50 && !clN.whole) {
        var bn = RC3D.blendOnto(clN.x, clN.z, null, null, { index: NET_IDX, near: 12, blend: 30 });
        if (bn.matched >= 0.5) {
          var nN = bn.x.length, used = {};
          line = { x: bn.x, z: bn.z };
          closed = LAPS.length > 0 &&
                   Math.hypot(bn.x[0] - bn.x[nN - 1], bn.z[0] - bn.z[nN - 1]) < 30;
          bn.src.forEach(function (h) {
            if (h) (NET[h.line].names || []).forEach(function (nm) { used[nm] = 1; });
          });
          TRACK_INFO.source = "network";
          TRACK_INFO.matched = Math.round(bn.matched * 100);
          TRACK_INFO.laps = LAPS.length;
          TRACK_INFO.layouts = Object.keys(used);
        }
      }
      TRACK_INFO.network = NET.length;
    }

    if (!line && plain) {
      line = { x: P0.dense.x.slice(), z: P0.dense.z.slice() };
    } else if (!line) {
      var cl = consensus();
      line = { x: cl.x, z: cl.z };
      var n0 = line.x.length;
      var e1 = n0 > 2 ? Math.sqrt((line.x[0] - line.x[n0 - 1]) * (line.x[0] - line.x[n0 - 1]) +
                                  (line.z[0] - line.z[n0 - 1]) * (line.z[0] - line.z[n0 - 1])) : 1e9;
      closed = LAPS.length > 0 && e1 < 30;
      TRACK_INFO.laps = LAPS.length;
    }
    if (closed) line = RC3D.trimLoop(line.x, line.z);
    // does the terrain actually cover the circuit? (a bad asset once floated
    // its ground above the track)
    var covers = !!DEMFN;
    for (i = 0; covers && i < line.x.length; i += 9) {
      var ll = RC3D.localToLatLon(line.x[i], line.z[i], o);
      if (!DEMFN.covers(ll[0], ll[1])) covers = false;
    }
    TRACK_INFO.demOK = covers;
    TRACK_INFO.dem = covers ? ((demHr && demHr.source) || (demFar && demFar.source) || "coarse grid") : null;
    var ys = new Array(line.x.length);
    if (covers) {
      YREF = Infinity;
      for (i = 0; i < line.x.length; i++) {
        var l2 = RC3D.localToLatLon(line.x[i], line.z[i], o);
        ys[i] = DEMFN(l2[0], l2[1]);
        if (ys[i] < YREF) YREF = ys[i];
      }
      for (i = 0; i < ys.length; i++) ys[i] -= YREF;
      ys = RC3D.smooth(ys, 5);
    } else {
      // the logged altitude, carried over from the nearest driven station
      var pidx = RC3D.lineIndex(P0.dense.x, P0.dense.z, 40);
      for (i = 0; i < line.x.length; i++) {
        var h0 = pidx.nearest(line.x[i], line.z[i], 80);
        ys[i] = h0 ? P0.dense.y[Math.min(P0.dense.y.length - 1, h0.i)] : 0;
      }
      YREF = P0.yRef || 0;
    }
    var T = RC3D.linePath(line.x, line.z, ys, { closed: closed, step: 1, o: o });
    TRACK = T;
    var hw = trackHalfWidths(T);
    TRACK_HALF = hw.half;
    realWidth = hw.real;

    // the driven line, kept on that road (a fix a metre or two over the edge
    // is GPS error; one 20 m away is the pit lane and stays where it is)
    var mid = function (k) { return Math.max(TRACK_HALF[0][k], TRACK_HALF[1][k]); };
    var sides = function (k) { return [TRACK_HALF[0][k], TRACK_HALF[1][k]]; };
    var sn = plain ? { samples: S, moved: 0 } : RC3D.snapSamples(S, o, T.dense.x, T.dense.z, sides, {});
    if (hw.real) {
      // the drawn surface's own width, station by station (p10 / median / p90)
      var wsum = [];
      for (i = 0; i < TRACK_HALF[0].length; i += 3) wsum.push(TRACK_HALF[0][i] + TRACK_HALF[1][i]);
      wsum.sort(function (a, b) { return a - b; });
      var wq = function (f) { return Math.round(wsum[Math.min(wsum.length - 1, Math.floor(wsum.length * f))]); };
      if (wsum.length) { TRACK_INFO.wP10 = wq(0.1); TRACK_INFO.wMed = wq(0.5); TRACK_INFO.wP90 = wq(0.9); }
    }
    if (hw.env) {
      TRACK_INFO.onRoad = Math.round(hw.env.inside * 1000) / 10;
      TRACK_INFO.onRoadAfter = Math.round(hw.env.insideAfter * 1000) / 10;
      TRACK_INFO.widened = Math.round(hw.env.widened * 100);
    }
    PATH = RC3D.buildPath(sn.samples, { smooth: opts.smooth, denseStep: 1, o: o });
    TRACK_INFO.snapped = sn.moved;
    // ...and seated on it: the car, the camera and the input line ride the
    // road surface, not the GPS altitude
    var tidx = RC3D.lineIndex(T.dense.x, T.dense.z, 30), d = PATH.dense;
    for (i = 0; i < d.x.length; i++) {
      var h1 = tidx.nearest(d.x[i], d.z[i], 30);
      if (h1 && h1.d < mid(h1.i) + 4) d.y[i] = T.dense.y[h1.i];
      else if (covers) {
        var l3 = RC3D.localToLatLon(d.x[i], d.z[i], o);
        d.y[i] = DEMFN(l3[0], l3[1]) - YREF;
      }
    }
    PATH.yRef = YREF;
    try { computeEvents(tidx); }
    catch (e) {
      EVENTS = { byLap: {}, zones: [], laps: 0, best: 0 };
      console.warn("[track3d] corner events failed:", e && e.message ? e.message : e);
    }
  }

  // Corner events (brake / slowest point / throttle) for every complete lap,
  // each mapped onto the track, and the braking zones that decide which
  // corners deserve brake boards.
  function computeEvents(tidx) {
    EVENTS = { byLap: {}, zones: [], laps: 0, best: 0 };
    if (!PATH || !TRACK) return;
    tidx = tidx || RC3D.lineIndex(TRACK.dense.x, TRACK.dense.z, 30);
    var onT = function (i) {
      var h = tidx.nearest(PATH.x[i], PATH.z[i], 40);
      return h ? h.i : -1;
    };
    var add = function (lapNo, i0, i1) {
      var ev = RC3D.cornerEvents(PATH, { i0: i0, i1: i1 });
      ev.forEach(function (e) {
        e.lap = lapNo;
        e.brake_ti = onT(e.brake_i);
        e.min_ti = onT(e.min_i);
        e.throttle_ti = e.throttle_i != null ? onT(e.throttle_i) : -1;
        if (e.kind === "brake" && e.peak_g >= 0.25 && e.min_ti >= 0) {
          EVENTS.zones.push({ s: TRACK.dense.s[e.min_ti], entry_mph: e.entry_mph, lap: lapNo });
        }
      });
      EVENTS.byLap[lapNo] = ev;
    };
    var best = null;
    LAPS.forEach(function (L) {
      if (!(L.t_end > L.t_start + 5)) return;
      var i0 = RC3D.indexOfTime(PATH.t, L.t_start), i1 = RC3D.indexOfTime(PATH.t, L.t_end);
      if (i1 - i0 < 20) return;
      add(L.lap, i0, i1);
      EVENTS.laps++;
      if (L.seconds && (!best || L.seconds < best.seconds)) best = L;
    });
    if (!LAPS.length) { add(0, 0, PATH.t.length - 1); EVENTS.laps = 1; }
    EVENTS.best = best ? best.lap : 0;
  }

  /* ---- markers: where you braked, the slowest point, back on the throttle -- */
  var EVT_TEX = {};
  function labelTexture(text, bg, fg) {
    var key = text + "|" + bg + "|" + fg;
    if (EVT_TEX[key]) return EVT_TEX[key];
    var c = document.createElement("canvas"), g = null;
    var w = Math.max(160, 36 + text.length * 30);
    c.width = w; c.height = 84;
    try { g = c.getContext("2d"); } catch (e) { g = null; }
    if (!g) return null;
    g.fillStyle = bg;
    var r = 18;
    g.beginPath();
    g.moveTo(r, 4); g.lineTo(w - r, 4); g.quadraticCurveTo(w - 4, 4, w - 4, r);
    g.lineTo(w - 4, 80 - r); g.quadraticCurveTo(w - 4, 80, w - r, 80);
    g.lineTo(r, 80); g.quadraticCurveTo(4, 80, 4, 80 - r);
    g.lineTo(4, r); g.quadraticCurveTo(4, 4, r, 4);
    g.closePath();
    g.fill();
    g.fillStyle = fg;
    g.font = "bold 50px Inter, Arial, sans-serif";
    g.textAlign = "center"; g.textBaseline = "middle";
    g.fillText(text, w / 2, 45);
    var t = new THREE.CanvasTexture(c);
    t.colorSpace = THREE.SRGBColorSpace;
    t.userData = { aspect: w / 84 };
    EVT_TEX[key] = t;
    return t;
  }

  function trackRel(a, b) {
    var v = a - b, tot = TRACK ? TRACK.dense.total : 0;
    if (TRACK && TRACK.closed && tot > 0) {
      while (v > tot / 2) v -= tot;
      while (v < -tot / 2) v += tot;
    }
    return v;
  }

  function makeEventMarkers(list, refList, W) {
    if (!list || !list.length || !W) return null;
    var grp = new THREE.Group(), d = W.d, n = d.x.length;
    var sprites = [];
    var hL = function (k) { return W.half && W.half[0][k] > 1 ? W.half[0][k] : W.hw(k); };
    var hR = function (k) { return W.half && W.half[1][k] > 1 ? W.half[1][k] : W.hw(k); };
    var bar = function (ti, along, colour, alpha, dashes) {
      if (ti < 0) return;
      var half = Math.max(1, Math.round(along / 2));
      var j0 = Math.max(0, ti - half), j1 = Math.min(n - 1, ti + half);
      var pieces = dashes ? dashes * 2 - 1 : 1, p;
      for (p = 0; p < pieces; p += (dashes ? 2 : 1)) {
        var a0 = function (k) { var L = hL(k), R = hR(k); return L - (L + R) * p / pieces; };
        var b0 = function (k) { var L = hL(k), R = hR(k); return L - (L + R) * (p + 1) / pieces; };
        var geo = stripGeo(d, j0, j1, a0, b0, function (k) { return d.y[k] + 0.075; }, 1, null);
        if (!geo) continue;
        var m = new THREE.MeshBasicMaterial({ color: colour, transparent: true, opacity: alpha,
          depthWrite: false, side: THREE.DoubleSide, polygonOffset: true,
          polygonOffsetFactor: -4, polygonOffsetUnits: -8 });
        m.toneMapped = false;
        var mesh = new THREE.Mesh(geo, m);
        mesh.renderOrder = 4;
        grp.add(mesh);
      }
    };
    var label = function (ti, side, height, text, bg, fg, size) {
      if (ti < 0) return;
      var tex = labelTexture(text, bg, fg);
      if (!tex) return;
      var tx = d.tan[ti][0], tz = d.tan[ti][1];
      var off = side * ((side > 0 ? hL(ti) : hR(ti)) + 1.2);
      var x = d.x[ti] + tz * off, z = d.z[ti] - tx * off;
      var spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, toneMapped: false,
                                                            depthWrite: false }));
      var h = 1.3 * (size || 1), asp = (tex.userData && tex.userData.aspect) || 2.5;
      spr.scale.set(h * asp, h, 1);
      spr.userData.base = [h * asp, h];
      spr.position.set(x, W.groundY(x, z) + height, z);
      spr.renderOrder = 6;
      grp.add(spr);
      sprites.push(spr);
    };
    var findRef = function (e) {
      if (!refList) return null;
      var best = null, bd = 90;
      refList.forEach(function (r) {
        if (r.kind !== e.kind || r.min_ti < 0) return;
        var dd = Math.abs(trackRel(d.s[r.min_ti], d.s[e.min_ti]));
        if (dd < bd) { bd = dd; best = r; }
      });
      return best;
    };
    list.forEach(function (e) {
      if (e.min_ti < 0) return;
      var turnSide = 1;                     // labels on the driver's left...
      var ti = e.brake_ti;
      if (e.kind === "brake") {
        bar(ti, 0.5, 0xFF3B30, 0.88, 0);
        var ref = findRef(e), txt = "BRAKE " + Math.round(e.brake_mph);
        if (ref && ref.brake_ti >= 0) {
          var dm = Math.round(trackRel(d.s[e.brake_ti], d.s[ref.brake_ti]));
          if (Math.abs(dm) >= 3) {
            bar(ref.brake_ti, 0.45, 0xFFFFFF, 0.8, 5);
            label(ref.brake_ti, -turnSide, 1.7, "BEST", "rgba(240,242,246,0.92)", "#15171C", 0.8);
            txt += "  " + (dm > 0 ? "+" : "") + dm + "m";
          }
        }
        label(ti, turnSide, 2.6, txt, "rgba(214,40,32,0.92)", "#FFFFFF", 1.45);
      } else {
        bar(ti, 0.45, 0xFFC21A, 0.82, 0);
        label(ti, turnSide, 2.2, "LIFT " + Math.round(e.brake_mph), "rgba(250,190,30,0.92)", "#1A1300", 0.9);
      }
      // the slowest point: a small amber flag over the line
      var mx = PATH.x[e.min_i], mz = PATH.z[e.min_i];
      var mq = W.field.nearest(mx, mz);
      if (mq) {
        var tex = labelTexture("MIN " + Math.round(e.min_mph), "rgba(255,176,32,0.95)", "#1A1300");
        if (tex) {
          var spr = new THREE.Sprite(new THREE.SpriteMaterial({ map: tex, toneMapped: false,
                                                                depthWrite: false }));
          var asp = (tex.userData && tex.userData.aspect) || 2.5;
          spr.scale.set(1.1 * asp, 1.1, 1);
          spr.userData.base = [1.1 * asp, 1.1];
          spr.position.set(mx, d.y[mq.i] + 2.0, mz);
          spr.renderOrder = 6;
          grp.add(spr);
          sprites.push(spr);
        }
      }
      if (e.throttle_ti >= 0 && e.throttle_i != null) {
        bar(e.throttle_ti, 0.4, 0x34D058, 0.8, 0);
        label(e.throttle_ti, turnSide, 1.8, "GAS " + Math.round(e.throttle_mph),
              "rgba(36,170,72,0.92)", "#FFFFFF", 0.8);
      }
    });
    grp.userData.sprites = sprites;
    return grp;
  }

  /* ---- OpenStreetMap: buildings, grandstands, water, paddock roads, fences -- */
  function fxz(p) { var q = RC3D.project(p[0], p[1], PATH.o); return [q.x, q.z]; }
  function ringArea(r) {
    var a = 0;
    for (var k = 0; k < r.length; k++) {
      var k2 = (k + 1) % r.length;
      a += r[k][0] * r[k2][1] - r[k2][0] * r[k][1];
    }
    return Math.abs(a / 2);
  }
  function clearOfTrack(W, x, z) {
    var q = W.field.nearest(x, z);
    return q ? q.d - W.hw(q.i) : Infinity;
  }
  function bldHeight(b, area, rnd) {
    if (b.h && b.h > 1.5 && b.h < 150) return b.h;
    if (b.l && b.l > 0 && b.l < 50) return b.l * 3.2 + 0.8;
    var k = String(b.k || "yes"), r = 0.85 + 0.3 * rnd();
    if (k === "grandstand") return 9 * r;
    if (/^(garage|garages|shed|carport|hut|kiosk|toilets|cabin|container)$/.test(k)) return 3.2 * r;
    if (k === "roof") return 4.6 * r;
    if (/^(house|detached|residential|apartments|terrace|semidetached_house)$/.test(k)) return 7 * r;
    if (/^(hospital|hotel|office|dormitory)$/.test(k)) return 11 * r;
    if (/^(industrial|warehouse|commercial|retail|hangar|service|transportation|barn)$/.test(k)) return 7.5 * r;
    if (area < 50) return 3.2 * r;
    if (area < 250) return 4.8 * r;
    if (area < 1200) return 6.5 * r;
    return 8 * r;
  }
  var WIN_TEX = null;
  function windowTexture() {
    if (WIN_TEX) return WIN_TEX;
    var c = document.createElement("canvas"), g = null;
    c.width = 128; c.height = 112;
    try { g = c.getContext("2d"); } catch (e) { return null; }
    g.fillStyle = "#FFFFFF"; g.fillRect(0, 0, 128, 112);
    g.fillStyle = "#E8E8E8"; g.fillRect(0, 104, 128, 8);        // floor band
    g.fillStyle = "#3C4652"; g.fillRect(30, 30, 68, 44);         // a window
    g.fillStyle = "#56616E"; g.fillRect(30, 30, 68, 10);
    g.fillStyle = "#D8D8D8"; g.fillRect(62, 30, 4, 44);
    WIN_TEX = new THREE.CanvasTexture(c);
    WIN_TEX.wrapS = WIN_TEX.wrapT = THREE.RepeatWrapping;
    WIN_TEX.repeat.set(1 / 4.2, 1 / 3.4);
    WIN_TEX.colorSpace = THREE.SRGBColorSpace;
    try { WIN_TEX.anisotropy = renderer.capabilities.getMaxAnisotropy(); } catch (e) {}
    return WIN_TEX;
  }

  function buildBuildings(W, F) {
    var list = (F && F.buildings) || [];
    if (!list.length) return null;
    var rnd = seededRandom(TRACK_SEED + ":bld");
    var walls = { pos: [], nor: [], uv: [], col: [] }, caps = { pos: [], nor: [], col: [] };
    var pal = [[0.80, 0.78, 0.74], [0.88, 0.87, 0.84], [0.72, 0.68, 0.62], [0.64, 0.67, 0.71],
               [0.70, 0.52, 0.42], [0.92, 0.92, 0.90], [0.58, 0.60, 0.56]];
    var roofs = [[0.36, 0.37, 0.39], [0.45, 0.43, 0.41], [0.30, 0.33, 0.36], [0.52, 0.30, 0.26]];
    var nb = 0;
    list.forEach(function (b) {
      if (!b.p || b.p.length < 3) return;
      var ring = b.p.map(fxz);
      var gmin = Infinity, gmax = -Infinity, clear = Infinity, k;
      for (k = 0; k < ring.length; k++) {
        var y = W.groundY(ring[k][0], ring[k][1]);
        if (y < gmin) gmin = y;
        if (y > gmax) gmax = y;
        clear = Math.min(clear, clearOfTrack(W, ring[k][0], ring[k][1]));
      }
      if (clear < 0.8 || !isFinite(gmin)) return;             // over the tarmac: bad data
      var area = ringArea(ring);
      if (area < 6 || area > 60000) return;
      var stand = String(b.k) === "grandstand";
      var h = bldHeight(b, area, rnd);
      var shape = new THREE.Shape(ring.map(function (q) { return new THREE.Vector2(q[0], -q[1]); }));
      var geo;
      try {
        geo = new THREE.ExtrudeGeometry(shape, { depth: h + (gmax - gmin) + 0.4,
                                                 bevelEnabled: false, steps: 1 });
      } catch (e) { return; }
      geo.rotateX(-Math.PI / 2);
      geo.translate(0, gmin - 0.4, 0);
      if (geo.index) geo = geo.toNonIndexed();
      var P = geo.attributes.position, N = geo.attributes.normal, U = geo.attributes.uv;
      var topY = gmax + h;
      if (stand) {
        // the seating rake: low at the front (nearest the track), up at the back
        var dmin = Infinity, dmax = -Infinity;
        for (k = 0; k < ring.length; k++) {
          var cl = clearOfTrack(W, ring[k][0], ring[k][1]);
          dmin = Math.min(dmin, cl); dmax = Math.max(dmax, cl);
        }
        for (k = 0; k < P.count; k++) {
          if (P.getY(k) < topY - 0.01) continue;
          var t = dmax > dmin + 0.5 ? (clearOfTrack(W, P.getX(k), P.getZ(k)) - dmin) / (dmax - dmin) : 1;
          P.setY(k, gmax + 1.4 + (h - 1.4) * Math.max(0, Math.min(1, t)));
        }
        geo.computeVertexNormals();
        N = geo.attributes.normal;
      }
      var wc = stand ? [0.74, 0.74, 0.72] : pal[Math.floor(rnd() * pal.length)];
      var rc = stand ? [0.20, 0.36, 0.62] : roofs[Math.floor(rnd() * roofs.length)];
      var groups = geo.groups && geo.groups.length ? geo.groups : [{ start: 0, count: P.count, materialIndex: 1 }];
      groups.forEach(function (gr) {
        var isCap = gr.materialIndex === 0;
        for (k = gr.start; k < gr.start + gr.count; k++) {
          var tgt = (isCap || stand) ? caps : walls;
          var c = isCap ? rc : wc;
          tgt.pos.push(P.getX(k), P.getY(k), P.getZ(k));
          tgt.nor.push(N.getX(k), N.getY(k), N.getZ(k));
          tgt.col.push(c[0], c[1], c[2]);
          if (tgt === walls) tgt.uv.push(U ? U.getX(k) : 0, U ? U.getY(k) : 0);
        }
      });
      geo.dispose();
      nb++;
    });
    if (!nb) return null;
    var grp = new THREE.Group();
    var mk = function (buf, map) {
      if (!buf.pos.length) return;
      var g = new THREE.BufferGeometry();
      g.setAttribute("position", new THREE.Float32BufferAttribute(buf.pos, 3));
      g.setAttribute("normal", new THREE.Float32BufferAttribute(buf.nor, 3));
      g.setAttribute("color", new THREE.Float32BufferAttribute(buf.col, 3));
      if (buf.uv) g.setAttribute("uv", new THREE.Float32BufferAttribute(buf.uv, 2));
      var m = new THREE.MeshLambertMaterial({ vertexColors: true, map: map || null });
      var mesh = new THREE.Mesh(g, m);
      mesh.castShadow = true; mesh.receiveShadow = true;
      grp.add(mesh);
    };
    mk(walls, windowTexture());
    mk(caps, null);
    grp.userData.count = nb;
    return grp;
  }

  function buildWater(W, F) {
    var list = (F && F.water) || [];
    if (!list.length) return null;
    var grp = new THREE.Group();
    var mat = new THREE.MeshStandardMaterial({ color: 0x2E5F78, roughness: 0.1, metalness: 0.0,
      envMap: scene.background && scene.background.isTexture ? scene.background : null,
      envMapIntensity: 0.85, polygonOffset: true, polygonOffsetFactor: -1, polygonOffsetUnits: -2 });
    list.forEach(function (rg) {
      if (!rg || rg.length < 3) return;
      var ring = rg.map(fxz), ys = [], k, clear = Infinity;
      for (k = 0; k < ring.length; k++) {
        ys.push(W.groundY(ring[k][0], ring[k][1]));
        clear = Math.min(clear, clearOfTrack(W, ring[k][0], ring[k][1]));
      }
      if (clear < 2 || ringArea(ring) < 30) return;
      ys.sort(function (a, b) { return a - b; });
      var level = ys[Math.floor(ys.length * 0.15)] + 0.18;
      var shape = new THREE.Shape(ring.map(function (q) { return new THREE.Vector2(q[0], -q[1]); }));
      var geo;
      try { geo = new THREE.ShapeGeometry(shape); } catch (e) { return; }
      geo.rotateX(-Math.PI / 2);
      geo.translate(0, level, 0);
      var mesh = new THREE.Mesh(geo, mat);
      mesh.receiveShadow = true;
      grp.add(mesh);
    });
    return grp.children.length ? grp : null;
  }

  var ROAD_W = { motorway: 11, trunk: 10, primary: 8, secondary: 7.5, tertiary: 6.5,
                 residential: 6, unclassified: 5.5, living_street: 5, service: 4.2,
                 raceway: 9, track: 3, path: 1.6, footway: 1.6, cycleway: 2,
                 bridleway: 2, pedestrian: 3 };
  function ribbonAlong(segs, lift, uvM, W) {
    var pos = [], uv = [], idx = [], nv = 0;
    segs.forEach(function (sg) {
      var pts = sg.pts, w = sg.w / 2, k, along = 0;
      if (pts.length < 2) return;
      for (k = 0; k < pts.length; k++) {
        var a = pts[Math.max(0, k - 1)], b = pts[Math.min(pts.length - 1, k + 1)];
        var tx = b[0] - a[0], tz = b[1] - a[1], L = Math.sqrt(tx * tx + tz * tz) || 1;
        tx /= L; tz /= L;
        if (k) along += Math.sqrt((pts[k][0] - pts[k - 1][0]) * (pts[k][0] - pts[k - 1][0]) +
                                  (pts[k][1] - pts[k - 1][1]) * (pts[k][1] - pts[k - 1][1]));
        var lx = pts[k][0] + tz * w, lz = pts[k][1] - tx * w;
        var rx = pts[k][0] - tz * w, rz = pts[k][1] + tx * w;
        pos.push(lx, W.groundY(lx, lz) + lift, lz, rx, W.groundY(rx, rz) + lift, rz);
        uv.push(along / uvM, 0, along / uvM, sg.w / uvM);
        if (k) idx.push(nv - 2, nv, nv - 1, nv - 1, nv, nv + 1);
        nv += 2;
      }
    });
    if (!nv) return null;
    var g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
    g.setIndex(idx);
    g.computeVertexNormals();
    return g;
  }

  function buildRoads(W, F) {
    var list = (F && F.roads) || [];
    if (!list.length) return null;
    var buckets = { asph: [], dirt: [], foot: [] };
    var near = function (x, z) { return { i: 0, d: W.edgeDist(x, z) }; };
    list.forEach(function (r) {
      if (!r.p || r.p.length < 2) return;
      var k = r.k || "service";
      var w = (r.w > 1 && r.w < 30) ? r.w : (ROAD_W[k] || 4);
      var segs = RC3D.cutNearRoad(r.p.map(fxz), 3, near,
                                  function () { return w / 2 + 0.8; });
      var b = k === "track" ? buckets.dirt
        : (/^(path|footway|cycleway|bridleway|steps)$/.test(k) ? buckets.foot : buckets.asph);
      segs.forEach(function (sg) { b.push({ pts: sg, w: w }); });
    });
    var T = surfaces(), grp = new THREE.Group();
    var add = function (segs, tex, colour, lift) {
      var g = ribbonAlong(segs, lift, 6, W);
      if (!g) return;
      var m = new THREE.MeshLambertMaterial({ map: tex || null, color: colour, side: THREE.DoubleSide });
      m.polygonOffset = true; m.polygonOffsetFactor = -1; m.polygonOffsetUnits = -2;
      var mesh = new THREE.Mesh(g, m);
      mesh.receiveShadow = true;
      grp.add(mesh);
    };
    add(buckets.asph, null, 0x5B5E62, 0.09);
    add(buckets.dirt, T.dirt, 0xFFFFFF, 0.07);
    add(buckets.foot, T.gravel, 0xE8E2D6, 0.08);
    return grp.children.length ? grp : null;
  }

  var FENCE_TEX = null;
  function fenceTexture() {
    if (FENCE_TEX) return FENCE_TEX;
    var c = document.createElement("canvas"), g = null;
    c.width = 64; c.height = 64;
    try { g = c.getContext("2d"); } catch (e) { return null; }
    g.clearRect(0, 0, 64, 64);
    g.strokeStyle = "rgba(205,210,214,1)";
    g.lineWidth = 3;
    g.beginPath();
    g.moveTo(0, 0); g.lineTo(64, 64);
    g.moveTo(64, 0); g.lineTo(0, 64);
    g.stroke();
    FENCE_TEX = new THREE.CanvasTexture(c);
    FENCE_TEX.wrapS = FENCE_TEX.wrapT = THREE.RepeatWrapping;
    FENCE_TEX.colorSpace = THREE.SRGBColorSpace;
    return FENCE_TEX;
  }

  // a vertical strip (two faces + a cap) along a polyline: walls, hedges, Armco
  function wallGeo(segs, h, th, W, lift0) {
    var pos = [], idx = [], nv = 0;
    segs.forEach(function (pts) {
      var k, base = nv;
      if (pts.length < 2) return;
      for (k = 0; k < pts.length; k++) {
        var a = pts[Math.max(0, k - 1)], b = pts[Math.min(pts.length - 1, k + 1)];
        var tx = b[0] - a[0], tz = b[1] - a[1], L = Math.sqrt(tx * tx + tz * tz) || 1;
        var nx = tz / L * th / 2, nz = -tx / L * th / 2;
        var y0 = W.groundY(pts[k][0], pts[k][1]) + (lift0 || 0) - 0.15;
        var y1 = y0 + h + 0.15;
        pos.push(pts[k][0] + nx, y0, pts[k][1] + nz, pts[k][0] + nx, y1, pts[k][1] + nz,
                 pts[k][0] - nx, y1, pts[k][1] - nz, pts[k][0] - nx, y0, pts[k][1] - nz);
        if (k) {
          var p = base + (k - 1) * 4, q = base + k * 4;
          idx.push(p, q, p + 1, p + 1, q, q + 1);              // side A
          idx.push(p + 1, q + 1, p + 2, p + 2, q + 1, q + 2);  // top
          idx.push(p + 2, q + 2, p + 3, p + 3, q + 2, q + 3);  // side B
        }
      }
      nv += pts.length * 4;
    });
    if (!nv) return null;
    var g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
    g.setIndex(idx);
    g.computeVertexNormals();
    return g;
  }

  function buildBarriers(W, F) {
    var list = (F && F.barriers) || [];
    if (!list.length) return null;
    var near = function (x, z) { return { i: 0, d: W.edgeDist(x, z) }; };
    var kinds = { wall: [], fence: [], rail: [], hedge: [] };
    list.forEach(function (b) {
      if (!b.p || b.p.length < 2) return;
      var k = String(b.k || "fence");
      var bucket = /^(wall|retaining_wall|city_wall|jersey_barrier)$/.test(k) ? kinds.wall
        : k === "guard_rail" ? kinds.rail : k === "hedge" ? kinds.hedge : kinds.fence;
      RC3D.cutNearRoad(b.p.map(fxz), 2, near, function () { return 0.45; })
        .forEach(function (sg) { bucket.push(sg); });
    });
    var grp = new THREE.Group(), g;
    if ((g = wallGeo(kinds.wall, 1.15, 0.35, W))) {
      var wm = new THREE.Mesh(g, new THREE.MeshLambertMaterial({ color: 0xB9B6AE }));
      wm.castShadow = true; wm.receiveShadow = true; grp.add(wm);
    }
    if ((g = wallGeo(kinds.hedge, 1.5, 1.0, W))) {
      var hm = new THREE.Mesh(g, new THREE.MeshLambertMaterial({ color: 0x3D6B2E }));
      hm.castShadow = true; grp.add(hm);
    }
    if ((g = wallGeo(kinds.rail, 0.35, 0.08, W, 0.45))) {
      grp.add(new THREE.Mesh(g, new THREE.MeshLambertMaterial({ color: 0xC6CBD1 })));
    }
    if (kinds.fence.length) {
      // chain-link panels (alpha-tested, so no sorting trouble) on posts
      var pos = [], uv = [], idx = [], nv = 0, posts = [];
      kinds.fence.forEach(function (pts) {
        var along = 0, k, sinceP = 99;
        for (k = 0; k < pts.length; k++) {
          if (k) along += Math.sqrt((pts[k][0] - pts[k - 1][0]) * (pts[k][0] - pts[k - 1][0]) +
                                    (pts[k][1] - pts[k - 1][1]) * (pts[k][1] - pts[k - 1][1]));
          var y0 = W.groundY(pts[k][0], pts[k][1]);
          pos.push(pts[k][0], y0, pts[k][1], pts[k][0], y0 + 1.9, pts[k][1]);
          uv.push(along / 0.5, 0, along / 0.5, 1.9 / 0.5);
          if (k) idx.push(nv - 2, nv, nv - 1, nv - 1, nv, nv + 1);
          nv += 2;
          sinceP += k ? 2 : 0;
          if (sinceP >= 3 || k === pts.length - 1) { posts.push([pts[k][0], y0, pts[k][1]]); sinceP = 0; }
        }
        nv += 0;
      });
      // panels must not join across separate fences
      var fg = new THREE.BufferGeometry();
      fg.setAttribute("position", new THREE.Float32BufferAttribute(pos, 3));
      fg.setAttribute("uv", new THREE.Float32BufferAttribute(uv, 2));
      fg.setIndex(idx);
      fg.computeVertexNormals();
      var fm = new THREE.MeshLambertMaterial({ color: 0xFFFFFF, map: fenceTexture(),
        alphaTest: 0.35, transparent: false, side: THREE.DoubleSide });
      grp.add(new THREE.Mesh(fg, fm));
      if (posts.length) {
        var pg = new THREE.BoxGeometry(0.07, 2.0, 0.07);
        pg.translate(0, 1.0, 0);
        var im = new THREE.InstancedMesh(pg, new THREE.MeshLambertMaterial({ color: 0x5A6068 }), posts.length);
        var m4 = new THREE.Matrix4();
        posts.forEach(function (p, k) { m4.makeTranslation(p[0], p[1], p[2]); im.setMatrixAt(k, m4); });
        im.instanceMatrix.needsUpdate = true;
        im.computeBoundingSphere();
        grp.add(im);
      }
    }
    return grp.children.length ? grp : null;
  }

  function osmTreeSpots(F) {
    var out = [];
    if (!F) return out;
    (F.trees || []).forEach(function (p) { var q = fxz(p); out.push({ x: q[0], z: q[1] }); });
    (F.tree_rows || []).forEach(function (row) {
      var pts = row.map(fxz), k, j;
      for (k = 0; k + 1 < pts.length; k++) {
        var L = Math.sqrt((pts[k + 1][0] - pts[k][0]) * (pts[k + 1][0] - pts[k][0]) +
                          (pts[k + 1][1] - pts[k][1]) * (pts[k + 1][1] - pts[k][1]));
        var nseg = Math.max(1, Math.round(L / 7));
        for (j = 0; j < nseg; j++) out.push({ x: pts[k][0] + (pts[k + 1][0] - pts[k][0]) * j / nseg,
                                               z: pts[k][1] + (pts[k + 1][1] - pts[k][1]) * j / nseg });
      }
    });
    return out;
  }

  // imagery land cover with OSM's woods / car parks / water burned in (or,
  // with no imagery, a grid from OSM alone) - cached on the asset object
  function landcoverFor(a) {
    if (!a) return null;
    if (a._lcFinal !== undefined) return a._lcFinal;
    var lc = decodeLandcover(a), res = lc;
    if (a.features) {
      var b = null;
      if (!lc) {
        var src = (a.dem_hr && a.dem_hr.bounds) || (a.dem && a.dem.bounds) || a.bbox;
        if (src) {
          var mLat = 600 / M_LAT, mLon = 600 / (M_LAT * Math.cos(src[0] * Math.PI / 180));
          b = [src[0] - mLat, src[1] - mLon, src[2] + mLat, src[3] + mLon];
        }
      }
      try {
        res = RC3D.burnLandcover(lc ? { cols: lc.cols, rows: lc.rows, bounds: lc.bounds,
                                        _codes: lc._codes, cell_m: lc.cell_m } : null,
                                 a.features, { bounds: b, cell: 4 }) || lc;
      } catch (e) {
        console.warn("[track3d] land cover burn:", e && e.message);
        res = lc;
      }
    }
    a._lcFinal = res;
    return res;
  }

  // Sub-path for one lap window, sharing the dense centreline of the session
  // path. Used for the road (so overlapping laps cannot z-fight) and ghosts.
  function slicePath(path, t0, t1) {
    var sA = RC3D.sAtTime(path, t0), sB = RC3D.sAtTime(path, t1);
    if (!(sB > sA + 5)) return null;
    var d = path.dense, i, cum = 0, prev = null;
    // sample arc length (path.cum, 2D) and spline arc length (dense.s) differ by
    // a fraction of a percent - tens of metres by the end of a long session -
    // so the window is converted (through the spline knots), never compared
    var dA = RC3D.cumToDense(path, sA), dB = RC3D.cumToDense(path, sB), dFirst = null;
    var out = { o: path.o, x: [], y: [], z: [], cum: [], speed: [], t: [], total: 0,
                accel: [], input: { state: [], level: [] },
                dense: { x: [], y: [], z: [], s: [], tan: [], total: 0 } };
    for (i = 0; i < d.s.length; i++) {
      if (d.s[i] < dA || d.s[i] > dB) continue;
      if (prev) {
        var dx = d.x[i] - prev[0], dy = d.y[i] - prev[1], dz = d.z[i] - prev[2];
        cum += Math.sqrt(dx * dx + dy * dy + dz * dz);
      }
      if (dFirst === null) dFirst = d.s[i];
      out.dense.x.push(d.x[i]); out.dense.y.push(d.y[i]); out.dense.z.push(d.z[i]);
      out.dense.s.push(cum); out.dense.tan.push(d.tan[i]);
      prev = [d.x[i], d.y[i], d.z[i]];
    }
    if (out.dense.x.length < 3) return null;
    out.dense.total = cum;
    out.total = sB - sA;
    if (d.knC && d.knC.length > 1) {          // the parent's knots, re-based
      var kc = [0], ks = [0], kq;
      for (kq = 0; kq < d.knC.length; kq++) {
        if (d.knC[kq] <= sA || d.knC[kq] >= sB) continue;
        kc.push(d.knC[kq] - sA);
        ks.push(Math.min(cum, Math.max(ks[ks.length - 1], d.knS[kq] - dFirst)));
      }
      kc.push(out.total); ks.push(Math.max(ks[ks.length - 1], cum));
      out.dense.knC = kc; out.dense.knS = ks;
    }
    for (i = 0; i < path.cum.length; i++) {
      if (path.cum[i] < sA || path.cum[i] > sB) continue;
      out.cum.push(path.cum[i] - sA);
      out.speed.push(path.speed[i]);
      out.accel.push(path.accel ? path.accel[i] : 0);
      out.input.state.push(path.input ? path.input.state[i] : 0);
      out.input.level.push(path.input ? path.input.level[i] : 0);
      out.t.push(path.t[i]);
      out.x.push(path.x[i]); out.y.push(path.y[i]); out.z.push(path.z[i]);
    }
    if (out.cum.length < 2) return null;
    return out;
  }

  // RC3D.corners / brakeMarkers / ribbons speak dense (spline) arc length;
  // pointAtS / accelAtS take sample arc length. Convert at the boundary.
  function denseToCum(path, sd) {
    return RC3D.denseToCum(path, sd);
  }

  function lapObj(lapNo) {
    var i, L = null;
    for (i = 0; i < LAPS.length; i++) if (LAPS[i].lap === lapNo) L = LAPS[i];
    return L;
  }

  function addMesh(key, m) {
    meshes[key] = m || null;
    if (m) scene.add(m);
    return m;
  }

  function rebuild() {
    var t0 = (window.performance && performance.now) ? performance.now() : 0;
    try {
      disposeMeshes();
      if (!PATH) return;
      if (!TRACK) setupTrack();
      var L = lapObj(LAPNO), base = PATH, sub = null;
      if (L) { sub = slicePath(PATH, L.t_start, L.t_end); if (sub) base = sub; }
      BASE = base;
      LC = landcoverFor(ASSET);
      // the ROAD is the circuit (TRACK): built once from the prepared
      // centreline or the laps' consensus, whichever lap is being viewed
      if (!realWidth) {                 // the width slider decides
        var hw0 = trackHalfWidths(TRACK);
        TRACK_HALF = hw0.half;
      }
      var half = TRACK_HALF;
      if (el("b-road")) {
        el("b-road").disabled = !!realWidth;
        var lab = el("b-road").parentNode;
        if (lab) lab.title = realWidth ? "width comes from the prepared track (" +
          realWidth.toFixed(1) + " m)" : "road width (no prepared track)";
      }
      var useW = realWidth ? realWidth : opts.road;
      var W = WORLD = buildWorld(TRACK, half);
      var sat = opts.ground === "satellite" && TEX && ASSET && ASSET.texture;
      var F = ASSET && ASSET.features;

      // 1. the ground (simulated land cover, or the satellite drape)
      if (opts.ground !== "none") addMesh("ground", buildGround(W));
      // 2. the tarmac (procedural, or the imagery of the real surface)
      addMesh("road", makeRoad(TRACK, useW, 0.03, false, null, 1, sat
        ? { half: half, uvBounds: ASSET.texture.bounds, tex: TEX, o: PATH.o }
        : { asphalt: true, half: half, o: PATH.o }));
      if (!sat) addMesh("edges", makeEdgeLines(W));
      // (over satellite ground the imagery already shows the other layouts)
      if (W.net && !(sat && GROUND_TEX)) {
        try { addMesh("network", makeNetwork(W)); }
        catch (e) { console.warn("[track3d] network:", e && e.message ? e.message : e); }
      }
      // 3. what the driver did: a chevron line (driving view) and a full-width
      //    wash (plan view), both coloured by the classified input
      addMesh("line", makeInputRibbon(base, 1.05, 0.09, "line", null));
      addMesh("wash", makeInputRibbon(base, Math.min(useW, 6), 0.08, "wash", null));
      CORNERS = RC3D.corners(TRACK, {});
      // everything from here on is optional dressing: one failing piece must
      // not take the car marker, the ideal line or the legend down with it
      var opt = function (key, fn) {
        try { addMesh(key, fn()); }
        catch (e) { console.warn("[track3d] " + key + ":", e && e.message ? e.message : e); }
      };
      if (!sat) opt("kerbs", function () { return makeKerbs(W); });
      BOARDS = [];
      if (opts.brakes) {
        opt("signs", function () {
          BOARDS = (EVENTS && EVENTS.zones.length)
            ? RC3D.boardsFromData(TRACK, CORNERS, EVENTS.zones, { laps: EVENTS.laps })
            : RC3D.brakeMarkers(TRACK, CORNERS, {});
          return makeBrakeSigns(BOARDS, TRACK, W);
        });
      }
      if (opts.markers && EVENTS) {
        var lapNo = LAPNO || EVENTS.best || 0;
        var refs = (EVENTS.best && EVENTS.best !== lapNo) ? EVENTS.byLap[EVENTS.best] : null;
        opt("events", function () { return makeEventMarkers(EVENTS.byLap[lapNo], refs, W); });
      }
      if (opts.ghost && LAPS.length > 1) {
        var grp = new THREE.Group();
        LAPS.forEach(function (LL) {
          if (LL.lap === LAPNO) return;
          var g = slicePath(PATH, LL.t_start, LL.t_end);
          if (g) grp.add(makeRoad(g, 1.8, 0.05, false, [0.32, 0.34, 0.38], 0.65));
        });
        addMesh("ghost", grp);
      }
      opt("gantry", function () {
        return makeGantry(LAPS.sf, W, (L && sub) ? [sub.dense.x[2], sub.dense.z[2]] : null);
      });
      // trackside: gravel + Armco outside the corners, trees where the imagery
      // (or OpenStreetMap) says woods, never on or over any part of the
      // circuit; buildings, grandstands, water, paddock roads and fences from
      // OpenStreetMap; corner labels for the plan view
      if (opts.dressing) {
        opt("barriers", function () { return makeRunoff(W, CORNERS, sat); });
        opt("trees", function () { return buildTrees(W, TRACK_SEED); });
        opt("labels", function () { return buildCornerLabels(TRACK, CORNERS); });
        if (F) {
          opt("buildings", function () { return buildBuildings(W, F); });
          opt("water", function () { return buildWater(W, F); });
          if (!sat) opt("roads", function () { return buildRoads(W, F); });
          opt("fences", function () { return buildBarriers(W, F); });
        }
      }
      addMesh("car", makeCarMarker());
      placeIdeal();
      applyViewVisibility();
      updateTrackLegend();
      if (t0) console.info("[track3d] world built in " + Math.round(performance.now() - t0) +
                           " ms: " + (meshes.trees && meshes.trees.userData.count || 0) + " trees, " +
                           (meshes.buildings && meshes.buildings.userData.count || 0) + " buildings, " +
                           (LC ? (LC.osmOnly ? "OSM land cover" : "land cover") : "procedural woods") +
                           ", track from " + (TRACK_INFO ? TRACK_INFO.source : "?") +
                           (TRACK_INFO && TRACK_INFO.dem ? ", terrain " + TRACK_INFO.dem : ""));
    } catch (e) {
      console.warn("[track3d] rebuild:", e && e.message ? e.message : e, e && e.stack);
    }
  }

  function updateTrackLegend() {
    var lg = el("lg-src");
    if (!lg || !TRACK_INFO) return;
    var t = TRACK_INFO, parts = [];
    if (t.source === "network") {
      parts.push("track: your route through the facility (" +
                 ((t.layouts && t.layouts.length) ? t.layouts.slice(0, 3).join(" + ") : "OpenStreetMap") +
                 "), real centrelines for " + t.matched + "% of it");
      var others = (WORLD && WORLD.net) ? WORLD.net.pieces.length : 0;
      if (others) parts.push(others + " other layout pieces shown");
      if (t.wMed) {
        parts.push("surface: " + t.wP10 + "\u2013" + t.wP90 + " m wide (median " + t.wMed +
                   " m), traced edge by edge from the imagery" +
                   (t.widened ? "; widened on " + t.widened + "% of it to fit your laps" : "") +
                   (t.onRoadAfter ? " \u00b7 " + t.onRoadAfter + "% of your fixes on the tarmac" : ""));
        var lt = el("lg-track");
        if (lt && lt.dataset && lt.dataset.head) {
          lt.textContent = lt.dataset.head + " \u2014 " + t.wP10 + "\u2013" + t.wP90 +
            " m wide (traced from imagery)" + (lt.dataset.tail || "");
        }
      }
    } else if (t.source === "asset") {
      parts.push("track shape: OpenStreetMap + imagery, aligned to your GPS (moved " + t.shift + " m)");
    } else if (t.source === "blend") {
      parts.push("track shape: the layout you drove (" + t.laps + " laps), on OpenStreetMap + imagery for " +
                 t.matched + "% of it");
    } else {
      parts.push("track shape: consensus of " + (t.laps || 0) + " laps" +
                 (t.rejected ? " (the prepared line is a different layout)" : ""));
    }
    if (t.dem) parts.push("terrain: " + t.dem);
    if (ASSET && ASSET.features) parts.push("buildings/roads: OpenStreetMap");
    if (PATH && PATH.accelSource) parts.push("input: " + (PATH.accelSource === "gps+imu" ? "GPS speed + IMU" : "GPS speed"));
    if (PATH && PATH.positionSource === "kalman") parts.push("position: GPS fixes + speed + heading (Kalman)");
    lg.style.display = "flex";
    lg.textContent = parts.join("\\n");
    window.__rc3dStats = function () {
      var g = meshes.ground && meshes.ground.geometry;
      return { calls: renderer.info.render.calls, tris: renderer.info.render.triangles,
               ground: g ? g.attributes.position.count : 0,
               net: meshes.network ? meshes.network.children.length : 0 };
    };
    // where the car runs across the road, metres from the track centreline
    // (+ = RIGHT of travel - lineIndex.signed): percentiles over the session
    window.__rc3dOffsets = function () {
      if (!PATH || !TRACK) return null;
      var ix = RC3D.lineIndex(TRACK.dense.x, TRACK.dense.z, 20), out = [], tot = PATH.dense.total, s;
      for (s = 0; s < tot; s += Math.max(2, tot / 3000)) {
        var c = RC3D.pointAtS(PATH, s), q = ix.nearest(c.x, c.z, 60);
        if (q) out.push(q.signed);
      }
      out.sort(function (a, b) { return a - b; });
      var L = out.length, pc = function (f) { return Math.round(out[Math.min(L - 1, Math.floor(L * f))] * 10) / 10; };
      return { n: L, p5: pc(0.05), p25: pc(0.25), p50: pc(0.5), p75: pc(0.75), p95: pc(0.95) };
    };
    window.__rc3dWhere = function () {
      if (!PATH || !TRACK) return null;
      var c0 = RC3D.pointAtS(PATH, RC3D.sAtTime(PATH, NOW));
      var ix = RC3D.lineIndex(TRACK.dense.x, TRACK.dense.z, 20).nearest(c0.x, c0.z, 60);
      return { x: Math.round(c0.x), z: Math.round(c0.z),
               trackS: ix ? Math.round(TRACK.dense.s[ix.i]) : null, off: ix ? Math.round(ix.signed * 10) / 10 : null };
    };
    // read-only state for debugging / automated checks (no behaviour hangs on it)
    window.__rc3dInfo = {
      track: TRACK_INFO, boards: BOARDS.length, corners: CORNERS.length,
      pieces: (WORLD && WORLD.net) ? WORLD.net.pieces.length : 0,
      position: PATH && PATH.positionSource,
      boardAt: BOARDS.map(function (b) { return [Math.round(b.s), b.m, b.corner]; }),
      cornerAt: CORNERS.map(function (c) { return [Math.round(c.s0), Math.round(c.s1), Math.round(c.deg)]; }),
      total: TRACK && TRACK.dense ? Math.round(TRACK.dense.total) : 0,
      accel: PATH && PATH.accelSource, imuR: PATH && PATH.imuR,
      events: (EVENTS && EVENTS.byLap[LAPNO || EVENTS.best || 0] || []).map(function (e) {
        return { kind: e.kind, permille: TB > TA ? Math.round(1000 * (PATH.t[e.brake_i] - TA) / (TB - TA)) : 0,
                 brake_mph: Math.round(e.brake_mph), min_mph: Math.round(e.min_mph), peak_g: e.peak_g,
                 brake_s: e.brake_ti >= 0 ? Math.round(TRACK.dense.s[e.brake_ti]) : null,
                 min_s: e.min_ti >= 0 ? Math.round(TRACK.dense.s[e.min_ti]) : null,
                 t_brake: Math.round((PATH.t[e.brake_i] - TA) * 100) / 100,
                 t_min: Math.round((PATH.t[e.min_i] - TA) * 100) / 100 };
      })
    };
  }

  function applyViewVisibility() {
    var plan = (view === "plan");
    if (meshes.line) meshes.line.visible = !plan;
    if (meshes.wash) meshes.wash.visible = plan && opts.speedColour;
    if (meshes.labels) meshes.labels.visible = plan;
    if (meshes.car) meshes.car.visible = plan;
    // event labels are sized for the driving view; from above they would be
    // specks, so they grow (and the throttle/min flags stay readable)
    if (meshes.events && meshes.events.userData.sprites &&
        meshes.events.userData.plan !== plan) {
      meshes.events.userData.plan = plan;
      var k = plan ? 6 : 1;
      meshes.events.userData.sprites.forEach(function (sp) {
        var b = sp.userData.base;
        if (b) sp.scale.set(b[0] * k, b[1] * k, 1);
      });
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
      // from above there is no horizon to hide: push the haze out of the way,
      // and let the shadow map cover the whole circuit
      scene.fog.near = pf.h * 2.5; scene.fog.far = pf.h * 8;
      placeSun(tgt, pf.span * 1.15);
    } else {
      eye = new THREE.Vector3(st.eye.x, st.eye.y, st.eye.z);
      tgt = new THREE.Vector3(st.target.x, st.target.y, st.target.z);
      scene.fog.near = 350; scene.fog.far = 3400;
      // the sharp shadow box sits just ahead of the car, where you are looking
      var fwd = new THREE.Vector3(st.target.x - st.eye.x, 0, st.target.z - st.eye.z);
      if (fwd.lengthSq() > 1e-6) fwd.normalize();
      placeSun(new THREE.Vector3(st.eye.x + fwd.x * 55, st.pos.y, st.eye.z + fwd.z * 55), 190);
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
    applyViewVisibility();
    if (meshes.car && view === "plan") placeCar(st.s);
    var g = RC3D.accelAtS(PATH, st.s);
    if (el("h-g")) {
      var ist = 0, ci = RC3D.indexOfTime(PATH.t, NOW);
      if (PATH.input && PATH.input.state.length) ist = PATH.input.state[ci] || 0;
      el("h-g").textContent = (g >= 0 ? "+" : "") + g.toFixed(2) + " g  " +
        (ist > 0 ? "THROTTLE" : ist < 0 ? "BRAKE" : "COAST");
      el("h-g").style.color = ist > 0 ? "#5CE07F" : (ist < 0 ? "#FF6B6B" : "#FFC83A");
    }
    el("h-bar").style.width = Math.min(100, (st.mph / 160) * 100) + "%";
    if (el("lg-corner")) {
      if (CORNERS.length) {
        var withB = {};
        BOARDS.forEach(function (b) { withB[b.corner != null ? b.corner : b.s] = 1; });
        var nb = Object.keys(withB).length;
        el("lg-corner").style.display = "flex";
        el("lg-corner").textContent = CORNERS.length + " corners \u00b7 " +
          (nb ? nb + " with brake boards" + (EVENTS && EVENTS.zones.length ? " (where your laps brake)" : "")
              : "none need boards");
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
    cur = (PATH.samples || S)[RC3D.indexOfTime(PATH.t, NOW)] || null;
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
    BOOTED = true;
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
      cur = (PATH.samples || S)[RC3D.indexOfTime(PATH.t, NOW)] || null;
    });
    el("b-rate").addEventListener("change", function () { rate = Number(el("b-rate").value) || 1; });
    el("b-lap").addEventListener("change", function () {
      setLap(Number(el("b-lap").value));
      playing = true; syncPlay();
    });
    el("b-smooth").addEventListener("change", function () {
      opts.smooth = Number(el("b-smooth").value);
      setupTrack();
      setLap(LAPNO);
    });
    el("b-eye").addEventListener("input", function () { opts.eye = Number(el("b-eye").value); });
    el("b-road").addEventListener("change", function () {   // on release: a rebuild is ~0.3 s
      opts.road = Number(el("b-road").value);
      if (!realWidth) setupTrack();            // the snap band follows the width
      rebuild();
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
      applyViewVisibility();
      render();
    });
    if (el("b-brakes")) el("b-brakes").addEventListener("change", function () {
      opts.brakes = el("b-brakes").checked; rebuild();
    });
    if (el("b-net")) el("b-net").addEventListener("change", function () {
      opts.network = el("b-net").checked; rebuild();
    });
    if (el("b-groundsel")) {
      el("b-groundsel").value = opts.ground;
      el("b-groundsel").addEventListener("change", function () {
        opts.ground = el("b-groundsel").value;
        rebuild();                      // the road surface follows the ground mode
      });
    }
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
  // The ideal line is NOT one of the rebuilt meshes (a rebuild used to delete
  // it for good); it is re-seated on the ground after every rebuild instead.
  var IDEAL = null, IDEAL_MESH = null;
  function placeIdeal() {
    if (!IDEAL || !scene) return;
    if (IDEAL_MESH) {
      scene.remove(IDEAL_MESH);
      IDEAL_MESH.geometry.dispose(); IDEAL_MESH.material.dispose();
      IDEAL_MESH = null;
    }
    var d = IDEAL.dense;
    if (WORLD) for (var i = 0; i < d.x.length; i++) d.y[i] = WORLD.groundY(d.x[i], d.z[i]) + 0.1;
    // a crisp thin CYAN line over the driver's soft chevrons, so both read
    // (green already means "on the throttle" here)
    var r = RC3D.ribbon(IDEAL, 0.32, 0.06, {});
    var g = new THREE.BufferGeometry();
    g.setAttribute("position", new THREE.BufferAttribute(r.position, 3));
    g.setIndex(new THREE.BufferAttribute(r.index, 1));
    var m = new THREE.MeshBasicMaterial({ color: 0x3FD8FF, transparent: true, opacity: 0.95,
      depthWrite: false, side: THREE.DoubleSide, polygonOffset: true,
      polygonOffsetFactor: -4, polygonOffsetUnits: -8 });
    m.toneMapped = false;
    IDEAL_MESH = new THREE.Mesh(g, m);
    IDEAL_MESH.renderOrder = 4;
    scene.add(IDEAL_MESH);
  }

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
      // same local frame as the session (its own mean origin drew it offset)
      IDEAL = RC3D.buildPath(samples, { smooth: 7, denseStep: 1.5, o: PATH.o });
      placeIdeal();
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
    RAW_ASSET = asset;
    ASSET = asset;
    if (!asset || !PATH) return;
    var dems = loadDems(asset);
    // A prepared track is worth SHOWING first: open on the whole circuit over
    // the real imagery (that is the "how big is the track" view), one click from
    // the driving view. Without imagery there is nothing to see from above, so
    // stay in the car.
    TRACK_SEED = asset.slug || "track";
    if (el("b-groundsel") && opts.ground === "sim") {
      // a prepared track with validated imagery: offer it, but the SIMULATED
      // surface stays the default (it is sharper than a 1 m satellite pixel)
      el("b-groundsel").title += " (satellite imagery is available for this track)";
    }
    if (asset.length_m) {
      notice("prepared track: " + asset.track + " " +
             (asset.length_m / 1000).toFixed(2) + " km, " +
             (asset.width_osm_m || asset.width_imagery_m || "?") + " m wide");
      setTimeout(hideNotice, 4500);
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
    // updateTrackLegend() swaps the one-number width for the traced range
    el("lg-track").dataset.head = asset.track +
      (asset.length_m ? " (" + (asset.length_m / 1000).toFixed(2) + " km)" : "");
    el("lg-track").dataset.tail = ", line from " + src;
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
        // the imagery only changes what is drawn in satellite mode
        if (BOOTED && opts.ground === "satellite") rebuild();
      }, undefined, function () {
        console.warn("[track3d] texture failed to load");
      });
      if (attr) {
        el("lg-track").textContent += " · " + attr;
      }
    }
    // a (re-)applied asset starts without the previous one's imagery, and a
    // late image for an older asset is ignored
    GROUND_TEX = null;
    var gtok = GROUND_TOK = {};
    var groundImg = (asset.ground && asset.ground.file) ? new Promise(function (res) {
      var done = false, fin = function () { if (!done) { done = true; res(); } };
      new THREE.TextureLoader().load("/trackassets/" + encodeURIComponent(asset.slug) +
        "/ground.jpg?v=" + encodeURIComponent((asset.enrich && asset.enrich.at) || asset.generated || 0),
        function (t) {
          if (gtok !== GROUND_TOK) { t.dispose(); fin(); return; }
          t.colorSpace = THREE.SRGBColorSpace;
          t.wrapS = t.wrapT = THREE.ClampToEdgeWrapping;
          t.generateMipmaps = true;
          t.minFilter = THREE.LinearMipmapLinearFilter;
          t.magFilter = THREE.LinearFilter;
          try { t.anisotropy = renderer.capabilities.getMaxAnisotropy(); } catch (e) {}
          t.needsUpdate = true;
          GROUND_TEX = t;
          // arrived after the first build: re-shade the ground (only the modes
          // that draw it)
          if (done && BOOTED && (opts.ground === "sim" || opts.ground === "satellite")) rebuild();
          fin();
        }, undefined, fin);
      setTimeout(fin, 2500);                 // never hold the first render for long
    }) : Promise.resolve();
    return Promise.all([dems, groundImg]).then(function () {
      try { setupTrack(); }
      catch (e) { console.warn("[track3d] track setup:", e && e.message ? e.message : e); }
      if (BOOTED) setLap(LAPNO);
    });
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
          "/data?target=30000&fixes=1")
      .then(function (r) { return r.json(); })
      .then(function (d) {
        var all = d.samples || [];
        S = all.filter(function (s) {
          return typeof s.lat === "number" && typeof s.lon === "number" &&
                 (s.lat || s.lon) && Math.abs(s.lat) <= 90 && Math.abs(s.lon) <= 180;
        });
        if (S.length < 20) { notice("no GPS fixes in this session", true); return; }
        // the server times laps from the FILE's first row; our clock starts at
        // the first row with a fix (a session that starts before GPS lock)
        var firstT = function (arr) {
          for (var q = 0; q < arr.length; q++) {
            var r0 = arr[q];
            if (r0 && typeof r0.t === "number" && isFinite(r0.t)) return r0.t;
            if (r0 && typeof r0.t_ms === "number" && isFinite(r0.t_ms)) return r0.t_ms / 1000;
          }
          return null;
        };
        var tFile = firstT(all), tFix = firstT(S);
        var lapShift = (tFile !== null && tFix !== null && tFix > tFile) ? tFix - tFile : 0;
        return fetch("/sessions/" + encodeURIComponent(USER) + "/" +
                     encodeURIComponent(FILE) + "/laps")
          .then(function (r) { return r.json(); })
          .catch(function () { return {}; })
          .then(function (lj) {
            LAPS = ((lj && lj.laps) || []).map(function (L) {
              if (!lapShift) return L;
              var c = {}, k;
              for (k in L) c[k] = L[k];
              c.t_start = L.t_start - lapShift; c.t_end = L.t_end - lapShift;
              return c;
            });
            setupTrack();
            fillLapSelect();
            hideNotice();
            el("hud").style.display = "block";
            if (el("mini")) el("mini").style.display = "block";
            // build the world ONCE, after the prepared track (or its absence)
            // is known - not procedural first and then again with the asset,
            // which made every tree visibly jump on load
            var first = function () {
              if (BOOTED) return;
              BOOTED = true;
              setLap(Number(el("b-lap").value) || 0);
            };
            loadAsset().then(first, first);
            setTimeout(first, 6000);            // a slow asset must not hold the view
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
  /* Three columns on a wide screen: the map, the live tiles + G-meter, and the
     "all channels" table on the RIGHT. The map card stretches to the row's
     height and the map itself grows to fill it, so there is never an empty
     box under the 3D-drive / circle buttons whichever column is tallest.
     Narrower: the table drops under the tiles; narrower still: one column. */
  main { max-width: 2200px; }
  .grid { display: grid; gap: var(--sp-md); align-items: stretch;
    grid-template-columns: minmax(0, 1.25fr) minmax(0, 0.85fr) minmax(0, 0.9fr);
    grid-template-areas: "map tiles chan"; }
  .grid > .mapcard { grid-area: map; display: flex; flex-direction: column; }
  .grid > .tiles { grid-area: tiles; align-content: start; }
  .grid > .chancol { grid-area: chan; display: flex; flex-direction: column;
    gap: var(--sp-md); min-width: 0; }
  @media (max-width: 1500px) {
    main { max-width: 1400px; }
    .grid { grid-template-columns: minmax(0, 1.4fr) minmax(0, 1fr);
      grid-template-areas: "map tiles" "map chan"; }
  }
  @media (max-width: 980px) {
    .grid { grid-template-columns: 1fr; grid-template-areas: "map" "tiles" "chan"; }
  }
  .card { background: var(--surface); border: 1px solid var(--line);
    border-radius: var(--r-md); overflow: hidden; }
  .card-head { display:flex; justify-content:space-between; align-items:center;
    padding: 10px var(--sp-md); border-bottom: 1px solid var(--line);
    background: var(--surface); }
  .card-body { padding: var(--sp-md); }
  #map { height: 560px; width: 100%; background: var(--bg); }
  .mapcard #map { height: auto; min-height: 560px; flex: 1 1 auto; }
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
      <div class="card mapcard">
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
      </div>
      <div class="chancol">
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
  // The map's height follows its card (which follows the tallest column), so
  // Leaflet must be told whenever that changes, or it leaves grey bands.
  if (window.ResizeObserver) {
    let rz = 0;
    new ResizeObserver(function () {
      cancelAnimationFrame(rz);
      rz = requestAnimationFrame(function () { map.invalidateSize({ pan: false }); });
    }).observe(el('map'));
  }
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

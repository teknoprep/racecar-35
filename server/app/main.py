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
import shutil
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
# Firmware-upload auth is DELIBERATELY separate from session-upload auth. If we
# reused RACECAR_API_KEY, setting it to lock down firmware uploads would also
# force every dash session upload to send that exact key (and 401 otherwise —
# which is precisely the bug that made uploads "die at ~250 lines"). Set
# RACECAR_FIRMWARE_KEY to gate POST /firmware/upload independently; it falls
# back to RACECAR_API_KEY only if unset.
FIRMWARE_KEY = os.environ.get("RACECAR_FIRMWARE_KEY", "").strip() or API_KEY
SERVICE_NAME = os.environ.get("RACECAR_SERVICE_NAME", "racecar-35 cloud")
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
                f"<td><span style='color:var(--muted)'>locked</span></td></tr>")

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
            .replace("__SELF__", html.escape(self_email.lower())))
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

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="empty body")
    if len(body) > MAX_BODY_BYTES:
        raise HTTPException(status_code=413, detail="body too large")

    validation = validate_ndjson_body(body)

    # A per-user API key (X-API-Key) namespaces the upload under its owner
    # when no explicit X-User-Email is supplied.
    key_email = email_for_api_key(x_api_key) if x_api_key else None
    email = safe_name(x_user_email or (web_user or {}).get("email") or key_email)

    # Sanitize + epoch-sanity-check the session id. If the firmware uploads
    # something we can't interpret as a real epoch (RTC not set, all zeros,
    # millis()-as-epoch fallback) we substitute the current server time so
    # the file's display row, filename, and review page header all read as
    # "right now" rather than 1970-01-01.
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

    # Defensive cleanup so filenames stay tidy even if a client sends a
    # filename-ish track (older firmware sent the whole SD basename here):
    #   - drop a trailing ".ndjson" so we don't double the extension
    #   - strip a leading "session_" prefix
    #   - if the track still begins with "<sid>_", drop that duplicate id
    raw_track = (x_track_name or "").strip()
    if raw_track.lower().endswith(".ndjson"):
        raw_track = raw_track[: -len(".ndjson")]
    if raw_track.startswith("session_"):
        raw_track = raw_track[len("session_"):]
    if raw_track.startswith(f"{sid}_"):
        raw_track = raw_track[len(sid) + 1:]
    track = safe_name(raw_track, default="UNKNOWN")
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

    # Anchor = first moving sample with a valid fix; fall back to first fix.
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
    crossings = [ai]
    left = False
    last_cross_t = rel[ai]
    for i in range(ai + 1, n):
        g = geo(i)
        if g is None:
            continue
        d = _haversine_km(g[0], g[1], alat, alon)
        if not left and d > _LAP_RADIUS_KM * 2.0:
            left = True
        if left and d <= _LAP_RADIUS_KM and (rel[i] - last_cross_t) >= _LAP_MIN_SEC:
            crossings.append(i)
            left = False
            last_cross_t = rel[i]

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
        "sf": {"lat": alat, "lon": alon, "radius_m": int(_LAP_RADIUS_KM * 1000)},
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


def _region_prompt(metrics: dict, question: str) -> list:
    """Build the chat messages: a race-engineer system prompt + a compact
    per-lap metrics table + the driver's question."""
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
    table = "\n".join(lines)
    system = (
        "You are a professional race engineer and driving coach analyzing "
        "telemetry from an amateur's track car. Be concise and concrete: give "
        "specific, actionable coaching (braking points, apex speed, throttle "
        "application, gear, line) grounded in the numbers provided. Compare the "
        "laps to each other, call out the best and worst, and quantify the time "
        "or speed on offer. Prefer short paragraphs and bullet points. If the "
        "data is insufficient to answer, say so plainly."
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
        return json.dumps(payload)[:2000], use_model
    # Open WebUI appends a collapsible <details> usage/cost/token footer (admin-
    # only info) to the reply. Strip EVERY such block anywhere in the text so the
    # review card shows only the coaching content.
    content = re.sub(r"<details>.*?</details>", "", content, flags=re.S | re.I).strip()
    return content, use_model


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
    # Web users may only delete their OWN sessions (admins may delete anyone's).
    # Being granted visibility of another account does NOT grant delete. Device/
    # API-key callers (the dash) are exempt from the gate.
    if not (x_api_key and (x_api_key == API_KEY or email_for_api_key(x_api_key))):
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


@app.get("/admin/sessions/targets")
async def admin_session_targets(request: Request) -> JSONResponse:
    """Emails an admin may reassign a session TO (all known accounts). Admin only."""
    require_admin(request)
    return JSONResponse({"targets": sorted(all_known_users())})


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
    return JSONResponse(
        {"count": len(samples), "total": total, "stride": eff_stride,
         "bounds": bounds, "samples": samples}
    )


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
    return JSONResponse(_detect_laps(samples))


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
    question = str(body.get("prompt", "")).strip()
    messages = _region_prompt(metrics, question)
    answer, used_model = _ai_chat(messages, model=body.get("model"))

    entry = {
        "id": secrets.token_hex(8),
        "ts": int(time.time()),
        "question": question,
        "model": used_model,
        "answer": answer,
        "region": {"points": poly},
        "laps": len(metrics.get("laps", [])),
        "points_in_region": metrics.get("points_in_region", 0),
    }
    history = _ai_history_append(user, p.name, entry)
    return JSONResponse({
        "ok": True,
        "model": used_model,
        "metrics": metrics,
        "answer": answer,
        "entry": entry,
        "history": history,
    })


@app.get("/sessions/{user}/{filename}/ai/history")
async def session_ai_history(request: Request, user: str, filename: str) -> JSONResponse:
    """Persistent AI Q&A history for this session (newest handling is client-side)."""
    require_web_user(request)
    gate_view_dir(request, safe_name(user))
    p = _resolve_session(user, filename)
    return JSONResponse({"history": _ai_history_load(user, p.name)})


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
    return f'<span class="pill good">{email}</span><a class="btn" href="/account">account</a>{admin_link}<a class="btn" href="/logout">logout</a>'


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
                rows.append(
                    f"<tr><td>{user_h}</td>"
                    f"<td class=mono>{when_h}</td>"
                    f"<td>{track_h}</td>"
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

    current_email = html.escape((user or {}).get("email", ""))
    upload_panel = _UPLOAD_PANEL_HTML.replace("__CURRENT_EMAIL__", current_email)
    user_chip = _user_chip_html(user)
    return _INDEX_HEAD.replace("__USER_CHIP__", user_chip) + upload_panel + listing + _INDEX_JS + "</main></body></html>"


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
  .ai-row { display:flex; align-items:center; gap: var(--sp-sm); margin: 0 0 var(--sp-sm); }
  .ai-presets { display:flex; flex-wrap:wrap; gap: 6px; margin: 0 0 var(--sp-sm); }
  .ai-preset { padding: 6px 10px; }
  .ai-prompt { width:100%; background: var(--surface); color: var(--text);
    border: 1px solid var(--line); border-radius: var(--r-sm); padding: 8px 12px;
    font: 14px var(--ff-ui); outline: none; resize: vertical; margin: 0 0 var(--sp-sm); }
  .ai-prompt:focus { border-color: var(--primary); }
  .ai-answer { margin-top: var(--sp-sm); padding: var(--sp-md); background: var(--surface);
    border: 1px solid var(--line); border-radius: var(--r-sm); white-space: pre-wrap;
    line-height: 1.5; max-height: 460px; overflow-y: auto; }
  .ai-answer h1,.ai-answer h2,.ai-answer h3 { font-size: 15px; margin: 10px 0 4px; color: var(--primary); }
  .ai-answer code { font-family: var(--ff-mono); color: var(--primary); }
  .ai-answer strong { color: var(--text); }
  .ai-history { margin-top: var(--sp-sm); display:flex; flex-direction:column; gap: var(--sp-sm); }
  .ai-hist-item { border: 1px solid var(--line); border-radius: var(--r-sm); background: var(--surface); overflow:hidden; }
  .ai-hist-head { display:flex; align-items:center; gap: var(--sp-sm); padding: 10px 12px; cursor:pointer; }
  .ai-hist-head:hover { background: var(--surface-2); }
  .ai-hist-q { flex:1; color: var(--text); font-weight:600; }
  .ai-hist-meta { color: var(--muted); font: 400 11px var(--ff-mono); white-space:nowrap; }
  .ai-hist-body { display:none; padding: 4px 12px 12px; white-space:pre-wrap; line-height:1.5; }
  .ai-hist-item.open .ai-hist-body { display:block; }
  .ai-hist-body code { font-family: var(--ff-mono); color: var(--primary); }
  .ai-hist-body strong { color: var(--text); }
  .ai-hist-actions { display:flex; gap:6px; padding: 0 12px 10px; }
  .ai-hist-actions .btn { padding: 4px 8px; }
  .ai-hist-x { color: var(--bad); }
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
    <select id="move-target" class="cmp-input" style="width:auto;min-width:180px"></select>
    <button id="move-btn" class="btn">reassign</button>
  </span>
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
            <thead><tr><th>Lap</th><th>Time</th><th>+/−</th><th>Max</th></tr></thead>
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
        <div class="ai-row">
          <button id="ai-draw" class="btn">circle a section</button>
          <button id="ai-clear" class="btn">clear</button>
          <span style="flex:1"></span>
          <label class="t-label" style="display:flex;align-items:center;gap:6px">model
            <select id="ai-model" class="cmp-input" style="width:auto;min-width:160px"></select>
          </label>
        </div>
        <div class="ai-presets">
          <button class="btn ai-preset" data-q="Analyze the braking zone for this section: where should I brake, how hard, and how consistent am I lap to lap?">brake zones</button>
          <button class="btn ai-preset" data-q="How is my corner entry speed through this section, and where can I carry more speed in?">entry speed</button>
          <button class="btn ai-preset" data-q="How is my corner exit and throttle application through this section? Where am I losing exit speed?">exit speed</button>
          <button class="btn ai-preset" data-q="What is the fastest line through this section and how does my best lap compare to the others here?">best line</button>
          <button class="btn ai-preset" data-q="How consistent am I through this section lap to lap, and which lap was best and why?">consistency</button>
        </div>
        <textarea id="ai-prompt" class="ai-prompt" rows="2"
          placeholder="Ask about the section you circled — entry/exit speed, brake points, best line, consistency…"></textarea>
        <div class="ai-row">
          <button id="ai-ask" class="btn primary">ask ai</button>
          <span id="ai-status" class="t-label" style="color:var(--muted)"></span>
        </div>
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
    placeDeltaCursor(i0);
    updateCompReadouts(i0);
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
  function refIdxByTime(ref, tInto){
    const tm=ref.series.tm, last=tm.length-1;
    if (last<0) return 0;
    if (tInto<=tm[0]) return 0; if (tInto>=tm[last]) return last;
    let lo=0, hi=last;
    while (lo<hi){ const m=(lo+hi)>>1; if (tm[m]<tInto) lo=m+1; else hi=m; }
    return lo;
  }
  function refIdxByDist(ref, dq){
    const dist=ref.series.dist, last=dist.length-1;
    if (last<0) return 0;
    if (dq<=dist[0]) return 0; if (dq>=dist[last]) return last;
    let lo=0, hi=last;
    while (lo<hi){ const m=(lo+hi)>>1; if (dist[m]<dq) lo=m+1; else hi=m; }
    return lo;
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
      refLine=L.polyline(ctxSeg(ref.ctx, ref.lap),
        {color:'#6CD07A', weight:2, opacity:0.55, dashArray:'4 5'}).addTo(map);
    }
    const seg=ctxSeg(selfCtx, primLapArg);
    selLine=L.polyline(seg, {color:'#FFB020', weight:4, opacity:0.95}).addTo(map);
    if (seg.length) map.fitBounds(selLine.getBounds(), {padding:[20,20]});
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
  function placeDeltaCursor(idx){
    const cur=el('delta-cursor'); const live=el('delta-live');
    if (!cur) return;
    if (!deltaState || idx<deltaState.i0 || idx>deltaState.i1 || !(deltaState.maxD>0)){
      cur.style.display='none'; if (live) live.textContent=''; return;
    }
    const k=idx-deltaState.i0;
    const dq=deltaState.primSeries.dist[k];
    const v=deltaState.primSeries.tm[k]-interpTime(deltaState.refSeries, dq);
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
  function updateCompReadouts(idx){
    const active = currentRef && primLap && lapWindow && deltaState
      && idx>=lapWindow.i0 && idx<=lapWindow.i1
      && !(currentRef.ctx===selfCtx && currentRef.lap.lap===primLap.lap);
    if (!active){ hideCompDot(); setCompTiles(null); return; }
    const k=idx-lapWindow.i0;
    const tInto=deltaState.primSeries.tm[k];
    const dq=deltaState.primSeries.dist[k];
    let rk;
    if (syncMode==='time'){
      // same elapsed time into the lap -> generally a DIFFERENT place; show
      // where the comparison car was at this instant with its own red dot.
      rk=refIdxByTime(currentRef, tInto);
      const gs=currentRef.ctx.S[currentRef.i0+rk];
      if (gs && typeof gs.lat==='number' && typeof gs.lon==='number' && (gs.lat||gs.lon))
        showCompDot([gs.lat, gs.lon]);
      else hideCompDot();
    } else {
      // same place on track -> one dot; compare the telemetry at this spot.
      rk=refIdxByDist(currentRef, dq);
      hideCompDot();
    }
    setCompTiles(currentRef.ctx.S[currentRef.i0+rk]);
  }

  // ---- selection state ------------------------------------------------
  let defaultRef=null, selfLaps=[], selfBest=null;
  function setRef(ref){
    currentRef=ref;
    if (!primLap) return;
    el('delta-sel').textContent='\u00b7 lap '+primLap.lap+' vs '+(ref?ref.label:'\u2014');
    highlight(primLap, ref);
    drawDelta(primLap, ref);
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
        (lap.max_mph!=null?Math.round(lap.max_mph):'\u2014')+'</td>';
      tr.addEventListener('click', ()=>selectPrimary(lap, rows));
      rows.appendChild(tr);
    }
    selectPrimary(bestLap, rows);
    loadCompList();
  })();

  // ---- AI corner analysis ------------------------------------------
  (function(){
    const card = el('aicard'); if (!card) return;
    let poly = null, pts = [], drawing = false;
    const drawBtn = el('ai-draw'), clearBtn = el('ai-clear'),
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
      if (pts.length<3){ info.textContent='no region selected'; return; }
      let n=0;
      for (const s of S){
        if (typeof s.lat==='number' && typeof s.lon==='number' && (s.lat||s.lon)
            && inPoly(s.lat, s.lon, pts)) n++;
      }
      info.textContent = n+' points in region';
    }
    function setDraw(on){
      drawing=on;
      drawBtn.classList.toggle('primary', on);
      drawBtn.textContent = on ? 'drag on the map…' : 'circle a section';
      const c = map.getContainer();
      if (on){ map.dragging.disable(); c.classList.add('leaflet-crosshair'); }
      else   { map.dragging.enable();  c.classList.remove('leaflet-crosshair'); }
    }
    function onMove(e){ pts.push([e.latlng.lat, e.latlng.lng]); if(poly) poly.setLatLngs(pts); }
    drawBtn.addEventListener('click', ()=> setDraw(!drawing));
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

    // Light markdown -> HTML (escape first, then inline bold/code + headings).
    function md(t){
      let h = t.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
      h = h.replace(/^#{1,6}\\s*(.+)$/gm,'<h3>$1</h3>');
      h = h.replace(/\\*\\*([^*]+)\\*\\*/g,'<strong>$1</strong>');
      h = h.replace(/`([^`]+)`/g,'<code>$1</code>');
      return h;
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
    function renderHistory(list){
      const items = (list||[]).slice().reverse();
      histEl.innerHTML = '';
      items.forEach((e, k)=>{
        const div = document.createElement('div');
        div.className = 'ai-hist-item' + (k===0 ? ' open' : '');
        const q = esc(e.question || '(no question)');
        const meta = esc((e.model||'') + ' · ' + (e.laps||0) + ' laps · ' + fmtWhen(e.ts));
        div.innerHTML =
          '<div class="ai-hist-head"><span class="ai-hist-q">'+q+'</span>'+
          '<span class="ai-hist-meta">'+meta+'</span></div>'+
          '<div class="ai-hist-body">'+md(e.answer||'')+'</div>'+
          '<div class="ai-hist-actions">'+
            '<button class="btn ai-region-btn">show region</button>'+
            '<button class="btn ai-hist-x ai-del-btn">delete</button></div>';
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
    document.querySelectorAll('.ai-preset').forEach(b=>{
      b.addEventListener('click', ()=>{ el('ai-prompt').value=b.dataset.q; ask(); });
    });
    loadModels();
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
  const btn=document.getElementById('move-btn');
  if (!wrap||!sel||!btn) return;
  try {
    const t = await (await fetch('/admin/sessions/targets')).json();
    for (const em of (t.targets||[])){
      const o=document.createElement('option'); o.value=em; o.textContent=em; sel.appendChild(o);
    }
  } catch(e){}
  wrap.style.display='inline-flex';
  btn.addEventListener('click', async ()=>{
    const target=sel.value;
    if (!target){ return; }
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
</body></html>
"""
)

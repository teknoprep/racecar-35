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
    itself proof the update landed."""
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
    return JSONResponse({"ok": True, "status": st, "pending": pending,
                         "running_since": _PROC_START, "now": int(time.time())})


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
    An old dash never asks; an old server 404s and the dash sends raw."""
    return {"ok": True, "zblocks": True, "coach": True}


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
  #map{height:100vh}
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
  <div id="map"></div>
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
  L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
    {maxZoom:20, attribution:'Imagery © Esri'}).addTo(map);
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
                       .replace("__WHEN__", when)


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
    var poll=null, t0=0;
    function fmt(s){ return s||''; }
    async function tick(){
      try{
        var r=await fetch('/admin/update/status'); var j=await r.json();
        var st=(j.status&&j.status.state)||'', pend=!!j.pending;
        if(j.running_since && t0 && j.running_since>t0){
          m.textContent='updated \\u2713 server restarted'; b.disabled=false;
          clearInterval(poll); poll=null; return;
        }
        m.textContent = pend ? 'queued\\u2026 waiting for host watcher'
                             : (st ? ('host: '+fmt(st)) : 'queued\\u2026');
      }catch(e){}
    }
    b.addEventListener('click', async function(){
      if(!confirm('Update the server?\\n\\ngit pull + docker compose up -d --build\\nThe site will restart.')) return;
      b.disabled=true; m.textContent='requesting\\u2026';
      try{
        var s=await (await fetch('/admin/update/status')).json();
        t0=s.running_since||0;
        var r=await fetch('/admin/update',{method:'POST'});
        var j=await r.json();
        if(!r.ok){ m.textContent='error: '+((j&&j.detail)||r.status); b.disabled=false; return; }
        m.textContent='queued\\u2026';
        if(!poll) poll=setInterval(tick,3000);
      }catch(e){ m.textContent='failed: '+e.message; b.disabled=false; }
    });
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
                rows.append(
                    f'<tr><td><input type="checkbox" class="cmb" '
                    f'data-user="{user_h}" data-file="{file_h}"></td>'
                    f"<td>{user_h}</td>"
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
            '</div><button id="combine-btn" class="btn" '
            'title="select 2+ sessions of the same user, oldest+newest are joined in time order">'
            'combine selected</button><span class="pill" id="vis"></span></div>'
            "<table><thead><tr><th></th><th>user</th><th>started (UTC)</th>"
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
  .ai-hist-body { display:none; padding: 4px 12px 12px; white-space:normal; line-height:1.55; }
  .ai-hist-item.open .ai-hist-body { display:block; }
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
  .ai-hist-actions { display:flex; gap:6px; padding: 0 12px 10px; }
  .ai-hist-actions .btn { padding: 4px 8px; }
  .ai-hist-x { color: var(--bad); }
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
        <div class="ai-row">
          <button id="ai-draw" class="btn">circle a section</button>
          <button id="ai-clear" class="btn">clear</button>
          <button id="ai-line" class="btn" title="popout: fastest real line through this section vs yours, with brake/apex/throttle markers and speed labels">ideal line ↗</button>
          <button id="ai-coach" class="btn" title="run the whole-session review that normally happens automatically on upload, and file 1-3 checklist items">checklist</button>
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
    function renderHistory(list){
      const items = (list||[]).slice().reverse();
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
        const meta = esc((e.model||'') + ' · ' + (e.laps||0) + ' laps' + (cost?(' · '+cost):'') + ' · ' + fmtWhen(e.ts));
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
    document.querySelectorAll('.ai-preset').forEach(b=>{
      b.addEventListener('click', ()=>{ el('ai-prompt').value=b.dataset.q; ask(); });
    });
    loadModels();

    // ---- session tools: change TRACK + this session's coach checklist ----
    (function(){
      var row=document.createElement('div'); row.className='ai-row';
      row.innerHTML='<label class="t-label">track '+
        '<select id="trk-sel" class="cmp-input" style="width:auto;min-width:170px"></select></label>'+
        '<input id="trk-custom" class="cmp-input" style="display:none;width:170px" placeholder="custom track name">'+
        '<button id="trk-save" class="btn">rename session</button>'+
        '<span class="t-label" id="trk-msg"></span>';
      var host=el('ai-draw').parentNode; host.parentNode.insertBefore(row, host.nextSibling);
      var chd=document.createElement('div'); chd.id='sess-coach';
      host.parentNode.insertBefore(chd, row.nextSibling);
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

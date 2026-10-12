"""Public share links (server/app/main.py): one session, or a whole driver.

A token is an unguessable URL that grants READ-ONLY access with no account:
`/shared/<token>` opens that one session, or — for a "user" share — lists every
session that driver has. Two things must hold, and they are what this file pins:

  * a token reaches EXACTLY its target (a session token cannot jump to another
    file, a user token cannot reach another driver, and neither can be walked
    out of the data dir with a crafted filename);
  * the token file format stays compatible — tokens minted before whole-user
    shares existed carry no `kind` and must keep meaning "this one session".

The real helpers and handlers are extracted out of main.py with ast and run
against a temp data dir, with only the auth gates stubbed (they are asserted
separately). Same approach as tests/test_session_edit.py: no fastapi needed.
"""
import ast
import asyncio
import inspect
import json
import pathlib
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "server/app/main.py"
SRC = MAIN.read_text()

_OLD = "1788543989_Summit_Point_Main.ndjson"

WANTED_FUNCS = (
    "safe_name", "reasonable_epoch", "_resolve_session", "session_dir_for",
    "display_epoch_for", "parse_session_id", "_human_bytes",
    "_share_path", "_share_bad", "_share_meta", "_shared_session", "_share_token_for",
    "_share_user_token_for", "_share_create", "_shares_all",
    "_user_share_target", "_shared_list_html",
    "load_managed_users", "save_managed_users", "managed_upsert", "generate_api_key",
    "profile_of", "clean_profile_value",
    "oauth_enabled", "is_admin_email", "can_delete_dir", "gate_delete_dir",
    "require_admin", "require_web_user", "current_user",
    "get_share", "create_share", "revoke_share",
    "admin_user_share_get", "admin_user_share_create", "admin_shares",
    "admin_share_revoke",
)
WANTED_VARS = (
    "DATA_DIR", "SESSION_COOKIE", "SHARE_DIR", "LAPCACHE_DIR", "USERS_FILE",
    "LAP_META_DIR", "VIDEO_META_DIR", "AI_HISTORY_DIR", "COACH_DIR", "SESSION_EXT",
    "_safe_re", "API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
    "ALLOWED_EMAILS", "ADMIN_EMAILS", "SESSION_SECRET",
    "EPOCH_REASONABLE_MIN", "EPOCH_REASONABLE_MAX", "_LAP_SUMMARY_MAX_PER_CALL",
    "_SHARED_LIST_HTML", "_BASE_CSS", "_FONTS_LINK",
)


class HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class JSONResponse:
    def __init__(self, content):
        self.content = content

    def body(self):
        return json.dumps(self.content).encode()


class _App:
    def __getattr__(self, name):
        def deco(*a, **kw):
            def wrap(fn):
                return fn
            return wrap
        return deco


class Request:
    def __init__(self, payload=None):
        self._raw = json.dumps(payload or {}).encode()

    async def body(self):
        return self._raw


def _extract(data_dir: pathlib.Path) -> dict:
    keep = []
    for node in ast.parse(SRC).body:
        name = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            name = node.name
        elif isinstance(node, ast.Assign):
            targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
            name = targets[0] if targets else None
        if name and (name in WANTED_FUNCS or name in WANTED_VARS):
            keep.append(node)
    module = ast.Module(body=keep, type_ignores=[])
    import os
    os.environ["RACECAR_DATA_DIR"] = str(data_dir)
    ns = {
        "os": os, "re": __import__("re"), "json": json, "html": __import__("html"),
        "time": __import__("time"), "shutil": shutil, "pathlib": pathlib,
        "secrets": __import__("secrets"), "urllib": __import__("urllib"),
        "urllib.parse": __import__("urllib.parse"),
        "Optional": __import__("typing").Optional, "Any": __import__("typing").Any,
        "HTTPException": HTTPException, "JSONResponse": JSONResponse,
        "Request": Request, "app": _App(),
        "HTMLResponse": lambda content, **kw: content,
        "Response": lambda content=None, **kw: content,
        "Query": lambda default=None, **kw: default,
        "log": __import__("logging").getLogger("test"),
    }
    exec(compile(ast.fix_missing_locations(module), str(MAIN), "exec"), ns)
    return ns


class ShareTokenTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="rc35-share-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ns = _extract(self.tmp)
        for sub in ("sessions", "shares", "lapcache"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self.me = self.ns["safe_name"]("dev@example.com")        # dev_example.com
        self.other = self.ns["safe_name"]("maria.rivera@example.com")
        for slug in (self.me, self.other):
            d = self.tmp / "sessions" / slug
            d.mkdir(parents=True, exist_ok=True)
            (d / _OLD).write_text('{"t":1}\n')
        # who the stubbed session belongs to, for the auth gates
        self.viewer = {"email": "dev@example.com"}
        self.ns["current_user"] = lambda req: self.viewer
        self.ns["require_web_user"] = lambda req: self.viewer
        self.ns["require_admin"] = self.require_admin
        self.gates: list = []
        self.ns["gate_delete_dir"] = self.gate_delete_dir
        self.admins = {"admin@example.com"}
        # the real is_admin_email reads users.json / .env; drive it from the
        # same set the require_admin stub uses so "who is an admin" is one fact
        self.ns["is_admin_email"] = lambda email: str(email or "").lower() in self.admins

    def require_admin(self, req):
        email = str((self.ns["current_user"](req) or {}).get("email", ""))
        if email not in self.admins:
            raise HTTPException(status_code=403, detail="admin required")
        return {"email": email}

    def gate_delete_dir(self, req, dirname):
        self.gates.append(dirname)
        email = str((self.ns["current_user"](req) or {}).get("email", ""))
        if self.ns["can_delete_dir"](email, dirname):
            return
        raise HTTPException(status_code=403, detail="not yours")

    def call(self, fn, *a, **kw):
        """Run a helper or a route handler; (status, payload) — status is None
        when it succeeded, exactly like the HTTP layer would report."""
        f = self.ns[fn]
        try:
            if inspect.iscoroutinefunction(f):
                out = asyncio.run(f(Request(kw.pop("body", None)), *a))
            else:
                out = f(*a)
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}
        if isinstance(out, JSONResponse):
            out = out.content
        return None, out

    # -- the token contract ------------------------------------------------
    def test_old_tokens_are_session_tokens(self):
        """A token written before whole-user shares existed has no `kind`."""
        (self.tmp / "shares" / "legacy.json").write_text(json.dumps(
            {"user": self.me, "filename": _OLD, "created": 1, "by": "x"}))
        code, out = self.call("_share_meta", "legacy")
        self.assertIsNone(code, out)
        self.assertEqual(out["kind"], "session")
        user, name = self.ns["_shared_session"](out)
        self.assertEqual((user, name), (self.me, _OLD))

    def test_a_session_token_reaches_exactly_one_file(self):
        tok = self.ns["_share_create"]("session", self.me, "admin@example.com", _OLD)
        meta = self.ns["_share_meta"](tok)
        self.assertEqual(self.ns["_shared_session"](meta), (self.me, _OLD))
        # ...not a sibling file, and no crafted name escapes the directory.
        # (An EMPTY filename means "this token's own session" — that is how
        # /shared/<token>/data calls in, and it resolves to the same file.)
        for bad in ("1785588879_Thompson.ndjson", "../" + _OLD, "..%2F" + _OLD):
            code, _ = self.call("_shared_session", meta, bad)
            self.assertEqual(code, 404, f"a session token accepted {bad!r}")

    def test_a_user_token_lists_that_driver_only(self):
        tok = self.ns["_share_create"]("user", self.me, "admin@example.com",
                                       email="dev@example.com")
        meta = self.ns["_share_meta"](tok)
        self.assertEqual(meta["kind"], "user")
        self.assertEqual(self.ns["_shared_session"](meta, _OLD), (self.me, _OLD))
        for bad in (_OLD,):
            # the same FILENAME under another driver is a different session
            code, _ = self.call("_shared_session",
                                {"kind": "user", "user": self.other}, bad)
            self.assertIsNone(code, "that driver has their own copy of the name")
        # another driver's file is simply not in this token's directory
        (self.tmp / "sessions" / self.other / "only-theirs.ndjson").write_text("{}")
        code, _ = self.call("_shared_session", meta, "only-theirs.ndjson")
        self.assertEqual(code, 404)
        # ...and a traversal attempt cannot escape the directory
        for bad in ("../../etc/passwd", "..%2F..%2Fetc%2Fpasswd", "/etc/passwd"):
            code, _ = self.call("_shared_session", meta, bad)
            self.assertEqual(code, 404, f"a user token accepted {bad!r}")

    def test_creating_is_idempotent_and_revoking_kills_it(self):
        a = self.ns["_share_create"]("session", self.me, "x", _OLD)
        b = self.ns["_share_create"]("session", self.me, "x", _OLD)
        self.assertEqual(a, b, "one link per session, not a new one per click")
        u1 = self.ns["_share_create"]("user", self.me, "x")
        u2 = self.ns["_share_create"]("user", self.me, "x")
        self.assertEqual(u1, u2)
        self.assertNotEqual(a, u1, "a session link is not a user link")
        self.call("revoke_share", "dev@example.com", _OLD)
        code, _ = self.call("_share_meta", a)
        self.assertEqual(code, 404, "a revoked token is gone")
        code, out = self.call("_share_meta", u1)
        self.assertIsNone(code, "revoking the session link must not touch the user link")

    def test_shares_all_lists_everything_with_a_url(self):
        self.ns["_share_create"]("session", self.me, "x", _OLD)
        self.ns["_share_create"]("user", self.other, "y")
        rows = self.ns["_shares_all"]()
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertTrue(r["url"].startswith("/shared/"))
            self.assertIn(r["kind"], ("session", "user"))

    # -- auth -------------------------------------------------------------
    def test_only_the_owner_or_an_admin_may_share_a_session(self):
        self.viewer = {"email": "someone.else@example.com"}
        code, _ = self.call("create_share", "dev_example.com", _OLD, body={})
        self.assertEqual(code, 403)
        code, _ = self.call("get_share", "dev_example.com", _OLD)
        self.assertEqual(code, 403)
        self.viewer = {"email": "dev@example.com"}          # the owner
        code, out = self.call("create_share", "dev_example.com", _OLD, body={})
        self.assertIsNone(code, out)
        self.assertTrue(out["token"])
        self.viewer = {"email": "admin@example.com"}        # ...and an admin
        code, out = self.call("create_share", "dev_example.com", _OLD, body={})
        self.assertIsNone(code, out)

    def test_user_shares_are_admin_only(self):
        self.viewer = {"email": "dev@example.com"}          # the driver themselves
        code, _ = self.call("admin_user_share_create", "dev@example.com", body={})
        self.assertEqual(code, 403, "a driver cannot publish their whole history")
        self.viewer = {"email": "admin@example.com"}
        code, out = self.call("admin_user_share_create", "dev@example.com", body={})
        self.assertIsNone(code, out)
        self.assertTrue(out["token"])
        # and the admin can kill ANY token by value
        code, out = self.call("admin_share_revoke", body={"token": out["token"]})
        self.assertIsNone(code, out)
        self.assertEqual(self.ns["_shares_all"](), [])
        code, _ = self.call("admin_share_revoke", body={"token": "not-a-token"})
        self.assertEqual(code, 404)

    def test_a_user_with_nothing_to_share_is_refused(self):
        code, out = self.call("_user_share_target", "nobody@example.com")
        self.assertEqual(code, 404)
        self.assertIn("no sessions", out["detail"])
        code, out = self.call("_user_share_target", self.me)
        self.assertIsNone(code)
        self.assertEqual(out, (self.me, ""))

    # -- the landing page -------------------------------------------------
    def test_the_list_page_shows_only_that_drivers_sessions(self):
        (self.tmp / "sessions" / self.other / "their-file.ndjson").write_text("{}")
        meta = {"kind": "user", "user": self.me}
        html = self.ns["_shared_list_html"](meta, "tok")
        self.assertIn(_OLD, html)
        self.assertNotIn("their-file.ndjson", html)
        self.assertIn('href="/shared/tok/s/' + _OLD + '"', html)
        self.assertIn('data-best="' + _OLD + '"', html)
        self.assertIn("1 session", html)

    def test_filenames_are_escaped_in_the_list(self):
        sneaky = '<img src=x onerror=alert(1)>.ndjson'
        (self.tmp / "sessions" / self.me / sneaky).write_text("{}")
        html = self.ns["_shared_list_html"]({"kind": "user", "user": self.me}, "tok")
        self.assertNotIn("<img src=x", html)
        self.assertIn("&lt;img", html)


if __name__ == "__main__":
    unittest.main()

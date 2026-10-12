"""Editing a session's STATIC metadata (server/app/main.py).

The filename IS the metadata:

    <sid>_<track>[-combined].ndjson   in   /data/sessions/<safe_name(user)>/

so "change the start date & time", "change the track" and "reassign to another
driver" are one rename and/or move. Everything keyed by (user, filename) has to
follow it or the session quietly loses its AI history, lap exclusions, video
link, lap cache, debug log, public share links and coach items — which is why
the same mover serves /update and the older /rename.

The REAL handlers are extracted out of main.py with ast and executed against a
temp data dir (same approach as tests/test_server_version.py: no fastapi needed
on the host; the async handler is driven by asyncio).
"""
import ast
import asyncio
import json
import pathlib
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "server/app/main.py"
SRC = MAIN.read_text()

FUNC = "session_update"
_OLD = "1788543989_Summit_Point_Main.ndjson"
_NEW = "1791201600_Summit_Point_Shenandoah.ndjson"

WANTED_FUNCS = (
    # config / names
    "safe_name", "reasonable_epoch",
    "split_session_name", "session_filename", "session_dir_for", "_resolve_session",
    # the mover
    "_ai_history_path", "_lap_meta_path", "_video_meta_path", "_session_meta_path", "_debug_path_for",
    "_coach_path", "_coach_load", "_coach_save",
    "_session_sidecar_paths", "session_sidecars_follow",
    # auth
    "oauth_enabled", "is_admin_email", "can_delete_dir", "gate_delete_dir",
    "_may_reassign", "load_managed_users", "save_managed_users", "managed_upsert",
    "generate_api_key", "profile_of", "clean_profile_value", "PROFILE_FIELDS",
    # the handler
    FUNC,
)
WANTED_VARS = (
    "DATA_DIR", "AI_HISTORY_DIR", "VIDEO_META_DIR", "SESSION_META_DIR", "SHARE_DIR", "LAP_META_DIR",
    "LAPCACHE_DIR", "COACH_DIR", "USERS_FILE", "SESSION_EXT", "PROFILE_FIELDS",
    "PROFILE_MAX", "_safe_re", "API_KEY", "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
    "ALLOWED_EMAILS", "ADMIN_EMAILS",
    "EPOCH_REASONABLE_MIN", "EPOCH_REASONABLE_MAX",
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
    """A decorator sink: the extracted handlers must stay callable directly."""

    def __getattr__(self, name):
        def deco(*a, **kw):
            def wrap(fn):
                return fn
            return wrap
        return deco


class Request:
    """Just enough request for a handler that reads the body."""

    def __init__(self, payload=None):
        self._raw = json.dumps(payload or {}).encode()
        self.url = type("U", (), {"path": "/", "query": ""})()

    async def body(self):
        return self._raw


def _extract(data_dir: pathlib.Path) -> dict:
    """Compile the whitelisted top-level nodes of main.py into a namespace."""
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
    ns = {
        "os": __import__("os"), "re": __import__("re"), "json": json,
        "time": __import__("time"), "shutil": shutil, "pathlib": pathlib,
        "Optional": __import__("typing").Optional,
        "HTTPException": HTTPException, "JSONResponse": JSONResponse,
        "Request": Request, "app": _App(),
        "log": __import__("logging").getLogger("test"),
        "RACECAR_DATA_DIR_OVERRIDE": str(data_dir),
    }
    # DATA_DIR reads this env var, and the rest of the paths derive from it
    import os
    os.environ["RACECAR_DATA_DIR"] = str(data_dir)
    exec(compile(ast.fix_missing_locations(module), str(MAIN), "exec"), ns)
    return ns


class SessionEditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="rc35-edit-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ns = _extract(self.tmp)
        for sub in ("sessions", "ai_history", "lap_meta", "video_meta",
                    "lapcache", "debug", "shares", "coach"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        # the on-disk dir is safe_name(email) — computed, not guessed: "@"
        # becomes "_" and dots survive ("dev@example.com" -> dev_example.com)
        self.user = self.ns["safe_name"]("dev@example.com")
        self.dir = self.tmp / "sessions" / self.user
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / _OLD).write_text('{"t":1}\n')
        self.sync()

    def sync(self):
        pass

    # -- helpers ---------------------------------------------------------
    def call(self, body, user=None, filename=None):
        """Run the real handler; return (status_or_None, payload)."""
        req = Request(body)
        try:
            resp = asyncio.run(self.ns[FUNC](req, user or self.user, filename or _OLD))
        except HTTPException as exc:
            return exc.status_code, {"detail": exc.detail}
        return None, resp.content

    def write_sidecars(self, user, name):
        """Every sidecar a session can own, at the current name."""
        ns = self.ns

        def put(path, text):
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

        put(ns["_ai_history_path"](user, name), '[{"a":1}]')
        put(ns["_lap_meta_path"](user, name), '{"excluded":[3]}')
        put(ns["_video_meta_path"](user, name), '{"id":"abc"}')
        put(ns["_session_meta_path"](user, name), '{"weight_lb": 2420}')
        put(self.tmp / "lapcache" / ns["safe_name"](user) / (name + ".json"), '{"best":1}')
        put(ns["_debug_path_for"](user, name), '{"dbg":1}')
        (self.tmp / "shares" / "tok.json").write_text(json.dumps(
            {"user": ns["safe_name"](user), "filename": name, "created": 1}))
        ns["_coach_save"](user, [
            {"id": "abc123", "text": "brake later", "session": name, "done": False},
            {"id": "keep", "text": "other session", "session": "other.ndjson", "done": False}])

    def assert_moved(self, user, name):
        ns = self.ns
        for label, path in ns["_session_sidecar_paths"](user, name):
            self.assertTrue(path.exists(), f"{label} did not follow to {path}")
        for label, path in ns["_session_sidecar_paths"](self.user, _OLD):
            self.assertFalse(path.exists(), f"{label} was left behind at {path}")

    # -- naming ----------------------------------------------------------
    def test_filename_is_the_metadata(self):
        ns = self.ns
        split, build = ns["split_session_name"], ns["session_filename"]
        self.assertEqual(split(_OLD), (1788543989, "Summit_Point_Main", False))
        self.assertEqual(split("1785588879_Thompson-combined.ndjson"),
                         (1785588879, "Thompson", True))
        self.assertEqual(split("no-sid.ndjson"), (0, "no-sid", False))
        self.assertEqual(split(""), (0, "UNKNOWN", False))
        # an empty track cannot produce a nameless file
        self.assertEqual(build(1791201600, "Summit Point Main"),
                         "1791201600_Summit_Point_Main.ndjson")
        self.assertEqual(build(1791201600, "Summit Point Main", True),
                         "1791201600_Summit_Point_Main-combined.ndjson")
        self.assertEqual(build(1, "un/usable  name"), "1_un_usable_name.ndjson")
        # round trip, and the -combined flag survives an edit
        sid, track, comb = split(build(1791201600, "Summit Point Main"))
        self.assertEqual((sid, comb), (1791201600, False))
        self.assertEqual(build(sid, track, comb), "1791201600_Summit_Point_Main.ndjson")

    # -- the handler ------------------------------------------------------
    def test_track_and_date_and_driver_in_one_call(self):
        self.write_sidecars(self.user, _OLD)
        # the target is an EMAIL: the dir is its slug (dots survive, @ does not)
        other = self.ns["safe_name"]("maria.rivera@example.com")
        code, out = self.call({"track": "Summit Point Shenandoah",
                               "epoch": 1791201600, "target": "maria.rivera@example.com"})
        self.assertIsNone(code, out)
        self.assertEqual(out["user"], other)
        self.assertEqual(out["filename"], _NEW)
        self.assertEqual(out["review"], f"/review/{other}/{_NEW}")
        self.assertTrue((self.tmp / "sessions" / other / _NEW).exists())
        self.assertFalse((self.dir / _OLD).exists())
        self.assert_moved(other, _NEW)
        # the share token names the session INSIDE its json
        tok = json.loads((self.tmp / "shares" / "tok.json").read_text())
        self.assertEqual(tok, {"user": other, "filename": _NEW, "created": 1})
        # coach items belong to the DRIVER: moved, renamed, not duplicated
        new_items = self.ns["_coach_load"](other)
        self.assertEqual([i["id"] for i in new_items], ["abc123"])
        self.assertEqual(new_items[0]["session"], _NEW)
        self.assertEqual([i["id"] for i in self.ns["_coach_load"](self.user)], ["keep"])

    def test_a_rename_keeps_the_owner_and_the_coach_order(self):
        self.write_sidecars(self.user, _OLD)
        self.ns["_coach_save"](self.user, [
            {"id": "1", "text": "a", "session": _OLD, "done": False},
            {"id": "2", "text": "b", "session": "other.ndjson", "done": False},
            {"id": "3", "text": "c", "session": _OLD, "done": True}])
        code, out = self.call({"track": "Summit Point Shenandoah"})
        self.assertIsNone(code, out)
        self.assertEqual(out["filename"], "1788543989_Summit_Point_Shenandoah.ndjson")
        items = self.ns["_coach_load"](self.user)
        self.assertEqual([i["id"] for i in items], ["1", "2", "3"])
        self.assertEqual(items[0]["session"], "1788543989_Summit_Point_Shenandoah.ndjson")
        self.assertEqual(items[1]["session"], "other.ndjson")

    def test_nothing_to_do_is_not_an_error(self):
        code, out = self.call({"track": "Summit_Point_Main", "epoch": 1788543989})
        self.assertIsNone(code, out)
        self.assertTrue(out.get("unchanged"))
        self.assertTrue((self.dir / _OLD).exists())

    def test_refuses_to_overwrite_a_session(self):
        (self.dir / _NEW).write_text("{}")
        code, out = self.call({"track": "Summit Point Shenandoah", "epoch": 1791201600})
        self.assertEqual(code, 409)
        self.assertTrue((self.dir / _OLD).exists(), "the source must not be touched")
        self.assertEqual((self.dir / _NEW).read_text(), "{}")

    def test_validation(self):
        code, _ = self.call({"epoch": "not a number"})
        self.assertEqual(code, 422)
        code, _ = self.call({"epoch": 42})
        self.assertEqual(code, 422, "outside 2000-2100")
        code, _ = self.call({"track": "   "})
        self.assertEqual(code, 400)
        code, _ = self.call({"target": "nobody-in-particular"})
        self.assertEqual(code, 422, "not an email and not an existing user")
        code, out = self.call({"target": self.user})
        self.assertIsNone(code, out)
        self.assertTrue(out.get("unchanged"), "reassigning to the current owner is a no-op")

    def test_only_an_admin_may_reassign(self):
        other = self.ns["safe_name"]("maria.rivera@example.com")
        (self.tmp / "sessions" / other).mkdir()
        ns = self.ns
        ns["oauth_enabled"] = lambda: True          # production: gates are live
        ns["current_user"] = lambda req: {"email": "dev@example.com"}
        ns["is_admin_email"] = lambda email: False
        code, out = self.call({"target": other})
        self.assertEqual(code, 403)
        self.assertTrue((self.dir / _OLD).exists())
        # ...but the owner may still rename their own session
        code, out = self.call({"track": "Thompson"})
        self.assertIsNone(code, out)
        self.assertEqual(out["filename"], "1788543989_Thompson.ndjson")

    def test_owner_and_admin_rules(self):
        """Owner-or-admin renames; only an admin reassigns."""
        ns = self.ns
        self.assertTrue(ns["can_delete_dir"]("dev@example.com", self.user),
                        "the owner may edit their own session")
        self.assertFalse(ns["can_delete_dir"]("someone@else.com", self.user),
                         "another account may not")
        ns["is_admin_email"] = lambda email: email == "someone@else.com"
        self.assertTrue(ns["can_delete_dir"]("someone@else.com", self.user),
                        "an admin may edit anyone's session")

    def test_dev_mode_is_open(self):
        """No OAuth configured = every gate is a no-op, so a bench can move a
        session (that is how the local harness exercises this path)."""
        self.assertFalse(self.ns["oauth_enabled"]())
        self.assertTrue(self.ns["_may_reassign"](Request({})))


class ReviewEditUiTests(unittest.TestCase):
    """The review page carries ONE edit button; the old track box (in the AI
    card) and reassign box (in the header) are gone."""

    def html(self):
        import sys
        sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
        import test_server_version as tsv
        return tsv._evaluated("_REVIEW_HTML")

    def test_one_button_no_boxes(self):
        html = self.html()
        self.assertIn('id="edit-btn"', html)
        for gone in ('id="move-target"', 'id="move-list"', 'id="move-btn"',
                     'id="trk-sel"', 'id="trk-save"', 'id="admin-move"'):
            self.assertNotIn(gone, html)
        # ...and the popup itself, with the three fields
        for frag in ('id="meta-modal"', 'id="meta-when"', 'id="meta-user"',
                     'id="meta-track"', 'id="meta-track-custom"',
                     'id="meta-preview"', 'id="meta-save"'):
            self.assertIn(frag, html)
        self.assertIn("/update'", html)
        self.assertIn("const CAN_EDIT=__CAN_EDIT__, IS_ADMIN=__IS_ADMIN__, EPOCH=__EPOCH__;", html)

    def test_every_placeholder_is_replaced(self):
        """A __TOKEN__ the route forgets to replace is a JS syntax error (the
        editor dies silently), so the two lists must line up."""
        import re
        sys_path = pathlib.Path(__file__).resolve().parent
        import sys
        sys.path.insert(0, str(sys_path))
        import test_server_version as tsv
        html = tsv._evaluated("_REVIEW_HTML")
        tokens = set(re.findall(r"__[A-Z0-9_]+__", html))
        self.assertTrue(tokens)
        route = SRC[SRC.index("async def review(request: Request, user: str, filename: str)"):]
        route = route[:route.index("@app.get")]
        replaced = set(re.findall(r'\.replace\("(__[A-Z0-9_]+__)"', route))
        self.assertTrue(tokens <= replaced, f"never replaced: {sorted(tokens - replaced)}")


if __name__ == "__main__":
    unittest.main()

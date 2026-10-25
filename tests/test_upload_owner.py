"""Who OWNS an upload (server/app/main.py).

A per-user API key is an identity: only its owner can be holding it. The dash
sends X-User-Email unconditionally — blank or stale when the panel's cloud
account field was never filled in — and that header used to decide the target,
so sessions uploaded WITH a real account key landed in `sessions/anon/`. Now a
key that resolves to an account decides the owner on a device upload; a
signed-in web user's header still wins (the index page's upload box uses it to
import a file for a chosen driver).

The REAL resolver and the admin re-filing endpoint are extracted out of main.py
with ast and executed against a temp data dir (same approach as
tests/test_session_edit.py: no fastapi needed on the host).
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

FW_KEY = "test-firmware-key"
GOOD_KEY = "KEYGOOD1234"
OTHER_KEY = "KEYOTHER5678"

WANTED_FUNCS = (
    "safe_name", "reasonable_epoch", "session_dir_for",
    "load_managed_users", "email_for_api_key",
    "profile_of", "clean_profile_value",
    "_norm_owner_email", "_upload_owner_email", "_resolve_upload_target",
    # the mover used by the re-filing endpoints
    "_ai_history_path", "_lap_meta_path", "_video_meta_path", "_debug_path_for",
    "_session_sidecar_paths", "session_sidecars_follow",
    "_coach_path", "_coach_load", "_coach_save",
    "_resolve_session",
    "admin_move_session", "admin_attribute_sessions",
)
WANTED_VARS = (
    "DATA_DIR", "AI_HISTORY_DIR", "VIDEO_META_DIR", "SHARE_DIR", "LAP_META_DIR",
    "LAPCACHE_DIR", "COACH_DIR", "USERS_FILE", "_safe_re", "_ANON_OWNERS",
    "PROFILE_FIELDS", "PROFILE_MAX", "EPOCH_REASONABLE_MIN", "EPOCH_REASONABLE_MAX",
)


class HTTPException(Exception):
    def __init__(self, status_code=500, detail=""):
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


class JSONResponse:
    def __init__(self, content):
        self.content = content


def _header(default=None):
    return default


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


def _require_admin(request):
    raise HTTPException(status_code=403, detail="admin access required")


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
    ns = {
        "os": __import__("os"), "re": __import__("re"), "json": json,
        "time": __import__("time"), "shutil": shutil, "pathlib": pathlib,
        "hmac": __import__("hmac"), "secrets": __import__("secrets"),
        "Optional": __import__("typing").Optional,
        "HTTPException": HTTPException, "JSONResponse": JSONResponse,
        "Request": Request, "app": _App(),
        "log": __import__("logging").getLogger("test"),
        # main.py derives this from the env at import; the handler only needs it
        # to exist so the firmware-key branch can be exercised.
        "FIRMWARE_KEY": FW_KEY,
        "Header": _header, "Query": lambda default=None, **kw: default,
        # The admin portal gate itself is unchanged by this work (it needs OAuth
        # + fastapi); it must simply still refuse an unauthenticated caller.
        "require_admin": _require_admin,
    }
    import os
    os.environ["RACECAR_DATA_DIR"] = str(data_dir)
    exec(compile(ast.fix_missing_locations(module), str(MAIN), "exec"), ns)
    return ns


class UploadOwnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="rc35-owner-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ns = _extract(self.tmp)
        for sub in ("sessions", "ai_history", "lap_meta", "video_meta",
                    "lapcache", "debug", "shares", "coach"):
            (self.tmp / sub).mkdir(parents=True, exist_ok=True)
        self.ns["USERS_FILE"].parent.mkdir(parents=True, exist_ok=True)
        self.ns["USERS_FILE"].write_text(json.dumps({"users": [
            {"email": "cm.rawlings@gmail.com", "api_key": GOOD_KEY},
            {"email": "someone@else.test", "api_key": OTHER_KEY},
            {"email": "admin@else.test", "is_admin": True, "api_key": ""},
        ]}))

    # -- helpers ---------------------------------------------------------
    def target(self, header=None, key=None, web=None, sid="1788543989",
               track="Summit Point Main", kind=""):
        return self.ns["_resolve_upload_target"](
            header, sid, track, key, web, kind)

    def owner(self, header=None, key=None, web=None):
        return self.ns["_upload_owner_email"](header, web, key)

    # -- the bug ---------------------------------------------------------
    def test_key_names_the_owner_when_the_panel_has_no_account(self):
        # The exact field case: the screen holds a real account key but its
        # cloud-account box was never filled in, so it uploads no email.
        for header in (None, "", "   "):
            email, _, _, _, _, _, out, src = self.target(header=header, key=GOOD_KEY)
            self.assertEqual(email, "cm.rawlings_gmail.com")
            self.assertEqual(src, "key")
            self.assertEqual(out.parent, self.tmp / "sessions" / "cm.rawlings_gmail.com")

    def test_placeholder_headers_do_not_win_over_the_key(self):
        # Anything a panel might have in the field instead of "": still the key.
        for header in ("ANON", "anon", "UNKNOWN", "none", "n/a"):
            self.assertEqual(self.owner(header=header, key=GOOD_KEY),
                             ("cm.rawlings@gmail.com", "key"))

    def test_stale_or_mistyped_header_does_not_move_the_session(self):
        # The panel says one thing, the credential says another: the credential
        # is the only claim that CANNOT be faked by whoever typed in settings.
        for header in ("old@elsewhere.test", "someone@else.test"):
            email, _, _, _, _, _, out, src = self.target(header=header, key=GOOD_KEY)
            self.assertEqual(email, "cm.rawlings_gmail.com")
            self.assertEqual(src, "key")
            self.assertEqual(out.parent.name, "cm.rawlings_gmail.com")

    def test_email_case_cannot_split_one_account_into_two_dirs(self):
        self.assertEqual(
            self.owner(header="CM.Rawlings@Gmail.com", key=GOOD_KEY),
            ("cm.rawlings@gmail.com", "key"))
        # ... and that is true with no key at all, in a web/header upload.
        self.assertEqual(
            self.target(header="CM.Rawlings@Gmail.com")[0],
            "cm.rawlings_gmail.com")
        # safe_name() alone does NOT lowercase (it must not mangle a slug), so
        # the normalisation has to happen in the resolver — that is the whole
        # reason _norm_owner_email exists.
        self.assertEqual(self.ns["safe_name"]("CM.Rawlings@Gmail.com"),
                         "CM.Rawlings_Gmail.com")
        self.assertEqual(
            self.ns["safe_name"](self.ns["_norm_owner_email"]("CM.Rawlings@Gmail.com")),
            "cm.rawlings_gmail.com")

    def test_other_accounts_key_names_its_own_owner(self):
        self.assertEqual(self.owner(header="", key=OTHER_KEY),
                         ("someone@else.test", "key"))
        # Unknown/garbage keys are not identities (the firmware key is one of
        # these: it belongs to the SERVER, not to a person), so the header is
        # used exactly as before.
        for key in (FW_KEY, "nope", "", None):
            self.assertEqual(self.owner(header="me@here.test", key=key),
                             ("me@here.test", "header"))

    def test_firmware_key_falls_back_to_the_header(self):
        for key in (FW_KEY, "nope", None):
            email, _, _, _, _, _, _, src = self.target(header="me@here.test", key=key)
            self.assertEqual(email, "me_here.test")
            self.assertEqual(src, "header")

    def test_no_identity_at_all_is_still_anon(self):
        for header in (None, "", "ANON"):
            email, _, _, _, _, _, out, src = self.target(header=header)
            self.assertEqual(email, "anon")
            self.assertEqual(src, "anon")
            self.assertEqual(out.parent, self.tmp / "sessions" / "anon")

    # -- the web upload box must not regress ------------------------------
    def test_signed_in_user_targets_the_account_they_typed(self):
        # The index page's upload box: an admin imports a file for a driver.
        web = {"email": "admin@else.test"}
        self.assertEqual(self.owner(header="driver@team.test", key="", web=web),
                         ("driver@team.test", "web"))
        # ... even though the admin's own per-user key is in the form.
        self.assertEqual(self.owner(header="driver@team.test", key=OTHER_KEY, web=web),
                         ("driver@team.test", "web"))
        # Blank box: their own account, not anon.
        self.assertEqual(self.owner(header="", key="", web=web),
                         ("admin@else.test", "web"))
        # No account email on the record, but a key names one.
        self.assertEqual(self.owner(header="", key=GOOD_KEY, web={"email": ""}),
                         ("cm.rawlings@gmail.com", "key"))

    def test_debug_companion_lands_under_the_same_owner(self):
        # The .dbg sidecar shares session id + track; it must never be filed
        # under an account the session itself did not go to.
        email, _, _, _, _, filename, out, _ = self.target(
            header="", key=GOOD_KEY, kind="debug")
        self.assertEqual(out.parent, self.tmp / "debug" / "cm.rawlings_gmail.com")
        self.assertTrue(filename.endswith(".dbg.ndjson"))

    # -- re-filing what already landed in anon/ ---------------------------
    def test_attribute_moves_a_whole_directory_and_its_sidecars(self):
        ns = self.ns
        anon = self.tmp / "sessions" / "anon"
        anon.mkdir(parents=True, exist_ok=True)
        names = ["1546300828_Summit_Point_Main.ndjson",
                 "1792777376_Summit_Point_Main.ndjson"]
        for n in names:
            (anon / n).write_text('{"t":1}\n')
        (ns["_ai_history_path"]("anon", names[0])).parent.mkdir(parents=True, exist_ok=True)
        (ns["_ai_history_path"]("anon", names[0])).write_text("[]")

        # dry run first: nothing moves, the plan is complete
        req = Request({"from": "anon", "target": "cm.rawlings@gmail.com"})
        plan = asyncio.run(ns["admin_attribute_sessions"](req, x_api_key=FW_KEY)).content
        self.assertTrue(plan["ok"])
        self.assertFalse(plan["executed"])
        self.assertEqual(sorted(m["file"] for m in plan["moved"]), names)
        self.assertTrue((anon / names[0]).exists())

        req = Request({"from": "anon", "target": "cm.rawlings@gmail.com", "execute": 1})
        done = asyncio.run(ns["admin_attribute_sessions"](req, x_api_key=FW_KEY)).content
        self.assertTrue(done["executed"])
        self.assertEqual(done["user"], "cm.rawlings_gmail.com")
        dest = self.tmp / "sessions" / "cm.rawlings_gmail.com"
        for n in names:
            self.assertTrue((dest / n).exists(), n)
        self.assertTrue(ns["_ai_history_path"]("cm.rawlings@gmail.com", names[0]).exists())
        self.assertFalse(anon.exists(), "the emptied anon bucket should be tidied")

    def test_attribute_never_overwrites_and_skips_collisions(self):
        ns = self.ns
        anon = self.tmp / "sessions" / "anon"
        anon.mkdir(parents=True, exist_ok=True)
        dest = self.tmp / "sessions" / "cm.rawlings_gmail.com"
        dest.mkdir(parents=True, exist_ok=True)
        name = "1792780850_Summit_Point_Main.ndjson"
        (anon / name).write_text('{"anon":1}\n')
        (dest / name).write_text('{"mine":1}\n')
        req = Request({"from": "anon", "target": "cm.rawlings@gmail.com", "execute": 1})
        out = asyncio.run(ns["admin_attribute_sessions"](req, x_api_key=FW_KEY)).content
        self.assertEqual(out["moved"], [])
        self.assertEqual(len(out["skipped"]), 1)
        self.assertEqual((dest / name).read_text(), '{"mine":1}\n')
        self.assertTrue((anon / name).exists())

    def test_refiling_requires_the_firmware_key_or_an_admin(self):
        req = Request({"from": "anon", "target": "cm.rawlings@gmail.com"})
        with self.assertRaises(HTTPException) as ctx:
            asyncio.run(self.ns["admin_attribute_sessions"](req, x_api_key="wrong"))
        self.assertEqual(ctx.exception.status_code, 403)

    def test_move_one_session_carries_every_sidecar(self):
        ns = self.ns
        anon = self.tmp / "sessions" / "anon"
        anon.mkdir(parents=True, exist_ok=True)
        name = "1792780850_Summit_Point_Main.ndjson"
        (anon / name).write_text('{"t":1}\n')
        (ns["_lap_meta_path"]("anon", name)).parent.mkdir(parents=True, exist_ok=True)
        (ns["_lap_meta_path"]("anon", name)).write_text('{"excluded":[2]}')
        req = Request({"user": "anon", "filename": name,
                       "target": "cm.rawlings@gmail.com"})
        out = asyncio.run(ns["admin_move_session"](req, x_api_key=FW_KEY)).content
        self.assertEqual(out["user"], "cm.rawlings_gmail.com")
        self.assertTrue((self.tmp / "sessions" / "cm.rawlings_gmail.com" / name).exists())
        self.assertTrue(ns["_lap_meta_path"]("cm.rawlings@gmail.com", name).exists())
        self.assertFalse(ns["_lap_meta_path"]("anon", name).exists())


if __name__ == "__main__":
    unittest.main()

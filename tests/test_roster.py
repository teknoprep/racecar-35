"""Contact details + the roster (server/app/main.py).

Name / phone / address live ON the account record, so the allowlist, the roster
and the per-user API key survive a rebuild together. Two rules matter and are
pinned here:

  * a field is only ever written when it is PRESENT in the body — a form that
    edits the phone number cannot blank the address by omission;
  * the CSV export is opened in Excel, so a cell that starts a formula must not
    be executed, while a phone number that merely starts with "+" must survive
    exactly as typed. Mangling real data is the other half of that bug.

The real helpers are extracted out of main.py with ast and run against a temp
data dir (same approach as tests/test_shares.py: no fastapi needed).
"""
import ast
import json
import pathlib
import re
import shutil
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "server/app/main.py"
SRC = MAIN.read_text()

WANTED_FUNCS = (
    "safe_name", "profile_of", "profile_missing", "profile_from_body",
    "clean_profile_value", "load_managed_users", "save_managed_users",
    "managed_upsert", "generate_api_key", "roster_rows", "csv_cell",
    "_roster_rows_html", "_shared_with_you_html", "session_share_rows",
    "session_shares_for", "_session_share_read", "allowed_emails",
    "is_admin_email", "user_sees_all", "can_delete_dir", "_human_bytes",
    "display_epoch_for", "parse_session_id", "reasonable_epoch",
)
WANTED_VARS = (
    "DATA_DIR", "USERS_FILE", "SESSION_SHARE_DIR", "_safe_re", "API_KEY",
    "ADMIN_EMAILS", "ALLOWED_EMAILS", "PROFILE_FIELDS", "PROFILE_MAX",
    "EPOCH_REASONABLE_MIN", "EPOCH_REASONABLE_MAX", "_API_KEY_ALPHABET",
)


class _App:
    def __getattr__(self, name):
        def deco(*a, **kw):
            def wrap(fn):
                return fn
            return wrap
        return deco


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
    import os
    os.environ["RACECAR_DATA_DIR"] = str(data_dir)
    ns = {
        "os": os, "re": re, "json": json, "html": __import__("html"),
        "time": __import__("time"), "shutil": shutil, "pathlib": pathlib,
        "Optional": __import__("typing").Optional, "app": _App(),
        "secrets": __import__("secrets"),
        "HTTPException": type("HTTPException", (Exception,), {
            "__init__": lambda self, status_code=500, detail="": (
                Exception.__init__(self, detail),
                setattr(self, "status_code", status_code),
                setattr(self, "detail", detail))[-1]}),
        "log": __import__("logging").getLogger("test"),
    }
    exec(compile(ast.fix_missing_locations(ast.Module(body=keep, type_ignores=[])),
                 str(MAIN), "exec"), ns)
    return ns


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="rc35-profile-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ns = _extract(self.tmp)
        self.me = "dev@example.com"

    def test_a_field_is_only_written_when_it_is_present(self):
        ns = self.ns
        ns["managed_upsert"](self.me, **ns["profile_from_body"](
            {"name": "Dev Driver", "phone": "555", "address": "12 Paddock Lane\nWV"}))
        rec = ns["load_managed_users"]()[self.me]
        self.assertEqual(ns["profile_of"](rec)["address"], "12 Paddock Lane\nWV")
        # only the phone in the body: the name and address must survive
        ns["managed_upsert"](self.me, **ns["profile_from_body"]({"phone": "556"}))
        rec = ns["load_managed_users"]()[self.me]
        self.assertEqual(ns["profile_of"](rec),
                         {"name": "Dev Driver", "phone": "556",
                          "address": "12 Paddock Lane\nWV"})
        # an explicitly empty string CLEARS it
        ns["managed_upsert"](self.me, **ns["profile_from_body"]({"address": "  "}))
        self.assertEqual(ns["profile_of"](ns["load_managed_users"]()[self.me])["address"], "")

    def test_whitespace_and_length_are_handled(self):
        clean = self.ns["clean_profile_value"]
        self.assertEqual(clean("name", "  Dev   Driver  "), "Dev Driver")
        self.assertEqual(clean("name", None), "")
        self.assertEqual(clean("address", "\n\n 12  Lane \n \n WV \n\n"), "12 Lane\nWV")
        self.assertEqual(len(clean("address", "x" * 9000)), self.ns["PROFILE_MAX"]["address"])

    def test_missing_reports_the_blank_ones(self):
        ns = self.ns
        rec = {"name": "Dev", "phone": "", "address": "x"}
        self.assertEqual(ns["profile_missing"](rec), ["phone"])
        self.assertEqual(ns["profile_missing"](None), ["name", "phone", "address"])

    def test_an_unrelated_write_does_not_wipe_the_details(self):
        """managed_upsert is read-modify-write: 'make admin' must not blank the
        fields it knows nothing about (it destroyed API keys that way once)."""
        ns = self.ns
        ns["managed_upsert"](self.me, **ns["profile_from_body"]({"name": "Dev Driver"}))
        key = ns["load_managed_users"]()[self.me]["api_key"]
        ns["managed_upsert"](self.me, is_admin=True)
        rec = ns["load_managed_users"]()[self.me]
        self.assertEqual(rec["name"], "Dev Driver")
        self.assertEqual(rec["api_key"], key)
        self.assertTrue(rec["is_admin"])

    def test_the_roster_lists_people_with_their_details(self):
        ns = self.ns
        ns["managed_upsert"]("maria.rivera@example.com", **ns["profile_from_body"](
            {"name": "Maria Rivera", "phone": "+1 555 010 0100", "address": "12 Lane"}))
        rows = {r["email"]: r for r in ns["roster_rows"]()}
        self.assertIn("maria.rivera@example.com", rows)
        self.assertEqual(rows["maria.rivera@example.com"]["name"], "Maria Rivera")
        self.assertEqual(rows["maria.rivera@example.com"]["missing"], [])
        html_out = ns["_roster_rows_html"](list(rows.values()))
        self.assertIn("Maria Rivera", html_out)
        self.assertIn('href="tel:+15550100100"', html_out)


class CsvExportTests(unittest.TestCase):
    """The export is opened in Excel/Sheets by whoever runs the race file."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="rc35-csv-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.ns = _extract(self.tmp)

    def cell(self, v):
        return self.ns["csv_cell"](v)

    def test_formulas_are_defused(self):
        for bad in ("=cmd|'/c calc'!A0", "@SUM(1+1)", "\tleading tab", "\rCR",
                    "+cmd|'/c calc'!A0", "-2+3+cmd|' /C calc'!A0",
                    # a leading +/- is only left alone when the whole cell is
                    # number shaped, so this one is escaped (and shows a stray
                    # apostrophe — the price of never executing a cell)
                    "-5 psi"):
            self.assertTrue(self.cell(bad).startswith("'"), bad)

    def test_real_data_is_left_exactly_as_typed(self):
        for good in ("+1 555 010 0100", "(555) 010-0100", "+44 7700 900123",
                     "Maria Rivera", "12 Paddock Lane\nSummit Point, WV 25446",
                     "12.5"):
            self.assertEqual(self.cell(good), good)

    def test_csv_writer_quotes_what_needs_quoting(self):
        import csv as _csv
        import io
        buf = io.StringIO()
        _csv.writer(buf, lineterminator="\n").writerow(
            [self.cell("Maria, Rivera"), self.cell("line\nbreak"), self.cell("=A1")])
        out = buf.getvalue()
        self.assertIn('"Maria, Rivera"', out)
        self.assertIn('"line\nbreak"', out)
        self.assertIn("'=A1", out)


if __name__ == "__main__":
    unittest.main()

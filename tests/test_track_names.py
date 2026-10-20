"""Server track names must be the DASH's track names (server/app/main.py).

The dash is the source of truth: `TRACKS[]` (plus the `*_CFGS[]` arrays) in
`crowpanel-arduino/RaceDash/RaceDash.ino` decides the string a session is
recorded under — `confirmConfigAndStart()` writes "<base> <config>" for a track
with configs ("Summit Point Main", "Mid-Ohio Full + Chicane", "Watkins Glen
Grand Prix") and the bare base name otherwise.

Two things used to disagree with that, and both are pinned here:

  * `GET /tracks` (the review page's "edit session" track picker) offered only
    the BARE base names, so a session's real name was not in the list at all;
  * the sessions table / 3D view printed the sanitized filename form
    (`Mid_Ohio_Full___Chicane` — the Teensy turns every illegal character into
    '_') or the lowercase `_track_key()`, neither of which is a track name.

The server's copy of the table lives in `server/app/sf_picker.html` (the Docker
image ships only `app/`), so this also fails if that mirror drifts from the
firmware — `tools/gen_dash_tracks.py` regenerates it.
"""
import ast
import json
import pathlib
import re
import sys
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "server/app/main.py"
SRC = MAIN.read_text(encoding="utf-8")
sys.path.insert(0, str(ROOT / "tools"))

import gen_dash_tracks  # noqa: E402  (the firmware -> mirror generator)


def _load_main_helpers():
    """main.py's track-name helpers, executed without importing fastapi."""
    wanted = ("_track_norm", "_dash_tracks", "_dash_track_names",
              "_track_canon", "_display_track_name")
    tree = ast.parse(SRC)
    parts = []
    for node in tree.body:
        targets = []
        if isinstance(node, ast.Assign):
            targets = [n.id for n in node.targets if isinstance(n, ast.Name)]
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            targets = [node.target.id]
        if "_TRACK_MIRROR" in targets:
            parts.append(ast.get_source_segment(SRC, node))
        elif isinstance(node, ast.FunctionDef) and node.name in wanted:
            parts.append(ast.get_source_segment(SRC, node))
    assert len(parts) >= len(wanted) + 1, \
        "main.py's track-name helpers changed shape — update this test"
    ns = {
        "__file__": str(MAIN),          # _dash_tracks() reads sf_picker.html beside it
        "pathlib": pathlib,
        "json": json,
        "re": re,
        "log": types.SimpleNamespace(info=lambda *a: None,
                                     warning=lambda *a: None),
    }
    exec("\n\n".join(parts), ns)
    return ns


class DashMirror(unittest.TestCase):
    def test_picker_mirror_matches_the_firmware(self):
        """server/app/sf_picker.html is a mechanical copy of RaceDash.ino."""
        fw = gen_dash_tracks.firmware_tracks()
        html = gen_dash_tracks.PICKER.read_text(encoding="utf-8")
        m = gen_dash_tracks.MIRROR_RE.search(html)
        self.assertIsNotNone(m, "const TRACKS not found in sf_picker.html")
        mirror = json.loads(re.search(r"\[.*\]", m.group(0), re.S).group(0))
        self.assertEqual([t["name"] for t in mirror], [t["name"] for t in fw])
        for a, b in zip(mirror, fw):
            self.assertEqual(a.get("cfgs") or [], b.get("cfgs") or [], a["name"])
            self.assertEqual(bool(a.get("aux")), bool(b.get("aux")), a["name"])
            for key, tol in (("lat", 1e-6), ("lon", 1e-6), ("r", 1e-6)):
                self.assertAlmostEqual(a[key], b[key], delta=tol,
                                       msg="%s %s" % (a["name"], key))

    def test_mirror_is_not_stale(self):
        """tools/gen_dash_tracks.py --check, i.e. the exact file a release ships."""
        html = gen_dash_tracks.PICKER.read_text(encoding="utf-8")
        m = gen_dash_tracks.MIRROR_RE.search(html)
        indent = re.match(r"[ \t]*", m.group(0)).group(0)
        self.assertEqual(m.group(0),
                         gen_dash_tracks.mirror_line(gen_dash_tracks.firmware_tracks(),
                                                     indent))


class ServerTrackNames(unittest.TestCase):
    def setUp(self):
        self.ns = _load_main_helpers()

    def test_known_track_list_is_every_dash_name(self):
        """GET /tracks == base names + every config, i.e. what the dash writes."""
        names = self.ns["_dash_track_names"]()
        for want in ("Summit Point", "Summit Point Main", "Summit Point Jefferson",
                     "Summit Point Shenandoah", "Watkins Glen", "Watkins Glen Grand Prix",
                     "Watkins Glen Short Course", "Mid-Ohio Full + Chicane",
                     "Mid-Ohio Full Course", "Mid-Ohio Short Course",
                     "VIR Grand Course", "Sonoma Short Course", "Thompson"):
            self.assertIn(want, names)
        # exactly the firmware's set, nothing invented, nothing missing
        self.assertEqual(sorted(set(names), key=str.lower),
                         sorted(set(gen_dash_tracks.session_names(
                             gen_dash_tracks.firmware_tracks())), key=str.lower))
        self.assertEqual(len(names), len(set(names)), "duplicate names")

    def test_filename_is_displayed_as_the_dash_spells_it(self):
        show = self.ns["_display_track_name"]
        cases = {
            "1788543989_Summit_Point_Main.ndjson": "Summit Point Main",
            "1788543989_Summit_Point_Jefferson.ndjson": "Summit Point Jefferson",
            "1712345678_Mid_Ohio_Full___Chicane.ndjson": "Mid-Ohio Full + Chicane",
            "1712345678_Watkins_Glen_Grand_Prix.ndjson": "Watkins Glen Grand Prix",
            "1712345678_Thompson.ndjson": "Thompson",
            "1712345678_Barber.ndjson": "Barber",
            # Teensy on-SD spelling, and a combined session
            "session_1712345678_Barber.ndjson": "Barber",
            "1788543989_Summit_Point_Main-combined.ndjson": "Summit Point Main \u00b7 combined",
            # a track the firmware does not know: no invented name, just readable
            "1712345678_Some_New_Track.ndjson": "Some New Track",
        }
        for raw, want in cases.items():
            self.assertEqual(show(raw), want, raw)

    def test_no_base_only_list_remains(self):
        """The old endpoint returned base names only (aux entries filtered out)."""
        self.assertNotIn("_KNOWN_TRACKS", SRC)
        self.assertNotIn("if not t.get(\"aux\")", SRC)

    def test_sessions_table_and_json_use_the_helper(self):
        """Both places that NAME a session's track call _display_track_name."""
        self.assertIn("track = _display_track_name(f.name)", SRC)
        self.assertIn('"track": _display_track_name(f.name)', SRC)


if __name__ == "__main__":
    unittest.main()

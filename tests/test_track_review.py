"""The track-render QA / AI review / admin re-render (server/app/main.py + trackprep).

A baked track can be wrong in ways only the driver sees at speed: the width
measured off run-off, a line drawn on the grass, the road bucking because the
elevation was a raw DEM read, no lidar at all. The server's AUDIT is the ground
truth (numbers compared with what a good render needs); the AI is only allowed
to interpret that sheet. These tests pin:

  * the model's JSON is parsed even inside a fence or with a trailing comma, and
    a non-answer is reported instead of crashing;
  * both admin endpoints are gated by the SAME rule that shows the buttons;
  * the 3D page carries the admin panel, hidden for non-admins (__ADMIN__ is
    injected by the route; a forgotten token = a dead page).
"""
import ast
import pathlib
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "server/app/main.py"
SRC = MAIN.read_text()


def _load_json_from_model():
    tree = ast.parse(SRC)
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_json_from_model")
    import json
    import re
    ns = {"Optional": object, "json": json, "re": re}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(MAIN), "exec"), ns)
    return ns["_json_from_model"]


class ModelJsonTests(unittest.TestCase):
    def test_fenced_or_preamble_json_is_accepted(self):
        f = _load_json_from_model()
        self.assertEqual(f('```json\n{"verdict": "ok"}\n```'), {"verdict": "ok"})
        self.assertEqual(f('Sure, here it is: {"a": 1} hope that helps'), {"a": 1})

    def test_trailing_comma_is_tolerated(self):
        f = _load_json_from_model()
        self.assertEqual(f('{"issues": [{"issue": "x"},],}'), {"issues": [{"issue": "x"}]})

    def test_a_non_answer_is_none_not_an_exception(self):
        f = _load_json_from_model()
        for bad in ("", "no json here", "{broken", "[1,2,3]", None):
            self.assertIsNone(f(bad), bad)


class EndpointGateTests(unittest.TestCase):
    def test_review_and_rerender_use_the_same_admin_rule_as_the_buttons(self):
        for name in ("trackasset_review", "trackasset_rerender"):
            i = SRC.index("def %s(" % name)
            block = SRC[i:i + 1400]
            self.assertIn("_gate_track_admin(request)", block, name)
            self.assertNotIn("require_admin(request)", block,
                             "%s must work on a dev bench too (see _gate_track_admin)" % name)
        # ...and that rule IS the page's own (dev open, else admin email)
        i = SRC.index("def _gate_track_admin(")
        self.assertIn("_req_is_admin(request)", SRC[i:i + 900])

    def test_prep_endpoint_is_view_gated_and_returns_the_sheet(self):
        i = SRC.index('@app.get("/trackassets/{slug}/prep")')
        block = SRC[i:i + 1200]
        self.assertIn("require_web_user(request)", block)
        for k in ("audit", "review", "prep", "ai_enabled"):
            self.assertIn('"%s"' % k, SRC[i:i + 2000], k)

    def test_rerender_runs_the_whole_pipeline_and_the_ai_review(self):
        i = SRC.index("def trackasset_rerender(")
        block = SRC[i:i + 2200]
        self.assertIn('"enrich": True', block)
        self.assertIn('"review": True', block)
        self.assertIn('"source": "asset"', block)      # re-bake from its OWN line
        # _prep_run honours both flags
        j = SRC.index("def _prep_run(")
        pb = SRC[j:j + 2600]
        self.assertIn('params.get("enrich")', pb)
        self.assertIn('params.get("review"', pb)
        self.assertIn("_track_render_review", pb)

    def test_the_3d_page_has_the_admin_panel_and_the_route_fills_the_flag(self):
        page = SRC[SRC.index("_TRACK3D_HTML = ("):SRC.index("_REVIEW_HTML = (")]
        for frag in ('id="qa"', 'id="qa-rerender"', 'id="qa-review"', 'id="qa-body"',
                     "var ADMIN = __ADMIN__;", "function qaSeedAsset(", "qaSeedAsset(asset);",
                     "qaInit();", "/rerender", "/review", "/prep"):
            self.assertIn(frag, page, frag)
        route = SRC[SRC.index("async def track3d("):SRC.index("async def track3d(") + 1200]
        self.assertIn('.replace("__ADMIN__"', route)
        self.assertIn("_req_is_admin(request)", route)


if __name__ == "__main__":
    unittest.main()


def _t3d():
    i = SRC.index("_TRACK3D_HTML = (")
    return SRC[i:SRC.index("_CAN_REVIEW_HTML = (")]


def _review():
    i = SRC.index("_REVIEW_HTML = (")
    return SRC[i:SRC.index("_ROSTER_HTML = (")]


class ProblemHereTests(unittest.TestCase):
    """The admin "problem here" tool: point, say what is wrong, get ONE bounded
    fix (the AI only picks from trackprep.CORRECTION_OPS), preview, accept."""

    def test_every_fix_endpoint_uses_the_admin_rule(self):
        for name in ("trackasset_profile", "trackasset_fix_propose",
                     "trackasset_fix_apply", "trackasset_fix_remove"):
            i = SRC.index("def %s(" % name)
            self.assertIn("_gate_track_admin(request)", SRC[i:i + 900], name)
            # blocking work (AI call, numpy, file locks) -> plain def, thread pool
            self.assertNotIn("async def %s(" % name, SRC)

    def test_the_ai_choice_is_always_validated_by_trackprep(self):
        i = SRC.index("def trackasset_fix_propose(")
        block = SRC[i:SRC.index("def trackasset_fix_apply(")]
        self.assertIn("tp.validate_correction(", block)
        j = SRC.index("def trackasset_fix_apply(")
        self.assertIn("tp.validate_correction(", SRC[j:j + 1500])   # never trust the client copy
        self.assertIn("_slug_lock(path)", SRC[j:j + 1500])

    def test_corrections_survive_rerender_reprep_and_seed_refresh(self):
        i = SRC.index("def trackasset_rerender(")
        self.assertIn('"corrections": asset.get("corrections")', SRC[i:i + 2400])
        j = SRC.index("def session_track_prep(")
        self.assertIn('"corrections": keep_corr', SRC[j:j + 9000])
        k = SRC.index("def _prep_run(")
        self.assertIn('"corrections": params.get("corrections")', SRC[k:k + 2600])
        m = SRC.index("def _seed_tracks(")
        self.assertIn("reapply_corrections", SRC[m:m + 4000])

    def test_heuristic_without_ai(self):
        tree = ast.parse(SRC)
        node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_fix_heuristic")
        ns = {}
        exec(compile(ast.Module(body=[node], type_ignores=[]), str(MAIN), "exec"), ns)
        f = ns["_fix_heuristic"]
        self.assertEqual(f("make it flat", [1, 2], [1, 1])["op"], "flatten")
        self.assertEqual(f("way too steep here", [1], [1])["op"], "max_grade")
        self.assertEqual(f("it bucks and is bumpy", [1], [1])["op"], "smooth")
        self.assertEqual(f("impossibly high hill", [1], [1])["op"], "scale_relief")

    def test_panel_has_the_tool_and_a_collapse_button(self):
        h = _t3d()
        for idv in ("qa-toggle", "qa-prof", "qa-here", "qa-note", "qa-ask", "qa-op",
                    "qa-accept", "qa-discard", "qa-corr"):
            self.assertIn('id="%s"' % idv, h, idv)
        self.assertIn("rc3.qa.collapsed", h)              # the collapse is remembered
        self.assertIn("#qa.collapsed #qa-main { display:none; }", h)
        self.assertIn("/fix/propose", h)
        self.assertIn("/fix/apply", h)
        self.assertIn("/fix/remove", h)

    def test_the_viewer_draws_the_prepared_profile_not_its_own_dem_read(self):
        h = _t3d()
        self.assertIn('TRACK_INFO.profile = "asset"', h)
        self.assertIn("ASSET.elevation.v || 0) >= 2", h)


class EmbeddedThreeDTests(unittest.TestCase):
    """The 3D view opens IN the pit wall's map card on the data the page has,
    and only grows when its full-screen button is pressed."""

    def test_review_page_embeds_and_shares_its_data(self):
        r = _review()
        self.assertIn('id="map3dwrap"', r)
        self.assertIn('id="map3dframe"', r)
        self.assertIn('id="rc3dfull"', r)
        self.assertIn("window.__rcReview.data = data;", r)
        self.assertIn("window.__rcReview.laps = lr;", r)
        self.assertIn("q.push('embed=1');", r)
        self.assertIn("window.__rc3dFull = function", r)
        self.assertIn("window.__rc3dExitFull = function", r)
        self.assertNotIn("'track3d');", r)                # no more pop-up window

    def test_track3d_reads_the_parent_in_embed_mode_and_has_the_buttons(self):
        h = _t3d()
        self.assertIn('q.get("embed") === "1"', h)
        self.assertIn("window.parent.__rcReview", h)
        self.assertIn('id="b-full"', h)
        self.assertIn('id="b-exitfs"', h)
        self.assertIn("window.__rc3dNow = function", h)
        # compact embed hides the admin panel, legend and options
        self.assertIn(".embed #legend, .embed #qa", h)
        # the full view fetches its OWN full-resolution data
        self.assertIn('"/data?target=30000&fixes=1"', h)


class KeyboardShortcutTests(unittest.TestCase):
    def test_space_and_arrows_never_steal_keys_from_a_text_box(self):
        """Typing a note to the AI in the QA panel: space must be a space, not
        play/pause (and the arrows must move the cursor, not scrub)."""
        h = _t3d()
        i = h.index('document.addEventListener("keydown", function (e) {\n      // typing')
        self.assertIn("if (isTypingTarget(e.target)) return;", h[i:i + 400])
        j = h.index("function isTypingTarget(t)")
        body = h[j:j + 400]
        for tag in ("TEXTAREA", "SELECT", "isContentEditable", '"INPUT"'):
            self.assertIn(tag, body)

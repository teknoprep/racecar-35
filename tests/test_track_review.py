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

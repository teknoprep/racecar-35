"""Which model the AI surfaces use (server/app/main.py).

The coach checklist runs AUTOMATICALLY on every upload and the corner analysis
is used many times a session, so both are pinned to Haiku 5.5 (cheap/fast)
instead of whatever the biggest model on the gateway is. Two things make that
non-trivial and worth pinning:

  * the model id the gateway EXPOSES varies — Open WebUI aggregates providers, so
    the same model is "anthropic.anthropic/claude-haiku-5.5", "claude-haiku-5-5",
    "…:latest" … — the configured id is therefore resolved against the live
    catalogue, and if nothing matches, returned UNCHANGED (a clear upstream
    "model not found" beats silently coaching on the wrong model);
  * the legacy RACECAR_AI_MODEL must NOT drag these two surfaces back onto the
    old default — it seeded the allowlist and used to be the whole selection.

The real functions are extracted out of main.py and executed (same approach as
tests/test_server_version.py: no fastapi needed on the host); the catalogue
fetch is stubbed, so nothing here touches the network.
"""
import ast
import pathlib
import re
import types
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
MAIN = ROOT / "server/app/main.py"
SRC = MAIN.read_text()

HAIKU = "anthropic.anthropic/claude-haiku-5.5"

_WANTED_FUNCS = ("ai_resolve_model", "_ai_haiku_id")
_WANTED_VARS = (
    "_ai_temp_raw",                       # helper for AI_TEMPERATURE
    "AI_MODELS", "AI_HAIKU_MODEL", "AI_ANALYSIS_MODEL", "AI_COACH_MODEL",
    "AI_DEFAULT_MODEL", "AI_FEATURE_MODELS", "AI_API_KEY", "AI_BASE_URL",
    "AI_MODEL", "AI_TIMEOUT", "AI_TEMPERATURE",
)


def _config_nodes(tree):
    """Every top-level node that defines one of the AI_* config values."""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if any(n in _WANTED_VARS for n in names):
                out.append(node)
        elif isinstance(node, ast.If):
            body = [n for n in node.body if isinstance(n, (ast.Assign, ast.AnnAssign))]
            names = []
            for n in body:
                tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                names += [t.id for t in tg if isinstance(t, ast.Name)]
            if any(n in _WANTED_VARS for n in names):
                out.append(node)
    return out


def _load(env: dict, catalogue):
    """main.py's AI config + model-selection functions, with `env` as the
    environment and `catalogue` as the gateway's live model list."""
    tree = ast.parse(SRC)
    parts = [ast.get_source_segment(SRC, n) for n in _config_nodes(tree)]
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name in _WANTED_FUNCS:
            parts.append(ast.get_source_segment(SRC, node))
    assert len(parts) >= len(_WANTED_VARS) + len(_WANTED_FUNCS) - 3, \
        "main.py's AI config changed shape — update this test"

    ns = {
        "os": types.SimpleNamespace(environ=dict(env)),
        "log": types.SimpleNamespace(info=lambda *a: None, warning=lambda *a: None),
        "time": __import__("time"),
    }
    exec("\n\n".join(parts), ns)
    # Stub the network: _ai_haiku_id resolves against this list. A list that is
    # None means "the fetch failed" (the code must cope).
    ns["_ai_catalogue_ids"] = lambda: list(catalogue) if catalogue is not None else []
    return ns


class AiModelDefaults(unittest.TestCase):
    def test_both_surfaces_default_to_haiku_55(self):
        ns = _load({}, [])
        self.assertEqual(ns["AI_ANALYSIS_MODEL"], HAIKU)
        self.assertEqual(ns["AI_COACH_MODEL"], HAIKU)
        self.assertEqual(ns["AI_FEATURE_MODELS"]["analysis"], HAIKU)
        self.assertEqual(ns["AI_FEATURE_MODELS"]["coach"], HAIKU)

    def test_legacy_ai_model_cannot_pull_them_back(self):
        """The whole point of the switch: an old RACECAR_AI_MODEL=sonnet in .env
        must not silently keep the checklist/analysis on Sonnet."""
        ns = _load({"RACECAR_AI_MODEL": "anthropic.anthropic/claude-sonnet-5"}, [])
        self.assertEqual(ns["AI_ANALYSIS_MODEL"], HAIKU)
        self.assertEqual(ns["AI_COACH_MODEL"], HAIKU)
        # ...while still seeding the allowlist (back-compat)
        self.assertEqual(ns["AI_MODELS"], ["anthropic.anthropic/claude-sonnet-5"])

    def test_per_feature_env_overrides_win(self):
        ns = _load({
            "RACECAR_AI_ANALYSIS_MODEL": "vendor/claude-haiku-5.5-fast",
            "RACECAR_AI_COACH_MODEL": "vendor/claude-haiku-5.5-cheap",
        }, [])
        self.assertEqual(ns["AI_ANALYSIS_MODEL"], "vendor/claude-haiku-5.5-fast")
        self.assertEqual(ns["AI_COACH_MODEL"], "vendor/claude-haiku-5.5-cheap")

    def test_blank_vars_fall_back(self):
        """A blank line in .env must not disable the feature (the repo's rule for
        every optional env var)."""
        ns = _load({"RACECAR_AI_HAIKU_MODEL": "", "RACECAR_AI_ANALYSIS_MODEL": "",
                    "RACECAR_AI_COACH_MODEL": ""}, [])
        self.assertEqual(ns["AI_ANALYSIS_MODEL"], HAIKU)
        self.assertEqual(ns["AI_COACH_MODEL"], HAIKU)

    def test_haiku_base_is_overridable(self):
        ns = _load({"RACECAR_AI_HAIKU_MODEL": "x/claude-haiku-5.5"},
                   ["x/claude-haiku-5.5"])
        self.assertEqual(ns["_ai_haiku_id"](ns["AI_COACH_MODEL"]), "x/claude-haiku-5.5")


class AiModelResolution(unittest.TestCase):
    """The gateway's spelling, not ours, is what must be sent."""

    def test_exact_id_is_used_as_is(self):
        ns = _load({}, [HAIKU, "anthropic.anthropic/claude-sonnet-5"])
        self.assertEqual(ns["_ai_haiku_id"](HAIKU), HAIKU)

    def test_variant_spelling_resolves(self):
        ns = _load({}, ["anthropic.anthropic/claude-haiku-5-5",
                        "anthropic.anthropic/claude-sonnet-5"])
        self.assertEqual(ns["_ai_haiku_id"](HAIKU),
                         "anthropic.anthropic/claude-haiku-5-5")

    def test_date_suffixed_id_resolves(self):
        ns = _load({}, ["claude-haiku-5.5-20260219", "claude-opus-5.5"])
        self.assertEqual(ns["_ai_haiku_id"](HAIKU), "claude-haiku-5.5-20260219")

    def test_never_swaps_to_a_non_haiku_model(self):
        ns = _load({}, ["anthropic.anthropic/claude-sonnet-5",
                        "anthropic.anthropic/claude-opus-5.5",
                        "anthropic.anthropic/claude-haiku-4.5"])
        self.assertEqual(ns["_ai_haiku_id"](HAIKU), HAIKU,
                         "a 4.5 Haiku / Sonnet / Opus must never be substituted")

    def test_fetch_failure_returns_configured(self):
        ns = _load({}, None)          # catalogue unavailable
        self.assertEqual(ns["_ai_haiku_id"](HAIKU), HAIKU)

    def test_prefers_the_configured_provider_prefix(self):
        ns = _load({}, ["openai.openai/claude-haiku-5-5",
                        "anthropic.anthropic/claude-haiku-5-5"])
        self.assertEqual(ns["_ai_haiku_id"](HAIKU),
                         "anthropic.anthropic/claude-haiku-5-5")

    def test_explicit_allowlist_pin_is_never_second_guessed(self):
        ns = _load({"RACECAR_AI_MODELS": "vendor/pinned-haiku"},
                   ["anthropic.anthropic/claude-haiku-5-5"])
        self.assertEqual(ns["_ai_haiku_id"]("vendor/pinned-haiku"), "vendor/pinned-haiku")

    def test_empty_and_unknown_features_resolve_to_nothing(self):
        ns = _load({}, [HAIKU])
        self.assertEqual(ns["_ai_haiku_id"](""), "")
        self.assertEqual(ns["_ai_haiku_id"]("   "), "")


class AiAllowlist(unittest.TestCase):
    def test_disallowed_pick_falls_back_to_the_resolved_default(self):
        ns = _load({"RACECAR_AI_MODELS": "a/sonnet"}, [HAIKU])
        self.assertEqual(ns["ai_resolve_model"]("b/other", fallback=HAIKU), HAIKU)
        self.assertEqual(ns["ai_resolve_model"]("a/sonnet", fallback=HAIKU), "a/sonnet")

    def test_unrestricted_mode_honours_the_pick(self):
        ns = _load({}, [HAIKU])
        self.assertEqual(ns["ai_resolve_model"]("b/other", fallback=HAIKU), "b/other")
        self.assertEqual(ns["ai_resolve_model"]("", fallback=HAIKU), HAIKU)


class AiWiring(unittest.TestCase):
    """The call sites must actually pass their feature, or the switch is inert."""

    def test_coach_calls_with_its_feature(self):
        block = SRC[SRC.index("def _coach_analyze"):]
        block = block[:block.index("\ndef ", 1)]
        self.assertIn("feature=\"coach\"", block)

    def test_analysis_call_sites_pass_their_feature(self):
        self.assertGreaterEqual(SRC.count('feature="analysis"'), 2,
                                "corner analysis AND the ideal-line AI both need it")

    def test_caps_reports_the_configured_models(self):
        caps = SRC[SRC.index('@app.get("/caps")'):]
        caps = caps[:caps.index("\ndef ", 1)]
        self.assertIn('"ai_models"', caps)
        self.assertIn('"analysis": AI_ANALYSIS_MODEL', caps)
        self.assertIn('"coach": AI_COACH_MODEL', caps)

    def test_catalogue_fetch_is_off_the_event_loop(self):
        """It is a blocking HTTP GET: on the event loop it froze the server."""
        block = SRC[SRC.index('@app.get("/ai/models")'):]
        block = block[:block.index("\n@app.", 1)]
        self.assertIn("asyncio.to_thread(_ai_model_list)", block)

    def test_haiku_runs_in_a_worker_thread(self):
        """A resolution may fetch the catalogue — never from an async handler."""
        for marker in ("def _coach_kick", "asyncio.to_thread("):
            self.assertIn(marker, SRC)
        block = SRC[SRC.index("def _coach_analyze"):]
        block = block[:block.index("\ndef ", 1)]
        self.assertIn("_ai_chat", block)
        # the endpoint wraps it in to_thread (never awaited inline)
        ep = SRC[SRC.index('@app.post("/sessions/{user}/{filename}/coach")'):]
        ep = ep[:ep.index("\n@app.", 1)]
        self.assertIn("asyncio.to_thread", ep)


if __name__ == "__main__":
    unittest.main()

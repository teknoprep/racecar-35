"""Estimated wheel power (server/app/power.py). Synthetic runs with known answers."""
import sys
import pathlib
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import power as P  # noqa: E402

DT = 0.04  # 25 Hz


def run(seconds, mph_fn, alt_fn=None, rpm=5000.0):
    t = np.arange(0.0, seconds, DT)
    mph = [float(mph_fn(x)) for x in t]
    alt = [float(alt_fn(x)) for x in t] if alt_fn else [0.0] * len(t)
    rp = [rpm] * len(t)
    return t, mph, alt, rp


def cruise_wkg(mph):
    """Textbook steady-speed answer: P/m = v * g * (0.045 + 0.095 (mph/100)^2)."""
    v = mph * P.MPH_MS
    return v * P.G * (0.045 + 0.095 * (mph / 100.0) ** 2)


class CruiseTests(unittest.TestCase):
    def test_steady_60mph_flat_is_drag_only(self):
        s = P.per_kg_series(*run(30, lambda x: 60.0))
        mid = (s["t"] > 8) & (s["t"] < 22)
        self.assertAlmostEqual(float(np.median(s["wkg"][mid])), cruise_wkg(60.0), delta=0.02 * cruise_wkg(60.0))

    def test_steady_speed_gives_no_torque_without_rpm(self):
        s = P.per_kg_series(*run(20, lambda x: 60.0, rpm=0.0))
        self.assertTrue(np.all(s["tqkg"] == 0.0))


class AccelerationTests(unittest.TestCase):
    def test_constant_0p3g_pull_matches_textbook(self):
        A = 0.3 * P.G                       # m/s^2
        v0 = 20.0 * P.MPH_MS                # starts at 20 mph
        t = np.arange(0.0, 12.0, DT)
        mph = (v0 + A * t) / P.MPH_MS
        s = P.per_kg_series(t, mph.tolist(), [0.0] * len(t), [5000.0] * len(t))
        mid = (s["t"] > 3) & (s["t"] < 9)
        v = (v0 + A * s["t"]) / 1.0
        expect = (A + P.G * (0.045 + 0.095 * ((v / P.MPH_MS) / 100.0) ** 2)) * v
        ratio = s["wkg"][mid] / expect[mid]
        self.assertTrue(np.all(np.abs(ratio - 1.0) < 0.04), f'ratio {ratio.min():.3f}..{ratio.max():.3f}')


class SpikeTests(unittest.TestCase):
    def test_single_glitch_sample_cannot_set_the_peak(self):
        clean = P.per_kg_series(*run(30, lambda x: 60.0))
        t, mph, alt, rpm = run(30, lambda x: 60.0)
        mph[300] = 100.0                    # one sample reads 40 mph too fast
        glitched = P.per_kg_series(t, mph, alt, rpm)
        self.assertLess(glitched["wkg"].max(), clean["wkg"].max() * 1.05)

    def test_glitch_does_not_cross_into_torque(self):
        t, mph, alt, rpm = run(30, lambda x: 60.0)
        mph[300] = 100.0
        s = P.per_kg_series(t, mph, alt, rpm)
        self.assertLess(s["tqkg"].max(), (cruise_wkg(60.0) / P.HP_W * 5252 / 5000) * 1.05)


class TorqueAndGradeTests(unittest.TestCase):
    def test_torque_is_hp_times_5252_over_rpm(self):
        s = P.per_kg_series(*run(20, lambda x: 60.0, rpm=4000.0))
        mid = (s["t"] > 5) & (s["t"] < 15)
        self.assertAlmostEqual(float(np.median(s["tqkg"][mid])),
                               float(np.median(s["wkg"][mid])) / P.HP_W * 5252.0 / 4000.0, places=3)

    def test_five_percent_uphill_adds_g_sin_theta(self):
        v = 60.0 * P.MPH_MS
        flat = P.per_kg_series(*run(30, lambda x: 60.0))
        up = P.per_kg_series(*run(30, lambda x: 60.0, alt_fn=lambda x: 0.05 * v * x))
        mid = (flat["t"] > 8) & (flat["t"] < 22)
        diff = float(np.median(up["wkg"][mid] - flat["wkg"][mid]))
        self.assertAlmostEqual(diff, P.G * 0.05 * v, delta=1.0)


class WeightTests(unittest.TestCase):
    def test_2420_lb_cruise_in_wheel_hp(self):
        s = P.per_kg_series(*run(20, lambda x: 60.0))
        w = P.apply_weight(s, 2420)
        mid = (s["t"] > 5) & (s["t"] < 15)
        expect_hp = cruise_wkg(60.0) * 2420 * P.LB_KG / P.HP_W     # ~30.6 hp
        self.assertAlmostEqual(float(np.median(w["whp"][mid])), expect_hp, delta=0.5)

    def test_power_is_linear_in_weight(self):
        s = P.per_kg_series(*run(20, lambda x: 60.0))
        a = P.apply_weight(s, 2000)["whp"]
        b = P.apply_weight(s, 4000)["whp"]
        self.assertTrue(np.allclose(b, 2 * a))


class LapTests(unittest.TestCase):
    def setUp(self):
        t = np.arange(0.0, 100.0, 1.0)
        wkg = np.where(t < 40, 10.0, 30.0)
        wkg[45] = 12.0
        self.s = {"t": t, "wkg": wkg, "tqkg": wkg / 10.0}
        self.laps = [{"lap": 1, "t_start": 0, "t_end": 39},
                     {"lap": 2, "t_start": 40, "t_end": 80}]

    def test_per_lap_peaks(self):
        got = {r["lap"]: r["wkg"] for r in P.lap_maxima(self.s, self.laps)}
        self.assertEqual(got, {1: 10.0, 2: 30.0})

    def test_session_peak_respects_the_lap_list(self):
        only_first = [self.laps[0]]
        self.assertEqual(P.session_maxima(self.s, only_first)[0], 10.0)
        self.assertEqual(P.session_maxima(self.s, None)[0], 30.0)


class RobustnessTests(unittest.TestCase):
    def test_missing_gps_and_altitude_do_not_break(self):
        t = np.arange(0.0, 10.0, DT)
        mph = [None if i % 7 == 0 else 50.0 for i in range(len(t))]
        s = P.per_kg_series(t, mph, [None] * len(t), [None] * len(t))
        self.assertTrue(np.all(np.isfinite(s["wkg"])))
        self.assertTrue(np.all(np.isfinite(s["tqkg"])))

    def test_tiny_input_is_zeros_not_an_error(self):
        s = P.per_kg_series([0.0, 0.04], [50.0, 50.0], [0.0, 0.0], [5000.0, 5000.0])
        self.assertEqual(len(s["wkg"]), 2)
        s0 = P.per_kg_series([], [], [], [])
        self.assertEqual(len(s0["wkg"]), 0)


class ScaleTests(unittest.TestCase):
    def test_scale_matches_the_wheel_hp_formula(self):
        whp, wtq = P.scale(20.0, 0.01, 2420)
        self.assertAlmostEqual(whp, 20.0 * 2420 * P.LB_KG / P.HP_W, places=6)
        self.assertAlmostEqual(wtq, 0.01 * 2420 * P.LB_KG, places=6)

    def test_missing_peak_stays_missing(self):
        self.assertEqual(P.scale(None, None, 2420), (None, None))


if __name__ == "__main__":
    unittest.main()

"""Estimated wheel power (server/app/power.py). Synthetic runs with hand-computed answers."""
import sys
import pathlib
import unittest

import numpy as np

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "server" / "app"))

import power as P  # noqa: E402

DT = 0.04          # 25 Hz
LB = 2420          # test car weight, lb
M = LB * P.LB_KG   # kg


def run(seconds, mph_fn, alt_fn=None, rpm=5000.0):
    t = np.arange(0.0, seconds, DT)
    mph = [float(mph_fn(x)) for x in t]
    alt = [float(alt_fn(x)) for x in t] if alt_fn else [0.0] * len(t)
    return t, mph, alt, [rpm] * len(t)


def road_load_hp(mph, weight_lb=LB):
    """Textbook steady-speed answer: rolling + aero, at the wheels."""
    v = mph * P.MPH_MS
    m = weight_lb * P.LB_KG
    watts = m * P.G * P.CRR * v + 0.5 * P.RHO * P.CDA * v ** 3
    return watts / P.HP_W


class CruiseTests(unittest.TestCase):
    def test_steady_60mph_flat_is_rolling_plus_aero(self):
        s = P.wheel_series(*run(30, lambda x: 60.0), LB)
        mid = (s["t"] > 8) & (s["t"] < 22)
        self.assertAlmostEqual(float(np.median(s["whp"][mid])), road_load_hp(60.0), delta=0.05)

    def test_steady_60mph_is_about_14_wheel_hp(self):
        # a sanity anchor: a small roadster needs roughly this at 60 mph on the flat
        self.assertTrue(12.0 < road_load_hp(60.0) < 16.0, road_load_hp(60.0))

    def test_no_weight_means_no_numbers(self):
        s = P.wheel_series(*run(10, lambda x: 60.0), None)
        self.assertEqual(len(s["whp"]), 250)
        self.assertTrue(np.all(np.isnan(s["whp"])) and np.all(np.isnan(s["wtq"])))


class AccelerationTests(unittest.TestCase):
    def test_constant_0p3g_pull_matches_textbook(self):
        A = 0.3 * P.G
        v0 = 20.0 * P.MPH_MS
        t = np.arange(0.0, 12.0, DT)
        mph = (v0 + A * t) / P.MPH_MS
        s = P.wheel_series(t, mph.tolist(), [0.0] * len(t), [5000.0] * len(t), LB)
        mid = (s["t"] > 3) & (s["t"] < 9)
        v = v0 + A * s["t"]
        expect = (M * v * (A + P.G * P.CRR) + 0.5 * P.RHO * P.CDA * v ** 3) / P.HP_W
        ratio = s["whp"][mid] / expect[mid]
        self.assertTrue(np.all(np.abs(ratio - 1.0) < 0.04),
                        f"ratio {ratio.min():.3f}..{ratio.max():.3f}")

    def test_more_weight_costs_more_only_where_mass_matters(self):
        # same steady cruise: the difference is exactly the extra rolling resistance
        a = P.wheel_series(*run(20, lambda x: 60.0), 2000)["whp"]
        b = P.wheel_series(*run(20, lambda x: 60.0), 4000)["whp"]
        mid = slice(len(a) // 4, 3 * len(a) // 4)
        extra = 2000 * P.LB_KG * P.G * P.CRR * 60.0 * P.MPH_MS / P.HP_W
        self.assertAlmostEqual(float(np.median(b[mid] - a[mid])), extra, delta=0.02)


class SpikeTests(unittest.TestCase):
    def test_single_glitch_sample_cannot_set_the_peak(self):
        clean = P.wheel_series(*run(30, lambda x: 60.0), LB)
        t, mph, alt, rpm = run(30, lambda x: 60.0)
        mph[300] = 100.0                    # one sample reads 40 mph too fast
        glitched = P.wheel_series(t, mph, alt, rpm, LB)
        self.assertLess(glitched["whp"].max(), clean["whp"].max() * 1.05)

    def test_glitch_does_not_cross_into_torque(self):
        clean = P.wheel_series(*run(30, lambda x: 60.0), LB)
        t, mph, alt, rpm = run(30, lambda x: 60.0)
        mph[300] = 100.0
        s = P.wheel_series(t, mph, alt, rpm, LB)
        self.assertLess(s["wtq"].max(), clean["wtq"].max() * 1.05)


class TorqueAndGradeTests(unittest.TestCase):
    def test_torque_is_hp_times_5252_over_rpm(self):
        s = P.wheel_series(*run(20, lambda x: 60.0, rpm=4000.0), LB)
        mid = (s["t"] > 5) & (s["t"] < 15)
        self.assertAlmostEqual(float(np.median(s["wtq"][mid])),
                               float(np.median(s["whp"][mid])) * 5252.0 / 4000.0, places=3)

    def test_five_percent_uphill_adds_m_g_sin_theta(self):
        v = 60.0 * P.MPH_MS
        flat = P.wheel_series(*run(30, lambda x: 60.0), LB)
        up = P.wheel_series(*run(30, lambda x: 60.0, alt_fn=lambda x: 0.05 * v * x), LB)
        mid = (flat["t"] > 8) & (flat["t"] < 22)
        diff = float(np.median(up["whp"][mid] - flat["whp"][mid]))
        self.assertAlmostEqual(diff, M * P.G * 0.05 * v / P.HP_W, delta=0.3)


class LapTests(unittest.TestCase):
    def setUp(self):
        t = np.arange(0.0, 100.0, 1.0)
        whp = np.where(t < 40, 10.0, 30.0)
        whp[45] = 12.0
        self.s = {"t": t, "whp": whp, "wtq": whp / 2}
        self.laps = [{"lap": 1, "t_start": 0, "t_end": 39},
                     {"lap": 2, "t_start": 40, "t_end": 80}]

    def test_per_lap_peaks(self):
        got = {r["lap"]: r["whp"] for r in P.lap_maxima(self.s, self.laps)}
        self.assertEqual(got, {1: 10.0, 2: 30.0})

    def test_session_peak_respects_the_lap_list(self):
        self.assertEqual(P.session_maxima(self.s, [self.laps[0]])[0], 10.0)
        self.assertEqual(P.session_maxima(self.s, self.laps)[0], 30.0)

    def test_no_laps_means_no_session_peak(self):
        self.assertEqual(P.session_maxima(self.s, []), (None, None))


class RobustnessTests(unittest.TestCase):
    def test_missing_gps_and_altitude_do_not_break(self):
        t = np.arange(0.0, 10.0, DT)
        mph = [None if i % 7 == 0 else 50.0 for i in range(len(t))]
        s = P.wheel_series(t, mph, [None] * len(t), [None] * len(t), LB)
        self.assertTrue(np.all(np.isfinite(s["whp"])))
        self.assertTrue(np.all(np.isfinite(s["wtq"])))

    def test_tiny_and_empty_input(self):
        s = P.wheel_series([0.0, 0.04], [50.0, 50.0], [0.0, 0.0], [5000.0, 5000.0], LB)
        self.assertEqual(len(s["whp"]), 2)
        self.assertEqual(len(P.wheel_series([], [], [], [], LB)["whp"]), 0)


if __name__ == "__main__":
    unittest.main()

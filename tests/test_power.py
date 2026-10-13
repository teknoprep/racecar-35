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


def simulate_pulls(n_pulls=20, hp=100.0, ratio=65.0, weight_lb=LB, outlier=None, heading_rate=0.0):
    """Repeated straight full-throttle pulls in one gear at a known constant wheel power.
    Speed is integrated from (P - road load) / (m v); rpm = ratio x mph; a hard stop
    between pulls. Returns t, mph, rpm, heading."""
    m = weight_lb * P.LB_KG
    t, mph, rpm, hd = [], [], [], []
    now, v = 0.0, 30 * P.MPH_MS
    for k in range(n_pulls):
        pw = hp * P.HP_W * (outlier if (outlier and k == 0) else 1.0)
        while v / P.MPH_MS * ratio < 7000:
            load = m * P.G * P.CRR * v + 0.5 * P.RHO * P.CDA * v ** 3
            v += (pw - load) / (m * v) * DT
            t.append(now); mph.append(v / P.MPH_MS); rpm.append(v / P.MPH_MS * ratio)
            hd.append((now * heading_rate) % 360); now += DT
        for _ in range(int(3.0 / DT)):                       # brake back down, foot off
            v = max(30 * P.MPH_MS, v - 0.9 * P.G * DT)
            t.append(now); mph.append(v / P.MPH_MS); rpm.append(v / P.MPH_MS * ratio)
            hd.append((now * heading_rate) % 360); now += DT
    return np.array(t), mph, rpm, hd


class VirtualDynoTests(unittest.TestCase):
    def lap(self, t):
        return [{"lap": 1, "t_start": 0.0, "t_end": float(t[-1])}]

    def test_flat_100hp_car_reads_100hp_across_the_curve(self):
        t, mph, rpm, hd = simulate_pulls()
        d = P.dyno(t, mph, rpm, LB, np.zeros(len(t)), self.lap(t), heading=hd)
        self.assertTrue(d["straight_only"])
        self.assertGreaterEqual(len(d["curve"]), 8)
        for c in d["curve"]:
            self.assertAlmostEqual(c["whp"], 100.0, delta=3.0, msg=c)
        self.assertAlmostEqual(d["peak"]["whp"], 100.0, delta=3.0)
        # torque is the same power over rpm, so it peaks at the bottom of the curve
        lo = d["curve"][0]
        self.assertAlmostEqual(d["peak"]["wtq"], lo["whp"] * 5252 / lo["rpm"], places=6)

    def test_one_wild_pull_cannot_move_the_curve(self):
        t, mph, rpm, hd = simulate_pulls(outlier=1.6)          # one pull reads 60 % high
        d = P.dyno(t, mph, rpm, LB, np.zeros(len(t)), self.lap(t), heading=hd)
        self.assertAlmostEqual(d["peak"]["whp"], 100.0, delta=4.0)

    def test_cornering_pulls_fall_back_and_say_so(self):
        # turning hard the whole time (> 0.35 g): no straight pulls, so every pull is used
        t, mph, rpm, hd = simulate_pulls(heading_rate=40.0)
        d = P.dyno(t, mph, rpm, LB, np.zeros(len(t)), self.lap(t), heading=hd)
        self.assertFalse(d["straight_only"])
        self.assertAlmostEqual(d["peak"]["whp"], 100.0, delta=3.0)

    def test_corner_exit_rpm_becomes_a_dashed_tier_not_the_peak(self):
        t, mph, rpm, hd = simulate_pulls()
        # below 4000 rpm the car is turning hard (a corner exit); above it, straight
        hd = list(hd); ang = 0.0
        for i in range(1, len(t)):
            if rpm[i] < 4000:
                ang += 40.0 * DT
            hd[i] = ang % 360
        d = P.dyno(t, mph, rpm, LB, np.zeros(len(t)), self.lap(t), heading=hd)
        main = [c for c in d["curve"] if c["tier"] == "main"]
        low = [c for c in d["curve"] if c["tier"] == "low"]
        self.assertTrue(main and low)
        self.assertLess(max(c["rpm"] for c in low), min(c["rpm"] for c in main))
        self.assertGreaterEqual(min(c["rpm"] for c in main), 3900)
        best = max(main, key=lambda c: c["wtq"])
        self.assertEqual(d["peak"]["wtq_rpm"], best["rpm"])      # never from the dashed tier

    def test_each_lap_reports_its_maximum(self):
        t, mph, rpm, hd = simulate_pulls()
        half = float(t[len(t) // 2])
        laps = [{"lap": 1, "t_start": 0.0, "t_end": half}, {"lap": 2, "t_start": half, "t_end": float(t[-1])}]
        d = P.dyno(t, mph, rpm, LB, np.zeros(len(t)), laps, heading=hd)
        for lp in d["laps"]:
            self.assertAlmostEqual(lp["whp"], 100.0, delta=3.0, msg=lp)

    def test_no_weight_no_curve(self):
        t, mph, rpm, hd = simulate_pulls(n_pulls=6)
        self.assertEqual(P.dyno(t, mph, rpm, 0, None, self.lap(t))["curve"], [])


class GearTests(unittest.TestCase):
    def test_two_gears_are_found(self):
        mph = np.concatenate([np.linspace(20, 60, 2000), np.linspace(40, 90, 2000)])
        rpm = np.concatenate([mph[:2000] * 100.0, mph[2000:] * 70.0])
        g = sorted(np.exp(P.gear_centres(rpm, mph)))
        self.assertEqual(len(g), 2)
        self.assertAlmostEqual(g[0], 70.0, delta=1.5)
        self.assertAlmostEqual(g[1], 100.0, delta=2.0)

    def test_shift_samples_are_not_pull_samples(self):
        t = np.arange(0.0, 20.0, DT)
        mph = 30 + 3.0 * t                                     # steady pull
        rpm = np.where(t < 10, mph * 100.0, mph * 70.0)        # upshift at t = 10 s
        a = np.full(len(t), 3.0 * P.MPH_MS)
        pull, gears = P.pull_mask(t, mph, rpm, a)
        near = np.abs(t - 10.0) < P.SHIFT_GUARD_S
        self.assertFalse(pull[near].any())
        self.assertTrue(pull[(t > 2) & (t < 9)].all())


class GradeProfileTests(unittest.TestCase):
    def laps(self, n=6, offsets=None, seed=1):
        rng = np.random.default_rng(seed)
        L, v = 2000.0, 30.0                                    # 2 km lap at 30 m/s
        per = L / v
        t = np.arange(0.0, n * per, DT)
        frac = (t % per) / per
        true_el = 8.0 * np.sin(2 * np.pi * frac)               # +/- 8 m hill
        el = true_el + rng.normal(0, 1.0, len(t))              # 1 m sideways-wander noise
        if offsets is not None:
            el = el + np.array(offsets)[np.minimum((t // per).astype(int), n - 1)]
        wins = [(k * per, (k + 1) * per - DT) for k in range(n)]
        true_grade = 8.0 * 2 * np.pi / L * np.cos(2 * np.pi * frac)
        return t, [v / P.MPH_MS] * len(t), el, wins, true_grade

    def test_consensus_recovers_the_hill_through_noise(self):
        t, mph, el, wins, true_g = self.laps()
        g, info = P.grade_profile(t, mph, el, wins)
        self.assertTrue(info["used"], info)
        mid = slice(len(t) // 6, 5 * len(t) // 6)
        self.assertLess(np.median(np.abs(g[mid] - true_g[mid])), 0.004)   # < 0.4 % grade

    def test_laps_that_disagree_are_rejected(self):
        t, mph, el, wins, _ = self.laps(offsets=[0, 3, 0, 3, 0, 3])
        g, info = P.grade_profile(t, mph, el, wins)
        self.assertFalse(info["used"])
        self.assertTrue(np.all(g == 0))

    def test_too_few_laps_is_flat(self):
        t, mph, el, wins, _ = self.laps(n=2)
        g, info = P.grade_profile(t, mph, el, wins)
        self.assertFalse(info["used"])


if __name__ == "__main__":
    unittest.main()

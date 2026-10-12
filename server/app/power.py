"""Estimated WHEEL power and torque for one session.

These are wheel figures ("est WHP" / "est WTQ"). No drivetrain loss is taken off.

Power at the wheels is what the car's ENGINE must deliver through the drivetrain
to hold the logged motion. The road-load model is

    P = m * v * (a + g * grade + g * CRR) + 0.5 * RHO * CDA * v^3

  m      car weight with driver, in kg (from the session's car weight, lb)
  v      GPS speed in m/s (the logged speed_mph; null = 0)
  a      longitudinal acceleration: a least-squares slope of speed against TIME over
         +/-0.25 s, the same GPS-only path as RC3D.longG in the 3D view. Differencing
         the raw speed sample to sample is what produced the wild early numbers.
  grade  d(altitude)/d(distance). The altitude slope is taken over +/-4 s and clamped
         to +/-6 %, and is zero below 5 m/s, where GPS altitude is noise.
  CRR    rolling resistance coefficient (street tyres on asphalt)
  CDA    drag area, Cd x frontal area, m^2
  RHO    air density, kg/m^3

The three constants are TYPICAL VALUES for a small roadster, not measured on the car.
They are the largest source of error and are kept here so that they can be measured
later. A previous version used the throttle/coast classifier's coasting-deceleration
curve instead. That curve includes engine braking, so it overstated the aero load at
speed by roughly 2x and gave about 150 WHP on a car that dynos at 124.

Spike filter, four layers, in this order:
  1. speed and altitude are median-of-5 BEFORE they are differenced. A single bad GPS
     fix would otherwise spread through the acceleration regression and lift the peak.
  2. a sample whose acceleration is more than 1 g FORWARD is dropped (set to 0).
     Braking (negative power) is also 0.
  3. a median of 5 on the power itself: outliers are ignored, not averaged in.
  4. a 2 s time-window average, because an engine's peak is what it holds for seconds.

Torque: lb-ft = HP * 5252 / rpm. Below 400 rpm there is no torque figure.

The model is computed for the session's own weight (no weight, no numbers), so the
per-lap and session peaks are all for one set of assumptions. Nothing here is checked
against a dyno except the single point recorded in CLAUDE.md.
"""
from __future__ import annotations

from typing import Iterable, Optional

import numpy as np

G = 9.80665
LB_KG = 0.45359237
HP_W = 745.69987
MPH_MS = 0.44704
TQ_CONST = 5252.0
RPM_MIN = 400.0

CRR = 0.012          # rolling resistance (street tyres, asphalt)
CDA = 0.60           # m^2: Cd ~0.36 x frontal ~1.65 m^2 for a Miata-class car
RHO = 1.20           # kg/m^3, sea level, about 20 C

ACCEL_HALF_S = 0.25          # longG's window
GRADE_HALF_S = 4.0
GRADE_MAX = 0.06
GRADE_MIN_MS = 5.0
GLITCH_G = 1.0               # forward acceleration above this is a GPS glitch
PEAK_HALF_S = 1.0            # 2 s total averaging window


def _prefix(x: np.ndarray) -> np.ndarray:
    # longdouble: a 3 h session has t ~ 1e4 s, and float64 prefix sums lose the
    # tiny window variance that a 0.25 s slope is made of
    return np.concatenate([np.zeros(1, dtype=np.longdouble),
                           np.cumsum(x.astype(np.longdouble))])


def _slope(t: np.ndarray, y: np.ndarray, half: float, max_span: float) -> np.ndarray:
    """dy/dt over the samples within +/- half seconds (least squares, time = x).

    Windows are widened to the nearest neighbours when sparse (>= 3 points), and a
    window whose span exceeds max_span (a logging gap) gives 0, as RC3D.longG does.
    """
    n = len(t)
    if n < 3:
        return np.zeros(n)
    ts = (t - t[0]).astype(np.longdouble)
    yy = y.astype(np.longdouble)
    lo = np.searchsorted(ts, ts - half, side="left")
    hi = np.searchsorted(ts, ts + half, side="right")
    idx = np.arange(n)
    lo = np.minimum(lo, np.maximum(idx - 1, 0))
    hi = np.maximum(hi, np.minimum(idx + 2, n))
    cnt = (hi - lo).astype(np.longdouble)
    pt, py = _prefix(ts), _prefix(yy)
    ptt, pty = _prefix(ts * ts), _prefix(ts * yy)
    st = pt[hi] - pt[lo]
    sy = py[hi] - py[lo]
    stt = ptt[hi] - ptt[lo]
    sty = pty[hi] - pty[lo]
    den = cnt * stt - st * st
    num = cnt * sty - st * sy
    span = ts[np.maximum(hi - 1, 0)] - ts[lo]
    ok = (cnt >= 3) & (span > 0) & (span <= max_span) & (den > 1e-9)
    out = np.zeros(n)
    out[ok] = (num[ok] / den[ok]).astype(float)
    return out


def _median5(x: np.ndarray) -> np.ndarray:
    n = len(x)
    if n == 0:
        return x
    p = np.pad(x, 2, mode="edge")
    return np.median(np.stack([p[k:k + n] for k in range(5)]), axis=0)


def _window_mean(t: np.ndarray, x: np.ndarray, half: float) -> np.ndarray:
    ts = (t - t[0]).astype(np.longdouble)
    p = _prefix(x)
    lo = np.searchsorted(ts, ts - half, side="left")
    hi = np.searchsorted(ts, ts + half, side="right")
    return ((p[hi] - p[lo]) / (hi - lo).astype(np.longdouble)).astype(float)


def _fill_nan(a: np.ndarray) -> np.ndarray:
    """Linear fill of missing (NaN) values; all-missing becomes zeros."""
    good = np.isfinite(a)
    if not good.any():
        return np.zeros(len(a))
    idx = np.arange(len(a))
    return np.interp(idx, idx[good], a[good])


def _as_float_array(values: Iterable, fill: float) -> np.ndarray:
    arr = [float(x) if (x is not None and not isinstance(x, bool)
                        and isinstance(x, (int, float))
                        and np.isfinite(float(x))) else np.nan for x in values]
    out = np.asarray(arr, dtype=float)
    return np.where(np.isfinite(out), out, fill)


def air_density(elev_m: Optional[float]) -> float:
    """Sea-level RHO thinned with altitude (scale height 8.5 km). Unknown = sea level."""
    if elev_m is None or not np.isfinite(elev_m):
        return RHO
    return RHO * float(np.exp(-float(elev_m) / 8500.0))


def _inst_power(t, mph_a, rpm_a, weight_lb, grade, rho):
    """Despiked speed/rpm, regression acceleration and instantaneous wheel power (W)."""
    mph5 = _median5(mph_a)                                       # layer 1: despike input
    rpm5 = _median5(rpm_a)
    v = mph5 * MPH_MS
    a = _slope(t, v, ACCEL_HALF_S, max_span=2.0)                 # m/s^2
    m = float(weight_lb) * LB_KG
    p = m * v * (a + G * grade + G * CRR) + 0.5 * rho * CDA * v ** 3
    p = np.where(a > GLITCH_G * G, 0.0, p)                       # layer 2: glitch
    p = np.maximum(p, 0.0)                                       # braking is not power
    return mph5, rpm5, v, a, p


def wheel_series(t: Iterable, mph: Iterable, alt: Iterable, rpm: Iterable,
                 weight_lb: float, grade: Optional[Iterable] = None,
                 elev_m: Optional[float] = None) -> dict:
    """Estimated WHEEL power (hp) and torque (lb-ft) per sample for one car weight.

    t          seconds (epoch or relative; only differences matter), monotonic
    mph        speed; None / NaN = 0 (no GPS)
    alt        altitude in metres; None = unknown (grade 0)
    rpm        engine speed; None / < 400 = no torque figure
    weight_lb  car weight with driver, lb
    """
    t = np.asarray(list(t), dtype=float)
    n = len(t)
    if n == 0 or weight_lb is None or float(weight_lb) <= 0:
        nan = np.full(n, np.nan)              # no weight = no numbers, never a guess
        return {"t": t, "whp": nan, "wtq": nan.copy()}
    mph_a = _as_float_array(mph, 0.0)
    alt_a = _as_float_array(alt, np.nan)
    rpm_a = _as_float_array(rpm, 0.0)

    if grade is None:                    # fallback: raw altitude slope (noisy; see grade_profile)
        v0 = _median5(mph_a) * MPH_MS
        h = _median5(_fill_nan(alt_a))
        dh = _slope(t, h, GRADE_HALF_S, max_span=2 * GRADE_HALF_S + 1.0)
        with np.errstate(divide="ignore", invalid="ignore"):
            g = np.where(v0 >= GRADE_MIN_MS, dh / np.maximum(v0, 1e-9), 0.0)
        grade = np.clip(np.nan_to_num(g), -GRADE_MAX, GRADE_MAX)
    else:
        grade = np.nan_to_num(np.asarray(list(grade), dtype=float))
    _, rpm5, _, _, p = _inst_power(t, mph_a, rpm_a, weight_lb, grade, air_density(elev_m))
    p = _median5(p)                                              # layer 3: median
    whp = _window_mean(t, p, PEAK_HALF_S) / HP_W                 # layer 4: 2 s mean
    # torque on the SAME clock: rpm averaged over the same window. Dividing a 2 s power
    # average by the instantaneous rpm made a false torque spike after every upshift.
    rpm_w = _window_mean(t, rpm5, PEAK_HALF_S)
    with np.errstate(divide="ignore", invalid="ignore"):
        wtq = np.where(rpm_w >= RPM_MIN, whp * TQ_CONST / rpm_w, 0.0)
    return {"t": t, "whp": whp, "wtq": np.nan_to_num(wtq)}


# ---------------------------------------------------------------------------
# Grade from a lap-consensus elevation profile
# ---------------------------------------------------------------------------
GRADE_PROFILE_MAX = 0.12          # a real circuit's steepest pitch is under ~10 %
GRADE_SMOOTH_M = 50.0             # along-track smoothing of the profile
GRADE_SPREAD_MAX_M = 0.5          # lap-to-lap disagreement above this = untrustworthy


def grade_profile(t, mph, elev, lap_windows) -> tuple:
    """Grade per sample from ONE elevation profile per lap position.

    Elevation sampled at each GPS fix (lidar DEM or GPS altitude) moves 1-2 m with the
    car's sideways wander, which over a 30 m window is a several-percent grade. The hill
    is in the same place every lap, so the profile is the MEDIAN across laps at each
    5 m of lap distance, smoothed over 50 m. If the laps disagree by more than 0.5 m
    (typical median), the source is rejected and grade is 0: a bare-earth DEM under a
    bridge, or a session whose laps are different layouts, must not invent hills.

    lap_windows: [(t_start, t_end)] of the INCLUDED laps, same clock as t.
    Returns (grade array, info dict). Samples outside the laps get grade 0.
    """
    t = np.asarray(t, dtype=float)
    n = len(t)
    zero = np.zeros(n)
    info = {"laps": 0, "spread_m": None, "relief_m": None, "used": False}
    el = _as_float_array(elev, np.nan)
    if n == 0 or not np.isfinite(el).any() or len(lap_windows) < 3:
        info["why"] = "fewer than 3 laps" if len(lap_windows) < 3 else "no elevation"
        return zero, info
    v = _median5(_as_float_array(mph, 0.0)) * MPH_MS
    laps = []
    for t0, t1 in lap_windows:
        idx = np.where((t >= t0) & (t <= t1))[0]
        if len(idx) < 20:
            continue
        d = np.concatenate([[0.0], np.cumsum(v[idx][1:] * np.diff(t[idx]))])
        if d[-1] <= 0:
            continue
        laps.append((idx, d))
    if len(laps) < 3:
        info["why"] = "fewer than 3 usable laps"
        return zero, info
    lens = np.array([d[-1] for _, d in laps])
    med = float(np.median(lens))
    laps = [(i, d) for i, d in laps if abs(d[-1] - med) <= 0.1 * med]   # same layout only
    if len(laps) < 3:
        info["why"] = "laps differ in length"
        return zero, info
    nb = max(50, int(med / 5.0))
    binlen = med / nb
    prof = np.full((len(laps), nb), np.nan)
    for k, (idx, d) in enumerate(laps):
        b = np.minimum((d / d[-1] * nb).astype(int), nb - 1)
        e = el[idx]
        ok = np.isfinite(e)
        sums = np.bincount(b[ok], weights=e[ok], minlength=nb)
        cnts = np.bincount(b[ok], minlength=nb)
        with np.errstate(invalid="ignore", divide="ignore"):
            prof[k] = np.where(cnts > 0, sums / np.maximum(cnts, 1), np.nan)
    with np.errstate(all="ignore"):
        spread = float(np.nanmedian(np.nanstd(prof, axis=0)))
        pm = np.nanmedian(prof, axis=0)
    good = np.isfinite(pm)
    info.update(laps=len(laps), spread_m=round(spread, 2))
    if good.sum() < nb * 0.8:
        info["why"] = "elevation missing on part of the lap"
        return zero, info
    if not np.isfinite(spread) or spread > GRADE_SPREAD_MAX_M:
        info["why"] = "laps disagree on elevation"
        return zero, info
    ib = np.arange(nb)
    pm = np.interp(ib, ib[good], pm[good])
    w = max(3, int(round(GRADE_SMOOTH_M / binlen)) | 1)
    pad = np.concatenate([pm[-w:], pm, pm[:w]])                  # a lap is a loop
    sm = np.convolve(pad, np.ones(w) / w, mode="same")[w:-w]
    gb = np.gradient(np.concatenate([sm[-1:], sm, sm[:1]]))[1:-1] / binlen
    gb = np.clip(gb, -GRADE_PROFILE_MAX, GRADE_PROFILE_MAX)
    grade = zero.copy()
    for idx, d in laps:
        grade[idx] = gb[np.minimum((d / d[-1] * nb).astype(int), nb - 1)]
    info.update(used=True, relief_m=round(float(sm.max() - sm.min()), 1))
    return grade, info


# ---------------------------------------------------------------------------
# Virtual dyno: power against rpm, from the full-throttle pulls only
# ---------------------------------------------------------------------------
GEAR_TOL = 0.06          # |log(rpm/mph) - gear| below this = in that gear
SHIFT_GUARD_S = 0.3      # samples this close to a shift / clutch slip are dropped
PULL_MIN_S = 1.0         # a pull shorter than this is not a pull
CURVE_BIN_RPM = 250
CURVE_PCT = 90           # the full-throttle level at each rpm (partial throttle sits below)
CURVE_MIN_PULLS = 5
LAT_MAX_G = 0.35         # near-straight only: scrub ~ lateral g squared (~3 hp here, ~20 at 0.8 g)


def gear_centres(rpm5: np.ndarray, mph5: np.ndarray) -> list:
    """The car's gears, as log(rpm/mph) peaks. Gears are >= ~19 % apart."""
    ok = (mph5 > 15) & (rpm5 > 1500)
    if ok.sum() < 100:
        return []
    lr = np.log(rpm5[ok] / mph5[ok])
    edges = np.arange(lr.min() - 0.05, lr.max() + 0.06, 0.01)
    h, e = np.histogram(lr, bins=edges)
    hs = np.convolve(h, np.ones(3) / 3, mode="same")
    pk = [i for i in range(1, len(hs) - 1)
          if hs[i] >= hs[i - 1] and hs[i] > hs[i + 1] and hs[i] > 0.02 * hs.max()]
    out: list = []
    for i in sorted(pk, key=lambda i: -hs[i]):
        c = (e[i] + e[i + 1]) / 2
        if all(abs(c - o) >= 0.12 for o in out):
            out.append(c)
    return sorted(out)


def _count_in(t, mask, half):
    ts = t - t[0]
    c = np.concatenate([[0.0], np.cumsum(mask.astype(float))])
    lo = np.searchsorted(ts, ts - half, side="left")
    hi = np.searchsorted(ts, ts + half, side="right")
    return c[hi] - c[lo]


def _masked_mean(t, x, mask, half):
    ts = t - t[0]
    p = np.concatenate([[0.0], np.cumsum(np.where(mask, x, 0.0))])
    c = np.concatenate([[0.0], np.cumsum(mask.astype(float))])
    lo = np.searchsorted(ts, ts - half, side="left")
    hi = np.searchsorted(ts, ts + half, side="right")
    k = c[hi] - c[lo]
    return np.where(k > 0, (p[hi] - p[lo]) / np.maximum(k, 1), 0.0)


def pull_mask(t, mph5, rpm5, a) -> tuple:
    """Samples that are a clean full-throttle-style pull: in a gear (rpm/speed on a gear
    ratio, so no shift or clutch slip within SHIFT_GUARD_S), accelerating, rpm rising."""
    gears = gear_centres(rpm5, mph5)
    if not gears:
        return np.zeros(len(t), dtype=bool), gears
    lr = np.log(np.maximum(rpm5, 1.0) / np.maximum(mph5, 1.0))
    dist = np.abs(lr[:, None] - np.array(gears)[None, :])
    near, gi = dist.min(axis=1), dist.argmin(axis=1)
    ingear = (mph5 > 15) & (rpm5 > 1500) & (near < GEAR_TOL)
    # a CHANGE of gear is a shift even when no sample landed between the ratios
    # (a quick shift logged at 25 Hz can jump straight from one gear to the next)
    changed = np.zeros(len(t), dtype=bool)
    changed[1:] = gi[1:] != gi[:-1]
    changed[:-1] |= changed[1:]
    bad = ~ingear | changed
    ingear &= _count_in(t, bad, SHIFT_GUARD_S) == 0
    drpm = _slope(t, rpm5, ACCEL_HALF_S, max_span=2.0)
    return ingear & (a > 0.05 * G) & (a < GLITCH_G * G) & (drpm > 0), gears


def _pull_bins(t, rpm_s, pw_s, pull, sel):
    """{bin_start_rpm: [mean power of each pull in that bin]} over pulls inside sel."""
    m = pull & sel
    edges = np.diff(np.concatenate([[0], m.astype(int), [0]]))
    st, en = np.where(edges == 1)[0], np.where(edges == -1)[0]
    out: dict = {}
    npulls = 0
    for s0, e0 in zip(st, en):
        if t[e0 - 1] - t[s0] < PULL_MIN_S:
            continue
        npulls += 1
        b = (rpm_s[s0:e0] // CURVE_BIN_RPM).astype(int) * CURVE_BIN_RPM
        for bb in np.unique(b):
            k = b == bb
            if k.sum() >= 3:
                out.setdefault(int(bb), []).append(float(pw_s[s0:e0][k].mean()))
    return out, npulls


def _curve_from_bins(bins: dict, stat, min_pulls: int) -> list:
    keys = sorted(k for k, v in bins.items() if len(v) >= min_pulls)
    raw = [(k + CURVE_BIN_RPM / 2.0, stat(np.array(bins[k])), len(bins[k])) for k in keys]
    out = []
    for i, (r, w, nn) in enumerate(raw):      # 3-bin median: one odd bin cannot be the peak
        nb = [raw[j][1] for j in (i - 1, i, i + 1)
              if 0 <= j < len(raw) and abs(raw[j][0] - r) <= CURVE_BIN_RPM * 1.01]
        out.append({"rpm": int(r), "whp": float(np.median(nb)), "n": nn})
    for c in out:
        c["wtq"] = c["whp"] * TQ_CONST / c["rpm"]
    return out


def _peaks(curve: list) -> dict:
    if not curve:
        return {"whp": None, "wtq": None, "whp_rpm": None, "wtq_rpm": None}
    h = max(curve, key=lambda c: c["whp"])
    q = max(curve, key=lambda c: c["wtq"])
    return {"whp": h["whp"], "wtq": q["wtq"], "whp_rpm": h["rpm"], "wtq_rpm": q["rpm"]}


def lateral_g(t, v, heading_deg) -> np.ndarray:
    """|lateral acceleration| in g from speed x turn rate (GPS course, unwrapped)."""
    h = _as_float_array(heading_deg, np.nan)
    if not np.isfinite(h).any():
        return np.zeros(len(t))
    psi = np.unwrap(np.radians(_fill_nan(h)))
    rate = _slope(t, psi, ACCEL_HALF_S, max_span=2.0)            # rad/s
    return np.abs(v * rate) / G


def dyno(t, mph, rpm, weight_lb, grade, lap_windows, elev_m=None, heading=None) -> dict:
    """The session's virtual dyno curve and peaks, plus a peak per lap.

    Session: at each 250-rpm bin, the 90th percentile over every clean pull's mean power
    in that bin (>= 5 pulls), then a 3-bin median. That is the full-throttle level at that
    rpm: partial-throttle pulls sit below it, and a single noisy pull cannot set it.
    Lap: the same bins from that lap's pulls only (median per bin, 3-bin median).
    lap_windows: [{"lap", "t_start", "t_end"}] of the INCLUDED laps.
    """
    t = np.asarray(list(t), dtype=float)
    empty = {"curve": [], "peak": _peaks([]), "laps": [], "pulls": 0, "gears": 0}
    if len(t) < 50 or not weight_lb or float(weight_lb) <= 0:
        return empty
    mph_a, rpm_a = _as_float_array(mph, 0.0), _as_float_array(rpm, 0.0)
    g = np.nan_to_num(np.asarray(list(grade), dtype=float)) if grade is not None else np.zeros(len(t))
    mph5, rpm5, v, a, p = _inst_power(t, mph_a, rpm_a, weight_lb, g, air_density(elev_m))
    pull, gears = pull_mask(t, mph5, rpm5, a)
    # A dyno is a straight line. Out of a corner the tyres run a slip angle, and that
    # scrub absorbs power the speed trace never shows (measured: 3rd-gear corner exits
    # read 15-25 % under 4th-gear straights at the same rpm). Straight pulls only.
    in_laps = np.zeros(len(t), dtype=bool)
    for lp in lap_windows:
        in_laps |= (t >= float(lp["t_start"])) & (t <= float(lp["t_end"]))

    def build(mask):
        pw = _masked_mean(t, p, mask, ACCEL_HALF_S) / HP_W         # 0.5 s, inside pulls only
        rs = _masked_mean(t, rpm5, mask, ACCEL_HALF_S)             # rpm on the same clock
        bins, k = _pull_bins(t, rs, pw, mask, in_laps)
        return pw, rs, k, _curve_from_bins(bins, lambda x: np.percentile(x, CURVE_PCT),
                                           CURVE_MIN_PULLS)

    straight = False
    if heading is not None:
        sp = pull & (lateral_g(t, v, heading) < LAT_MAX_G)
        pw_s, rpm_s, npulls, curve = build(sp)
        if len(curve) >= 4:                     # enough straight pulls for a real curve
            pull, straight = sp, True
    if not straight:
        if not pull.any():
            return dict(empty, gears=len(gears))
        pw_s, rpm_s, npulls, curve = build(pull)
    laps = []
    for lp in lap_windows:
        sel = (t >= float(lp["t_start"])) & (t <= float(lp["t_end"]))
        lb, _ = _pull_bins(t, rpm_s, pw_s, pull, sel)
        pk = _peaks(_curve_from_bins(lb, np.median, 1))
        laps.append({"lap": lp["lap"], "whp": pk["whp"], "wtq": pk["wtq"]})
    return {"curve": curve, "peak": _peaks(curve), "laps": laps, "pulls": npulls,
            "gears": len(gears), "straight_only": straight}


def _maxima(series: dict, lo: float, hi: float):
    t = series["t"]
    m = (t >= lo) & (t <= hi)
    if not m.any():
        return None, None
    return float(series["whp"][m].max()), float(series["wtq"][m].max())


def lap_maxima(series: dict, laps: list) -> list:
    """Peak WHP and WTQ inside each lap window.

    laps: [{"lap": n, "t_start": s, "t_end": s}] (the server's _detect_laps shape).
    Returns [{"lap", "whp", "wtq"}] (None where a lap has no samples).
    """
    out = []
    for lp in laps:
        w, q = _maxima(series, float(lp["t_start"]), float(lp["t_end"]))
        out.append({"lap": lp["lap"], "whp": w, "wtq": q})
    return out


def session_maxima(series: dict, laps: Optional[list]) -> tuple:
    """Session peak WHP and WTQ over the given lap windows. No laps = no answer
    (a whole-file peak would count the out-lap and pit-lane junk)."""
    if not laps:
        return None, None
    ws = [_maxima(series, float(lp["t_start"]), float(lp["t_end"])) for lp in laps]
    ws = [w for w in ws if w[0] is not None]
    if not ws:
        return None, None
    return max(w[0] for w in ws), max(w[1] for w in ws)


def round_or_none(x, nd: int = 0):
    if x is None or not np.isfinite(x):
        return None
    return round(float(x), nd)

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


def wheel_series(t: Iterable, mph: Iterable, alt: Iterable, rpm: Iterable,
                 weight_lb: float) -> dict:
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

    mph_a = _median5(mph_a)                                      # layer 1: despike input
    v = mph_a * MPH_MS
    a = _slope(t, v, ACCEL_HALF_S, max_span=2.0)                 # m/s^2
    h = _median5(_fill_nan(alt_a))
    dh = _slope(t, h, GRADE_HALF_S, max_span=2 * GRADE_HALF_S + 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        grade = np.where(v >= GRADE_MIN_MS, dh / np.maximum(v, 1e-9), 0.0)
    grade = np.clip(np.nan_to_num(grade), -GRADE_MAX, GRADE_MAX)

    m = float(weight_lb) * LB_KG
    p = m * v * (a + G * grade + G * CRR) + 0.5 * RHO * CDA * v ** 3   # W at the wheels
    p = np.where(a > GLITCH_G * G, 0.0, p)                       # layer 2: glitch
    p = np.maximum(p, 0.0)                                       # braking is not power
    p = _median5(p)                                              # layer 3: median
    whp = _window_mean(t, p, PEAK_HALF_S) / HP_W                 # layer 4: 2 s mean

    with np.errstate(divide="ignore", invalid="ignore"):
        wtq = np.where(rpm_a >= RPM_MIN, whp * TQ_CONST / rpm_a, 0.0)
    return {"t": t, "whp": whp, "wtq": np.nan_to_num(wtq)}


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

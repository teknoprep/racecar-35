"""Estimated WHEEL power and torque for one session.

These are wheel figures ("est WHP" / "est WTQ"). No drivetrain loss is taken off,
because the car's engine and drivetrain are not measured here.

Per sample, the power at the road per kilogram is

    P/m = v * (a + C(v) + g * grade)

  v      GPS speed in m/s (the logged speed_mph; null = 0)
  a      longitudinal acceleration: a least-squares slope of speed against TIME
         over +/-0.25 s. This is the same GPS-only path as RC3D.longG in the 3D
         view. Differencing the raw speed sample to sample is what produced
         565 hp on a 2420 lb car, so it is not used.
  C(v)   the coast curve the throttle/coast classifier already uses,
         g * (0.045 + 0.095 * (mph/100)^2): rolling, aero and engine braking.
  grade  d(altitude)/d(distance). The altitude slope is taken over +/-4 s and
         clamped to +/-6 %, and is zero below 5 m/s, where GPS altitude is noise.

Spike filter, four layers, in this order:
  1. the INPUT is despiked: speed and altitude get a median of 5 before anything is
     differenced. A single bad GPS fix is replaced by its neighbours' median, so it
     cannot leak into the acceleration regression (which spreads one outlier over
     about seven samples and would otherwise lift the peak by ~17 %).
  2. a sample whose acceleration is still more than 1 g FORWARD is dropped (set to
     0). Braking (negative power) is also 0.
  3. a median of 5 on the power itself, so outliers are ignored, not averaged in.
  4. a 2 s time-window average. An engine's peak is what it holds for seconds.

Torque: lb-ft = HP * 5252 / rpm. Below 400 rpm there is no torque figure.

Car weight is applied LAST. Power is linear in mass, so one per-kg series serves the
review page, the 3D view and the sessions list. Nothing here is calibrated against
a dyno; the numbers are estimates.
"""
from __future__ import annotations

import math
from typing import Iterable, Optional

import numpy as np

G = 9.80665
LB_KG = 0.45359237
HP_W = 745.69987
MPH_MS = 0.44704
TQ_CONST = 5252.0
RPM_MIN = 400.0
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


def per_kg_series(t: Iterable, mph: Iterable, alt: Iterable, rpm: Iterable) -> dict:
    """Estimated power (W/kg, at the wheels) and torque (lb-ft per kg) per sample.

    t    seconds from the session start (server _relative_seconds, monotonic)
    mph  speed; None / NaN = 0 (no GPS)
    alt  altitude in metres; None = unknown (grade 0)
    rpm  engine speed; None / < 400 = no torque figure
    """
    t = np.asarray(list(t), dtype=float)
    n = len(t)
    if n == 0:
        return {"t": t, "wkg": t.copy(), "tqkg": t.copy()}
    mph_a = np.asarray([float(x) if x is not None else np.nan for x in mph], dtype=float)
    mph_a = np.where(np.isfinite(mph_a), mph_a, 0.0)
    alt_a = np.asarray([float(x) if x is not None else np.nan for x in alt], dtype=float)
    rpm_a = np.asarray([float(x) if x is not None else np.nan for x in rpm], dtype=float)
    rpm_a = np.where(np.isfinite(rpm_a), rpm_a, 0.0)

    mph_a = _median5(mph_a)                                      # layer 1: despike input
    v = mph_a * MPH_MS
    a = _slope(t, v, ACCEL_HALF_S, max_span=2.0)                 # m/s^2
    h = _median5(_fill_nan(alt_a))
    dh = _slope(t, h, GRADE_HALF_S, max_span=2 * GRADE_HALF_S + 1.0)
    with np.errstate(divide="ignore", invalid="ignore"):
        grade = np.where(v >= GRADE_MIN_MS, dh / np.maximum(v, 1e-9), 0.0)
    grade = np.clip(np.nan_to_num(grade), -GRADE_MAX, GRADE_MAX)
    c = G * (0.045 + 0.095 * (mph_a / 100.0) ** 2)               # coast curve, m/s^2

    x = (a + c + G * grade) * v                                  # W/kg
    x = np.where(a > GLITCH_G * G, 0.0, x)                       # layer 2: glitch
    x = np.maximum(x, 0.0)                                       # braking is not power
    x = _median5(x)                                              # layer 3: median
    wkg = _window_mean(t, x, PEAK_HALF_S)                        # layer 4: 2 s mean

    with np.errstate(divide="ignore", invalid="ignore"):
        tqkg = np.where(rpm_a >= RPM_MIN, wkg / HP_W * TQ_CONST / rpm_a, 0.0)
    return {"t": t, "wkg": wkg, "tqkg": np.nan_to_num(tqkg)}


def apply_weight(series: dict, weight_lb: float) -> dict:
    """Scale the per-kg series to a car: est WHP and est WTQ (lb-ft) per sample."""
    kg = float(weight_lb) * LB_KG
    return {"t": series["t"],
            "whp": series["wkg"] * kg / HP_W,
            "wtq": series["tqkg"] * kg}


def _maxima(series: dict, lo: float, hi: float):
    t = series["t"]
    m = (t >= lo) & (t <= hi)
    if not m.any():
        return None, None
    return float(series["wkg"][m].max()), float(series["tqkg"][m].max())


def lap_maxima(series: dict, laps: list) -> list:
    """Peak per-kg power and torque inside each lap window.

    laps: [{"lap": n, "t_start": s, "t_end": s}] (the server's _detect_laps shape).
    Returns [{"lap", "wkg", "tqkg"}] (None where a lap has no samples).
    """
    out = []
    for lp in laps:
        w, q = _maxima(series, float(lp["t_start"]), float(lp["t_end"]))
        out.append({"lap": lp["lap"], "wkg": w, "tqkg": q})
    return out


def session_maxima(series: dict, laps: Optional[list]) -> tuple:
    """Session peak per kg over the given lap windows (whole session if none)."""
    if not laps:
        if len(series["t"]) == 0:
            return None, None
        return float(series["wkg"].max()), float(series["tqkg"].max())
    ws = [_maxima(series, float(lp["t_start"]), float(lp["t_end"])) for lp in laps]
    ws = [w for w in ws if w[0] is not None]
    if not ws:
        return None, None
    return max(w[0] for w in ws), max(w[1] for w in ws)


def round_or_none(x, nd: int = 0):
    if x is None or not math.isfinite(x):
        return None
    return round(float(x), nd)


def scale(wkg, tqkg, weight_lb: float):
    """(est WHP, est WTQ) for one per-kg peak pair and a car weight in lb."""
    kg = float(weight_lb) * LB_KG
    return (None if wkg is None else wkg * kg / HP_W,
            None if tqkg is None else tqkg * kg)

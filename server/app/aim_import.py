"""AiM (Race Studio) import — .xrk binary and AiM .csv -> racecar session NDJSON.

An AiM logger can hand you the same run in two containers:

    .xrk   Race Studio 3 binary; read here with `libxrk` (chunked, per-channel
           timecodes, laps table, metadata dict).
    .csv   AiM CSV export: a "Format","AiM CSV File" preamble of `"Key","Value"`
           rows, then a channel-name row, a UNITS row, and fixed-rate data.

Both are converted to exactly the NDJSON the dash uploads, so an imported file
becomes an ordinary session: laps, map, replay, AI coaching all work unchanged.

UNITS ARE THE WHOLE TRICK — the two containers disagree:

    .xrk   is METRIC    altitude m, speed m/s, WATER_TEMP C, OIL_PRESSURE bar
    .csv   is IMPERIAL  altitude ft, speed mph, WATER TEMP F, OIL PRESSURE psi

The server schema is neither; it is the dash's own mix (speed_mph, alt_m,
coolant_f, oil_psi). So every mapping below carries its own converter, the XRK
side reads units from the Arrow field metadata, and the CSV side is driven by
the declared units row instead of hardcoded assumptions. Cross-check: the same
run reads 515.25 ft (CSV) and 157.04 m (XRK) of altitude, and 190.29 F (CSV)
against 87.94 C (XRK) of water temperature.

The validator that guards normal dash uploads (validate_ndjson_body) is run
against this output by the caller, so an import can never bypass ingest rules.
"""

from __future__ import annotations

import csv
import datetime as _dt
import json
import math
import os
import pathlib
from dataclasses import dataclass, field

# --- container signatures ---------------------------------------------------
XRK_MAGIC = b"<hCNF"
CSV_MAGIC = b'"Format","AiM CSV File"'

# --- unit conversions -------------------------------------------------------
MPS_TO_MPH = 2.2369362920544
FT_TO_M = 0.3048
BAR_TO_PSI = 14.503773773

# Default output rate. The XRK merged table lands around 91 Hz (the union of
# every channel's own timecodes); 20 Hz matches what the AiM CSV exports and
# keeps a 25-minute run near 30 k samples / ~4 MB instead of ~140 k / ~20 MB.
DEFAULT_HZ = 20.0


def c_to_f(c: float) -> float:
    return c * 9.0 / 5.0 + 32.0


def _identity(v):
    return v


def _deg_to_deg(v):
    return v


# ---------------------------------------------------------------------------
# Channel maps
# ---------------------------------------------------------------------------
# field units are declared per source so a units-row change cannot silently
# corrupt the numbers.
#
#   (ndjson_key, converver, expected_source_unit)
#
# GPS_InlineAcc is longitudinal and GPS_LateralAcc is lateral, so they land on
# ax/ay with the same x-forward/y-lateral convention the firmware's IMU line
# uses. These are GPS-derived, not a real IMU — there is no IMU channel in
# either container — but they are the correct physical quantities.
XRK_CHANNELS = {
    "GPS Latitude":           ("lat",         _identity,  "deg"),
    "GPS Longitude":          ("lon",         _identity,  "deg"),
    "GPS Altitude":           ("alt_m",       _identity,  "m"),
    "GPS Speed":              ("speed_mph",   lambda v: v * MPS_TO_MPH, "m/s"),
    "GPS_Satellites":         ("sats",        _identity,  ""),
    "GPS_Fix":                ("fix",         _identity,  ""),
    "GPS_InlineAcc":          ("ax",          _identity,  "g"),
    "GPS_LateralAcc":         ("ay",          _identity,  "g"),
    "GPS_Yaw_Rate":           ("gz",          _identity,  "deg/s"),
    "RPM":                    ("rpm",         _identity,  "rpm"),
    "OIL_PRESSURE":           ("oil_psi",     lambda v: v * BAR_TO_PSI, "bar"),
    "WATER_TEMP":             ("coolant_f",   c_to_f,     "C"),
    "TPS":                    ("tps_pct",     _identity,  "%"),
    "External Voltage":       ("batt_v",      _identity,  "V"),
    "AFR":                    ("afr_can",     _identity,  "A/F"),
}

CSV_CHANNELS = {
    "Time":                   ("t",           _identity,  "s"),
    "GPS Latitude":           ("lat",         _identity,  "deg"),
    "GPS Longitude":          ("lon",         _identity,  "deg"),
    "GPS Altitude":           ("alt_m",       lambda v: v * FT_TO_M, "ft"),
    "GPS Speed":              ("speed_mph",   _identity,  "mph"),
    "GPS Nsat":               ("sats",        _identity,  " "),
    "GPS Heading":            ("heading_deg", _identity,  "deg"),
    "GPS LonAcc":             ("ax",          _identity,  "g"),
    "GPS LatAcc":             ("ay",          _identity,  "g"),
    "GPS Gyro":               ("gz",          _identity,  "deg/s"),
    "RPM":                    ("rpm",         _identity,  "rpm"),
    "OIL PRESSURE":           ("oil_psi",     _identity,  "psi"),
    "WATER TEMP":             ("coolant_f",   _identity,  "°F"),
    "TPS":                    ("tps_pct",     _identity,  "%"),
    "External Voltage":       ("batt_v",      _identity,  "V"),
    "AFR":                    ("afr_can",     _identity,  "A/F"),
}

# Accepted unit spellings per expected unit. The CSV writes "°F" but that byte
# sequence has been seen mangled in older exports, so accept the variants.
_UNIT_ALIASES = {
    "°F": {"°F", "F", "degF", "\u00b0F"},
    # AiM writes 'lambda' for AFR channels in .xrk. See _afr_scale: the label is
    # not trustworthy, the samples are what matter.
    "A/F": {"A/F", "AFR", "afr", "lambda"},
    " ": {"", " ", None},
    "deg/s": {"deg/s", "deg\u00b0/s"},
}

AFR_LAMBDA_TO_AFR = 14.7


def _afr_scale(values, declared_unit: str | None) -> float:
    """Decide whether an AFR channel holds lambda or an A/F ratio.

    The .xrk in the reference set declares units='lambda' for AFR but delivers
    14.245 - an A/F ratio - at the same timestamp where the matching .csv reads
    14.245 from a column it labels 'A/F'. So the declared unit cannot be
    trusted here and the data has to decide: a median at or below 3 is a real
    lambda trace (gasoline stoich 14.7), anything higher is already A/F.
    """
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return 1.0
    median = vals[len(vals) // 2]
    return AFR_LAMBDA_TO_AFR if median <= 3.0 else 1.0


def _unit_matches(expected: str, declared: str | None) -> bool:
    if declared is not None:
        declared = declared.strip()
    if expected in _UNIT_ALIASES:
        return declared in _UNIT_ALIASES[expected]
    return (declared or "").strip() == expected


@dataclass
class AimImport:
    ndjson: bytes
    samples: int
    laps: int
    track: str
    session_id: int
    source: str                      # "xrk" | "csv"
    metadata: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def sniff_format(head: bytes) -> str | None:
    """Return 'xrk' / 'csv' from the first bytes of a file, else None."""
    h = head.lstrip(b"\xef\xbb\xbf")
    if h.startswith(XRK_MAGIC):
        return "xrk"
    if h.startswith(CSV_MAGIC):
        return "csv"
    # Tolerate stray leading blank lines / BOM in hand-edited exports.
    stripped = h.lstrip(b" \t\r\n")
    if stripped.startswith(CSV_MAGIC) or stripped.startswith(b'"Format"'):
        return "csv"
    return None


def _num(v):
    """Coerce to a finite float, else None. libxrk yields NaN for gaps."""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(f):
        return None
    return f


def _r(v: float, nd: int) -> float:
    r = round(v, nd)
    return 0.0 if r == 0 else r          # collapse -0.0, which is ugly in JSON


def _lap_lookup(laps):
    """laps: list of (start_ms, end_ms). Returns fn(t_ms) -> int|None."""
    def find(t_ms: int):
        for i, (a, b) in enumerate(laps):
            if a <= t_ms < b:
                return i
        if laps and t_ms >= laps[-1][1]:
            return len(laps) - 1        # clamp the tail
        return None
    return find


def _safe_track(name: str | None, fallback: str = "UNKNOWN") -> str:
    if not name:
        return fallback
    cleaned = "".join(c if (c.isalnum() or c in "._-") else "_" for c in name.strip())
    cleaned = cleaned.strip("_")
    return cleaned[:48] or fallback


def _epoch_from(date_s: str | None, time_s: str | None, fallback: int) -> int:
    """Best-effort unix epoch from AiM's two date/time metadata spellings.

    The CSV says ("Friday, October 9, 2026", "3:24 PM"); the XRK says
    ("10/09/2026", "15:24:04"). Anything unparseable falls back to the file
    mtime so the session still sorts sensibly.
    """
    if not (date_s and time_s):
        return fallback
    stamp = f"{date_s.strip()} {time_s.strip()}"
    for dfmt in ("%m/%d/%Y", "%Y-%m-%d", "%A, %B %d, %Y", "%B %d, %Y"):
        for tfmt in ("%H:%M:%S", "%I:%M %p", "%H:%M"):
            try:
                dt = _dt.datetime.strptime(stamp, f"{dfmt} {tfmt}")
            except ValueError:
                continue
            # Logger local time; treated as UTC so the value is deterministic
            # instead of depending on the server's TZ.
            return int(dt.replace(tzinfo=_dt.timezone.utc).timestamp())
    return fallback


# ---------------------------------------------------------------------------
# CSV
# ---------------------------------------------------------------------------
def _csv_preamble(rows):
    """Pull the leading `"Key","Value"...` rows into a dict, keeping duplicates."""
    meta: dict = {}
    for r in rows:
        if not r or len(r) < 2:
            break
        key = (r[0] or "").strip()
        if not key:
            break
        # The preamble ALSO contains a `"Time","3:24 PM"` row. Only a wide
        # 'Time' row is the actual channel-name header, so test the width —
        # breaking on the metadata row would skip Sample Rate, Duration and,
        # critically, Beacon Markers (which is where lap boundaries live).
        if key == "Time" and len(r) > 5:
            break
        vals = [c for c in r[1:]]
        meta[key] = vals[0] if len(vals) == 1 else vals
    return meta


def csv_to_ndjson(path, fallback_epoch: int | None = None) -> AimImport:
    fallback_epoch = int(fallback_epoch or time_now())
    with open(path, newline="", encoding="utf-8-sig", errors="replace") as f:
        rows = list(csv.reader(f))

    meta = _csv_preamble(rows)

    # Locate the channel-name row: first row whose first cell is "Time" and
    # which actually carries multiple channels. The preamble also has a "Time"
    # row ("Time","3:24 PM"), which is why the width test is required.
    hdr_i = None
    for i, r in enumerate(rows):
        if r and (r[0] or "").strip() == "Time" and len(r) > 5:
            hdr_i = i
            break
    if hdr_i is None:
        raise ValueError("not an AiM CSV export: no channel-name row found")

    names = [(c or "").strip() for c in rows[hdr_i]]
    units = [(c or "").strip() for c in rows[hdr_i + 1]]

    # Resolve each column we care about to (index, ndjson_key, converter).
    wanted = []
    for idx, nm in enumerate(names):
        spec = CSV_CHANNELS.get(nm)
        if not spec:
            continue
        key, conv, expected_unit = spec
        declared = units[idx] if idx < len(units) else None
        if not _unit_matches(expected_unit, declared):
            raise ValueError(
                f"CSV unit mismatch for {nm!r}: expected {expected_unit!r}, "
                f"file declares {declared!r}")
        wanted.append((idx, key, conv))

    have = {k for _, k, _ in wanted}
    for req in ("lat", "lon"):
        if req not in have:
            raise ValueError(f"AiM CSV is missing the {req} channel")

    # AFR needs a column-level decision (lambda vs A/F), so resolve it once
    # from a sample of the column rather than guessing per row.
    if "afr_can" in have:
        afr_idx = next(i for i, k, _ in wanted if k == "afr_can")
        probe = [_num(r[afr_idx]) for r in rows[hdr_i + 2:hdr_i + 602]
                 if len(r) > afr_idx]
        scale = _afr_scale(probe, None)
        if scale != 1.0:
            wanted = [(i, k, (lambda v, s=scale: v * s) if k == "afr_can" else c)
                      for i, k, c in wanted]

    # Lap boundaries from the beacon markers (seconds). The markers ARE the lap
    # ends, so lap 0 runs 0 -> first marker, exactly as the XRK laps table has it.
    lap_bounds = []
    bm = meta.get("Beacon Markers")
    if isinstance(bm, list):
        try:
            lap_bounds = [float(x) for x in bm]
        except (TypeError, ValueError):
            lap_bounds = []

    lap_fn = None
    if lap_bounds:
        pairs = []
        prev = 0.0
        for m in lap_bounds:
            pairs.append((int(prev * 1000), int(m * 1000)))
            prev = m
        lap_fn = _lap_lookup(pairs)

    track = _safe_track(meta.get("Session") or meta.get("Venue"))
    session_id = _epoch_from(meta.get("Date"), meta.get("Time"), fallback_epoch)

    out = bytearray()
    samples = 0
    laps_seen = set()
    t_lat = t_lon = None

    for r in rows[hdr_i + 2:]:
        if not r or len(r) <= max(i for i, _, _ in wanted):
            continue
        rec = {}
        for idx, key, conv in wanted:
            v = _num(r[idx])
            if v is None:
                continue
            try:
                rec[key] = conv(v)          # ft->m, degF, psi, ... live here
            except Exception:
                continue
        if "lat" not in rec or "lon" not in rec:
            continue
        t = rec.pop("t", None)
        if t is None:
            continue
        rec["lat"] = _r(rec["lat"], 7)
        rec["lon"] = _r(rec["lon"], 7)
        rec["t"] = _r(t, 4)
        for k in list(rec):
            if k not in ("t", "lat", "lon"):
                rec[k] = _r(rec[k], 4)
        if lap_fn is not None:
            lp = lap_fn(int(t * 1000))
            if lp is not None:
                rec["lap"] = lp
                laps_seen.add(lp)
        out += json.dumps(rec, separators=(",", ":")).encode() + b"\n"
        samples += 1

    return AimImport(bytes(out), samples, len(laps_seen), track, session_id, "csv",
                     {"meta": {k: v for k, v in meta.items() if k != "Segment Times"}})


def time_now() -> int:
    import time
    return int(time.time())


# ---------------------------------------------------------------------------
# XRK
# ---------------------------------------------------------------------------
def xrk_to_ndjson(path, hz: float = DEFAULT_HZ, fallback_epoch: int | None = None) -> AimImport:
    import pyarrow as pa
    import libxrk

    fallback_epoch = int(fallback_epoch or time_now())
    log = libxrk.aim_xrk(str(path))

    # Laps first — they also give us the session length for the resample grid.
    laps: list[tuple[int, int]] = []
    lap_nums = None
    try:
        lt = log.laps
        for i in range(lt.num_rows):
            laps.append((int(lt.column("start_time")[i].as_py()),
                         int(lt.column("end_time")[i].as_py())))
    except Exception:
        laps = []

    # Union of every channel's timecodes is the honest span of the recording.
    end_ms = 0
    for name, tbl in log.channels.items():
        try:
            col = tbl.column("timecodes")
            if col.length:
                end_ms = max(end_ms, int(col[col.length - 1].as_py()))
        except Exception:
            continue
    if laps:
        end_ms = max(end_ms, laps[-1][1])
    if end_ms <= 0:
        raise ValueError("XRK contains no channel data")

    step_ms = max(1, int(round(1000.0 / float(hz))))
    grid = pa.array(list(range(0, end_ms + 1, step_ms)), type=pa.int64())
    resampled = log.resample_to_timecodes(grid)
    tbl = resampled.get_channels_as_table()

    # Map the channels we want, reading the declared unit out of the Arrow
    # field metadata so a metric/imperial change cannot corrupt the numbers.
    wanted = []
    for nm, (key, conv, expected_unit) in XRK_CHANNELS.items():
        if nm not in tbl.schema.names:
            continue
        fld = tbl.schema.field(nm)
        declared = None
        if fld.metadata and b"units" in fld.metadata:
            declared = fld.metadata[b"units"].decode("utf-8", "replace").strip()
        if not _unit_matches(expected_unit, declared):
            raise ValueError(
                f"XRK unit mismatch for {nm!r}: expected {expected_unit!r}, "
                f"file declares {declared!r}")
        wanted.append((nm, key, conv))

    have = {k for _, k, _ in wanted}
    for req in ("lat", "lon"):
        if req not in have:
            raise ValueError(f"XRK is missing the {req} channel")

    tc_col = tbl.column("timecodes")
    cols = {nm: tbl.column(nm) for nm, _, _ in wanted}

    # AFR needs a column-level decision (lambda vs A/F) - see _afr_scale.
    if "afr_can" in have:
        afr_nm = next(nm for nm, k, _ in wanted if k == "afr_can")
        scale = _afr_scale(cols[afr_nm].to_pylist(), None)
        if scale != 1.0:
            wanted = [(nm, k, (lambda v, s=scale: v * s) if k == "afr_can" else c)
                      for nm, k, c in wanted]
            cols = {nm: tbl.column(nm) for nm, _, _ in wanted}

    meta = dict(log.metadata or {})
    track = _safe_track(meta.get("Venue") or meta.get("Session"))
    session_id = _epoch_from(meta.get("Log Date"), meta.get("Log Time"), fallback_epoch)
    lap_fn = _lap_lookup(laps) if laps else None

    out = bytearray()
    samples = 0
    laps_seen = set()
    n = tbl.num_rows
    for i in range(n):
        rec = {}
        for nm, key, conv in wanted:
            v = _num(cols[nm][i].as_py())
            if v is None:
                continue
            try:
                rec[key] = conv(v)
            except Exception:
                continue
        if "lat" not in rec or "lon" not in rec:
            continue
        t_ms = int(tc_col[i].as_py())
        rec["lat"] = _r(rec["lat"], 7)
        rec["lon"] = _r(rec["lon"], 7)
        rec["t"] = _r(t_ms / 1000.0, 4)
        for k in list(rec):
            if k not in ("t", "lat", "lon"):
                rec[k] = _r(rec[k], 4)
        if lap_fn is not None:
            lp = lap_fn(t_ms)
            if lp is not None:
                rec["lap"] = lp
                laps_seen.add(lp)
        out += json.dumps(rec, separators=(",", ":")).encode() + b"\n"
        samples += 1

    return AimImport(bytes(out), samples, len(laps_seen), track, session_id, "xrk",
                     {"meta": meta})


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------
def import_aim(path, filename: str | None = None, hz: float = DEFAULT_HZ) -> AimImport:
    """Convert an AiM .xrk or AiM .csv into racecar session NDJSON.

    Format is decided by container signature first and extension second, so a
    .xrk that was renamed still imports and a mislabelled file is rejected
    rather than silently parsed as the wrong thing.
    """
    p = pathlib.Path(path)
    if not p.is_file():
        raise FileNotFoundError(str(p))
    try:
        fallback_epoch = int(os.path.getmtime(p))
    except OSError:
        fallback_epoch = time_now()

    with open(p, "rb") as f:
        head = f.read(512)
    fmt = sniff_format(head)
    if fmt is None:
        name = (filename or p.name).lower()
        if name.endswith(".xrk"):
            fmt = "xrk"
        elif name.endswith(".csv"):
            fmt = "csv"

    if fmt == "xrk":
        return xrk_to_ndjson(p, hz=hz, fallback_epoch=fallback_epoch)
    if fmt == "csv":
        return csv_to_ndjson(p, fallback_epoch=fallback_epoch)
    raise ValueError("not an AiM file: expected an AiM .xrk or an 'AiM CSV File' export")


def sniff_filename(name: str) -> str | None:
    n = (name or "").lower()
    if n.endswith(".xrk"):
        return "xrk"
    if n.endswith(".csv"):
        return "csv"
    return None

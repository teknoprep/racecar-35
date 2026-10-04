"""Track geometry pre-render — turn "a line we drove" into a real track.

The 3D drive view (/track3d) can only draw what it knows: a constant-width
ribbon around the driven line. This module bakes, per TRACK (on demand, cached),
what the world actually knows about that track, so the render can show its real
shape, its real width and its real surroundings:

  centreline   from OUR sessions (the line the car drove) or from OpenStreetMap
               (`highway=raceway` ways, which also carry a `width` tag on some
               circuits) — whichever is available
  WIDTH        measured from SATELLITE IMAGERY, not guessed: perpendicular
               brightness/colour profiles along the centreline find the paved
               corridor (asphalt + painted kerbs) and report the true left/right
               distance to its edge, station by station
  elevation    AWS's keyless "terrarium" DEM (and, when the session carries
               `alt_m`, that wins — it is what the car actually saw)
  texture      the satellite mosaic itself, baked to one image so the 3D view
               can drape the REAL asphalt, kerbs, run-off and grass on the
               ground with no tile fetching at view time

Everything is keyless (OSM Overpass, Esri World Imagery, AWS terrain) and cached
under RACECAR_DATA_DIR so a track is prepared once and then just read.

CLI:
    python3 -m app.trackprep --list-tracks
    python3 -m app.trackprep --osm "Shenandoah Circuit" --track "Summit Point Shenandoah"
    python3 -m app.trackprep --session /data/sessions/u/1714_Summit.ndjson --track "..."
    python3 -m app.trackprep --line line.json --track "My Track"     # [[lat,lon],...]
"""
from __future__ import annotations

import io
import json
import math
import os
import pathlib
import re
import time
import urllib.parse
import urllib.request
from typing import Iterable, Optional

try:                                  # Pillow/numpy are the only real deps
    import numpy as np
    from PIL import Image
except Exception:                     # pragma: no cover - reported by the caller
    np = None
    Image = None

UA = "racecar-35-trackprep/0.2 (+https://racecar.api.blueuc.com)"
ESRI = ("https://server.arcgisonline.com/ArcGIS/rest/services/"
        "World_Imagery/MapServer/tile/{z}/{y}/{x}")
TERRARIUM = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.osm.ch/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
M_PER_DEG_LAT = 111320.0
TILE = 256


def _require_deps() -> None:
    if np is None or Image is None:
        raise RuntimeError("trackprep needs Pillow and numpy (see app/requirements.txt)")


def slugify(track: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", (track or "").lower()).strip("-")
    return s or "track"


# ---------------------------------------------------------------------------
# web-mercator helpers
# ---------------------------------------------------------------------------
def lon_to_x(lon: float, z: int) -> float:
    return (lon + 180.0) / 360.0 * (TILE << z)


def lat_to_y(lat: float, z: int) -> float:
    r = math.radians(max(-85.05112878, min(85.05112878, lat)))
    return (1.0 - math.asinh(math.tan(r)) / math.pi) / 2.0 * (TILE << z)


def x_to_lon(x: float, z: int) -> float:
    return x / (TILE << z) * 360.0 - 180.0


def y_to_lat(y: float, z: int) -> float:
    n = math.pi - 2.0 * math.pi * y / (TILE << z)
    return math.degrees(math.atan(math.sinh(n)))


def metres_per_px(lat: float, z: int) -> float:
    return 156543.03392804097 * math.cos(math.radians(lat)) / (1 << z)


def _get(url: str, timeout: float = 45.0, data: Optional[bytes] = None) -> bytes:
    req = urllib.request.Request(url, data=data, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _cache_get(path: pathlib.Path) -> Optional[bytes]:
    try:
        if path.exists() and path.stat().st_size > 0:
            return path.read_bytes()
    except OSError:
        return None
    return None


def _cache_put(path: pathlib.Path, blob: bytes, min_bytes: int = 64) -> None:
    if len(blob) < min_bytes:            # placeholder / error tile
        return
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(blob)
        tmp.replace(path)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# imagery
# ---------------------------------------------------------------------------
def imagery_mosaic(bbox, z: int, cache_dir: pathlib.Path,
                   workers: int = 6, max_tiles: int = 400, log=print):
    """Stitch Esri World Imagery into ONE image covering `bbox`.

    bbox = (min_lat, min_lon, max_lat, max_lon). Returns (PIL.Image, bounds)
    where bounds = {"z", "x0", "y0", "lat0", "lon0", "step_lat", "step_lon"} —
    enough to map a lat/lon back to a pixel.
    """
    _require_deps()
    min_lat, min_lon, max_lat, max_lon = bbox
    x0 = int(math.floor(lon_to_x(min_lon, z) / TILE))
    x1 = int(math.floor(lon_to_x(max_lon, z) / TILE))
    y0 = int(math.floor(lat_to_y(max_lat, z) / TILE))
    y1 = int(math.floor(lat_to_y(min_lat, z) / TILE))
    nx, ny = x1 - x0 + 1, y1 - y0 + 1
    if nx * ny > max_tiles:
        raise RuntimeError(f"{nx*ny} tiles at z{z} is too many (max {max_tiles}); "
                           f"lower the zoom or shrink the track")
    log(f"[imagery] z{z} tiles {nx}x{ny} = {nx*ny}")
    canvas = Image.new("RGB", (nx * TILE, ny * TILE), (0, 0, 0))

    def one(args):
        i, j = args
        x, y = x0 + i, y0 + j
        p = cache_dir / "imagery" / str(z) / str(x) / f"{y}.jpg"
        blob = _cache_get(p)
        if blob is None:
            url = ESRI.format(z=z, y=y, x=x)
            try:
                blob = _get(url)
                _cache_put(p, blob)
            except Exception as e:
                log(f"[imagery] tile {z}/{x}/{y} failed: {e}")
                return None
        return (i, j, blob)

    jobs = [(i, j) for j in range(ny) for i in range(nx)]
    done = 0
    if workers > 1:
        from concurrent.futures import ThreadPoolExecutor
        it = ThreadPoolExecutor(max_workers=workers).map(one, jobs)
    else:
        it = map(one, jobs)
    for res in it:
        done += 1
        if res is None:
            continue
        i, j, blob = res
        try:
            t = Image.open(io.BytesIO(blob)).convert("RGB")
            canvas.paste(t, (i * TILE, j * TILE))
        except Exception as e:
            log(f"[imagery] tile decode failed: {e}")
        if done % 40 == 0:
            log(f"[imagery] {done}/{len(jobs)}")
    bounds = {
        "z": z, "x0": x0, "y0": y0,
        "lon0": x_to_lon(x0 * TILE, z), "lat0": y_to_lat(y0 * TILE, z),
        "lon1": x_to_lon((x1 + 1) * TILE, z), "lat1": y_to_lat((y1 + 1) * TILE, z),
    }
    return canvas, bounds


def mosaic_px(bounds: dict, lat: float, lon: float):
    """lat/lon -> (px, py) float pixel in the mosaic (y grows southward)."""
    z = bounds["z"]
    return (lon_to_x(lon, z) - bounds["x0"] * TILE,
            lat_to_y(lat, z) - bounds["y0"] * TILE)


def sample_px(img, x: float, y: float):
    """Nearest-pixel read as an (r,g,b) tuple, or None outside the image."""
    w, h = img.size
    xi, yi = int(x), int(y)
    if xi < 0 or yi < 0 or xi >= w or yi >= h:
        return None
    return img.getpixel((xi, yi))


# ---------------------------------------------------------------------------
# elevations (keyless AWS "terrarium" DEM)
# ---------------------------------------------------------------------------
def dem_elevations(points: Iterable, cache_dir: pathlib.Path, z: int = 14,
                   log=print):
    """Elevation (m) per (lat,lon) from terrarium tiles (z14 ~ 9.5 m/px)."""
    _require_deps()
    out, tiles = [], {}
    for lat, lon in points:
        tx, ty = int(lon_to_x(lon, z) // TILE), int(lat_to_y(lat, z) // TILE)
        key = (tx, ty)
        if key not in tiles:
            p = cache_dir / "dem" / str(z) / str(tx) / f"{ty}.png"
            blob = _cache_get(p)
            if blob is None:
                try:
                    blob = _get(TERRARIUM.format(z=z, x=tx, y=ty))
                    _cache_put(p, blob)
                except Exception as e:
                    log(f"[dem] tile {z}/{tx}/{ty} failed: {e}")
                    blob = None
            if blob is None:
                tiles[key] = None
            else:
                try:
                    arr = np.asarray(Image.open(io.BytesIO(blob)).convert("RGB"),
                                     dtype=np.float32)
                    tiles[key] = (arr[:, :, 0] * 256.0 + arr[:, :, 1] +
                                  arr[:, :, 2] / 256.0) - 32768.0
                except Exception:
                    tiles[key] = None
        arr = tiles[key]
        if arr is None:
            out.append(None)
            continue
        px = int(lon_to_x(lon, z) - tx * TILE)
        py = int(lat_to_y(lat, z) - ty * TILE)
        px = max(0, min(TILE - 1, px))
        py = max(0, min(TILE - 1, py))
        out.append(float(arr[py, px]))
    return out


def dem_grid(bbox, cols: int, rows: int, cache_dir: pathlib.Path, z: int = 14,
             log=print):
    """Coarse elevation grid (metres) over bbox, for the 3D ground mesh.

    Returns {"cols", "rows", "bounds": [south, west, north, east], "values":
    [row-major, south row first]}. Every sample is a bilinear read of the same
    keyless terrarium tiles the centreline profile uses, so it costs nothing
    extra once those are cached. A flat quad would be metres out on a circuit
    with real relief (Shenandoah climbs ~13 m)."""
    _require_deps()
    min_lat, min_lon, max_lat, max_lon = bbox
    pts = []
    for r in range(rows):
        lat = min_lat + (max_lat - min_lat) * (r / max(1, rows - 1))
        for c in range(cols):
            lon = min_lon + (max_lon - min_lon) * (c / max(1, cols - 1))
            pts.append((lat, lon))
    vals = dem_elevations(pts, cache_dir, z=z, log=log)
    clean = [v for v in vals if v is not None]
    fill = float(sorted(clean)[len(clean) // 2]) if clean else 0.0
    return {"cols": cols, "rows": rows,
            "bounds": [min_lat, min_lon, max_lat, max_lon],
            "values": [round(float(fill if v is None else v), 2) for v in vals]}


# ---------------------------------------------------------------------------
# OpenStreetMap raceways
# ---------------------------------------------------------------------------
OSM_CACHE_DIR = pathlib.Path(os.environ.get("RACECAR_OSM_CACHE")
                              or (pathlib.Path.home() / ".cache" / "racecar-osm"))
OSM_CACHE_DAYS = 45


def _osm_cache_path(bbox, key_extra: str = "") -> pathlib.Path:
    import hashlib
    k = ",".join("%.4f" % float(x) for x in bbox) + "|" + key_extra
    h = hashlib.sha1(k.encode()).hexdigest()[:20]
    return OSM_CACHE_DIR / (h + ".json")


def osm_raceways(bbox, timeout: float = 90.0, log=print, use_cache: bool = True):
    """`highway=raceway` ways inside bbox -> [{name, width_m, points[(lat,lon)]}].

    Cached on disk (45 days) and PREFERRED when the API is unreachable: Overpass
    is a shared community service that rate-limits, and a stale cache beats
    failing a track preparation. Also, mirrors are only tried with backoff so a
    busy day does not turn into a hammering loop.
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    q = (f'[out:json][timeout:{int(timeout)-5}];'
         f'way["highway"="raceway"]({min_lat},{min_lon},{max_lat},{max_lon});'
         f'out geom;')
    cp = _osm_cache_path(bbox, "raceway")
    fresh = None
    if use_cache:
        try:
            if cp.is_file():
                age_days = (time.time() - cp.stat().st_mtime) / 86400.0
                if age_days <= OSM_CACHE_DAYS:
                    fresh = json.loads(cp.read_text("utf-8"))
                    log("[osm] cache hit (%.1f days old)" % age_days)
        except Exception:
            fresh = None
    if fresh is not None:
        return fresh
    body = urllib.parse.urlencode({"data": q}).encode()
    errs = 0
    for ep in OVERPASS:
        for attempt in (1, 2):
            try:
                d = json.loads(_get(ep, timeout=timeout, data=body))
            except Exception as e:
                errs += 1
                log(f"[osm] {ep}: {type(e).__name__}")
                time.sleep(min(8, 2 * errs))
                continue
            out = []
            for e in d.get("elements", []):
                g = e.get("geometry") or []
                if len(g) < 2:
                    continue
                tags = e.get("tags", {})
                try:
                    w = float(tags.get("width", "").replace("m", "").strip())
                except Exception:
                    w = None
                out.append({"id": e.get("id"), "name": tags.get("name"),
                            "width_m": w, "surface": tags.get("surface"),
                            "sport": tags.get("sport"),
                            "points": [(p["lat"], p["lon"]) for p in g]})
            if out and use_cache:
                try:
                    cp.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cp.with_suffix(".tmp")
                    tmp.write_text(json.dumps(out), "utf-8")
                    tmp.replace(cp)
                except OSError:
                    pass
            return out
    if use_cache:
        try:                                   # last resort: any stale copy
            if cp.is_file():
                stale = json.loads(cp.read_text("utf-8"))
                log("[osm] all mirrors down; using a STALE cache copy")
                return stale
        except Exception:
            pass
    log("[osm] all mirrors failed (%d attempts)" % errs)
    return []


def way_length_m(points) -> float:
    """Approximate length of a lat/lon polyline (metres)."""
    total = 0.0
    for i in range(1, len(points)):
        (a0, o0), (a1, o1) = points[i - 1], points[i]
        x = (o1 - o0) * M_PER_DEG_LAT * math.cos(math.radians((a0 + a1) / 2))
        y = (a1 - a0) * M_PER_DEG_LAT
        total += math.hypot(x, y)
    return total


def osm_match_by_trace(points, ways, max_dist_m: float = 35.0, log=print):
    """Which OSM way IS the circuit we drove? Compare SHAPE, not names.

    Names are hopeless here: a session says "Thompson" while OSM calls that
    circuit's ways "Road Course" / "Thompson Speedway", and carries Pit Lane,
    Short Course and Drifting Course fragments in the same bbox. Measured on the
    real data: the name matcher picked a 560 m fragment, this picks the 1612 m
    circuit (mean distance ~1 m from the driven line).

    Returns (way, mean_dist_m); refuses anything further than max_dist_m.
    """
    if len(points) < 10 or not ways:
        return None, None
    step = max(1, len(points) // 400)
    probe = points[::step]
    lat0 = sum(p[0] for p in probe) / len(probe)
    kx = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    ox, oy = probe[0][1], probe[0][0]
    px = [((p[1] - ox) * kx, (p[0] - oy) * M_PER_DEG_LAT) for p in probe]
    best, best_d = None, 1e18
    for w in ways:
        wp = w["points"]
        if len(wp) < 4:
            continue
        wx = [((q[1] - ox) * kx, (q[0] - oy) * M_PER_DEG_LAT) for q in wp]
        cell = 20.0
        grid = {}
        for i, (x, y) in enumerate(wx):
            grid.setdefault((int(x // cell), int(y // cell)), []).append(i)
        total, n = 0.0, 0
        for (x, y) in px:
            gx, gy = int(x // cell), int(y // cell)
            near = 1e18
            for a in (-1, 0, 1):
                for b in (-1, 0, 1):
                    for i in grid.get((gx + a, gy + b), ()):
                        dx, dy = wx[i][0] - x, wx[i][1] - y
                        d = dx * dx + dy * dy
                        if d < near:
                            near = d
            total += math.sqrt(near) if near < 1e17 else 500.0
            n += 1
        mean_d = total / max(1, n)
        if mean_d < best_d:
            best, best_d = w, mean_d
    if best is None or best_d > max_dist_m:
        log("[osm] no way follows the driven line (closest %.0f m)" % (best_d if best else -1))
        return None, (None if best is None else best_d)
    log("[osm] trace match: way %s %r %.0f m, mean %.1f m from the driven line"
        % (best["id"], best.get("name"), way_length_m(best["points"]), best_d))
    return best, best_d


def _seg_dir_m(points, at_end: bool):
    """Unit direction (east, north) of a segment's first/last stretch."""
    if len(points) < 2:
        return (0.0, 0.0)
    a, b = (points[-2], points[-1]) if at_end else (points[0], points[1])
    x = (b[1] - a[1]) * M_PER_DEG_LAT * math.cos(math.radians((a[0] + b[0]) / 2))
    y = (b[0] - a[0]) * M_PER_DEG_LAT
    n = math.hypot(x, y) or 1.0
    return (x / n, y / n)


def _dist_m(a, b) -> float:
    x = (b[1] - a[1]) * M_PER_DEG_LAT * math.cos(math.radians((a[0] + b[0]) / 2))
    y = (b[0] - a[0]) * M_PER_DEG_LAT
    return math.hypot(x, y)


def stitch_circuit(ways, min_len_m: float = 800.0, close_tol_m: float = 60.0,
                   max_gap_m: float = 150.0, log=print):
    """Join OSM raceway ways into ONE closed circuit.

    A circuit is usually mapped as many short ways sharing endpoints: at Watkins
    Glen the longest single way is 924 m while the real 5.5 km lap is split
    across 'The Esses', 'The Boot', 'The Ninety', 'The Toe', ... (23 ways, with
    4 junction nodes carrying pit-lane / short-course branches and exactly two
    loose ends). So no single way is the track and no name matching is needed -
    the shape is the answer.

    Walk: start on a way, and at every step continue through the unused way that
    (a) touches the current end and (b) keeps the heading - the STRAIGHTEST
    continuation, which is what a racing circuit does and what a pit-lane branch
    does not. If nothing touches, bridge a gap (<= max_gap_m) to the nearest free
    endpoint, because real mappings have small gaps. Stop when the walk returns
    to where it started; keep the longest ring found.

    Returns {"points", "len", "ways", "closed", "gaps"} or None.
    """
    segs = []
    for w in ways:
        pts = [(float(a), float(b)) for a, b in (w.get("points") or [])]
        if len(pts) >= 2:
            segs.append({"id": w.get("id"), "name": w.get("name"),
                         "pts": pts, "len": way_length_m(pts)})
    if not segs:
        return None
    segs.sort(key=lambda s: -s["len"])

    def key(p):
        return (round(p[0], 6), round(p[1], 6))

    best = None
    starts = segs[:12]                       # the longest few are enough to seed
    for start in starts:
        for rev in (False, True):
            path = list(reversed(start["pts"])) if rev else list(start["pts"])
            used = {start["id"]}
            total = start["len"]
            gaps = 0
            closed = False
            for _ in range(len(segs) + 4):
                if total >= min_len_m and (
                        key(path[0]) == key(path[-1]) or
                        _dist_m(path[-1], path[0]) <= close_tol_m):
                    closed = True
                    break
                head = _seg_dir_m(path[-2:], True)      # heading at the current end
                end = key(path[-1])
                cands = []
                for s2 in segs:
                    if s2["id"] in used:
                        continue
                    if key(s2["pts"][0]) == end:
                        cands.append((s2, False))
                    elif key(s2["pts"][-1]) == end:
                        cands.append((s2, True))
                if cands:
                    # straightest continuation wins; a pit-lane branch turns hard
                    def score(c):
                        s2, flip = c
                        pts2 = list(reversed(s2["pts"])) if flip else s2["pts"]
                        d = _seg_dir_m(pts2, False)
                        return head[0] * d[0] + head[1] * d[1]
                    s2, flip = max(cands, key=score)
                else:
                    # gap: jump to the nearest free endpoint within max_gap_m
                    near = None
                    for s2 in segs:
                        if s2["id"] in used:
                            continue
                        for at_end in (False, True):
                            p = s2["pts"][-1] if at_end else s2["pts"][0]
                            d = _dist_m(path[-1], p)
                            if d <= max_gap_m and (near is None or d < near[0]):
                                near = (d, s2, at_end)
                    if near is None:
                        break
                    gaps += 1
                    s2, flip = near[1], near[2]
                pts2 = list(reversed(s2["pts"])) if flip else s2["pts"]
                used.add(s2["id"])
                path.extend(pts2[1:] if _dist_m(path[-1], pts2[0]) < 1 else pts2)
                total += s2["len"]
            if closed and total >= min_len_m:
                # rank by FEWEST gap jumps first, then longest: the real circuit
                # usually stitches with no jumps, while a dead end (pit lane,
                # access road) can only be closed by jumping back - so it loses
                # even though the detour makes it longer.
                cand = {"points": path, "len": total, "ways": sorted(used),
                        "closed": True, "gaps": gaps}
                if (best is None or gaps < best["gaps"] or
                        (gaps == best["gaps"] and total > best["len"])):
                    best = cand
    if best is None:
        log("[osm] no closed raceway ring found (%d ways considered)" % len(segs))
        return None
    log("[osm] stitched a %.0f m circuit from %d ways (%d nodes, %d gap jumps)"
        % (best["len"], len(best["ways"]), len(best["points"]), best["gaps"]))
    return best


def osm_circuit(bbox, log=print, min_len_m: float = 800.0):
    """The best STITCHED circuit in a bbox (motor-sport ways preferred)."""
    ways = osm_raceways(bbox, log=log)
    if not ways:
        return None
    motor = [w for w in ways if (w.get("sport") or "").lower() == "motor"]
    for pool in (motor, ways):
        if pool:
            c = stitch_circuit(pool, min_len_m=min_len_m, log=log)
            if c:
                return c
    return None


def osm_best(track: str, bbox, min_len_m: float = 400.0, log=print):
    """Name-based pick, for when there is no driven line to match against.

    A name match alone is not enough (a pit-lane fragment can share the name), so
    candidates are weighted by length and anything under min_len_m is only used
    if nothing longer exists.
    """
    ways = osm_raceways(bbox, log=log)
    if not ways:
        return None
    long_enough = [w for w in ways if way_length_m(w["points"]) >= min_len_m]
    pool = long_enough or ways
    if not long_enough:
        log("[osm] no way >= %.0f m found; using the longest fragment" % min_len_m)
    want = set(re.findall(r"[a-z0-9]+", (track or "").lower()))
    best, score = None, -1.0
    for w in pool:
        have = set(re.findall(r"[a-z0-9]+", (w.get("name") or "").lower()))
        overlap = len(want & have) / max(1, len(want))
        s = overlap + 0.6 * min(1.0, way_length_m(w["points"]) / 1500.0)
        if s > score:
            best, score = w, s
    if best is not None:
        best["length_m"] = way_length_m(best["points"])
        log("[osm] picked way %s %r %.0f m (%d pts)"
            % (best["id"], best.get("name"), best["length_m"], len(best["points"])))
    return best


# ---------------------------------------------------------------------------
# geometry
# ---------------------------------------------------------------------------
def resample(points, step_m: float = 2.0, smooth_win: int = 5):
    """Evenly-spaced, smoothed centreline with a tangent per station."""
    pts = [(float(a), float(b)) for a, b in points]
    if len(pts) < 3:
        raise ValueError("need at least 3 centreline points")
    lat0 = sum(p[0] for p in pts) / len(pts)
    lon0 = sum(p[1] for p in pts) / len(pts)
    kx = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    xy = np.array([[(p[1] - lon0) * kx, (p[0] - lat0) * M_PER_DEG_LAT] for p in pts])

    def smooth(a, r):
        r = max(0, int(r))
        if not r:
            return a
        k = np.ones(2 * r + 1) / (2 * r + 1)
        pad = np.pad(a, ((r, r), (0, 0)), mode="edge")
        return np.stack([np.convolve(pad[:, 0], k, "valid"),
                         np.convolve(pad[:, 1], k, "valid")], axis=1)
    xy = smooth(xy, (smooth_win - 1) // 2)
    seg = np.linalg.norm(np.diff(xy, axis=0), axis=1)
    cum = np.concatenate([[0.0], np.cumsum(seg)])
    total = float(cum[-1])
    n = max(3, int(total / step_m) + 1)
    ss = np.linspace(0.0, total, n)
    xs = np.interp(ss, cum, xy[:, 0])
    ys = np.interp(ss, cum, xy[:, 1])
    out = {
        "lat": lat0 + ys / M_PER_DEG_LAT,
        "lon": lon0 + xs / kx,
        "s": ss,
        "total_m": total,
    }
    dx = np.gradient(xs)
    dy = np.gradient(ys)
    ang = np.arctan2(dx, dy)              # 0 = north, +ve = clockwise (east)
    out["tan"] = ang
    out["normal"] = np.stack([np.cos(ang), -np.sin(ang)], axis=1)   # (east,north) perp
    return out


# ---------------------------------------------------------------------------
# width from imagery — the part that makes the track REAL
# ---------------------------------------------------------------------------
def _paved(rgb, opts) -> bool:
    """Asphalt (or paint on it) rather than grass, trees or gravel run-off.

    Calibrated against real Esri imagery over Shenandoah, where the track reads
    as LIGHT neutral grey (140-190) and the surroundings are darker and greener
    (30-130, G-R 10-30). So the discriminators are vegetation GREEN-ness and
    saturation, not brightness — a "not green = asphalt" rule happily swallows a
    light gravel paddock.
    """
    r, g, b = rgb
    v = max(r, g, b)
    mn = min(r, g, b)
    sat = 0.0 if v == 0 else (v - mn) / float(v)
    if v < float(opts.get("dark_max") or 25):
        return False                     # deep shade / tree canopy
    # SATURATION is the discriminator that actually separates them. Measured on
    # real Esri imagery at Watkins Glen, the asphalt reads as a light green-tinted
    # grey (131,137,123 / 145,146,132 - G only 4-9 above R) while the grass is
    # 96,101,69 / 124,135,92. So a greenness test alone throws away half the
    # track; saturation is 0.04-0.20 on asphalt and 0.25-0.37 on grass.
    if sat > float(opts.get("sat_max") or 0.22):
        return False                     # grass / trees / tinted dirt
    if (g - max(r, b)) > float(opts.get("green_max") or 10):
        return False                     # bright, still unmistakably green
    return True                          # neutral, mid/dark or bleached asphalt


def measure_width(img, bounds, line, opts=None):
    """Per-station left/right half-widths (m) of the paved corridor.

    The classifier is SELF-CALIBRATING. A fixed colour rule cannot fit every
    circuit's imagery: at Watkins Glen the asphalt is a light green-tinted grey
    (131,137,123) while at Shenandoah it is a warm grey, and a single saturation
    threshold that works for one is wrong for the other (measured: 7.5 m on an
    11 m track). But the centreline is LABELLED asphalt - the car drove there -
    and 25 m out is labelled background (grass/gravel/trees). So: sample both,
    and classify each profile sample by which mean it is closer to, falling back
    to the fixed rules only when the two clusters are too similar to separate.

    Returns left[], right[], width[], ok[], confidence, and the classifier used.
    """
    _require_deps()
    opts = opts or {}
    reach = float(opts.get("reach_m") or 26.0)
    step = float(opts.get("profile_step_m") or 0.5)
    min_w = float(opts.get("min_width_m") or 4.0)
    max_w = float(opts.get("max_width_m") or 30.0)
    n = len(line["lat"])
    offs = np.arange(-reach, reach + 1e-9, step)
    coslat = math.cos(math.radians(float(np.mean(line["lat"]))))

    def rgb_at(i, o):
        la, lo = float(line["lat"][i]), float(line["lon"][i])
        nx, ny = float(line["normal"][i][0]), float(line["normal"][i][1])
        dlat = (o * ny) / M_PER_DEG_LAT
        dlon = (o * nx) / (M_PER_DEG_LAT * coslat)
        return sample_px(img, *mosaic_px(bounds, la + dlat, lo + dlon))

    # ---- learn the two clusters -----------------------------------------
    asph, back = [], []
    for i in range(0, n, max(1, n // 250)):
        for o in (-0.8, 0.0, 0.8):
            c = rgb_at(i, o)
            if c:
                asph.append(c)
        for o in (-reach, reach):
            c = rgb_at(i, o)
            if c:
                back.append(c)
    learned = None
    if len(asph) >= 20 and len(back) >= 20:
        ca = np.mean(np.array(asph, dtype=float), axis=0)
        cb = np.mean(np.array(back, dtype=float), axis=0)
        if float(np.linalg.norm(ca - cb)) >= float(opts.get("min_contrast") or 22.0):
            learned = (ca, cb)

    def paved(rgb):
        r, g, b = rgb
        if max(rgb) < 22:
            return False                       # deep shade / canopy
        if learned is not None:
            da = (r - learned[0][0]) ** 2 + (g - learned[0][1]) ** 2 + (b - learned[0][2]) ** 2
            db = (r - learned[1][0]) ** 2 + (g - learned[1][1]) ** 2 + (b - learned[1][2]) ** 2
            return da <= db
        return _paved(rgb, opts)

    left = np.zeros(n)
    right = np.zeros(n)
    ok = np.zeros(n, dtype=bool)
    for i in range(n):
        run = []
        for o in offs:
            rgb = rgb_at(i, o)
            run.append(True if rgb is None else paved(rgb))
        zero = int(np.argmin(np.abs(offs)))
        if not run[zero]:
            continue
        a = zero
        while a > 0 and run[a - 1]:
            a -= 1
        b = zero
        while b < len(run) - 1 and run[b + 1]:
            b += 1
        if a == 0 or b == len(run) - 1:
            continue      # run reaches the profile edge: no real edge either side
        lw = abs(float(offs[a]))
        rw = abs(float(offs[b]))
        if lw + rw < min_w or lw + rw > max_w:
            continue
        left[i], right[i], ok[i] = lw, rw, True
    conf = float(ok.mean()) if n else 0.0
    used = "learned" if learned is not None else "colour"
    if ok.sum() >= 3 and conf >= float(opts.get("min_confidence") or 0.25):
        det = left[ok] + right[ok]
        med = float(np.median(det))
        # Robust representative: median + MAD, dropping the strays. Measured
        # across real circuits, pixel width is only good to ~+-30% (a run of
        # shadow reads narrow, a gravel trap reads wide), so the scatter has to
        # be trimmed before it is averaged - otherwise one paddock station drags
        # a 10 m circuit to 19.5 m.
        mad = float(np.median(np.abs(det - med))) * 1.4826
        if mad > 0.2:
            keep = det[np.abs(det - med) <= 2.5 * mad]
            if len(keep) >= 3:
                med = float(np.median(keep))
        rep = med
        total = np.where(ok, left + right, rep)
        k2 = 9
        pad = np.pad(total, k2 // 2, mode="edge")
        total = np.array([np.median(pad[i:i + k2]) for i in range(len(total))])
        total = np.convolve(np.pad(total, 3, mode="edge"), np.ones(7) / 7, "valid")
        ratio = np.where(ok, left / np.maximum(1e-6, left + right), 0.5)
        ratio = np.clip(0.5 + (ratio - 0.5) * 0.7, 0.15, 0.85)
        return {"left": (total * ratio).tolist(),
                "right": (total * (1.0 - ratio)).tolist(),
                "width": total.tolist(), "ok": ok.tolist(), "confidence": conf,
                "median_width_m": round(rep, 2), "mode_width_m": round(rep, 2),
                "classifier": used,
                "asphalt_rgb": None if learned is None else [round(float(x), 1) for x in learned[0]],
                "background_rgb": None if learned is None else [round(float(x), 1) for x in learned[1]]}
    fallback = float(opts.get("width_fallback_m") or 12.0)
    return {"left": [fallback / 2] * n, "right": [fallback / 2] * n,
            "width": [fallback] * n, "ok": ok.tolist(), "confidence": conf,
            "median_width_m": fallback, "mode_width_m": fallback,
            "classifier": used, "asphalt_rgb": None, "background_rgb": None}


# ---------------------------------------------------------------------------
# the asset
# ---------------------------------------------------------------------------
def build_asset(track: str, line_points, data_dir: pathlib.Path, opts=None,
                log=print, cache_dir: Optional[pathlib.Path] = None) -> dict:
    """Bake one track: centreline + measured width + elevation + ground texture."""
    _require_deps()
    opts = dict(opts or {})
    z = int(opts.get("zoom") or 18)
    step = float(opts.get("step_m") or 2.0)
    margin_m = float(opts.get("margin_m") or 70.0)
    cache_dir = cache_dir or (pathlib.Path(data_dir) / "tilecache")
    out_dir = pathlib.Path(data_dir) / "tracks"
    out_dir.mkdir(parents=True, exist_ok=True)
    slug = slugify(track)

    line = resample(line_points, step)
    lats, lons = line["lat"], line["lon"]
    min_lat, max_lat = float(lats.min()), float(lats.max())
    min_lon, max_lon = float(lons.min()), float(lons.max())
    dlat = margin_m / M_PER_DEG_LAT
    dlon = margin_m / (M_PER_DEG_LAT * math.cos(math.radians((min_lat + max_lat) / 2)))
    bbox = (min_lat - dlat, min_lon - dlon, max_lat + dlat, max_lon + dlon)

    log(f"[{slug}] centreline {len(lats)} stations, {line['total_m']:.0f} m")
    img, bounds = imagery_mosaic(bbox, z, cache_dir, log=log)
    w = measure_width(img, bounds, line, opts)
    osm_w = opts.get("osm_width_m")
    imagery_w = float(w["median_width_m"])
    # A circuit's racing surface is 8-15 m; a pixel estimate outside that is the
    # measurement being wrong (shadow reads narrow, gravel reads wide), not an
    # unusual circuit. Clamp the value we DRAW and keep the raw number in the
    # asset, so nothing is hidden.
    lo_w = float(opts.get("plausible_min_m") or 8.0)
    hi_w = float(opts.get("plausible_max_m") or 15.0)
    clamped = None
    if imagery_w < lo_w:
        clamped, imagery_w = imagery_w, lo_w
    elif imagery_w > hi_w:
        clamped, imagery_w = imagery_w, hi_w
    if osm_w and 3 <= osm_w <= 30:
        # A surveyed tag beats anything inferred from pixels; the imagery
        # estimate is kept as a cross-check.
        width_profile = [float(osm_w)] * len(line["lat"])
        width_source = "osm-tag"
        agreement = (clamped is None and
                     abs(imagery_w - float(osm_w)) / float(osm_w) <= 0.4)
    else:
        width_profile = [float(x) for x in w["width"]]
        width_source = "imagery"
        agreement = None
    if clamped is not None:
        log(f"[{slug}] width: imagery measured {clamped:.1f} m, outside the "
            f"{lo_w:.0f}-{hi_w:.0f} m a circuit can be -> clamped to {imagery_w:.1f} m "
            f"(raw value kept in the asset)")
    log(f"[{slug}] width: imagery {imagery_w:.1f} m (conf {w['confidence']*100:.0f}%, "
        f"{w.get('classifier')} classifier, asphalt {w.get('asphalt_rgb')} vs bg {w.get('background_rgb')}), "
        f"osm tag {osm_w}, using {width_source}"
        + ("" if agreement is None else f", agreement={agreement}"))

    dz = int(opts.get("dem_zoom") or 14)
    elev = dem_elevations(zip(lats, lons), cache_dir, z=dz, log=log)
    if all(e is None for e in elev):
        elev = [0.0] * len(lats)
    grid = dem_grid(bbox, int(opts.get("dem_cols") or 40), int(opts.get("dem_rows") or 40),
                    cache_dir, z=dz, log=log)
    log(f"[{slug}] dem grid {grid['cols']}x{grid['rows']} "
        f"{min(grid['values']):.1f}-{max(grid['values']):.1f} m")

    # ground texture: the mosaic itself, cropped to the bbox and downscaled
    tex_w = int(opts.get("texture_px") or 2048)
    if img.width > tex_w:
        ratio = tex_w / img.width
        tex = img.resize((tex_w, max(1, int(img.height * ratio))), Image.LANCZOS)
    else:
        tex = img
    tex_name = f"{slug}.jpg"
    try:
        tex.save(out_dir / tex_name, "JPEG", quality=86, optimize=True)
    except OSError as e:
        log(f"[{slug}] texture save failed: {e}")
        tex_name = None

    asset = {
        "track": track, "slug": slug, "generated": int(time.time()),
        "prep_version": int(opts.get("prep_version") or 1),
        "source": {"line": opts.get("line_source") or "session",
                   "imagery": f"Esri World Imagery z{z}",
                   "dem": "AWS terrarium z14",
                   "osm_id": opts.get("osm_id")},
        "centre": [float(np.mean(lats)), float(np.mean(lons))],
        "length_m": round(float(line["total_m"]), 1),
        "bbox": [min_lat, min_lon, max_lat, max_lon],
        "line": [[float(a), float(b), None if e is None else round(float(e), 1)]
                 for a, b, e in zip(lats, lons, elev)],
        "width_m": [round(float(x), 2) for x in width_profile],
        "width_source": width_source,
        "width_imagery_m": round(imagery_w, 2),
        "width_imagery_raw_m": round(clamped if clamped is not None else imagery_w, 2),
        "width_clamped": clamped is not None,
        "width_osm_m": osm_w,
        "width_agreement": agreement,
        "width_confidence": round(w["confidence"], 3),
        "width_classifier": w.get("classifier"),
        "width_asphalt_rgb": w.get("asphalt_rgb"),
        "width_background_rgb": w.get("background_rgb"),
        "lateral_reference": "centreline from the driven line",
        "dem": grid,
        "step_m": step,
        "texture": None if not tex_name else {
            "file": tex_name,
            "bounds": {"south": bounds["lat1"], "west": bounds["lon0"],
                       "north": bounds["lat0"], "east": bounds["lon1"]},
            "px": [tex.width, tex.height],
            "attrib": "Imagery \u00a9 Esri, Maxar, Earthstar Geographics",
        },
    }
    (out_dir / f"{slug}.json").write_text(json.dumps(asset), "utf-8")
    log(f"[{slug}] asset written ({out_dir / (slug + '.json')})")
    return asset


def session_centreline(path: pathlib.Path, target: int = 6000):
    """Subsample a session's GPS trace into a centreline (lat,lon) list.

    Multi-lap sessions keep every lap; they overlap within centimetres, the
    smoothing in resample() copes, and extra passes only make the width
    measurement more robust."""
    rows = []
    with open(path, "rb") as f:
        for raw in f:
            raw = raw.strip()
            if not raw:
                continue
            try:
                o = json.loads(raw)
            except Exception:
                continue
            lat, lon = o.get("lat"), o.get("lon")
            if isinstance(lat, (int, float)) and isinstance(lon, (int, float)) \
                    and (lat or lon) and -90 <= lat <= 90 and -180 <= lon <= 180:
                rows.append((lat, lon))
    if len(rows) < 10:
        raise ValueError("session has no usable GPS fixes")
    step = max(1, len(rows) // target)
    return rows[::step]


def load_assets(data_dir: pathlib.Path) -> dict:
    """{slug: asset} for everything already prepared."""
    out = {}
    d = pathlib.Path(data_dir) / "tracks"
    if not d.is_dir():
        return out
    for f in sorted(d.glob("*.json")):
        try:
            out[f.stem] = json.loads(f.read_text())
        except Exception:
            continue
    return out


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="pre-render a track's real geometry")
    ap.add_argument("--data-dir", default=os.environ.get("RACECAR_DATA_DIR", "/data"))
    ap.add_argument("--track")
    ap.add_argument("--session")
    ap.add_argument("--osm", help="OSM raceway name to trace (needs --near)")
    ap.add_argument("--near", help="lat,lon hint used to search OSM, e.g. 39.24,-77.96")
    ap.add_argument("--osm-id", type=int, help="use this exact OSM way id")
    ap.add_argument("--line", help="json file of [[lat,lon],...] (a driven line)")
    ap.add_argument("--osm-from-trace", action="store_true",
                    help="borrow OSM's surveyed width, matching the way by shape")
    ap.add_argument("--zoom", type=int, default=18)
    ap.add_argument("--list-tracks", action="store_true")
    ap.add_argument("--list-osm", action="store_true")
    a = ap.parse_args(argv)
    data_dir = pathlib.Path(a.data_dir)

    if a.list_tracks:
        for slug, asset in load_assets(data_dir).items():
            print(f"{slug:34} {asset.get('track'):28} "
                  f"stations {len(asset.get('line') or []):5} "
                  f"width {np.median(asset.get('width_m') or [0]):5.1f} m "
                  f"conf {asset.get('width_confidence')}")
        return 0

    if not a.track:
        ap.error("--track is required (unless --list-tracks)")

    if a.list_osm:
        line = _line_from_any(a)
        bbox = _bbox_around(line, 120)
        for w in osm_raceways(bbox):
            print(f"{w['id']:>12} {str(w['name']):32} {len(w['points']):5} pts "
                  f"width={w['width_m']} surface={w['surface']}")
        return 0

    # Our own driven line is the best geometry we have: it is the line the car
    # actually takes. OSM is then used for what it knows BETTER — a surveyed
    # `width` tag — matched to the trace by SHAPE, never by name.
    points = None
    source = None
    osm_id = None
    osm_w = None
    if a.session or a.line:
        points, source, _, _ = _line_from_any(
            type("A", (), {"session": a.session, "line": a.line, "osm": None,
                           "osm_id": None, "near": None})(), want_source=True)
        if a.osm or a.osm_id or a.osm_from_trace:
            near = a.near
            if not near:
                near = "%.5f,%.5f" % (sum(p[0] for p in points) / len(points),
                                      sum(p[1] for p in points) / len(points))
            lat, lon = [float(x) for x in near.split(",")]
            ways = osm_raceways((lat - 0.06, lon - 0.08, lat + 0.06, lon + 0.08))
            way = None
            if a.osm_id:
                way = next((x for x in ways if x["id"] == a.osm_id), None)
            if way is None:
                way, _ = osm_match_by_trace(points, ways)
            if way is not None:
                osm_id, osm_w = way["id"], way.get("width_m")
                source = "%s+osm:%s" % (source, way["id"])
                if osm_w:
                    print("[osm] borrowed surveyed width %s m from way %s"
                          % (osm_w, way["id"]))
    else:
        if not (a.osm or a.osm_id):
            raise SystemExit("give one of --session / --line / --osm")
        points, source, osm_id, osm_w = _line_from_any(a, want_source=True)

    asset = build_asset(a.track, points, data_dir,
                        {"zoom": a.zoom, "line_source": source, "osm_id": osm_id,
                         "osm_width_m": osm_w})
    print(json.dumps({k: asset.get(k) for k in
                      ("track", "slug", "source", "width_source", "width_imagery_m",
                       "width_osm_m", "width_agreement", "length_m")}, indent=1))
    return 0


def _bbox_around(points, margin_m: float):
    lats = [p[0] for p in points]
    lons = [p[1] for p in points]
    dlat = margin_m / M_PER_DEG_LAT
    dlon = margin_m / (M_PER_DEG_LAT * math.cos(math.radians(sum(lats) / len(lats))))
    return (min(lats) - dlat, min(lons) - dlon, max(lats) + dlat, max(lons) + dlon)


def _line_from_any(a, want_source: bool = False):
    if a.session:
        pts = session_centreline(pathlib.Path(a.session))
        return (pts, "session", None, None) if want_source else pts
    if a.line:
        pts = [(float(x[0]), float(x[1])) for x in json.loads(pathlib.Path(a.line).read_text())]
        return (pts, "line-file", None, None) if want_source else pts
    if a.osm or a.osm_id:
        if not a.near:
            raise SystemExit("--osm needs --near lat,lon (the track's area)")
        lat, lon = [float(x) for x in a.near.split(",")]
        bbox = (lat - 0.06, lon - 0.08, lat + 0.06, lon + 0.08)
        ways = osm_raceways(bbox)
        w = None
        if a.osm_id:
            w = next((x for x in ways if x["id"] == a.osm_id), None)
        else:
            w = osm_best(a.osm, bbox)
        if not w:
            raise SystemExit(f"no raceway way matched {a.osm or a.osm_id!r} near {a.near}")
        return ((w["points"], f"osm:{w['id']}", w["id"], w.get("width_m"))
                if want_source else w["points"])
    raise SystemExit("give one of --session / --line / --osm")


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(main())

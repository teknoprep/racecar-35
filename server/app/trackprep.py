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

Everything is keyless (OSM Overpass, Esri World Imagery, AWS + USGS terrain) and cached
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
import tempfile
import threading
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
    import hashlib
    seen = {}
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
            seen[hashlib.sha1(blob).hexdigest()] = seen.get(hashlib.sha1(blob).hexdigest(), 0) + 1
        except Exception:
            pass
        try:
            t = Image.open(io.BytesIO(blob)).convert("RGB")
            canvas.paste(t, (i * TILE, j * TILE))
        except Exception as e:
            log(f"[imagery] tile decode failed: {e}")
        if done % 40 == 0:
            log(f"[imagery] {done}/{len(jobs)}")
    # A blocked or proxied tile source answers every URL with the SAME image,
    # which pastes into a repeating pattern that looks like nothing on earth and
    # silently poisons the width measurement. Refuse to bake that.
    if len(jobs) >= 8 and seen:
        dup = max(seen.values())
        if dup > max(4, int(len(jobs) * 0.25)):
            raise RuntimeError(
                "imagery returned %d identical tiles out of %d - the tile source "
                "is blocked or proxying (nothing usable to bake)" % (dup, len(jobs)))
    bounds = {
        "z": z, "x0": x0, "y0": y0,
        "lon0": x_to_lon(x0 * TILE, z), "lat0": y_to_lat(y0 * TILE, z),
        "lon1": x_to_lon((x1 + 1) * TILE, z), "lat1": y_to_lat((y1 + 1) * TILE, z),
    }
    return canvas, bounds


def mosaic_px(bounds: dict, lat: float, lon: float):
    """lat/lon -> (px, py) float pixel in the mosaic (y grows southward)."""
    z = bounds["z"]
    k = bounds.get("scale", 1.0)        # a downscaled copy of the mosaic (the baked JPEG)
    return ((lon_to_x(lon, z) - bounds["x0"] * TILE) * k,
            (lat_to_y(lat, z) - bounds["y0"] * TILE) * bounds.get("scale_y", k))


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
# high-resolution elevation: USGS 3DEP (US) -> AWS terrarium (anywhere)
# ---------------------------------------------------------------------------
DEM3DEP = ("https://elevation.nationalmap.gov/arcgis/rest/services/"
           "3DEPElevation/ImageServer/exportImage?")
DEM_MAX_TILE_PX = 2000
DEM_BAD_MAX_FRAC = 0.02           # more invalid cells than this = the source failed
DEM_MIN_OK_M, DEM_MAX_OK_M = -500.0, 9000.0


def _grid_dims(bbox, max_cells: int, min_cell_m: float):
    """(cols, rows, cell_m) for a NODE grid over bbox: sample (r,c) sits at
    lat = s + (n-s)*r/(rows-1), lon = w + (e-w)*c/(cols-1)."""
    s, w, n, e = [float(x) for x in bbox]
    h_m = max(1.0, (n - s) * M_PER_DEG_LAT)
    w_m = max(1.0, (e - w) * M_PER_DEG_LAT * math.cos(math.radians((s + n) / 2)))
    cell = max(float(min_cell_m), math.sqrt(h_m * w_m / float(max_cells)))
    cols = max(2, int(w_m / cell))
    rows = max(2, int(h_m / cell))
    return cols, rows, math.sqrt((w_m / (cols - 1)) * (h_m / (rows - 1)))


def _clean_dem(v, what: str):
    """Fill the few invalid cells (NaN, nodata ~ -3.4e38, absurd values) with the
    median; refuse the whole grid when more than DEM_BAD_MAX_FRAC are bad."""
    v = np.asarray(v, dtype=np.float32)
    bad = ~np.isfinite(v) | (v < DEM_MIN_OK_M) | (v > DEM_MAX_OK_M)
    frac = float(bad.mean()) if v.size else 1.0
    if frac >= DEM_BAD_MAX_FRAC or bad.all():
        raise RuntimeError("%s: %.1f%% of the cells are invalid" % (what, 100 * frac))
    if bad.any():
        v = v.copy()
        v[bad] = float(np.median(v[~bad]))
    return v


def _dem3dep(bbox, cols: int, rows: int, cache_dir: pathlib.Path, log=print,
             timeout: float = 90.0):
    """(rows, cols) float32, SOUTH row first, node-aligned (see _grid_dims)."""
    import hashlib
    s, w, n, e = [float(x) for x in bbox]
    dx = (e - w) / (cols - 1)
    dy = (n - s) / (rows - 1)
    out = np.empty((rows, cols), dtype=np.float32)
    col_parts = np.array_split(np.arange(cols), max(1, -(-cols // DEM_MAX_TILE_PX)))
    row_parts = np.array_split(np.arange(rows), max(1, -(-rows // DEM_MAX_TILE_PX)))
    for rp in row_parts:
        for cp in col_parts:
            r0, r1, c0, c1 = int(rp[0]), int(rp[-1]) + 1, int(cp[0]), int(cp[-1]) + 1
            tw, th = c1 - c0, r1 - r0
            # a pixel AREA is requested, so pad half a cell to centre pixels on nodes
            bb = (w + c0 * dx - dx / 2, s + r0 * dy - dy / 2,
                  w + (c1 - 1) * dx + dx / 2, s + (r1 - 1) * dy + dy / 2)
            url = DEM3DEP + urllib.parse.urlencode({
                "bbox": "%.8f,%.8f,%.8f,%.8f" % bb, "bboxSR": 4326, "imageSR": 4326,
                "size": f"{tw},{th}", "format": "tiff", "pixelType": "F32",
                "noDataInterpretation": "esriNoDataMatchAny",
                "interpolation": "RSP_BilinearInterpolation", "f": "image"})
            cpath = cache_dir / "dem3dep" / (hashlib.sha1(url.encode()).hexdigest() + ".tif")
            blob = _cache_get(cpath)
            arr = None
            fresh = False
            for attempt in (1, 2, 3):
                if blob is None:
                    try:
                        blob = _get(url, timeout=timeout)
                        fresh = True
                    except Exception as ex:
                        log(f"[dem3dep] request failed ({type(ex).__name__}: {ex})")
                        blob = None
                        time.sleep(min(6, 2 * attempt))
                        continue
                try:
                    a = np.array(Image.open(io.BytesIO(blob)), dtype=np.float32)
                    if a.shape != (th, tw):
                        raise ValueError("got %s, wanted %s" % (a.shape, (th, tw)))
                    arr = a
                    break
                except Exception as ex:
                    log(f"[dem3dep] bad answer ({type(ex).__name__}: {ex})")
                    blob = None
                    try:                   # a cached bad blob must not be re-read forever
                        cpath.unlink()
                    except OSError:
                        pass
            if arr is None:
                raise RuntimeError("3DEP gave no usable tile")
            if fresh:
                _cache_put(cpath, blob)
            out[r0:r1, c0:c1] = arr[::-1]          # TIFF row 0 is NORTH
    return _clean_dem(out, "3DEP")


def _terrarium_tile(z: int, tx: int, ty: int, cache_dir: pathlib.Path, log=print):
    p = cache_dir / "dem" / str(z) / str(tx) / f"{ty}.png"
    blob = _cache_get(p)
    if blob is None:
        try:
            blob = _get(TERRARIUM.format(z=z, x=tx, y=ty))
            _cache_put(p, blob)
        except Exception as e:
            log(f"[dem] tile {z}/{tx}/{ty} failed: {e}")
            return None
    try:
        a = np.asarray(Image.open(io.BytesIO(blob)).convert("RGB"), dtype=np.float32)
        return (a[:, :, 0] * 256.0 + a[:, :, 1] + a[:, :, 2] / 256.0) - 32768.0
    except Exception:
        return None


def _terrarium_grid(bbox, cols: int, rows: int, z: int, cache_dir: pathlib.Path,
                    log=print, max_tiles: int = 400):
    """BILINEAR terrarium samples on the node grid; (rows, cols) float32, south first.
    (dem_elevations is nearest-pixel, which terraces a 3 m grid.)"""
    s, w, n, e = [float(x) for x in bbox]
    lons = np.linspace(w, e, cols)
    lats = np.linspace(s, n, rows)
    X = (lons + 180.0) / 360.0 * (TILE << z) - 0.5            # pixel CENTRES at +0.5
    r = np.radians(np.clip(lats, -85.05112878, 85.05112878))
    Y = (1.0 - np.arcsinh(np.tan(r)) / math.pi) / 2.0 * (TILE << z) - 0.5
    tx0, tx1 = int(math.floor(X.min() / TILE)), int(math.floor((X.max() + 1) / TILE))
    ty0, ty1 = int(math.floor(Y.min() / TILE)), int(math.floor((Y.max() + 1) / TILE))
    nt = (tx1 - tx0 + 1) * (ty1 - ty0 + 1)
    if nt > max_tiles:
        raise RuntimeError(f"{nt} terrarium tiles at z{z} is too many")
    M = np.full(((ty1 - ty0 + 1) * TILE, (tx1 - tx0 + 1) * TILE), np.nan, dtype=np.float32)
    jobs = [(tx, ty) for ty in range(ty0, ty1 + 1) for tx in range(tx0, tx1 + 1)]

    def one(j):
        return j, _terrarium_tile(z, j[0], j[1], cache_dir, log)
    if len(jobs) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=6) as ex:
            res = list(ex.map(one, jobs))
    else:
        res = [one(j) for j in jobs]
    for (tx, ty), a in res:
        if a is not None:
            M[(ty - ty0) * TILE:(ty - ty0 + 1) * TILE,
              (tx - tx0) * TILE:(tx - tx0 + 1) * TILE] = a
    fx, fy = X - tx0 * TILE, Y - ty0 * TILE
    i0 = np.clip(np.floor(fx).astype(int), 0, M.shape[1] - 2)
    j0 = np.clip(np.floor(fy).astype(int), 0, M.shape[0] - 2)
    ax = np.clip(fx - i0, 0.0, 1.0)[None, :]
    ay = np.clip(fy - j0, 0.0, 1.0)[:, None]
    top = M[np.ix_(j0, i0)] * (1 - ax) + M[np.ix_(j0, i0 + 1)] * ax
    bot = M[np.ix_(j0 + 1, i0)] * (1 - ax) + M[np.ix_(j0 + 1, i0 + 1)] * ax
    return _clean_dem(top * (1 - ay) + bot * ay, f"terrarium z{z}")


def _dem_fetch(bbox, cols, rows, cache_dir, terrarium_z: int, log):
    try:
        return _dem3dep(bbox, cols, rows, cache_dir, log=log), "USGS 3DEP"
    except Exception as e:
        log(f"[dem] 3DEP unavailable ({type(e).__name__}: {e}); using AWS terrarium z{terrarium_z}")
    return (_terrarium_grid(bbox, cols, rows, terrarium_z, cache_dir, log=log),
            f"AWS terrarium z{terrarium_z}")


def dem_hires(bbox, cache_dir: pathlib.Path, max_cells: int = 250_000,
              min_cell_m: float = 3.0, log=print) -> dict:
    """Hi-res elevation grid over bbox = (min_lat, min_lon, max_lat, max_lon).

    {"cols","rows","bounds":[s,w,n,e],"cell_m","source","values": float32 ndarray
    (rows*cols, row-major, SOUTH row first)}. Sample (r,c) is the NODE at
    lat = s+(n-s)*r/(rows-1), lon = w+(e-w)*c/(cols-1). USGS 3DEP where it
    covers (US), else AWS terrarium z15 with bilinear sampling. Raises when
    neither source gives a usable grid."""
    _require_deps()
    cache_dir = pathlib.Path(cache_dir)
    cols, rows, cell = _grid_dims(bbox, max_cells, min_cell_m)
    v, src = _dem_fetch(bbox, cols, rows, cache_dir, 15, log)
    log(f"[dem] hi-res {cols}x{rows} @ {cell:.1f} m from {src}: "
        f"{float(v.min()):.1f}..{float(v.max()):.1f} m")
    return {"cols": cols, "rows": rows, "bounds": [float(x) for x in bbox],
            "cell_m": round(cell, 2), "source": src,
            "values": v.reshape(-1).astype(np.float32)}


def dem_far(centre_lat: float, centre_lon: float, half_m: float = 3500.0,
            cells: int = 240, cache_dir: Optional[pathlib.Path] = None,
            log=print) -> dict:
    """Coarse elevation over a square of +-half_m round the track: the distant
    hills for the horizon. Same layout as dem_hires (3DEP, else terrarium z12)."""
    _require_deps()
    cache_dir = pathlib.Path(cache_dir) if cache_dir else \
        pathlib.Path(tempfile.gettempdir()) / "trackprep-cache"
    dlat = half_m / M_PER_DEG_LAT
    dlon = half_m / (M_PER_DEG_LAT * math.cos(math.radians(centre_lat)))
    bbox = (centre_lat - dlat, centre_lon - dlon, centre_lat + dlat, centre_lon + dlon)
    v, src = _dem_fetch(bbox, int(cells), int(cells), cache_dir, 12, log)
    log(f"[dem] far {cells}x{cells} from {src}: {float(v.min()):.1f}..{float(v.max()):.1f} m")
    return {"cols": int(cells), "rows": int(cells), "bounds": [float(x) for x in bbox],
            "cell_m": round(2.0 * half_m / (cells - 1), 2), "source": src,
            "values": v.reshape(-1).astype(np.float32)}


def dem_sample(grid: dict, lats, lons):
    """Bilinear read of a dem_hires/dem_far grid; NaN outside its bounds."""
    s, w, n, e = grid["bounds"]
    cols, rows = int(grid["cols"]), int(grid["rows"])
    V = np.asarray(grid["values"], dtype=np.float32).reshape(rows, cols)
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    fx = (lons - w) / (e - w) * (cols - 1)
    fy = (lats - s) / (n - s) * (rows - 1)
    inside = (fx >= 0) & (fx <= cols - 1) & (fy >= 0) & (fy <= rows - 1)
    i0 = np.clip(np.floor(fx).astype(int), 0, cols - 2)
    j0 = np.clip(np.floor(fy).astype(int), 0, rows - 2)
    ax = np.clip(fx - i0, 0.0, 1.0)
    ay = np.clip(fy - j0, 0.0, 1.0)
    top = V[j0, i0] * (1 - ax) + V[j0, i0 + 1] * ax
    bot = V[j0 + 1, i0] * (1 - ax) + V[j0 + 1, i0 + 1] * ax
    out = (top * (1 - ay) + bot * ay).astype(float)
    out[~inside] = np.nan
    return out


def write_dem_bin(grid: dict, path) -> dict:
    """Quantise a grid to uint16 little-endian: q = round((v - base) / scale),
    base = min (floored to 1 mm), scale 0.05 m (0.1 / 0.25 if the range needs it).
    Rows are SOUTH first, row-major. Atomic (unique temp + os.replace)."""
    _require_deps()
    path = pathlib.Path(path)
    v = np.asarray(grid["values"], dtype=np.float64).reshape(-1)
    if v.size != int(grid["cols"]) * int(grid["rows"]):
        raise ValueError("values do not match cols*rows")
    base = math.floor(float(v.min()) * 1000.0) / 1000.0
    span = float(v.max()) - base
    scale = 0.05
    for sc in (0.05, 0.1, 0.25):
        scale = sc
        if span / sc <= 65535:
            break
    q = np.clip(np.rint((v - base) / scale), 0, 65535).astype("<u2")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(q.tobytes())
        os.chmod(tmp, 0o644)             # mkstemp makes 0600; the server may run as another user
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return {"file": path.name, "cols": int(grid["cols"]), "rows": int(grid["rows"]),
            "bounds": [float(x) for x in grid["bounds"]], "base": base, "scale": scale,
            "cell_m": grid.get("cell_m"), "source": grid.get("source"),
            "format": "u16le"}


def read_dem_bin(meta: dict, path):
    """Inverse of write_dem_bin -> float32 ndarray (rows*cols, south row first)."""
    _require_deps()
    raw = pathlib.Path(path).read_bytes()
    q = np.frombuffer(raw, dtype="<u2")
    if q.size != int(meta["cols"]) * int(meta["rows"]):
        raise ValueError("dem bin is %d samples, meta says %d x %d"
                         % (q.size, meta["cols"], meta["rows"]))
    return (float(meta["base"]) + q.astype(np.float64) * float(meta["scale"])).astype(np.float32)


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


def _overpass_search(q: str, timeout: float, log, build, is_empty):
    """POST `q` to the Overpass mirrors until one gives a NON-EMPTY answer.

    An empty answer is not proof of anything: a region-limited mirror (the
    Switzerland-only instance we used to list) returns a perfectly VALID empty
    document for a US bbox, and a busy main instance can do the same, so one
    mirror saying "nothing" must not end the search. Errors back off (bounded)
    and retry the same mirror once, then move on.

    Returns the built result of the first non-empty answer; else the (empty)
    result of a mirror that DID answer; else None (every mirror failed)."""
    body = urllib.parse.urlencode({"data": q}).encode()
    errs = 0
    empty = None
    for ep in OVERPASS:
        for attempt in (1, 2):
            try:
                d = json.loads(_get(ep, timeout=timeout, data=body))
                if not isinstance(d, dict):
                    raise ValueError("not an object")
                rem = str(d.get("remark") or "")
                if "runtime error" in rem.lower():
                    raise RuntimeError(rem[:80])        # partial answer: do not trust
            except Exception as e:
                errs += 1
                log(f"[osm] {ep}: {type(e).__name__}")
                time.sleep(min(8, 2 * errs))
                continue
            res = build(d.get("elements") or [])
            if is_empty(res):
                log(f"[osm] {ep}: empty answer; trying the next mirror")
                empty = res
                break
            return res
    return empty


def _osm_cached(bbox, key: str, q: str, timeout: float, log, use_cache: bool,
                build, is_empty):
    """Shared cache / mirror / stale-fallback logic. Returns the result, or
    None when nothing at all could be obtained (every mirror failed, no cache).
    Only NON-EMPTY results are ever cached."""
    cp = _osm_cache_path(bbox, key)
    if use_cache:
        try:
            if cp.is_file():
                age_days = (time.time() - cp.stat().st_mtime) / 86400.0
                if age_days <= OSM_CACHE_DAYS:
                    fresh = json.loads(cp.read_text("utf-8"))
                    log("[osm] cache hit (%.1f days old)" % age_days)
                    return fresh
        except Exception:
            pass
    res = _overpass_search(q, timeout, log, build, is_empty)
    if res is not None and not is_empty(res):
        if use_cache:
            try:
                cp.parent.mkdir(parents=True, exist_ok=True)
                tmp = cp.with_suffix(".tmp")
                tmp.write_text(json.dumps(res), "utf-8")
                tmp.replace(cp)
            except OSError:
                pass
        return res
    if use_cache:
        try:                                   # last resort: any stale copy
            if cp.is_file():
                stale = json.loads(cp.read_text("utf-8"))
                log("[osm] no live data; using a STALE cache copy")
                return stale
        except Exception:
            pass
    if res is None:
        log("[osm] all mirrors failed")
    return res


def osm_raceways(bbox, timeout: float = 90.0, log=print, use_cache: bool = True):
    """`highway=raceway` ways inside bbox -> [{name, width_m, points[(lat,lon)]}].

    Cached on disk (45 days) and PREFERRED when the API is unreachable: Overpass
    is a shared community service that rate-limits, and a stale cache beats
    failing a track preparation. Also, mirrors are only tried with backoff so a
    busy day does not turn into a hammering loop. An EMPTY answer from one
    mirror falls through to the next (and is never cached).
    """
    min_lat, min_lon, max_lat, max_lon = bbox
    q = (f'[out:json][timeout:{int(timeout)-5}];'
         f'way["highway"="raceway"]({min_lat},{min_lon},{max_lat},{max_lon});'
         f'out geom;')

    def build(elements):
        out = []
        for e in elements:
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
        return out

    res = _osm_cached(bbox, "raceway", q, timeout, log, use_cache, build,
                      lambda r: not r)
    return res or []


# ---------------------------------------------------------------------------
# OpenStreetMap features: buildings, water, woods, roads, barriers, trees ...
# ---------------------------------------------------------------------------
OSM_ATTRIB = "\u00a9 OpenStreetMap contributors (ODbL)"
FEATURE_CAPS = {"buildings": 4000, "roads": 4000, "barriers": 3000, "trees": 6000,
                "polygons": 3000}
_AREA_CLASSES = ("water", "woods", "scrub", "grass", "farmland", "parking", "paved")
_BARRIERS = {"fence", "wall", "guard_rail", "retaining_wall", "jersey_barrier",
             "city_wall", "hedge", "handrail"}
_ROAD_SKIP = {"steps", "elevator", "platform"}
_GRASS_LANDUSE = {"grass", "meadow", "recreation_ground", "village_green"}
_FARM_LANDUSE = {"farmland", "orchard", "vineyard", "farmyard"}
_GRASS_LEISURE = {"park", "pitch", "golf_course"}


def _empty_features() -> dict:
    d = {"v": 1, "attrib": OSM_ATTRIB, "buildings": []}
    for k in _AREA_CLASSES:
        d[k] = []
    d.update({"roads": [], "barriers": [], "tree_rows": [], "trees": []})
    return d


def _features_empty(f) -> bool:
    if not f:
        return True
    return not any(f.get(k) for k in
                   ("buildings", "roads", "barriers", "tree_rows", "trees") + _AREA_CLASSES)


_NUM = r"(-?\d+(?:[.,]\d+)?)"


def parse_height_m(v) -> Optional[float]:
    """OSM `height` -> metres. '12', '12 m', '12.5m', "40'", '40 ft', "40'6\\""."""
    if v is None:
        return None
    t = str(v).strip().lower()
    if not t:
        return None
    m = re.match(r"^" + _NUM + r"\s*'\s*(?:" + _NUM + r"\s*\"?)?$", t)
    if m:
        val = float(m.group(1).replace(",", ".")) * 0.3048
        if m.group(2):
            val += float(m.group(2).replace(",", ".")) * 0.0254
    else:
        m = re.match(r"^" + _NUM + r"\s*(m|meters?|metres?|ft|feet|foot)?\.?$", t)
        if not m:
            return None
        val = float(m.group(1).replace(",", "."))
        if (m.group(2) or "").startswith("f"):
            val *= 0.3048
    if not (0.0 < val < 1000.0):
        return None
    return round(val, 1)


def _parse_levels(v) -> Optional[int]:
    try:
        n = int(float(str(v).strip().replace(",", ".")))
    except Exception:
        return None
    return n if 0 < n < 300 else None


def _geom_pts(g):
    return [[round(float(p["lat"]), 6), round(float(p["lon"]), 6)]
            for p in (g or []) if p and "lat" in p and "lon" in p]


def _ring(g):
    """Closed way geometry -> ring without the repeated closing point (or None)."""
    pts = _geom_pts(g)
    if len(pts) < 4 or pts[0] != pts[-1]:
        return None
    pts = pts[:-1]
    return pts if len(pts) >= 3 else None


def _area_class(tags: dict) -> Optional[str]:
    nat, lu, lei = tags.get("natural"), tags.get("landuse"), tags.get("leisure")
    if nat == "water" or tags.get("waterway") == "riverbank" or lu in ("reservoir", "basin"):
        return "water"
    if nat == "wood" or lu == "forest":
        return "woods"
    if nat in ("scrub", "heath"):
        return "scrub"
    if lu in _GRASS_LANDUSE or nat == "grassland" or lei in _GRASS_LEISURE:
        return "grass"
    if lu in _FARM_LANDUSE:
        return "farmland"
    if tags.get("amenity") == "parking":
        return "parking"
    if tags.get("area:highway"):
        return "paved"
    return None


def _features_from_elements(elements) -> dict:
    out = _empty_features()
    caps = FEATURE_CAPS
    for e in elements:
        tags = e.get("tags") or {}
        et = e.get("type")
        if et == "node":
            if tags.get("natural") == "tree" and "lat" in e and "lon" in e \
                    and len(out["trees"]) < caps["trees"]:
                out["trees"].append([round(float(e["lat"]), 6), round(float(e["lon"]), 6)])
            continue
        if et == "way":
            g = e.get("geometry") or []
            b = tags.get("building")
            if b and b != "no":
                ring = _ring(g)
                if ring and len(out["buildings"]) < caps["buildings"]:
                    out["buildings"].append({
                        "p": ring, "k": b,
                        "h": parse_height_m(tags.get("height") or tags.get("building:height")),
                        "l": _parse_levels(tags.get("building:levels")),
                        "n": tags.get("name")})
                continue
            cls = _area_class(tags)
            if cls:
                ring = _ring(g)
                if ring and len(out[cls]) < caps["polygons"]:
                    out[cls].append(ring)
            hw = tags.get("highway")
            if hw and hw not in _ROAD_SKIP and tags.get("area") != "yes":
                pts = _geom_pts(g)
                if len(pts) >= 2 and len(out["roads"]) < caps["roads"]:
                    try:
                        w = float(str(tags.get("width", "")).replace("m", "").strip())
                    except Exception:
                        w = None
                    out["roads"].append({"p": pts, "k": hw,
                                         "w": w if (w and 0 < w < 100) else None,
                                         "n": tags.get("name")})
            bar = tags.get("barrier")
            if bar in _BARRIERS:
                pts = _geom_pts(g)
                if len(pts) >= 2 and len(out["barriers"]) < caps["barriers"]:
                    out["barriers"].append({"p": pts, "k": bar})
            if tags.get("natural") == "tree_row":
                pts = _geom_pts(g)
                if len(pts) >= 2 and len(out["tree_rows"]) < caps["polygons"]:
                    out["tree_rows"].append(pts)
        elif et == "relation":
            cls = _area_class(tags)
            if not cls:
                continue
            for m in e.get("members") or []:
                if m.get("type") != "way" or m.get("role") != "outer":
                    continue
                ring = _ring(m.get("geometry"))
                if ring and len(out[cls]) < caps["polygons"]:
                    out[cls].append(ring)
    return out


def osm_features(bbox, timeout: float = 90.0, log=print, use_cache: bool = True) -> dict:
    """Buildings, water, woods, roads, barriers, trees ... inside bbox as one
    COMPACT dict (see the module notes / asset schema "features"). bbox =
    (min_lat, min_lon, max_lat, max_lon). Cached like osm_raceways; an
    all-mirrors-empty/failed answer returns the empty features dict."""
    s, w, n, e = bbox
    bb = f"({s},{w},{n},{e})"
    parts = ['way["building"]', 'way["natural"]', 'way["landuse"]',
             'way["amenity"="parking"]', 'way["highway"]', 'way["barrier"]',
             'way["leisure"]', 'way["area:highway"]', 'way["man_made"]',
             'way["waterway"="riverbank"]', 'node["natural"="tree"]',
             'relation["natural"="water"]', 'relation["landuse"]',
             'relation["natural"="wood"]']
    q = (f'[out:json][timeout:{max(10, int(timeout) - 5)}];('
         + "".join(p + bb + ";" for p in parts) + ');out geom;')
    res = _osm_cached(bbox, "features", q, timeout, log, use_cache,
                      _features_from_elements, _features_empty)
    return res if res is not None else _empty_features()


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
    With opts["raw"] it also returns left_raw[] / right_raw[]: the UNPROCESSED
    per-station edges along the line's -normal / +normal (0 where not ok), which
    refine_centreline uses (the published left/right go through a squeeze).
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
    raw_out = ({"left_raw": left.tolist(), "right_raw": right.tolist()}
               if opts.get("raw") else {})
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
                "background_rgb": None if learned is None else [round(float(x), 1) for x in learned[1]],
                **raw_out}
    fallback = float(opts.get("width_fallback_m") or 12.0)
    return {"left": [fallback / 2] * n, "right": [fallback / 2] * n,
            "width": [fallback] * n, "ok": ok.tolist(), "confidence": conf,
            "median_width_m": fallback, "mode_width_m": fallback,
            "classifier": used, "asphalt_rgb": None, "background_rgb": None,
            **raw_out}


# ---------------------------------------------------------------------------
# centreline refinement: pull the line onto the middle of the paved corridor
# ---------------------------------------------------------------------------
REFINE_MAX_SHIFT_M = 4.0
REFINE_MIN_OK_FRAC = 0.35


def _movavg(a, k: int):
    a = np.asarray(a, dtype=float)
    k = max(1, int(k))
    if k <= 1 or a.size == 0:
        return a.copy()
    r = k // 2
    pad = np.pad(a, (r, k - 1 - r), mode="edge")
    return np.convolve(pad, np.ones(k) / k, "valid")


def _line_frame(lats, lons) -> dict:
    """lat/lon stations -> the same dict resample() makes (s, tan, normal), but
    WITHOUT resampling, so an existing asset's stations keep their indices."""
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    lat0, lon0 = float(lats.mean()), float(lons.mean())
    kx = M_PER_DEG_LAT * math.cos(math.radians(lat0))
    xs = (lons - lon0) * kx
    ys = (lats - lat0) * M_PER_DEG_LAT
    dx, dy = np.gradient(xs), np.gradient(ys)
    ang = np.arctan2(dx, dy)
    cum = np.concatenate([[0.0], np.cumsum(np.hypot(np.diff(xs), np.diff(ys)))])
    return {"lat": lats, "lon": lons, "s": cum, "total_m": float(cum[-1]),
            "tan": ang, "normal": np.stack([np.cos(ang), -np.sin(ang)], axis=1)}


def _centre_offsets(ok, left_raw, right_raw, max_shift: float = REFINE_MAX_SHIFT_M):
    """Smoothed lateral offset (m, along +normal) of the paved corridor's middle.

    The profile's NEGATIVE offsets are `left`, so the corridor spans
    [-left, +right] along the normal and its middle is (right - left) / 2.
    Robust: a 21-station rolling median of the ok stations, then a 15-station
    moving average; where fewer than 40 % of a 41-station neighbourhood are ok
    the imagery has not seen the road there and the offset fades to 0 (smoothly)."""
    ok = np.asarray(ok, dtype=bool)
    n = len(ok)
    if n == 0:
        return np.zeros(0)
    raw = (np.asarray(right_raw, dtype=float) - np.asarray(left_raw, dtype=float)) / 2.0
    med = np.full(n, np.nan)
    for i in range(n):
        lo, hi = max(0, i - 10), min(n, i + 11)
        sel = ok[lo:hi]
        if sel.any():
            med[i] = float(np.median(raw[lo:hi][sel]))
    valid = ~np.isnan(med)
    if not valid.any():
        return np.zeros(n)
    idx = np.arange(n)
    med = np.interp(idx, idx[valid], med[valid])
    sm = _movavg(med, 15)
    cs = np.concatenate([[0], np.cumsum(ok.astype(int))])
    lo = np.maximum(0, idx - 20)
    hi = np.minimum(n, idx + 21)
    frac = (cs[hi] - cs[lo]) / np.maximum(1, hi - lo)
    gate = _movavg((frac >= 0.4).astype(float), 21)
    return np.clip(sm * gate, -max_shift, max_shift)


def _refine_stations(line: dict, img, bounds: dict, opts=None):
    """-> (offsets_m per station, report). Does not move anything."""
    opts = dict(opts or {})
    opts["raw"] = True
    n = len(line["lat"])
    m = measure_width(img, bounds, line, opts) or {}
    ok, lr, rr = m.get("ok"), m.get("left_raw"), m.get("right_raw")
    if ok is None or lr is None or rr is None or len(ok) != n:
        return np.zeros(n), {"mean_abs_shift_m": 0.0, "max_abs_shift_m": 0.0,
                             "ok_frac": 0.0, "applied": False}
    ok_arr = np.asarray(ok, dtype=bool)
    ok_frac = float(ok_arr.mean()) if n else 0.0
    off = _centre_offsets(ok_arr, lr, rr, float(opts.get("max_shift_m") or REFINE_MAX_SHIFT_M))
    applied = ok_frac >= float(opts.get("min_ok_frac") or REFINE_MIN_OK_FRAC)
    if not applied:
        off = np.zeros(n)
    return off, {"mean_abs_shift_m": round(float(np.abs(off).mean()), 2) if n else 0.0,
                 "max_abs_shift_m": round(float(np.abs(off).max()), 2) if n else 0.0,
                 "ok_frac": round(ok_frac, 3), "applied": bool(applied)}


def _shift_along_normal(lats, lons, normal, off):
    lats = np.asarray(lats, dtype=float)
    lons = np.asarray(lons, dtype=float)
    coslat = math.cos(math.radians(float(lats.mean())))
    nrm = np.asarray(normal, dtype=float)
    return (lats + off * nrm[:, 1] / M_PER_DEG_LAT,
            lons + off * nrm[:, 0] / (M_PER_DEG_LAT * coslat))


def refine_centreline(line_points, img, bounds, opts=None):
    """Centre a (lat,lon) line on the paved corridor the imagery shows.

    OSM ways and driven lines routinely sit 1-4 m off the middle of the tarmac.
    Returns (points[(lat,lon)...], report) with report = {mean_abs_shift_m,
    max_abs_shift_m, ok_frac, applied}. Applied only when the imagery found the
    corridor on >= 35 % of the stations; otherwise the (resampled) input is
    returned unmoved. Shifts are clamped to +-4 m. bounds as measure_width's."""
    _require_deps()
    opts = dict(opts or {})
    line = resample(line_points, float(opts.get("step_m") or 2.0))
    off, rep = _refine_stations(line, img, bounds, opts)
    if rep["applied"]:
        la, lo = _shift_along_normal(line["lat"], line["lon"], line["normal"], off)
    else:
        la, lo = line["lat"], line["lon"]
    return [(float(a), float(b)) for a, b in zip(la, lo)], rep


# ---------------------------------------------------------------------------
# the asset
# ---------------------------------------------------------------------------
LANDCOVER_CODES = "pgwo"        # paved/built, grass, woods, other (dirt, water, gravel)


def _rle(codes: str) -> str:
    out, i, n = [], 0, len(codes)
    while i < n:
        j = i
        while j < n and codes[j] == codes[i]:
            j += 1
        out.append(f"{j - i}{codes[i]}")
        i = j
    return "".join(out)


def unrle(rle: str) -> str:
    out, num = [], ""
    for ch in rle:
        if ch.isdigit():
            num += ch
        else:
            out.append(ch * int(num or 1))
            num = ""
    return "".join(out)


def land_cover(img, bounds: dict, cell_m: float = 4.0) -> dict:
    """Classify the imagery into paved / grass / woods / other, on a cell grid.

    Thresholds measured on real Esri imagery at Watkins Glen (spring, leafless):
      woods  : saturated (>= 0.24), darker (V < 112) and TEXTURED (luminance std
               >= 15 inside an 8 m cell) - canopy is rough, mowed grass is smooth
      paved  : low saturation (< 0.17) or very bright (roofs, V > 175)
      grass  : green above (R+B)/2 by > 8 and not the above
    Woods is decided on 8 m cells (texture needs area) and cleaned with a
    majority filter so single shadowed cells cannot plant a tree; paved and
    grass use the finer cell. Row 0 is the NORTH edge (image order).
    """
    _require_deps()
    A = np.asarray(img.convert("RGB"), dtype=np.float32)
    H, W, _ = A.shape
    lat_mid = (bounds["lat0"] + bounds["lat1"]) / 2.0
    m_per_px = ((bounds["lon1"] - bounds["lon0"]) * 111320.0 *
                math.cos(math.radians(lat_mid)) / W)
    c = max(2, int(round(cell_m / m_per_px)))          # fine cell, px
    gh, gw = H // c, W // c
    if gh < 4 or gw < 4:
        return None
    X = A[:gh * c, :gw * c].reshape(gh, c, gw, c, 3).mean(axis=(1, 3))
    r, g, b = X[..., 0], X[..., 1], X[..., 2]
    v = X.max(axis=2)
    mn = X.min(axis=2)
    sat = np.where(v > 0, (v - mn) / np.maximum(v, 1.0), 0.0)
    green = g - (r + b) / 2.0
    paved = (sat < 0.17) | (v > 175)
    grass = ~paved & (green > 8)

    # woods on 2x2 fine cells (~8 m): mean colour + luminance texture
    gh2, gw2 = gh // 2, gw // 2
    c2 = c * 2
    L = A[:gh2 * c2, :gw2 * c2].mean(axis=2).reshape(gh2, c2, gw2, c2)
    tex = L.std(axis=(1, 3))
    X2 = A[:gh2 * c2, :gw2 * c2].reshape(gh2, c2, gw2, c2, 3).mean(axis=(1, 3))
    v2 = X2.max(axis=2)
    sat2 = np.where(v2 > 0, (v2 - X2.min(axis=2)) / np.maximum(v2, 1.0), 0.0)
    woods2 = (sat2 >= 0.24) & (v2 < 112) & (tex >= 15)
    pad = np.pad(woods2.astype(np.int32), 1)
    nb = sum(pad[1 + dy:1 + dy + gh2, 1 + dx:1 + dx + gw2]
             for dy in (-1, 0, 1) for dx in (-1, 0, 1)) - woods2
    woods2 = woods2 & (nb >= 4)
    woods = np.zeros((gh, gw), dtype=bool)
    woods[:gh2 * 2, :gw2 * 2] = np.repeat(np.repeat(woods2, 2, axis=0), 2, axis=1)
    woods &= ~paved                      # a road through the trees stays a road

    cls = np.full((gh, gw), 3, dtype=np.uint8)          # other
    cls[grass] = 1
    cls[woods] = 2
    cls[paved] = 0
    codes = "".join(LANDCOVER_CODES[k] for k in cls.ravel())
    # bounds of the classified area (whole cells only)
    lon_w = bounds["lon0"] + (bounds["lon1"] - bounds["lon0"]) * (gw * c / W)
    lat_s = bounds["lat0"] + (bounds["lat1"] - bounds["lat0"]) * (gh * c / H)
    return {"bounds": [float(lat_s), float(bounds["lon0"]),
                       float(bounds["lat0"]), float(lon_w)],     # [S, W, N, E]
            "cols": int(gw), "rows": int(gh), "cell_m": round(c * m_per_px, 2),
            "codes": LANDCOVER_CODES, "row0": "north",
            "rle": _rle(codes),
            "share": {k: round(float((cls == i).mean()), 3)
                      for i, k in enumerate(("paved", "grass", "woods", "other"))}}


def landcover_at(lc: dict, codes: str, lat: float, lon: float):
    s, w, n, e = lc["bounds"]
    if not (s <= lat <= n and w <= lon <= e):
        return None
    col = min(lc["cols"] - 1, int((lon - w) / (e - w) * lc["cols"]))
    row = min(lc["rows"] - 1, int((n - lat) / (n - s) * lc["rows"]))
    return codes[row * lc["cols"] + col]


LINE_PAVED_RADIUS = 3       # cells (~12 m): GPS/OSM vs imagery registration slack
LINE_PAVED_MIN = 0.35       # below this the imagery does not show the circuit at all


def landcover_line_agreement(lc: dict, lats, lons, radius: int = LINE_PAVED_RADIUS) -> float:
    """Share of the driven line that the imagery calls PAVED (within `radius` cells).

    The GPS says where the track is; the imagery has to agree. Imagery that is
    tiled wallpaper, from the wrong place, or a placeholder from a blocked tile
    server scores near zero and is refused - the test that would have stopped
    the Watkins Glen 'wallpaper' bake reaching anyone.

    The slack matters: a real, correctly placed circuit can still sit 10-15 m
    off its imagery (the shipped Summit Point Jefferson line is ~15 m off the
    Esri mosaic: 24 % within one cell, 52 % within three). Misregistration
    lowers this score a little; wallpaper drives it to ~0."""
    codes = unrle(lc["rle"])
    cols, rows = lc["cols"], lc["rows"]
    s, w, n, e = lc["bounds"]
    hit = tot = 0
    for lat, lon in zip(lats, lons):
        if not (s <= lat <= n and w <= lon <= e):
            continue
        col = min(cols - 1, int((lon - w) / (e - w) * cols))
        row = min(rows - 1, int((n - lat) / (n - s) * rows))
        tot += 1
        ok = False
        for dr in range(-radius, radius + 1):
            rr = row + dr
            if not 0 <= rr < rows:
                continue
            for dc in range(-radius, radius + 1):
                cc = col + dc
                if 0 <= cc < cols and codes[rr * cols + cc] == "p":
                    ok = True
                    break
            if ok:
                break
        hit += ok
    return hit / tot if tot else 0.0


_LANDCOVER_LOCK = threading.Lock()


def ensure_landcover(asset_path: pathlib.Path, log=print) -> Optional[dict]:
    """Give an already-published asset its land cover, from the texture it
    already has on disk (no network). Assets baked before land cover existed
    heal themselves the first time they are served: the 3D view then plants
    trees only where the imagery shows woods. Returns the updated asset, or
    None when there is nothing to do / nothing to do it with.

    Imagery that does not show the circuit (line_paved < LINE_PAVED_MIN) is
    recorded as {"rejected": ...} instead: the viewer then ignores it (no rle)
    and the next request does not re-classify it (no retry storm). One writer
    at a time, unique temp file, atomic replace - a concurrent reader never
    sees half an asset."""
    with _LANDCOVER_LOCK:
        try:
            asset = json.loads(asset_path.read_text("utf-8"))
        except Exception:
            return None
        if asset.get("landcover") or not asset.get("texture"):
            return None
        tex = asset["texture"]
        img_path = asset_path.parent / tex.get("file", "")
        if not tex.get("file") or not img_path.is_file():
            return None
        _require_deps()
        b = tex["bounds"]
        with Image.open(img_path) as img:
            add_landcover(asset, img, {"lat0": b["north"], "lat1": b["south"],
                                       "lon0": b["west"], "lon1": b["east"]}, log=log)
        lc = asset.get("landcover")
        if not lc:
            asset["landcover"] = {"rejected": "imagery too small to classify"}
        elif lc.get("line_paved", 0.0) < LINE_PAVED_MIN:
            asset["landcover"] = {"rejected": "imagery does not show the circuit",
                                  "line_paved": lc.get("line_paved")}
            log(f"[{asset.get('slug')}] land cover rejected: only "
                f"{100 * lc.get('line_paved', 0):.0f}% of the line on paved pixels")
        fd, tmp = tempfile.mkstemp(prefix=asset_path.stem + ".", suffix=".tmp",
                                   dir=str(asset_path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asset, f)
            os.replace(tmp, asset_path)
        except Exception:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return asset


def add_landcover(asset: dict, img, bounds: dict, log=print) -> dict:
    lc = land_cover(img, bounds)
    if not lc:
        return asset
    lats = [p[0] for p in asset["line"]]
    lons = [p[1] for p in asset["line"]]
    lc["line_paved"] = round(landcover_line_agreement(lc, lats, lons), 3)
    asset["landcover"] = lc
    log(f"[{asset.get('slug')}] land cover {lc['cols']}x{lc['rows']} @ {lc['cell_m']} m: "
        f"{lc['share']}, line on paved {lc['line_paved'] * 100:.0f}%")
    return asset


# ---------------------------------------------------------------------------
# enrichment: real-world features, hi-res terrain, a centred line
# ---------------------------------------------------------------------------
ENRICH_VERSION = 1
ENRICH_RETRY_S = 6 * 3600           # a failed network step is not retried sooner
_ENRICH_ACTIVE = set()
_ENRICH_ACTIVE_LOCK = threading.Lock()
# One writer per asset: a bake and a background enrichment both write
# <slug>.json AND <slug>.dem.bin / .demfar.bin, so they must not interleave
# (metadata from one, grid from the other, decodes as garbage terrain).
# Lock order: slug lock, then _LANDCOVER_LOCK.
_SLUG_LOCKS: dict = {}
_SLUG_LOCKS_GUARD = threading.Lock()


def _slug_lock(asset_path) -> "threading.Lock":
    key = str(pathlib.Path(asset_path).resolve())
    with _SLUG_LOCKS_GUARD:
        lk = _SLUG_LOCKS.get(key)
        if lk is None:
            lk = _SLUG_LOCKS[key] = threading.Lock()
        return lk


def _write_json_atomic(path: pathlib.Path, obj: dict) -> None:
    path = pathlib.Path(path)
    fd, tmp = tempfile.mkstemp(prefix=path.stem + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f)
        os.chmod(tmp, 0o644)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _expand_bbox(bbox, margin_m: float):
    s, w, n, e = [float(x) for x in bbox]
    dlat = margin_m / M_PER_DEG_LAT
    dlon = margin_m / (M_PER_DEG_LAT * math.cos(math.radians((s + n) / 2)))
    return (s - dlat, w - dlon, n + dlat, e + dlon)


def _asset_bbox(asset: dict, prefer_dem: bool):
    d = asset.get("dem") or {}
    if prefer_dem and d.get("bounds") and len(d["bounds"]) == 4:
        return tuple(float(x) for x in d["bounds"])
    if asset.get("bbox") and len(asset["bbox"]) == 4:
        return tuple(float(x) for x in asset["bbox"])
    la = [p[0] for p in asset["line"]]
    lo = [p[1] for p in asset["line"]]
    return (min(la), min(lo), max(la), max(lo))


def _texture_bounds(tex: dict, size) -> dict:
    """The baked JPEG is the (possibly downscaled) Mercator mosaic: rebuild the
    {"z","x0","y0","scale"} frame mosaic_px() needs from its lat/lon bounds."""
    z = 18
    b = tex["bounds"]
    x0, x1 = lon_to_x(b["west"], z), lon_to_x(b["east"], z)
    y0, y1 = lat_to_y(b["north"], z), lat_to_y(b["south"], z)
    return {"z": z, "x0": x0 / TILE, "y0": y0 / TILE,
            "scale": size[0] / (x1 - x0), "scale_y": size[1] / (y1 - y0),
            "lat0": b["north"], "lat1": b["south"], "lon0": b["west"], "lon1": b["east"]}


def _enrich_missing(asset: dict, adir: pathlib.Path) -> bool:
    if not asset.get("features"):
        return True
    for k in ("dem_hr", "dem_far"):
        m = asset.get(k)
        if not m or not (adir / str(m.get("file") or "?")).is_file():
            return True
    return False


def _enrich_steps(asset: dict, adir: pathlib.Path, cache_dir: pathlib.Path, log,
                  network: bool = True, refine: bool = True) -> bool:
    """Mutates `asset` (and writes <slug>.dem.bin / <slug>.demfar.bin into adir).
    Every step is independent: one failing never stops the others. Returns True
    when a .bin file was (re)written."""
    wrote = False
    slug = asset.get("slug") or "track"
    now = time.time()
    line = asset.get("line") or []
    lats = np.array([p[0] for p in line], dtype=float)
    lons = np.array([p[1] for p in line], dtype=float)
    refined_now = False

    # a. centreline refinement, offline, from the asset's own texture ----------
    tex = asset.get("texture")
    if refine and "centreline_refine" not in asset and tex and tex.get("file") \
            and len(line) >= 20 and (adir / tex["file"]).is_file():
        try:
            with Image.open(adir / tex["file"]) as im:
                img = im.convert("RGB")
            frame = _line_frame(lats, lons)
            off, rep = _refine_stations(frame, img, _texture_bounds(tex, img.size))
            asset["centreline_refine"] = rep
            if rep["applied"]:
                la, lo = _shift_along_normal(lats, lons, frame["normal"], off)
                asset["line"] = [[float(a), float(b), p[2] if len(p) > 2 else None]
                                 for a, b, p in zip(la, lo, line)]
                lats, lons = la, lo
                refined_now = True
            log(f"[{slug}] centreline refine: {rep}")
        except Exception as e:
            log(f"[{slug}] centreline refine failed: {type(e).__name__}: {e}")

    # b. OSM features ----------------------------------------------------------
    if network and not asset.get("features") and len(line) >= 2 \
            and now - float(asset.get("features_tried") or 0) >= ENRICH_RETRY_S:
        try:
            bb = _expand_bbox(_asset_bbox(asset, True), 250.0)
            f = osm_features(bb, log=log)
            if _features_empty(f):
                raise RuntimeError("Overpass returned no features")
            asset["features"] = f
            asset.pop("features_error", None)
            asset.pop("features_tried", None)
            log(f"[{slug}] features: " + ", ".join(
                f"{k}={len(v)}" for k, v in f.items() if isinstance(v, list)))
        except Exception as e:
            asset["features_error"] = f"{type(e).__name__}: {e}"[:200]
            asset["features_tried"] = int(now)
            log(f"[{slug}] features failed: {asset['features_error']}")

    # c. hi-res + far elevation ------------------------------------------------
    hr_grid = None
    hr_new = False
    dem_ok_to_try = now - float(asset.get("dem_tried") or 0) >= ENRICH_RETRY_S
    dem_failed = False

    def need(key):
        m = asset.get(key)
        return not m or not (adir / str(m.get("file") or "?")).is_file()

    if network and dem_ok_to_try and len(line) >= 2:
        if need("dem_hr"):
            try:
                bb = _expand_bbox(_asset_bbox(asset, False), 180.0)
                hr_grid = dem_hires(bb, cache_dir, log=log)
                asset["dem_hr"] = write_dem_bin(hr_grid, adir / f"{slug}.dem.bin")
                hr_new = wrote = True
            except Exception as e:
                dem_failed = True
                hr_grid = None
                log(f"[{slug}] hi-res dem failed: {type(e).__name__}: {e}")
        if need("dem_far"):
            try:
                c = asset.get("centre") or [float(lats.mean()), float(lons.mean())]
                far = dem_far(float(c[0]), float(c[1]), cache_dir=cache_dir, log=log)
                asset["dem_far"] = write_dem_bin(far, adir / f"{slug}.demfar.bin")
                wrote = True
            except Exception as e:
                dem_failed = True
                log(f"[{slug}] far dem failed: {type(e).__name__}: {e}")
        if dem_failed:
            asset["dem_tried"] = int(now)
        else:
            asset.pop("dem_tried", None)

    # d. station elevations from the hi-res grid --------------------------------
    hr = asset.get("dem_hr")
    if hr and len(line) >= 2 and (hr_new or refined_now
                                  or asset.get("line_elev_source") != hr.get("source")):
        try:
            grid = hr_grid
            if grid is None:
                grid = dict(hr)
                grid["values"] = read_dem_bin(hr, adir / hr["file"])
            z = dem_sample(grid, lats, lons)
            old = np.array([np.nan if p[2] is None else p[2] for p in asset["line"]],
                           dtype=float)
            z = np.where(np.isfinite(z), z, old)
            good = np.isfinite(z)
            if good.sum() >= 2:
                idx = np.arange(len(z))
                z = np.interp(idx, idx[good], z[good])
                z = _movavg(z, 5)
                asset["line"] = [[p[0], p[1], round(float(v), 1)]
                                 for p, v in zip(asset["line"], z)]
                asset["line_elev_source"] = hr.get("source")
        except Exception as e:
            log(f"[{slug}] station elevations failed: {type(e).__name__}: {e}")

    # e. record ------------------------------------------------------------------
    complete = bool(asset.get("features")) and not _enrich_missing(asset, adir)
    old = asset.get("enrich") or {}
    rec = {"v": ENRICH_VERSION if complete else int(old.get("v") or 0),
           "features": bool(asset.get("features")),
           "dem_hr": (asset.get("dem_hr") or {}).get("source"),
           "dem_far": (asset.get("dem_far") or {}).get("source"),
           "refined": bool((asset.get("centreline_refine") or {}).get("applied"))}
    if {k: v for k, v in old.items() if k != "at"} != rec:
        rec["at"] = int(now)
        asset["enrich"] = rec
    if hr:
        src = dict(asset.get("source") or {})
        src["dem"] = "%s %s m (hi-res)" % (hr.get("source"), hr.get("cell_m"))
        if asset.get("dem_far"):
            src["dem_far"] = asset["dem_far"].get("source")
        if asset.get("features"):
            src["features"] = "OpenStreetMap (Overpass)"
        asset["source"] = src
    return wrote


def enrich_asset(asset_path: pathlib.Path, cache_dir: pathlib.Path, log=print,
                 network: bool = True) -> Optional[dict]:
    """Idempotently upgrade an EXISTING asset in place (see _enrich_steps).

    Adds `features`, `dem_hr` / `dem_far` (+ their .bin files next to the JSON),
    a refined `line`, hi-res station elevations and the `enrich` record. Returns
    the updated asset, or None when there was nothing to do (already enriched,
    nothing new possible, another thread is already on it). network=False runs
    only the offline steps. The slow work happens WITHOUT the asset lock; only
    the final read-merge-write is under it, so serving never waits on Overpass."""
    import copy
    asset_path = pathlib.Path(asset_path)
    key = str(asset_path.resolve())
    with _ENRICH_ACTIVE_LOCK:
        if key in _ENRICH_ACTIVE:
            return None
        _ENRICH_ACTIVE.add(key)
    try:
        _require_deps()
        # the slug lock spans read -> steps -> merge: a bake of the same track
        # waits for it (and vice versa), so grids and metadata always match
        with _slug_lock(asset_path):
            try:
                asset = json.loads(asset_path.read_text("utf-8"))
            except Exception:
                return None
            adir = asset_path.parent
            if int((asset.get("enrich") or {}).get("v") or 0) >= ENRICH_VERSION \
                    and not _enrich_missing(asset, adir):
                return None
            orig = copy.deepcopy(asset)
            wrote = _enrich_steps(asset, adir, pathlib.Path(cache_dir), log, network=network)
            changed = {k: asset[k] for k in asset if asset[k] != orig.get(k)}
            removed = [k for k in orig if k not in asset]
            if not changed and not removed:
                return asset if wrote else None      # only a missing .bin was rewritten
            with _LANDCOVER_LOCK:
                try:
                    fresh = json.loads(asset_path.read_text("utf-8"))
                except Exception:
                    # deleted under us (a forced re-prepare): never resurrect it
                    log(f"[enrich] {asset_path.name} vanished mid-run; not written")
                    return None
                if fresh.get("generated") != orig.get("generated"):
                    log(f"[enrich] {asset_path.name} was re-baked mid-run; not merged")
                    return None
                fresh.update(changed)
                for k in removed:
                    fresh.pop(k, None)
                _write_json_atomic(asset_path, fresh)
            return fresh
    finally:
        with _ENRICH_ACTIVE_LOCK:
            _ENRICH_ACTIVE.discard(key)


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
    # Centre the line on the paved corridor the imagery shows (OSM ways and
    # driven lines sit 1-4 m off the middle of the tarmac).
    refine_report = None
    if opts.get("refine", True):
        try:
            pts, refine_report = refine_centreline(
                line_points, img, bounds, {"step_m": step})
            log(f"[{slug}] centreline refine: {refine_report}")
            if refine_report["applied"]:
                line = resample(pts, step)
                lats, lons = line["lat"], line["lon"]
                min_lat, max_lat = float(lats.min()), float(lats.max())
                min_lon, max_lon = float(lons.min()), float(lons.max())
        except Exception as e:
            log(f"[{slug}] centreline refine failed: {type(e).__name__}: {e}")
            refine_report = None
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
    if refine_report is not None:
        asset["centreline_refine"] = refine_report
    if tex_name:
        add_landcover(asset, img, bounds, log=log)
    problems = validate_asset(asset, line)
    if problems:
        raise RuntimeError("refusing to publish a broken track asset: " +
                           "; ".join(problems))
    asset_path = out_dir / f"{slug}.json"
    with _slug_lock(asset_path):
        if opts.get("enrich", True):
            # features + hi-res/far terrain, so a fresh bake is complete (every
            # step is independent and failure is recorded, never fatal)
            try:
                _enrich_steps(asset, out_dir, cache_dir, log, network=True, refine=False)
            except Exception as e:
                log(f"[{slug}] enrichment failed: {type(e).__name__}: {e}")
        with _LANDCOVER_LOCK:
            _write_json_atomic(asset_path, asset)
    log(f"[{slug}] asset written ({asset_path})")
    return asset


def validate_asset(asset: dict, line: dict) -> list:
    """Reasons this asset must NOT be published (empty list = fine).

    Every one of these was a real failure: imagery that does not cover the
    track, or is so coarse/duplicated that the ground is a smear, or a texture
    whose bounds are so small the viewer's UVs would tile it into wallpaper.
    Failing the bake is far better than shipping a "track" nobody recognises.
    """
    bad = []
    lat = line["lat"]
    min_lat, max_lat = float(lat.min()), float(lat.max())
    lon = line["lon"]
    min_lon, max_lon = float(lon.min()), float(lon.max())
    mpp = 111320.0
    need_w = (max_lon - min_lon) * mpp * math.cos(math.radians((min_lat + max_lat) / 2))
    need_h = (max_lat - min_lat) * mpp
    if not asset.get("line") or len(asset["line"]) < 20:
        bad.append("no usable centreline")
    if asset.get("length_m") and asset["length_m"] > 25000:
        bad.append("traced line is %.1f km - that is laps, not a circuit"
                   % (asset["length_m"] / 1000.0))
    tex = asset.get("texture")
    if not tex:
        return bad                      # geometry-only assets are allowed
    b = tex["bounds"]
    span_w = (b["east"] - b["west"]) * mpp * math.cos(math.radians((min_lat + max_lat) / 2))
    span_h = (b["north"] - b["south"]) * mpp
    if span_w < need_w * 0.95 or span_h < need_h * 0.95:
        bad.append("texture (%.0fx%.0f m) does not cover the track (%.0fx%.0f m)"
                   % (span_w, span_h, need_w, need_h))
    if span_w < 300 or span_h < 300:
        bad.append("texture spans only %.0fx%.0f m - UVs would tile it" % (span_w, span_h))
    px = tex.get("px") or [0, 0]
    if px[0] < 512 or px[1] < 512:
        bad.append("texture is only %dx%d px" % (px[0], px[1]))
    else:
        res = max(span_w / px[0], span_h / px[1])
        if res > 6.0:
            bad.append("texture resolution is %.1f m/px (too coarse to see a track)" % res)
    lc = asset.get("landcover")
    if lc is not None and lc.get("line_paved", 1.0) < LINE_PAVED_MIN:
        bad.append("imagery does not show a road where the GPS line is (only %.0f%% "
                   "of the line is on paved pixels) - wrong place, tiled or placeholder "
                   "imagery" % (100.0 * lc.get("line_paved", 0.0)))
    return bad


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
    ap.add_argument("--enrich", metavar="SLUG|all",
                    help="add OSM features, hi-res terrain and a centred line to "
                         "existing prepared track(s) in place")
    ap.add_argument("--tracks-dir", help="where the <slug>.json assets live "
                    "(default DATA_DIR/tracks; e.g. server/app/seed-tracks)")
    ap.add_argument("--cache-dir", help="tile/DEM cache (default DATA_DIR/tilecache)")
    ap.add_argument("--no-network", action="store_true",
                    help="with --enrich: only the offline steps")
    a = ap.parse_args(argv)
    data_dir = pathlib.Path(a.data_dir)

    if a.enrich:
        tdir = pathlib.Path(a.tracks_dir) if a.tracks_dir else data_dir / "tracks"
        cdir = pathlib.Path(a.cache_dir) if a.cache_dir else data_dir / "tilecache"
        slugs = ([f.stem for f in sorted(tdir.glob("*.json"))] if a.enrich == "all"
                 else [a.enrich])
        rc = 0
        for slug in slugs:
            path = tdir / f"{slug}.json"
            if not path.is_file():
                print(f"{slug}: no such asset in {tdir}")
                rc = 1
                continue
            res = enrich_asset(path, cdir, network=not a.no_network)
            if res is None:
                print(f"{slug}: nothing to do")
                continue
            f = res.get("features") or {}
            print(f"{slug}: enrich={res.get('enrich')} refine={res.get('centreline_refine')}")
            print("    features: " + (", ".join(f"{k}={len(v)}" for k, v in f.items()
                                              if isinstance(v, list)) or "none"),
                  res.get("features_error") or "")
            for k in ("dem_hr", "dem_far"):
                m = res.get(k)
                if m:
                    print(f"    {k}: {m['cols']}x{m['rows']} {m['cell_m']} m {m['source']} "
                          f"-> {m['file']}")
        return rc

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

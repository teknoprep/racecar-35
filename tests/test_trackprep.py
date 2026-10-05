"""Host tests for the track pre-render (server/app/trackprep.py).

The pipeline's expensive parts hit the network, so the tests drive the maths
that decides the TRACK WIDTH — the whole point of the module — against imagery
this file draws itself, where the true width is known to the metre. That covers
the failure that actually happened on real data (a light gravel paddock being
counted as asphalt, reporting a 22 m track that is 10 m wide).

The real end-to-end path (OSM raceway + Esri tiles + AWS DEM) is exercised by
`tests/test_track3d.py` through the baked fixture, and by running
`python3 -m app.trackprep` by hand (see the module docstring).
"""
import io
import json
import math
import os
import pathlib
import time
import unittest
import urllib.parse
from unittest import mock

try:
    import numpy as np
    from PIL import Image
    HAVE_DEPS = True
except Exception:                       # pragma: no cover
    HAVE_DEPS = False

ROOT = pathlib.Path(__file__).resolve().parents[1]
M = 111320.0


def _tp():
    import sys
    sys.path.insert(0, str(ROOT / "server"))
    from app import trackprep
    return trackprep


@unittest.skipUnless(HAVE_DEPS, "Pillow/numpy not installed")
class WidthDetectionTests(unittest.TestCase):
    LAT0, LON0 = 39.0, -77.0
    Z = 18

    def _bounds(self, cols, rows):
        """Georeference the synthetic scene AT the stated latitude.

        The tile indices must come from the latitude, not be picked by hand: a
        scene gridded at 74 N has a Mercator scale 3.6x the one at 39 N, so a
        2 m offset would move ~12 px and every width would come out 3x wrong
        (which is exactly how this test first failed).
        """
        tp = _tp()
        x0 = int(tp.lon_to_x(self.LON0, self.Z) // 256) - cols // 2
        y0 = int(tp.lat_to_y(self.LAT0, self.Z) // 256) - rows // 2
        return {"z": self.Z, "x0": x0, "y0": y0,
                "lon0": tp.x_to_lon(x0 * 256, self.Z),
                "lat0": tp.y_to_lat(y0 * 256, self.Z),
                "lon1": tp.x_to_lon((x0 + cols) * 256, self.Z),
                "lat1": tp.y_to_lat((y0 + rows) * 256, self.Z)}

    def _scene(self, kind):
        """A synthetic top-down view: an asphalt band of exactly 10 m.

        kind='grass'  -> dark green either side  (the easy, detect accurately)
        kind='gravel' -> light neutral run-off below (asphalt-like; the case that
                         made real imagery read 22 m for a 10 m circuit)
        kind='flat'   -> uniform light grey: no track to find at all
        """
        tp = _tp()
        cols, rows = 8, 6                                   # 2048 x 1536 px
        bounds = self._bounds(cols, rows)
        img = Image.new("RGB", (cols * 256, rows * 256))
        px = img.load()
        mpp = tp.metres_per_px(self.LAT0, self.Z)
        half_px = (10.0 / 2) / mpp                          # 5 m in pixels
        shoulder_px = 8.0 / mpp
        w, h = img.size
        for x in range(w):
            yc = h / 2 + 40 * math.sin(x / 260.0)
            for y in range(h):
                dy = y - yc
                if kind == "bare":                      # uniform, no track
                    px[x, y] = (150, 150, 148)
                elif abs(dy) <= half_px:
                    px[x, y] = (120, 120, 118)              # asphalt, 10 m wide
                elif kind == "flat":
                    px[x, y] = (150, 150, 148)
                elif kind == "gravel" and 0 < dy <= half_px + shoulder_px:
                    px[x, y] = (168, 163, 150)              # light neutral run-off
                elif dy < 0:
                    px[x, y] = (52, 92, 40)                 # trees above
                else:
                    px[x, y] = (70, 118, 52)                # grass below
        return img, bounds, half_px, mpp

    def _line(self, bounds, img, mpp):
        """Centreline along the band, in lat/lon."""
        tp = _tp()
        pts = []
        w, h = img.size
        for x in range(200, w - 200, 20):
            yc = h / 2 + 40 * math.sin(x / 260.0)
            # pixel -> lat/lon (inverse of mosaic_px)
            lon = tp.x_to_lon(bounds["x0"] * 256 + x, self.Z)
            lat = tp.y_to_lat(bounds["y0"] * 256 + yc, self.Z)
            pts.append((lat, lon))
        return tp.resample(pts, 2.0)

    def test_band_on_a_light_uniform_surface_is_still_found(self):
        # the old fixed colour rules called the light background "paved" too, so
        # every run was unbounded and nothing was measured; learning the two
        # clusters from the corridor itself separates them
        tp = _tp()
        img, bounds, _, mpp = self._scene("flat")
        line = self._line(bounds, img, mpp)
        got = tp.measure_width(img, bounds, line, {"width_fallback_m": 12})
        self.assertEqual(got["classifier"], "learned")
        self.assertAlmostEqual(got["median_width_m"], 10.0, delta=1.2)

    def test_grass_bounded_asphalt_measures_exactly(self):
        tp = _tp()
        img, bounds, _, mpp = self._scene("grass")
        line = self._line(bounds, img, mpp)
        got = tp.measure_width(img, bounds, line, {"width_fallback_m": 12})
        self.assertGreater(got["confidence"], 0.8)
        self.assertAlmostEqual(got["median_width_m"], 10.0, delta=1.2)

    def test_gravel_runoff_over_reads_which_is_why_the_osm_tag_wins(self):
        tp = _tp()
        img, bounds, _, mpp = self._scene("gravel")
        line = self._line(bounds, img, mpp)
        got = tp.measure_width(img, bounds, line, {"width_fallback_m": 12})
        # light neutral run-off is asphalt-coloured to any colour test: the
        # estimate over-reads. It must NOT come out NARROWER than the truth
        # (that would cut the road in the viewer), and the tag takes precedence.
        self.assertGreaterEqual(got["median_width_m"], 9.0)
        self.assertGreater(got["confidence"], 0.5)

    def test_uniform_surface_has_no_track_and_must_not_invent_one(self):
        tp = _tp()
        img, bounds, _, mpp = self._scene("bare")
        line = self._line(bounds, img, mpp)
        got = tp.measure_width(img, bounds, line, {"width_fallback_m": 12,
                                                   "reach_m": 20})
        # a uniform paddock has no edges, so every run hits the profile edge and
        # is rejected: the answer is the fallback, not a 40 m highway
        self.assertAlmostEqual(got["median_width_m"], 12.0, delta=0.01)

    def test_paved_thresholds(self):
        tp = _tp()
        self.assertTrue(tp._paved((120, 120, 118), {}))       # asphalt
        self.assertTrue(tp._paved((188, 188, 186), {}))       # bleached asphalt
        self.assertFalse(tp._paved((60, 110, 45), {}))        # grass
        self.assertFalse(tp._paved((30, 70, 25), {}))         # trees
        self.assertFalse(tp._paved((5, 20, 3), {}))           # deep shade
        self.assertFalse(tp._paved((120, 150, 70), {}))       # green-tinged dirt
        self.assertFalse(tp._paved((150, 90, 70), {}))        # red dirt / clay
        # measured off real Esri imagery: Watkins Glen's asphalt is a light
        # green-tinted grey (G only +4..+9 above R) and MUST read as paved, while
        # the grass beside it is (96,101,69) / (124,135,92)
        self.assertTrue(tp._paved((131, 137, 123), {}))
        self.assertTrue(tp._paved((145, 146, 132), {}))
        self.assertTrue(tp._paved((129, 133, 118), {}))
        self.assertFalse(tp._paved((96, 101, 69), {}))
        self.assertFalse(tp._paved((124, 135, 92), {}))
        self.assertFalse(tp._paved((105, 114, 69), {}))


@unittest.skipUnless(HAVE_DEPS, "Pillow/numpy not installed")
class AssetTests(unittest.TestCase):
    def test_asset_prefers_an_osm_width_tag_and_records_agreement(self):
        """build_asset must publish the surveyed tag, keep the imagery estimate
        as a cross-check, and never invent an asset without geometry."""
        tp = _tp()
        import numpy as np
        line_pts = [(39.0 + i * 0.00004, -77.0) for i in range(400)]
        fake_img = Image.new("RGB", (512, 512), (70, 118, 52))
        # the imagery must actually show the circuit: a grey road down
        # lon -77.0 (x=256), from the line's start (lat 39.0, y=256) north
        from PIL import ImageDraw
        ImageDraw.Draw(fake_img).rectangle([252, 0, 260, 256], fill=(96, 97, 100))
        bounds = {"z": 18, "x0": 0, "y0": 0, "lon0": -77.01, "lat0": 39.01,
                  "lon1": -76.99, "lat1": 38.99}
        grid = {"cols": 3, "rows": 3, "bounds": [38.99, -77.01, 39.01, -76.99],
                "values": [180.0] * 9}
        with mock.patch.object(tp, "imagery_mosaic", return_value=(fake_img, bounds)), \
             mock.patch.object(tp, "measure_width",
                               return_value={"width": [12.5] * 800, "left": [6.0] * 800,
                                             "right": [6.5] * 800, "ok": [True] * 800,
                                             "confidence": 0.8, "median_width_m": 12.5,
                                             "mode_width_m": 12.5}), \
             mock.patch.object(tp, "dem_elevations", return_value=[181.0] * 800), \
             mock.patch.object(tp, "dem_grid", return_value=grid):
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                asset = tp.build_asset("Test Track", line_pts, pathlib.Path(td),
                                       {"osm_width_m": 10.0, "line_source": "osm:1",
                                        "enrich": False})      # no network in tests
                self.assertEqual(asset["width_source"], "osm-tag")
                self.assertEqual(set(asset["width_m"]), {10.0})
                self.assertEqual(asset["width_imagery_m"], 12.5)
                self.assertTrue(asset["width_agreement"])
                self.assertEqual(asset["dem"], grid)
                self.assertIn("texture", asset)
                self.assertEqual(len(asset["line"][0]), 3)      # lat, lon, elev
                # and it is on disk as <slug>.json (+ jpg)
                written = json.loads((pathlib.Path(td) / "tracks/test-track.json").read_text())
                self.assertEqual(written["slug"], "test-track")

                # land cover rides along, and the imagery agrees with the line
                lc = asset["landcover"]
                self.assertGreater(lc["line_paved"], 0.9)
                self.assertEqual(len(tp.unrle(lc["rle"])), lc["cols"] * lc["rows"])

                # without a tag the imagery estimate IS the width
                asset2 = tp.build_asset("Test Track", line_pts, pathlib.Path(td),
                                        {"line_source": "session", "enrich": False})
                self.assertEqual(asset2["width_source"], "imagery")
                self.assertAlmostEqual(asset2["width_m"][0], 12.5, delta=0.01)

    def test_imagery_that_does_not_show_the_track_is_refused(self):
        """Uniform grass where the GPS says the circuit is = wallpaper /
        placeholder / wrong place: the bake must fail, not publish."""
        tp = _tp()
        line_pts = [(39.0 + i * 0.00004, -77.0) for i in range(400)]
        fake_img = Image.new("RGB", (512, 512), (70, 118, 52))
        bounds = {"z": 18, "x0": 0, "y0": 0, "lon0": -77.01, "lat0": 39.01,
                  "lon1": -76.99, "lat1": 38.99}
        grid = {"cols": 3, "rows": 3, "bounds": [38.99, -77.01, 39.01, -76.99],
                "values": [180.0] * 9}
        with mock.patch.object(tp, "imagery_mosaic", return_value=(fake_img, bounds)), \
             mock.patch.object(tp, "measure_width",
                               return_value={"width": [12.5] * 800, "left": [6.0] * 800,
                                             "right": [6.5] * 800, "ok": [True] * 800,
                                             "confidence": 0.8, "median_width_m": 12.5,
                                             "mode_width_m": 12.5}), \
             mock.patch.object(tp, "dem_elevations", return_value=[181.0] * 800), \
             mock.patch.object(tp, "dem_grid", return_value=grid):
            import tempfile
            with tempfile.TemporaryDirectory() as td:
                with self.assertRaises(RuntimeError) as cm:
                    tp.build_asset("Test Track", line_pts, pathlib.Path(td), {})
                self.assertIn("paved", str(cm.exception))

    def test_misregistered_imagery_still_counts_as_the_track(self):
        """A circuit 10 m off its imagery is still that circuit (the shipped
        Summit Point Jefferson line sits ~15 m off the Esri mosaic)."""
        tp = _tp()
        cols = rows = 40
        codes = ["g"] * (cols * rows)
        for r in range(rows):
            codes[r * cols + 23] = "p"                 # road 3 cells east of the line
        lc = {"cols": cols, "rows": rows, "bounds": [0.0, 0.0, 1.0, 1.0],
              "rle": tp._rle("".join(codes))}
        lats = [0.01 + i * 0.0245 for i in range(40)]
        lons = [20.5 / 40.0] * 40                     # column 20
        self.assertEqual(tp.landcover_line_agreement(lc, lats, lons, radius=1), 0.0)
        self.assertEqual(tp.landcover_line_agreement(lc, lats, lons), 1.0)

    def test_old_assets_gain_land_cover_from_their_own_texture(self):
        """ensure_landcover() heals an asset baked before land cover existed,
        offline, from the texture already on disk - and is a no-op after."""
        tp = _tp()
        import tempfile
        from PIL import ImageDraw
        with tempfile.TemporaryDirectory() as td:
            d = pathlib.Path(td)
            img = Image.new("RGB", (600, 600), (84, 130, 60))          # grass
            dr = ImageDraw.Draw(img)
            dr.rectangle([0, 0, 200, 600], fill=(34, 62, 28))          # dark band
            # canopy texture: woods are rough, not flat
            import random
            rnd = random.Random(3)
            for _ in range(9000):
                x, y = rnd.randrange(0, 200), rnd.randrange(0, 600)
                v = rnd.choice([(16, 40, 14), (58, 96, 40)])
                dr.rectangle([x, y, x + 2, y + 2], fill=v)
            dr.rectangle([290, 0, 310, 600], fill=(100, 100, 104))    # road
            img.save(d / "t.jpg", quality=92)
            line = [[39.0 + i * 0.00002, -77.0, 100.0] for i in range(200)]
            asset = {"slug": "t", "line": line,
                     "texture": {"file": "t.jpg", "px": [600, 600],
                                 "bounds": {"south": 38.998, "north": 39.006,
                                            "west": -77.0052, "east": -76.9948}}}
            (d / "t.json").write_text(json.dumps(asset))
            healed = tp.ensure_landcover(d / "t.json", log=lambda *_: None)
            self.assertIsNotNone(healed)
            lc = healed["landcover"]
            self.assertGreater(lc["share"]["woods"], 0.1, lc["share"])
            self.assertGreater(lc["line_paved"], 0.8)
            on_disk = json.loads((d / "t.json").read_text())
            self.assertIn("landcover", on_disk)
            self.assertIsNone(tp.ensure_landcover(d / "t.json", log=lambda *_: None))
            self.assertEqual([p.name for p in d.glob("*.tmp")], [])

            # imagery that does not show the circuit: recorded as rejected (no
            # rle, so the viewer ignores it) and never re-classified
            Image.new("RGB", (600, 600), (84, 130, 60)).save(d / "g.jpg")
            asset["texture"]["file"] = "g.jpg"
            (d / "g.json").write_text(json.dumps(asset))
            rej = tp.ensure_landcover(d / "g.json", log=lambda *_: None)
            self.assertIn("rejected", rej["landcover"])
            self.assertNotIn("rle", rej["landcover"])
            self.assertIsNone(tp.ensure_landcover(d / "g.json", log=lambda *_: None))

    def test_resample_is_even_and_monotone(self):
        tp = _tp()
        pts = [(39.0 + i * 0.0001, -77.0 + i * 0.0001) for i in range(200)]
        line = tp.resample(pts, 2.0)
        s = line["s"]
        self.assertGreater(len(s), 50)
        steps = np.diff(s)
        self.assertGreater(steps.min(), 1.5)
        self.assertLess(steps.max(), 2.5)
        self.assertGreater(line["total_m"], 100)

    def test_slugify(self):
        tp = _tp()
        self.assertEqual(tp.slugify("Summit Point Shenandoah"), "summit-point-shenandoah")
        self.assertEqual(tp.slugify("  VIR  Full  "), "vir-full")
        self.assertEqual(tp.slugify(""), "track")


class OsmTraceMatchTests(unittest.TestCase):
    """Picking which OSM way IS the circuit. The real Thompson data has a 1612 m
    'Road Course' plus a 560 m 'Thompson Speedway' fragment and a pit lane, and
    the name matcher picked the fragment — so these tests are the shape match."""

    def _ways(self):
        # a circuit loop (the truth), a short fragment with the "right" name,
        # and a pit lane beside the main straight
        loop = []
        for i in range(120):
            a = 2 * math.pi * i / 120
            loop.append((39.0 + 0.004 * math.cos(a), -77.0 + 0.005 * math.sin(a)))
        frag = [(39.01, -77.0 + 0.0001 * i) for i in range(15)]
        pit = [(39.0005 + 0.0001 * i, -76.998) for i in range(20)]
        return [{"id": 1, "name": "Road Course", "width_m": None, "points": loop},
                {"id": 2, "name": "Thompson Speedway", "width_m": None, "points": frag},
                {"id": 3, "name": "Pit Lane", "width_m": 12.0, "points": pit}]

    def test_shape_beats_name(self):
        tp = _tp()
        import random
        random.seed(3)
        loop = self._ways()[0]["points"]
        trace = [(p[0] + random.uniform(-2, 2) / 111320,
                  p[1] + random.uniform(-2, 2) / 111320) for p in loop]
        way, dist = tp.osm_match_by_trace(trace, self._ways(), log=lambda *_: None)
        self.assertIsNotNone(way)
        self.assertEqual(way["id"], 1)
        self.assertLess(dist, 5.0)
        # The name matcher, with its length floor, ALSO refuses the fragment
        # (patch the fetch out: osm_best would otherwise hit Overpass).
        with mock.patch.object(tp, "osm_raceways", return_value=self._ways()):
            picked = tp.osm_best("Thompson Speedway", (38.99, -77.02, 39.02, -76.98),
                                 log=lambda *_: None)
            # ...and without the floor, the name alone picks the 120 m fragment:
            # that is exactly the real-data bug the shape match exists to fix.
            naive = tp.osm_best("Thompson Speedway", (38.99, -77.02, 39.02, -76.98),
                                min_len_m=0, log=lambda *_: None)
        self.assertEqual(picked["id"], 1, "length floor should reject the fragment")
        self.assertEqual(naive["id"], 2, "name alone picks the fragment")

    def test_a_trace_nowhere_near_any_way_is_refused(self):
        tp = _tp()
        far = [(39.2 + 0.001 * i, -77.3) for i in range(50)]
        way, dist = tp.osm_match_by_trace(far, self._ways(), log=lambda *_: None)
        self.assertIsNone(way)

    def test_way_length(self):
        tp = _tp()
        # 0.01 deg of latitude is ~1113 m
        self.assertAlmostEqual(tp.way_length_m([(39.0, -77.0), (39.01, -77.0)]),
                               1113, delta=5)
        self.assertAlmostEqual(tp.way_length_m([(39.0, -77.0)]), 0.0)


class StitchTests(unittest.TestCase):
    """A circuit is mapped as many short ways plus branches. Picking ONE way (by
    name or length) cannot work - at Watkins Glen the longest is 506 m of a
    5552 m lap - so the ways must be stitched into a ring."""

    def _ways(self):
        """A square-ish ring in 4 ways + a pit-lane branch off one corner."""
        def seg(a, b, n=8):
            return [(a[0] + (b[0] - a[0]) * i / n, a[1] + (b[1] - a[1]) * i / n)
                    for i in range(n + 1)]
        A, B, C, D = (39.000, -77.000), (39.000, -76.980), (39.010, -76.980), (39.010, -77.000)
        ring = [{"id": 1, "name": "Main Straight", "points": seg(A, B), "sport": "motor"},
                {"id": 2, "name": "The Esses", "points": seg(B, C), "sport": "motor"},
                {"id": 3, "name": "The Boot", "points": seg(C, D), "sport": "motor"},
                {"id": 4, "name": "The Ninety", "points": seg(D, A), "sport": "motor"}]
        # a pit lane: a dead end (and a hard turn) hanging off corner A
        pit = [{"id": 9, "name": "Pit Lane", "sport": "motor",
                "points": seg(A, (39.0005, -76.9995))}]
        return ring + pit

    def test_stitches_the_ring_and_drops_the_pit_lane(self):
        tp = _tp()
        c = tp.stitch_circuit(self._ways(), min_len_m=200, log=lambda *_: None)
        self.assertIsNotNone(c, "a closed ring exists but was not found")
        self.assertNotIn(9, c["ways"], "the pit lane must not be part of the circuit")
        self.assertEqual(sorted(c["ways"]), [1, 2, 3, 4])
        self.assertTrue(c["closed"])
        # the ring is ~0.02 deg per side: 2 sides E-W (~1.7 km) + 2 N-S (~2.2 km)
        self.assertGreater(c["len"], 5000)
        self.assertLess(c["len"], 7000)

    def test_osm_circuit_prefers_motor_ways(self):
        tp = _tp()
        with mock.patch.object(tp, "osm_raceways", return_value=self._ways()):
            c = tp.osm_circuit((38.99, -77.01, 39.02, -76.97), log=lambda *_: None,
                               min_len_m=200)
        self.assertIsNotNone(c)
        self.assertEqual(sorted(c["ways"]), [1, 2, 3, 4])

    def test_no_ring_returns_none(self):
        tp = _tp()
        ways = [{"id": 1, "name": "x", "points": [(39.0, -77.0), (39.001, -77.0)]}]
        self.assertIsNone(tp.stitch_circuit(ways, min_len_m=100, log=lambda *_: None))


class AssetValidationTests(unittest.TestCase):
    """A bake that would produce wallpaper must FAIL, not publish.

    This is the check that was missing when the server baked a 'track' whose
    ground was a few texels of imagery stretched over the whole screen."""

    def _line(self, span_deg=0.01):
        n = 40
        return {"lat": np.linspace(42.30, 42.30 + span_deg, n),
                "lon": np.linspace(-76.93, -76.93 + span_deg, n)}

    def _asset(self, **over):
        a = {"line": [[42.30, -76.93, 0]] * 40, "length_m": 5000.0,
             "texture": {"bounds": {"south": 42.295, "west": -76.935,
                                    "north": 42.315, "east": -76.915},
                         "px": [2048, 2048], "file": "x.jpg"}}
        a.update(over)
        return a

    def test_good_asset_passes(self):
        tp = _tp()
        self.assertEqual(tp.validate_asset(self._asset(), self._line()), [])

    def test_texture_that_does_not_cover_the_track_fails(self):
        tp = _tp()
        a = self._asset()
        a["texture"]["bounds"] = {"south": 42.300, "west": -76.929,
                                  "north": 42.301, "east": -76.928}   # ~80 m
        bad = tp.validate_asset(a, self._line())
        self.assertTrue(any("does not cover" in b or "spans only" in b for b in bad), bad)

    def test_coarse_texture_fails(self):
        tp = _tp()
        a = self._asset()
        a["texture"]["bounds"] = {"south": 41.0, "west": -78.0, "north": 43.5, "east": -75.5}
        a["texture"]["px"] = [512, 512]          # ~530 m/px
        bad = tp.validate_asset(a, self._line())
        self.assertTrue(any("too coarse" in b for b in bad), bad)

    def test_tiny_texture_fails(self):
        tp = _tp()
        a = self._asset()
        a["texture"]["px"] = [256, 256]
        bad = tp.validate_asset(a, self._line())
        self.assertTrue(any("only 256x256" in b for b in bad), bad)

    def test_a_whole_session_of_laps_is_rejected(self):
        tp = _tp()
        a = self._asset(length_m=31820.0)         # the 31.8 km bug
        bad = tp.validate_asset(a, self._line())
        self.assertTrue(any("laps, not a circuit" in b for b in bad), bad)

    def test_duplicate_tiles_are_refused(self):
        """A blocked/proxied tile source answers every URL with the same image."""
        tp = _tp()
        import hashlib
        blob = b"x" * 2000
        h = hashlib.sha1(blob).hexdigest()
        seen = {h: 100}
        dup = max(seen.values())
        self.assertGreater(dup, max(4, int(100 * 0.25)))     # the guard's condition


class SeedTrackTests(unittest.TestCase):
    """The prepared tracks that ship with the server, so the 3D view is worth
    looking at before anyone has clicked 'prepare track'."""

    def test_seeds_are_valid_assets_of_real_circuits(self):
        d = ROOT / "server/app/seed-tracks"
        seeds = sorted(d.glob("*.json"))
        self.assertGreaterEqual(len(seeds), 4)
        # length is how we know the OSM geometry really is that circuit (a bad
        # match - a 100 m pit fragment - cannot be mistaken for a 3 km lap), and
        # width is the measured/surveyed value that gets DRAWN
        expect = {"summit-point": (2900, 3200, 8.0, 11.0),
                  "summit-point-jefferson": (1500, 1750, 13.0, 16.0),
                  "summit-point-shenandoah": (3000, 3300, 9.0, 11.0),
                  "watkins-glen-grand-prix": (4900, 5500, 9.0, 13.0)}
        for f in seeds:
            a = json.loads(f.read_text())
            self.assertIn(a["slug"], expect, a["slug"])
            lo, hi, wlo, whi = expect[a["slug"]]
            # length is how we know the OSM way really is that circuit, and how a
            # bad match (a 100 m pit fragment) gets caught
            self.assertGreater(a["length_m"], lo, a["slug"])
            self.assertLess(a["length_m"], hi, a["slug"])
            w = a.get("width_osm_m") or a.get("width_imagery_m")
            self.assertGreater(w, wlo)
            self.assertLess(w, whi)
            self.assertGreaterEqual(a["width_confidence"], 0.5)
            if a["width_clamped"]:
                # a clamped width must carry the raw measurement, so the
                # disagreement is visible instead of hidden
                self.assertIsNotNone(a.get("width_imagery_raw_m"))
                self.assertNotAlmostEqual(a["width_imagery_raw_m"],
                                          a["width_imagery_m"], delta=0.01)
            self.assertIn("Esri", a["texture"]["attrib"])
            self.assertTrue((d / a["texture"]["file"]).is_file())
            self.assertEqual(len(a["line"]), len(a["width_m"]))
            self.assertEqual(a.get("prep_version"), 2,
                             "seeds must be current-schema, else the server "
                             "treats them as stale and re-prepares")
            self.assertTrue((d / a["texture"]["file"]).is_file(), a["texture"]["file"])
            self.assertIn("Esri", a["texture"]["attrib"])
            # current enrichment: the whole facility's track network + a ground
            # image; network v2 = edges traced in 2-D (a stale seed would be
            # re-enriched on every server it lands on)
            self.assertEqual((a.get("enrich") or {}).get("v"), _tp().ENRICH_VERSION, a["slug"])
            net = a.get("network") or {}
            self.assertEqual(net.get("v"), _tp().NETWORK_V)
            self.assertTrue(net.get("chains"), a["slug"])
            kinds = {c["kind"] for c in net["chains"]}
            self.assertIn("circuit", kinds, a["slug"])
            self.assertIn("pit", kinds, a["slug"])
            for c in net["chains"]:
                if c["kind"] != "area":
                    self.assertEqual(len(c["p"]), len(c["hw"]))
                    lo_w, hi_w = (3.0, 30.0) if c["ws"] == "osm" else \
                        self.tp_clamp(c["kind"], c.get("edges"))
                    self.assertGreaterEqual(c["w"], lo_w - 0.1, c)
                    self.assertLessEqual(c["w"], hi_w + 0.1, c)
            g = a.get("ground") or {}
            self.assertEqual(g.get("file"), a["slug"] + ".ground.jpg")
            self.assertTrue((d / g["file"]).is_file(), g["file"])
            self.assertLessEqual((d / g["file"]).stat().st_size, 1_700_000)
            self.assertIn("Esri", g["attrib"])
            self.assertLessEqual(max(g["px"]), 3072)
            b = g["bounds"]
            nb = net["bbox"]
            self.assertTrue(b["south"] <= nb[0] and b["west"] <= nb[1]
                            and b["north"] >= nb[2] and b["east"] >= nb[3], a["slug"])
        # the facility layouts are all there (Summit Point: 3 circuits + kart + pits)
        sp = json.loads((d / "summit-point.json").read_text())
        names = {n for c in sp["network"]["chains"] for n in c["names"]}
        for want in ("Summit Point Circuit", "Jefferson Circuit", "Kart Track", "Pit Lane"):
            self.assertIn(want, names)
        # ...and the circuits' real edges were traced, not defaulted: a width that
        # VARIES along the lap (a constant is the v1 failure)
        for want in ("Summit Point Circuit", "Jefferson Circuit", "Shennandoah Circuit"):
            c = max((c for c in sp["network"]["chains"] if want in c["names"]),
                    key=lambda c: len(c["p"]))
            self.assertEqual(c.get("edges"), "trace2d", want)
            self.assertGreaterEqual(c.get("conf") or 0, 0.85, want)
            tot = [l + r for l, r in c["hw"]]
            self.assertGreater(max(tot) - min(tot), 3.0, want)
            self.assertTrue(8.0 <= sorted(tot)[len(tot) // 2] <= 14.0, want)
        wg = json.loads((d / "watkins-glen-grand-prix.json").read_text())
        names = {n for c in wg["network"]["chains"] for n in c["names"]}
        for want in ("Pit Lane", "The Boot"):
            self.assertIn(want, names)

    @staticmethod
    def tp_clamp(kind, edges=None):
        tp = _tp()
        return (tp.TRACE_WIDTH_CLAMP if edges == "trace2d" else tp.WIDTH_CLAMP)[kind]


class FixtureSchemaTests(unittest.TestCase):
    """The baked Shenandoah asset (real imagery/DEM/OSM data) is the viewer
    test's input — pin its schema so a pipeline change cannot silently break it."""

    def test_baked_fixture_shape(self):
        a = json.loads((ROOT / "tests/fixtures/track-shenandoah.json").read_text())
        for k in ("track", "slug", "line", "width_m", "width_source", "dem",
                  "texture", "bbox", "centre"):
            self.assertIn(k, a, k)
        self.assertGreater(len(a["line"]), 1000)
        self.assertEqual(len(a["line"]), len(a["width_m"]))
        self.assertEqual(a["width_source"], "osm-tag")
        self.assertEqual(a["width_osm_m"], 10.0)
        self.assertAlmostEqual(a["width_imagery_m"], 12.5, delta=2.0)
        self.assertTrue(a["width_agreement"])
        d = a["dem"]
        self.assertEqual(len(d["values"]), d["cols"] * d["rows"])
        self.assertGreater(max(d["values"]) - min(d["values"]), 5)
        t = a["texture"]
        self.assertEqual(set(t["bounds"]), {"south", "west", "north", "east"})
        self.assertIn("Esri", t["attrib"])
        self.assertTrue(t["px"][0] >= 512)


# ---------------------------------------------------------------------------
# more data sources: OSM features, hi-res DEM, centreline refinement, enrich
# ---------------------------------------------------------------------------
def _overpass_doc():
    def g(*pts):
        return [{"lat": a, "lon": b} for a, b in pts]
    sq = [(39.0, -77.0), (39.0, -76.999), (39.001, -76.999), (39.001, -77.0), (39.0, -77.0)]
    return {"elements": [
        {"type": "way", "id": 1, "tags": {"building": "yes", "height": "40'", "name": "Pit"},
         "geometry": g(*sq)},
        {"type": "way", "id": 2, "tags": {"building": "house", "height": "12.5m",
                                          "building:levels": "3"}, "geometry": g(*sq)},
        {"type": "way", "id": 3, "tags": {"building": "no"}, "geometry": g(*sq)},
        {"type": "way", "id": 4, "tags": {"building": "yes"},          # not closed
         "geometry": g(*sq[:4])},
        {"type": "way", "id": 5, "tags": {"man_made": "tower"}, "geometry": g(*sq)},
        {"type": "way", "id": 6, "tags": {"highway": "service", "name": "Paddock Rd",
                                          "width": "6 m"},
         "geometry": g((39.0, -77.0), (39.0005, -77.0))},
        {"type": "way", "id": 7, "tags": {"highway": "steps"},
         "geometry": g((39.0, -77.0), (39.0005, -77.0))},
        {"type": "way", "id": 8, "tags": {"highway": "service", "area": "yes"},
         "geometry": g(*sq)},
        {"type": "way", "id": 9, "tags": {"barrier": "guard_rail"},
         "geometry": g((39.0, -77.0), (39.0, -76.999))},
        {"type": "way", "id": 10, "tags": {"barrier": "gate"},
         "geometry": g((39.0, -77.0), (39.0, -76.999))},
        {"type": "way", "id": 11, "tags": {"natural": "tree_row"},
         "geometry": g((39.0, -77.0), (39.0, -76.999))},
        {"type": "way", "id": 12, "tags": {"leisure": "pitch"}, "geometry": g(*sq)},
        {"type": "way", "id": 13, "tags": {"amenity": "parking"}, "geometry": g(*sq)},
        {"type": "node", "id": 14, "lat": 39.0004, "lon": -77.0004,
         "tags": {"natural": "tree"}},
        {"type": "node", "id": 15, "lat": 39.0004, "lon": -77.0004, "tags": {"barrier": "bollard"}},
        {"type": "relation", "id": 16, "tags": {"natural": "water", "type": "multipolygon"},
         "members": [{"type": "way", "role": "outer", "geometry": g(*sq)},
                     {"type": "way", "role": "inner", "geometry": g(*sq)},
                     {"type": "way", "role": "outer", "geometry": g(*sq[:3])}]},
        {"type": "relation", "id": 17, "tags": {"landuse": "forest"},
         "members": [{"type": "way", "role": "outer", "geometry": g(*sq)}]},
    ]}


class OsmFeatureTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tp = _tp()
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        p1 = mock.patch.object(self.tp, "OSM_CACHE_DIR", pathlib.Path(self.td.name))
        p2 = mock.patch.object(self.tp.time, "sleep", lambda *_: None)
        p1.start(); p2.start()
        self.addCleanup(p1.stop); self.addCleanup(p2.stop)

    def test_overpass_json_becomes_compact_features(self):
        tp = self.tp
        calls = []

        def fake_get(url, timeout=45.0, data=None):
            calls.append(url)
            return json.dumps(_overpass_doc()).encode()
        with mock.patch.object(tp, "_get", fake_get):
            f = tp.osm_features((38.99, -77.01, 39.01, -76.99), log=lambda *_: None)
        self.assertNotIn("overpass.osm.ch", " ".join(tp.OVERPASS))
        self.assertEqual(f["v"], 1)
        self.assertIn("OpenStreetMap", f["attrib"])
        b = f["buildings"]
        self.assertEqual(len(b), 2)                       # no / open / man_made dropped
        self.assertEqual(b[0]["k"], "yes")
        self.assertAlmostEqual(b[0]["h"], 12.2, delta=0.06)   # 40 ft
        self.assertEqual(b[0]["n"], "Pit")
        self.assertIsNone(b[0]["l"])
        self.assertEqual(b[1]["h"], 12.5)
        self.assertEqual(b[1]["l"], 3)
        self.assertEqual(len(b[0]["p"]), 4)               # closing point dropped
        self.assertEqual(b[0]["p"][0], [39.0, -77.0])
        self.assertEqual([r["k"] for r in f["roads"]], ["service"])   # steps, area=yes out
        self.assertEqual(f["roads"][0]["w"], 6.0)
        self.assertEqual(f["roads"][0]["n"], "Paddock Rd")
        self.assertEqual([x["k"] for x in f["barriers"]], ["guard_rail"])  # gate/bollard out
        self.assertEqual(len(f["tree_rows"]), 1)
        self.assertEqual(f["trees"], [[39.0004, -77.0004]])
        self.assertEqual(len(f["water"]), 1)              # outer+closed only
        self.assertEqual(len(f["woods"]), 1)
        self.assertEqual(len(f["grass"]), 1)              # leisure=pitch
        self.assertEqual(len(f["parking"]), 1)
        for k in ("scrub", "farmland", "paved"):
            self.assertEqual(f[k], [])
        # the query asked for what the spec lists
        self.assertTrue(calls)

    def test_height_parsing(self):
        tp = self.tp
        for raw, want in (("12", 12.0), ("12 m", 12.0), ("12.5m", 12.5),
                          ("40'", 12.2), ("40 ft", 12.2), ("12,5", 12.5)):
            self.assertAlmostEqual(tp.parse_height_m(raw), want, delta=0.06, msg=raw)
        for raw in ("", "tall", None, "0", "-3", "5000"):
            self.assertIsNone(tp.parse_height_m(raw), raw)

    def test_caps_and_degenerate_geometry(self):
        tp = self.tp
        els = [{"type": "node", "lat": 1.0, "lon": float(i) / 1e5, "tags": {"natural": "tree"}}
               for i in range(7000)]
        els.append({"type": "way", "tags": {"highway": "path"},
                    "geometry": [{"lat": 1.0, "lon": 1.0}]})        # 1 point: dropped
        f = tp._features_from_elements(els)
        self.assertEqual(len(f["trees"]), 6000)
        self.assertEqual(f["roads"], [])

    def test_empty_mirror_falls_through_and_only_data_is_cached(self):
        tp = self.tp
        seen = []

        def fake_get(url, timeout=45.0, data=None):
            seen.append(url)
            if url == tp.OVERPASS[0]:
                return b'{"elements": []}'                          # valid but EMPTY
            return json.dumps(_overpass_doc()).encode()
        bbox = (38.99, -77.01, 39.01, -76.99)
        with mock.patch.object(tp, "_get", fake_get):
            f = tp.osm_features(bbox, log=lambda *_: None)
        self.assertEqual(seen, tp.OVERPASS[:2])
        self.assertEqual(len(f["buildings"]), 2)
        cp = tp._osm_cache_path(bbox, "features")
        self.assertTrue(cp.is_file())
        # a second call is a cache hit: no network at all
        with mock.patch.object(tp, "_get", side_effect=AssertionError("network")):
            self.assertEqual(tp.osm_features(bbox, log=lambda *_: None), f)

        # raceways take the same path
        rw = {"elements": [{"type": "way", "id": 5, "tags": {"highway": "raceway", "width": "10 m"},
                            "geometry": [{"lat": 1, "lon": 2}, {"lat": 1.1, "lon": 2}]}]}
        seen.clear()

        def fake_rw(url, timeout=45.0, data=None):
            seen.append(url)
            return b'{"elements": []}' if url == tp.OVERPASS[0] else json.dumps(rw).encode()
        with mock.patch.object(tp, "_get", fake_rw):
            ways = tp.osm_raceways((0.9, 1.9, 1.2, 2.1), log=lambda *_: None)
        self.assertEqual(len(ways), 1)
        self.assertEqual(ways[0]["width_m"], 10.0)
        self.assertEqual(len(seen), 2)

    def test_every_mirror_empty_returns_empty_and_caches_nothing(self):
        tp = self.tp
        seen = []

        def fake_get(url, timeout=45.0, data=None):
            seen.append(url)
            return b'{"elements": []}'
        bbox = (10.0, 10.0, 10.1, 10.1)
        with mock.patch.object(tp, "_get", fake_get):
            f = tp.osm_features(bbox, log=lambda *_: None)
            r = tp.osm_raceways(bbox, log=lambda *_: None)
        self.assertEqual(seen.count(tp.OVERPASS[-1]), 2)    # every mirror was asked
        self.assertTrue(tp._features_empty(f))
        self.assertEqual(r, [])
        self.assertFalse(tp._osm_cache_path(bbox, "features").exists())
        self.assertFalse(tp._osm_cache_path(bbox, "raceway").exists())

    def test_errors_back_off_then_use_a_stale_cache(self):
        tp = self.tp
        bbox = (20.0, 20.0, 20.1, 20.1)
        cp = tp._osm_cache_path(bbox, "features")
        cp.parent.mkdir(parents=True, exist_ok=True)
        stale = tp._features_from_elements(_overpass_doc()["elements"])
        cp.write_text(json.dumps(stale))
        old = time.time() - 90 * 86400
        os.utime(cp, (old, old))
        with mock.patch.object(tp, "_get", side_effect=OSError("504")):
            f = tp.osm_features(bbox, log=lambda *_: None)
        self.assertEqual(f, stale)


@unittest.skipUnless(HAVE_DEPS, "Pillow/numpy not installed")
class DemTests(unittest.TestCase):
    BBOX = (39.2000, -77.9800, 39.2018, -77.9774)          # ~200 x 220 m

    def setUp(self):
        import tempfile
        self.tp = _tp()
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.cache = pathlib.Path(self.td.name) / "cache"
        p = mock.patch.object(self.tp.time, "sleep", lambda *_: None)
        p.start(); self.addCleanup(p.stop)

    def _tiff(self, arr):
        buf = io.BytesIO()
        Image.fromarray(np.asarray(arr, dtype=np.float32), mode="F").save(buf, "TIFF")
        return buf.getvalue()

    def test_bin_round_trip_and_south_first(self):
        tp = self.tp
        cols, rows = 7, 5
        r, c = np.mgrid[0:rows, 0:cols]
        vals = (120.0 + r * 3.7 + c * 0.113).astype(np.float32).reshape(-1)  # grows NORTH
        grid = {"cols": cols, "rows": rows, "bounds": [1.0, 2.0, 1.1, 2.1],
                "cell_m": 3.0, "source": "unit", "values": vals}
        p = pathlib.Path(self.td.name) / "x.dem.bin"
        meta = tp.write_dem_bin(grid, p)
        self.assertEqual(meta["format"], "u16le")
        self.assertEqual(meta["file"], "x.dem.bin")
        self.assertEqual(meta["scale"], 0.05)
        self.assertEqual(p.stat().st_size, cols * rows * 2)
        self.assertEqual(list(pathlib.Path(self.td.name).glob("*.tmp")), [])
        back = tp.read_dem_bin(meta, p)
        self.assertLessEqual(float(np.abs(back - vals).max()), meta["scale"] / 2 + 1e-3)
        # row-major, SOUTH row first: the first row is the lowest here
        self.assertLess(back[0], back[-1])
        self.assertAlmostEqual(float(back[0]), 120.0, delta=0.03)
        raw = np.frombuffer(p.read_bytes(), dtype="<u2")
        self.assertEqual(int(raw[0]), 0)                       # base = min
        # a big range switches to a coarser scale instead of clipping
        grid2 = dict(grid, values=np.linspace(0, 4000, cols * rows).astype(np.float32))
        m2 = tp.write_dem_bin(grid2, p)
        self.assertIn(m2["scale"], (0.1, 0.25))
        b2 = tp.read_dem_bin(m2, p)
        self.assertLessEqual(float(np.abs(b2 - grid2["values"]).max()), m2["scale"] / 2 + 1e-3)

    def test_3dep_is_used_flipped_and_node_aligned(self):
        tp = self.tp
        reqs = []
        W0 = self.BBOX[1]

        def fake_get(url, timeout=45.0, data=None):
            self.assertIn("3DEPElevation", url)
            reqs.append(url)
            q = urllib.parse.parse_qs(url.split("?", 1)[1])
            w, s, e, n = [float(x) for x in q["bbox"][0].split(",")]
            tw, th = [int(x) for x in q["size"][0].split(",")]
            self.assertEqual(q["pixelType"], ["F32"])
            lon_c = w + (np.arange(tw) + 0.5) * (e - w) / tw
            lat_c = n - (np.arange(th) + 0.5) * (n - s) / th      # row 0 = NORTH
            a = ((lon_c[None, :] - W0) * 1e5 + (lat_c[:, None] - 39.2) * 1e5).astype(np.float32)
            return self._tiff(a)
        with mock.patch.object(tp, "_get", fake_get):
            g = tp.dem_hires(self.BBOX, self.cache, log=lambda *_: None)
        self.assertEqual(g["source"], "USGS 3DEP")
        self.assertEqual(len(reqs), 1)
        self.assertEqual(g["values"].dtype, np.float32)
        self.assertEqual(g["values"].size, g["cols"] * g["rows"])
        self.assertGreaterEqual(g["cell_m"], 3.0)
        self.assertLessEqual(g["cols"] * g["rows"], 250_000)
        V = g["values"].reshape(g["rows"], g["cols"])
        s, w, n, e = g["bounds"]
        # node (r, c) is at lat s + (n-s)r/(rows-1): value must match that position
        for r, c in ((0, 0), (0, g["cols"] - 1), (g["rows"] - 1, 0), (g["rows"] // 2, g["cols"] // 3)):
            lat = s + (n - s) * r / (g["rows"] - 1)
            lon = w + (e - w) * c / (g["cols"] - 1)
            self.assertAlmostEqual(float(V[r, c]), (lon - W0) * 1e5 + (lat - 39.2) * 1e5, delta=0.2)
        self.assertLess(V[0, 0], V[-1, 0])                     # south row first
        # cached: the blob is re-used without the network
        self.assertTrue(list((self.cache / "dem3dep").glob("*.tif")))
        with mock.patch.object(tp, "_get", side_effect=AssertionError("network")):
            g2 = tp.dem_hires(self.BBOX, self.cache, log=lambda *_: None)
        self.assertTrue(np.array_equal(g2["values"], g["values"]))

    def test_large_grids_are_tiled_at_2000_px(self):
        tp = self.tp
        reqs = []
        W0 = self.BBOX[1]

        def fake_get(url, timeout=45.0, data=None):
            reqs.append(url)
            q = urllib.parse.parse_qs(url.split("?", 1)[1])
            w, s, e, n = [float(x) for x in q["bbox"][0].split(",")]
            tw, th = [int(x) for x in q["size"][0].split(",")]
            self.assertLessEqual(max(tw, th), 2000)
            lon_c = w + (np.arange(tw) + 0.5) * (e - w) / tw
            return self._tiff(np.tile(((lon_c - W0) * 1e5)[None, :], (th, 1)))
        bbox = (39.2, W0, 39.2004, W0 + 0.05)                  # ~4.3 km x 44 m
        with mock.patch.object(tp, "_get", fake_get):
            g = tp.dem_hires(bbox, self.cache, max_cells=10_000_000, min_cell_m=1.0,
                             log=lambda *_: None)
        self.assertGreater(g["cols"], 2000)
        self.assertEqual(len(reqs), -(-g["cols"] // 2000))
        V = g["values"].reshape(g["rows"], g["cols"])
        want = (np.linspace(W0, W0 + 0.05, g["cols"]) - W0) * 1e5
        self.assertLess(float(np.abs(V[0] - want).max()), 0.5)  # seamless across the tiles

    def test_3dep_failure_falls_back_to_bilinear_terrarium(self):
        tp = self.tp
        seen = []
        z15 = {}

        def fake_get(url, timeout=45.0, data=None):
            seen.append(url)
            if "nationalmap.gov" in url:
                raise OSError("boom")
            self.assertIn("/terrarium/15/", url)
            # elevation 100 + 0.01 * pixel-column inside the tile (a ramp)
            col = np.arange(256, dtype=np.float64)
            v = np.tile(((100.0 + 0.5 * col) + 32768.0)[None, :], (256, 1))
            rgb = np.stack([np.floor(v / 256), np.floor(v) % 256,
                            np.floor((v - np.floor(v)) * 256)], axis=2).astype(np.uint8)
            buf = io.BytesIO()
            Image.fromarray(rgb, "RGB").save(buf, "PNG")
            return buf.getvalue()
        with mock.patch.object(tp, "_get", fake_get):
            g = tp.dem_hires(self.BBOX, self.cache, log=lambda *_: None)
        self.assertEqual(g["source"], "AWS terrarium z15")
        self.assertTrue(any("nationalmap" in u for u in seen))
        V = g["values"]
        self.assertTrue(np.isfinite(V).all())
        self.assertGreaterEqual(float(V.min()), 99.9)
        self.assertLessEqual(float(V.max()), 100.0 + 0.5 * 255 + 0.1)
        # bilinear: a 0.5 m/px ramp sampled at ~3 m cells is NOT stair-stepped
        row = g["values"].reshape(g["rows"], g["cols"])[0]
        self.assertGreater(len(np.unique(np.round(row, 3))), 20)

        # dem_far uses the same machinery (z12 on the fallback)
        seen.clear()

        def fake_get12(url, timeout=45.0, data=None):
            if "nationalmap.gov" in url:
                raise OSError("boom")
            self.assertIn("/terrarium/12/", url)
            return fake_get(url.replace("/12/", "/15/"))
        with mock.patch.object(tp, "_get", fake_get12):
            far = tp.dem_far(39.2, -77.97, half_m=3500.0, cells=64, cache_dir=self.cache,
                             log=lambda *_: None)
        self.assertEqual(far["source"], "AWS terrarium z12")
        self.assertEqual((far["cols"], far["rows"]), (64, 64))
        self.assertAlmostEqual(far["cell_m"], 7000.0 / 63, delta=0.05)

    def test_nodata_is_filled_when_rare_and_refused_when_common(self):
        tp = self.tp
        a = np.full((50, 50), 200.0, dtype=np.float32)
        a[3, 4] = -3.4e38
        out = tp._clean_dem(a, "t")
        self.assertEqual(float(out[3, 4]), 200.0)
        a[:10, :] = -3.4e38
        with self.assertRaises(RuntimeError):
            tp._clean_dem(a, "t")


@unittest.skipUnless(HAVE_DEPS, "Pillow/numpy not installed")
class RefineTests(unittest.TestCase):
    LAT0, LON0, Z = 39.0, -77.0, 18

    def _scene(self, road_offset_m, width_m=10.0, scale=1.0):
        """A straight north-going line 500 m long and a grey road whose middle is
        `road_offset_m` EAST of it, on grass. Returns (line_points, img, bounds)."""
        tp = _tp()
        cols, rows = 8, 6
        x0 = int(tp.lon_to_x(self.LON0, self.Z) // 256) - cols // 2
        y0 = int(tp.lat_to_y(self.LAT0, self.Z) // 256) - rows // 2
        bounds = {"z": self.Z, "x0": x0, "y0": y0,
                  "lon0": tp.x_to_lon(x0 * 256, self.Z), "lat0": tp.y_to_lat(y0 * 256, self.Z),
                  "lon1": tp.x_to_lon((x0 + cols) * 256, self.Z),
                  "lat1": tp.y_to_lat((y0 + rows) * 256, self.Z)}
        img = Image.new("RGB", (cols * 256, rows * 256), (70, 118, 52))
        mpp = tp.metres_per_px(self.LAT0, self.Z)
        px = tp.mosaic_px(bounds, self.LAT0, self.LON0)[0]
        cx = px + road_offset_m / mpp
        from PIL import ImageDraw
        ImageDraw.Draw(img).rectangle([cx - width_m / 2 / mpp, 0, cx + width_m / 2 / mpp,
                                       img.height], fill=(120, 120, 124))
        d = 250.0 / M
        pts = [(self.LAT0 - d + i * (2 * d) / 100, self.LON0) for i in range(101)]
        return pts, img, bounds

    def _east_shift_m(self, pts_in, pts_out):
        n = len(pts_out)
        mid = pts_out[n // 4:3 * n // 4]
        k = M * math.cos(math.radians(self.LAT0))
        return float(np.median([(p[1] - self.LON0) * k for p in mid]))

    def test_line_is_pulled_onto_an_offset_road(self):
        tp = _tp()
        pts, img, bounds = self._scene(3.0)
        out, rep = tp.refine_centreline(pts, img, bounds)
        self.assertTrue(rep["applied"], rep)
        self.assertGreater(rep["ok_frac"], 0.8)
        self.assertAlmostEqual(self._east_shift_m(pts, out), 3.0, delta=0.7)
        self.assertAlmostEqual(rep["max_abs_shift_m"], 3.0, delta=0.9)
        # and the other side, so the sign is not an accident
        pts, img, bounds = self._scene(-2.5)
        out, rep = tp.refine_centreline(pts, img, bounds)
        self.assertAlmostEqual(self._east_shift_m(pts, out), -2.5, delta=0.7)

    def test_a_centred_road_barely_moves_the_line(self):
        tp = _tp()
        pts, img, bounds = self._scene(0.0)
        out, rep = tp.refine_centreline(pts, img, bounds)
        self.assertTrue(rep["applied"])
        self.assertLess(abs(self._east_shift_m(pts, out)), 0.7)
        self.assertLess(rep["mean_abs_shift_m"], 0.7)

    def test_shift_is_clamped_and_blank_imagery_is_not_applied(self):
        tp = _tp()
        pts, img, bounds = self._scene(6.0, width_m=14.0)      # road middle 6 m away
        out, rep = tp.refine_centreline(pts, img, bounds)
        self.assertLessEqual(rep["max_abs_shift_m"], 4.0 + 1e-6)
        pts, _, bounds = self._scene(0.0)
        blank = Image.new("RGB", (2048, 1536), (70, 118, 52))   # no road anywhere
        out, rep = tp.refine_centreline(pts, blank, bounds)
        self.assertFalse(rep["applied"])
        self.assertEqual(rep["max_abs_shift_m"], 0.0)

    def test_measure_width_raw_does_not_change_existing_outputs(self):
        tp = _tp()
        pts, img, bounds = self._scene(0.0)
        line = tp.resample(pts, 2.0)
        a = tp.measure_width(img, bounds, line)
        b = tp.measure_width(img, bounds, line, {"raw": True})
        self.assertNotIn("left_raw", a)
        self.assertEqual(a["left"], b["left"])
        self.assertEqual(a["width"], b["width"])
        self.assertEqual(len(b["left_raw"]), len(line["lat"]))


def _way(i, nodes, pts, **tags):
    return dict({"id": i, "name": None, "width_m": None, "nodes": list(nodes),
                 "points": list(pts)}, **tags)


def _P(k):
    """node k -> a point on a 100 m grid (deterministic coordinates)"""
    return (39.0 + (k // 10) * 100 / M, -77.0 + (k % 10) * 100 / (M * math.cos(math.radians(39))))


class NetworkMergeTests(unittest.TestCase):
    def setUp(self):
        self.tp = _tp()

    def W(self, i, nodes, ids=True, **tags):
        return _way(i, nodes if ids else [], [_P(k) for k in nodes], **tags)

    def test_degree_two_endpoints_merge(self):
        ch = self.tp.merge_chains([self.W(1, [1, 2, 3]), self.W(2, [5, 4, 3])])
        self.assertEqual(len(ch), 1)
        c = ch[0]
        self.assertEqual(c["ids"], [1, 2])
        self.assertEqual(c["points"], [_P(k) for k in (1, 2, 3, 4, 5)])
        self.assertFalse(c["closed"])
        self.assertEqual(c["kind"], "circuit")
        # the merge also works backwards from a later way
        ch = self.tp.merge_chains([self.W(2, [3, 4, 5]), self.W(1, [1, 2, 3])])
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["points"], [_P(k) for k in (1, 2, 3, 4, 5)])
        self.assertEqual(ch[0]["ids"], [1, 2])

    def test_junction_breaks_chains(self):
        ch = self.tp.merge_chains([self.W(1, [1, 2, 3]), self.W(2, [3, 4, 5]),
                                   self.W(3, [3, 6, 7])])
        self.assertEqual(sorted(c["ids"] for c in ch), [[1], [2], [3]])
        # a way passing THROUGH an endpoint (interior occurrence) is a junction too
        ch = self.tp.merge_chains([self.W(1, [1, 2, 3]), self.W(2, [3, 4, 5]),
                                   self.W(3, [6, 3, 7])])
        self.assertEqual(sorted(c["ids"] for c in ch), [[1], [2], [3]])

    def test_kind_change_breaks_chains(self):
        ch = self.tp.merge_chains([self.W(1, [1, 2, 3], name="Main"),
                                   self.W(2, [3, 4, 5], name="Pit Lane"),
                                   self.W(3, [5, 6, 7], name="Pit Lane")])
        by = {tuple(c["ids"]): c for c in ch}
        self.assertEqual(set(by), {(1,), (2, 3)})
        self.assertEqual(by[(2, 3)]["kind"], "pit")
        self.assertEqual(by[(2, 3)]["names"], ["Pit Lane"])

    def test_closed_loop(self):
        ch = self.tp.merge_chains([self.W(1, [1, 2, 3], name="A"), self.W(2, [3, 13, 11], name="B"),
                                   self.W(3, [1, 21, 11], name="A")])
        self.assertEqual(len(ch), 1)
        c = ch[0]
        self.assertTrue(c["closed"])
        self.assertEqual(sorted(c["ids"]), [1, 2, 3])
        self.assertEqual(c["names"], [n for n in c["names"] if n])
        self.assertEqual(sorted(c["names"]), ["A", "B"])
        self.assertEqual(c["points"][0], c["points"][-1])
        self.assertEqual(len(c["points"]), 7)
        # a single closed way, and a loop with a branch at its seam (still closed)
        ch = self.tp.merge_chains([self.W(1, [1, 2, 12, 11, 1])])
        self.assertTrue(ch[0]["closed"])
        ch = self.tp.merge_chains([self.W(1, [1, 2, 12, 11, 1]), self.W(2, [1, 31])])
        self.assertTrue(next(c for c in ch if c["ids"] == [1])["closed"])
        self.assertFalse(next(c for c in ch if c["ids"] == [2])["closed"])

    def test_coordinate_fallback_without_node_ids(self):
        tp = self.tp
        a = self.W(1, [1, 2, 3], ids=False)
        b = self.W(2, [3, 4, 5], ids=False)
        # 0.3 m off: still the same node
        b["points"][0] = (b["points"][0][0] + 0.3 / M, b["points"][0][1])
        ch = tp.merge_chains([a, b])
        self.assertEqual(len(ch), 1)
        self.assertEqual(ch[0]["ids"], [1, 2])
        # 2 m off: not connected
        b["points"][0] = (b["points"][0][0] + 2.0 / M, b["points"][0][1])
        self.assertEqual(len(tp.merge_chains([a, b])), 2)
        # junction by coordinates
        ch = tp.merge_chains([self.W(1, [1, 2, 3], ids=False), self.W(2, [3, 4, 5], ids=False),
                              self.W(3, [6, 3, 7], ids=False)])
        self.assertEqual(len(ch), 3)
        # loop by coordinates
        ch = tp.merge_chains([self.W(1, [1, 2, 12], ids=False), self.W(2, [12, 11, 1], ids=False)])
        self.assertEqual(len(ch), 1)
        self.assertTrue(ch[0]["closed"])

    def test_area_ways_are_their_own_polygons(self):
        ch = self.tp.merge_chains([self.W(1, [1, 2, 12, 11, 1], area="yes", name="Paddock"),
                                   self.W(2, [1, 21, 22])])
        by = {tuple(c["ids"]): c for c in ch}
        self.assertEqual(by[(1,)]["kind"], "area")
        self.assertTrue(by[(1,)]["closed"])
        self.assertEqual(by[(2,)]["kind"], "circuit")

    def test_kind_classification(self):
        k = self.tp.raceway_kind
        sq = [_P(1), _P(2), _P(12), _P(1)]
        cases = [({"name": "Pit Lane"}, "pit"), ({"raceway": "pit_lane"}, "pit"),
                 ({"name": "PITLANE"}, "pit"), ({"name": "Spitfire Straight"}, "circuit"),
                 ({"name": "Kart Track"}, "kart"), ({"raceway": "karting"}, "kart"),
                 ({"name": "Little Thompson Speedway"}, "oval"),
                 ({"name": "Thompson Speedway"}, "oval"), ({"name": "Tri-Oval"}, "oval"),
                 ({"raceway": "oval"}, "oval"),
                 ({"name": "Road Course"}, "circuit"), ({"name": "Shennandoah Circuit"}, "circuit"),
                 ({}, "circuit"), ({"name": "Drifting Course"}, "circuit")]
        for tags, want in cases:
            self.assertEqual(k(dict(tags, points=[_P(1), _P(2)])), want, tags)
        self.assertEqual(k({"area": "yes", "points": sq, "name": "Pit Lane"}), "area")
        self.assertEqual(k({"area": "yes", "points": sq[:3], "name": "Pit Lane"}), "pit")

    def test_width_tag_rides_along_per_point(self):
        ch = self.tp.merge_chains([self.W(1, [1, 2, 3], width_m=10.0), self.W(2, [3, 4, 5])])
        self.assertEqual(ch[0]["wtag"], [10.0, 10.0, 10.0, None, None])

    def test_osm_raceways_keeps_node_ids_and_tags(self):
        import tempfile
        tp = self.tp
        with tempfile.TemporaryDirectory() as td, \
                mock.patch.object(tp, "OSM_CACHE_DIR", pathlib.Path(td)), \
                mock.patch.object(tp.time, "sleep", lambda *_: None):
            g = lambda *ks: [{"lat": _P(k)[0], "lon": _P(k)[1]} for k in ks]  # noqa: E731
            doc = {"elements": [
                {"type": "way", "id": 7, "nodes": [101, 102, 103], "geometry": g(1, 2, 3),
                 "tags": {"highway": "raceway", "name": "Main", "width": "12",
                          "raceway": "track", "oneway": "yes", "sport": "motor"}},
                {"type": "way", "id": 8, "nodes": [103, 104], "geometry": g(3, 4),
                 "tags": {"highway": "raceway", "name": "Main"}},
                {"type": "way", "id": 9, "nodes": [103, 105], "geometry": g(3, 5),
                 "tags": {"highway": "raceway", "name": "Pit Lane"}}]}
            with mock.patch.object(tp, "_get", lambda *a, **k: json.dumps(doc).encode()):
                ways = tp.osm_raceways((38, -78, 40, -76), log=lambda *_: None)
            self.assertEqual(ways[0]["nodes"], [101, 102, 103])
            self.assertEqual((ways[0]["raceway"], ways[0]["oneway"], ways[0]["width_m"],
                              ways[0]["sport"]), ("track", "yes", 12.0, "motor"))
            self.assertIn("area", ways[0])
            self.assertTrue(tp._osm_cache_path((38, -78, 40, -76), "raceway2").is_file())
            self.assertFalse(tp._osm_cache_path((38, -78, 40, -76), "raceway").exists())
            # from the cache (JSON lists, not tuples) into chains: junction at 103
            with mock.patch.object(tp, "_get", side_effect=AssertionError("network")):
                ch = tp.osm_network((38, -78, 40, -76), log=lambda *_: None)
            self.assertEqual(sorted(c["ids"] for c in ch), [[7], [8], [9]])
            self.assertEqual({c["kind"] for c in ch}, {"circuit", "pit"})


@unittest.skipUnless(HAVE_DEPS, "Pillow/numpy not installed")
class NetworkWidthTests(unittest.TestCase):
    def setUp(self):
        self.tp = _tp()
        self.cache = pathlib.Path("/nonexistent-cache")

    def _scene(self, width_m, offset=0.0):
        sc = RefineTests()
        return sc._scene(offset, width_m=width_m)

    def _net(self, chains, img=None, bounds=None, fail=False, **kw):
        tp = self.tp

        def mosaic(bbox, z, cache_dir, **k):
            if fail:
                raise RuntimeError("tiles blocked")
            return img, dict(bounds)
        with mock.patch.object(tp, "imagery_mosaic", mosaic):
            return tp.build_network(chains, self.cache, log=lambda *_: None, **kw)

    @staticmethod
    def _chain(pts, kind="circuit", tag=None):
        return {"ids": [1], "names": ["X"], "kind": kind, "closed": False,
                "points": pts, "wtag": [tag] * len(pts)}

    def test_imagery_width_and_tag_precedence(self):
        pts, img, bounds = self._scene(10.0)
        net = self._net([self._chain(pts)], img, bounds)
        c = net["chains"][0]
        self.assertEqual(c["ws"], "imagery")
        self.assertAlmostEqual(c["w"], 10.0, delta=1.2)
        l, r = np.array(c["hw"]).T
        self.assertAlmostEqual(float(np.median(l)), float(np.median(r)), delta=1.0)
        # traced edges (network v2) BEAT a plausible OSM width tag: a tag is ONE
        # number for a whole way, the imagery is the surface station by station
        self.assertEqual(c.get("edges"), "trace2d")
        net = self._net([self._chain(pts, tag=12.0)], img, bounds)
        c = net["chains"][0]
        self.assertEqual(c["ws"], "imagery")
        self.assertAlmostEqual(c["w"], 10.0, delta=1.2)
        # ...but a tag overrides stations where the imagery is absurd against it
        net = self._net([self._chain(pts, tag=30.0)], img, bounds)
        c = net["chains"][0]
        self.assertEqual((c["ws"], c["w"]), ("osm", 30.0))
        self.assertTrue(all(h == [15.0, 15.0] for h in c["hw"]))
        # a tag on only part of the chain: those stations only
        tags = [30.0] * 30 + [None] * (len(pts) - 30)
        ch = dict(self._chain(pts), wtag=tags)
        c = self._net([ch], img, bounds)["chains"][0]
        self.assertEqual(c["ws"], "imagery")
        self.assertEqual(c["hw"][0], [15.0, 15.0])
        self.assertNotEqual(c["hw"][-1], [15.0, 15.0])

    def test_defaults_when_imagery_fails(self):
        pts, img, bounds = self._scene(10.0)
        chains = [self._chain(pts, k) for k in ("circuit", "oval", "pit", "kart")]
        net = self._net(chains, fail=True, default_circuit_w=11.0)
        got = {c["kind"]: (c["ws"], c["w"], c["refined"]) for c in net["chains"]}
        self.assertEqual(got, {"circuit": ("default", 11.0, False), "oval": ("default", 14.0, False),
                               "pit": ("default", 9.0, False), "kart": ("default", 6.0, False)})
        # no road in the imagery at all: also the default
        blank = Image.new("RGB", img.size, (70, 118, 52))
        c = self._net([self._chain(pts, "pit")], blank, bounds)["chains"][0]
        self.assertEqual((c["ws"], c["w"], c["refined"]), ("default", 9.0, False))
        # and a tag still wins over the default
        c = self._net([self._chain(pts, "pit", tag=7.0)], fail=True)["chains"][0]
        self.assertEqual((c["ws"], c["w"]), ("osm", 7.0))

    def test_imagery_width_is_clamped_per_kind(self):
        # traced widths are MEASURED - only physical limits per kind apply (the
        # v1 7-16 m squeeze is what turned a 12 m circuit into 8 m)
        lim = self.tp.TRACE_WIDTH_CLAMP
        pts, img, bounds = self._scene(12.0)
        c = self._net([self._chain(pts, "kart")], img, bounds)["chains"][0]
        self.assertEqual((c["ws"], c.get("edges")), ("imagery", "trace2d"))
        self.assertAlmostEqual(c["w"], 12.0, delta=1.2)          # a 12 m kart track is 12 m
        self.assertTrue(all(l + r <= lim["kart"][1] + 0.02 for l, r in c["hw"]))
        pts, img, bounds = self._scene(5.0)
        c = self._net([self._chain(pts, "circuit")], img, bounds)["chains"][0]
        self.assertAlmostEqual(c["w"], lim["circuit"][0], delta=0.05)    # circuit min
        c = self._net([self._chain(pts, "pit")], img, bounds)["chains"][0]
        self.assertAlmostEqual(c["w"], 5.0, delta=1.0)           # a 5 m pit lane is 5 m

    def test_refined_centreline_and_short_chains_and_cap(self):
        pts, img, bounds = self._scene(10.0, offset=3.0)
        short = self._chain([pts[0], (pts[0][0] + 10 / M, pts[0][1])])
        net = self._net([self._chain(pts), short], img, bounds)
        self.assertEqual(len(net["chains"]), 1)                  # < 15 m dropped
        c = net["chains"][0]
        self.assertTrue(c["refined"])
        k = M * math.cos(math.radians(39.0))
        mid = c["p"][len(c["p"]) // 4: 3 * len(c["p"]) // 4]
        self.assertAlmostEqual(float(np.median([(p[1] + 77.0) * k for p in mid])), 3.0, delta=0.8)
        # the open ends stay pinned (they meet other chains at junctions)
        self.assertLess(abs((c["p"][0][1] + 77.0) * k), 0.5)
        # the total-length cap keeps the chains nearest the circuit
        far = [(a + 0.01, b) for a, b in pts]
        net = self._net([self._chain(far), self._chain(pts)], img, bounds,
                        centre=[39.0, -77.0], max_total_m=600.0, imagery=False)
        self.assertEqual(len(net["chains"]), 1)
        self.assertLess(abs(net["chains"][0]["p"][0][0] - pts[0][0]), 0.001)


@unittest.skipUnless(HAVE_DEPS, "Pillow/numpy not installed")
class GroundImageTests(unittest.TestCase):
    @staticmethod
    def fake_mosaic(bbox, z, cache_dir, **kw):
        """What imagery_mosaic returns: whole tiles, exact Mercator bounds, and
        a noisy picture (so the JPEG has something to compress)."""
        tp = _tp()
        s, w, n, e = bbox
        x0, x1 = int(tp.lon_to_x(w, z) // 256), int(tp.lon_to_x(e, z) // 256)
        y0, y1 = int(tp.lat_to_y(n, z) // 256), int(tp.lat_to_y(s, z) // 256)
        nx, ny = x1 - x0 + 1, y1 - y0 + 1
        rng = np.random.default_rng(1)
        a = rng.integers(60, 140, size=(ny * 256, nx * 256, 3), dtype=np.uint8)
        return Image.fromarray(a, "RGB"), {
            "z": z, "x0": x0, "y0": y0,
            "lon0": tp.x_to_lon(x0 * 256, z), "lat0": tp.y_to_lat(y0 * 256, z),
            "lon1": tp.x_to_lon((x1 + 1) * 256, z), "lat1": tp.y_to_lat((y1 + 1) * 256, z)}

    def test_zoom_crop_bounds_and_size_cap(self):
        import tempfile
        tp = _tp()
        bbox = tp._expand_bbox((42.33, -76.93, 42.345, -76.92), 250.0)   # ~2.2 x 1.3 km
        with mock.patch.object(tp, "imagery_mosaic", self.fake_mosaic):
            img, b, z = tp.ground_image(bbox, pathlib.Path("/nonexistent"), log=lambda *_: None)
        self.assertLessEqual(max(img.size), tp.GROUND_MAX_PX)
        # the highest zoom that fits: one more would not
        s, w, n, e = bbox
        self.assertGreater(max(tp.lon_to_x(e, z + 1) - tp.lon_to_x(w, z + 1),
                               tp.lat_to_y(s, z + 1) - tp.lat_to_y(n, z + 1)), tp.GROUND_MAX_PX)
        # cropped to (just over) the requested box, not whole tiles
        self.assertLessEqual(b["south"], s); self.assertLessEqual(b["west"], w)
        self.assertGreaterEqual(b["north"], n); self.assertGreaterEqual(b["east"], e)
        px_m = tp.metres_per_px((s + n) / 2, z)
        self.assertLess((b["east"] - e) * M * math.cos(math.radians(n)), 2 * px_m)
        self.assertLess((s - b["south"]) * M, 2 * px_m)
        # mapping is consistent: the box corners land on the crop's corners
        self.assertAlmostEqual((tp.lon_to_x(b["east"], z) - tp.lon_to_x(b["west"], z)),
                               img.size[0], delta=0.01)
        self.assertAlmostEqual((tp.lat_to_y(b["south"], z) - tp.lat_to_y(b["north"], z)),
                               img.size[1], delta=0.01)
        # the JPEG writer respects the byte cap (shrinking if it must)
        with tempfile.TemporaryDirectory() as td:
            p = pathlib.Path(td) / "g.ground.jpg"
            size, q, nbytes = tp._save_jpeg_capped(img, p, max_bytes=400_000)
            self.assertLessEqual(nbytes, 400_000)
            self.assertEqual(p.stat().st_size, nbytes)
            with Image.open(p) as im:
                self.assertEqual(im.size, tuple(size))
            self.assertEqual([x.name for x in pathlib.Path(td).iterdir()], ["g.ground.jpg"])


@unittest.skipUnless(HAVE_DEPS, "Pillow/numpy not installed")
class EnrichTests(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tp = _tp()
        self.td = tempfile.TemporaryDirectory()
        self.addCleanup(self.td.cleanup)
        self.d = pathlib.Path(self.td.name)
        self.cache = self.d / "cache"
        sc = RefineTests()
        sc.LAT0, sc.LON0, sc.Z = RefineTests.LAT0, RefineTests.LON0, RefineTests.Z
        pts, img, bounds = sc._scene(3.0)
        small = img.resize((img.width // 2, img.height // 2), Image.LANCZOS)   # downscaled
        small.save(self.d / "e.jpg", quality=92)
        lons = [p[1] for p in pts]
        self.tex = {"file": "e.jpg", "px": list(small.size),
                    "bounds": {"south": bounds["lat1"], "north": bounds["lat0"],
                               "west": bounds["lon0"], "east": bounds["lon1"]}}
        line = self.tp.resample(pts, 2.0)
        self.n = len(line["lat"])
        self.asset = {
            "track": "E", "slug": "e", "prep_version": 2, "centre": [39.0, -77.0],
            "source": {"line": "x", "dem": "AWS terrarium z14"},
            "bbox": [min(p[0] for p in pts), min(lons), max(p[0] for p in pts), max(lons)],
            "line": [[float(a), float(b), 100.0] for a, b in zip(line["lat"], line["lon"])],
            "width_m": [10.0] * self.n, "texture": self.tex,
            "dem": {"cols": 2, "rows": 2, "bounds": [38.99, -77.01, 39.01, -76.99],
                    "values": [100.0] * 4}}
        (self.d / "e.json").write_text(json.dumps(self.asset))
        self.calls = {"feat": 0, "hr": 0, "far": 0}
        tp = self.tp

        def feat(bbox, **kw):
            self.calls["feat"] += 1
            return tp._features_from_elements(_overpass_doc()["elements"])

        def hires(bbox, cache_dir, **kw):
            self.calls["hr"] += 1
            cols, rows = 30, 40
            r, c = np.mgrid[0:rows, 0:cols]
            return {"cols": cols, "rows": rows, "bounds": list(bbox), "cell_m": 4.0,
                    "source": "USGS 3DEP",
                    "values": (200.0 + r * 1.0).astype(np.float32).reshape(-1)}   # +1 m / row north

        def far(lat, lon, **kw):
            self.calls["far"] += 1
            return {"cols": 8, "rows": 8, "bounds": [lat - 0.03, lon - 0.04, lat + 0.03, lon + 0.04],
                    "cell_m": 900.0, "source": "AWS terrarium z12",
                    "values": np.linspace(100, 300, 64).astype(np.float32)}
        self.scene_img, self.scene_bounds, self.scene_pts = img, bounds, pts
        self.net_calls = {"osm": 0, "img": 0}

        def network(bbox, **kw):
            self.net_calls["osm"] += 1
            east = 12.0 / (M * math.cos(math.radians(39.0)))
            sq = [(39.0005, -76.9995), (39.0005, -76.9993), (39.0007, -76.9993),
                  (39.0007, -76.9995), (39.0005, -76.9995)]
            return [
                {"ids": [1, 2], "names": ["Main"], "kind": "circuit", "closed": False,
                 "points": list(pts), "wtag": [None] * len(pts)},
                {"ids": [3], "names": ["Pit Lane"], "kind": "pit", "closed": False,
                 "points": [(a, b + east) for a, b in pts[20:60]],
                 "wtag": [8.0] * 40},
                {"ids": [4], "names": [], "kind": "circuit", "closed": False,   # 4 m: dropped
                 "points": [(39.0, -77.0), (39.0 + 4 / M, -77.0)], "wtag": [None, None]},
                {"ids": [5], "names": ["Paddock"], "kind": "area", "closed": True,
                 "points": sq, "wtag": [None] * 5}]

        def mosaic(bbox, z, cache_dir, **kw):
            self.net_calls["img"] += 1
            return self.scene_img, dict(self.scene_bounds)
        for name, fn in (("osm_features", feat), ("dem_hires", hires), ("dem_far", far),
                         ("osm_network", network), ("imagery_mosaic", mosaic)):
            p = mock.patch.object(tp, name, fn)
            p.start()
            self.addCleanup(p.stop)
        p = mock.patch.object(tp.time, "sleep", lambda *_: None)
        p.start(); self.addCleanup(p.stop)

    def _east_shift(self, a):
        k = M * math.cos(math.radians(39.0))
        return float(np.median([(p[1] + 77.0) * k for p in a["line"]]))

    def test_enrich_never_resurrects_or_merges_into_a_rebake(self):
        """A forced re-prepare deletes the asset; a re-bake replaces it. An
        enrichment that was mid-run must not write the OLD asset back, nor
        merge its line/terrain into the new one."""
        tp = self.tp
        ap = self.d / "e.json"
        real_steps = tp._enrich_steps

        def rebake_during(asset, adir, cache_dir, log, **kw):
            out = real_steps(asset, adir, cache_dir, log, **kw)
            newer = dict(self.asset, generated=self.asset.get("generated", 0) + 100,
                         track="E re-baked")
            ap.write_text(json.dumps(newer))
            return out
        with mock.patch.object(tp, "_enrich_steps", rebake_during):
            self.assertIsNone(tp.enrich_asset(ap, self.cache, log=lambda *_: None))
        got = json.loads(ap.read_text())
        self.assertEqual(got["track"], "E re-baked")
        self.assertNotIn("enrich", got, "nothing from the stale run merged in")

        ap.write_text(json.dumps(self.asset))

        def delete_during(asset, adir, cache_dir, log, **kw):
            out = real_steps(asset, adir, cache_dir, log, **kw)
            ap.unlink()
            return out
        with mock.patch.object(tp, "_enrich_steps", delete_during):
            self.assertIsNone(tp.enrich_asset(ap, self.cache, log=lambda *_: None))
        self.assertFalse(ap.exists(), "a deleted asset stays deleted")

    def test_enrich_adds_everything_and_is_idempotent_and_atomic(self):
        tp = self.tp
        quiet = lambda *_: None
        res = tp.enrich_asset(self.d / "e.json", self.cache, log=quiet)
        self.assertIsNotNone(res)
        self.assertEqual(len(res["line"]), len(res["width_m"]))
        self.assertEqual(len(res["line"]), self.n)
        # features
        self.assertEqual(len(res["features"]["buildings"]), 2)
        # refinement from the (downscaled) texture: the road is 3 m east
        rep = res["centreline_refine"]
        self.assertTrue(rep["applied"], rep)
        self.assertAlmostEqual(self._east_shift(res), 3.0, delta=0.8)
        # bins next to the json
        self.assertEqual(res["dem_hr"]["file"], "e.dem.bin")
        self.assertEqual(res["dem_far"]["file"], "e.demfar.bin")
        self.assertTrue((self.d / "e.dem.bin").is_file())
        self.assertTrue((self.d / "e.demfar.bin").is_file())
        self.assertEqual(res["dem_hr"]["format"], "u16le")
        # station elevations now come from the hi-res grid (200..239 m, rising north)
        el = [p[2] for p in res["line"]]
        self.assertTrue(all(150 < e < 260 for e in el), (min(el), max(el)))
        self.assertGreater(el[-1], el[0])
        self.assertEqual(res["line_elev_source"], "USGS 3DEP")
        self.assertIn("USGS 3DEP", res["source"]["dem"])
        e = res["enrich"]
        self.assertEqual(e["v"], tp.ENRICH_VERSION)
        self.assertEqual(tp.ENRICH_VERSION, 3)
        self.assertTrue(e["features"] and e["refined"])
        self.assertEqual(e["network"], 3)
        # the network: schema
        net = res["network"]
        self.assertEqual(net["v"], tp.NETWORK_V)
        self.assertIn("OpenStreetMap", net["attrib"])
        self.assertEqual([c["kind"] for c in net["chains"]], ["circuit", "pit", "area"])
        for c in net["chains"]:
            base = {"ids", "names", "kind", "closed", "p", "hw", "w", "ws", "refined"}
            # a chain whose edges were traced in 2-D says so (and how sure it is)
            self.assertIn(set(c), (base, base | {"edges", "conf"}))
            self.assertIn(c["ws"], ("osm", "imagery", "default"))
            places = 7 if c.get("edges") == "trace2d" else 6
            for p in c["p"]:
                self.assertEqual(len(p), 2)
                self.assertEqual(p[0], round(p[0], places))
            if c["kind"] == "area":
                self.assertEqual((c["hw"], c["w"], c["closed"]), ([], 0, True))
                continue
            self.assertEqual(len(c["p"]), len(c["hw"]))
            steps = [tp._dist_m(a, b) for a, b in zip(c["p"], c["p"][1:])]
            self.assertAlmostEqual(float(np.median(steps)), 3.0, delta=0.3)
            for l, r in c["hw"]:          # traced edges carry cm, v1 widths dm
                self.assertEqual(l, round(l, 2 if c.get("edges") == "trace2d" else 1))
        main, pit = net["chains"][0], net["chains"][1]
        self.assertEqual((main["ws"], main["names"], main["ids"]), ("imagery", ["Main"], [1, 2]))
        self.assertAlmostEqual(main["w"], 10.0, delta=1.5)
        self.assertTrue(main["refined"])
        self.assertEqual((pit["ws"], pit["w"]), ("osm", 8.0))
        self.assertTrue(all(h == [4.0, 4.0] for h in pit["hw"]))
        # the ground image: schema + the file
        g = res["ground"]
        self.assertEqual(g["file"], "e.ground.jpg")
        self.assertEqual(set(g["bounds"]), {"south", "north", "west", "east"})
        self.assertIn("Esri", g["attrib"])
        self.assertEqual(g["attrib"], self.asset["texture"].get("attrib", tp.ESRI_ATTRIB))
        self.assertEqual(g["cover"], "network")
        self.assertLessEqual(g["z"], 18)
        with Image.open(self.d / "e.ground.jpg") as im:
            self.assertEqual(list(im.size), g["px"])
            self.assertLessEqual(max(im.size), tp.GROUND_MAX_PX)
        b = g["bounds"]
        self.assertLess(b["south"], b["north"])
        self.assertLess(b["west"], b["east"])   # (containment: GroundImageTests)
        self.assertEqual(e["dem_hr"], "USGS 3DEP")
        # on disk is the same, nothing temporary left behind
        on_disk = json.loads((self.d / "e.json").read_text())
        self.assertEqual(on_disk["enrich"]["v"], tp.ENRICH_VERSION)
        self.assertEqual([p.name for p in self.d.glob("*.tmp")], [])
        self.assertEqual([p.name for p in self.d.glob("*.tmp*")], [])
        # idempotent: nothing left to do, nothing fetched
        before, nbefore = dict(self.calls), dict(self.net_calls)
        self.assertIsNone(tp.enrich_asset(self.d / "e.json", self.cache, log=quiet))
        self.assertEqual(self.calls, before)
        self.assertEqual(self.net_calls, nbefore)
        # a deleted ground image is regenerated without refetching the network
        (self.d / "e.ground.jpg").unlink()
        res = tp.enrich_asset(self.d / "e.json", self.cache, log=quiet)
        self.assertTrue((self.d / "e.ground.jpg").is_file())
        self.assertEqual(self.net_calls["osm"], nbefore["osm"])

    def test_enrich_missing_v2(self):
        tp = self.tp
        res = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
        self.assertFalse(tp._enrich_missing(res, self.d))
        a = dict(res)
        a.pop("network")
        self.assertTrue(tp._enrich_missing(a, self.d))
        a = dict(res, network={"v": 1, "chains": []})
        self.assertTrue(tp._enrich_missing(a, self.d))
        a = dict(res)
        a.pop("ground")
        self.assertTrue(tp._enrich_missing(a, self.d))
        (self.d / "e.ground.jpg").unlink()
        self.assertTrue(tp._enrich_missing(res, self.d))
        # a v1 asset (complete for v1) is upgraded by enrich_asset: only the new steps run
        v1 = dict(res, enrich=dict(res["enrich"], v=1))
        for k in ("network", "ground"):
            v1.pop(k)
        (self.d / "e.json").write_text(json.dumps(v1))
        before = dict(self.calls)
        up = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
        self.assertEqual(up["enrich"]["v"], tp.ENRICH_VERSION)
        self.assertEqual(self.calls, before)

    def test_a_v1_network_is_rebuilt_with_traced_edges(self):
        """Network v1 widths were a squeezed near-constant; an asset carrying one
        must be re-enriched (rebuilt), never kept forever as 'complete'."""
        tp = self.tp
        res = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
        self.assertFalse(tp._network_stale(res))
        self.assertFalse(tp._enrich_missing(res, self.d))
        old = json.loads(json.dumps(res))
        old["network"]["v"] = 1
        old["enrich"]["v"] = 2
        for ch in old["network"]["chains"]:
            ch.pop("edges", None)
            ch.pop("conf", None)
        self.assertTrue(tp._network_stale(old))
        self.assertTrue(tp._enrich_missing(old, self.d))
        (self.d / "e.json").write_text(json.dumps(old))
        osm_before, before = self.net_calls["osm"], dict(self.calls)
        up = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
        self.assertEqual(up["network"]["v"], tp.NETWORK_V)
        self.assertEqual(up["enrich"]["v"], tp.ENRICH_VERSION)
        self.assertEqual(self.net_calls["osm"], osm_before + 1)     # the network, rebuilt
        self.assertEqual(self.calls, before)                        # nothing else refetched
        main = up["network"]["chains"][0]
        self.assertEqual(main.get("edges"), "trace2d")

    def test_network_failure_is_recorded_and_rate_limited(self):
        tp = self.tp
        with mock.patch.object(tp, "osm_network", return_value=[]) as m:
            res = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
            self.assertIn("network_error", res)
            self.assertNotIn("network", res)
            self.assertEqual(res["enrich"]["v"], 0)
            # the ground still came from the line's bbox
            self.assertEqual(res["ground"]["cover"], "line")
            self.assertIsNone(tp.enrich_asset(self.d / "e.json", self.cache,
                                              log=lambda *_: None))
            self.assertEqual(m.call_count, 1)
        a = json.loads((self.d / "e.json").read_text())
        a["network_tried"] = int(time.time()) - 7 * 3600
        (self.d / "e.json").write_text(json.dumps(a))
        res = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
        self.assertEqual(res["enrich"]["v"], tp.ENRICH_VERSION)
        self.assertNotIn("network_error", res)
        self.assertEqual(res["ground"]["cover"], "network")      # remade for the network

    def test_a_missing_bin_is_regenerated(self):
        tp = self.tp
        quiet = lambda *_: None
        tp.enrich_asset(self.d / "e.json", self.cache, log=quiet)
        (self.d / "e.demfar.bin").unlink()
        res = tp.enrich_asset(self.d / "e.json", self.cache, log=quiet)
        self.assertIsNotNone(res)
        self.assertTrue((self.d / "e.demfar.bin").is_file())
        self.assertEqual(self.calls["feat"], 1)               # features were not refetched

    def test_offline_mode_never_touches_the_network(self):
        tp = self.tp
        with mock.patch.object(tp, "_get", side_effect=AssertionError("network")):
            res = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None,
                                  network=False)
        self.assertEqual(self.calls, {"feat": 0, "hr": 0, "far": 0})
        self.assertIsNotNone(res)                             # the offline refinement ran
        self.assertTrue(res["centreline_refine"]["applied"])
        self.assertNotIn("features", res)
        self.assertEqual(res["enrich"]["v"], 0)               # not finished
        self.assertEqual(len(res["line"]), len(res["width_m"]))
        # a later online call finishes the job without refining twice
        shift = self._east_shift(res)
        res2 = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
        self.assertEqual(res2["enrich"]["v"], tp.ENRICH_VERSION)
        self.assertAlmostEqual(self._east_shift(res2), shift, delta=0.05)

    def test_one_failing_step_does_not_stop_the_others_and_retry_is_rate_limited(self):
        tp = self.tp
        with mock.patch.object(tp, "osm_features", side_effect=RuntimeError("504")) as m:
            res = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
            self.assertIn("features_error", res)
            self.assertNotIn("features", res)
            self.assertIn("dem_hr", res)                      # DEM + refine still happened
            self.assertTrue(res["centreline_refine"]["applied"])
            self.assertEqual(res["enrich"]["v"], 0)
            self.assertEqual(m.call_count, 1)
            # within 6 h: not retried
            self.assertIsNone(tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None))
            self.assertEqual(m.call_count, 1)
        # after the back-off it retries and completes
        a = json.loads((self.d / "e.json").read_text())
        a["features_tried"] = int(time.time()) - 7 * 3600
        (self.d / "e.json").write_text(json.dumps(a))
        res = tp.enrich_asset(self.d / "e.json", self.cache, log=lambda *_: None)
        self.assertEqual(res["enrich"]["v"], tp.ENRICH_VERSION)
        self.assertNotIn("features_error", res)

    def test_build_asset_enriches_a_fresh_bake(self):
        tp = self.tp
        line_pts = [(39.0 + i * 0.00004, -77.0) for i in range(400)]
        img = Image.new("RGB", (512, 512), (70, 118, 52))
        from PIL import ImageDraw
        ImageDraw.Draw(img).rectangle([252, 0, 260, 256], fill=(96, 97, 100))
        bounds = {"z": 18, "x0": 0, "y0": 0, "lon0": -77.01, "lat0": 39.01,
                  "lon1": -76.99, "lat1": 38.99}
        grid = {"cols": 3, "rows": 3, "bounds": [38.99, -77.01, 39.01, -76.99],
                "values": [180.0] * 9}
        with mock.patch.object(tp, "imagery_mosaic", return_value=(img, bounds)), \
             mock.patch.object(tp, "dem_elevations", side_effect=lambda pts, *a, **k:
                               [181.0] * len(list(pts))), \
             mock.patch.object(tp, "dem_grid", return_value=grid):
            out = self.d / "bake"
            asset = tp.build_asset("Bake", line_pts, out, {"line_source": "x"},
                                   log=lambda *_: None)
        self.assertIn("centreline_refine", asset)
        self.assertEqual(asset["enrich"]["v"], tp.ENRICH_VERSION)
        self.assertTrue((out / "tracks/bake.dem.bin").is_file())
        self.assertTrue((out / "tracks/bake.demfar.bin").is_file())
        self.assertEqual(len(asset["line"]), len(asset["width_m"]))
        self.assertEqual(json.loads((out / "tracks/bake.json").read_text())["enrich"]["v"],
                         tp.ENRICH_VERSION)
        self.assertTrue(asset["network"]["chains"])
        self.assertTrue((out / "tracks" / asset["ground"]["file"]).is_file())
        # no features/dem on a re-enrich: complete already
        self.assertIsNone(tp.enrich_asset(out / "tracks/bake.json", self.cache,
                                          log=lambda *_: None))


if __name__ == "__main__":
    unittest.main()

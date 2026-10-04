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
import json
import math
import pathlib
import unittest
from unittest import mock

try:
    import numpy as np
    from PIL import Image
    HAVE_DEPS = True
except Exception:                       # pragma: no cover
    HAVE_DEPS = False

ROOT = pathlib.Path(__file__).resolve().parents[1]


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
                                       {"osm_width_m": 10.0, "line_source": "osm:1"})
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

                # without a tag the imagery estimate IS the width
                asset2 = tp.build_asset("Test Track", line_pts, pathlib.Path(td),
                                        {"line_source": "session"})
                self.assertEqual(asset2["width_source"], "imagery")
                self.assertAlmostEqual(asset2["width_m"][0], 12.5, delta=0.01)

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


if __name__ == "__main__":
    unittest.main()

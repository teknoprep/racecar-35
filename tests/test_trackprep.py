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
                if abs(dy) <= half_px:
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

    def test_no_track_falls_back_instead_of_inventing_one(self):
        tp = _tp()
        img, bounds, _, mpp = self._scene("flat")
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


@unittest.skipUnless(HAVE_DEPS, "Pillow/numpy not installed")
class AssetTests(unittest.TestCase):
    def test_asset_prefers_an_osm_width_tag_and_records_agreement(self):
        """build_asset must publish the surveyed tag, keep the imagery estimate
        as a cross-check, and never invent an asset without geometry."""
        tp = _tp()
        import numpy as np
        line_pts = [(39.0 + i * 0.00004, -77.0) for i in range(400)]
        fake_img = Image.new("RGB", (512, 512), (70, 118, 52))
        bounds = {"z": 18, "x0": 0, "y0": 0, "lon0": -77.2, "lat0": 39.2,
                  "lon1": -76.8, "lat1": 38.8}
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

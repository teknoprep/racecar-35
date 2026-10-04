"""Lap detection on the server (server/app/main.py: _detect_laps / _apply_lap_meta).

The Teensy stamps `"lap": 0` from REC until the first start/finish crossing. That
segment begins wherever recording started - the pits, or mid-track - so it is an
out-lap, never a timed lap. It used to be reported as lap 1 and, being SHORTER
than a real lap, became the session's best (a 1:39 "best" at Watkins Glen over
1:46 laps), which the sessions list, the review page and the 3D view all showed.
"""
import importlib
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _main():
    os.environ.setdefault("RACECAR_DATA_DIR", tempfile.mkdtemp(prefix="rc-laps-"))
    sys.path.insert(0, str(ROOT / "server"))
    return importlib.import_module("app.main")


try:
    import fastapi  # noqa: F401
    HAVE_SERVER = True
except Exception:                       # pragma: no cover
    HAVE_SERVER = False


def _session(lap_secs, out_secs=60.0, hz=5.0):
    """Samples: an out-lap (lap 0) then laps 1..n with the given durations."""
    rows, t, lap = [], 0.0, 0
    for dur in [out_secs] + list(lap_secs):
        n = int(dur * hz)
        for _ in range(n):
            rows.append({"t": 1789000000.0 + t, "lat": 42.34, "lon": -76.92,
                         "speed_mph": 80.0, "lap": lap})
            t += 1.0 / hz
        lap += 1
    # the stream ends just after the last crossing
    rows.append({"t": 1789000000.0 + t, "lat": 42.34, "lon": -76.92,
                 "speed_mph": 80.0, "lap": lap})
    return rows


@unittest.skipUnless(HAVE_SERVER, "server dependencies not installed")
class OutLapTests(unittest.TestCase):
    def test_out_lap_is_flagged_and_never_the_best(self):
        m = _main()
        info = m._detect_laps(_session([106.0, 108.0, 107.0], out_secs=99.0))
        laps = info["laps"]
        self.assertEqual([lp["lap"] for lp in laps], [1, 2, 3, 4],
                         "numbering is unchanged, so stored exclusions still match")
        self.assertTrue(laps[0].get("out_lap"))
        self.assertFalse(any(lp.get("out_lap") for lp in laps[1:]))
        self.assertEqual(info["best_lap"], 2)

    def test_meta_excludes_the_out_lap_unless_the_driver_includes_it(self):
        m = _main()
        rows = _session([106.0, 108.0], out_secs=99.0)
        payload = m._apply_lap_meta(m._detect_laps(rows), "nobody", "x.ndjson")
        self.assertEqual([lp["lap"] for lp in payload["laps"]], [2, 3])
        self.assertEqual(payload["excluded_laps"][0]["excluded_reason"], "out lap")
        self.assertEqual(payload["best_lap"], 2)

    def test_a_stream_that_starts_on_a_counted_lap_has_no_out_lap(self):
        # stamped 3, 4, 5 (a resumed/combined file): every segment begins at a
        # crossing the Teensy counted, so nothing is flagged
        m = _main()
        rows = _session([100.0, 101.0], out_secs=102.0)
        for r in rows:
            r["lap"] += 3
        info = m._detect_laps(rows)
        self.assertFalse(any(lp.get("out_lap") for lp in info["laps"]))
        self.assertEqual(info["best_lap"], 2)

    def test_combined_file_flags_its_second_out_lap_and_the_reset(self):
        # two recordings concatenated: 0,1,2 then 0,1,2 again
        m = _main()
        a = _session([106.0, 107.0], out_secs=90.0)
        b = _session([105.0, 109.0], out_secs=95.0)
        t_end = a[-1]["t"]
        a = a[:-1]                          # file 1 ends mid-lap 2
        for r in b:
            r["t"] += t_end - b[0]["t"] + 600.0   # ten minutes in the paddock
        info = m._detect_laps(a + b)
        flags = [(lp["lap"], bool(lp.get("out_lap")), bool(lp.get("partial")))
                 for lp in info["laps"]]
        self.assertEqual(flags, [(1, True, False), (2, False, False),
                                 (3, False, True), (4, True, False),
                                 (5, False, False), (6, False, False)])
        self.assertEqual(info["best_lap"], 5)

    def test_lap_zero_throughout_falls_back_to_line_crossing_unflagged(self):
        # an S/F line the car never crossed: every row stamped lap 0. The
        # server's line-crossing fallback finds the laps, and they are REAL laps
        m = _main()
        import math
        rows, t = [], 0.0
        for lap in range(4):
            for k in range(500):                       # 100 s a lap at 5 Hz
                a = 2 * math.pi * k / 500
                rows.append({"t": 1789000000.0 + t, "lat": 42.34 + 0.003 * math.sin(a),
                             "lon": -76.92 + 0.004 * (1 - math.cos(a)),
                             "speed_mph": 80.0 + lap, "heading_deg": 0.0, "lap": 0})
                t += 0.2
        info = m._detect_laps(rows)
        self.assertEqual(info["source"], "line_crossing")
        self.assertGreaterEqual(len(info["laps"]), 3)
        self.assertFalse(any(lp.get("out_lap") or lp.get("partial") for lp in info["laps"]))
        self.assertIsNotNone(info["best_lap"])


@unittest.skipUnless(HAVE_SERVER, "server dependencies not installed")
class FixPayloadTests(unittest.TestCase):
    """/data?fixes=1 - repeated GPS rows dropped BEFORE thinning, two passes."""

    def _write(self, rows, extra_lines=()):
        import json
        fd, name = tempfile.mkstemp(suffix=".ndjson")
        with os.fdopen(fd, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
            for x in extra_lines:
                f.write(x + "\n")
        self.addCleanup(os.unlink, name)
        return pathlib.Path(name)

    @staticmethod
    def _naive(rows):
        """the viewer's RC3D.cleanFixes rule, written out plainly"""
        out, last = [], None
        for r in rows:
            lat, lon = r.get("lat"), r.get("lon")
            if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
                out.append(r)
                continue
            spd = r.get("speed_mph")
            if (last is not None and isinstance(spd, (int, float)) and lat == last["lat"]
                    and lon == last["lon"] and spd == last.get("speed_mph")):
                continue
            out.append(r)
            last = r
        return out

    def test_repeats_dropped_like_the_viewer_then_strided(self):
        m = _main()
        rows, t = [], 1789000000.0
        for k in range(3000):
            fix = {"t": t, "lat": 42.3 + k * 1e-5, "lon": -76.9, "speed_mph": 60.0 + k % 7}
            rows.append(fix)
            t += 0.04
            if k % 3:                                  # the logger's repeat row
                rows.append(dict(fix, t=t))
                t += 0.001
            if k % 500 == 0:                           # a row with no fix
                rows.append({"t": t, "rpm": 4000})
            if k % 700 == 0:                           # no speed: never a repeat
                rows.append({"t": t, "lat": fix["lat"], "lon": fix["lon"], "speed_mph": None})
        p = self._write(rows, extra_lines=["", "not json", "[1,2]"])
        want = self._naive(rows)
        full = m._session_fix_payload(p, 0)
        self.assertEqual(full["samples"], want)
        self.assertEqual(full["total"], len(want))      # blank / non-object lines skipped
        thin = m._session_fix_payload(p, 1000)
        self.assertLessEqual(thin["count"], 1000)
        self.assertEqual(thin["samples"][0], want[0], "the first row is always kept")
        self.assertEqual(thin["samples"], want[::thin["stride"]])
        self.assertTrue(thin["fixes"])


if __name__ == "__main__":
    unittest.main()

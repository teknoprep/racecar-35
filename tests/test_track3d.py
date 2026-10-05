"""Host tests for the /track3d data-only driving view (server/app/main.py).

The view is pure geometry + time maths wrapped in a thin three.js drawing layer,
so the maths is extracted from the served page and driven here with synthetic and
realistic inputs. That is the only way to check this without a browser: a wrong
spline LUT, a non-monotonic time->distance map or a flipped bank sign all render
"fine" and are invisible in a screenshot review.

Skipped when node is unavailable. The spline branch additionally needs three.js
(installed by the test only if it can be found); without it the smoothing/no-spline
path is still fully exercised, which is the fallback the page itself uses.
"""
import ast
import json
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
NODE = shutil.which("node")
# Optional: a real three.js so the spline branch is exercised too. Install with
#   mkdir -p /home/chris/racecar-tools/threejs-test && cd $_ && npm i three@0.160.0
THREE_CANDIDATES = [
    ROOT / "node_modules/three/build/three.module.js",
    pathlib.Path("/home/chris/racecar-tools/threejs-test/node_modules/three/build/three.module.js"),
]


def _page_html() -> str:
    """The real _TRACK3D_HTML constant, evaluated out of the server module."""
    tree = ast.parse((ROOT / "server/app/main.py").read_text())

    def ev(node, ns):
        if isinstance(node, ast.Constant):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            return ev(node.left, ns) + ev(node.right, ns)
        if isinstance(node, ast.Name):
            return ns[node.id]
        raise ValueError(ast.dump(node))

    ns = {}
    for _ in range(6):
        for n in tree.body:
            if isinstance(n, ast.Assign) and len(n.targets) == 1 and isinstance(n.targets[0], ast.Name):
                try:
                    ns[n.targets[0].id] = ev(n.value, ns)
                except Exception:
                    pass
    return ns["_TRACK3D_HTML"]


DRIVER = r"""
// ---- host driver: synthetic inputs through the real RC3D layer ------------
function circle(n, radius, opts) {
  opts = opts || {};
  var out = [], lat0 = 39.0, lon0 = -77.0, mph = opts.mph || 60;
  for (var i = 0; i < n; i++) {
    var a = (i / n) * Math.PI * 2 * (opts.dir || 1);       // dir +1 = right-hand (CW seen from above)
    var lat = lat0 + (radius * Math.cos(a)) / 111320;
    var lon = lon0 + (radius * Math.sin(a)) / (111320 * Math.cos(lat0 * Math.PI / 180));
    var jit = opts.jitter || 0;
    if (jit) { lat += ((i % 2) ? jit : -jit) / 111320; }
    out.push({ t: 1700000000 + i / 25, lat: lat, lon: lon, speed_mph: mph,
               alt_m: opts.alt ? 300 + 10 * Math.sin(a) : null, rpm: 5000 });
  }
  return out;
}
function straight(n, mph) {
  var out = [], lat0 = 39.0, lon0 = -77.0;
  for (var i = 0; i < n; i++) {
    out.push({ t: 1700000000 + i / 25, lat: lat0 + (i * 6) / 111320, lon: lon0,
               speed_mph: mph, alt_m: null });
  }
  return out;
}

var R = {}, THREE_OK = typeof THREE !== "undefined" && typeof THREE.CatmullRomCurve3 === "function";
R.three = THREE_OK;

// --- the ring the car drove (jittery GPS) --------------------------------
var samples = circle(900, 120, { jitter: 2.2, alt: true, mph: 70 });
var path = RC3D.buildPath(samples, { smooth: 5 });

// 1. a path with no NaN, monotonic arc length, sane total
var nan = 0, mono = true;
for (var i = 0; i < path.cum.length; i++) {
  if (!isFinite(path.cum[i])) nan++;
  if (i && path.cum[i] < path.cum[i - 1] - 1e-9) mono = false;
}
R.path = { nan: nan, mono: mono, total: path.total,
           circumference: 2 * Math.PI * 120, dense: path.dense.s.length,
           denseMono: true, denseTotal: path.dense.total };

// the dense centreline must be evenly spaced (~1 m) if the spline LUT is right
var worstGap = 0, minGap = 1e9, avgGap = 0, gaps = 0;
for (i = 1; i < path.dense.s.length; i++) {
  var g = path.dense.s[i] - path.dense.s[i - 1];
  if (g > worstGap) worstGap = g;
  if (g < minGap) minGap = g;
  avgGap += g; gaps++;
}
R.spacing = { avg: avgGap / gaps, min: minGap, max: worstGap };

// 1b. accuracy on a CLEAN ring (projection + spline + arc length together),
//     and how much of the noisy ring's length inflation smoothing removes.
var cleanRing = RC3D.buildPath(circle(900, 120, {}), { smooth: 5 });
R.accuracy = (function () {
  var rawNoisy = RC3D.buildPath(samples, { smooth: 1 });
  return { clean: cleanRing.total, circ: 2 * Math.PI * 120,
           noisySmoothed: path.total, noisyRaw: rawNoisy.total };
})();

// 2. smoothing actually smooths: heading change per sample, raw vs smoothed
function roughness(p) {
  var worst = 0;
  for (var i = 2; i < p.dense.x.length - 2; i++) {
    var a = Math.atan2(p.dense.tan[i][1], p.dense.tan[i][0]);
    var b = Math.atan2(p.dense.tan[i + 2][1], p.dense.tan[i + 2][0]);
    var d = Math.abs(((b - a + Math.PI * 3) % (Math.PI * 2)) - Math.PI);
    if (d > worst) worst = d;
  }
  return worst;
}
var rNo = roughness(RC3D.buildPath(samples, { smooth: 1, denseStep: 0.6 }));
var rSmooth = roughness(RC3D.buildPath(samples, { smooth: 11, denseStep: 0.6 }));
R.smoothing = { raw: rNo, smoothed: rSmooth };
// where the driver really is, per sample, vs the smoothed line
R.smoothing.accuracy = (function () {
  var worst = 0;
  for (var i = 0; i < samples.length; i += 20) {
    var p = RC3D.pointAtS(path, path.cum[i]);
    var e = Math.abs(p.x - path.x[i]) + Math.abs(p.z - path.z[i]);
    if (e > worst) worst = e;
  }
  return worst;
})();

// 3. time -> arc length: monotonic, continuous, matched to the samples
R.timeMap = (function () {
  var bad = 0, worstJump = 0, worstClamp = 0, prev = -1;
  for (var k = 0; k <= 4000; k++) {
    var t = path.t[0] + (path.t[path.t.length - 1] - path.t[0]) * (k / 4000);
    var s = RC3D.sAtTime(path, t);
    if (!isFinite(s) || s < 0 || s > path.total + 1e-6) bad++;
    if (prev >= 0 && s < prev - 1e-9) bad++;
    if (prev >= 0) worstJump = Math.max(worstJump, s - prev);
    prev = s;
  }
  // bracketing: s(t) must sit inside the samples that straddle t
  for (var i = 0; i < path.t.length - 1; i++) {
    var mid = (path.t[i] + path.t[i + 1]) / 2;
    var sm = RC3D.sAtTime(path, mid);
    if (sm < path.cum[i] - 1e-6 || sm > path.cum[i + 1] + 1e-6) worstClamp++;
  }
  return { bad: bad, worstJump: worstJump, worstClamp: worstClamp };
})();

// 4. 60 fps out of 25 Hz data: no jumps, no stalls
function stepStats(p, mph) {
  var dt = 1 / 60, steps = [], stalls = 0, prev = null, t0 = p.t[0];
  for (var k = 0; k < 3000; k++) {
    var t = t0 + 5 + k * dt;
    if (t >= p.t[p.t.length - 1]) break;
    var pt = RC3D.pointAtS(p, RC3D.sAtTime(p, t));
    if (prev) {
      var d = Math.hypot(pt.x - prev[0], pt.y - prev[1], pt.z - prev[2]);
      steps.push(d);
      if (d < 1e-9) stalls++;
    }
    prev = [pt.x, pt.y, pt.z];
  }
  steps.sort(function (a, b) { return a - b; });
  return { max: steps[steps.length - 1], median: steps[Math.floor(steps.length / 2)],
           perFrame: (mph || 70) * 0.44704 * dt, n: steps.length, stalls: stalls };
}
R.interp = { clean: stepStats(cleanRing, 70), noisy: stepStats(path, 70) };

// 5. ribbon geometry
R.ribbon = (function () {
  var width = 12, r = RC3D.ribbon(path, width, 0);
  var n = path.dense.x.length;
  var perpWorst = 0, halfWorst = 0, yWorst = 0;
  for (var i = 0; i < n; i += 7) {
    var lx = r.position[i * 6], ly = r.position[i * 6 + 1], lz = r.position[i * 6 + 2];
    var rx = r.position[i * 6 + 3], rz = r.position[i * 6 + 5];
    var cx = path.dense.x[i], cz = path.dense.z[i];
    var tx = path.dense.tan[i][0], tz = path.dense.tan[i][1];
    perpWorst = Math.max(perpWorst, Math.abs((lx - cx) * tx + (lz - cz) * tz));
    halfWorst = Math.max(halfWorst, Math.abs(Math.hypot(lx - cx, lz - cz) - width / 2));
    yWorst = Math.max(yWorst, Math.abs(ly - path.dense.y[i]));
  }
  return { verts: r.position.length / 3, expectVerts: n * 2,
           tris: r.index.length / 3, expectTris: (n - 1) * 2,
           perpWorst: perpWorst, halfWorst: halfWorst, yWorst: yWorst };
})();

// 6. kerbs exist on the ring, not on a straight
R.kerbs = (function () {
  var k = RC3D.kerbs(path, 12, 3);
  var st = RC3D.buildPath(straight(300, 90), { smooth: 5 });
  var ks = RC3D.kerbs(st, 12, 3);
  return { ring: k.count, straight: ks.count };
})();

// 7. bank sign: a right-hand ring (dir +1) must read +ve lateral g
R.bank = (function () {
  var right = RC3D.buildPath(circle(600, 120, { dir: 1, mph: 80 }), { smooth: 3 });
  var left = RC3D.buildPath(circle(600, 120, { dir: -1, mph: 80 }), { smooth: 3 });
  var gR = RC3D.latAccel(right, right.total * 0.25, 80);
  var gL = RC3D.latAccel(left, left.total * 0.25, 80);
  var pose = RC3D.cameraPose(right, right.total * 0.25, 80, { eye: 1.15, latG: gR });
  var poseFlat = RC3D.cameraPose(right, right.total * 0.25, 80, { eye: 1.15, latG: gR, bank: false });
  return { right: gR, left: gL, roll: pose.roll, rollOff: poseFlat.roll };
})();

// 8. camera: eye height above the surface, aiming forwards, FOV/lead vs speed
R.camera = (function () {
  var s = path.total * 0.3, mph = 90;
  var p = RC3D.pointAtS(path, s);
  var pose = RC3D.cameraPose(path, s, mph, { eye: 1.15 });
  var ahead = RC3D.pointAtS(path, s + pose.lead);
  var fwd = (ahead.x - p.x) * p.tan[0] + (ahead.z - p.z) * p.tan[1];
  var slow = RC3D.cameraPose(path, s, 20, {});
  var fast = RC3D.cameraPose(path, s, 130, {});
  return { eyeAbove: pose.eye.y - p.y, forward: fwd,
           leadSlow: slow.lead, leadFast: fast.lead, leadMin: RC3D.cameraPose(path, s, 0, {}).lead };
})();

// 9. markers from a speed trace: slowest point = apex, brake before it
R.markers = (function () {
  var n = 600, out = [], lat0 = 39.0, lon0 = -77.0;
  for (var i = 0; i < n; i++) {
    var a = (i / n) * Math.PI * 2;
    var lat = lat0 + (150 * Math.cos(a)) / 111320;
    var lon = lon0 + (150 * Math.sin(a)) / (111320 * Math.cos(lat0 * Math.PI / 180));
    var mph = 40 + 60 * Math.abs(Math.sin(a));           // min at a = 0/pi
    out.push({ t: 1700000000 + i / 25, lat: lat, lon: lon, speed_mph: mph, alt_m: null });
  }
  var p = RC3D.buildPath(out, { smooth: 2 });
  var laps = [{ lap: 1, t_start: 0, t_end: 23.96, seconds: 23.96 }];
  var mk = RC3D.markers(p, laps, 1);
  var kinds = mk.map(function (m) { return m.kind; });
  return { kinds: kinds.join(","), n: mk.length,
           y: mk.map(function (m) { return +m.y.toFixed(4); }).join(",") };
})();

// 10a. "where am I" — the single frameState the camera, HUD and mini-map all
//      read must put the camera ON the car for every instant of the lap, and lap
//      progress must run 0 -> 1 monotonically. This is exactly what "the view
//      doesn't follow me" looks like when it breaks.
R.follow = (function () {
  var lap = { lap: 1, t_start: 4, t_end: 20, seconds: 16 };
  var worst = 0, back = 0, prev = -1, prog0 = null, prog1 = null;
  for (var k = 0; k <= 400; k++) {
    var t = lap.t_start + (lap.t_end - lap.t_start) * (k / 400);
    var st = RC3D.frameState(path, t, lap, { eye: 1.15 });
    // the camera's ground position must be the path at that time
    var near = 1e9;
    for (var i = 0; i < path.cum.length; i += 5) {
      var p = RC3D.pointAtS(path, path.cum[i]);
      var d = Math.hypot(p.x - st.pos.x, p.z - st.pos.z);
      if (d < near) near = d;
    }
    if (near > worst) worst = near;
    if (k === 0) prog0 = st.progress;
    if (k === 400) prog1 = st.progress;
    if (st.progress < prev - 1e-9) back++;
    prev = st.progress;
  }
  // a time outside the lap clamps INTO the lap rather than leaving the driver
  var before = RC3D.frameState(path, 0, lap, {});
  var after = RC3D.frameState(path, 1e6, lap, {});
  return { worst: worst, back: back, prog0: prog0, prog1: prog1,
           clampStart: before.t, clampEnd: after.t,
           eye: RC3D.frameState(path, 10, lap, { eye: 1.15 }).eye.y -
                RC3D.frameState(path, 10, lap, { eye: 1.15 }).pos.y };
})();

// 10b. free look eases back on its own, so the view cannot be left pointing
//      away from the car (the other half of "it doesn't follow me")
R.recentre = (function () {
  var lk = { yaw: 0.4, pitch: -0.3 }, i;
  var held = RC3D.recentreLook({ yaw: 0.4, pitch: -0.3 }, 0.05, false);
  for (i = 0; i < 120; i++) RC3D.recentreLook(lk, 1 / 60, true);
  return { heldYaw: held.yaw, yaw: lk.yaw, pitch: lk.pitch };
})();

// 11. altitude is referenced to the session minimum (no floating track)
R.alt = (function () {
  var s = circle(400, 100, { alt: true });
  var p = RC3D.buildPath(s, { smooth: 5 });
  var mn = Math.min.apply(null, p.y), mx = Math.max.apply(null, p.y);
  return { min: mn, max: mx, yRef: p.yRef };
})();

// ---- the REAL Shenandoah asset (imagery-measured width + DEM grid) ---------
var ASSET = JSON.parse(__ASSET_JSON__);
R.asset = (function () {
  var o = { lat: ASSET.centre[0], lon: ASSET.centre[1] };
  var line = ASSET.line.map(function (p) { return { lat: p[0], lon: p[1], alt_m: p[2] }; });
  var p = RC3D.buildPath(line, { smooth: 5, denseStep: 3 });
  var sample = RC3D.assetSampler(ASSET, p.o);
  // 1. width sampled along the driven line is the real width, not the slider
  var widths = [], missing = 0;
  for (var i = 0; i < p.dense.x.length; i += 7) {
    var r = sample(p.dense.x[i], p.dense.z[i]);
    if (!r || !r.width_m) { missing++; continue; }
    widths.push(r.width_m);
  }
  widths.sort(function (a, b) { return a - b; });
  // 2. the ribbon built with those widths is the real width, in metres
  var half = [new Float64Array(p.dense.x.length), new Float64Array(p.dense.x.length)];
  for (var j = 0; j < p.dense.x.length; j++) {
    var rr = sample(p.dense.x[j], p.dense.z[j]);
    var w = (rr && rr.width_m) ? rr.width_m / 2 : null;
    half[0][j] = w; half[1][j] = w;
  }
  var rib = RC3D.ribbon(p, 12, 0, { half: half, o: p.o,
    uv: ASSET.texture.bounds });
  var measured = [];
  for (var k = 0; k < p.dense.x.length; k += 25) {
    var lx = rib.position[k * 6], lz = rib.position[k * 6 + 2];
    var rx = rib.position[k * 6 + 3], rz = rib.position[k * 6 + 5];
    measured.push(Math.hypot(rx - lx, rz - lz));
  }
  measured.sort(function (a, b) { return a - b; });
  // 3. UVs must land inside the texture
  var uvMin = 9, uvMax = -9, uvBad = 0;
  for (var u = 0; u < rib.uv.length; u++) {
    var v = rib.uv[u];
    if (!(v >= -0.01 && v <= 1.01)) uvBad++;
    if (v < uvMin) uvMin = v;
    if (v > uvMax) uvMax = v;
  }
  // 4. terrain: the road must sit ON the DEM mesh, sharing the reference
  var yref = RC3D.applyAssetElevation(p, ASSET.dem, p.o);
  var mesh = RC3D.demMesh(ASSET.dem, p.o, yref, ASSET.texture.bounds);
  var mv = mesh.position, roadMax = -1e9, roadMin = 1e9;
  for (var m = 1; m < p.dense.y.length; m++) {
    if (p.dense.y[m] > roadMax) roadMax = p.dense.y[m];
    if (p.dense.y[m] < roadMin) roadMin = p.dense.y[m];
  }
  var meshMin = 1e9, meshMax = -1e9;
  for (var q = 1; q < mv.length; q += 3) {
    if (mv[q] < meshMin) meshMin = mv[q];
    if (mv[q] > meshMax) meshMax = mv[q];
  }
  // a mesh vertex under the road centreline must be within a couple of metres
  // the road must sit ON the terrain field the mesh is built from
  var worstOnField = 0, worstGap = 0;
  for (var s3 = 0; s3 < p.dense.x.length; s3 += 11) {
    var ll3 = RC3D.localToLatLon(p.dense.x[s3], p.dense.z[s3], p.o);
    var field = RC3D.demAt(ASSET.dem, ll3[0], ll3[1]) - yref;
    worstOnField = Math.max(worstOnField, Math.abs(field - p.dense.y[s3]));
  }
  for (var s2 = 0; s2 < p.dense.x.length; s2 += 40) {
    var best = 1e9, bx = p.dense.x[s2], bz = p.dense.z[s2];
    for (var t = 0; t < mv.length; t += 3) {
      var d2 = Math.hypot(mv[t] - bx, mv[t + 2] - bz);
      if (d2 < best) { best = d2; if (best < 12) { break; } }
    }
    if (best < 12) {
      var gap = Math.abs(mv[t + 1] - p.dense.y[s2]);
      if (gap > worstGap) worstGap = gap;
    }
  }
  return {
    stations: ASSET.line.length, missing: missing,
    widthMedian: widths.length ? widths[Math.floor(widths.length / 2)] : null,
    widthMin: widths[0], widthMax: widths[widths.length - 1],
    ribbonMedian: measured[Math.floor(measured.length / 2)],
    osm: ASSET.width_osm_m, imagery: ASSET.width_imagery_m,
    uvBad: uvBad, uvMin: uvMin, uvMax: uvMax,
    roadMin: roadMin, roadMax: roadMax, meshMin: meshMin, meshMax: meshMax,
    worstGap: worstGap, worstOnField: worstOnField,
    uvAligned: (function () {
      // ground imagery must line up with the road: the mesh UV at a node has to
      // agree with the ribbon's own UV convention for the same lat/lon
      var g = ASSET.dem, tb = ASSET.texture.bounds, worst = 0, k;
      for (k = 0; k < g.values.length; k += 37) {
        var r = Math.floor(k / g.cols), c = k % g.cols;
        var lat = g.bounds[0] + (g.bounds[2] - g.bounds[0]) * (r / (g.rows - 1));
        var lon = g.bounds[1] + (g.bounds[3] - g.bounds[1]) * (c / (g.cols - 1));
        var want = [(lon - tb.west) / (tb.east - tb.west),
                    (lat - tb.south) / (tb.north - tb.south)];
        worst = Math.max(worst, Math.abs(mesh.uv[k * 2] - want[0]),
                                Math.abs(mesh.uv[k * 2 + 1] - want[1]));
      }
      return worst;
    })(),
    demRelief: Math.max.apply(null, ASSET.dem.values) -
                                  Math.min.apply(null, ASSET.dem.values),
    densify: mesh.position.length / 3
  };
})();

// ---- corners + brake boards: geometry with a KNOWN angle ------------------
// build a path of straights joined by arcs of a chosen total heading change
function trackWith(arcDeg, radius) {
  var pts = [], lat0 = 39.0, lon0 = -77.0, heading = 0;   // heading: deg clockwise from north
  var step = 2.0;
  function push(dist, turnPerM) {
    var n = Math.max(1, Math.round(dist / step));
    for (var k = 0; k < n; k++) {
      heading += turnPerM * step * 180 / Math.PI;
      var r = heading * Math.PI / 180;
      var east = Math.sin(r) * step, north = Math.cos(r) * step;
      lat0 += north / 111320;
      lon0 += east / (111320 * Math.cos(lat0 * Math.PI / 180));
      pts.push({ lat: lat0, lon: lon0, speed_mph: 60, alt_m: 0 });
    }
  }
  push(700, 0);                                  // long straight in
  var arcM = Math.abs(arcDeg) * Math.PI / 180 * radius;
  push(arcM, (arcDeg > 0 ? 1 : -1) / radius);     // the corner
  push(700, 0);                                  // long straight out
  return RC3D.buildPath(pts, { smooth: 3, denseStep: 2 });
}
R.corners = {};
[[95, 55], [70, 80], [50, 140], [38, 200], [20, 400]].forEach(function (spec) {
  var p = trackWith(spec[0], spec[1]);
  var cs = RC3D.corners(p, {});
  var mk = RC3D.brakeMarkers(p, cs, {});
  R.corners[spec[0] + "deg"] = {
    found: cs.length,
    deg: cs.length ? Math.round(cs[0].deg) : null,
    dir: cs.length ? cs[0].dir : null,
    radius: cs.length ? Math.round(cs[0].radius_m) : null,
    labels: mk.map(function (m) { return m.label; }).join(""),
    distances: mk.map(function (m) { return m.m; }),
    sides: mk.map(function (m) { return m.side; }).join(""),
    beforeEntry: mk.every(function (m) { return m.s <= cs[0].s0 + 0.001; }),
    apexInside: cs.length ? (cs[0].apex_s >= cs[0].s0 - 0.001 &&
                             cs[0].apex_s <= cs[0].s1 + 0.001) : null
  };
});
// two corners close together: the far boards of the second must be dropped
(function () {
  var pts = [], lat0 = 39.0, lon0 = -77.0, heading = 0, step = 2.0;
  function push(dist, turnPerM) {
    var n = Math.max(1, Math.round(dist / step));
    for (var k = 0; k < n; k++) {
      heading += turnPerM * step * 180 / Math.PI;
      var r = heading * Math.PI / 180;
      lat0 += (Math.cos(r) * step) / 111320;
      lon0 += (Math.sin(r) * step) / (111320 * Math.cos(lat0 * Math.PI / 180));
      pts.push({ lat: lat0, lon: lon0, speed_mph: 60, alt_m: 0 });
    }
  }
  push(500, 0);
  push(90 * Math.PI / 180 * 60, 1 / 60);          // corner 1 (90 deg)
  push(200, 0);                                   // short link
  push(90 * Math.PI / 180 * 60, 1 / 60);          // corner 2 (90 deg)
  push(600, 0);
  var p = RC3D.buildPath(pts, { smooth: 3, denseStep: 2 });
  var cs = RC3D.corners(p, {});
  var mk = RC3D.brakeMarkers(p, cs, {});
  // no board may stand INSIDE a corner (or within 12 m of its entry - a board
  // has to be somewhere the driver can read it while braking, not mid-apex)
  var bad = 0;
  for (var i = 0; i < mk.length; i++) {
    for (var c = 0; c < cs.length; c++) {
      if (mk[i].s > cs[c].s0 - 12 && mk[i].s < cs[c].s1) bad++;
    }
  }
  R.corners.pair = { corners: cs.length, boards: mk.length, insideLink: bad,
                     labels: mk.map(function (m) { return m.label; }).join(""),
                     s0: cs.map(function (c) { return Math.round(c.s0); }),
                     s1: cs.map(function (c) { return Math.round(c.s1); }),
                     boards_s: mk.map(function (m) { return Math.round(m.s); }) };
})();
// and an almost-straight path has no corners at all
(function () {
  var p = trackWith(6, 900);
  R.corners.straightish = { found: RC3D.corners(p, {}).length };
})();

// ---- road colour = braking / accelerating / neither ----------------------
R.colour = (function () {
  // a trace that accelerates, holds, then brakes hard into a corner
  var pts = [], lat0 = 39.0, lon0 = -77.0, v = 40, t = 0, out = [];
  function push(secs, accel) {                  // accel in mph per second
    var step = 1 / 25;
    for (var k = 0; k < Math.round(secs / step); k++) {
      v = Math.max(5, v + accel * step);
      t += step;
      lat0 += (v * 0.44704 * step) / 111320;
      out.push({ t: 1700000000 + t, lat: lat0, lon: lon0, speed_mph: v, alt_m: 0 });
    }
  }
  push(6, 6);        // accelerating  (~0.27 g)
  push(6, 0);        // steady: must be GREY
  push(3, -22);      // braking hard  (~1.0 g)
  var p = RC3D.buildPath(out, { smooth: 3 });
  function at(secs) {
    var i = RC3D.indexOfTime(p.t, secs);
    return { g: p.accel[i], c: RC3D.driveColour(p.accel[i]) };
  }
  var accel = at(3), steady = at(9), brake = at(13.5);
  var grey = RC3D.NEUTRAL_GREY;
  function near(c, ref, tol) {
    return Math.abs(c[0] - ref[0]) < tol && Math.abs(c[1] - ref[1]) < tol &&
           Math.abs(c[2] - ref[2]) < tol;
  }
  return {
    accelG: accel.g, accelGreen: accel.c[1] - Math.max(accel.c[0], accel.c[2]),
    steadyG: steady.g, steadyIsGrey: near(steady.c, grey, 0.02),
    brakeG: brake.g, brakeRed: brake.c[0] - Math.max(brake.c[1], brake.c[2]),
    brakeIsRedderThanAccel: (brake.c[0] - brake.c[1]) > (accel.c[1] - accel.c[0]),
    // harder braking must be redder than light braking
    soft: RC3D.driveColour(-0.1)[0] < RC3D.driveColour(-0.9)[0],
    // and any real acceleration must read green, floored ("assume 100%")
    tiny: RC3D.driveColour(0.05)[1] - RC3D.driveColour(0.05)[2],
    zero: RC3D.driveColour(0),
    // speed must NOT drive the colour any more
    speedIrrelevant: RC3D.driveColour(0).join() ===
                     RC3D.driveColour(0.0).join(),
    at60: RC3D.driveColour(RC3D.accelAtS(p, p.total * 0.1)),
    at90: RC3D.driveColour(RC3D.accelAtS(p, p.total * 0.9))
  };
})();

// ---- plan view: the whole circuit fits, to scale -------------------------
R.plan = (function () {
  var o = { lat: ASSET.centre[0], lon: ASSET.centre[1] };
  var line = ASSET.line.map(function (p) { return { lat: p[0], lon: p[1] }; });
  var p = RC3D.buildPath(line, { smooth: 5, denseStep: 3 });
  var b = ASSET.dem.bounds;                       // S,W,N,E
  var p1 = RC3D.project(b[0], b[1], p.o), p2 = RC3D.project(b[2], b[3], p.o);
  var spanX = Math.abs(p2.x - p1.x), spanZ = Math.abs(p2.z - p1.z);
  var span = Math.max(spanX, spanZ);
  // what the camera sees at the ground plane for a top-down view
  var fov = 58 * Math.PI / 180;
  var h = (span / 2) / Math.tan(fov / 2) * 1.12;
  var visible = 2 * h * Math.tan(fov / 2);
  // every station must be inside that footprint
  var worst = 0;
  for (var i = 0; i < p.dense.x.length; i++) {
    var dx = Math.abs(p.dense.x[i] - (p1.x + p2.x) / 2);
    var dz = Math.abs(p.dense.z[i] - (p1.z + p2.z) / 2);
    worst = Math.max(worst, Math.max(dx, dz));
  }
  return { spanM: span, visibleM: visible, halfTrackM: worst,
           fits: visible / 2 > worst, aspect: spanX / spanZ, planZoomable: true };
})();

// ---- translucent wash + the driven line -----------------------------------
R.wash = (function () {
  var o = { lat: ASSET.centre[0], lon: ASSET.centre[1] };
  var line = ASSET.line.map(function (p) { return { lat: p[0], lon: p[1] }; });
  var p = RC3D.buildPath(line, { smooth: 5, denseStep: 4 });
  var road = RC3D.ribbon(p, 12, 0.03, { worldUV: 6 });
  var wash = RC3D.ribbon(p, 12, 0.07, { worldUV: 6 });
  var drv = RC3D.ribbon(p, 0.45, 0.11, { worldUV: 6 });
  // v across the ribbon must span exactly width/6, u must advance with arc length
  var uMono = true, uPrev = -1, vMax = 0, vMin = 9;
  for (var i = 0; i < road.uv.length; i += 2) {
    if (road.uv[i] < uPrev - 1e-9) uMono = false;
    uPrev = road.uv[i];
    vMax = Math.max(vMax, road.uv[i + 1]);
    vMin = Math.min(vMin, road.uv[i + 1]);
  }
  // the line ribbon is thin: its two edges are 0.45 m apart
  var thin = 0;
  for (var k = 0; k < p.dense.x.length; k += 40) {
    thin = Math.max(thin, Math.hypot(drv.position[k * 6] - drv.position[k * 6 + 3],
                                     drv.position[k * 6 + 2] - drv.position[k * 6 + 5]));
  }
  // and the wash must sit ABOVE the tarmac (it is drawn over it)
  var lift = wash.position[1] - road.position[1];
  return { uMono: uMono, vSpan: vMax - vMin, wantVSpan: 12 / 6,
           lineWidth: thin, lift: lift,
           brightness: [RC3D.driveColour(-0.2)[0], RC3D.driveColour(-0.45)[0],
                        RC3D.driveColour(-0.7)[0]],
           greenB: [RC3D.driveColour(0.1)[1], RC3D.driveColour(0.3)[1]],
           intensity: [RC3D.driveIntensity(0), RC3D.driveIntensity(-0.1),
                       RC3D.driveIntensity(-0.4), RC3D.driveIntensity(-0.9)] };
})();

// ---- trackside: the road distance field, ground, and NO TREES ON THE ROAD --
R.world = (function () {
  // a hairpin that folds back on itself: two straights 46 m apart joined by a
  // 180 deg bend - the infield between them is exactly where a tree placed
  // "18-55 m from the section it was generated from" lands on the OTHER straight
  var pts = [], lat0 = 39.0, lon0 = -77.0, heading = 0, step = 2.0;
  function push(dist, turnPerM) {
    var n = Math.max(1, Math.round(dist / step));
    for (var k = 0; k < n; k++) {
      heading += turnPerM * step * 180 / Math.PI;
      var r = heading * Math.PI / 180;
      lat0 += (Math.cos(r) * step) / 111320;
      lon0 += (Math.sin(r) * step) / (111320 * Math.cos(lat0 * Math.PI / 180));
      pts.push({ lat: lat0, lon: lon0, speed_mph: 60, alt_m: 100 + k * 0.02 });
    }
  }
  push(600, 0);
  push(Math.PI * 23, 1 / 23);            // 180 deg, radius 23 m -> straights 46 m apart
  push(600, 0);
  var p = RC3D.buildPath(pts, { smooth: 3, denseStep: 1 });
  var d = p.dense, hwM = 5;
  var hw = function () { return hwM; };
  var box = RC3D.denseBox(d, 400);
  var field = RC3D.roadField(d, box, 4);
  function brute(x, z) {
    var best = 1e18, bi = -1;
    for (var i = 0; i < d.x.length; i++) {
      var dx = d.x[i] - x, dz = d.z[i] - z, dd = dx * dx + dz * dz;
      if (dd < best) { best = dd; bi = i; }
    }
    return { i: bi, d: Math.sqrt(best) };
  }
  // 1. the distance field agrees with brute force (exact near the road)
  var worstNear = 0, worstApprox = 0, probes = 0;
  var rnd = (function (s) { return function () { s = (s * 1103515245 + 12345) & 0x7fffffff; return s / 0x7fffffff; }; })(5);
  for (var k = 0; k < 3000; k++) {
    var x = box.minX + rnd() * (box.maxX - box.minX), z = box.minZ + rnd() * (box.maxZ - box.minZ);
    var b = brute(x, z), q = field.nearest(x, z), a = field.approx(x, z);
    if (b.d < 80) { worstNear = Math.max(worstNear, Math.abs(q.d - b.d)); probes++; }
    worstApprox = Math.max(worstApprox, Math.abs(a - b.d));
  }
  // 2. trees: procedural AND land-cover driven (woods EVERYWHERE, road included)
  function clearance(spots) {
    var worst = 1e9;
    for (var i = 0; i < spots.length; i++) {
      var t = spots[i], b2 = brute(t.x, t.z);
      worst = Math.min(worst, b2.d - hwM - t.canopy);
    }
    return worst;
  }
  var proc = RC3D.treeSpots(d, field, { rnd: Math.random, halfWidth: hw, gap: 14, seed: "x", max: 4000 });
  var S0 = p.o.lat - 0.01, N0 = p.o.lat + 0.01, W0 = p.o.lon - 0.012, E0 = p.o.lon + 0.012;
  var cols = 300, rows = 300, codes = new Array(cols * rows + 1).join("w");
  var lcSpots = RC3D.treeSpots(d, field, { rnd: Math.random, halfWidth: hw, gap: 14, max: 6000,
    landcover: { codes: codes, cols: cols, rows: rows, bounds: [S0, W0, N0, E0] }, o: p.o });
  // a land cover with NO woods plants nothing
  var none = RC3D.treeSpots(d, field, { rnd: Math.random, halfWidth: hw, gap: 14,
    landcover: { codes: new Array(cols * rows + 1).join("g"), cols: cols, rows: rows,
                 bounds: [S0, W0, N0, E0] }, o: p.o });
  // 3. ground: flat just under the road across its width, the terrain far away
  var terrain = function (x, z) { return 7 + 0.02 * x; };
  var gy = RC3D.groundField(d, field, { halfWidth: hw, terrain: terrain, drop: 0.06 });
  var worstUnder = 0, worstAbove = -1e9;
  for (var i = 0; i < d.x.length; i += 7) {
    var tx = d.tan[i][0], tz = d.tan[i][1];
    for (var o = -hwM; o <= hwM; o += 2.5) {
      var gx = d.x[i] + tz * o, gz = d.z[i] - tx * o;
      var dy = gy(gx, gz) - d.y[i];          // ground minus road
      worstAbove = Math.max(worstAbove, dy);
      worstUnder = Math.max(worstUnder, -dy);
    }
  }
  // no-DEM terrain through the logged altitude: two straights 46 m apart at
  // 100 m and 108 m (a steep 17 % between them) must meet in a SLOPE - no
  // step anywhere, and ease out to a regional surface far away
  var pts2 = pts.map(function (q2, k2) {
    var f = Math.max(0, Math.min(1, (k2 - 300) / (pts.length - 600)));
    return { lat: q2.lat, lon: q2.lon, speed_mph: 60, alt_m: 100 + 8 * f };
  });
  var p2 = RC3D.buildPath(pts2, { smooth: 3, denseStep: 1 });
  var pt = RC3D.pathTerrain(p2.dense), worstStep = 0, prevY = null;
  var mid = p2.dense.x.length >> 2;
  for (var m2 = -400; m2 <= 400; m2 += 2) {                // across both straights
    var ty = pt(p2.dense.x[mid] + m2, p2.dense.z[mid]);
    if (prevY !== null) worstStep = Math.max(worstStep, Math.abs(ty - prevY));
    prevY = ty;
  }
  prevY = null;
  for (var m3 = 0; m3 <= 600; m3 += 2) {                   // along, out past the end
    var ty2 = pt(p2.dense.x[mid], p2.dense.z[mid] + m3);
    if (prevY !== null) worstStep = Math.max(worstStep, Math.abs(ty2 - prevY));
    prevY = ty2;
  }
  var farX = box.maxX - 5, farZ = box.maxZ - 5;
  return { worstNear: worstNear, worstApprox: worstApprox, probes: probes,
           proc: proc.length, procClear: clearance(proc),
           lc: lcSpots.length, lcClear: clearance(lcSpots), none: none.length,
           terrainStep: worstStep, aboveRoad: worstAbove, underRoad: worstUnder,
           far: Math.abs(gy(farX, farZ) - terrain(farX, farZ)),
           unrle: RC3D.unrle("3p2w1g") };
})();

// ---- driver input from REAL logger behaviour ------------------------------
// deterministic noise
function rng(seed) { var s = seed >>> 0; return function () { s = (s * 1664525 + 1013904223) >>> 0; return s / 4294967296; }; }
function gauss(r) { var u = Math.max(1e-12, r()), v = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v); }
var MPH_PER_G_S = 9.80665 / 0.44704;          // mph gained per second at 1 g

// A trace the way the logger writes it: each fix at its 40 ms slot, then a
// REPEAT of it 40 ms later, then the next fix 1 ms after that. Speed is
// quantised to 0.1 mph. segs = [[g, secs], ...]; returns rows + truth.
function loggerTrace(v0, segs, opts) {
  opts = opts || {};
  var rows = [], truth = [], v = v0, dist = 0, tt = 0, lat0 = 39.0, lon0 = -77.0, k = 0;
  segs.forEach(function (sg) {
    var n = Math.round(sg[1] * 25);
    for (var j = 0; j < n; j++) {
      var tFix = k * 0.041;                   // repeat at +0.040, next fix at +0.041
      var dt = k ? 0.041 : 0;
      v = Math.max(0, v + sg[0] * MPH_PER_G_S * dt);
      dist += v * 0.44704 * dt;
      var row = { t: 1700000000 + tFix, lat: lat0 + dist / 111320, lon: lon0,
                  speed_mph: Math.round(v * 10) / 10, rpm: 5000 };
      if (opts.imu) { var im = opts.imu(sg[0], k); row.ax = im[0]; row.ay = im[1]; row.az = im[2]; }
      rows.push(row);
      truth.push({ t: tFix, g: sg[0], mph: v });
      if (opts.repeats !== false) {
        var rep = {}; for (var key in row) rep[key] = row[key];
        rep.t = row.t + 0.040;
        rows.push(rep);
      }
      k++;
    }
  });
  return { rows: rows, truth: truth };
}

R.clean = (function () {
  var tr = loggerTrace(40, [[0.2, 4]]);
  var rows = tr.rows.slice();
  rows.splice(10, 0, { t: rows[9].t + 0.0005, rpm: 4000 });   // no fix: kept untouched
  var c = RC3D.cleanFixes(rows);
  var gaps = [], noFix = 0;
  for (var i = 0; i < c.length; i++) {
    if (typeof c[i].lat !== "number") { noFix++; continue; }
    if (i && typeof c[i - 1].lat === "number") gaps.push(c[i].t - c[i - 1].t);
  }
  gaps.sort(function (a, b) { return a - b; });
  var p = RC3D.buildPath(rows, { smooth: 3 });
  var pk = RC3D.buildPath(rows, { smooth: 3, keepRepeats: true });
  // laps are given as times: the cleaned path keeps the same time base
  var lap = { lap: 1, t_start: 1.0, t_end: 3.0 };
  var fs = RC3D.frameState(p, 2.0, lap, {});
  return { rows: rows.length, kept: c.length, fixes: tr.truth.length, noFix: noFix,
           gapMin: gaps[0], gapMax: gaps[gaps.length - 1], same: c[0] === rows[0],
           pathN: p.n, pathKeep: pk.n, samplesN: p.samples.length,
           srcLast: p.srcIndex[p.srcIndex.length - 1], t0: p.t[0],
           tMatch: Math.abs(p.t[5] - (c[5].t - c[0].t)),
           lapProgress: fs.progress, lapT: fs.t };
})();

R.longG = (function () {
  // 0.5 g constant from 20 mph, logger repeats + 0.1 mph quantisation, 25 Hz
  var tr = loggerTrace(20, [[0, 2], [0.5, 6], [0, 2]]);
  var p = RC3D.buildPath(tr.rows, { smooth: 5 });
  var errs = [];
  for (var i = 0; i < p.n; i++) {
    if (p.t[i] > 2.6 && p.t[i] < 7.6) errs.push(Math.abs(p.accel[i] - 0.5));
  }
  errs.sort(function (a, b) { return a - b; });
  var steady = [];
  for (i = 0; i < p.n; i++) if (p.t[i] > 8.8 && p.t[i] < 9.6) steady.push(Math.abs(p.accel[i]));
  // the old neighbour-difference on the RAW rows is what made garbage of it
  var raw = RC3D.buildPath(tr.rows, { smooth: 5, keepRepeats: true }), rawWorst = 0;
  for (i = 0; i < raw.n; i++) if (raw.t[i] > 2.6 && raw.t[i] < 7.6) rawWorst = Math.max(rawWorst, Math.abs(raw.accel[i] - 0.5));
  return { n: errs.length, median: errs[errs.length >> 1], worst: errs[errs.length - 1],
           steadyWorst: Math.max.apply(null, steady), source: p.accelSource };
})();

R.fusion = (function () {
  // a long varying-g drive: brake/throttle/coast waves, always above 30 mph
  var segs = [];
  for (var q = 0; q < 40; q++) segs.push([0.3, 3], [0, 1], [-0.8, 1.2], [-0.1, 1]);
  var r = rng(7), th = 0.6;
  var good = loggerTrace(60, segs, { repeats: false, imu: function (g) {
    // unknown mounting: rotated + scaled + offset copy of the true g, plus noise
    return [0.9 * g * Math.cos(th) + 0.05 + 0.03 * gauss(r),
            0.9 * g * Math.sin(th) - 0.03 + 0.03 * gauss(r),
            1.0 + 0.1 * g + 0.03 * gauss(r)];
  } });
  var r2 = rng(11);
  var junk = loggerTrace(60, segs, { repeats: false, imu: function () {
    return [0.3 * gauss(r2), 0.3 * gauss(r2), 1 + 0.3 * gauss(r2)];
  } });
  var pg = RC3D.buildPath(good.rows, { smooth: 3 });
  var pj = RC3D.buildPath(junk.rows, { smooth: 3 });
  function err(p, tr) {
    var e = [];
    for (var i = 0; i < p.n; i++) {
      // skip the edges of each step: truth is a square wave, both estimates smear it
      var tt = p.t[i], near = false;
      for (var k = Math.max(0, i - 8); k <= Math.min(p.n - 1, i + 8); k++) if (tr.truth[k].g !== tr.truth[i].g) near = true;
      if (!near) e.push(Math.abs(p.accel[i] - tr.truth[i].g));
    }
    e.sort(function (a, b) { return a - b; });
    return e[e.length >> 1];
  }
  var gpsOnly = RC3D.longG(pg.t, good.rows.map(function (s) { return s.speed_mph; }), null);
  return { goodSource: pg.accelSource, goodR: pg.imuR, junkSource: pj.accelSource, junkR: pj.imuR,
           goodErr: err(pg, good), junkErr: err(pj, junk), gpsOnlySource: gpsOnly.source };
})();

R.input = (function () {
  // classifier on a designed g/speed trace (25 Hz)
  var t = [], sp = [], ac = [];
  function seg(secs, mph, g) {
    for (var k = 0; k < Math.round(secs * 25); k++) {
      t.push(t.length / 25); sp.push(typeof mph === "function" ? mph(k) : mph);
      ac.push(typeof g === "function" ? g(k / 25) : g);
    }
  }
  seg(5, 110, 0.0);                       // top speed, full throttle, g ~ 0
  seg(3, 90, -0.12);                      // coasting at 90: drag alone
  seg(2, 70, -0.8);                       // braking
  seg(4, 50, function (x) { return (x >= 1 && x < 1.25) ? -0.10 : 0.3; });  // shift dip
  seg(3, 50, function (x) { return (x >= 1 && x < 1.12) ? -0.5 : -0.069; });  // brake blip in a coast
  seg(2, 8, 0.2);                         // crawling: never throttle
  seg(2, 8, -0.6);                        // ... but braking still counts
  var path = { t: t, speed: sp, accel: ac };
  var r = RC3D.inputStates(path);
  function at(sec) { return r.state[Math.round(sec * 25)]; }
  function lv(sec) { return r.level[Math.round(sec * 25)]; }
  return { top: at(2.5), coast90: at(6.5), brake: at(9), shiftDip: at(11.1),
           afterShift: at(12.5), blip: at(15.05), coast50: at(16), crawl: at(18), crawlBrake: at(20.5),
           lvTop: lv(2.5), lvBrake: lv(9), lvCoast: lv(6.5),
           coastG100: RC3D.coastG(100), coastG0: RC3D.coastG(0),
           colThr: RC3D.inputColour(1, 1), colThrLo: RC3D.inputColour(1, 0.15),
           colBrk: RC3D.inputColour(-1, 1), colBrkLo: RC3D.inputColour(-1, 0.15),
           colCoast: RC3D.inputColour(0, 0) };
})();

R.events = (function () {
  // a lap: three braking zones (to 45, 60, 35 mph) and one LIFT (85 -> ~77)
  var v = 60, segs = [];
  function to(target, g) { var s = (target - v) / (g * MPH_PER_G_S); v = target; segs.push([g, s]); }
  function hold(g, secs) { v += g * MPH_PER_G_S * secs; segs.push([g, secs]); }
  var expect = [];                        // the slowest point of each event
  to(100, 0.3); hold(0, 3);
  to(45, -0.8); hold(RC3D.coastG(45), 1.5); expect.push(v);   // rolls on a little
  to(85, 0.3); hold(-0.15, 2.4); expect.push(v);
  to(95, 0.3); to(60, -0.8); hold(RC3D.coastG(60), 1.0); expect.push(v);
  to(90, 0.3); to(35, -0.8); expect.push(v); to(60, 0.3);
  var tr = loggerTrace(60, segs);
  var p = RC3D.buildPath(tr.rows, { smooth: 5 });
  var ev = RC3D.cornerEvents(p, {});
  var half = RC3D.cornerEvents(p, { i0: 0, i1: Math.floor(p.n / 2) });
  return { kinds: ev.map(function (e) { return e.kind; }).join(","),
           mins: ev.map(function (e) { return +e.min_mph.toFixed(1); }), expect: expect,
           entry: ev.map(function (e) { return +e.entry_mph.toFixed(1); }),
           peak: ev.map(function (e) { return +e.peak_g.toFixed(2); }),
           ordered: ev.every(function (e) {
             return e.brake_i <= e.release_i && e.brake_i <= e.min_i && e.min_i <= e.throttle_i &&
                    e.brake_s <= e.min_s && e.min_s <= e.throttle_s &&
                    p.input.state[e.throttle_i] === 1;
           }),
           sorted: ev.every(function (e, i) { return !i || ev[i - 1].brake_i <= e.brake_i; }),
           halfN: half.length };
})();

// a closed circuit with varying curvature (metres, local frame)
function loopPt(th) { return [400 * Math.cos(th) + 80 * Math.cos(3 * th), 250 * Math.sin(th) + 40 * Math.sin(2 * th)]; }
function loopNormal(th) {
  var e = 1e-4, a = loopPt(th - e), b = loopPt(th + e), tx = b[0] - a[0], tz = b[1] - a[1], L = Math.hypot(tx, tz);
  return [-tz / L, tx / L];
}
R.register = (function () {
  var lx = [], lz = [], fx = [], fz = [], r = rng(3), N = 3000, lap, k;
  for (k = 0; k <= N; k++) { var q = loopPt(2 * Math.PI * k / N); lx.push(q[0] - 3); lz.push(q[1] + 2); }
  for (lap = 0; lap < 5; lap++) {
    // racing line: +/-4 m off centre at CORNER scale (~150-250 m wavelength).
    // A wander at the scale of the whole lap (1-5 cycles) is genuinely
    // indistinguishable from a shift of the line, so that is not what is tested.
    var ph = r() * 6.28, ph2 = r() * 6.28, fr = 9 + lap;
    for (k = 0; k < 2400; k++) {
      var th = 2 * Math.PI * k / 2400, p0 = loopPt(th), nn = loopNormal(th);
      var w = 3 * Math.sin(fr * th + ph) + Math.sin(2.3 * fr * th + ph2);
      fx.push(p0[0] + nn[0] * w + gauss(r)); fz.push(p0[1] + nn[1] * w + gauss(r));
    }
  }
  var reg = RC3D.registerLine(lx, lz, fx, fz, {});
  var big = [], bz = [];
  for (k = 0; k < lx.length; k++) { big.push(lx[k] - 12); bz.push(lz[k] + 9); }
  var reg2 = RC3D.registerLine(big, bz, fx, fz, {});
  return { dx: reg.dx, dz: reg.dz, med: reg.medDist, inFrac: reg.inFrac, used: reg.used,
           dx2: reg2.dx, dz2: reg2.dz };
})();

R.consensus = (function () {
  var r = rng(21), lat0 = 39.0, lon0 = -77.0, cosl = Math.cos(lat0 * Math.PI / 180);
  var rows = [], laps = [], tt = 0, lapN, k;
  function toLL(x, z) { return [lat0 - z / 111320, lon0 + x / (111320 * cosl)]; }
  // loop length ~2.2 km; each lap at its own speed, its own smooth wander
  for (lapN = 0; lapN < 6; lapN++) {
    var v = 30 + (lapN === 2 ? 1.0 : 0.15 * lapN), ph = r() * 6.28, fr = 3 + (lapN % 4);
    var amp = 2.5, n = Math.round(2230 / v * 25), t0 = tt;
    for (k = 0; k < n; k++) {
      var th = 2 * Math.PI * k / n, p0 = loopPt(th), nn = loopNormal(th);
      var w = amp * Math.sin(fr * th + ph);
      var ll = toLL(p0[0] + nn[0] * w + gauss(r), p0[1] + nn[1] * w + gauss(r));
      rows.push({ t: 1700000000 + tt, lat: ll[0], lon: ll[1], speed_mph: v / 0.44704 });
      tt += 0.04;
    }
    laps.push({ lap: lapN + 1, t_start: t0, t_end: tt, seconds: tt - t0 });
  }
  var p = RC3D.buildPath(rows, { smooth: 5, denseStep: 1 });
  // the truth in the path's own frame
  var tx = [], tz = [];
  for (k = 0; k <= 4000; k++) {
    var q = loopPt(2 * Math.PI * k / 4000), lq = toLL(q[0], q[1]), pr = RC3D.project(lq[0], lq[1], p.o);
    tx.push(pr.x); tz.push(pr.z);
  }
  var truth = RC3D.lineIndex(tx, tz, 20);
  function meanErr(x, z) {
    var s = 0, c = 0;
    for (var i = 0; i < x.length; i += 3) { var h = truth.nearest(x[i], z[i], 40); if (h) { s += h.d; c++; } }
    return c ? s / c : 1e9;
  }
  var kd = p.dense.total / p.total, single = [];
  laps.forEach(function (L) {
    var a = RC3D.sAtTime(p, L.t_start) * kd, b = RC3D.sAtTime(p, L.t_end) * kd, x = [], z = [];
    for (var i = 0; i < p.dense.s.length; i++) if (p.dense.s[i] >= a && p.dense.s[i] <= b) { x.push(p.dense.x[i]); z.push(p.dense.z[i]); }
    single.push(meanErr(x, z));
  });
  var cons = RC3D.consensusLine(p, laps);
  var one = RC3D.consensusLine(p, laps.slice(0, 1));
  return { cons: meanErr(cons.x, cons.z), bestSingle: Math.min.apply(null, single),
           single: single, pts: cons.x.length, onePts: one.x.length };
})();

R.snap = (function () {
  var o = { lat: 39.0, lon: -77.0 }, lx = [], lz = [], k;
  for (k = -500; k <= 500; k += 2) { lx.push(k); lz.push(0); }
  var offs = [3, 4.8, -6, 8.9, 12, -4.5, -9.5], samples = [];
  offs.forEach(function (d, i) {
    var ll = RC3D.localToLatLon(-200 + i * 50, d, o);
    samples.push({ t: i, lat: ll[0], lon: ll[1], speed_mph: 50, rpm: 4000 + i });
  });
  samples.push({ t: 99, rpm: 1 });                      // no fix: passed through
  var before = JSON.stringify(samples);
  var res = RC3D.snapSamples(samples, o, lx, lz, function () { return 5; });
  var after = res.samples.map(function (s) {
    if (typeof s.lat !== "number") return null;
    var p = RC3D.project(s.lat, s.lon, o);
    return [+p.x.toFixed(3), +p.z.toFixed(3)];
  });
  return { moved: res.moved, after: after, untouched: JSON.stringify(samples) === before,
           kept: res.samples[1].rpm, len: res.samples.length,
           same: res.samples[0] === samples[0] };
})();

// ---- sample arc length <-> spline arc length over a LONG session ---------
// 12 laps of jittery 25 Hz fixes: the spline is built on decimated control
// points, so its length and the summed fix-to-fix chords drift apart. One
// global ratio put the car 180 m from where it really was by lap 8 at Summit
// Point; the knot map must keep it on the fix it is showing.
R.knots = (function () {
  var r = rng(5), lat0 = 39.0, lon0 = -77.0, cosl = Math.cos(lat0 * Math.PI / 180);
  var rows = [], tt = 0, lap, k;
  for (lap = 0; lap < 12; lap++) {
    var v = 30 + 2 * Math.sin(lap), n = Math.round(2230 / v * 25);
    for (k = 0; k < n; k++) {
      var th = 2 * Math.PI * k / n, p0 = loopPt(th);
      // some laps wide, some tight: the chord/spline ratio varies lap to lap
      var nn = loopNormal(th), w = (lap % 3) * 2.5;
      var x = p0[0] + nn[0] * w + 0.8 * gauss(r), z = p0[1] + nn[1] * w + 0.8 * gauss(r);
      rows.push({ t: 1700000000 + tt, lat: lat0 - z / 111320, lon: lon0 + x / (111320 * cosl),
                  speed_mph: v / 0.44704 });
      tt += 0.04;
    }
  }
  var p = RC3D.buildPath(rows, { smooth: 3, denseStep: 1 });
  var worst = 0, worstRatio = 0;
  for (k = 0; k < p.n; k += 97) {
    var q = RC3D.pointAtS(p, RC3D.sAtTime(p, p.t[k]));
    worst = Math.max(worst, Math.hypot(q.x - p.x[k], q.z - p.z[k]));
    // what the old single ratio would have drawn
    var sd = RC3D.sAtTime(p, p.t[k]) * p.dense.total / p.total, d = p.dense, j = 0;
    while (j < d.s.length - 1 && d.s[j] < sd) j++;
    worstRatio = Math.max(worstRatio, Math.hypot(d.x[j] - p.x[k], d.z[j] - p.z[k]));
  }
  // and the inverse is the inverse
  var rt = 0;
  for (k = 0; k < 50; k++) {
    var s0 = p.total * k / 50;
    rt = Math.max(rt, Math.abs(RC3D.denseToCum(p, RC3D.cumToDense(p, s0)) - s0));
  }
  return { knots: p.dense.knC ? p.dense.knC.length : 0, worst: worst, worstRatio: worstRatio,
           roundTrip: rt, n: p.n };
})();

// ---- a closed lap that runs past its own start must not fold back ----------
R.trimLoop = (function () {
  var x = [], z = [], k, N = 2000, over = 25;       // 25 m past the start
  var circ = 2 * Math.PI * 300;
  for (k = 0; k <= N + Math.round(over / circ * N); k++) {
    var a = 2 * Math.PI * k / N; x.push(300 * Math.sin(a)); z.push(-300 * Math.cos(a));
  }
  var t = RC3D.trimLoop(x, z);
  var n = t.x.length, len = 0;
  for (k = 1; k < n; k++) len += Math.hypot(t.x[k] - t.x[k - 1], t.z[k] - t.z[k - 1]);
  // closed through linePath: no heading reversal anywhere
  var lp = RC3D.linePath(t.x, t.z, t.x.map(function () { return 0; }), { closed: true, step: 1 });
  var cs = RC3D.corners(lp, {}), maxTurn = 0, d = lp.dense;
  for (k = 2; k < d.x.length; k++) {
    var h1 = Math.atan2(d.z[k - 1] - d.z[k - 2], d.x[k - 1] - d.x[k - 2]);
    var h2 = Math.atan2(d.z[k] - d.z[k - 1], d.x[k] - d.x[k - 1]);
    var dh = Math.abs(((h2 - h1 + 3 * Math.PI) % (2 * Math.PI)) - Math.PI);
    maxTurn = Math.max(maxTurn, dh * 180 / Math.PI);
  }
  // a line that stops SHORT of its start is left alone
  var gx = x.slice(0, N - 20), gz = z.slice(0, N - 20), g = RC3D.trimLoop(gx, gz);
  return { len: len, circ: circ, gap: Math.hypot(t.x[n - 1] - t.x[0], t.z[n - 1] - t.z[0]),
           maxTurnDeg: maxTurn, cutTail: t.cutTail, shortKept: g.x.length === gx.length };
})();

// ---- driven layout != prepared layout: pull on where they agree only ------
R.blend = (function () {
  // prepared: a 1 km x 400 m rounded rectangle. Driven: the same, but a
  // short-course link cuts straight across the middle of the bottom half.
  var lx = [], lz = [], cx = [], cz = [], k;
  function rect(t) {                         // t in [0,1): a stadium, 300 m radius ends
    var L = 1000, Rr = 200, per = 2 * L + 2 * Math.PI * Rr, d = t * per;
    if (d < L) return [d - L / 2, -Rr];
    d -= L; if (d < Math.PI * Rr) { var a = d / Rr; return [L / 2 + Rr * Math.sin(a), -Rr * Math.cos(a)]; }
    d -= Math.PI * Rr; if (d < L) return [L / 2 - d, Rr];
    d -= L; var b = d / Rr; return [-L / 2 - Rr * Math.sin(b), Rr * Math.cos(b)];
  }
  for (k = 0; k < 3256; k++) { var q = rect(k / 3256); lx.push(q[0]); lz.push(q[1]); }
  // driven: 2 m off the prepared line (GPS / racing line) everywhere, plus a
  // 40 m smooth bulge INTO the infield on the bottom straight - a bypass road
  // the prepared layout does not have
  var bump = function (x) { return Math.abs(x) < 160 ? 0.5 * (1 + Math.cos(Math.PI * x / 160)) : 0; };
  for (k = 0; k < 3256; k++) {
    var q2 = rect(k / 3256), inside = q2[1] < -150 ? 40 * bump(q2[0]) : 0;
    cx.push(q2[0]); cz.push(q2[1] + 2 + inside);
  }
  var b = RC3D.blendOnto(cx, cz, lx, lz, { near: 8, blend: 40 });
  var idx = RC3D.lineIndex(lx, lz, 20), onPrep = 0, onPrepN = 0, link = 0, linkN = 0, jump = 0;
  for (k = 0; k < b.x.length; k++) {
    var h = idx.nearest(b.x[k], b.z[k], 100), qq = rect(k / 3256);
    var inLink = qq[1] < -150 && qq[0] > -60 && qq[0] < 60;
    var farFromLink = !(qq[1] < -150 && qq[0] > -200 && qq[0] < 200);
    if (farFromLink) { onPrep = Math.max(onPrep, h ? h.d : 99); onPrepN++; }
    if (inLink) { link = Math.max(link, Math.hypot(b.x[k] - cx[k], b.z[k] - cz[k])); linkN++; }
    if (k) jump = Math.max(jump, Math.hypot(b.x[k] - b.x[k - 1], b.z[k] - b.z[k - 1]));
  }
  return { onPrep: onPrep, onPrepN: onPrepN, link: link, linkN: linkN, jump: jump, matched: b.matched };
})();

// ---- terrain grids decode only at exactly the promised size ----------------
R.decode = (function () {
  var meta = { cols: 3, rows: 2, bounds: [0, 0, 1, 1], base: 100, scale: 0.5 };
  var ok = new ArrayBuffer(12), dv = new DataView(ok);
  for (var k = 0; k < 6; k++) dv.setUint16(k * 2, k * 10, true);
  var g = RC3D.decodeDem(meta, ok);
  return { ok: g ? Array.prototype.slice.call(g.values) : null,
           longer: RC3D.decodeDem(meta, new ArrayBuffer(14)) === null,
           shorter: RC3D.decodeDem(meta, new ArrayBuffer(10)) === null };
})();

// ---- an S-bend is two corners, not one that cancels out --------------------
R.sbend = (function () {
  var pts = [], lat0 = 39.0, lon0 = -77.0, heading = 0, step = 2.0;
  function push(dist, turnPerM) {
    var n = Math.max(1, Math.round(dist / step));
    for (var k = 0; k < n; k++) {
      heading += turnPerM * step * 180 / Math.PI;
      var rr = heading * Math.PI / 180;
      lat0 += Math.cos(rr) * step / 111320;
      lon0 += Math.sin(rr) * step / (111320 * Math.cos(lat0 * Math.PI / 180));
      pts.push({ lat: lat0, lon: lon0, speed_mph: 60, alt_m: 0 });
    }
  }
  push(600, 0);
  push(70 * Math.PI / 180 * 60, 1 / 60);       // 70 deg right, 60 m radius
  push(70 * Math.PI / 180 * 60, -1 / 60);      // straight into 70 deg left
  push(600, 0);
  var p = RC3D.buildPath(pts, { smooth: 3, denseStep: 2 });
  var cs = RC3D.corners(p, {});
  return { n: cs.length, degs: cs.map(function (c) { return Math.round(c.deg); }),
           dirs: cs.map(function (c) { return c.dir; }) };
})();

// --- Kalman/RTS position path vs the moving average: a synthetic race drive
R.kalman = (function () {
  function mulberry(a) {
    return function () {
      a |= 0; a = a + 0x6D2B79F5 | 0;
      var t = Math.imul(a ^ a >>> 15, 1 | a);
      t = t + Math.imul(t ^ t >>> 7, 61 | t) ^ t;
      return ((t ^ t >>> 14) >>> 0) / 4294967296;
    };
  }
  var rnd = mulberry(20260517);
  function gauss() {
    var u = Math.max(1e-12, rnd()), v = rnd();
    return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * v);
  }
  var D2R = Math.PI / 180;
  // closed course, clockwise: straights + 90-degree right corners of 60/120/40/80 m
  var radii = [60, 120, 40, 80], L = [400, 250, 380, 310];
  var segs = [], x = 0, z = 0, h = 90 * D2R, s0 = 0;
  for (var q = 0; q < 4; q++) {
    segs.push({ s0: s0, len: L[q], r: 0, x: x, z: z, h: h });
    x += L[q] * Math.sin(h); z -= L[q] * Math.cos(h); s0 += L[q];
    var r = radii[q], len = r * Math.PI / 2;
    var cx = x + r * Math.cos(h), cz = z + r * Math.sin(h);
    segs.push({ s0: s0, len: len, r: r, x: x, z: z, h: h, cx: cx, cz: cz });
    h += Math.PI / 2; x = cx - r * Math.cos(h); z = cz - r * Math.sin(h); s0 += len;
  }
  var LAP = s0, closure = Math.hypot(x, z);
  function at(s) {
    s = ((s % LAP) + LAP) % LAP;
    var g = segs[segs.length - 1];
    for (var k = 0; k < segs.length; k++) if (s < segs[k].s0 + segs[k].len) { g = segs[k]; break; }
    var u = s - g.s0;
    if (!g.r) return { x: g.x + u * Math.sin(g.h), z: g.z - u * Math.cos(g.h), h: g.h, seg: g };
    var hh = g.h + u / g.r;
    return { x: g.cx - g.r * Math.cos(hh), z: g.cz - g.r * Math.sin(hh), h: hh, seg: g };
  }
  // speed: corner speed sqrt(11 r) (~1.1 g), brake 7 / accelerate 5 m/s^2, cap 45
  function vAt(s) {
    var v = 45, sl = ((s % LAP) + LAP) % LAP;
    segs.forEach(function (g) {
      if (!g.r) return;
      var vc2 = 11 * g.r, a = g.s0, b = g.s0 + g.len, dB, dA;
      if (sl >= a && sl < b) { v = Math.min(v, Math.sqrt(vc2)); return; }
      dB = ((a - sl) % LAP + LAP) % LAP;       // ahead of the corner: braking
      dA = ((sl - b) % LAP + LAP) % LAP;       // after it: accelerating
      v = Math.min(v, Math.sqrt(vc2 + 2 * 7 * dB), Math.sqrt(vc2 + 2 * 5 * dA));
    });
    return Math.min(v, Math.sqrt(1 + 2 * 4 * s));   // launch from the park
  }
  // truth table s(t) on a fine arc-length grid
  var PARK = 8, LAPS = 3, ds = 0.05, TS = [0], SS = [0], tt = 0;
  for (var ss = ds; ss <= LAPS * LAP; ss += ds) {
    tt += ds / vAt(ss - ds / 2); TS.push(tt); SS.push(ss);
  }
  function sAtT(t) {
    var lo = 0, hi = TS.length - 1;
    if (t >= TS[hi]) return SS[hi];
    while (hi - lo > 1) { var m = (lo + hi) >> 1; if (TS[m] <= t) lo = m; else hi = m - 1 < lo ? lo + 1 : m; }
    var f = (t - TS[lo]) / ((TS[lo + 1] - TS[lo]) || 1);
    return SS[lo] + f * (SS[lo + 1] - SS[lo]);
  }
  var lat0 = 39.0, lon0 = -77.0, cosL = Math.cos(lat0 * D2R), T0 = 1700000000;
  var GAP0 = PARK + 70, GAP1 = GAP0 + 3, END = PARK + TS[TS.length - 1];
  var rows = [], prev = null;
  for (var k = 0; k * 0.04 < END; k++) {
    var te = k * 0.04, parked = te < PARK, sTrue = parked ? 0 : sAtT(te - PARK);
    var p = at(sTrue), v = parked ? 0 : vAt(sTrue);
    if (te >= GAP0 && te < GAP1) continue;              // 3 s without fixes
    var tl = T0 + te + (rnd() * 2 - 1) * 0.008;          // logged t jitters +/-8 ms
    if (prev && rnd() < 0.4) {                           // the logger's repeat row
      var rep = {}; for (var key in prev) rep[key] = prev[key];
      rep.t = tl - 0.001; rows.push(rep);
    }
    var mx = p.x + 0.8 * gauss(), mz = p.z + 0.8 * gauss();
    var row = {
      t: tl, lat: lat0 - mz / 111320, lon: lon0 + mx / (111320 * cosL),
      speed_mph: Math.max(0, v + 0.15 * gauss()) / 0.44704,
      heading_deg: parked ? rnd() * 360 : (p.h / D2R + gauss()) % 360,
      alt_m: 200, rpm: 4000,
      _tx: p.x, _tz: p.z, _th: p.h, _v: v, _parked: parked,
      _r: p.seg.r, _cx: p.seg.cx, _cz: p.seg.cz, _te: te
    };
    rows.push(row); prev = row;
  }
  var o = { lat: lat0, lon: lon0 };
  var t0 = Date.now();
  var kp = RC3D.buildPath(rows, { smooth: 5, o: o });
  var ms = Date.now() - t0;
  var ma = RC3D.buildPath(rows, { smooth: 5, o: o, kalman: false });
  function stats(p) {
    var se = 0, ne = 0, rb = 0, rn = 0, park = 0, nan = 0, gapErr = 0, stepErr = 0, i, sm;
    for (i = 0; i < p.n; i++) {
      sm = p.samples[i];
      if (!isFinite(p.x[i]) || !isFinite(p.z[i])) { nan++; continue; }
      var ex = p.x[i] - sm._tx, ez = p.z[i] - sm._tz;
      if (sm._parked) { park = Math.max(park, Math.hypot(ex, ez)); continue; }
      if (sm._v > 4) {
        var lat = ex * Math.cos(sm._th) + ez * Math.sin(sm._th);
        se += lat * lat; ne++;
      }
      if (sm._r === 60) { rb += Math.hypot(p.x[i] - sm._cx, p.z[i] - sm._cz) - 60; rn++; }
      if (Math.abs(sm._te - GAP0) < 2 || Math.abs(sm._te - GAP1) < 2)
        gapErr = Math.max(gapErr, Math.hypot(ex, ez));
      if (i) {
        var pr = p.samples[i - 1];
        var dEst = Math.hypot(p.x[i] - p.x[i - 1], p.z[i] - p.z[i - 1]);
        var dTru = Math.hypot(sm._tx - pr._tx, sm._tz - pr._tz);
        stepErr = Math.max(stepErr, Math.abs(dEst - dTru));
      }
    }
    return { latRms: Math.sqrt(se / ne), n: ne, bias60: rb / rn, n60: rn, park: park,
             nan: nan, gapErr: gapErr, stepErr: stepErr, source: p.positionSource };
  }
  var noHd = rows.map(function (r) {
    var c = {}; for (var key in r) if (key !== "heading_deg") c[key] = r[key]; return c;
  });
  var direct = RC3D.kalmanPath(RC3D.cleanFixes(rows), o, {});
  return { kalman: stats(kp), avg: stats(ma), ms: ms, rows: rows.length, closure: closure,
           noHeading: RC3D.buildPath(noHd, { smooth: 5, o: o }).positionSource,
           direct: direct ? { nVel: direct.nVel, n: direct.x.length, ok: direct.ok } : null,
           tooFew: RC3D.kalmanPath(rows.slice(0, 10), o, {}) };
})();

R.network = (function () {
  // a facility in local metres (x east, z south): the circuit's straight drawn
  // WEST-bound with asymmetric half widths, a pit lane 9 m north of it drawn
  // east-bound, and a paddock AREA (never a road line)
  var o = { lat: 41.0, lon: -72.0 }, M = 111320, cosl = Math.cos(o.lat * Math.PI / 180), k;
  function ll(x, z) { return [o.lat - z / M, o.lon + x / (M * cosl)]; }
  var circ = [], chw = [], pit = [], phw = [];
  for (k = 0; k <= 200; k++) { circ.push(ll(300 - 3 * k, 0)); chw.push([4, 6]); }
  for (k = 0; k <= 100; k++) { pit.push(ll(-150 + 3 * k, -9)); phw.push([3, 3]); }
  var asset = { network: { v: 1, chains: [
    { kind: "circuit", names: ["Main"], closed: false, p: circ, hw: chw, w: 10 },
    { kind: "pit", names: ["Pit Lane"], closed: false, p: pit, hw: phw, w: 6 },
    { kind: "area", names: ["Paddock"], closed: true, p: [ll(0, 50), ll(10, 50), ll(10, 60)] }
  ] } };
  var lines = RC3D.networkLines(asset, o);
  var bias = function (L) { return L.kind === "pit" ? 6 : 0; };
  var idx0 = RC3D.multiIndex(lines, 20, {}), idxB = RC3D.multiIndex(lines, 20, { bias: bias });
  var idxS = RC3D.multiIndex(lines, 20, { skip: function (L) { return L.kind === "pit"; } });
  // 5 m north of the circuit = 4 m from the pit lane: nearest is the pit, but
  // the bias hands the tie to the circuit
  var h0 = idx0.nearest(0, -5, 30), hB = idxB.nearest(0, -5, 30), hS = idxS.nearest(0, -8, 30);
  var hc = idx0.nearest(0, 1, 30);
  // the laps' consensus 2 m south of the circuit, driven EAST-bound
  var cx = [], cz = [];
  for (k = -280; k <= 280; k++) { cx.push(k); cz.push(2); }
  var und = RC3D.blendOnto(cx, cz, null, null, { index: idxB, near: 8, blend: 30 });
  var dir = RC3D.blendOnto(cx, cz, null, null, { index: idxB, near: 8, blend: 30, directed: true });
  var mid = Math.floor(cx.length / 2), worst = 0;
  for (k = 40; k < cx.length - 40; k++) worst = Math.max(worst, Math.abs(und.z[k]));
  return {
    n: lines.length, kinds: lines.map(function (L) { return L.kind; }),
    len0: lines[0].len, hl0: lines[0].hl[5], hr0: lines[0].hr[5],
    near0: h0 && h0.line, nearB: hB && hB.line, effB: hB && hB.eff, nearS: hS && hS.line,
    hwEast: RC3D.networkHalfAt(lines, hc, 1, 0), hwWest: RC3D.networkHalfAt(lines, hc, -1, 0),
    undMatched: und.matched, undWorst: worst, undSrc: und.src[mid] ? und.src[mid].line : null,
    dirMatched: dir.matched, dirMidZ: dir.z[mid],
    none: RC3D.networkLines({}, o).length,
    // no usable lap: the consensus is the whole session, and says so (the
    // viewer must never route that through the network as "the track")
    wholeNoLaps: !!RC3D.consensusLine(path, []).whole
  };
})();

console.log(JSON.stringify(R));
"""

# The page touches location/document before the RC3D_NO_MAIN early return, so
# the harness provides the minimum it can reach; nothing past the early return
# runs (no DOM, no WebGL).
PRELUDE = (
    "globalThis.RC3D_NO_MAIN = 1;"
    "globalThis.location = { search: '' };"
    "globalThis.document = { getElementById: () => null };"
    "globalThis.window = { addEventListener() {}, devicePixelRatio: 1 };\n"
)


class Track3DMathTests(unittest.TestCase):
    @unittest.skipIf(NODE is None, "node not available")
    def test_data_only_page_and_geometry_maths(self):
        html = _page_html()

        # --- the page itself: data only, no imagery/tiles/terrain ------------
        self.assertIn("three@0.160.0", html)
        self.assertIn("first-person DRIVING view, rendered from DATA ONLY", html)
        for banned in ("World_Imagery", "tileLayer", "maplibre", "raster-dem", "arcgisonline"):
            self.assertNotIn(banned, html, f"/track3d must not pull {banned}")
        # smoothness: MSAA on, supersampling available, and NO logarithmic depth
        # buffer (several drivers drop MSAA with one, which shimmers)
        self.assertIn("antialias: true", html)
        self.assertNotIn("logarithmicDepthBuffer", html)
        self.assertIn("setPixelRatio(scale", html)
        self.assertIn('id="b-scale"', html)

        # --- extract the module and drive the real RC3D layer ----------------
        import re
        m = re.search(r"<script type=\"module\">(.*?)</script>", html, re.S)
        self.assertIsNotNone(m, "module script missing")
        # Drop the page's own CDN import: the test supplies THREE (local, or an
        # empty stub when it cannot be found so the no-spline path is exercised).
        body = re.sub(r"^\s*import .*$", "", m.group(1), count=1, flags=re.M)
        three = next((p for p in THREE_CANDIDATES if p.exists()), None)
        header = (("import * as THREE from '%s';\n" % three.as_uri()) if three
                  else "const THREE = {};\n")
        fixture = (ROOT / "tests/fixtures/track-shenandoah.json").read_text()
        driver = DRIVER.replace("__ASSET_JSON__", json.dumps(fixture))
        src = PRELUDE + header + body + "\n" + driver
        with tempfile.TemporaryDirectory() as td:
            f = pathlib.Path(td) / "rc3d_test.mjs"
            f.write_text(src)
            proc = subprocess.run([NODE, str(f)], capture_output=True, text=True, timeout=180)
            self.assertEqual(proc.returncode, 0, proc.stderr[-4000:])
            res = json.loads(proc.stdout.strip().splitlines()[-1])

        # 1. path sanity (a 120 m radius ring is ~754 m around)
        self.assertEqual(res["path"]["nan"], 0)
        self.assertTrue(res["path"]["mono"], "arc length must be monotonic")
        self.assertGreater(res["path"]["dense"], 100)
        self.assertGreater(res["path"]["denseTotal"], 0)
        circ = res["path"]["circumference"]
        # a CLEAN ring must come out within 2% of the true circumference: that is
        # projection + centripetal spline + the arc-length table agreeing.
        acc = res["accuracy"]
        self.assertLess(abs(acc["clean"] - acc["circ"]) / acc["circ"], 0.02, acc)
        # The noisy ring (worst-case alternating +/-2.2 m GPS jitter) is longer
        # than the truth no matter what; the point is that smoothing removes most
        # of that inflation rather than following the zig-zag.
        self.assertLess(acc["noisySmoothed"], acc["noisyRaw"] * 0.4, acc)
        self.assertLess(acc["noisySmoothed"], acc["circ"] * 1.6, acc)
        # evenly spaced dense points = the spline arc-length LUT is fine enough
        self.assertLess(res["spacing"]["max"], 3.0, res["spacing"])
        self.assertGreater(res["spacing"]["min"], 0.2, res["spacing"])

        # 2. smoothing removes GPS jitter without moving the line
        self.assertLess(res["smoothing"]["smoothed"], res["smoothing"]["raw"])
        self.assertLess(res["smoothing"]["accuracy"], 3.0, "smoothed line drifted off the trace")

        # 3. time -> arc length: monotonic (a stutter here is visible at 60 fps),
        #    brackets the samples it sits between, and never teleports
        self.assertEqual(res["timeMap"]["bad"], 0, "time -> distance must be monotonic")
        self.assertEqual(res["timeMap"]["worstClamp"], 0)
        self.assertLess(res["timeMap"]["worstJump"], 1.0, "jump between adjacent probe steps")

        # 4. 60 fps motion out of 25 Hz data. On the clean ring the interpolated
        #    speed must match the real speed (70 mph) to within 15% on EVERY
        #    frame; on the noisy ring (where the geometry's own local speed
        #    wobbles) the requirement is that no frame jumps relative to the
        #    typical frame - that is the stutter/jump guard.
        ci, ni = res["interp"]["clean"], res["interp"]["noisy"]
        self.assertGreater(ci["n"], 1000)
        # uniform motion: no frame may deviate from the typical frame
        self.assertLess(ci["max"] / ci["median"], 1.15, "clean-ring motion is not uniform")
        # and the typical frame must be the GEOMETRY's speed. The synthetic ring
        # walks 0.838 m per 40 ms sample = 46.9 mph; if a scaling bug ever
        # appeared in the dense/segment arc-length mapping this drifts.
        mph = ci["median"] * 60 / 0.44704
        self.assertLess(abs(mph - 46.9) / 46.9, 0.05, f"interpolated speed {mph:.1f} mph")
        self.assertLess(ni["max"], ni["median"] * 3.0, "a frame jumped the median step")
        self.assertEqual(ci["stalls"] + ni["stalls"], 0, "interpolation must never freeze")

        # 5. ribbon geometry
        self.assertEqual(res["ribbon"]["verts"], res["ribbon"]["expectVerts"])
        self.assertEqual(res["ribbon"]["tris"], res["ribbon"]["expectTris"])
        self.assertLess(res["ribbon"]["perpWorst"], 1e-3, "edges must be perpendicular to the tangent")
        self.assertLess(res["ribbon"]["halfWorst"], 1e-3, "edges must sit at +/- width/2")
        self.assertLess(res["ribbon"]["yWorst"], 1e-3, "ribbon must follow the surface")

        # 6. kerbs only where the track turns
        self.assertGreater(res["kerbs"]["ring"], 0)
        self.assertEqual(res["kerbs"]["straight"], 0)

        # 7. the bank sign: right-hand turn = positive lateral g = positive roll
        #    (camera.rotateZ(+roll) lifts the camera's right side = body rolls
        #    out of a right-hander; flip the sign in latAccel if this ever reads
        #    backwards on screen).
        self.assertGreater(res["bank"]["right"], 0.3)
        self.assertLess(res["bank"]["left"], -0.3)
        self.assertGreater(res["bank"]["roll"], 0)
        self.assertLess(res["bank"]["roll"], 0.06)
        self.assertEqual(res["bank"]["rollOff"], 0)

        # 8. camera is at eye height, looking forwards, and looks further ahead
        #    the faster you go
        self.assertAlmostEqual(res["camera"]["eyeAbove"], 1.15, places=2)
        self.assertGreater(res["camera"]["forward"], 0)
        self.assertGreater(res["camera"]["leadFast"], res["camera"]["leadSlow"])
        self.assertAlmostEqual(res["camera"]["leadMin"], 16.0, places=2)

        # 9. markers: apex + throttle at least, all on the surface
        self.assertIn("apex", res["markers"]["kinds"])
        self.assertGreaterEqual(res["markers"]["n"], 2)

        # 10a. the camera is ON the car at every instant of the lap, and lap
        #      progress runs 0 -> 1 monotonically
        f = res["follow"]
        self.assertLess(f["worst"], 3.0, "camera is not on the driven line")
        self.assertEqual(f["back"], 0, "lap progress must not run backwards")
        self.assertLess(f["prog0"], 0.02)
        self.assertGreater(f["prog1"], 0.98)
        self.assertAlmostEqual(f["clampStart"], 4.0, places=3)   # clamped into the lap
        self.assertAlmostEqual(f["clampEnd"], 20.0, places=3)
        self.assertAlmostEqual(f["eye"], 1.15, places=3)

        # 10c. the REAL prepared track (fixture = the asset actually baked from
        #      Esri imagery + AWS DEM for Summit Point Shenandoah): the viewer
        #      must pick up its width, drape its texture and sit on its terrain
        a = res["asset"]
        self.assertEqual(a["missing"], 0, "asset sampler lost stations")
        self.assertGreater(a["stations"], 1000)
        # OSM tags 10 m; imagery measured 12.5 m — the asset uses the tag, and
        # the ribbon must come out at THAT width, not the 12 m slider default
        self.assertAlmostEqual(a["ribbonMedian"], a["osm"], delta=0.6)
        self.assertGreater(a["widthMedian"], 3)
        self.assertLess(a["widthMedian"], 30)
        # every ribbon vertex must sample INSIDE the baked texture
        self.assertEqual(a["uvBad"], 0, "ribbon UVs outside the texture")
        self.assertGreaterEqual(a["uvMin"], -0.01)
        self.assertLessEqual(a["uvMax"], 1.01)
        # terrain: real relief, and the road sits within a couple of metres of
        # the DEM mesh (they share one reference so it cannot float)
        self.assertGreater(a["demRelief"], 5, "DEM fixture has no relief")
        self.assertLess(a["worstOnField"], 1e-6,
                        "road is not on the terrain field the mesh uses")
        self.assertLess(a["worstGap"], 4.0, "road floats off the DEM mesh vertices")
        # tolerance is float32 storage precision (the UVs live in a
        # Float32Array), not sloppiness: a real misalignment would be ~0.05
        self.assertLess(a["uvAligned"], 1e-6,
                        "ground imagery is not aligned with the road's UV frame")
        self.assertGreater(a["densify"], 1000, "DEM mesh is too coarse to be a ground")

        # 10d. brake boards: the ladder depends on corner severity, and a gentle
        #      bend (under ~45 deg, so 38 and 20 too) gets NOTHING
        c95 = res["corners"]["95deg"]
        self.assertEqual(c95["found"], 1)
        self.assertAlmostEqual(c95["deg"], 95, delta=8)
        self.assertEqual(c95["dir"], 1)                     # right-hander
        self.assertEqual(c95["labels"], "54321")
        self.assertEqual(c95["distances"], [500, 400, 300, 200, 100])
        self.assertEqual(c95["sides"], "-1-1-1-1-1")        # outside of a right turn = left
        self.assertTrue(c95["beforeEntry"], "a board landed inside the corner")
        self.assertTrue(c95["apexInside"])
        self.assertAlmostEqual(c95["radius"], 55, delta=25)

        c70 = res["corners"]["70deg"]
        self.assertEqual(c70["labels"], "321")
        self.assertEqual(c70["distances"], [300, 200, 100])

        c50 = res["corners"]["50deg"]
        self.assertEqual(c50["labels"], "21")
        self.assertEqual(c50["distances"], [200, 100])

        for gentle in ("38deg", "20deg"):
            g = res["corners"][gentle]
            self.assertEqual(g["labels"], "", f"{gentle} must have no brake boards")
            self.assertEqual(g["found"], 0)

        self.assertEqual(res["corners"]["straightish"]["found"], 0)
        pair = res["corners"]["pair"]
        self.assertEqual(pair["corners"], 2)
        self.assertEqual(pair["insideLink"], 0, "a board landed inside a corner")
        # corner 1's entry is 490 m in, so its 500 m board has nowhere to stand
        # (correctly dropped); corner 2 sits 200 m later, so its 500/400/300/200
        # boards would fall at/inside corner 1 - only the 100 m board survives
        self.assertEqual(pair["labels"], "43211", pair)

        # 10e. road colour is the DRIVER'S INPUT: green accelerating, grey
        #      neither, red braking (deeper with g) — never the speed
        col = res["colour"]
        self.assertGreater(col["accelG"], 0.1, "accel segment reads as accelerating")
        self.assertGreater(col["accelGreen"], 0.1, "accelerating must be green")
        self.assertTrue(col["steadyIsGrey"], col["steadyG"])
        self.assertLess(abs(col["steadyG"]), 0.05, "steady must be neither")
        self.assertLess(col["brakeG"], -0.3, "braking segment reads as braking")
        self.assertGreater(col["brakeRed"], 0.1, "braking must be red")
        self.assertTrue(col["brakeIsRedderThanAccel"])
        self.assertTrue(col["soft"], "harder braking must be redder")
        self.assertGreater(col["tiny"], 0.05, "any real acceleration is green")
        self.assertAlmostEqual(col["zero"][0], col["zero"][1], delta=0.12)
        self.assertTrue(col["speedIrrelevant"])

        # 10f. the plan view fits the whole circuit (that is what shows its size)
        pl = res["plan"]
        self.assertTrue(pl["fits"], pl)
        self.assertGreater(pl["spanM"], 500)
        self.assertLess(pl["visibleM"] / pl["spanM"], 1.4, "plan view is not wasteful")

        # 10g. the wash is translucent and gets BRIGHTER with harder braking,
        #      the tarmac texture is in world metres, and the driven line is thin
        w = res["wash"]
        self.assertTrue(w["uMono"], "tarmac u must advance with arc length")
        self.assertAlmostEqual(w["vSpan"], w["wantVSpan"], delta=0.01)
        self.assertAlmostEqual(w["lineWidth"], 0.45, delta=0.02)
        self.assertGreater(w["lift"], 0.01, "the wash must sit above the tarmac")
        b = w["brightness"]
        self.assertLess(b[0], b[1], "harder braking must be brighter")
        self.assertLess(b[1], b[2], "harder braking must be brighter")
        self.assertLess(w["greenB"][0], w["greenB"][1],
                        "harder acceleration must be brighter")
        it = w["intensity"]
        self.assertEqual(it[0], 0.0, "no input = no wash")
        self.assertLess(it[1], it[2])
        self.assertLess(it[2], it[3])

        # 10b. look-around eases back so the view can never be left behind
        rc = res["recentre"]
        self.assertAlmostEqual(rc["heldYaw"], 0.4, places=6)     # held while dragging
        self.assertLess(abs(rc["yaw"]), 0.02, "free look did not recentre")
        self.assertLess(abs(rc["pitch"]), 0.02)

        # 12. trackside dressing. The bug this pins: trees were offset ALONG the
        #     tangent (onto the road ahead), and only checked against the section
        #     they were generated from - so on a circuit that folds back they
        #     stood on the other straight.
        w = res["world"]
        self.assertGreater(w["probes"], 100)
        self.assertLess(w["worstNear"], 0.01, "road field must be exact near the road")
        self.assertLess(w["worstApprox"], 4.0, "raster distance is off by more than a cell")
        self.assertGreater(w["proc"], 50, "procedural woods planted nothing")
        self.assertGreater(w["lc"], 200, "land-cover woods planted nothing")
        self.assertEqual(w["none"], 0, "trees where the imagery shows no woods")
        # canopy edge >= 14 m clear of the road edge of ANY section - measured
        # by brute force against every station, so the other straight of the
        # hairpin counts too (tiny slack for the station spacing)
        self.assertGreaterEqual(w["procClear"], 13.4, w)
        self.assertGreaterEqual(w["lcClear"], 13.4, w)
        # the ground is flat JUST under the road everywhere across it (never
        # poking through, never a gap you can see), and is the terrain far away
        self.assertLess(w["aboveRoad"], -0.03, "ground pokes through the road")
        self.assertLess(w["underRoad"], 0.12, "road floats above the ground")
        self.assertLess(w["far"], 1e-6)
        # no-DEM terrain is continuous: a 2 m step never changes it by more
        # than the road's own steepest grade would (it used to jump to the
        # session mean at the edge of the search window)
        self.assertLess(w["terrainStep"], 0.5, w["terrainStep"])
        self.assertEqual(w["unrle"], "pppwwg")

        # 11. altitude referenced to the session minimum so the ribbon sits on
        #     the ground plane instead of floating at MSL
        self.assertAlmostEqual(res["alt"]["min"], 0.0, places=3)
        self.assertGreater(res["alt"]["max"], 5)

        # 13. driver input from REAL logger behaviour. The logger writes each
        #     fix twice (repeat 40 ms later, next fix 1 ms after that): repeats
        #     go, first appearances stay ~41 ms apart, rows without a fix stay.
        c = res["clean"]
        self.assertEqual(c["kept"], c["fixes"] + 1, c)
        self.assertEqual(c["noFix"], 1)
        self.assertTrue(c["same"], "cleanFixes must not copy rows")
        self.assertAlmostEqual(c["gapMin"], 0.041, delta=0.002)
        self.assertAlmostEqual(c["gapMax"], 0.041, delta=0.002)
        self.assertEqual(c["pathN"], c["kept"], "buildPath must drop repeats")
        self.assertEqual(c["pathKeep"], c["rows"], "keepRepeats opts out")
        self.assertEqual(c["samplesN"], c["pathN"])
        self.assertEqual(c["srcLast"], c["rows"] - 2, "srcIndex maps back to the input")
        # the time base is unchanged, so laps (given as times) still line up
        self.assertEqual(c["t0"], 0)
        self.assertLess(c["tMatch"], 1e-9)
        self.assertAlmostEqual(c["lapProgress"], 0.5, places=6)
        self.assertAlmostEqual(c["lapT"], 2.0, places=6)

        # longitudinal g by time regression: 0.5 g with repeats + 0.1 mph steps
        lg = res["longG"]
        self.assertGreater(lg["n"], 100)
        self.assertLess(lg["worst"], 0.05, lg)
        self.assertLess(lg["steadyWorst"], 0.05, lg)
        self.assertEqual(lg["source"], "gps", "no IMU -> gps only")

        # IMU fusion only when the IMU actually fits the GPS g
        fu = res["fusion"]
        self.assertEqual(fu["goodSource"], "gps+imu", fu)
        self.assertGreater(fu["goodR"], 0.85)
        self.assertLess(fu["goodErr"], 0.05, fu)
        self.assertEqual(fu["junkSource"], "gps", fu)
        self.assertLess(fu["junkR"], 0.5)
        self.assertLess(fu["junkErr"], 0.05, fu)
        self.assertEqual(fu["gpsOnlySource"], "gps")

        # throttle / coast / brake relative to the speed-dependent coast curve
        ip = res["input"]
        self.assertEqual(ip["top"], 1, "full throttle at top speed (g ~ 0) is throttle")
        self.assertEqual(ip["coast90"], 0, "-0.12 g at 90 mph is just drag: coast")
        self.assertEqual(ip["brake"], -1)
        self.assertEqual(ip["shiftDip"], 1, "a 0.25 s shift dip stays throttle")
        self.assertEqual(ip["afterShift"], 1)
        self.assertEqual(ip["blip"], 0, "a brake blip under 0.25 s is coast")
        self.assertEqual(ip["coast50"], 0)
        self.assertEqual(ip["crawl"], 0, "below 12 mph is never throttle")
        self.assertEqual(ip["crawlBrake"], -1, "... but braking still counts")
        self.assertGreater(ip["lvTop"], 0.15)
        self.assertGreater(ip["lvBrake"], 0.6)
        self.assertEqual(ip["lvCoast"], 0)
        self.assertAlmostEqual(ip["coastG100"], -0.14, places=6)
        self.assertAlmostEqual(ip["coastG0"], -0.045, places=6)
        thr, thrLo = ip["colThr"], ip["colThrLo"]
        brk, brkLo = ip["colBrk"], ip["colBrkLo"]
        self.assertGreater(thr[1], max(thr[0], thr[2]) + 0.5, "throttle is green")
        self.assertGreater(thr[1], thrLo[1], "harder throttle is brighter")
        self.assertGreaterEqual(thrLo[1], 0.45 * 0.99, "throttle green is floored")
        self.assertGreater(brk[0], max(brk[1], brk[2]) + 0.5, "brake is red")
        self.assertGreater(brk[0], brkLo[0], "harder braking is brighter")
        co = ip["colCoast"]
        self.assertGreater(co[0], 0.8)
        self.assertGreater(co[1], 0.6)
        self.assertLess(co[2], 0.35, "coast is amber")

        # braking zones + lifts: the right count, the right slowest points
        ev = res["events"]
        self.assertEqual(ev["kinds"], "brake,lift,brake,brake", ev)
        for got, want in zip(ev["mins"], ev["expect"]):
            self.assertAlmostEqual(got, want, delta=1.0, msg=ev)
        self.assertAlmostEqual(ev["peak"][0], 0.8, delta=0.08)
        self.assertAlmostEqual(ev["peak"][1], 0.15, delta=0.05)
        self.assertGreater(ev["entry"][0], 95)
        self.assertTrue(ev["ordered"], ev)
        self.assertTrue(ev["sorted"])
        self.assertEqual(ev["halfN"], 1, "the i0/i1 window must be honoured")

        # registration: a (3, -2) m misplaced line comes back, despite a racing
        # line +/-4 m off centre and 1 m GPS noise; a bigger shift converges too
        rg = res["register"]
        self.assertLess(abs(rg["dx"] - 3), 0.3, rg)
        self.assertLess(abs(rg["dz"] + 2), 0.3, rg)
        self.assertLess(abs(rg["dx2"] - 15), 0.5, rg)
        self.assertLess(abs(rg["dz2"] + 11), 0.5, rg)
        self.assertLess(rg["med"], 3.5)
        self.assertGreater(rg["inFrac"], 0.95)
        self.assertGreater(rg["used"], 1000)

        # consensus of all laps beats every single lap
        cs = res["consensus"]
        self.assertLess(cs["cons"], cs["bestSingle"] * 0.85, cs)
        self.assertGreater(cs["pts"], 1000)
        self.assertGreater(cs["onePts"], 1000, "one lap -> the reference itself")

        # snapping: only the band just past the edge is pulled in
        sn = res["snap"]
        self.assertEqual(sn["moved"], 3, sn)
        want = [[-200, 3], [-150, 4.6], [-100, -4.6], [-50, 4.6], [0, 12], [50, -4.5], [100, -9.5], None]
        for got, w in zip(sn["after"], want):
            if w is None:
                self.assertIsNone(got)
                continue
            self.assertAlmostEqual(got[0], w[0], delta=0.01)
            self.assertAlmostEqual(got[1], w[1], delta=0.01)
        self.assertTrue(sn["untouched"], "snapSamples must not mutate its input")
        self.assertTrue(sn["same"], "unmoved rows are passed through")
        self.assertEqual(sn["kept"], 4001)
        self.assertEqual(sn["len"], 8)

        # time -> place over a 12-lap session: the knot map keeps the car on the
        # fix it is showing; one global ratio drifted tens of metres
        kn = res["knots"]
        if three is not None:
            self.assertGreater(kn["knots"], 1000, kn)
            self.assertLess(kn["worst"], 4.0, kn)
            self.assertGreater(kn["worstRatio"], 4 * kn["worst"],
                               "the test must actually exercise the drift")
        self.assertLess(kn["roundTrip"], 1e-6)

        # a closed lap that overruns its start is cut, so the loop never
        # folds back into a fake hairpin at the seam
        tl = res["trimLoop"]
        self.assertLess(abs(tl["len"] - tl["circ"]), 3.0, tl)
        self.assertLess(tl["gap"], 2.0, tl)
        self.assertLess(tl["maxTurnDeg"], 3.0, tl)
        self.assertGreater(tl["cutTail"], 10)
        self.assertTrue(tl["shortKept"], "a line short of its start is left alone")

        # driven layout vs prepared layout: on the prepared line where they are
        # the same road, the driven shape where they are not, no step between
        bl = res["blend"]
        self.assertLess(bl["onPrep"], 0.3, bl)
        self.assertGreater(bl["onPrepN"], 2000)
        self.assertLess(bl["link"], 0.5, bl)
        self.assertGreater(bl["linkN"], 50)
        self.assertLess(bl["jump"], 1.6, bl)
        self.assertGreater(bl["matched"], 0.85)
        self.assertLess(bl["matched"], 0.97)

        # terrain grids: exact length only (a cached grid from another bake
        # would decode as wrong terrain)
        dc = res["decode"]
        self.assertEqual(dc["ok"], [100, 105, 110, 115, 120, 125])
        self.assertTrue(dc["longer"])
        self.assertTrue(dc["shorter"])

        # an S-bend is two corners of opposite hand (merged, they cancelled)
        sb = res["sbend"]
        self.assertEqual(sb["n"], 2, sb)
        self.assertEqual(sorted(sb["dirs"]), [-1, 1])
        for d in sb["degs"]:
            self.assertGreater(abs(d), 55, sb)

        # Kalman/RTS positions (speed + course over ground fused with the fixes)
        # put the car where it really was across the track: a synthetic race
        # drive with 0.8 m fix noise, +/-8 ms timestamp jitter, 1 deg heading
        # noise, logger repeat rows, a parked start and a 3 s fix gap
        kf = res["kalman"]
        ka, av = kf["kalman"], kf["avg"]
        self.assertLess(kf["closure"], 1e-6, "the synthetic course must close")
        self.assertEqual(ka["source"], "kalman")
        self.assertEqual(av["source"], "smooth")
        self.assertEqual(kf["noHeading"], "smooth", "no heading -> moving average")
        self.assertGreater(ka["n"], 3000)
        self.assertLess(ka["latRms"], 0.35, kf)
        self.assertLess(ka["latRms"] * 2, av["latRms"], kf)
        # no corner cutting through the 60 m radius corner
        self.assertGreater(ka["n60"], 100)
        self.assertLess(abs(ka["bias60"]), 0.12, kf)
        # parked with a random heading: no wander
        self.assertLess(ka["park"], 1.5, kf)
        # the 3 s gap: no NaN, no teleport, back on the line either side
        self.assertEqual(ka["nan"], 0)
        self.assertLess(ka["gapErr"], 1.5, kf)
        self.assertLess(ka["stepErr"], 3.0, kf)
        self.assertTrue(kf["direct"]["ok"])
        self.assertGreater(kf["direct"]["nVel"], 3000)
        self.assertIsNone(kf["tooFew"], "fewer than 20 fixes -> null")

        # the facility network: every layout as a road line (areas are not),
        # a nearest-road index over all of them where a pit lane loses a tie
        # with the circuit beside it, half widths that follow the direction of
        # travel, and the laps pulled onto the real centreline whichever way
        # OpenStreetMap happened to draw it
        nw = res["network"]
        self.assertEqual(nw["n"], 2, nw)
        self.assertEqual(nw["kinds"], ["circuit", "pit"])
        self.assertAlmostEqual(nw["len0"], 600, delta=1)
        self.assertEqual([nw["hl0"], nw["hr0"]], [4, 6])
        self.assertEqual(nw["near0"], 1, "unbiased: the pit lane is nearer")
        self.assertEqual(nw["nearB"], 0, "biased: the circuit wins the tie")
        self.assertAlmostEqual(nw["effB"], 5, delta=0.01)
        self.assertEqual(nw["nearS"], 0, "a skipped line is never returned")
        self.assertEqual(nw["hwWest"], [4, 6], "the chain's own direction: as stored")
        self.assertEqual(nw["hwEast"], [6, 4], "travelling the other way swaps sides")
        self.assertGreater(nw["undMatched"], 0.98, nw)
        self.assertLess(nw["undWorst"], 0.05, nw)
        self.assertEqual(nw["undSrc"], 0)
        self.assertLess(nw["dirMatched"], 0.01, "directed: a reversed chain never matches")
        self.assertAlmostEqual(nw["dirMidZ"], 2, delta=1e-9)
        self.assertEqual(nw["none"], 0)
        self.assertTrue(nw["wholeNoLaps"])


if __name__ == "__main__":
    unittest.main()

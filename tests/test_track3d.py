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

        # 11. altitude referenced to the session minimum so the ribbon sits on
        #     the ground plane instead of floating at MSL
        self.assertAlmostEqual(res["alt"]["min"], 0.0, places=3)
        self.assertGreater(res["alt"]["max"], 5)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""Rev D final finishing: close the connections the autorouter could not, remove
one dead-end stub, and drop silkscreen labels that no longer clear the mask.

Run ONCE after route.py --finish on the frozen autorouter result.

The long NET_RESET_ASSERT run uses a raster/BFS pathfinder on In2.Cu instead of a
hand-drawn L, because hand-drawn Ls crossed existing copper. Everything is then
re-checked by the caller's DRC.
"""
from collections import deque
from pathlib import Path
import json
import math

import pcbnew as k

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / 'design'
NAME = 'racecar-integrated-revd'
PCB = D / (NAME + '.kicad_pcb')
b = k.LoadBoard(str(PCB))
alias = json.loads((D / 'COMPONENT-ALIASES.json').read_text())
P = lambda x, y: k.VECTOR2I(k.FromMM(x), k.FromMM(y))
pt = lambda p: (k.ToMM(p.x), k.ToMM(p.y))
coords = {(f.GetReference(), p.GetNumber()): pt(p.GetPosition())
          for f in b.GetFootprints() for p in f.Pads()}


def pos(al, n):
    return coords[(alias.get(al, al), str(n))]


def route(net, points, layer=k.F_Cu, w=.25):
    for a, z in zip(points, points[1:]):
        if a == z:
            continue
        t = k.PCB_TRACK(b)
        t.SetStart(P(*a)); t.SetEnd(P(*z))
        t.SetWidth(k.FromMM(w)); t.SetLayer(layer)
        t.SetNet(b.FindNet(net)); b.Add(t)


def via(net, x, y):
    v = k.PCB_VIA(b); v.SetPosition(P(x, y))
    v.SetWidth(k.FromMM(.6)); v.SetDrill(k.FromMM(.3))
    v.SetViaType(k.VIATYPE_THROUGH); v.SetLayerPair(k.F_Cu, k.B_Cu)
    v.SetNet(b.FindNet(net)); b.Add(v)


# ---------------------------------------------------------------- 1. cleanup --
CLIP = {'1 +5V', '2 RET', 'NTC', '1 NTC', '2 +5V', '3V3 NET'}
gone = []
for item in list(b.GetDrawings()):
    if isinstance(item, k.PCB_TEXT) and item.GetText() in CLIP:
        gone.append(item.GetText()); b.RemoveNative(item)
print('silkscreen labels removed (pinouts remain in ASSEMBLY.md):', sorted(set(gone)))

# ------------------------------------------------------------------ 2. paths --
# +5V_SCREEN -> C33 pin 1. The rail already reaches U5 pin 6; this closes the
# last few mm to the screen bulk capacitor.
c33 = pos('C33', 1)
route('+5V_SCREEN', [(55.14, 123.0), c33], k.F_Cu, .8)

# DASH_RX_5V: R34 pin 2 -> U5 pin 4, via In2.Cu under the crowded screen area.
r34 = pos('R34', 2)
u5 = pos('U5', 4)
via('DASH_RX_5V', u5[0], 127.0)
route('DASH_RX_5V', [u5, (u5[0], 127.0)], k.F_Cu, .25)
route('DASH_RX_5V', [(u5[0], 127.0), (u5[0], r34[1]), (r34[0], r34[1])], k.In2_Cu, .25)
via('DASH_RX_5V', r34[0], r34[1])

# ------------------------------------------- 3. BFS path for NET_RESET_ASSERT --
NET = 'NET_RESET_ASSERT'
LAYER = k.In2_Cu
STEP, MARGIN = 0.25, 0.30
X0, Y0, X1, Y1 = 20.0, 20.0, 170.0, 175.0
NX, NY = int((X1 - X0) / STEP), int((Y1 - Y0) / STEP)
blocked = bytearray(NX * NY)


def cell(x, y):
    return int((x - X0) / STEP), int((y - Y0) / STEP)


def mark(x, y, r):
    ix, iy = cell(x, y)
    for dx in range(-int(r / STEP) - 1, int(r / STEP) + 2):
        for dy in range(-int(r / STEP) - 1, int(r / STEP) + 2):
            a, c = ix + dx, iy + dy
            if 0 <= a < NX and 0 <= c < NY and math.hypot(dx, dy) * STEP <= r + STEP:
                blocked[c * NX + a] = 1


def raster_seg(a, z, r):
    n = max(2, int(math.hypot(z[0] - a[0], z[1] - a[1]) / (STEP / 2)) + 1)
    for i in range(n + 1):
        f = i / n
        mark(a[0] + (z[0] - a[0]) * f, a[1] + (z[1] - a[1]) * f, r)


for t in b.GetTracks():
    if t.GetClass() == 'PCB_VIA':
        if t.IsOnLayer(LAYER):
            mark(*pt(t.GetPosition()), MARGIN + 0.35)
        continue
    if t.GetLayer() != LAYER or t.GetNetname() == NET:
        continue
    raster_seg(pt(t.GetStart()), pt(t.GetEnd()), MARGIN + k.ToMM(t.GetWidth()) / 2)
for f in b.GetFootprints():
    for p in f.Pads():
        if p.GetNetname() == NET or not p.IsOnLayer(LAYER):
            continue
        x, y = pt(p.GetPosition())
        raster_seg((x - k.ToMM(p.GetSize().x) / 2, y - k.ToMM(p.GetSize().y) / 2),
                   (x + k.ToMM(p.GetSize().x) / 2, y + k.ToMM(p.GetSize().y) / 2),
                   MARGIN)

start, goal = pos('U1', 8), pos('R39', 1)
sx, sy = cell(*start); gx, gy = cell(*goal)
for c in ((sx, sy), (gx, gy)):
    if blocked[c[1] * NX + c[0]]:
        print('WARNING: start/goal cell blocked; clearing')
        for dx in range(-3, 4):
            for dy in range(-3, 4):
                a, c2 = c[0] + dx, c[1] + dy
                if 0 <= a < NX and 0 <= c2 < NY:
                    blocked[c2 * NX + a] = 0

prev = [None] * (NX * NY)
seen = bytearray(NX * NY)
q = deque([(sx, sy)]); seen[sy * NX + sx] = 1
found = False
while q:
    cx, cy = q.popleft()
    if (cx, cy) == (gx, gy):
        found = True; break
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        a, c2 = cx + dx, cy + dy
        if 0 <= a < NX and 0 <= c2 < NY and not blocked[c2 * NX + a] and not seen[c2 * NX + a]:
            seen[c2 * NX + a] = 1
            prev[c2 * NX + a] = (cx, cy)
            q.append((a, c2))

if not found:
    print('PATHFIND FAILED for', NET, '- leave it for manual review')
else:
    path = []
    n = (gy, gx)
    while n:
        path.append(n); n = prev[n[1] * NX + n[0]]
    path.reverse()
    pts = [(X0 + c[0] * STEP, Y0 + c[1] * STEP) for c in path]
    pts[0], pts[-1] = start, goal
    # collapse collinear runs so we emit few long segments
    simp = [pts[0]]
    for i in range(1, len(pts) - 1):
        a, c, z = simp[-1], pts[i], pts[i + 1]
        if (z[0] - a[0]) * (c[1] - a[1]) != (z[1] - a[1]) * (c[0] - a[0]):
            simp.append(c)
    simp.append(pts[-1])
    print(f'NET_RESET_ASSERT path: BFS found, {len(simp)} waypoints on In2.Cu')
    route(NET, simp, k.In2_Cu, .25)
    via(NET, goal[0], goal[1])

# ------------------------------------------------------- 4. remove dead stub --
removed = 0
for t in list(b.GetTracks()):
    if t.GetClass() == 'PCB_VIA' or t.GetNetname() != '+5V_MAIN':
        continue
    if t.IsLocked() or t.GetLayer() != k.F_Cu:
        continue
    ends = [pt(t.GetStart()), pt(t.GetEnd())]
    touch = 0
    for other in b.GetTracks():
        if other is t:
            continue
        for e in ends:
            for o in (pt(other.GetStart()), pt(other.GetEnd())):
                if math.hypot(e[0] - o[0], e[1] - o[1]) < 0.05:
                    touch += 1
    for f in b.GetFootprints():
        for p in f.Pads():
            if p.GetNetname() != '+5V_MAIN':
                continue
            x, y = pt(p.GetPosition())
            for e in ends:
                if math.hypot(e[0] - x, e[1] - y) < k.ToMM(p.GetSize().x) / 2 + 0.05:
                    touch += 1
    if touch < 2:
        b.RemoveNative(t); removed += 1
print('dead-end +5V_MAIN stubs removed:', removed)

b.BuildConnectivity()
k.ZONE_FILLER(b).Fill(b.Zones())
k.SaveBoard(str(PCB), b)
print('Finishing written. Re-run DRC/ERC before export.')

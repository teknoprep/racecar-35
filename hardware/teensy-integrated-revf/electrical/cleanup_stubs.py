#!/usr/bin/env python3
"""DRC-driven dead-end cleanup for the Rev F board.

The finishing pass and the autorouter leave stray track stubs and orphan vias
behind. They show up as `track_dangling` / `via_dangling` warnings - and the
fail-closed exporter counts warnings as violations, so the package is refused.

Rather than guessing connectivity in python (an earlier attempt asked "does this
point lie inside any same-net copper?", which said the stubs were fine while the
DRC disagreed), this asks KiCad itself: run `kicad-cli pcb drc --format json`,
delete exactly the items whose UUIDs the DRC reports as dangling, save, and repeat
until the board is clean or a pass makes no progress.

Usage: python3 electrical/cleanup_stubs.py [max_passes]
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import pcbnew as k

ROOT = Path(__file__).resolve().parents[1]
D = ROOT / 'design'
NAME = 'racecar-integrated-revf'
PCB = D / (NAME + '.kicad_pcb')
DANGLING = ('track_dangling', 'via_dangling', 'track_dangling_via', 'dangling_via')


def drc(path):
    out = Path(tempfile.mkstemp(suffix='.json')[1])
    subprocess.run(['kicad-cli', 'pcb', 'drc', '--format', 'json', '--severity-all',
                    '--all-track-errors', '-o', str(out), str(path)],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    data = json.loads(out.read_text())
    out.unlink(missing_ok=True)
    return data



def islands(board, net):
    """Count separate copper islands on a net. Used by the finishing pass to decide
    whether a Rev-D-specific repair is still needed at all: adding a duplicate route
    to an already-connected net is what produced shorted vias and hole_to_hole pairs."""
    mm = lambda v: k.ToMM(v)
    pt = lambda p: (mm(p.x), mm(p.y))
    items = []
    for t in board.GetTracks():
        if t.GetNetname() != net:
            continue
        if t.GetClass() == 'PCB_VIA':
            items.append(('via', {k.F_Cu, k.In2_Cu, k.B_Cu}, [pt(t.GetPosition())]))
        else:
            items.append(('trk', {t.GetLayer()}, [pt(t.GetStart()), pt(t.GetEnd())]))
    for f in board.GetFootprints():
        for p in f.Pads():
            if p.GetNetname() != net:
                continue
            layers = {l for l in (k.F_Cu, k.In2_Cu, k.B_Cu) if p.IsOnLayer(l)}
            items.append(('pad', layers, [pt(p.GetPosition())]))
    if not items:
        return 0

    def seg_dist(p, a, z):
        (px, py), (ax, ay), (zx, zy) = p, a, z
        dx, dy = zx - ax, zy - ay
        if dx == 0 and dy == 0:
            return ((px - ax) ** 2 + (py - ay) ** 2) ** .5
        t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
        return ((px - (ax + t * dx)) ** 2 + (py - (ay + t * dy)) ** 2) ** .5

    def touch(i, j):
        ki, li, pi = items[i]
        kj, lj, pj = items[j]
        if not (li & lj):
            return False
        for a in pi:
            for c in pj:
                if ((a[0] - c[0]) ** 2 + (a[1] - c[1]) ** 2) ** .5 < 0.06:
                    return True
        for a in pi:
            if kj == 'trk' and seg_dist(a, pj[0], pj[1]) < 0.06:
                return True
        for c in pj:
            if ki == 'trk' and seg_dist(c, pi[0], pi[1]) < 0.06:
                return True
        return False

    parent = list(range(len(items)))
    def find(a):
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a
    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if touch(i, j):
                ra, rc = find(i), find(j)
                if ra != rc:
                    parent[ra] = rc
    return len({find(i) for i in range(len(items))})


def dedupe_vias(board):
    """Drop vias that sit on top of another via on the same net (the finishing pass and
    the autorouter can both place one, which the DRC reports as hole_to_hole)."""
    mm = lambda v: k.ToMM(v)
    kept, killed = [], 0
    for t in board.GetTracks():
        if t.GetClass() != 'PCB_VIA' or t.IsLocked():
            continue
        p = t.GetPosition()
        if any(t.GetNetname() == q.GetNetname() and
               abs(mm(p.x) - mm(q.GetPosition().x)) < 0.5 and
               abs(mm(p.y) - mm(q.GetPosition().y)) < 0.5 for q in kept):
            board.RemoveNative(t); killed += 1
        else:
            kept.append(t)
    return killed


def main(max_passes=8):
    removed = 0
    for p in range(1, max_passes + 1):
        data = drc(PCB)
        bad = [it for v in data.get('violations', []) if v.get('type') in DANGLING
               for it in v.get('items', []) if it.get('uuid')]
        left = len(data.get('violations', [])) + len(data.get('unconnected_items', []))
        if not bad:
            print(f'pass {p}: nothing dangling ({left} other violation(s)/unconnected item(s) remain)')
            break
        uuids = {it['uuid'] for it in bad}
        b = k.LoadBoard(str(PCB))
        dup = dedupe_vias(b)
        if dup:
            print(f'  removed {dup} duplicate via(s)')
            removed += dup
            b.BuildConnectivity(); k.ZONE_FILLER(b).Fill(b.Zones()); k.SaveBoard(str(PCB), b)
            b = k.LoadBoard(str(PCB))
        victims = [t for t in b.GetTracks() if str(t.m_Uuid.AsString()) in uuids]
        for t in victims:
            # A dangling stub or unconnected via has no function, locked or not: the
            # Specctra import locks the autorouter's items, which is why they survived.
            if t.IsLocked():
                print('  removing LOCKED but dangling item:', t.m_Uuid.AsString())
            b.RemoveNative(t)
        removed += len(victims)
        b.BuildConnectivity()
        k.ZONE_FILLER(b).Fill(b.Zones())
        k.SaveBoard(str(PCB), b)
        print(f'pass {p}: removed {len(victims)} dangling item(s) of {len(uuids)} flagged')
    print(f'dead-end cleanup done: {removed} item(s) removed.')
    final = drc(PCB)
    print('final DRC: %d violation(s), %d unconnected item(s)'
          % (len(final.get('violations', [])), len(final.get('unconnected_items', []))))
    return 0


if __name__ == '__main__':
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 8))

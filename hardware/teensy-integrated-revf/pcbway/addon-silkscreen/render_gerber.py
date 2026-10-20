#!/usr/bin/env python3
"""Independent CAM readback: rasterise a KiCad silkscreen GERBER (not the board).

What PCBWay prints is the .gbr file, so the final check is to look at the file
itself. This is a deliberately small RS-274X reader covering what KiCad emits on a
silk layer: %FS/%MO, circular apertures (%ADD..C), D01/D02/D03, G01/G02/G03 (G75
multi-quadrant arcs), and G36/G37 regions. Anything else raises.

  python3 render_gerber.py <file.gbr> <out.png> [x0 x1 y0 y1 (board mm)] [--px-per-mm N]
"""
import math
import re
import sys

from PIL import Image, ImageDraw


def parse(path):
    text = open(path).read()
    fs = re.search(r'%FSLAX(\d)(\d)Y(\d)(\d)\*%', text)
    if not fs or '%MOMM*%' not in text:
        raise SystemExit('unsupported format (need %FSLAX..Y..% and %MOMM%)')
    scale = 10 ** int(fs.group(2))
    aps = {int(m.group(1)): float(m.group(2))
           for m in re.finditer(r'%ADD(\d+)C,([\d.]+)\*%', text)}
    kinds = set(re.findall(r'%ADD\d+([A-Za-z_][A-Za-z0-9_]*)', text))
    if kinds - {'C'}:
        raise SystemExit(f'non-circular aperture(s) {sorted(kinds - {"C"})} - extend the reader first')
    body = re.sub(r'%[^%]*%', '', text)
    strokes, flashes, regions = [], [], []
    x = y = 0.0
    ap = None
    mode = 'G01'
    region = None
    for cmd in body.replace('\n', '').split('*'):
        cmd = cmd.strip()
        if not cmd or cmd.startswith('G04') or cmd == 'M02':
            continue
        if cmd in ('G75', 'G74'):
            continue
        if cmd == 'G36':
            region, cur = [], []
            continue
        if cmd == 'G37':
            if cur:
                region.append(cur)
            regions.extend(region)
            region = None
            continue
        g = re.match(r'G0([123])', cmd)
        if g:
            mode = 'G0' + g.group(1)
            cmd = cmd[3:]
            if not cmd:
                continue
        m = re.fullmatch(r'D(\d+)', cmd)
        if m and int(m.group(1)) >= 10:
            ap = int(m.group(1))
            continue
        m = re.fullmatch(r'(?:X(-?\d+))?(?:Y(-?\d+))?(?:I(-?\d+))?(?:J(-?\d+))?D0([123])', cmd)
        if not m:
            raise SystemExit(f'unsupported gerber command: {cmd!r}')
        nx = int(m.group(1)) / scale if m.group(1) else x
        ny = int(m.group(2)) / scale if m.group(2) else y
        i = int(m.group(3)) / scale if m.group(3) else 0.0
        j = int(m.group(4)) / scale if m.group(4) else 0.0
        d = m.group(5)
        pts = [(nx, ny)]
        if d == '1' and mode in ('G02', 'G03'):
            cx, cy = x + i, y + j
            r = math.hypot(x - cx, y - cy)
            a0 = math.atan2(y - cy, x - cx)
            a1 = math.atan2(ny - cy, nx - cx)
            if mode == 'G03':           # counter-clockwise
                while a1 <= a0:
                    a1 += 2 * math.pi
            else:
                while a1 >= a0:
                    a1 -= 2 * math.pi
            n = max(8, int(abs(a1 - a0) * r / 0.05))
            pts = [(cx + r * math.cos(a0 + (a1 - a0) * t / n),
                    cy + r * math.sin(a0 + (a1 - a0) * t / n)) for t in range(1, n + 1)]
        if region is not None:
            if d == '2':
                if cur:
                    region.append(cur)
                cur = [(nx, ny)]
            elif d == '1':
                if not cur:
                    cur = [(x, y)]
                cur.extend(pts)
        elif d == '1':
            prev = (x, y)
            for p in pts:
                strokes.append((prev, p, aps[ap]))
                prev = p
        elif d == '3':
            flashes.append(((nx, ny), aps[ap]))
        x, y = nx, ny
    return strokes, flashes, regions


def render(path, out, window=None, ppm=40):
    strokes, flashes, regions = parse(path)
    if window is None:
        xs = [p[0] for s in strokes for p in s[:2]] or [0]
        ys = [p[1] for s in strokes for p in s[:2]] or [0]
        window = (min(xs) - 1, max(xs) + 1, -max(ys) - 1, -min(ys) + 1)
    x0, x1, y0, y1 = window          # board coordinates (KiCad: y down); gerber y = -board y
    W, H = int((x1 - x0) * ppm), int((y1 - y0) * ppm)
    im = Image.new('RGB', (W, H), (14, 64, 30))
    dr = ImageDraw.Draw(im)
    ink = (245, 245, 235)

    def px(p):
        return ((p[0] - x0) * ppm, (-p[1] - y0) * ppm)

    for poly in regions:
        dr.polygon([px(p) for p in poly], fill=ink)
    for a, b, w in strokes:
        r = w * ppm / 2
        pa, pb = px(a), px(b)
        dr.line([pa, pb], fill=ink, width=max(1, int(round(w * ppm))))
        for c in (pa, pb):
            dr.ellipse([c[0] - r, c[1] - r, c[0] + r, c[1] + r], fill=ink)
    for c, w in flashes:
        r = w * ppm / 2
        q = px(c)
        dr.ellipse([q[0] - r, q[1] - r, q[0] + r, q[1] + r], fill=ink)
    im.save(out)
    return len(strokes), len(flashes), len(regions)


if __name__ == '__main__':
    import argparse
    ap = argparse.ArgumentParser(description='rasterise a KiCad silkscreen gerber')
    ap.add_argument('gerber')
    ap.add_argument('png')
    ap.add_argument('window', nargs='*', type=float, help='x0 x1 y0 y1 in board mm')
    ap.add_argument('--px-per-mm', type=float, default=40)
    a = ap.parse_args()
    if a.window and len(a.window) != 4:
        ap.error('window needs exactly 4 numbers: x0 x1 y0 y1')
    print('strokes %d, flashes %d, regions %d'
          % render(a.gerber, a.png, tuple(a.window) or None, a.px_per_mm))

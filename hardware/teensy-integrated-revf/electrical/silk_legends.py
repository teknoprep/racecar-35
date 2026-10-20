#!/usr/bin/env python3
"""Board-level F.SilkS legends for Rev F - ONE table, two callers.

  * electrical/generate.py              - fresh board (called after references are final)
  * pcbway/addon-silkscreen/addon_silk.py - in place on the routed board (idempotent)

What it owns (nothing else on F.SilkS is touched):
  1. every field screw terminal: a TITLE centred just above the connector body
     ("J2 SCREEN") and one pin label per pad, LEFT-ALIGNED in one column right of
     the body and centred on its own pad row;
  2. the Teensy insertion-orientation marks (chevron, TEENSY / USB END / THIS EDGE,
     socket pin numbers 1 and 25);
  3. the crossed screen-harness table under J2, digits in fixed columns.

Placement is computed from the board itself (pad mask openings, footprint silk and
the other legends), not hand-typed offsets: a label sits exactly on its pad row
unless that would touch a mask opening or other silk, in which case it slides the
SMALLEST distance along its column, never more than PIN_MAX_SLIDE (so it can never
end up nearer a neighbouring pin). A label that cannot be placed raises - deleting
labels to get a clean DRC is what left whole terminals unmarked in the first upload.

Text spec: 0.90 mm high, 0.16 mm stroke (fab legend minimums are ~0.81 mm / 0.153 mm).

CLI (dev aid):  python3 silk_legends.py <board.kicad_pcb> [--write]
"""
import sys

import pcbnew as k

SIZE = 0.90           # glyph height, mm
THICK = 0.16          # stroke, mm
MASK_GAP = 0.15       # silk -> solder-mask opening
SILK_GAP = 0.35       # silk -> other silk (at 0.12 a part outline read as an overbar on the label)
EDGE_GAP = 0.50       # silk -> board outline
TITLE_GAP = 0.40      # title bottom -> top line of its own connector body
PIN_GAP_X = 0.60      # pin-label column starts this far right of the body
PIN_MAX_SLIDE = 1.80  # pad pitch is 5.08 mm: a label stays far nearer its own row
STEP = 0.05

# (reference, title, pin labels top-to-bottom). Pin 1 is always the TOP pad.
TERMINALS = [
    ('J3', 'J3 TACH', ['1 SIG', '2 RET']),
    ('J4', 'J4 OIL', ['1 +5V', '2 RET', '3 SIG']),
    ('J5', 'J5 COOLANT', ['1 NTC', '2 RET']),
    ('J6', 'J6 AEM AFR', ['1 WHT+', '2 BRN-']),
    ('J11', 'J11 THROTTLE', ['1 +5V', '2 RET', '3 SIG']),
    ('J12', 'J12 BRAKE', ['1 +5V', '2 RET', '3 SIG']),
    ('J1', 'J1 10-20V IN', ['1 VIN+', '2 GND']),
    ('J2', 'J2 SCREEN', ['1 GND', '2 +5V', '3 RX', '4 TX']),
]

# A warning printed directly above a terminal's name (the tach input takes a conditioned
# ECU / cluster tach signal ONLY - never the coil negative or an injector drive).
NOTES = [('J3', 'NO COIL / INJECTOR')]

# Every other connector, jumper and the RTC cell: a name AT the part (no reference
# designators are printed on this board, so the name is the only identification).
#   row  - one horizontal pin row: name centred above, a label under each pad
#          (rotated, hanging from the body, when the pitch is too tight to read flat)
#   dual - 2xN header: odd pins labelled on the left, even pins on the right
#   side - name only, in the first clear spot of `order`
PARTS = [
    # J13 -> Raspberry Pi 5 40-pin header, straight (NOT crossed): this board's TX goes
    # to the Pi's RXD (GPIO15, header pin 10), its RX to the Pi's TXD (GPIO14, pin 8).
    dict(ref='J13', style='row', name='J13 PI 5 VIDEO 3V3',
         labels=['1 GND', '2 TX>PI 10', '3 RX<PI 8', '4 GND']),
    # J9 -> the Teensy's underside VBAT / GND pads (a hand-made lead; ASSEMBLY.md step 6)
    dict(ref='J9', style='row', name='J9 RTC LEAD', labels=['1 VBAT', '2 GND']),
    # J14's wires enter from the BOTTOM (pin-1 triangle side), so its labels go on the
    # TOP, where wiring cannot cover them. One word over each terminal: '1 CANH 2 CANL
    # 3 GND' on a 5.08 mm pitch ran together and read as 'CANH 2'. Pin 1 = CANH, marked
    # by the footprint's pin-1 triangle (ASSEMBLY.md: 1 CANH / 2 CANL / 3 GND).
    dict(ref='J14', style='row', name='J14 CAN', labels=['CANH', 'CANL', 'GND'],
         side='above', name_order=('left', 'right', 'below')),
    dict(ref='J10', style='dual', name='J10 NET PROG 3V3',
         labels=['1 GND', '2 3V3', '3 TX OUT', '4 RX IN', '5 BOOT', '6 EN']),
    dict(ref='J8', style='side', name='J8 GPS ANT', name_order=('right', 'left', 'below')),
    dict(ref='JP1', style='side', name='JP1 TEENSY PWR', name_order=('right', 'left', 'below')),
    dict(ref='JP2', style='side', name='JP2 CAN TERM: BUS END ONLY',
         name_order=('above', 'below', 'right', 'left')),
    dict(ref='BT1', style='side', name='BT1 CR2032 RTC - NO CHARGER', name_order=('above', 'below')),
]
ROTATE_BELOW_PITCH = 4.0   # mm; tighter pin rows get rotated labels
PART_GAP = 0.40            # name / label -> the part's own outline

# Teensy 4.1 socket (U1): pad 1 = GND / pad 25 = TEENSY_VIN at the USB end (top).
# The 2x24 socket is symmetric, so a 180-degree or one-pin-off insertion also fits.
TEENSY_TEXT = [  # text, x, y, size, stroke (centre-justified)
    ('TEENSY', 67.62, 31.2, 1.0, 0.16),
    ('USB END', 67.62, 34.0, 1.3, 0.20),
    ('THIS EDGE', 67.62, 36.6, 0.95, 0.16),
    ('1', 62.6, 28.2, 1.0, 0.16),
    ('25', 72.3, 28.2, 0.95, 0.16),
]
TEENSY_CHEVRON = [((65.6, 29.4), (67.62, 26.9)), ((67.62, 26.9), (69.64, 29.4))]
CHEVRON_W = 0.5

# Screen harness: J2 counts GND first, the CrowPanel Advance J10 counts its UART
# first (1 RXD0_H / 2 TXD0_H / 3 +5V_IN / 4 GND), so the cable is wired 1-4, 2-3,
# 3-2, 4-1. Digits sit in fixed columns so each J2 pin is directly above its partner.
CROSS_TITLE = 'J2 -> SCREEN J10: CROSSED'
CROSS_ROWS = [('J2', ['1', '2', '3', '4']), ('J10', ['4', '3', '2', '1'])]
CROSS_COL_X = [35.0, 37.6, 40.2, 42.8]    # digit centres
CROSS_LABEL_RIGHT = 32.8                  # row labels end here (right-justified)
CROSS_Y = [164.6, 166.9, 169.2]           # title row, J2 row, J10 row

IU = 1e6


def _P(x, y):
    return k.VECTOR2I(int(round(x * IU)), int(round(y * IU)))


def _mm(v):
    return v / IU


def _bbox(item):
    r = item.GetBoundingBox()
    return (_mm(r.GetLeft()), _mm(r.GetTop()), _mm(r.GetRight()), _mm(r.GetBottom()))


def _managed_strings():
    s = set()
    for _, title, labels in TERMINALS:
        s.add(title)
        s.update(labels)
    s.update(t[0] for t in TEENSY_TEXT)
    s.add(CROSS_TITLE)
    for lab, digits in CROSS_ROWS:
        s.add(lab)
        s.update(digits)
    s.update(n for _, n in NOTES)
    for part in PARTS:
        s.add(part['name'])
        s.update(part.get('labels', []))
    # labels the first artwork pass left far from their parts (28-44 mm away)
    s.update({'PI 5 VIDEO UART 3V3', 'GPS SMA', 'RTC 3V NO CHARGE',
              'CAN TERM FIT ONLY AT BUS END', '1 CANH', '2 CANL', '3 GND',
              'J13 PI 5 UART 3V3'})
    # strings written by earlier artwork passes (removed so a re-run cannot duplicate)
    s.update({'TACH', 'OIL', 'NTC', 'AEM AFR', 'THROTTLE', 'BRAKE', '10-20V IN',
              'SCREEN J10', 'J2 -> J10 CROSSED', 'J2:  1  2  3  4', 'J10: 4  3  2  1'})
    return s


def remove(board):
    """Delete every board-level F.SilkS item this module owns. Returns the count."""
    owned = _managed_strings()

    def key(a, b):  # an undirected segment, endpoints rounded to 0.01 mm
        return frozenset({(round(a[0], 2), round(a[1], 2)), (round(b[0], 2), round(b[1], 2))})
    chevron = {key(a, b) for a, b in TEENSY_CHEVRON}
    gone = 0
    for d in list(board.GetDrawings()):
        if d.GetLayer() != k.F_SilkS:
            continue
        if isinstance(d, k.PCB_TEXT) and d.GetText() in owned:
            board.RemoveNative(d)
            gone += 1
        elif isinstance(d, k.PCB_SHAPE) and d.GetShape() == k.SHAPE_T_SEGMENT:
            a, b = d.GetStart(), d.GetEnd()
            if key((_mm(a.x), _mm(a.y)), (_mm(b.x), _mm(b.y))) in chevron:
                board.RemoveNative(d)
                gone += 1
    return gone


class _Field:
    """Obstacles on the front silkscreen: mask openings, other silk, the outline."""

    def __init__(self, board):
        self.mask, self.silk = [], []
        for f in board.GetFootprints():
            for p in f.Pads():
                if p.IsOnLayer(k.F_Mask):
                    self.mask.append(_bbox(p))
            for g in f.GraphicalItems():
                if g.GetLayer() == k.F_SilkS and not (isinstance(g, k.PCB_TEXT) and not g.IsVisible()):
                    self.silk.append(_bbox(g))
        for t in board.GetTracks():
            if t.GetClass() == 'PCB_VIA' and t.IsOnLayer(k.F_Mask):
                self.mask.append(_bbox(t))
        for d in board.GetDrawings():
            if d.GetLayer() == k.F_SilkS:
                self.silk.append(_text_box(d) if isinstance(d, k.PCB_TEXT) else _bbox(d))
        e = board.GetBoardEdgesBoundingBox()
        self.edge = (_mm(e.GetLeft()), _mm(e.GetTop()), _mm(e.GetRight()), _mm(e.GetBottom()))

    def clear(self, box):
        x0, y0, x1, y1 = box
        ex0, ey0, ex1, ey1 = self.edge
        if x0 < ex0 + EDGE_GAP or y0 < ey0 + EDGE_GAP or x1 > ex1 - EDGE_GAP or y1 > ey1 - EDGE_GAP:
            return False
        for obs, gap in ((self.mask, MASK_GAP), (self.silk, SILK_GAP)):
            for a, b, c, d in obs:
                if x1 + gap > a and x0 - gap < c and y1 + gap > b and y0 - gap < d:
                    return False
        return True


def _text_box(t):
    r = t.GetEffectiveTextShape().BBox()
    return (_mm(r.GetLeft()), _mm(r.GetTop()), _mm(r.GetRight()), _mm(r.GetBottom()))


def _new_text(board, s, x, y, size=SIZE, thick=THICK, h=k.GR_TEXT_H_ALIGN_CENTER):
    t = k.PCB_TEXT(board)
    t.SetText(s)
    t.SetLayer(k.F_SilkS)
    t.SetTextSize(_P(size, size))
    t.SetTextThickness(k.FromMM(thick))
    t.SetHorizJustify(h)
    t.SetVertJustify(k.GR_TEXT_V_ALIGN_CENTER)
    t.SetPosition(_P(x, y))
    return t


def _place(board, field, t, candidates, what):
    """Move t through `candidates` (x, y) in order; keep the first clear spot."""
    for x, y in candidates:
        t.SetPosition(_P(x, y))
        box = _text_box(t)
        if field.clear(box):
            board.Add(t)
            field.silk.append(box)
            return x, y
    raise RuntimeError(f'silk_legends: no clear spot for {what} - fix the layout, '
                       f'do not delete the label')


def _slides(limit):
    yield 0.0
    n = int(round(limit / STEP))
    for i in range(1, n + 1):
        yield i * STEP
        yield -i * STEP


def _outline(fp):
    """Whole F.SilkS extent of a footprint (its printed outline)."""
    segs = [_bbox(g) for g in fp.GraphicalItems() if g.GetLayer() == k.F_SilkS]
    pads = [_bbox(p) for p in fp.Pads()]
    allb = segs + pads
    return (min(s[0] for s in allb), min(s[1] for s in allb),
            max(s[2] for s in allb), max(s[3] for s in allb))


def _size(board, s, angle=0):
    t = _new_text(board, s, 0, 0)
    if angle:
        t.SetTextAngleDegrees(angle)
    x0, y0, x1, y1 = _text_box(t)
    return x1 - x0, y1 - y0


def _body(fp):
    """Connector body outline = the long F.SilkS segments of the terminal block."""
    segs = [_bbox(g) for g in fp.GraphicalItems() if g.GetLayer() == k.F_SilkS]
    body = [s for s in segs if max(s[2] - s[0], s[3] - s[1]) > 3.0]
    if not body:
        raise RuntimeError(f'{fp.GetReference()}: no body outline on F.SilkS')
    return (min(s[0] for s in body), min(s[1] for s in body),
            max(s[2] for s in body), max(s[3] for s in body))


def min_stroke(board, thick=THICK, height=SIZE):
    """Raise every board-level F.SilkS text to the legend minimums (idempotent):
    stroke >= THICK and height >= SIZE (fab minimums are ~0.153 mm / ~0.81 mm)."""
    n = 0
    for d in board.GetDrawings():
        if not (isinstance(d, k.PCB_TEXT) and d.GetLayer() == k.F_SilkS):
            continue
        changed = False
        if d.GetTextThickness() < k.FromMM(thick):
            d.SetTextThickness(k.FromMM(thick))
            changed = True
        if d.GetTextHeight() < k.FromMM(height) - 1:
            d.SetTextSize(_P(height, height))
            changed = True
        n += changed
    return n


def apply(board, log=print):
    """Remove the owned legends and lay them out again. Returns a placement report."""
    removed = remove(board)
    thickened = min_stroke(board)
    field = _Field(board)
    fps = {f.GetReference(): f for f in board.GetFootprints()}
    report = []

    # Teensy orientation marks + harness table first: they are fixed artwork, the
    # terminal labels flow around them.
    for s, x, y, size, thick in TEENSY_TEXT:
        t = _new_text(board, s, x, y, size, thick)
        board.Add(t)
        field.silk.append(_text_box(t))
    for a, b in TEENSY_CHEVRON:
        seg = k.PCB_SHAPE(board)
        seg.SetShape(k.SHAPE_T_SEGMENT)
        seg.SetStart(_P(*a)); seg.SetEnd(_P(*b))
        seg.SetWidth(k.FromMM(CHEVRON_W)); seg.SetLayer(k.F_SilkS)
        board.Add(seg)
        field.silk.append(_bbox(seg))
    cx = (CROSS_LABEL_RIGHT + CROSS_COL_X[-1]) / 2
    t = _new_text(board, CROSS_TITLE, cx, CROSS_Y[0])
    _place(board, field, t, [(cx, CROSS_Y[0])], 'harness title')
    for (lab, digits), y in zip(CROSS_ROWS, CROSS_Y[1:]):
        t = _new_text(board, lab, CROSS_LABEL_RIGHT, y, h=k.GR_TEXT_H_ALIGN_RIGHT)
        _place(board, field, t, [(CROSS_LABEL_RIGHT, y)], f'harness row {lab}')
        for d, x in zip(digits, CROSS_COL_X):
            t = _new_text(board, d, x, y)
            _place(board, field, t, [(x, y)], f'harness {lab} digit {d}')

    for ref, title, labels in TERMINALS:
        fp = fps[ref]
        bx0, by0, bx1, by1 = _body(fp)
        rows = sorted(_mm(p.GetPosition().y) for p in fp.Pads())
        if len(rows) != len(labels):
            raise RuntimeError(f'{ref}: {len(rows)} pads but {len(labels)} labels')
        # title: centred over the body, bottom TITLE_GAP above its top line
        t = _new_text(board, title, 0, 0)
        h = _text_box(t)[3] - _text_box(t)[1]
        ty = by0 - TITLE_GAP - h / 2
        tx = (bx0 + bx1) / 2
        cands = [(tx + dx, ty - dy) for dy in (i * STEP for i in range(0, 13))
                 for dx in _slides(3.0)]
        px, py = _place(board, field, t, cands, f'{ref} title')
        report.append((ref, title, px, py, round(px - tx, 2), round(py - ty, 2)))
        # pin labels: left-aligned column, centred on each pad row
        lx = bx1 + PIN_GAP_X
        for lab, ry in zip(labels, rows):
            t = _new_text(board, lab, lx, ry, h=k.GR_TEXT_H_ALIGN_LEFT)
            px, py = _place(board, field, t, [(lx, ry + dy) for dy in _slides(PIN_MAX_SLIDE)],
                            f'{ref} pin label {lab!r}')
            report.append((ref, lab, px, py, 0.0, round(py - ry, 2)))
    for ref, note in NOTES:
        title = next(tt for r, tt, _ in TERMINALS if r == ref)
        tbox = next(_text_box(d) for d in board.GetDrawings()
                    if isinstance(d, k.PCB_TEXT) and d.GetText() == title)
        w, h = _size(board, note)
        nx, ny = (tbox[0] + tbox[2]) / 2, tbox[1] - SILK_GAP - 0.05 - h / 2
        t = _new_text(board, note, nx, ny)
        px, py = _place(board, field, t, [(nx + dx, ny - dy) for dy in (i * STEP for i in range(0, 25))
                                          for dx in _slides(6.0)], f'{ref} note {note!r}')
        report.append((ref, note, px, py, round(px - nx, 2), round(py - ny, 2)))

    for part in PARTS:
        ref, style, name = part['ref'], part['style'], part['name']
        labels, size = part.get('labels', []), part.get('size', SIZE)
        fp = fps[ref]
        ox0, oy0, ox1, oy1 = _outline(fp)
        pads = sorted(fp.Pads(), key=lambda p: (int(p.GetNumber()), _mm(p.GetPosition().x)))
        if labels and len(pads) != len(labels):
            raise RuntimeError(f'{ref}: {len(pads)} pads but {len(labels)} labels')
        if style == 'row':
            pitch = abs(_mm(pads[1].GetPosition().x) - _mm(pads[0].GetPosition().x))
            up = part.get('side', 'below') == 'above'
            for lab, p in zip(labels, pads):
                x = _mm(p.GetPosition().x)
                if pitch < ROTATE_BELOW_PITCH:
                    # rotated 90 deg (reads bottom-to-top). Below the row it is RIGHT-justified
                    # so it hangs down from the body; above, LEFT-justified so it stands up.
                    y0 = oy0 - PART_GAP if up else oy1 + PART_GAP
                    t = _new_text(board, lab, x, y0, size,
                                  h=k.GR_TEXT_H_ALIGN_LEFT if up else k.GR_TEXT_H_ALIGN_RIGHT)
                    t.SetTextAngleDegrees(90)
                    cands = [(x, y0 - dy if up else y0 + dy) for dy in (i * STEP for i in range(0, 21))]
                else:
                    t = _new_text(board, lab, 0, 0, size)
                    lh = _text_box(t)[3] - _text_box(t)[1]
                    y0 = oy0 - PART_GAP - lh / 2 if up else oy1 + PART_GAP + lh / 2
                    cands = [(x, y0 - dy if up else y0 + dy) for dy in (i * STEP for i in range(0, 21))]
                qx, qy = _place(board, field, t, cands, f'{ref} pin label {lab!r}')
                report.append((ref, lab, qx, qy, 0.0, 0.0))
        elif style == 'dual':
            for lab, p in zip(labels, pads):
                odd = int(p.GetNumber()) % 2 == 1
                y = _mm(p.GetPosition().y)
                x = ox0 - PART_GAP if odd else ox1 + PART_GAP
                t = _new_text(board, lab, x, y, size,
                              h=k.GR_TEXT_H_ALIGN_RIGHT if odd else k.GR_TEXT_H_ALIGN_LEFT)
                qx, qy = _place(board, field, t, [(x, y + dy) for dy in _slides(0.6)],
                                f'{ref} pin label {lab!r}')
                report.append((ref, lab, qx, qy, 0.0, round(qy - y, 2)))
        # name LAST: pin labels must sit on their pads; the name can go anywhere near
        w, h = _size(board, name)
        cx, cy = (ox0 + ox1) / 2, (oy0 + oy1) / 2
        spots = {
            'above': [(cx + dx, oy0 - PART_GAP - h / 2 - dy) for dy in (i * STEP for i in range(0, 21))
                      for dx in _slides(6.0)],
            'below': [(cx + dx, oy1 + PART_GAP + h / 2 + dy) for dy in (i * STEP for i in range(0, 21))
                      for dx in _slides(6.0)],
            'right': [(ox1 + PART_GAP + w / 2 + dx, cy + dy) for dx in (i * STEP for i in range(0, 21))
                      for dy in _slides(2.0)],
            'left': [(ox0 - PART_GAP - w / 2 - dx, cy + dy) for dx in (i * STEP for i in range(0, 21))
                     for dy in _slides(2.0)],
        }
        order = part.get('name_order', ('above', 'right', 'left', 'below'))
        t = _new_text(board, name, cx, cy)
        px, py = _place(board, field, t, [c for side in order for c in spots[side]], f'{ref} name')
        report.append((ref, name, px, py, 0.0, 0.0))

    log(f'silk_legends: removed {removed} old items, raised {thickened} other texts to '
        f'{SIZE} mm / {THICK} mm stroke, placed {len(report)} terminal labels + Teensy marks + harness table')
    return report


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    b = k.LoadBoard(sys.argv[1])
    for ref, s, x, y, dx, dy in apply(b):
        flag = '' if abs(dy) < 1e-9 and abs(dx) < 1e-9 else f'   (slid dx={dx:+.2f} dy={dy:+.2f})'
        print(f'  {ref:4} {s:14} at ({x:6.2f},{y:7.2f}){flag}')
    if '--write' in sys.argv:
        b.Save(sys.argv[1])
        print('saved', sys.argv[1])

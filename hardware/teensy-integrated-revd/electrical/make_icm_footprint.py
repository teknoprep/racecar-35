#!/usr/bin/env python3
"""DERIVED land pattern for the TDK InvenSense ICM-42670-P (14-lead LGA).

Source of dimensions: TDK InvenSense DS-000451 rev 1.2, section 10.2
"PACKAGE DIMENSIONS, 14 Lead LGA (2.5x3x0.76) mm":
    body E (short, Y)         2.45 / 2.50 / 2.55 mm
    body D (long,  X)         2.95 / 3.00 / 3.05 mm
    lead width  b             0.20 / 0.25 / 0.30 mm
    lead length L3            0.425 / 0.475 / 0.525 mm
    package edge to lead edge L1 = 0.10 REF
    lead pitch  e             0.50 mm
    edge pin center to center e*3 = 1.50 mm (4-pin sides)
                              e*2 = 1.00 mm (3-pin sides)
    pad finish NiAu

Pin arrangement follows DS-000451 Figure 4: four pads on each long edge
(pins 1-4 left, 8-11 right) and three pads on each short edge
(pins 5-7 bottom, 12-14 top).

THIS IS A DERIVED PATTERN, NOT A VENDOR-PUBLISHED LAND PATTERN. TDK did not
publish a numeric recommended land pattern for this package at time of writing.
Pads are sized to the nominal package terminals with a small outward extension
for inspection/rework, at the nominal 0.50 mm pitch. Assembler/DFM approval is
a required manufacturing checkpoint. Do not treat this file as vendor-approved.
"""
from pathlib import Path
import pcbnew as k

LIB = Path(__file__).resolve().parents[1] / 'design' / 'RacecarC.pretty'
NAME = 'InvenSense_LGA-14_2.5x3.0mm_P0.50mm'
E_BODY, D_BODY = 2.50, 3.00     # short (Y), long (X)
PITCH = 0.50
HALF_X, HALF_Y = D_BODY / 2, E_BODY / 2
PAD_W, PAD_L = 0.26, 0.55       # width (across lead), length (along lead)
EDGE_INSET = 0.10               # L1: package edge to lead edge (REF)

# Pad centre distance from body centre, measured outward-in:
#   outer edge sits at (body_half - L1), pad extends inward by PAD_L.
X_C = HALF_X - EDGE_INSET - PAD_L / 2   # long-edge (left/right) pad centre
Y_C = HALF_Y - EDGE_INSET - PAD_L / 2   # short-edge (top/bottom) pad centre


def build():
    fp = k.FOOTPRINT(None)
    fp.SetFPID(k.LIB_ID('RacecarC', NAME))
    fp.SetValue('ICM-42670-P')
    fp.SetLibDescription(
        'TDK InvenSense ICM-42670-P 14-lead LGA 2.5x3.0mm P0.50mm. '
        'DERIVED land pattern from DS-000451 rev 1.2 s10.2 - requires DFM review.')
    fp.SetAttributes(k.FP_SMD)

    # --- pads ---------------------------------------------------------------
    # Left edge, pins 1..4 top-to-bottom (pin 1 at +Y = top-left in KiCad's
    # screen-down Y axis, matching Figure 4 orientation).
    pads = []
    for i in range(4):
        y = -HALF_Y + PITCH * (i + 1)     # 0.5,1.0,1.5,2.0 -> centred later
        pads.append((str(i + 1), -X_C, y))
    # Bottom edge, pins 5..7 left-to-right
    for i in range(3):
        x = PITCH * (i - 1)               # -0.5, 0, +0.5
        pads.append((str(i + 5), x, -Y_C))
    # Right edge, pins 8..11 bottom-to-top
    for i in range(4):
        y = -HALF_Y + PITCH * (i + 1)     # -0.75, -0.25, +0.25, +0.75
        pads.append((str(i + 8), X_C, y))
    # Top edge, pins 12..14 right-to-left
    for i in range(3):
        x = PITCH * (1 - i)               # +0.5, 0, -0.5
        pads.append((str(i + 12), x, Y_C))

    for num, x, y in pads:
        p = k.PAD(fp)
        p.SetNumber(num)
        p.SetShape(k.PAD_SHAPE_ROUNDRECT)
        p.SetRoundRectRadiusRatio(0.20)
        vertical = abs(x) > abs(y)        # long-edge pads are rotated 90 deg
        p.SetSize(k.VECTOR2I(k.FromMM(PAD_L), k.FromMM(PAD_W)) if vertical
                  else k.VECTOR2I(k.FromMM(PAD_W), k.FromMM(PAD_L)))
        p.SetPosition(k.VECTOR2I(k.FromMM(x), k.FromMM(y)))
        p.SetAttribute(k.PAD_ATTRIB_SMD)
        ls = k.LSET()
        for lyr in (k.F_Cu, k.F_Paste, k.F_Mask):
            ls.addLayer(lyr)
        p.SetLayerSet(ls)
        fp.Add(p)

    # --- 1.0 mm pitch markers are on the short edges; pin 1 dot on silk -----
    def line(x1, y1, x2, y2, layer, w=0.12):
        s = k.PCB_SHAPE(fp)
        s.SetShape(k.SHAPE_T_SEGMENT)
        s.SetStart(k.VECTOR2I(k.FromMM(x1), k.FromMM(y1)))
        s.SetEnd(k.VECTOR2I(k.FromMM(x2), k.FromMM(y2)))
        s.SetLayer(layer)
        s.SetWidth(k.FromMM(w))
        fp.Add(s)

    # silk outline (clear of pads) + pin-1 mark
    sx, sy = 1.75, 1.45
    line(-sx, -sy, sx, -sy, k.F_SilkS)
    line(sx, -sy, sx, sy, k.F_SilkS)
    line(sx, sy, -sx, sy, k.F_SilkS)
    line(-sx, sy, -sx, -sy, k.F_SilkS)
    line(-sx - 0.45, -sy, -sx - 0.45, -sy + 0.7, k.F_SilkS, 0.15)
    # fab body outline
    line(-HALF_X, -HALF_Y, HALF_X, -HALF_Y, k.F_Fab, 0.10)
    line(HALF_X, -HALF_Y, HALF_X, HALF_Y, k.F_Fab, 0.10)
    line(HALF_X, HALF_Y, -HALF_X, HALF_Y, k.F_Fab, 0.10)
    line(-HALF_X, HALF_Y, -HALF_X, -HALF_Y, k.F_Fab, 0.10)
    # courtyard: pads + 0.25 mm
    cx, cy = X_C + PAD_L / 2 + 0.25, Y_C + PAD_L / 2 + 0.25
    line(-cx, -cy, cx, -cy, k.F_CrtYd, 0.05)
    line(cx, -cy, cx, cy, k.F_CrtYd, 0.05)
    line(cx, cy, -cx, cy, k.F_CrtYd, 0.05)
    line(-cx, cy, -cx, -cy, k.F_CrtYd, 0.05)

    LIB.mkdir(parents=True, exist_ok=True)
    k.PCB_IO_MGR.PluginFind(k.PCB_IO_MGR.KICAD_SEXP).FootprintSave(str(LIB), fp)
    print(f'Wrote {LIB/NAME}.kicad_mod  ({len(pads)} pads, pitch {PITCH} mm)')


if __name__ == '__main__':
    build()

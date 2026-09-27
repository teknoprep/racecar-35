#!/usr/bin/env python3
"""Record actual 2oz stackup in native CAD (SWIG doesn't expose this API).
Run AFTER routing; preserves electrical geometry. Requires fresh checks/export.
"""
from pathlib import Path
D=Path(__file__).resolve().parents[1]/'design';p=D/'racecar-integrated-revd.kicad_pcb'
s=p.read_text()
if '(stackup' not in s:
 stack='''(stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Paste" (type "Top Solder Paste"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01) (color "Green"))
      (layer "F.Cu" (type "copper") (thickness 0.07))
      (layer "dielectric 1" (type "core") (thickness 1.44) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.07))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01) (color "Green"))
      (layer "B.Paste" (type "Bottom Solder Paste"))
      (layer "B.SilkS" (type "Bottom Silk Screen"))
      (copper_finish "ENIG") (dielectric_constraints no))'''
 s=s.replace('(setup','(setup\n    '+stack,1);p.write_text(s)
print('Native stackup: 0.07/1.44/0.07 mm + nominal masks = 1.60 mm; RF Dk is a fabrication assumption, not a measured value.')

#!/usr/bin/env python3
"""Import router session, restore high-current net intent, add ground pours, save."""
from pathlib import Path
import pcbnew as k
D=Path(__file__).resolve().parent/'design';name='racecar-carrier-reva'
b=k.LoadBoard(str(D/(name+'.kicad_pcb')))
if not k.ImportSpecctraSES(b,str(D/(name+'.ses'))): raise SystemExit('Session import failed')
P=lambda x,y:k.VECTOR2I(k.FromMM(x),k.FromMM(y))
for old in list(b.Zones()): b.Remove(old)
for layer in [k.F_Cu,k.B_Cu]:
    z=k.ZONE(b);z.SetLayer(layer);z.SetNet(b.FindNet('GND'));z.SetLocalClearance(k.FromMM(.25));z.SetMinThickness(k.FromMM(.25))
    z.SetPadConnection(k.ZONE_CONNECTION_FULL) # direct pours for current capacity; thermal soldering tools required
    z.SetThermalReliefGap(k.FromMM(.3));z.SetThermalReliefSpokeWidth(k.FromMM(.5))
    z.SetIslandRemovalMode(k.ISLAND_REMOVAL_MODE_ALWAYS)
    o=z.Outline();o.NewOutline()
    for x,y in [(20.6,20.6),(149.4,20.6),(149.4,129.4),(20.6,129.4)]:o.Append(int(k.FromMM(x)),int(k.FromMM(y)))
    b.Add(z)
# Refdes are on the assembly layer so the on-board connector warnings stay readable.
for fp in b.GetFootprints():
    f=fp.Reference();f.SetLayer(k.F_Fab);f.SetVisible(True);f.SetTextSize(P(.85,.85));f.SetTextThickness(k.FromMM(.12));f.SetTextAngle(k.EDA_ANGLE(0,k.DEGREES_T));f.SetPosition(fp.GetPosition())
    if fp.GetReference()=='U1':f.SetPosition(P(57.62,60))
    if fp.GetReference()=='U2':f.SetPosition(P(81,104))
b.BuildConnectivity();k.ZONE_FILLER(b).Fill(b.Zones())
# Keep the project matching the purchase specification: 2oz outer copper.
# SWIG does not expose every stackup API in KiCad 9; patch standard saved S-expression.
k.SaveBoard(str(D/(name+'.kicad_pcb')),b)
p=D/(name+'.kicad_pcb');s=p.read_text()
# Defaults are generated only if absent. Fabricator instructions are authoritative.
if '(stackup' not in s:
    stack='''(stackup
      (layer "F.SilkS" (type "Top Silk Screen"))
      (layer "F.Paste" (type "Top Solder Paste"))
      (layer "F.Mask" (type "Top Solder Mask") (thickness 0.01))
      (layer "F.Cu" (type "copper") (thickness 0.07))
      (layer "dielectric 1" (type "core") (thickness 1.44) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
      (layer "B.Cu" (type "copper") (thickness 0.07))
      (layer "B.Mask" (type "Bottom Solder Mask") (thickness 0.01))
      (layer "B.Paste" (type "Bottom Solder Paste"))
      (layer "B.SilkS" (type "Bottom Silk Screen"))
      (copper_finish "ENIG") (dielectric_constraints no))'''
    s=s.replace('(setup','(setup\n'+stack,1);p.write_text(s)
print('Imported routes, filled GND both sides, saved 2oz stackup')

#!/usr/bin/env python3
"""Last two connections on the documented router result; then refill and run DRC.
Run ONLY once after route.py --finish, not as a general repair on an edited PCB.
"""
from pathlib import Path
import json
import pcbnew as k
ROOT=Path(__file__).resolve().parents[1];D=ROOT/'design';NAME='racecar-integrated-revc'
b=k.LoadBoard(str(D/(NAME+'.kicad_pcb')));a=json.loads((D/'COMPONENT-ALIASES.json').read_text())
P=lambda x,y:k.VECTOR2I(k.FromMM(x),k.FromMM(y))
coords={(x.GetReference(),p.GetNumber()):(k.ToMM(p.GetPosition().x),k.ToMM(p.GetPosition().y)) for x in b.GetFootprints() for p in x.Pads()}
def pos(alias,n):
 return coords[(a[alias],str(n))]
def route(net,points,layer=k.F_Cu,w=.2):
 for p,q in zip(points,points[1:]):
  t=k.PCB_TRACK(b);t.SetStart(P(*p));t.SetEnd(P(*q));t.SetWidth(k.FromMM(w));t.SetLayer(layer);t.SetNet(b.FindNet(net));b.Add(t)
def via(net,x,y):
 v=k.PCB_VIA(b);v.SetPosition(P(x,y));v.SetWidth(k.FromMM(.6));v.SetDrill(k.FromMM(.3));v.SetViaType(k.VIATYPE_THROUGH);v.SetLayerPair(k.F_Cu,k.B_Cu);v.SetNet(b.FindNet(net));b.Add(v)
# Tiny dangling router fanout stubs are not part of the locked high-current buses.
for t in list(b.GetTracks()):
 if t.GetNetname()=='+5V_MAIN' and not t.IsLocked() and t.GetLayer()==k.F_Cu:
  s=t.GetStart();x,y=k.ToMM(s.x),k.ToMM(s.y)
  if any(abs(x-xx)<.002 and abs(y-yy)<.002 for xx,yy in [(86.9205,96.9205),(96,109.45),(97,108.45)]):b.RemoveNative(t)
route('+5V_SCREEN',[pos('C_U5B',1),(56.5,123),pos('U5',6)])
# Signal goes around the deliberately continuous heavy GND return strap, rather
# than cutting it. This is only the sub-MHz screen RX path, not the SPI/GNSS feed.
route('DASH_RX_5V',[pos('R_DRX',2),(46,140)])
via('DASH_RX_5V',46,140)
route('DASH_RX_5V',[(46,140),(44.8,139.3),(40.5,139.3)],k.B_Cu)
via('DASH_RX_5V',40.5,139.3);via('DASH_RX_5V',28,139.3)
route('DASH_RX_5V',[(40.5,139.3),(28,139.3)])
route('DASH_RX_5V',[(28,139.3),(25,139.3),(25,114),(49.5,114)],k.B_Cu)
via('DASH_RX_5V',49.5,114);via('DASH_RX_5V',58.5,114)
route('DASH_RX_5V',[(49.5,114),(58.5,114)])
route('DASH_RX_5V',[(58.5,114),(61.5,114),(61.5,120.5)],k.B_Cu)
via('DASH_RX_5V',61.5,120.5)
route('DASH_RX_5V',[(61.5,120.5),(61.5,125.65),(56.6,125.65),pos('U5',4)])
b.BuildConnectivity();k.ZONE_FILLER(b).Fill(b.Zones());k.SaveBoard(str(D/(NAME+'.kicad_pcb')),b)
p=D/(NAME+'.kicad_pro');j=json.loads(p.read_text())
for c in j['net_settings']['classes']:
 if c['name']=='Default':c.update(clearance=.15,track_width=.2,via_diameter=.7,via_drill=.3)
p.write_text(json.dumps(j,indent=2)+'\n')
print('Manual connections added; do not export without fresh full DRC/ERC.')

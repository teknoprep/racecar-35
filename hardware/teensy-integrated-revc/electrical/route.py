#!/usr/bin/env python3
"""Prepare locked power/RF routes and DSN for the actual Rev C electrical PCB.
Run once after generate.py. --finish imports the autorouter SES and fills ground.
This is CAD preparation, not current/thermal/RF certification.
"""
from pathlib import Path
import argparse,re,json
import pcbnew as k
ROOT=Path(__file__).resolve().parents[1];D=ROOT/'design';NAME='racecar-integrated-revc'
P=lambda x,y:k.VECTOR2I(k.FromMM(x),k.FromMM(y))
b=k.LoadBoard(str(D/(NAME+'.kicad_pcb')))
by_ref={str(f.GetReference()):f for f in b.GetFootprints()}
fp={alias:by_ref[ref] for alias,ref in json.loads((D/'COMPONENT-ALIASES.json').read_text()).items()}
def pos(ref,n):
 p=next(p for p in fp[ref].Pads() if str(p.GetNumber())==str(n)).GetPosition()
 return k.ToMM(p.x),k.ToMM(p.y)
def route(net,points,width,layer=k.F_Cu):
 for a,z in zip(points,points[1:]):
  if a==z:continue
  t=k.PCB_TRACK(b);t.SetStart(P(*a));t.SetEnd(P(*z));t.SetWidth(k.FromMM(width));t.SetLayer(layer);t.SetNet(b.FindNet(net));t.SetLocked(True);b.Add(t)
p=argparse.ArgumentParser();p.add_argument('--finish',action='store_true');args=p.parse_args()
if not args.finish:
 if list(b.GetTracks()):raise SystemExit('PCB already contains routes; refusing to overwrite routed work')
 route('VIN_RAW',[pos('J1',1),pos('F1',1)],3)
 route('VIN_FUSED',[pos('F1',2),(47,112),(65,112),(65,106.62),pos('U2',7),pos('U2',8)],2.2)
 route('VIN_FUSED',[(47,112),(44,112),(44,108),pos('D1',1)],1.2)
 route('+5V_MAIN',[pos('U2',1),pos('U2',2),(82,99)],2.6)
 route('+5V_MAIN',[(82,99),(96,99),(96,124),(54,129),pos('F2',1)],4)
 route('+5V_SCREEN',[pos('F2',2),(40,136.08),pos('J2',2)],4)
 route('GND',[pos('J1',2),(35,124),(61,124),(82,112),(82,101.54)],4,k.B_Cu)
 route('GND',[pos('J2',1),(35,124)],4,k.B_Cu)
 for pin in (3,5):
  y=pos('U2',pin)[1]
  route('GND',[(82,y),pos('U2',pin+1),pos('U2',pin)],2.2,k.B_Cu)
 route('VIN_PROTECTED',[pos('U2',9),pos('U2',10)],2.2)
 route('VIN_PROTECTED',[pos('U2',10),(73,109.16),(73,116),pos('C1',1)],1.0)
 # RF geometry locked before router. GCPW impedance must be reviewed against
 # fabricator stackup; a short trace is NOT a substitute for RF validation.
 route('RF_ANT',[pos('J_ANT',1),(125,27.8)],.8)
 route('RF_ANT',[(125,27.8),(125,28.5),pos('D_RF',1),(124.65,30.6)],.3)
 route('RF_ANT',[(124.65,30.6),(125,31.6)],.8)
 route('RF_ANT',[(125,31.6),pos('C_RF',1)],.3)
 route('RF_ANT',[(125,28.5),pos('L_RF',1)],.3)
 route('RF_GNSS',[pos('C_RF',2),(125,34.2)],.3)
 route('RF_GNSS',[(125,34.2),(125,35),(117,35),(117,pos('U_GPS',11)[1]),pos('U_GPS',11)],.8)
 # Preserve an unbroken B-side RF reference plane; forbid signal tracks/vias
 # below the feed. Copper pours remain allowed. Ground stitches sit outside it.
 rf=k.ZONE(b);rf.SetLayer(k.B_Cu);rf.SetIsRuleArea(True)
 rf.SetDoNotAllowTracks(True);rf.SetDoNotAllowVias(True);rf.SetDoNotAllowPads(False)
 rf.SetDoNotAllowCopperPour(False);rf.SetDoNotAllowFootprints(False)
 poly=rf.Outline();poly.NewOutline()
 for x,y in [(115.5,33.6),(126.4,33.6),(126.4,36.4),(118.4,36.4),(118.4,pos('U_GPS',11)[1]+1.3),(115.5,pos('U_GPS',11)[1]+1.3)]:poly.Append(P(x,y))
 b.Add(rf)
 for x,y in [(115,36),(115,42),(115,48),(120,33),(124,33)]:
  v=k.PCB_VIA(b);v.SetPosition(P(x,y));v.SetWidth(k.FromMM(.6));v.SetDrill(k.FromMM(.3));v.SetViaType(k.VIATYPE_THROUGH);v.SetLayerPair(k.F_Cu,k.B_Cu);v.SetNet(b.FindNet('GND'));v.SetLocked(True);b.Add(v)
 k.SaveBoard(str(D/(NAME+'.kicad_pcb')),b)
 k.ExportSpecctraDSN(b,str(D/(NAME+'.dsn')))
 path=D/(NAME+'.dsn');s=path.read_text()
 # SWIG LoadBoard doesn't load project netclasses; explicitly set routing rules.
 s=s.replace('(width 200)', '(width 200)').replace('(clearance 200)', '(clearance 150)')
 s=s.replace('(clearance 50 (type smd_smd))','(clearance 150 (type smd_smd))')
 # Explicit bus widths: do not send WiFi/regulator power down default signal tracks.
 start=s.index('(class kicad_default ')
 rule_start=s.index('(circuit',start)
 power_nets=['+3V3_NET','+3V3_AUX','VIN_PROTECTED','TEENSY_VIN','VIN_DIODE']
 header=s[start:rule_start]
 for n in power_nets:
  header=re.sub(r'(?<!\S)'+re.escape(n)+r'(?!\S)','',header)
 s=s[:start]+header+s[rule_start:]
 via=re.search(r'\(use_via ([^)]+)\)',s).group(1)
 # Add class INSIDE the network section, immediately before its closing parenthesis.
 class_start=s.index('(class kicad_default ');depth=0;end=None
 for i in range(class_start,len(s)):
  depth += (s[i]=='(') - (s[i]==')')
  if depth==0:
   end=i+1;break
 assert end
 new_class='\n    (class power_bus '+' '.join(power_nets)+' (circuit (use_via '+via+')) (rule (width 800) (clearance 150)))'
 s=s[:end]+new_class+s[end:]
 path.write_text(s)
 # SaveBoard's standalone project overwrites netclass defaults; restore explicit rules.
 project_path=D/(NAME+'.kicad_pro');project=json.loads(project_path.read_text())
 for cl in project['net_settings']['classes']:
  if cl['name']=='Default':cl.update(clearance=.15,track_width=.2,via_diameter=.7,via_drill=.3)
 project_path.write_text(json.dumps(project,indent=2)+'\n')
 print('Prepared locked power/RF + DSN. Run DRC before autorouting.')
else:
 if not k.ImportSpecctraSES(b,str(D/(NAME+'.ses'))):raise SystemExit('Router session import failed')
 # Keep RF footprint rule area, replace board-level ground pours only.
 for z in list(b.Zones()):
  if not z.GetIsRuleArea():b.Remove(z)
 for layer in [k.F_Cu,k.B_Cu]:
  z=k.ZONE(b);z.SetLayer(layer);z.SetNet(b.FindNet('GND'));z.SetLocalClearance(k.FromMM(.15));z.SetMinThickness(k.FromMM(.2))
  z.SetPadConnection(k.ZONE_CONNECTION_FULL);z.SetThermalReliefGap(k.FromMM(.3));z.SetThermalReliefSpokeWidth(k.FromMM(.5));z.SetIslandRemovalMode(k.ISLAND_REMOVAL_MODE_ALWAYS)
  poly=z.Outline();poly.NewOutline()
  for x,y in [(20.5,20.5),(149.5,20.5),(149.5,159.5),(92.5,159.5),(92.5,153.5),(43.5,153.5),(43.5,159.5),(20.5,159.5)]:poly.Append(P(x,y))
  b.Add(z)
 b.BuildConnectivity();k.ZONE_FILLER(b).Fill(b.Zones())
 k.SaveBoard(str(D/(NAME+'.kicad_pcb')),b)
 project_path=D/(NAME+'.kicad_pro');project=json.loads(project_path.read_text())
 for cl in project['net_settings']['classes']:
  if cl['name']=='Default':cl.update(clearance=.15,track_width=.2,via_diameter=.7,via_drill=.3)
 project_path.write_text(json.dumps(project,indent=2)+'\n')
 print('Imported routes and filled ground. ERC/DRC/netlist/current-path checks REQUIRED before export.')

#!/usr/bin/env python3
"""Re-read exported manufacturing files with independent Gerbonara parser.
Run in a Python environment with gerbonara and matching system pcbnew bindings.
Not a substitute for fabricator DFM/impedance or independent electrical approval.
"""
from pathlib import Path
from collections import Counter
import json,zipfile,tempfile,hashlib,sys,warnings
import gerbonara
# This host's KiCad bindings use the same Python ABI as the CAM virtualenv.
sys.path.append('/usr/lib/python3/dist-packages')
import pcbnew as k
ROOT=Path(__file__).resolve().parents[1];D=ROOT/'design';NAME='racecar-integrated-revc'
package=ROOT/'Racecar-RevC-GERBERS-ENGINEERING-PROTOTYPE.zip'
b=k.LoadBoard(str(D/(NAME+'.kicad_pcb')))
expected={'PTH':Counter(),'NPTH':Counter()}
def key(x,y,d):return round(x,6),round(y,6),round(d,6)
for f in b.GetFootprints():
 for p in f.Pads():
  if p.GetAttribute() not in (k.PAD_ATTRIB_PTH,k.PAD_ATTRIB_NPTH):continue
  drill=p.GetDrillSize();assert drill.x==drill.y,'Slot requires explicit CAM comparison'
  point=p.GetPosition();typ='PTH' if p.GetAttribute()==k.PAD_ATTRIB_PTH else 'NPTH'
  expected[typ][key(k.ToMM(point.x),-k.ToMM(point.y),k.ToMM(drill.x))]+=1
for t in b.GetTracks():
 if isinstance(t,k.PCB_VIA):
  p=t.GetPosition();expected['PTH'][key(k.ToMM(p.x),-k.ToMM(p.y),k.ToMM(t.GetDrillValue()))]+=1
with zipfile.ZipFile(package) as z, tempfile.TemporaryDirectory(prefix='revc-cam-readback-') as temporary:
 assert z.testzip() is None
 manifest=json.loads(z.read('PACKAGE-MANIFEST.json'))
 for name,info in manifest['files'].items():assert hashlib.sha256(z.read(name)).hexdigest()==info['sha256'],name
 actual_pcb=z.read('editable-CAD/'+NAME+'.kicad_pcb')
 assert actual_pcb==(D/(NAME+'.kicad_pcb')).read_bytes(),'Package is not current CAD'
 stage=Path(temporary)
 for name in z.namelist():
  if name.startswith('gerbers/') and name.endswith(('.gbr','.drl')):(stage/Path(name).name).write_bytes(z.read(name))
 with warnings.catch_warnings(record=True) as notices:
  warnings.simplefilter('always')
  stack=gerbonara.LayerStack.open(stage)
  assert len(stack.graphic_layers)==9
  bounds=stack.bounding_box();assert all(abs(a-bb)<.01 for pair,want in zip(bounds,((19.975,-160.025),(150.025,-19.975))) for a,bb in zip(pair,want)),bounds
  counts={}
  for kind in ('PTH','NPTH'):
   path=stage/(NAME+'-'+kind+'.drl');text=path.read_text()
   # Gerbonara doesn't infer KiCad 9's comment-form X2 plating metadata reliably.
   # Check that header explicitly, then pass the declared plating to the parser.
   assert ('TF.FileFunction,Plated,' if kind=='PTH' else 'TF.FileFunction,NonPlated,') in text
   drill=gerbonara.ExcellonFile.open(path,plated=(kind=='PTH'))
   got=Counter(key(o.x,o.y,o.aperture.diameter) for o in drill.objects)
   # KiCad prints Excellon to 0.001 mm; preserve PCB precision and permit only
   # the unavoidable half-unit rounding, rather than comparing Python/C++ ties.
   remaining=expected[kind].copy()
   for hole,count in got.items():
    candidates=[h for h,n in remaining.items() if n and abs(h[0]-hole[0])<=.00051 and abs(h[1]-hole[1])<=.00051 and abs(h[2]-hole[2])<.000001]
    assert len(candidates)==1,(kind,'Unmatched/ambiguous drill',hole,candidates)
    remaining[candidates[0]]-=count
    assert remaining[candidates[0]]>=0,(kind,'Extra duplicate drill',hole)
   assert not any(remaining.values()),(kind,'Missing drills',+remaining)
   counts[kind]=sum(got.values())
  # Independent raster/vector inspection outputs, not source PCB screenshots.
  output=ROOT/'fabrication/cam-readback';output.mkdir(exist_ok=True)
  for side in ('top','bottom'):
   (output/(side+'-copper.svg')).write_text(str(stack.graphic_layers[(side,'copper')].to_svg(force_bounds=bounds)))
 report={'gerbers_parsed':9,'drills_matched_to_cad':counts,'bounds_mm':bounds,
         'package_sha256':hashlib.sha256(package.read_bytes()).hexdigest(),
         'parser_notices':sorted(set(str(w.message).split(':',1)[-1].strip() for w in notices)),
         'independent_electrical_review':False,'physical_validation':False}
 (ROOT/'fabrication/cam-readback-report.json').write_text(json.dumps(report,indent=2)+'\n')
 print(json.dumps({k:v for k,v in report.items() if k!='parser_notices'},indent=2))

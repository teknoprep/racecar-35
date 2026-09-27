#!/usr/bin/env bash
# Export the existing routed prototype, never silently re-route or regenerate it.
set -euo pipefail
cd "$(dirname "$(realpath "$0")")"
name=racecar-carrier-reva
pcb="design/$name.kicad_pcb"
sch="design/$name.kicad_sch"
rm -f "$name-GERBERS-PROTOTYPE.zip"
mkdir -p reports
kicad-cli pcb drc --format json --all-track-errors --exit-code-violations \
  -o reports/drc.json "$pcb"
kicad-cli sch erc --format json --exit-code-violations -o reports/erc.json "$sch"
/usr/bin/python3 verify.py
# Old outputs cannot survive a failed export and look like a new complete package.
rm -rf gerbers
mkdir -p gerbers
kicad-cli sch export pdf -o schematic.pdf "$sch"
kicad-cli pcb export gerbers --layers F.Cu,B.Cu,F.Mask,B.Mask,F.Silkscreen,B.Silkscreen,F.Paste,Edge.Cuts \
  -o gerbers/ "$pcb"
kicad-cli pcb export drill --format excellon --excellon-units mm \
  --excellon-separate-th --generate-map --map-format pdf --generate-report \
  --report-path reports/drill-report.txt -o gerbers/ "$pcb"
kicad-cli pcb export pos --format csv --units mm --side both -o assembly-positions.csv "$pcb"
kicad-cli pcb export pos --format csv --units mm --side both --smd-only -o assembly-positions-SMD.csv "$pcb"
kicad-cli pcb export svg --layers F.Cu,F.Silkscreen,Edge.Cuts --mode-single --fit-page-to-board \
  --exclude-drawing-sheet -o board-top.svg "$pcb"
kicad-cli pcb export svg --layers B.Cu,B.Silkscreen,Edge.Cuts --mirror --mode-single --fit-page-to-board \
  --exclude-drawing-sheet -o board-bottom.svg "$pcb"
kicad-cli pcb export svg --layers F.Fab,F.Silkscreen,Edge.Cuts --mode-single --fit-page-to-board \
  --exclude-drawing-sheet --black-and-white -o assembly-top.svg "$pcb"
/usr/bin/python3 - <<'PY'
from pathlib import Path
import cairosvg,zipfile,hashlib,json,csv,collections
groups=collections.defaultdict(list)
for p in json.loads(Path('components.json').read_text()):
    if p['ref'].startswith(('H','TP')): continue # PCB features / hardware in BOM-extra
    groups[(p['value'],p['maker'],p['mpn'],p['footprint'])].append(p['ref'])
with open('BOM-grouped.csv','w') as f:
    w=csv.writer(f);w.writerow(['References','Quantity','Value','Manufacturer','MPN','Footprint'])
    for (value,maker,mpn,fp),refs in groups.items():w.writerow([', '.join(refs),len(refs),value,maker,mpn,fp])
for s in ['board-top','board-bottom','assembly-top']:
    cairosvg.svg2png(url=s+'.svg',write_to=s+'.png',output_width=1800,background_color='white')
cairosvg.svg2pdf(url='assembly-top.svg',write_to='assembly-top.pdf')
fs=list(Path('gerbers').iterdir())
assert len(fs)>=10 and len(list(Path('gerbers').glob('*.drl')))==2, 'Incomplete manufacturing output'
with zipfile.ZipFile('racecar-carrier-reva-GERBERS-PROTOTYPE.zip','w',zipfile.ZIP_DEFLATED) as z:
    for f in sorted(fs): z.write(f,f.name)
    z.write('FABRICATION.md','READ-ME-FABRICATION.md')
# Include PCB/source nets so checks can be matched to these exact deliverables.
paths=sorted(set(list(Path('gerbers').iterdir())+list(Path('design').glob('*.kicad_*'))+[Path(p) for p in ['BOM.csv','BOM-grouped.csv','BOM-extra.csv','components.json','README.md','FABRICATION.md','BRINGUP.md','generate.py','schematic.py','finish_board.py','verify.py','export.sh','schematic.pdf','assembly-positions.csv','assembly-positions-SMD.csv','assembly-top.pdf','racecar-carrier-reva-GERBERS-PROTOTYPE.zip']]))
Path('reports/SHA256SUMS').write_text(''.join(hashlib.sha256(p.read_bytes()).hexdigest()+'  '+str(p)+'\n' for p in paths))
print('Export complete. PROTOTYPE ONLY; thermal, circuit and physical validation still required.')
PY

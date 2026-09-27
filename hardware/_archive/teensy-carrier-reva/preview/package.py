#!/usr/bin/env python3
"""Two user-facing files, without omitting necessary manufacturing layers."""
from pathlib import Path
import zipfile,shutil,hashlib
R=Path(__file__).resolve().parent.parent;S=R/'simple';S.mkdir(exist_ok=True)
name='racecar-carrier-reva'
layers=['F_Cu.gtl','B_Cu.gbl','F_Mask.gts','B_Mask.gbs','F_Silkscreen.gto','B_Silkscreen.gbo','F_Paste.gtp','Edge_Cuts.gm1','PTH.drl','NPTH.drl']
png=R/'ASSEMBLED-PREVIEW.png';assert png.is_file()
with zipfile.ZipFile(S/'PCB-FILES.zip','w',zipfile.ZIP_DEFLATED) as z:
    for suffix in layers:
        p=R/'gerbers'/(name+'-'+suffix);assert p.is_file(),p
        z.write(p,p.name)
    for source,dest in [('BOM-grouped.csv','PARTS.csv'),('BOM-extra.csv','SOCKETS-AND-HARDWARE.csv'),('FABRICATION.md','READ-FIRST-FABRICATION.md'),('BRINGUP.md','POWER-UP-CHECKLIST.md')]:
        z.write(R/source,dest)
with zipfile.ZipFile(S/'PCB-FILES.zip') as z:
    assert z.testzip() is None
    # Verify copper/drill contents are byte-for-byte original validated exports.
    for suffix in layers:
        n=name+'-'+suffix
        assert z.read(n)==(R/'gerbers'/n).read_bytes()
shutil.copy2(png,S/png.name)
target=Path('/home/chris/Downloads/Racecar-Board-Simple');target.mkdir(exist_ok=True)
for p in S.iterdir():
    if p.is_file():shutil.copy2(p,target/p.name)
print('Simple handoff:',target)
for p in S.iterdir():print(p.name,p.stat().st_size,'bytes',hashlib.sha256(p.read_bytes()).hexdigest())

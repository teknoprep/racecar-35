#!/usr/bin/env python3
"""Assembled 3D presentation from the real PCB; simplified module illustrations.
Never edits the manufacturing PCB or Gerbers. Requires KiCad 9 and Pillow.
Stock VRML models reside under models/; custom geometry is generated here.
"""
from pathlib import Path
import re, math, subprocess, json
import pcbnew as k
from PIL import Image, ImageDraw, ImageFont, ImageFilter
HERE=Path(__file__).resolve().parent;ROOT=HERE.parent;MODELS=HERE/'models'
GREEN=(.025,.27,.12);BLACK=(.035,.04,.045);METAL=(.65,.68,.70);GOLD=(.76,.58,.19);CERAMIC=(.57,.40,.24)
shapes=[]
def mesh(points,faces,color):
    pts=', '.join(' '.join(f'{v/2.54:.6f}' for v in p) for p in points)
    ix=', '.join(', '.join(map(str,f))+', -1' for f in faces)
    shapes.append('Shape { appearance Appearance { material Material { diffuseColor '+ ' '.join(map(str,color))+' specularColor 0.25 0.25 0.25 shininess 0.3 } } geometry IndexedFaceSet { solid TRUE coord Coordinate { point [ '+pts+' ] } coordIndex [ '+ix+' ] } }')
def box(x,y,z,w,d,h,c):
    vs=[(x-w/2,y-d/2,z-h/2),(x+w/2,y-d/2,z-h/2),(x+w/2,y+d/2,z-h/2),(x-w/2,y+d/2,z-h/2),(x-w/2,y-d/2,z+h/2),(x+w/2,y-d/2,z+h/2),(x+w/2,y+d/2,z+h/2),(x-w/2,y+d/2,z+h/2)]
    mesh(vs,[(0,3,2,1),(4,5,6,7),(0,1,5,4),(1,2,6,5),(2,3,7,6),(3,0,4,7)],c)
def cyl(x,y,z,r,h,c,N=32):
    vs=[(x+r*math.cos(i*2*math.pi/N),y+r*math.sin(i*2*math.pi/N),z+dz) for dz in (-h/2,h/2) for i in range(N)]
    fs=[tuple(reversed(range(N))),tuple(range(N,2*N))]+[(i,(i+1)%N,(i+1)%N+N,i+N) for i in range(N)]
    mesh(vs,fs,c)
def save(name):
    p=MODELS/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text('#VRML V2.0 utf8\n# Simplified VISUAL module model, NOT a mechanical or component-layout reference.\n'+'\n'.join(shapes)+'\n');shapes.clear();return p
# Teensy above two socket strips. Coordinates follow custom footprint top view.
for x in (0,15.24):
    box(x,-29.21,4.4,2.54,60.96,8.8,BLACK)
    for i in range(24): box(x,-i*2.54,4.9,.64,.64,12,GOLD)
box(7.62,-29.21,10.2,17.78,60.96,1.6,GREEN)
for x in (0,15.24):
    for i in range(24):
        cyl(x,-i*2.54,11.06,.9,.10,GOLD)
        cyl(x,-i*2.54,11.13,.36,.05,BLACK)
# Main CPU and representative support parts, not exact vendor placement.
box(7.62,-17,11.65,9.7,9.7,1.2,BLACK)
box(7.62,-17,12.27,7.8,7.8,.06,(.09,.10,.11))
for x,y,w,d in [(5,-29,4.8,5.8),(11.8,-32,3,3),(11.4,-6,3,3),(4,-7,2.5,3.5),(7.6,-38,4.5,3.5)]:
    box(x,y,11.45,w,d,.8,BLACK)
    for s in (-1,1):
        for j in range(4):box(x+s*(w/2+.35),y+(j-1.5)*.8,11.3,.7,.4,.3,METAL)
for x in (2.4,12.9):
    for i in range(10):
        y=-7-i*3.4
        box(x,y,11.3,.9,1.4,.5,CERAMIC);box(x,y-.65,11.3,.9,.25,.5,METAL);box(x,y+.65,11.3,.9,.25,.5,METAL)
# USB metal shell at the USB end, with a dark opening and plastic tongue.
box(7.62,.05,12.45,7.6,5.6,2.8,METAL)
box(7.62,2.88,12.45,6.5,.12,1.8,BLACK)
box(7.62,2.96,12.15,4.2,.12,.5,(.19,.20,.21))
# Pushbutton and micro-SD socket.
box(5,-39,11.6,4.6,4.6,1.0,METAL);cyl(5,-39,12.35,1.25,.8,(.42,.20,.13))
box(7.62,-51.2,12.1,13.5,14.8,2.1,METAL)
box(7.62,-58.65,12.15,11.8,.12,1.15,BLACK)
box(7.62,-51.2,13.18,9,7,.07,(.56,.59,.60))
save('Teensy41-illustration.wrl')
# Pololu D36V50F5: exact header grid and board envelope, illustrative components.
box(1.27,-6.35,3.2,5.08,15.24,2.54,BLACK)
for r in range(6):
    for x in (0,2.54):box(x,-r*2.54,3.2,.64,.64,9,GOLD)
box(11.43,-6.35,5.8,25.4,25.4,1.6,GREEN)
for r in range(6):
    for x in (0,2.54):
        cyl(x,-r*2.54,6.65,.92,.1,GOLD);cyl(x,-r*2.54,6.73,.34,.04,BLACK)
box(14,0,9.15,10.5,10.5,5.1,(.20,.22,.23))
box(14,0,11.74,8.8,8.8,.08,(.27,.29,.30))
for x,y,r,h in [(18,-9.7,3.7,7.7),(10.3,-14.5,2.9,6.2)]:
    cyl(x,y,6.7+h/2,r,h,(.10,.12,.13));cyl(x,y,6.7+h+.06,r-.15,.12,METAL)
    box(x,y,6.7+h+.13,r*1.45,.12,.02,(.30,.32,.34))
box(7.5,-6.5,7.05,4.8,6.2,.9,BLACK)
for i in range(4):box(22,-4-i*3,6.9,1.1,1.8,.6,CERAMIC)
save('Pololu4091-illustration.wrl')
# Fuse case matches the source footprint; supplier model wasn't available.
box(0,0,1.55,6.1,2.69,2.69,(.93,.92,.87))
for x in (-2.67,2.67):box(x,0,1.55,.76,2.72,2.72,METAL)
save('Fuse.3dshapes/Fuse_Littelfuse-NANO2-451_453.wrl')
# Rendering COPY only. Existing manufacturing files are never saved through pcbnew.
b=k.LoadBoard(str(ROOT/'design/racecar-carrier-reva.kicad_pcb'))
for fp in b.GetFootprints():
    ref=fp.GetReference()
    if ref in ('U1','U2'):
        fp.Models().clear();m=k.FP_3DMODEL();m.m_Filename=str(MODELS/('Teensy41-illustration.wrl' if ref=='U1' else 'Pololu4091-illustration.wrl'));fp.Add3DModel(m)
    else:
        for model in fp.Models():
            fn=model.m_Filename.replace('${KICAD9_3DMODEL_DIR}/','').replace('.step','.wrl')
            target=MODELS/fn
            if target.exists():model.m_Filename=str(target)
k.SaveBoard(str(HERE/'render-only.kicad_pcb'),b)
s=(HERE/'render-only.kicad_pcb').read_text()
# SWIG returns copies of FP_3DMODEL items on some KiCad builds. Relink the saved
# render-copy paths too, otherwise the standard components silently disappear.
for old in set(re.findall(r'\$\{KICAD9_3DMODEL_DIR\}/[^\"]+',s)):
    target=MODELS/old.split('}/',1)[1].replace('.step','.wrl')
    if target.exists():s=s.replace(old,str(target))
s=s.replace('(type "Top Solder Mask")','(type "Top Solder Mask") (color "#0b703ddd")').replace('(type "Bottom Solder Mask")','(type "Bottom Solder Mask") (color "#0b703ddd")')
(HERE/'render-only.kicad_pcb').write_text(s)
# Positive 330deg avoids CLI parsing a negative comma-separated angle as an option.
subprocess.run(['kicad-cli','pcb','render','--width','2500','--height','2000','--rotate','330,0,20','--zoom','0.83','--quality','high','--background','transparent','-o',str(HERE/'assembled-raw.png'),str(HERE/'render-only.kicad_pcb')],check=True)
subprocess.run(['kicad-cli','pcb','render','--width','1700','--height','1500','--side','top','--zoom','0.82','--quality','high','--background','transparent','-o',str(HERE/'top-raw.png'),str(HERE/'render-only.kicad_pcb')],check=True)
# One clean PNG, rather than a page full of technical plots.
W,H=2400,2050
im=Image.new('RGB',(W,H),(242,245,247));d=ImageDraw.Draw(im)
font='/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf';bold='/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
f=lambda n,b=False:ImageFont.truetype(bold if b else font,n)
d.rectangle((0,0,W,220),fill=(15,30,43))
d.text((82,44),'RACECAR-35  /  TEENSY CARRIER',font=f(55,True),fill='white')
d.text((85,122),'REV A   •   ASSEMBLED 3D PREVIEW',font=f(28),fill=(130,214,185))
d.text((85,170),'130 × 110 mm   |   10–20 V input   |   5 V / 5 A design budget',font=f(25),fill=(214,223,231))
raw=Image.open(HERE/'assembled-raw.png').convert('RGBA');boxbounds=raw.getbbox();raw=raw.crop(boxbounds)
raw.thumbnail((2200,1490),Image.Resampling.LANCZOS)
x=(W-raw.width)//2;y=260+(1510-raw.height)//2
shadow=Image.new('RGBA',im.size);shadow.alpha_composite(Image.new('RGBA',raw.size,(0,0,0,0)),(x,y))
mask=raw.getchannel('A').filter(ImageFilter.GaussianBlur(12))
sh=Image.new('RGBA',raw.size,(19,29,37,55));sh.putalpha(mask.point(lambda a:int(a*.12)))
im.paste(sh,(x+8,y+15),sh);im.paste(raw,(x,y),raw)
d=ImageDraw.Draw(im)
d.rounded_rectangle((70,1770,2330,1870),18,fill='white',outline=(213,222,228),width=2)
d.text((102,1791),'Socketed Teensy 4.1   •   Pololu 5 V regulator   •   Four-wire screen terminal',font=f(28,True),fill=(20,53,65))
d.text((103,1833),'GPS / IMU / CAN / W5500 connectors   |   External optocoupler input → Teensy pin 9',font=f(23),fill=(64,86,98))
d.text((83,1912),'PROTOTYPE — NOT BUILT OR VALIDATED',font=f(25,True),fill=(143,64,34))
d.text((83,1954),'Actual carrier PCB and connector positions. Teensy/Pololu internals are simplified visual models.',font=f(21),fill=(84,101,111))
d.text((83,1988),'Green solder mask illustrated. Render is not a photograph or proof of electrical/thermal performance.',font=f(21),fill=(84,101,111))
im.save(ROOT/'ASSEMBLED-PREVIEW.png')
print('Saved',ROOT/'ASSEMBLED-PREVIEW.png')

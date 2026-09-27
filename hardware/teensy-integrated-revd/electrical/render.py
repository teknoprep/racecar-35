#!/usr/bin/env python3
"""Render the ACTUAL electrical board, using a separate presentation-only copy.
No electrical source changes. Module bodies are illustrative, not fit certificates.
"""
from pathlib import Path
import hashlib,json,re,subprocess
import pcbnew as k
from PIL import Image,ImageDraw,ImageFont
ROOT=Path(__file__).resolve().parents[1];HERE=Path(__file__).resolve().parent
OUT=HERE/'render';OUT.mkdir(exist_ok=True)
PCB=ROOT/'design/racecar-integrated-revd.kicad_pcb';before=hashlib.sha256(PCB.read_bytes()).hexdigest()
alias=json.loads((ROOT/'design/COMPONENT-ALIASES.json').read_text())
old=ROOT.parent/'_archive/teensy-carrier-reva/preview/models';concept=ROOT/'preview/models'
b=k.LoadBoard(str(PCB));fps={f.GetReference():f for f in b.GetFootprints()}
custom={'U1':old/'Teensy41-illustration.wrl','U2':old/'Pololu4091-illustration.wrl',
        'U_GPS':concept/'NEO-M9N-illustration.wrl','J_ANT':concept/'SMA-female-illustration.wrl',
        'U_NET':concept/'ESP32-S3-WROOM-1-illustration.wrl'}
for name,path in custom.items():
 assert path.exists(),path
 f=fps[alias[name]];f.Models().clear();m=k.FP_3DMODEL();m.m_Filename=str(path.resolve());f.Add3DModel(m)
m=k.FP_3DMODEL();m.m_Filename=str((concept/'CR2032-illustration.wrl').resolve());fps['BT1'].Add3DModel(m)
copy=OUT/'RENDER-COPY-NOT-CAD.kicad_pcb';k.SaveBoard(str(copy),b)
s=copy.read_text()
for original in set(re.findall(r'\$\{KICAD\d+_3DMODEL_DIR\}/[^"\n]+',s)):
 rel=original.split('}/')[1].replace('.step','.wrl')
 target=next((d/rel for d in [HERE/'models',concept,old] if (d/rel).exists()),None)
 assert target,'Missing visible model: '+rel
 s=s.replace(original,str(target.resolve()))
s=s.replace('(color "Green")','(color "#0b703ddd")');copy.write_text(s)
for name,args in [('angle',['--rotate','330,0,17']),('top',['--side','top'])]:
 with (OUT/(name+'.log')).open('w') as log:
  subprocess.run(['kicad-cli','pcb','render','--width','2400','--height','1800','--zoom','.82','--quality','high','--background','transparent',*args,'-o',str(OUT/(name+'.png')),str(copy)],stdout=log,stderr=subprocess.STDOUT,check=True)
W,H=2560,1800;im=Image.new('RGB',(W,H),'#eef3f5');d=ImageDraw.Draw(im)
font=lambda n,b=False:ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSans'+('-Bold' if b else '')+'.ttf',n)
d.rectangle((0,0,W,185),fill='#102431')
d.text((65,28),'RACECAR-35  /  REV D',font=font(52,True),fill='white')
d.text((68,103),'ASSEMBLED VIEW OF THE ROUTED ELECTRICAL PROTOTYPE',font=font(26,True),fill='#9ae1c4')
d.text((68,145),'130 × 140 mm  •  LEFT-edge terminals  •  Internal WiFi antenna  •  External GPS SMA',font=font(22),fill='#d6e7ec')
raw=Image.open(OUT/'angle.png').convert('RGBA');raw=raw.crop(raw.getbbox());raw.thumbnail((1750,1335))
im.paste(raw,(25+(1750-raw.width)//2,200+(1335-raw.height)//2),raw)
top=Image.open(OUT/'top.png').convert('RGBA');top=top.crop(top.getbbox());top.thumbnail((645,750))
im.paste(top,(1830+(645-top.width)//2,225),top)
d=ImageDraw.Draw(im)
lines=[('J3  TACH','1 signal / 2 return — conditioned only'),('J4  OIL','1 +5V / 2 return / 3 signal'),('J5  COOLANT','1 dedicated NTC / 2 return'),('J6  AEM AFR','1 WHITE+ / 2 BROWN return'),('J1  POWER','1 +10–20V in / 2 GND'),('J2  SCREEN','1 GND / 2 +5V / 3 RX / 4 TX')]
for i,(title,sub) in enumerate(lines):
 y=1030+i*70;d.text((1840,y),title,font=font(22,True),fill='#153a45');d.text((1840,y+29),sub,font=font(17),fill='#49636c')
d.rounded_rectangle((60,1510,2500,1750),18,fill='#fff4dc',outline='#ddb667',width=2)
d.text((90,1530),'ENGINEERING PROTOTYPE — NOT PHYSICALLY VALIDATED',font=font(29,True),fill='#794514')
for i,t in enumerate(['136 PCB parts • routed copper • 392 connected pads checked against schematic • zero ERC/DRC/unconnected items',
 'New WiFi software and oil/coolant conversion changes are still required. Do not use legacy analog readings as calibrated values.',
 'Independent electrical/footprint review and bench testing required before ordering/vehicle use. 3D bodies are illustrative.',
 'Screen power: verified Advance J10 5V_IN ONLY, never 3V3_OUT. CR2032 retains RTC only; do not charge.']):
 d.text((90,1583+i*36),t,font=font(20),fill='#65492e')
path=ROOT/'REV-C-ASSEMBLED-ENGINEERING-PROTOTYPE.png';im.save(path)
assert hashlib.sha256(PCB.read_bytes()).hexdigest()==before,'Renderer modified source PCB'
(HERE/'render-report.json').write_text(json.dumps({'pcb_sha256':before,'png_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'models':'Illustrative KiCad/library and original module bodies; not mechanical approval'},indent=2)+'\n')
print(path)

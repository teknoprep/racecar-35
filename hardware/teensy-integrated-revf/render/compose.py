#!/usr/bin/env python3
"""Presentation-only 3D view of the actual Rev F board.

Works on a COPY: the installed KiCad 3D library is absent, so model paths are
rewritten to the .wrl bodies archived with the project. Never modifies electrical
source. Module bodies are illustrative, not mechanical fit certificates.
"""
from pathlib import Path
import hashlib, re, subprocess
import pcbnew as k
from PIL import Image, ImageDraw, ImageFont

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
PCB = ROOT / 'design/racecar-integrated-revf.kicad_pcb'
before = hashlib.sha256(PCB.read_bytes()).hexdigest()

OLD = ROOT.parent / '_archive/teensy-carrier-reva/preview/models'
CONCEPT = ROOT.parent / 'teensy-integrated-revc/preview/models'
MODEL_DIRS = [HERE / 'models', CONCEPT, OLD]

# ---------------------------------------------------------------- 3D bodies --
b = k.LoadBoard(str(PCB))
fps = {f.GetReference(): f for f in b.GetFootprints()}

CUSTOM = {
    'U1': OLD / 'Teensy41-illustration.wrl',
    'U2': OLD / 'Pololu4091-illustration.wrl',
    'U8': CONCEPT / 'NEO-M9N-illustration.wrl',       # NEO-M9N
    'J8': CONCEPT / 'SMA-female-illustration.wrl',    # GPS SMA
    'U17': CONCEPT / 'ESP32-S3-WROOM-1-illustration.wrl',
}
for ref, path in CUSTOM.items():
    if ref in fps and path.exists():
        f = fps[ref]; f.Models().clear()
        m = k.FP_3DMODEL(); m.m_Filename = str(path.resolve()); f.Add3DModel(m)
if 'BT1' in fps:
    p = CONCEPT / 'CR2032-illustration.wrl'
    if p.exists():
        m = k.FP_3DMODEL(); m.m_Filename = str(p.resolve()); fps['BT1'].Add3DModel(m)

copy = HERE / 'RENDER-COPY-NOT-CAD.kicad_pcb'
k.SaveBoard(str(copy), b)
s = copy.read_text()

resolved, dropped = 0, 0
for original in set(re.findall(r'\$\{KICAD\d+_3DMODEL_DIR\}/[^"\n]+', s)):
    rel = original.split('}/')[1].replace('.step', '.wrl')
    target = next((d / rel for d in MODEL_DIRS if (d / rel).exists()), None)
    if target:
        s = s.replace(original, str(target.resolve())); resolved += 1
    else:
        # no body available: drop the link so the render still completes
        s = s.replace(original, ''); dropped += 1
copy.write_text(s)
print(f'3D bodies: {resolved} resolved, {dropped} unavailable')

# ------------------------------------------------------------------ render --
for name, args in [('top', ['--side', 'top']), ('angle', ['--rotate', '330,0,17'])]:
    subprocess.run(['kicad-cli', 'pcb', 'render', '--width', '2200', '--height', '1600',
                    '--zoom', '0.85', '--quality', 'high', '--background', 'transparent',
                    *args, '-o', str(HERE / f'{name}.png'), str(copy)],
                   check=True, capture_output=True)

# ---------------------------------------------------------------- compose ---
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans'
font = lambda n, bo=False: ImageFont.truetype(FONT + ('-Bold' if bo else '') + '.ttf', n)

W, H = 2600, 1860
im = Image.new('RGB', (W, H), '#eef3f5')
d = ImageDraw.Draw(im)

d.rectangle((0, 0, W, 186), fill='#0f2230')
d.text((56, 24), 'RACECAR-35  /  REV F', font=font(54, True), fill='white')
d.text((59, 100), 'FOUR-LAYER INTEGRATED TRUNK LOGGER — 3D VIEW OF THE ROUTED BOARD',
       font=font(24, True), fill='#8fe0c0')
d.text((59, 142), '150 × 155 mm  •  158 parts  •  89 nets  •  1,474 routed items  •  In1.Cu = continuous ground plane',
       font=font(20), fill='#cfe3ea')

# angled view, left
ang = Image.open(HERE / 'angle.png').convert('RGBA'); ang = ang.crop(ang.getbbox())
ang.thumbnail((1620, 930))
im.paste(ang, (30 + (1620 - ang.width) // 2, 200 + (930 - ang.height) // 2), ang)

# top view, upper right
top = Image.open(HERE / 'top.png').convert('RGBA'); top = top.crop(top.getbbox())
top.thumbnail((800, 660))
im.paste(top, (1730 + (800 - top.width) // 2, 205), top)

# legend, lower right (starts clear of the top view)
LX, RX = 1730, 1922
y = 900
def head(t):
    global y
    d.text((LX, y), t, font=font(20, True), fill='#0f2230'); y += 30
def row(ref, name, sub):
    global y
    d.text((LX, y), ref, font=font(18, True), fill='#12303c')
    d.text((LX + 52, y), name, font=font(18, True), fill='#12303c')
    d.text((RX, y + 1), sub, font=font(15), fill='#4a646e')
    y += 31

head('LEFT-EDGE SCREW TERMINALS')
for r in [('J3', 'TACH', 'conditioned ECU / cluster only'),
          ('J4', 'OIL', '0.5–4.5 V, protected 0.500'),
          ('J5', 'NTC', 'coolant, 4.096 V / 2.49 k'),
          ('J6', 'AEM AFR', '30-0300 gauge output'),
          ('J11', 'THROTTLE', 'NEW — A7 / pin 21'),
          ('J12', 'BRAKE', 'NEW — A10 / pin 24'),
          ('J1', 'POWER', '10–20 V in, 5 A class'),
          ('J2', 'SCREEN', 'J10 5 V + 921600 UART')]:
    row(*r)

y += 12
head('OTHER CONNECTORS')
for r in [('J13', 'PI 5 VIDEO', 'NEW — 3.3 V UART, pins 0/1'),
          ('J7', 'CAN', 'external SN65HVD230'),
          ('J8', 'GPS SMA', 'external active antenna'),
          ('J9', 'RTC VBAT', 'CR2032 → Teensy pads'),
          ('J10', 'NET SERVICE', 'BOOT0 / EN / UART')]:
    row(*r)

# status, bottom left
d.rounded_rectangle((30, 1160, 1700, 1820), 16, fill='white', outline='#c8d6dd', width=2)
d.text((62, 1186), 'WHERE THIS STANDS', font=font(26, True), fill='#0f2230')
yy = 1234
for t, c in [
    ('PASSING', '#1b6b45'), (' 0 ERC   •   0 DRC   •   0 unconnected   •   456 pads match the schematic', '#12303c'),
    ('', '#12303c'), ('  531 pin / geometry / calculation assertions', '#12303c'),
    ('', '#12303c'), ('BUILT', '#1b6b45'), (' 4 copper layers with a real In1.Cu ground plane', '#12303c'),
    (' ICM-42670-P, throttle, brake and the Raspberry Pi video UART all added', '#12303c'),
    ('', '#12303c'), ('PACKAGED', '#1b6b45'), (' Gerbers + separate PTH/NPTH drills + BOM + CPL, ready for PCBWay', '#12303c'),
    ('', '#12303c'), ('STILL OPEN', '#8a2b12'),
    (' ICM-42670-P land pattern is DERIVED — needs assembler sign-off', '#4a3a2a'),
    (' Confirm prepreg thickness to In1.Cu (0.20 mm assumed) with the fabricator', '#4a3a2a'),
    (' GNSS feed recalculated: 0.38 mm microstrip = 50 Ω at H = 0.20 mm', '#1b6b45'),
    (' Copper settled at 1 oz - screen branch fuse is now 3 A', '#1b6b45'),
    (' NO FIRMWARE YET for the ICM, throttle, brake or WiFi coprocessor', '#4a3a2a'),
    (' Not independently reviewed or physically validated', '#4a3a2a'),
]:
    if t in ('PASSING', 'BUILT', 'PACKAGED', 'STILL OPEN'):
        d.text((62, yy), t, font=font(18, True), fill=c)
    elif t:
        d.text((62 if not t.startswith(' ') else 200, yy), t, font=font(17), fill=c)
    yy += 30

out = ROOT / 'REV-E-3D-VIEW.png'
im.save(out)
assert hashlib.sha256(PCB.read_bytes()).hexdigest() == before, 'renderer modified source PCB'
print(out)

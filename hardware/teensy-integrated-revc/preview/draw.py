#!/usr/bin/env python3
"""Rev C VISUAL PLACEMENT / functional drawings, not a circuit or fabrication release.
Uses KiCad 9 / pcbnew + Pillow. Never modifies Rev A. No nets, traces or Gerbers.
Run with /usr/bin/python3. --compose-only reuses the existing rendered views.
"""
from pathlib import Path
import argparse
import hashlib
import json
import math
import re
import shutil
import subprocess
import xml.sax.saxutils as xml

import pcbnew as k
from PIL import Image, ImageDraw, ImageFont, ImageFilter

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REVA = ROOT.parent / '_archive/teensy-carrier-reva'
MODELS = HERE / 'models'
OLD_MODELS = REVA / 'preview/models'
PCB = HERE / 'PLACEMENT-ONLY-NOT-FOR-FAB.kicad_pcb'
MM = k.FromMM
P = lambda x, y: k.VECTOR2I(MM(x), MM(y))
FONT = '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf'
BOLD = '/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf'
OUT_PNG = ROOT / 'REV-C-INTEGRATED-PREVIEW.png'

# These assets are presentation geometry, not mechanical/footprint certification.
shapes = []
def mesh(points, faces, color):
    pts = ', '.join(' '.join(f'{v / 2.54:.6f}' for v in p) for p in points)
    ids = ', '.join(', '.join(map(str, face)) + ', -1' for face in faces)
    shapes.append('Shape { appearance Appearance { material Material { diffuseColor '
                  + ' '.join(map(str, color)) + ' specularColor 0.25 0.25 0.25 shininess 0.3 } } '
                  + 'geometry IndexedFaceSet { solid TRUE coord Coordinate { point [ '
                  + pts + ' ] } coordIndex [ ' + ids + ' ] } }')

def box(x, y, z, w, d, h, color):
    v = [(x + sx * w / 2, y + sy * d / 2, z + sz * h / 2)
         for sz in (-1, 1) for sy in (-1, 1) for sx in (-1, 1)]
    mesh(v, [(0, 2, 3, 1), (4, 5, 7, 6), (0, 1, 5, 4),
             (1, 3, 7, 5), (3, 2, 6, 7), (2, 0, 4, 6)], color)

def cyl_y(x, y, z, r, length, color, steps=48):
    points = [(x + r * math.cos(i * 2 * math.pi / steps), y + dy,
               z + r * math.sin(i * 2 * math.pi / steps))
              for dy in (-length / 2, length / 2) for i in range(steps)]
    # Reverse winding versus the usual Z-axis cylinder.
    faces = [tuple(range(steps)), tuple(reversed(range(steps, 2 * steps)))]
    faces += [(i + steps, (i + 1) % steps + steps, (i + 1) % steps, i)
              for i in range(steps)]
    mesh(points, faces, color)

def cyl_z(x, y, z, r, height, color, steps=64):
    points = [(x + r * math.cos(i * 2 * math.pi / steps),
               y + r * math.sin(i * 2 * math.pi / steps), z + dz)
              for dz in (-height / 2, height / 2) for i in range(steps)]
    faces = [tuple(reversed(range(steps))), tuple(range(steps, 2 * steps))]
    faces += [(i, (i + 1) % steps, (i + 1) % steps + steps, i + steps)
              for i in range(steps)]
    mesh(points, faces, color)


def save_model(name):
    p = MODELS / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text('#VRML V2.0 utf8\n# ILLUSTRATIVE ONLY; not a vendor mechanical model.\n'
                 + '\n'.join(shapes) + '\n')
    shapes.clear()
    return p

def custom_models():
    green, gold, metal = (.025, .27, .12), (.78, .59, .19), (.70, .73, .75)
    # NEO module approximate 12.2 x 16 mm envelope, directly soldered (no headers).
    box(0, 0, .45, 12.2, 16, .85, green)
    for x in (-6, 6):
        for i in range(12):
            box(x, 7 - 1.1 * i, .48, .75, .7, .9, gold)
    box(0, 0, 1.65, 11.2, 14.8, 1.55, metal)
    box(0, 0, 2.445, 10.5, 14.1, .035, (.78, .80, .81))
    save_model('NEO-M9N-illustration.wrl')
    # Standard SMA female appearance. Exact supplier selection remains open.
    box(0, 0, 3.3, 7, 7, 6.0, gold)
    cyl_y(0, 7, 3.4, 2.95, 8, gold)
    for y in (4, 5.1, 6.2, 7.3, 8.4, 9.5, 10.6):
        cyl_y(0, y, 3.4, 3.2, .3, (.84, .68, .31))
    cyl_y(0, 11.035, 3.4, 2.16, .06, (.92, .91, .84))
    cyl_y(0, 11.078, 3.4, .58, .035, (.06, .06, .045))
    for x in (-2.54, 2.54):
        for y in (-2.54, 2.54):
            box(x, y, -.4, .9, .9, 4, gold)
    box(0, 0, -.5, .6, .6, 4, gold)
    save_model('SMA-female-illustration.wrl')
    # Original approximate WiFi module/coin geometry, not vendor mechanical data.
    # WROOM-1 (NOT 1U): 18 x 25.5 mm, built-in PCB antenna at local +Y.
    # Copper-like shapes below are visual geometry ONLY, not an antenna design.
    box(0, 0, .4, 18, 25.5, .8, green)
    for x in (-8.8, 8.8):
        for i in range(14):
            box(x, 5.4 - i * 1.27, .45, .6, .6, .9, gold)
    box(0, -3.15, 1.7, 16.4, 17.5, 1.8, metal)
    box(0, -3.15, 2.62, 15.8, 16.9, .04, (.82, .84, .85))
    for x in (-6, -2, 2, 6):
        box(x, 9.5, .825, .45, 3.4, .04, gold)
    for x, y in [(-4, 11.0), (0, 8.0), (4, 11.0)]:
        box(x, y, .825, 4.4, .45, .04, gold)
    box(-6, 6.8, .825, .45, 2.2, .04, gold)
    save_model('ESP32-S3-WROOM-1-illustration.wrl')
    cyl_z(0, 0, 2.0, 10, 3.2, (.78, .80, .82))
    box(0, 0, 3.63, 5, .6, .04, (.25, .27, .28))
    box(0, 0, 3.63, .6, 5, .04, (.25, .27, .28))
    save_model('CR2032-illustration.wrl')


def build_placement():
    custom_models()
    source = k.LoadBoard(str(REVA / 'design/racecar-carrier-reva.kicad_pcb'))
    old = {f.GetReference(): f for f in source.GetFootprints()}
    b = k.BOARD()
    b.SetCopperLayerCount(2)  # rendering only; production stackup NOT decided here.
    refs = {}

    def line(a, z, layer=k.F_SilkS, width=.13):
        ob = k.PCB_SHAPE(b)
        ob.SetShape(k.SHAPE_T_SEGMENT)
        ob.SetStart(P(*a)); ob.SetEnd(P(*z))
        ob.SetLayer(layer); ob.SetWidth(MM(width)); b.Add(ob)

    def text(s, x, y, size=.95):
        ob = k.PCB_TEXT(b); ob.SetText(s); ob.SetPosition(P(x, y))
        ob.SetTextSize(P(size, size)); ob.SetTextThickness(MM(.14))
        ob.SetLayer(k.F_SilkS); b.Add(ob)

    def place(ref, fp, x, y, angle=0, value='ILLUSTRATIVE / TBD', model=None):
        assert ref not in refs, ref
        fp.SetReference(ref); fp.SetValue(value)
        # No electrical content is copied from Rev A into this rendering board.
        for pad in fp.Pads():
            pad.SetNetCode(0)
        for item in list(fp.GraphicalItems()):
            if item.GetLayer() == k.Edge_Cuts:
                item.SetLayer(k.F_Fab)  # keep stock edge hint off the board outline (SWIG-safe)
        fp.SetPosition(P(x, y)); fp.SetOrientationDegrees(angle)
        fp.Reference().SetVisible(False); fp.Value().SetVisible(False)
        if model:
            fp.Models().clear()
            m = k.FP_3DMODEL(); m.m_Filename = str(model); fp.Add3DModel(m)
        b.Add(fp); refs[ref] = fp
        return fp

    def clone(oldref, ref=None, x=None, y=None, angle=None, **kw):
        fp = old[oldref].Duplicate(); p = old[oldref].GetPosition()
        return place(ref or oldref, fp,
                     k.ToMM(p.x) if x is None else x,
                     k.ToMM(p.y) if y is None else y,
                     old[oldref].GetOrientationDegrees() if angle is None else angle,
                     **kw)

    def stock(ref, name, x, y, angle=0, **kw):
        lib, item = name.split(':')
        fp = k.FootprintLoad('/usr/share/kicad/footprints/' + lib + '.pretty', item)
        assert fp, name
        return place(ref, fp, x, y, angle, **kw)

    # Keep the serviceable Teensy, power supplies and powered-screen interface.
    clone('U1', x=60, model=OLD_MODELS / 'Teensy41-illustration.wrl', value='Socketed Teensy 4.1')
    clone('U2', model=OLD_MODELS / 'Pololu4091-illustration.wrl', value='Pololu 5V supply module')
    for r in ['U3', 'F1', 'F2', 'D1', 'C1', 'C2', 'U4', 'U5',
              'C3', 'C4', 'C6', 'C7', 'C8', 'C9', 'R1', 'R2', 'R3', 'R4',
              'D3', 'D4', 'D6', 'R14', 'TP1', 'TP2', 'TP3', 'TP4', 'H1', 'H2']:
        clone(r)
    clone('H3', y=156); clone('H4', y=156)
    # All six screw terminals face LEFT, in one serviceable edge bank.
    clone('J1', x=30, y=112, angle=270, value='POWER: 1 VIN+, 2 GND (proposed)')
    clone('J2', x=30, y=131, angle=270,
          value='SCREEN J10: 1 GND, 2 +5V OUT, 3 RX, 4 TX (proposed)')
    clone('JP1', x=84, y=28)
    clone('D2', x=94, y=28)
    clone('C5', x=81, y=42)

    # Replaces the old external GPS connector with soldered receiver and SMA.
    stock('U_GPS', 'RF_GPS:ublox_NEO', 125, 42,
          value='NEO-M9N soldered receiver (footprint review pending)',
          model=MODELS / 'NEO-M9N-illustration.wrl')
    stock('J_ANT', 'Connector_Coaxial:SMA_Amphenol_901-143_Horizontal', 125, 24.5,
          value='SMA antenna (illustrative selection)', model=MODELS / 'SMA-female-illustration.wrl')
    for i, (x, y, angle) in enumerate([(119, 30, 0), (125, 30, 0), (131, 30, 0),
                                       (114, 37, 90), (114, 43, 90), (136, 39, 90), (136, 45, 90)]):
        clone('C5' if i % 2 else 'R1', ref=f'RF_{i}', x=x, y=y, angle=angle)
    clone('U6', ref='U_RF', x=139, y=32)

    # Bare IMU chip, NOT a GY-521 daughterboard. Package/part is illustrative.
    stock('U_IMU', 'Package_DFN_QFN:QFN-24-1EP_4x4mm_P0.5mm_EP2.6x2.6mm', 96, 45,
          value='Onboard 6-axis IMU; final IC/package TBD')
    for i, (x, y) in enumerate([(91, 40), (100, 40), (91, 50), (101, 50)]):
        clone('C5' if i < 2 else 'R1', ref=f'IMU_{i}', x=x, y=y, angle=0)
    # Visual axis arrows; orientation is not a mounting prescription yet.
    line((105, 44), (105, 39)); line((105, 39), (104.3, 40.2)); line((105, 39), (105.7, 40.2))
    line((105, 44), (110, 44)); line((110, 44), (108.8, 43.3)); line((110, 44), (108.8, 44.7))

    # Three actual input terminals and representative supporting circuit groups.
    clone('J3', ref='J_TACH', x=30, y=30, angle=270,
          value='TACH: 1 SIGNAL, 2 RETURN; ECU/CLUSTER ONLY (proposed)')
    stock('J_OIL', 'TerminalBlock_Phoenix:TerminalBlock_Phoenix_MKDS-1,5-3-5.08_1x03_P5.08mm_Horizontal',
          30, 49, 270, value='OIL: 1 +5V, 2 RETURN, 3 SIGNAL (proposed)')
    clone('J3', ref='J_COOL', x=30, y=73, angle=270,
          value='COOLANT: 1 NTC, 2 RETURN (proposed)')
    stock('U_OPTO', 'Package_DIP:DIP-6_W7.62mm', 43, 34,
          value='Onboard optocoupler (illustrative package; part TBD)')
    clone('U6', ref='U_TACH_BUFFER', x=50, y=27, value='High-impedance input / driver')
    clone('U6', ref='U_OIL_FRONT', x=47, y=60, value='Oil input protection / filtering')
    clone('U6', ref='U_COOL_FRONT', x=47, y=82, value='Coolant input protection / filtering')
    for group, points in {
        'TACH': [(40, 27), (43, 45), (50, 45), (39, 38), (54, 35)],
        'OIL': [(42, 52), (49, 52), (41, 58), (41, 65), (48, 67), (54, 60)],
        'COOL': [(42, 74), (49, 74), (41, 80), (41, 87), (49, 89), (54, 81)],
    }.items():
        for i, (x, y) in enumerate(points):
            clone('C5' if i % 2 else 'R1', ref=f'{group}_{i}', x=x, y=y, angle=0)

    # Ethernet remains removed; screen WiFi stays, with NEW trunk WiFi below.
    clone('J6', x=112, y=64, value='Optional external CAN logic expansion')
    clone('C14', x=110, y=71)
    # AEM 30-0300 EXTERNAL GAUGE OUTPUT ONLY. No sensor/heater/controller/jumper.
    clone('J3', ref='J_AFR', x=30, y=94, angle=270,
          value='AEM gauge output: 1=WHITE SIG+, 2=BROWN RETURN (verify final footprint)')
    clone('U6', ref='U_AFR_PROTECT', x=47, y=94,
          value='AFR protection/power-off isolation (representative; part not selected)')
    clone('R1', ref='R_AFR_TOP', x=44, y=90, value='20.0k 0.1% (gain target; review required)')
    clone('R1', ref='R_AFR_BOTTOM', x=52, y=90, value='20.0k 0.1% (gain target; review required)')
    clone('R1', ref='R_AFR_SERIES', x=53, y=96, value='1k filter starting value')
    clone('C5', ref='C_AFR', x=48, y=99, value='100nF filter starting value')

    # New lower strip: independent TRUNK WiFi and Teensy RTC backup.
    # Antenna points towards the BOTTOM edge, over the carrier's 48 x 6 mm notch.
    # Stock footprint's RF rule area is retained; final manufacturer RF review required.
    stock('U_NET', 'RF_Module:ESP32-S3-WROOM-1', 64, 147.25, 180,
          value='ESP32-S3-WROOM-1-N8R2; INTERNAL PCB antenna; SPI firmware NOT implemented',
          model=MODELS / 'ESP32-S3-WROOM-1-illustration.wrl')
    clone('U6', ref='U_NET_PWR', x=132, y=76,
          value='Dedicated 3.3V 1A-class buck; part/circuit NOT selected')
    for i, (x, y) in enumerate([(129, 71), (137, 71), (143, 71), (140, 80), (52, 134), (76, 135)]):
        clone('C5' if i % 2 else 'R1', ref=f'NET_{i}', x=x, y=y,
              value='Network power/boot/filter support - representative only')
    clone('J7', ref='J_NET_SERVICE', x=86, y=143,
          value='NET programming/recovery header; final USB/BOOT/EN pinout pending')
    # Existing outer-row Teensy socket DOES NOT include VBAT: explicit auxiliary
    # contact required in final CAD. This marker is not an electrical connection.
    clone('JP1', ref='J_VBAT_AUX', x=94, y=133,
          value='VBAT auxiliary contact to Teensy REQUIRED; not a shunt jumper')
    bat = stock('BT1', 'Battery:BatteryHolder_Keystone_1058_1x2032', 117, 144,
                value='RTC 3V CR2032 to VBAT/GND; holder/retention review required')
    cell = k.FP_3DMODEL(); cell.m_Filename = str(MODELS / 'CR2032-illustration.wrl')
    bat.Add3DModel(cell)
    line((50, 129), (145, 129))
    text('WIFI INTERNAL', 64, 131, .85)
    text('RTC BACKUP', 117, 131, .85)
    text('3V3 NET', 134, 85, .8)
    text('NET SERVICE', 88, 154, .65)
    text('VBAT AUX', 94, 137, .65)
    text('RTC 3V CR2032  +', 118, 156, .75)

    for a, z in [((20, 20), (150, 20)), ((150, 20), (150, 160)),
                 ((150, 160), (88, 160)), ((88, 160), (88, 154)),
                 ((88, 154), (40, 154)), ((40, 154), (40, 160)),
                 ((40, 160), (20, 160)), ((20, 160), (20, 20))]:
        line(a, z, k.Edge_Cuts, .05)
    # Legible grouped legends, no fictitious wiring information or copper routes.
    text('RACECAR-35 / INTEGRATED', 84, 122.5, 1.4)
    text('REV C - PROPOSED PLACEMENT ONLY', 86, 126, .95)
    text('1  GPS ONBOARD', 126, 54, 1.0)
    text('SMA ANTENNA', 140, 23, .75)
    text('RF BIAS / FILTER', 137, 49, .65)
    text('2  IMU', 96, 35, 1.15)
    # Proposed physical pin order, top-to-bottom at this edge; NOT an electrical netlist.
    terminal_labels = {
        'J_TACH': ('TACH', ['1 SIG', '2 RET']),
        'J_OIL': ('OIL', ['1 +5V', '2 RET', '3 SIG']),
        'J_COOL': ('COOLANT', ['1 NTC', '2 RET']),
        'J_AFR': ('AEM AFR', ['1 SIG+', '2 RET']),
        'J1': ('10-20V IN', ['1 VIN+', '2 GND']),
        'J2': ('SCREEN', ['1 GND', '2 +5V', '3 RX', '4 TX']),
    }
    for ref, (title, labels) in terminal_labels.items():
        fp = refs[ref]; y = k.ToMM(fp.GetPosition().y)
        text(title, 40, y - 5.7, .95)
        for i, label in enumerate(labels):
            text(label, 37.6, y + i * 5.08, .65)
    text('J10 ONLY', 31, 151.5, .7)
    text('RX FROM / TX TO SCREEN', 43, 152.7, .5)
    text('ONBOARD OPTO', 47, 42, .7)
    text('NO COIL / IGNITION', 41, 48, .65)
    text('TEENSY 4.1', 69, 93, .85)
    text('CUT VUSB-VIN FOR CAR + USB POWER', 75, 23, .65)
    text('OPTIONAL CAN', 116, 70, .7)
    text('AFR SCALE / PROTECT', 47, 103.5, .55)
    text('AUX 3V3', 101, 67, .85)
    text('MAIN 5V / 5A TARGET', 81, 118, .85)
    text('4A SCREEN FUSE', 108, 92, .65)
    text('MCU PWR', 84, 35, .7)

    assert b.GetNetCount() <= 1, 'Rendering must contain no electrical netlist'
    assert len(list(b.GetTracks())) == 0
    assert all(z.GetIsRuleArea() for z in b.Zones()), 'No copper zones in a rendering'
    assert all(ref not in refs for ref in ['J3', 'J4', 'J5', 'J7', 'C15', 'J_WIFI_ANT'])
    terminals = [fp for fp in b.GetFootprints() if 'TerminalBlock_' in str(fp.GetFPID().GetLibItemName())]
    assert {fp.GetReference() for fp in terminals} == set(terminal_labels)
    for fp in terminals:
        assert fp.GetOrientationDegrees() % 360 == 270, 'Every wire entry faces LEFT'
        pads = sorted(fp.Pads(), key=lambda pad: int(pad.GetNumber()))
        assert len(pads) == len(terminal_labels[fp.GetReference()][1])
        assert all(abs(k.ToMM(p.GetPosition().x) - 30) < .001 for p in pads)
        assert all(a.GetPosition().y < z.GetPosition().y for a, z in zip(pads, pads[1:]))
    # No host copper/PCB below antenna: notch spans the stock footprint's rule area.
    assert str(refs['U_NET'].GetFPID().GetLibItemName()) == 'ESP32-S3-WROOM-1'
    for r in ['U_GPS', 'J_ANT', 'U_IMU', 'J_OIL', 'J_COOL', 'J_TACH', 'U_OPTO', 'J_AFR',
              'U_NET', 'U_NET_PWR', 'J_NET_SERVICE', 'BT1', 'J_VBAT_AUX']:
        assert r in refs, r
    k.SaveBoard(str(PCB), b)
    s = PCB.read_text()
    # SWIG may return copies of model records, so rewrite paths after SaveBoard.
    for oldpath in set(re.findall(r'\$\{KICAD9_3DMODEL_DIR\}/[^\"]+', s)):
        relative = oldpath.split('}/', 1)[1].replace('.step', '.wrl')
        candidates = [MODELS / relative, OLD_MODELS / relative]
        target = next((p for p in candidates if p.is_file()), None)
        assert target, f'Missing visible component model: {relative}'
        s = s.replace(oldpath, str(target))
    s = s.replace('(type "Top Solder Mask")', '(type "Top Solder Mask") (color "#0b703ddd")')
    s = s.replace('(type "Bottom Solder Mask")', '(type "Bottom Solder Mask") (color "#0b703ddd")')
    PCB.write_text(s)
    (HERE / 'visual-inventory.json').write_text(json.dumps({
        'status': 'CONCEPT ONLY - NO CIRCUIT / NO ROUTING / NOT FOR MANUFACTURE',
        'working_board_envelope_mm': [130, 140],
        'parts_are_representative_not_a_bom': True,
        'screw_terminals_edge': 'LEFT; pad 1 at top; wire entries left',
        'terminal_labels_proposed_not_wiring_release': terminal_labels,
        'wifi_antenna': 'INTERNAL PCB antenna; no external WiFi connector',
        'antenna_notch_mm': [40, 154, 88, 160],
        'placements': {r: {'x': k.ToMM(fp.GetPosition().x), 'y': k.ToMM(fp.GetPosition().y),
                           'description': fp.GetValue()} for r, fp in refs.items()},
    }, indent=2) + '\n')
    print(f'Wrote placement-only drawing: {len(refs)} representative parts, no nets/tracks')


def render():
    for name, extra, w, h in [('angle', ['--rotate', '330,0,17'], '2500', '2000'),
                             ('top', ['--side', 'top'], '1900', '1700')]:
        with (HERE / f'{name}.log').open('w') as log:
            subprocess.run(['kicad-cli', 'pcb', 'render', '--width', w, '--height', h,
                            '--zoom', '.82', '--quality', 'high', '--background', 'transparent',
                            *extra, '-o', str(HERE / f'{name}-raw.png'), str(PCB)],
                           stdout=log, stderr=subprocess.STDOUT, check=True)


def compose():
    W, H = 2560, 1900
    im = Image.new('RGB', (W, H), '#f1f5f7')
    d = ImageDraw.Draw(im)
    f = lambda n, bold=False: ImageFont.truetype(BOLD if bold else FONT, n)
    d.rectangle((0, 0, W, 205), fill='#102431')
    d.text((70, 36), 'RACECAR-35  /  INTEGRATED SENSOR BOARD', font=f(49, True), fill='white')
    d.text((74, 107), 'REV C   •   PROPOSED ASSEMBLED LAYOUT   •   1990–2005 SPEC MIATA',
           font=f(25), fill='#87ddc3')
    d.text((74, 155), '130 × 140 mm concept   |   Screw terminals: LEFT EDGE   |   INTERNAL WiFi antenna',
           font=f(23), fill='#d8e5ed')

    # Large angled assembly, plus a second, clearly readable top view below.
    raw = Image.open(HERE / 'angle-raw.png').convert('RGBA')
    raw = raw.crop(raw.getbbox()); raw.thumbnail((1770, 1210), Image.Resampling.LANCZOS)
    x = 35 + (1770 - raw.width) // 2; y = 235 + (1220 - raw.height) // 2
    alpha = raw.getchannel('A').filter(ImageFilter.GaussianBlur(9))
    shadow = Image.new('RGBA', raw.size, (20, 30, 38, 40))
    shadow.putalpha(alpha.point(lambda a: int(a * .12)))
    im.paste(shadow, (x + 7, y + 12), shadow); im.paste(raw, (x, y), raw)

    d = ImageDraw.Draw(im)
    cards = [
        ('1', 'GPS SOLDERED ON BOARD', ['NEO-M9N receiver + SMA antenna jack', 'RF bias / filtering beside the receiver', 'No external GPS breakout or UART cable']),
        ('2', 'IMU SOLDERED ON BOARD', ['Bare accelerometer / gyro IC', 'Local capacitors and I²C support', 'No external GY-521 module']),
        ('3', 'DIRECT OIL-PRESSURE WIRING', ['Three-wire sender terminal', '5 V supply • return • analogue signal', 'Scaling, filtering and protection area']),
        ('4', 'DIRECT COOLANT WIRING', ['Two-wire thermistor terminal', 'Matched pull-up / filtering / protection', 'Sender calibration still to be finalized']),
        ('5', 'ONBOARD TACH OPTOCOUPLER', ['ECU / cluster signal → high-Z front end', 'Optocoupler → Teensy pin 9', 'NEVER coil negative / spark / injector']),
    ]
    for i, (n, title, lines) in enumerate(cards):
        yy = 242 + i * 222
        d.rounded_rectangle((1830, yy, 2490, yy + 202), 16, fill='white', outline='#d2dfe5', width=2)
        d.ellipse((1852, yy + 18, 1899, yy + 65), fill='#19755f')
        d.text((1875, yy + 40), n, font=f(25, True), fill='white', anchor='mm')
        d.text((1916, yy + 24), title, font=f(22, True), fill='#173f4b')
        for j, text in enumerate(lines):
            d.text((1860, yy + 83 + j * 34), text, font=f(21), fill='#455f6b')
    d.text((1840, 1379), 'Teensy remains socketed for service.', font=f(21, True), fill='#173f4b')
    d.text((1840, 1413), 'Internal WiFi antenna + RTC cell at the bottom.', font=f(19), fill='#586f7a')
    d.text((1840, 1445), 'AFR: external AEM 30-0300 gauge output only.', font=f(19), fill='#8b4521')

    # Supplemental flat orientation view, helpful for finding the small IMU/opto.
    d.rounded_rectangle((70, 1478, 640, 1812), 15, fill='white', outline='#d2dfe5', width=2)
    d.text((92, 1492), 'TOP VIEW', font=f(17, True), fill='#395361')
    top = Image.open(HERE / 'top-raw.png').convert('RGBA')
    top = top.crop(top.getbbox()); top.thumbnail((470, 275), Image.Resampling.LANCZOS)
    im.paste(top, (355 - top.width // 2, 1527), top)
    d = ImageDraw.Draw(im)
    d.text((702, 1495), 'DIRECT TRUNK UPLOAD - SCREEN WIFI KEEPS OTA + FALLBACK', font=f(25, True), fill='#173f4b')
    stages = [('TEENSY SD', 'Owns the files'), ('LOCAL SPI', 'New transport required'),
              ('ESP32-S3 WIFI', 'Independent radio'), ('SERVER', 'No screen in this path')]
    for i, (title, sub) in enumerate(stages):
        xx = 705 + i * 441
        d.rounded_rectangle((xx, 1558, xx + 397, 1670), 13, fill='#e1efe9', outline='#93b9aa', width=2)
        d.text((xx + 198, 1591), title, font=f(21, True), fill='#1a5646', anchor='mm')
        d.text((xx + 198, 1632), sub, font=f(20), fill='#466c61', anchor='mm')
        if i < 3:
            d.line((xx + 402, 1612, xx + 432, 1612), fill='#547565', width=4)
            d.polygon([(xx + 432, 1612), (xx + 422, 1605), (xx + 422, 1619)], fill='#547565')
    d.text((705, 1703), 'Screen WiFi stays independent for OTA/fallback. Main power target: 10–20 V in → 5 V / 5 A TOTAL.',
           font=f(22), fill='#456371')
    d.text((705, 1744), 'CR2032 → Teensy VBAT: keeps the RTC only. Replaceable cell, not forever. No charging circuit.',
           font=f(21), fill='#456371')
    d.rectangle((0, 1830, W, H), fill='#fff0dd')
    d.text((72, 1845), 'LAYOUT CONCEPT — NOT ROUTED / NOT FOR MANUFACTURE', font=f(23, True), fill='#8b4521')
    d.text((72, 1877), 'Parts, passive counts and positions are illustrative; final circuitry, calibration, RF layout and validation remain open.',
           font=f(16), fill='#775d47')
    im.save(OUT_PNG)
    shutil.copy2(OUT_PNG, Path('/home/chris/Downloads/Racecar-RevC-Integrated-Preview.png'))
    print('Saved', OUT_PNG)


def functional_svg():
    # System connection drawing; NO pin numbers or resistor values are claimed.
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="1800" height="1540" viewBox="0 0 1800 1540">',
             '<rect width="1800" height="1540" fill="#f1f5f7"/>',
             '<style>text{font-family:DejaVu Sans,sans-serif;fill:#173f4b}.title{font-size:35px;font-weight:bold}.h{font-size:24px;font-weight:bold}.s{font-size:20px}</style>',
             '<defs><marker id="arrow" markerWidth="10" markerHeight="7" refX="9" refY="3.5" orient="auto"><polygon points="0 0,10 3.5,0 7" fill="#578575"/></marker></defs>']
    def text(s, x, y, cls='s'):
        parts.append(f'<text x="{x}" y="{y}" class="{cls}">{xml.escape(s)}</text>')
    def box(x, y, w, h, title, lines, fill='#fff'):
        parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" stroke="#9fb7b2" stroke-width="2"/>')
        text(title, x + 20, y + 36, 'h')
        for i, line in enumerate(lines): text(line, x + 20, y + 72 + 28 * i)
    def arrow(x1, y1, x2, y2):
        parts.append(f'<path d="M{x1},{y1} H{x2}" stroke="#578575" stroke-width="3" fill="none" marker-end="url(#arrow)"/>')
    text('REV C / INTEGRATED SYSTEM CONNECTIONS', 60, 65, 'title')
    text('Functional drawing only — proposed interfaces, not a circuit schematic or wiring pinout', 60, 105)
    parts.append('<rect x="520" y="145" width="1220" height="1190" rx="20" fill="#e2eee8" stroke="#83ad98" stroke-width="3"/>')
    text('MAIN PCB — ALL SENSOR INTERFACE ELECTRONICS HERE', 550, 187, 'h')
    rows = [
        ('SMA antenna', ['External antenna only'], 'Soldered NEO-M9N', ['RF bias / protection / filtering', 'UART to Teensy RX7 / TX8']),
        ('Oil-pressure sender', ['AUTEX 150 PSI candidate', '5 V / signal / return'], 'Oil input circuit', ['Regulated supply, scaling, filtering', 'Protected analogue input → A2 / 16']),
        ('Coolant sender', ['Delphi TS10075 candidate', 'Dedicated two-wire connection'], 'Coolant input circuit', ['Matched pull-up, filter, protection', 'Calibration required → A3 / 17']),
        ('ECU / cluster tach', ['Conditioned signal + return', 'NEVER coil / spark / injector'], 'Onboard tach circuit + OPTO', ['High-impedance front end + driver', 'Clean 3.3 V pulses → pin 9']),
        ('AEM 30-0300 GAUGE', ['WHITE signal / BROWN return', 'Gauge stays powered externally'], 'AFR scale / filter / protect', ['0.500 gain target → A6 / pin 20', 'No sensor / heater / source jumper']),
    ]
    for i, (a, lines, c, inner) in enumerate(rows):
        y = 220 + 158 * i
        box(60, y, 380, 128, a, lines)
        box(560, y, 540, 128, c, inner)
        arrow(442, y + 63, 552, y + 63)
        arrow(1103, y + 63, 1250, y + 63)
    box(1260, 220, 420, 760, 'SOCKETED TEENSY 4.1', ['Main controller + built-in micro-SD', '',
        'Soldered IMU on the same PCB', 'I²C on SDA18 / SCL19', '', '10–20 V input / 5 V supply target',
        'Separate auxiliary 3.3 V rail', '', 'Powered display UART → Serial3', 'CrowPanel verified J10 only', '', 'SPI → TRUNK WiFi coprocessor', 'VBAT ← 3V CR2032 + GND'])
    box(560, 1010, 540, 145, 'TRUNK WIFI / ESP32-S3 (NEW)',
        ['Short SPI link, own 3.3V supply', 'INTERNAL PCB antenna; direct upload', 'Coprocessor firmware still required'])
    box(1140, 1010, 540, 145, 'RTC BACKUP / CR2032 (NEW)',
        ['Positive → Teensy VBAT; negative → GND', 'RTC only, finite life; DO NOT CHARGE', 'Auxiliary VBAT contact must be added'])
    box(560, 1180, 1120, 115, 'SCREEN WIFI STAYS — OTA DOWNLOAD PATH UNCHANGED',
        ['Screen self-update + UART Teensy update. Optional screen-upload fallback.'])
    box(60, 1370, 1680, 100, 'ARCHITECTURE ONLY — NO NEW WORKING UPLINK OR FABRICATION FILES',
        ['Review power, SPI ownership/software, RF/EMC, RTC retention and actual-Internet resynchronization.'], fill='#fff0dd')
    parts.append('</svg>')
    (ROOT / 'CONNECTIONS-CONCEPT.svg').write_text('\n'.join(parts) + '\n')


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--compose-only', action='store_true')
    a = p.parse_args()
    # Check preservation of ALL Rev A fabrication/editable files, not just the PCB.
    protected = [p for base in [REVA / 'design', REVA / 'gerbers'] for p in base.rglob('*') if p.is_file()]
    before = {p: hashlib.sha256(p.read_bytes()).digest() for p in protected}
    if not a.compose_only:
        build_placement(); render()
    compose(); functional_svg()
    assert all(hashlib.sha256(p.read_bytes()).digest() == digest for p, digest in before.items()), 'Rev A modified!'
    print(f'Preserved {len(before)} Rev A source/manufacturing files unchanged')

if __name__ == '__main__':
    main()

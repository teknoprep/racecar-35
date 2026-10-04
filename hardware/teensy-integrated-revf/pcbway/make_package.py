#!/usr/bin/env python3
"""Build the PCBWay turnkey package for a revision: the grouped assembly BOM, the
pick-and-place CSV and the gerber-only ZIP.

It reads `components.json` (the same source as `design/BOM.csv`) and the routed PCB,
so the package always matches the engineering export. No documents travel in the
gerber ZIP; the order settings come from the order guide.

Usage: python3 pcbway/make_package.py [revision-directory]
       (defaults to the directory containing this script's parent)
"""
import csv
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parents[1]
DESIGN = ROOT / 'design'
PCB = next(DESIGN.glob('racecar-integrated-rev*.kicad_pcb'))
NAME = PCB.stem
REV = 'RevF' if 'revf' in NAME else ('RevE' if 'reve' in NAME else NAME)
OUT = ROOT / 'pcbway'
OUT.mkdir(exist_ok=True)
parts = json.loads((ROOT / 'components.json').read_text())

# --- grouped turnkey assembly BOM, in PCBWay's column layout -------------------
rows, index = [], {}
for p in parts:
    fp = p['footprint'].split(':', 1)[-1]
    key = (p['mpn'], p['maker'], fp)
    if key not in index:
        index[key] = len(rows)
        rows.append({'comment': f"{p['value']} ({p['alias']})", 'refs': [p['ref']],
                     'fp': fp, 'mpn': p['mpn'], 'maker': p['maker']})
    else:
        rows[index[key]]['refs'].append(p['ref'])

# The assembly BOM is a bill of MATERIALS the assembler can actually buy. U1 in
# components.json is the placeholder 'TEENSY41' (a Teensy cannot be sourced/fitted), so the
# assembly BOM asks for what is really fitted there: two 1x24 sockets. Same convention the
# accepted Rev D order used. The other entries just state what the assembler may NOT fit.
FIXUPS = {
    'TEENSY41': ('FIT 2x 1x24 2.54mm THT FEMALE SOCKET (Samtec SSW-124-01-G-S or equivalent; '
                 'source and fit ONLY these). The Teensy 4.1 is CUSTOMER SUPPLIED - we buy it WITH '
                 'PINS and plug it in; do NOT source or fit a Teensy.', 'SSW-124-01-G-S', 'Samtec'),
    'TSW-102-07-G-S': ('JP1 MCU POWER / JP2 CAN TERM 2-pin headers - FIT THE HEADERS ONLY. Do NOT '
                       'fit the 2.54mm shunts: the customer fits the JP1 shunt after rail tests and '
                       'the JP2 shunt only when this board is an end of the CAN bus.', None, None),
    '1058': ('CR2032 HOLDER only - fit the Keystone 1058 holder. The CR2032 CELL is customer '
             'supplied (fit after reflow/cleaning, primary cell, never charge).', None, None),
    'B2B-XH-A(LF)(SN)': ('J9 RTC CELL LEAD connector - fit the JST connector only. The two-wire lead '
                         'to the Teensy VBAT/GND pads is hand-made by the customer.', None, None),
    '1729021': ('3-pos 5.08mm Phoenix field terminal - J14 CAN (1 CANH / 2 CANL / 3 GND), J4 OIL, '
                'J11 THROTTLE, J12 BRAKE (the analog three are 1 +5V / 2 return / 3 signal).', None, None),
    'TCAN1042HGVDRQ1': ('U21 CAN TRANSCEIVER - TI TCAN1042HGVDRQ1 from AUTHORIZED DISTRIBUTION ONLY '
                        '(TI / Mouser / Digi-Key / LCSC-original). Do NOT substitute without written '
                        'approval; no clones, no re-marked parts. The "V" suffix is MANDATORY - it is the '
                        'VIO pin (pin 5); a non-V TCAN1042 has pin 5 = NC and would put 5V on the 3.3V '
                        'Teensy. Approved alternates only: TCAN1042VDRQ1, then NXP TJA1051T/3/1J (its '
                        'pin 8 S must also go to GND).', None, None),
}
for r in rows:
    if r['mpn'] in FIXUPS:
        c, mpn, maker = FIXUPS[r['mpn']]
        r['comment'] = c
        if mpn:
            r['mpn'] = mpn
        if maker:
            r['maker'] = maker
bom = OUT / f'Racecar-{REV}-BOM-pcbway-assembly.csv'
with bom.open('w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['No.', 'Quantity', 'Comment', 'Designator', 'Footprint',
                'Manufacturer Part Number', 'Manufacturer', 'Supplier Part Number'])
    for i, r in enumerate(rows, 1):
        w.writerow([i, len(r['refs']), r['comment'], ' '.join(r['refs']),
                    r['fp'], r['mpn'], r['maker'], ''])

# --- pick-and-place; PCBWay wants origin at the board lower-left, Y positive up -
tmp = Path(tempfile.mkdtemp(prefix='pcbway-package-'))
subprocess.run(['kicad-cli', 'pcb', 'export', 'pos', '--format', 'csv', '--units', 'mm',
                '--side', 'both', '-o', str(tmp / 'pos.csv'), str(PCB)],
               check=True, stdout=subprocess.DEVNULL)
cpl = OUT / f'Racecar-{REV}-CPL.csv'
with (tmp / 'pos.csv').open() as src, cpl.open('w', newline='') as dst:
    w = csv.writer(dst)
    w.writerow(['Designator', 'Mid X', 'Mid Y', 'Layer', 'Rotation'])
    for row in csv.DictReader(src):
        w.writerow([row['Ref'], f"{float(row['PosX']):.4f}", f"{-float(row['PosY']):.4f}",
                    row['Side'].capitalize(), str(float(row['Rot']))])

# --- gerber-only ZIP (copper, mask, silk, paste, outline, PTH + NPTH drills) ---
gz = tmp / 'gerbers'
gz.mkdir()
layers = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu', 'F.Mask', 'B.Mask',
          'F.Silkscreen', 'B.Silkscreen', 'F.Paste', 'B.Paste', 'Edge.Cuts']
subprocess.run(['kicad-cli', 'pcb', 'export', 'gerbers', '--no-protel-ext',
                '--layers', ','.join(layers), '-o', str(gz) + '/', str(PCB)],
               check=True, stdout=subprocess.DEVNULL)
subprocess.run(['kicad-cli', 'pcb', 'export', 'drill', '--format', 'excellon',
                '--excellon-units', 'mm', '--excellon-separate-th',
                '--generate-map', '--map-format', 'pdf', '-o', str(gz) + '/', str(PCB)],
               check=True, stdout=subprocess.DEVNULL)
zip_path = OUT / f'Racecar-{REV}-PCBWAY-GERBERS.zip'
with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
    for p in sorted(gz.rglob('*')):
        if p.is_file():
            z.write(p, p.name)

print(f'Wrote {bom.name}, {cpl.name}, {zip_path.name} '
      f'({len(rows)} BOM lines, {len(parts)} placements, {len(list(gz.iterdir()))} gerber/drill files)')

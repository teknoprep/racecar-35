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
import hashlib
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
    # NOT A COMPONENT - the bare SS56 is normally SMA (DO-214AC).
    'SS56': ('SMB (DO-214AA) 5 A / 60 V Schottky ONLY - e.g. LCSC C2687867 (BORN SS56 SMB) or '
             'Diotec SK56. Do NOT fit the DO-214AC (SMA) SS56: it does not fit this land pattern.',
             None, None),
}

# PCB FEATURES, NOT PARTS - the M3 mounting holes (H1-H4, non-plated) and the test pads
# (TP1-TP7, bare copper). There is nothing to buy or place, and they must not appear in
# the assembly BOM at all: assemblers match BOM designators against the CPL, and a BOM
# designator that has no placement STALLS their part matching (JLCPCB, 2026-10-08: the
# H1-H4/TP1-TP7 rows were the only BOM-vs-CPL mismatch in Rev F). Drop the rows, leave
# the CPL alone - the pads are in the Gerbers where they belong.
NOT_PARTS = {'M3 insulated hardware', 'PCB test pad'}

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
    written = 0
    bom_refs = []
    for r in rows:
        if r['mpn'] in NOT_PARTS:
            continue
        written += 1
        r['no'] = written                      # the number PCBWay sees in the BOM
        bom_refs += r['refs']
        w.writerow([written, len(r['refs']), r['comment'], ' '.join(r['refs']),
                    r['fp'], r['mpn'], r['maker'], ''])

# --- pick-and-place; PCBWay wants origin at the board lower-left, Y positive up -
tmp = Path(tempfile.mkdtemp(prefix='pcbway-package-'))
subprocess.run(['kicad-cli', 'pcb', 'export', 'pos', '--format', 'csv', '--units', 'mm',
                '--side', 'both', '-o', str(tmp / 'pos.csv'), str(PCB)],
               check=True, stdout=subprocess.DEVNULL)
cpl = OUT / f'Racecar-{REV}-CPL.csv'
cpl_refs = []
with (tmp / 'pos.csv').open() as src, cpl.open('w', newline='') as dst:
    w = csv.writer(dst)
    w.writerow(['Designator', 'Mid X', 'Mid Y', 'Layer', 'Rotation'])
    for row in csv.DictReader(src):
        cpl_refs.append(row['Ref'])
        w.writerow([row['Ref'], f"{float(row['PosX']):.4f}", f"{-float(row['PosY']):.4f}",
                    row['Side'].capitalize(), str(float(row['Rot']))])

# --- HARD GUARD: the BOM and the CPL must list the SAME designators. Assemblers run a
#     BOM-vs-CPL correspondence check, and a BOM designator with no placement stalls their
#     part matching (JLCPCB query, 2026-10-08: H1-H4/TP1-TP7 were in the BOM only). This
#     check fails hard - printing and continuing is how the bad package shipped last time.
if sorted(bom_refs) != sorted(cpl_refs):
    raise SystemExit(
        f"ABORT: BOM and CPL designators differ - "
        f"BOM-only {sorted(set(bom_refs) - set(cpl_refs))}, "
        f"CPL-only {sorted(set(cpl_refs) - set(bom_refs))}")

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

# --- "Actual Purchase Mfg Part #" list, in PCBWay's own column layout -----------
# PCBWay's quotation asks the CUSTOMER to confirm the exact part that will be
# purchased for every line (`*Actual Purchase Mfg Part #`). Our BOM already names
# one exact MPN + maker per line, so the answer IS that line; this file puts it in
# their column order with the fit/no-fit instruction beside it. It is generated
# from the same rows as the BOM, so the two can never disagree.
mpn_csv = OUT / f'Racecar-{REV}-ACTUAL-PURCHASE-MPN.csv'
with mpn_csv.open('w', newline='') as f:
    w = csv.writer(f)
    w.writerow(['Item #', 'Designator', 'Qty', 'Mfg Part #',
                'Actual Purchase Mfg Part #', 'Manufacturer', 'Customer Reply / Note'])
    for r in sorted((r for r in rows if r['mpn'] not in NOT_PARTS),
                    key=lambda r: r['no']):
        w.writerow([r['no'], ' '.join(r['refs']), len(r['refs']),
                    r['mpn'], r['mpn'], r['maker'], r['comment']])

# --- complete upload bundle (what we hand PCBWay) ------------------------------
# PCBWay asked for the COMPLETE file set, not one layer, so ship every order input
# in one ZIP: gerbers, BOM, CPL, the exact-purchase-part list, the order settings
# and the full engineering package. Fail hard if the engineering ZIP is stale - a
# bundle that disagrees with the routed board is exactly the kind of thing that
# gets a board built to the wrong copper.
eng = ROOT / f'Racecar-{REV}-GERBERS-ENGINEERING-PROTOTYPE.zip'
if not eng.is_file():
    raise SystemExit(f'ABORT: {eng.name} missing - run fabrication/export.py --engineering-prototype')
if int(eng.stat().st_mtime) < int(PCB.stat().st_mtime):
    raise SystemExit(f'ABORT: {eng.name} is older than {PCB.name} - re-export the engineering ZIP')
members = [zip_path, bom, cpl, mpn_csv,
           OUT / 'PCBWAY-ORDER-GUIDE.md', OUT / 'REVF-CHANGES-AND-ORDER.md', eng]
missing = [m.name for m in members if not m.is_file()]
if missing:
    raise SystemExit('ABORT: bundle inputs missing: ' + ', '.join(missing))
sums = ''.join(f'{hashlib.sha256(m.read_bytes()).hexdigest()}  {m.name}\n' for m in members)
bundle = OUT / f'Racecar-{REV}-PCBWAY-UPLOAD-ALL-FILES.zip'
with zipfile.ZipFile(bundle, 'w', zipfile.ZIP_DEFLATED) as z:
    for m in members:
        z.write(m, m.name)
    z.writestr('SHA256SUMS.txt', sums)

print(f'Wrote {bom.name}, {cpl.name}, {mpn_csv.name}, {zip_path.name}, {bundle.name} '
      f'({written} BOM lines, {len(cpl_refs)} placements, {len(list(gz.iterdir()))} gerber/drill files, '
      f'{len(members)} bundle files)')

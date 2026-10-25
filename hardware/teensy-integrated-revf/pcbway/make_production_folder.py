#!/usr/bin/env python3
"""Assemble everything PCBWay needs into ONE folder.

Produces `pcbway/PRODUCTION-READY-<date>/`:

  README-PRODUCTION.md            what each file is + what to upload where
  gerbers/                        the complete 16-file gerber/drill set, unpacked
  Racecar-RevF-PCBWAY-GERBERS.zip the same set as the file to upload
  Racecar-RevF-BOM-pcbway-assembly.csv
  Racecar-RevF-CPL.csv
  Racecar-RevF-ACTUAL-PURCHASE-MPN.csv
  Quotation ...(...)-REPLIED.xls/.xlsx   the agreed BOM in PCBWay's own sheet
  PCBWAY-ORDER-GUIDE.md, REVF-CHANGES-AND-ORDER.md, PCBWAY-REPLY-<date>.md
  Racecar-RevF-GERBERS-ENGINEERING-PROTOTYPE.zip (+ .sha256)
  docs/ASSEMBLY.md, docs/ASSEMBLY-EXTRAS.csv, docs/BRINGUP.md
  SHA256SUMS.txt

Nothing is hand-copied: it all comes from `make_package.py` /
`update_quotation_reply.py` outputs, and the script hard-fails if the gerber ZIP
is not the complete 16-file set, if the silk is not the verified 2026-10-19
layer, or if a source file is missing.

Usage: python3 pcbway/make_production_folder.py [--date YYYY-MM-DD]
"""
import argparse
import datetime
import hashlib
import pathlib
import re
import shutil
import sys
import zipfile

HERE = pathlib.Path(__file__).resolve().parent
REV = HERE.parent
HARDWARE = REV.parent          # hardware/ (quotations live here)
DESIGN = REV / 'design'


def sha(p: pathlib.Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--date', default=datetime.date.today().isoformat())
    args = ap.parse_args()

    out = HERE / f'PRODUCTION-READY-{args.date}'
    gerber_zip = HERE / 'Racecar-RevF-PCBWAY-GERBERS.zip'
    eng = REV / 'Racecar-RevF-GERBERS-ENGINEERING-PROTOTYPE.zip'
    addon_silk = HERE / 'addon-silkscreen' / 'racecar-integrated-revf-F_Silkscreen.gbr'

    # newest REPLIED quotation (PCBWay reissues under new date/quantity)
    quotes = [p for p in HARDWARE.glob('Quotation for *T-2SJ3W1113248A*RevF*REPLIED*.xls')
              if p.suffix == '.xls']
    if not quotes:
        raise SystemExit('ABORT: no REPLIED quotation - run pcbway/update_quotation_reply.py first')
    quote_xls = max(quotes, key=lambda p: p.stat().st_mtime)
    quote_xlsx = quote_xls.with_suffix('.xlsx')
    if not quote_xlsx.is_file():
        raise SystemExit(f'ABORT: {quote_xlsx.name} missing (update_quotation_reply.py writes both)')

    # --- hard checks: complete gerber set, and the verified silk --------------
    with zipfile.ZipFile(gerber_zip) as z:
        names = z.namelist()
        if len(names) != 16:
            raise SystemExit(f'ABORT: {gerber_zip.name} has {len(names)} files, expected 16')
        need = {'racecar-integrated-revf-F_Silkscreen.gbr',
                'racecar-integrated-revf-B_Silkscreen.gbr',
                'racecar-integrated-revf-Edge_Cuts.gbr',
                'racecar-integrated-revf-PTH.drl', 'racecar-integrated-revf-NPTH.drl',
                'racecar-integrated-revf-job.gbrjob'}
        missing = need - set(names)
        if missing:
            raise SystemExit('ABORT: gerber ZIP missing ' + ', '.join(sorted(missing)))
        silk = z.read('racecar-integrated-revf-F_Silkscreen.gbr')
    addon = addon_silk.read_bytes()


    def norm(b: bytes) -> list:
        out_lines = []
        for ln in b.decode('utf8', 'ignore').splitlines():
            s = ln.strip()
            if re.match(r'^(G04[^G]*?(creationdate|date|created).*|%TF\.(CreationDate|GenerationSoftware).*)$', s, re.I):
                continue
            if s.startswith(';') and re.search(r'date|creation|KiCad', s, re.I):
                continue
            out_lines.append(s)
        return out_lines


    if norm(silk) != norm(addon):
        raise SystemExit('ABORT: the gerber ZIP does not carry the verified 2026-10-19 silkscreen')

    members = [
        (gerber_zip, 'Racecar-RevF-PCBWAY-GERBERS.zip', ''),
        (HERE / 'Racecar-RevF-BOM-pcbway-assembly.csv', '', ''),
        (HERE / 'Racecar-RevF-CPL.csv', '', ''),
        (HERE / 'Racecar-RevF-ACTUAL-PURCHASE-MPN.csv', '', ''),
        (quote_xls, '', ''),
        (quote_xlsx, '', ''),
        (HERE / 'PCBWAY-ORDER-GUIDE.md', '', ''),
        (HERE / 'REVF-CHANGES-AND-ORDER.md', '', ''),
        (HERE / 'PCBWAY-REPLY-2026-10-22.md', '', ''),
        (eng, '', ''),
        (eng.with_name(eng.name + '.sha256'), '', ''),
        (DESIGN / 'ASSEMBLY.md', 'docs', ''),
        (DESIGN / 'ASSEMBLY-EXTRAS.csv', 'docs', ''),
        (DESIGN / 'BRINGUP.md', 'docs', ''),
    ]
    absent = [p.name for p, _, _ in members if not p.is_file()]
    if absent:
        raise SystemExit('ABORT: missing source files: ' + ', '.join(absent))

    if out.exists():
        shutil.rmtree(out)
    (out / 'gerbers').mkdir(parents=True)
    (out / 'docs').mkdir()

    for p, name, sub in members:
        dest = out / sub / (name or p.name)
        shutil.copy2(p, dest)

    with zipfile.ZipFile(gerber_zip) as z:
        z.extractall(out / 'gerbers')

    (out / 'README-PRODUCTION.md').write_text(README.format(date=args.date), encoding='utf-8')

    lines = []
    for p in sorted(out.rglob('*')):
        if p.is_file() and p.name != 'SHA256SUMS.txt':
            lines.append(f'{sha(p)}  {p.relative_to(out).as_posix()}')
    (out / 'SHA256SUMS.txt').write_text('\n'.join(lines) + '\n', encoding='utf-8')

    total = sum(p.stat().st_size for p in out.rglob('*') if p.is_file())
    print(f'{out}')
    for p in sorted(out.rglob('*')):
        if p.is_file():
            print(f'  {p.stat().st_size:>9}  {p.relative_to(out).as_posix()}')
    print(f'  {total:>9}  TOTAL ({sum(1 for p in out.rglob("*") if p.is_file())} files)')
    return 0


README = """# Racecar-35 Rev F — production-ready package ({date})

Everything PCBWay needs for the order, in one folder. **The gerber and the BOM are
the two files that must be uploaded**; the rest is there so nothing has to be
looked up or re-explained.

## Upload
| Where | File |
|---|---|
| Gerber upload ("under review" slot) | `Racecar-RevF-PCBWAY-GERBERS.zip` (or the `gerbers/` folder, same 16 files) |
| Turnkey assembly BOM | `Racecar-RevF-BOM-pcbway-assembly.csv` |
| Turnkey assembly placement | `Racecar-RevF-CPL.csv` |
| Returned to PCBWay with the reply | `Quotation for T-2SJ3W1113248A-1unit-...-REPLIED.xls` (Customer Reply + Actual Purchase Mfg Part # filled) |

## The gerber set is COMPLETE — do not treat it as a single layer
`gerbers/` (and the ZIP) contains **16 files**:
4 copper (`F_Cu`, `In1_Cu`, `In2_Cu`, `B_Cu`), `F_Mask`, `B_Mask`, `F_Paste`,
`B_Paste`, `F_Silkscreen`, `B_Silkscreen`, `Edge_Cuts`, `PTH.drl`, `NPTH.drl`,
the two drill-map PDFs and `racecar-integrated-revf-job.gbrjob`.

The silkscreen is the 2026-10-19 revision (every connector named at the part with
pin labels, Teensy insertion orientation, crossed screen-lead table). It is
verified to be the same geometry as the standalone replacement layer that was
sent earlier.

## Order settings (bare PCB)
FR-4 · **4 layers** · **150.05 × 155.05 mm** · **1.6 mm** · **1 oz outer copper**
(inner layers per the fab's standard 4-layer stackup) · **ENIG** · **green** mask ·
**white** silkscreen · **impedance control ON** (50 ohm single-ended, GNSS feed) ·
0.20 mm min track / 0.15 mm min spacing / 0.30 mm min hole · **no panelisation** ·
PCB quantity: your 5-piece minimum · assembly quantity: **1** (see the quotation).
The 48 × 6 mm notch in the bottom edge is profile routing (`Edge_Cuts`), not an
internal slot. Full detail: `PCBWAY-ORDER-GUIDE.md`.

## BOM / assembly — the fit rules that matter
77 lines, one exact MPN per line (`Actual Purchase Mfg Part #` is filled on all of
them in the quotation sheet). BOM and CPL correspond **1:1: 182 designators each**
(H1–H4 / TP1–TP7 are PCB features — NPTH holes and bare-copper test pads — not parts).
* **U1** = **2 × `SSW-124-01-G-S`** sockets (one 2×24 footprint). **Do not fit a Teensy** —
  it is customer supplied, bought with pins, and plugs in.
* **JP1 / JP2** = headers only, **no shunts** (customer fits them).
* **BT1** = Keystone `1058` holder only, **no CR2032 cell**.
* **J9** = JST `B2B-XH-A(LF)(SN)` connector only; the RTC lead is customer-made.
* **U21** = TI `TCAN1042HGVDRQ1` — **no substitution**, authorized distribution only,
  the `V` suffix is mandatory (VIO on pin 5).
* **D23** = `SS56` **SMB (DO-214AA)** only, not the SMA/DO-214AC part.

## Files in this folder
| File | What it is |
|---|---|
| `Racecar-RevF-PCBWAY-GERBERS.zip` / `gerbers/` | complete 16-file fab set (same contents) |
| `Racecar-RevF-BOM-pcbway-assembly.csv` | turnkey BOM, PCBWay column layout, fit notes in Comment |
| `Racecar-RevF-CPL.csv` | pick-and-place, mm, Y flipped to PCBWay's convention |
| `Racecar-RevF-ACTUAL-PURCHASE-MPN.csv` | the exact part to buy per line (77) |
| `Quotation ...-REPLIED.xls/.xlsx` | PCBWay's own 1-unit quote with Customer Reply + Actual Purchase filled |
| `PCBWAY-ORDER-GUIDE.md` | order settings + the fit/no-fit table |
| `REVF-CHANGES-AND-ORDER.md` | what changed since Rev D/E and why (CAN, 5 V rail, input monitor) |
| `PCBWAY-REPLY-2026-10-22.md` | the reply text and the open commercial items |
| `Racecar-RevF-GERBERS-ENGINEERING-PROTOTYPE.zip` | full engineering package (schematics, reports, manifest) |
| `docs/ASSEMBLY.md` | connector pinouts, Teensy orientation, build order |
| `docs/ASSEMBLY-EXTRAS.csv` | customer-supplied / hand-fitted parts |
| `docs/BRINGUP.md` | first-power checks (incl. the U21 marking check) |
| `SHA256SUMS.txt` | hashes of every file here |

## Status
UNAPPROVED ENGINEERING PROTOTYPE. ERC/DRC/unconnected are all zero and the pin
contract is script-asserted, but there is **no independent human review and no
physical, thermal or automotive validation**. Ordering prototypes is exactly what
that status permits.
"""


if __name__ == '__main__':
    sys.exit(main())

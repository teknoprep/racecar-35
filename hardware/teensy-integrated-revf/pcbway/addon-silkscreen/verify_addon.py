#!/usr/bin/env python3
"""Prove the silkscreen add-on changed NOTHING but F.Silkscreen.

Compares every gerber/drill layer exported from the add-on board (same board +
the two added silk legends) against the layer files in the package that is
already uploaded to PCBWay, ignoring the embedded creation-date comments (the
only thing a re-plot changes on an otherwise identical layer).

Usage:
  python3 verify_addon.py <path-to-Racecar-RevF-PCBWAY-GERBERS.zip> [export-dir]

'exports dir' must contain the 11 gerbers + 2 drill files re-exported from the
add-on board (they are produced by the kicad-cli commands in SILKSCREEN-ADDON.md).
With no export dir the script only reports what is inside the zip.
"""
import sys, os, re, zipfile, tempfile, shutil

DATEY = re.compile(r'^(G04[^G]*?(creationdate|date|created).*|%TF\.CreationDate.*|%TF\.GenerationSoftware.*)$', re.I)


def norm(lines):
    out = []
    for ln in lines:
        s = ln.rstrip('\r\n')
        if DATEY.match(s.strip()):
            continue
        if s.strip().startswith(';') and re.search(r'date|creation', s, re.I):
            continue
        if s.strip().startswith(';') and 'KiCad' in s:
            continue
        out.append(s)
    return out


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(2)
    zpath = sys.argv[1]
    exp = sys.argv[2] if len(sys.argv) > 2 else None
    tmp = tempfile.mkdtemp()
    try:
        with zipfile.ZipFile(zpath) as z:
            z.extractall(tmp)
        names = sorted(n for n in os.listdir(tmp) if n.endswith(('.gbr', '.drl')))
        print(f'uploaded package: {os.path.basename(zpath)}  ({len(names)} layer files)')
        if not exp:
            for n in names:
                print(f'  {n}')
            return
        print(f"{'layer':<48} {'geometry':<22} note")
        bad = []
        for n in names:
            a = norm(open(os.path.join(tmp, n), errors='ignore'))
            p = os.path.join(exp, n)
            if not os.path.exists(p):
                print(f'  {n:<48} MISSING in export')
                bad.append(n)
                continue
            b = norm(open(p, errors='ignore'))
            same = a == b
            note = 'UNCHANGED' if same else ('silk only - expected' if 'F_Silkscreen' in n else 'CHECK!')
            print(f'  {n:<48} {"identical" if same else "differs":<22} {note}')
            if not same and 'F_Silkscreen' not in n:
                bad.append(n)
        # Fail HARD: a check that prints and exits 0 cannot stop a bad package.
        if bad:
            print('\nFAIL - these layers are not identical to the uploaded package: ' + ', '.join(bad))
            sys.exit(1)
        print('\nPASS - only racecar-integrated-revf-F_Silkscreen.gbr differs.')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == '__main__':
    main()

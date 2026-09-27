#!/usr/bin/env python3
"""Package a DESIGN-COMPLETION RFQ, never a Rev C manufacturing release.
Requires CairoSVG. Uses an explicit allowlist: no Rev A files, CAD, Gerbers,
render coordinates, firmware binaries, model libraries, logs or secrets.
"""
from pathlib import Path, PurePosixPath
from datetime import datetime, timezone
import csv
import hashlib
import io
import json
import re
import shutil
import tempfile
import textwrap
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape
import zipfile

import cairosvg
from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
REPO = ROOT.parents[1]
NAME = 'Racecar-RevC-DESIGN-REVIEW-NOT-FOR-FAB.zip'
DEST = ROOT / NAME
DOWNLOAD = Path('/home/chris/Downloads') / NAME


def read_first_pdf(text):
    lines = []
    for line in text.splitlines()[4:]:
        lines.extend(textwrap.wrap(line, width=88) or [''])
    assert len(lines) <= 62, 'First-page brief needs pagination; do not clip it'
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="210mm" height="297mm" viewBox="0 0 1240 1754">',
             '<rect width="1240" height="1754" fill="white"/>',
             '<rect width="1240" height="158" fill="#102431"/>',
             '<text x="65" y="68" font-family="DejaVu Sans" font-weight="bold" font-size="38" fill="white">RACECAR-35 / INTEGRATED REV C</text>',
             '<text x="65" y="118" font-family="DejaVu Sans" font-size="25" fill="#87ddc3">DESIGN COMPLETION + REVIEW / QUOTE PACKAGE</text>',
             '<rect x="45" y="177" width="1150" height="64" rx="8" fill="#fff0dd"/>',
             '<text x="66" y="219" font-family="DejaVu Sans" font-weight="bold" font-size="27" fill="#8b4521">NOT FOR FABRICATION OR ASSEMBLY</text>']
    pitch = min(25, 1380 / max(len(lines), 1))
    for i, line in enumerate(lines):
        parts.append(f'<text x="65" y="{283 + i * pitch:.2f}" font-family="DejaVu Sans Mono" font-size="19" fill="#173f4b">{escape(line)}</text>')
    parts.append('</svg>')
    return cairosvg.svg2pdf(bytestring='\n'.join(parts).encode())


def make_payload():
    # This handoff documents specific legacy firmware constraints. Fail rather
    # than silently ship stale documentation after a future firmware revision.
    for relative in ['src/main.cpp', 'crowpanel-arduino/RaceDash/RaceDash.ino']:
        text = (REPO / relative).read_text()
        version = re.search(r'^#define FIRMWARE_VERSION "([^"]+)"', text, re.M)
        if not version or version[1] != '0.1.149':
            raise RuntimeError('Review FIRMWARE-INTERFACE.md against current source before repackaging')
    docs = {
        'READ-ME-FIRST.txt': HERE / 'READ-ME-FIRST.txt',
        'VENDOR-REQUEST.txt': HERE / 'VENDOR-REQUEST.txt',
        'REQUIREMENTS.md': ROOT / 'REQUIREMENTS.md',
        'TACH_INTERFACE.md': ROOT / 'TACH_INTERFACE.md',
        'AFR_INTERFACE.md': ROOT / 'AFR_INTERFACE.md',
        'WIFI_RTC_ARCHITECTURE.md': ROOT / 'WIFI_RTC_ARCHITECTURE.md',
        'LAYOUT_AND_FABRICATION.md': ROOT / 'LAYOUT_AND_FABRICATION.md',
        'ASSEMBLY-AND-LABELS.md': HERE / 'ASSEMBLY-AND-LABELS.md',
        'FIRMWARE-INTERFACE.md': HERE / 'FIRMWARE-INTERFACE.md',
        'MAJOR-PART-CANDIDATES-NOT-A-BOM.csv': HERE / 'MAJOR-PART-CANDIDATES-NOT-A-BOM.csv',
        'MANUFACTURING-READINESS.csv': HERE / 'MANUFACTURING-READINESS.csv',
        'preview/README.md': HERE / 'ILLUSTRATION-LIMITS.md',
        'REV-C-INTEGRATED-PREVIEW.png': ROOT / 'REV-C-INTEGRATED-PREVIEW.png',
        'CONNECTIONS-CONCEPT.svg': ROOT / 'CONNECTIONS-CONCEPT.svg',
    }
    payload = {name: source.read_bytes() for name, source in docs.items()}
    for name, data in payload.items():
        if name.endswith('.csv'):
            rows = list(csv.reader(io.StringIO(data.decode())))
            assert rows and all(len(row) == len(rows[0]) for row in rows), name
    with Image.open(io.BytesIO(payload['REV-C-INTEGRATED-PREVIEW.png'])) as im:
        im.verify()
    ET.fromstring(payload['CONNECTIONS-CONCEPT.svg'])
    payload['READ-ME-FIRST.pdf'] = read_first_pdf(payload['READ-ME-FIRST.txt'].decode())
    payload['CONNECTIONS-CONCEPT.pdf'] = cairosvg.svg2pdf(bytestring=payload['CONNECTIONS-CONCEPT.svg'])
    for name in ['READ-ME-FIRST.pdf', 'CONNECTIONS-CONCEPT.pdf']:
        assert payload[name].startswith(b'%PDF-'), name
    manifest = {
        'package_type': 'DESIGN_COMPLETION_RFQ_NOT_FOR_FABRICATION',
        'manufacturing_ready': False,
        'manufacturing_authorized': False,
        'revision': 'Integrated Rev C concept',
        'created_utc': datetime.now(timezone.utc).isoformat(),
        'not_included': ['schematic/netlist', 'final BOM', 'routed PCB', 'Gerbers/drills',
                         'pick-and-place/CPL', 'approved programming image', 'approved test limits',
                         'Rev A manufacturing files', 'render-only CAD and visual coordinates'],
        'files': {name: {'size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
                  for name, data in payload.items()},
    }
    payload['PACKAGE-MANIFEST.json'] = (json.dumps(manifest, indent=2) + '\n').encode()
    return payload


def main():
    payload = make_payload()
    forbidden_suffixes = {'.kicad_pcb', '.kicad_sch', '.kicad_pro', '.drl', '.gbr',
                          '.gtl', '.gbl', '.gtp', '.bin', '.hex', '.wrl', '.step'}
    for name in payload:
        p = PurePosixPath(name)
        assert not p.is_absolute() and '..' not in p.parts
        assert p.suffix.lower() not in forbidden_suffixes
    with tempfile.TemporaryDirectory(prefix='racecar-revc-rfq-') as temporary:
        tmp = Path(temporary) / NAME
        with zipfile.ZipFile(tmp, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as z:
            for name, data in payload.items():
                z.writestr(name, data)
        with zipfile.ZipFile(tmp) as z:
            assert z.testzip() is None
            assert len(z.namelist()) == len(set(z.namelist()))
            assert set(z.namelist()) == set(payload)
            z.extractall(Path(temporary) / 'unpacked')  # path names validated above
            manifest = json.loads(z.read('PACKAGE-MANIFEST.json'))
            assert manifest['manufacturing_ready'] is False
            for name, entry in manifest['files'].items():
                data = (Path(temporary) / 'unpacked' / name).read_bytes()
                assert len(data) == entry['size']
                assert hashlib.sha256(data).hexdigest() == entry['sha256']
        # Publish only after the archive passes integrity/content checks.
        shutil.copy2(tmp, DEST)
        DOWNLOAD.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(tmp, DOWNLOAD)
    digest = hashlib.sha256(DEST.read_bytes()).hexdigest()
    assert hashlib.sha256(DOWNLOAD.read_bytes()).hexdigest() == digest
    (ROOT / (NAME + '.sha256')).write_text(f'{digest}  {NAME}\n')
    print(f'Created {DEST}\nDownloads copy: {DOWNLOAD}\n'
          f'{len(payload)} files, {DEST.stat().st_size:,} bytes\nSHA256 {digest}\n'
          'DESIGN-COMPLETION RFQ ONLY; NOT FOR FABRICATION')


if __name__ == '__main__':
    main()

#!/usr/bin/env python3
"""Fail-closed export of the real Rev C electrical PCB.
Default: independently reviewed prototype release. --engineering-prototype:
explicitly UNAPPROVED first-build engineering files, still gated on full CAD/net
checks. No invented human approval. Never accepts the placement-only preview.
"""
from pathlib import Path
from datetime import datetime, timezone
import hashlib
import json
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
NAME = 'racecar-integrated-revc'
OUTPUT = 'Racecar-RevC-GERBERS-PROTOTYPE.zip'
DOCUMENTS = ('BOM.csv', 'FABRICATION.md', 'ASSEMBLY.md', 'DESIGN_NOTES.md',
             'BRINGUP.md', 'ASSEMBLY-EXTRAS.csv', 'COMPONENT-ALIASES.json')
REVIEWS = ('full_electrical_review', 'footprints_and_pinouts', 'power_and_fault_analysis',
           'one_edge_terminal_access_and_labels', 'internal_wifi_rf_clearance',
           'wifi_spi_and_rtc_vbat_circuits', 'stackup_and_fabrication_spec')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def preflight(root, engineering=False):
    design = root / 'design'
    required = [design / (NAME + suffix) for suffix in ('.kicad_pcb', '.kicad_sch', '.kicad_pro')]
    required += [design / name for name in DOCUMENTS]
    if not engineering:
        required += [design / 'design-review.json']
    missing = [str(p.relative_to(root)) for p in required if not p.is_file()]
    require(not missing, 'Missing electrical-design/review inputs:\n  ' + '\n  '.join(missing))
    require(not any(p.is_symlink() for p in design.rglob('*')), 'No linked/aliased design inputs')
    if engineering:
        review = {'revision': 'C', 'approved_for_prototype_fabrication': False,
                  'reviewed_by': None, 'independent_review_complete': False,
                  'purpose': 'UNAPPROVED ENGINEERING PROTOTYPE; independent review and physical tests still required'}
    else:
        review = json.loads((design / 'design-review.json').read_text())
        require(review.get('revision') == 'C', 'Review must name revision C')
        require(review.get('approved_for_prototype_fabrication') is True,
                'Independent design review has not approved prototype fabrication')
        require(isinstance(review.get('reviewed_by'), str) and review['reviewed_by'].strip(),
                'Named reviewer required; clean DRC is not electrical approval')
        require(all(review.get('checks', {}).get(k) is True for k in REVIEWS), 'Incomplete review checklist')
    suffixes = {'.kicad_pcb', '.kicad_sch', '.kicad_pro', '.kicad_sym', '.kicad_mod'}
    inputs = [p for p in design.rglob('*') if p.is_file() and
              (p.suffix in suffixes or p.name in (*DOCUMENTS, 'fp-lib-table', 'sym-lib-table'))]
    current = {str(p.relative_to(design)): sha(p) for p in inputs}
    if engineering:
        review['source_sha256'] = current
    else:
        require(current == review.get('source_sha256'), 'Review hashes do not match ALL current design inputs')
    return design, review, current


def connected_pads(board):
    result = {}
    for fp in board.GetFootprints():
        for pad in fp.Pads():
            net = str(pad.GetNetname())
            if not net or net.startswith('unconnected-'):
                continue
            key = (str(fp.GetReference()), str(pad.GetNumber()))
            require(key not in result or result[key] == net, 'Conflicting duplicate PCB pad: ' + str(key))
            result[key] = net
    return result


def export(root=ROOT, engineering=False):
    output_name = 'Racecar-RevC-GERBERS-ENGINEERING-PROTOTYPE.zip' if engineering else OUTPUT
    destination = root / output_name
    # No stale successful ZIP may masquerade as this attempt's output.
    destination.unlink(missing_ok=True)
    destination.with_suffix('.zip.sha256').unlink(missing_ok=True)
    design, review, original_hashes = preflight(root, engineering)
    import pcbnew as k
    pcb = design / (NAME + '.kicad_pcb')
    sch = design / (NAME + '.kicad_sch')
    board = k.LoadBoard(str(pcb))
    require(str(board.GetTitleBlock().GetRevision()) == 'C', 'PCB title-block revision must be C')
    require(board.GetNetCount() > 5 and len(list(board.GetTracks())) > 0,
            'No genuine nets/routing: a placement-only PCB cannot be fabricated as this product')
    refs = {str(fp.GetReference()) for fp in board.GetFootprints()}
    aliases = json.loads((design / 'COMPONENT-ALIASES.json').read_text())
    required_aliases = {'U1', 'U_GPS', 'U_IMU', 'U_NET', 'BT1', 'J_AFR'}
    require(required_aliases <= set(aliases), 'Missing integrated-component identity map')
    require({aliases[n] for n in required_aliases} <= refs,
            'Integrated GPS/IMU/WiFi/RTC/AFR missing; do not substitute the Rev A carrier')
    pads = connected_pads(board)
    require(len(pads) > 50, 'Too few connected pads for an integrated Rev C')
    layers = [str(board.GetLayerName(n)) for n in k.LSET.AllCuMask().Seq()
              if board.GetEnabledLayers().Contains(n)]
    layers += ['F.Mask', 'B.Mask', 'F.Silkscreen', 'B.Silkscreen', 'F.Paste', 'B.Paste', 'Edge.Cuts']
    with tempfile.TemporaryDirectory(prefix='revc-fabrication-') as temporary:
        stage = Path(temporary)
        reports = stage / 'reports'; reports.mkdir()
        subprocess.run([sys.executable, str(root / 'electrical/verify.py')], check=True)
        (reports / 'pin-contract-report.json').write_bytes((design / 'pin-contract-report.json').read_bytes())
        gerbers = stage / 'gerbers'; gerbers.mkdir()
        def run(*args):
            subprocess.run(['kicad-cli', *map(str, args)], check=True)
        run('pcb', 'drc', '--format', 'json', '--severity-all', '--all-track-errors',
            '--exit-code-violations', '-o', reports / 'drc.json', pcb)
        run('sch', 'erc', '--format', 'json', '--severity-all', '--exit-code-violations',
            '-o', reports / 'erc.json', sch)
        drc = json.loads((reports / 'drc.json').read_text())
        erc = json.loads((reports / 'erc.json').read_text())
        require('violations' in drc and 'unconnected_items' in drc, 'Unknown DRC report schema')
        require(not drc['violations'] and not drc['unconnected_items'], 'DRC/unconnected items remain')
        require(bool(erc.get('sheets')) and all('violations' in s and not s['violations'] for s in erc['sheets']),
                'ERC violations or unknown report schema')
        run('sch', 'export', 'netlist', '--format', 'kicadxml', '-o', reports / 'netlist.xml', sch)
        tree = ET.parse(reports / 'netlist.xml')
        nets = {}
        for net in tree.findall('./nets/net'):
            name = net.get('name', '')
            if not name or name.startswith('unconnected-'):
                continue
            for node in net.findall('node'):
                key = (node.get('ref'), node.get('pin'))
                require(key not in nets or nets[key] == name, 'Conflicting schematic node')
                nets[key] = name
        require(nets == pads, 'Schematic and PCB connected-pad netlists differ')
        run('pcb', 'export', 'gerbers', '--no-protel-ext', '--layers', ','.join(layers),
            '-o', str(gerbers) + '/', pcb)
        run('pcb', 'export', 'drill', '--format', 'excellon', '--excellon-units', 'mm',
            '--excellon-separate-th', '--generate-map', '--map-format', 'pdf', '--generate-report',
            '--report-path', reports / 'drill-report.txt', '-o', str(gerbers) + '/', pcb)
        run('sch', 'export', 'pdf', '-o', stage / 'schematic.pdf', sch)
        run('pcb', 'export', 'pos', '--format', 'csv', '--units', 'mm', '--side', 'both',
            '-o', stage / 'assembly-positions.csv', pcb)
        for layer in layers:
            file = gerbers / (NAME + '-' + layer.replace('.', '_') + '.gbr')
            require(file.is_file() and file.stat().st_size > 0, 'Missing Gerber: ' + layer)
        require(len(list(gerbers.glob('*.drl'))) == 2, 'Separate PTH/NPTH drill files required')
        for name in DOCUMENTS:
            (stage / name).write_bytes((design / name).read_bytes())
        (stage / ('ENGINEERING-STATUS.json' if engineering else 'design-review.json')).write_text(json.dumps(review, indent=2) + '\n')
        picture = root / 'REV-C-ASSEMBLED-ENGINEERING-PROTOTYPE.png'
        render_report = root / 'electrical/render-report.json'
        if picture.exists():
            require(render_report.is_file(), 'Assembled picture has no source identity')
            rendered = json.loads(render_report.read_text())
            require(rendered.get('pcb_sha256') == sha(pcb) and rendered.get('png_sha256') == sha(picture),
                    'Assembled picture does not match this exact PCB')
            (stage / 'assembled-preview.png').write_bytes(picture.read_bytes())
            (reports / 'render-report.json').write_bytes(render_report.read_bytes())
        # Editable sources travel with fabrication layers, not just a render.
        import shutil
        for relative in original_hashes:
            target = stage / 'editable-CAD' / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(design / relative, target)
        _, _, now_hashes = preflight(root, engineering)
        require(now_hashes == original_hashes, 'Source changed during export')
        manifest = {
            'revision': 'C', 'type': 'CAD-CHECKED PROTOTYPE EXPORT; PHYSICAL VALIDATION STILL REQUIRED',
            'hardware_validated': False, 'independent_review_complete': not engineering,
            'approved_for_prototype_fabrication': not engineering, 'created_utc': datetime.now(timezone.utc).isoformat(),
            'connected_pads_checked': len(pads), 'gerber_layers': layers,
            'files': {str(p.relative_to(stage)): {'size': p.stat().st_size, 'sha256': sha(p)}
                      for p in sorted(stage.rglob('*')) if p.is_file()},
        }
        (stage / 'PACKAGE-MANIFEST.json').write_text(json.dumps(manifest, indent=2) + '\n')
        # Temp ZIP on same filesystem as destination; only a verified ZIP is published.
        with tempfile.TemporaryDirectory(prefix='.fabrication-', dir=root) as outdir:
            out = Path(outdir) / output_name
            with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
                for p in sorted(stage.rglob('*')):
                    if p.is_file():
                        z.write(p, str(p.relative_to(stage)))
            with zipfile.ZipFile(out) as z:
                require(z.testzip() is None, 'ZIP CRC failure')
                for name, info in manifest['files'].items():
                    require(hashlib.sha256(z.read(name)).hexdigest() == info['sha256'], 'ZIP hash mismatch')
            out.replace(destination)
    destination.with_suffix('.zip.sha256').write_text(sha(destination) + '  ' + destination.name + '\n')
    print('Exported', destination, '\nInspect Gerbers/drills before any prototype order.')


if __name__ == '__main__':
    try:
        import argparse
        parser = argparse.ArgumentParser(description=__doc__)
        parser.add_argument('--engineering-prototype', action='store_true',
                            help='Export explicitly unapproved engineering files; full CAD checks still mandatory')
        export(engineering=parser.parse_args().engineering_prototype)
    except (RuntimeError, OSError, ValueError, subprocess.CalledProcessError) as error:
        print('BLOCKED: no new fabrication package produced.\n' + str(error), file=sys.stderr)
        sys.exit(1)

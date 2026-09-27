"""Rejection/preflight tests only; NOT electrical or successful-Gerber-export tests."""
from pathlib import Path
import importlib.util
import json
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PATH = ROOT / 'hardware/teensy-integrated-revc/fabrication/export.py'
spec = importlib.util.spec_from_file_location('revc_export', PATH)
exporter = importlib.util.module_from_spec(spec)
spec.loader.exec_module(exporter)


class FabricationGuardTests(unittest.TestCase):
    def inputs(self, root, approved=False):
        design = root / 'design'; design.mkdir()
        for name in [exporter.NAME + s for s in ('.kicad_pcb', '.kicad_sch', '.kicad_pro')]:
            (design / name).write_text('UNIT TEST PLACEHOLDER, NOT CAD')
        for name in exporter.DOCUMENTS:
            (design / name).write_text('UNIT TEST, NOT A DESIGN APPROVAL')
        review = {'revision': 'C', 'reviewed_by': 'UNIT TEST ONLY',
                  'approved_for_prototype_fabrication': approved,
                  'checks': {name: True for name in exporter.REVIEWS},
                  'source_sha256': {p.name: exporter.sha(p) for p in design.iterdir()}}
        (design / 'design-review.json').write_text(json.dumps(review))
        return design, review

    def test_missing_design_cannot_leave_stale_zip(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); out = root / exporter.OUTPUT
            out.write_text('stale output')
            sidecar = out.with_suffix('.zip.sha256'); sidecar.write_text('stale hash')
            with self.assertRaisesRegex(RuntimeError, 'Missing electrical-design'):
                exporter.export(root)
            self.assertFalse(out.exists())
            self.assertFalse(sidecar.exists())

    def test_no_approval_refuses_export(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); self.inputs(root)
            with self.assertRaisesRegex(RuntimeError, 'has not approved'):
                exporter.preflight(root)

    def test_changed_and_additional_sources_invalidate_review(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); design, _ = self.inputs(root, approved=True)
            # Preflight only; these synthetic files must never reach the CAD exporter.
            exporter.preflight(root)
            extra = design / 'power.kicad_sch'; extra.write_text('unreviewed new child sheet')
            with self.assertRaisesRegex(RuntimeError, 'hashes do not match'):
                exporter.preflight(root)
            extra.unlink()
            (design / 'BOM.csv').write_text('changed unreviewed parts')
            with self.assertRaisesRegex(RuntimeError, 'hashes do not match'):
                exporter.preflight(root)

    def test_incomplete_review_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); design, review = self.inputs(root, approved=True)
            review['checks']['full_electrical_review'] = False
            (design / 'design-review.json').write_text(json.dumps(review))
            with self.assertRaisesRegex(RuntimeError, 'Incomplete review checklist'):
                exporter.preflight(root)

    def test_engineering_mode_never_invents_approval(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d); design, _ = self.inputs(root)
            (design / 'design-review.json').unlink()
            _, status, hashes = exporter.preflight(root, engineering=True)
            self.assertIs(status['approved_for_prototype_fabrication'], False)
            self.assertIs(status['independent_review_complete'], False)
            self.assertIsNone(status['reviewed_by'])
            self.assertEqual(status['source_sha256'], hashes)
            with self.assertRaisesRegex(RuntimeError, 'Missing electrical-design'):
                exporter.preflight(root)
            (design / 'BOM.csv').unlink()
            with self.assertRaisesRegex(RuntimeError, 'Missing electrical-design'):
                exporter.preflight(root, engineering=True)

    def test_shared_pad_number_must_not_hide_conflicting_nets(self):
        class Pad:
            def __init__(self, number, net): self.number, self.net = number, net
            def GetNumber(self): return self.number
            def GetNetname(self): return self.net
        class Footprint:
            def __init__(self, pads): self.pads = pads
            def GetReference(self): return 'U_TEST'
            def Pads(self): return self.pads
        class Board:
            def __init__(self, pads): self.pads = pads
            def GetFootprints(self): return [Footprint(self.pads)]
        b = Board([Pad('1', 'GND'), Pad('1', 'GND'), Pad('2', ''), Pad('3', 'unconnected-test')])
        self.assertEqual(exporter.connected_pads(b), {('U_TEST', '1'): 'GND'})
        b.pads.append(Pad('1', '+5V'))
        with self.assertRaisesRegex(RuntimeError, 'Conflicting duplicate'):
            exporter.connected_pads(b)

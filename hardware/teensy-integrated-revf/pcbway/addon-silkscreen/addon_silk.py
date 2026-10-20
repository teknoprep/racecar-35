#!/usr/bin/env python3
"""Rev F silkscreen ADD-ON: lay out every board-level F.SilkS legend in place.

Thin wrapper around ../../electrical/silk_legends.py (the SAME table generate.py
uses), so the routed board and a fresh generation can never disagree:
  * field screw terminals J3 J4 J5 J6 J11 J12 J1 J2 - title just above each body,
    one left-aligned pin label per pad row;
  * Teensy socket orientation (chevron, TEENSY / USB END / THIS EDGE, pins 1 / 25);
  * crossed screen-harness table under J2;
  * every other board text raised to the 0.16 mm legend stroke.

Only F.SilkS graphics are touched - no copper, mask, paste, drill, netlist or
footprint. Idempotent: running it twice gives the same board.

Run:  python3 addon_silk.py [board.kicad_pcb]   (default: racecar-integrated-revf.kicad_pcb in cwd)
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'electrical'))
import pcbnew as k            # noqa: E402
import silk_legends           # noqa: E402

BOARD = sys.argv[1] if len(sys.argv) > 1 else 'racecar-integrated-revf.kicad_pcb'
b = k.LoadBoard(BOARD)
silk_legends.apply(b)
b.Save(BOARD)
print('saved', BOARD)

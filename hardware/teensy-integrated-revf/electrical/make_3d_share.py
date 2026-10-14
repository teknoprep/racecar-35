#!/usr/bin/env python3
"""Build the single-file, offline 3D viewer: ../render/Racecar-RevF-3D.html

One HTML file with everything inside it - three.js, the GLTF loader, the meshopt decoder,
the model (base64 GLB) and the hotspot/part list. Send it to anyone; it opens in a browser
with no server, no CDN and no network.

Needs, on this build host only:
  * the GLB from export3d.py
  * node + a local `three` install (used by esbuild to bundle the viewer)
Hotspot coordinates come from the PCB itself (pcbnew), never hand-typed.
"""
from __future__ import annotations

import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                       # hardware/teensy-integrated-revf
REPO = ROOT.parents[1]
RENDER = ROOT / "render"
GLB = RENDER / "Racecar-RevF-3D.glb"
WEB_GLB = RENDER / "Racecar-RevF-3D-web.glb"
OUT = RENDER / "Racecar-RevF-3D.html"
THREE = Path(os.environ.get("RC3D_THREE", "/home/chris/racecar-tools/threejs-test/node_modules/three"))

# The parts worth a label. `refs` may name several footprints; the label sits at their
# centre. `h` = label height above the PCB top (mm). `key` parts are highlighted.
PARTS = [
    ("U1", ["U1"], "Teensy 4.1 (socketed)", "sensing, lap timing, SD logging — J9 RTC lead", 13),
    ("U17", ["U17"], "ESP32-S3-WROOM-1-N8R2", "planned trunk WiFi + SPI0 link to the Teensy", 4.5),
    ("GPS", ["U8", "J8"], "u-blox NEO-M9N + SMA", "25 Hz GPS; external antenna only", 7),
    ("BT1", ["BT1"], "CR2032 RTC cell", "RTC keep-alive only — no charger", 7),
    ("U21", ["U21", "J14"], "TCAN1042HGV (soldered CAN)", "500 kbit/s MS3Pro; JP2 = bus-end termination", 4),
    ("U2", ["U2", "L2", "D23"], "TPS54560B 5 V / 5 A buck", "car input → 5 V; no Pololu module", 6),
    ("U3", ["U3", "U6"], "2× TRACO TSR 1-2433", "each rail its own 3.3 V switcher", 6),
    ("J1", ["J1"], "J1 POWER", "+10–20 V in / GND", 12),
    ("J2", ["J2"], "J2 SCREEN", "5 V + UART to the CrowPanel dash", 12),
    ("J3", ["J3"], "J3 TACH", "conditioned tachometer only", 12),
    ("J4", ["J4"], "J4 OIL", "+5 V / return / signal", 12),
    ("J5", ["J5"], "J5 COOLANT NTC", "dedicated NTC, 2.49 k excitation", 12),
    ("J6", ["J6"], "J6 AEM AFR", "gauge WHITE + BROWN reference", 12),
    ("J11", ["J11"], "J11 THROTTLE", "0.500-gain analogue input", 12),
    ("J12", ["J12"], "J12 BRAKE", "0.500-gain analogue input", 12),
    ("J13", ["J13"], "J13 Raspberry Pi 5 video UART", "3V3 UART to the video box", 6),
    ("U7", ["U7"], "ICM-42670-P IMU", "I²C 0x68 on the board (firmware pending)", 3.5),
    ("U10", ["U10"], "GT911 touch / screen header", "dash panel interface", 4),
    ("U15", ["U15"], "ADC front end", "protected 0.500-gain sensor inputs", 3),
]

MISSING_NODE_MODULES = (
    f"esbuild needs a local `three` install; expected {THREE}.\n"
    "Install with: cd /tmp && npm i three@0.160.0 (or point THREE at an existing install)."
)


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, check=True, **kw)


def optimise_glb() -> Path:
    """Meshopt-compress the model for the viewer (the raw GLB stays for other tools)."""
    run(["npx", "--yes", "@gltf-transform/cli", "optimize", str(GLB), str(WEB_GLB),
         "--compress", "meshopt", "--simplify", "false"], cwd=RENDER)
    return WEB_GLB


def bundle_viewer() -> str:
    if not (THREE / "package.json").exists():
        raise SystemExit(MISSING_NODE_MODULES)
    with tempfile.TemporaryDirectory() as td:
        work = Path(td)
        shutil.copy(HERE / "viewer" / "viewer.js", work / "viewer.js")
        (work / "node_modules").mkdir()
        (work / "node_modules" / "three").symlink_to(THREE)
        out = work / "viewer.bundle.js"
        run(["npx", "--yes", "esbuild", "viewer.js", "--bundle", "--format=esm",
             "--minify", f"--outfile={out}"], cwd=work)
        return out.read_text()


def board_positions(refs: list[str]) -> dict[str, tuple[float, float]]:
    """Footprint centres in board mm, straight from the PCB (KiCad's own python)."""
    import pcbnew as kicad
    board = kicad.LoadBoard(str(ROOT / "design" / "racecar-integrated-revf.kicad_pcb"))
    want = set(refs)
    out: dict[str, tuple[float, float]] = {}
    for f in board.GetFootprints():
        r = f.GetReference()
        if r in want:
            p = f.GetPosition()
            out[r] = (p.x / 1e6, p.y / 1e6)
    return out


def main() -> int:
    optimise_glb()
    bundle = bundle_viewer()

    refs = [r for _, rs, _, _, _ in PARTS for r in rs]
    pos = board_positions(refs)
    missing = sorted({r for r in refs if r not in pos})
    if missing:
        raise SystemExit(f"no such footprint(s) in the PCB: {missing}")

    parts = []
    for _pid, rs, label, sub, h in PARTS:
        xs = [pos[r][0] for r in rs]
        ys = [pos[r][1] for r in rs]
        parts.append({"key": True, "label": label, "sub": sub,
                      "x": round(sum(xs) / len(xs), 2), "y": round(sum(ys) / len(ys), 2),
                      "z": h})

    data = {
        "glb": base64.b64encode(WEB_GLB.read_bytes()).decode(),
        "parts": parts,
        "meta": {"board": "RACECAR-35 Rev F", "units": "mm", "glb_bytes": WEB_GLB.stat().st_size},
    }
    html = (HERE / "viewer" / "template.html").read_text()
    html = html.replace("/*__DATA__*/null", json.dumps(data, separators=(",", ":")))
    html = html.replace("/*__BUNDLE__*/", bundle)
    OUT.write_text(html)

    print(f"{OUT}  ({OUT.stat().st_size / 1048576:.2f} MB; model {WEB_GLB.stat().st_size / 1024:.0f} KB, "
          f"bundle {len(bundle) / 1024:.0f} KB, {len(parts)} hotspots)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Export SHAREABLE 3D models (glTF/GLB) of the Rev F board.

Presentation only. The routed CAD in `design/` is NEVER modified: this script works on a
copy and asserts the source PCB's SHA-256 is identical before and after.

Two bodies do not exist in KiCad's 3D library and are GENERATED here as plain boxes of the
correct size (they are illustrative, not mechanical models):
  * U1  - the socketed Teensy 4.1 (2 sockets + board + USB + microSD + MCU), built from
          several boxes so the module reads as a Teensy from any angle. No vendor model
          exists for it anywhere in the repo or in kicad-packages3d.
  * U8  - the u-blox NEO-M9N (RF_GPS.3dshapes ships no model at all), and F1, a
          Littelfuse Nano2 fuse holder whose exact library model is absent.

Two library parts get a named near-twin because the exact model is not in kicad-packages3d.

Outputs (all in ../render/):
  Racecar-RevF-3D.kicad_pcb   presentation copy (NOT the CAD source)
  Racecar-RevF-3D.glb         assembled look - the file to share
  Racecar-RevF-3D-copper.glb  same board with copper/tracks/pads revealed
  3D-EXPORT-REPORT.json       source hash + every substitution, for audit
"""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent                       # hardware/teensy-integrated-revf
REPO = ROOT.parents[1]                   # repo root
PCB = ROOT / "design" / "racecar-integrated-revf.kicad_pcb"
OUT = ROOT / "render"
MODELS = OUT / "models"
OUT.mkdir(exist_ok=True)
MODELS.mkdir(exist_ok=True)
COPY = OUT / "Racecar-RevF-3D.kicad_pcb"
GLB = OUT / "Racecar-RevF-3D.glb"
GLB_COPPER = OUT / "Racecar-RevF-3D-copper.glb"
REPORT = OUT / "3D-EXPORT-REPORT.json"
KICAD3D = Path("/usr/share/kicad/3dmodels")

# Exact models missing from kicad-packages3d -> the nearest named library twin.
SUBSTITUTIONS = {
    "Connector_Coaxial.3dshapes/SMA_Amphenol_901-143_Horizontal":
        "Connector_Coaxial.3dshapes/SMA_Amphenol_132289_EdgeMount",
    "Package_SO.3dshapes/SOIC-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.3mm":
        "Package_SO.3dshapes/HSOP-8-1EP_3.9x4.9mm_P1.27mm_EP2.41x3.1mm",
    # u-blox NEO / Nano2 holder: generated boxes, see generated_models()
    "RF_GPS.3dshapes/ublox_NEO": "@GEN/gps_neo",
    "Fuse.3dshapes/Fuse_Littelfuse-NANO2-451_453": "@GEN/fuse_nano2",
}

# name -> (size_x, size_y, size_z, offset_x, offset_y, offset_z) in mm, footprint-local.
# Offset is the box CENTRE relative to the footprint origin; z=0 is the PCB top surface.
GENERATED: dict[str, tuple[float, float, float, float, float, float, str]] = {
    # name: (size_x, size_y, size_z, centre_x, centre_y, z_base, colour)
    # u-blox NEO-M9N: 16.0 x 12.2 x 2.4 mm, origin at the module centre
    "gps_neo": (16.0, 12.2, 2.4, 0.0, 0.0, 0.0, "#26262a"),
    # Littelfuse Nano2 451/453 holder + cap, ~10.3 x 6.0 x 6.0 mm
    "fuse_nano2": (10.3, 6.0, 6.0, 0.0, 0.0, 0.0, "#3a3a3c"),
}

# Teensy 4.1 in two 1x24 sockets. Footprint origin = pin 1; pads run x=0/15.24, y=0..58.42.
# Sizes are (x, y, z) mm; the LAST TWO values are the box CENTRE in FOOTPRINT coordinates
# (x right, y down) and z measured from the PCB top surface. model_line() converts to
# KiCad's 3D model space, which negates Y.
SOCKET_H = 8.51
TEENSY: list[tuple[str, float, float, float, float, float, float]] = [
    # kind,        sx,    sy,    sz,   cx,    cy,     z0     (footprint-local mm)
    ("socket",    2.54, 60.96, SOCKET_H,   0.00, 29.21, 0.00),
    ("socket",    2.54, 60.96, SOCKET_H,  15.24, 29.21, 0.00),
    ("board",    17.78, 60.96, 1.60,       7.62, 29.21, SOCKET_H),
    ("mcu",      12.00, 12.00, 1.20,       7.62, 32.00, SOCKET_H + 1.60),
    ("flash",     5.00,  5.00, 0.80,       7.62, 20.00, SOCKET_H + 1.60),
    ("usb",       7.70,  6.00, 3.20,       7.62, -4.30, SOCKET_H + 1.60),
    ("sd",       15.50, 15.00, 2.20,       7.62, 52.20, SOCKET_H + 1.60),
    # the 0.7" pin fields, as thin gold bars on top of the board
    ("pins",      1.60, 58.42, 0.60,       0.00, 29.21, SOCKET_H + 1.60),
    ("pins",      1.60, 58.42, 0.60,      15.24, 29.21, SOCKET_H + 1.60),
]
TEENSY_COLOURS = {
    # the body colour comes from the generated board's SOLDERMASK colour, which KiCad's
    # STEP writer exports per body - so a "purple Teensy in black sockets" is just a mask
    # colour on each generated box. Verified in the raytraced render, not assumed.
    "socket": "#0d0d0f", "board": "#4a2a72", "mcu": "#141416",
    "flash": "#141416", "usb": "#b9bcc0", "sd": "#c8cacd", "pins": "#b08a2e",
}


def step_box(name: str, size: tuple[float, float, float], colour: str = "#0b703d") -> Path:
    """Generate (once) a STEP box of the given mm size, centred on X/Y, z from 0."""
    tag = hashlib.sha256(f"{size}{colour}".encode()).hexdigest()[:8]
    dest = MODELS / f"{name}-{tag}.step"
    if dest.exists():
        return dest
    sx, sy, sz = size
    tmp = MODELS / f"_build_{name}.kicad_pcb"
    tmp.write_text(f"""(kicad_pcb (version 20240108) (generator export3d)
  (general (thickness {sz}))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (36 "B.SilkS" user) (37 "F.SilkS" user) (44 "Edge.Cuts" user))
  (setup (stackup
    (layer "F.SilkS" (type "Top Silk Screen"))
    (layer "F.Paste" (type "Top Solder Paste"))
    (layer "F.Mask" (type "Top Solder Mask") (color "{colour}"))
    (layer "F.Cu" (type "copper") (thickness 0.035))
    (layer "dielectric 1" (type "core") (thickness {max(sz - 0.07, 0.01):.3f}) (material "FR4") (epsilon_r 4.5) (loss_tangent 0.02))
    (layer "B.Cu" (type "copper") (thickness 0.035))
    (layer "B.Mask" (type "Bottom Solder Mask") (color "{colour}"))
    (layer "B.SilkS" (type "Bottom Silk Screen"))
    (copper_finish "None") (dielectric_constraints no)))
  (gr_rect (start {-sx / 2:.4f} {-sy / 2:.4f}) (end {sx / 2:.4f} {sy / 2:.4f})
    (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts"))
)
""")
    proc = subprocess.run(["kicad-cli", "pcb", "export", "step", "-o", str(dest), "--force",
                           "--subst-models", str(tmp)], capture_output=True, text=True)
    tmp.unlink(missing_ok=True)
    if proc.returncode != 0 or not dest.exists():
        raise SystemExit(f"step_box({name}) failed:\n{proc.stdout}\n{proc.stderr}")
    return dest


def teensy_models() -> list[tuple[Path, tuple[float, float, float]]]:
    out = []
    for i, (kind, sx, sy, sz, cx, cy, z0) in enumerate(TEENSY):
        p = step_box(f"teensy41_{i:02d}_{kind}", (sx, sy, sz), TEENSY_COLOURS[kind])
        out.append((p, (cx, cy, z0)))
    return out


def model_line(path: Path, offset=(0.0, 0.0, 0.0)) -> str:
    """A `(model ...)` block. `offset` is in FOOTPRINT coordinates (x right, y DOWN, z up
    from the PCB top): KiCad's 3D model space negates Y, so it is flipped here - the 3D
    renderer otherwise puts a part at +y where the footprint has -y."""
    ox, oy, oz = offset
    return (f'\t\t(model "{path}"\n'
            f'\t\t\t(offset (xyz {ox:.4f} {-oy:.4f} {oz:.4f}))\n'
            f'\t\t\t(scale (xyz 1 1 1))\n'
            f'\t\t\t(rotate (xyz 0 0 0))\n'
            f'\t\t)')


def main() -> int:
    sha_before = hashlib.sha256(PCB.read_bytes()).hexdigest()
    text = PCB.read_text()
    notes: list[dict] = []

    # 1. point every ${KICADn_3DMODEL_DIR} reference at the installed library
    # A whole `(model "..." (offset..) (scale..) (rotate..))` block, multi-line.
    block_re = re.compile(r'\t\t\(model "([^"\n]+)"(\n(?:\t\t\t[^\n]*\n)*\t\t\))')

    def lib_repl(m: re.Match[str]) -> str:
        path, mid = m.group(1), m.group(2)
        rel = path.split("}/", 1)[1]           # e.g. '.../ublox_NEO.step'
        key = rel[:-5] if rel.endswith(".step") else rel
        target = SUBSTITUTIONS.get(key, key)
        if target.startswith("@GEN/"):
            name = target.split("/", 1)[1]
            sx, sy, sz, cx, cy, cz, colour = GENERATED[name]
            p = step_box(f"gen_{name}", (sx, sy, sz), colour)
            notes.append({"ref": rel, "model": str(p.relative_to(REPO)), "kind": "generated-box"})
            return model_line(p, (cx, cy, cz))
        if target != key:
            notes.append({"ref": rel, "model": target, "kind": "substitute"})
        p = KICAD3D / (target + ".step")
        if not p.exists():
            notes.append({"ref": rel, "model": None, "kind": "MISSING"})
            return m.group(0)
        if target == key:
            notes.append({"ref": rel, "model": target, "kind": "library"})
        return f'\t\t(model "{p}"{mid}'

    text = block_re.sub(lib_repl, text)

    # 2. hang the generated Teensy body (and only it) on U1, which has no model at all
    parts = text.split("\n\t(footprint ")
    for i, part in enumerate(parts):
        if '(property "Reference" "U1"' in part and '(model "' not in part:
            body = "\n".join(model_line(p, off) for p, off in teensy_models())
            parts[i] = part.replace("(attr through_hole)", "(attr through_hole)\n" + body, 1)
            notes.append({"ref": "U1", "model": "generated Teensy 4.1 in 2x 1x24 sockets",
                          "kind": "generated-assembly"})
            break
    text = "\n\t(footprint ".join(parts)

    COPY.write_text(text)
    assert hashlib.sha256(PCB.read_bytes()).hexdigest() == sha_before, \
        "export3d.py modified the CAD source - refusing to continue"

    # Cheap structural validation before the slow export: the copy must load.
    sys.path.insert(0, "/usr/lib/python3/dist-packages")
    import pcbnew as kicad  # noqa: E402  (KiCad's own python, available on this host)
    board = kicad.LoadBoard(str(COPY))
    fps = list(board.GetFootprints())
    n_models = sum(len(f.Models()) for f in fps)
    assert len(fps) == 193, f"copy has {len(fps)} footprints"
    print(f"copy loads: {len(fps)} footprints, {n_models} 3D models")

    def export(dest: Path, extra: list[str]) -> int:
        cmd = ["kicad-cli", "pcb", "export", "glb", "-o", str(dest), "--force",
               "--include-silkscreen", "--include-soldermask", *extra, str(COPY)]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        (OUT / f"kicad-cli-glb-{dest.stem}.log").write_text(proc.stdout + proc.stderr)
        failed = [ln for ln in proc.stdout.splitlines() if "Could not add" in ln or "Cannot load" in ln]
        print(f"{dest.name}: rc={proc.returncode} "
              f"{dest.stat().st_size / 1048576:.2f} MB model-errors={len(failed)}")
        return proc.returncode

    rc = export(GLB, [])                                    # assembled look
    rc |= export(GLB_COPPER, ["--include-zones", "--include-tracks", "--include-pads",
                              "--include-inner-copper"])    # engineering look

    REPORT.write_text(json.dumps({
        "pcb": str(PCB.relative_to(REPO)),
        "pcb_sha256": sha_before,
        "pcb_sha256_after": hashlib.sha256(PCB.read_bytes()).hexdigest(),
        "presentation_copy": str(COPY.relative_to(REPO)),
        "glb": str(GLB.relative_to(REPO)), "glb_bytes": GLB.stat().st_size,
        "glb_copper": str(GLB_COPPER.relative_to(REPO)), "glb_copper_bytes": GLB_COPPER.stat().st_size,
        "kicad_cli": subprocess.run(["kicad-cli", "version"], capture_output=True,
                                    text=True).stdout.strip(),
        "model_substitutions": notes,
        "missing_models": [n for n in notes if n["kind"] == "MISSING"],
        "caveat": ("Bodies are the KiCad library STEP models plus GENERATED boxes for the "
                   "socketed Teensy 4.1 (no vendor model exists), the u-blox NEO-M9N and the "
                   "Nano2 fuse holder. Illustrative, not a mechanical/fit/thermal model, and "
                   "not evidence that the board works. Unbuilt engineering prototype."),
    }, indent=2) + "\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())

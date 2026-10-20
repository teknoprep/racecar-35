#!/usr/bin/env python3
"""Rebuild the Rev F 3D STILLS from the current 3D export, so they can never lag the model.

Run AFTER export3d.py (presentation copy + GLB) and make_3d_share.py (viewer):

  render/check/top.png, render/check/angle.png  kicad-cli raytraces of the presentation copy
                                                (render/Racecar-RevF-3D.kicad_pcb)
  Racecar-RevF-3D-preview.png                   screenshot of the single-file viewer, home view,
                                                labels on, auto-rotate off

Needs kicad-cli, Pillow and - for the preview only - Playwright with its Chromium:

  /home/chris/racecar-tools/servervenv/bin/python3 electrical/render_stills.py [--no-preview]

Works only on generated presentation files; the CAD in design/ is never opened for writing.
"""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

from PIL import Image

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
RENDER = ROOT / "render"
COPY = RENDER / "Racecar-RevF-3D.kicad_pcb"
HTML = RENDER / "Racecar-RevF-3D.html"
CHECK = RENDER / "check"
PREVIEW = ROOT / "Racecar-RevF-3D-preview.png"

# The TOP view exists to read the silkscreen, so it is flat-lit ("basic"): the raytracer's
# soft shadows darken the band between two tall terminal blocks, which is exactly where the
# terminal names sit. The angled view is the presentation shot, raytraced ("high").
VIEWS = [("top", ["--side", "top"], "basic"), ("angle", ["--rotate", "330,0,17"], "high")]


def raytrace() -> None:
    CHECK.mkdir(exist_ok=True)
    for name, args, quality in VIEWS:
        with tempfile.TemporaryDirectory() as t:
            raw = Path(t) / f"{name}.png"
            proc = subprocess.run(
                ["kicad-cli", "pcb", "render", "--width", "2200", "--height", "1600",
                 "--zoom", "0.85", "--quality", quality, "--background", "transparent",
                 *args, "-o", str(raw), str(COPY)], capture_output=True, text=True)
            if proc.returncode != 0 or not raw.exists():
                raise SystemExit(f"kicad-cli render {name} failed:\n{proc.stdout}\n{proc.stderr}")
            im = Image.open(raw).convert("RGBA")
            im = im.crop(im.getbbox())
            im.save(CHECK / f"{name}.png")
            print(f"{CHECK / f'{name}.png'}  {im.size[0]}x{im.size[1]}")


def screenshot() -> None:
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(args=["--use-gl=angle", "--use-angle=swiftshader",
                                          "--enable-unsafe-swiftshader"])
        page = browser.new_page(viewport={"width": 2000, "height": 1200},
                                reduced_motion="reduce")   # no intro sweep
        page.goto(HTML.as_uri())
        page.wait_for_function("window.__rc3d !== undefined", timeout=120_000)
        page.wait_for_function("getComputedStyle(document.getElementById('loading')).display === 'none'")
        page.wait_for_timeout(500)
        # stop auto-rotate through the viewer's own controls (a click can land before the
        # viewer has switched it on, and then turns it ON); keep the button in step
        page.evaluate("() => { window.__rc3d.controls.autoRotate = false;"
                      " document.getElementById('b-spin').classList.remove('on'); }")
        page.click("#b-reset")                              # framed home view
        page.wait_for_timeout(2500)                         # let the camera settle
        page.screenshot(path=str(PREVIEW))
        browser.close()
    print(f"{PREVIEW}  2000x1200")


if __name__ == "__main__":
    if not COPY.exists() or not HTML.exists():
        raise SystemExit("run electrical/export3d.py and electrical/make_3d_share.py first")
    raytrace()
    if "--no-preview" not in sys.argv:
        screenshot()

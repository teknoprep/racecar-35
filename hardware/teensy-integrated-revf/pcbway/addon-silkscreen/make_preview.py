#!/usr/bin/env python3
"""Rebuild Racecar-RevF-SILKSCREEN-ADDON-preview.png from the CURRENT board.

Renders F.Silkscreen (+ a faint F.Mask so pads are visible) from a throwaway copy
of ../../design with addon_silk.py applied, then composes the whole-board view
and the three detail insets.

Needs: kicad-cli (9.x), python3-cairosvg, python3-pil, python3-pcbnew.

  python3 make_preview.py [--board <prepared add-on kicad_pcb>]

With no --board the copy is made and addon_silk.py is applied here, so the PNG
always shows what the shipped gerber actually contains.
"""
import argparse
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DESIGN = HERE.parents[1] / 'design'
NAME = 'racecar-integrated-revf'
OUT = HERE / 'Racecar-RevF-SILKSCREEN-ADDON-preview.png'

# board-coordinate windows for the detail tiles: x0, x1, y0, y1, caption
INSETS = [
    (22.0, 49.0, 26.0, 89.0, 'J3-J6: NAME ABOVE, ONE PIN COLUMN'),
    (22.0, 49.0, 88.0, 171.0, 'J11-J2 + CROSSED SCREEN HARNESS'),
    (57.0, 79.0, 21.5, 39.5, 'TEENSY ORIENTATION'),
    (146.0, 170.0, 120.0, 142.0, 'J13 PI 5 VIDEO UART'),
]
TILE_PPM = 15          # px per mm in the detail tiles


def stage(temp: Path) -> Path:
    for f in ('racecar-integrated-revf.kicad_pcb', 'racecar-integrated-revf.kicad_pro',
              'fp-lib-table'):
        shutil.copy(DESIGN / f, temp / f)
    shutil.copytree(DESIGN / 'RacecarC.pretty', temp / 'RacecarC.pretty')
    subprocess.run([sys.executable, str(HERE / 'addon_silk.py')], cwd=temp, check=True)
    return temp / (NAME + '.kicad_pcb')


def render(board: Path, png: Path, width: int):
    """Plot the silk/mask/outline layers; return (png, origin_x, origin_y, mm_per_px)."""
    import cairosvg
    import pcbnew as k

    svg = png.with_suffix('.svg')
    subprocess.run(['kicad-cli', 'pcb', 'export', 'svg',
                    '--layers', 'F.Silkscreen,F.Mask,Edge.Cuts',
                    '--page-size-mode', '2', '--exclude-drawing-sheet',
                    '-o', str(svg), str(board)], check=True, capture_output=True)
    vb = [float(v) for v in re.search(r'viewBox="([^"]+)"', svg.read_text()).group(1).split()]
    cairosvg.svg2png(url=str(svg), write_to=str(png), output_width=width,
                     background_color='#0d0d0f')
    # KiCad plots the board's full bounding box (footprint courtyards included), so the
    # plot origin is the board bbox minimum - NOT the outline corner.
    r = k.LoadBoard(str(board)).GetBoundingBox()
    ox, oy = k.ToMM(r.GetLeft()), k.ToMM(r.GetTop())
    mmpx = vb[2] / width
    return png, ox, oy, mmpx


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument('--board', type=Path, help='already-prepared add-on board')
    a = ap.parse_args()
    from PIL import Image, ImageDraw, ImageFont

    temp = None
    if a.board:
        board = a.board
    else:
        temp = Path(tempfile.mkdtemp(prefix='silkprev-'))
        board = stage(temp)
    pngdir = Path(tempfile.mkdtemp(prefix='silkpng-'))
    try:
        png, ox, oy, mmpx = render(board, pngdir / 'board.png', 3200)
        im = Image.open(png).convert('RGB')

        def crop(x0, x1, y0, y1):
            return im.crop((int((x0 - ox) / mmpx), int((y0 - oy) / mmpx),
                            int((x1 - ox) / mmpx), int((y1 - oy) / mmpx)))

        BOARD_W = 900
        board_img = im.resize((BOARD_W, int(im.height * BOARD_W / im.width)), Image.LANCZOS)
        board_scale = board_img.width / (im.width * mmpx)   # px per mm in the board view
        PAD, CAP = 26, 30
        tiles = []
        for x0, x1, y0, y1, cap in INSETS:
            pic = crop(x0, x1, y0, y1)
            k2 = TILE_PPM * mmpx
            tiles.append((pic.resize((int(pic.width * k2), int(pic.height * k2)), Image.LANCZOS), cap))
        (t1, c1), (t2, c2), (t3, c3), (t4, c4) = tiles
        right_x = PAD + BOARD_W + PAD
        col2_x = right_x + t1.width + PAD
        W = col2_x + max(t2.width, t3.width) + PAD
        H = 96 + max(board_img.height + CAP + 10 + max(t3.height, t4.height),
                     CAP + t1.height, CAP + t2.height) + PAD
        canvas = Image.new('RGB', (W, H), '#0d0d0f')
        d = ImageDraw.Draw(canvas)
        f_big = ImageFont.load_default(size=32)
        f_mid = ImageFont.load_default(size=17)
        f_small = ImageFont.load_default(size=18)
        d.text((PAD, 18), 'Racecar-35 Rev F - SILKSCREEN ADD-ON (F.Silkscreen only)', font=f_big, fill='white')
        d.text((PAD, 58), 'Copper, mask, paste, outline and both drills are geometry-identical to the '
                          'uploaded package (verify_addon.py).', font=f_small, fill='#b9b9c2')
        canvas.paste(board_img, (PAD, 96))

        def box(x0, x1, y0, y1, col='#e02020', width=3):
            d.rectangle([PAD + (x0 - ox) * board_scale, 96 + (y0 - oy) * board_scale,
                         PAD + (x1 - ox) * board_scale, 96 + (y1 - oy) * board_scale],
                        outline=col, width=width)

        for x0, x1, y0, y1, _ in INSETS:
            box(x0, x1, y0, y1)

        def tile(img, cap, x, y):
            d.text((x, y), cap, font=f_mid, fill='white')
            canvas.paste(img, (x, y + CAP))
            d.rectangle([x, y + CAP, x + img.width - 1, y + CAP + img.height - 1], outline='#e02020', width=2)

        tile(t1, c1, right_x, 96)
        tile(t2, c2, col2_x, 96)
        tile(t3, c3, PAD, 96 + board_img.height + 10)
        tile(t4, c4, PAD + t3.width + PAD, 96 + board_img.height + 10)
        canvas.save(OUT)
        print('wrote', OUT, canvas.size)
    finally:
        if temp:
            shutil.rmtree(temp, ignore_errors=True)
        shutil.rmtree(pngdir, ignore_errors=True)


if __name__ == '__main__':
    main()

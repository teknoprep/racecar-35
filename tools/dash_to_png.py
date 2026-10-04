#!/usr/bin/env python3
"""Render the 800x480 race dash to PNG (Pillow only, no Tk/X).

Port of tools/dash_view.py DashView.redraw with a fixed, reproducible state.
Usage:  python3 dash_to_png.py [outdir]      (default outdir /tmp)
Writes dash-800.png (1x) and dash-1600.png (2x).
"""
import os
import sys
from collections import Counter

from PIL import Image, ImageDraw, ImageFont

W, H = 800, 480
OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
os.makedirs(OUT_DIR, exist_ok=True)

BOLD_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono-Bold.ttf",
]
REG_PATHS = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/dejavu/DejaVuSansMono.ttf",
]

# name: (pixel size, bold)  -- sizes are the Tk pixel sizes from dash_view.py
FONT_SPECS = {
    "speed": (112, True), "rpm": (46, True), "lbl": (15, False),
    "row4": (26, True), "row2": (16, True), "row0": (9, True),
    "tiny": (15, False), "mid": (20, True), "big": (28, True),
}

STATE = dict(
    speed_mph=87.0, rpm=5240, rpm_max=7000, rpm_bar_color="#00c000",
    delta_ms=-412, pred_ms=62450, last_ms=62870, best_ms=62110,
    lap_n=4, gps_text="GPS 3D", sess_time="12:41", clock="12:41",
    recording=True, track_name="TRACK 1",
    temp=196.0, oil=52.0, volt=13.8, afr=13.2, iat=104.0,
    warn=None, flashing=False,
)

THRESH = {"OIL": (15.0, None), "VOLT": (12.8, None), "AFR": (12.0, 15.5),
          "TEMP": (None, 220.0), "IAT": (None, 200.0)}
ITEMS = ["TEMP", "OIL", "VOLT", "AFR", "IAT", "MAP", "TPS", "RPM"]
MODES = {"TEMP": "always", "OIL": "always", "VOLT": "always", "AFR": "always",
         "IAT": "always", "MAP": "hidden", "TPS": "hidden", "RPM": "hidden"}


def first_existing(paths):
    for p in paths:
        if os.path.isfile(p):
            return p
    return None


def load_fonts(s):
    bold = first_existing(BOLD_PATHS)
    reg = first_existing(REG_PATHS)
    if not bold and not reg:
        sys.exit("dash_to_png: no DejaVu Sans Mono font found (looked in: %s)"
                 % ", ".join(BOLD_PATHS + REG_PATHS))
    bold = bold or reg
    reg = reg or bold
    return {name: ImageFont.truetype(bold if b else reg, size=round(px * s))
            for name, (px, b) in FONT_SPECS.items()}


def _lap(ms):
    if not ms:
        return "--"
    m = int(ms // 60000)
    return f"{m}:{(ms % 60000) / 1000:06.3f}" if m else f"{ms / 1000:.3f}"


def row_text(item, val):
    if val is None:
        return f"{item}: ---"
    if item == "VOLT":
        return f"{item}: {int(val) // 1}.{int(round(val * 10)) % 10}"
    if item == "AFR":
        return f"{item}: {int(val)}.{int(round(val * 10)) % 10}"
    if item in ("TEMP", "IAT"):
        return f"{item}: {int(round(val))}\u00b0F"
    if item == "OIL":
        return f"{item}: {int(round(val))} psi"
    if item == "MAP":
        return f"{item}: {int(round(val))} kPa"
    if item == "TPS":
        return f"{item}: {int(round(val))}%"
    return f"{item}: {int(val)}"


def is_warning(item, val):
    if val is None or item == "RPM":
        return False
    lo, hi = THRESH.get(item, (None, None))
    if lo is not None and val < lo:
        return True
    if hi is not None and val > hi:
        return True
    return False


def render(s, out_path):
    st = STATE
    fonts = load_fonts(s)
    img = Image.new("RGB", (round(W * s), round(H * s)), "#000000")
    d = ImageDraw.Draw(img)
    lw = max(1, round(s))

    def X(v):
        return round(v * s)

    def rect(x0, y0, x1, y1, fill=None, outline=None):
        ex = 1 if outline is None else 0          # Tk fills end exclusive
        ax0, ay0, ax1, ay1 = X(x0), X(y0), X(x1) - ex, X(y1) - ex
        if ax1 < ax0 or ay1 < ay0:
            return
        d.rectangle([ax0, ay0, ax1, ay1], fill=fill, outline=outline,
                    width=lw if outline else 0)

    def text(x, y, t, font, fill, anchor="center"):
        a = {"w": "lm", "center": "mm", "rm": "rm", "r": "rm"}[anchor]
        d.text((x * s, y * s), t, font=fonts[font], fill=fill, anchor=a)

    # 1) flash band (black unless a shift flash is lit)
    rect(0, 0, 800, 130, fill="#000000")

    # 2) RPM bar
    rpm, rpm_max = st["rpm"], st["rpm_max"]
    frac = min(1.0, rpm / rpm_max)
    rect(20, 10, 780, 90, fill="#101010", outline="#303030")
    rect(20, 10, 20 + 760 * frac, 90, fill=st["rpm_bar_color"])
    text((20 + 780) / 2, (10 + 90) / 2, f"{rpm:.0f}", "rpm", "#ffffff")

    # 3) delta bar
    rect(22, 113, 778, 125, fill="#101010", outline="#202020")
    cx = 22 + 756 / 2
    d.line([(X(cx), X(113)), (X(cx), X(125))], fill="#ffffff", width=lw)
    dm = st["delta_ms"]
    if dm is not None and abs(dm) > 100.0:
        px = min(1.0, abs(dm) / 2000.0) * 378
        if dm < 0:
            rect(cx - px, 114, cx, 124, fill="#00c000")
        else:
            rect(cx, 114, cx + px, 124, fill="#ff0000")

    # 4) speed
    spd = st["speed_mph"]
    text(600, 230, f"{spd:.0f}" if spd >= 100 else f"{spd:.1f}", "speed", "#ffffff")

    # 5) middle column PRED / LAP / BEST
    pred, last, best = st["pred_ms"], st["last_ms"], st["best_ms"]
    pcol = "#00ff00" if (pred is not None and last is not None and pred < last) else "#ff8080"
    for y, lab, val, col in ((378, "PRED", _lap(pred), pcol),
                             (406, "LAP", _lap(last), "#ffffff"),
                             (434, "BEST", _lap(best), "#00ff00")):
        text(264, y, lab, "lbl", "#7f93a8", "w")
        text(325, y, val, "row4", col, "w")

    # 6) right column
    text(610, 346, "LAP " + (str(st["lap_n"]) if st["lap_n"] else "--"), "mid", "#ffff00", "w")
    text(610, 378, st["gps_text"] or "GPS --", "mid", "#00ff00", "w")
    text(610, 410, "TIME " + (st["sess_time"] or "--"), "mid", "#ffffff", "w")

    # 7) sensor monitor block
    rect(0, 340, 260, 480, fill="#000000", outline="#181818")
    vals = {"TEMP": st["temp"], "OIL": st["oil"], "VOLT": st["volt"],
            "AFR": st["afr"], "IAT": st["iat"], "MAP": None, "TPS": None,
            "RPM": st["rpm"]}
    rows = []
    for item in ITEMS:
        if MODES[item] == "hidden":
            continue
        if item == "VOLT" and rpm < 500:
            continue
        rows.append(item)
    n = len(rows)
    if n:
        pitch = min(34, 140 // n)
        fname = "row4" if pitch >= 30 else ("row2" if pitch >= 19 else "row0")
        for k, item in enumerate(rows):
            ry = 340 + (140 - (n - k) * pitch)
            col = "#ff4040" if is_warning(item, vals[item]) else "#ffffff"
            text(20, ry + pitch / 2, row_text(item, vals[item]), fname, col, "w")

    # 8) START/STOP + TRACK buttons, REC indicator
    if st["recording"]:
        rect(30, 155, 190, 225, fill="#ff0000", outline="#ffffff")
        text(110, 190, "STOP", "mid", "#ffffff")
    else:
        rect(30, 155, 190, 225, fill="#00a000", outline="#ffffff")
        text(110, 190, "START", "mid", "#ffffff")
    rect(30, 235, 190, 305, fill="#1e3f5b", outline="#ffffff")
    text(110, 270, st["track_name"] or "TRACK", "row2", "#ffffff")
    if st["recording"]:
        text(200, 140, "REC", "row4", "#ff0000", "w")

    # 9) wall clock -- right-anchored so it cannot run off the 800px panel
    clk = st["clock"]
    text(792, 448, clk or "--:--", "tiny", "#888888" if clk else "#444444", "rm")

    img.save(out_path, "PNG")
    return img


def histogram(img, top=8):
    counts = Counter(img.getdata())
    total = img.width * img.height
    return "%d unique colours; top: %s" % (
        len(counts),
        ", ".join("#%02x%02x%02x %.2f%%" % (*rgb, 100.0 * c / total)
                  for rgb, c in counts.most_common(top)))


def main():
    for s, name in ((1.0, "dash-800.png"), (2.0, "dash-1600.png")):
        path = os.path.join(OUT_DIR, name)
        img = render(s, path)
        print(f"{path}: {img.width}x{img.height}")
        print("  " + histogram(img))


if __name__ == "__main__":
    main()

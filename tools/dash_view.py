#!/usr/bin/env python3
"""Dash clone — renders the CrowPanel dash page from live data, so you can SEE what the
screen should look like without walking to the car.

Layout constants are lifted from the real firmware (crowpanel-arduino/RaceDash/RaceDash.ino):
RPM bar 20,10 760x80; flash band y<130; delta bar x22 y113 756x12, +-2.0 s full / +-0.1 s dead;
speed sprite 360x200 (Font7 size 4) centred on SPEED_CX at y130..330; middle column labels at
x264 y378/406/434 (PRED/LAP/BEST); LAP-number row at 610,346; wall clock at 776,448; the sensor
monitor block at 0,340 260x140 with bottom-anchored rows, pitch = min(34, 140/n) and font steps
at >=30 / >=19; PALETTE = RED ORANGE YELLOW GREEN CYAN BLUE MAGENTA WHITE; row text formats are
monRowText() from the firmware ("TEMP: 220F", "OIL: 25 psi", "VOLT: 12.8", "AFR: 14.7",
"IAT: 120F", "MAP: 101 kPa", "TPS: 12%", "RPM: 3200"); RPM never warns (shift alerts own it).

Draws on a Tk canvas: no firmware, no LovyanGFX, no dependencies.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import font as tkfont

PANEL_W, PANEL_H = 800, 480

# firmware PALETTE order -> Tk colours
PALETTE = ["#ff0000", "#ffa500", "#ffff00", "#00ff00", "#00ffff", "#0000ff", "#ff00ff", "#ffffff"]
PALETTE_NAMES = ["RED", "ORANGE", "YELLOW", "GREEN", "CYAN", "BLUE", "MAGENTA", "WHITE"]

# monitor items, in the firmware's enum order (MON_TEMP .. MON_RPM)
MON_ITEMS = ["TEMP", "OIL", "VOLT", "AFR", "IAT", "MAP", "TPS", "RPM"]
MON_ALWAYS, MON_WARN_ONLY, MON_OFF = 0, 1, 2
MON_MODE_NAMES = ["ALWAYS", "WARN ONLY", "HIDDEN"]


class MonCfg:
    """Mirror of the firmware's MonCfg: order + mode + warn lo/hi + colour, per item.

    Thresholds are in the item's natural unit (F, psi, V, AFR, kPa, %, rpm); None = disabled,
    which is the firmware's MON_WARN_OFF. RPM has no thresholds by design (v0.1.160).
    """

    def __init__(self):
        self.order = list(MON_ITEMS)
        self.mode = {i: MON_ALWAYS for i in MON_ITEMS}
        self.lo = {"TEMP": None, "OIL": 15.0, "VOLT": 12.8, "AFR": 12.0, "IAT": None,
                   "MAP": None, "TPS": None, "RPM": None}
        self.hi = {"TEMP": 220.0, "OIL": None, "VOLT": None, "AFR": 15.5, "IAT": 200.0,
                   "MAP": None, "TPS": None, "RPM": None}
        self.col = {i: 0 for i in MON_ITEMS}          # PALETTE index (0 = RED)
        self.mode["MAP"] = MON_OFF
        self.mode["TPS"] = MON_OFF
        self.mode["RPM"] = MON_OFF

    def item_warns(self, item, val):
        """Firmware rules: RPM never warns; VOLT only while running (checked in the view)."""
        if item == "RPM" or val is None:
            return False
        if self.lo.get(item) is not None and val <= self.lo[item]:
            return True
        if self.hi.get(item) is not None and val >= self.hi[item]:
            return True
        return False


class DashView:
    """Tk canvas that draws the dash page. Call set_state() then let the 25 Hz tick repaint."""

    def __init__(self, master, scale=1.0, cfg=None):
        self.scale = scale
        self.cfg = cfg or MonCfg()
        self.cv = tk.Canvas(master, width=int(PANEL_W * scale), height=int(PANEL_H * scale),
                            bg="#000000", highlightthickness=0)
        self.cv.pack(fill="both", expand=True)
        f = lambda px, bold=False: ("DejaVu Sans Mono", -max(6, int(px * scale)),
                                    "bold" if bold else "normal")
        self.f = {
            "speed": f(112, True), "rpm": f(46, True), "lbl": f(15), "row4": f(26, True),
            "row2": f(16, True), "row0": f(9, True), "tiny": f(15), "mid": f(20, True),
            "big": f(28, True),
        }
        self.st = {}
        self.blink = True
        self._apply_fonts()

    def _apply_fonts(self):
        self.f_px = {k: tkfont.Font(family=v[0], size=v[1], weight=v[2]) for k, v in self.f.items()}

    # ---------------- data ----------------
    def set_state(self, **kw):
        self.st.update(kw)

    # ---------------- layout (mirrors the firmware) ----------------
    def visible_rows(self, n=None):
        """[(item, text, colour)] for the rows the block should show right now."""
        out = []
        for item in self.cfg.order:
            mode = self.cfg.mode.get(item, MON_ALWAYS)
            if mode == MON_OFF:
                continue
            val = self.st.get(item.lower() if item != "RPM" else "rpm")
            if item == "VOLT" and not (self.st.get("rpm") or 0) >= 500:
                continue                       # firmware: only displayed while running
            text = self.row_text(item, val)
            warn = self.cfg.item_warns(item, val) and not (
                item == "VOLT" and (self.st.get("rpm") or 0) < 500)
            if mode == MON_WARN_ONLY and not warn:
                continue
            col = PALETTE[self.cfg.col.get(item, 0)] if warn else "#ffffff"
            if val is None:
                col = "#555555"
            out.append((item, text, col, warn))
        return out

    def row_text(self, item, val):
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

    def pitch(self, n):
        return min(34, 140 // n) if n else 34

    # ---------------- drawing ----------------
    def redraw(self, blink_on=True):
        s = self.scale
        cv = self.cv
        cv.delete("all")
        st = self.st
        warn = st.get("warn")

        # the flash band (y<130) is repainted in the alert colour during a shift flash
        band = st.get("flash_color") if st.get("flashing") and blink_on else None
        cv.create_rectangle(0, 0, 800 * s, 130 * s,
                            fill=band or "#000000", outline="")

        # RPM bar: gradient-ish fill + empty remainder
        rpm = st.get("rpm") or 0
        rpm_max = max(1, st.get("rpm_max") or 7000)
        frac = min(1.0, rpm / rpm_max)
        cv.create_rectangle(20 * s, 10 * s, (20 + 760) * s, (10 + 80) * s,
                            outline="#303030", fill="#101010")
        if frac > 0:
            cv.create_rectangle(20 * s, 10 * s, (20 + 760 * frac) * s, (10 + 80) * s,
                                outline="", fill=st.get("rpm_bar_color") or "#00c000")
        cv.create_text((20 + 760) * s / 2, (10 + 80) * s / 2, text=f"{rpm:.0f}",
                       fill="#000000" if band else "#ffffff", font=self.f_px["rpm"])

        # delta bar (x22 y113 756x12): fill grows RIGHT/RED slow, LEFT/GREEN fast
        cv.create_rectangle(22 * s, 113 * s, (22 + 756) * s, (113 + 12) * s,
                            outline="#202020", fill="#101010")
        mid = (22 + 756 / 2) * s
        cv.create_line(mid, 113 * s, mid, (113 + 12) * s, fill="#ffffff")
        d = st.get("delta_ms")
        if d is not None:
            full, dead = 2000.0, 100.0
            if abs(d) > dead:
                px = min(1.0, min(1.0, abs(d) / full)) * 378
                if d > 0:      # slower -> right, red
                    cv.create_rectangle(mid, 114 * s, (mid + px * s), 124 * s, fill="#ff0000", outline="")
                else:          # faster -> left, green
                    cv.create_rectangle((mid - px * s), 114 * s, mid, 124 * s, fill="#00c000", outline="")

        # speed: huge, right side, x=600 centre, y 130..330
        spd = st.get("speed_mph")
        txt = "--" if spd is None else (f"{spd:.0f}" if spd >= 100 else f"{spd:.1f}")
        cv.create_text(600 * s, 230 * s, text=txt, fill="#ffffff", font=self.f_px["speed"])

        # middle column: PRED / LAP / BEST (labels x264, values to their right)
        # firmware: labels at x264 y378/406/434, value sprites pushed at (325, 372/400/428).
        # (v0.1.165 fix: I had drawn the labels at x=20, which collided with the sensor block.)
        for i, (lab, key) in enumerate((("PRED", "pred_ms"), ("LAP", "last_ms"), ("BEST", "best_ms"))):
            y = (378 + i * 28) * s
            cv.create_text(264 * s, y, text=lab, fill="#7f93a8", font=self.f_px["lbl"], anchor="w")
            ms = st.get(key)
            col = "#ffffff"
            if key == "pred_ms" and ms is not None and st.get("last_ms") is not None:
                col = "#00ff00" if ms < st["last_ms"] else "#ff8080"
            if key == "best_ms":
                col = "#00ff00"
            cv.create_text(325 * s, y, text=_lap(ms), fill=col, font=self.f_px["row4"], anchor="w")

        # right column: LAP n / GPS / TIME (values at x610)
        rows = [("LAP " + (str(st["lap_n"]) if st.get("lap_n") else "--"), "#ffff00"),
                (st.get("gps_text") or "GPS --", "#00ff00"),
                ("TIME " + (st.get("sess_time") or "--"), "#ffffff")]
        for i, (txt, col) in enumerate(rows):
            cv.create_text(610 * s, (346 + i * 32) * s, text=txt, fill=col,
                           font=self.f_px["mid"], anchor="w")

        # sensor monitor block 0,340 260x140, bottom-anchored, auto-sized rows
        cv.create_rectangle(0, 340 * s, 260 * s, 480 * s, outline="#181818", fill="#000000")
        rows = self.visible_rows()
        n = len(rows)
        if warn and blink_on:
            # the warning flash OWNS the block while lit: name (Font4x2) + live value
            cv.create_rectangle(0, 340 * s, 260 * s, 480 * s, fill="#000000", outline="")
            cv.create_text(130 * s, 390 * s, text=warn[0], fill=warn[1],
                           font=self.f_px["big"])
            cv.create_text(130 * s, 448 * s, text=warn[2], fill=warn[1],
                           font=self.f_px["row4"])
        elif n:
            pitch = self.pitch(n)
            fnt = self.f_px["row4"] if pitch >= 30 else (self.f_px["row2"] if pitch >= 19 else self.f_px["row0"])
            for k, (item, text, col, _w) in enumerate(rows):
                ry = 340 + (140 - (n - k) * pitch)
                cv.create_text(20 * s, (ry + pitch / 2) * s, text=text, fill=col,
                               font=fnt, anchor="w")

        # START/STOP + TRACK: firmware RECBTN_X/Y/W/H = 30,155,160,70 and TRKBTN_Y = 235.
        # (v0.1.165 fix: I had it at y=380, which overlapped the sensor monitor block.)
        rec = st.get("recording")
        cv.create_rectangle(30 * s, 155 * s, (30 + 160) * s, (155 + 70) * s,
                            outline="#ffffff", fill="#ff0000" if rec else "#00c000")
        cv.create_text(110 * s, 190 * s, text="STOP" if rec else "START", fill="#ffffff",
                       font=self.f_px["mid"])
        cv.create_rectangle(30 * s, 235 * s, (30 + 160) * s, (235 + 70) * s,
                            outline="#ffffff", fill="#1e3f5b")
        cv.create_text(110 * s, 270 * s, text=st.get("track_name") or "TRACK",
                       fill="#ffffff", font=self.f_px["row2"])
        if rec:
            cv.create_text(200 * s, 140 * s, text="REC", fill="#ff0000", font=self.f_px["row4"], anchor="w")

        # wall clock, tiny, bottom-right
        cv.create_text(776 * s, 448 * s, text=st.get("clock") or "--:--",
                       fill="#888888" if st.get("clock") else "#ff8000",
                       font=self.f_px["tiny"], anchor="w")


def _lap(ms):
    if not ms:
        return "--"
    m = int(ms // 60000)
    return f"{m}:{(ms % 60000) / 1000:06.3f}" if m else f"{ms / 1000:.3f}"


def selftest():
    ok = True
    cfg = MonCfg()
    print("order:", cfg.order)
    print("modes:", {k: MON_MODE_NAMES[v] for k, v in cfg.mode.items()})
    # default visibility: TEMP/OIL/VOLT/AFR/IAT shown, MAP/TPS/RPM hidden
    print("warn(TEMP 230F):", cfg.item_warns("TEMP", 230.0),
          " warn(RPM 9000):", cfg.item_warns("RPM", 9000.0))
    ok &= cfg.item_warns("TEMP", 230.0) is True and cfg.item_warns("RPM", 9000.0) is False
    print("row text:", [cfg.__class__.__name__])
    v = DashView.__dict__
    # pitch rule
    import types
    inst = types.SimpleNamespace(scale=1.0)
    for n in (1, 3, 4, 5, 6, 8, 10):
        print(f"  n={n:2d} pitch={DashView.pitch(inst, n):3d} "
              f"font={'Font4' if DashView.pitch(inst,n)>=30 else 'Font2' if DashView.pitch(inst,n)>=19 else 'Font0'}")
    ok &= DashView.pitch(inst, 3) == 34 and DashView.pitch(inst, 7) == 20
    print("SELFTEST:", "OK" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(selftest())

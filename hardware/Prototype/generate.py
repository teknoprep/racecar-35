#!/usr/bin/env python3
"""Generate the Prototype wiring sheets (SVG + PNG).

Same visual language as ../breadboard/OPTO_WIRING.svg (which covers the PC817
tach front end — deliberately NOT repeated here).

    python3 generate.py          # writes 00..04 .svg and .png next to this file
"""
import math
import os

W = 1700
BG, INK = '#f3efe6', '#1b2420'
OUT = os.path.dirname(os.path.abspath(__file__))

STYLE = """<style>
 text{font-family:DejaVu Sans,Liberation Sans,sans-serif;fill:#1b2420}
 .t{font-size:34px;font-weight:700}
 .h{font-size:24px;font-weight:700}
 .s{font-size:18px}
 .v{font-family:DejaVu Sans Mono,monospace;font-size:16px}
 .xs{font-size:15px}
 .pin{font-family:DejaVu Sans Mono,monospace;font-size:19px;font-weight:700;fill:#a33}
 .box{fill:#f7f4ee;stroke:#1b2420;stroke-width:2.5}
 .wire{stroke:#1b2420;stroke-width:3;fill:none}
 .comp{fill:none;stroke:#1b2420;stroke-width:3}
 .panel{fill:#ffffff;stroke:#1b2420;stroke-width:2}
 .chip{fill:#eef3f7;stroke:#1b2420;stroke-width:2.5}
 .term{fill:#222222;stroke:#c9a227;stroke-width:2}
 .free{stroke:#b4b4b4;stroke-width:2;fill:none}
 .dim{fill:#9a9a9a}
</style>"""


# ---------------------------------------------------------------- primitives
def esc(s):
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')


def T(x, y, s, cls='xs', anchor=None, fill=None):
    a = f' text-anchor="{anchor}"' if anchor else ''
    f = f' style="fill:{fill}"' if fill else ''
    return f'<text x="{x}" y="{y}" class="{cls}"{a}{f}>{esc(s)}</text>'


def L(x1, y1, x2, y2, cls='wire', c=None, w=None):
    st = []
    if c:
        st.append(f'stroke:{c}')
    if w:
        st.append(f'stroke-width:{w}')
    s = f' style="{';'.join(st)}"' if st else ''
    return f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" class="{cls}"{s}/>'


def DOT(x, y, r=7, c=None):
    return f'<circle cx="{x}" cy="{y}" r="{r}" fill="{c or INK}"/>'


def RECT(x, y, w, h, cls='box', rx=8, fill=None, stroke=None, sw=None):
    s = f' fill="{fill}"' if fill else ''
    s += f' stroke="{stroke}"' if stroke else ''
    s += f' stroke-width="{sw}"' if sw else ''
    return f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{rx}" class="{cls}"{s}/>'


def BOX(x, y, w, h, label, sub=None, cls='box', tcls='s', tfill=None, rx=8):
    o = [RECT(x, y, w, h, cls, rx=rx)]
    o.append(T(x + w / 2, y + h / 2 + (-4 if sub else 7), label, tcls,
               anchor='middle', fill=tfill))
    if sub:
        o.append(T(x + w / 2, y + h / 2 + 20, sub, 'xs', anchor='middle', fill='#666'))
    return ''.join(o)


def TERM(x, y, w, h, label, sub=None):
    o = [RECT(x, y, w, h, 'term', rx=8)]
    o.append(T(x + w / 2, y + 30, label, 's', anchor='middle', fill='#f5e6a8'))
    if sub:
        o.append(T(x + w / 2, y + 50, sub, 'xs', anchor='middle', fill='#dddddd'))
    return ''.join(o)


def ZIG(x, y, length, vertical=False, amp=14, n=6):
    pts = [(x, y)]
    step = length / n
    for i in range(n):
        if vertical:
            pts.append((x + (amp if i % 2 == 0 else -amp), y + step * (i + 0.5)))
        else:
            pts.append((x + step * (i + 0.5), y + (amp if i % 2 == 0 else -amp)))
    pts.append((x, y + length) if vertical else (x + length, y))
    p = ' '.join(f'{a:.1f},{b:.1f}' for a, b in pts)
    return f'<polyline points="{p}" class="comp"/>'


def GND(x, y, label=None, dx=38):
    o = [L(x, y, x, y + 20), L(x - 26, y + 20, x + 26, y + 20, w=3.5, c=INK),
         L(x - 15, y + 32, x + 15, y + 32, w=3.5, c=INK),
         L(x - 5, y + 44, x + 5, y + 44, w=3.5, c=INK)]
    if label:
        o.append(T(x + dx, y + 32, label, 'xs'))
    return ''.join(o)


def RAIL(x1, x2, y, label=None, ly=None):
    o = [L(x1, y, x2, y, c='#0a6', w=8)]
    if label:
        o.append(T(x1 + 16, ly or y + 34, label, 's'))
    return ''.join(o)


def ARROW(x1, y1, x2, y2, c=None):
    dx, dy = x2 - x1, y2 - y1
    d = math.hypot(dx, dy) or 1
    ux, uy = dx / d, dy / d
    px, py = -uy, ux
    b1 = (x2 - 15 * ux + 6 * px, y2 - 15 * uy + 6 * py)
    b2 = (x2 - 15 * ux - 6 * px, y2 - 15 * uy - 6 * py)
    return (L(x1, y1, x2 - 12 * ux, y2 - 12 * uy, c=c) +
            f'<polygon points="{x2:.1f},{y2:.1f} {b1[0]:.1f},{b1[1]:.1f} '
            f'{b2[0]:.1f},{b2[1]:.1f}" fill="{c or INK}"/>')


def CARD(x, y, w, h, title, sub=None):
    o = [RECT(x, y, w, h, 'panel')]
    o.append(T(x + 18, y + 34, title, 'h'))
    if sub:
        o.append(T(x + 18, y + 58, sub, 'xs', fill='#666'))
    return ''.join(o)


def PANEL(x, y, w, h, title, lines):
    o = [RECT(x, y, w, h, 'panel')]
    yy = y + 34
    if title:
        o.append(T(x + 18, yy, title, 'h'))
        yy += 32
    for ln in lines:
        cls, txt = ln if isinstance(ln, tuple) else ('v', ln)
        o.append(T(x + 18, yy, txt, cls))
        yy += 26
    return ''.join(o)


def HEADER(title, sub=None, warn=None):
    o = [T(48, 52, title, 't')]
    y = 52
    if sub:
        y = 84
        o.append(T(48, y, sub, 's', fill='#555'))
    y += 16
    if warn:
        hh = 26 * len(warn) + 26
        o.append(RECT(48, y, 1604, hh, rx=6, fill='#f4d2c4', stroke='#a33', sw='1.5'))
        for i, ln in enumerate(warn):
            o.append(T(62, y + 30 + i * 26, ln, 'xs'))
        y += hh
    return ''.join(o), y + 46


def CARDS(x, y, w, h, title, left, right, rows, sub=None, pitch=66):
    """Two boxes with labelled wire rows between them."""
    o = [CARD(x, y, w, h, title, sub)]
    lw = 250
    o.append(RECT(x + 24, y + 66, lw, 56, cls='chip'))
    o.append(T(x + 24 + lw / 2, y + 100, left, 's', anchor='middle'))
    o.append(RECT(x + w - 24 - lw, y + 66, lw, 56, cls='chip'))
    o.append(T(x + w - 24 - lw / 2, y + 100, right, 's', anchor='middle'))
    ry = y + 180
    for lpin, direction, rpin, note in rows:
        o.append(T(x + 24 + lw - 8, ry + 7, lpin, 'v', anchor='end'))
        o.append(T(x + w - 24 - lw + 8, ry + 7, rpin, 'v', anchor='start'))
        x1, x2 = x + 24 + lw + 20, x + w - 24 - lw - 20
        if direction == '>':
            o.append(ARROW(x1, ry, x2, ry))
        elif direction == '<':
            o.append(ARROW(x2, ry, x1, ry))
        else:
            o.append(L(x1, ry, x2, ry))
        if note:
            o.append(T(x + w / 2, ry + 26, note, 'xs', anchor='middle', fill='#666'))
        ry += pitch
    return ''.join(o)


def doc(h, *body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{h}" '
            f'viewBox="0 0 {W} {h}"><rect width="{W}" height="{h}" fill="{BG}"/>'
            + STYLE + ''.join(body) + '</svg>')


# ---------------------------------------------------------------- sheet 00
def sheet_pinmap():
    head, y0 = HEADER(
        'Prototype board — Teensy 4.1 pin map',
        'Every wire that lands on the Teensy. Car connections are the screw terminals J1–J7 on the left edge.',
        ['Header order follows the standard Teensy 4.1 layout — confirm the 5V / GND / 3.3V pins against the silkscreen on YOUR board before soldering.',
         'Teensy 4.1 pins are NOT 5 V tolerant. The 3.3V pin is rated for about 250 mA of external load (PJRC).'])
    left = [
        ('GND', 'GND rail — star point', 'used'),
        ('0', 'RX1  <-  Pi GPIO14   (video, 1 kΩ)', 'used'),
        ('1', 'TX1  ->  Pi GPIO15   (video, 1 kΩ)', 'used'),
        ('2', 'not used', 'free'),
        ('3', 'not used', 'free'),
        ('4', 'not used', 'free'),
        ('5', 'reserved — NET module, no firmware', 'res'),
        ('6', 'reserved — NET module, no firmware', 'res'),
        ('7', 'RX2  <-  GPS TX', 'used'),
        ('8', 'TX2  ->  GPS RX', 'used'),
        ('9', 'TACH  <-  PC817 pin 4  (see the opto sheet)', 'used'),
        ('10', 'reserved — NET module, no firmware', 'res'),
        ('11', 'reserved — NET module, no firmware', 'res'),
        ('12', 'reserved — NET module, no firmware', 'res'),
        ('3.3V', '3.3 V rail -> GPS, IMU, 4.7 kΩ, 150 Ω', 'used'),
        ('24', 'not used  (Serial6 TX spare)', 'free'),
        ('25', 'not used  (Serial6 RX spare)', 'free'),
        ('26', 'not used', 'free'),
        ('27', 'not used', 'free'),
        ('28', 'not used', 'free'),
        ('29', 'not used', 'free'),
        ('30', 'not used', 'free'),
        ('31', 'not used', 'free'),
        ('32', 'not used', 'free'),
    ]
    right = [
        ('5V', '<-  J1 5 V from the buck   (VIN)', 'used'),
        ('GND', 'GND rail — star point', 'used'),
        ('3.3V', '3.3 V rail  (same net as the left 3.3V pin)', 'used'),
        ('23', 'CAN1 RX  <-  SN65HVD230 CRX   (optional)', 'used'),
        ('22', 'CAN1 TX  ->  SN65HVD230 CTX   (optional)', 'used'),
        ('21', 'not used  (A7 spare ADC)', 'free'),
        ('20', 'A6  <-  AEM 30-0300 via 20k/20k + 1 kΩ', 'used'),
        ('19', 'SCL  ->  IMU SCL', 'used'),
        ('18', 'SDA  ->  IMU SDA', 'used'),
        ('17', 'A3  <-  coolant sender + 150 Ω', 'used'),
        ('16', 'A2  <-  oil sender via 10 kΩ / 20 kΩ', 'used'),
        ('15', 'RX3  <-  CrowPanel J10 TXD0_H  (921600)', 'used'),
        ('14', 'TX3  ->  CrowPanel J10 RXD0_H  (921600)', 'used'),
        ('13', 'heartbeat LED — leave alone', 'res'),
        ('GND', 'GND rail', 'used'),
        ('41', 'not used', 'free'),
        ('40', 'not used', 'free'),
        ('39', 'not used', 'free'),
        ('38', 'not used', 'free'),
        ('37', 'not used', 'free'),
        ('36', 'not used', 'free'),
        ('35', 'not used', 'free'),
        ('34', 'not used', 'free'),
        ('33', 'not used', 'free'),
    ]
    pitch, top = 42, y0 + 40
    body_top, body_h = top - 32, len(left) * pitch + 64
    o = [head]
    o.append(RECT(600, body_top, 500, body_h, rx=18, fill='#f7f4ee', stroke=INK, sw=3))
    o.append(RECT(735, body_top - 26, 230, 26, rx=6, fill='#dddddd', stroke=INK, sw=2))
    o.append(T(850, body_top - 8, 'USB-C  (flash from this end)', 'xs', anchor='middle'))
    o.append(RECT(700, body_top + body_h + 14, 300, 26, rx=6, fill='#dddddd', stroke=INK, sw=2))
    o.append(T(850, body_top + body_h + 34, 'micro-SD socket (on the back)', 'xs',
               anchor='middle', fill='#666'))
    o.append(T(850, body_top + 30, 'TEENSY 4.1', 'h', anchor='middle', fill='#cccccc'))
    for i, (lp, la, ls) in enumerate(left):
        y = top + i * pitch
        ok = ls == 'used'
        o.append(L(600, y, 552, y, cls='wire' if ok else 'free'))
        if ok:
            o.append(DOT(552, y))
        o.append(T(614, y + 6, lp, 'v', anchor='start',
                   fill=None if ok else '#9a9a9a'))
        o.append(T(538, y + 6, la, 'v', anchor='end',
                   fill=None if ok else '#9a9a9a'))
    for i, (rp, ra, rs) in enumerate(right):
        y = top + i * pitch
        ok = rs == 'used'
        o.append(L(1100, y, 1148, y, cls='wire' if ok else 'free'))
        if ok:
            o.append(DOT(1148, y))
        o.append(T(1086, y + 6, rp, 'v', anchor='end',
                   fill=None if ok else '#9a9a9a'))
        o.append(T(1162, y + 6, ra, 'v', anchor='start',
                   fill=None if ok else '#9a9a9a'))
    o.append(DOT(56, top - 4, r=7))
    o.append(T(74, top + 6, 'wired', 'v'))
    o.append(f'<circle cx="56" cy="{top + 26}" r="6" fill="none" stroke="#9a9a9a" stroke-width="2"/>')
    o.append(T(74, top + 32, 'reserved', 'v', fill='#9a9a9a'))
    o.append(T(48, top + 62, 'no dot = not used', 'v', fill='#9a9a9a'))

    ty = body_top + body_h + 70
    o.append(PANEL(48, ty, 1604, 250, 'Screw terminals — left edge of the board, top to bottom', [
        ('v', 'J1 POWER   5V · GND                       <- 5 V buck only, never 12 V'),
        ('v', 'J2 SCREEN  GND · 5V · TX · RX              -> CrowPanel J10 (XH2.54), 921600'),
        ('v', 'J3 VIDEO   GND · TX · RX                   -> Raspberry Pi 5 header 6/10/8, 3.3 V, 115200'),
        ('v', 'J4 TACH    SIG · GND                       -> PC817 input (R1 1 kΩ)'),
        ('v', 'J5 OIL     5V · SIG · GND                  -> 0.5-4.5 V transducer, divider at A2'),
        ('v', 'J6 NTC     SIG · GND                       -> coolant sender, 150 Ω pull-up at A3'),
        ('v', 'J7 AEM     WHT · BRN                       -> AEM 30-0300 pin 9 / pin 10, divider at A6'),
        ('xs', 'Full pin-by-pin build sheet: ../breadboard/PINOUT.md  ·  tach front end: ../breadboard/OPTO_WIRING.svg'),
    ]))
    return doc(ty + 290, *o)


# ---------------------------------------------------------------- sheet 01
def sheet_power():
    head, y0 = HEADER(
        'Power and ground — 12 V in, 5 V and 3.3 V rails',
        'The buck feeds the board; the Teensy makes its own 3.3 V; the Pi 5 has a separate supply.',
        ['NEVER put 12 V on J1. NEVER put 5 V on the Teensy 3.3V pin. NEVER put 5 V on J3 or a Pi GPIO.',
         'Cut the Teensy VUSB-VIN pads (underside) if a USB cable and J1 5 V will be connected at the same time.'])
    o = [head]
    # --- card A: 5 V chain
    ya = y0
    o.append(CARD(48, ya, 1604, 480, '5 V rail — 12 V battery in, everything else comes off J1'))
    o.append(BOX(90, ya + 76, 230, 48, '12 V battery +', 'through a chassis fuse'))
    o.append(L(320, ya + 100, 370, ya + 100))
    o.append(BOX(370, ya + 76, 150, 48, '5 A fuse'))
    o.append(L(520, ya + 100, 570, ya + 100))
    o.append(BOX(570, ya + 76, 260, 48, '5 V buck', '3 A or better'))
    o.append(L(830, ya + 100, 880, ya + 100))
    o.append(L(880, ya + 50, 880, ya + 250, w=6))
    o.append(T(770, ya + 160, 'measure 5.00 V here', 'xs', anchor='middle', fill='#a33'))
    o.append(T(770, ya + 180, 'before anything is connected', 'xs', anchor='middle',
               fill='#a33'))
    for yy, label, sub in ((ya + 50, 'Teensy 5V (VIN pin)', 'top-right header pin'),
                           (ya + 150, 'J2 5V  ->  CrowPanel J10', '+5V_IN pin'),
                           (ya + 250, 'J5 5V  ->  oil sender', 'transducer supply')):
        o.append(L(880, yy, 940, yy))
        o.append(BOX(940, yy - 22, 430, 44, label, sub))
    o.append(RAIL(160, 880, ya + 420, 'battery -  and  buck OUT-  ->  GND rail (see the ground card)'))
    o.append(L(205, ya + 124, 205, ya + 420, c='#0a6', w=6))
    o.append(L(640, ya + 124, 640, ya + 420, c='#0a6', w=6))
    o.append(T(48 + 18, ya + 330, 'Nothing else hangs off the 5 V rail — the screen is the only other 5 V load.',
               'xs', fill='#666'))

    # --- card B: 3.3 V rail
    yb = ya + 520
    o.append(CARD(48, yb, 790, 400, '3.3 V rail — made by the Teensy itself'))
    o.append(L(150, yb + 80, 150, yb + 320, w=6))
    o.append(T(150, yb + 68, 'Teensy 3.3V pin', 'xs', anchor='middle'))
    for i, (label, sub) in enumerate((
            ('GPS VCC', 'u-blox NEO-M9N'),
            ('IMU VCC', 'GY-521 MPU-6050'),
            ('R2 4.7 kΩ -> PC817 pin 4 / pin 9', 'tach pull-up'),
            ('150 Ω -> coolant sender (J6)', 'pull-up at A3'))):
        yy = yb + 80 + i * 80
        o.append(DOT(150, yy))
        o.append(L(150, yy, 240, yy))
        o.append(BOX(240, yy - 22, 560, 44, label, sub))
    o.append(T(66, yb + 368, 'PJRC rates the 3.3V pin for about 250 mA of external load — GPS + IMU + two pull-ups', 'xs', fill='#666'))
    o.append(T(66, yb + 390, 'is a few tens of mA, so there is plenty of headroom. Do not hang a screen or a Pi off it.', 'xs', fill='#666'))

    # --- card C: ground
    o.append(CARD(862, yb, 790, 400, 'Ground — one star point'))
    o.append(L(910, yb + 76, 910, yb + 316, c='#0a6', w=8))
    items = ['J1 GND  (buck OUT−)', 'J2 GND  (screen)', 'J3 GND  (Pi)',
             'J4 GND  (tach opto)', 'J5 GND  (oil)', 'J6 GND  (coolant — own wire)',
             'J7 BRN  (AEM reference)', 'Teensy GND  (both pins)',
             'GPS GND', 'IMU GND', 'CAN GND  (optional)']
    for i, it in enumerate(items):
        yy = yb + 84 + i * 20
        o.append(L(910, yy, 960, yy, c='#0a6', w=3))
        o.append(T(968, yy + 5, it, 'xs'))
    o.append(T(1240, yb + 120, 'Rules:', 's'))
    for i, ln in enumerate(('Every return lands on this one rail.',
                            'Never daisy-chain grounds through the engine block.',
                            'The coolant sender needs its own return wire.',
                            'Keep the tach and GPS returns apart until here.',
                            'The screen and the Pi have their own supplies —',
                            'only their GND / signals come to this board.')):
        o.append(T(1240, yb + 152 + i * 26, ln, 'xs', fill='#666'))

    yf = yb + 440
    o.append(PANEL(48, yf, 1604, 220, 'Before you power up', [
        ('v', '1.  Buck unloaded: 5.00 V.   With the Teensy connected: 4.8-5.2 V.   Adjust or replace it if not.'),
        ('v', '2.  Cut the VUSB-VIN pads on the Teensy underside if USB and J1 will both be live.'),
        ('v', '3.  Ohmmeter, everything off: 5 V rail to GND is not a short.   3.3 V rail to GND is not a short.'),
        ('v', '4.  Pi 5 keeps its OWN 5 V 5 A USB-C supply — J3 carries only GND, TX and RX.'),
        ('v', '5.  3.3 V comes out of the Teensy. Never feed 3.3 V INTO the 3.3V pin.'),
    ]))
    return doc(yf + 260, *o)


# ---------------------------------------------------------------- sheet 02
def sheet_uart():
    head, y0 = HEADER(
        'The two UART cables — screen (921600) and Pi 5 video (115200)',
        'Both are crossed TX/RX plus ground, but they are different voltages and different baud rates. Do not mix them up.',
        ['Disconnect J2 TX/RX before USB-flashing the CrowPanel — its UART0 is shared with the CH340 uploader.',
         'J3 is 3.3 V ONLY. The Pi and the Teensy must share ground or neither link works.'])
    o = [head]
    o.append(CARDS(48, y0, 790, 520, 'J2 SCREEN -> CrowPanel J10', 'Teensy 4.1',
                   'CrowPanel J10', [
        ('GND', '--', 'GND', 'shared ground — mandatory'),
        ('pin 14  TX3', '>', 'RXD0_H', 'crossed: Teensy TX -> screen RX'),
        ('pin 15  RX3', '<', 'TXD0_H', 'crossed: screen TX -> Teensy RX'),
        ('J1 5 V', '>', '+5V_IN', 'or power the screen from its own USB-C'),
    ], sub='XH2.54 4-pin header only. 921600 8N1.'))
    o.append(T(66, y0 + 428, 'NEVER use the small HY2.0 header on the screen: its 3V3_OUT pin',
               'xs', fill='#a33'))
    o.append(T(66, y0 + 450, 'is an OUTPUT — 5 V there kills the ESP32.', 'xs', fill='#a33'))
    o.append(T(66, y0 + 486, 'A 5" Advance needs the -DDASH_BOARD=51 build; a 7" the =71 build.',
               'xs', fill='#666'))

    o.append(CARDS(862, y0, 790, 520, 'J3 VIDEO -> Raspberry Pi 5', 'Teensy 4.1',
                   'Pi 5 40-pin header', [
        ('GND', '--', 'pin 6  GND', 'shared ground'),
        ('pin 1  TX1', '>', 'pin 10  GPIO15 RXD', '1 kΩ in series, in the cable'),
        ('pin 0  RX1', '<', 'pin 8  GPIO14 TXD', '1 kΩ in series, in the cable'),
        ('--', '--', '--', 'no power pins — the Pi has its own 5 V supply'),
    ], sub='3.3 V logic only. 115200 8N1.'))
    o.append(T(880, y0 + 430, 'Pi config:  dtparam=uart0=on   and   usb_max_current_enable=1',
               'v'))
    o.append(T(880, y0 + 456, 'Cameras and the USB stick live on the Pi, not on this board.',
               'xs', fill='#666'))

    yf = y0 + 560
    o.append(PANEL(48, yf, 1604, 200, 'Which cable is which', [
        ('v', 'J2 SCREEN   Teensy pin 14 / 15   <->  CrowPanel J10 RXD0_H / TXD0_H    921600   screen is 5 V-tolerant behind its shifters'),
        ('v', 'J3 VIDEO    Teensy pin 1 / 0     <->  Pi 5 GPIO15 / GPIO14             115200   3.3 V both ends — 5 V would damage the Pi'),
        ('xs', 'A UART wired to the wrong header produces nothing at all: no telemetry, no video status line. Check the crossing first.'),
        ('xs', 'Both links are line-oriented, \\n terminated. The screen link carries the whole dash protocol; the Pi link only REC / TRACK / HUD.'),
    ]))
    return doc(yf + 240, *o)


# ---------------------------------------------------------------- sheet 03
def sheet_gps_imu():
    head, y0 = HEADER(
        'GPS and IMU — the two modules that sit on the board',
        'Both run on 3.3 V and both are read automatically at boot. Neither one needs a terminal block.',
        ['Feed the IMU 3.3 V, never 5 V — its pull-ups reference VCC and the Teensy is not 5 V tolerant.',
         'Solder or crimp these four-wire connections properly: an intermittent jumper shows up as "IMU not found" or a frozen GPS.'])
    o = [head]
    o.append(CARDS(48, y0, 790, 560, 'GPS — u-blox NEO-M9N', 'GPS module',
                   'Teensy 4.1 (Serial2)', [
        ('VCC / 3V3', '>', '3.3 V', 'SparkFun RTK boards accept 3.3-5 V; feed 3.3 V'),
        ('GND', '--', 'GND', 'to the star rail, not the engine block'),
        ('TX', '>', 'pin 7  RX2', 'module sends UBX/NMEA to the Teensy'),
        ('RX', '<', 'pin 8  TX2', 'REQUIRED — boot config and baud change go here'),
    ], sub='External active antenna on the SMA connector.'))
    o.append(T(66, y0 + 460, 'Boot baud scan: 230400 -> 38400 -> 9600, then raised to 230400.', 'xs'))
    o.append(T(66, y0 + 486, 'Keep these wires short and away from the tach/oil harness.', 'xs', fill='#666'))
    o.append(T(66, y0 + 516, 'Settings -> GPS baud can change it live; a bad pick is rescanned.', 'xs', fill='#666'))

    o.append(CARDS(862, y0, 790, 560, 'IMU — MPU-6050 on a GY-521 board', 'GY-521',
                   'Teensy 4.1 (Wire)', [
        ('VCC', '>', '3.3 V', 'NOT 5 V'),
        ('GND', '--', 'GND', 'shared star ground'),
        ('SDA', '<', 'pin 18  SDA', 'I2C0 — not 16/17, not 24/25'),
        ('SCL', '<', 'pin 19  SCL', '400 kHz, address 0x68'),
        ('AD0', '--', 'GND', 'AD0 high or floating -> 0x69 -> "not found"'),
    ], sub='XDA / XCL / INT stay unconnected.'))
    o.append(T(880, y0 + 500, 'Detection is one-shot at boot: all-zero IMU lines mean it never ACKed,', 'xs'))
    o.append(T(880, y0 + 524, 'not a calibration problem. Re-seat the header and reboot.', 'xs', fill='#666'))

    yf = y0 + 600
    o.append(PANEL(48, yf, 790, 250, 'What the boot banner should say', [
        ('v', 'MPU-6050 ready'),
        ('v', 'IMU cal: gyro bias removed, accel scaled to 1.00 g'),
        ('v', 'FreqMeasureMulti on pin 9: armed   (tach front end)'),
        ('v', 'CAN1 ready at 500000 bps   (only if the transceiver is fitted)'),
        ('xs', 'Boot the car stationary or the IMU auto-cal is rejected and the last good'),
        ('xs', 'calibration in EEPROM is kept instead.'),
    ]))
    o.append(PANEL(862, yf, 790, 250, 'Mounting the IMU', [
        ('v', 'Board flat, component side up, header pins toward the rear of the car.'),
        ('v', '  +X = forward      (accelerate / brake)'),
        ('v', '  +Y = right        (right-hand turns read negative)'),
        ('v', '  +Z = up           (about -1.0 g sitting level)'),
        ('xs', 'Get this wrong and the lateral / longitudinal g in your logs will be swapped'),
        ('xs', 'or mirrored, which is hard to spot afterwards.'),
    ]))
    return doc(yf + 290, *o)


# ---------------------------------------------------------------- sheet 04
def sheet_analog():
    head, y0 = HEADER(
        'Analogue inputs — oil, coolant, AEM AFR (and the optional CAN + SD)',
        'Every one of these is a divider or a pull-up in front of an ADC pin. Values are firmware contracts, not suggestions.',
        ['The Teensy ADC is 3.3 V. A raw 5 V sensor output, or the AEM WHITE wire, will damage the pin — the divider is not optional.',
         'Meter each one at a known input voltage before enabling it in Settings.'])
    o = [head]

    # ---- row 1: oil + coolant
    y = y0
    o.append(CARD(48, y, 790, 410, 'Oil pressure — J5 -> pin 16 (A2)'))
    o.append(T(66, y + 96, 'J5 SIG', 'v'))
    o.append(L(150, y + 90, 200, y + 90))
    o.append(ZIG(200, y + 90, 130))
    o.append(T(265, y + 70, '10 kΩ', 'xs', anchor='middle'))
    o.append(L(330, y + 90, 430, y + 90))
    o.append(DOT(400, y + 90))
    o.append(BOX(430, y + 68, 380, 44, 'Teensy pin 16 (A2)', 'oil PSI'))
    o.append(L(400, y + 90, 400, y + 150))
    o.append(ZIG(400, y + 150, 110, vertical=True))
    o.append(T(442, y + 212, '20 kΩ', 'xs'))
    o.append(GND(400, y + 260, 'GND rail'))
    o.append(T(66, y + 330, 'Firmware expects  V_adc = V_sensor × 2/3', 'xs'))
    o.append(T(66, y + 358, '0.50 V in -> 0.33 V on A2      |      4.50 V in -> 3.00 V on A2', 'v'))
    o.append(T(66, y + 386, 'A genuine 0.5-4.5 V sender reads ~0.5 V at atmosphere when powered.',
               'xs', fill='#666'))

    o.append(CARD(862, y, 790, 410, 'Coolant — J6 -> pin 17 (A3)'))
    o.append(T(880, y + 96, '3.3 V rail', 'v'))
    o.append(L(990, y + 90, 1040, y + 90))
    o.append(ZIG(1040, y + 90, 120))
    o.append(T(1100, y + 70, '150 Ω', 'xs', anchor='middle'))
    o.append(L(1160, y + 90, 1290, y + 90))
    o.append(DOT(1230, y + 90))
    o.append(BOX(1290, y + 68, 330, 44, 'Teensy pin 17 (A3)', 'coolant °F'))
    o.append(T(880, y + 156, 'J6 SIG', 'v'))
    o.append(L(980, y + 150, 1230, y + 150))
    o.append(L(1230, y + 90, 1230, y + 150))
    o.append(T(880, y + 200, 'J6 GND', 'v'))
    o.append(L(980, y + 194, 1050, y + 194))
    o.append(GND(1050, y + 194, 'its own wire to the GND rail'))
    o.append(T(880, y + 292, 'VDO 1600-22 Ω curve: 700 Ω at 100 °F, 110 Ω at 180 °F, 22 Ω at 250 °F.', 'xs'))
    o.append(T(880, y + 320, 'A different sender needs a different pull-up and new coefficients in src/main.cpp.',
               'xs', fill='#666'))
    o.append(T(880, y + 348, 'Do not rely on the engine block for the sender ground — run the wire.',
               'xs', fill='#666'))

    # ---- row 2: AEM + CAN
    y = y0 + 450
    o.append(CARD(48, y, 790, 470, 'AEM 30-0300 gauge output — J7 -> pin 20 (A6)'))
    o.append(T(66, y + 96, 'J7 WHT', 'v'))
    o.append(L(160, y + 90, 210, y + 90))
    o.append(ZIG(210, y + 90, 130))
    o.append(T(275, y + 70, '20 kΩ 0.1%', 'xs', anchor='middle'))
    o.append(L(340, y + 90, 400, y + 90))
    o.append(DOT(400, y + 90, r=8))
    o.append(ZIG(400, y + 90, 130))
    o.append(T(470, y + 70, '20 kΩ 0.1%', 'xs', anchor='middle'))
    o.append(L(530, y + 90, 610, y + 90))
    o.append(T(610, y + 96, 'J7 BRN -> GND', 'v'))
    o.append(L(400, y + 90, 400, y + 110))
    o.append(ZIG(400, y + 110, 90, vertical=True))
    o.append(T(442, y + 164, '1 kΩ', 'xs'))
    o.append(L(400, y + 200, 400, y + 232))
    o.append(L(400, y + 232, 470, y + 232))
    o.append(DOT(440, y + 232))
    o.append(BOX(470, y + 210, 340, 44, 'Teensy pin 20 (A6)', 'AEM AFR'))
    o.append(L(440, y + 232, 440, y + 280))
    o.append(L(415, y + 280, 465, y + 280, w=4, c=INK))
    o.append(L(415, y + 296, 465, y + 296, w=4, c=INK))
    o.append(L(440, y + 296, 440, y + 320))
    o.append(GND(440, y + 320, '100 nF  +  GND', dx=44))
    o.append(T(66, y + 200, '5.00 V in -> 2.50 V at pin 20', 's', fill='#a33'))
    o.append(T(66, y + 228, 'range 0.50-4.50 V = 8.50-18.00 AFR', 'xs', fill='#666'))
    o.append(T(66, y + 268, 'Use BOTH AEM wires:', 'xs'))
    o.append(T(66, y + 292, 'WHITE = pin 9 analogue +,  BROWN = pin 10 reference.', 'xs', fill='#666'))
    o.append(T(66, y + 322, 'The gauge keeps its own 12 V supply and fuse.', 'xs', fill='#666'))

    o.append(CARDS(862, y, 790, 470, 'CAN (optional) — MS3Pro over SN65HVD230',
                   'SN65HVD230', 'Teensy / bus', [
        ('3V3', '>', '3.3 V', 'a 5 V MCP2551 would damage the Teensy'),
        ('GND', '--', 'GND', 'star rail'),
        ('CTX', '>', 'pin 22  CAN1 TX', ''),
        ('CRX', '<', 'pin 23  CAN1 RX', ''),
        ('CANH / CANL', '--', 'MS3Pro', '120 Ω terminator at the end of the bus'),
    ], sub='Only needed when the sensor source is MegaSquirt.', pitch=52))

    yf = y + 510
    o.append(PANEL(48, yf, 1604, 210, 'Settings that must stay OFF until the matching circuit exists', [
        ('v', 'AEM 30-0300 AFR input        enable only after pin 20 meters 2.50 V with 5.00 V on J7 WHT-BRN.'),
        ('v', 'Video interconnect           enable only after J3 is wired and the Pi answers at 115200.'),
        ('v', 'Sensor data source           Direct (opto tach + ADCs) unless CAN or the BLE OBD dongle is fitted.'),
        ('v', 'Tach pulses / rev            calibrate against a known RPM before trusting it — default 2.0.'),
        ('xs', 'The SD card needs no wiring: it is the built-in socket on the back of the Teensy. FAT32, insert and go.'),
    ]))
    return doc(yf + 250, *o)


SHEETS = [
    ('00-Teensy-pin-map', sheet_pinmap),
    ('01-Power-and-ground', sheet_power),
    ('02-UART-screen-and-video', sheet_uart),
    ('03-GPS-and-IMU', sheet_gps_imu),
    ('04-Analogue-inputs', sheet_analog),
]


def main():
    try:
        import cairosvg
    except ImportError:
        cairosvg = None
    for name, fn in SHEETS:
        svg = fn()
        sp = os.path.join(OUT, name + '.svg')
        with open(sp, 'w', encoding='utf-8') as f:
            f.write(svg)
        line = f'  {name}.svg'
        if cairosvg:
            png = os.path.join(OUT, name + '.png')
            cairosvg.svg2png(url=sp, write_to=png, output_width=2400)
            line += f'  +  {name}.png'
        print(line)


if __name__ == '__main__':
    main()

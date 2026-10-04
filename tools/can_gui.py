#!/usr/bin/env python3
"""RC35 bench CAN console — a GUI front-end for `tools/can_sim.py bench`.

Answers, in one window, the questions we keep asking each other over chat:

  * is the broadcaster ACTUALLY running, and at what rate / bus load / gap?
  * what VALUES is it putting on the wire right now (and at what refresh rate)?
  * what exact BYTES go out for each frame id (0x700 CORE / 0x701 AUX / MS3)?
  * is the Teensy RECEIVING them, and is it ACKING them? (needs the Teensy's USB
    serial plugged in as well — its 1 Hz CANDIAG line carries frames/s, dup%,
    ACK_ERR, TX/RX error counters and the controller state; the ACK question is
    otherwise unknowable from the adapter, whose slcan firmware does not implement
    the status commands.)

It NEVER touches tools/can_sim.py — that file belongs to the bench-tool work. This
app spawns it exactly as you would by hand:

    python3 -u tools/can_sim.py bench -p /dev/ttyACM0 --hz 100

and parses its stdout. It only *imports* it (no edits) to reuse rc35_frames() so the
bytes shown are the bytes actually sent, not a re-implementation that could drift.

Run:  python3 tools/can_gui.py            (or --port, --hz, --teensy to preset)
      python3 tools/can_gui.py --selftest (no GUI: checks parsing + frame encoding)
"""

from __future__ import annotations

import argparse
import os
import queue
import re
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import ttk

try:
    import serial
    from serial.tools import list_ports
except ImportError:  # pragma: no cover
    sys.exit("pyserial missing")

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import dash_view                        # the dash clone (separate window)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CAN_SIM = os.path.join(HERE, "can_sim.py")

# Reuse the tool's OWN encoder so the displayed bytes cannot drift from the wire.
sys.path.insert(0, HERE)
try:
    import can_sim as _sim                      # import-safe (guarded by __main__)
    _HAVE_ENCODER = hasattr(_sim, "rc35_frames")
except Exception:                                # pragma: no cover
    _sim = None
    _HAVE_ENCODER = False

# ---------------------------------------------------------------------------
# line parsing
# ---------------------------------------------------------------------------
RE_VALUES = re.compile(
    r"RPM\s+(-?[\d.]+)\s+TEMP\s+(-?[\d.]+)\s*F?\s+OIL\s+(-?[\d.]+)\s+"
    r"VOLT\s+(-?[\d.]+)\s*V?\s+AFR\s+(-?[\d.]+)\s+IAT\s+(-?[\d.]+)\s*F?\s+"
    r"MAP\s+(-?[\d.]+)\s+TPS\s+(-?[\d.]+)")
# Real line: "  t=   6.0s  100 cycles/s  worst gap  10.8 ms  tx queue    0 B  load  5.2%  tx fail    0 (0/s)"
# NOTE the number comes BEFORE "cycles/s" — getting that backwards is a silent "--" panel.
RE_STAT = re.compile(
    r"([\d.]+)\s+cycles/s\s+worst gap\s+([\d.]+)\s*ms\s+tx queue\s+(\d+)\s*B"
    r"\s+load\s+([\d.]+)\s*%\s+tx fail\s+(\d+)")
RE_CANDIAG = re.compile(
    r"CANDIAG,(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+),(\d+)")
# The Teensy sends TWO shapes: the compact comma form to the DASH, and this verbose form to
# USB (which is what this GUI reads) — the verbose one is the only place the controller STATE
# appears, and the state is the ACK evidence.
RE_CANDIAG_USB = re.compile(
    r"CANDIAG\s+frames/s=(\d+)\s+dup=(\d+)%\s+total=(\d+)\s+base_hits=(\d+)"
    r"(?:\s+ids=\[[^\]]*\])?\s+state=([^\s].*?)\s+ACK_ERR=(\d+)\s+CRC_ERR=(\d+)"
    r"\s+FRM=(\d+)\s+STF=(\d+)\s+TXerr=(\d+)\s+RXerr=(\d+)")


def parse_candiag_usb(line):
    m = RE_CANDIAG_USB.search(line)
    if not m:
        return None
    fps, dup, total, base, state, ack, crc, frm, stf, tec, rec = m.groups()
    return {"fps": int(fps), "dup_pct": int(dup), "total": int(total),
            "base_hits": int(base), "state": state.strip(), "ack_err": int(ack),
            "crc_err": int(crc), "frm_err": int(frm), "stf_err": int(stf),
            "tec": int(tec), "rec": int(rec)}


# Their tool states the wire rate itself: "... at 100 Hz cycles = 200 frames/s (bus load ...)"
RE_TOOLRATE = re.compile(r"=\s*(\d+)\s+frames/s")


RE_BENCH_T = re.compile(r"^\s*t=\s*([\d.]+)s")


def usb_ports():
    """Real USB serial devices only - this box lists 32 useless /dev/ttyS* ports."""
    out = []
    for p in list_ports.comports():
        if p.device.startswith(("/dev/ttyACM", "/dev/ttyUSB", "/dev/cu.usb", "/dev/tty.usb")):
            out.append(p.device)
    return sorted(out)


def parse_values(line):
    m = RE_VALUES.search(line)
    if not m:
        return None
    keys = ("rpm", "clt", "oil", "volt", "afr", "iat", "map", "tps")
    return dict(zip(keys, (float(g) for g in m.groups())))


def parse_status(line):
    m = RE_STAT.search(line)
    if not m:
        return None
    cyc, gap, q, load, fail = m.groups()
    return {"cycles": float(cyc), "gap_ms": float(gap), "queue_b": int(q),
            "load_pct": float(load), "tx_fail": int(fail or 0)}


def parse_candiag(line):
    m = RE_CANDIAG.search(line)
    if not m:
        return None
    fps, total, base, dup, ack, tec, rec, txtest = (int(g) for g in m.groups())
    return {"fps": fps, "total": total, "base_hits": base, "dup_pct": dup,
            "ack_err": ack, "tec": tec, "rec": rec, "txtest": txtest}


# ---------------------------------------------------------------------------
# broadcaster subprocess
# ---------------------------------------------------------------------------
class Broadcaster:
    def __init__(self, on_line, on_exit):
        self.proc = None
        self.on_line = on_line
        self.on_exit = on_exit
        self.started = 0.0
        self.cmd = []
        self._q = queue.Queue()

    def running(self):
        return self.proc is not None and self.proc.poll() is None

    def start(self, port, hz, profile=None, bitrate="500k"):
        if self.running():
            return False
        cmd = [sys.executable, "-u", CAN_SIM, "bench", "-p", port, "--hz", str(hz)]
        if profile:
            cmd += ["--profile", profile]
        if bitrate and bitrate != "500k":
            cmd += ["-b", bitrate]
        self.cmd = cmd
        self.proc = subprocess.Popen(cmd, cwd=ROOT, stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True,
                                     bufsize=1, start_new_session=True)
        self.started = time.time()

        def reader():
            assert self.proc and self.proc.stdout
            for line in self.proc.stdout:
                self.on_line(line.rstrip("\n"))
            rc = self.proc.wait()
            self.on_exit(rc)
        threading.Thread(target=reader, daemon=True).start()
        self.on_line("[gui] started: " + " ".join(cmd))
        return True

    def stop(self):
        if not self.proc:
            return
        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
        except Exception:
            try:
                self.proc.terminate()
            except Exception:
                pass
        self.on_line("[gui] stop requested (SIGTERM to the process group)")


# ---------------------------------------------------------------------------
# Teensy USB serial reader (optional, read-only)
# ---------------------------------------------------------------------------
class TeensyReader:
    def __init__(self, on_line):
        self.ser = None
        self.on_line = on_line
        self._stop = False
        self.lines_s = 0
        self._lines = 0
        self._t0 = time.time()

    def open(self, port, baud=115200):
        self.close()
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self._stop = False

        def reader():
            buf = b""
            while not self._stop and self.ser:
                try:
                    data = self.ser.read(512)
                except Exception:
                    break
                if not data:
                    continue
                buf += data
                while b"\n" in buf:
                    raw, buf = buf.split(b"\n", 1)
                    txt = raw.decode(errors="replace").strip()
                    if txt:
                        self._lines += 1
                        now = time.time()
                        if now - self._t0 >= 1.0:
                            self.lines_s = int(self._lines / (now - self._t0))
                            self._lines = 0
                            self._t0 = now
                        self.on_line(txt)
        threading.Thread(target=reader, daemon=True).start()

    def close(self):
        self._stop = True
        if self.ser:
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None


# ---------------------------------------------------------------------------
# the app
# ---------------------------------------------------------------------------
CHANNELS = [("rpm", "RPM", "", 0), ("clt", "TEMP", "F", 1), ("oil", "OIL", "psi", 1),
            ("volt", "VOLT", "V", 2), ("afr", "AFR", "", 2), ("iat", "IAT", "F", 1),
            ("map", "MAP", "kPa", 1), ("tps", "TPS", "%", 1)]


class App:
    def __init__(self, root, args):
        self.root = root
        self.args = args
        self.q = queue.Queue()
        self.values = {}
        self.status = {}
        self.candiag = {}
        self.teensy_ver = "-"
        self.teensy_bench = None
        self.value_lines = 0
        self.value_rate = 0
        self._vl = 0
        self._vt0 = time.time()
        self.history = {k: [] for k, *_ in CHANNELS}
        self.frame_lines = []
        # dash-clone feed: filled from the Teensy's lines when connected (faithful), else from
        # the broadcaster's values with a modelled speed (the bench has no GPS).
        self.dash_state = {}
        self.tool_t = None            # the broadcaster's own elapsed seconds
        self.tool_t_wall = 0.0        # ...and when we last saw it, for phase lock
        self.feed_note = '-'
        self.dash = None
        self.dash_cfg = dash_view.MonCfg()
        self.dash_win = None
        self.rec_start = None

        root.title("RC35 bench CAN console")
        root.geometry("1180x780")
        root.configure(bg="#101418")
        self._build()
        self.bc = Broadcaster(self.q.put, lambda rc: self.q.put(f"[gui] broadcaster exited rc={rc}"))
        self.teensy = TeensyReader(self.q.put)
        self.root.after(100, self._drain)
        self.root.protocol("WM_DELETE_WINDOW", self._quit)
        if args.autostart:
            self.root.after(400, self.start)

    # ---------------- UI ----------------
    def _build(self):
        P = {"bg": "#101418", "fg": "#d8e0e8"}
        pad = {"padx": 8, "pady": 4}

        top = tk.Frame(self.root, bg="#101418")
        top.pack(fill="x", **pad)
        tk.Label(top, text="CANable / slcan port", **P).pack(side="left")
        self.port_var = tk.StringVar(value=self.args.port)
        ports = usb_ports() or [self.args.port]
        ttk.Combobox(top, textvariable=self.port_var, values=ports, width=14).pack(side="left", padx=4)
        tk.Label(top, text="Hz", **P).pack(side="left")
        self.hz_var = tk.StringVar(value=str(self.args.hz))
        tk.Entry(top, textvariable=self.hz_var, width=6, bg="#1b2028", fg="#d8e0e8",
                 insertbackground="#d8e0e8").pack(side="left", padx=4)
        tk.Label(top, text="profile", **P).pack(side="left")
        self.prof_var = tk.StringVar(value=self.args.profile or "sweep")
        ttk.Combobox(top, textvariable=self.prof_var, width=9,
                     values=("sweep", "chop", "pull", "steady")).pack(side="left", padx=4)
        self.start_btn = tk.Button(top, text="START broadcaster", command=self.start,
                                   bg="#1e5b2a", fg="white", activebackground="#2c7d3c")
        self.start_btn.pack(side="left", padx=8)
        self.stop_btn = tk.Button(top, text="STOP", command=self.stop,
                                  bg="#5b1e1e", fg="white", activebackground="#7d2c2c")
        self.stop_btn.pack(side="left")
        self.state_lbl = tk.Label(top, text="idle", bg="#101418", fg="#ffcc66")
        self.state_lbl.pack(side="left", padx=10)
        tk.Button(top, text="DASH VIEW", command=self.open_dash,
                  bg="#1e3f5b", fg="white").pack(side="left", padx=8)

        # ---- values ----
        mid = tk.Frame(self.root, bg="#101418")
        mid.pack(fill="x", **pad)
        vals = tk.LabelFrame(mid, text="values on the wire (parsed from the broadcaster)",
                             bg="#101418", fg="#9fb4c8")
        vals.pack(side="left", fill="both", expand=True)
        self.val_lbls = {}
        for i, (key, name, unit, prec) in enumerate(CHANNELS):
            cell = tk.Frame(vals, bg="#101418")
            cell.grid(row=i // 4, column=i % 4, sticky="w", padx=10, pady=2)
            tk.Label(cell, text=name, bg="#101418", fg="#7f93a8", width=5, anchor="w").pack(side="left")
            lbl = tk.Label(cell, text="--", bg="#101418", fg="#eaf2fa",
                           font=("DejaVu Sans Mono", 20, "bold"), width=7, anchor="e")
            lbl.pack(side="left")
            tk.Label(cell, text=unit, bg="#101418", fg="#7f93a8", width=4, anchor="w").pack(side="left")
            self.val_lbls[key] = lbl
        self.rate_lbl = tk.Label(vals, text="value updates: --/s", bg="#101418", fg="#ffcc66")
        self.rate_lbl.grid(row=2, column=0, columnspan=4, sticky="w", padx=10, pady=(6, 2))

        # ---- tool health ----
        health = tk.LabelFrame(mid, text="broadcaster health (their tool's own counters)",
                               bg="#101418", fg="#9fb4c8")
        health.pack(side="left", fill="y", padx=(8, 0))
        self.health_lbls = {}
        for i, (k, label) in enumerate((("cycles", "cycles/s"), ("gap_ms", "worst gap ms"),
                                        ("queue_b", "tx queue B"), ("load_pct", "bus load %"),
                                        ("tx_fail", "tx fail (ACK proxy)"))):
            tk.Label(health, text=label, bg="#101418", fg="#7f93a8", anchor="w").grid(row=i, column=0, sticky="w", padx=8)
            l = tk.Label(health, text="--", bg="#101418", fg="#eaf2fa",
                         font=("DejaVu Sans Mono", 13, "bold"), width=8, anchor="e")
            l.grid(row=i, column=1, sticky="e", padx=8)
            self.health_lbls[k] = l

        # ---- Teensy link ----
        tns = tk.LabelFrame(self.root, text="Teensy link (USB serial — this is where the ACK question is answered)",
                            bg="#101418", fg="#9fb4c8")
        tns.pack(fill="x", **pad)
        row = tk.Frame(tns, bg="#101418")
        row.pack(fill="x")
        tk.Label(row, text="port", bg="#101418", fg="#d8e0e8").pack(side="left")
        self.tport_var = tk.StringVar(value=self.args.teensy or "")
        cand = usb_ports()
        ttk.Combobox(row, textvariable=self.tport_var, values=cand, width=14).pack(side="left", padx=4)
        tk.Button(row, text="CONNECT", command=self.connect_teensy,
                  bg="#1e3f5b", fg="white").pack(side="left", padx=4)
        tk.Button(row, text="disconnect", command=self.disconnect_teensy,
                  bg="#333b44", fg="white").pack(side="left")
        self.tstate = tk.Label(row, text="not connected", bg="#101418", fg="#ffcc66")
        self.tstate.pack(side="left", padx=10)
        self.verdict = tk.Label(tns, text="verdict: (connect the Teensy to see whether it receives/ACKs)",
                                bg="#0b0f13", fg="#ffcc66", anchor="w", justify="left",
                                font=("DejaVu Sans Mono", 11, "bold"), wraplength=1140)
        self.verdict.pack(fill="x", padx=6, pady=4)
        self.cd_lbl = tk.Label(tns, text="", bg="#101418", fg="#c8d6e4",
                               font=("DejaVu Sans Mono", 10), anchor="w", justify="left")
        self.cd_lbl.pack(fill="x", padx=6)

        # ---- frames ----
        fr = tk.LabelFrame(self.root, text="frames on the wire (exact bytes, re-encoded with the tool's own encoder)",
                           bg="#101418", fg="#9fb4c8")
        fr.pack(fill="x", **pad)
        self.frames_txt = tk.Text(fr, height=4, bg="#0b0f13", fg="#b8e0c0",
                                  font=("DejaVu Sans Mono", 10), insertbackground="#d8e0e8")
        self.frames_txt.pack(fill="x", padx=6, pady=4)

        # ---- log ----
        lg = tk.LabelFrame(self.root, text="log (broadcaster stdout + GUI events)", bg="#101418", fg="#9fb4c8")
        lg.pack(fill="both", expand=True, **pad)
        self.log_txt = tk.Text(lg, bg="#0b0f13", fg="#c8d6e4",
                               font=("DejaVu Sans Mono", 9), insertbackground="#d8e0e8")
        self.log_txt.pack(fill="both", expand=True, padx=6, pady=4)

    # ---------------- actions ----------------
    def start(self):
        try:
            hz = float(self.hz_var.get())
        except ValueError:
            hz = 100.0
        self.bc.start(self.port_var.get(), hz, self.prof_var.get())

    def stop(self):
        self.bc.stop()

    def connect_teensy(self):
        p = self.tport_var.get().strip()
        if not p:
            return
        try:
            self.teensy.open(p)
            self.tstate.config(text=f"connected {p}", fg="#7fd07f")
        except Exception as exc:
            self.tstate.config(text=f"open failed: {exc}", fg="#ff8080")

    def disconnect_teensy(self):
        self.teensy.close()
        self.tstate.config(text="not connected", fg="#ffcc66")

    def _quit(self):
        try:
            self.bc.stop()
            self.teensy.close()
        finally:
            self.root.destroy()

    # ---------------- main loop ----------------
    def _drain(self):
        try:
            while True:
                line = self.q.get_nowait()
                self._line(line)
        except queue.Empty:
            pass
        self._tick()
        self.root.after(100, self._drain)

    def _line(self, line):
        self.log_txt.insert("end", line + "\n")
        if int(self.log_txt.index("end-1c").split(".")[0]) > 400:
            self.log_txt.delete("1.0", "200.0")
        self.log_txt.see("end")

        v = parse_values(line)
        if v:
            self.values = v
            if not self.dash_state.get("from_teensy"):
                st = self.dash_state
                st.update({k: v[k] for k in ("rpm", "clt", "oil", "volt", "afr", "iat", "map", "tps")})
                # the bench has no GPS: model a plausible road speed from RPM so the big number
                # moves instead of sitting at "--". Labelled in the window title area.
                st["speed_mph"] = max(0.0, (v["rpm"] - 900) / 33.0)
                st["gps_text"] = "GPS (modelled)"
                st["recording"] = st.get("recording", False)
            self._vl += 1
            now = time.time()
            if now - self._vt0 >= 5.0:      # 5 s: a ~1/s print rate reads stably
                self.value_rate = int(round(self._vl / (now - self._vt0)))
                self._vl, self._vt0 = 0, now
            for k in self.history:
                if k in v:
                    self.history[k].append(v[k])
                    if len(self.history[k]) > 240:
                        self.history[k].pop(0)
            self._render_frames(v)

        s = parse_status(line)
        if s:
            self.status = s
        m = RE_TOOLRATE.search(line)
        if m:
            self.tool_frames_s = int(m.group(1))
        mt = RE_BENCH_T.search(line)      # "  t= 205.6s ..." = the tool's clock
        if mt:
            self.tool_t = float(mt.group(1))
            self.tool_t_wall = time.time()

        c = parse_candiag(line) or parse_candiag_usb(line)
        if c:
            self.candiag = c
            if c.get("state"):
                self.candiag_state = c["state"]
        if "VER,teensy," in line:
            self.teensy_ver = line.split("VER,teensy,", 1)[1].strip()
        if line.startswith("BENCH "):
            self.teensy_bench = line.strip()
        self._feed_dash(line)

    def _tick(self):
        for key, name, unit, prec in CHANNELS:
            v = self.values.get(key)
            txt = "--" if v is None else f"{v:.{prec}f}"
            self.val_lbls[key].config(text=txt)
        # THE wire rate: cycles/s x 2 (each RC35 cycle is a CORE + an AUX frame), cross-checked
        # against the tool's own "= N frames/s" line. The printed-value rate below is the tool's
        # DISPLAY cadence (~25/s at --hz 100) and says nothing about the frame rate - conflating
        # the two is exactly the confusion this label exists to kill.
        cyc = (self.status or {}).get("cycles", 0.0)
        derived = int(round(cyc * 2))
        tool = getattr(self, "tool_frames_s", None)
        recv = (self.candiag or {}).get("fps")
        self.rate_lbl.config(
            text=(f"WIRE RATE: {cyc:.0f} cycles/s  ->  ~{derived} frames/s"
                  + (f"  (tool says {tool})" if tool else "")
                  + (f"   |  Teensy receives {recv} frames/s" if recv is not None else "")
                  + f"   |   value lines printed {self.value_rate}/s"
                    f" - the TOOL'S display cadence, NOT the frame rate"))
        for k, lbl in self.health_lbls.items():
            v = self.status.get(k)
            lbl.config(text="--" if v is None else f"{v:g}")
            if k == "tx_fail":
                lbl.config(fg="#ff8080" if (v or 0) > 0 else "#7fd07f")
        up = time.time() - self.bc.started if self.bc.running() else 0
        self.state_lbl.config(
            text=(f"running pid {self.bc.proc.pid}  {int(up)}s" if self.bc.running() else "idle"),
            fg="#7fd07f" if self.bc.running() else "#ffcc66")
        self._verdict()

    def _feed_dash(self, line):
        """Mirror what the DASH extracts from the wire, so the clone shows what it shows."""
        st = self.dash_state
        try:
            if line.startswith("ENG,"):
                p = line.split(",")
                if len(p) >= 4:
                    st["rpm"] = float(p[1])
                    if int(p[2]) >= 0:
                        st["oil"] = int(p[2]) / 10.0
                    if int(p[3]) >= 0:
                        st["clt"] = int(p[3]) / 10.0
                st["from_teensy"] = True
            elif line.startswith("ECU,"):
                p = line.split(",")
                if len(p) >= 8:
                    st["rpm"] = float(p[1])
                    st["clt"] = int(p[2]) / 10.0
                    st["map"] = int(p[3]) / 10.0
                    st["tps"] = int(p[4]) / 10.0
                    st["afr"] = int(p[5]) / 10.0 if int(p[5]) >= 0 else None
                    st["iat"] = int(p[6]) / 10.0 if int(p[6]) >= 0 else None
                    st["volt"] = int(p[7]) / 10.0 if int(p[7]) > 0 else None
                    if len(p) >= 9 and int(p[8]) >= 0:      # v0.1.159: 9th field = bench oil
                        st["oil"] = int(p[8]) / 10.0
                st["from_teensy"] = True
            elif line.startswith("GPS,"):
                p = line.split(",")
                if len(p) >= 8:
                    fix, sats, mph, status = int(p[1]), int(p[2]), float(p[5]), int(p[7])
                    st["speed_mph"] = mph
                    st["gps_text"] = ("GPS 3D" if fix == 3 else "GPS 2D" if fix == 2
                                      else "GPS --" if status in (0, 1) else "GPS STALE")
                    st["sats"] = sats
            elif line.startswith("TIME,"):
                st["epoch"] = int(line.split(",")[1])
            elif line.startswith("SD,REC,"):
                p = line.split(",")
                now = p[1] == "1"
                if now and not st.get("recording"):
                    self.rec_start = time.time()
                st["recording"] = now
        except (ValueError, IndexError):
            pass

    def _render_frames(self, v):
        if not _HAVE_ENCODER:
            return
        try:
            frames = _sim.rc35_frames(rpm=v["rpm"], clt_f=v["clt"], map_kpa=v["map"],
                                      tps_pct=v["tps"], iat_f=v["iat"], afr=v["afr"],
                                      batt_v=v["volt"], oil_psi=v["oil"], seq=0)
        except Exception:
            return
        out = []
        for mid, payload in frames.items():
            hx = payload.hex().upper()
            if mid == 0x700:
                note = (f"rpm {int(v['rpm'])}  map {v['map']:.1f}kPa  tps {v['tps']:.1f}%  "
                        f"clt {v['clt']:.0f}F  iat {v['iat']:.0f}F  seq 0")
            else:
                note = (f"afr {v['afr']:.2f}  batt {v['volt']:.2f}V  oil {v['oil']:.1f}PSI  adv")
            out.append(f"0x{mid:03X}  {hx:<20} -> {note}")
        txt = "\n".join(out)
        if txt != self.frames_txt.get("1.0", "end-1c"):
            self.frames_txt.delete("1.0", "end")
            self.frames_txt.insert("1.0", txt)

    # ---------------- dash clone ----------------
    def open_dash(self):
        if self.dash_win and self.dash_win.winfo_exists():
            self.dash_win.lift()
            return
        w = tk.Toplevel(self.root)
        w.title("Dash view — what the screen should show")
        w.configure(bg="#101418")
        self.dash_win = w
        self.dash = dash_view.DashView(w, scale=self.args.dash_scale, cfg=self.dash_cfg)

        strip = tk.Frame(w, bg="#101418")
        strip.pack(fill="x")
        tk.Label(strip, text="monitor order / mode  (mirrors the firmware's rules)", bg="#101418",
                 fg="#9fb4c8").pack(side="left", padx=6)
        for item in ("TEMP", "OIL", "VOLT", "AFR", "IAT", "MAP", "TPS", "RPM"):
            cell = tk.Frame(strip, bg="#101418")
            cell.pack(side="left", padx=3)
            tk.Label(cell, text=item, bg="#101418", fg="#d8e0e8",
                     font=("DejaVu Sans Mono", 9, "bold")).pack()
            b = tk.Button(cell, text="", width=10, font=("DejaVu Sans Mono", 7),
                          bg="#223", fg="white",
                          command=lambda i=item: self._cycle_mode(i))
            b.pack()
            cell.mode_btn = b
            tk.Button(cell, text="\u25b2", font=("DejaVu Sans Mono", 7), bg="#223", fg="white",
                      command=lambda i=item: self._move(i, -1)).pack()
            tk.Button(cell, text="\u25bc", font=("DejaVu Sans Mono", 7), bg="#223", fg="white",
                      command=lambda i=item: self._move(i, +1)).pack()
        self._sync_mode_buttons()
        self.root.after(40, self._dash_tick)

    def _cycle_mode(self, item):
        self.dash_cfg.mode[item] = (self.dash_cfg.mode[item] + 1) % 3
        self._sync_mode_buttons()

    def _move(self, item, delta):
        o = self.dash_cfg.order
        i = o.index(item)
        j = max(0, min(len(o) - 1, i + delta))
        if i != j:
            o[i], o[j] = o[j], o[i]

    def _sync_mode_buttons(self):
        if not (self.dash_win and self.dash_win.winfo_exists()):
            return
        for cell in self.dash_win.winfo_children()[1].winfo_children():
            b = getattr(cell, "mode_btn", None)
            if b is None:
                continue
            item = b.master.winfo_children()[0].cget("text")
            m = self.dash_cfg.mode.get(item, 0)
            b.config(text=dash_view.MON_MODE_NAMES[m],
                     bg=("#1e5b2a" if m == 0 else "#3f3f1e" if m == 1 else "#3a3a3a"))

    def _dash_tick(self):
        if not (self.dash_win and self.dash_win.winfo_exists()):
            return
        st = self.dash_state
        # THE FIX for the Dash View sitting at ~1 Hz: their tool only PRINTS values about
        # once a second, but the frames leave at cycles/s. bench_values() is the very
        # function that produces those frames, so calling it at 25 Hz - phase-locked to
        # the tool's own t= clock so the phase matches - shows what is really on the wire
        # rather than what happened to get printed. The frame BYTES panel still uses the
        # printed values, and rc35_frames() to encode them.
        if (self.bc.running() and self.tool_t is not None
                and not st.get("from_teensy") and _HAVE_ENCODER
                and hasattr(_sim, "bench_values")):
            t = self.tool_t + (time.time() - self.tool_t_wall)
            try:
                bv = _sim.bench_values(t, self.prof_var.get())
                st["rpm"] = bv.get("rpm")
                st["clt"] = bv.get("clt_f")
                st["oil"] = bv.get("oil_psi")
                st["volt"] = bv.get("batt_v")
                st["afr"] = bv.get("afr")
                st["iat"] = bv.get("iat_f")
                st["map"] = bv.get("map_kpa")
                st["tps"] = bv.get("tps_pct")
                st["speed_mph"] = max(0.0, ((bv.get("rpm") or 0.0) - 900.0) / 33.0)
                st["gps_text"] = "GPS (modelled)"
                self.feed_note = "tool waveform @25 Hz (phase-locked to t=)"
            except Exception:
                self.feed_note = "value lines only (bench_values failed)"
        elif st.get("from_teensy"):
            self.feed_note = "Teensy telemetry lines (faithful)"
        st["rpm_max"] = 7000
        st["clock"] = time.strftime("%H:%M")
        if self.rec_start and st.get("recording"):
            el = int(time.time() - self.rec_start)
            st["sess_time"] = f"{el // 3600}:{(el % 3600) // 60:02d}:{el % 60:02d}"
        # the flash: first enabled item (in DISPLAY order) that is warning — firmware v0.1.155+
        warn = None
        if self.dash:
            for item, val in ((i, st.get(i.lower() if i != "RPM" else "rpm"))
                              for i in self.dash_cfg.order):
                if self.dash_cfg.mode.get(item, 0) == 0 and item != "RPM" and \
                   self.dash_cfg.item_warns(item, val) and val is not None and not \
                   (item == "VOLT" and (st.get("rpm") or 0) < 500):
                    col = dash_view.PALETTE[self.dash_cfg.col.get(item, 0)]
                    warn = (item, col, self.dash.row_text(item, val).split(": ", 1)[1])
                    break
        st["warn"] = warn
        st["flashing"] = bool(warn)
        st["flash_color"] = warn[1] if warn else None
        st["rpm_bar_color"] = "#ff0000" if warn else "#00c000"
        self.dash.set_state(**st)
        self.dash.redraw(blink_on=int(time.time() * 2) % 2 == 0)
        if self.dash_win and self.dash_win.winfo_exists():
            self.dash_win.title("Dash view - feed: " + self.feed_note)
        self.root.after(40, self._dash_tick)          # 25 Hz, like the firmware

    def _verdict(self):
        """The debug the driver actually wants: is the Teensy seeing and ACKing?"""
        c = self.candiag
        tf = (self.status or {}).get("tx_fail", 0)
        if not c:
            if not self.teensy.ser:
                msg = ("No CANDIAG because the Teensy's USB serial is NOT OPEN here. It prints "
                       "CANDIAG once a second unconditionally, so plug the Teensy's USB into this "
                       "PC (it shows up as /dev/ttyACM1 while the CANable holds ACM0) and pick it "
                       "above. Without it the ACK question cannot be answered at all - this "
                       "adapter's slcan firmware has no status commands.")
            else:
                msg = ("Teensy USB is open but no CANDIAG has arrived. Either it is not running "
                       "dash firmware, or it is not powered.")
            col = "#ffcc66"
            if tf:
                msg += f"   Broadcaster reports tx fail={tf} (the adapter's own transmit-failure counter)."
            self.verdict.config(text="verdict: " + msg, fg=col)
            self.cd_lbl.config(text=f"Teensy version: {self.teensy_ver}   " +
                                    (self.teensy_bench or ""))
            return
        fps, dup, ack, tec, rec = c["fps"], c["dup_pct"], c["ack_err"], c["tec"], c["rec"]
        if fps >= 50:
            msg = (f"TEENSY IS RECEIVING AND ACKING — {fps} frames/s arriving, dup {dup}% . "
                   f"A completed CAN frame REQUIRES an ACK, so with only this pairing on the bus "
                   f"the Teensy must be driving it. Any remaining slowness is display-side.")
            col = "#7fd07f"
        elif fps > 0:
            msg = (f"PARTIAL: only {fps} frames/s (dup {dup}%). Frames are completing but mostly "
                   f"as retransmissions — marginal ACK/termination. Check the two 120R ends and "
                   f"the transceiver TXD path.")
            col = "#ffcc66"
        elif "bus off" in (getattr(self, "candiag_state", "") or "").lower() or tec >= 250 or rec >= 250:
            msg = (f"NO FRAMES and the controller is in an ERROR STATE (TEC {tec} REC {rec}). "
                   f"v0.1.164 re-inits it within ~5 s; if it keeps happening, the ACK path is the "
                   f"problem: transceiver TXD (pin 1) -> Teensy pin 22, and Rs (pin 8) -> GND.")
            col = "#ff8080"
        elif ack > 0:
            msg = f"NO FRAMES. {ack} ACK errors counted — the sender is not being ACKed."
            col = "#ff8080"
        else:
            msg = ("NO FRAMES and NO ERRORS: nothing at all is reaching the receiver. That is "
                   "wiring/termination/bitrate, not ACK — check CANH/CANL are not swapped, a "
                   "common ground exists, and exactly two 120R terminators are fitted.")
            col = "#ffcc66"
        st = getattr(self, "candiag_state", "")
        if st:
            msg += f"   [controller state: {st}]"
        self.verdict.config(text="verdict: " + msg, fg=col)
        self.cd_lbl.config(text=(f"fps {fps}  dup {dup}%  ACK_ERR {ack}  TEC {tec}  REC {rec}  "
                                 f"| Teensy {self.teensy_ver}  | {self.teensy_bench or ''}"))


# ---------------------------------------------------------------------------
def selftest():
    ok = True
    line = ("  t=   6.0s  100 cycles/s  worst gap  10.8 ms  tx queue    0 B  "
            "load  5.2%  tx fail    0 (0/s)")
    print("status parse:", parse_status(line))
    ok &= parse_status(line)["cycles"] == 100.0 and parse_status(line)["tx_fail"] == 0
    vline = ("           RPM  4022  TEMP  69.4F  OIL  52.7  VOLT 13.62  AFR 13.98  "
             "IAT  93.4  MAP  68.1  TPS  55.1")
    v = parse_values(vline)
    print("values parse:", v)
    ok &= v is not None and abs(v["rpm"] - 4022) < 1e-6 and abs(v["oil"] - 52.7) < 1e-6
    cd = parse_candiag("CANDIAG,200,12345,0,3,0,0,0,0")
    print("candiag (dash form) parse:", cd)
    ok &= cd and cd["fps"] == 200
    usb = ("CANDIAG frames/s=200 dup=3% total=12345 base_hits=0 ids=[0x700,0x701] "
           "state=Error Active ACK_ERR=0 CRC_ERR=0 FRM=0 STF=0 TXerr=0 RXerr=0")
    cu = parse_candiag_usb(usb)
    print("candiag (USB form) parse:", cu)
    ok &= cu and cu["fps"] == 200 and cu["state"] == "Error Active"
    if _HAVE_ENCODER:
        fr = _sim.rc35_frames(rpm=3000, clt_f=190, map_kpa=100, tps_pct=40, iat_f=100,
                              afr=14.7, batt_v=13.8, oil_psi=45, seq=0)
        print("0x700:", fr[0x700].hex().upper())
        print("0x701:", fr[0x701].hex().upper())
        ok &= fr[0x700][:2] == bytes([0x0B, 0xB8])         # 3000 rpm big-endian
        ok &= fr[0x701][0] == 147                          # 14.7 AFR x10
    else:
        print("(can_sim not importable — byte display disabled)")
    print("SELFTEST:", "OK" if ok else "FAIL")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", default="/dev/ttyACM0")
    ap.add_argument("--hz", type=float, default=100)
    ap.add_argument("--profile", default=None)
    ap.add_argument("--teensy", default=None, help="Teensy USB serial port (e.g. /dev/ttyACM1)")
    ap.add_argument("--autostart", action="store_true", help="start broadcasting on launch")
    ap.add_argument("--dash-scale", type=float, default=1.0, help="dash clone zoom (0.5-1.5)")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    root = tk.Tk()
    App(root, a)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())

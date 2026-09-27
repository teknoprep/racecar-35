#!/usr/bin/env python3
"""RaceDash video box for Raspberry Pi 5.

Listens on the Teensy Serial1 UART (VIDEN/TRACK/REC/HUD) and records
1080p30 H.264: front full-frame, rear as bottom-right PIP, HUD overlay.
"""
from __future__ import annotations

import glob
import json
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

VER = "0.1.150"
HERE = os.path.dirname(os.path.abspath(__file__))
HUD_PATH = "/tmp/racecar-hud.txt"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"


def load_env():
    cfg = {
        "UART_DEV": "/dev/serial0",
        "UART_BAUD": "115200",
        "FRONT": "",
        "BACK": "",
        "FRONT_SIZE": "1920x1080",
        "BACK_SIZE": "1920x1080",
        "FPS": "30",
        "USB_DIR": "",   # unused: any USB stick is auto-mounted
        "X264_PRESET": "veryfast",
        "VIDEO_BITRATE": "8M",
        "PIP_W": "480",
        "PIP_H": "270",
        "PIP_MARGIN": "16",
    }
    path = os.path.join(HERE, "config.env")
    if os.path.isfile(path):
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                cfg[k.strip()] = v.strip().strip('"').strip("'")
    return cfg


def which_uart(preferred: str) -> str:
    for p in (preferred, "/dev/serial0", "/dev/ttyAMA0", "/dev/ttyAMA10"):
        if p and os.path.exists(p):
            return p
    return preferred


def _is_usb_v4l(node: str) -> bool:
    name = os.path.basename(node)
    sysfs = f"/sys/class/video4linux/{name}"
    try:
        real = os.path.realpath(sysfs)
    except OSError:
        return False
    return "usb" in real.lower()


def v4l2_capture_nodes():
    """USB UVC capture nodes only (skip Pi ISP/HEVC platform devices)."""
    out = []
    for node in sorted(glob.glob("/dev/video*"), key=lambda s: int(s.replace("/dev/video", "") or 0)):
        if not _is_usb_v4l(node):
            continue
        try:
            r = subprocess.run(
                ["v4l2-ctl", "-d", node, "--all"],
                capture_output=True, text=True, timeout=3,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            if int(node.replace("/dev/video", "") or 0) % 2 == 0:
                out.append(node)
            continue
        txt = (r.stdout or "") + (r.stderr or "")
        if "Video Capture" in txt and "Can't" not in txt:
            out.append(node)
    return out


def _writable(d: str) -> bool:
    try:
        if not d or not os.path.isdir(d):
            return False
        t = os.path.join(d, ".racecar-write-test")
        with open(t, "w") as f:
            f.write("ok")
        os.remove(t)
        return True
    except OSError:
        return False


def _lsblk():
    r = subprocess.run(
        ["lsblk", "-J", "-o", "NAME,TRAN,TYPE,FSTYPE,MOUNTPOINT,PKNAME,RM"],
        capture_output=True, text=True, timeout=5,
    )
    if r.returncode != 0 or not r.stdout:
        return []
    try:
        return json.loads(r.stdout).get("blockdevices") or []
    except json.JSONDecodeError:
        return []


def _walk_usb_parts(devs, usb_disks=None, out=None):
    if usb_disks is None:
        usb_disks = set()
    if out is None:
        out = []
    for d in devs:
        name = d.get("name") or ""
        typ = d.get("type") or ""
        tran = (d.get("tran") or "") or ""
        if typ == "disk" and tran == "usb":
            usb_disks.add(name)
        parent = d.get("pkname") or ""
        on_usb = name in usb_disks or parent in usb_disks or tran == "usb"
        fstype = (d.get("fstype") or "").lower()
        mp = d.get("mountpoint") or ""
        if on_usb and typ in ("part", "disk") and fstype in (
            "vfat", "fat32", "exfat", "ext4", "ext3", "ntfs", "fuseblk"
        ):
            out.append({
                "dev": "/dev/" + name,
                "fstype": fstype,
                "mount": mp if isinstance(mp, str) else "",
            })
        kids = d.get("children") or []
        if kids:
            _walk_usb_parts(kids, usb_disks, out)
    return out


def _mount_usb(dev: str, fstype: str) -> str | None:
    slug = os.path.basename(dev)
    dest = f"/media/usbrec/{slug}"
    os.makedirs(dest, exist_ok=True)
    if os.path.ismount(dest):
        return dest if _writable(dest) else None
    opts = "rw,nofail"
    if fstype in ("vfat", "fat32", "exfat"):
        opts += ",uid=0,gid=0,umask=000"
    r = subprocess.run(
        ["mount", "-t", fstype if fstype != "fuseblk" else "ntfs3",
         "-o", opts, dev, dest],
        capture_output=True, text=True, timeout=8,
    )
    if r.returncode != 0:
        r = subprocess.run(["mount", "-o", "rw,nofail", dev, dest],
                           capture_output=True, text=True, timeout=8)
    if r.returncode != 0:
        sys.stderr.write(f"mount {dev}: {r.stderr}\n")
        return None
    return dest if _writable(dest) else None


def usb_dir(_preferred: str = "") -> str | None:
    """First writable USB mass-storage filesystem. Auto-mounts if needed."""
    parts = _walk_usb_parts(_lsblk())
    for p in parts:
        mp = p["mount"]
        if mp and mp not in ("/", "/boot", "/boot/firmware") and _writable(mp):
            return mp
    for p in parts:
        if p["mount"]:
            continue
        mp = _mount_usb(p["dev"], p["fstype"])
        if mp:
            sys.stderr.write(f"mounted {p['dev']} -> {mp}\n")
            return mp
    return None


def fmt_ms(ms: int) -> str:
    if ms < 0:
        return "--"
    s, ms = divmod(int(ms), 1000)
    m, s = divmod(s, 60)
    return f"{m}:{s:02d}.{ms // 10:02d}"


def hud_text(h: dict) -> str:
    rpm = int(h.get("rpm", 0) or 0)
    mph = int(h.get("mph_x10", 0) or 0) / 10.0
    lap = int(h.get("lap", -1) or -1)
    last_ms = int(h.get("last_ms", -1) or -1)
    pred_ms = int(h.get("pred_ms", -1) or -1)
    best_ms = int(h.get("best_ms", -1) or -1)
    oil = int(h.get("oil_x10", -1) or -1)
    clt = int(h.get("clt_x10", -1) or -1)
    afr = int(h.get("afr_x10", -1) or -1)
    l1 = f"{mph:5.1f} mph    {rpm:5d} rpm"
    lap_s = f"LAP {lap}" if lap >= 0 else "LAP --"
    l2 = f"{lap_s}   LAST {fmt_ms(last_ms)}   PRED {fmt_ms(pred_ms)}   BEST {fmt_ms(best_ms)}"
    oil_s = f"{oil / 10.0:.0f} psi" if oil >= 0 else "--"
    clt_s = f"{clt / 10.0:.0f}F" if clt >= 0 else "--"
    afr_s = f"{afr / 10.0:.1f}" if afr >= 0 else "--"
    l3 = f"OIL {oil_s}    TEMP {clt_s}    AFR {afr_s}"
    return l1 + "\n" + l2 + "\n" + l3 + "\n"


def write_hud(h: dict) -> None:
    tmp = HUD_PATH + ".tmp"
    with open(tmp, "w") as f:
        f.write(hud_text(h))
    os.replace(tmp, HUD_PATH)


def parse_hud(line: str) -> dict:
    # HUD,rpm,mph_x10,lap,last_ms,pred_ms,best_ms,oil_x10,clt_x10,afr_x10,lat,lon
    p = line.split(",")
    keys = ["_tag", "rpm", "mph_x10", "lap", "last_ms", "pred_ms", "best_ms",
            "oil_x10", "clt_x10", "afr_x10", "lat", "lon"]
    d = {}
    for i, k in enumerate(keys):
        if i == 0 or i >= len(p):
            continue
        d[k] = p[i].strip()
    return d


class Box:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.ser = None
        self.proc = None
        self.recording = False
        self.track = "UNKNOWN"
        self.viden = True  # record REC even before VIDEN,1 (Teensy gates)
        self.hud = {}
        self.last_uart = time.monotonic()
        self.front = cfg.get("FRONT") or ""
        self.back = cfg.get("BACK") or ""
        self.lock = threading.Lock()

    def send(self, line: str) -> None:
        msg = line if line.endswith("\n") else line + "\n"
        sys.stderr.write(">> " + msg)
        if self.ser is not None:
            try:
                self.ser.write(msg.encode("ascii", "replace"))
            except Exception as e:
                sys.stderr.write(f"uart tx: {e}\n")

    def open_uart(self):
        import serial  # python3-serial
        dev = which_uart(self.cfg["UART_DEV"])
        baud = int(self.cfg["UART_BAUD"])
        self.ser = serial.Serial(dev, baud, timeout=0.2)
        sys.stderr.write(f"uart {dev} {baud}\n")
        self.send(f"VID,HELLO,{VER}")

    def detect_cameras(self) -> None:
        nodes = v4l2_capture_nodes()
        cfg_f = self.cfg.get("FRONT") or ""
        cfg_b = self.cfg.get("BACK") or ""
        self.front = cfg_f if cfg_f and os.path.exists(cfg_f) else (nodes[0] if nodes else "")
        rest = [n for n in nodes if n != self.front]
        self.back = cfg_b if cfg_b and os.path.exists(cfg_b) else (rest[0] if rest else "")
        if self.back == self.front:
            self.back = ""

    def refresh_ready(self) -> None:
        self.detect_cameras()
        ncam = (1 if self.front else 0) + (1 if self.back else 0)
        usb_ok = 1 if usb_dir() else 0
        self.send(f"VID,READY,{ncam},{usb_ok}")

    def ffmpeg_cmd(self, out_path: str) -> list[str]:
        fps = self.cfg["FPS"]
        fs = self.cfg["FRONT_SIZE"]
        bs = self.cfg["BACK_SIZE"]
        pip_w, pip_h = self.cfg["PIP_W"], self.cfg["PIP_H"]
        m = self.cfg["PIP_MARGIN"]
        preset = self.cfg["X264_PRESET"]
        br = self.cfg["VIDEO_BITRATE"]
        font = FONT if os.path.isfile(FONT) else ""
        draw = (
            f"drawtext=textfile={HUD_PATH}:reload=1:x=24:y=24:"
            f"fontsize=36:fontcolor=white:borderw=3:bordercolor=black"
        )
        if font:
            draw = f"drawtext=fontfile={font}:" + draw[len("drawtext="):]
        cmd = [
            "ffmpeg", "-hide_banner", "-loglevel", "warning", "-y",
            "-f", "v4l2", "-input_format", "mjpeg", "-video_size", fs,
            "-framerate", fps, "-thread_queue_size", "64", "-i", self.front,
        ]
        if self.back:
            cmd += [
                "-f", "v4l2", "-input_format", "mjpeg", "-video_size", bs,
                "-framerate", fps, "-thread_queue_size", "64", "-i", self.back,
                "-filter_complex",
                f"[1:v]scale={pip_w}:{pip_h},setsar=1[pip];"
                f"[0:v][pip]overlay=W-w-{m}:H-h-{m}[v];"
                f"[v]{draw},format=yuv420p[out]",
                "-map", "[out]",
            ]
        else:
            cmd += ["-vf", f"{draw},format=yuv420p"]
        cmd += [
            "-c:v", "libx264", "-preset", preset, "-tune", "zerolatency",
            "-b:v", br, "-maxrate", br, "-bufsize", "4M", "-g", fps,
            "-pix_fmt", "yuv420p",
            "-movflags", "frag_keyframe+empty_moov+delay_moov",
            "-f", "mp4", out_path,
        ]
        return cmd

    def start_rec(self) -> None:
        with self.lock:
            if self.recording:
                return
            self.detect_cameras()
            if not self.front:
                self.send("VID,ERR,nocam")
                return
            dest = usb_dir()
            if not dest:
                self.send("VID,ERR,nousb")
                return
            write_hud(self.hud)
            safe = "".join(c if c.isalnum() or c in "._-" else "_" for c in self.track)[:24]
            ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
            name = f"vid_{ts}_{safe}.mp4"
            path = os.path.join(dest, name)
            cmd = self.ffmpeg_cmd(path)
            sys.stderr.write("exec " + " ".join(cmd) + "\n")
            try:
                self.proc = subprocess.Popen(cmd)
            except OSError as e:
                self.send(f"VID,ERR,ffmpeg")
                sys.stderr.write(f"ffmpeg spawn: {e}\n")
                return
            self.recording = True
            self.last_uart = time.monotonic()
            self.send(f"VID,REC,1,{name}")

    def stop_rec(self) -> None:
        with self.lock:
            if not self.recording:
                return
            proc = self.proc
            self.proc = None
            self.recording = False
        if proc is not None:
            proc.send_signal(signal.SIGINT)
            try:
                proc.wait(timeout=8)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=3)
        self.send("VID,REC,0")

    def handle(self, line: str) -> None:
        line = line.strip()
        if not line:
            return
        self.last_uart = time.monotonic()
        sys.stderr.write("<< " + line + "\n")
        if line.startswith("VIDEN,"):
            self.viden = line[6:7] != "0"
            if not self.viden:
                self.stop_rec()
        elif line.startswith("TRACK,"):
            t = line[6:].strip() or "UNKNOWN"
            self.track = t[:32]
        elif line.startswith("REC,"):
            want = line[4:5] != "0"
            if want:
                self.start_rec()
            else:
                self.stop_rec()
        elif line.startswith("HUD,"):
            self.hud = parse_hud(line)
            write_hud(self.hud)

    def watchdog(self) -> None:
        if self.recording and (time.monotonic() - self.last_uart) > 15:
            sys.stderr.write("uart silent 15s — stopping recording\n")
            self.send("VID,ERR,uart_timeout")
            self.stop_rec()


def main() -> int:
    cfg = load_env()
    box = Box(cfg)
    write_hud({})
    try:
        box.open_uart()
    except Exception as e:
        sys.stderr.write(f"uart open failed: {e}\n")
        return 1
    box.refresh_ready()

    stop = {"n": False}

    def _sig(_s, _f):
        stop["n"] = True
    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    buf = b""
    last_ready = time.monotonic()
    while not stop["n"]:
        box.watchdog()
        now = time.monotonic()
        if now - last_ready > 5 and not box.recording:
            box.refresh_ready()
            last_ready = now
        try:
            chunk = box.ser.read(256) if box.ser else b""
        except Exception as e:
            sys.stderr.write(f"uart rx: {e}\n")
            time.sleep(0.5)
            continue
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.replace(b"\r", b"").decode("ascii", "replace")
            box.handle(line)
        if len(buf) > 4096:
            buf = b""
    box.stop_rec()
    return 0


if __name__ == "__main__":
    sys.exit(main())

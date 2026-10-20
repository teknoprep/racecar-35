#!/usr/bin/env python3
"""Regenerate the server's mirror of the dash's TRACKS[] table.

`server/app/sf_picker.html` embeds `const TRACKS = [...]`. That single mirror
feeds BOTH the S/F picker's dropdown and the server's known-track list
(`GET /tracks`, used by the review page's "edit session" track picker), so it
must be a mechanical copy of `crowpanel-arduino/RaceDash/RaceDash.ino` — not a
hand-maintained list. This script rewrites it from the firmware.

WHY the `cfgs` field exists (v0.1.177): the dash writes a session's track as
"<base> <config>" for every track that has configs (see confirmConfigAndStart()
in RaceDash.ino), i.e. the names a driver actually sees are
"Summit Point Main", "Mid-Ohio Full + Chicane", "Watkins Glen Grand Prix", …
A mirror holding only the base names made the server offer/print names the dash
never produces. `cfgs` carries the config names so the server can expand the
base name into exactly the dash's set.

Usage:
  tools/gen_dash_tracks.py            # rewrite the mirror
  tools/gen_dash_tracks.py --check    # exit 1 if the mirror is stale (tests)
"""
from __future__ import annotations

import json
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[1]
DASH = ROOT / "crowpanel-arduino" / "RaceDash" / "RaceDash.ino"
PICKER = ROOT / "server" / "app" / "sf_picker.html"

CFG_DECL = re.compile(
    r"static\s+const\s+TrackConfig\s+(\w+)\s*\[\]\s*=\s*\{(.*?)\};", re.S)
TRACKS_BLOCK = re.compile(
    r"static\s+const\s+TrackInfo\s+TRACKS\s*\[\]\s*=\s*\{(.*?)\n\};", re.S)
CFG_ROW = re.compile(r'\{\s*"([^"]+)"')
ROW = re.compile(r'\{\s*"([^"]+)"\s*,([^{}]*)\}', re.S)
MIRROR_RE = re.compile(r"^[ \t]*const TRACKS = \[.*?\];", re.S | re.M)


def _num(tok: str) -> float:
    return float(tok.strip().rstrip("fF"))


def firmware_tracks(src: str | None = None) -> list[dict]:
    """Parse TRACKS[] (+ the *_CFGS[] arrays) out of RaceDash.ino."""
    src = DASH.read_text(encoding="utf-8") if src is None else src
    cfgs: dict[str, list[str]] = {}
    for m in CFG_DECL.finditer(src):
        cfgs[m.group(1)] = CFG_ROW.findall(m.group(2))
    block = TRACKS_BLOCK.search(src)
    if not block:
        raise SystemExit("TRACKS[] block not found in %s" % DASH)
    out: list[dict] = []
    for m in ROW.finditer(block.group(1)):
        name = m.group(1)
        args = [a.strip() for a in m.group(2).split(",")]
        if len(args) < 6:
            raise SystemExit("cannot parse TRACKS[] row for %r: %r" % (name, args))
        entry = {
            "name": name,
            "lat": _num(args[0]),
            "lon": _num(args[1]),
            "r": _num(args[2]),
            "sfa": [_num(args[3]), _num(args[4])],
            # args: lat,lon,r,sf_lat,sf_lon, configs_sym, n_configs, sf_lat2,sf_lon2, tz, aux
            "sfb": [_num(args[7]), _num(args[8])] if len(args) > 8 else [0.0, 0.0],
        }
        cfg_sym = args[5] if len(args) > 5 else "nullptr"
        aux = args[10].strip() if len(args) > 10 else "0"
        entry["aux"] = bool(int(aux)) if re.fullmatch(r"\d+", aux) else False
        names = cfgs.get(cfg_sym, []) if cfg_sym != "nullptr" else []
        if names:
            entry["cfgs"] = names
        out.append(entry)
    return out


def session_names(tracks: list[dict]) -> list[str]:
    """Every track name the dash can write to a filename, in firmware order."""
    out: list[str] = []
    for t in tracks:
        base = str(t.get("name") or "")
        if not base:
            continue
        out.append(base)                      # the picker label / pre-config sessions
        for cfg in t.get("cfgs") or []:
            out.append(base + " " + cfg)      # what confirmConfigAndStart() sends
    return out


def mirror_line(tracks: list[dict], indent: str = "  ") -> str:
    return indent + "const TRACKS = " + json.dumps(tracks, separators=(",", ":")) + ";"


def main() -> int:
    tracks = firmware_tracks()
    html = PICKER.read_text(encoding="utf-8")
    m = MIRROR_RE.search(html)
    if not m:
        raise SystemExit("const TRACKS = [...] not found in %s" % PICKER)
    indent = re.match(r"[ \t]*", m.group(0)).group(0)
    line = mirror_line(tracks, indent)
    current = m.group(0)
    stale = current != line
    names = session_names(tracks)
    if "--check" in sys.argv[1:]:
        if stale:
            print("STALE: %s does not match %s" % (PICKER.relative_to(ROOT),
                                                   DASH.relative_to(ROOT)))
            print("       run tools/gen_dash_tracks.py")
            return 1
        print("ok: %d tracks, %d session names" % (len(tracks), len(set(names))))
        return 0
    if stale:
        PICKER.write_text(html[:m.start()] + line + html[m.end():], encoding="utf-8")
        print("wrote %s (%d tracks)" % (PICKER.relative_to(ROOT), len(tracks)))
    else:
        print("already current (%d tracks)" % len(tracks))
    for n in names:
        print("  " + n)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

# Rev C — actual electrical engineering prototype

**The requested Gerbers and separate PTH/NPTH drills now exist.** These are not
pads exported from the former rendering: the board has **136 PCB parts, 76 nets,
routed copper and matching native schematic**.

## Deliverables

- **`Racecar-RevC-GERBERS-ENGINEERING-PROTOTYPE.zip`** — 9 Gerber layers, separate
  plated/non-plated drills and maps, schematic PDF, selected BOM, assembly extras,
  placement CSV, fabrication/assembly/bring-up notes, editable KiCad sources,
  validation reports, assembled preview and SHA256 manifest.
- **`REV-C-ASSEMBLED-ENGINEERING-PROTOTYPE.png`** — view of this actual routed PCB.
  Module/part bodies are illustrative, not mechanical fit certificates.
- `design/` — authoritative electrical CAD and manufacturing documents.
- `electrical/` — generation, routing, finishing, verification and rendering scripts.

**Status: UNBUILT ENGINEERING PROTOTYPE, not independently approved or physically
validated.** Full ERC and DRC report zero violations and zero unconnected items;
392 connected pads match the schematic; 427 pin/geometry/calculation assertions pass.
These do NOT establish 5 A thermal capability, RF performance, component authenticity,
automotive qualification or a fully working firmware product.

## Important differences from the earlier concept

- Left terminal bank: **J3 TACH / J4 OIL / J5 NTC / J6 AEM / J1 POWER / J2 SCREEN**.
  Names and exact pin order are in `design/ASSEMBLY.md`; they agree with the netlist.
- Separate AUX and NET **TRACO TSR 1-2433** regulators run from Pololu protected
  input (VRP), not from marginal 5 V or the Teensy's 3V3 output.
- C1 polarized input bulk is **after reverse-polarity protection** on VRP.
- Oil/AEM/NTC inputs have independent clamps, RC filtering and TMUX1511 isolation.
  **Oil gain is 0.500; coolant uses 4.096 V / 2.49k excitation and 0.500 sensing.**
  Existing firmware still needs matching oil/coolant conversions and sender calibration.
- WiFi is the internal-antenna WROOM-1-N8R2, with buffered SPI and protected reset/
  service interfaces. The new coprocessor software/upload transport is NOT implemented.
- CR2032 feeds Teensy VBAT through **J9 and a short removable auxiliary-pad lead**.
  The outer-row sockets do not carry VBAT. Fresh-time/validity firmware work remains.
- MPU-6050 is selected to match the existing driver, but genuine authorized stock
  is a procurement/lifecycle gate; do not substitute a different IMU without revision.

## Export and reproducibility

Re-export the current completed CAD, without regenerating/rerouting it:

```sh
/usr/bin/python3 hardware/teensy-integrated-revc/electrical/verify.py
/usr/bin/python3 hardware/teensy-integrated-revc/fabrication/export.py --engineering-prototype
```

That explicit mode makes no human-approval claim. It still requires all documents,
real nets/tracks, pin contracts, clean full-severity ERC/DRC, no unconnected items,
exact connected-pad equality, matching render identity, all manufacturing layers,
separate drills and ZIP/hash verification. Default mode instead requires a real,
named, hash-pinned independent review. **Never fabricate an approval record.**

**Do not casually run `electrical/generate.py`: it overwrites the routed PCB.**
The reproducible development sequence is generator → schematic → route.py (locked
power/RF + DSN) → Freerouting 1.9.0 (`-mp 50`) → route.py `--finish` →
complete_routes.py (documented final two connections) → finish_artwork.py → full
checks → render.py → export. The saved native PCB is the authoritative result;
a differently optimized router result requires reviewing the manual-finishing step.
KiCad 9.0.8/system Python pcbnew are used. Rendering uses downloaded KiCad WRL bodies
and the explicitly approximate original module models from the archived carrier.

The old `preview/`, `handoff/`, `REV-C-INTEGRATED-PREVIEW.png`, concept connection
SVG and design-review RFQ ZIP are retained as history, **not current fabrication
sources**. Their candidate/unfinished-circuit wording is superseded by `design/`.
The same applies to earlier requirements where they differ from these selected
circuits. A/B remain archived, not alternate integrated boards.

No firmware version bump, flash, OTA publication, purchase or fabrication order was
performed by this hardware export. Read `design/BRINGUP.md` before applying power.

# Racecar-35 Rev D — PCBWay order guide

Everything in this folder is ready to upload. **Read the "Before you click Buy"
section at the end — there are two items PCBWay's DFM check may push back on.**

Revision D · four-layer · 150.05 × 155.05 mm · 158 placements · 89 nets

---

## Files in this folder

| File | What it is |
|---|---|
| `Racecar-RevD-PCBWAY-GERBERS.zip` | 16 files: 4 copper layers, both masks, both silkscreens, both pastes, `Edge.Cuts`, separate **PTH** and **NPTH** Excellon drills, drill maps, `.gbrjob`. This is the file you upload. |
| `Racecar-RevD-BOM-pcbway-assembly.csv` | Turnkey-assembly BOM in PCBWay's column layout. |
| `Racecar-RevD-BOM-purchasing.csv` | 68 unique parts aggregated by MPN with references — use this if you buy parts yourself. |
| `Racecar-RevD-CPL.csv` | Pick-and-place, origin at board lower-left, Y positive up, 158 rows. |
| `PARTS-AVAILABILITY-REVIEW.md` | **Read before replying to the PCBWay quote.** Six BOM lines must change, two capacitor subs need a counter-proposal, plus verified where-to-buy for the 12 consignment parts. |
| `BOM-CHANGES.csv` | The same change list in machine-readable form. |

> **⚠️ The BOM files in this folder are CORRECTED to BOM revision 1**
> (`1206L010/30YR` → `1206L010/60WR`, `PESD3V3U1UL,315` → `PESD5V0F1BL,315`,
> bare `SS14` → onsemi `SS14`, and three Murata caps with no stock → Yageo/Samsung
> X7R equivalents). The file to send to PCBWay is
> **`Racecar-RevD-BOM-pcbway-assembly-REV1.csv`** — it is the corrected BOM with
> **U1 (Teensy 4.1) and U2 (Pololu 4091) removed**, because we fit those two
> modules ourselves. The full BOM with both modules still listed (marked
> "CUSTOMER SUPPLIED") is `Racecar-RevD-BOM-pcbway-assembly.csv`. Reply document:
> **`PCBWAY-REPLY-2026-09-29.pdf`**. Background: `PARTS-AVAILABILITY-REVIEW.md`.

The full engineering package (schematic PDF, editable CAD, reports, manifest) is
the parent folder's `Racecar-RevD-GERBERS-ENGINEERING-PROTOTYPE.zip`.

---

## Step 1 — Order the bare PCB

Go to **pcbway.com → PCB Instant Quote → Quote Now**, then **Upload Gerber File**
and select `Racecar-RevD-PCBWAY-GERBERS.zip`.

PCBWay will auto-detect the size, layer count and outline. **Check these against
the table before continuing** — if auto-detection disagrees, stop and look at
why rather than clicking past it.

| Setting | Value | Why |
|---|---|---|
| Board type | **FR-4** | |
| Layers | **4** | Auto-detected. `In1.Cu` is the ground plane. |
| Dimensions | **150.05 × 155.05 mm** | Auto-detected. |
| Quantity | 5 (typical minimum) | |
| Thickness | **1.6 mm** | Matches the native stackup. |
| Min track / spacing | **0.20 mm / 0.15 mm** | The design's real minimums. |
| Min hole size | **0.30 mm** | The via drill. Within standard for 4 layers. |
| Copper weight | **1 oz** | The screen branch is now fused at **3 A**, so 1 oz is adequate — see below. |
| Surface finish | **ENIG** | Specified in the design. |
| Solder mask | **Green** | |
| Silkscreen | **White** | |
| Impedance control | **Yes** | The GNSS feed needs 50 Ω. |
| Castellation | No | |
| Edge plating | No | |
| Gold fingers | No | |

### About the outline

The board profile includes a **48 × 6 mm WiFi antenna notch** cut into the bottom
edge. It is part of `Edge.Cuts` and is routed as normal board profile — you do
**not** need to order an internal slot. Four **3.2 mm NPTH** mounting holes are in
the NPTH drill file.

Leave the "Panel by PCBWay"/panelisation option **off** — this board has connectors
on one edge and a battery holder; a v-cut or tab panel will make assembly harder,
not easier.

---

## Step 2 — Decide how it gets assembled

**This board is mostly hand assembly.** Only 46 of the 68 unique parts are normal
SMD. The other 22 are modules, screw terminals and through-hole parts that
pick-and-place machines don't handle.

| Approach | What PCBWay does | What you do |
|---|---|---|
| **A — Bare PCB (recommended)** | Fabricates the board only | Buy parts from the purchasing BOM; hand-solder everything. |
| **B — Partial turnkey** | Machine-places the SMD passives and ICs | Hand-solder the 22 modules/connectors/THT parts. |
| **C — Full turnkey** | Would need every part consigned | Not practical — see below. |

**Recommendation: start with A.** It is the cheapest, it removes all part-availability
risk, and this board was designed for hand assembly of the connectors and modules.

If you want **B**, use PCBWay's **PCB Assembly → Turnkey** flow and upload
`Racecar-RevD-BOM-pcbway-assembly.csv` plus `Racecar-RevD-CPL.csv`. Their BOM tool
matches your MPNs to LCSC stock; **check every match it proposes** against the
MPN and package in the purchasing BOM. Expect some to be unmatched.

**Why C doesn't work:** these parts are not LCSC line items and would have to be
consigned (shipped to PCBWay) or sourced by them at a premium —

Teensy 4.1 · Pololu D36V50F5 · 2× TRACO TSR 1-2433 · u-blox NEO-M9N-00B ·
ESP32-S3-WROOM-1-N8R2 · TDK ICM-42670-P · Amphenol 901-143 SMA · Keystone 1058
holder · 8× Phoenix MKDS terminals · VO617A-3 optocoupler

---

## Step 3 — Ordering the parts yourself (approach A)

`Racecar-RevD-BOM-purchasing.csv` is grouped by MPN with quantities and reference
designators. Sourcing notes:

- **Passives (Yageo 0805, Murata 0805)** — Digi-Key / Mouser / LCSC. Note the BOM
  marks the analog divider resistors as **0.1 % thin-film** and the 150 Ω / 2.49 kΩ
  as precision. Do not substitute 1 % parts there; the analog front end depends on
  the ratio.
- **Teensy 4.1** — PJRC direct. Buy two 1×24 2.54 mm **female** headers as well
  (they are not in the BOM as a purchasable line — see assembly notes).
- **ICM-42670-P** — buy genuine, not a marketplace re-mark.
- **36 of the 158 placements are not purchased parts**: 5 test pads, 4 mounting
  holes, and the Pololu/TRACO modules' mechanical lines.

---

## Step 4 — Assembly notes that will bite if ignored

1. **Socket the Teensy — do not solder it.** Two 1×24 2.54 mm female headers go on
   the board; matching male headers go on the Teensy. This is what makes it
   removable and repairable.
2. **Cut the Teensy's VUSB–VIN bridge before USB and car power are ever both
   connected.** JP1 is *not* a substitute. Leave JP1 open until the rails have
   been tested unloaded.
3. **Fit the CR2032 cell last**, after reflow and cleaning. It is a primary cell —
   never reflow it, never solder a non-tabbed cell, never substitute a rechargeable
   LIR2032.
4. **J9 is a hand-made lead**, not a socket contact. It runs to the Teensy's
   labelled auxiliary **VBAT and GND pads**. The outer socket rows do not carry
   VBAT — do not hunt for it there.
5. **Fit JP1's shunt only after bring-up.**

---

## Before you click Buy — the two previously-open items are now resolved

### 1. Copper weight — settled at 1 oz

The screen branch fuse is **3 A** (`F2`, Littelfuse `0451003.MRL`). The
`+5V_MAIN` trunk is 3.5 mm wide, and at 1 oz / 35 µm that is adequate for the
fused screen load plus the Teensy and the three 100 mA sensor branches.

**Order 1 oz outer copper** — it is PCBWay's standard, cheaper, and no longer a
marginal call. Do **not** raise the screen fuse back to 4 A on this build.

### 2. GNSS feed — corrected, and the target is now explicit

The feed was **0.80 mm wide, which is about 30 Ω — badly wrong.** It has been
recalculated as a **microstrip over the In1.Cu plane**:

| Prepreg L1→L2 (H) | W for 50 Ω at εr 4.4 |
|---|---|
| 0.10 mm | 0.19 mm |
| 0.15 mm | 0.29 mm |
| **0.20 mm** | **0.38 mm** ← what the board now uses |
| 0.25 mm | 0.48 mm |
| 0.36 mm | 0.69 mm |

Validated with the Wheeler/Hammerstad closed form (agrees to <1 % against the
parallel-plate limit). **The board is set to 0.38 mm, which is 50 Ω if the
prepreg to `In1.Cu` is 0.20 mm.**

The trace still has the F.Cu ground pour alongside it at 0.15 mm, which makes it
partly coplanar and pulls the real impedance **below** 50 Ω. So:

- **Order with impedance control ON**, and tell them the target: **50 Ω
  single-ended on `RF_ANT`/`RF_GNSS`, from J8 to the NEO-M9N RF_IN.**
- **Ask for their finished stackup first** (prepreg thickness to L2 and Dk). If it
  is not 0.20 mm, the width must change — the table above gives the new value.

The standard 4-layer 1.6 mm construction is one 7628 prepreg each side of a
1.065 mm core; it sums to 1.596 mm, which is the 1.6 mm finished board. That fixes
H = 0.1955 mm, and the routed 0.38 mm gives **49.6-50.6 ohm** across eps_r 4.2-4.6.
Nothing further needs deciding.

---

## Checklist before ordering

- [ ] Gerber zip uploaded; auto-detected layers = **4**, size = **150.05 × 155.05 mm**
- [ ] Copper weight **1 oz** (screen fuse is 3 A)
- [ ] **ENIG** selected (not HASL)
- [ ] **Impedance control ON**, GNSS feed target 50 Ω stated in the order notes
- [ ] NPTH drill file present so the 3.2 mm mounting holes are not plated
- [ ] Antenna notch visible in the Edge.Cuts preview
- [ ] Panelisation **off**
- [ ] DFM report read — not just accepted

---

## Standing caveat

This is an **unbuilt engineering prototype**. It has passed ERC, full-severity DRC
and netlist verification (0 violations, 0 unconnected, 456 pads matching the
schematic, 531 pin/geometry assertions) — but it has **not** been independently
reviewed or physically validated, and **no firmware exists for the ICM-42670-P,
throttle, brake or WiFi coprocessor yet**.

Clean CAD is not a working board. Review the DFM report, and bench-test before it
goes near a car.

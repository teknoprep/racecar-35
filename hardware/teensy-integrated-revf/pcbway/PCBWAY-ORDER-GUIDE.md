# Racecar-35 Rev F — PCBWay order guide

Everything needed to quote and order is in this folder. **Read "Before you click Buy"
at the end — it lists the things PCBWay's DFM check will ask about.**

Revision F · four-layer · 150.05 × 155.05 mm · 193 placements · 104 nets.

> ⚠️ **This is an UNAPPROVED engineering prototype.** The CAD passes ERC/DRC/unconnected
> at zero and the pin contract is asserted by script, but there is **no independent
> human review and no physical, thermal or RF validation**. Ordering prototypes is
> exactly what that status permits; winning races is not.

---

## Files in this folder

| File | What it is |
|---|---|
| `Racecar-RevF-PCBWAY-GERBERS.zip` | **This is the file you upload for the bare board.** 16 files: 4 copper layers, both masks, both silkscreens, both pastes, `Edge.Cuts`, separate **PTH** and **NPTH** Excellon drills with maps, and the `.gbrjob`. |
| `Racecar-RevF-BOM-pcbway-assembly.csv` | Turnkey-assembly BOM in PCBWay's column layout, with the "fit this, not that" instructions in the Comment column. |
| `Racecar-RevF-CPL.csv` | Pick-and-place, origin at the board lower-left, Y positive up, 182 rows (mounting holes and test pads are deliberately absent — nothing to place). |
| `REVF-CHANGES-AND-ORDER.md` | What changed since Rev D/E and why (the CAN fix, the 5 V rail, the input monitor). |
| `make_package.py` | Regenerates the three files above from `components.json` + the routed PCB. |

The full engineering package (schematic PDF, editable CAD, reports, manifest) is the
parent folder's `Racecar-RevF-GERBERS-ENGINEERING-PROTOTYPE.zip`. The fab file you
upload is the gerber-only ZIP above; **every order setting still comes from this guide.**

---

## Step 1 — Order the bare PCB

**pcbway.com → PCB Instant Quote → Quote Now → Upload Gerber File**, select
`Racecar-RevF-PCBWAY-GERBERS.zip`. PCBWay auto-detects size, layer count and outline —
**check these against the table before continuing**; if auto-detection disagrees, stop and
find out why.

| Setting | Value | Why |
|---|---|---|
| Board type | **FR-4** | |
| Layers | **4** | Auto-detected. `In1.Cu` is a continuous ground plane. |
| Dimensions | **150.05 × 155.05 mm** | Auto-detected. |
| Quantity | 5 (minimum) | |
| Thickness | **1.6 mm** | Matches the native stackup. |
| Min track / spacing | **0.20 mm / 0.15 mm** | The design's real minimums. |
| Min hole size | **0.30 mm** | The via drill. Standard for 4 layers. |
| Copper weight | **1 oz outer** | The screen branch is fused at **3 A** (`F2`), which is what makes 1 oz adequate. |
| Surface finish | **ENIG** | |
| Solder mask | **Green** | |
| Silkscreen | **White** | |
| Impedance control | **Yes** | The GNSS feed needs 50 Ω. |
| Castellation / edge plating / gold fingers | **No** | |

### About the outline
The profile includes a **48 × 6 mm WiFi antenna notch** cut into the bottom edge — it is
part of `Edge.Cuts`, just normal profile routing, **not** an internal slot. Four
**3.2 mm NPTH** mounting holes are in the NPTH drill file. Leave PCBWay's panelisation
**off** (connectors on one edge + a battery holder).

---

## Step 2 — Turnkey assembly: fit this, NOT that

Upload `Racecar-RevF-BOM-pcbway-assembly.csv` **and** `Racecar-RevF-CPL.csv` in PCBWay's
**Assembly → Turnkey** flow. The BOM Comment column carries the rules; the important ones:

| Ref | Fit | Do **NOT** fit |
|---|---|---|
| **U1** | 2× 1×24 2.54 mm THT female socket (`SSW-124-01-G-S`) | **a Teensy** — it is customer supplied, bought with pins, plugged in |
| **U2** | the soldered TI `TPS54560BDDAR` buck | — (there is no Pololu module and **no socket** at U2) |
| **JP1 / JP2** | the two 2-pin headers (`TSW-102-07-G-S`) | **the 2.54 mm shunts** — customer fits JP1 after rail tests, JP2 only at a bus end |
| **BT1** | the Keystone `1058` holder | **the CR2032 cell** — customer fits it after reflow/cleaning (primary cell, never charge) |
| **J9** | the JST `B2B-XH-A` connector | the two-wire RTC lead — hand-made by the customer to the Teensy VBAT/GND pads |

Check every MPN/package PCBWay matches against LCSC before paying — expect a few to be
unmatched and to need the substitutes already agreed (see `REVF-CHANGES-AND-ORDER.md` §4).

### Customer-supplied / hand-fitted (not ordered here)
Full list in `../design/ASSEMBLY-EXTRAS.csv`: Teensy 4.1 + its two male strips, the JP1/JP2
shunts, the CR2032 cell, the J9 RTC lead (housing + contacts + wire), M3 standoffs, and the
3.3 V active GPS antenna.

---

## Step 3 — CAN (new in Rev F)

The external CAN module and its `J7` header are **gone** — don't expect them in the
Gerbers or BOM. In their place the board is built with a soldered **`TCAN1042HGV-Q1`**
(`U21`) whose `STB` pin is tied to GND, a **`NUP2105L`** bus TVS, a third Phoenix
**`1729021`** field terminal (`J14` = **1 CANH / 2 CANL / 3 GND**) and a switchable
**split 120.8 Ω** termination behind the **`JP2`** shunt.

⚠️ **`U21` must not be substituted.** Order `TCAN1042HGVDRQ1` from **authorized
distribution only**, and do not swap it without written approval — no clones, no re-marked
parts. The reason is a bench-proven one: the module this replaces was **mislabelled** (a
TJA1051T/3-class chip sold as an SN65HVD230), and the pinout difference caused the failure.
Approved alternates, in order: `TCAN1042VDRQ1`, then NXP `TJA1051T/3/1J` (its pin 8 `S` must
also go to GND). The **`V` suffix is mandatory** — `TCAN1042DRQ1` / `TCAN1042HDRQ1` /
`TCAN1042GDRQ1` have **pin 5 = NC**, which would put 5 V on Teensy pin 23 and destroy it.
**Never** an SN65HVD23x, a plain `TJA1051T` (no `/3`), a `TJA1050`, or a "pin-compatible"
clone.

**When the boards arrive, check `U21`'s top marking against TI's *Device Marking* for
`TCAN1042HGVDRQ1` before power-up and reject on mismatch** (procedure in
`../design/BRINGUP.md`).

CAN is **not built or measured**. Bring-up acceptance (U21 VCC/VIO/STB voltages, ~60 Ω at
J14 with the jumper, a CANable `CANTX,10` test) is in `../design/BRINGUP.md`.

---

## Before you click Buy

1. **Quote, don't guess.** Send the quote to us first — a wrong layer stack or copper
   weight is expensive to discover after fabrication.
2. **U1 is a socket pair, not a Teensy.** If your BOM tool flags `SSW-124-01-G-S`, that is
   correct and intended.
3. **Do not fit the shunts or the CR2032 cell.**
4. **The board is not panelised** on purpose.
5. **No independent review has happened.** Do not present this package as reviewed or
   production-qualified.

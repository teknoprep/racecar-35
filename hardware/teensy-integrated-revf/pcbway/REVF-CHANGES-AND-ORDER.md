# Rev F — what changed, and what to order

**This replaces the Rev D quotation (`T-2SJ3W1113248A`) and the Rev E files.** The board
is the same size, the same 4 layers and the same process — **please re-quote the same
settings against the new Gerbers and BOM**, which are in this folder.

## 1. NEW: the CAN transceiver is ON THE BOARD (`J7` and its module are deleted)

On the bench the external module that was going to be fitted at `J7` **received but never
drove the bus**: it never ACKed, so the sender retransmitted ~3,800 identical frames/s.
**The root cause was NOT the transceiver concept — the module was mislabelled.** Sold as an
"SN65HVD230", it carries a **TJA1051T/3-class** chip (same footprint, different pins): pin 3
VCC needs **5 V** (the driver locks out below 4.5 V), pin 5 is **VIO** not Vref, pin 8 is
**S/standby**. Driven the SN65HVD230 way — VCC 3.3 V, pin 5 floating, pin 8 floating — the
driver was locked out and floating VIO was phantom-fed from TXD (~2.1 V), so RXD collapsed
whenever TXD went low; every Teensy-side self-test still looked like a working transmitter.
Rewired VCC 5 V / pin 5 3.3 V / pin 8 GND it ran at exactly 200 frames/s, both ids, 0
errors. Full analysis: `../../CAN-TRANSCEIVER-FINDINGS-2026-10-04.md`.

**Delete `J7` and do not fit any CAN module.** A plug-in board can carry the wrong chip and
be wired three ways wrong; the soldered transceiver below has its supplies and mode pin
hard-wired, so none of that can happen.

| Ref | Function | MPN | Manufacturer |
|---|---|---|---|
| U21 | CAN transceiver, VIO = 3.3 V, ±70 V bus fault, SOIC-8 | `TCAN1042HGVDRQ1` | Texas Instruments |
| D25 | Dual CAN bus TVS, SOT-23 | `NUP2105LT1G` | onsemi |
| J14 | CAN field terminal, 3-pos 5.08 mm (same family as J4/J11/J12) | `1729021` | Phoenix Contact |
| JP2 | CAN termination jumper, 2-pin (Samtec TSW-102 + shunt, as JP1) | `TSW-102-07-G-S` | Samtec |
| R58 R59 | Split-termination legs, 60.4 Ω 1 % 0805 | `RC0805FR-0760R4L` | Yageo |
| C49 C50 | 100 nF X7R 0805 decoupling at VCC / VIO | `GRM21BR71H104KA01L` | Murata |
| C51 | 1 µF 0805 bulk on VCC | `GRM21BR71H105KA12L` | Murata |
| C52 | 4.7 nF 50 V X7R 0805, termination midpoint | `CL21B472KBANNNC` | Samsung |

`U21` pin 8 (`STB`) is tied **directly to GND with no resistor** — `STB` high is standby /
receive-only, never ACKs. `J14` is **1 CANH / 2 CANL /
3 GND**. The split termination (`R58`+`R59` = 120.8 Ω across the pair, `C52` from the
midpoint to GND) is enabled by the `JP2` shunt and **must only be fitted when this board
is an end of the bus**. `TP6`/`TP7` are `CAN_TX`/`CAN_RX` test points, and the former
CAN-module header `J7` is gone from the Gerbers, CPL and BOM.

Orderable alternates for U21, in order: `TCAN1042VDRQ1`, then NXP `TJA1051T/3/1J` (its
pin 8 `S` must also go to GND).

⚠️ **No unapproved substitution of U21.** Order `TCAN1042HGVDRQ1` from **authorized
distribution only** (TI / Mouser / Digi-Key / LCSC-original) and do **not** substitute
without written approval — no clones, no re-marked parts. The **`V` suffix is mandatory**:
`TCAN1042DRQ1` / `TCAN1042HDRQ1` / `TCAN1042GDRQ1` have **pin 5 = NC**, which puts 5 V on
Teensy pin 23 and destroys it. **Never** an SN65HVD23x, a plain `TJA1051T` (no `/3`), a
`TJA1050`, or any "pin-compatible" equivalent.

This applies because the bench failure was a **mislabelled part** (a TJA1051T/3-class chip
sold as an SN65HVD230): correct marking is the whole defence here.

All eight new lines are standard **LCSC / TI / onsemi / Phoenix Contact / Samtec / Yageo /
Murata / Samsung** stock parts — no consignment and no customer-supplied module, exactly
like the rest of the board.

## 2. The Pololu module is GONE (this removes both sourcing problems you reported)

U2 is no longer a Pololu `4091 / D36V50F5` module and no socket is needed.
**Do not source or fit the Pololu 4091, and do not fit any socket at U2.**
It is replaced by a converter we designed from parts you can buy from stock:

| Ref | Function | MPN | Manufacturer |
|---|---|---|---|
| U2 | 5 V / 5 A buck regulator, 400 kHz | `TPS54560BDDAR` | Texas Instruments |
| L2 | 6.8 µH, 13.5 A sat / 8 A RMS shielded inductor | `IHLP4040DZER6R8M01` | Vishay |
| D23 | 5 A / 60 V catch diode (**SMB / DO-214AA only**) | `SS56` (BORN, SMB) — LCSC C2687867; alternate Diotec `SK56` | BORN |
| Q3 | P-channel reverse-polarity protection, 40 V, 15 mΩ | `AO4485` | Alpha & Omega |
| C41 C42 | 10 µF 50 V X7R 1210 input ceramics | `GRM32ER71H106KA12L` | Murata |
| C44 C45 C46 | 47 µF 16 V X5R 1210 output ceramics | `GRM32ER61C476KE15L` | Murata |
| D24 | 15 V gate clamp (SOT-23) | `BZX84C15` | Nexperia |
| R51–R57, C43, C47, C48 | passives | see BOM | — |

Everything is a standard LCSC/TI/Vishay/Murata part — **no consignment and no module
fitting.** U1 is unchanged: the assembly BOM asks for **two 1×24 sockets
(`SSW-124-01-G-S`)** — please source and fit those and **do NOT fit a Teensy** (the Teensy
4.1 is customer supplied, bought with pins, and plugs in). **Do not fit the JP1/JP2 shunts
or the CR2032 cell** either. Order settings and the full fit/no-fit list are in
`PCBWAY-ORDER-GUIDE.md`.

## 3. The car's input voltage is measured on the board

A sixth analog channel (a 180 k/20 k 0.1 % divider with its own 3.0 V shunt clamp and
filter, on the spare TMUX1511 switch) taps the protected car input. New refs R48, R49, R50,
D21, D22, C40 — all ordinary parts.

## 4. Other corrections already agreed

* **D5** = `PESD5V0F1BL,315` (your 09-30 quote reverted to the unavailable `PESD3V3U1UL,315` — please use the replacement).
* C10, C18, C11 substitutions as previously agreed; F3–F5 = `1206L010/60WR`; D2 = onsemi `SS14`.
* **Delete the two 1×24 male pin strips** (`TSW-124-07-G-S`) — we supply the Teensy with pins.
* U8 `NEO-M9N-00B`: please confirm the final price before production; genuine TRACO `TSR 1-2433` only.

## 5. Files in this folder

* `Racecar-RevF-PCBWAY-GERBERS.zip` — 16 files: 11 Gerbers, `Edge_Cuts`, PTH + NPTH drills
  with maps, and the job file. Board is **150.05 × 155.05 mm**, 4 layer, 1.6 mm, **1 oz outer**,
  ENIG, impedance control ON (50 Ω single-ended GNSS feed), green mask, white silk,
  **no panelisation** (connectors and a battery holder sit on the edges).
* `Racecar-RevF-BOM-pcbway-assembly.csv` — grouped BOM with designators.
* `Racecar-RevF-CPL.csv` — placement positions (Top layer, millimetres, Y already flipped to
  PCBWay's convention).
* `PCBWAY-ORDER-GUIDE.md` — **read this before quoting**: board settings, the fit/no-fit
  list for U1 / JP1 / JP2 / BT1 / J9, and the CAN notes.

## 6. Verification status (CAD only)

ERC **0**, DRC **0**, unconnected **0**, 749 pin/geometry/calculation assertions passed,
538 schematic↔PCB connected pads agree, 193 footprints / 104 nets / 1,647 routed segments
with 131 vias. This is a CAD check, **not** a physical or thermal validation, and it is not
an independent human review. The CAN interface in particular has never been built, measured
or reviewed, and no automotive transient qualification is claimed.

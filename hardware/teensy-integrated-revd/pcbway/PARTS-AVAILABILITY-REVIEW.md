# Rev D — parts availability review (PCBWay quote T-2SJ3W1113248A, 5 units)

> **STATUS: APPLIED (2026-09-29), with U1/U2 handled in the reply letter.** The six
> substitutions below are baked into `../components.json`, `../electrical/generate.py`,
> `../design/BOM.csv` and both `pcbway/*BOM*.csv`; the engineering fab ZIP was
> re-exported so its shipped `BOM.csv` matches (ERC/DRC 0 violations, 531
> assertions, 456 pads).
>
> **STATUS update (2026-09-30): U2 is now a SOCKET.** PCBWay's second attempt
> (quote `T-2SJ3W1113248A`, 2026-09-30) again could not source the Pololu 4091, so
> the module is **de-scoped from their sourcing**: they fit a **2×6 0.1 in THT female
> socket** (Samtec `SSW-106-01-G-D` proposed) into the U2 footprint and **we buy the
> five modules and plug them in**. No footprint change, no respin. See §4b.
>
> **Decision on the two modules:**
> * **U2 (Pololu D36V50F5) — socket fitted by PCBWay, module supplied by us.** It is
>   not an LCSC part; Pololu direct is *Rationed* and neither source would sell to
>   PCBWay. We buy 5× Pololu 4091 (US$36.75 ea at qty 5), solder their supplied pin
>   strips on, and plug them in; consignment to PCBWay for that step is offered in
>   the 09-30 letter.
> * **U1 (Teensy 4.1) — customer supplied**, bought *with pins* so the module needs
>   no soldering; PCBWay fits the two 1×24 sockets, we plug the Teensy in.
> * Left to us, all plug-in or hand steps: the CR2032 cell, the JP1 shunt (after
>   bench tests), plugging in the Teensy and the Pololu module, and the 2-wire J9
>   RTC lead.
>
> ⚠️ **The module can be plugged in the mirrored way round and that destroys it**
> (VIN would meet the 5 V rail, VOUT would meet 10–20 V). The carrier footprint has
> no silkscreen labels at all, so the orientation keys are the module's own four
> **square** GND holes, the **single rectangular U2 pad 1**, and the pre-power
> continuity check in `../design/BRINGUP.md` step 3 — all three are in §4b.
>
> The reply to send is **`PCBWAY-REPLY-2026-09-29.pdf`** with
> **`Racecar-RevD-BOM-pcbway-assembly-REV1.csv`** (identical content to the
> canonical `Racecar-RevD-BOM-pcbway-assembly.csv`, under a distinct name so
> PCBWay can tell it from the file they already have; the Comment column carries
> the U1/U2 fitting instructions).

Reviewed **2026-09-29** against the PCBWay Bom Quotation
`Quotation for T-2SJ3W1113248A-5units-Racecar-RevD-BOM-pcbway-assembly(2026-09-29).xls`
and the local `Racecar-RevD-BOM-pcbway-assembly.csv`.

**Result: 6 BOM lines need correcting** — three are outright bad MPNs (F3–F5,
D5, D2) and three are Murata capacitors with no LCSC stock (C10, C11, C18); on
the latter, two of PCBWay's proposed substitutes are fine and one must be
counter-proposed. There is also one clarification (BT1) and one price risk
(U8, u-blox). Everything else in the BOM is obtainable — the remaining risk is
*who* buys the consignment parts (see §4).

---

## 1. What PCBWay actually flagged

| Item | Ref | MPN | PCBWay note |
|---|---|---|---|
| 1 | U2 | `4091 / D36V50F5` | out of stock — recommend a substitute or supply it |
| 11 | D2 | `SS14` | out of stock — substitute or supply |
| 13 | U1 | `TEENSY41` | out of stock — substitute or supply |
| 28 | D5 | `PESD3V3U1UL,315` | out of stock — substitute or supply |
| 48 | F3 F4 F5 | `1206L010/30YR` | out of stock — substitute or supply |
| 21 | C10 | `GRM21BR71H103KA01L` | will supply `CL21B103KBANNNC` — OK? |
| 22 | C11 | `GRM21BR71H225KA01L` | will supply `CL21A225KBQNNNE` — OK? |
| 35 | C18 | `GRM21BR71H102KA01L` | will supply `CC0805KRX7R9BB102` — OK? |
| 14 | BT1 | `1058` | "the quoted part number is 1058, not CR2032 as in the description" |
| 24 | U8 | `NEO-M9N-00B` | "price is changing higher, final price subject to real price at order" |

Lead-time notes (7–10 workdays, i.e. ordered-in, **not** a problem): TSR 1-2433,
EEU-FR1A471B, 1058, 901-143, RT0805BRD0720KL, 1729034.

---

## 2. Required BOM changes

| # | Ref | Old MPN | Problem (verified) | New MPN | LCSC | LCSC stock | Other sources |
|---|---|---|---|---|---|---|---|
| 1 | F3 F4 F5 | `1206L010/30YR` | **This part number does not exist.** Littelfuse's own 1206L datasheet (rev BA 12/04/20) lists the 0.10 A device as **1206L010/60** only; there is no `/30` 100 mA variant (`/30` exists only at 0.20 A and 0.35 A). The `YR` reel code belongs to the 0.20–1.1 A rows. | **`1206L010/60WR`** | C2153714 | 6,922 | Digi-Key 4,861; Newark 10,764 |
| 2 | D5 | `PESD3V3U1UL,315` | Effectively unavailable: LCSC 2 pcs, Newark/Digi-Key/TME/Avnet all 0. | **`PESD5V0F1BL,315`** (recommended) or `PESD5V0U1UL,315` | C45961 / C85401 | 51,874 / 9,875 | standard Nexperia line |
| 3 | D2 | `SS14` (Mfr: Diodes Inc) | Bare `SS14` is not an orderable Diodes Inc PN, so PCBWay's supplier had nothing to match. | **onsemi `SS14`** (or Vishay `SS14-E3/61T`) | C83852 | 139,581 | LCSC also has 1.2 M generic-brand SS14; Digi-Key onsemi SS14 200,489 |
| 4 | U2 | `4091 / D36V50F5` | Not an LCSC part; Pololu direct is **Rationed / backorder-only**; PCBWay could not source it in **two** attempts. | **fit a 2×6 0.1 in THT female socket** (`SSW-106-01-G-D`); we plug the module in (§4b) | — | — | **Digi-Key 2183-4091-ND, 264 pcs, $39.95**; Pololu direct $36.75 @5 |
| 5 | U1 | `TEENSY41` | Not an LCSC part. | keep `TEENSY41` | — | — | **Digi-Key / SparkFun `DEV-16771`, 1,169 pcs, $31.50**; PJRC direct |
| 6 | C11 | `GRM21BR71H225KA01L` | Murata part has no LCSC stock. PCBWay's proposed `CL21A225KBQNNNE` is **X5R (85 °C)** — the original is **X7R (125 °C)**. | **`CC0805KKX7R9BB225`** (Yageo) or `CL21B225KBYNNNE` (Samsung) | C125847 / C2762602 | 110,671 / 83,283 | Digi-Key CC0805KKX7R9BB225 248,725 |

### Approve as-is (PCBWay's proposals are electrically equivalent)

| Item | Ref | Proposed | Verdict |
|---|---|---|---|
| 21 | C10 | `CL21B103KBANNNC` | **OK** — Samsung 10 nF 50 V **X7R** 0805, exactly the original spec (LCSC C1710, 2.3 M). |
| 35 | C18 | `CC0805KRX7R9BB102` | **OK** — Yageo 1 nF 50 V **X7R** 0805, exactly the original spec (LCSC C94121, 1.66 M). |
| 22 | C11 | `CL21A225KBQNNNE` | **Not as-is** — X5R/85 °C. Use the X7R part in row 6 above. |

### Clarification (no change needed)

| Item | Ref | Answer |
|---|---|---|
| 14 | BT1 | `1058` **is** the correct part. It is Keystone's SMD 20 mm coin-cell holder ("BATTERY HOLDER COIN 20MM SMD", Digi-Key `36-1058-ND`); *CR2032* in the description is the **cell**, which the BOM does not include and which is fitted by hand after reflow. Nothing to change — just confirm 1058 to PCBWay. |

---

## 3. Note on D5 — a better part than the original

`D5` sits on the **RF_ANT** net, i.e. the SMA antenna port
(`RF_ANT = C14 · D5 · J8 · L1`), and that port carries the **switched 3.3 V
active-antenna bias** (`+3V3_AUX → U9 TPS2553 → ANT_SWITCH → R5 10 R → ANT_BIAS → L1 → RF_ANT`).
The original `PESD3V3U1UL` has a **3.3 V standoff**, i.e. zero margin on a 3.3 V
bias line. Both replacements are in the same **SOD-882 / DFN1006-2** footprint
(no layout change) and are strictly better:

| Part | Standoff | Cd @ 0 V | Note |
|---|---|---|---|
| PESD3V3U1UL (original) | 3.3 V | 2.6 pF typ | out of stock |
| PESD5V0U1UL,315 | 5.0 V | 2.0 pF typ | unidirectional, closest like-for-like |
| **PESD5V0F1BL,315** | 5.5 V | **0.4 pF typ / 0.55 max** | bidirectional, femtofarad class, sold for antenna protection |

`PESD5V0F1BL` loses ~2 pF of shunt capacitance on a 1575 MHz feed, which is the
right direction for GNSS. Pin 1 marking is irrelevant on a bidirectional part.

---

## 4. Parts that are not LCSC line items — who gets them

Stock verified 2026-09-29. **U2 and U1 are the only two the quote's automatic
sourcing failed on; the letter asks PCBWay to buy U2 from Digi-Key/Pololu and ship
the sockets, and we supply the Teensy.** Everything else in this table PCBWay can
quote directly (they already price most of it in the quote).

| Ref | Part | Source (verified) | Stock | Price |
|---|---|---|---|---|
| U2 | Pololu 4091 / D36V50F5 | **Digi-Key 2183-4091-ND** | 264 | $39.95 (1) |
| U1 | Teensy 4.1 — **order the "with pins" version** | **Digi-Key** (SparkFun `DEV-16771`) or PJRC | 1,169 | $31.50 |
| U8 | u-blox NEO-M9N-00B | **LCSC C5119087** | 516 | $16.31 |
| U17 | ESP32-S3-WROOM-1-N8R2 | **LCSC C2913204** | 2,794 | $4.63 |
| U7 | TDK ICM-42670-P | **LCSC C3288646** (alt Newark) | 10,525 (LCSC) / 2,298 (Newark) | $2.28 |
| U3 U6 | TRACO TSR 1-2433 | **Digi-Key** (alt TME / Newark / Sager) | 9,405 / 5,162 / 573 / 11,697 | $6.28 (1) |
| J8 | Amphenol 901-143 SMA | **Digi-Key** | 1,730 | $26.31 (1) / $22.36 (10) |
| BT1 | Keystone 1058 | **Digi-Key 36-1058-ND** / TME | 1,610 / 458 | $1.34 (1) |
| J1 J3 J5 J6 | Phoenix 1729018 | **Digi-Key** | 13,434 | $0.95 (1) |
| J4 J11 J12 | Phoenix 1729021 | LCSC C3817836 (alt TME / Dynamic Solutions) | 857 / 180 / 1,800 | $0.78 |
| J2 | Phoenix 1729034 | **Digi-Key** 2,058 / Newark 2,630 | — | $1.94 (1) |
| J10 | Samtec TSW-103-07-G-D | **Digi-Key** (alt Avnet; LCSC only 73) | 10,111 / 16,238 | $0.69 (1) |
| J7 J9 J13 | JST B2B/B4B-XH-A | LCSC C158012 / C144395 | 344 k / 93 k | $0.04 / $0.06 |
| U12 | Vishay VO617A-3X017T | Digi-Key (alt LCSC C3035004) | 22,072 / 466 | — |

**⚠️ TRACO clone on LCSC:** LCSC *does* list a "TSR 1-2433" (C53183919, 1,791
pcs, $5.46) but the brand is **YLPTEC**, not TRACO Power. Do not accept it as the
TRACO part or as a PCBWay substitution. Buy the genuine part from Digi-Key /
TME / Newark / Sager.

**⚠️ Not in the BOM at all (assembly extras + hand-fit items).** `../design/ASSEMBLY-EXTRAS.csv`
is the full list; the ones that matter for this order:

| Qty/board | Part | Who | Stock / price |
|---|---|---|---|
| 2 | 1×24 2.54 mm female socket (Samtec `SSW-124-01-G-S`) — soldered into the U1 footprint | **PCBWay to fit** | Newark 427 @ $4.20 · Digi-Key 175 (we can consign the Samtec parts) |
| 1 | 2×6 0.1 in THT **female socket** (Samtec `SSW-106-01-G-D`) — soldered into the U2 footprint | **PCBWay to fit** | ≥3 A/pin; ~$1 (we can consign) |
| 1 | Pololu 4091 module + its two 1×6 pin strips — **plugs into that socket** | us — we solder the strips into the module, then plug it in | Pololu $36.75 ea @ qty 5 |
| 1 | Teensy 4.1 **with pins** (PJRC sells this variant) | us — plugs into the sockets, no soldering | Digi-Key / PJRC, $31.50 |
| 1 | CR2032 primary cell — **never reflowed**, fitted after cleaning | us | any |
| 1 | JP1 shunt (Harwin `M7566-05`) — **only after unloaded rail tests** | us | Digi-Key 5,393 · Newark 15,847, $0.30 |
| 1 | RTC lead: JST `XHP-2` housing + 2× `SXH-001T-P0.6` contacts + hook-up wire (J9 → Teensy VBAT/GND pads) | us | Digi-Key 262,854 · 1,329,000 |

---

### 4b. U2 — de-scoped to a fitted socket (2026-09-30)

PCBWay's second attempt (quote `T-2SJ3W1113248A`, 2026-09-30) still could not
source the Pololu 4091: item 1 is the **only unpriced line** in the quote and
neither Digi-Key nor Pololu would sell to them. Rather than ask a third time, the
module comes off their sourcing entirely.

| | Before (09-29 letter) | Now (09-30) |
|---|---|---|
| U2 BOM line, PCBWay | source **and fit** the Pololu 4091 | fit a **2×6 0.1 in THT female socket** (`SSW-106-01-G-D` proposed) |
| Pololu 4091 module | PCBWay buys it (Digi-Key `2183-4091-ND`) | **we buy 5×** (Pololu $36.75 ea at qty 5) and plug them in |
| Footprint, nets, Gerbers, CPL | — | **unchanged** — the socket uses the same 12 through-holes, so no respin and no re-export |
| Fitted cost | part + sourcing fee + shipping | ~$1 commodity socket; we pay the module ourselves |

Why this way round:

- It removes the **last unpriced line** immediately and depends on nobody's stock.
- A socket is a commodity part any assembler has in the house, and it has **no
  polarity** — the "which way round" problem moves to a mechanical key on the
  module instead of a soldering decision.
- Consignment (ship the five $40 modules to PCBWay so they solder the strips too)
  remains available **in writing** from them; the 09-30 letter asks them to quote
  it. Under the socket plan the only hand joint left is **soldering each module's
  two 1×6 strips into the module itself** — 12 joints, ~5 minutes each, nothing on
  the board.

Socket candidates (LCSC stock/ratings read 2026-09-30):

| LCSC | MPN | Rating | Body | Stock | Note |
|---|---|---|---|---|---|
| **C3323352** | Samtec `SSW-106-01-G-D` | **4.7 A/pin** | 8.51 mm, gold, −55…+125 °C | 17 | **primary** — same SSW family as the U1 socket PCBWay already bought |
| C2894967 | HCTL `PZ254-2-06-Z-8.5` | 3 A/pin | 8.5 mm, −40…+105 °C | 3,446 | commodity fallback |
| C239346 | CJT `A2541HWV-2x6P` | 3 A/pin | 8.5 mm | 2,134 | second fallback |

At the **5 A total** budget VIN and VOUT are each carried by 2 socket pins, so the
worst case is 2.5 A/pin: **≥3 A/pin is the requirement**, and the Samtec part has
real margin. Mounted stack height is ~19 mm above the carrier (8.5 mm socket +
9.5 mm module) against ~15 mm for the socketed Teensy beside it — a note for the
future enclosure, not a conflict: nothing else sits inside the U2 courtyard and the
nearest other pad is 10.4 mm away, so the extra height and the socket body are clear.

⚠️ **Orientation is now load-bearing.** The U2 footprint carries **no silkscreen
label at all** (verified: courtyard + fab lines only; pads are 1 rectangular + 11
round), and a socket accepts either orientation, so the module can be **inserted
the mirrored way round**. That puts the module's VIN where +5 V is and its VOUT
where 10–20 V is — immediate destruction. Three keys exist without touching the
board:

1. On the module the **four square plated holes are GND**; the **VOUT column is one
   column in** from them and the **EN/PG column is three columns** from them. True
   from either face, because they are the same physical holes.
2. **U2 pad 1 is the only rectangular pad** in the footprint (all others are round).
   It is VOUT, at the **north** end of the block (toward the terminal blocks) — the
   layout matches Pololu's *"top view with labels"* drawing (image 0J10742), so
   "VOUT at the north end" is the correct insertion.
3. **Continuity check before power:** inserted and unpowered, the four middle-row
   socket pads are GND and must read ~0 Ω to J1 pin 2 / the ground plane. In the
   mirrored orientation the module's VIN/VRP sit in those positions and the reading
   is not a solid short.

A **silk "VOUT / GND / VIN / VRP / EN·PG" label** and a pad-1 arrow next to the
block would remove the trap entirely. That is a **silkscreen-only** change (no
copper, no re-route, DRC unaffected) — but it does change the silkscreen Gerber, so
it needs a re-export before the PCB order is placed. Flagged, deliberately NOT done
in this revision.

---

## 5. Cost notes where the PCBWay quote is well above distributor pricing

| Line | Part | PCBWay unit | Distributor | Saving/board |
|---|---|---|---|---|
| 25 | Amphenol 901-143 | $39.32 | Digi-Key $26.31 (1) / $22.36 (10) | ~$13–17 |
| 2 | TRACO TSR 1-2433 | $9.48 | Newark $4.87, TME $5.35, Digi-Key $6.28 | ~$3.2–4.6 |
| 59 | Phoenix 1729034 | $3.64 | Digi-Key $1.94, Newark $2.69 | ~$1–1.7 |
| 20 | TDK ICM-42670-P | $3.56 | LCSC $2.28, Newark $2.42 | ~$1.1 |
| 14 | Keystone 1058 | $2.40 | Digi-Key $1.34, TME $1.19 | ~$1 |
| 63 | ESP32-S3-WROOM-1-N8R2 | $4.23 | LCSC $4.63 (comparable) | — |

Supplying the SMA jack, the two TRACO modules and the Keystone holder ourselves
saves roughly **$19–24 per board** before shipping, i.e. ~$100 on the 5-unit run.

---

## 6. The reply actually sent

**`PCBWAY-REPLY-2026-09-29.pdf`** (source: `PCBWAY-REPLY-2026-09-29.md`) — a
5-page letter answering every note in the quotation, plus
**`Racecar-RevD-BOM-pcbway-assembly-REV1.csv`** as the corrected BOM to re-match
from. Both are also copied to `~/Downloads/`. The letter's key asks:

1. approve C10/C18, counter-propose X7R `CC0805KKX7R9BB225` for C11;
2. use onsemi `SS14`, `PESD5V0F1BL,315`, `1206L010/60WR`;
3. **purchase and fit U2** (Digi-Key `2183-4091-ND` / Pololu 4091) — we pay part +
   sourcing fee, consignment only if they refuse;
4. **fit the two 1×24 sockets for U1**; we supply the Teensy (with pins) and plug it in;
5. do not fit the CR2032 cell, the JP1 shunt or the J9 lead;
6. confirm the `NEO-M9N-00B` price before production; genuine TRACO only.

---

## 7. Method and confidence

- **PCBWay notes** — read directly from the quotation `.xls` (converted with
  LibreOffice; single sheet, 68 line items).
- **LCSC stock/price** — JLCPCB's public component-search API
  (`jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList`),
  exact-MPN match, queried per BOM line. This is the same inventory PCBWay's
  turnkey flow draws on, so it is the relevant number for them.
- **US/EU distributor stock** — Findchips aggregation (Digi-Key, Newark, RS,
  Avnet, TME, Sager, Rochester, plus independents), exact-MPN match.
- **Datasheet facts** (Littelfuse ordering codes, Nexperia capacitance) — read
  from the manufacturer PDFs fetched from LCSC's document mirror, not from
  memory.
- **Vendor-direct** — pololu.com product page (rationed/backorder status),
  SparkFun/Adafruit listings, pjrc.com.

**Caveats.** Digi-Key and Mouser both block automated queries, so their numbers
come only via the Findchips feed — which returns **0** for some perfectly
ordinary parts (e.g. it reports Digi-Key 0 for `LM2903BIDR`,
`SN74LVC1G125DBVR`, `GRM21BR71H105KA12L`). Treat any single-source "0" as
*unverified, ask the distributor*, and treat "in stock with a price break" as
good. Every part above is backed by at least two independent sources except the
Teensy/Pololu consign items (vendor + Digi-Key) and the u-blox module (LCSC
only, which is exactly the price risk PCBWay flagged).

**U2 socket change (2026-09-30).** Swapping the fitted module for a fitted
**socket** changes no copper either: the 12 through-holes, their nets, the
footprint and the CPL are untouched, so the Gerbers and the PCBWay Gerber ZIP stay
valid and nothing needs re-exporting. Only the BOM Comment/part mapping and the
assembly documents changed. (Adding silkscreen labels to the U2 footprint — §4b —
*would* change a Gerber and is deliberately not done.)

**Nothing in this document changes the schematic, the PCB or the Gerbers.** The
two part changes (D5, F3–F5) and the three capacitor substitutions are
same-footprint substitutions. `1206L010/60WR` is a 1206 PPTC like the part it
replaces, and `PESD5V0F1BL` is SOD-882 like `PESD3V3U1UL`. What *has* changed in
the repo is the parts metadata (`components.json`, `design/BOM.csv`, the
`pcbway/*.csv` files, and the notes/rationale in `electrical/generate.py`), plus a
freshly re-exported fab ZIP carrying the corrected BOM.

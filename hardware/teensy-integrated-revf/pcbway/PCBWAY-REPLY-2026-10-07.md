# Reply to PCBWay quotation T-2SJ3W1113248A — 2026-10-07

**Product:** Racecar-35 integrated controller (this quotation was built from our **Rev E** BOM)
**Quantity:** 5 units · turnkey assembly (parts + assembly + bare PCB)
**Their totals:** parts $716.78 *incomplete* + assembly $88.00 + PCB $174.05 = **$978.83**

This document is the reply to the email of 2026-10-06 22:45 and fills the
`Customer Reply` column of the quotation. **Two parts change, one line that was
dropped has to go back in, two lines are not parts at all — and the quotation
carries the wrong board revision (Rev E instead of Rev F).**

---

## 1. Email to send back

> **Subject:** Re: Quotation T-2SJ3W1113248A — Racecar-35, 5 units — answers, two part changes, and a revision correction
>
> Hi,
>
> Thanks for the updated quotation. We have filled in the **Customer Reply**
> column for every note (copied below). Summary of what we are changing:
>
> **Parts we are changing**
>
> | Ref (your item) | Change | Why |
> |---|---|---|
> | **J8** (item 37) | `901-143` → **`901-143-6RFX`** (Amphenol RF) | Same SMA right-angle THT PCB jack, same footprint and the same PCB hole pattern (1 centre Ø1.5 + 4× Ø1.7 at ±2.54), same 50 Ω / 500 mating cycles — it is the brass-body variant of the same jack. Your $41.123 each is ~1.5–2× the distributor price for the stainless version; the `-6RFX` is **~$6.84 at 5 pcs**. Please re-price. |
> | **D23** (item 7) | `SS56` → **an SMB (DO-214AA) 5 A / 60 V Schottky** — e.g. **BORN `SS56` SMB, LCSC C2687867**, or Diotec `SK56` | You are right: the bare `SS56` part number is normally supplied as **SMA (DO-214AC)** and will **not** fit our **DO-214AA (SMB)** land pattern. Please supply the SMB-packaged part only — **not** the DO-214AC one. |
> | **U1** (item 27) | **Re-add 2× `SSW-124-01-G-S`** (1×24 2.54 mm THT female socket) | This line was **dropped** from the revised quotation. The Teensy 4.1 is customer-supplied — we buy it *with pins* and plug it in. You source and fit the two sockets only. |
> | **H1–H4** (item 75), **TP1–TP5** (item 76) | Mark **"no part — do not source, do not place"** | These are not components, which is why your system can't find a part number or brand. H1–H4 are 3.2 mm mounting holes in the PCB; TP are 2.0 mm plated through-hole test pads (bare copper). Nothing to buy, nothing to place, no assembly cost. |
> | **D24** (item 3) | Accept your `BZX84-C15,215` | Correct — that is Nexperia's orderable MPN (same device, tape/reel suffix). |
> | **U8** (item 36) | Price accepted, no change | — |
>
> **Also — this quotation is built from our Rev E BOM; the board we need built
> is Rev F.** Rev E is superseded, and the CAN interface is the entire reason
> Rev F exists (see §3). The two BOMs are otherwise identical. Please send us a
> **revised quotation from the Rev F BOM/CPL/Gerbers** (attached), 5 units, with
> the changes above applied.
>
> What we need back:
> 1. The revised quotation from the **Rev F** BOM, with J8 re-priced, D23 as an
>    SMB part, and the U1 socket line restored.
> 2. Lead times for the items you flagged (7–10 working days).
> 3. Confirmation that H1–H4 and TP1–TP5 carry **no part and no assembly charge**.
>
> Thanks,
> Chris

---

## 2. `Customer Reply` column — copy/paste per item

| Item | Ref | Their note (verbatim) | **Customer Reply** |
|---|---|---|---|
| 3 | D24 | *The part we will supply is BZX84-C15,215 OK？* | **Approved.** `BZX84-C15,215` is the correct orderable Nexperia MPN (same device). Please supply it. |
| 7 | D23 | *The correct package for the listed Part Number [SS56] is indeed [SMA(DO-214AC)], not [SMB]. Please confirm.* | **Confirmed: SMB (DO-214AA).** You are correct that the bare `SS56` is normally SMA — our land pattern is DO-214AA and the SMA part will not fit. Please fit an **SMB (DO-214AA) 5 A / 60 V** Schottky: BORN `SS56` SMB (**LCSC C2687867**) or Diotec `SK56`. Do **not** fit the DO-214AC version. |
| 27 | U1 | *It's out of stock. Please recommend substitutes or you can supply this part to us.* | **Customer supplied — no substitute, do NOT source or fit a Teensy.** We buy the Teensy 4.1 with pins and plug it in. Please **source and fit 2× `SSW-124-01-G-S`** (1×24, 2.54 mm, THT female socket) at U1 — **this line was dropped from the revised quotation, please re-add it.** |
| 28 | BT1 | *The quoted part number is [1058], not [CR2032] as specified in description* | **`1058` is correct** — the Keystone SMD 20 mm coin-cell **holder**. "CR2032" in the description is the **cell**, which is not in the BOM. Please supply the holder and **do NOT fit the cell** (primary lithium cell — we fit it by hand after reflow/cleaning). |
| 75 | H1–H4 | *pls provide exact part number and brand* | **No part — do not source, do not place.** H1–H4 are **3.2 mm non-plated mounting holes in the PCB**, not components. The "M3 insulated hardware" note in the description is **customer-fitted** and must not be sourced. Please charge no part and no assembly cost for this line. |
| 76 | TP1–TP5 | *pls provide exact part number and brand* | **No part — do not source, do not place.** TP1–TP5 are **2.0 mm plated through-hole pads** (bare copper test points). They are features of the PCB; there is no component, no part number and no brand. Please charge no part and no assembly cost for this line. |
| 36 | U8 | *(price)* | Price accepted, no change. |
| 37 | J8 | *(price)* | **Part changed to `901-143-6RFX`** (Amphenol RF) — same footprint and PCB hole pattern. Please re-price. |

### `Actual Purchase Mfg Part #` column — verification you asked for

Only one entry is populated: **item 3 = `BZX84-C15,215` — correct**, that is the
orderable Nexperia MPN. Everything else in that column is blank on our copy; no
other supplier part numbers have been proposed.

---

## 3. ⚠️ Revision correction — this quotation is Rev E, we need Rev F

The BOM in this quotation is our **Rev E**. Rev E is **superseded**; the active
board is **Rev F**. Concretely, this quotation still contains the **deleted `J7`
external CAN-module header** and has **no CAN transceiver at all**, so a board
built from it cannot talk to the MS3Pro on CAN — which is the exact failure Rev F
was created to fix (the external module we had been using turned out to be
mislabelled at the bench, see `hardware/CAN-TRANSCEIVER-FINDINGS-2026-10-04.md`).

The two BOMs are otherwise identical. **Please re-quote from the Rev F BOM/CPL/
Gerbers** (`Racecar-RevF-BOM-pcbway-assembly.csv`, `Racecar-RevF-CPL.csv`,
`Racecar-RevF-PCBWAY-GERBERS.zip`).

### Rev E → Rev F delta

| Ref | Rev F action | MPN | Manufacturer |
|---|---|---|---|
| `U21` | **add** | `TCAN1042HGVDRQ1` | Texas Instruments |
| `D25` | **add** | `NUP2105LT1G` | onsemi |
| `J14` | **add** | `1729021` (3-pos 5.08 mm field terminal, 1 CANH / 2 CANL / 3 GND) | Phoenix Contact |
| `JP2` | **add** | `TSW-102-07-G-S` (2-pin header — **header only, no shunt**) | Samtec |
| `R58` `R59` | **add** | `RC0805FR-0760R4L` (60.4 R 1 %, split termination) | Yageo |
| `C49` `C50` | **add** | `GRM21BR71H104KA01L` (100 n) | Murata |
| `C51` | **add** | `GRM21BR71H105KA12L` (1 u) | Murata |
| `C52` | **add** | `CL21B472KBANNNC` (4.7 n) | Samsung |
| `TP6` `TP7` | **add** | — (test pads, **no part**) | — |
| `J7` | **remove** | `B4B-XH-A(LF)(SN)` | JST |
| `C8` | **remove** | `GRM21BR71H104KA01L` (100 n) | Murata |

> ⚠️ **`U21` must not be substituted.** Order `TCAN1042HGVDRQ1` from **authorized
> distribution only** — no clones, no re-marked parts. The **`V` suffix is
> mandatory**: it is the VIO pin (pin 5). A non-V `TCAN1042` (`TCAN1042DRQ1` /
> `TCAN1042HDRQ1` / `TCAN1042GDRQ1`) has **pin 5 = NC** and would put 5 V on the
> 3.3 V Teensy. Approved alternates, in order: `TCAN1042VDRQ1`, then NXP
> `TJA1051T/3/1J` (its pin 8 `S` must also go to GND). Never an SN65HVD23x, a
> plain `TJA1051T`, a `TJA1050`, or a "pin-compatible" clone.

---

## 4. Summary of the BOM changes (for your BOM edit)

| Ref | Out | In | Who fits it |
|---|---|---|---|
| `D24` | `BZX84C15` (no suffix) | `BZX84-C15,215` (Nexperia) | PCBWay |
| `D23` | `SS56` (SMA/DO-214AC) | SMB (DO-214AA) 5 A / 60 V — BORN `SS56` SMB (**LCSC C2687867**) or Diotec `SK56` | PCBWay |
| `J8` | `901-143` (Amphenol RF) | `901-143-6RFX` (Amphenol RF) | PCBWay |
| `U1` | *(line missing)* | 2× `SSW-124-01-G-S` sockets | PCBWay — sockets only; Teensy is ours |
| `U8` | `NEO-M9N-00B` @ $21.176 | unchanged | PCBWay |
| `H1`–`H4` | `M3 insulated hardware` | **no part** | customer |
| `TP1`–`TP7` | `PCB test pad` | **no part** | — (PCB feature) |
| `BT1` | `1058` holder | unchanged — **do not fit the CR2032 cell** | holder: PCBWay · cell: customer |
| *(BOM itself)* | Rev E | **Rev F** | — |

---

### Attachments to send with the reply

- `Racecar-RevF-BOM-pcbway-assembly.csv`
- `Racecar-RevF-CPL.csv`
- `Racecar-RevF-PCBWAY-GERBERS.zip`
- *(reference)* `PCBWAY-ORDER-GUIDE.md`, `REVF-CHANGES-AND-ORDER.md`

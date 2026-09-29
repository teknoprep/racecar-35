# Reply to RFQ / BOM quotation — T-2SJ3W1113248A

**Product:** Racecar-35 Rev D integrated controller
**Quantity:** 5 units · turnkey PCB assembly (parts + assembly + bare PCB)
**Date:** 29 September 2026 · **Quote reference:** T-2SJ3W1113248A

Please apply the decisions below and issue the **revised quotation**. The
corrected BOM to match from is attached:
`Racecar-RevD-BOM-pcbway-assembly-REV1.csv` (six part numbers corrected, plus
fitting instructions for U1 and U2 in the Comment column).

---

## 1. Answers to the three "substitute OK?" questions

| Item | Original | Your proposed substitute | Our answer |
|---|---|---|---|
| 21 | `GRM21BR71H103KA01L` (C10) | `CL21B103KBANNNC` | **APPROVED** — same value: 10 nF, 50 V, X7R, 0805 |
| 22 | `GRM21BR71H225KA01L` (C11) | `CL21A225KBQNNNE` | **NOT APPROVED** — this is X5R (85 °C). The original is X7R (125 °C) and this part sits on the 3.3 V MCU rail. Please use **`CC0805KKX7R9BB225`** (Yageo, LCSC C125847) or **`CL21B225KBYNNNE`** (Samsung, LCSC C2762602) |
| 35 | `GRM21BR71H102KA01L` (C18) | `CC0805KRX7R9BB102` | **APPROVED** — same value: 1 nF, 50 V, X7R, 0805 |

## 2. The four out-of-stock items — what to do

| Item | Ref | Original | Our decision |
|---|---|---|---|
| 1 | **U2** | `4091 / D36V50F5` (Pololu) | **Please source and fit it** — see §3 below. Do not substitute another regulator. |
| 13 | **U1** | `TEENSY41` (PJRC Teensy 4.1) | **Customer supplied.** We buy the Teensy 4.1 (**with pins**), so nothing needs soldering on the module. **Please fit the two 1×24 2.54 mm female sockets** into the U1 footprint (Samtec `SSW-124-01-G-S`, or any standard 2.54 mm 1×24 socket — say the word and we will consign the Samtec parts). We plug the Teensy into the sockets ourselves. |
| 11 | D2 | `SS14` (Diodes Inc) | **Please use onsemi `SS14`** (LCSC C83852, stock >100 000). Alternative: Vishay `SS14-E3/61T` (LCSC C47460). Identical 40 V / 1 A SMA Schottky. |
| 28 | D5 | `PESD3V3U1UL,315` | **Please use Nexperia `PESD5V0F1BL,315`** (LCSC C45961, stock >50 000) — same SOD-882 / DFN1006-2 land pattern. Alternative if unavailable: `PESD5V0U1UL,315` (LCSC C85401). |
| 48 | F3 F4 F5 | `1206L010/30YR` | **This part number does not exist** in the Littelfuse 1206L series (the 0.10 A device is only made as `1206L010/60`, i.e. `1206L010/60WR`). **Please use `1206L010/60WR`** (LCSC C2153714, stock ~7 000; also Newark 10 764 / Digi-Key 4 861). Same 1206 package, same 100 mA hold current, 60 V instead of 30 V. |

## 3. U2 — the Pololu D36V50F5: please purchase and fit it

You told us this part is out of stock and asked us to recommend a substitute or
supply it. **Please source it instead — we cannot substitute it** (it is the main
5 V rail and the PCB footprint is specific to this module).

It **is** available today:

| Supplier | Part number | Stock | Price |
|---|---|---|---|
| Digi-Key | **2183-4091-ND** (Pololu 4091) | 264 pcs | US$39.95 |
| Pololu (factory) | **item 4091**, pololu.com/product/4091 | made to order | US$39.95 (US$36.75 at qty 5) |

You already sourced our **Amphenol `901-143`** SMA jack and our **Keystone `1058`**
battery holder for this same quotation, and neither of those is an LCSC part
either — please source this module the same way. **We will pay the part price plus
your normal sourcing fee.** If for any reason you cannot purchase it from an
outside supplier, tell us before the quote is finalised and **we will ship five
of them to you as consigned parts** — but please try to source it first, as
consignment from the US costs both of us time and customs paperwork.

## 4. The other two notes

| Item | Note | Our reply |
|---|---|---|
| 14 | BT1 `1058` — "quoted part number is 1058, not CR2032" | **1058 is correct.** It is the Keystone SMD 20 mm coin-cell holder. "CR2032" in the description is the **cell**, which is not part of the BOM. Please supply the holder; **do not fit the cell** (see §6). |
| 24 | U8 `NEO-M9N-00B` — "price is changing higher" | Understood. **Please confirm the final unit price with us before production starts.** If it exceeds **US$20 per unit** please stop and tell us before ordering. |

## 5. One part-name warning

Item 2, TRACO `TSR 1-2433`: this must be **genuine TRACO Power**. A module with
the same generic name is sold on LCSC (C53183919) but the brand is *YLPTEC* — that
is **not acceptable** as the TRACO part. Please quote and supply genuine TRACO
Power from an authorised distributor (Digi-Key, TME, Newark or Sager all have it
in stock).

## 6. What we fit ourselves after assembly — please do NOT fit these

Everything below is a plug-in or hand operation, so please leave these four things
to us:

1. **BT1 — do not fit the CR2032 cell.** It is a primary lithium cell: no reflow,
   no soldering, fit after cleaning. We insert it by hand.
2. **JP1 — leave the shunt off.** We fit it only after our unloaded power-rail
   tests have passed.
3. **U1 — do not fit the Teensy.** Please fit only the two 1×24 sockets (§2).
   We plug the Teensy in.
4. **J9 RTC lead** — we attach the short 2-wire lead from J9 to the Teensy's
   auxiliary VBAT/GND pads. Just fit J9 to the board.

## 7. What is unchanged

- The **BOM structure, references, quantities and footprints** are unchanged —
  every substitution above is the same package/footprint, so **no PCB change is
  required**.
- The **bare PCB** settings from your quote are accepted as-is: 4 layers,
  150.05 × 155.05 mm, 1.6 mm, 1 oz outer copper, **ENIG**, impedance control ON
  (50 Ω single-ended on the GNSS feed), white silkscreen, green mask, do not
  panelise, keep the antenna notch in the profile.

## 8. What we need back from you

1. The **revised quotation** for 5 units from the attached corrected BOM, with
   **U2 (Pololu 4091) priced as a sourced part** and the **two 1×24 sockets for
   U1 added** as assembly items.
2. Confirmation that the five corrected MPNs in §1–§2 are **matched and in stock**
   in your system, with lead times.
3. The final confirmed price for `NEO-M9N-00B` before production (see §4).
4. If you cannot source U2 — tell us **now**, so we can consign it in time.

---

### Summary of changes (for your BOM edit)

| Ref | Out | In | Who fits it |
|---|---|---|---|
| C10 | `GRM21BR71H103KA01L` (Murata) | `CL21B103KBANNNC` (Samsung) | PCBWay |
| C11 | `GRM21BR71H225KA01L` (Murata) | `CC0805KKX7R9BB225` (Yageo) | PCBWay |
| C18 | `GRM21BR71H102KA01L` (Murata) | `CC0805KRX7R9BB102` (Yageo) | PCBWay |
| D2 | `SS14` (Diodes Incorporated) | `SS14` (onsemi) | PCBWay |
| D5 | `PESD3V3U1UL,315` (Nexperia) | `PESD5V0F1BL,315` (Nexperia) | PCBWay |
| F3 F4 F5 | `1206L010/30YR` (Littelfuse) | `1206L010/60WR` (Littelfuse) | PCBWay |
| U2 | Pololu 4091 (out of stock) | same part, sourced from Digi-Key/Pololu | **PCBWay** |
| U1 | Teensy 4.1 (out of stock) | same part, supplied by us (with pins) | sockets: PCBWay · Teensy: us |

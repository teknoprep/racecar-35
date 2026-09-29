# Reply to RFQ / BOM quotation — T-2SJ3W1113248A

**Product:** Racecar-35 Rev D integrated controller
**Quantity:** 5 units · turnkey PCB assembly (parts + assembly + bare PCB)
**Date:** 29 September 2026 · **Quote reference:** T-2SJ3W1113248A

Please apply the decisions below and issue the **revised quotation**. A corrected
BOM is attached (`Racecar-RevD-BOM-pcbway-assembly-REV1.csv`) — please re-match
from that file rather than the original.

---

## 1. Answers to the three "substitute OK?" questions

| Item | Original | Your proposed substitute | Our answer |
|---|---|---|---|
| 21 | `GRM21BR71H103KA01L` (C10) | `CL21B103KBANNNC` | **APPROVED** — same value: 10 nF, 50 V, X7R, 0805 |
| 22 | `GRM21BR71H225KA01L` (C11) | `CL21A225KBQNNNE` | **NOT APPROVED** — this is X5R (85 °C). The original is X7R (125 °C) and this part sits on the 3.3 V MCU rail. Please use **`CC0805KKX7R9BB225`** (Yageo, LCSC C125847) or **`CL21B225KBYNNNE`** (Samsung, LCSC C2762602) |
| 35 | `GRM21BR71H102KA01L` (C18) | `CC0805KRX7R9BB102` | **APPROVED** — same value: 1 nF, 50 V, X7R, 0805 |

## 2. Answers to the four "out of stock" items

| Item | Ref | Original | Our decision |
|---|---|---|---|
| 1 | U2 | `4091 / D36V50F5` (Pololu) | **Remove from the assembly** — we will fit this module ourselves. It is a socketed/headered raised module and needs no PCB change. Do not source it, do not charge assembly for it. |
| 13 | U1 | `TEENSY41` (PJRC Teensy 4.1) | **Remove from the assembly** — we will fit the Teensy ourselves (it goes into the supplied 2× 1×24 socket). Do not source it, do not charge assembly for it. |
| 11 | D2 | `SS14` (Diodes Inc) | **Please use onsemi `SS14`** (LCSC C83852, stock >100 000). Alternative: Vishay `SS14-E3/61T` (LCSC C47460). Identical 40 V / 1 A SMA Schottky. |
| 28 | D5 | `PESD3V3U1UL,315` | **Please use Nexperia `PESD5V0F1BL,315`** (LCSC C45961, stock >50 000) — same SOD-882 / DFN1006-2 land pattern. Alternative if unavailable: `PESD5V0U1UL,315` (LCSC C85401). |
| 48 | F3 F4 F5 | `1206L010/30YR` | **This part number does not exist** in the Littelfuse 1206L series (the 0.10 A device is only made as `1206L010/60`, i.e. `1206L010/60WR`). **Please use `1206L010/60WR`** (LCSC C2153714, stock ~7 000; also Newark 10 764 / Digi-Key 4 861). Same 1206 package, same 100 mA hold current, 60 V instead of 30 V. |

## 3. The other two notes

| Item | Note | Our reply |
|---|---|---|
| 14 | BT1 `1058` — "quoted part number is 1058, not CR2032" | **1058 is correct.** It is the Keystone SMD 20 mm coin-cell holder. "CR2032" in the description is the **cell**, which is not part of the BOM and which we fit by hand after assembly. Please supply Keystone `1058`. |
| 24 | U8 `NEO-M9N-00B` — "price is changing higher" | Understood. **Please confirm the final unit price with us before production starts.** If it exceeds **US$20 per unit** please stop and tell us before ordering. |

## 4. One part-name warning

Item 2, TRACO `TSR 1-2433`: this must be **genuine TRACO Power**. A module with
the same generic name is sold on LCSC (C53183919) but the brand is *YLPTEC* — that
is **not acceptable** as the TRACO part. Please quote and supply genuine TRACO
Power from an authorised distributor (Digi-Key, TME, Newark or Sager all have it
in stock).

## 5. What is unchanged

- The **BOM structure, references, quantities and footprints** are unchanged —
  every substitution above is the same package/footprint, so **no PCB change is
  required**.
- The **bare PCB** settings from your quote are accepted as-is: 4 layers,
  150.05 × 155.05 mm, 1.6 mm, 1 oz outer copper, **ENIG**, impedance control ON
  (50 Ω single-ended on the GNSS feed), white silkscreen, green mask, do not
  panelise, keep the antenna notch in the profile.
- Substitutions above are only in the **BOM / parts list**.

## 6. What we need back from you

1. The **revised quotation** for 5 units, built from the attached corrected BOM,
   with U1 (Teensy 4.1) and U2 (Pololu) removed.
2. Confirmation that all five corrected MPNs in §1 and §2 are **matched and in
   stock in your system**, with their lead times.
3. The final confirmed price for `NEO-M9N-00B` before production (see §3).
4. Your **PCB Assembly (turnkey) uploading instructions confirmed for the file
   `Racecar-RevD-BOM-pcbway-assembly-REV1.csv`** — if your console requires the
   project's original file name, we can rename it back on request.

---

### Summary of part changes (for your BOM edit)

| Ref | Out | In |
|---|---|---|
| C10 | `GRM21BR71H103KA01L` (Murata) | `CL21B103KBANNNC` (Samsung) |
| C11 | `GRM21BR71H225KA01L` (Murata) | `CC0805KKX7R9BB225` (Yageo) |
| C18 | `GRM21BR71H102KA01L` (Murata) | `CC0805KRX7R9BB102` (Yageo) |
| D2 | `SS14` (Diodes Incorporated) | `SS14` (onsemi) |
| D5 | `PESD3V3U1UL,315` (Nexperia) | `PESD5V0F1BL,315` (Nexperia) |
| F3 F4 F5 | `1206L010/30YR` (Littelfuse) | `1206L010/60WR` (Littelfuse) |
| U1 U2 | Teensy 4.1, Pololu 4091 | **deleted from the BOM — customer supplied** |

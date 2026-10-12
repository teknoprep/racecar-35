# Reply to JLCPCB (Fiona, 2026-10-08) — BOM/CPL designators + MPN matching

**Context:** JLCPCB asked two questions on the Racecar-35 Rev F order:
1. The BOM lists `TP1–TP7` and `H1–H4` but the CPL does not — ignore (A) or send corrected files (B)?
2. May they match components using the **Manufacturer Part Number** column of the BOM?

**Answer: 1 = B** — the two "no part" rows are gone from the corrected BOM, so BOM and CPL
designators now correspond exactly (182 each). **2 = yes**, with the Comment column
authoritative wherever it constrains a part.

---

## 1. Email to send back

> **Subject:** Re: Racecar-35 Rev F — BOM/CPL designators (corrected files) and matching by MPN
>
> Hi Fiona,
>
> Thanks for the two questions — answers below, and the corrected files are attached
> (I am also replacing them on the website).
>
> **1. BOM/CPL designators (TP1–TP7, H1–H4) — option B: the files are corrected.**
> You were right: those 11 designators were BOM rows with no placement. They are PCB
> features, not components:
>
> * **H1–H4** — 3.2 mm non-plated mounting holes in the PCB;
> * **TP1–TP7** — 2.0 mm plated through-hole pads (bare-copper test points).
>
> Nothing to buy, nothing to place, no assembly cost. They are in the Gerbers, which is
> where they belong, so their absence from the CPL is correct. I have removed those two
> rows from the BOM entirely, so the corrected BOM and your CPL now correspond exactly:
> **182 placements, every designator in both files, none in only one.** The CPL and the
> Gerbers are unchanged — **only the BOM changed.** Until you have the corrected upload,
> option A is safe to use: please ignore those designators and charge no part and no
> assembly cost for them.
>
> **2. Matching by Manufacturer Part Number — yes, please.**
> Match every line by the **Manufacturer Part Number** column (use the **Manufacturer**
> column where it disambiguates). Two rules:
>
> * The **Comment** column is authoritative whenever it constrains a part, in particular:
>   **D23** must be the **SMB (DO-214AA)** 5 A / 60 V Schottky (e.g. BORN `SS56` SMB,
>   LCSC **C2687867**, or Diotec `SK56`) — **not** the SMA `SS56`; **U21** must be
>   `TCAN1042HGVDRQ1` from **authorized distribution only**, no substitution without
>   written approval, the **“V” suffix is mandatory**; **J8** exactly `901-143-6RFX`
>   (Amphenol RF); **U1** = two 1×24 2.54 mm THT female sockets `SSW-124-01-G-S` only —
>   the Teensy 4.1 is customer-supplied and must **not** be sourced or fitted;
>   **BT1** = the Keystone `1058` holder only — do **not** fit the CR2032 cell;
>   **JP1/JP2** = the two 2-pin headers only, **no shunts**; **J9** = the JST connector
>   only (the lead is hand-made by us).
> * For commodity passives (Yageo / Murata / Samsung 0805/1210 resistors and capacitors),
>   an equivalent from your stock is fine **if** value, tolerance, voltage rating,
>   dielectric and package all match.
>
> Please send us the **matched part list** (your part number for each BOM line) before
> production so we can confirm — especially **U8** (price) and **U21**. If any line cannot
> be matched, please tell us instead of substituting silently.
>
> To be clear, this is the **Rev F** BOM/CPL/Gerbers; the item numbers in the earlier
> quotation were from the superseded Rev E BOM.
>
> Thanks,
> Chris

---

## 2. What changed in the files

Regenerated with `python3 pcbway/make_package.py .` (the script now drops the two
"not a component" groups, so the mismatch cannot come back on the next quote).

| File | Change | sha256 |
|---|---|---|
| `Racecar-RevF-BOM-pcbway-assembly.csv` | Two rows removed (H1–H4, TP1–TP7). Now **77 BOM lines / 182 designators = the CPL exactly.** | `401994e81c5c74f0189794ccca6c30d8176fd0eadb4308084343f2f6293c4214` |
| `Racecar-RevF-CPL.csv` | **Unchanged** — 182 placements, byte-identical to what JLCPCB already has. | `eaebabc4d5519ccee42589b1dad3f48bd11c05c92412edceb287dee591f143cd` |
| `Racecar-RevF-PCBWAY-GERBERS.zip` | **Unchanged** — 16 gerber/drill files (a re-export differs only in KiCad's embedded creation-date lines). | `3e47f35f0d2f022e0ff98811e1796468112d3d4383d368a9b77db420f583251a` |

Why not leave the rows in and say "ignore them" (option A)? JLCPCB's part matching walks
the BOM designator by designator and stops when a designator has no placement — every
re-quote would hit the same wall. The rows were documentation, not an order line; the
Board itself already carries them as `exclude_from_pos_files exclude_from_bom`
footprint attributes. The padding is in the Gerbers; the CPL was right all along.

---

## 3. MPN-matching quick reference (for the JLCPCB engineer)

| Ref | Match by MPN — rule |
|---|---|
| D23 | SMB (DO-214AA) 5 A / 60 V Schottky only: BORN `SS56` SMB (LCSC C2687867) or Diotec `SK56`. **Never the SMA/DO-214AC `SS56`** — it does not fit. |
| U21 | `TCAN1042HGVDRQ1` (TI), authorized distribution only, no substitutes. **“V” suffix mandatory** (VIO pin 5). Approved alternates, in order: `TCAN1042VDRQ1`, then NXP `TJA1051T/3/1J` (pin 8 `S` to GND). |
| U1 | 2× `SSW-124-01-G-S` sockets. **No Teensy** — customer supplied, bought with pins, plugged in. |
| J8 | `901-143-6RFX` (Amphenol RF) — exact. |
| BT1 | Keystone `1058` holder only. **No CR2032 cell** (customer fits it after reflow). |
| JP1 JP2 | Samtec `TSW-102-07-G-S` headers only. **No 2.54 mm shunts.** |
| J9 | JST `B2B-XH-A(LF)(SN)` connector only; 2-wire lead is customer-made. |
| U8 | `NEO-M9N-00B` — confirm final price before production. |
| Passives | Yageo/Murata/Samsung equivalents OK if value, tolerance, voltage, dielectric, package match. Otherwise ask first. |

Gerber-only ZIP rule from the 2026-10-08 email is respected: the Gerber ZIP contains only
Gerbers/drills/maps/`.gbrjob`, no documents.

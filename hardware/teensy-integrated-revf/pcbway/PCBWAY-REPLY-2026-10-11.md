# Reply to PCBWay (Kelly Fu, 2026-10-07) — quotation T-2SJ3W1113248A, returned Rev F

**What they asked:** *"could you kindly reply to the questions listed in the attached BOM list
first? Please reply in 'customer reply'."* — i.e. fill column **M** of their quotation, and
they will then change the quantity.

**What we send back:** their own workbook, updated to our **Rev F** BOM, with every note
answered in the **Customer Reply** column.

* `hardware/Quotation for T-2SJ3W1113248A-5units-Racecar-RevF-BOM-pcbway-assembly(2026-10-11).xls`
  — the file to attach (`.xlsx` alongside it is the same workbook; regenerate both with
  `python3 hardware/teensy-integrated-revf/pcbway/make_pcbway_quotation_reply.py`).
* Plus the three Rev F files already in this folder: `Racecar-RevF-BOM-pcbway-assembly.csv`,
  `Racecar-RevF-CPL.csv`, `Racecar-RevF-PCBWAY-GERBERS.zip`.

⚠️ This **supersedes the 2026-10-07 draft** (`PCBWAY-REPLY-2026-10-07.md`): the answers are
the same, but they now sit in the column PCBWay asked for, and the line items are Rev F
instead of Rev E. Do **not** send the old draft as well — it is keyed to Rev E item numbers.

---

## 1. Email to send back

> **Subject:** Re: Quotation T-2SJ3W1113248A — Racecar-35 — Customer Reply filled in, and the quotation is updated to Rev F
>
> Hi Kelly,
>
> Thanks — attached is your quotation returned with the **Customer Reply** column filled in
> for every note, and rebuilt against our **Rev F** BOM. Two things to know before you
> re-quote:
>
> 1. **The board you quoted (Rev E) is superseded.** Rev E still had the external CAN-module
>    header and no CAN transceiver at all; Rev F deletes that header and puts the transceiver
>    on the board. Please quote from the attached **Rev F** BOM/CPL/Gerbers — the sheet's
>    "Rev E to Rev F" tab lists every line that changed.
> 2. **Your six questions, answered** (also in the sheet's "Your questions" tab):
>    * **D24 — yes**, `BZX84-C15,215` is the correct orderable Nexperia MPN. Approved.
>    * **D23 — confirmed SMB (DO-214AA).** You are right that the bare `SS56` is normally SMA;
>      our land pattern is DO-214AA and the SMA part will not fit. Please fit an SMB 5 A / 60 V
>      Schottky — BORN `SS56` SMB (**LCSC C2687867**) or Diotec `SK56` — and **not** the
>      DO-214AC version.
>    * **U1 — customer supplied, no substitutes needed.** We buy the Teensy 4.1 *with pins* and
>      plug it in. Please source and fit **2 × `SSW-124-01-G-S`** (1×24, 2.54 mm, THT female
>      socket) and do **not** source or fit a Teensy.
>    * **BT1 — `1058` is correct.** That is the Keystone SMD 20 mm coin-cell *holder*;
>      "CR2032" is the *cell*, which is not in the BOM. Please supply the holder and **do not
>      fit the cell**.
>    * **H1–H4 and TP1–TP5 — no part, nothing to place, no assembly charge.** They are 3.2 mm
>      non-plated mounting holes and 2.0 mm plated through-hole test pads — features of the PCB.
>      They are no longer BOM rows in Rev F at all, so your part matching has nothing to stall
>      on. The "M3 insulated hardware" note is customer-fitted.
>
> Also please **re-price `J8`**: the part is now `901-143-6RFX` (Amphenol RF) — the brass-body
> variant of the same SMA jack, identical footprint and PCB hole pattern. Your $41.123 each
> was ~1.5–2× the distributor price; the `-6RFX` should be around $6.84 at 5 pcs.
>
> What we need back: the completed **Rev F** quotation at **5 units** (we will confirm the
> final quantity once the parts are settled), with the lead time for every line — we only need
> the longest one to plan around. The `Component Cost` on the sheet is marked **INCOMPLETE** on
> purpose: six lines (D23, U1, U21, D25, R58/R59, J8) still need your price, and the rest are
> your own Rev E figures carried forward for reference only.
>
> One warning that matters more than the price: **`U21` must not be substituted.** It is a TI
> `TCAN1042HGVDRQ1` and must come from authorized distribution — no clones, no re-marked parts.
> The **`V` suffix is mandatory** (it is the VIO pin, pin 5); a non-V `TCAN1042` has pin 5 = NC
> and would put 5 V on the 3.3 V Teensy. Approved alternates, in order: `TCAN1042VDRQ1`, then
> NXP `TJA1051T/3/1J` (its pin 8 `S` must also go to GND). This is not theoretical: the module
> this replaces was a **mislabelled** part (a TJA1051T/3-class chip sold as an SN65HVD230) and
> that pinout difference is exactly what failed on the bench.
>
> Thanks,
> Chris

---

## 2. What is in the workbook

| Tab | Contents |
|---|---|
| `RevF BOM Quotation` | Your column layout, all **77 Rev F BOM lines**, `Customer Reply` filled on the 17 lines that need it. Prices/lead times are **your Rev E figures carried forward** where the part is unchanged; six lines are blank and the `Component Cost` row says INCOMPLETE. |
| `Your questions` | Your six notes verbatim + our answer + the Rev F item number each one is now on, plus the J8 price, the U8 price confirmation, the lead-time flags and the quantity question. |
| `Rev E to Rev F` | What was **added** (U21, D25, J14, JP2, R58/R59, C49–C52, TP6/TP7), **removed** (J7, C8, and the never-a-part H1–H4 / TP1–TP5), **changed** (U1, J8, D23 and the merged lines), and a **line-number map** for the rows that moved. |

## 3. How the sheet was built (and how to rebuild it)

`hardware/teensy-integrated-revf/pcbway/make_pcbway_quotation_reply.py` reads

* the Rev E quotation `.xls` (newest `hardware/Quotation for …RevE-BOM-pcbway-assembly*.xls`),
* `Racecar-RevF-BOM-pcbway-assembly.csv` (the single source of truth for the Rev F lines),

matches Rev E rows to Rev F rows by designators (and MPN), carries PCBWay's own note, price
and lead time forward wherever the part is unchanged, adds our `Customer Reply` text, and
writes both `.xlsx` and `.xls`.

```bash
python3 hardware/teensy-integrated-revf/pcbway/make_pcbway_quotation_reply.py            # 5 units, today's date
python3 hardware/teensy-integrated-revf/pcbway/make_pcbway_quotation_reply.py --qty 10 --date 2026-10-12
```

Re-run it after any change to `Racecar-RevF-BOM-pcbway-assembly.csv` so the quotation can
never drift from the BOM PCBWay actually imports.

## 4. Still open (not answered by this document)

* The **final quantity** — PCBWay asked to settle the part questions first, and 5 units is
  quoted here. Nothing in the repo records a different target, so confirm before ordering.
* **U21 incoming inspection** — match the top marking against TI's *Device Marking* for
  `TCAN1042HGVDRQ1` and photograph it into `logs/` (procedure in `../design/BRINGUP.md`).
* The Rev F package is still an **unapproved engineering prototype**: CAD checks pass, but
  there is no independent human review and no physical, thermal or automotive validation.

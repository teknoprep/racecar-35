# Reply to PCBWay — 2026-10-22 (BOM checked, Gerber uploaded, 1 unit)

**Their email (2026-10-22 02:45):** *"Sorry for the mistake. Kindly please upload your Gerber
file to the [under review]. And please carefully check the attachment, we will just follow the
final BOM list."*

So: (1) the gerber goes into the under-review slot, (2) **the attached 1-unit Rev F BOM
workbook is the contract** — we check it line by line and answer in its `Customer Reply`
column.

| What to send | File |
|---|---|
| Upload to **[under review]** | `pcbway/Racecar-RevF-PCBWAY-GERBERS.zip` (16 files, complete set, latest 2026-10-19 silk) — sha256 `fa1142a4…5291d` |
| Attach to the reply / portal | `hardware/Quotation for T-2SJ3W1113248A-1unit-Racecar-RevF-BOM-pcbway-assembly(2026-10-22)-REPLIED.xls` (`.xlsx` alongside) — sha256 `6b0e89c1…3450c` |
| Optional | `Racecar-RevF-PCBWAY-UPLOAD-ALL-FILES.zip` (everything incl. engineering package, `ef4e5502…c329a`) |

Both are rebuilt by `pcbway/make_package.py` and `pcbway/update_quotation_reply.py`.

---

## 1. The BOM check — result

Their 2026-10-22 attachment is **our Rev F BOM, line for line**: 77 lines, same MPN,
manufacturer, designators and quantities, with **one intended correction — U1 quantity 2**
(two 1×24 `SSW-124-01-G-S` sockets for the single 2×24 `U1` footprint). That is right and is
confirmed in the sheet.

`update_quotation_reply.py` re-verifies this every time by loading the quotation and comparing
all 77 rows against `Racecar-RevF-BOM-pcbway-assembly.csv`; it **aborts** on any MPN /
manufacturer / designator / qty difference (U1's 2 accepted explicitly). It then fills:

* **column K, `Actual Purchase Mfg Part #`** on all 77 lines (our exact MPN — one part per line),
* **column M, `Customer Reply`** on the lines that need a decision:

| Item | Ref | Reply |
|---|---|---|
| 3 | D24 | Yes — `BZX84-C15,215` (Nexperia) is correct |
| 7 | D23 | **SMB (DO-214AA) only** — BORN `SS56` SMB (LCSC C2687867) or Diotec `SK56`; **not** the SMA part |
| 17 | U3 U6 | Genuine TRACO `TSR 1-2433` only, no clone/second-source |
| 26 | JP1 JP2 | Headers only, **no shunts** (we fit JP1 after rail tests, JP2 only at a bus end) |
| 27 | U1 | Qty **2 confirmed** — two 1×24 sockets; **do not fit a Teensy** |
| 28 | BT1 | `1058` holder **only**, no CR2032 cell |
| 30 | U21 | `TCAN1042HGVDRQ1` correct; **`V` suffix mandatory**; other part numbers are fallbacks needing written approval; authorized distribution only |
| 41 | U8 | Price accepted subject to order-day price; tell us the delta before ordering |

Plus a `Customer Reply` tab carrying the confirmed build spec (4 layer, 150.05 × 155.05 mm,
1.6 mm, 1 oz outer, ENIG, green/white, 50 Ω impedance control, no panelisation; the 48 × 6 mm
notch is profile) and the commercial answer below.

---

## 2. Email / portal text (paste)

> Hi,
>
> Thank you — no problem. The complete Gerber set is uploaded to the under-review slot:
> **`Racecar-RevF-PCBWAY-GERBERS.zip`**, 16 files (4 copper layers, both masks, both pastes,
> both silkscreens, Edge_Cuts, PTH + NPTH drills with maps, .gbrjob). It already contains the
> updated silkscreen.
>
> We have checked your attached BOM line by line: it is our final Rev F BOM — 77 lines, same
> MPN, manufacturer, designators and quantities — with one intended correction, **U1 quantity 2**
> (two 1×24 sockets for the one 2×24 footprint). That is correct; please keep it at 2 and do not
> fit a Teensy.
>
> The returned workbook has **`Actual Purchase Mfg Part #` filled on all 77 lines** and
> **`Customer Reply`** on the lines that need a decision (D24, D23, TRACO U3/U6, JP1/JP2, U1,
> BT1, U21, U8). The important ones: **U21 must be `TCAN1042HGVDRQ1`** (the `V` suffix is the
> VIO pin; no substitution, authorized distribution only) and **D23 must be the SMB /
> DO-214AA `SS56`**, not the SMA part.
>
> To finish the quotation we only need: the **PCB price** for the 5-piece minimum (or the board
> quantity you recommend) added to the 1-unit component + assembly prices already shown, the
> **longest lead time**, and — for reference — the assembly total at **5 units**. Then send the
> proforma and we will pay.
>
> Thanks,
> Chris

---

## 3. Open questions this reply asks PCBWay to close

1. **PCB price** — the sheet's `PCB Cost` row is still empty ("please upload your Gerber…"); now
   that the gerber is uploaded, add it so `All Total` is meaningful. Their PCB minimum is 5 pcs;
   the assembly quote is for **1** unit.
2. **Assembly at 5 units** — we may want more boards than one assembled unit; just for reference.
3. **Longest lead time** across the 77 lines.

Nothing else is outstanding on our side: the gerber is final (2026-10-19 silk baked in), the BOM
and CPL are 1:1 (182 designators), and the engineering package travels with the upload bundle.

---

## 4. One-folder bundle for the order

`make_production_folder.py` assembles **`pcbway/PRODUCTION-READY-<date>/`** (and a
`.zip` of it) with everything above plus the unpacked `gerbers/`, the engineering
package, `docs/ASSEMBLY.md` / `ASSEMBLY-EXTRAS.csv` / `BRINGUP.md`, a
`README-PRODUCTION.md` index and `SHA256SUMS.txt`. It hard-fails unless the gerber
ZIP is the complete 16-file set **and** carries the verified 2026-10-19 silk.

```bash
python3 pcbway/make_production_folder.py            # -> PRODUCTION-READY-<today>/
(cd pcbway/PRODUCTION-READY-<today> && zip -qr ../PRODUCTION-READY-<today>.zip .)
```

---

## 5. Rebuild / re-check

```bash
cd hardware/teensy-integrated-revf
python3 pcbway/make_package.py                 # gerbers (+ 2026-10-19 silk), BOM, CPL, MPN list, bundle
python3 pcbway/update_quotation_reply.py       # newest quotation -> ...-REPLIED.xls/.xlsx
```

`update_quotation_reply.py` picks the **newest** Rev F quotation **by mtime** (PCBWay reissues
the same project under a new date/quantity — sorting by filename would grab the old 5-unit
sheet), verifies all 77 lines against the BOM, and aborts on any difference.

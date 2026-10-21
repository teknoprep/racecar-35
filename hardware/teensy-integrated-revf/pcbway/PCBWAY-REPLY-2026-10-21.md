# Reply to PCBWay — 2026-10-20 email (two projects)

**Their email (2026-10-20 16:41):** they checked all our emails, changed the status of
project **`#W1113248AS2SJ2`** so we can upload the **complete file set (not 1 layer)**, and for
quotation **`T-2SJ3W1113248A`** they sent a BOM list asking us to **double-check the PCB notes
and the `Actual Purchase Mfg Part #`**, replying in the **Customer Reply** column.

**Built 2026-10-21** by `python3 pcbway/make_package.py` from `design/` (which already contains
the 2026-10-19 silkscreen), so the package and the upload bundle cannot drift.

| Artifact | SHA-256 | Size |
|---|---|---|
| `Racecar-RevF-PCBWAY-UPLOAD-ALL-FILES.zip` | `ef4e550295944a362052172772c906d6ffd61d09308d73dcce51ccffc57c329a` | 2,752,008 |
| `Racecar-RevF-PCBWAY-GERBERS.zip` (inside it) | `fa1142a474f45054db8533c76e05bd7ebe7dbf066f76c5ad2eb29bcb1925291d` | 381,686 |

`SHA256SUMS.txt` inside the bundle lists every member. A rebuild only changes the creation
date stamp inside the gerbers, so treat the hashes above as "this upload", not a fixed value.

---

## 0. ⚠️ The newest BOM list in `hardware/` is NOT this board

`Quotation for  T-6C2W1148674A-5units-Parts+list+BOM(2026-10-21).xls` is **a different design**:
9 lines — a 2×20 Raspberry-Pi HAT socket (`SSW-120-01-G-D` / `TSW-120-07-G-D`), an NXP
**`PCA9955BTW`** 24-channel LED driver, CREE **`CLW6A`** RGBA LEDs, a JST XH 8-pin.
Rev F is 77 lines: Phoenix field terminals, TI `TCAN1042HGVDRQ1`, `TPS54560BDDAR`, u-blox
`NEO-M9N-00B`, Teensy sockets. Its only question is *"The part we will supply is PCA9955BTWJ
OK?"* — that is not our BOM, so **do not fill it with Rev F answers**. Either it is another
project or it was attached by mistake; ask (text in §3) before touching it.

---

## 1. Upload for `#W1113248AS2SJ2` — this is the COMPLETE set

Upload **`Racecar-RevF-PCBWAY-UPLOAD-ALL-FILES.zip`** (or, if that slot accepts gerbers only,
`Racecar-RevF-PCBWAY-GERBERS.zip`). The gerber ZIP is **16 files — every layer**, not one:

```
F_Cu  In1_Cu  In2_Cu  B_Cu          (4 copper)
F_Mask  B_Mask                      (both masks)
F_Paste  B_Paste                    (both pastes)
F_Silkscreen  B_Silkscreen          (both silkscreens — the 2026-10-19 legends are HERE)
Edge_Cuts
<PTH>.drl + PTH-drl_map.pdf         (plated drill + map)
<NPTH>.drl + NPTH-drl_map.pdf       (non-plated drill + map)
<job>.gbrjob                        (layer/job manifest)
```

What changed since the 2026-10-11 upload, and what did **not**:

* **`F_Silkscreen` is the only layer whose geometry changed.** Verified mechanically by
  comparing every layer of the old and new package with creation-date comments stripped:
  4 copper, both masks, both pastes, `Edge_Cuts`, `B_Cu`/`B_Silkscreen`, and **both Excellon
  drill files are byte-identical in content**. The drill-map PDFs and the `.gbrjob` differ only
  in their embedded creation date; the regenerated `F_Silkscreen` is geometrically identical to
  the standalone replacement layer we sent (`addon-silkscreen/verify_addon.py`).
* Nothing moved, no copper/mask/drill edit, same stackup, same outline, same BOM/CPL.

---

## 2. Customer Reply text (paste into the `Customer Reply` column / portal box)

> **Complete files:** the full 16-file gerber set is uploaded for this project — all 4 copper
> layers, both masks, both pastes, both silkscreens, Edge_Cuts, PTH + NPTH drills with maps and
> the .gbrjob. Nothing is a single-layer upload: the 2026-10-19 update changed only the
> **F.Silkscreen** layer, and it is now baked into the complete package we have uploaded.
>
> **PCB notes — please build exactly this:** FR-4, **4 layers**, **150.05 × 155.05 mm**,
> **1.6 mm**, **1 oz outer copper** (inner layers per your standard 4-layer stackup), **ENIG**,
> **green** mask, **white**
> silk, **impedance control ON** (50 Ω single-ended on the GNSS feed), **no panelisation**
> (connectors and the coin-cell holder sit on the edges), 0.20 mm min track / 0.15 mm min
> spacing, 0.30 mm min hole. The 48 × 6 mm notch in the bottom edge is part of `Edge_Cuts`
> (profile routing), not an internal slot.
>
> **Actual Purchase Mfg Part #:** every line's exact part is listed in
> `Racecar-RevF-ACTUAL-PURCHASE-MPN.csv` (77 lines, same numbering as the BOM), and the same
> MPN + manufacturer is in the BOM's own columns — there is exactly one part per line, no
> "equivalent" needed. The notes that matter:
> * **U21 `TCAN1042HGVDRQ1` (TI)** — authorized distribution only, **no substitution**. The
>   **`V` suffix is mandatory** (it is the VIO pin 5). Approved alternates, in order:
>   `TCAN1042VDRQ1`, then NXP `TJA1051T/3/1J` (pin 8 `S` must go to GND). Never an SN65HVD23x,
>   a plain `TJA1051T`, a `TJA1050` or a clone. Pin 8 is tied to GND on the board.
> * **D23 `SS56` — must be SMB (DO-214AA)**, e.g. BORN `SS56` SMB (LCSC C2687867) or Diotec
>   `SK56`. Do **not** fit the SMA / DO-214AC `SS56`.
> * **U1** — fit **2 × `SSW-124-01-G-S`** sockets only. The Teensy 4.1 is customer supplied
>   (with pins) and plugs in: do not source or fit a Teensy.
> * **JP1 / JP2** — fit the `TSW-102-07-G-S` headers, **no shunts** (customer fits them).
> * **BT1** — fit the Keystone `1058` **holder only**, no CR2032 cell.
> * **J9** — fit the JST `B2B-XH-A(LF)(SN)` connector only; the two-wire lead is customer-made.
> * **H1–H4 / TP1–TP7** are PCB features (NPTH mounting holes, bare-copper test pads): no part,
>   no placement, not in the BOM.
> * Previously agreed parts stand: **D5 `PESD5V0F1BL,315`**, **D2 `SS14` (onsemi)**,
>   **F3–F5 `1206L010/60WR`**, C10/C11/C18 as listed, **U8 `NEO-M9N-00B`** (confirm final
>   price), genuine TRACO **`TSR 1-2433`**, **J8 `901-143-6RFX`** (brass-body SMA; please
>   re-price — the earlier $41.123 each was ~1.5–2× distributor).
>
> BOM and CPL correspond **1:1 (182 designators each)**. The 3D/engineering package, order
> guide and the full changes list are inside the uploaded ZIP.

---

## 3. Ask about `T-6C2W1148674A`

> We received `Quotation for T-6C2W1148674A-5units-Parts+list+BOM(2026-10-21).xls`, but its BOM
> is not our board (PCA9955BTW / CREE CLW6A / 2×20 Pi-HAT socket — 9 lines). Our project is
> `T-2SJ3W1113248A` / `W1113248AS2SJ2` (77 lines: Phoenix terminals, TCAN1042HGVDRQ1,
> TPS54560BDDAR, NEO-M9N-00B). Please confirm which project that sheet belongs to; if you meant
> our Rev F, the BOM to use is `Racecar-RevF-BOM-pcbway-assembly.csv` (attached, with the
> `Actual Purchase Mfg Part #` list beside it).

---

## 4. Why the reply is safe to send

* The package is **regenerated from the routed CAD** in this repo (not hand-edited):
  `make_package.py` re-exports the gerbers/drills with `kicad-cli`, rebuilds the BOM from
  `components.json`, exports the CPL from the same PCB, and **aborts** if the BOM and CPL
  designator sets differ or if the engineering ZIP is older than the PCB.
* `Racecar-RevF-ACTUAL-PURCHASE-MPN.csv` is emitted from the same rows as the BOM — one exact
  MPN per line, no second list to go stale.
* The only silk change is the one already verified by `addon-silkscreen/verify_addon.py`
  (all non-silk layers identical to the uploaded package).
* Still an **unapproved engineering prototype**: CAD checks pass (ERC/DRC/unconnected 0,
  1,078 assertions), but there is no independent human review and no physical, thermal or
  automotive validation. Do not present it as reviewed.

---

## 5. Rebuild / re-verify

```bash
cd hardware/teensy-integrated-revf
python3 pcbway/make_package.py          # gerbers + BOM + CPL + MPN list + upload bundle
python3 pcbway/addon-silkscreen/verify_addon.py \
        pcbway/Racecar-RevF-PCBWAY-GERBERS.zip   # reports the layer list (PASS needs an export dir)
```

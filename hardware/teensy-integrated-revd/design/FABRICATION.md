# Rev D — ENGINEERING-PROTOTYPE fabrication specification

**UNBUILT, UNAPPROVED, NOT INDEPENDENTLY REVIEWED.** This is a first-build
electrical prototype, not an automotive-qualified product. It passes CAD checks
(ERC/DRC zero, exact schematic/netlist pad equality) but has had no independent
review, no fabricator DFM sign-off and no physical validation. Do not order
production quantities and do not put it in a vehicle before the `BRINGUP.md`
sequence. A clean CAD report is not proof that an unbuilt board works.

## Board and process

- **Envelope 150 × 155 mm** (Edge.Cuts 150.05 × 155.05 with the 0.05 mm outline
  stroke). Use the supplied Edge.Cuts, including the **48 × 6 mm internal-WiFi
  antenna notch** cut into the bottom edge at x = 44…92. Never manufacture the
  netless illustration under `../teensy-integrated-revc/preview/`.
- **Four copper layers**, 1.60 mm finished FR-4, Tg ≥ 150 °C, **ENIG**, green mask,
  white legend, lead-free process compatible with the supplied modules.
  - `F.Cu` — signals + GND pour
  - `In1.Cu` — **continuous GND reference plane, no routing** (declared to the
    router as a plane layer so signals cannot consume it)
  - `In2.Cu` — signals
  - `B.Cu` — signals + GND pour
- **1 oz / 35 µm outer copper.** That is adequate because the screen branch is fused
  at **3 A** (`F2`, Littelfuse `0451003.MRL`) and the `+5V_MAIN` trunk is 3.5 mm
  wide. Do **not** raise the screen fuse back to 4 A on this build, and do not
  silently substitute a thinner outer copper weight. Inner layers use the
  fabricator's standard 4-layer construction.
- Design rules actually used: **0.20 mm minimum track width and 0.15 mm minimum
  clearance** (CAD rule floor 0.15 mm), 0.30 mm copper-to-edge, 0.30 mm minimum
  hole, 0.25 mm hole-to-hole and hole-to-copper, **0.60 mm via land / 0.30 mm via
  drill** (0.15 mm annular ring), vias **tented** on both sides.
- Separate metric Excellon **PTH and NPTH** files. Four **3.2 mm NPTH** mounting
  holes. The ESP32 ground-pad vias are 0.30 mm drills / 0.60 mm lands — not laser
  microvias. Do not plug/change land connectivity.
- Main 5 V / screen and return trunks are deliberately wide (up to 3.5 mm); local
  regulator pad fanouts are narrower parallel branches. **Do not let CAM thin these
  into signal tracks.** 5 A is a TOTAL supply design target, not a tested continuous
  rating.
- Four 3.2 mm NPTH mounting holes; insulated standoffs, clearance below soldered
  leads. Do not clamp a metal enclosure against traces, the cell holder or headers.
- Inspect all mask slivers, annular rings, plane necks, module lands, drill
  alignment, outline and silkscreen in the CAM viewer before ordering.

## Impedance control — the GNSS feed

Order with **impedance control ON** and state the target: **50 Ω single-ended on
`RF_ANT` / `RF_GNSS`, from J8 to the NEO-M9N `RF_IN`.**

The trace is **0.38 mm wide microstrip over `In1.Cu`**. With the standard 4-layer
1.6 mm construction (one 7628 prepreg each side of a 1.065 mm core → prepreg
H = 0.1955 mm, εr 4.2–4.6) that gives **49.6–50.6 Ω**. An `F.Cu` ground pour runs
alongside it at 0.15 mm, which makes the real impedance partly coplanar and pulls it
slightly below 50 Ω.

**Ask for the finished stackup first.** If the L1→L2 dielectric is not ≈0.1955 mm
the width must change: 0.10 mm → 0.19 mm, 0.15 mm → 0.29 mm, 0.20 mm → 0.38 mm,
0.25 mm → 0.48 mm, 0.36 mm → 0.69 mm (50 Ω at εr 4.4). This is a starting geometry,
not an impedance certificate; coupon/VNA measurement remains outstanding.

## RF / enclosure requirements

- WiFi is **ESP32-S3-WROOM-1-N8R2 with its built-in PCB antenna**. Its stock
  all-layer antenna keepout is retained and the carrier is notched away beneath the
  antenna tip. **No copper, vias, traces, screws, battery, cable or metal enclosure
  may intrude into the antenna region.** Use a plastic enclosure or an
  RF-transparent window; a closed metal trunk/enclosure can still block the signal.
- The **SMA is GPS only**, Amphenol 901-143. Inspect its mechanical drawing against
  the actual board edge, solder tails and enclosure hole before assembly.
- Active GPS antenna: **3.3 V bias**, current limited by a TPS2553 whose ILIM is tied
  to IN → datasheet-limited to **50 / 75 / 100 mA** (min/typ/max). Specify an antenna
  whose draw works at the minimum limit. The external gauge, display and WiFi antenna
  are **not** powered through this SMA.

## Before an order

Obtain independent schematic/footprint/power-off review and fabricator DFM feedback.
Check availability of genuine **ICM-42670-P** (its land pattern is derived from
DS-000451, not vendor-published), **NEO-M9N-00B**, **ESP32-S3-WROOM-1-N8R2**,
**Pololu 4091 / D36V50F5** and genuine **TRACO TSR 1-2433** (a same-name LCSC
module is not TRACO Power). No pin-compatible-looking substitutions without a
design/firmware revision.

Firmware for the ICM-42670-P, the throttle/brake channels, the Pi 5 video link and
the ESP32 network coprocessor **does not exist**, and the revised oil/coolant
conversions are not implemented. See `ASSEMBLY.md` and `DESIGN_NOTES.md`. The
archive's Rev A Gerbers and the Rev C illustration are not substitutes for these
layers.

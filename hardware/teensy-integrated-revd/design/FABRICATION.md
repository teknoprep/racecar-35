# Rev C — ENGINEERING PROTOTYPE fabrication specification

This is a first-build electrical prototype, not an automotive-qualified product.
The engineering export contains real copper, solder mask, paste, outline and drill
files. **A clean CAD report is not independent electrical approval, a load test,
or proof that this unbuilt board works.** Do not order production quantities.
The normal reviewed-release exporter remains separately gated.

## Board and process

- Envelope **130 × 140 mm**; use the supplied Edge.Cuts, including the **48 × 6 mm
  internal-WiFi antenna notch**. Never manufacture the former netless preview PCB.
- Two copper layers, **70 µm / 2 oz finished copper on BOTH sides**; 1.60 mm finished
  FR-4, Tg ≥150 °C. Quote capability for **0.15 mm copper clearance**, 0.20 mm signal
  routing, and the fine-pitch IC lands. Do not silently substitute 1 oz copper.
- ENIG, green mask, white legend. Lead-free process compatible with actual supplied
  modules/components. No automatic mirroring/scaling; all outputs share PCB origin.
- Separate metric Excellon **PTH and NPTH** files. The ESP32 ground-pad vias are
  0.30 mm drills / 0.60 mm lands, not laser microvias. Tented/plugged process must
  prevent paste draining under the module without changing land connectivity.
- Four 3.2 mm NPTH mounting holes; insulated standoffs, clearance below soldered
  leads. Do not clamp a metal enclosure against traces, cell holder, or headers.
- Main 5 V/screen and return trunks are deliberately wide; local regulator pad
  fanouts are narrower parallel branches. Do not let CAM thin these into signal
  tracks. **5 A is a TOTAL supply design target, not a tested continuous rating.**
- Inspect all mask slivers, annular rings, plane necks, module lands, drill alignment,
  outline and silkscreen in the CAM viewer before quoting/ordering.

## RF / enclosure requirements

- WiFi is **ESP32-S3-WROOM-1-N8R2 with its built-in PCB antenna**. Its stock all-layer
  antenna keepout is retained. No copper, vias, traces, screws, battery, cable or
  metal enclosure may intrude into the antenna region. The carrier is notched away
  beneath the antenna tip. Use a plastic enclosure/RF-transparent window; a closed
  metal trunk/enclosure can still block the signal.
- The **SMA is GPS only**, Amphenol 901-143. Inspect its mechanical drawing against
  the actual board edge, solder tails and enclosure hole before assembly.
- GNSS main RF traces target **50 Ω grounded coplanar waveguide**: nominal 0.80 mm
  width / 0.15 mm copper gap, with short 0.30 mm necks at tiny RF components. B-side
  routing/via keepout preserves the reference plane; stitching vias flank it.
  **These dimensions are a starting geometry, not an impedance certificate.** CAM
  must check the actual laminate Dk, finished copper and dielectric thickness and
  report the predicted impedance. Do not change the stackup or RF geometry without
  revising CAD and rerunning the checks. Coupon/VNA and GNSS reception tests remain.
- Active GPS antenna: **3.3 V bias**, current limited (TPS2553, nominal 75 mA setting,
  worst-case 50–100 mA). Specify an antenna whose draw works at the minimum limit.
  The external gauge, display and WiFi antenna are not powered through this SMA.

## Before an order

Obtain independent schematic/footprint/power-off review and fabricator DFM feedback.
Check availability of genuine **MPU-6050** (legacy/lifecycle risk) and NEO-M9N modules;
**no pin-compatible-looking substitutions** without a design/firmware revision.
The archive's Rev A Gerbers and the earlier Rev C illustration are not substitutes.
Firmware for the new WiFi processor and revised analog conversions is not supplied
by these manufacturing layers; see ASSEMBLY.md and DESIGN_NOTES.md.

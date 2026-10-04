# Rev F electrical choices and limitations

## What is real

The `design/` PCB has assigned nets, components and routed copper: **193 footprints,
104 nets, 1,647 routed segments, 131 vias**. Its native schematic uses manufacturer
pin/function symbols, actual pin numbers and electrical types. A full connected-pad
comparison runs in addition to ERC and DRC (KiCad 9.0.8): **0 ERC violations, 0 DRC
violations, 0 unconnected items, 749 pin/geometry/calculation
assertions, 538 schematic ↔ PCB connected pads agree** (multi-pad through-hole pins
collapse to a single entry).

Those are statements about files, not about a board. Nothing here has been built,
measured or independently reviewed.

The netless illustration `../teensy-integrated-revc/preview/PLACEMENT-ONLY-NOT-FOR-FAB.kicad_pcb`
and the Rev A carrier are separate, obsolete artefacts and are not sources for these
Gerbers.

## Selected circuits

- **Power:** our own **TI TPS54560B** 60 V / 5 A asynchronous buck at 400 kHz on a
  **5 A TOTAL** design budget, fed through a 5 A fuse. It is built from TI's own 5 V/5 A
design example (SLVSBN0C 8.2) with the input narrowed to the car's real 10–20 V
  (`RT/CLK` 240 k, feedback 52.3 k/10 k, UVLO 680 k/100 k ≈ 9.4 V start, compensation
  16.9 k + 4.7 n + 47 p, 3 × 47 µF output, 6.8 µH inductor, `SS56` catch diode). A
  **P-channel Q3 (`AO4485`)** provides reverse-polarity protection with its drain on the
  raw input and source on the protected load. **The Pololu D36V50F5 module and its 2×6
  socket are gone.** The protected input (`VIN_PROTECTED`) feeds **two separate TRACO
  TSR 1-2433** 3.3 V regulators — AUX (GPS) and NET (WiFi) — so neither spends the
  Teensy's 3V3 budget. Screen branch fused at **3 A**. C1 (100 µF / 50 V) sits on
  `VIN_PROTECTED`, i.e. **after** reverse protection, not on the raw input. Raw input TVS
  is SMBJ20CA. D2 (SS14) + JP1 feed the Teensy VIN, and the Teensy's own VUSB–VIN bridge
  must be cut for dual power. Nothing here establishes ISO 7637 / ISO 16750 or load-dump
  qualification, and the asynchronous buck runs hotter than the module it replaced —
  continuous 5 A stays gated on bench thermal testing.
- **CAN (Rev F):** `U21` = **TI TCAN1042HGV-Q1** soldered down (SOIC-8, AEC-Q100, ±70 V
  bus fault, `VIO` on pin 5). `TXD`/`RXD` go to Teensy pins 22/23 (U1 pads 29/28),
  `VCC` = `+5V_MAIN`, `VIO` = `+3V3_MCU`, and **`STB` (pin 8) is hard-tied to GND** —
  standby is receive-only with no ACK, the failure that removed the external SN65HVD230
  module (with `TXD` held low it received but never drove `CANH-CANL` off 0 V). `J14`
  (Phoenix 1729021) carries **1 CANH / 2 CANL / 3 GND**; `R58`+`R59` are a switchable
  **split 120.8 Ω** termination (2 × 60.4 Ω 1 %) with `C52` 4.7 nF to GND, enabled by the
  `JP2` shunt **only at a bus end**; `D25` (`NUP2105L`) is the bus TVS; `TP6`/`TP7` are
  `CAN_TX`/`CAN_RX`. **No firmware change** — 500 kbit/s, normal ACKing mode. CAN needs the
  5 V rail, so it is dead on a Teensy-USB-only bench. The TCAN1042's TXD dominant time-out
  cuts the firmware's `CANHOLD` diagnostic short; that is expected. No automotive
  transient qualification is claimed. **Layout note:** the autorouter routed
  `CAN_TX`/`CAN_RX` at roughly **80–90 mm** across two inner layers, and `CANH`/`CANL` at
  roughly **30–45 mm**, with `U21`↔`J14` ≈23 mm. That is longer than a hand layout would
  give and is a known limitation of this pass — electrically harmless at 500 kbit/s
  (≈0.5 ns of flight time into a CMOS transceiver input), but a future revision should
  shorten the logic-side pair to U1 pads 29/28. The optional common-mode choke (TDK
  `ACT45B-510-2P-TL003` + 0 Ω bypass links) is omitted for want of space beside the
  transceiver; add it only if emission measurements require it.
- **Teensy:** socketed 4.1 (inspect pin 1 and seating). Outer-row pin contract:
  0/1 Pi 5 video, 5 NET IRQ, 6 NET reset (active high), 7/8 GPS, 9 tach, 10–13 NET
  SPI, 14/15 screen, 18/19 IMU I²C, 20 AFR, 21 throttle, 24 brake, 16/17 oil/coolant,
  22/23 CAN1, 25 VIN. **Teensy pin 13 is NET_SCK — the firmware's pin-13 heartbeat
  LED must be disabled before SPI is used.**
- **GPS:** soldered NEO-M9N-00B. `D_SEL` high selects UART, `V_USB` grounded (USB
  unused), `V_BCKP` on AUX (not the RTC cell), `VCC_RF` / `SAFEBOOT_N` / `RESET_N`
  left open. UART at pins 7/8 through cross-domain Ioff buffers (SN74LVC2G17 in,
  SN74LVC1G125 out with OE grounded). External active-GPS SMA, 27 nH bias choke,
  10 Ω series, 100 pF DC block, PESD5V0F1BL ESD, TPS2553 bias switch (ILIM → IN =
  50/75/100 mA). RF geometry and antenna current still need measurement.
- **IMU:** **ICM-42670-P** — not an MPU-6050 — on I²C 18/19 at **0x68**: `AP_AD0`
  = GND, `AP_CS` = VDDIO selects I²C, `FSYNC` = GND, RESV pins to GND, INT1/INT2
  unconnected, polling only. The LGA-14 land pattern is **derived** from DS-000451
  (0.50 mm pitch, 0.26 × 0.55 mm pads, 1.5 mm and 1.0 mm pin spans, 0.14 mm mask
  web) and checks arithmetically against the package drawing and Figure 4 pin-out —
  it is not a vendor-published pattern. The current firmware is MPU-6050
  register-level and **will not work** with this part until a driver plus identity
  detection is written.
- **Tach:** conditioned ECU/cluster signal only. 180 kΩ / 22 kΩ divider (~202 kΩ DC
  loading), 2.5 V LM4040 clamp, 1 nF filtering, LM2903B comparator with positive
  feedback, MMBT3904 driving a VO617A-3 optocoupler, SN74LVC2G17 Schmitt buffer to
  pin 9. Input-referred trip points ≈ **1.84 V rising / 1.56 V falling** before
  tolerance, hysteresis from the 2.2 MΩ feedback resistor. Shared ground means the
  optocoupler does **not** make the system galvanically isolated. This is a design
  calculation, not a measured universal Miata interface.
- **Five analog inputs (oil, AFR, coolant, throttle, brake):** identical protected
  **0.500-gain** front ends — 20 kΩ / 20 kΩ 0.1 % divider, LM4040 3.0 V shunt,
  negative Schottky clamp, 1 kΩ / 100 nF filter, then TMUX1511 powered-off isolation
  (two devices, U15/U16). The clamps do not dump fault current into an unpowered MCU
  rail; the switch's own powered-off protection is limited to 3.6 V and depends on
  those clamps. At +20 V steady input the top resistor dissipates ≈14.5 mW and shunt
  current is ≈0.7 mA. Each sensor's +5 V output is separately PTC-fused (100 mA).
  These calculations are not an ESD, reverse-battery or fast-transient test.
- **Coolant:** diode-isolated 5 V feed → 150 Ω → LM4040 **4.096 V** shunt; 2.49 kΩ
  0.1 % sensor pull-up; half-gain sense divider, so the divider's **40 kΩ parallel
  loading** must be compensated. For measured sense-node voltage `V` *before* the
  divider: `R_sensor = 2490·V / (4.096 − V − 2490·V/40000)`. Open / short /
  out-of-domain results must be invalid, not plausible temperatures. A verified
  sender resistance/temperature curve and reference/gain calibration are required —
  the legacy VDO curve and the legacy 150 Ω / 3.3 V firmware conversion are **both
  wrong for this board**.
- **Screen:** fixed-direction SN74LVC1T45 buffers (`DIR` = VCCA for TX, GND for RX),
  100 Ω series resistors and PESD5V0S1BA UART ESD. Connects **only** to the verified
  CrowPanel Advance J10 5 V level-shifted UART. The long 921600-baud single-ended
  cable remains a bench/vehicle validation item; no RS485 claim.
- **WiFi:** internal-antenna ESP32-S3-WROOM-1-N8R2 on its own 3.3 V rail. SPI is
  Teensy 10/11/12/13 → module GPIO10/11/12/13 (the S3's FSPI pins), IRQ GPIO14,
  boot GPIO0. Teensy-side signals pass SN74LVC125A buffers with 33 Ω series
  resistors; module MISO and IRQ return through an SN74LVC2G125 (both have Ioff).
  Reset is an NPN (MMBT3904) pulling EN low from Teensy pin 6, with 10 kΩ / 1 µF on
  EN and 10 kΩ on BOOT. The 2×3 service header J10 is the network programming port
  (GND, 3V3 reference, TX from NET, RX to NET buffered, BOOT0, EN). All-layer metal
  keepout under the module antenna; grounded-coplanar GNSS routing is separate.
- **Video:** J13 (JST XH) → Teensy `Serial1` pins 0/1 at 115200, 1 kΩ series and
  ESD, deliberately separate from the 5 V screen UART.
- **RTC:** Keystone 1058 primary CR2032 holder with a removable two-wire lead (J9) to
  the Teensy's labelled auxiliary **VBAT / GND** pads — a required hand-assembly
  step. No charger, no second RTC chip, no whole-system backup or infinite-life
  claim, and no other load on the cell.

## Verification boundary

Automated checks can find shorts, disconnected pads, source/netlist disagreement,
wrong stated pin mappings and manufacturing-layer mistakes. They cannot prove
component authenticity, solderability, assembly fit, 5 A thermal performance, RF
link quality, automotive transients, clock accuracy, uploader throughput, MEMS
behaviour or firmware completeness. Independent electrical/footprint review and
physical tests remain required. No human approval record is invented by the
engineering exporter.

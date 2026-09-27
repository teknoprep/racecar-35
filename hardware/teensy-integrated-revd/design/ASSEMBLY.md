# Rev C — engineering-prototype assembly

Use the actual `BOM.csv`, `COMPONENT-ALIASES.json`, schematic, editable PCB and
placement CSV together. Function aliases are descriptive names used by source;
physical reference designators are fully annotated (U1, U2, R1…). Placement CSV
rotations are KiCad conventions: assembler must inspect IC pin 1 and polarity.
SMT/THT/module mixed assembly is required; a bare PCB is not a complete unit.

## External terminal bank — ALL face LEFT in top view

Pin numbers increase **top to bottom**; connector entry points outward.

| PCB connector | Function | Pins, top to bottom |
|---|---|---|
| J3 | TACH | 1 conditioned SIGNAL, 2 RETURN |
| J4 | OIL | 1 +5 V sensor OUT, 2 RETURN, 3 analog SIGNAL |
| J5 | COOLANT | 1 dedicated NTC lead, 2 RETURN |
| J6 | AEM AFR | 1 solid WHITE signal+, 2 BROWN reference return |
| J1 | POWER | 1 +10–20 V IN, 2 GND |
| J2 | SCREEN | 1 GND, 2 fused +5 V OUT, 3 RX FROM screen, 4 TX TO screen |

**J2 goes ONLY to the verified screen J10 5V_IN / TXD0_H / RXD0_H interface.** It
must NEVER feed the small screen HY2.0/J2 **3V3_OUT** pin. Verify the particular
screen's connector orientation and levels before connecting it. Disconnect both
UART conductors before a screen USB flash. These are TTL UART signals, not RS232.

TACH is for a **conditioned ECU/cluster signal only**, initially Spec Miata. No coil
negative, ignition/injector drive, VR pickup, or default pull-up into the vehicle.
Verify the specific car's waveform and pulses/revolution with a scope.

J6 takes the **AEM 30-0300 gauge** analog output, not an oxygen sensor. The gauge and
heater stay externally powered/fused per AEM (10–18 V gauge rating); do not power
them from this board or apply the recorder's full 20 V test input to the gauge.
The coolant sender is a dedicated two-wire NTC; do not tee into an ECU-biased sensor.

## Module / through-hole operations

1. Fit the BOM SMT parts with the verified manufacturer pinouts. Inspect bridges
   at the MPU, analog switch and logic buffers before any module is installed.
2. Fit all terminals, SMA, TRACO modules, electrolytics, fuse parts and service
   headers. Observe C1/C2 polarity. **C1 positive is on VIN_PROTECTED, after Pololu
   reverse-polarity protection**, not on raw/reversible input.
3. Pololu D36V50F5/4091: fit the complete raised 2×6 mating/header assembly as drawn;
   both output/ground/input columns carry current. Respect module underside clearance.
   AUX and NET use **separate TSR 1-2433 regulators from protected input**, not Teensy
   3V3. Do not tie those rails together or feed a 3.3 V TSR from marginal 5 V input.
4. Fit two 24-position 2.54 mm female Teensy sockets (e.g. Samtec
   **SSW-124-01-G-S**, two pieces), matching 24-pin male strips on Teensy. Confirm
   mating height and access to USB, SD and Program button. The module footprint is
   an envelope; inspect actual fit and solder-tail protrusion before populating a batch.
5. **Physically cut and verify the Teensy VUSB–VIN bridge before simultaneous USB
   and carrier power.** The carrier diode/jumper is NOT a substitute. Leave JP1 open
   until power-stage tests pass; fit a standard 2.54 mm shunt afterward.
6. RTC: J9 uses a short, keyed two-wire lead (JST XHP-2 housing and matching
   SXH-001T-P0.6 contacts) to the Teensy's labelled auxiliary **VBAT and GND pads**.
   J9 pin 1 → VBAT; pin 2 → GND. This manual lead is REQUIRED: the outer-row sockets
   do not carry VBAT. Leave Program and On/Off unconnected. Insulate/strain-relieve
   the lead; unplug it before removing Teensy. Do NOT connect it to adjacent 3V3.
7. Fit **Panasonic CR2032 primary 3 V cell** into Keystone 1058 only AFTER soldering
   and cleaning. Verify `+` polarity. No LIR2032, charger, direct soldering or reflow
   of a coin cell. The battery retains RTC only, not the processor/SD/GPS/WiFi/screen.
8. Fit the soldered NEO-M9N and ESP32 modules, respecting orientation, underside
   ground/paste and antenna clearance. Obtain genuine MPU-6050 stock; reject an
   unreviewed substitute, counterfeit or unknown salvage in a supplied assembly.

## Programming / firmware limitations — important

- Source/build version at this hardware checkpoint is **0.1.149**; last known bench
  installation was **0.1.147**. These files do not flash or publish anything.
- Existing firmware is NOT a complete Rev C product image. Oil input gain is now
  **0.500**, not the legacy 2/3; coolant uses **4.096 V / 2.49 kΩ excitation and a
  0.500 sense divider**, not the old 150 Ω circuit. New conversions/calibration and
  a verified sender curve are required before believing those displayed values.
- AEM retains the existing nominal 0.500 gain contract; calibrate measured ADC
  reference/gain and test fault/stale behavior. AEM setting remains default OFF.
- Network ESP32 is a third programmable MCU; its SPI upload/provisioning/recovery
  firmware is still required. Pin 13's old heartbeat must stop before SPI use.
  No faster-upload claim follows from merely populating the module.
- J10 **on this carrier** is the network programming header (not the screen J10):
  1 GND, 2 3V3 reference OUT, 3 TX from NET, 4 RX to NET, 5 BOOT0, 6 EN.
  Use 3.3 V UART; pins 5/6 may be grounded/open-drain ONLY. Do not power via pin 2.
  RX has powered-off isolation. Arrange manual BOOT/EN grounding or a proper fixture.
- Existing screen WiFi and screen-led OTA are retained architecturally, not replaced.
  RTC fresh-network-time validity/resynchronization work remains; a plausible date
  alone does not prove a trustworthy Internet time source.

Do not install in a car until the staged bring-up and independent review are complete.

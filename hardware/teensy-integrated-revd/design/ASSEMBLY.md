# Rev D — engineering-prototype assembly

Use the actual `BOM.csv`, `COMPONENT-ALIASES.json`, schematic, editable PCB and
placement CSV together. Function aliases are descriptive names used by source;
physical reference designators are fully annotated (U1, U2, R1…). Placement CSV
rotations are KiCad conventions: the assembler must inspect IC pin 1 and polarity.
SMT/THT/module mixed assembly is required; a bare PCB is not a complete unit.

## External terminal bank — ALL EIGHT face LEFT in top view

Pin numbers increase **top to bottom**; connector entries point outward.

| PCB connector | Function | Pins, top to bottom |
|---|---|---|
| J3 | TACH | 1 conditioned SIGNAL, 2 RETURN |
| J4 | OIL | 1 +5 V sensor OUT, 2 RETURN, 3 analog SIGNAL |
| J5 | COOLANT | 1 dedicated NTC lead, 2 RETURN |
| J6 | AEM AFR | 1 solid WHITE signal+, 2 BROWN reference return |
| J11 | THROTTLE | 1 +5 V sensor OUT, 2 RETURN, 3 analog SIGNAL |
| J12 | BRAKE | 1 +5 V sensor OUT, 2 RETURN, 3 analog SIGNAL |
| J1 | POWER | 1 +10–20 V IN, 2 GND |
| J2 | SCREEN | 1 GND, 2 fused +5 V OUT, 3 RX FROM screen, 4 TX TO screen |

Other field connectors: **J7** CAN module logic (3V3 / GND / CAN_TX / CAN_RX, JST
XH), **J8** GPS SMA, **J9** RTC cell lead, **J10** NET 3V3 service/programming 2×3
header, **J13** Pi 5 video UART (JST XH).

**J2 goes ONLY to the verified screen J10 5V_IN / TXD0_H / RXD0_H interface.** It must
NEVER feed the small screen HY2.0/J2 **3V3_OUT** pin — +5 V there destroys the
ESP32-S3. Verify the particular screen's connector orientation and levels before
connecting. Disconnect both UART conductors before a screen USB flash. These are TTL
UART signals, not RS232.

⚠️ **J4 / J11 / J12 supply +5 V to the sensor.** Connect only sensors that this board
powers. **Do not** parallel these +5 V pins onto a transducer the ECU or vehicle
harness already feeds — two supplies on one node can backfeed the other regulator.
If the sensor is already powered by the car, leave the recorder's +5 V pin
unconnected and take signal + ground only (verify the sensor's ground reference
first).

⚠️ **J5 is a dedicated two-wire NTC.** Do not tee into an ECU-biased coolant sender.

⚠️ **J6 takes the AEM 30-0300 gauge analog output, not an oxygen sensor.** The gauge
and its heater stay externally powered and fused per AEM (10–18 V gauge rating); do
not power them from this board, and do not apply the recorder's 20 V test input to
the gauge.

⚠️ **TACH (J3) is a conditioned ECU/cluster signal only.** No coil negative, ignition
or injector drive, VR pickup, or default pull-up into the vehicle. Verify the
specific car's waveform and pulses/revolution with a scope.

## Module / through-hole operations

1. Fit the BOM SMT parts with the verified manufacturer pinouts. Inspect bridges at
   the ICM-42670-P, the two analog switches (U15/U16) and the logic buffers before
   any module is installed.
2. Fit all terminals, SMA, TRACO modules, electrolytics, fuse parts and service
   headers. Observe C1/C2 polarity. **C1 positive is on VIN_PROTECTED, after Pololu
   reverse-polarity protection**, not on the raw/reversible input.
3. Pololu D36V50F5/4091 (**U2 is fitted as a SOCKET, not the module**): fit a **2×6
   0.1 in through-hole female socket** into the U2 footprint — 2.54 mm grid, **≥3 A per
   pin** (at the 5 A total budget each of VIN and VOUT is carried by 2 socket pins),
   standard 8.5 mm body so the module's underside components clear the carrier.
   Candidates: Samtec `SSW-106-01-G-D` (LCSC **C3323352**, 4.7 A/pin, 8.51 mm) or HCTL
   `PZ254-2-06-Z-8.5` (LCSC **C2894967**, 3 A/pin). Do **not** source or fit the Pololu
   4091 module: the customer supplies it and plugs it in. Both column pairs of
   VOUT/GND/VIN/VRP carry current. EN and PG are unconnected on the carrier — the
   module is enabled by default through its internal pull-up, and PG is
   open-drain/unused. Mounted stack height above the carrier is ~19 mm (8.5 mm socket
   + 9.5 mm module) — check enclosure clearance.

   ⚠️ **The module only fits one way round, and the other way destroys it** (VIN
   would meet the 5 V rail and VOUT would meet 10–20 V). Per Pololu's *"top view
   with labels"* drawing (image 0J10742): the **module's TOP (labelled) face — the
   side with the inductor, the diode and the two electrolytic capacitors — must face
   UP**, with its **VOUT column over U2 pad 1**, the only *rectangular* pad in the
   footprint (the north end of the courtyard, toward the terminal blocks), and EN/PG
   at the south end. On the module the four **square** plated holes are GND: the VOUT
   column is **one column in** from them and the EN/PG column is **three columns**
   from them, so the VOUT end is identifiable from either face. Verify this before
   power — see `ASSEMBLY-EXTRAS.csv`.
4. **U1 — fit two 1×24 2.54 mm female sockets; do NOT fit a Teensy.** Samtec
   `SSW-124-01-G-S` or any standard 1×24 socket, matching male pins on the Teensy.
   The socket is **mandatory for the RTC feature** (the J9 lead lands on the Teensy's
   underside VBAT/GND pads). If sockets cannot be sourced, **leave U1 completely
   unpopulated — do not substitute, and do not solder a Teensy down.** Confirm mating
   height and access to USB, SD and the Program button.
5. **Physically cut and verify the Teensy VUSB–VIN bridge before simultaneous USB and
   carrier power.** The carrier diode/jumper is NOT a substitute. Leave JP1 open until
   power-stage tests pass; fit a standard 2.54 mm shunt afterward.
6. RTC: J9 uses a short, keyed two-wire lead (JST XHP-2 housing, matching
   SXH-001T-P0.6 contacts) to the Teensy's labelled auxiliary **VBAT and GND** pads.
   J9 pin 1 → VBAT; pin 2 → GND. This manual lead is REQUIRED: the outer-row sockets
   do not carry VBAT. Leave Program and On/Off unconnected. Insulate and
   strain-relieve the lead; unplug it before removing the Teensy. Do NOT connect it
   to adjacent 3V3.
7. Fit the **Panasonic CR2032 primary 3 V cell** into the Keystone 1058 holder only
   AFTER soldering and cleaning. Verify `+` polarity. No LIR2032, no charger, no
   direct soldering or reflow of a coin cell. The battery retains RTC only — not the
   processor, SD, GPS, WiFi or screen.
8. Fit the soldered NEO-M9N and ESP32 modules, respecting orientation, underside
   ground/paste and antenna clearance (the module antenna sits over the board notch).
   Obtain a **genuine ICM-42670-P**; reject an unreviewed substitute, counterfeit or
   unknown salvage in a supplied assembly.

## Assembly extras (deliberately not BOM lines)

`ASSEMBLY-EXTRAS.csv` is the full list; the ones that matter:

| Qty | Part | Who |
|---|---|---|
| 2 | 1×24 2.54 mm female socket (Samtec SSW-124-01-G-S) into the U1 footprint | assembler, else customer |
| 2 | 1×24 2.54 mm male strip on the Teensy | customer |
| 1 | 2×6 0.1 in through-hole **female socket** for U2 (Samtec SSW-106-01-G-D) | assembler, else customer |
| 1 | Pololu 4091 module **+ its two 1×6 pin strips, plugged into that socket** | customer |
| 1 | CR2032 cell — never reflowed | customer |
| 1 | JP1 shunt (Harwin M7566-05) — only after unloaded rail tests | customer |
| 1 | RTC lead: JST XHP-2 + 2× SXH-001T-P0.6 + hook-up wire (J9 → Teensy VBAT/GND) | customer |
| 4 | M3 insulated standoffs and screws | customer |
| 1 | 3.3 V active GNSS antenna, SMA male, draw compatible with the 50 mA minimum bias limit | customer |

## Programming / firmware limitations — important

- Source/build version at this hardware checkpoint is **0.1.149**; last known bench
  installation was **0.1.147**. These files do not flash or publish anything.
- **IMU:** the board carries an **ICM-42670-P**, register-incompatible with the
  MPU-6050 the current firmware drives. A new driver, identity detection (WHO_AM_I
  0x67 vs the legacy 0x68 path) and an `ImuCalStore` sensor-type tag are required
  before any IMU value is believable.
- **Oil** input gain is now **0.500**, not the legacy 2/3. **Coolant** uses
  **4.096 V / 2.49 kΩ excitation with a 0.500 sense divider**, not the old 150 Ω
  circuit. New conversions/calibration and a verified sender curve are required
  before believing those displayed values.
- **Throttle (A7) and brake (A10)** need conversions, plausibility limits and logging
  fields. They are not implemented.
- AEM retains the existing nominal 0.500 gain contract; calibrate measured ADC
  reference/gain and test fault/stale behaviour. The AEM setting remains default OFF.
- The **Pi 5 video link** (Serial1, pins 0/1) needs the `VID,` protocol implemented;
  the Pi-side recorder is a separate project.
- The **network ESP32 is a third programmable MCU**; its SPI upload / provisioning /
  recovery firmware is still required. **Teensy pin 13's old heartbeat must stop
  before SPI use.** No faster-upload claim follows from merely populating the module.
- J10 **on this carrier** is the network programming header (not the screen J10):
  1 GND, 2 3V3 reference OUT, 3 TX from NET, 4 RX to NET (buffered), 5 BOOT0, 6 EN.

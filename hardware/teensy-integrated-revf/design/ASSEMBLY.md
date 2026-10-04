# Rev F — engineering-prototype assembly

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

⚠️ **`J14` (CAN) is mid-board, not on the left edge.** It is a third Phoenix
**1729021** 3-position 5.08 mm terminal, next to the onboard transceiver rather than
with the left-edge bank (that edge cannot hold a ninth screw terminal with usable
clearances). Pinout, **left to right** in the board's normal orientation — the
**square pad is pin 1**:
**1 = CANH, 2 = CANL, 3 = GND** (signal-ground reference to the ECU).
`JP2` ("CAN TERM") is a 2-pin shunt **in series with the split-termination pair**:
**fit the shunt only if this board is an end of the CAN bus**; remove it on a mid-bus tap
and remove it entirely if the ECU end is already terminated at 120 Ω. `TP6`/`TP7` are the
`CAN_TX`/`CAN_RX` logic-side test points. **`U21` orientation:** the footprint's own
silkscreen is kept on the board (Rev F restores library footprint silk board-wide), so the
usual SOIC-8 **pin-1 dot** is printed at the `TXD` corner — the corner nearest `J14`/U1;
pin 8 (`STB`, already tied to GND on the board) is the far end of that row.

Other field connectors: **J8** GPS SMA, **J9** RTC cell lead, **J10** NET 3V3
service/programming 2×3 header, **J13** Pi 5 video UART (JST XH), **J14** CAN
(Phoenix 1729021). The old `J7` CAN-module header is deleted — the transceiver is on
the board and its `STB` pin is tied to GND, so there is no receive-only module to fit.

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
   headers. Observe C1/C2 polarity. **C1 positive is on VIN_PROTECTED, after the Q3
   reverse-polarity FET**, not on the raw/reversible input.
3. **U2 is a soldered TI `TPS54560BDDAR` buck — there is NO socket and NO Pololu module.**
   Do not fit a socket at U2 and do not source or fit a Pololu 4091 / D36V50F5: Rev E
   removed the module. Reflow U2 as an ordinary SMT part and confirm the **PowerPAD
   (pad 9) is fully soldered to the ground copper** — it carries the heat, and the
   continuous-5 A claim is gated on bench thermal testing. **C1 positive is on
   `VIN_PROTECTED`**, i.e. *after* the Q3 P-channel reverse-polarity FET, not on the
   raw/reversible input. Observe L2, D23 and the input/output ceramic positions as placed;
   the switch node is the noisy node to keep probes and fingers away from.
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
| 1 | CR2032 cell — never reflowed | customer |
| 1 | JP1 shunt (Harwin M7566-05) — only after unloaded rail tests | customer |
| 1 | JP2 shunt (same 2.54 mm type as JP1) — **only if this board is a CAN bus end** | customer |
| 1 | RTC lead: JST XHP-2 + 2× SXH-001T-P0.6 + hook-up wire (J9 → Teensy VBAT/GND) | customer |
| 4 | M3 insulated standoffs and screws | customer |
| 1 | 3.3 V active GNSS antenna, SMA male, draw compatible with the 50 mA minimum bias limit | customer |

## Programming / firmware limitations — important

- Source/build version at this hardware checkpoint is **0.1.169**. These files do not
  flash or publish anything.
- **CAN (Rev F):** the onboard TCAN1042 needs **no firmware change** — 500 kbit/s, normal
  ACKing mode. `CANTX,<n>` and `ACKTEST` on the USB serial console are the bench
  transmit self-tests (see `BRINGUP.md`). The TCAN1042's TXD dominant time-out (~1 ms)
  cuts the firmware's `CANHOLD` diagnostic short; that is expected.
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

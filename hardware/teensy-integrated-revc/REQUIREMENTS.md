# Integrated sensor PCB — Rev C requirements (NOT A FABRICATION RELEASE)

**Rev C supersedes A/B:** internal WiFi antenna and ALL screw terminals on one
labelled LEFT edge. A/B are retired, not alternative fabrication packages.
Gerbers + drills are the REQUIRED final deliverable after electrical design and
routing are complete; the current concept/review ZIP does not satisfy that milestone.
See `LAYOUT_AND_FABRICATION.md` for the connector bank and outstanding gates.

The user clarified that the Rev A carrier is NOT the requested final design.
Rev C must integrate the GPS and IMU and accept direct sensor wiring. **Do not
order the Rev A carrier expecting these functions.** No Rev C Gerbers have been
issued. The user has now resolved the tach SOURCE CLASS: **conditioned ECU or
instrument-cluster tach signal only**, initially on **1990–2005 Mazda MX-5/Miata
Spec Miata**. Raw ignition is explicitly outside this board's requirements.
See [TACH_INTERFACE.md](TACH_INTERFACE.md) for the input contract and remaining
bench-verification requirements.

## Required changes from Rev A

- Solder the **u-blox NEO-M9N** receiver module directly to the main PCB. No external
  GPS breakout, GPS UART cable, or daughterboard. Include the manufacturer-required
  supply/backup/decoupling/reset circuitry, a controlled-impedance RF connection to
  an onboard **standard SMA female** connector, appropriate RF ESD protection and
  active-antenna bias/filtering. The external antenna/puck is still required.
- Solder a **six-axis IMU IC** directly to the main PCB, with required supply,
  regulator/charge-pump capacitors if applicable, I2C pull-ups, address straps and
  documented axis orientation. No GY-521/external module connector as the primary
  implementation. Select the exact part based on genuine availability and driver
  compatibility; legacy MPU-6050 firmware support alone is not a sourcing guarantee.
- Three-terminal direct **oil pressure sender** connection: regulated 5 V output,
  dedicated sensor ground/return, analogue signal. Include voltage scaling, input
  filtering, fault/ESD protection and power-off/backfeed analysis, not bare ADC pins.
- Two-terminal direct **coolant NTC sender** connection: sensor input and dedicated
  sensor return. Include sender-matched precision pull-up, filter, protection and
  appropriate calibration/profile support in firmware. Do not tee into an ECU's
  existing temperature sensor circuit; that changes both systems' readings.
- Direct **ECU/cluster tach input**, initially for 1990–2005 Spec Miata. An
  **onboard optocoupler is required**, with a high-impedance input front end,
  voltage/current limiting, reverse/transient protection, filtering/hysteresis and
  clean 3.3 V pulse delivery to Teensy **pin 9**. No external opto is required for
  an already-conditioned ECU/cluster signal. **NEVER connect coil negative, a spark
  lead, ignition primary or injector drive.** Any ignition-derived source requires
  a separate conditioner that actually meets this board's low-voltage input spec.
- **TWO WiFi endpoints, no Ethernet**: add a soldered ESP32-S3 network coprocessor
  on this PCB for Teensy-direct session uploads over a short local SPI link.
  Use **ESP32-S3-WROOM-1-N8R2 with built-in PCB antenna**, no external WiFi
  antenna/pigtail/connector. Provide edge clearance and plastic/RF-transparent
  enclosure space; GPS antenna stays external. Keep
  CrowPanel WiFi for OTA and optional upload fallback. Both may connect to the AP;
  file ownership must prevent competing uploaders. Existing OTA downloads and
  screen-to-Teensy update path remain unchanged. See `WIFI_RTC_ARCHITECTURE.md`.
- Add **replaceable 3 V CR2032 RTC backup**: positive to Teensy VBAT, negative to
  GND; no charger, no CPU/WiFi backup power. Add the missing auxiliary VBAT
  contact to the socketed Teensy assembly. Preserve acquired time while offline
  and resync after actual fresh Internet time is available. Finite cell life;
  specify retention tests and a maintenance interval, not 'forever'.
- Add **AFR/lambda input from an external AEM 30-0300 gauge ONLY**: solid WHITE
  analogue output and BROWN signal reference on a two-position input. No direct
  oxygen-sensor interface, onboard heater/controller, or source-selection jumper.
  See `AFR_INTERFACE.md` for the verified AEM scaling and proposed protected 0.500
  gain input to A6/pin 20; full electrical protection/layout review remains open.
- Preserve socketed Teensy 4.1 and built-in SD unless user requests otherwise.
- Preserve requested 10–20 V input / 5 V 5 A main supply design target and the
  four-function powered UART connection to the verified CrowPanel Advance J10.
  Any changed loads/protection/thermal constraints must be recalculated.
- Deliver a simple user-facing handoff: **one manufacturing ZIP, one assembled PNG**;
  component lists and instructions in the ZIP. Do not substitute a copper-layer plot
  for the assembled-board preview.

## Sender selections / evidence

### Oil — selected electrical target

**AUTEX 150 PSI pressure transducer with harness**, Amazon ASIN **B00NIK98O8**:
https://www.amazon.com/dp/B00NIK98O8

The fetched listing specifies:
- 5 V DC supply;
- 0.5–4.5 V linear output over 0–150 PSI;
- 1/8-27 NPT thread;
- includes mating harness;
- advertised -40 to +125 C working range.

This matches the current software's nominal pressure transfer function. Seller
specifications are not independent validation of calibration, thermal performance
or authenticity. Bench-check zero/span, actual connector pinout and output before
use. Do not rely on colours alone. Thread fit to the engine is NOT established;
use the correct rated adaptor rather than forcing NPT into BSPT/metric threads.
For track use, assess a remote pressure hose/bracket to reduce sensor vibration and
heat. The chosen sensor must remain within its temperature limit; if actual
installation conditions exceed it, select a higher-rated sender instead.

Current code (`src/main.cpp`) expects physical **16 / A2**, 10k/20k voltage divider
(2/3 ratio), 0.5 V at zero and 4.5 V at 150 PSI. That is the nominal signal scaling,
NOT a complete protection circuit: 5 V faults, battery shorts, negative transients,
ADC acquisition and power-off injection need separate design review. Preserve the
transfer ratio or explicitly change the hardware-specific firmware profile.

### Coolant — proposed direct-wired sensor

**Delphi TS10075**, Amazon ASIN **B000CGM9O2**:
https://www.amazon.com/dp/B000CGM9O2

The Amazon page identifies this part as an engine coolant temperature sensor. Use
its proper mating two-wire connector. Confirm manufacturer thread specification,
installed temperature/pressure ratings, exact resistance-vs-temperature curve and
connector keying before freezing the BOM; a generic 'GM sensor' curve is not proof
of the curve of the purchased part. Vehicle port/adaptor fit is still unconfirmed.

Current firmware on physical **17 / A3** assumes a **150 ohm pull-up and a VDO-like
1600–22 ohm sender curve**. It is NOT already calibrated for this Delphi sensor.
Rev C requires a sender-specific pull-up and calibration (verified data and/or
measured calibration points). Do not silently change calibration for existing Rev A/
legacy installations: introduce a hardware/sender profile or other explicit selection.

## Confirmed tach scope — user requirement

- Application: **Mazda Miata 1990–2005, Spec Miata**.
- Source: a **conditioned ECU tach output or the conditioned tach signal at the
  instrument cluster**. The user explicitly excludes engine spark/coil signals.
- Put the optocoupler and its entire interface on this PCB, not on an external board.
- If ignition pickup is needed in a future installation, a **separate external
  adapter** converts it into a compatible low-voltage tach waveform first. Merely
  reducing spike amplitude or powering an adapter from 12 V is not sufficient.
- Design for logic-level and battery-level conditioned pulses; don't depend on a
  fixed input resistor driving the optocoupler LED directly from the factory tach
  line. A weak cluster pull-up can be dragged down by that LED load.

This resolves the architectural source question; it does NOT establish the exact
wire colour/ECU pin, pulse voltage, pull-up arrangement or duty cycle for every
NA/NB model year and replacement ECU. Confirm those from year-specific wiring data
and scope measurements before claiming universal compatibility. Keep pulses/rev
configurable and confirm against a trusted RPM reference. Sender-port threads and
separate coolant calibration remain part of the installation/validation work.

## Release gates

Manufacturer datasheet/reference-layout checks; full schematic electrical review
(not merely passive-pin ERC); mechanical/footprint verification; genuine component
sourcing; analog calibration and fault tests; GNSS RF/antenna testing; IMU driver/
orientation tests; thermal/current/load/startup validation; powered screen cable
integrity; appropriate automotive transient qualification. Bench prototypes first.

## Updated visual drawings

`REV-C-INTEGRATED-PREVIEW.png` now shows the **proposed** integrated placement:
soldered GPS/SMA, bare IMU, direct sender terminals, onboard tach optocoupler,
AEM gauge-output input, **trunk WiFi with INTERNAL PCB antenna/supply** and **RTC coin cell**.
ALL screw terminals now face the LEFT edge, labelled by function and proposed pad order.
The working envelope grows to **130 x 140 mm** to allow service access; not final.
`CONNECTIONS-CONCEPT.svg` shows the corresponding functional connections. See
`preview/README.md` for the reproducible drawing source and explicit limitations.
Ethernet remains removed. A new lower strip illustrates the separate network
module and RTC cell; the AEM gauge-output input remains. These drawings contain representative parts
only, **not a finalized circuit**.
The original Rev A fabrication outputs are unchanged.

Until the circuits are defined, this directory contains requirements and visual
layout proposals, not a finished integrated board, final component BOM, or
manufacturing package.

## Vendor handoff — design completion, NOT fabrication

`handoff/package.py` creates
`Racecar-RevC-DESIGN-REVIEW-NOT-FOR-FAB.zip` and a copy in Downloads. It contains
these requirements, the drawings, a vendor RFQ, assembly/label requirements,
candidate major parts and an explicit manufacturing-readiness checklist. It is
for a design engineer or supplier offering design completion, not an assembly-only
order. No Rev A fabrication files, render-only CAD, visual placement coordinates,
final BOM or programming images are included. Read `READ-ME-FIRST.txt` in the ZIP.

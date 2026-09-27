# Requested finished assembly / labels / test scope

**Design-review requirements, not released assembly instructions.** No final BOM,
CPL, component orientation drawing, PCB stackup or approved test limits exist yet.

## Finished assembly requested

- All approved PCB components installed, not a bare PCB plus loose component bags.
- Soldered GPS module and IMU IC, input protection/filtering/passives and onboard opto.
- All connectors, fuses, regulators, signal translators and auxiliary parts installed.
- Soldered **ESP32-S3-WROOM-1-N8R2 with INTERNAL PCB antenna**, dedicated NET
  3.3 V supply/decoupling and programming/recovery interface; no Ethernet or WiFi
  pigtail/bulkhead. Preserve antenna clearance and plastic/RF-window enclosure.
- ALL screw terminals at the LEFT edge, entries facing left; retain screw/tool/wire
  access. See `LAYOUT_AND_FABRICATION.md` for the labelled proposed bank.
- Replaceable vibration-retained CR2032 holder and the extra socket/contact for
  Teensy VBAT. Install the primary cell after soldering/cleaning; do not charge or
  reflow it. Agree cell/environmental rating and replacement interval.
- Teensy remains replaceable in sockets: install sockets and module headers, fit the
  Teensy, retain USB and SD access, and include suitable mechanical support.
- If the Pololu/Traco module approach is retained, include the modules, headers and
  secure supports. Verify that the assembler accepts their sourcing/consignment and
  manual assembly operations; availability alone does not establish suitability.
- Cut/verify the Teensy VUSB-VIN bridge according to PJRC before dual USB/carrier
  power use. A carrier diode or jumper is not a replacement for that modification.
- Provide test results and an agreed, compatible firmware image/programming procedure.
  Current firmware is not an approved integrated Rev C factory-test image.
- Treat the external GPS antenna, oil/coolant senders, their mating pigtails, external screen and car
  harness as explicit quoted inclusions or exclusions, not silently included in PCBA.

## Labels to implement in the finalized CAD

Request white silkscreen, readable AFTER components are fitted. Place connector
labels where tall terminal blocks do not obscure them. Include:

- Board name, revision and prototype status; optional separate serialized sticker.
- Power input voltage/polarity, GND and fuse identification/rating after validation.
- Screen connector: GND, +5V OUT, RX FROM SCREEN and TX TO SCREEN (PCB-relative).
  Mark verified Advance J10 only; do not confuse the display's HY2.0 3V3_OUT header.
- Oil: sensor supply OUT, dedicated sensor return and signal.
- Coolant: NTC input and dedicated sensor return.
- Tach: signal and reference/return; **ECU / CLUSTER ONLY - NO COIL / IGNITION**.
- GPS SMA/antenna designation; final antenna-power specification after RF design.
- `WIFI INTERNAL`, `NET SERVICE` and `3V3 NET`. No external WiFi port; external
  GPS SMA remains. Antenna clearance is not a place for labels/copper/metal fixtures.
- `RTC 3V CR2032`, polarity `+`, `DO NOT CHARGE` and VBAT contact/service identification.
- IMU axis orientation and test-point functions after placement/orientation is fixed.
- MCU power jumper function and the VUSB-VIN bridge warning.
- Optional CAN header explicitly described as a module logic interface, not CANH/CANL.
  Ethernet is permanently removed; BOTH trunk-module and screen WiFi are planned.
- AFR two-position connector: `AFR SIG+ / AFR RETURN`, for AEM 30-0300 solid
  WHITE / BROWN. No direct sensor, heater/controller or source-selection jumper.
  Verify the final footprint's pad numbering; complete scaled/protected input and
  power-off isolation review before assembly.

Printed labels must match the FINAL netlist and connector pin order. The current
rendering is not a wiring pinout and must not be copied as one without verification.

## Testing to scope and quote

AOI/X-ray/continuity inspection does not prove functional or automotive performance.
A qualified engineer must define safe fixtures and numeric pass/fail limits after
the circuit is completed. Expected work includes:

1. Assembly/identity/orientation inspection and unpowered short/power-path checks.
2. Controlled current-limited initial power-up, supplies and startup behaviour.
3. Input/sensor fault and powered/unpowered backfeed tests under defined conditions.
4. Load, startup, ripple and enclosure thermal testing at 10/14.4/20 V input; verify
   the requested 5 A TOTAL main-rail target before claiming that capability.
   The Rev A concept's screen branch was 4 A, not a 5 A screen output.
5. GPIO/tach waveform tests without loading the factory tach; independent RPM/PPR
   verification and year-specific vehicle checks. No raw-ignition testing on this input.
6. Oil zero/span checks and coolant resistance/temperature calibration and fault tests.
7. GPS power/RF/antenna operation and accepted update rate, plus IMU identity,
   orientation, static output and calibration/driver verification.
8. SD logging and the full-length 921600-baud screen cable, including power drop and
   activity under representative interference. Verify the particular clone's header.
9. New-module WiFi power-burst/thermal/RF testing, SPI correctness, real authenticated
   uploads under failure/retry/resume, screen fallback and unchanged OTA recovery.
10. RTC retention across main-power removal, no charging/backfeed, cell-loss/invalid
    time handling and fresh-Internet resync; document a battery service interval.
11. Documented firmware versions (including network module), test setup, results,
    anomalies and rework.

Automotive transient/EMC/environmental qualification is a separate engineering scope.
Do not apply uncontrolled automotive faults or a full load to an unverified circuit.

# Firmware / pin constraints for the Rev C design engineer

**Reference only; not an approved programming or wiring release.** No firmware image
is included. The current Teensy and CrowPanel sources both report **0.1.149**
(WiFi-only plus opt-in AEM 30-0300 gauge-output acquisition, display and logging).
Enable AEM only after installing/reviewing the required scaled/protected front end.
**New requirement:** two WiFi endpoints (on-PCB coprocessor + screen), with OTA
remaining screen-led, and a VBAT RTC cell. Current v0.1.149 has NO new-module SPI
transport or direct-upload implementation. RTC hardware access exists, but the
new validity/resync policy remains work. See `WIFI_RTC_ARCHITECTURE.md`.
Source baseline: `e3942d85366aafc626fea52513b2f5eaeb8152d3` in the racecar-35 repository.
The Rev C concept/requirements and WiFi-only/AEM gauge firmware changes are local work
beyond that baseline.

## Existing Teensy 4.1 pin usage

| Function | Teensy pins | Constraint |
|---|---|---|
| GPS UART / Serial2 | RX 7, TX 8 | 3.3 V logic. NEO-M9N is the existing firmware target. |
| Dash UART / Serial3 | TX 14, RX 15 | Bidirectional 921600 8N1; external screen interface needs appropriate level translation. |
| Tach | 9 | FreqMeasureMulti input capture; clean 0-3.3 V logic, never vehicle voltage directly. |
| Oil analogue | A2 / physical 16 | 3.3 V ADC, not 5 V tolerant. |
| Coolant analogue | A3 / physical 17 | 3.3 V ADC; sender/pull-up/calibration must agree. |
| IMU / Wire I2C0 | SDA 18, SCL 19 | Current MPU-6050 implementation uses address 0x68. |
| CAN1 logic | TX 22, RX 23 | Needs a transceiver; not direct CANH/CANL. |
| AEM 30-0300 analogue input | A6 / physical 20 | Protected 0.500 gain front end required; firmware assumes nominal 3.300 V reference; NVS/CFG `afraem` opt-in. |
| Proposed WiFi SPI | CS 10, MOSI 11, MISO 12, SCK 13 | RESERVED for the new coprocessor. Disable current pin-13 LED writes before using SPI. Not implemented in v0.1.149. |
| Proposed NET IRQ / reset | 5 / 6 | Reserved; reviewed 3.3 V/power-off/reset stages required. |
| RTC backup | VBAT + GND | Dedicated auxiliary Teensy contact, NOT a GPIO. Primary 3 V cell only; no charging. Existing outer-row socket does not expose it. |
| SD | Built-in Teensy slot / SDIO | Preserve mechanical access. |

Do NOT use the ordinary FreqMeasure library for this Teensy tach path: it uses
pin 22 on this MCU, conflicting with CAN. The existing code uses FreqMeasureMulti
on pin 9. Pulse count is configurable (`CFG,rpmppr`, units of tenths of a pulse/rev);
the current 2 pulses/rev setting must be checked against the installed tach source.

## Known compatibility work still required

- Oil currently assumes a 0.5-4.5 V / 0-150 PSI sender with a 10k/20k divider (2/3).
  Those are nominal scaling constants, NOT a complete fault-protection circuit.
  Final divider/source impedance/protection changes may require calibration changes.
- Coolant currently assumes **150 ohm pull-up and a VDO-like curve**. It is NOT
  calibrated for the proposed Delphi TS10075. Obtain its correct curve and suitable
  bias resistor; add explicit profile support without corrupting legacy units.
- The IMU driver is currently MPU-6050-specific. The rendering's generic QFN is not
  an approved substitute. A different selected IC requires driver/identity/range/
  temperature/calibration work, even if it happens to share an I2C address.
- Existing GPS settings offer 1/5/10/25 Hz. Verify actual acceptance of the requested
  rate; the current implementation is not proof every module/configuration supports it.
- AFR uses ONLY the external AEM 30-0300 gauge's WHITE/BROWN analogue output.
  No direct sensor/controller circuitry or selection jumper. Exact formulas,
  fault/null logging, signal-return wiring and the still-required input protection
  review are documented in `AFR_INTERFACE.md`.
- Complete the new WiFi coprocessor firmware, SPI flow control/ownership,
  authentication/resume and programming/recovery plan; do not change existing
  screen-led OTA downloads. Harden retained-clock validity and fresh-NTP resync.
- No new supply, sensor-fault or factory-test behaviour should be assumed merely
  because a protection circuit is drawn or firmware currently compiles.

## Power and display caveats

The Teensy rail and auxiliary 3.3 V rail must not be casually tied together. Account
for peripheral-to-MCU backfeeding, USB-only/car-only operation and power sequencing.
Cut/verify VUSB-VIN as required by PJRC before simultaneous sources.

The intended powered screen connection is to its verified Advance **J10 +5V_IN**
interface. NEVER feed 5 V to its small HY2.0 **3V3_OUT** connector. USB identity/flash
size does not certify a clone's power-header wiring. Verify the actual panel.
Disconnect the Teensy-screen UART before flashing the display: it shares UART0
with the display's USB serial adapter.

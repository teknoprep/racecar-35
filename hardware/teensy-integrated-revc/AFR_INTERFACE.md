# Rev C AFR input — external AEM 30-0300 gauge ONLY

## Scope fixed by the user

Use the **AEM X-Series 30-0300 gauge/controller's analogue output**. The complete
powered gauge and its oxygen sensor stay external. **NO direct oxygen-sensor
connector, onboard wideband/heater controller, or source-selection jumper.**
The earlier six-pin/direct-sensor idea is superseded. The 30-2004 sensor is not a
part to connect to this PCB. Preserve WiFi-only networking.

Source: [official AEM/Holley 30-0300 manual](https://documents.holley.com/30-0300.pdf),
document **10-0300, 2017-07-12**, sections **Gauge Connections**, **0-5V Analog
Output**, and **Specifications**. Use the manual matching the actual supplied
30-0300/harness; do not substitute another AEM model's calibration.

## External wiring — the POWER/IO harness, NOT the sensor harness

| AEM Connector A | Wire | Recorder interface |
|---|---|---|
| Pin 9 | **WHITE**, solid | `AFR SIG+`: analogue positive, into the scaled/protected input |
| Pin 10 | **BROWN** | `AFR RETURN`: dedicated signal reference / analogue ground |

These numbers identify the AEM **Power/IO connector**, not a bare-sensor connector
and not Teensy pins. Proposed board connector `J_AFR` has two positions:
**1 = AFR SIG+**, **2 = AFR RETURN**; final footprint/assembly must verify numbering.

The manual describes a differential output: WHITE goes to the positive input;
BROWN goes to the negative input, or a single-ended logger's shared signal ground.
We propose the latter with a dedicated return to the ADC reference ground. Do not
leave BROWN floating or use an unrelated distant chassis point as the signal return.
Route signal/return together, away from ignition, alternator and heater-current wiring.
Do not assume wire colours on a different model/aftermarket harness mean the same thing.

The existing gauge remains powered per AEM instructions: RED switched automotive
supply through its specified **5 A fuse**, BLACK power ground. No gauge/heater power
comes from this PCB's 5 V output or the two-pin AFR connector. Do not feed the gauge
from the board's unrestricted 10–20 V test supply; observe AEM's supply limits.
**BLUE is RS-232, not the analogue signal. WHITE/BLACK is CANH, not solid WHITE.**

## Analogue front end — firmware contract, electrical review still required

- Target MCU channel: **Teensy A6 / physical pin 20**.
- Target gain from WHITE-to-BROWN voltage to ADC-reference voltage: **0.500**.
  Starting divider: **20.0 kΩ top / 20.0 kΩ bottom, 0.1%**, with bottom to signal
  return. 5.0 V becomes 2.5 V, safely below nominal 3.3 V in NORMAL operation.
- Starting filter: **1 kΩ series to ADC / 100 nF to signal return**, after the
  divider (roughly 1.1 ms time constant including divider impedance). Validate ADC
  settling, output loading, noise and filter placement with the final circuit.
- Use **no internal GPIO pull-up/down**: it would change divider gain. The external
  bottom resistor pulls a disconnected signal low, which is logged as not ready.
- Complete the connector-side transient/fault protection, low-leakage clamps and
  **power-off isolation/buffer** design. Gauge-powered/logger-off must not inject
  into Teensy power rails. Never rely on the MCU's internal ESD diodes. Clamping
  leakage, ground offsets and any added resistance must be included in gain/error
  calculations. The protection/isolation ICs and ratings are NOT selected yet.
- The resistor/filter values define a proposed NORMAL-signal scaling path, **not
  a complete protected circuit, fault rating, or permission to wire a gauge straight
  to the MCU**. The full Rev C schematic and layout still require completion/review.
- Firmware assumes a **3.300 V ADC reference**. Verify/calibrate actual reference,
  divider ratio and offsets against a meter before trusting recorded AFR. If the
  final analogue gain changes, update the shared decoder and tests in the same change.

## Model-specific scaling and states

Let V be the gauge output voltage measured WHITE relative to BROWN, BEFORE scaling:

- **AFR = 2.3750 × V + 7.3125** (gasoline-equivalent; AEM uses 14.65 stoichiometric)
- **Lambda = 0.1621 × V + 0.4990** (use the published formula independently)
- **V < 0.50:** sensor not ready. Also covers a disconnected/unpowered signal pulled
  low; the analogue output alone cannot identify which of those happened.
- **0.50 ≤ V ≤ 4.50:** valid analogue measurement range.
- **V > 4.50:** error/out of analogue measurement range, per the manual.

The analogue valid range corresponds to **8.50–18.00 gasoline-equivalent AFR**,
not the full range that the gauge can display. Do not extrapolate the fault bands
into fake AFR. For alternate fuels use lambda, not an assumed gasoline mass AFR.

## Firmware (v0.1.149 source; bench verification pending)

- Settings → **AEM 30-0300 AFR input** (`afraem` bool, default OFF). Enable only with
  the reviewed front end installed. Independent of Direct/MS3/Bluetooth engine
  source. `CFG,afraem,0|1` selects acquisition on the Teensy; old boards stay off.
- **Show AFR** controls display ONLY; it does not stop recording. When AEM is enabled
  its source wins; a bad/stale AEM value does NOT silently fall back to CAN.
- Settings → **AEM voltage / lambda** shows input diagnostics; dash AFR uses the
  existing AFR row/warnings. That row continues to share space with voltage.
- UART: `AFR,<status>,<afr_x100>,<lambda_x10000>,<gauge_mV>`;
  status `0=off, 1=valid, 2=not_ready, 3=error`. Invalid measurements are `-1`;
  voltage is retained for diagnostics when available. Strict, bounded parsing
  checks fields against the shared transfer function; 2 s freshness timeout.
- SD NDJSON, on every normal recording sample while AEM is enabled:
  `afr`, `lambda`, `afr_v`, `afr_status`, `afr_source:"aem30-0300"`.
  Invalid AFR/lambda are **JSON null**, never zero or a held last-good value.
  The standard WiFi upload preserves these fields; no new cloud chart is implied.
- Both screen-local and Teensy test modes generate AEM samples when enabled.
  These are synthetic pipeline tests, not proof of real analogue accuracy.

## Acceptance checks before vehicle use

1. Complete/independently review protection, unpowered isolation, PCB pin numbering
   and the 0.500 gain. Keep the expensive MCU disconnected for initial fault tests.
2. Apply measured reference voltages to the completed input: 0.50, 1.00, 2.50, 3.00,
   4.50 V. Compare raw ADC voltage, reconstructed input and decoded values to the
   manual; set an explicit gain/offset error budget during design review.
3. Check below 0.50 V, above 4.50 V, open lead, shorted input and gauge warm-up.
   Confirm null readings/status on SD and no AFR warning from stale/invalid data.
4. Verify gauge on/logger off and reverse sequencing do not back-power rails;
   qualify accidental shorts/transients using the reviewed design/test limits.
5. Compare live gauge/meter/SD readings with engine running, then upload the session
   and verify the AFR/lambda/status fields survive unchanged.

Host tests validate conversion boundaries, all 4096 ADC codes, malformed UART
frames, real SD JSON serialization, legacy upload capacities, UI selection/stale
handling and server data passthrough. They do not validate physical circuitry.

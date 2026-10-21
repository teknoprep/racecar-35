# AEMnet on the Racecar-35 logger

AEMnet is AEM's CAN protocol. It shares the **physical bus** with the MS3Pro broadcast
(CAN 2.0, 500 kbit/s, CANH/CANL, 120 Ω at each end), so the Rev F transceiver, J14, JP2
and the CANable all work unchanged. The **messages** are different: AEMnet uses **29-bit
extended identifiers**, big-endian, with AEM's own layouts. Both protocols can be on one
bus at the same time (an MS3Pro plus an AEM X-Series wideband gauge is the usual case), so
the dash picks the protocol **per input**.

## Sources (AEM's own documents)

| Document | What it defines |
|---|---|
| **TA2 Infinity AEMnet Configuration**, Rev A 2016-02-19, data stream "AEMnet v150609" — <https://documents.aemelectronics.com/techlibrary_ta2_infinity_aemnet_configuration.pdf> | The AEM Infinity / EMS V2 / EMS-4 ECU broadcast `0x01F0A000`…. "29 bit, 500 kBit/sec, 8 data bytes per message. Multi-byte data is packed big endian (Motorola format)." |
| **30-0300 X-Series UEGO gauge**, doc 10-0300 (2017-07-12), "AEMnet (CAN Bus) Output" — <https://documents.holley.com/30-0300.pdf> | The wideband gauge this car carries: ID `0x180`–`0x18F`, 100 Hz, no internal termination. Harness: **WHITE/BLACK = AEMnet+ / CANH, GREEN/BLACK = AEMnet− / CANL** (Connector A pins 4 / 3). |

Third-party summaries disagreed with these on details (one claims little-endian); this
implementation follows the AEM documents only.

## Frames decoded

| ID (29-bit) | Rate | Bytes → value (SI scaling from the documents) |
|---|---|---|
| `0x01F0A000` ECU | 50 Hz | `[0:2]` engine speed u16 × 0.39063 rpm · `[2:4]` load (deprecated) · `[4:6]` throttle u16 × 0.0015259 % · `[6]` IAT s8 °C · `[7]` coolant s8 °C |
| `0x01F0A003` ECU | 50 Hz | `[0]` lambda #1 u8 × 0.00390625 + 0.5 · `[1]` lambda #2 · `[2:4]` speed · `[4]` gear · `[5]` ignition timing u8 × 0.35156 − 17 ° · `[6:8]` battery u16 × 0.0002455 V |
| `0x01F0A004` ECU (Infinity V96.1+) | 50 Hz | `[0:2]` MAP u16 × 0.1 kPa · `[2]` VE · `[3]` fuel pressure · `[4]` oil pressure u8 × 0.580151 psi · `[5]` lambda target · `[6:8]` flags |
| `0x180` + (ID−1) X-Series UEGO "ID 1…16" | 100 Hz | `[0:2]` lambda u16 × 0.0001 · `[2:4]` O₂ s16 × 0.001 % · `[4]` supply V u8 × 0.1 · `[6]` bit1 LSU4.9 detected, bit5 free-air cal, **bit7 Lambda Data Valid** · `[7]` **bit6 Sensor Fault** |

AFR = lambda × **14.65**, AEM's gasoline constant (30-0300 spec table; the TA2 US scaling
0.057227 AFR/bit = 0.00390625 × 14.65), the same convention as the analogue AEM input.

Resolution is AEMnet's, not ours: coolant/IAT arrive in **whole °C (1.8 °F steps)**, oil in
**0.58 psi** steps, ECU lambda in 0.0039 steps (the UEGO gauge's 0.0001 is far finer).

### Rules the decoder follows (`src/aemnet.h`)

* Only **29-bit** frames are AEMnet. A standard frame with the same number is a different
  frame (`pumpCAN()` now checks the IDE flag before any decode, for every protocol).
* **The UEGO gauge owns lambda while it broadcasts**: valid → its reading; warming up
  (Lambda Data Valid clear) or faulted → **no value** (-1), never a silent fall-back to the
  ECU's lambda. With no gauge on the bus, the ECU's lambda #1 is used.
* Battery = the ECU's measurement; with no ECU stream, the gauge's own supply voltage (it
  runs on switched 12 V).
* Each frame group expires on its own clock (2 s), so a device that stops reads as no data.

## Logger → dash: the `AEMNET` line

```
AEMNET,<rpm>,<clt_f_x10>,<map_x10>,<tps_x10>,<afr_x10>,<iat_f_x10>,<bat_x10>,<oil_x10>,<lambda_x10000>,<uego_status>
```

Same field order as the MegaSquirt `ECU` line (one parser on the dash), `-1` = not received,
`uego_status` 0 none / 1 valid / 2 not ready / 3 fault. Sent only while an AEMnet device is
broadcasting. The Teensy prints the same line on USB, plus once a second:

```
AEMNET frames/s=250 ecu=ELP uego=U(id 1, valid) rpm=3000 clt_f=1904 ... lambda=10034 afr=147 adv=182
```

SD log keys (their own, never merged with the MS3 ones): `aem_lambda aem_afr aem_uego`,
`aem_rpm aem_tps_pct aem_iat_f aem_coolant_f`, `aem_map_kpa aem_oil_psi`,
`aem_lambda1 aem_spark_deg aem_batt_v` — each group only while live, written in that
priority order and only while the 640 B sample line has room.

## Dash: picking the protocol per input

Settings → **Sensor monitoring** → tap an item → **Source = CANBUS** → the **CAN bus** row
cycles **MegaSquirt ↔ AEMnet**. Every item (TEMP, OIL, VOLT, AFR, IAT, MAP, TPS, RPM) has its
own choice. Typical car: everything on MegaSquirt, **AFR on AEMnet** (the X-Series gauge).
With only the gauge on AEMnet, the other AEMnet items read "no data" (the gauge sends lambda
and its supply voltage only).

## Bench testing with the CANable

```bash
# broadcast fake data in a chosen protocol (values identical across protocols)
python3 tools/can_sim.py bench -p /dev/ttyACM0 --protocol aemnet                 # ECU + UEGO gauge
python3 tools/can_sim.py bench -p /dev/ttyACM0 --protocol aemnet --aem-devices uego --uego-state warmup
python3 tools/can_sim.py bench --dry-run --protocol aemnet --profile steady       # bytes + the line the Teensy must print
python3 tools/can_sim.py listen -p /dev/ttyACM0                                   # decodes AEMnet, MS3 and RC35

# PROVE the Teensy decodes it: known values held still, its USB line compared field by field
python3 tools/can_sim.py validate -p /dev/ttyACM0 --teensy /dev/ttyACM1 --protocol aemnet
python3 tools/can_sim.py validate -p /dev/ttyACM0 --teensy /dev/ttyACM1 --protocol megasquirt

# the GUI: protocol + AEM-device dropdowns, live "teensy" row under each value, VALIDATE button
python3 tools/can_gui.py --protocol aemnet --teensy /dev/ttyACM1
```

`validate` predicts the Teensy's exact line with a Python mirror of the firmware decoder and
demands **bit-exact** agreement (exit code 0 = PASS). For AEMnet it also checks the UEGO
warm-up and fault paths read as *no lambda*. `tests/test_aemnet.py` compiles `src/aemnet.h`
on the host and proves the mirror and the firmware agree on 3 000 random frame sets, checks
the golden bytes against the AEM tables, and runs `validate` end to end against a fake
CANable + a fake Teensy that decodes with the real header (and must FAIL on a 1-rpm error).

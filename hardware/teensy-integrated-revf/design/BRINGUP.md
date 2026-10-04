# Rev F first-build bring-up — no vehicle connection initially

This is an unbuilt prototype. Obtain independent schematic/footprint and CAM review
first. Use a current-limited bench supply, DMM, oscilloscope, electronic load and
non-contact/attached temperature instrumentation. **Do not use a finger as a
thermometer.** Do not connect a Li-ion car battery directly to an untested assembly.

1. Inspect component orientation, solder bridges, polarity and header pin numbering.
   Teensy and CR2032 OUT, JP1 and JP2 open; no display, sensors, antenna or USB
   attached. Check for hard supply shorts with appropriate instrumentation; there is
   no universal ohms threshold that proves this mixed electronic load is healthy.
2. Verify the Teensy's cut VUSB–VIN bridge separately. Verify RTC lead continuity to
   the ACTUAL labelled VBAT/GND pads, not an inferred neighbouring contact.
3. **U2 is the soldered TI `TPS54560BDDAR` buck — there is no socket and no Pololu
   module to insert.** Before applying any power, confirm the PowerPAD solder joint and
   that C1 is on the protected rail. Start at 10 V with a conservative current limit and
   no external loads. Check protected input, +5 V MAIN, +5 V SCREEN, +3V3 AUX and
   +3V3 NET. **Without a good U2 joint, VIN_PROTECTED and all three derived rails are
   dead by design** — that is not a fault. Investigate a current-limit event rather than
   increasing the limit blindly. Check ripple, startup/restart and regulator temperature
   at 10, 14.4 and 20 V, and remember the asynchronous buck runs hotter than the module
   it replaced.
4. Confirm C1 is on the protected rail, diode orientation, and no charging/backfeed
   on VBAT. Perform power-off/source-sequencing checks with limited fault energy; do
   not improvise destructive reverse-battery or load-dump tests.
5. Fit the Teensy only after the rails pass. Verify 3V3, baseline supply current,
   CPU health and UART. Test carrier-only, USB-only (bridge cut), and controlled dual
   power. Test GPS/NET powered with the MCU off and vice versa for unintended
   backfeed on the Ioff buffers.
6. **IMU identity first.** The board carries an ICM-42670-P, not an MPU-6050: confirm
   the part and its I²C address (0x68) before trusting any driver, and expect the
   current firmware's MPU-6050 register accesses to be meaningless here.
7. With the display still disconnected, ramp an electronic load on the SCREEN branch;
   account for the MCU/sensor load in the **5 A TOTAL** budget. Characterize startup,
   inrush, ripple, cable drop and steady-state temperature. Do not label the assembly
   'continuous 5 A' until worst-case enclosure/ambient testing supports it.
8. Confirm the particular Advance screen's J10 pinout; use its 5V_IN interface, not
   3V3_OUT. Initially power with current limiting. Verify touch, boot identity and
   bidirectional 921600-baud communication over the intended full cable length.
9. Apply a low-energy test waveform to conditioned TACH: scope loading, trip levels
   (≈1.84 V rising / 1.56 V falling input-referred, before tolerance), optocoupler
   transitions and the final pin-9 swing. Check 3.3/5/12 V amplitudes and 1–500 Hz
   before any car connection. Establish actual vehicle PPR independently.
10. Analog testing with MCU on AND off, using precision sources and resistor decades.
    **All five channels (oil, AFR/coolant, throttle, brake) are 0.500 gain with a
    3.0 V shunt clamp.** Sweep oil/AFR/throttle/brake 0–5 V; verify gain, clamp,
    open/short behaviour and no power-off injection. Test overvoltage only with a
    current-limited fixture inside the reviewed fault budget. For coolant, use a
    resistor decade against the 4.096 V / 2.49 kΩ excitation and verify the 40 kΩ
    parallel-loading compensation. Compare displayed/logged values to instruments;
    DO NOT trust the legacy oil/NTC scaling.
11. Program the network module through its 3V3 UART fixture on J10 (1 GND, 2 3V3
    reference, 3 TX from NET, 4 RX to NET, 5 BOOT0, 6 EN). Then implement and validate
    SPI identity, flow control, CRC, reset, upload ownership/auth/resume and recovery.
    **Disable the Teensy's pin-13 heartbeat before enabling SPI (pin 13 is NET_SCK).**
    No uploader firmware is included here. Test RF while GNSS runs and while the
    screen uses WiFi/BLE; test video UART (J13) while RF is active. Record real
    throughput rather than a SPI theoretical rate.
12. After soldering/cleaning, install the CR2032. Measure retained RTC behaviour
    across hours/days without main power, cell removal/depletion handling, and
    fresh-time resync. Verify no cell charging in car/USB/off states and normal
    restart with VBAT fitted.
13. Finally test the real AEM output, the dedicated oil/coolant senders, the
    throttle/brake senders and the conditioned vehicle tach with appropriately fused
    harnesses. Keep gauge/heater power external, and do not parallel a car-powered
    sensor onto the board's +5 V sensor pins. Recording, warnings, SD integrity, GPS
    freezes, communications and temperatures need vehicle validation before track
    use. Retain original data on upload failure.

## CAN bring-up (Rev F) — must PASS before any car bus connection

> **Root cause this checklist exists to catch** (bench, 2026-10-04): the Amazon "SN65HVD230"
> modules are mislabelled TJA1051T/3-class chips. Wired the SN65HVD230 way (VCC = 3.3 V,
> pin 5 floating) the logger *receives* but never drives a dominant bit, never ACKs, and the
> sender storms ~3,800 frames/s. Full analysis:
> `../../../CAN-TRANSCEIVER-FINDINGS-2026-10-04.md`.

### 0. Prerequisites — get these right or the test lies

* **Power: `J1` at 12 V must be connected AND the Teensy must be seated.** `U21` VCC is
  `+5V_MAIN` (from the buck) and VIO is `+3V3_MCU` (**the Teensy's own 3.3 V rail**). On
  Teensy-USB power alone VCC = 0 and the board shows the *identical* "no ACK" symptom.
  That is not a fault — do not "fix" it.
* **Incoming inspection of `U21` BEFORE power-up.** Read the top marking and compare it
  with TI's *Device Marking* column in the Package Option Addendum for
  `TCAN1042HGVDRQ1`; photograph it into `logs/`. **Mismatch = reject the part.**
  The part is substituted at your peril: the **`V` suffix is mandatory** (it *is* the VIO
  pin). `TCAN1042DRQ1` / `TCAN1042HDRQ1` / `TCAN1042GDRQ1` have **pin 5 = NC**, so RXD
  would swing to 5 V and **destroy Teensy pin 23**. Never accept an SN65HVD23x, a plain
  `TJA1051T` (no `/3`), a `TJA1050`, or any "pin-compatible" clone for U21.
  Approved alternates, only: `TCAN1042VDRQ1`, then NXP `TJA1051T/3/1J` (its pin 8 `S`
  likewise goes to GND).

### 1. DC levels (with J1 powered and the Teensy seated)

| Point | Expected | Fail signature |
|---|---|---|
| U21 pin 3 (VCC) | **5.0 V** | 3.3 V / 0 V = wrong supply (the original bench bug) |
| U21 pin 5 (VIO) | **3.3 V** | **~2 V = VIO floating / phantom-fed by TXD** — reject |
| U21 pin 8 (STB) | **0 V** | non-zero = standby, receive-only, never ACKs |

### 2. Termination (everything unpowered)

Measure `CANH-CANL` at `J14`: about **60 Ω** with the ECU connected and the `JP2` shunt
fitted at the bus end (120.8 Ω here in parallel with the ECU's 120 Ω); a lone fitted shunt
reads about **120 Ω**.

### 3. Functional tests that cannot be fooled

With a **CANable 2.5** running slcan (Elmue firmware), **error reports on** (`ME`), on the
same bus:

1. **Single-frame ACK test (decisive).** Have the CANable send **one** frame. The logger's
   `CANDIAG total` must rise by **exactly 1**, and the CANable must report **no** `E?3……`
   (protocol error 3 = No ACK). Thousands = no ACK = **FAIL**. (On the old module the chip
   had no dominant time-out; on Rev F the TCAN1042 does — see §4.)
2. **Feed test.** A 100 Hz bench feed must read **≈200 frames/s** on `CANDIAG`, listing
   **both `0x700` and `0x701`**, with `oil`/`afr`/`batt` ≠ `-1` and roughly **0 %
   duplicates**. **~3,800 frames/s is a retransmit storm and is a FAIL**, even though it
   superficially looks like "receiving fine".
3. **Transmit test.** Teensy USB `CANTX,10` must deliver 10 frames the CANable receives
   **exactly once each**, with the Teensy transmit-error counter (`TXerr`) staying **0**.

### 4. Diagnostics that are NOT valid pass criteria on Rev F

* Firmware `CANDRIVE` / `CANPROBE` "TX PATH OK" — an internal TXD→RXD echo passes it
  (that echo is *why* the old module fooled every self-test).
* The bench console's "fps ≥ 50 → ACKING" verdict — a 3,800/s storm passes it.
* `CANHOLDON` + a multimeter on `CANH-CANL`: the Rev F **TCAN1042 TXD dominant time-out
  (~1 ms) releases the bus**, so a perfectly healthy board reads ≈0 V. It was only ever a
  valid test on the old module because that chip had no time-out.

Only then connect a real vehicle bus. These are bench acceptance criteria, not automotive
transient qualification.

---

Record instrument readings, ambient conditions, firmware identity, observed faults
and modifications. A passed software test or a green ERC/DRC report is not a
substitute for these measurements.

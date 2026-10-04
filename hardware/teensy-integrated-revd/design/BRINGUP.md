# Rev D first-build bring-up — no vehicle connection initially

This is an unbuilt prototype. Obtain independent schematic/footprint and CAM review
first. Use a current-limited bench supply, DMM, oscilloscope, electronic load and
non-contact/attached temperature instrumentation. **Do not use a finger as a
thermometer.** Do not connect a Li-ion car battery directly to an untested assembly.

1. Inspect component orientation, solder bridges, polarity and header pin numbering.
   Teensy, Pololu module and CR2032 OUT, JP1 open; no display, sensors, antenna or USB
   attached. Check for hard supply shorts with appropriate instrumentation; there is
   no universal ohms threshold that proves this mixed electronic load is healthy.
2. Verify the Teensy's cut VUSB–VIN bridge separately. Verify RTC lead continuity to
   the ACTUAL labelled VBAT/GND pads, not an inferred neighbouring contact.
3. **Insert the Pololu 4091 module into the fitted U2 socket FIRST** — the module is
   destroyed if it is inserted the mirrored way round, so verify the orientation rule
   in `ASSEMBLY.md` step 3 (TOP/labelled face up, VOUT column over the only
   rectangular pad, U2 pad 1) before applying any power. Then start at 10 V with a
   conservative current limit and no external loads. Check protected input, +5 V MAIN,
   +5 V SCREEN, +3V3 AUX and +3V3 NET. **With U2 absent, VIN_PROTECTED and all three
   derived rails are dead by design** — that is not a fault. Investigate a
   current-limit event rather than increasing the limit blindly. Check ripple,
   startup/restart and regulator temperature at 10, 14.4 and 20 V.
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

Record instrument readings, ambient conditions, firmware identity, observed faults
and modifications. A passed software test or a green ERC/DRC report is not a
substitute for these measurements.

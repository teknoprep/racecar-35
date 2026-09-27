# Rev C first-build bring-up — no vehicle connection initially

This is an unbuilt prototype. Obtain independent schematic/footprint and CAM review
first. Use a current-limited bench supply, DMM, oscilloscope, electronic load and
non-contact/attached temperature instrumentation. **Do not use a finger as a
thermometer.** Do not connect a Li-ion car battery directly to an untested assembly.

1. Inspect component orientation, solder bridges, polarity and header pin numbering.
   Teensy and CR2032 OUT, JP1 open; no display, sensors, antenna or USB attached.
   Check for hard supply shorts with appropriate instrumentation; there is no
   universal ohms threshold that proves this mixed electronic load is healthy.
2. Verify the Teensy's cut VUSB–VIN bridge separately. Verify RTC lead continuity
   to the ACTUAL labelled VBAT/GND pads, not an inferred neighbouring contact.
3. Start at 10 V with a conservative current limit and no external loads. Check
   protected input, +5 V MAIN, +5 V SCREEN, +3V3 AUX and +3V3 NET. Investigate a
   current-limit event rather than increasing the limit blindly. Check ripple,
   startup/restart and regulator temperature at 10, 14.4 and 20 V.
4. Confirm C1 is on the protected rail, diode orientation and no charging/backfeed
   on VBAT. Perform power-off/source-sequencing checks with limited fault energy;
   do not improvise destructive reverse-battery/load-dump tests.
5. Fit Teensy only after rails pass. Verify 3V3, baseline supply current, CPU/IMU
   health and UART. Test carrier-only, USB-only (bridge cut), and controlled dual
   power. Test GPS/NET powered with MCU off and vice versa for unintended backfeed.
6. With the display still disconnected, ramp electronic load on the SCREEN branch;
   account for the MCU/sensor load in the **5 A TOTAL** budget. Characterize startup,
   inrush, ripple, cable drop and steady-state temperature. Do not label the assembly
   'continuous 5 A' until worst-case enclosure/ambient testing supports it.
7. Confirm the particular Advance screen's J10 pinout; use its 5V_IN interface, not
   3V3_OUT. Initially power with current limiting. Verify touch, boot identity and
   bidirectional 921600-baud communication over the intended full cable length.
8. Apply a low-energy test waveform to conditioned TACH: scope loading, trip levels,
   optocoupler transitions and final pin-9 swing. Check 3.3/5/12 V amplitudes and
   1–500 Hz before any car connection. Establish actual vehicle PPR independently.
9. Use precision sources/resistors for analog testing with MCU on AND off. Sweep
   oil/AEM 0–5 V; verify input gain, clamps, open/short behavior and no power-off
   injection. Test overvoltage only with a current-limited fixture within the reviewed
   fault budget. Use resistor decades for coolant; new conversion/curve required.
   Compare displayed/logged values to instruments; DO NOT trust legacy oil/NTC scaling.
10. Program the network module via its 3V3 UART fixture. Implement/validate SPI
    identity, flow control, CRC, reset, upload ownership/auth/resume and recovery.
    No uploader firmware is included here. Test RF while GNSS runs and while the
    screen is using WiFi/BLE. Record real throughput rather than SPI theoretical rate.
11. After soldering/cleaning, install CR2032. Measure retained RTC behavior across
    hours/days without main power, removal/depletion handling, and fresh-time resync.
    Verify no cell charging in car/USB/off states and normal restart with VBAT fitted.
12. Finally test real AEM output, dedicated oil/coolant senders and conditioned
    vehicle tach with appropriate harness fusing. Keep gauge/heater power external.
    Recording, warnings, SD integrity, GPS freezes, communications and temperatures
    need vehicle validation before track use. Retain original data on upload failure.

Record instrument readings, ambient conditions, firmware identity, observed faults
and modifications. A passed software test or a green ERC/DRC report is not a
substitute for these measurements.

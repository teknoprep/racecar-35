# Rev A bring-up — no expensive boards connected first

**Never use a finger as a temperature probe.** If a part heats rapidly, smells, or
the supply unexpectedly enters current limit, switch off immediately. Do not keep
cycling power, press recovery buttons, or flash firmware to diagnose a hot chip.
Use an IR camera/thermometer or insulated contact probe. Let parts cool first.
Current limiting reduces fault energy; it does not guarantee protection from an
incorrect voltage, reverse polarity, or a semiconductor fault.

## 0. Tools and preparation

- Adjustable **current-limited bench supply**, 0–20 V or greater; NOT the car battery
  for first tests. A bare car battery can source destructive currents into mistakes.
- Multimeter, preferably oscilloscope, temperature probe, and suitable electronic
  load or resistor loads with safe heatsinking. Keep 25 W loads away from flammables.
- Regulator/screen/Teensy manufacturer pinouts, printed 1:1 assembly drawing.
- Remove all USB leads, the Teensy, external modules and screen cable. JP1 OPEN.
- Compare populated board to BOM and polarity drawings. Verify every connector's
  square pad/pin 1, not just the wire colour. No frayed strands or solder bridges.

## 1. Unpowered checks

1. Check J1/J2 pin order end-to-end through the actual harness. Verify J2 pin 2 ends
   at **screen +5V_IN on its verified J10**, not any 3V3_OUT terminal.
2. Check polarity of C1/C2 and D2; confirm U2 top-view orientation and both header
   rows. Confirm U3 pins 1/2/3 VIN/GND/VOUT and that its input is U2 VRP.
3. Confirm F1/F2 continuity and JP1 open. Confirm no solder bridge between AUX 3V3
   and MCU 3V3 or between either of those and the 5 V output.
4. Measure resistance from each supply net to GND: VIN_FUSED, 5V_MAIN, 5V_SCREEN,
   3V3_AUX and MCU 3V3. Capacitors can make the first reading low then rising.
   **There is no universal ohms threshold proving a CPU dead.** A sustained near-
   zero reading requires isolating the affected part/net before power is applied.
5. Check mounting screws/spacers cannot touch tracks or components under the modules.

## 2. Power section only — JP1 open; no peripherals

1. Set disconnected supply to **12.0 V**, confirm voltage/polarity with a meter,
   set a **100 mA starting input-current limit**, then switch its output OFF.
2. Connect J1 positive/negative. Enable power while watching current. Converter
   startup may briefly hit a low limit, but sustained limiting or a low output
   requires investigation, not blindly increasing the current limit.
3. Measure, referenced to TP1/GND:
   - TP2 / VIN_FUSED: about 12 V, positive.
   - U2 VRP / U3 input: close to input voltage, after reverse protection.
   - TP3 / +5V_MAIN: nominal **5 V**, within the module's published tolerance.
   - J2 pin 2 / +5V_SCREEN: nominal **5 V** after F2.
   - TP4 / +3V3_AUX: nominal **3.3 V** within U3 tolerance.
   - MCU VIN side of JP1 and TP5 / MCU 3V3: **unpowered** at this stage.
4. Check temperatures with an instrument and observe several minutes at no load.
   No rapidly heating logic device is acceptable. A switching module can be warm;
   compare with its datasheet, not an assumed universal temperature limit.
5. Turn OFF and discharge before changing wires. Repeat unloaded checks at 10 V
   and 20 V. Never test reverse polarity by deliberately risking attached boards.

## 3. Load tests — dummy loads, NOT the screen

- At 12 V input, start with a modest 5 V load, e.g. **10 Ω / >=5 W** (~0.5 A,
  2.5 W dissipation; the resistor will get hot). Raise the input current limit only
  after correct voltage, polarity, assembly and the expected load are confirmed.
- Increase load in steps, observing output voltage, input current, ripple, startup
  and temperature. With 25 W output at 10 V input, input current is roughly
  **2.8–3.2 A**, depending on efficiency, plus the separate AUX-regulator load.
- Test the **main rail** at 5 A with a proper rated fixture connected to the wide
  main bus/module output, not tiny test-pad wires. J2's 4 A fast fuse is not meant
  to pass a 5 A test. Do not bypass the fuse to make it pass. Use a qualified bench
  setup for high-current testing; the small test points are for measurements.
- Test the actual screen branch up to the measured intended screen current (below
  4 A, allowing startup margin). Test AUX initially to <=500 mA combined.
- Repeat at 10, 14.4 and 20 V; evaluate in the intended enclosure/ambient. Stop if
  voltage droops, a connector/neck gets too hot, oscillation/ripple is excessive,
  the fuse opens unexpectedly, or regulator thermal limiting starts.
- Check a long cable with a **remote load**: measure at its far end. Calculate
  voltage loss with the real wire gauge/length. Do not infer it from the source's
  display. This is also needed at screen startup and maximum brightness.
- Do NOT perform load-dump/EMC tests with ad-hoc battery switching. Proper automotive
  qualification needs specified waveforms, energy limits and suitable equipment.

## 4. Teensy alone

1. All supplies OFF. Keep GPS, IMU, opto, CAN, Ethernet and screen disconnected.
2. **Cut the Teensy 4.1 VUSB-to-VIN bridge** following PJRC's underside drawing.
   Inspect and verify the intended copper link is physically open. Do not cut by
   guessing from an upside-down photograph. Other circuit paths can affect an ohm
   reading; use the manufacturer procedure and isolate power sources.
3. Fit the Teensy in the two sockets, USB at the marked end. Confirm no one-pin
   offset. Fit JP1 only now. D2's drop means VIN will be slightly below main 5 V;
   it must remain in the Teensy's specified VIN range.
4. Start at 12 V with a modest ~200 mA input-current limit for the MCU-only stage;
   if startup or draw is abnormal, switch off and investigate. The expected input
   current is not the same as the Teensy's 5 V current because of the converter.
5. Verify MCU 3V3 with a meter, then firmware/USB enumeration and die-temperature
   telemetry. A normal die reading does not certify every other component is cool.
6. Once the VUSB/VIN isolation is verified, keep carrier power on as required while
   attaching USB for programming. With the bridge cut, USB alone no longer powers
   VIN. Do not feed an external 3.3 V source into the Teensy to compensate.

## 5. Modules, one at a time

All wiring changes with all power OFF; re-check current/temperature after each step.

- GPS: verify its exact VCC input permits J4's 3.3 V and its UART is 3.3 V logic.
  Cross TX/RX per README. Verify real PVT sample rate and fix acquisition. Do not
  promise M10/F10 plug-and-play without a firmware compatibility test.
- IMU: verify 18 SDA / 19 SCL, AD0 GND; check at-rest g magnitude and gyro readings.
- Tach: default is open collector into J3, output-side emitter to carrier GND.
  Check with an oscilloscope: idle ~3.3 V, pulses near 0 V, no negative/12 V spikes.
  Then confirm RPM on pin 9. Never use the raw ignition coil as a test source.
- CAN: J6 to a 3.3 V transceiver's LOGIC side; verify CANH/CANL and end termination
  on the external module before connecting to the ECU.
- Ethernet: verify the custom J7 cable against the exact W5500 breakout and ensure
  total AUX load stays within the commissioning budget.

## 6. Screen LAST

1. First measure all four cable conductors disconnected from the screen. Confirm
   +5 V is only on the power conductor and the grounds match.
2. On this particular **clone**, verify which connector truly accepts 5 V. Use its
   own documentation/pin labels/continuity measurements. Do not assume an Elecrow-
   looking connector is equivalent. If that cannot be established, STOP.
3. With both devices OFF, connect the verified screen J10: GND / +5V_IN /
   TXD0_H to carrier RX / RXD0_H to carrier TX. Use short bench wires first.
4. Observe supply draw and both ends' voltage on startup. Verify display, touch,
   telemetry, commands and long-duration uploads. Inspect serial errors, not just
   whether some characters arrive.
5. Test the full-length harness under realistic electrical conditions before a
   track session. Differential signalling is the next revision if TTL is unreliable.
6. Disconnect the Teensy UART from the screen before flashing the screen's CH340-
   shared UART0. Never run two push-pull transmitters onto that UART.

## Stop / release criteria

Do not install permanently until independent review, footprint fit, unloaded/load/
thermal/cable tests and an appropriate vehicle power-protection review are complete.
Log voltages, current, ambient and temperature with photos. A successful firmware
flash, a green LED, or a zero-DRC PCB is **not** proof of safe automotive hardware.

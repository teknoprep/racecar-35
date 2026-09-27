# Rev C electrical choices and limitations

## What is real

The `design/` PCB has assigned nets, components and routed copper. Its native
schematic uses manufacturer pin/function symbols, actual pin numbers and electrical
types. A full connected-pad comparison is required in addition to ERC and DRC.
The earlier 95-part `preview/PLACEMENT-ONLY-NOT-FOR-FAB.kicad_pcb` is a separate,
obsolete illustration and is not a source for these Gerbers.

## Selected circuits

- **Power:** Pololu 4091/D36V50F5 main regulator, 5 A input fuse, 4 A screen branch
  fuse, separate TRACO TSR 1-2433 regulators for GPS AUX and network 3V3. Regulator
  inputs and C1 are behind Pololu VRP. The raw bidirectional TVS is SMBJ20CA.
  These parts do not establish ISO 7637 / ISO 16750 or load-dump qualification.
- **Teensy:** socketed 4.1, isolated carrier VIN path and removable power jumper;
  mandatory physical VUSB–VIN bridge cut for dual power. MPU and optional external
  CAN transceiver use MCU 3V3; GPS and WiFi do not spend the Teensy's 3V3 budget.
- **GPS:** soldered NEO-M9N-00B, UART at pins 7/8 through cross-domain Ioff logic,
  external active-GPS SMA, 27 nH bias choke, RF DC block and ESD component. TPS2553
  bias switch's ILIM ties to IN (50/75/100 mA min/typ/max). USB disabled; V_BCKP
  powered from AUX, not the RTC cell. RF dimensions/antenna current need validation.
- **IMU:** MPU-6050, exact 24-pin package and datasheet support capacitors, address
  0x68. Matches existing driver. It is a legacy procurement risk: obtain genuine
  authorized stock or revise both hardware and driver for a current part.
- **Tach:** 180 kΩ / 22 kΩ divider (~202 kΩ DC loading), 1 nF filtering, independent
  positive/negative clamps, LM2903B comparator with positive feedback, MMBT3904
  locally driving VO617A-3 optocoupler, then SN74LVC2G17 Schmitt buffer to pin 9.
  Approximate 5 V-powered trip points are 1.84 V rising / 1.56 V falling, before
  tolerance; this is a design calculation, NOT a measured universal Miata interface.
  Test 3.3/5/battery-level conditioned signals up to at least 500 Hz. Shared ground
  means the optocoupler does NOT make the whole system galvanically isolated.
- **Oil/AEM:** 20 kΩ / 20 kΩ 0.1% dividers, LM4040 3.0 V shunts to GND and negative
  Schottky clamps, then 1 kΩ / 100 nF filtering and TMUX1511 isolation. Clamps do not
  dump fault current into an unpowered MCU rail. The switch's powered-off protection
  is limited to 3.6 V; this depends on the external clamps. At +20 V steady input,
  the top resistor dissipates approximately 14.5 mW and shunt current is ~0.7 mA.
  These calculations are not an ESD, reverse-battery or fast-transient test.
- **Coolant:** diode-isolated 5 V feed and 150 Ω bias into LM4040 4.096 V shunt;
  2.49 kΩ sensor pull-up; 20 kΩ / 20 kΩ sense divider with the same protected ADC
  path. Compensate the divider's 40 kΩ parallel loading on the NTC. For measured
  sense-node voltage `V` BEFORE the half-gain divider:
  `R_sensor = 2490*V / (4.096 - V - 2490*V/40000)`.
  Open/short/out-of-domain results must be invalid, not plausible temperatures.
  A verified sender resistance/temperature curve and actual reference/gain calibration
  are required. Do not silently use the old VDO-like curve for a Delphi sensor.
- **Screen:** fixed-direction SN74LVC1T45 buffers, 100 Ω series resistors and UART ESD.
  Only verified CrowPanel Advance J10 5 V level-shifted UART. The long 921600-baud
  single-ended cable remains a bench/vehicle validation item; no RS485 claim.
- **WiFi:** internal-antenna WROOM-1-N8R2; dedicated supply, buffered SPI/IRQ with
  Ioff, open-collector reset and protected service UART input. Master pins 10/11/12/13,
  READY pin 5, active-HIGH reset-assert pin 6. Boot/EN pulls and RC are explicit.
  Grounded-coplanar GNSS routing is separate from the module's antenna keepout.
- **RTC:** primary CR2032 holder and removable lead to existing Teensy VBAT/GND.
  No charger or second RTC chip; no whole-system backup or infinite-life claim.

## Verification boundary

Automated checks can find shorts, disconnected pads, source/netlist disagreement,
wrong stated pin mappings and manufacturing-layer mistakes. They cannot prove
component authenticity, solderability, assembly fit, 5 A thermal performance,
RF link quality, automotive transients, clock accuracy, uploader throughput or
firmware completeness. Independent electrical/footprint review and physical tests
remain required. No human approval record is invented by the engineering exporter.

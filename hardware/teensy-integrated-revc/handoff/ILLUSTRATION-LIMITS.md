# Illustration status — NOT FOR MANUFACTURE

The supplied assembled PNG and functional SVG/PDF show the requested Rev C
architecture and a proposed 130 x 110 mm placement. They are not schematic,
mechanical-fit, component-value, connector-pinout or manufacturing approvals.

The source illustration uses representative packages and an unconnected placement
board with **no nets, tracks or copper zones** (WiFi RF rule area only). It is not a PCB to order. That
render-only CAD, visual inventory, model assets and pick/place-style coordinates
are intentionally excluded from this RFQ package to prevent them being used as
fabrication or assembly data.

The standard footprint used to visualize the NEO receiver was not a completed
NEO-M9N land-pattern review. The depicted IMU QFN and optocoupler DIP likewise do
not select those parts or establish their pinouts. Passive and support-IC counts
are illustrative. The labelled interface blocks describe desired functions,
not completed circuits. Silkscreen/pin order must be finalized from the real netlist.

Teensy stays socketed and GPS/IMU/input conditioning are intended onboard.
The optional CAN header remains a module interface. Ethernet has been removed;
The AEM 30-0300 gauge-output terminal/input remain; a NEW lower strip illustrates
independent trunk WiFi (built-in PCB antenna/supply/service) and a CR2032 RTC holder.
Rev C places ALL six screw terminals at the LEFT edge with proposed function/pin
labels, and removes the external WiFi connector. GPS SMA remains external.
The working envelope is now 130 x 140 mm, not a mechanically released dimension.
See `WIFI_RTC_ARCHITECTURE.md`; new network firmware and VBAT contact are still needed. No direct-sensor interface or heater controller.
The drawing does not establish protection/isolation or routing; see `AFR_INTERFACE.md`.

Reproduction source is in the developer's repository at
`hardware/teensy-integrated-revc/preview/draw.py`, with its detailed preview README.
It is not needed to quote the engineering work. Request the source separately if
useful for layout discussion; do not substitute it for missing circuit design.

No Rev A Gerbers are included: that older carrier would build the wrong product.

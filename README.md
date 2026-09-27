# racecar-35

Two-MCU dash for a track car.

**Latest Rev C target:** add a third MCU—an on-board ESP32-S3 WiFi coprocessor—to
upload directly from Teensy SD over local SPI, while the screen keeps WiFi for
unchanged OTA and optional upload fallback. Add a replaceable CR2032 on Teensy VBAT
for RTC retention. This is an architecture update, **not an implemented new uplink**;
see [WiFi/RTC requirements](hardware/teensy-integrated-revc/WIFI_RTC_ARCHITECTURE.md).
Rev C has an **internal WiFi PCB antenna** and **all screw terminals on the LEFT
edge**, labelled by function/pin. **Actual 130 × 140 mm routed electrical CAD,
Gerber/drill ZIP, BOM, assembly documents and an assembled view now exist:**
[Rev C engineering prototype](hardware/teensy-integrated-revc/README.md).
It passes ERC/DRC/netlist checks but is unbuilt and not independently approved;
new WiFi firmware, oil/coolant conversions and bench validation remain.
A/B are retired under `hardware/_archive/`, not alternative orderable designs.

**WiFi-only on the CrowPanel since v0.1.148.** Ethernet/W5500 support
and the Internet-route setting have been removed. WiFi credentials/status are
always visible; old saved Ethernet selections are ignored. NTP/OTA/uploads run
through the screen. See [CLAUDE.md](CLAUDE.md) and [BUILD.md](BUILD.md) for current
architecture/toolchains; much of the original overview below is historical.
The integrated Rev C board is an unbuilt electrical prototype. **v0.1.149 source adds
opt-in AFR/lambda logging from the external AEM 30-0300 gauge output** (WHITE signal
and BROWN reference), not a direct oxygen sensor. A protected/scaled ADC input
is implemented in Rev C with clamps and powered-off isolation, but requires
independent review and physical validation. See
[AFR_INTERFACE.md](hardware/teensy-integrated-revc/AFR_INTERFACE.md).

```
+--------------+   Serial2 (UART)   +--------------+   Serial3 (UART)   +--------------+
|              |  pin 7 RX2 / 8 TX2 |              | pin 14 TX3 / 15 RX |              |
|  u-blox GNSS | <----------------> | Teensy 4.1   | -----------------> | CrowPanel S3 |
|              |   38400/9600 baud  |              |    115200 baud     |  7" 800x480  |
+--------------+                    +--------------+                    +--------------+
                                                                         (LovyanGFX UI)
```

The Teensy reads GNSS PVT data from a SparkFun u-blox module and publishes
ASCII CSV lines on Serial3. The CrowPanel ESP32-S3 listens on UART0 and
renders the dash with LovyanGFX. UART is one-way, Teensy → CrowPanel.

## Repository layout

| Path | Project | Target |
| --- | --- | --- |
| `platformio.ini`, `src/main.cpp` | Teensy firmware | Teensy 4.1 |
| `crowpanel-ui/platformio.ini`, `crowpanel-ui/src/main.cpp` | Dash UI | CrowPanel ESP32-S3 7" v3.0 |

Each subproject is its own PlatformIO env. Build them from their own
directory.

## Wire protocol

ASCII CSV lines, `\n`-terminated, 115200 8N1, ~5 Hz:

```
GPS,<fix>,<sats>,<lat_deg>,<lon_deg>,<speed_mph>,<heading_deg>
```

| Field | Meaning |
| --- | --- |
| `fix` | 0=None 1=DR 2=2D 3=3D 4=3D+DR 5=Time-only |
| `sats` | satellites in view |
| `lat_deg`, `lon_deg` | decimal degrees, 6 decimals |
| `speed_mph` | mph, 1 decimal |
| `heading_deg` | course over ground in degrees, 1 decimal |

Example: `GPS,3,12,40.123456,-74.123456,67.5,123.4`

## Wiring

| Link | From | To |
| --- | --- | --- |
| GPS → Teensy | u-blox TX | Teensy pin 7 (Serial2 RX) |
| Teensy → GPS | Teensy pin 8 (Serial2 TX) | u-blox RX |
| Teensy → Dash | Teensy pin 14 (Serial3 TX) | CrowPanel UART0 RX (GPIO 44) |
| Dash → Teensy | Teensy pin 15 (Serial3 RX) | CrowPanel UART0 TX (GPIO 43) |
| Common ground | Teensy GND | CrowPanel GND |
| Power | USB-C to CrowPanel | (Teensy powered separately by its own USB) |

Teensy ↔ CrowPanel is two devices both on 3.3 V logic — no level shifter.
A common ground is required.

## Flashing

### Teensy firmware

```powershell
pio run -t upload
```

The first time, you may need to physically tap the program button on the
Teensy. After that, `teensy_reboot` resets the board over USB.

### CrowPanel firmware

> [!IMPORTANT]
> **Disconnect the two UART jumpers between the Teensy and the CrowPanel
> before flashing the CrowPanel.** UART0 (GPIO 43/44) is shared with the
> CH340 USB-serial chip used to upload firmware. If the Teensy is
> connected, both devices will drive the same wire and the upload will
> fail or corrupt.

```powershell
cd crowpanel-ui
pio run -t upload
```

After the upload finishes ("Hash of data verified"), unplug USB-C, wire
the Teensy back in, then power the CrowPanel via USB-C again.

## End-to-end smoke test

1. Flash both firmwares (Teensy first or CrowPanel first, doesn't matter).
2. Wire everything per the table above. Double-check common GND.
3. Power the CrowPanel via USB-C. The dash should boot and show 0.0 mph
   with `LINK STALE` in red — that's correct: nothing on the wire yet.
4. Plug in the Teensy. Within a second, `LINK` should turn green and you
   should see the speed/heading/lat/lon updating at ~5 Hz, even with no
   GPS fix yet (those fields will read 0 until `FIX` shows `2D`/`3D`).
5. Take it outside or near a window. `FIX` will go yellow → green and
   `SATS` will climb as the module gets a fix.

## Debugging

- Open `pio device monitor` on the Teensy to see the same CSV stream the
  Teensy is sending — useful for confirming the GPS half is working
  before chasing UART issues on the dash side.
- Open `pio device monitor` on the CrowPanel (with the Teensy
  disconnected, and a `Serial.print` of incoming bytes added if you want
  to see what's hitting UART0). Note that with `ARDUINO_USB_CDC_ON_BOOT=0`
  the CrowPanel's `Serial` object IS UART0 — the same wire the Teensy
  uses — so you can't tap it from USB and the Teensy at the same time.
- If the panel boots black, retry the power-only test (USB-C only, no
  Teensy wires) to confirm the panel itself is alive before suspecting
  firmware.

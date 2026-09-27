# Rev C: two WiFi endpoints + battery-backed Teensy RTC

**Updated user requirement.** The earlier SCREEN-ONLY WiFi plan is superseded.
Ethernet is still removed. The PCB must have its OWN WiFi network coprocessor;
the screen keeps its built-in WiFi. Existing screen-led OTA downloads stay as-is.

**Implementation status:** hardware/firmware ARCHITECTURE, not a working new
uplink. Current v0.1.149 sources still upload through the screen. No coprocessor
firmware, SPI transport, route arbitration or new manufacturing files exist yet.
The existing Teensy code already reads its RTC and sets it on `SETTIME`; the backup
cell/holder and VBAT connection are new hardware requirements, not installed hardware.

## 1. Where WiFi lives and what each side does

```
Teensy SD -> short PCB SPI -> on-board ESP32-S3 -> WiFi -> server (primary upload)
     |
     +---- existing UART -> CrowPanel WiFi -> server (fallback upload)
                               |
                         existing OTA downloads
                         screen self-update + Teensy UART update
```

- Add soldered **ESP32-S3-WROOM-1-N8R2** as the proposed network module (8 MB
  flash, 2 MB PSRAM, **built-in PCB antenna**). The user's INTERNAL-antenna request
  supersedes the previous 1U/external-antenna proposal; no WiFi pigtail or jack. This is a THIRD programmable
  MCU, not a passive WiFi chip the Teensy can drive without software.
- Its WiFi is independent of the screen's WiFi: both can associate with the same
  hotspot/AP and have separate IP addresses. No AP-to-AP or mesh link is required.
  Initial target is 2.4 GHz WiFi; do not promise 5 GHz support.
- Primary session upload bypasses BOTH the long cabin UART and the RGB screen's
  shared memory/radio workload. Teensy owns the SD card, streams bounded chunks
  over local SPI; network module owns TCP/TLS/HTTP and responds with durable status.
  Do NOT wire a second SD host to the Teensy card.
- Keep screen WiFi for OTA and existing services, plus optional upload fallback.
  Screen WiFi/BLE exclusivity still applies to the SCREEN chip; it does not turn
  off the separate trunk module. Do not remove the existing radio arbiter.
- Both radios may be connected, but a session file has only ONE upload owner.
  Start with uploads while not recording, preserving the no-blocking-network-work
  rule in the Teensy sampling loop. No live-streaming feature is implied.

## 2. Proposed local link and pin allocation (not yet wired or implemented)

| Function | Teensy 4.1 | Network ESP32-S3 | Notes |
|---|---|---|---|
| SPI CS | 10 | GPIO10 / module pad 18 | Default inactive pull-up to NET rail |
| SPI MOSI | 11 | GPIO11 / module pad 19 | Teensy master -> ESP slave |
| SPI MISO | 12 | GPIO13 / module pad 21 | ESP slave -> Teensy master |
| SPI SCK | 13 | GPIO12 / module pad 20 | Shares Teensy's built-in LED |
| READY / IRQ | 5 | GPIO14 / module pad 22 | Proposed active-high, inactive when unpowered |
| NET reset/enable control | 6 | EN / module pad 3 | Reviewed open-drain/reset stage, not direct cross-rail drive |

These are two DIFFERENT MCUs: ESP GPIO14 is not Teensy pin 14 (the latter stays
screen TX). GPIO19/20 on the ESP are reserved for its service USB, not the Teensy's
IMU/AFR pins. Verify the selected module revision's pinout and boot straps.

Pins 5/6/10–13 were freed by Ethernet removal; they are now RESERVED for this
proposed WiFi link. **Current firmware still blinks pin 13 as an LED.** New firmware
must stop GPIO heartbeat writes before enabling SPI, or it will corrupt transfers.
Do not connect/provision the new module assuming v0.1.149 already drives it.

Use a short, reviewed SPI layout with controlled edges and flow control. Start
conservatively and characterize 10–20 MHz. Gross bus ceilings are 1.25–2.5 MB/s,
NOT promised upload rates. End-to-end performance depends on SD latency, protocol,
compression, TLS, RF, AP/Internet/server and concurrency; measure real sessions.

Required software work:
- Framed, length-bounded SPI transport; sequence IDs, CRC, credits/backpressure,
  timeouts, abort and restart; DMA-safe buffers. No long blocking SPI/SD waits in REC.
- Configure and benchmark TCP send/window buffers, batched writes and compressed
  bodies against the real path RTT. A second WiFi chip alone does NOT fix the
  small-TCP-window throughput limit seen on the screen. Compare effective raw
  KB/s and wire KB/s against the existing compressed UART path, not just SPI clock.
- Module identity/capability/status handshake and explicit upload ownership. Default
  to the existing screen path on old hardware or when direct capability is absent.
- Reuse the server's user/auth/session identifiers and resume/zblocks contract.
  Archive only after server success; failure keeps the file. Never run competing
  writers against the same session during fallback/retry.
- Share WiFi credentials/cloud account from screen settings through a length-safe
  command path (SSID/password may contain commas); do not log secrets. Report
  trunk and screen IP/RSSI independently. Do NOT repurpose retired NVS `inet`.
- New module firmware/toolchain, provisioning/programming/recovery procedure and
  tests. A factory ESP-AT image is NOT assumed to implement this SPI/HTTP protocol.

## 3. OTA remains screen-led

Do not redirect current OTA downloads to the new module. Preserve manifest board
selection, screen A/B update and screen -> Teensy UART/FlasherX update, with the
existing safety/version/hash checks. Pause uploads before an OTA can own the link.

The NEW module also needs a maintainable firmware path. Design gate: factory USB
service/recovery plus a tested update relay that still downloads through the screen
if remote module updates are offered. No new manifest entry/artifact or fourth
release build is authorized merely by this drawing; agree and implement that
extension before the module becomes a deployed dependency.

## 4. WiFi power, antenna and mechanical requirements

- Module supply **3.3 V**, NOT 5 V. Espressif specifies 3.0–3.6 V and an external
  supply capable of at least 0.5 A. Propose a dedicated **1 A-class 3.3 V buck from
  regulated 5 V**, with verified burst/startup margin, local bulk + ceramic
  decoupling and power-off isolation/sequence review. Exact regulator/protection
  parts and thermal/current performance remain unselected/unvalidated.
- Do not power it from the Teensy's ~250 mA-budget 3.3 V output. Do not simply add
  it to the already-budgeted GPS/IMU auxiliary rail. Recalculate the main **5 A
  TOTAL** load and wiring/fuse/thermal budget including WiFi peak power.
- Prefer **N8R2 (-40 to +85 C listed)** over an N8R8 Octal-PSRAM variant listed at
  only -40 to +65 C without its special ECC conditions. This is not automotive
  qualification; verify enclosure/module temperatures and procurement suffix.
- Use the **WROOM-1 built-in PCB antenna**, NOT the 1U external-connector module.
  Its envelope is 18 x 25.5 mm (larger than 1U). Orient the antenna at the bottom
  carrier edge; the illustration has a 48 x 6 mm carrier notch under/alongside
  the antenna and retains the stock footprint's all-layer RF rule area. No host
  copper, traces, components, battery metal, fasteners or cable bundle in that
  clearance. Final keepout/overhang must follow Espressif's layout guidance and
  the exact module footprint, not this illustrative shape alone.
- **Plastic enclosure or RF-transparent window required around the antenna.**
  Short distance does NOT make a closed metal enclosure/trunk RF-transparent.
  Test actual WiFi throughput with the lid closed, installed in the car, with
  screen WiFi and GNSS operating. Do not promise range or speed from antenna type.
  GPS still has its own external antenna/SMA and bias circuit; it is unchanged.
- Add NET programming/recovery access (USB D+/D-, GND, safe supply reference,
  EN/BOOT and optional serial recovery). Do not reuse the screen's UART connector.
- The revised ILLUSTRATION uses **130 x 140 mm** to make room for WiFi, its supply
  and a serviceable coin cell. Final layout/enclosure dimensions are not frozen.

## 5. RTC backup — a clock battery, not a system UPS

PJRC documents that Teensy 4.1 already has an RTC and 32.768 kHz crystal. A **3 V
CR2032 primary coin cell** connected **positive to VBAT, negative to GND** keeps
it advancing through main-power loss. No separate RTC chip/crystal is required.
It does not run the Teensy CPU, SD, WiFi, GPS or display when main power is off.

- Add a replaceable, vibration-retained holder (Keystone 1058 is an illustrative
  candidate; validate fit/retention, temperature and actual cell specification).
  Mark `RTC 3V CR2032`, `+`, and **DO NOT CHARGE**. No battery charging circuit;
  do not substitute LIR2032 or connect the cell to 5 V/3V3/sensor backup rails.
- The existing Rev A socket footprint contains only the outer Teensy rows and
  does **not** bring out VBAT. The final Rev C footprint/assembly must add the
  matching auxiliary VBAT contact/socket or an explicitly documented serviceable
  connection to that Teensy pin. It is not a spare GPIO on the outer header.
  PJRC also notes VBAT retains the On/Off power state: leave On/Off unasserted
  unless deliberately used and verify restart after power cycling with the cell.
- Install the cell AFTER soldering/cleaning; do not reflow a coin cell or solder
  directly to a non-tabbed cell. Prevent tools/debris from shorting it. Verify
  polarity and no charging current under USB-only/car-only/off conditions.
- Battery life is finite, load/temperature dependent, and has to be measured.
  Establish a replacement interval and retained-time test; never promise forever.
  EEPROM storing a timestamp is not an alternative: it cannot count elapsed OFF time.

### Time behaviour and current firmware limitations

- Current code uses `setSyncProvider(getTeensyTime)` / `Teensy3Clock.get()` and
  writes the hardware RTC on `SETTIME`. The Teensy core preserves a running backup
  RTC at boot; if it is stopped, the installed core seeds Jan 1, 2019. Loader
  behaviour can also set PC time during programming. A plausible-looking date is
  therefore NOT proof of a successful network sync.
- Required release behaviour: accept a validated initial time, keep it through
  offline boots, and periodically correct drift after actual fresh Internet time
  is available via either radio. Lack of WiFi must never clear a valid RTC.
- Add trustworthy time-validity metadata (battery-backed state with loss detection)
  and bounded timestamp validation; reject corrupt/zero `SETTIME` instead of
  overwriting a good clock. Do not overwrite retained time with a build/default
  timestamp. Keep per-session recorded timestamps monotonic when correcting RTC.
- Screen code currently relays time once per connection. Harden it to wait for an
  actual SNTP success, not merely an existing plausible ESP system clock after a
  reconnect; add safe periodic resync/retry without interleaving upload/OTA traffic.
- Older docs claim GPS sets the RTC, but current `src/main.cpp` has no GNSS UTC ->
  RTC update path. Validated GPS time can be added as an offline time source; do
  not describe that as implemented today.
- Existing firmware/RTC support is not proof the proposed battery connection or
  new synchronization policy has been implemented/bench-tested.

## Sources and acceptance gates

- [PJRC Teensy 4.1: RTC, VBAT, power, RTC RAM](https://www.pjrc.com/store/teensy41.html)
- [Espressif WROOM-1/1U datasheet v1.8](https://www.espressif.com/sites/default/files/documentation/esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf)
  (ordering table, pin definitions, recommended operating conditions).
- Installed Teensy core `cores/teensy4/startup.c` and `rtc.c` were inspected; no core
  patches are part of this architecture change.

Before release: independent circuit/footprint/power-off review; WiFi burst/thermal
and antenna/GNSS-coexistence tests; successful authenticated uploads with resets,
loss/retry/resume and matching file hashes; old-screen fallback; unchanged OTA
verification/recovery; and RTC retention after hours/days with all main supplies
removed, invalid-clock/cell-removal tests and actual-Internet-return resync tests.

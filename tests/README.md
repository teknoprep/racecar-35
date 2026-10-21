# Host regression checks

Run `python3 -m unittest discover -s tests -v` from the repository root.
`test_wifi_only.py` checks removal of the retired Ethernet paths, compatibility
with legacy `inet`/`ETH` messages, WiFi/BLE radio guarding, NTP/time relay and
lockstep versions. It also extracts and compiles the real dash row-visibility
function to verify WiFi credentials/status stay visible across sensor/SD modes
(requires g++; that case is skipped if the compiler is unavailable).

These tests do not emulate the ESP32 radio, UART, SD or vehicle hardware. Build
the Teensy and both Advance display variants as the integration check; bench
verification is separate.

`test_aem_afr.py` compiles the real shared AEM decoder/parser, dash source selector
and Teensy SD serializer under UndefinedBehaviorSanitizer. It checks published
30-0300 scaling, all ADC codes, malformed frames, faults/nulls, source/staleness,
NVS/CFG opt-in, buffer overflow rejection and the server's actual data passthrough.
It does not verify a real ADC, reference voltage or the input protection circuit.

`test_fabrication_guard.py` checks Rev C fabrication-export rejection paths:
missing design inputs, stale output removal, absent/incomplete review, changed
source hashes and conflicting duplicate pad numbers. Explicit engineering mode
is also checked to ensure it never invents an independent approval. These unit
tests are NOT electrical, DRC, mechanical, RF or successful-Gerber-export tests.

The actual routed Rev C separately exercises the full engineering export path:
`electrical/verify.py` checks 427 pin/geometry/calculation assertions and 392 real
schematic/PCB connections; export runs fresh full ERC/DRC and package checks.
`fabrication/inspect_cam.py` re-reads Gerbers and compares drill locations/diameters
using Gerbonara. None of these checks proves physical performance or approval.

`test_aemnet.py` (v0.1.177) compiles the real AEMnet decoder `src/aemnet.h` under
UndefinedBehaviorSanitizer and checks it three ways: the encoder's golden bytes against
AEM's TA2 / 30-0300 tables (worked out by hand, not from the encoder), bit-exact agreement
between the firmware decoder and the Python mirror `tools/can_sim.py validate` relies on
(3 000 random frame sets incl. UEGO valid/warm-up/fault flags), and `can_sim.py validate`
end to end against a fake CANable + a fake Teensy that decodes with the real header - it
must PASS, and must FAIL when the fake Teensy is off by 1 rpm. It does not test a real
transceiver, the Teensy's FlexCAN FIFO, or the dash; `validate` on the bench does that.

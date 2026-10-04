# Handover: RC35 bench CAN — 0x701 never arrives, and the dash refreshes at ~3 Hz

You are taking over a live hardware debugging session. Read this whole file first. It is
self-contained: you have no prior context, and the person you are working with has been
through a lot of failed theories, so **measure before you claim anything**.

Repo: `/home/chris/coding/racecar-35` (Linux host, user `chris`). Hardware is connected and
powered right now.

---

## 1. What the system is

Three pieces:

| Piece | What | Link |
|---|---|---|
| **Teensy 4.1** | the data logger (`src/main.cpp`, PlatformIO) | CAN1 (pins 22/23 via an SN65HVD230 transceiver) for CAN; Serial3 @921600 for the dash |
| **CrowPanel Advance 7" (ESP32-S3)** | the driver display (`crowpanel-arduino/RaceDash/RaceDash.ino`) | UART0 (`Serial` on that side) @921600 to the Teensy |
| **CANable 2.5 (slcan)** | bench CAN adapter on this PC | `/dev/ttyACM0` |

Bench data comes from a **fake CAN broadcaster** (`tools/can_sim.py`, owned by another agent —
**do NOT edit it**) which emits two frames per cycle at 100 Hz = **200 frames/s**:

```
0x700 RC35_CORE  [0:2] rpm u16  [2:4] map kPa x10 u16  [4] tps % x2 u8
                 [5] clt whole F u8   [6] iat whole F u8   [7] seq u8
0x701 RC35_AUX   [0] afr x10 u8  [1] batt V x10 u8  [2:4] oil PSI x10 u16
                 [4] adv degBTDC i8   [5:8] reserved
```
Authoritative layout doc: `tools/RC35_BENCH_CAN.md`. The firmware parses these in
`pumpCAN()` in `src/main.cpp`.

Ports right now: `/dev/ttyACM0` = CANable, `/dev/ttyACM1` = Teensy USB serial (CDC).
The panel is **not** on this PC's USB (no CH340 present) — it hangs off the Teensy's Serial3.

---

## 2. THE BUGS TO FIX

### Bug A (the important one): 0x701 AUX frames never arrive

Reading the Teensy's USB serial while the broadcaster runs:

```
$ timeout 8 cat /dev/ttyACM1 | grep -E "^(BENCH|CANDIAG)"
BENCH   frames/s=206 oil=-1 rpm=6208 (RC35 0x700/0x701)
CANDIAG frames/s=3795 dup=99% total=48950 base_hits=0 ids=[0x700] state=? ACK_ERR=0 CRC_ERR=0 FRM=0 STF=0 TXerr=0 RXerr=0 flt=?  dashw=4135 dashsent=14767 dashskip=0 emit=2946
BENCH   frames/s=211 oil=-1 rpm=5150 (RC35 0x700/0x701)
CANDIAG frames/s=0    dup=0%  total=48950 base_hits=0 ids=[]      ...
BENCH   frames/s=245 oil=-1 rpm=3554 (RC35 0x700/0x701)
```

Read that carefully:

* `rpm` **is** arriving → **0x700 is being received and parsed**.
* `oil=-1` **always** → **0x701 is NEVER received** (oil comes from the AUX frame; afr and batt
  are also AUX, and the `ECU,` line shows `-1` for all three: `ECU,6324,1660,977,910,-1,1210,-1,-1`).
* The id ring only ever contains `[0x700]`.
* `frames/s` alternates between **~3800** and **0** on successive 1 Hz reports, with `dup=99%`.
* `TXerr=0 RXerr=0 CRC_ERR=0 FRM_ERR=0` on the Teensy → the Teensy itself is not erroring.
* The broadcaster's own log claims `100 cycles/s, tx queue 0 B, load 5.2%, tx fail 0`.

So ~3800 fps of *only 0x700*, 99% duplicate, while the tool thinks it is sending 200 frames/s of
two different ids. Something is swallowing/mangling 0x701 specifically.

**Strong hypotheses to test, in order:**
1. **The CANable's single TX mailbox is stuck retransmitting 0x700.** slcan adapters typically
   have one TX mailbox; if 0x700 is never ACKed it retransmits forever and **0x701 is never
   transmitted at all**. That would explain "only 0x700, thousands of times, nothing else".
   Test: put a second listener on the bus (the teensy's own CANDIAG can't see sent ids directly).
   Cheaper test: run the broadcaster in its `ms3` compatibility mode
   (`python3 tools/can_sim.py ms3 -p /dev/ttyACM0 --hz 10`) and see whether *multiple* distinct
   ids appear in the Teensy's id ring. If the MS3 ids all arrive fine, the fault is specific to
   how 0x700/0x701 are being sent or ACKed.
2. **Only the low id bit is failing** — 0x700 vs 0x701 differ in the last ID bit. A marginal bit
   timing / sample point on the Teensy's transceiver, or a bad CANH/CANL pair, can fail
   systematically on one pattern. Note CRC/FRM are 0 on the Teensy, which argues against gross
   electrical noise, but check termination anyway: with power off, **CANH↔CANL should read ~60 Ω**
   (two 120 Ω ends). One terminator = 120 Ω = reflections. Also confirm a **common ground**
   between the CANable and the Teensy's transceiver, and that CANH/CANL are not swapped.
3. **A receive filter/mailbox misconfiguration in `canBegin()`** (`src/main.cpp`): the firmware
   calls `Can1.begin(); Can1.setBaudRate(can_baud); Can1.setMaxMB(16); Can1.enableFIFO();`
   `enableFIFO()` clears every mailbox and installs **no filters**, relying on accept-all.
   Verify that 0x701 is not being filtered out. (A filter bug would be a clean explanation for
   "exactly one id arrives".)

Also relevant: this bus was previously storming (~3800 fps) because the Teensy was in
**listen-only** mode — `FlexCAN_T4::begin()` sets `CTRL1[LOM]` and `setBaudRate()` can return
early *before* the line that clears it. `canBegin()` now clears LOM explicitly, and there is a
bus-off recovery task. Both live in `src/main.cpp` (search `canBusOffRecover`, `FLEXCAN_CTRL_LOM`).

### Bug B: the panel refreshes at ~3 Hz

The owner wants a high refresh rate on the **physical panel**, and explicitly does not care what
the on-PC clone shows.

Measured on the Teensy side (USB diagnostics, `src/main.cpp`):

| Field | Meaning | Value |
|---|---|---|
| `emit` | cumulative `emitToDash()` calls | advancing ~**99/s** → emitting at 100 Hz |
| `dashsent` | lines written to Serial3 | advancing ~**500/s** |
| `dashskip` | lines dropped because Serial3 was full | **0** |
| `dashw` | `Serial3.availableForWrite()` | **4135** of a ~4096-byte buffer → link nearly idle |

Panel side (verified in `RaceDash.ino`):
* `Serial.setRxBufferSize(32768)` before `Serial.begin(921600)` → 32 KB RX ring, no overflow at
  the ~30 KB/s this link carries.
* `pumpUart()` is the **first** call in `loop()`, drains everything available, no rate cap.
* No blocking calls in `loop()`.

**So the data leaves at 100 Hz and the panel should consume it — yet the owner sees ~3 Hz. That
contradiction is unresolved and is the core of the handover.**

The panel has a `UART n lines/s (link OK)` row on its **Tools** page (added in v0.1.162). That
single number resolves it: ≥400 means the panel receives everything and the fault is in its
render path; single digits means it is not actually getting the data.
**The panel is not on this PC's USB, so nobody has ever measured the panel.** Getting it onto
USB (or reading that row off the screen) is the highest-value next action.
If the row **does not exist**, the panel is running pre-0.1.162 firmware — the panel OS updates
have repeatedly timed out, so do not assume it is current.

---

## 3. How to reproduce / measure

```bash
cd /home/chris/coding/racecar-35

# broadcaster (holds /dev/ttyACM0 - ONE opener only)
python3 -u tools/can_sim.py bench -p /dev/ttyACM0 --hz 100 > /tmp/bench.log 2>&1 &

# the Teensy's own 1 Hz diagnostics (gives the numbers in the tables above)
timeout 12 cat /dev/ttyACM1 | grep -E "^(CANDIAG|BENCH|VER)"
```
**Gotcha that cost an hour:** `cat /dev/ttyACM1 | head -12` shows only telemetry — at ~500
lines/s that is 30 ms of data and the 1 Hz `CANDIAG` line never appears. Always filter with
`grep`, or read for several seconds.

GUI (spawns the broadcaster itself): `./tools/canbench.sh start|stop|status`, or
`python3 tools/can_gui.py --port /dev/ttyACM0 --hz 100 --autostart`. It also opens a DASH VIEW
window (`tools/dash_view.py`) that clones the panel layout — useful for judging *layout*, but
**it is fed from the broadcaster's own waveform, so its smoothness proves nothing about the
panel.** The owner is (rightly) annoyed that this was treated as evidence.

### Flashing
Teensy:
```bash
pio run -t upload                       # may fail with "error writing to Teensy"
# fallback that works: put it in the bootloader first, then program
python3 -c "import serial,time; s=serial.Serial('/dev/ttyACM1',134,timeout=0.2); time.sleep(0.3); s.close()"
teensy_loader_cli --mcu=TEENSY41 -v .pio/build/teensy41/firmware.hex
```
`teensy_loader_cli -w` waits **forever** for the bootloader — never run it without `timeout`.
The panel needs `arduino-cli` (see `CLAUDE.md` for the exact FQBN) and is not currently on USB.

---

## 4. Traps that already burned hours — do not repeat them

1. **One process per serial port.** A USB CDC port cannot be shared. The GUI holds
   `/dev/ttyACM1` while it is "connected", which silently breaks any other reader *and* makes
   `pio run -t upload` fail with `error writing to Teensy`. Also: a stale broadcaster holding
   `/dev/ttyACM0` makes the next one fail to bind and the dash then sees nothing while a zombie
   process feeds the bus.
2. **Do not use `pkill -f <pattern>` where the pattern appears in your own command line** — it
   kills your own shell (exit 143). Use anchored patterns or the bracket form (`can_sim[.]py`).
3. **`dup=99%` is NOT a storm.** On a slowly-varying signal most consecutive frames are
   byte-identical; the firmware documents this explicitly. Do not "fix" it.
4. **Do not measure the USB mirror rate and call it the dash rate.** They are different UARTs.
   This mistake is why the refresh bug survived so long.
5. **`frames/s` on the CANDIAG line counts every frame read from the FIFO, any id.** `base_hits`
   counts only MS3 ids (0x5E8+); `BENCH` counts 0x700/0x701 only.
6. The Teensy emits telemetry to **both** USB and Serial3. `usbTele()` / `dashTele()` are
   non-blocking guards (drop-if-full, with counters) — added because blocking USB writes were
   throttling the whole loop.

---

## 5. Version state

Firmware is 0.1.169 (published to `https://racecar.api.blueuc.com/firmware/manifest.json` for
teensy + crowpanel5adv + crowpanel7adv). Release contract is in `CLAUDE.md`: bump both
`FIRMWARE_VERSION` defines, rebuild all three artifacts, publish, re-download and verify hashes.

---

## 6. What to deliver

1. **Root cause of the missing 0x701 AUX frame**, with the measurement that proves it, and a fix.
   This is the priority — without it the logger never receives oil/afr/battery on the bench.
2. **Root cause of the panel's ~3 Hz refresh**, with the panel's own `UART lines/s` number as
   evidence, and a fix.
3. If you change firmware, keep the release discipline (both version defines, all three
   artifacts, publish + verify) and update `CLAUDE.md`.

Do not report progress as "should be fixed" — report the number, before and after.

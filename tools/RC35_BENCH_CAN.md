# RC35 bench CAN frames — handover for the firmware agent

**Status:** the *tool* side is done and on the wire. The **logger does not parse
these frames yet** — that is the only firmware work needed for OIL (and the rest)
to show up on the dash from the bench. Owner of `src/main.cpp`: the firmware
agent. The RC35 edits were deliberately **not** applied and not left in the tree.

## Why this exists

The logger only understood MS3Pro "Simplified Dash Broadcasting". That broadcast
has **no oil channel at all**, which is why the bench tool had to impersonate an
MS3Pro (`tools/can_sim.py ms3`) and *still* could not drive OIL. RC35 bench
frames carry every channel the dash shows, in our own layout:

    TEMP(coolant)  OIL  VOLT  AFR  IAT  MAP  TPS  + RPM

MS3 framing is kept in the tool as an explicit compatibility mode for real-MS3
work; it is no longer the fake-data source.

## Wire layout — authoritative

```
0x700 RC35_CORE   [0:2] rpm      u16   rpm            (1 rpm/LSB)
                  [2:4] map_kpa  u16   kPa x10
                  [4]   tps_pct  u8    % x2           (0..127.5 %)
                  [5]   clt_f    u8    TEMP, whole °F (0..255 F)
                  [6]   iat_f    u8    whole °F
                  [7]   seq      u8    rolling cycle counter (drop detection)

0x701 RC35_AUX    [0]   afr_x10  u8    AFR x10        (147 = 14.7)
                  [1]   batt_x10 u8    V x10          (138 = 13.8 V)
                  [2:4] oil_x10  u16   PSI x10        (450 = 45.0 PSI)
                  [4]   adv_deg  i8    °BTDC, signed  (negative = retard)
                  [5:8] reserved            must be 0 — room to grow
```

Reference implementation (encoder + decoder, keep the two sides in sync):
`tools/can_sim.py` → `rc35_frames()` / `decode_rc35()`. Golden bytes are pinned
in `tests/test_can_sim.py::test_rc35_wire_bytes_are_frozen`.

### Scalings the firmware must apply (easy to get wrong)

| Field | On the wire | Firmware target (`can_ecu`) |
| --- | --- | --- |
| rpm | rpm | `rpm` as-is |
| map | kPa x10 | `map_x10` as-is |
| tps | % **x2** | `tps_x10 = buf[4] * 5` |
| clt / iat | whole °F | `clt_f_x10 = buf[5] * 10`, `iat_f_x10 = buf[6] * 10` |
| afr | AFR x10 | `afr_x10` as-is |
| batt | V x10 | `bat_x10` as-is |
| oil | PSI x10 (u16) | new `oil_x10` as-is |
| adv | °BTDC i8 | not stored today (no field in `ECU`) — optional |

## Firmware changes (src/main.cpp) — 6 small edits

1. **Constants** next to `CAN_BASE_ID`:

```cpp
  constexpr uint32_t CAN_BENCH_CORE_ID  = 0x700;   // RC35 bench: rpm/map/tps/TEMP/IAT
  constexpr uint32_t CAN_BENCH_AUX_ID   = 0x701;   // RC35 bench: afr/batt/oil/adv
```

2. **`struct CanEcu`** — two new members:

```cpp
    int16_t  oil_x10    = -1;  // RC35 bench frames only (a real MS3 dash has no oil)
    uint32_t bench_ms   = 0;   // millis() of the most recent RC35 bench frame
```

3. **`pumpCAN()`** — count bench frames (diagnostics):

```cpp
        if (msg.id == CAN_BENCH_CORE_ID || msg.id == CAN_BENCH_AUX_ID) can_diag.bench_hits++;
```

   plus a `uint8_t bench_hits = 0;` in `struct CanDiag` and its reset next to
   `base_hits` in `canDiagReport()`.

4. **`pumpCAN()`** — parse them, next to the existing `CAN_BASE_ID + 3` case:

```cpp
            case CAN_BENCH_CORE_ID:
                can_ecu.rpm       = (((uint16_t)msg.buf[0] << 8) | msg.buf[1]);
                can_ecu.map_x10   = (int16_t)(((uint16_t)msg.buf[2] << 8) | msg.buf[3]);
                can_ecu.tps_x10   = (int16_t)msg.buf[4] * 5;   // % x2 -> x10
                can_ecu.clt_f_x10 = (int16_t)msg.buf[5] * 10;  // whole °F -> x10
                can_ecu.iat_f_x10 = (int16_t)msg.buf[6] * 10;
                can_ecu.last_ms   = now;
                can_ecu.bench_ms  = now;
                break;
            case CAN_BENCH_AUX_ID:
                can_ecu.afr_x10   = (int16_t)msg.buf[0];
                can_ecu.bat_x10   = (int16_t)msg.buf[1];
                can_ecu.oil_x10   = (int16_t)(((uint16_t)msg.buf[2] << 8) | msg.buf[3]);
                can_ecu.last_ms   = now;
                can_ecu.bench_ms  = now;
                break;
```

5. **`pumpCAN()` staleness** — bench oil must expire on its own clock (a real MS3
   keeps `last_ms` fresh forever, so oil would otherwise latch a stale value):

```cpp
    if (can_ecu.bench_ms != 0 && now - can_ecu.bench_ms > CAN_STALE_MS) {
        can_ecu.oil_x10  = -1;
        can_ecu.bench_ms = 0;
    }
```

6. **`emitToDash()`** — oil source priority (everything else already flows
   through `can_ecu`, and `use_can` already goes true whenever CAN data is live):

```cpp
    const bool bench_live = (can_ecu.bench_ms != 0)
                            && (millis() - can_ecu.bench_ms <= CAN_STALE_MS);
    int16_t        oil_psi_x10 = bench_live ? can_ecu.oil_x10 : readOilPsiX10();
```

⚠️ **Do not add fields to the `CANDIAG` line** (the dash parses that format). For
visibility, print the bench counter on **USB only**:

```cpp
    Serial.printf("BENCH frames/s=%u oil=%d rpm=%u (RC35 0x%03lX/0x%03lX)\n",
                  can_diag.bench_hits, can_ecu.oil_x10, can_ecu.rpm,
                  (unsigned long)CAN_BENCH_CORE_ID, (unsigned long)CAN_BENCH_AUX_ID);
```

## Verification

```bash
python3 tools/can_sim.py bench --dry-run          # prints frames + decode, no hardware
python3 tools/can_sim.py bench --profile chop --dry-run   # hard steps on EVERY channel
python3 tools/can_sim.py bench -p /dev/ttyACM0 --hz 100   # live; prints cycles/s + worst gap
python3 -m pytest tests/test_can_sim.py -q        # golden bytes + pty cadence tests
```

To judge a display's refresh rate, use **`--profile chop`**: a physically plausible
sweep (the default, and `pull`) changes too slowly to see whether the screen is
updating at 1 Hz or 25 Hz. `chop` flips every channel hard, twice a second.

* Expected on the wire: **200 frames/s** at `--hz 100` (two frames per cycle),
  worst cycle gap a few ms, `tx queue 0 B`.
* With the parser in: USB serial shows `BENCH frames/s=200 oil=<fake value>`.
* Dash: set **Settings → Sensor data source = MegaSquirt**. MAP/TPS/AFR/IAT are
  read from `ecu.*` only in that mode (`fromCan`), so in Direct mode those four
  rows will not display the bench values even though they are on the bus.

## The "screen refreshes slowly" investigation

Measured facts (not guesses):

* The **source** is not the bottleneck. `tools/can_sim.py bench` puts
  **201 frames/s** on the wire at `--hz 100`, evenly — verified with a pty
  harness (`tests/test_can_sim.py`) that fails if the cadence collapses.
* The **logger's emit** is not a 1 Hz fallback either: `loop()` emits on
  `freshThisCall || millis() - lastEmit >= emit_floor_ms` with
  `emit_floor_ms = 40` (25 Hz floor, independent of GPS), and `ENG`/`ECU` ride
  that same call. It used to be GPS-gated; that was already fixed.

So the remaining suspects are on the **dash display path** (RaceDash.ino). In
order of likelihood for "the data is slow on the screen":

1. **Sensor-source rule.** MAP/TPS/AFR/IAT come from `ecu.*` only when the dash's
   sensor source is MegaSquirt (`fromCan`); in Direct mode those rows never show
   bench CAN data (settings, not code — but it *looks* like a dead pipeline).
2. **`rpm_smooth` (`rpmsm`, −10..10, default 0).** A new, heavy smoothing on the
   RPM path makes RPM *lag* rather than jump. Check the default and the effective
   time constant before blaming the data — a bench `--profile pull` sweep should
   track within ~100 ms.
3. **Deliberate display caps.** The RPM number is rate-capped (~10 Hz) and the
   dash-page sensor rows repaint only when the *displayed* value changes (the
   `LastDrawn`/`sens_tag` keys; TEMP/OIL/VOLT are integers). A slow coolant ramp
   legitimately repaints ~2×/s — that is the data, not a bug.
4. **`ecuStale` = 2000 ms** on the dash (`ecu.last_ms`), same as the Teensy's
   `CAN_STALE_MS`: a bench source slower than 0.5 Hz blanks every CAN row to
   `---` together. Ours is 100 Hz, so if rows blank, the *UART link* paused, not
   the CAN source.

### How to localize it in three measurements

1. **On the CAN wire** — run `bench --hz 100` and read its once-a-second line:
   `cycles/s` should be ~100 and `worst gap` a few ms. (Source proven good.)
2. **On the USB serial** — count `ENG,` lines per second: expect ~25. If it is
   ~1, the emit cadence regressed (look at `emit_floor_ms`).
3. **On the dash** — with `--profile chop` (hard steps on every channel), watch
   TPS/MAP/RPM together:
   * all three slow → the UART link or the dash loop, not the values;
   * MAP/TPS slow, RPM live → the sensor-row path / sensor-source rule;
   * RPM alone laggy → `rpm_smooth`.

   Also confirm each row's own source in Settings (v0.1.157 made the sensor SOURCE
   per-item): a row set to Direct while the data arrives over CAN will sit still
   no matter how fast the bus is.

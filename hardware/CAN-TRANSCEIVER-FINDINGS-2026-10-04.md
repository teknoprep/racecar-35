# CAN transceiver root cause (bench, 2026-10-04) — what Rev F must guarantee

**For the agent owning the Rev F PCB / PCBWay order. Read all of it.**

## What happened
The bench logger (Teensy 4.1 + Amazon "3-Pack SN65HVD230 CAN Transceiver Module", ASIN
B0FDLDXCK9) received CAN perfectly but **never transmitted a single dominant bit**, so it
never ACKed. A lone sender (CANable / MS3) then retransmits its highest-priority frame
forever: ~3,800 identical frames/s, values updating only 3–4×/s, 0x701 never arriving.
Swapping to another module from the same pack changed nothing.

**Root cause: the chip on those boards is NOT an SN65HVD230.** It behaves as a
TJA1051T/3-class part (same SOIC-8 footprint, different pin meaning):

| Pin | SN65HVD230 (what the board is built for) | Chip actually fitted |
|---|---|---|
| 3 VCC | 3.3 V | **needs 4.5–5.5 V** — transmitter locked out below that |
| 5 | Vref (output, left floating) | **VIO** = logic supply for TXD/RXD |
| 8 | Rs | **S / standby** — high = receive-only |

With VCC = 3.3 V the driver was locked out; with VIO floating it was phantom-fed from
TXD (measured **2.1 V**), so RXD collapsed whenever TXD went low — an internal TXD→RXD
echo that made every Teensy-side self-test look like a working transmitter.
**Fix that worked: VCC 5 V, pin 5 (VIO) 3.3 V, pin 8 to GND** → exactly 200 frames/s,
both IDs, 0 errors.

## Rev F status — design is correct, verified from `design/checked-netlist.xml`
`U21` (TCAN1042HGV-Q1): pin 3 VCC = `+5V_MAIN` (C49, C51), **pin 5 VIO = `+3V3_MCU`**
(C50), **pin 8 STB = GND**, pin 1 TXD = `CAN_TX` → U1.29 (pin 22), pin 4 RXD = `CAN_RX`
→ U1.28 (pin 23). **Do not change this.** The remaining risks are below.

## Required actions
1. **No unapproved substitution of U21.** In the PCBWay order notes and BOM comment:
   "U21 = TI `TCAN1042HGVDRQ1` from authorized distribution only (TI / Mouser / Digi-Key /
   LCSC-original). Do NOT substitute without written approval. No clones, no re-marked parts."
   Approved alternates only: `TCAN1042VDRQ1`, then NXP `TJA1051T/3/1J` (pin 8 S → GND).
   - ⚠️ The **`V` suffix is mandatory** (it is the VIO pin). `TCAN1042DRQ1`, `TCAN1042HDRQ1`,
     `TCAN1042GDRQ1` etc. have **pin 5 = NC** → RXD swings to 5 V → destroys Teensy pin 23.
   - **Never** an SN65HVD23x, plain `TJA1051T` (no /3), `TJA1050`, or any "pin-compatible"
     Chinese equivalent (SIT/clone) for U21.
2. **Incoming inspection when the boards arrive:** read U21's top marking and compare it
   with the "Device Marking" column of TI's Package Option Addendum for the ordered part
   number. Photo it into `logs/`. Mismatch = reject before power-up.
3. **Bring-up must be done with J1 (12 V) powered.** `+5V_MAIN` comes from the buck; on
   Teensy-USB power alone U21 VCC = 0 and the board shows the **identical "no ACK"
   symptom**. That is not a fault, and nobody should "fix" it.
4. **Make `design/BRINGUP.md` CAN acceptance un-foolable** (it already has DC levels and
   the CANable test — add/keep these, they are the tests that actually discriminate):
   - DC: U21.3 = **5.0 V**, U21.5 = **3.3 V** (NOT ~2 V — a floating VIO reads ~2 V),
     U21.8 = **0 V**.
   - **Single-frame ACK test (decisive):** CANable (Elmue slcan, `ME` error reports on)
     sends **one** frame; the logger's `CANDIAG total` must rise by **exactly 1**.
     Thousands = no ACK = FAIL. CANable must report no `E?3……` (protocol error 3 = No ACK).
   - Bench feed at 100 Hz: logger `CANDIAG frames/s` ≈ **200 (= sent rate ±2 %)**, both
     `0x700` and `0x701`, `oil`/`afr`/`batt` ≠ -1. **~3,800 frames/s is a retransmit
     storm and is a FAIL even though it looks like "receiving".**
   - `CANTX,10` from the Teensy: CANable receives each frame exactly once; Teensy TEC stays 0.
5. **Known misleading diagnostics — do not use them as pass criteria:**
   - Firmware `CANDRIVE` / `CANPROBE` "TX PATH OK": an internal TXD→RXD echo passes it.
   - Bench console verdict "fps ≥ 50 → ACKING": a storm passes it.
   - `CANHOLDON` + multimeter on CANH–CANL: on Rev F the **TCAN1042 TXD dominant time-out
     (~ms) releases the bus**, so a healthy board reads ~0 V. Not a valid test on Rev F.
     (It was valid on the old module only because that chip had no time-out.)
6. **Bench modules (pre-Rev F) from that Amazon pack:** document in `WIRING.md` /
   `hardware/breadboard/README.md`: **VCC = 5 V, pin 5 = 3.3 V, pin 8 = GND** — never
   3.3 V on VCC, never 5 V on pin 5.

No firmware change is needed for Rev F (500 kbit/s, normal ACKing mode).

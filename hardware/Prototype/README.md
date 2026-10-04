# Prototype board — wiring sheets

Hand-soldered Teensy 4.1 protoboard. **These are the sheets for everything
except the tach optocoupler** — that one lives next door in
[../breadboard/OPTO_WIRING.svg](../breadboard/OPTO_WIRING.svg) (PC817, pins
1–4, R1 1 kΩ, R2 4.7 kΩ). Physical board layout:
[../breadboard/BREADBOARD.svg](../breadboard/BREADBOARD.svg).

Open the `.png`; the `.svg` is the source. Same visual language as the opto
sheet: schematic on top, warnings, then a tick-list.

| Sheet | Covers |
| --- | --- |
| [00-Teensy-pin-map.png](00-Teensy-pin-map.png) | Every wire on the Teensy, pin by pin, plus the J1–J7 terminal map |
| [01-Power-and-ground.png](01-Power-and-ground.png) | 12 V → buck → J1, the 5 V and 3.3 V rails, star ground, pre-power checks |
| [02-UART-screen-and-video.png](02-UART-screen-and-video.png) | J2 → CrowPanel J10 (921600) and J3 → Pi 5 (115200, 3.3 V) |
| [03-GPS-and-IMU.png](03-GPS-and-IMU.png) | NEO-M9N on Serial2, MPU-6050 on Wire, boot banner, IMU mounting |
| [04-Analogue-inputs.png](04-Analogue-inputs.png) | Oil 10k/20k, coolant 150 Ω, AEM 20k/20k, optional CAN transceiver + SD |

## Terminals — left edge, top to bottom

| Block | Positions | Goes to |
| --- | --- | --- |
| J1 POWER | 5V · GND | 5 V buck output only (never 12 V) |
| J2 SCREEN | GND · 5V · TX · RX | CrowPanel **J10** only (XH2.54), 921600 |
| J3 VIDEO | GND · TX · RX | Pi 5 header 6 / 10 / 8, 3.3 V, 115200 |
| J4 TACH | SIG · GND | PC817 input via R1 1 kΩ |
| J5 OIL | 5V · SIG · GND | 0.5–4.5 V transducer, divider at A2 |
| J6 NTC | SIG · GND | Coolant sender, 150 Ω pull-up at A3, own ground wire |
| J7 AEM | WHT · BRN | AEM 30-0300 pin 9 / pin 10, divider at A6 |

## Numbers that are firmware contracts

- Oil divider **10 kΩ / 20 kΩ** ⇒ `V_adc = V_sensor × 2/3`, 150 PSI full scale.
- Coolant pull-up **150 Ω** to 3.3 V, VDO 1600–22 Ω curve (Steinhart-Hart in `src/main.cpp`).
- AEM divider **20 kΩ / 20 kΩ 0.1%** + 1 kΩ/100 nF ⇒ 0.500 gain, `AFR = 2.3750·V + 7.3125`.
- Tach: PC817 plus **4.7 kΩ** pull-up to 3.3 V on pin 9; pulses/rev default **2.0**.
- Teensy 3.3V pin: about **250 mA** of external load (PJRC). GPS + IMU + pull-ups only.
- Baud: screen **921600**, Pi **115200**, GPS raised to **230400**.

## Regenerating

```bash
python3 generate.py        # writes the .svg and, with cairosvg, the .png
```

Edit `generate.py` rather than the generated files. Keep this folder in sync with
[../breadboard/PINOUT.md](../breadboard/PINOUT.md) — that file is the full
pin-by-pin build sheet, and `WIRING.md` at the repo root is the older
reference (its baud and GPS sections have been corrected).

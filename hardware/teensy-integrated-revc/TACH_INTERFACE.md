# Rev C tach input — confirmed scope / proposed electrical contract

**Requirement established by the user:** ECU or instrument-cluster tach signal,
initially Mazda Miata 1990–2005 Spec Miata. The optocoupler MUST be on the main PCB.
**NEVER a raw engine-spark/coil input.** This document specifies the design intent;
it is not a finished schematic, a validated input rating, or fabrication approval.

## Accepted source family

- Documented, already-conditioned ECU/dedicated tach output.
- Documented conditioned tach signal at the instrument-cluster connector.
- An external ignition-to-tach adapter ONLY if its output meets the eventual
  voltage, polarity, timing and transient limits of this interface.

**Not accepted:** coil negative, ignition primary/secondary, a spark plug lead,
injector drive, or an unconditioned VR/magnetic pickup. A terminal marked IG-/TACH
is not sufficient evidence of signal conditioning on an unfamiliar installation.
Do not add a coil/high-voltage mode or jumper to this board. Use a separate adapter
for those sources, as the user requested.

## Architecture

```
ECU / cluster TACH ---- high-impedance, protected sense input
                                  |
                           filter + hysteresis
                                  |
                  local driver supplies optocoupler LED current
                                  |
                         ONBOARD OPTOCOUPLER
                                  |
                      3.3V pull-up / clean logic edge
                                  |
                         Teensy pin 9 (tach)
ECU/cluster reference ---- signal return / board reference
```

The factory tach line should NOT have to supply several milliamps directly into
an optocoupler LED. A high-impedance input senses the line; a locally powered stage
drives the optocoupler. This avoids depressing a weak cluster/open-collector
pull-up and interfering with the factory tach or ECU output.

An optocoupler in the signal path does not, by itself, mean the complete dash is
galvanically isolated from the car: power and ground may still be common. Do not
advertise full system isolation unless isolated power/returns actually provide it.

## Design targets to resolve into a reviewed schematic

- Accept conditioned nominal **3.3 V, 5 V and 12 V pulses**; include the normal
  charging-voltage range for battery-level outputs. Do not assume every Miata year
  or aftermarket ECU provides the same amplitude.
- Target **>=100 kohm DC input resistance** in the normal operating region, without
  unintended back-drive into the car's tach line. Verify powered AND unpowered
  loading, and include the behaviour of clamps and any optional pull-up.
- Include deliberate voltage/current limiting, reverse-pulse/ESD protection,
  hysteresis and sensible filtering. Input thresholds must be checked across supply,
  tolerance and temperature corners. Ratings are not frozen merely by these targets.
- Default: **no added pull-up into the vehicle line**. Provide a disabled-by-default
  provision only if required for a verified open-collector output without its own
  pull-up/cluster. Select its voltage/current to the source's documented limits;
  do not blindly add a pull-up to car battery voltage.
- Optocoupler output must be a valid **0–3.3 V logic signal** on Teensy pin 9 over
  temperature, ageing and input duty cycle; never the marginal ~1.5 V LOW previously
  seen with an inadequately driven external module.
- Protect against loss of input/board power and avoid GPIO supply backfeeding.
- Frequency headroom: validate through at least **500 Hz**, with additional test
  margin where practical. At the existing 2 pulses/rev setting, 8000 RPM is about
  **267 Hz**. This is a design-margin calculation, not proof of every year's PPR.
- Keep the existing adjustable `CFG,rpmppr` / NVS `rpmppr`; begin with 2 pulses/rev
  only as a commissioning assumption, then check against a trusted ECU/cluster RPM.
- Connector: separate, clearly labelled **TACH SIGNAL** and **SIGNAL RETURN**.
  No external 12 V supply wire is required merely to feed the tach interface; a
  signal whose HIGH level is 12 V is different from the board's power input.
- Silkscreen/manual: **ECU / CLUSTER TACH ONLY — NO COIL / IGNITION**.

## Year-specific validation before claiming compatibility

Obtain the correct NA/NB wiring diagram for the car/ECU in use. Establish the actual
conditioned signal pin, common reference, normal HIGH/LOW levels, output topology
(push-pull/open-collector), pulse width, edge rate and pulses/revolution. Never guess
one wire colour or ECU pin for the entire 1990–2005 span.

Scope the signal with and without the proposed interface, with the stock cluster
connected where applicable. Verify the new board does not change the stock reading,
prevent an output from reaching a valid HIGH/LOW, or backfeed when either device is
unpowered. Check at cranking, idle, high RPM, radio/alternator activity and through
the intended cable. Use proper instrumentation, not a Teensy GPIO as a voltage probe.

## Ignition-adapter boundary

Any separate ignition pickup/converter must deliver a **clean, bounded, compatible
tach waveform**. 'Less powerful' or '12 V powered' is not an adequate output rating:
flyback spikes must be removed, not simply made smaller. The main board should not
be expected to absorb the ignition energy that belongs in that external device.

## What is not yet complete

Final optocoupler/comparator/driver selection, resistor/capacitor/clamp values,
corner calculations, circuit simulation/review, the integrated PCB layout and bench
validation are still required. No new Gerber files are produced by this scope
clarification. The previous Rev A carrier still expects an EXTERNAL optocoupler and
must not be mistaken for this revised onboard-input design.

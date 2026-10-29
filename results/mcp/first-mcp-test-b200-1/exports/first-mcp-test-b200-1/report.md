# Sized design 1

- Circuit: built-in OTA
- Task: spec_7a49c4517cee, run first-mcp-test-b200-1
- Simulations used: 157
- ngspice: ngspice-47 : Circuit level simulation program

## Sizing

| Parameter | Value |
|---|---|
| W_in | 14.000 um |
| L_in | 0.600 um |
| W_load | 12.000 um |
| L_load | 1.200 um |
| W_tail | 16.000 um |
| L_tail | 0.600 um |
| I_bias | 26.000 uA |

## Targets

- Gain >= 35 dB
- UGF >= 25 MHz
- Phase margin >= 60 deg
- Power <= 200 uW

## Corners (stored results)

| Corner | Gain (dB) | UGF (MHz) | PM (deg) | Power (uW) | Round trip |
|---|---|---|---|---|---|
| typical (27 C, 1.8 V) | 36.04 | 32.55 | 78.66 | 46.80 | PASS |
| hot_low_supply (125 C, 1.62 V) | 36.63 | 27.34 | 78.32 | 42.12 | PASS |
| cold_low_supply (-40 C, 1.62 V) | 35.10 | 38.24 | 78.93 | 42.12 | PASS |
| hot_high_supply (125 C, 1.98 V) | 36.90 | 27.41 | 78.32 | 51.48 | PASS |
| cold_high_supply (-40 C, 1.98 V) | 35.48 | 38.46 | 78.88 | 51.49 | PASS |

## Round trip

Revalidated every corner from exact exported netlist and model snapshots (verified cache reuse allowed). Tolerance: relative 0.001. Result: **PASS**.

## Reproduce

Run `ngspice -b sized.cir` in this folder (typical corner). It writes `ac.tsv`. `ptm180.lib` must stay next to `sized.cir`.

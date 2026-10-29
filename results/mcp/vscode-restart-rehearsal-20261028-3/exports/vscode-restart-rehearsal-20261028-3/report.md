# Sized design 1

- Circuit: built-in OTA
- Task: spec_a33707277b1b, run vscode-restart-rehearsal-20261028-3
- Simulations used: 36
- ngspice: ngspice-47 : Circuit level simulation program

## Sizing

| Parameter | Value |
|---|---|
| W_in | 6.000 um |
| L_in | 0.800 um |
| W_load | 16.000 um |
| L_load | 1.600 um |
| W_tail | 18.000 um |
| L_tail | 0.450 um |
| I_bias | 32.000 uA |

## Targets

- Gain >= 30 dB
- UGF >= 20 MHz
- Phase margin >= 60 deg
- Power <= 200 uW

## Corners (stored results)

| Corner | Gain (dB) | UGF (MHz) | PM (deg) | Power (uW) | Round trip |
|---|---|---|---|---|---|
| typical (27 C, 1.8 V) | 36.14 | 27.31 | 77.09 | 57.60 | PASS |
| hot_low_supply (125 C, 1.62 V) | 36.62 | 22.68 | 76.87 | 51.84 | PASS |
| cold_low_supply (-40 C, 1.62 V) | 35.44 | 32.88 | 77.15 | 51.85 | PASS |
| hot_high_supply (125 C, 1.98 V) | 36.83 | 22.61 | 76.89 | 63.36 | PASS |
| cold_high_supply (-40 C, 1.98 V) | 35.73 | 32.97 | 77.10 | 63.37 | PASS |

## Round trip

Revalidated every corner from exact exported netlist and model snapshots (verified cache reuse allowed). Tolerance: relative 0.001. Result: **PASS**.

## Reproduce

Run `ngspice -b sized.cir` in this folder (typical corner). It writes `ac.tsv`. `ptm180.lib` must stay next to `sized.cir`.

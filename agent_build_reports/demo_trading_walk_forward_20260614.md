# Demo Trading Walk-Forward Test

- Hidden window: `2026-06-15` to `2026-06-30`
- Agent as-of date: `2026-06-12`
- Initial demo capital: `50,000,000 VND`
- Execution assumption: buy at hidden-window first-day open, evaluate at end-date close.
- Strict app rule: buy only `candidate_long` symbols; otherwise hold cash.

## Agent State At Cutoff

- Recommendation counts: `{'candidate_long': 2, 'watch': 27, 'avoid_or_reduce': 1}`

| Rank | Symbol | Recommendation | Score | Confidence | Risk | Suggested Weight |
|---:|---|---|---:|---:|---|---:|
| 1 | VJC | candidate_long | 0.652 | 0.652 | medium | 50.00% |
| 2 | ACB | candidate_long | 0.618 | 0.618 | medium | 50.00% |
| 3 | SAB | watch | 0.598 | 0.498 | high | 0.00% |
| 4 | STB | watch | 0.563 | 0.563 | low | 0.00% |
| 5 | POW | watch | 0.548 | 0.448 | high | 0.00% |
| 6 | SSB | watch | 0.539 | 0.439 | high | 0.00% |
| 7 | GAS | watch | 0.534 | 0.434 | high | 0.00% |
| 8 | TPB | watch | 0.516 | 0.416 | high | 0.00% |
| 9 | GVR | watch | 0.508 | 0.408 | high | 0.00% |
| 10 | VCB | watch | 0.487 | 0.413 | high | 0.00% |
| 11 | VIB | watch | 0.473 | 0.427 | high | 0.00% |
| 12 | BVH | watch | 0.440 | 0.460 | high | 0.00% |

## Portfolio Result

- Strict app portfolio: `2` positions, gross `-0.88%`, net `-1.27%`, cash `0%`
- VN30 constituent equal-weight gross: `0.49%`; net-if-traded `0.09%`
- VN30 index gross: `1.83%`
- VNINDEX index gross: `3.09%`

## Forced Top 5 Basket

- Equal-weight gross: `2.11%`
- Equal-weight net after fees/tax: `1.70%`

| Symbol | State | Score | Entry Open | Exit Close | Gross | Net |
|---|---|---:|---:|---:|---:|---:|
| VJC | candidate_long | 0.652 | 139.23 | 139.50 | 0.19% | -0.21% |
| ACB | candidate_long | 0.618 | 23.10 | 22.65 | -1.95% | -2.34% |
| SAB | watch | 0.598 | 48.50 | 48.50 | 0.00% | -0.40% |
| STB | watch | 0.563 | 71.10 | 73.80 | 3.80% | 3.38% |
| POW | watch | 0.548 | 13.55 | 14.70 | 8.49% | 8.05% |

## Forced Top 8 Basket

- Equal-weight gross: `1.87%`
- Equal-weight net after fees/tax: `1.46%`

| Symbol | State | Score | Entry Open | Exit Close | Gross | Net |
|---|---|---:|---:|---:|---:|---:|
| VJC | candidate_long | 0.652 | 139.23 | 139.50 | 0.19% | -0.21% |
| ACB | candidate_long | 0.618 | 23.10 | 22.65 | -1.95% | -2.34% |
| SAB | watch | 0.598 | 48.50 | 48.50 | 0.00% | -0.40% |
| STB | watch | 0.563 | 71.10 | 73.80 | 3.80% | 3.38% |
| POW | watch | 0.548 | 13.55 | 14.70 | 8.49% | 8.05% |
| SSB | watch | 0.539 | 14.65 | 16.20 | 10.58% | 10.14% |
| GAS | watch | 0.534 | 83.30 | 77.40 | -7.08% | -7.45% |
| TPB | watch | 0.516 | 16.45 | 16.60 | 0.91% | 0.51% |

## Forced Top 10 Basket

- Equal-weight gross: `0.74%`
- Equal-weight net after fees/tax: `0.34%`

| Symbol | State | Score | Entry Open | Exit Close | Gross | Net |
|---|---|---:|---:|---:|---:|---:|
| VJC | candidate_long | 0.652 | 139.23 | 139.50 | 0.19% | -0.21% |
| ACB | candidate_long | 0.618 | 23.10 | 22.65 | -1.95% | -2.34% |
| SAB | watch | 0.598 | 48.50 | 48.50 | 0.00% | -0.40% |
| STB | watch | 0.563 | 71.10 | 73.80 | 3.80% | 3.38% |
| POW | watch | 0.548 | 13.55 | 14.70 | 8.49% | 8.05% |
| SSB | watch | 0.539 | 14.65 | 16.20 | 10.58% | 10.14% |
| GAS | watch | 0.534 | 83.30 | 77.40 | -7.08% | -7.45% |
| TPB | watch | 0.516 | 16.45 | 16.60 | 0.91% | 0.51% |
| GVR | watch | 0.508 | 35.40 | 32.80 | -7.34% | -7.71% |
| VCB | watch | 0.487 | 62.30 | 62.20 | -0.16% | -0.56% |

## Interpretation

- Under the current strict decision threshold, the agent opened 2 strict positions.
- The ranked watchlist can be evaluated as a separate deployment layer when strict candidate selection is too narrow.
- This is a short 7-trading-day window, so it is a smoke test of workflow behavior rather than statistical validation.

# Demo Trading Walk-Forward Test

- Hidden window: `2026-06-22` to `2026-06-30`
- Agent as-of date: `2026-06-19`
- Initial demo capital: `50,000,000 VND`
- Execution assumption: buy at hidden-window first-day open, evaluate at end-date close.
- Strict app rule: buy only `candidate_long` symbols; otherwise hold cash.

## Agent State At Cutoff

- Recommendation counts: `{'watch': 15, 'avoid_or_reduce': 15}`

| Rank | Symbol | Recommendation | Score | Confidence | Risk | Suggested Weight |
|---:|---|---|---:|---:|---|---:|
| 1 | VJC | watch | 0.550 | 0.550 | medium | 0.00% |
| 2 | POW | watch | 0.498 | 0.502 | medium | 0.00% |
| 3 | STB | watch | 0.489 | 0.511 | low | 0.00% |
| 4 | ACB | watch | 0.486 | 0.514 | medium | 0.00% |
| 5 | SAB | watch | 0.477 | 0.423 | high | 0.00% |
| 6 | GVR | watch | 0.466 | 0.434 | high | 0.00% |
| 7 | BID | watch | 0.444 | 0.456 | high | 0.00% |
| 8 | SSB | watch | 0.431 | 0.469 | high | 0.00% |
| 9 | VIC | watch | 0.414 | 0.586 | medium | 0.00% |
| 10 | TPB | watch | 0.395 | 0.505 | high | 0.00% |
| 11 | VCB | watch | 0.321 | 0.579 | high | 0.00% |
| 12 | HDB | watch | 0.320 | 0.680 | medium | 0.00% |

## Portfolio Result

- Strict app portfolio: `0` positions, gross `0.00%`, net `0.00%`, cash `100%`
- VN30 constituent equal-weight gross: `0.41%`; net-if-traded `0.01%`
- VN30 index gross: `1.28%`
- VNINDEX index gross: `1.78%`

## Forced Top 5 Basket

- Equal-weight gross: `1.87%`
- Equal-weight net after fees/tax: `1.47%`

| Symbol | State | Score | Entry Open | Exit Close | Gross | Net |
|---|---|---:|---:|---:|---:|---:|
| VJC | watch | 0.550 | 140.50 | 139.50 | -0.71% | -1.11% |
| POW | watch | 0.498 | 14.00 | 14.70 | 5.00% | 4.58% |
| STB | watch | 0.489 | 72.20 | 73.80 | 2.22% | 1.81% |
| ACB | watch | 0.486 | 22.20 | 22.65 | 2.03% | 1.62% |
| SAB | watch | 0.477 | 48.10 | 48.50 | 0.83% | 0.43% |

## Forced Top 10 Basket

- Equal-weight gross: `2.28%`
- Equal-weight net after fees/tax: `1.87%`

| Symbol | State | Score | Entry Open | Exit Close | Gross | Net |
|---|---|---:|---:|---:|---:|---:|
| VJC | watch | 0.550 | 140.50 | 139.50 | -0.71% | -1.11% |
| POW | watch | 0.498 | 14.00 | 14.70 | 5.00% | 4.58% |
| STB | watch | 0.489 | 72.20 | 73.80 | 2.22% | 1.81% |
| ACB | watch | 0.486 | 22.20 | 22.65 | 2.03% | 1.62% |
| SAB | watch | 0.477 | 48.10 | 48.50 | 0.83% | 0.43% |
| GVR | watch | 0.466 | 35.00 | 32.80 | -6.29% | -6.66% |
| BID | watch | 0.444 | 41.80 | 42.40 | 1.44% | 1.03% |
| SSB | watch | 0.431 | 14.85 | 16.20 | 9.09% | 8.65% |
| VIC | watch | 0.414 | 205.50 | 220.00 | 7.06% | 6.63% |
| TPB | watch | 0.395 | 16.25 | 16.60 | 2.15% | 1.75% |

## Interpretation

- Under the current strict decision threshold, the agent chose risk control over deployment and would hold cash.
- The ranked watchlist still had useful signal: the forced top baskets outperformed the VN30 constituent equal-weight basket over this short window.
- This is a short 7-trading-day window, so it is a smoke test of workflow behavior rather than statistical validation.

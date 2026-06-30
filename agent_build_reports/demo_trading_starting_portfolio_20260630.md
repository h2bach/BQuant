# Demo Trading Starting Portfolio Allocation

- Agent context date: `2026-06-30`
- Capital: `50,000,000 VND`
- Selection mode: `risk_adjusted_watchlist_deployment_affordable_board_lot`
- Max positions: `8`
- Board lot: `100`
- One-lot cap filter: `<= 22% capital`
- Residual cash redeployment: `add board lots to underweight selected holdings`
- Risk multipliers: `{'low': 1.0, 'medium': 0.85, 'high': 0.55}`
- Imported rows: `8`
- Invested value: `49,780,000 VND`
- Residual cash: `220,000 VND`

| Symbol | Agent State | Risk | Score | Confidence | Adj Score | Target W. | Qty | Price VND | Value VND | Actual W. |
|---|---|---|---:|---:|---:|---:|---:|---:|---:|---:|
| STB | watch | low | 0.567 | 0.567 | 0.321 | 21.38% | 100 | 73,800 | 7,380,000 | 14.76% |
| POW | watch | medium | 0.542 | 0.542 | 0.250 | 16.64% | 600 | 14,700 | 8,820,000 | 17.64% |
| TCB | watch | medium | 0.381 | 0.619 | 0.200 | 13.34% | 200 | 33,500 | 6,700,000 | 13.40% |
| ACB | watch | medium | 0.375 | 0.625 | 0.199 | 13.26% | 300 | 22,650 | 6,795,000 | 13.59% |
| HDB | watch | medium | 0.370 | 0.630 | 0.198 | 13.19% | 300 | 25,850 | 7,755,000 | 15.51% |
| SSB | watch | high | 0.463 | 0.437 | 0.111 | 7.41% | 200 | 16,200 | 3,240,000 | 6.48% |
| BID | watch | high | 0.469 | 0.431 | 0.111 | 7.40% | 100 | 42,400 | 4,240,000 | 8.48% |
| SAB | watch | high | 0.487 | 0.413 | 0.111 | 7.37% | 100 | 48,500 | 4,850,000 | 9.70% |

## Demo Trading Snapshot After Import

- Cash: `220,000 VND`
- Market value: `49,780,000 VND`
- Equity: `50,000,000 VND`

## Portfolio PnL + Agent Analysis

- Historical buy point: `2026-06-15` open
- Current data point: `2026-06-30` close
- Entry value: `48,075,000 VND`
- Current value: `49,780,000 VND`
- Gross PnL: `+1,705,000 VND` (`+3.55%`)
- Estimated net PnL if closed: `+1,508,438 VND` (`+3.13%`)
- Winners / losers / neutral: `6 / 1 / 1`

| Symbol | Qty | Entry | Current | Gross PnL | Gross Return | Net If Closed | Agent Action | State | Score | Risk | Trend | 5D | 20D | RS20 |
|---|---:|---:|---:|---:|---:|---:|---|---|---:|---|---|---:|---:|---:|
| POW | 600 | 13,550 | 14,700 | +690,000 | +8.49% | +643,613 | HOLD | watch | 0.542 | medium | uptrend | +2.80% | +8.49% | +7.34% |
| TCB | 200 | 31,700 | 33,500 | +360,000 | +5.68% | +333,740 | WATCH | watch | 0.381 | medium | sideways | +4.52% | +5.02% | +3.86% |
| SSB | 200 | 14,650 | 16,200 | +310,000 | +10.58% | +297,505 | TRIM | watch | 0.463 | high | sideways | +6.93% | +12.50% | +11.35% |
| STB | 100 | 71,100 | 73,800 | +270,000 | +3.80% | +240,885 | HOLD | watch | 0.567 | low | uptrend | +2.93% | +10.81% | +9.66% |
| HDB | 300 | 25,400 | 25,850 | +135,000 | +1.77% | +103,748 | WATCH | watch | 0.370 | medium | sideways | +0.78% | +3.82% | +2.66% |
| BID | 100 | 41,650 | 42,400 | +75,000 | +1.80% | +58,153 | TRIM | watch | 0.469 | high | uptrend | -0.59% | +1.68% | +0.53% |
| SAB | 100 | 48,500 | 48,500 | 0 | 0.00% | -19,400 | HOLD | watch | 0.487 | high | uptrend | +1.46% | +3.19% | +2.04% |
| ACB | 300 | 23,100 | 22,650 | -135,000 | -1.95% | -162,158 | WATCH | watch | 0.375 | medium | sideways | +1.34% | +4.72% | +3.56% |

### Agent Reasoning Summary

- `POW`: buy thesis remains strongest among the current basket by realized PnL and uptrend; hold while trend and 20D momentum stay positive.
- `STB`: low-risk holding with uptrend and strongest score in the imported basket; hold unless trend weakens or score falls materially.
- `TCB`, `HDB`, `ACB`: medium-risk sideways names; keep on watch rather than add aggressively. `ACB` is the only historical loser from the selected buy point.
- `SSB`, `BID`: high-risk names; agent action is `TRIM` despite positive PnL because risk is high and short-term momentum/liquidity quality is less robust.
- `SAB`: flat gross PnL and high risk, but current trend is still uptrend. Hold only at controlled sizing; estimated net if closed is slightly negative after costs.

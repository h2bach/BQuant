# Skill: Backtesting

## Purpose

Use this skill whenever implementing or reviewing strategy simulation logic.

## Required Modules

- `backtesting/strategies.py`
- `backtesting/run_backtest.py`
- `backtesting/metrics.py`
- `backtesting/reports.py`

## MVP Strategy

Baseline strategy: Top-K technical score

1. Rank VN30 symbols by `technical_score` on rebalance dates
2. Select top `K`
3. Allocate equal weight
4. Hold until the next rebalance
5. Apply transaction cost
6. Save run metrics

Default parameters:

- `top_k = 5`
- `rebalance_freq = weekly`
- `transaction_cost = 0.0015`

## Metrics

Compute:

- `total_return`
- `annualized_return`
- `volatility`
- `sharpe`
- `max_drawdown`
- `win_rate`
- `turnover`
- `transaction_cost`

## Critical Rules

- Use shifted weights when computing returns.
- Do not use same-day ranking and execution without a shift.
- Do not use target labels in strategy decisions.
- Keep transaction cost explicit and testable.
- Save summary results to `backtest_runs`.

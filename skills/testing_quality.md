# Skill: Testing and Quality

## Purpose

Use this skill whenever adding tests, validating outputs, or reviewing research integrity risks.

## Test Location

Put tests under:

- `tests/`

Use `pytest` for the first pass unless the repo adopts another framework later.

## Data Tests

Check:

- no duplicate `symbol + trading_date` in clean OHLCV
- `high >= low`
- `volume >= 0`
- `close > 0`
- no missing `symbol`
- valid dates

## Feature Tests

Check:

- rolling features are computed per symbol
- labels use the correct future shift
- signal features exclude target leakage
- `technical_score` stays in an expected range

## Backtest Tests

Check:

- weights sum to an expected bound
- transaction costs are applied
- returns use shifted weights
- target columns are not used in portfolio selection

## Model and Signal Tests

Check:

- time-based train/test split
- no target columns in feature inputs
- predictions include `symbol`, `date`, and `model_version`
- signal labeling thresholds are correct

## Review Priorities

- bias and leakage
- broken time alignment
- silent schema drift
- accidental raw-data overwrite

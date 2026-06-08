# Skill: Feature Engineering

## Purpose

Use this skill whenever building technical features, combined features, or training labels.

## Required Modules

- `features/technical_features.py`
- `features/build_feature_table.py`

## Technical Features

Compute per symbol:

- `return_1d`
- `return_5d`
- `return_20d`
- `log_return_1d`
- `volatility_20d`
- `ma_20`
- `ma_50`
- `price_to_ma20`
- `rsi_14`
- `macd`
- `macd_signal`
- `atr_14`
- `volume_zscore_20`
- `trend_score`
- `momentum_score`

## Combined Features and Labels

Build:

- `technical_score`
- `nlp_score`
- `market_regime_score`
- `risk_score`
- `target_return_5d`
- `target_up_5d`

MVP placeholder rules:

- `nlp_score = 0.0`
- `market_regime_score = 0.5`
- `risk_score = volatility rank`

## Critical Rules

- Always sort by `symbol` and `trading_date`.
- Compute rolling features per symbol only.
- Never compute rolling windows across symbols.
- Drop insufficient-history rows only in derived feature tables.
- Use targets for training and evaluation only.
- Do not use target columns in live signal logic.

## Storage Targets

- Silver input: `clean_ohlcv_daily`
- Gold outputs: `technical_features_daily`, `combined_features_daily`

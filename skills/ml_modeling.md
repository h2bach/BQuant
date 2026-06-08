# Skill: ML Modeling

## Purpose

Use this skill whenever extending BQuant from rule-based baselines into supervised models.

## Status

This is a later phase. Do not start here before the OHLCV, feature, backtest, and signal pipeline is stable.

## Required Modules

- `models/build_dataset.py`
- `models/train_baseline.py`
- `models/train_lightgbm.py`
- `models/predict_signal.py`

## Modeling Rules

- Use time-based train/test splits.
- Never random-shuffle time-series labels for validation.
- Exclude target columns from model features.
- Keep feature generation separate from model training.
- Version artifacts and prediction outputs.

## Minimum Prediction Output

Predictions should carry:

- `symbol`
- `trading_date`
- `model_version`
- score or probability output

## Preferred Sequence

1. baseline dataset builder
2. simple benchmark model
3. stronger tree-based model
4. comparison and validation
5. optional integration into signal scoring

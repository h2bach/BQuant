# Skill: Signal Generation

## Purpose

Use this skill whenever turning features or model outputs into trading signals.

## Required Module

- `models/predict_signal.py`

## Baseline Scoring Formula

```text
final_score =
    0.45 * technical_score
  + 0.25 * nlp_score
  + 0.15 * market_regime_score
  - 0.15 * risk_score
```

## Signal Labels

- `BUY`: top 20 percent by `final_score`
- `AVOID`: bottom 20 percent by `final_score`
- `HOLD`: everything else

## Outputs

Write signals to:

- DuckDB table: `trading_signals`
- Parquet path: `data/lake/gold/trading_signals/`

## Rules

- Do not use target labels to generate current signals.
- Keep the formula explicit and versioned.
- Include `model_version` even for rule-based baselines.
- Store explanations as support text, not as a source of truth for the score.
- Keep disclaimers clear: output is decision support, not financial advice.

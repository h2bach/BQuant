# Skill: NLP and LLM Extension

## Purpose

Use this skill whenever working on the later-phase news, sentiment, event, or explanation stack.

## Status

This is not part of the first OHLCV MVP. Build it only after the core market-data pipeline works.

## Planned NLP Flow

```text
news source
-> raw_news
-> clean_text
-> entity_linking
-> sentiment_analysis
-> event_extraction
-> nlp_features_daily
-> combined_features_daily
```

## Initial NLP Features

- `sentiment_1d`
- `sentiment_3d`
- `sentiment_7d`
- `positive_event_count`
- `negative_event_count`
- `risk_event_score`
- `news_count`

## LLM Explanation Role

LLM output should explain an existing signal, not create the signal itself.

Expected shape:

```json
{
  "symbol": "FPT",
  "signal": "BUY",
  "summary": "...",
  "positive_factors": [],
  "negative_factors": [],
  "risk_factors": [],
  "confidence": 0.0,
  "disclaimer": "This is not financial advice."
}
```

## Rules

- Do not let the LLM invent market data, metrics, or news.
- Use retrieved warehouse or vector-store context.
- Store explanations as structured support output.
- Keep trading decisions grounded in computed data, not generated prose.

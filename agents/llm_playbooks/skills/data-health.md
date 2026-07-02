# Data Health

## Purpose
Verify whether BQuant data is fresh enough for analysis and trading decisions.

## Required Inputs
- Expected daily date.
- Latest daily/index/intraday coverage.
- Data quality status and stale-day counts.
- Manifest or refresh-state warnings.

## Procedure
1. Compare latest available data with expected trading date.
2. Flag stale daily or market index data before portfolio advice.
3. Treat failed quality rows as blocked symbols.
4. Separate source-data issues from model uncertainty.

## Output Fields
- `data_status`
- `freshness_warning`
- `blocked_symbols`
- `decision_allowed`

## Risk Checks
- If daily or market index is stale, warn explicitly.
- If symbol quality is not `pass`, do not recommend BUY.

## Failure Modes
- Missing exchange holidays can overstate stale days.
- Intraday gaps should not block daily-only backtests.

## Do Not
- Do not hide data freshness problems.
- Do not infer missing prices.

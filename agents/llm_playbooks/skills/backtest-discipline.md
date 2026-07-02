# Backtest Discipline

## Purpose
Prevent future leakage and keep walk-forward evaluation honest.

## Required Inputs
- `as_of_date`
- `trade_date`
- Context cutoff statement.
- Execution and valuation prices.

## Procedure
1. Use only fields dated on or before `as_of_date`.
2. Make the decision before `trade_date` execution.
3. Execute at configured trade-date price.
4. Evaluate only after decisions are stored.
5. Preserve prompts, raw responses, validations, and orders.

## Output Fields
- `as_of_date`
- `trade_date`
- `decision_source`
- `validation_status`

## Risk Checks
- Reject rationales mentioning future dates.
- Reject symbols absent from the context.

## Failure Modes
- Accidentally using latest recommendations in a historical run creates leakage.

## Do Not
- Do not use future return or final PnL in a decision rationale.

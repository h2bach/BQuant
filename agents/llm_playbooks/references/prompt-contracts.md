# Prompt Contracts

Backtest decisions must return JSON only.

Required top-level fields:

- `as_of_date`
- `trade_date`
- `market_view`
- `risk_mode`
- `qaoa_agreement`
- `cash_weight`
- `positions`
- `portfolio_notes`
- `risk_warnings`

Position fields:

- `symbol`
- `action`
- `target_weight`
- `confidence`
- `risk_level`
- `ta_summary`
- `qaoa_summary`
- `rationale`

Execution constraints:

- BUY/HOLD/WATCH can be used for any supplied symbol.
- SELL/TRIM can only be used for currently held symbols with `sellable_quantity > 0`.
- If a holding is not sellable under T+2.5, keep it as HOLD/WATCH and mention settlement lock.

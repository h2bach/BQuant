# Explanation And Citation

## Purpose
Make LLM decisions auditable by tying every recommendation to supplied fields.

## Required Inputs
- TA field names and values.
- QAOA selected symbols and proposed weights.
- Portfolio state.
- Market regime.

## Procedure
1. Explain BUY with trend, momentum, liquidity, relative strength, and QAOA evidence.
2. Explain HOLD with current signal quality and risk.
3. Explain SELL/TRIM with deterioration, risk, or constraint evidence.
4. State uncertainty when signals conflict.

## Output Fields
- `ta_summary`
- `qaoa_summary`
- `rationale`
- `risk_warnings`

## Risk Checks
- Explanations must not cite unavailable fields.

## Failure Modes
- Long generic explanations reduce auditability.

## Do Not
- Do not invent news, macro events, or unseen prices.

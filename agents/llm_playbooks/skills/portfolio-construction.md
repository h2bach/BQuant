# Portfolio Construction

## Purpose
Convert symbol signals into a valid portfolio under BQuant constraints.

## Required Inputs
- Current cash and holdings.
- Sellable quantity.
- Target weights.
- Max positions and max symbol weight.
- Fees, taxes, slippage, board lot.

## Procedure
1. Keep at least configured cash buffer.
2. Use at most 8 active positions.
3. Cap any symbol at 15%.
4. Prefer lower turnover when signals are close.
5. Sell weak/high-risk holdings before opening new names if cash is constrained.
6. Respect board-lot execution.
7. For SELL/TRIM, first verify the position has `sellable_quantity > 0`.

## Output Fields
- `cash_weight`
- `positions`
- `target_weight`
- `portfolio_notes`

## Risk Checks
- Do not oversell unsettled shares.
- Do not emit SELL/TRIM for locked T+0/T+1/T+2 holdings.
- Do not create positions too small to matter after board-lot rounding.

## Failure Modes
- Optimized target weights may not be executable due to cash/lot constraints.
- High turnover can erase signal edge.

## Do Not
- Do not exceed cash or position limits.
- Do not use target weight 0 as a sell instruction unless shares are sellable.

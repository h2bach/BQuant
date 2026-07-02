# Vietnam Market Rules

## Purpose
Apply practical Vietnam equity-market constraints to demo trading decisions.

## Required Inputs
- Board lot.
- Trade date.
- Lot settlement date.
- Sellable quantity.
- Fees and taxes.

## Procedure
1. Round buy/sell quantities to board lots.
2. Treat newly bought shares as locked until T+2.5.
3. Use sellable quantity for SELL/TRIM.
4. If `sellable_quantity = 0`, SELL and TRIM are forbidden. Use HOLD or WATCH and explain the settlement lock.
5. Account for buy fee, sell fee, and sell tax.

## Output Fields
- `sellable_quantity`
- `execution_constraint`
- `cash_after_trade`

## Risk Checks
- A SELL/TRIM signal cannot execute if shares are not sellable.
- Treat T+0, T+1, and T+2 morning holdings as locked for daily open rebalancing.

## Failure Modes
- Daily backtests approximate T+2.5 as sellable after the settlement day for next-day open execution.

## Do Not
- Do not sell unsettled shares.
- Do not suggest SELL/TRIM for a symbol with `sellable_quantity = 0`.

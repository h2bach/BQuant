# Risk Management

## Purpose
Control drawdown, concentration, and regime risk before maximizing return.

## Required Inputs
- Market regime and volatility regime.
- Symbol volatility, ATR, drawdown, beta.
- Liquidity state.
- Current exposure and cash.

## Procedure
1. Reduce exposure in bearish or high-volatility regimes.
2. Size down high ATR or high drawdown symbols.
3. Prefer liquid symbols for larger weights.
4. Avoid concentrated exposure when signals are mixed.
5. Use SELL/TRIM for deteriorating holdings.

## Output Fields
- `risk_mode`
- `risk_level`
- `risk_warnings`
- `position_size_adjustment`

## Risk Checks
- High beta plus bearish market requires caution.
- Low liquidity increases execution risk.

## Failure Modes
- Defensive positioning can lag sharp rebounds.

## Do Not
- Do not maximize expected alpha while ignoring drawdown and liquidity.

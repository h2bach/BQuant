# Technical Analysis

## Purpose
Use TA signals as evidence for direction, timing, and risk, not as isolated trading rules.

## Required Inputs
- Trend scores and moving-average state.
- Momentum indicators: RSI, MACD histogram, ROC, stochastic, MFI.
- Volatility indicators: ATR, Bollinger width, realized volatility, drawdown.
- Liquidity and volume indicators.
- Relative strength vs VN30/VNINDEX.

## Procedure
1. Start with trend: prefer bullish bias when price/MA structure and ADX agree.
2. Confirm momentum: MACD histogram, RSI zone, ROC, and MFI should not conflict sharply.
3. Check volatility and drawdown before sizing.
4. Check liquidity before any BUY or top-up.
5. Use relative strength to rank symbols within VN30.
6. Combine scores; avoid single-indicator decisions.

## Output Fields
- `ta_action_bias`
- `ta_summary`
- `trend_evidence`
- `momentum_evidence`
- `risk_evidence`

## Risk Checks
- RSI above 70 can be strong momentum or short-term overbought; require context.
- High ATR/drawdown reduces size even when momentum is positive.

## Failure Modes
- Sideways markets create false breakouts.
- Low liquidity can distort volume indicators.

## Do Not
- Do not buy solely because RSI is low.
- Do not sell solely because RSI is high.

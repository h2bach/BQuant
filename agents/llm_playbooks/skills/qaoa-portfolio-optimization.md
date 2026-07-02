# QAOA Portfolio Optimization

## Purpose
Interpret QAOA as a constrained portfolio-selection signal generated from TA alpha, covariance risk, turnover, and position-count penalties.

## Required Inputs
- Candidate symbols.
- Expected alpha terms.
- Covariance/risk terms.
- QAOA selected symbols.
- Proposed weights.
- Backend/fallback status.

## Procedure
1. Check whether QAOA ran with simulator or fallback.
2. Compare selected symbols with TA composite ranking.
3. Treat selected weights as a starting allocation.
4. Override QAOA only when TA, data quality, liquidity, or portfolio constraints justify it.
5. Explain every override.

## Output Fields
- `qaoa_agreement`
- `qaoa_summary`
- `selected_symbols`
- `override_reason`

## Risk Checks
- QAOA is not an oracle; it optimizes the supplied objective only.
- Candidate pool size and QUBO penalties shape the result.

## Failure Modes
- Simulator fallback can select a basket without quantum sampling.
- Bad alpha estimates produce bad optimized portfolios.

## Do Not
- Do not follow QAOA blindly.
- Do not override QAOA without citing supplied evidence.

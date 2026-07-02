# QAOA QUBO Reference

BQuant QAOA v1 solves binary asset selection:

```text
minimize:
  - expected_alpha * x
  + risk_aversion * x' covariance x
  + turnover_penalty
  + concentration_penalty
  + high_risk_penalty
```

The selected basket is converted into weights after solving. Weights are capped and normalized with a cash buffer.

QAOA output should be interpreted as optimizer evidence. It is not a guarantee of future return.

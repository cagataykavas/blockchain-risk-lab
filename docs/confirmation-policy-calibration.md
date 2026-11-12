# Confirmation policy calibration

Applications often choose a fixed confirmation count without recording what
historical tail-risk claim the count is intended to support. The calibrator in
`blockchain_risk.confirmation_policy` converts reviewed reorganization-depth
observations into a bounded, reproducible policy decision.

## Evidence contract

Each input value is the **maximum canonical reorganization depth in one
independent exposure window**, including a zero for a window with no reorg.
Supplying only reorg events would condition away quiet periods and materially
bias the estimated breach probability. The caller also supplies a constrained
`observation_policy_id` that identifies the windowing and source policy.

For candidate confirmation count `c`, a breach is an observation with depth
greater than or equal to `c`. The calibrator computes the empirical breach rate
and adds the one-sided Dvoretzky-Kiefer-Wolfowitz margin:

```text
epsilon = sqrt(log(1 / (1 - confidence)) / (2 * sample_count))
upper(c) = min(1, empirical_breach_rate(c) + epsilon)
```

The bound is simultaneous over all candidate thresholds, so selecting the
smallest passing confirmation count does not silently spend the confidence
budget multiple times. If even a zero empirical breach rate cannot meet the
risk budget, the result is `insufficient_evidence_within_limit` and no count is
selected.

```python
from blockchain_risk import calibrate_confirmation_policy

report = calibrate_confirmation_policy(
    [0] * 980 + [1] * 17 + [2] * 3,
    observation_policy_id="hourly-max-depth-v1",
    risk_budget=0.05,
    confidence=0.95,
    max_confirmations=12,
)
assert report.selected_confirmations == 2
```

The report exposes every bounded candidate, the simultaneous margin, the
decision, and a deterministic SHA-256 commitment to the policy ID and depth
histogram. The digest detects accidental evidence changes; it does not
authenticate the producer.

## Production boundaries

- The statistical guarantee requires independent, identically distributed
  exposure windows. Overlapping windows, regime shifts, adversarial behavior,
  and source-selection bias invalidate that interpretation.
- The bound estimates the chance that a historical depth threshold is exceeded;
  it is not a consensus-security proof or a guarantee that a transaction is
  irreversible.
- Window length, chain identity, node diversity, source authenticity, and data
  completeness remain upstream governance responsibilities.
- Large confidence levels and small risk budgets require substantial clean
  history. Returning no policy is intentional rather than silently weakening
  the requested budget.

The next increment should bind signed, multi-node observation manifests and
regime labels to the evidence digest, then evaluate policy stability across
non-overlapping time periods.

# Finality tracker design

## Why this exists

Applications that credit deposits or settle transfers need an explicit finality
policy. A block can appear confirmed and later leave the canonical chain after
a heavier fork arrives. This module makes that state transition observable and
testable without binding the policy layer to a particular node provider.

## Invariants

- The first observation is the only permitted genesis block.
- Every later block references a known parent and increments its height by one.
- Weights are positive integers and cumulative along a branch.
- A competing branch must be **strictly** heavier to replace the current tip.
- Confirmation counts are available only for canonical blocks.

Keeping the current tip on equal cumulative weight prevents arrival-order
oscillation. Callers should normalize the consensus-specific fork-choice score
into the block's `weight`; the library deliberately does not claim to
implement Ethereum, Bitcoin, or any other protocol's full consensus rules.

## Example

```python
from blockchain_risk import Block, FinalityTracker

tracker = FinalityTracker()
tracker.add_block(Block("genesis", None, 0))
tracker.add_block(Block("a1", "genesis", 1))

assessment = tracker.assess("a1", required_confirmations=6)
print(assessment.confirmations_remaining)  # 5
```

## Limitations and next step

State is currently in-memory and intended for deterministic policy tests or
stream processors with external checkpointing. The next production increment
should add a versioned snapshot format plus replay tests so a consumer can
recover without silently changing canonical-chain decisions.

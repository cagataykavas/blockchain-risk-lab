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

# Persist and later restore the exact fork-choice state.
snapshot = tracker.snapshot_json()
restored = FinalityTracker.from_snapshot_json(snapshot)
assert restored.canonical_tip == tracker.canonical_tip
```

## Recovery guarantees

Snapshots use a versioned JSON schema and a SHA-256 integrity checksum. Block
observations remain in arrival order, which is consensus-relevant when two
branches have equal cumulative weight. Restoration replays every block through
the normal validation path, then verifies that the declared canonical tip
matches the replay result. Reorg history is reconstructed rather than trusted
as opaque serialized state.

The checksum detects accidental corruption and unexpected modification; it is
not an authenticity mechanism. Systems that accept snapshots across trust
boundaries should add a signature or authenticated transport.

## Limitations and next step

Snapshot persistence is caller-managed and the complete observed DAG remains
in memory. The next production increment should define bounded pruning rules
that retain enough ancestry to explain and safely reject deep reorganizations.


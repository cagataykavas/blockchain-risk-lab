"""Deterministic fork-choice and confirmation-risk tracking.

The implementation models a proof-of-work-like heaviest-chain rule without
network or node dependencies. Callers provide an already-normalized positive
weight for each block, so the same tracker can be used with work, stake, or a
domain-specific fork-choice score.
"""

from __future__ import annotations

from dataclasses import dataclass


class ChainValidationError(ValueError):
    """Raised when a block violates structural chain invariants."""


class BlockNotCanonicalError(LookupError):
    """Raised when confirmations are requested for a non-canonical block."""


@dataclass(frozen=True, slots=True)
class Block:
    """A normalized block observation."""

    block_hash: str
    parent_hash: str | None
    height: int
    weight: int = 1


@dataclass(frozen=True, slots=True)
class ReorgEvent:
    """A canonical-chain replacement detected while adding a block."""

    old_tip: str
    new_tip: str
    common_ancestor: str | None
    removed_blocks: tuple[str, ...]
    added_blocks: tuple[str, ...]

    @property
    def depth(self) -> int:
        """Number of previously canonical blocks that were displaced."""

        return len(self.removed_blocks)


@dataclass(frozen=True, slots=True)
class FinalityAssessment:
    """Confirmation state for a block on the current canonical chain."""

    block_hash: str
    confirmations: int
    required_confirmations: int

    @property
    def finalized(self) -> bool:
        return self.confirmations >= self.required_confirmations

    @property
    def confirmations_remaining(self) -> int:
        return max(0, self.required_confirmations - self.confirmations)


class FinalityTracker:
    """Track a block DAG and choose a canonical tip by cumulative weight.

    Tie-breaking is stable: the current canonical tip remains selected when a
    competing tip only matches its score. This avoids artificial reorgs caused
    solely by block arrival order. A strictly heavier branch is required.
    """

    def __init__(self) -> None:
        self._blocks: dict[str, Block] = {}
        self._cumulative_weight: dict[str, int] = {}
        self._tip: str | None = None
        self._reorgs: list[ReorgEvent] = []

    @property
    def canonical_tip(self) -> str | None:
        return self._tip

    @property
    def reorg_history(self) -> tuple[ReorgEvent, ...]:
        return tuple(self._reorgs)

    def add_block(self, block: Block) -> ReorgEvent | None:
        """Validate and add a block, returning a reorg event when one occurs."""

        self._validate(block)
        parent_weight = (
            0 if block.parent_hash is None else self._cumulative_weight[block.parent_hash]
        )
        self._blocks[block.block_hash] = block
        self._cumulative_weight[block.block_hash] = parent_weight + block.weight

        old_tip = self._tip
        if old_tip is None:
            self._tip = block.block_hash
            return None

        if self._cumulative_weight[block.block_hash] <= self._cumulative_weight[old_tip]:
            return None

        self._tip = block.block_hash
        if self._is_ancestor(old_tip, block.block_hash):
            return None

        event = self._build_reorg(old_tip, block.block_hash)
        self._reorgs.append(event)
        return event

    def confirmations(self, block_hash: str) -> int:
        """Return one-based confirmations for a canonical block."""

        if block_hash not in self._blocks:
            raise KeyError(f"unknown block: {block_hash}")
        if self._tip is None or not self._is_ancestor(block_hash, self._tip):
            raise BlockNotCanonicalError(f"block is not canonical: {block_hash}")
        return self._blocks[self._tip].height - self._blocks[block_hash].height + 1

    def assess(self, block_hash: str, required_confirmations: int) -> FinalityAssessment:
        """Evaluate whether a canonical block meets a confirmation policy."""

        if required_confirmations < 1:
            raise ValueError("required_confirmations must be at least 1")
        return FinalityAssessment(
            block_hash=block_hash,
            confirmations=self.confirmations(block_hash),
            required_confirmations=required_confirmations,
        )

    def canonical_chain(self) -> tuple[str, ...]:
        """Return canonical block hashes from genesis to tip."""

        if self._tip is None:
            return ()
        return tuple(reversed(self._path_to_root(self._tip)))

    def _validate(self, block: Block) -> None:
        if not block.block_hash:
            raise ChainValidationError("block_hash must not be empty")
        if block.block_hash in self._blocks:
            raise ChainValidationError(f"duplicate block: {block.block_hash}")
        if block.weight <= 0:
            raise ChainValidationError("weight must be positive")
        if block.parent_hash is None:
            if self._blocks:
                raise ChainValidationError("only the first block may be a genesis block")
            if block.height != 0:
                raise ChainValidationError("genesis height must be 0")
            return
        if block.parent_hash not in self._blocks:
            raise ChainValidationError(f"unknown parent: {block.parent_hash}")
        expected_height = self._blocks[block.parent_hash].height + 1
        if block.height != expected_height:
            raise ChainValidationError(
                f"height {block.height} does not follow parent height; expected {expected_height}"
            )

    def _is_ancestor(self, ancestor: str, descendant: str) -> bool:
        cursor: str | None = descendant
        while cursor is not None:
            if cursor == ancestor:
                return True
            cursor = self._blocks[cursor].parent_hash
        return False

    def _path_to_root(self, block_hash: str) -> list[str]:
        path: list[str] = []
        cursor: str | None = block_hash
        while cursor is not None:
            path.append(cursor)
            cursor = self._blocks[cursor].parent_hash
        return path

    def _build_reorg(self, old_tip: str, new_tip: str) -> ReorgEvent:
        old_path = self._path_to_root(old_tip)
        new_path = self._path_to_root(new_tip)
        new_ancestors = set(new_path)
        common_ancestor = next(
            (block_hash for block_hash in old_path if block_hash in new_ancestors),
            None,
        )
        old_cut = old_path.index(common_ancestor) if common_ancestor is not None else len(old_path)
        new_cut = new_path.index(common_ancestor) if common_ancestor is not None else len(new_path)
        return ReorgEvent(
            old_tip=old_tip,
            new_tip=new_tip,
            common_ancestor=common_ancestor,
            removed_blocks=tuple(old_path[:old_cut]),
            added_blocks=tuple(reversed(new_path[:new_cut])),
        )

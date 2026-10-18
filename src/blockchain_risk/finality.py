"""Deterministic fork-choice and confirmation-risk tracking.

The implementation models a proof-of-work-like heaviest-chain rule without
network or node dependencies. Callers provide an already-normalized positive
weight for each block, so the same tracker can be used with work, stake, or a
domain-specific fork-choice score.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


class ChainValidationError(ValueError):
    """Raised when a block violates structural chain invariants."""


class BlockNotCanonicalError(LookupError):
    """Raised when confirmations are requested for a non-canonical block."""


class SnapshotValidationError(ValueError):
    """Raised when persisted tracker state is malformed or corrupted."""


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

    SNAPSHOT_VERSION = 1
    AUTH_ENVELOPE_VERSION = 1
    MIN_AUTH_KEY_BYTES = 32
    _KEY_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")

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

    def snapshot_json(self) -> str:
        """Return a versioned, checksummed JSON snapshot of observed blocks.

        Blocks retain insertion order because equal-weight fork choice keeps the
        existing canonical tip. Restoring by replay therefore reproduces both
        the selected tip and the reorganization audit trail.
        """

        payload: dict[str, Any] = {
            "schema_version": self.SNAPSHOT_VERSION,
            "canonical_tip": self._tip,
            "blocks": [
                {
                    "block_hash": block.block_hash,
                    "parent_hash": block.parent_hash,
                    "height": block.height,
                    "weight": block.weight,
                }
                for block in self._blocks.values()
            ],
        }
        checksum = hashlib.sha256(self._canonical_json(payload).encode()).hexdigest()
        return self._canonical_json({**payload, "checksum": f"sha256:{checksum}"})

    def authenticated_snapshot_json(self, auth_key: bytes, key_id: str) -> str:
        """Return a snapshot wrapped in a key-rotatable HMAC envelope.

        The authentication key is caller-managed and is never serialized. The
        key identifier lets a restore path select the right key after rotation.
        """

        self._validate_auth_key(auth_key)
        self._validate_key_id(key_id)
        payload: dict[str, Any] = {
            "envelope_version": self.AUTH_ENVELOPE_VERSION,
            "key_id": key_id,
            "snapshot": json.loads(self.snapshot_json()),
        }
        digest = hmac.new(
            auth_key,
            self._canonical_json(payload).encode(),
            hashlib.sha256,
        ).hexdigest()
        return self._canonical_json({**payload, "mac": f"hmac-sha256:{digest}"})

    @classmethod
    def from_authenticated_snapshot_json(
        cls,
        envelope: str,
        verification_keys: Mapping[str, bytes],
    ) -> FinalityTracker:
        """Verify an authenticated envelope before replaying its snapshot.

        ``verification_keys`` may contain active and retired keys, allowing
        persisted snapshots to survive key rotation without embedding secrets.
        Unknown key identifiers and MAC failures are rejected before block
        replay so unauthenticated state never reaches chain validation.
        """

        try:
            document = json.loads(envelope)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SnapshotValidationError("authenticated snapshot must be valid JSON") from exc
        if not isinstance(document, dict):
            raise SnapshotValidationError("authenticated snapshot root must be an object")

        required_keys = {"envelope_version", "key_id", "snapshot", "mac"}
        if set(document) != required_keys:
            raise SnapshotValidationError("authenticated snapshot fields do not match schema")
        if document["envelope_version"] != cls.AUTH_ENVELOPE_VERSION:
            raise SnapshotValidationError(
                f"unsupported authentication envelope version: {document['envelope_version']!r}"
            )

        key_id = document["key_id"]
        if not isinstance(key_id, str) or cls._KEY_ID_PATTERN.fullmatch(key_id) is None:
            raise SnapshotValidationError("authenticated snapshot key_id is invalid")
        if key_id not in verification_keys:
            raise SnapshotValidationError("unknown snapshot authentication key")
        auth_key = verification_keys[key_id]
        cls._validate_auth_key(auth_key)

        snapshot = document["snapshot"]
        if not isinstance(snapshot, dict):
            raise SnapshotValidationError("authenticated snapshot payload must be an object")
        supplied_mac = document["mac"]
        if not isinstance(supplied_mac, str):
            raise SnapshotValidationError("authenticated snapshot MAC must be a string")
        payload = {key: value for key, value in document.items() if key != "mac"}
        expected_digest = hmac.new(
            auth_key,
            cls._canonical_json(payload).encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied_mac, f"hmac-sha256:{expected_digest}"):
            raise SnapshotValidationError("authenticated snapshot MAC mismatch")

        return cls.from_snapshot_json(cls._canonical_json(snapshot))

    @classmethod
    def from_snapshot_json(cls, snapshot: str) -> FinalityTracker:
        """Restore state by validating and replaying a snapshot.

        Replaying through :meth:`add_block` reuses all structural invariants;
        snapshots never bypass normal block validation.
        """

        try:
            document = json.loads(snapshot)
        except (json.JSONDecodeError, TypeError) as exc:
            raise SnapshotValidationError("snapshot must be valid JSON text") from exc
        if not isinstance(document, dict):
            raise SnapshotValidationError("snapshot root must be an object")

        required_keys = {"schema_version", "canonical_tip", "blocks", "checksum"}
        if set(document) != required_keys:
            raise SnapshotValidationError("snapshot fields do not match schema")
        if document["schema_version"] != cls.SNAPSHOT_VERSION:
            raise SnapshotValidationError(
                f"unsupported snapshot schema version: {document['schema_version']!r}"
            )
        blocks = document["blocks"]
        if not isinstance(blocks, list):
            raise SnapshotValidationError("snapshot blocks must be a list")
        canonical_tip = document["canonical_tip"]
        if canonical_tip is not None and not isinstance(canonical_tip, str):
            raise SnapshotValidationError("canonical_tip must be a string or null")

        supplied_checksum = document["checksum"]
        if not isinstance(supplied_checksum, str):
            raise SnapshotValidationError("snapshot checksum must be a string")
        payload = {key: value for key, value in document.items() if key != "checksum"}
        expected_digest = hashlib.sha256(cls._canonical_json(payload).encode()).hexdigest()
        if not hmac.compare_digest(supplied_checksum, f"sha256:{expected_digest}"):
            raise SnapshotValidationError("snapshot checksum mismatch")

        tracker = cls()
        block_keys = {"block_hash", "parent_hash", "height", "weight"}
        try:
            for raw_block in blocks:
                if not isinstance(raw_block, dict) or set(raw_block) != block_keys:
                    raise SnapshotValidationError("snapshot block fields do not match schema")
                block_hash = raw_block["block_hash"]
                parent_hash = raw_block["parent_hash"]
                height = raw_block["height"]
                weight = raw_block["weight"]
                if not isinstance(block_hash, str):
                    raise SnapshotValidationError("block_hash must be a string")
                if parent_hash is not None and not isinstance(parent_hash, str):
                    raise SnapshotValidationError("parent_hash must be a string or null")
                if isinstance(height, bool) or not isinstance(height, int):
                    raise SnapshotValidationError("height must be an integer")
                if isinstance(weight, bool) or not isinstance(weight, int):
                    raise SnapshotValidationError("weight must be an integer")
                tracker.add_block(Block(block_hash, parent_hash, height, weight))
        except ChainValidationError as exc:
            raise SnapshotValidationError(f"invalid block replay: {exc}") from exc

        if tracker.canonical_tip != canonical_tip:
            raise SnapshotValidationError("canonical_tip does not match replayed blocks")
        return tracker

    @staticmethod
    def _canonical_json(value: object) -> str:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)

    @classmethod
    def _validate_auth_key(cls, auth_key: bytes) -> None:
        if not isinstance(auth_key, bytes):
            raise TypeError("snapshot authentication key must be bytes")
        if len(auth_key) < cls.MIN_AUTH_KEY_BYTES:
            raise ValueError(
                f"snapshot authentication key must contain at least {cls.MIN_AUTH_KEY_BYTES} bytes"
            )

    @classmethod
    def _validate_key_id(cls, key_id: str) -> None:
        if not isinstance(key_id, str):
            raise TypeError("snapshot key_id must be a string")
        if cls._KEY_ID_PATTERN.fullmatch(key_id) is None:
            raise ValueError(
                "snapshot key_id must be 1-128 ASCII letters, digits, dots, underscores, or hyphens"
            )

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

import hashlib
import json
import unittest

from blockchain_risk import (
    Block,
    BlockNotCanonicalError,
    ChainValidationError,
    FinalityTracker,
    SnapshotValidationError,
)


class FinalityTrackerTests(unittest.TestCase):
    AUTH_KEY = b"a" * 32

    def setUp(self) -> None:
        self.tracker = FinalityTracker()
        self.tracker.add_block(Block("g", None, 0))
        self.tracker.add_block(Block("a1", "g", 1))
        self.tracker.add_block(Block("a2", "a1", 2))

    def test_linear_chain_confirmations_are_one_based(self) -> None:
        self.assertEqual(self.tracker.confirmations("a2"), 1)
        self.assertEqual(self.tracker.confirmations("g"), 3)

    def test_assessment_reports_remaining_confirmations(self) -> None:
        assessment = self.tracker.assess("a1", required_confirmations=4)
        self.assertFalse(assessment.finalized)
        self.assertEqual(assessment.confirmations_remaining, 2)

    def test_strictly_heavier_fork_emits_reorg_details(self) -> None:
        self.tracker.add_block(Block("b1", "g", 1, weight=1))
        event = self.tracker.add_block(Block("b2", "b1", 2, weight=2))

        self.assertIsNotNone(event)
        assert event is not None
        self.assertEqual(event.common_ancestor, "g")
        self.assertEqual(event.removed_blocks, ("a2", "a1"))
        self.assertEqual(event.added_blocks, ("b1", "b2"))
        self.assertEqual(event.depth, 2)
        self.assertEqual(self.tracker.canonical_chain(), ("g", "b1", "b2"))

    def test_equal_weight_fork_does_not_reorganize(self) -> None:
        self.tracker.add_block(Block("b1", "g", 1))
        event = self.tracker.add_block(Block("b2", "b1", 2))

        self.assertIsNone(event)
        self.assertEqual(self.tracker.canonical_tip, "a2")

    def test_old_branch_block_is_not_canonical_after_reorg(self) -> None:
        self.tracker.add_block(Block("b1", "g", 1, weight=3))

        with self.assertRaises(BlockNotCanonicalError):
            self.tracker.confirmations("a1")

    def test_unknown_parent_is_rejected_without_mutating_tip(self) -> None:
        with self.assertRaises(ChainValidationError):
            self.tracker.add_block(Block("bad", "missing", 3))
        self.assertEqual(self.tracker.canonical_tip, "a2")

    def test_invalid_height_and_weight_are_rejected(self) -> None:
        with self.assertRaises(ChainValidationError):
            self.tracker.add_block(Block("bad-height", "a2", 9))
        with self.assertRaises(ChainValidationError):
            self.tracker.add_block(Block("bad-weight", "a2", 3, weight=0))

    def test_duplicate_block_is_rejected(self) -> None:
        with self.assertRaises(ChainValidationError):
            self.tracker.add_block(Block("a2", "a1", 2))

    def test_confirmation_policy_must_be_positive(self) -> None:
        with self.assertRaises(ValueError):
            self.tracker.assess("a2", required_confirmations=0)

    def test_snapshot_round_trip_preserves_forks_and_reorg_history(self) -> None:
        self.tracker.add_block(Block("b1", "g", 1, weight=1))
        self.tracker.add_block(Block("b2", "b1", 2, weight=2))

        restored = FinalityTracker.from_snapshot_json(self.tracker.snapshot_json())

        self.assertEqual(restored.canonical_tip, self.tracker.canonical_tip)
        self.assertEqual(restored.canonical_chain(), self.tracker.canonical_chain())
        self.assertEqual(restored.reorg_history, self.tracker.reorg_history)
        self.assertEqual(restored.confirmations("g"), self.tracker.confirmations("g"))

    def test_restored_tracker_makes_same_future_fork_choice(self) -> None:
        restored = FinalityTracker.from_snapshot_json(self.tracker.snapshot_json())

        live_event = self.tracker.add_block(Block("b1", "g", 1, weight=3))
        restored_event = restored.add_block(Block("b1", "g", 1, weight=3))

        self.assertEqual(restored_event, live_event)
        self.assertEqual(restored.canonical_chain(), self.tracker.canonical_chain())

    def test_snapshot_checksum_detects_tampering(self) -> None:
        document = json.loads(self.tracker.snapshot_json())
        document["blocks"][1]["weight"] = 99

        with self.assertRaisesRegex(SnapshotValidationError, "checksum mismatch"):
            FinalityTracker.from_snapshot_json(json.dumps(document))

    def test_snapshot_replay_rejects_semantically_invalid_state(self) -> None:
        document = json.loads(self.tracker.snapshot_json())
        document["blocks"][1]["parent_hash"] = "missing"
        payload = {key: value for key, value in document.items() if key != "checksum"}
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        document["checksum"] = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"

        with self.assertRaisesRegex(SnapshotValidationError, "invalid block replay"):
            FinalityTracker.from_snapshot_json(json.dumps(document))

    def test_snapshot_version_must_be_supported(self) -> None:
        document = json.loads(self.tracker.snapshot_json())
        document["schema_version"] = 2

        with self.assertRaisesRegex(SnapshotValidationError, "unsupported snapshot"):
            FinalityTracker.from_snapshot_json(json.dumps(document))

    def test_authenticated_snapshot_round_trip_supports_key_rotation(self) -> None:
        self.tracker.add_block(Block("b1", "g", 1, weight=3))
        envelope = self.tracker.authenticated_snapshot_json(
            self.AUTH_KEY,
            key_id="snapshot-key-2026-10",
        )

        restored = FinalityTracker.from_authenticated_snapshot_json(
            envelope,
            {
                "snapshot-key-2026-09": b"retired-key" * 3,
                "snapshot-key-2026-10": self.AUTH_KEY,
            },
        )

        self.assertEqual(restored.canonical_chain(), self.tracker.canonical_chain())
        self.assertEqual(restored.reorg_history, self.tracker.reorg_history)

    def test_authenticated_snapshot_rejects_tampering_before_replay(self) -> None:
        document = json.loads(
            self.tracker.authenticated_snapshot_json(self.AUTH_KEY, key_id="active-key")
        )
        document["snapshot"]["blocks"][1]["parent_hash"] = "missing"

        with self.assertRaisesRegex(SnapshotValidationError, "MAC mismatch"):
            FinalityTracker.from_authenticated_snapshot_json(
                json.dumps(document),
                {"active-key": self.AUTH_KEY},
            )

    def test_authenticated_snapshot_rejects_wrong_or_unknown_key(self) -> None:
        envelope = self.tracker.authenticated_snapshot_json(self.AUTH_KEY, key_id="active-key")

        with self.assertRaisesRegex(SnapshotValidationError, "unknown snapshot"):
            FinalityTracker.from_authenticated_snapshot_json(
                envelope,
                {"retired-key": self.AUTH_KEY},
            )
        with self.assertRaisesRegex(SnapshotValidationError, "MAC mismatch"):
            FinalityTracker.from_authenticated_snapshot_json(
                envelope,
                {"active-key": b"b" * 32},
            )

    def test_authenticated_snapshot_rejects_weak_keys_and_invalid_key_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "at least 32 bytes"):
            self.tracker.authenticated_snapshot_json(b"too-short", key_id="active-key")
        with self.assertRaisesRegex(ValueError, "key_id"):
            self.tracker.authenticated_snapshot_json(self.AUTH_KEY, key_id="contains spaces")

    def test_authenticated_snapshot_is_deterministic(self) -> None:
        first = self.tracker.authenticated_snapshot_json(self.AUTH_KEY, key_id="active-key")
        second = self.tracker.authenticated_snapshot_json(self.AUTH_KEY, key_id="active-key")

        self.assertEqual(first, second)


if __name__ == "__main__":
    unittest.main()

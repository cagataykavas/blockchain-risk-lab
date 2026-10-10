import unittest

from blockchain_risk import (
    Block,
    BlockNotCanonicalError,
    ChainValidationError,
    FinalityTracker,
)


class FinalityTrackerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()

import json
import math
import unittest

from blockchain_risk import (
    ConfirmationPolicyValidationError,
    calibrate_confirmation_policy,
)


class ConfirmationPolicyTests(unittest.TestCase):
    def test_selects_smallest_confirmation_count_meeting_budget(self) -> None:
        depths = [0] * 980 + [1] * 17 + [2] * 3

        report = calibrate_confirmation_policy(
            depths,
            observation_policy_id="hourly-max-depth-v1",
            risk_budget=0.05,
            confidence=0.95,
            max_confirmations=5,
        )

        self.assertEqual(report.decision, "risk_budget_met")
        self.assertEqual(report.selected_confirmations, 2)
        self.assertGreater(report.candidates[0].upper_breach_probability, 0.05)
        self.assertLessEqual(report.candidates[1].upper_breach_probability, 0.05)
        self.assertEqual(report.candidates[1].breach_count, 3)

    def test_margin_matches_one_sided_dkw_bound(self) -> None:
        report = calibrate_confirmation_policy(
            [0] * 1_000,
            observation_policy_id="daily-mainnet-v2",
            risk_budget=0.05,
            confidence=0.99,
        )

        expected = math.sqrt(math.log(100.0) / 2_000.0)
        self.assertAlmostEqual(report.simultaneous_margin, expected)
        self.assertAlmostEqual(
            report.candidates[0].upper_breach_probability,
            expected,
        )

    def test_rejects_policy_when_sample_cannot_support_budget(self) -> None:
        report = calibrate_confirmation_policy(
            [0] * 100,
            observation_policy_id="daily-mainnet-v2",
            risk_budget=0.01,
            confidence=0.95,
            max_confirmations=8,
        )

        self.assertEqual(report.decision, "insufficient_evidence_within_limit")
        self.assertIsNone(report.selected_confirmations)
        self.assertTrue(
            all(candidate.upper_breach_probability > 0.01 for candidate in report.candidates)
        )

    def test_tail_evidence_is_monotone_across_confirmations(self) -> None:
        report = calibrate_confirmation_policy(
            [0, 0, 1, 1, 2, 5],
            observation_policy_id="six-window-fixture",
            risk_budget=0.9,
            max_confirmations=7,
        )

        breach_counts = [candidate.breach_count for candidate in report.candidates]
        upper_bounds = [candidate.upper_breach_probability for candidate in report.candidates]
        self.assertEqual(breach_counts, sorted(breach_counts, reverse=True))
        self.assertEqual(upper_bounds, sorted(upper_bounds, reverse=True))

    def test_digest_is_permutation_invariant_and_input_sensitive(self) -> None:
        settings = {
            "observation_policy_id": "hourly-max-depth-v1",
            "risk_budget": 0.9,
        }
        first = calibrate_confirmation_policy([0, 1, 2, 0], **settings)
        permuted = calibrate_confirmation_policy([2, 0, 0, 1], **settings)
        changed = calibrate_confirmation_policy([2, 0, 1, 1], **settings)

        self.assertEqual(first.observation_digest, permuted.observation_digest)
        self.assertNotEqual(first.observation_digest, changed.observation_digest)
        self.assertTrue(first.observation_digest.startswith("sha256:"))

    def test_report_is_json_serializable_and_bounded(self) -> None:
        report = calibrate_confirmation_policy(
            [0, 1, 0],
            observation_policy_id="fixture-v1",
            risk_budget=0.9,
            max_confirmations=3,
        )

        payload = report.to_dict()
        json.dumps(payload, allow_nan=False)
        self.assertEqual(len(payload["candidates"]), 3)
        self.assertNotIn("max_reorg_depths", payload)

    def test_invalid_observations_fail_closed(self) -> None:
        invalid_samples = (None, [], [True], [1.5], [-1], [10_001])
        for depths in invalid_samples:
            with self.subTest(depths=depths):
                with self.assertRaises(ConfirmationPolicyValidationError):
                    calibrate_confirmation_policy(
                        depths,
                        observation_policy_id="fixture-v1",
                        risk_budget=0.1,
                    )

    def test_resource_limits_fail_closed(self) -> None:
        with self.assertRaisesRegex(ConfirmationPolicyValidationError, "observation count"):
            calibrate_confirmation_policy(
                (0 for _ in range(100_001)),
                observation_policy_id="fixture-v1",
                risk_budget=0.1,
            )
        with self.assertRaisesRegex(ConfirmationPolicyValidationError, "max_confirmations"):
            calibrate_confirmation_policy(
                [0],
                observation_policy_id="fixture-v1",
                risk_budget=0.1,
                max_confirmations=1_001,
            )

    def test_invalid_policy_settings_fail_closed(self) -> None:
        invalid_settings = (
            {"observation_policy_id": "", "risk_budget": 0.1},
            {"observation_policy_id": "bad id", "risk_budget": 0.1},
            {"observation_policy_id": "fixture-v1", "risk_budget": 0.0},
            {"observation_policy_id": "fixture-v1", "risk_budget": float("nan")},
            {"observation_policy_id": "fixture-v1", "risk_budget": 10**10_000},
            {
                "observation_policy_id": "fixture-v1",
                "risk_budget": 0.1,
                "confidence": 1.0,
            },
            {
                "observation_policy_id": "fixture-v1",
                "risk_budget": 0.1,
                "max_confirmations": True,
            },
        )
        for settings in invalid_settings:
            with self.subTest(settings=settings):
                with self.assertRaises(ConfirmationPolicyValidationError):
                    calibrate_confirmation_policy([0], **settings)


if __name__ == "__main__":
    unittest.main()

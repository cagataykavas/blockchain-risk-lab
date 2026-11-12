"""Finite-sample calibration of blockchain confirmation policies.

The input is one maximum observed reorganization depth per independent exposure
window, including zero-depth windows.  A one-sided Dvoretzky-Kiefer-Wolfowitz
bound is uniform across every candidate confirmation count, so selecting the
smallest passing count does not require a separate multiple-testing correction.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections import Counter
from collections.abc import Iterable
from dataclasses import asdict, dataclass
from itertools import islice

MAX_OBSERVATIONS = 100_000
MAX_REORG_DEPTH = 10_000
MAX_CONFIRMATIONS = 1_000
SCHEMA_VERSION = 1
_POLICY_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")


class ConfirmationPolicyValidationError(ValueError):
    """Raised when calibration evidence or policy settings are malformed."""


@dataclass(frozen=True, slots=True)
class ConfirmationCandidate:
    """Tail-risk evidence for one candidate confirmation count."""

    confirmations: int
    breach_count: int
    empirical_breach_probability: float
    upper_breach_probability: float


@dataclass(frozen=True, slots=True)
class ConfirmationPolicyReport:
    """A bounded, JSON-ready confirmation policy calibration result."""

    schema_version: int
    observation_policy_id: str
    decision: str
    selected_confirmations: int | None
    risk_budget: float
    confidence: float
    sample_count: int
    max_observed_depth: int
    simultaneous_margin: float
    observation_digest: str
    candidates: tuple[ConfirmationCandidate, ...]

    def to_dict(self) -> dict[str, object]:
        """Return a deterministic, JSON-serializable representation."""

        return {
            "schema_version": self.schema_version,
            "observation_policy_id": self.observation_policy_id,
            "decision": self.decision,
            "selected_confirmations": self.selected_confirmations,
            "risk_budget": self.risk_budget,
            "confidence": self.confidence,
            "sample_count": self.sample_count,
            "max_observed_depth": self.max_observed_depth,
            "simultaneous_margin": self.simultaneous_margin,
            "observation_digest": self.observation_digest,
            "candidates": [asdict(candidate) for candidate in self.candidates],
        }


def calibrate_confirmation_policy(
    max_reorg_depths: Iterable[int],
    *,
    observation_policy_id: str,
    risk_budget: float,
    confidence: float = 0.95,
    max_confirmations: int = 64,
) -> ConfirmationPolicyReport:
    """Select the smallest confirmation count meeting a tail-risk budget.

    A breach at candidate ``c`` is an observation with maximum reorganization
    depth greater than or equal to ``c``.  The reported upper probabilities add
    a one-sided DKW margin to each empirical tail probability.  That margin is
    simultaneous over all thresholds represented by the empirical CDF.

    No policy is selected when the evidence cannot support ``risk_budget`` at
    ``confidence`` within ``max_confirmations``.
    """

    _validate_policy_settings(
        observation_policy_id=observation_policy_id,
        risk_budget=risk_budget,
        confidence=confidence,
        max_confirmations=max_confirmations,
    )
    try:
        depths = tuple(islice(max_reorg_depths, MAX_OBSERVATIONS + 1))
    except TypeError as exc:
        raise ConfirmationPolicyValidationError(
            "max_reorg_depths must be an iterable of integers"
        ) from exc
    if not depths:
        raise ConfirmationPolicyValidationError("at least one observation is required")
    if len(depths) > MAX_OBSERVATIONS:
        raise ConfirmationPolicyValidationError(
            f"observation count exceeds limit of {MAX_OBSERVATIONS}"
        )
    for depth in depths:
        if isinstance(depth, bool) or not isinstance(depth, int):
            raise ConfirmationPolicyValidationError("reorganization depths must be integers")
        if not 0 <= depth <= MAX_REORG_DEPTH:
            raise ConfirmationPolicyValidationError(
                f"reorganization depths must be in [0, {MAX_REORG_DEPTH}]"
            )

    sample_count = len(depths)
    alpha = 1.0 - confidence
    margin = math.sqrt(math.log(1.0 / alpha) / (2.0 * sample_count))
    histogram = Counter(depths)

    # Start with all strictly positive depths, then remove observations as the
    # candidate threshold advances. This keeps calibration O(n + C).
    breach_count = sample_count - histogram.get(0, 0)
    candidates: list[ConfirmationCandidate] = []
    selected_confirmations: int | None = None
    for confirmations in range(1, max_confirmations + 1):
        if confirmations > 1:
            breach_count -= histogram.get(confirmations - 1, 0)
        empirical_probability = breach_count / sample_count
        upper_probability = min(1.0, empirical_probability + margin)
        candidates.append(
            ConfirmationCandidate(
                confirmations=confirmations,
                breach_count=breach_count,
                empirical_breach_probability=empirical_probability,
                upper_breach_probability=upper_probability,
            )
        )
        if selected_confirmations is None and upper_probability <= risk_budget:
            selected_confirmations = confirmations

    digest_payload = {
        "schema_version": SCHEMA_VERSION,
        "observation_policy_id": observation_policy_id,
        "sample_count": sample_count,
        "depth_histogram": [[depth, histogram[depth]] for depth in sorted(histogram)],
    }
    canonical = json.dumps(
        digest_payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    observation_digest = f"sha256:{hashlib.sha256(canonical.encode()).hexdigest()}"
    return ConfirmationPolicyReport(
        schema_version=SCHEMA_VERSION,
        observation_policy_id=observation_policy_id,
        decision=(
            "risk_budget_met"
            if selected_confirmations is not None
            else "insufficient_evidence_within_limit"
        ),
        selected_confirmations=selected_confirmations,
        risk_budget=risk_budget,
        confidence=confidence,
        sample_count=sample_count,
        max_observed_depth=max(depths),
        simultaneous_margin=margin,
        observation_digest=observation_digest,
        candidates=tuple(candidates),
    )


def _validate_policy_settings(
    *,
    observation_policy_id: str,
    risk_budget: float,
    confidence: float,
    max_confirmations: int,
) -> None:
    if not isinstance(observation_policy_id, str) or not _POLICY_ID.fullmatch(
        observation_policy_id
    ):
        raise ConfirmationPolicyValidationError("observation_policy_id is invalid")
    if isinstance(risk_budget, bool) or not isinstance(risk_budget, (int, float)):
        raise ConfirmationPolicyValidationError("risk_budget must be numeric")
    if not 0.0 < risk_budget < 1.0:
        raise ConfirmationPolicyValidationError("risk_budget must be finite and in (0, 1)")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise ConfirmationPolicyValidationError("confidence must be numeric")
    if not 0.5 < confidence < 1.0:
        raise ConfirmationPolicyValidationError("confidence must be finite and in (0.5, 1)")
    if isinstance(max_confirmations, bool) or not isinstance(max_confirmations, int):
        raise ConfirmationPolicyValidationError("max_confirmations must be an integer")
    if not 1 <= max_confirmations <= MAX_CONFIRMATIONS:
        raise ConfirmationPolicyValidationError(
            f"max_confirmations must be in [1, {MAX_CONFIRMATIONS}]"
        )

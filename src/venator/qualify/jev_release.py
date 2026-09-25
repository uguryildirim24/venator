"""Packaged Jev release. Immutable data, not a file the runtime finds under docs/."""

from __future__ import annotations

from dataclasses import dataclass

from venator.qualify.jev_contract import VALIDATOR_VERSION
from venator.qualify.jev_questions import (
    QUESTION_PACKAGE,
    domains_sha256,
    questions_sha256,
)
from venator.qualify.versions import jev_qualifier_identity, jev_qualifier_version, jev_release_hash

STATE_RENDERER_VERSION = "1"
REDUCER_VERSION = "2"

# Semantic hashes frozen with the development release. They are deliberately
# explicit: an unrelated source cleanup must not silently mutate JEV_RELEASE.
_CANONICALIZER_HASH = "293e18d40764bf0ffff802969f14d52ad962fd1b37b2fa2cbfe1edc8c00f8ac0"
_COMPILER_HASH = "fc181bd98f782a7da92eacd2d8561d03343ab2c808022ca89f16013369e67ce7"
_EVIDENCE_HASH = "2cb402ac2fd839ed4de672187bd25422f3f64f45a6eb2cbc926248947de0f4d8"
_DENY_HASH = "baf13a6541e1ab45cc7d94ad24f28810d81f4facfba73e137ae325858fb94db8"

# Public distribution contains no protected cohort or validated release evidence.
PROTECTED_GROUP_HASHES: tuple[str, ...] = ()


@dataclass(frozen=True)
class JevRelease:
    model_requested: str
    model_expected: str
    question_package: str
    questions_sha256: str
    domains_sha256: str
    theta: float
    restriction_threshold: float
    student_yes_threshold: float
    student_no_threshold: float
    confirmed_gap_threshold: float
    years_choice_threshold: float
    domain_review_threshold: float
    score_confidence_threshold: float
    temporary_exclusion_enabled: bool
    canonicalizer_hash: str
    compiler_hash: str
    evidence_hash: str
    projection_hash: str
    deny_hash: str
    state_renderer_version: str
    validator_version: str
    reducer_version: str
    protected_group_hashes: tuple[str, ...]
    provider: str
    api_contract: str
    qualifier_kind: str

    @property
    def release_hash(self) -> str:
        return jev_release_hash(self)

    @property
    def qualifier_identity(self) -> dict[str, str]:
        return jev_qualifier_identity(
            model_requested=self.model_requested,
            model_expected=self.model_expected,
            question_package=self.question_package,
            questions_sha256=self.questions_sha256,
            release_hash=self.release_hash,
        )

    @property
    def qualifier_version(self) -> str:
        return jev_qualifier_version(self.qualifier_identity)


JEV_RELEASE = JevRelease(
    model_requested="jev-1.13.0",
    model_expected="jev-1.13.0",
    question_package=QUESTION_PACKAGE,
    questions_sha256=questions_sha256(),
    domains_sha256=domains_sha256(),
    theta=0.6,
    restriction_threshold=0.8,
    student_yes_threshold=0.8,
    student_no_threshold=0.3,
    confirmed_gap_threshold=0.85,
    years_choice_threshold=0.6,
    domain_review_threshold=0.5,
    score_confidence_threshold=0.6,
    temporary_exclusion_enabled=False,
    canonicalizer_hash=_CANONICALIZER_HASH,
    compiler_hash=_COMPILER_HASH,
    evidence_hash=_EVIDENCE_HASH,
    projection_hash=_COMPILER_HASH,
    deny_hash=_DENY_HASH,
    state_renderer_version=STATE_RENDERER_VERSION,
    validator_version=VALIDATOR_VERSION,
    reducer_version=REDUCER_VERSION,
    protected_group_hashes=PROTECTED_GROUP_HASHES,
    provider="typesafe",
    api_contract="systemone-v1",
    qualifier_kind="typesafe_jev",
)

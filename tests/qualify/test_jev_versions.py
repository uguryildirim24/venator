"""Jev hashes: legacy identities stay truncated; Jev identities are full SHA-256."""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from venator.profile.loader import load_profile
from venator.qualify.compile import compile_profile
from venator.qualify.jev_questions import (
    DOMAIN_DESCRIPTIONS,
    QUESTION_PACKAGE,
    REQUIREMENT_KIND_OPTIONS,
    questions_sha256,
    request_questions,
)
from venator.qualify.jev_release import JEV_RELEASE, PROTECTED_GROUP_HASHES
from venator.qualify.versions import (
    canon, contract_version, h, input_version, jev_policy_hash,
    jev_release_hash, qualifier_version, sha256_json,
)

ROOT = Path(__file__).resolve().parents[2]
LEGACY = json.loads((Path(__file__).parent / "fixtures/identities/qualification.json").read_text())
GOLDEN = json.loads((Path(__file__).parent / "fixtures/jev/release.json").read_text())
SOURCE = ROOT / "src/venator/qualify"


def test_legacy_identifiers_are_unchanged() -> None:
    compiled = compile_profile(
        load_profile(ROOT / "tests/qualify/fixtures/profiles/two-degrees"),
        as_of_month=LEGACY["inputs"]["as_of_month"],
    )
    inputs = LEGACY["inputs"]
    assert compiled.profile_hash == LEGACY["identities"]["profile_hash"]
    assert contract_version() == LEGACY["identities"]["contract_version"]
    assert input_version(inputs["posting_key"], inputs["posting_revision"],
                         compiled.profile_hash, inputs["as_of_month"]) == LEGACY["identities"]["input_version"]
    assert qualifier_version(**inputs["qualifier"]) == LEGACY["identities"]["qualifier_version"]
    assert len(h("x")) == 12
    assert canon({"é": 1, "a": [2]}) == '{"a":[2],"é":1}'


def test_jev_hashes_are_full_sha256_and_match_the_golden_release() -> None:
    assert len(sha256_json({"a": 1})) == 64
    assert JEV_RELEASE.theta == 0.6
    assert JEV_RELEASE.validator_version == GOLDEN["validator_version"] == "4"
    assert JEV_RELEASE.temporary_exclusion_enabled is False
    assert JEV_RELEASE.question_package == QUESTION_PACKAGE
    assert JEV_RELEASE.questions_sha256 == questions_sha256() == GOLDEN["questions_sha256"]
    assert JEV_RELEASE.domains_sha256 == GOLDEN["domains_sha256"]
    assert JEV_RELEASE.release_hash == GOLDEN["release_hash"] == jev_release_hash(JEV_RELEASE)
    assert JEV_RELEASE.qualifier_version == GOLDEN["qualifier_version"]
    assert JEV_RELEASE.qualifier_version.startswith("jev:")
    assert len(JEV_RELEASE.qualifier_version) == 4 + 64
    assert JEV_RELEASE.protected_group_hashes == tuple(GOLDEN["protected_group_hashes"])
    semantic_hashes = {
        JEV_RELEASE.canonicalizer_hash,
        JEV_RELEASE.compiler_hash,
        JEV_RELEASE.evidence_hash,
        JEV_RELEASE.projection_hash,
        JEV_RELEASE.deny_hash,
    }
    assert all(len(value) == 64 for value in semantic_hashes)
    assert JEV_RELEASE.compiler_hash == JEV_RELEASE.projection_hash


def test_requirement_questions_are_independent_and_closed() -> None:
    questions = request_questions()
    assert "hard_requirement_unmet" not in questions
    assert questions["confirmed_requirement_gap"]["type"] == "noul"
    requirement_kind = questions["mandatory_requirement_kind"]
    assert requirement_kind["type"] == "choice"
    assert tuple(requirement_kind["criteria"]) == REQUIREMENT_KIND_OPTIONS
    would_apply = str(questions["would_apply"]["instructions"])
    assert "Unresolved mandatory gaps count against early priority" not in would_apply



def test_nonfinite_values_cannot_be_hashed() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        sha256_json({"x": float("nan")})
    with pytest.raises(ValueError, match="non-finite"):
        sha256_json({"x": float("inf")})


def test_policy_label_moves_policy_hash_but_not_domain_text() -> None:
    from dataclasses import replace
    base = load_profile(Path(__file__).parent / "fixtures/jev/profile").filters.jev
    assert base is not None
    relabeled = replace(base, policy_version="owner-triage-2-relabel")
    assert jev_policy_hash(base) != jev_policy_hash(relabeled)
    assert DOMAIN_DESCRIPTIONS["computational_life_sciences"].startswith("Computation directly")


def test_no_circular_imports_in_jev_modules() -> None:
    qualify = SOURCE
    contract = (qualify / "jev_contract.py").read_text(encoding="utf-8")
    versions = (qualify / "versions.py").read_text(encoding="utf-8")
    release = (qualify / "jev_release.py").read_text(encoding="utf-8")
    assert "qualify.compile" not in contract
    assert "qualify.engine" not in contract
    assert "import venator.qualify.jev_release" not in versions
    assert "from venator.qualify.jev_release" not in versions
    assert "jev_policy" not in ast.dump(ast.parse(release))
    import venator.qualify.jev_contract  # noqa: F401
    import venator.qualify.jev_policy  # noqa: F401
    import venator.qualify.jev_reduce  # noqa: F401
    import venator.qualify.jev_release  # noqa: F401
    import venator.qualify.jev_questions  # noqa: F401
    import venator.qualify.versions  # noqa: F401

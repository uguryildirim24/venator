"""Per-Posting Jev context on both Hard Filter budget arms."""

from __future__ import annotations

import pickle
from dataclasses import replace
from pathlib import Path

import pytest

from venator.match.budget import alarm_can_be_armed, hard_filter_budget
from venator.match.filters import apply_filters
from venator.profile.loader import load_profile
from venator.profile.schema import JevPolicy
from venator.qualify.jev_contract import JevContext, JevExclusion

JEV_POLICY = JevPolicy(
    policy_version="v",
    restricted_roles="exclude",
    temporary_student_authorization_exclusion="review",
    no_sponsorship_student="retain",
    no_sponsorship_nonstudent="review",
    unmet_completed_degree="exclude",
    domains=("biochemistry",),
)

PHD = {
    "key": "p:education",
    "title": "Research Associate",
    "location": "Cambridge, MA",
    "description_kind": "full",
    "description_html": "<p>Requires a PhD in biology.</p>",
}


def make_context(**overrides: object) -> JevContext:
    values: dict[str, object] = {
        "posting_key": "p:education",
        "posting_revision": "rev",
        "as_of_month": "2026-09",
        "input_version": "input-v",
        "qualifier_version": "jev:abc",
        "accepted_profile_hash": "accepted",
        "assessment_key": "assess-priority",
        "response_sha256": "a" * 64,
        "decision": "prioritize",
        "fit_probability": 0.9,
        "fit_score": 0.85,
        "exclusions": (),
        "primary_rule": None,
        "review_flags": (),
        "diagnostic_flags": (),
    }
    values.update(overrides)
    return JevContext(**values)  # type: ignore[arg-type]


@pytest.mark.parametrize("arm", ["alarm", "worker"])
def test_both_arms_delegate_education_for_a_current_jev_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, arm: str
) -> None:
    if arm == "alarm" and not alarm_can_be_armed():
        pytest.skip("this platform has no `signal.setitimer`; the worker arm is the only arm")
    monkeypatch.setattr("venator.match.budget.alarm_can_be_armed", lambda: arm == "alarm")
    profile = load_profile(Path(__file__).resolve().parents[2] / "profiles" / "example")
    policy = replace(profile.filters, qualification_mode="jev", jev=JEV_POLICY)
    context = make_context()
    in_process = apply_filters(PHD, policy, jev=context)
    with hard_filter_budget(10.0, profile.targeting_path, policy) as filtered:
        through_arm = filtered(PHD, context)
        omitted = filtered(PHD)
    assert tuple(through_arm) == tuple(in_process)
    assert through_arm.verdict == "pass"
    assert through_arm.facts["education_fit"] == "delegated"
    assert through_arm.facts["jev"] == "assess-priority"
    assert omitted.verdict == "kill"
    assert omitted.rule == "education_fit"


def test_worker_and_alarm_agree_on_exclude_and_legacy_none(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = load_profile(Path(__file__).resolve().parents[2] / "profiles" / "example")
    policy = replace(profile.filters, qualification_mode="jev", jev=JEV_POLICY)
    intern = {
        "key": "p:pass",
        "title": "Research Associate",
        "location": "Boston, MA",
        "description_kind": "full",
        "description_html": "<p>A BS and two years of experience.</p>",
    }
    exclude = make_context(
        posting_key="p:pass",
        decision="exclude",
        primary_rule="restricted_role",
        assessment_key="assess-exclude",
        exclusions=(
            JevExclusion(
                rule="restricted_role",
                basis="model_assisted_policy",
                question_ids=("citizenship_or_clearance",),
                probabilities=(("citizenship_or_clearance", 0.95),),
                guard_code="fired",
                policy_value="exclude",
                policy_exclusion=True,
            ),
        ),
    )
    monkeypatch.setattr("venator.match.budget.alarm_can_be_armed", lambda: False)
    with hard_filter_budget(10.0, profile.targeting_path, policy) as worker:
        worker_exclude = worker(intern, exclude)
        worker_none = worker(intern, None)
    in_exclude = apply_filters(intern, policy, jev=exclude)
    in_none = apply_filters(intern, policy, jev=None)
    assert tuple(worker_exclude) == tuple(in_exclude)
    assert worker_exclude.rule == "jev_policy:restricted_role"
    assert dict(worker_exclude.facts) == {"jev": "assess-exclude", "jev_input": "input-v"}
    assert tuple(worker_none) == tuple(in_none)


def test_a_jev_context_pickles_for_the_worker_message() -> None:
    context = make_context()
    restored = pickle.loads(pickle.dumps((PHD, context), protocol=pickle.HIGHEST_PROTOCOL))
    assert restored[0]["key"] == "p:education"
    assert restored[1] == context
    assert pickle.loads(pickle.dumps((PHD, None)))[1] is None

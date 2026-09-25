"""The one Jev command: preview, offline cache recording, live execute with a fake poster."""

from __future__ import annotations

import json
import threading
import time
from dataclasses import replace
from pathlib import Path

import pytest

from venator.discover.store import posting_revision
from venator.llm.system_one import (
    KEY_VAR,
    MODEL,
    OBSERVED_USD_PER_REQUEST,
    SystemOneHttpResult,
    SystemOneRequest,
)
from venator.profile.loader import load_profile
from venator.qualify.compile import compile_profile
from venator.qualify.jev import (
    MAX_IN_FLIGHT,
    QualificationLock,
    execute_prepared_cases,
    is_protected,
    main,
    select_work,
)
from venator.qualify.jev_contract import JevResult
from venator.qualify.jev_effective import (
    bound_input_version,
    current_filter_version,
    jev_promotion_state,
)
from venator.qualify.jev_policy import prepare_jev_case
from venator.qualify.jev_release import JEV_RELEASE
from venator.qualify.store import JEV_KIND, append_event, qualification_rows
from venator.qualify.versions import jev_accepted_profile_hash, jev_policy_hash, sha256_utf8

FIXTURE = Path(__file__).parent / "fixtures" / "jev"
SENTINEL = "sk-venator-jev-cli-sentinel-0123456789"
RAW = (FIXTURE / "valid_raw_response.json").read_text(encoding="utf-8")


class RecordingPoster:
    def __init__(self, body: bytes = RAW.encode("utf-8")) -> None:
        self.body = body
        self.calls: list[SystemOneRequest] = []

    def __call__(self, request: SystemOneRequest) -> SystemOneHttpResult:
        self.calls.append(request)
        assert request.url == "https://api.typesafe.ai/v1/systemone"
        assert request.headers["Authorization"] == f"Bearer {SENTINEL}"
        assert SENTINEL not in request.url
        return SystemOneHttpResult(200, self.body)


def _posting() -> dict:
    return json.loads((FIXTURE / "posting.json").read_text(encoding="utf-8"))


def _write_postings(directory: Path, *postings: dict) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / "2026-09-18.jsonl"
    path.write_text("".join(json.dumps(item) + "\n" for item in postings), encoding="utf-8")
    return directory


def _profile():
    return load_profile(FIXTURE / "profile")


def _argv(tmp_path: Path, extra: list[str]) -> list[str]:
    postings = tmp_path / "postings"
    qualifications = tmp_path / "qualifications"
    return [
        "--profile-dir", str(FIXTURE / "profile"),
        "--postings-dir", str(postings),
        "--qualifications-dir", str(qualifications),
        "--as-of", "2026-09-18",
        *extra,
    ]


def _activate(directory: Path, profile) -> None:
    compiled = compile_profile(
        profile, as_of_month="2026-09",
    )
    policy = profile.filters.jev
    assert policy is not None
    accepted = jev_accepted_profile_hash(compiled.profile_hash, jev_policy_hash(policy))
    row = {
        "event": "finalized", "profile_id": profile.identifier, "profile_hash": accepted,
        "compiled_profile_hash": compiled.profile_hash, "policy_hash": jev_policy_hash(policy),
        "qualifier_version": JEV_RELEASE.qualifier_version, "qualifier_kind": JEV_KIND,
        "components": {}, "finalization_sha256": "sha", "cohort_hashes": {}, "refers_to": None,
        "at": "2026-09-18T00:00:00+00:00", "by": "test",
    }
    finalized = append_event(directory, profile.identifier, row)
    signed = append_event(directory, profile.identifier, {**row, "event": "signed", "refers_to": finalized})
    append_event(directory, profile.identifier, {**row, "event": "activated", "refers_to": signed})


def _prepared_cases(count: int) -> list:
    profile = _profile()
    cases = []
    for index in range(count):
        posting = {
            **_posting(),
            "key": f"fixture:jev-{index}",
            "title": f"Laboratory Intern {index}",
        }
        prepared = prepare_jev_case(posting, profile, "2026-09")
        assert prepared.case is not None
        cases.append(prepared.case)
    return cases


def _execute_cases(tmp_path: Path, cases, poster, **overrides):
    profile = _profile()
    kwargs = {
        "qualifications_dir": tmp_path / "qualifications",
        "profile_id": profile.identifier,
        "mode": "shadow",
        "day": "2026-09-18",
        "decided_at": "2026-09-18T00:00:00+00:00",
        "offline": False,
        "max_usd": None,
        "max_requests": None,
        "poster": poster,
        "environ": {KEY_VAR: SENTINEL},
    }
    kwargs.update(overrides)
    return execute_prepared_cases(cases, **kwargs)


@pytest.mark.parametrize(
    ("statuses", "key", "cap", "paused", "completed"), [
        ([200, 200, 200], True, 10, None, 3),
        ([], False, 10, "no-key", 0),
        ([402], True, 10, "out-of-credit", 0),
        ([200, 402], True, 10, "out-of-credit", 1),
        ([200, 200], True, 0, "cap-reached", 0),
        ([503], True, 10, "transport-failed", 0),
    ],
)
def test_pipeline_pause_and_resume(
    tmp_path: Path, statuses: list[int], key: bool, cap: int,
    paused: str | None, completed: int,
) -> None:
    cases = _prepared_cases(3)
    calls = 0

    def first(request: SystemOneRequest) -> SystemOneHttpResult:
        nonlocal calls
        status = statuses[calls]
        calls += 1
        return SystemOneHttpResult(status, RAW.encode() if status == 200 else b"untrusted")

    first_report = _execute_cases(
        tmp_path, cases, first, pause_on_failure=True, max_usd=cap,
        environ={KEY_VAR: SENTINEL} if key else {},
    )
    assert first_report.paused == paused
    assert first_report.valid_responses == completed
    assert first_report.waiting == (3 - completed if paused else 0)
    assert calls == (0 if not key or cap == 0 else len(statuses) if paused == "out-of-credit" else 1 if paused else 3)
    rows = qualification_rows(tmp_path / "qualifications")
    assert len([row for row in rows if row.get("decision") != "unassessed"]) == completed
    assert all(row.get("reason") not in {"out_of_credit", "transport_failed", "credentials_missing"} for row in rows)

    # Selection uses the same binding as the next pipeline run: only completed
    # Postings are removed from the queue; a paused request is never a result.
    remaining = [case for case in cases if case.bindings.posting_key not in {
        row.get("posting_key") for row in rows if row.get("decision") != "unassessed"
    }]
    second = _execute_cases(tmp_path, remaining, RecordingPoster(), pause_on_failure=True, max_usd=10)
    assert second.paused is None
    assert second.valid_responses == 3 - completed
    assert len([row for row in qualification_rows(tmp_path / "qualifications")
                if row.get("decision") != "unassessed"]) == 3


def test_hard_filter_pass_selection_is_exact_and_read_only(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    passed = {**_posting(), "key": "fixture:passed"}
    killed = {**_posting(), "key": "fixture:killed"}
    stale = {**_posting(), "key": "fixture:stale"}
    old_rules = {**_posting(), "key": "fixture:old-rules"}
    old_binding = {**_posting(), "key": "fixture:old-binding"}
    _write_postings(tmp_path / "postings", passed, killed, stale, old_rules, old_binding)
    decisions = tmp_path / "decisions"
    decisions.mkdir()
    profile = _profile()
    version = current_filter_version(
        profile, jev_promotion_state([], profile.identifier), JEV_RELEASE,
    )

    def decision(posting: dict, verdict: str = "pass") -> dict:
        return {
            "posting_key": posting["key"], "stage": "hard_filter", "verdict": verdict,
            "posting_version": posting_revision(posting), "filters_version": version,
            "facts": {"jev": "fallback", "jev_input": bound_input_version(
                posting, profile, "2026-09", JEV_RELEASE,
            )},
        }

    rows = [
        decision(passed),
        decision(killed, "kill"),
        {**decision(stale), "posting_version": "an-older-revision"},
        {**decision(old_rules), "filters_version": "an-older-ruleset"},
        {**decision(old_binding), "facts": {"jev": "fallback", "jev_input": "stale"}},
    ]
    (decisions / "2026-09-18.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8"
    )
    poster = RecordingPoster()

    code = main(
        _argv(tmp_path, ["--decisions-dir", str(decisions), "--hard-filter-passes"]),
        poster=poster,
        environ={KEY_VAR: SENTINEL},
    )

    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert payload["selected"] == 1
    assert poster.calls == []
    assert not (tmp_path / "qualifications").exists()

    assert main(
        _argv(tmp_path, [
            "--decisions-dir", str(decisions), "--hard-filter-passes",
            "--execute", "--max-usd", "0.01",
        ]),
        poster=poster,
        environ={KEY_VAR: SENTINEL},
    ) == 0
    capsys.readouterr()
    assert len(poster.calls) == 1

    poster.calls.clear()
    assert main(
        _argv(tmp_path, ["--decisions-dir", str(decisions), "--hard-filter-passes"]),
        poster=poster,
        environ={KEY_VAR: SENTINEL},
    ) == 0
    second = json.loads(capsys.readouterr().out)
    assert second["selected"] == 0
    assert second["current_assessments"] == 1
    assert poster.calls == []


def test_preview_does_not_claim_or_call_network(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_postings(tmp_path / "postings", _posting())
    poster = RecordingPoster()
    code = main(_argv(tmp_path, []), poster=poster, environ={KEY_VAR: SENTINEL})
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["selected"] == 1
    assert payload["skipped_closed"] == 0
    assert payload["skipped_protected"] == 0
    assert payload["estimated_usd"] == pytest.approx(OBSERVED_USD_PER_REQUEST)
    assert "reservation_usd" not in payload
    assert poster.calls == []
    assert not (tmp_path / "qualifications").exists()
    assert SENTINEL not in captured.out
    assert SENTINEL not in captured.err


def test_live_execute_records_triage_without_match_scoring(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_postings(tmp_path / "postings", _posting())
    poster = RecordingPoster()
    code = main(
        _argv(tmp_path, ["--execute", "--max-usd", "1"]),
        poster=poster, environ={KEY_VAR: SENTINEL},
    )
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert code == 0
    assert payload["valid_responses"] == 1
    assert payload["decisions"]["prioritize"] == 1
    assert payload["http_attempts"] == 1
    assert payload["cache_hits"] == 0
    assert len(poster.calls) == 1
    rows = qualification_rows(tmp_path / "qualifications")
    assert rows[0]["decision"] == "prioritize"
    assert rows[0]["cache_hit"] is False
    assert poster.calls[0].payload["model"] == MODEL
    assert SENTINEL not in captured.out + captured.err
    assert SENTINEL not in json.dumps(rows[0])


def test_second_execute_skips_a_current_success(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    _write_postings(tmp_path / "postings", _posting())
    poster = RecordingPoster()
    assert main(_argv(tmp_path, ["--execute", "--max-usd", "1"]),
                poster=poster, environ={KEY_VAR: SENTINEL}) == 0
    capsys.readouterr()
    poster.calls.clear()
    code = main(_argv(tmp_path, ["--execute", "--max-usd", "1"]),
                poster=poster, environ={KEY_VAR: SENTINEL})
    payload = json.loads(capsys.readouterr().out)
    assert code == 0
    assert poster.calls == []
    assert payload["selected"] == 0
    assert payload["current_assessments"] == 1
    rows = qualification_rows(tmp_path / "qualifications")
    assert len([row for row in rows if row.get("decision") == "prioritize"]) == 1


def test_execute_prepared_cases_reuses_the_cache(tmp_path: Path) -> None:
    profile = _profile()
    prepared = prepare_jev_case(_posting(), profile, "2026-09")
    assert prepared.case is not None
    poster = RecordingPoster()
    first = execute_prepared_cases(
        [prepared.case],
        qualifications_dir=tmp_path / "qualifications",
        profile_id=profile.identifier,
        mode="shadow",
        day="2026-09-18",
        decided_at="2026-09-18T00:00:00+00:00",
        offline=False,
        max_usd=1.0,
        max_requests=1,
        poster=poster,
        environ={KEY_VAR: SENTINEL},
    )
    assert first.valid_responses == 1
    assert len(poster.calls) == 1
    second = execute_prepared_cases(
        [prepared.case],
        qualifications_dir=tmp_path / "qualifications",
        profile_id=profile.identifier,
        mode="shadow",
        day="2026-09-18",
        decided_at="2026-09-18T00:00:01+00:00",
        offline=True,
        max_usd=None,
        max_requests=None,
        poster=poster,
        environ={KEY_VAR: SENTINEL},
    )
    assert second.cache_hits == 1
    assert len(poster.calls) == 1
    rows = qualification_rows(tmp_path / "qualifications")
    assert len([row for row in rows if row.get("decision") == "prioritize"]) == 1


def test_budget_refuses_before_sending(tmp_path: Path) -> None:
    _write_postings(tmp_path / "postings", _posting())
    poster = RecordingPoster()
    code = main(
        _argv(tmp_path, ["--execute", "--max-usd", "0"]),
        poster=poster, environ={KEY_VAR: SENTINEL},
    )
    assert poster.calls == []
    rows = qualification_rows(tmp_path / "qualifications")
    assert rows[0]["reason"] == "budget_exhausted"
    assert code == 1


def test_contention_refuses_before_network(tmp_path: Path) -> None:
    _write_postings(tmp_path / "postings", _posting())
    qualifications = tmp_path / "qualifications"
    qualifications.mkdir()
    poster = RecordingPoster()
    held = QualificationLock(qualifications)
    held.acquire()
    try:
        code = main(
            _argv(tmp_path, ["--execute", "--max-usd", "1"]),
            poster=poster, environ={KEY_VAR: SENTINEL},
        )
        assert code == 1
        assert poster.calls == []
        assert qualification_rows(qualifications) == []
    finally:
        held.release()


def test_named_unknown_key_is_a_command_error(tmp_path: Path) -> None:
    _write_postings(tmp_path / "postings", _posting())
    with pytest.raises(SystemExit):
        main(_argv(tmp_path, ["--posting-key", "no-such-key"]))


def test_named_snippet_is_unassessed_without_http(tmp_path: Path) -> None:
    snippet = {**_posting(), "key": "fixture:snippet", "description_kind": "snippet"}
    _write_postings(tmp_path / "postings", snippet)
    poster = RecordingPoster()
    code = main(
        _argv(tmp_path, ["--posting-key", "fixture:snippet", "--execute", "--offline"]),
        poster=poster, environ={KEY_VAR: SENTINEL},
    )
    assert code == 1
    assert poster.calls == []
    rows = qualification_rows(tmp_path / "qualifications")
    assert rows[0]["reason"] == "snippet"
    assert rows[0]["decision"] == "unassessed"


def test_closed_and_protected_are_skipped(tmp_path: Path) -> None:
    closed = {**_posting(), "key": "fixture:closed", "listing_status": "closed"}
    protected_key = "fixture:protected"
    protected = {**_posting(), "key": protected_key}
    _write_postings(tmp_path / "postings", closed, protected)
    release = replace(
        JEV_RELEASE,
        protected_group_hashes=JEV_RELEASE.protected_group_hashes + (sha256_utf8(protected_key),),
    )
    profile = _profile()
    work = select_work(
        [closed, protected], profile=profile, month="2026-09", mode="shadow",
        qualifications_dir=tmp_path / "qualifications", named_keys=None,
        release=release, decided_at="2026-09-18T00:00:00+00:00",
    )
    assert work.report.skipped_closed == 1
    assert work.report.skipped_protected == 1
    assert work.prepared == []
    assert is_protected(protected_key, protected_key, release.protected_group_hashes)


def test_shadow_and_promoted_need_separate_rows(tmp_path: Path) -> None:
    _write_postings(tmp_path / "postings", _posting())
    poster = RecordingPoster()
    assert main(_argv(tmp_path, ["--execute", "--max-usd", "1"]),
                poster=poster, environ={KEY_VAR: SENTINEL}) == 0
    poster.calls.clear()
    with pytest.raises(SystemExit):
        main(_argv(tmp_path, ["--mode", "promoted", "--execute", "--offline"]),
             poster=poster, environ={KEY_VAR: SENTINEL})
    assert poster.calls == []
    _activate(tmp_path / "qualifications", _profile())
    assert main(_argv(tmp_path, ["--mode", "promoted", "--execute", "--offline"]),
                poster=poster, environ={KEY_VAR: SENTINEL}) == 0
    modes = {row["mode"] for row in qualification_rows(tmp_path / "qualifications")
             if row.get("decision") == "prioritize"}
    assert modes == {"shadow", "promoted"}
    assert poster.calls == []


def test_failed_transport_is_visible(tmp_path: Path) -> None:
    _write_postings(tmp_path / "postings", _posting())

    class Boom:
        def __call__(self, request: SystemOneRequest) -> SystemOneHttpResult:
            return SystemOneHttpResult(500, b"nope")

    code = main(
        _argv(tmp_path, ["--execute", "--max-usd", "1"]),
        poster=Boom(), environ={KEY_VAR: SENTINEL},
    )
    rows = qualification_rows(tmp_path / "qualifications")
    assert rows[0]["reason"] == "transport_failed"
    assert rows[0]["decision"] == "unassessed"
    assert code == 1


def test_execute_prepared_cases_is_the_r5_entry(tmp_path: Path) -> None:
    profile = _profile()
    prepared = prepare_jev_case(_posting(), profile, "2026-09")
    assert prepared.case is not None
    poster = RecordingPoster()
    report = execute_prepared_cases(
        [prepared.case],
        qualifications_dir=tmp_path / "qualifications",
        profile_id=profile.identifier,
        mode="shadow",
        day="2026-09-18",
        decided_at="2026-09-18T00:00:00+00:00",
        offline=False,
        max_usd=1.0,
        max_requests=1,
        poster=poster,
        environ={KEY_VAR: SENTINEL},
    )
    assert report.valid_responses == 1
    assert report.decisions["prioritize"] == 1
    assert qualification_rows(tmp_path / "qualifications")[0]["qualifier_kind"] == JEV_KIND


def test_live_execute_without_caps_posts_every_case(tmp_path: Path) -> None:
    postings = [
        {**_posting(), "key": f"fixture:jev-{index}", "title": f"Laboratory Intern {index}"}
        for index in range(20)
    ]
    _write_postings(tmp_path / "postings", *postings)
    poster = RecordingPoster()
    code = main(_argv(tmp_path, ["--execute"]), poster=poster, environ={KEY_VAR: SENTINEL})
    assert code == 0
    assert len(poster.calls) == 20


def test_scores_outside_the_unit_interval_are_unassessed_not_recorded(tmp_path: Path) -> None:
    """Validator 3 refuses a level distribution of five ones as sum_out_of_bound.

    The command must still record unassessed ``response_invalid`` and must not
    write a success row. The view's jev_triage CHECK requires the unit interval.
    """
    body = json.loads(RAW)
    body["answers"]["level"]["probabilities"] = {str(index): 1.0 for index in range(5)}
    body["answers"]["level"]["score"] = 10.0
    for question in ("plausible_candidate", "would_apply", "domain_match"):
        body["answers"][question]["noul"] = 1.0
    _write_postings(tmp_path / "postings", _posting())
    poster = RecordingPoster(json.dumps(body).encode("utf-8"))
    code = main(_argv(tmp_path, ["--execute", "--max-usd", "1"]), poster=poster, environ={KEY_VAR: SENTINEL})
    rows = qualification_rows(tmp_path / "qualifications")
    assert code == 1
    assert [row["decision"] for row in rows] == ["unassessed"]
    assert rows[0]["reason"] == "response_invalid"
    assert rows[0]["fit_score"] is None
    assert rows[0]["diagnostic_flags"] == ["validator=sum_out_of_bound"]
    assert not (tmp_path / "qualifications" / "jev").exists()


def test_command_still_refuses_a_reduced_score_outside_the_unit_interval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A validated body cannot overflow after validator 3. The reviewer's
    scores_out_of_range guard still fires if reduce_jev returns one."""
    overflow = JevResult("prioritize", 1.0, 1.25, (), None, (), (), False, False)
    monkeypatch.setattr("venator.qualify.jev.reduce_jev", lambda *_args, **_kwargs: overflow)
    _write_postings(tmp_path / "postings", _posting())
    poster = RecordingPoster()
    code = main(_argv(tmp_path, ["--execute", "--max-usd", "1"]), poster=poster, environ={KEY_VAR: SENTINEL})
    rows = qualification_rows(tmp_path / "qualifications")
    assert code == 1
    assert [row["decision"] for row in rows] == ["unassessed"]
    assert rows[0]["reason"] == "response_invalid"
    assert rows[0]["diagnostic_flags"] == ["scores_out_of_range"]
    assert rows[0]["fit_score"] is None


def test_forty_cases_start_together(tmp_path: Path) -> None:
    """A serial loop deadlocks here: the poster waits until the in-flight cap is inside."""
    count = min(40, MAX_IN_FLIGHT)
    cases = _prepared_cases(count)
    barrier = threading.Barrier(count, timeout=10)
    poster = RecordingPoster()

    def gated(request: SystemOneRequest) -> SystemOneHttpResult:
        result = poster(request)
        barrier.wait()
        return result

    report = _execute_cases(tmp_path, cases, gated)
    assert len(poster.calls) == count
    assert report.valid_responses == count
    assert SENTINEL not in json.dumps(report.as_dict())


def test_three_hundred_cases_keep_at_most_the_pool_in_flight(tmp_path: Path) -> None:
    """An unbounded pool lets every case enter the poster at once."""
    cases = _prepared_cases(300)
    lock = threading.Lock()
    inside = 0
    peak = 0
    poster = RecordingPoster()

    def gated(request: SystemOneRequest) -> SystemOneHttpResult:
        nonlocal inside, peak
        with lock:
            inside += 1
            if inside > peak:
                peak = inside
        try:
            time.sleep(0.02)
            return poster(request)
        finally:
            with lock:
                inside -= 1

    report = _execute_cases(tmp_path, cases, gated)
    assert peak <= MAX_IN_FLIGHT
    assert len(poster.calls) == 300
    assert report.valid_responses == 300
    assert SENTINEL not in json.dumps(report.as_dict())


def test_transport_failed_row_is_reselected_and_a_success_is_not(
    tmp_path: Path, capsys: pytest.CaptureFixture[str],
) -> None:
    failed = {**_posting(), "key": "fixture:jev-failed", "title": "Laboratory Intern failed"}
    ok = {**_posting(), "key": "fixture:jev-ok", "title": "Laboratory Intern ok"}
    _write_postings(tmp_path / "postings", failed, ok)

    class Selective:
        def __init__(self) -> None:
            self.calls: list[str] = []

        def __call__(self, request: SystemOneRequest) -> SystemOneHttpResult:
            title = str(request.payload["state"]["posting"]["title"])
            self.calls.append(title)
            if title.endswith("failed"):
                return SystemOneHttpResult(500, b"nope")
            return SystemOneHttpResult(200, RAW.encode("utf-8"))

    first = Selective()
    main(_argv(tmp_path, ["--execute"]), poster=first, environ={KEY_VAR: SENTINEL})
    capsys.readouterr()
    assert sorted(first.calls) == ["Laboratory Intern failed", "Laboratory Intern ok"]

    second = RecordingPoster()
    code = main(_argv(tmp_path, ["--execute"]), poster=second, environ={KEY_VAR: SENTINEL})
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 1
    assert payload["current_assessments"] == 1
    assert len(second.calls) == 1
    assert second.calls[0].payload["state"]["posting"]["title"] == "Laboratory Intern failed"
    assert code == 0


def test_max_requests_five_on_twenty_cases_is_exactly_five_posts(tmp_path: Path) -> None:
    postings = [
        {**_posting(), "key": f"fixture:jev-{index}", "title": f"Laboratory Intern {index}"}
        for index in range(20)
    ]
    _write_postings(tmp_path / "postings", *postings)
    poster = RecordingPoster()
    code = main(
        _argv(tmp_path, ["--execute", "--max-requests", "5"]),
        poster=poster, environ={KEY_VAR: SENTINEL},
    )
    assert len(poster.calls) == 5
    rows = qualification_rows(tmp_path / "qualifications")
    exhausted = [row for row in rows if row.get("reason") == "budget_exhausted"]
    assert len(exhausted) == 15
    assert code == 1


def test_reversed_completion_still_appends_in_case_order(tmp_path: Path) -> None:
    cases = _prepared_cases(8)
    barrier = threading.Barrier(8, timeout=10)
    poster = RecordingPoster()

    def reversed_order(request: SystemOneRequest) -> SystemOneHttpResult:
        result = poster(request)
        title = str(request.payload["state"]["posting"]["title"])
        index = int(title.rsplit(" ", 1)[-1])
        barrier.wait()
        time.sleep((len(cases) - index) * 0.01)
        return result

    _execute_cases(tmp_path, cases, reversed_order)
    rows = qualification_rows(tmp_path / "qualifications")
    assert [row["posting_key"] for row in rows] == [case.bindings.posting_key for case in cases]
    assert SENTINEL not in json.dumps(rows)


def test_auth_failed_stops_later_starts(tmp_path: Path) -> None:
    cases = _prepared_cases(8)
    lock = threading.Lock()
    calls: list[SystemOneRequest] = []
    released = threading.Event()
    entered = threading.Event()

    def poster(request: SystemOneRequest) -> SystemOneHttpResult:
        assert SENTINEL not in request.url
        with lock:
            calls.append(request)
            n = len(calls)
        if n == 1:
            entered.set()
            released.wait(timeout=5)
            return SystemOneHttpResult(401, b"no")
        # A start after the 401 must not reach here. In-flight first attempts
        # that reserved before the flag may still arrive; they also 401 so a
        # later 200 cannot slip through.
        return SystemOneHttpResult(401, b"no")

    def run() -> None:
        _execute_cases(tmp_path, cases, poster)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(timeout=5)
    # While the first 401 is in flight, every remaining case has had a chance
    # to reserve a start. Releasing the 401 then sets stop_auth; retries and
    # any start() that has not yet run must refuse.
    released.set()
    worker.join(timeout=15)
    assert not worker.is_alive()
    rows = qualification_rows(tmp_path / "qualifications")
    assert [row["decision"] for row in rows] == ["unassessed"] * 8
    assert {row["reason"] for row in rows} == {"credentials_missing"}
    assert 1 <= len(calls) <= 8
    assert SENTINEL not in json.dumps(rows)

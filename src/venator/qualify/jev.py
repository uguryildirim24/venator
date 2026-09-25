"""One Jev command: preview, offline cache recording, or live execute."""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
from collections.abc import Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, TextIO

from venator.discover.store import posting_revision
from venator.qualify.compile import compile_profile
from venator.llm.system_one import (
    CASE_DEADLINE,
    ENDPOINT,
    INPUT_TARIFF_USD_PER_MILLION,
    MAX_RETRY_ATTEMPTS,
    MODEL,
    OBSERVED_USD_PER_REQUEST,
    Clock,
    Poster,
    SystemOneError,
    SystemOneHttpResult,
    SystemOneRequest,
    httpx_poster,
    post_systemone,
)
from venator.match.store import load_latest_decisions, load_postings, verify_decisions_dir
from venator.paths import STORE_HELP, resolve_store_paths
from venator.profile import add_profile_argument, announce_profile, profile_from_arguments
from venator.profile.schema import Profile
from venator.qualify.jev_cache import (
    CacheCorruptError,
    CacheEscapeError,
    CacheIdentityError,
    publish_response,
    read_response,
    response_ref,
)
from venator.qualify.jev_effective import current_filter_version, jev_promotion_state
from venator.qualify.jev_contract import (
    JevExclusion,
    JevResult,
    JevValidation,
    validate_jev_response,
)
from venator.qualify.jev_policy import PreparedJevCase, prepare_jev_case
from venator.qualify.jev_reduce import reduce_jev
from venator.qualify.jev_release import JEV_RELEASE, JevRelease
from venator.qualify.store import (
    JEV_KIND,
    append_rows,
    as_of_month,
    effective_rows,
    fold_events,
    promotion_events,
    qualification_rows,
    verify_store,
)
from venator.qualify.versions import (
    jev_accepted_profile_hash,
    jev_input_version,
    jev_policy_hash,
    sha256_json,
    sha256_utf8,
)

REASON_FROM_TRANSPORT = {
    "credentials_missing": "credentials_missing",
    "auth_failed": "credentials_missing",
    "model_mismatch": "model_mismatch",
    "response_invalid": "response_invalid",
    "key_echo": "response_invalid",
    "budget_exhausted": "budget_exhausted",
    "out_of_credit": "out_of_credit",
    "timeout": "transport_failed",
    "transport_failed": "transport_failed",
    "redirected": "transport_failed",
    "rate_limited": "transport_failed",
}

MAX_IN_FLIGHT = 32
NOFILE_HEADROOM = 64


class ContentionError(RuntimeError):
    """A second writer already holds the qualification-root lock."""


class QualificationLock:
    """Process-exclusion lock in the OS temp directory, released on exit."""

    def __init__(self, root: Path) -> None:
        digest = hashlib.sha256(str(root.resolve()).encode("utf-8")).hexdigest()
        self.path = Path(tempfile.gettempdir()) / f"venator-jev-{digest}.lock"
        self._fh: Any = None

    def acquire(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            raise ContentionError("qualification root is already locked") from None
        self._fh = handle

    def release(self) -> None:
        handle = self._fh
        if handle is None:
            return
        self._fh = None
        try:
            if sys.platform == "win32":
                import msvcrt
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> QualificationLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()


@dataclass
class Budget:
    """Thread-safe spend and request accounting. ``None`` on a cap means no cap."""

    max_usd: float | None
    max_requests: int | None
    accounted: float = 0.0
    attempts: int = 0
    input_tokens: int = 0
    stop_auth: bool = False
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def can_start(self) -> bool:
        if self.stop_auth:
            return False
        if self.max_requests is not None and self.attempts >= self.max_requests:
            return False
        if self.max_usd is not None and self.accounted >= self.max_usd - 1e-15:
            return False
        return True

    def start(self) -> bool:
        with self._lock:
            if not self.can_start():
                return False
            self.attempts += 1
            return True

    def observe_success(self, input_tokens: int) -> None:
        with self._lock:
            self.accounted += input_tokens * INPUT_TARIFF_USD_PER_MILLION / 1_000_000
            self.input_tokens += input_tokens

    def note_auth_failed(self) -> None:
        with self._lock:
            self.stop_auth = True

    def auth_stopped(self) -> bool:
        with self._lock:
            return self.stop_auth


@dataclass
class ExecutionReport:
    selected: int = 0
    cache_hits: int = 0
    http_attempts: int = 0
    valid_responses: int = 0
    decisions: dict[str, int] = field(default_factory=dict)
    unassessed: dict[str, int] = field(default_factory=dict)
    input_tokens: int = 0
    accounted_usd: float = 0.0
    skipped_protected: int = 0
    skipped_closed: int = 0
    current_assessments: int = 0
    snippets: int = 0
    missing: int = 0
    too_long: int = 0
    incomplete: int = 0
    contention: bool = False
    http_429s: int = 0
    paused: str | None = None
    waiting: int = 0
    tariff_usd_per_million: float = INPUT_TARIFF_USD_PER_MILLION
    endpoint: str = ENDPOINT
    model: str = MODEL

    def as_dict(self) -> dict[str, Any]:
        return {
            "selected": self.selected,
            "cache_hits": self.cache_hits,
            "http_attempts": self.http_attempts,
            "valid_responses": self.valid_responses,
            "decisions": dict(self.decisions),
            "unassessed": dict(self.unassessed),
            "paused": self.paused,
            "waiting": self.waiting,
            "input_tokens": self.input_tokens,
            "accounted_usd": self.accounted_usd,
            "estimated_usd": self.selected * OBSERVED_USD_PER_REQUEST,
            "skipped_protected": self.skipped_protected,
            "skipped_closed": self.skipped_closed,
            "current_assessments": self.current_assessments,
            "snippets": self.snippets,
            "missing": self.missing,
            "too_long": self.too_long,
            "incomplete": self.incomplete,
            "contention": self.contention,
            "http_429s": self.http_429s,
            "tariff_usd_per_million": self.tariff_usd_per_million,
        }


def _count(target: dict[str, int], key: str) -> None:
    target[key] = target.get(key, 0) + 1


def _group_key(posting: Mapping[str, Any], group_index: Mapping[str, Any]) -> str:
    key = str(posting.get("key") or "")
    mapping = group_index.get("posting_to_group")
    if isinstance(mapping, dict) and key in mapping:
        return str(mapping[key])
    return key


def is_protected(posting_key: str, group_key: str, hashes: Sequence[str]) -> bool:
    blocked = set(hashes)
    return sha256_utf8(posting_key) in blocked or sha256_utf8(group_key) in blocked


def _pack_exclusion(item: JevExclusion) -> dict[str, Any]:
    return {
        "rule": item.rule,
        "basis": item.basis,
        "question_ids": list(item.question_ids),
        "probabilities": [[name, value] for name, value in item.probabilities],
        "guard_code": item.guard_code,
        "policy_value": item.policy_value,
        "policy_exclusion": item.policy_exclusion,
    }


def _assessment_key(input_version_value: str, qualifier_version: str,
                    request_key: str | None, response_sha256: str | None) -> str:
    return sha256_json({
        "input_version": input_version_value,
        "qualifier_version": qualifier_version,
        "request_key": request_key,
        "response_sha256": response_sha256,
    })


def _unassessed_row(
    *,
    posting_key: str,
    profile_id: str,
    posting_revision: str,
    profile_hash: str,
    policy_hash: str,
    as_of_month_value: str,
    qualifier_version: str,
    qualifier_identity: Mapping[str, str],
    input_version_value: str,
    mode: str,
    reason: str,
    decided_at: str,
    request_key: str | None = None,
    state_sha256: str | None = None,
    questions_sha256: str | None = None,
    diagnostic_flags: Sequence[str] = (),
) -> dict[str, Any]:
    accepted = jev_accepted_profile_hash(profile_hash, policy_hash)
    return {
        "schema": "jev-assessment-2",
        "qualifier_kind": JEV_KIND,
        "posting_key": posting_key,
        "profile_id": profile_id,
        "posting_revision": posting_revision,
        "profile_hash": profile_hash,
        "policy_hash": policy_hash,
        "accepted_profile_hash": accepted,
        "as_of_month": as_of_month_value,
        "input_version": input_version_value,
        "qualifier_version": qualifier_version,
        "qualifier_identity": dict(qualifier_identity),
        "assessment_key": _assessment_key(
            input_version_value, qualifier_version, request_key, None,
        ),
        "request_key": request_key,
        "state_sha256": state_sha256,
        "questions_sha256": questions_sha256,
        "mode": mode,
        "decision": "unassessed",
        "fit_probability": None,
        "fit_score": None,
        "exclusions": [],
        "primary_rule": None,
        "review_flags": [],
        "diagnostic_flags": list(diagnostic_flags),
        "response_validation": None,
        "response_sha256": None,
        "response_ref": None,
        "model_returned": None,
        "usage": None,
        "latency_ms": None,
        "decided_at": decided_at,
        "cache_hit": False,
        "reason": reason,
    }


def _success_row(
    prepared: PreparedJevCase,
    result: JevResult,
    validation: JevValidation,
    *,
    profile_id: str,
    mode: str,
    decided_at: str,
    cache_hit: bool,
    latency_ms: float,
    usage: Mapping[str, int],
    release: JevRelease,
) -> dict[str, Any]:
    bindings = prepared.bindings
    response = validation.response
    assert response is not None
    return {
        "schema": "jev-assessment-2",
        "qualifier_kind": JEV_KIND,
        "posting_key": bindings.posting_key,
        "profile_id": profile_id,
        "posting_revision": bindings.posting_revision,
        "profile_hash": bindings.profile_hash,
        "policy_hash": bindings.policy_hash,
        "accepted_profile_hash": bindings.accepted_profile_hash,
        "as_of_month": bindings.as_of_month,
        "input_version": bindings.input_version,
        "qualifier_version": bindings.qualifier_version,
        "qualifier_identity": dict(release.qualifier_identity),
        "assessment_key": _assessment_key(
            bindings.input_version, bindings.qualifier_version,
            bindings.request_key, response.response_sha256,
        ),
        "request_key": bindings.request_key,
        "state_sha256": bindings.state_sha256,
        "questions_sha256": bindings.questions_sha256,
        "mode": mode,
        "decision": result.decision,
        "fit_probability": result.fit_probability,
        "fit_score": result.fit_score,
        "exclusions": [_pack_exclusion(item) for item in result.exclusions],
        "primary_rule": result.primary_rule,
        "review_flags": list(result.review_flags),
        "diagnostic_flags": list(result.diagnostic_flags),
        "response_validation": {
            "passed": True,
            "version": validation.version,
            "error_codes": [],
        },
        "response_sha256": response.response_sha256,
        "response_ref": response_ref(bindings.request_key),
        "model_returned": response.model,
        "usage": {
            "input_tokens": int(usage.get("input_tokens", response.usage_input_tokens)),
            "output_tokens": int(usage.get("output_tokens", response.usage_output_tokens)),
        },
        "latency_ms": float(latency_ms),
        "decided_at": decided_at,
        "cache_hit": cache_hit,
        "reason": None,
    }


def _input_version_for(posting: Mapping[str, Any], profile_hash: str, policy_hash: str,
                       month: str, release: JevRelease) -> str:
    return jev_input_version(
        posting_key=str(posting["key"]),
        posting_revision=posting_revision(posting),
        profile_hash_value=profile_hash,
        policy_hash_value=policy_hash,
        as_of_month=month,
        canonicalizer_hash=release.canonicalizer_hash,
        compiler_hash=release.compiler_hash,
        evidence_hash=release.evidence_hash,
        projection_hash=release.projection_hash,
        deny_hash=release.deny_hash,
        state_renderer_version=release.state_renderer_version,
    )


def raise_open_file_limit() -> tuple[int, int, int, int, bool]:
    """Raise ``RLIMIT_NOFILE`` soft to hard when it cannot cover the pool.

    Returns before-soft, before-hard, after-soft, after-hard, and whether
    ``setrlimit`` ran. A missing ``resource`` module leaves the limits at 0.
    """
    try:
        import resource
    except ImportError:
        return (0, 0, 0, 0, False)
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    raised = False
    if soft < MAX_IN_FLIGHT + NOFILE_HEADROOM:
        try:
            resource.setrlimit(resource.RLIMIT_NOFILE, (hard, hard))
            raised = True
        except (ValueError, OSError):
            ceiling = 1_048_575
            if hard != resource.RLIM_INFINITY:
                ceiling = min(hard, ceiling)
            if ceiling > soft:
                resource.setrlimit(resource.RLIMIT_NOFILE, (ceiling, hard))
                raised = True
    after_soft, after_hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    return (soft, hard, after_soft, after_hard, raised)


def _current_keys(qualifications_dir: Path, profile_id: str, mode: str,
                  qualifier_version: str, input_versions: Mapping[str, str]) -> set[str]:
    if not qualifications_dir.exists():
        return set()
    verify_store(qualifications_dir, profile_id)
    rows = qualification_rows(qualifications_dir)
    effective = effective_rows(
        rows, profile_id=profile_id, mode=mode, qualifier_version=qualifier_version,
        input_versions=input_versions, qualifier_kind=JEV_KIND,
    )
    return {key for key, row in effective.items() if row.get("decision") in {"prioritize", "review", "exclude"}}


def current_mode(
    profile: Profile,
    month: str,
    qualifications_dir: Path,
    release: JevRelease | None = None,
) -> str:
    """The mode the current Jev activation permits, without writing the store."""
    packaged = release or JEV_RELEASE
    policy = profile.filters.jev
    if policy is None:
        raise ValueError("filters.jev is required for Jev")
    if not qualifications_dir.exists():
        return "shadow"
    verify_store(qualifications_dir, profile.identifier)
    accepted = jev_accepted_profile_hash(
        compile_profile(profile, as_of_month=month).profile_hash,
        jev_policy_hash(policy),
    )
    return (
        "promoted"
        if _promoted_fresh(
            qualifications_dir,
            profile.identifier,
            accepted,
            packaged.qualifier_version,
        )
        else "shadow"
    )


def hard_filter_passes(
    postings: Sequence[Mapping[str, Any]],
    decisions_dir: Path,
    profile: Profile,
    month: str,
    qualifications_dir: Path,
) -> list[Mapping[str, Any]]:
    """Postings whose current passing Filter Decision could need Jev."""
    if decisions_dir.exists():
        verify_decisions_dir(decisions_dir, profile.identifier)
    latest = load_latest_decisions(decisions_dir)
    jev_mode = profile.filters.qualification_mode == "jev"
    if qualifications_dir.exists():
        verify_store(qualifications_dir, profile.identifier)
    promotion = (
        jev_promotion_state(promotion_events(qualifications_dir), profile.identifier)
        if jev_mode else None
    )
    version = current_filter_version(profile, promotion, JEV_RELEASE if jev_mode else None)
    candidates = [
        posting for posting in postings
        if (decision := latest.get((str(posting.get("key") or ""), "hard_filter"))) is not None
        and decision.get("verdict") == "pass"
        and decision.get("filters_version") == version
        and decision.get("posting_version") == posting_revision(posting)
    ]
    if not jev_mode:
        return candidates
    # Only fallback passes can need a new Jev result. A pass bound to a
    # promoted result has already been assessed; if that result later goes
    # stale, the Hard Filters must run again before the Posting is qualified.
    # Compute the same binding as matching without preparing every Posting's
    # full Jev state (thousands of cases would time out the read-only plan).
    policy = profile.filters.jev
    if policy is None:
        raise ValueError("filters.jev is required for Jev")
    profile_hash = compile_profile(profile, as_of_month=month).profile_hash
    policy_hash = jev_policy_hash(policy)
    return [
        posting for posting in candidates
        if (facts := latest[(str(posting["key"]), "hard_filter")].get("facts")) is not None
        and isinstance(facts, dict)
        and facts.get("jev") == "fallback"
        and facts.get("jev_input") == _input_version_for(
            posting, profile_hash, policy_hash, month, JEV_RELEASE,
        )
    ]


@dataclass
class SelectedWork:
    prepared: list[PreparedJevCase]
    named_unassessed: list[dict[str, Any]]
    report: ExecutionReport


def select_work(
    postings: Sequence[Mapping[str, Any]],
    *,
    profile: Profile,
    month: str,
    mode: str,
    qualifications_dir: Path,
    named_keys: Sequence[str] | None,
    release: JevRelease,
    decided_at: str,
) -> SelectedWork:
    report = ExecutionReport()
    group_index: dict[str, object] = {}
    policy = profile.filters.jev
    if policy is None:
        raise ValueError("filters.jev is required for Jev")
    policy_digest = jev_policy_hash(policy)
    compiled = compile_profile(profile, as_of_month=month)
    profile_hash = compiled.profile_hash
    by_key = {str(item["key"]): item for item in postings if item.get("key")}
    if named_keys:
        missing = [key for key in named_keys if key not in by_key]
        if missing:
            raise ValueError(f"unknown posting_key: {missing[0]!r}")
        wanted = list(named_keys)
    else:
        wanted = [str(item["key"]) for item in postings if item.get("key")]
    input_versions = {
        key: _input_version_for(by_key[key], profile_hash, policy_digest, month, release)
        for key in wanted if key in by_key
    }
    current = _current_keys(
        qualifications_dir, profile.identifier, mode, release.qualifier_version, input_versions,
    )
    prepared: list[PreparedJevCase] = []
    named_unassessed: list[dict[str, Any]] = []
    for key in wanted:
        posting = by_key[key]
        if posting.get("listing_status") == "closed":
            report.skipped_closed += 1
            continue
        group = _group_key(posting, group_index)
        if is_protected(key, group, release.protected_group_hashes):
            report.skipped_protected += 1
            continue
        if key in current:
            report.current_assessments += 1
            continue
        prep = prepare_jev_case(
            posting, profile, month, release, group_index=group_index,
            compiled_profile=compiled,
        )
        if prep.reason is not None:
            if prep.reason == "snippet":
                report.snippets += 1
            elif prep.reason == "too_long":
                report.too_long += 1
            else:
                report.missing += 1
            if named_keys:
                named_unassessed.append(_unassessed_row(
                    posting_key=key,
                    profile_id=profile.identifier,
                    posting_revision=posting_revision(posting),
                    profile_hash=profile_hash,
                    policy_hash=policy_digest,
                    as_of_month_value=month,
                    qualifier_version=release.qualifier_version,
                    qualifier_identity=release.qualifier_identity,
                    input_version_value=input_versions[key],
                    mode=mode,
                    reason=prep.reason,
                    decided_at=decided_at,
                ))
            continue
        assert prep.case is not None
        prepared.append(prep.case)
    report.selected = len(prepared) + len(named_unassessed)
    return SelectedWork(prepared, named_unassessed, report)


def _promoted_fresh(qualifications_dir: Path, profile_id: str, accepted: str,
                    qualifier_version: str) -> bool:
    state = fold_events(promotion_events(qualifications_dir), profile_id)
    active = state.current(accepted, qualifier_kind=JEV_KIND)
    return active is not None and active.qualifier_version == qualifier_version


def _unit_scalars(result: JevResult) -> bool:
    return all(
        value is not None and 0.0 <= value <= 1.0
        for value in (result.fit_probability, result.fit_score)
    )


def _diagnostics(error: SystemOneError) -> tuple[str, ...]:
    flags = [f"code={error.code}"]
    if error.status is not None:
        flags.append(f"status={error.status}")
    if error.nbytes is not None:
        flags.append(f"bytes={error.nbytes}")
    if error.attempts:
        flags.append(f"attempts={error.attempts}")
    return tuple(flags)


@dataclass
class _CaseOutcome:
    row: dict[str, Any] | None = None
    incomplete: bool = False
    cache_hit: bool = False
    valid: bool = False
    decision: str | None = None
    unassessed_reason: str | None = None


def execute_prepared_cases(
    cases: Sequence[PreparedJevCase],
    *,
    qualifications_dir: Path,
    profile_id: str,
    mode: str,
    day: str,
    decided_at: str,
    offline: bool,
    max_usd: float | None,
    max_requests: int | None,
    poster: Poster | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    release: JevRelease | None = None,
    acquire_lock: bool = True,
    extra_rows: Sequence[Mapping[str, Any]] = (),
    pause_on_failure: bool = False,
) -> ExecutionReport:
    """Record prepared cases through the cache, transport and kind-aware store.

    Preview is the caller skipping this function. Live execution runs cache
    hits on this thread and posts uncached cases on a pool of at most
    ``MAX_IN_FLIGHT`` workers. ``max_usd`` and ``max_requests`` are optional;
    absent means no cap.
    """
    packaged = release or JEV_RELEASE
    report = ExecutionReport(selected=len(cases) + len(extra_rows))
    live_usd = None if offline else max_usd
    live_requests = None if offline else max_requests
    budget = Budget(live_usd, live_requests)
    lock: QualificationLock | None = None
    if acquire_lock:
        lock = QualificationLock(qualifications_dir)
        try:
            lock.acquire()
        except ContentionError:
            report.contention = True
            report.incomplete = len(cases) + len(extra_rows)
            return report
    http_429s = 0
    count_lock = threading.Lock()
    send = poster if poster is not None else httpx_poster

    def counting_poster(request: SystemOneRequest) -> SystemOneHttpResult:
        nonlocal http_429s
        result = send(request)
        if result.status == 429:
            with count_lock:
                http_429s += 1
        return result

    def unassessed_for(
        bindings: Any,
        reason: str,
        flags: Sequence[str] = (),
    ) -> dict[str, Any]:
        return _unassessed_row(
            posting_key=bindings.posting_key, profile_id=profile_id,
            posting_revision=bindings.posting_revision, profile_hash=bindings.profile_hash,
            policy_hash=bindings.policy_hash, as_of_month_value=bindings.as_of_month,
            qualifier_version=bindings.qualifier_version,
            qualifier_identity=packaged.qualifier_identity,
            input_version_value=bindings.input_version, mode=mode,
            reason=reason, decided_at=decided_at,
            request_key=bindings.request_key, state_sha256=bindings.state_sha256,
            questions_sha256=bindings.questions_sha256,
            diagnostic_flags=tuple(flags),
        )

    def assess_one(case: PreparedJevCase) -> _CaseOutcome:
        bindings = case.bindings
        if mode == "promoted" and not _promoted_fresh(
            qualifications_dir, profile_id, bindings.accepted_profile_hash,
            bindings.qualifier_version,
        ):
            return _CaseOutcome(incomplete=True)
        try:
            cached = read_response(qualifications_dir, bindings.request_key)
        except CacheCorruptError:
            return _CaseOutcome(
                row=unassessed_for(bindings, "response_invalid", ("cache_corrupt",)),
                unassessed_reason="response_invalid",
            )
        except CacheEscapeError:
            return _CaseOutcome(incomplete=True)
        raw: str | None = None
        cache_hit = False
        latency_ms = 0.0
        usage: dict[str, int] = {}
        if cached is not None:
            raw = str(cached["raw_response"])
            cache_hit = True
            latency_ms = float(cached["latency_ms"])
            usage = dict(cached["usage"])
        elif budget.auth_stopped():
            return _CaseOutcome(
                row=unassessed_for(bindings, "credentials_missing"),
                unassessed_reason="credentials_missing",
            )
        elif offline:
            return _CaseOutcome(incomplete=True)
        else:
            payload = {
                "state": dict(case.state),
                "model": MODEL,
                "questions": dict(case.questions),
            }
            try:
                success = post_systemone(
                    payload,
                    environ=environ,
                    poster=counting_poster,
                    clock=clock,
                    remaining_requests=MAX_RETRY_ATTEMPTS,
                    case_deadline=CASE_DEADLINE,
                    before_start=budget.start,
                )
            except SystemOneError as error:
                if error.code in {"auth_failed", "credentials_missing"}:
                    budget.note_auth_failed()
                    reason = "credentials_missing"
                elif budget.auth_stopped():
                    reason = "credentials_missing"
                else:
                    reason = REASON_FROM_TRANSPORT.get(error.code, "transport_failed")
                    if error.code == "budget_exhausted" and error.attempts == 0:
                        reason = "budget_exhausted"
                return _CaseOutcome(
                    row=unassessed_for(bindings, reason, _diagnostics(error)),
                    unassessed_reason=reason,
                )
            raw = success.raw_response
            latency_ms = success.latency_ms
            usage = dict(success.usage)
        assert raw is not None
        validation = validate_jev_response(case, raw)
        if not validation.passed or validation.response is None:
            codes = validation.error_codes
            reason = "model_mismatch" if "model_mismatch" in codes else "response_invalid"
            if not cache_hit and type(usage.get("input_tokens")) is int:
                budget.observe_success(int(usage["input_tokens"]))
            return _CaseOutcome(
                row=unassessed_for(
                    bindings, reason, tuple(f"validator={code}" for code in codes),
                ),
                unassessed_reason=reason,
            )
        result = reduce_jev(case, validation, packaged)
        if result.decision != "unassessed" and not _unit_scalars(result):
            # Validator 3 rescales or refuses every distribution, so a body
            # cannot reach here. The store and the view's DDL both require
            # the unit interval; a reducer change must not slip past them.
            return _CaseOutcome(
                row=unassessed_for(bindings, "response_invalid", ("scores_out_of_range",)),
                unassessed_reason="response_invalid",
            )
        if result.decision == "unassessed":
            return _CaseOutcome(
                row=unassessed_for(bindings, "response_invalid"),
                unassessed_reason="response_invalid",
            )
        tokens = validation.response.usage_input_tokens
        packed_usage = {
            "input_tokens": int(usage.get("input_tokens", tokens)),
            "output_tokens": int(usage.get("output_tokens", validation.response.usage_output_tokens)),
        }
        if not cache_hit:
            budget.observe_success(packed_usage["input_tokens"])
            try:
                publish_response(
                    qualifications_dir,
                    request_key=bindings.request_key,
                    state_sha256=bindings.state_sha256,
                    questions_sha256=bindings.questions_sha256,
                    raw_response=raw,
                    usage=packed_usage,
                    latency_ms=latency_ms,
                    received_at=decided_at,
                )
            except CacheIdentityError:
                return _CaseOutcome(incomplete=True)
            except CacheCorruptError:
                return _CaseOutcome(
                    row=unassessed_for(bindings, "response_invalid", ("cache_corrupt",)),
                    unassessed_reason="response_invalid",
                )
        if mode == "promoted" and not _promoted_fresh(
            qualifications_dir, profile_id, bindings.accepted_profile_hash,
            bindings.qualifier_version,
        ):
            return _CaseOutcome(incomplete=True)
        return _CaseOutcome(
            row=_success_row(
                case, result, validation, profile_id=profile_id, mode=mode,
                decided_at=decided_at, cache_hit=cache_hit, latency_ms=latency_ms,
                usage=packed_usage, release=packaged,
            ),
            cache_hit=cache_hit,
            valid=True,
            decision=str(result.decision),
        )

    try:
        for extra in extra_rows:
            reason = str(extra.get("reason") or "missing")
            _count(report.unassessed, reason)
        def needs_http(case: PreparedJevCase) -> bool:
            if offline:
                return False
            try:
                cached = read_response(qualifications_dir, case.bindings.request_key)
            except (CacheCorruptError, CacheEscapeError):
                return False
            return cached is None

        outcomes: list[_CaseOutcome | None] = [None] * len(cases)
        http_indices = [index for index, case in enumerate(cases) if needs_http(case)]
        local_indices = [
            index for index in range(len(cases)) if index not in set(http_indices)
        ]
        if pause_on_failure:
            # A pipeline run must not start a second paid request after a 402 or
            # transport failure. Keep earlier successes; leave the rest untouched.
            for index, case in enumerate(cases):
                outcome = assess_one(case)
                if outcome.unassessed_reason in {
                    "credentials_missing", "budget_exhausted", "transport_failed",
                    "out_of_credit",
                } and index in http_indices:
                    report.paused = {
                        "credentials_missing": "no-key" if budget.attempts == 0 else "account-unusable",
                        "budget_exhausted": "cap-reached",
                        "transport_failed": "transport-failed",
                        "out_of_credit": "out-of-credit",
                    }[outcome.unassessed_reason]
                    report.waiting = len(cases) - index
                    break
                outcomes[index] = outcome
        else:
            for index in local_indices:
                outcomes[index] = assess_one(cases[index])
        if http_indices and not pause_on_failure:
            before_soft, before_hard, after_soft, after_hard, raised = (
                raise_open_file_limit()
            )
            workers = min(MAX_IN_FLIGHT, len(http_indices))
            print(
                "jev_pool"
                f" workers={workers} http_cases={len(http_indices)}"
                f" rlimit_nofile before_soft={before_soft} before_hard={before_hard}"
                f" after_soft={after_soft} after_hard={after_hard} raised={str(raised).lower()}",
                file=sys.stderr,
            )
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {
                    pool.submit(assess_one, cases[index]): index for index in http_indices
                }
                for future, index in futures.items():
                    outcomes[index] = future.result()
        rows: list[dict[str, Any]] = [dict(row) for row in extra_rows]
        for outcome in outcomes:
            if outcome is None or outcome.incomplete:
                report.incomplete += 1
                continue
            if outcome.row is None:
                report.incomplete += 1
                continue
            rows.append(outcome.row)
            if outcome.valid:
                report.valid_responses += 1
                if outcome.decision is not None:
                    _count(report.decisions, outcome.decision)
            if outcome.cache_hit:
                report.cache_hits += 1
            if outcome.unassessed_reason is not None:
                _count(report.unassessed, outcome.unassessed_reason)
        if rows:
            append_rows(qualifications_dir, profile_id, rows, day=day)
        report.http_attempts = budget.attempts
        report.input_tokens = budget.input_tokens
        report.accounted_usd = budget.accounted
        report.http_429s = http_429s
    finally:
        if lock is not None:
            lock.release()
    return report


def _print_report(report: ExecutionReport, stream: TextIO | None = None) -> None:
    print(json.dumps(report.as_dict(), sort_keys=True, ensure_ascii=False),
          file=sys.stdout if stream is None else stream)


def run(
    *,
    profile: Profile,
    month: str,
    as_of: str,
    mode: str,
    postings_dir: Path,
    qualifications_dir: Path,
    named_keys: Sequence[str],
    hard_filter_passes_only: bool,
    decisions_dir: Path,
    execute: bool,
    offline: bool,
    max_usd: float | None,
    max_requests: int | None,
    poster: Poster | None = None,
    environ: Mapping[str, str] | None = None,
    clock: Clock | None = None,
    day: str | None = None,
    decided_at: str | None = None,
    pause_on_failure: bool = False,
) -> ExecutionReport:
    if profile.filters.jev is None:
        raise ValueError("filters.jev is required for Jev")
    postings = load_postings(postings_dir)
    if hard_filter_passes_only:
        postings = hard_filter_passes(postings, decisions_dir, profile, month, qualifications_dir)
    now = decided_at or f"{as_of}T00:00:00+00:00"
    today = day or as_of
    work = select_work(
        postings, profile=profile, month=month, mode=mode,
        qualifications_dir=qualifications_dir, named_keys=named_keys or None,
        release=JEV_RELEASE, decided_at=now,
    )
    if not execute:
        return work.report
    if mode == "promoted":
        accepted = jev_accepted_profile_hash(
            compile_profile(profile, as_of_month=month).profile_hash,
            jev_policy_hash(profile.filters.jev),
        )
        if not _promoted_fresh(
            qualifications_dir, profile.identifier, accepted, JEV_RELEASE.qualifier_version,
        ):
            raise ValueError("promoted execution requires an active, non-stale Jev activation")
    executed = execute_prepared_cases(
        work.prepared,
        qualifications_dir=qualifications_dir,
        profile_id=profile.identifier,
        mode=mode,
        day=today,
        decided_at=now,
        offline=offline,
        max_usd=max_usd,
        max_requests=max_requests,
        poster=poster,
        environ=environ,
        clock=clock,
        extra_rows=work.named_unassessed,
        acquire_lock=True,
        pause_on_failure=pause_on_failure,
    )
    executed.skipped_protected = work.report.skipped_protected
    executed.skipped_closed = work.report.skipped_closed
    executed.current_assessments = work.report.current_assessments
    executed.snippets = work.report.snippets
    executed.missing = work.report.missing
    executed.too_long = work.report.too_long
    executed.selected = work.report.selected
    return executed


def main(argv: Sequence[str] | None = None, *, poster: Poster | None = None,
         environ: Mapping[str, str] | None = None, clock: Clock | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_profile_argument(parser)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--mode", choices=("shadow", "promoted"), default="shadow")
    parser.add_argument("--posting-key", action="append", default=[])
    parser.add_argument(
        "--hard-filter-passes",
        action="store_true",
        help="select only Postings with a current passing Hard Filter Decision",
    )
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--pipeline", action="store_true", help="pause cleanly on unavailable Jev service")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--max-usd", type=float)
    parser.add_argument("--max-requests", type=int)
    parser.add_argument("--postings-dir", type=Path, help=STORE_HELP)
    parser.add_argument("--decisions-dir", type=Path, help=STORE_HELP)
    parser.add_argument("--qualifications-dir", type=Path, help=STORE_HELP)
    args = parser.parse_args(argv)
    if args.offline and not args.execute:
        parser.error("--offline requires --execute")
    if args.max_usd is not None and not args.execute:
        parser.error("--max-usd requires --execute")
    if args.max_requests is not None and not args.execute:
        parser.error("--max-requests requires --execute")
    if args.max_usd is not None and (not math.isfinite(args.max_usd) or args.max_usd < 0):
        parser.error("--max-usd must be a finite nonnegative number")
    if args.max_requests is not None and args.max_requests < 0:
        parser.error("--max-requests must be a nonnegative integer")
    try:
        month = as_of_month(args.as_of)
        profile = profile_from_arguments(args)
        announce_profile(profile)
        stores = resolve_store_paths(
            postings_dir=args.postings_dir,
            decisions_dir=args.decisions_dir,
            qualifications_dir=args.qualifications_dir,
        )
        print(
            f"as_of={args.as_of} Profile={profile.identifier} "
            f"root={stores['qualifications_dir'].parent.parent}",
            file=sys.stderr,
        )
        cap = args.max_usd
        if args.pipeline and cap is None:
            cap = float((os.environ if environ is None else environ).get("VENATOR_JEV_MAX_USD", "10"))
            if not math.isfinite(cap) or cap < 0:
                raise ValueError("VENATOR_JEV_MAX_USD must be a finite nonnegative number")
        if args.pipeline and profile.filters.jev is None:
            _print_report(ExecutionReport())
            return 0
        report = run(
            profile=profile,
            month=month,
            as_of=args.as_of,
            mode=current_mode(profile, month, stores["qualifications_dir"]) if args.pipeline else args.mode,
            postings_dir=stores["postings_dir"],
            qualifications_dir=stores["qualifications_dir"],
            named_keys=args.posting_key,
            hard_filter_passes_only=args.hard_filter_passes,
            decisions_dir=stores["decisions_dir"],
            execute=args.execute,
            offline=args.offline,
            max_usd=cap,
            max_requests=args.max_requests,
            poster=poster,
            environ=os.environ if environ is None else environ,
            clock=clock,
            day=args.as_of,
            pause_on_failure=args.pipeline,
        )
    except ContentionError:
        parser.exit(1, "qualification root is already locked\n")
    except (OSError, ValueError, KeyError) as error:
        parser.error(str(error))
    _print_report(report)
    # SPEC-jev §8.1 and §6.2: a selected case with no successful assessment is
    # incomplete, and a recorded failure does not make it complete.
    unassessed = sum(report.unassessed.values())
    return 1 if report.contention or (not args.pipeline and (report.incomplete or unassessed)) else 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Refresh every board and drain eligible Workday descriptions.

Direct boards are refreshed as snapshots.  Existing keys are observed again,
and keys missing from a complete snapshot become ``unknown``. Failed or
partial boards leave their previous state in place. Terminal and scheduled runs
walk Workday boards to their reported totals and drain eligible details; the
explicit interactive mode stays bounded.

Usage:
    python -m venator.discover.run [--profile NAME]
    python -m venator.discover.run --probe greenhouse:ginkgobioworks
"""

from __future__ import annotations

import argparse
from collections import deque
from collections.abc import Mapping, Sequence
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from venator.discover.adapters import (
    ADAPTERS,
    ENRICHERS,
    DiscoveryError,
    ExactJobClosed,
    PartialBoardError,
    PostingBatch,
    WORKDAY_PAGE_LIMIT,
    WorkdayClient,
    WorkdayTenantBlocked,
    WorkdayTenantThrottled,
    enrich_workday,
    fetch_workday,
    fetch_workday_window,
    parse_workday_board,
    transient_network_error,
)
from venator.discover.store import (
    append_detail_observations,
    current_postings,
    refresh_postings,
    update_source_health,
)
from venator.discover.progress import (
    get_source_progress,
    resume_offset,
    update_source_progress,
)
from venator.match.filters import apply_filters
from venator.paths import STORE_HELP, StoreRootError, resolve_store_paths
from venator.profile import (
    Profile,
    ProfileError,
    add_profile_argument,
    announce_profile,
    profile_from_arguments,
)


DETAIL_REFRESH_AFTER = timedelta(days=7)
DETAIL_RETRY_AFTER = timedelta(hours=6)
# The dashboard stays a short pass. A terminal or scheduled run has no listing
# or detail cap and drains the Hard-Filter-passing backlog instead.
INTERACTIVE_LISTING_PAGES = 3
INTERACTIVE_DETAILS_PER_BOARD = 20
WORKDAY_TENANT_WORKERS = 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_time(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None else None
    except ValueError:
        return None


def _detail_is_due(posting: Mapping[str, Any], *, now: datetime) -> bool:
    """Whether a Workday detail is missing or explicitly past its refresh age."""

    attempted = _parse_time(posting.get("detail_attempted_at"))
    # Failed checks keep the old full description. They need the same retry
    # interval as missing descriptions, rather than spending every run's budget.
    if attempted is not None and timedelta(0) <= now - attempted < DETAIL_RETRY_AFTER:
        return False
    if posting.get("detail_verification_status") in {"unknown", "failed"}:
        return True
    description = posting.get("description_html")
    kind = posting.get("description_kind")
    if not description or kind in {None, "missing", "snippet"}:
        return True
    # Legacy rows have no detail clock.  Treat their existing full text as
    # a bounded refresh candidate so their source facts eventually get
    # revalidated.  The per-board cap keeps an upgrade from issuing thousands
    # of detail requests on its first launch.  Newly enriched rows carry this
    # clock and are ordered by age below.
    stamp = posting.get("detail_verified_at") or posting.get("last_detail_verified_at")
    when = _parse_time(stamp)
    return when is None or when > now or now - when >= DETAIL_REFRESH_AFTER


def _ordered_detail_candidates(
    source: str,
    postings: Sequence[Mapping[str, Any]],
    existing: Mapping[str, Mapping[str, Any]],
    *,
    now: datetime,
    limit: int | None = None,
    profile: Profile | None = None,
) -> list[Mapping[str, Any]]:
    if source != "workday":
        return []
    candidates = [
        posting
        for posting in postings
        if isinstance(posting.get("key"), str)
        and posting.get("listing_status") != "closed"
        and (
            posting["key"] not in existing
            or _detail_is_due(existing[posting["key"]], now=now)
        )
        and (
            profile is None
            or apply_filters(dict(posting), profile.filters).verdict != "kill"
        )
    ]

    def priority(posting: Mapping[str, Any]) -> tuple[bool, bool, float, str]:
        key = str(posting.get("key", ""))
        prior = existing.get(key)
        if prior is None:
            return (False, False, 0.0, key)
        clocks = [_parse_time(prior.get(name)) for name in (
            "detail_attempted_at", "detail_verified_at", "last_detail_verified_at",
        )]
        stamp = max((clock for clock in clocks if clock is not None and clock <= now), default=None)
        has_full_text = bool(prior.get("description_html")) and prior.get("description_kind") not in {"missing", "snippet"}
        # Never-attempted missing descriptions first, then the least recently
        # attempted. Board round-robin happens in the drain, outside this sort.
        return (stamp is not None, has_full_text, stamp.timestamp() if stamp else 0.0, key)

    ordered = sorted(candidates, key=priority)
    return ordered if limit is None else ordered[: max(0, limit)]


def probe(spec: str) -> None:
    source, board = spec.split(":", 1)
    postings = ADAPTERS[source](board)
    print(f"{spec}: {len(postings)} postings")
    for posting in postings[:5]:
        print(f"  {posting['title']}  [{posting['location']}]")


def _adapter_result(value: object) -> tuple[list[dict], str, bool, str | None]:
    if isinstance(value, PostingBatch):
        # ``stalled`` describes why a bounded Workday window stopped; the
        # source-health contract records that as a partial observation while
        # the raw batch still carries ``stalled=True`` for cursor persistence.
        status = "partial" if value.status == "stalled" else value.status
        return list(value), status, value.complete, value.error
    if not isinstance(value, list):
        raise DiscoveryError("adapter returned a non-list result")
    return list(value), "ok", True, None


def _refresh_workday_details(
    postings_dir: Path,
    postings: list[dict],
    *,
    source: str,
    board: str | None = None,
    now: datetime,
    profile: Profile | None = None,
    limit: int | None = None,
) -> tuple[list[dict], set[str], int]:
    """Fetch at most the bounded set of due Workday details.

    The listing window is only one slice of a large board.  Detail candidates
    therefore come from both that slice and the current stored Workday rows.
    Rows outside the slice are returned only when a detail attempt actually
    touched them; the store can record their retry clock without pretending
    that their listing fields were observed in this window.
    """

    enrich = ENRICHERS.get(source)
    if enrich is None:
        return postings, set(), 0
    existing = current_postings(postings_dir)
    fetched_keys = {str(posting.get("key")) for posting in postings}
    candidate_pool = list(postings)
    candidate_board = board if board is not None else (postings[0].get("board") if postings else None)
    candidate_pool.extend(
        prior
        for key, prior in existing.items()
        if key not in fetched_keys
        and prior.get("source") == source
        and prior.get("board") == candidate_board
    )
    if limit is None:
        candidates = _ordered_detail_candidates(source, candidate_pool, existing, now=now, profile=profile)
    else:
        candidates = _ordered_detail_candidates(
            source,
            candidate_pool,
            existing,
            now=now,
            limit=max(0, limit),
            profile=profile,
        )
    by_key = {str(posting.get("key")): posting for posting in postings}
    detail_only: dict[str, dict] = {}
    closed: set[str] = set()
    failures = 0
    verified_at = now.isoformat(timespec="seconds")
    session = None
    if enrich is enrich_workday and candidate_board:
        session = WorkdayClient(parse_workday_board(str(candidate_board))[0])
    for candidate in candidates:
        key = str(candidate.get("key"))
        prior = existing.get(key)
        attempted_at = now.isoformat(timespec="seconds")
        stop_tenant = False
        try:
            detailed = dict(
                enrich_workday(candidate, session=session)
                if session is not None
                else enrich(candidate)
            )
        except ExactJobClosed:
            closed.add(key)
            if prior:
                result = dict(
                    prior,
                    listing_status="closed",
                    verification_status="verified",
                    detail_attempted_at=attempted_at,
                )
            else:
                result = dict(
                    candidate,
                    listing_status="closed",
                    verification_status="verified",
                    detail_attempted_at=attempted_at,
                )
            if key in fetched_keys:
                by_key[key] = result
            else:
                detail_only[key] = result
            continue
        except Exception as error:
            failures += 1
            stop_tenant = isinstance(error, (WorkdayTenantBlocked, WorkdayTenantThrottled))
            # Keep the prior full description and surface that the detail check
            # did not verify it.  The store's merge makes this sticky.
            fallback = dict(candidate)
            if prior:
                fallback = dict(prior, **fallback)
            fallback["verification_status"] = "unknown"
            fallback["detail_verification_status"] = "unknown"
            fallback["verification_error"] = " ".join(str(error).split())[:300]
            fallback["detail_attempted_at"] = attempted_at
            if key in fetched_keys:
                by_key[key] = fallback
            else:
                detail_only[key] = fallback
            if stop_tenant:
                break
            continue
        detailed["detail_verified_at"] = verified_at
        detailed["detail_attempted_at"] = attempted_at
        detailed["detail_verification_status"] = "verified"
        detailed["verification_status"] = "verified"
        # An exact 200 response for the requested job identity is presence
        # evidence even when the detail payload omits a separate lifecycle
        # flag.  This is especially important for legacy stored rows whose
        # old listing observation was ``unknown``.
        detailed["listing_status"] = "open"
        if key in fetched_keys:
            by_key[key] = detailed
        else:
            detail_only[key] = detailed
    if session is not None:
        session.close()
    # Input order is source order, which keeps append-only diffs easy to read.
    result = [by_key[str(posting.get("key"))] for posting in postings]
    # Keep stored-only detail observations after the listing slice. Failed
    # attempts are marked ``detail_only`` by the caller so their listing
    # lifecycle clocks remain intact; successful exact details are omitted
    # from that set and can establish a fresh open observation.
    result.extend(detail_only.values())
    return result, closed, failures


def _record_workday_progress(
    postings_dir: Path,
    board: str,
    before: Mapping[str, Any],
    batch: PostingBatch | PartialBoardError | None,
    *,
    attempted_at: str,
    error: object = None,
) -> dict[str, Any] | None:
    """Commit a listing window's safe cursor after its Posting rows are saved."""

    if batch is None:
        return None
    pages = int(getattr(batch, "pages_fetched", 0) or 0)
    raw_next = getattr(batch, "next_offset", None)
    if raw_next is None:
        return None
    next_offset = max(0, int(raw_next))
    scan_complete = bool(getattr(batch, "scan_complete", False))
    stalled = bool(getattr(batch, "stalled", False))
    status = "stalled" if stalled else ("complete" if scan_complete else "partial")
    progress_at = attempted_at if pages else before.get("last_progress_at")
    batch_error = getattr(batch, "error", None)
    message = error if error not in (None, "") else batch_error
    if message is not None:
        message = " ".join(str(message).split())[:300]
    return update_source_progress(
        postings_dir,
        f"workday:{board}",
        next_offset=0 if scan_complete else next_offset,
        page_limit=WORKDAY_PAGE_LIMIT,
        status=status,
        last_attempt_at=attempted_at,
        last_progress_at=progress_at,
        last_error=message,
        last_page_signature=getattr(batch, "last_page_signature", None) or before.get("last_page_signature"),
    )


def _workday_progress_fetcher(
    postings_dir: Path, board: str, *, max_pages: int | None = None,
) -> tuple[PostingBatch, dict[str, Any]]:
    """Load the cursor and fetch one bounded Workday listing window."""

    before = get_source_progress(postings_dir, f"workday:{board}")
    start_offset, _expired = resume_offset(before, expires_after=None)
    previous_signature = before.get("last_page_signature") if start_offset else None
    return fetch_workday_window(
        board,
        start_offset=start_offset,
        max_pages=max_pages,
        limit=WORKDAY_PAGE_LIMIT,
        previous_page_signature=previous_signature,
    ), before


def _process_board(
    postings_dir: Path,
    source: str,
    board: str,
    fetcher: Any,
    *,
    profile: Profile | None = None,
    detail_limit: int | None = None,
    listing_pages: int | None = None,
    retryable_failures: set[tuple[str, str]] | None = None,
) -> tuple[int, int, str]:
    """Fetch, bound detail work, append observations, and update health."""

    attempted_at = _now()
    progress_before: dict[str, Any] | None = None
    progress_batch: PostingBatch | PartialBoardError | None = None
    resumable_workday = source == "workday" and fetcher is fetch_workday
    try:
        if resumable_workday:
            fetched, progress_before = _workday_progress_fetcher(postings_dir, board, max_pages=listing_pages)
            progress_batch = fetched
        else:
            fetched = fetcher(board)
            if source == "workday" and isinstance(fetched, PostingBatch):
                progress_batch = fetched
        postings, status, complete, adapter_error = _adapter_result(fetched)
        if retryable_failures is not None and getattr(fetched, "transient_failure", False):
            retryable_failures.add((source, board))
    except PartialBoardError as error:
        postings = list(error.postings)
        status, complete, adapter_error = "partial", False, str(error)
        if retryable_failures is not None and transient_network_error(error):
            retryable_failures.add((source, board))
        if source == "workday":
            progress_batch = error
            if progress_before is None:
                try:
                    progress_before = get_source_progress(postings_dir, f"workday:{board}")
                except Exception:
                    progress_before = None
    except Exception as error:
        if retryable_failures is not None and transient_network_error(error):
            retryable_failures.add((source, board))
        if source == "workday":
            try:
                before = progress_before or get_source_progress(postings_dir, f"workday:{board}")
                update_source_progress(
                    postings_dir,
                    f"workday:{board}",
                    next_offset=before.get("next_offset", 0),
                    status="failed",
                    last_attempt_at=attempted_at,
                    last_error=" ".join(str(error).split())[:300],
                )
            except Exception:
                # A progress metadata problem must never mask the original
                # source failure or write a misleading cursor.
                pass
        update_source_health(
            postings_dir,
            f"{source}:{board}",
            status="failed",
            count=0,
            error=error,
            attempted_at=attempted_at,
        )
        print(f"{source}:{board}: FAILED — {error}")
        return 0, 0, "failed"

    # Direct ATS feeds include the full posting in the successful listing
    # response. Mark that observation explicitly so coverage doesn't require a
    # redundant exact-job request merely to recognize already fetched details.
    if source in {"greenhouse", "lever", "ashby", "smartrecruiters"}:
        postings = [
            dict(posting, verification_status=posting.get("verification_status", "verified"))
            for posting in postings
        ]

    closed: set[str] = set()
    detail_failures = 0
    listing_keys = {str(posting.get("key")) for posting in postings}
    detail_only_keys: set[str] = set()
    if source == "workday":
        postings, closed, detail_failures = _refresh_workday_details(
            postings_dir,
            postings,
            source=source,
            board=board,
            now=datetime.now(timezone.utc),
            profile=profile,
            limit=detail_limit,
        )
        detail_only_keys = {
            str(posting.get("key")) for posting in postings
            if str(posting.get("key")) not in listing_keys
            and posting.get("detail_verification_status") != "verified"
        } - closed
        if detail_failures and status == "ok":
            # The board listing succeeded, but some exact descriptions did not
            # verify.  Keep the board open while making the issue visible.
            adapter_error = f"{detail_failures} detail refresh(es) unavailable"
            status = "partial"

    summary = refresh_postings(
        postings_dir,
        postings,
        source=source,
        board=board,
        complete=complete,
        status=status,
        closed_keys=closed,
        error=adapter_error,
        detail_only_keys=detail_only_keys,
    )
    if source == "workday" and progress_before is not None:
        _record_workday_progress(
            postings_dir,
            board,
            progress_before,
            progress_batch,
            attempted_at=attempted_at,
            error=adapter_error,
        )
    update_source_health(
        postings_dir,
        f"{source}:{board}",
        status=summary.status,
        count=len(postings),
        error=adapter_error,
        attempted_at=attempted_at,
    )
    print(
        f"{source}:{board}: {len(postings)} postings, {summary.new} new, "
        f"{summary.refreshed} refreshed, {summary.unknown} unknown"
        + (f", {summary.closed} closed" if summary.closed else "")
        + (f" ({summary.status})" if summary.status != "ok" else "")
    )
    return summary.new, summary.appended, summary.status


@dataclass
class _TenantDrain:
    host: str
    session: WorkdayClient
    candidates: deque[dict[str, Any]]


def _drain_workday_details(
    postings_dir: Path,
    profile: Profile,
    boards: Sequence[str],
    *,
    candidate_cap: int | None = None,
) -> dict[str, int]:
    """Drain eligible detail work fairly across paced tenant sessions."""

    existing = current_postings(postings_dir)
    now = datetime.now(timezone.utc)
    board_queues: dict[str, deque[dict[str, Any]]] = {}
    for board in boards:
        rows = [
            row for row in existing.values()
            if row.get("source") == "workday" and row.get("board") == board
        ]
        ordered = _ordered_detail_candidates(
            "workday", rows, existing, now=now, profile=profile,
        )
        if ordered:
            board_queues[board] = deque(dict(row) for row in ordered)

    by_host: dict[str, deque[dict[str, Any]]] = {}
    # One Posting per board per turn prevents a large board from hiding every
    # other employer behind its backlog. Boards on one host share one session.
    while board_queues:
        for board in list(board_queues):
            candidate = board_queues[board].popleft()
            host = parse_workday_board(board)[0]
            by_host.setdefault(host, deque()).append(candidate)
            if not board_queues[board]:
                del board_queues[board]

    all_states = [
        _TenantDrain(host, WorkdayClient(host), candidates)
        for host, candidates in by_host.items()
    ]
    states = deque(all_states)
    submitted = verified = closed = failures = 0
    inflight: dict[Future[dict], tuple[_TenantDrain, dict[str, Any]]] = {}
    buffered: list[dict[str, Any]] = []
    buffered_closed: set[str] = set()
    buffered_detail_only: set[str] = set()

    def flush() -> None:
        if not buffered:
            return
        append_detail_observations(
            postings_dir,
            buffered,
            closed_keys=buffered_closed,
            detail_only_keys=buffered_detail_only,
        )
        buffered.clear()
        buffered_closed.clear()
        buffered_detail_only.clear()

    def can_submit() -> bool:
        return candidate_cap is None or submitted < max(0, candidate_cap)

    def submit_next(executor: ThreadPoolExecutor, state: _TenantDrain) -> None:
        nonlocal submitted
        candidate = state.candidates.popleft()
        future = executor.submit(enrich_workday, candidate, session=state.session)
        inflight[future] = (state, candidate)
        submitted += 1

    try:
        with ThreadPoolExecutor(max_workers=WORKDAY_TENANT_WORKERS) as executor:
            while states and len(inflight) < WORKDAY_TENANT_WORKERS and can_submit():
                submit_next(executor, states.popleft())
            while inflight:
                done, _pending = wait(inflight, return_when=FIRST_COMPLETED)
                for future in done:
                    state, candidate = inflight.pop(future)
                    key = str(candidate["key"])
                    attempted_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
                    prior = existing.get(key)
                    detail_only: set[str] = set()
                    closed_keys: set[str] = set()
                    stop_tenant = False
                    try:
                        row = dict(future.result())
                    except ExactJobClosed:
                        closed += 1
                        closed_keys.add(key)
                        row = dict(prior or candidate)
                        row.update(
                            listing_status="closed",
                            verification_status="verified",
                            detail_attempted_at=attempted_at,
                        )
                    except (WorkdayTenantBlocked, WorkdayTenantThrottled) as error:
                        failures += 1
                        stop_tenant = True
                        row = dict(prior or candidate)
                        row.update(
                            verification_status="unknown",
                            detail_verification_status="unknown",
                            verification_error=" ".join(str(error).split())[:300],
                            detail_attempted_at=attempted_at,
                        )
                        detail_only.add(key)
                    except Exception as error:
                        failures += 1
                        row = dict(prior or candidate)
                        row.update(
                            verification_status="unknown",
                            detail_verification_status="unknown",
                            verification_error=" ".join(str(error).split())[:300],
                            detail_attempted_at=attempted_at,
                        )
                        detail_only.add(key)
                    else:
                        verified += 1
                        row.update(
                            detail_verified_at=attempted_at,
                            detail_attempted_at=attempted_at,
                            detail_verification_status="verified",
                            verification_status="verified",
                            listing_status="open",
                        )
                    buffered.append(row)
                    buffered_closed.update(closed_keys)
                    buffered_detail_only.update(detail_only)
                    if len(buffered) >= 100:
                        flush()
                    existing[key] = row
                    if stop_tenant:
                        state.candidates.clear()
                    elif state.candidates and can_submit():
                        states.append(state)
                while states and len(inflight) < WORKDAY_TENANT_WORKERS and can_submit():
                    submit_next(executor, states.popleft())
            flush()
    finally:
        for state in all_states:
            state.session.close()

    request_count = sum(state.session.requests for state in all_states)
    rate_limits = sum(state.session.rate_limits for state in all_states)
    blocks = sum(state.session.blocks for state in all_states)
    print(
        f"workday detail drain: {request_count} requests, {verified} descriptions, "
        f"{closed} closed, {failures} unavailable, {rate_limits} rate-limited, "
        f"{blocks} blocked"
    )
    return {
        "candidates": submitted,
        "verified": verified,
        "closed": closed,
        "failures": failures,
        "requests": request_count,
        "rate_limits": rate_limits,
        "blocks": blocks,
    }


def run(
    profile: Profile,
    postings_dir: Path | None = None,
    *,
    interactive: bool = False,
    detail_candidate_cap: int | None = None,
) -> dict[str, int]:
    postings_dir = resolve_store_paths(postings_dir=postings_dir)["postings_dir"]
    total_new = total_observations = 0

    if not profile.sources.configured:
        print(f"{profile.name}: no board is registered — add sources.boards to {profile.targeting_path}")
    board_total = sum(len(boards or []) for boards in profile.sources.boards.values())
    board_index = 0
    workday_boards = list(profile.sources.boards.get("workday", ()))
    retryable_failures: set[tuple[str, str]] = set()
    for source, boards in profile.sources.boards.items():
        for board in boards or []:
            board_index += 1
            name = profile.sources.names.get(board, board)
            # Fixed progress marker consumed by the dashboard; board names are
            # single-line text, never executable input or a stage instruction.
            name = " ".join(str(name).split())[:200]
            print(f"discover: employer {board_index}/{board_total} {name}", flush=True)
            new, observed, _status = _process_board(
                postings_dir,
                source,
                board,
                ADAPTERS[source],
                profile=profile,
                detail_limit=(INTERACTIVE_DETAILS_PER_BOARD if interactive else 0),
                listing_pages=(INTERACTIVE_LISTING_PAGES if interactive else None),
                retryable_failures=retryable_failures,
            )
            total_new += new
            total_observations += observed
    # Once every board has had its turn, give exhausted transient failures a
    # final pass. Definite answers (including 403/404/410) never enter this set.
    for source, board in sorted(retryable_failures):
        print(f"discover: retrying {source}:{board} after board pass", flush=True)
        new, observed, _status = _process_board(
            postings_dir, source, board, ADAPTERS[source], profile=profile,
            detail_limit=(INTERACTIVE_DETAILS_PER_BOARD if interactive else 0),
            listing_pages=(INTERACTIVE_LISTING_PAGES if interactive else None),
        )
        total_new += new
        total_observations += observed
    drain = {"candidates": 0, "verified": 0, "closed": 0, "failures": 0, "requests": 0}
    if not interactive and workday_boards:
        drain = _drain_workday_details(
            postings_dir,
            profile,
            workday_boards,
            candidate_cap=detail_candidate_cap,
        )
        total_observations += drain["candidates"]
    print(f"total new postings: {total_new}")
    return {
        "new": total_new,
        "observations": total_observations,
        "detail_requests": drain["requests"],
        "descriptions": drain["verified"],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe", help="test one board: source:token (no store write)")
    parser.add_argument("--postings-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="use the dashboard's bounded listing and detail windows",
    )
    parser.add_argument(
        "--max-detail-candidates",
        type=int,
        default=None,
        help="explicitly cap detail candidates for a bounded diagnostic run",
    )
    add_profile_argument(parser)
    args = parser.parse_args()
    if args.probe:
        probe(args.probe)
        return
    try:
        profile = profile_from_arguments(args)
    except ProfileError as error:
        parser.error(str(error))
    announce_profile(profile)
    try:
        stores = resolve_store_paths(postings_dir=args.postings_dir)
    except StoreRootError as error:
        parser.error(str(error))
    if args.max_detail_candidates is not None and args.max_detail_candidates < 0:
        parser.error("--max-detail-candidates must be zero or greater")
    run(
        profile,
        stores["postings_dir"],
        interactive=args.interactive,
        detail_candidate_cap=args.max_detail_candidates,
    )


if __name__ == "__main__":
    main()

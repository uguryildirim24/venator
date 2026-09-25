"""Read public ATS listings and preserve their source identities and content.

Greenhouse, Lever, Ashby and SmartRecruiters use public job-board APIs. Workday
uses the careers site's own CXS endpoints, whose shape varies by employer.
Its listing POST is a read-only search; all detail requests are GETs.
Partial or malformed feeds must never imply that missing jobs have closed.
"""

from __future__ import annotations

import errno
import hashlib
import json
import re
import socket
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import unquote

import httpx

from venator.discover.common import (
    actual_publication,
    canonical_opportunity_type,
    canonical_workplace_type,
    country_iso2,
    deadline,
    description_kind,
    first_text,
    html_text,
    location_text,
    normalize_locations,
    source_updated,
    text,
)

UA = {"User-Agent": "venator/0.1 (personal job-search agent)"}
TIMEOUT = 20.0
# Three attempts: two pauses (10s, 20s) plus at most three 20s timeouts.
# This spans roughly 90s without turning a brief DNS outage into a failed board.
NETWORK_RETRY_DELAYS = (10.0, 20.0)


def transient_network_error(error: BaseException) -> bool:
    """Only transport DNS, refused/reset connections and timeouts are retryable."""

    seen: set[int] = set()
    current: BaseException | None = error
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(
            current,
            (socket.gaierror, ConnectionRefusedError, ConnectionResetError,
             TimeoutError, httpx.TimeoutException),
        ):
            return True
        if isinstance(current, OSError) and current.errno in {
            errno.ECONNREFUSED, errno.ECONNRESET, errno.ETIMEDOUT,
        }:
            return True
        current = current.__cause__ or current.__context__
    return False


def _network_request(request: Callable[[], httpx.Response]) -> httpx.Response:
    for delay in NETWORK_RETRY_DELAYS:
        try:
            return request()
        except Exception as error:
            if not transient_network_error(error):
                raise
            time.sleep(delay)
    return request()


class DiscoveryError(RuntimeError):
    """A source response cannot be represented as a trustworthy board result."""

    def __init__(
        self,
        message: str,
        *,
        postings: list[dict] | None = None,
        partial: bool = False,
    ) -> None:
        super().__init__(message)
        self.postings = list(postings or [])
        self.partial = partial


class BoardResponseError(DiscoveryError):
    """A board returned malformed data or a response known to be incomplete."""


class PartialBoardError(BoardResponseError):
    """A board yielded a usable prefix but not a complete snapshot."""

    def __init__(
        self,
        message: str,
        postings: list[dict] | None = None,
        *,
        next_offset: int | None = None,
        start_offset: int = 0,
        pages_fetched: int = 0,
        postings_observed: int = 0,
        scan_complete: bool = False,
        stalled: bool = False,
        last_page_signature: str | None = None,
    ) -> None:
        super().__init__(message, postings=postings, partial=True)
        # These fields let a resumable caller persist only the portion whose
        # observations it successfully commits.  Existing callers still see
        # the same exception and ``postings`` list.
        self.next_offset = next_offset
        self.start_offset = start_offset
        self.pages_fetched = pages_fetched
        self.postings_observed = postings_observed
        self.scan_complete = scan_complete
        self.stalled = stalled
        self.last_page_signature = last_page_signature
        self.transient_failure = False


class ExactJobClosed(DiscoveryError):
    """An exact job endpoint gave definitive closure evidence (404/410/closed)."""

    definitive = True


class WorkdayTenantBlocked(DiscoveryError):
    """A Workday tenant refused the paced drain; rotate to another tenant."""


class WorkdayTenantThrottled(DiscoveryError):
    """A Workday tenant remained rate-limited after paced retries."""


class PostingBatch(list[dict]):
    """A list-compatible adapter result carrying board completeness metadata.

    Existing callers iterate and take ``len`` just as they did with a plain
    list.  The run layer reads ``complete`` and ``status`` to keep a partial
    source from being interpreted as a complete empty board.
    """

    def __init__(
        self,
        postings: list[dict] | None = None,
        *,
        complete: bool = True,
        status: str = "ok",
        error: str | None = None,
        next_offset: int | None = None,
        start_offset: int = 0,
        pages_fetched: int = 0,
        postings_observed: int | None = None,
        scan_complete: bool = False,
        stalled: bool = False,
        last_page_signature: str | None = None,
    ) -> None:
        super().__init__(postings or [])
        self.complete = complete
        self.status = status
        self.error = error
        self.start_offset = max(0, start_offset)
        self.next_offset = next_offset if next_offset is None else max(0, next_offset)
        self.pages_fetched = max(0, pages_fetched)
        self.postings_observed = len(self) if postings_observed is None else max(0, postings_observed)
        self.scan_complete = scan_complete
        self.stalled = stalled
        self.last_page_signature = last_page_signature
        self.transient_failure = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _source_facts(value: Mapping[str, Any]) -> dict[str, Any]:
    """Copy a JSON object so structured source fields survive normalization."""

    return dict(value)


def _listed_status(value: Mapping[str, Any]) -> str:
    """Treat a row returned by a successful board as present/open by default."""

    return "closed" if _listing_status(value) == "closed" else "open"


def _listing_status(value: Mapping[str, Any]) -> str:
    explicit = value.get("listing_status") or value.get("listingStatus") or value.get("status")
    if isinstance(explicit, str):
        lowered = explicit.casefold().replace("_", "-").strip()
        if lowered in {"closed", "archived", "inactive", "unlisted", "filled", "cancelled", "canceled"}:
            return "closed"
        if lowered in {"open", "active", "listed", "published"}:
            return "open"
    if value.get("isListed") is False or value.get("is_listed") is False:
        return "closed"
    if value.get("archivedAt") or value.get("archived_at"):
        return "closed"
    return "unknown"


def _posting(source: str, board: str, external_id: str, **fields) -> dict:
    posting = {
        "key": f"{source}:{board}:{external_id}",
        "source": source,
        "board": board,
        "external_id": external_id,
        "discovered_at": _now(),
        **fields,
    }
    # The defaults are part of the normalized contract.  The store adds the
    # observation lifecycle fields because only it can see prior observations.
    posting.setdefault("locations", normalize_locations(posting.get("location", "")))
    posting.setdefault("workplace_type", "unknown")
    posting.setdefault("description_kind", description_kind(posting.get("description_html")))
    posting.setdefault("opportunity_type", "job")
    posting.setdefault("apply_url", posting.get("url") or None)
    posting.setdefault("published_at", None)
    posting.setdefault("source_updated_at", None)
    posting.setdefault("application_deadline", None)
    posting.setdefault("listing_status", "unknown")
    return posting


def _json_response(response: httpx.Response, source: str, board: str) -> Any:
    if response.status_code >= 400:
        raise DiscoveryError(f"{source}:{board} answered HTTP {response.status_code}")
    try:
        return response.json()
    except ValueError as error:
        raise BoardResponseError(f"{source}:{board} returned invalid JSON") from error


def _get_json(url: str, source: str, board: str, **kwargs: Any) -> Any:
    return _json_response(_network_request(lambda: httpx.get(url, **kwargs)), source, board)


def _validate_entries(data: Any, *, source: str, board: str, key: str | None = None) -> list[Mapping[str, Any]]:
    if key is None:
        entries = data
    else:
        if not isinstance(data, Mapping):
            raise BoardResponseError(f"{source}:{board} returned a non-object board response")
        if key not in data:
            raise BoardResponseError(f"{source}:{board} response is missing its {key!r} list")
        entries = data[key]
    if not isinstance(entries, list):
        raise BoardResponseError(f"{source}:{board} response field {key or 'root'} is not a list")
    valid: list[Mapping[str, Any]] = []
    invalid = 0
    for entry in entries:
        if isinstance(entry, Mapping):
            valid.append(entry)
        else:
            invalid += 1
    if invalid:
        raise PartialBoardError(
            f"{source}:{board} response contained {invalid} malformed listing entr"
            f"{'y' if invalid == 1 else 'ies'}",
            [],
        )
    return valid


def _response_indicates_partial(data: object) -> bool:
    """Recognize explicit truncation markers without guessing from row count."""

    if not isinstance(data, Mapping):
        return False
    for key in ("partial", "isPartial", "truncated", "isTruncated", "incomplete"):
        if data.get(key) is True:
            return True
    for key in ("hasMore", "has_more", "more", "nextPage", "next_page", "next"):
        value = data.get(key)
        if value not in (None, False, "", 0, [], {}):
            return True
    pagination = data.get("pagination")
    if isinstance(pagination, Mapping):
        for key in ("hasMore", "has_more", "more"):
            if pagination.get(key) is True:
                return True
        if pagination.get("next") not in (None, False, "", 0, [], {}):
            return True
    return False


def _description_value(value: object) -> str:
    return text(value)


def _lever_list_markup(value: object) -> str:
    """Render Lever's structured ``lists`` field without dropping any text."""

    sections: list[str] = []
    if not isinstance(value, list):
        return ""
    for section in value:
        if isinstance(section, Mapping):
            heading = html_text(section.get("text") or section.get("title") or section.get("heading"))
            body = section.get("content")
            if isinstance(body, list):
                items = []
                for item in body:
                    if isinstance(item, Mapping):
                        item = item.get("text") or item.get("content") or item.get("value")
                    rendered = html_text(item)
                    if rendered:
                        items.append(f"<li>{rendered}</li>")
                content = f"<ul>{''.join(items)}</ul>" if items else ""
            elif isinstance(body, Mapping):
                content = html_text(body.get("text") or body.get("content") or body.get("value"))
            else:
                content = html_text(body)
            if heading:
                sections.append(f"<h3>{heading}</h3>")
            if content:
                sections.append(content)
        else:
            rendered = html_text(section)
            if rendered:
                sections.append(f"<p>{rendered}</p>")
    return "".join(sections)


def _lever_description(job: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    """Return the complete Lever description and structured section facts."""

    parts: list[str] = []
    description = _description_value(job.get("description") or job.get("descriptionHtml"))
    if description:
        parts.append(description)
    lists = job.get("lists")
    list_markup = _lever_list_markup(lists)
    if list_markup:
        parts.append(list_markup)
    additional = job.get("additional")
    if isinstance(additional, list):
        additional_text = "".join(html_text(item) for item in additional if html_text(item))
    elif isinstance(additional, Mapping):
        additional_text = html_text(additional.get("text") or additional.get("content") or additional.get("value"))
    else:
        additional_text = html_text(additional)
    if additional_text:
        parts.append(additional_text)
    return "\n".join(parts), {
        "description": job.get("description"),
        "lists": lists,
        "additional": additional,
    }


def normalize_greenhouse_job(board: str, job: Mapping[str, Any]) -> dict:
    external_id = text(job.get("id"))
    if not external_id:
        raise BoardResponseError(f"greenhouse:{board} listing has no id")
    # Greenhouse offices are organizational assignments, not necessarily the
    # advertised workplace. A California job can belong to a Boston office.
    # Retain offices in source_facts, but never turn them into work locations.
    locations = normalize_locations(
        job.get("location"),
        job.get("locations"),
        country=first_text(job, "country", "country_code", "countryCode"),
    )
    legacy_location = location_text(locations, first_text(job, "location"))
    description = _description_value(job.get("content") or job.get("description"))
    apply_url = first_text(job, "absolute_url", "apply_url", "applyUrl")
    posting = _posting(
        "greenhouse",
        board,
        external_id,
        title=first_text(job, "title"),
        location=legacy_location,
        url=apply_url,
        posted_at=first_text(job, "updated_at", "updatedAt"),
        description_html=description,
        locations=locations,
        workplace_type=canonical_workplace_type(first_text(job, "workplace_type", "workplaceType")),
        description_kind=description_kind(description),
        opportunity_type=canonical_opportunity_type(
            job.get("opportunity_type") or job.get("opportunityType") or job.get("type"),
            title=job.get("title"),
            description=description,
        ),
        apply_url=apply_url or None,
        published_at=actual_publication(job),
        source_updated_at=source_updated(job),
        application_deadline=deadline(job),
        listing_status=_listed_status(job),
        source_facts=_source_facts(job),
    )
    for field in ("departments", "offices", "metadata", "pay_input_ranges", "questions"):
        if field in job:
            posting[field] = job[field]
    return posting


def normalize_lever_job(board: str, job: Mapping[str, Any]) -> dict:
    external_id = text(job.get("id"))
    if not external_id:
        raise BoardResponseError(f"lever:{board} listing has no id")
    categories = job.get("categories") if isinstance(job.get("categories"), Mapping) else {}
    description, sections = _lever_description(job)
    primary_location = (
        job.get("locations")
        or job.get("allLocations")
        or categories.get("location")
        or job.get("location")
    )
    locations = normalize_locations(
        primary_location,
        job.get("secondaryLocations") or job.get("additionalLocations"),
        country=first_text(job, "country", "countryCode", "country_code"),
    )
    legacy_location = location_text(locations, first_text(categories, "location") if categories else first_text(job, "location"))
    apply_url = first_text(job, "applyUrl", "apply_url", "hostedUrl", "url")
    posting = _posting(
        "lever",
        board,
        external_id,
        title=first_text(job, "text", "title"),
        location=legacy_location,
        url=first_text(job, "hostedUrl", "url", "applyUrl", "apply_url"),
        posted_at=first_text(job, "createdAt", "created_at"),
        description_html=description,
        locations=locations,
        workplace_type=canonical_workplace_type(
            first_text(job, "workplaceType", "workplace_type")
            or first_text(categories, "workplaceType", "workplace_type")
            if categories
            else first_text(job, "workplaceType", "workplace_type")
        ),
        description_kind=description_kind(description),
        opportunity_type=canonical_opportunity_type(
            job.get("opportunity_type") or job.get("opportunityType") or job.get("type"),
            title=job.get("text") or job.get("title"),
            description=description,
        ),
        apply_url=apply_url or None,
        published_at=actual_publication(job),
        source_updated_at=source_updated(job),
        application_deadline=deadline(job),
        listing_status=_listed_status(job),
        source_facts=_source_facts(job),
        description_sections=sections,
    )
    for field in ("categories", "tags", "salaryDescription", "salaryRange", "commitment", "team", "level"):
        if field in job:
            posting[field] = job[field]
    return posting


def normalize_ashby_job(board: str, job: Mapping[str, Any]) -> dict:
    external_id = text(job.get("id"))
    if not external_id:
        raise BoardResponseError(f"ashby:{board} listing has no id")
    locations = normalize_locations(
        job.get("location") or job.get("locations"),
        job.get("secondaryLocations") or job.get("additionalLocations"),
        country=first_text(job, "country", "countryCode", "country_code"),
    )
    legacy_location = location_text(locations, first_text(job, "location"))
    description = _description_value(job.get("descriptionHtml") or job.get("description"))
    apply_url = first_text(job, "applyUrl", "apply_url", "jobUrl", "url")
    posting = _posting(
        "ashby",
        board,
        external_id,
        title=first_text(job, "title"),
        location=legacy_location,
        url=first_text(job, "jobUrl", "url", "applyUrl", "apply_url"),
        # ``publishedAt`` is the historical legacy field; the actual update
        # timestamp lives separately in ``source_updated_at``.
        posted_at=first_text(job, "publishedAt", "published_at"),
        description_html=description,
        locations=locations,
        workplace_type=canonical_workplace_type(first_text(job, "workplaceType", "workplace_type")),
        description_kind=description_kind(description),
        opportunity_type=canonical_opportunity_type(
            job.get("opportunity_type") or job.get("opportunityType") or job.get("type") or job.get("employmentType"),
            title=job.get("title"),
            description=description,
        ),
        apply_url=apply_url or None,
        published_at=actual_publication(job),
        source_updated_at=source_updated(job),
        application_deadline=deadline(job),
        listing_status=_listed_status(job),
        source_facts=_source_facts(job),
    )
    for field in ("department", "team", "employmentType", "compensation", "isListed", "secondaryLocations"):
        if field in job:
            posting[field] = job[field]
    return posting


def fetch_greenhouse(board: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs"
    data = _get_json(
        url,
        "greenhouse",
        board,
        params={"content": "true"},
        headers=UA,
        timeout=TIMEOUT,
    )
    entries = _validate_entries(data, source="greenhouse", board=board, key="jobs")
    try:
        postings = [normalize_greenhouse_job(board, job) for job in entries]
    except BoardResponseError:
        raise
    except (KeyError, TypeError, ValueError) as error:
        raise BoardResponseError(f"greenhouse:{board} response contains malformed job data") from error
    if _response_indicates_partial(data):
        raise PartialBoardError(f"greenhouse:{board} response marked itself partial", postings)
    return PostingBatch(postings)


def fetch_lever(board: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{board}"
    data = _get_json(
        url,
        "lever",
        board,
        params={"mode": "json"},
        headers=UA,
        timeout=TIMEOUT,
    )
    entries = _validate_entries(data, source="lever", board=board)
    postings = [normalize_lever_job(board, job) for job in entries]
    if _response_indicates_partial(data):
        raise PartialBoardError(f"lever:{board} response marked itself partial", postings)
    return PostingBatch(postings)


def fetch_ashby(board: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
    data = _get_json(url, "ashby", board, headers=UA, timeout=TIMEOUT)
    entries = _validate_entries(data, source="ashby", board=board, key="jobs")
    postings = [normalize_ashby_job(board, job) for job in entries]
    if _response_indicates_partial(data):
        raise PartialBoardError(f"ashby:{board} response marked itself partial", postings)
    return PostingBatch(postings)


def _smartrecruiters_description(job: Mapping[str, Any]) -> tuple[str, dict[str, Any]]:
    job_ad = job.get("jobAd")
    sections = job_ad.get("sections") if isinstance(job_ad, Mapping) else None
    if not isinstance(sections, Mapping):
        raise BoardResponseError("SmartRecruiters detail response has no jobAd.sections object")
    rendered: list[str] = []
    retained: dict[str, Any] = {}
    for name, value in sections.items():
        if not isinstance(name, str) or not isinstance(value, Mapping):
            continue
        title = text(value.get("title"))
        body = text(value.get("text"))
        retained[name] = dict(value)
        if title:
            rendered.append(f"<h2>{html_text(title)}</h2>")
        if body:
            rendered.append(body)
    description = "\n".join(rendered)
    if not description:
        raise BoardResponseError("SmartRecruiters detail response has no description sections")
    return description, retained


def normalize_smartrecruiters_job(board: str, job: Mapping[str, Any]) -> dict:
    external_id = text(job.get("id"))
    if not external_id:
        raise BoardResponseError(f"smartrecruiters:{board} listing has no id")
    description, sections = _smartrecruiters_description(job)
    location = job.get("location") if isinstance(job.get("location"), Mapping) else {}
    locations = normalize_locations(
        location.get("fullLocation") or location,
        country=location.get("country"),
    )
    apply_url = first_text(job, "applyUrl", "postingUrl")
    remote = location.get("remote") is True
    hybrid = location.get("hybrid") is True
    workplace = "remote" if remote and not hybrid else ("hybrid" if hybrid else "onsite")
    return _posting(
        "smartrecruiters",
        board,
        external_id,
        title=first_text(job, "name"),
        location=location_text(locations, first_text(location, "fullLocation")),
        url=first_text(job, "postingUrl", "applyUrl"),
        posted_at=first_text(job, "releasedDate"),
        description_html=description,
        locations=locations,
        workplace_type=workplace,
        description_kind="full",
        opportunity_type=canonical_opportunity_type(
            job.get("type"), title=job.get("name"), description=description,
        ),
        apply_url=apply_url or None,
        published_at=first_text(job, "releasedDate") or actual_publication(job),
        source_updated_at=source_updated(job),
        application_deadline=deadline(job),
        listing_status=_listed_status(job),
        source_facts=_source_facts(job),
        description_sections=sections,
    )


def fetch_smartrecruiters(board: str) -> list[dict]:
    """List every public Posting even if one exact detail cannot be verified."""

    base = f"https://api.smartrecruiters.com/v1/companies/{board}/postings"
    summaries: list[Mapping[str, Any]] = []
    offset = 0
    total: int | None = None
    with httpx.Client(headers=UA, timeout=TIMEOUT, follow_redirects=False) as client:
        while total is None or offset < total:
            response = _network_request(lambda: client.get(base, params={"limit": 100, "offset": offset}))
            data = _json_response(response, "smartrecruiters", board)
            entries = _validate_entries(data, source="smartrecruiters", board=board, key="content")
            found = data.get("totalFound") if isinstance(data, Mapping) else None
            if isinstance(found, bool) or not isinstance(found, int) or found < 0:
                raise BoardResponseError(
                    f"smartrecruiters:{board} response has no valid totalFound"
                )
            total = found
            summaries.extend(entries)
            offset += len(entries)
            if offset < total and not entries:
                raise BoardResponseError(f"smartrecruiters:{board} stopped before its reported total")
        postings: list[dict] = []
        missing = 0
        transient_detail_failure = False
        for summary in summaries:
            external_id = text(summary.get("id"))
            if not external_id:
                missing += 1
                continue
            try:
                response = _network_request(lambda: client.get(f"{base}/{external_id}"))
                detail = _json_response(response, "smartrecruiters", board)
                if not isinstance(detail, Mapping) or text(detail.get("id")) != external_id:
                    raise BoardResponseError("SmartRecruiters exact detail has a mismatched id")
                postings.append(normalize_smartrecruiters_job(board, detail))
            except (httpx.HTTPError, DiscoveryError) as error:
                missing += 1
                transient_detail_failure |= transient_network_error(error)
                location = summary.get("location")
                postings.append(_posting(
                    "smartrecruiters", board, external_id,
                    title=first_text(summary, "name"),
                    location=first_text(location, "fullLocation") if isinstance(location, Mapping) else "",
                    url=first_text(summary, "postingUrl"),
                    apply_url=first_text(summary, "postingUrl") or None,
                    description_html="",
                    description_kind="missing",
                    listing_status="open",
                    verification_status="unknown",
                    detail_verification_status="unknown",
                    verification_error=" ".join(str(error).split())[:300],
                    source_facts=_source_facts(summary),
                ))
    batch = PostingBatch(
        postings, complete=missing == 0, status="partial" if missing else "ok",
        error=f"{missing} exact detail(s) unavailable" if missing else None,
    )
    batch.transient_failure = transient_detail_failure
    return batch


# ---------------------------------------------------------------------------
# Workday
# ---------------------------------------------------------------------------

class WorkdayError(RuntimeError):
    """A Workday board could not be read into Postings with any confidence.

    Raised rather than returning a short or empty result, so ``discover.run``
    prints the board as FAILED and nothing enters the append-only corpus. An
    unknown never quietly becomes a confident answer.
    """


#: A Workday board token: ``<tenant>.wd<N>~<site>``. Two identifiers are needed
#: — the host prefix (``amgen.wd1``) and the site string (``Careers``) — and the
#: other adapters take a single token, so the two are joined by ``~``. Not a
#: colon: a Posting key is ``source:board:external_id`` and a colon inside the
#: board half makes a key nobody can read back. Not a slash either: the key is a
#: path parameter in the dashboard's API. ``~`` is unreserved in a URL and
#: appears in neither half.
WORKDAY_TOKEN = re.compile(r"^(?P<tenant>[A-Za-z0-9][A-Za-z0-9_-]*)\.wd(?P<instance>\d+)~(?P<site>[A-Za-z0-9._-]+)$")

#: The most Postings asked for in one listing request. Whether the endpoint caps
#: the page size itself is not known, so the cap is applied here rather than
#: relied on there.
WORKDAY_PAGE_LIMIT = 20

def parse_workday_board(board: str) -> tuple[str, str, str]:
    """Split a Workday board token into (host, tenant, site).

    ``amgen.wd1~Careers`` -> ``("amgen.wd1.myworkdayjobs.com", "amgen",
    "Careers")``. The host carries the ``wd<N>`` instance; the path carries the
    bare tenant. A token that does not parse is refused by name, because the
    alternative is a request to a nonsense URL and a failure shaped like a
    missing key.
    """
    match = WORKDAY_TOKEN.fullmatch(board.strip())
    if match is None:
        raise WorkdayError(
            f"workday board token {board!r} is malformed — it must read "
            "'<tenant>.wd<N>~<site>', e.g. 'amgen.wd1~Careers' for "
            "https://amgen.wd1.myworkdayjobs.com/en-US/Careers"
        )
    tenant = match["tenant"]
    return f"{tenant}.wd{match['instance']}.myworkdayjobs.com", tenant, match["site"]


def workday_listing_body(limit: int, offset: int) -> dict:
    """The listing request body, with the page size capped here rather than there.

    All four keys the careers site's own client sends are sent, including an
    empty ``appliedFacets``: whether the endpoint tolerates a missing one is not
    known, and finding out is not worth a board going dark.
    """
    return {
        "appliedFacets": {},
        "limit": max(1, min(limit, WORKDAY_PAGE_LIMIT)),
        "offset": max(0, offset),
        "searchText": "",
    }


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _requisition_id(job: Mapping[str, Any]) -> str:
    """Select an ID-shaped bullet, regardless of employer-specific field order.

    A location or department can precede the ID. When several bullets look
    like IDs, the URL may corroborate one; the editable title slug is never
    itself used as the identity.
    """
    fields = job.get("bulletFields")
    candidates = list(dict.fromkeys(
        field.strip() for field in fields
        if isinstance(field, str)
        and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}", field.strip())
        and any(character.isdigit() for character in field)
    )) if isinstance(fields, list) else []
    if len(candidates) == 1:
        return candidates[0]
    path = _text(job.get("externalPath"))
    corroborated = [candidate for candidate in candidates
                    if re.search(r"_" + re.escape(candidate) + r"(?:-\d+)?$", path)]
    if len(corroborated) == 1:
        return corroborated[0]
    raise WorkdayError(
        f"workday listing entry {_text(job.get('title')) or '<untitled>'!r} has "
        "missing or ambiguous bulletFields requisition identity"
    )


def _locations(primary: object, additional: object) -> str:
    """One location string from the primary location and any additional ones.

    ``additionalLocations`` appeared on one of the three tenants this was built
    against, so it is optional; when it is there it is real Posting information
    the person should see.
    """
    places = [_text(primary)] if _text(primary) else []
    if isinstance(additional, list):
        places.extend(_text(place) for place in additional if _text(place))
    seen: list[str] = []
    for place in places:
        if place not in seen:
            seen.append(place)
    return "; ".join(seen)


def _workday_listing_location(job: Mapping[str, Any]) -> object:
    primary = job.get("locationsText") or job.get("location")
    if primary:
        return primary
    # Bullet order is employer-specific. Only treat a bullet as a location
    # when the source URL's location segment independently corroborates it.
    match = re.match(r"^/job/([^/]+)/", _text(job.get("externalPath")))
    fields = job.get("bulletFields")
    if match and isinstance(fields, list):
        def comparable(value: str) -> str:
            return re.sub(r"[\W_]+", "", unquote(value).casefold())
        location_slug = comparable(match[1])
        for field in fields:
            if isinstance(field, str) and location_slug and comparable(field) == location_slug:
                return field.strip()
    return ""


def _workday_country(info: Mapping[str, Any]) -> str:
    requisition_location = info.get("jobRequisitionLocation")
    if isinstance(requisition_location, Mapping):
        explicit = country_iso2(requisition_location.get("country"))
        if explicit:
            return explicit
    return country_iso2(info.get("country") or info.get("countryCode") or info.get("country_code"))


def normalize_workday_listing(board: str, data: Mapping[str, Any]) -> list[dict]:
    """Normalize one Workday listing page into Postings, descriptions still missing.

    ``posted_at`` comes from ``startDate``; ``postedOn`` is prose ("Posted
    Today") and is never read. ``external_path`` is carried so the detail fetch
    knows what to ask for and is consumed by ``enrich_workday`` before anything
    is appended.
    """
    host, _tenant, site = parse_workday_board(board)
    postings = []
    entries = data.get("jobPostings")
    if not isinstance(entries, list):
        raise BoardResponseError(f"workday:{board} response is missing its 'jobPostings' list")
    for job in entries:
        if not isinstance(job, Mapping):
            raise PartialBoardError(
                f"workday:{board} response contained a malformed listing entry", postings
            )
        external_path = _text(job.get("externalPath"))
        primary_location = _workday_listing_location(job)
        locations = normalize_locations(
            primary_location, job.get("additionalLocations"), country=_workday_country(job),
        )
        postings.append(
            _posting(
                "workday", board, _requisition_id(job),
                title=_text(job.get("title")),
                location=location_text(locations, _locations(primary_location, job.get("additionalLocations"))),
                url=f"https://{host}/{site}{external_path}" if external_path else "",
                posted_at=_text(job.get("startDate")),
                description_html="",
                external_path=external_path,
                locations=locations,
                workplace_type=canonical_workplace_type(
                    job.get("remoteType") or job.get("workplaceType") or job.get("remote")
                ),
                description_kind="missing",
                opportunity_type=canonical_opportunity_type(
                    job.get("opportunity_type") or job.get("opportunityType") or job.get("type"),
                    title=job.get("title"),
                ),
                apply_url=(f"https://{host}/{site}{external_path}" if external_path else None),
                published_at=actual_publication(job),
                source_updated_at=source_updated(job),
                application_deadline=deadline(job),
                listing_status=_listed_status(job),
                source_facts=_source_facts(job),
            )
        )
    return postings


def normalize_workday_detail(posting: Mapping[str, Any], data: Mapping[str, Any]) -> dict:
    """Complete one listing Posting from its detail document.

    ``hiringOrganization.name`` is deliberately not read: it answers with an
    internal entity code ("140 Pfizer Inc", "2100 NVIDIA USA"), not the employer.
    The employer display name comes from the Profile's ``sources.names``, exactly
    as it does for the other ATS adapters.

    Workday tenants vary in which optional facts they include.  When present,
    ``remote``/``remoteType`` and ``endDate`` are retained as source facts and
    normalized into ``workplace_type`` and ``application_deadline``.  The
    legacy location string remains available alongside the structured list.
    """
    info = data.get("jobPostingInfo")
    if not isinstance(info, Mapping):
        raise WorkdayError(
            f"workday detail for {posting.get('key', '?')} carries no jobPostingInfo — "
            "the response shape has changed"
        )
    board = str(posting["board"])
    external_id = _text(info.get("jobReqId"))
    requested_id = _text(posting.get("external_id"))
    if not external_id or external_id != requested_id:
        raise WorkdayError(
            f"workday detail jobReqId {external_id!r} does not match requested "
            f"external_id {requested_id!r} for {posting.get('key', '?')}"
        )
    primary_location = info.get("location") or info.get("locations")
    additional_locations = info.get("additionalLocations") or info.get("secondaryLocations")
    if not primary_location and not additional_locations:
        primary_location = posting.get("locations") or posting.get("location")
    locations = normalize_locations(
        primary_location, additional_locations, country=_workday_country(info),
    )
    location = location_text(
        locations,
        _locations(info.get("location"), info.get("additionalLocations"))
        or str(posting.get("location", "")),
    )
    description = _text(info.get("jobDescription") or info.get("descriptionHtml"))
    apply_url = _text(info.get("externalUrl") or info.get("applyUrl")) or str(posting.get("url", ""))
    normalized = _posting(
        "workday", board, external_id,
        title=_text(info.get("title")) or str(posting.get("title", "")),
        location=location,
        url=apply_url,
        posted_at=_text(info.get("startDate")) or str(posting.get("posted_at", "")),
        description_html=description,
        discovered_at=str(posting.get("discovered_at", _now())),
        locations=locations or list(posting.get("locations") or []),
        workplace_type=canonical_workplace_type(
            info.get("remoteType") or info.get("workplaceType") or info.get("remote")
        ),
        description_kind=description_kind(description),
        opportunity_type=canonical_opportunity_type(
            info.get("opportunity_type") or info.get("opportunityType") or info.get("type"),
            title=info.get("title") or posting.get("title"),
            description=description,
        ),
        apply_url=apply_url or None,
        published_at=actual_publication(info),
        source_updated_at=source_updated(info),
        application_deadline=deadline(info),
        listing_status=_listed_status(info),
        # The public detail payload may repeat an internal hiring-organization
        # code.  Keep the structured job facts while avoiding that identifier
        # becoming a misleading employer field in downstream views.
        source_facts={"jobPostingInfo": _source_facts(info)},
        external_path=_text(posting.get("external_path")),
    )
    if "remote" in info:
        normalized["remote"] = info["remote"]
    elif "remoteType" in info:
        normalized["remote"] = info["remoteType"]
    if "remoteType" in info:
        normalized["remote_type"] = info["remoteType"]
    if "endDate" in info:
        normalized["endDate"] = info["endDate"]
        normalized["end_date"] = info["endDate"]
    return normalized


PageFetcher = Callable[[int, int], Mapping[str, Any]]


def _workday_page_signature(postings: Sequence[Mapping[str, Any]]) -> str:
    """Hash the stable identity/address set returned by one listing page."""

    # A stale Workday offset can return the same page forever.  Identity and
    # external_path are enough to recognize that condition while still
    # allowing a changed title or other listing fact to be observed normally.
    values = sorted(
        (str(posting.get("key", "")), str(posting.get("external_path", "")))
        for posting in postings
    )
    payload = json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _partial_batch(
    postings: list[dict],
    *,
    status: str,
    error: str | None,
    next_offset: int,
    start_offset: int,
    pages_fetched: int,
    postings_observed: int,
    scan_complete: bool = False,
    stalled: bool = False,
    last_page_signature: str | None = None,
) -> PostingBatch:
    return PostingBatch(
        postings,
        complete=False,
        status=status,
        error=error,
        next_offset=next_offset,
        start_offset=start_offset,
        pages_fetched=pages_fetched,
        postings_observed=postings_observed,
        scan_complete=scan_complete,
        stalled=stalled,
        last_page_signature=last_page_signature,
    )


def workday_pages(
    board: str,
    fetch_page: PageFetcher,
    limit: int = WORKDAY_PAGE_LIMIT,
    *,
    start_offset: int = 0,
    max_pages: int | None = None,
    resumable: bool = False,
    previous_page_signature: str | None = None,
) -> list[dict]:
    """Page a Workday listing through ``fetch_page(limit, offset)``.

    ``start_offset`` and ``max_pages`` bound one listing window.  A resumable
    caller receives a list-compatible ``PostingBatch`` with ``next_offset``
    and ``scan_complete`` metadata; the caller must persist that metadata only
    after committing the returned observations.  The regular API retains its
    historical exception behavior for a failed prefix.

    Workday offsets are rolling positions, not durable identities.  A resumed
    window is therefore never a complete board snapshot, even if it reaches a
    short page.  A fresh offset-zero walk can be complete when it reaches a
    short page without malformed entries.  Repeated page identities or a page
    containing no new identity stops the walk at the current offset instead of
    burning the page ceiling.

    The offset-zero response's ``total`` is authoritative for pagination. Some
    tenants report zero on every later page despite returning rows. A resumed
    window probes offset zero if its first page reports zero; this also prevents
    wraparound rows from being mistaken for a new page at the end.
    """
    page_size = max(1, min(limit, WORKDAY_PAGE_LIMIT))
    start_offset = max(0, int(start_offset))
    max_pages = None if max_pages is None else max(0, int(max_pages))
    postings: list[dict] = []
    keys: set[str] = set()
    page_signatures: set[str] = set()
    skipped_entries = 0
    pages_fetched = 0
    postings_observed = 0
    next_offset = start_offset
    last_page_signature: str | None = None
    known_total: int | None = None

    def failed(error: Exception, *, offset: int) -> PostingBatch | None:
        if not resumable:
            return None
        batch = _partial_batch(
            postings,
            status="partial",
            error=f"stopped at offset {offset}: {error}",
            next_offset=offset,
            start_offset=start_offset,
            pages_fetched=pages_fetched,
            postings_observed=postings_observed,
            last_page_signature=last_page_signature,
        )
        batch.transient_failure = transient_network_error(error)
        return batch

    page = 0
    while max_pages is None or page < max_pages:
        offset = start_offset + page * page_size
        next_offset = offset
        try:
            data = fetch_page(page_size, offset)
        except (httpx.HTTPError, WorkdayError, BoardResponseError) as error:
            if postings:
                batch = failed(error, offset=offset)
                if batch is not None:
                    return batch
                raise PartialBoardError(
                    f"workday:{board} stopped after {len(postings)} postings: {error}",
                    postings,
                    next_offset=offset,
                    start_offset=start_offset,
                    pages_fetched=pages_fetched,
                    postings_observed=postings_observed,
                ) from error
            raise
        except Exception as error:
            # A transport callback supplied by a caller may use a different
            # exception class.  Preserve the old behavior while making the
            # resumable adapter safe for those test and integration clients.
            if postings:
                batch = failed(error, offset=offset)
                if batch is not None:
                    return batch
                raise PartialBoardError(
                    f"workday:{board} stopped after {len(postings)} postings: {error}",
                    postings,
                    next_offset=offset,
                    start_offset=start_offset,
                    pages_fetched=pages_fetched,
                    postings_observed=postings_observed,
                ) from error
            raise
        if not isinstance(data, Mapping):
            error = BoardResponseError(f"workday:{board} returned a non-object listing page")
            if postings:
                batch = failed(error, offset=offset)
                if batch is not None:
                    return batch
                raise PartialBoardError(str(error), postings, next_offset=offset) from error
            raise error
        entries = data.get("jobPostings")
        if not isinstance(entries, list):
            if postings:
                error = BoardResponseError(
                    f"workday:{board} page {page} is missing its 'jobPostings' list"
                )
                batch = failed(error, offset=offset)
                if batch is not None:
                    return batch
                raise PartialBoardError(str(error), postings, next_offset=offset) from error
            raise WorkdayError(
                f"workday:{board} response is missing its 'jobPostings' list"
            )
        total = data.get("total")
        if isinstance(total, bool) or not isinstance(total, int) or total < 0:
            raise WorkdayError(f"workday:{board} response has no valid total")
        if offset == 0:
            known_total = total
        elif total == 0 and known_total is None:
            # A resumed cursor has no offset-zero total in this window. Probe
            # it rather than treating this tenant's page-local zero as EOF.
            try:
                head = fetch_page(page_size, 0)
                head_total = head.get("total") if isinstance(head, Mapping) else None
                if (isinstance(head_total, bool) or not isinstance(head_total, int)
                        or head_total < 0 or not isinstance(head.get("jobPostings"), list)):
                    raise WorkdayError(f"workday:{board} head has no valid listing total")
                known_total = head_total
            except Exception as error:
                batch = failed(error, offset=offset)
                if batch is not None:
                    return batch
                if postings:
                    raise PartialBoardError(str(error), postings, next_offset=offset) from error
                raise
        if known_total is not None:
            total = known_total
        if offset >= total:
            authoritative = start_offset == 0 and skipped_entries == 0
            return PostingBatch(
                postings,
                complete=authoritative,
                status="ok" if authoritative else "partial",
                next_offset=offset,
                start_offset=start_offset,
                pages_fetched=pages_fetched,
                postings_observed=postings_observed,
                scan_complete=True,
                last_page_signature=_workday_page_signature(()),
            )
        if not entries:
            error = WorkdayError(
                f"workday:{board} returned no rows at offset {offset} before its reported total {total}"
            )
            batch = failed(error, offset=offset)
            if batch is not None:
                return batch
            if postings:
                raise PartialBoardError(str(error), postings, next_offset=offset) from error
            raise error
        postings_observed += len(entries)
        valid_entries = []
        for entry in entries:
            try:
                if not isinstance(entry, Mapping):
                    raise WorkdayError("listing entry is not an object")
                _requisition_id(entry)
            except WorkdayError:
                skipped_entries += 1
            else:
                valid_entries.append(entry)
        try:
            page_postings = normalize_workday_listing(board, {**data, "jobPostings": valid_entries})
        except (WorkdayError, BoardResponseError) as error:
            if postings:
                batch = failed(error, offset=offset)
                if batch is not None:
                    return batch
                raise PartialBoardError(str(error), postings, next_offset=offset) from error
            raise
        if len({posting["key"] for posting in page_postings}) < len(page_postings):
            error = WorkdayError(
                f"workday board {board!r} answered one listing page with repeated Posting keys, "
                "so its bulletFields do not identify a Posting. Keying on them would merge "
                "distinct jobs; the externalPath slug carries the editable title and cannot "
                "replace them."
            )
            if postings:
                batch = failed(error, offset=offset)
                if batch is not None:
                    return batch
            raise error
        signature = _workday_page_signature(page_postings)
        # An empty normalized page can contain only unidentifiable entries. It
        # still advances past this raw response, but remains partial and never
        # claims that the board ended cleanly.
        repeated = (
            (page == 0 and previous_page_signature and signature == previous_page_signature)
            or signature in page_signatures
            or (
            bool(page_postings) and not any(posting["key"] not in keys for posting in page_postings)
            )
        )
        if repeated:
            reason = f"repeated or non-advancing listing page at offset {offset}"
            if resumable:
                return _partial_batch(
                    postings,
                    status="stalled",
                    error=reason,
                    next_offset=offset,
                    start_offset=start_offset,
                    pages_fetched=pages_fetched,
                    postings_observed=postings_observed,
                    stalled=True,
                    last_page_signature=signature,
                )
            if postings:
                raise PartialBoardError(
                    f"workday:{board} {reason}",
                    postings,
                    next_offset=offset,
                    start_offset=start_offset,
                    pages_fetched=pages_fetched,
                    postings_observed=postings_observed,
                    stalled=True,
                    last_page_signature=signature,
                )
            raise WorkdayError(f"workday:{board} {reason}")
        page_signatures.add(signature)
        last_page_signature = signature
        pages_fetched += 1
        page += 1
        for posting in page_postings:
            if posting["key"] not in keys:
                keys.add(posting["key"])
                postings.append(posting)
        next_offset = offset + len(entries)
        if next_offset >= total:
            error = f"{skipped_entries} entries lack a reliable identity" if skipped_entries else None
            authoritative = start_offset == 0 and skipped_entries == 0
            return PostingBatch(
                postings,
                complete=authoritative,
                status="ok" if authoritative else "partial",
                error=error,
                next_offset=next_offset,
                start_offset=start_offset,
                pages_fetched=pages_fetched,
                postings_observed=postings_observed,
                scan_complete=True,
                last_page_signature=signature,
            )
    assert max_pages is not None
    if max_pages == 0:
        error = "no listing pages requested"
    else:
        error = f"stopped at {max_pages} pages"
    print(
        f"workday:{board}: stopped at {max_pages} pages ({len(postings)} postings) — "
        "the listing is still answering full pages, so this run is incomplete"
    )
    return _partial_batch(
        postings,
        status="partial",
        error=error,
        next_offset=next_offset if pages_fetched else start_offset,
        start_offset=start_offset,
        pages_fetched=pages_fetched,
        postings_observed=postings_observed,
        last_page_signature=last_page_signature,
    )


def _refuse_redirect(board: str, response: httpx.Response) -> None:
    """Refuse a 3xx rather than following it off the tenant's host.

    Both Workday call sites set ``follow_redirects=False``, so a redirect is
    never followed and a discovery fetch can never be steered to an arbitrary
    server: the only host either one contacts is the one the board token names.
    Without this, an unfollowed 3xx would fall through ``raise_for_status`` —
    which only raises on 4xx and 5xx — and surface as a JSON decode error on an
    empty body, which says nothing about what actually happened.
    """
    if response.is_redirect:
        raise WorkdayError(
            f"workday board {board!r} answered {response.status_code} redirecting to "
            f"{response.headers.get('location', '<no location>')!r}. Redirects are not "
            "followed: the only host this adapter contacts is the one the board token names."
        )


def _retry_after_seconds(response: httpx.Response) -> float | None:
    value = response.headers.get("retry-after")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            parsed = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if parsed.tzinfo is None:
            return None
        return max(0.0, (parsed - datetime.now(timezone.utc)).total_seconds())


class WorkdayClient:
    """One cookie-preserving, serial, paced session for one Workday tenant."""

    def __init__(self, host: str) -> None:
        self.host = host
        self.client = httpx.Client(headers=UA, timeout=TIMEOUT, follow_redirects=False)
        self.last_request_started: float | None = None
        self.requests = 0
        self.rate_limits = 0
        self.blocks = 0

    def __enter__(self) -> WorkdayClient:
        return self

    def __exit__(self, *args: object) -> None:
        self.client.close()

    def close(self) -> None:
        self.client.close()

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        rate_attempt = 0
        server_attempt = 0
        network_attempt = 0
        while True:
            if self.last_request_started is not None:
                time.sleep(max(0.0, 0.5 - (time.monotonic() - self.last_request_started)))
            self.last_request_started = time.monotonic()
            try:
                response = self.client.request(method, url, **kwargs)
            except Exception as error:
                self.requests += 1
                if transient_network_error(error) and network_attempt < len(NETWORK_RETRY_DELAYS):
                    time.sleep(NETWORK_RETRY_DELAYS[network_attempt])
                    network_attempt += 1
                    continue
                raise
            self.requests += 1
            if response.status_code == 429:
                self.rate_limits += 1
                if rate_attempt >= 3:
                    raise WorkdayTenantThrottled(
                        f"workday tenant {self.host} remained rate-limited after retries"
                    )
                delay = _retry_after_seconds(response)
                time.sleep(delay if delay is not None else 2.0**(rate_attempt + 1))
                rate_attempt += 1
                continue
            if 500 <= response.status_code < 600 and server_attempt == 0:
                server_attempt += 1
                time.sleep(1.0)
                continue
            return response

    def get(self, url: str) -> httpx.Response:
        return self._request("GET", url)

    def post(self, url: str, *, json: Mapping[str, Any]) -> httpx.Response:
        return self._request("POST", url, json=json)


def fetch_workday(
    board: str,
    *,
    start_offset: int = 0,
    limit: int = WORKDAY_PAGE_LIMIT,
    max_pages: int | None = None,
    resumable: bool = False,
    previous_page_signature: str | None = None,
) -> list[dict]:
    """Fetch a Workday listing through its reported total.

    ``max_pages`` is used only by an explicitly bounded interactive refresh.
    ``resumable=True`` returns a partial ``PostingBatch`` with a safe
    ``next_offset`` when such a window stops before the total.
    """
    host, tenant, site = parse_workday_board(board)
    url = f"https://{host}/wday/cxs/{tenant}/{site}/jobs"
    # `follow_redirects=False` is httpx's default; it is written out because it
    # is load-bearing rather than incidental. The host is pinned by the board
    # token, and a followed 3xx would let the endpoint redirect a discovery
    # fetch to an arbitrary server. A redirect is surfaced as a failure instead.
    with WorkdayClient(host) as session:
        def fetch_page(limit: int, offset: int) -> Mapping[str, Any]:
            # A POST, and the only one in the Discover path. It is Workday's
            # read-only job search — no state, nothing created — issued as a
            # POST because the query is a JSON body, exactly as the vendor's own
            # careers-site client issues it. The never-submit guards live in the
            # browser layer, which this never touches; see the module docstring.
            response = session.post(url, json=workday_listing_body(limit, offset))
            _refuse_redirect(board, response)
            if response.status_code >= 400:
                raise WorkdayError(
                    f"workday:{board} listing page answered HTTP {response.status_code}"
                )
            try:
                payload = response.json()
            except ValueError as error:
                raise BoardResponseError(
                    f"workday:{board} listing page returned invalid JSON"
                ) from error
            if not isinstance(payload, Mapping):
                raise BoardResponseError(
                    f"workday:{board} listing page returned a non-object response"
                )
            return payload

        result = workday_pages(
            board,
            fetch_page,
            limit=limit,
            start_offset=start_offset,
            max_pages=max_pages,
            resumable=resumable,
            previous_page_signature=previous_page_signature,
        )
        if isinstance(result, PostingBatch) and not result.complete and not resumable:
            raise PartialBoardError(
                f"workday:{board} listing was incomplete after {len(result)} postings: {result.error or 'source did not finish'}",
                list(result),
                next_offset=result.next_offset,
                start_offset=result.start_offset,
                pages_fetched=result.pages_fetched,
                postings_observed=result.postings_observed,
                stalled=result.stalled,
                last_page_signature=result.last_page_signature,
            )
        return result


def fetch_workday_window(
    board: str,
    *,
    start_offset: int = 0,
    max_pages: int | None = None,
    limit: int = WORKDAY_PAGE_LIMIT,
    previous_page_signature: str | None = None,
) -> PostingBatch:
    """Fetch one bounded Workday listing window for resumable callers.

    This is also the registration probe surface: ``max_pages=1`` issues one
    read-only listing request and returns its usable lower-bound count without
    requiring a whole employer board to finish.
    """

    result = fetch_workday(
        board,
        start_offset=start_offset,
        limit=limit,
        max_pages=max_pages,
        resumable=True,
        previous_page_signature=previous_page_signature,
    )
    if not isinstance(result, PostingBatch):
        # ``workday_pages`` always returns this type for resumable calls, but
        # keep the public probe contract stable if that implementation changes.
        return PostingBatch(list(result), complete=False, status="partial")
    return result


def _workday_detail_path(posting: Mapping[str, Any], host: str, site: str) -> str:
    """Use a stored source path, or recover one from a corroborated canonical URL."""
    from urllib.parse import urlsplit

    external_path = _text(posting.get("external_path"))
    if not external_path:
        try:
            parsed = urlsplit(_text(posting.get("url")))
        except ValueError as error:
            raise WorkdayError("workday stored URL is malformed") from error
        if (parsed.scheme != "https" or parsed.netloc != host
                or parsed.query or parsed.fragment):
            raise WorkdayError("workday stored URL is not a canonical URL on the configured host")
        prefix = re.fullmatch(
            r"/(?:[a-z]{2}-[A-Z]{2}/)?" + re.escape(site) + r"(?P<path>/job/.+)",
            parsed.path,
        )
        if not prefix:
            raise WorkdayError("workday stored URL does not identify the configured site and job route")
        external_path = prefix.group("path")
        external_id = _text(posting.get("external_id"))
        if not external_id or not re.search(r"_" + re.escape(external_id) + r"(?:-\d+)?$", external_path):
            raise WorkdayError("workday stored URL does not corroborate the requested requisition")
    decoded = unquote(external_path)
    if (not external_path.startswith("/job/") or any(c in decoded for c in "?#\\")
            or any(part in {".", "..", ""} for part in decoded[1:].split("/"))):
        raise WorkdayError("workday posting has no usable external_path for its exact job")
    return external_path


def enrich_workday(
    posting: Mapping[str, Any], *, session: WorkdayClient | None = None,
) -> dict:
    """Fetch one Workday Posting detail through a paced tenant session."""

    host, tenant, site = parse_workday_board(str(posting["board"]))
    if session is not None and session.host != host:
        raise WorkdayError("Workday detail session does not belong to the Posting's tenant")
    external_path = _workday_detail_path(posting, host, site)
    url = f"https://{host}/wday/cxs/{tenant}/{site}{external_path}"
    if session is None:
        with WorkdayClient(host) as owned:
            return enrich_workday(posting, session=owned)
    response = session.get(url)
    _refuse_redirect(str(posting["board"]), response)
    payload: object = None
    if response.status_code == 403:
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, Mapping) and payload.get("errorCode") == "S22":
            raise ExactJobClosed(
                f"workday exact job {posting.get('key', '?')} is closed"
            )
        session.blocks += 1
        raise WorkdayTenantBlocked(f"workday tenant {host} refused detail requests")
    if response.status_code in {404, 410}:
        raise ExactJobClosed(
            f"workday exact job {posting.get('key', '?')} answered HTTP {response.status_code}",
        )
    if response.status_code >= 400:
        raise DiscoveryError(
            f"workday exact job {posting.get('key', '?')} answered HTTP {response.status_code}"
        )
    if payload is None:
        try:
            payload = response.json()
        except ValueError as error:
            raise BoardResponseError(
                f"workday exact job {posting.get('key', '?')} returned invalid JSON"
            ) from error
    if not isinstance(payload, Mapping):
        raise BoardResponseError(
            f"workday exact job {posting.get('key', '?')} returned a non-object response"
        )
    return normalize_workday_detail({**posting, "external_path": external_path}, payload)


def _unknown_refresh(posting: Mapping[str, Any], error: object) -> dict:
    """Keep the current content while making an unavailable exact check visible."""

    result = dict(posting)
    result["listing_status"] = "unknown"
    result["verification_status"] = "unknown"
    result["verification_error"] = " ".join(str(error).split())[:300]
    return result


def _closed_refresh(posting: Mapping[str, Any], error: object) -> dict:
    result = dict(posting)
    result["listing_status"] = "closed"
    result["verification_status"] = "verified"
    result["closure_reason"] = "definitive exact-job verification"
    result["verification_error"] = " ".join(str(error).split())[:300]
    return result


def _exact_get(url: str, source: str, board: str, **kwargs: Any) -> tuple[str, Any]:
    """Fetch one exact posting and classify status before decoding its body."""

    try:
        response = _network_request(lambda: httpx.get(url, **kwargs))
    except httpx.HTTPError:
        raise
    if response.status_code in {404, 410}:
        return "closed", response
    if response.status_code >= 400:
        return "unknown", response
    try:
        payload = response.json()
    except ValueError as error:
        raise BoardResponseError(f"{source}:{board} exact job returned invalid JSON") from error
    return "ok", payload


def _exact_payload_matches(payload: Mapping[str, Any], external_id: str) -> bool:
    """Require an exact endpoint to return the identity that was requested."""

    return text(payload.get("id")) == external_id


def refresh_posting(posting: dict) -> dict:
    """Verify one posting by its exact source endpoint.

    This is deliberately one posting at a time, suitable immediately before
    preparation or handoff.  Availability failures become ``unknown`` while
    retaining the last usable description.  Only an exact 404/410 or an
    explicit closed source fact becomes ``closed``.
    """

    source = text(posting.get("source"))
    board = text(posting.get("board"))
    external_id = text(posting.get("external_id"))
    if not source or not board or not external_id:
        raise DiscoveryError("refresh_posting requires source, board, and external_id")

    try:
        if source == "greenhouse":
            url = f"https://boards-api.greenhouse.io/v1/boards/{board}/jobs/{external_id}"
            state, payload = _exact_get(
                url,
                source,
                board,
                params={"content": "true"},
                headers=UA,
                timeout=TIMEOUT,
            )
            if state == "closed":
                return _closed_refresh(posting, f"exact job answered HTTP {payload.status_code}")
            if state == "unknown":
                return _unknown_refresh(posting, f"exact job answered HTTP {payload.status_code}")
            if not isinstance(payload, Mapping):
                return _unknown_refresh(posting, "exact job returned a non-object response")
            if not _exact_payload_matches(payload, external_id):
                return _unknown_refresh(
                    posting,
                    "exact job response id did not match the requested external_id",
                )
            refreshed = normalize_greenhouse_job(board, payload)
        elif source == "lever":
            url = f"https://api.lever.co/v0/postings/{board}/{external_id}"
            state, payload = _exact_get(
                url,
                source,
                board,
                params={"mode": "json"},
                headers=UA,
                timeout=TIMEOUT,
            )
            if state == "closed":
                from venator.discover.availability import lever_public_closure

                closed, reason = lever_public_closure(
                    board, external_id, headers=UA, timeout=TIMEOUT,
                )
                return _closed_refresh(posting, reason) if closed else _unknown_refresh(posting, reason)
            if state == "unknown":
                return _unknown_refresh(posting, f"exact job answered HTTP {payload.status_code}")
            if not isinstance(payload, Mapping):
                return _unknown_refresh(posting, "exact job returned a non-object response")
            if not _exact_payload_matches(payload, external_id):
                return _unknown_refresh(
                    posting,
                    "exact job response id did not match the requested external_id",
                )
            refreshed = normalize_lever_job(board, payload)
        elif source == "ashby":
            # Ashby documents the public board endpoint rather than a separate
            # per-id endpoint.  Reading one board and selecting this id is still
            # bounded to the employer the Posting came from.
            url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
            state, payload = _exact_get(url, source, board, headers=UA, timeout=TIMEOUT)
            if state == "closed":
                # This endpoint verifies the employer board, not one exact
                # posting.  A missing board may be renamed, disabled, or
                # temporarily unavailable; it is never definitive evidence
                # that this individual job closed.
                return _unknown_refresh(posting, f"exact board answered HTTP {payload.status_code}")
            if state == "unknown":
                return _unknown_refresh(posting, f"exact board answered HTTP {payload.status_code}")
            if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
                return _unknown_refresh(posting, "exact board returned a malformed jobs list")
            match = next(
                (job for job in payload["jobs"] if isinstance(job, Mapping) and text(job.get("id")) == external_id),
                None,
            )
            if match is None:
                return _unknown_refresh(posting, "exact job was absent from the board response")
            refreshed = normalize_ashby_job(board, match)
        elif source == "smartrecruiters":
            url = f"https://api.smartrecruiters.com/v1/companies/{board}/postings/{external_id}"
            state, payload = _exact_get(
                url, source, board, headers=UA, timeout=TIMEOUT, follow_redirects=False,
            )
            if state == "closed":
                return _closed_refresh(posting, f"exact job answered HTTP {payload.status_code}")
            if state == "unknown":
                return _unknown_refresh(posting, f"exact job answered HTTP {payload.status_code}")
            if not isinstance(payload, Mapping):
                return _unknown_refresh(posting, "exact job returned a non-object response")
            if not _exact_payload_matches(payload, external_id):
                return _unknown_refresh(
                    posting, "exact job response id did not match the requested external_id",
                )
            refreshed = normalize_smartrecruiters_job(board, payload)
        elif source in {"icims", "peopleclick", "avature", "talentbrew"}:
            # Prepare and handoff verify just this Posting. A Quest sitemap has
            # thousands of URLs; draining it here would block one application.
            # A missing exact detail is not definitive evidence of closure.
            from venator.discover.public_boards import refresh_public_posting
            refreshed = refresh_public_posting(posting)
        elif source == "workable":
            # The public widget carries full descriptions in one response.
            batch = ADAPTERS[source](board)
            refreshed = next((item for item in batch if item["external_id"] == external_id), None)
            if refreshed is None:
                return _unknown_refresh(posting, "exact job absent from public board")
        elif source == "workday":
            try:
                refreshed = enrich_workday(posting)
            except ExactJobClosed as error:
                return _closed_refresh(posting, error)
            except (httpx.HTTPError, BoardResponseError, DiscoveryError, WorkdayError) as error:
                return _unknown_refresh(posting, error)
        else:
            raise DiscoveryError(f"refresh_posting does not support source {source!r}")
    except (httpx.HTTPError, TimeoutError) as error:
        return _unknown_refresh(posting, error)
    except ExactJobClosed as error:
        return _closed_refresh(posting, error)
    except BoardResponseError as error:
        return _unknown_refresh(posting, error)

    refreshed["listing_status"] = "closed" if _listing_status(refreshed) == "closed" else "open"
    refreshed["verification_status"] = "verified"
    if source == "workday":
        refreshed["detail_verified_at"] = _now()
        refreshed["detail_verification_status"] = "verified"
    # Preserve identity and first discovery fields; the store repeats this
    # merge, but doing it here makes the return value safe to inspect directly.
    for key in ("key", "source", "board", "external_id", "discovered_at", "first_seen_at"):
        if key in posting:
            refreshed[key] = posting[key]
    return refreshed


# The HTML/XML adapters share these primitives; importing them on invocation
# also allows their fixture tests to import the module directly.
def fetch_icims(board: str) -> PostingBatch:
    from venator.discover.public_boards import fetch_icims as fetch
    return fetch(board)


def fetch_peopleclick(board: str) -> PostingBatch:
    from venator.discover.public_boards import fetch_peopleclick as fetch
    return fetch(board)


def fetch_avature(board: str) -> PostingBatch:
    from venator.discover.public_boards import fetch_avature as fetch
    return fetch(board)


def fetch_workable(board: str) -> PostingBatch:
    from venator.discover.public_boards import fetch_workable as fetch
    return fetch(board)


def fetch_talentbrew(board: str) -> PostingBatch:
    from venator.discover.public_boards import fetch_talentbrew as fetch
    return fetch(board)


ADAPTERS = {
    "talentbrew": fetch_talentbrew,
    "icims": fetch_icims,
    "peopleclick": fetch_peopleclick,
    "avature": fetch_avature,
    "workable": fetch_workable,
    "greenhouse": fetch_greenhouse,
    "lever": fetch_lever,
    "ashby": fetch_ashby,
    "smartrecruiters": fetch_smartrecruiters,
    "workday": fetch_workday,
}

#: Sources whose listing does not carry everything a Posting needs, and the
#: per-Posting fetch that completes one. ``discover.run`` runs the hook over the
#: Postings the store has never seen, before they are appended, so the cost is
#: paid once per Posting rather than once per run.
ENRICHERS: dict[str, Callable[[Mapping[str, Any]], dict]] = {
    "workday": enrich_workday,
}

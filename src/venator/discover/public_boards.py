"""Public XML sitemaps and Workable's public widget feed.

Sitemap rows are addresses only. A failed detail never makes the remaining
rows look like a complete board snapshot. Requests are paced one per Posting.
"""
from __future__ import annotations

import json
import re
import time
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from html import unescape
from urllib.parse import urlparse

import httpx

from venator.discover.adapters import (
    BoardResponseError, DiscoveryError, PostingBatch, UA, TIMEOUT,
    _get_json, _network_request, _posting, _response_indicates_partial,
)
from venator.discover.common import (
    actual_publication, canonical_opportunity_type, description_kind,
    location_text, normalize_locations, text,
)

SCRIPT = re.compile(r'<script\b[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script\s*>', re.I | re.S)
ICIMS_HOST = re.compile(r"[a-z0-9-]+\.icims\.com")
PEOPLECLICK_BOARD = re.compile(r"client_[a-z0-9_-]+~[a-z0-9_-]+")
JOB_PATH = re.compile(r"/jobs/(\d+)/[^/?]+/job$")
PACE_SECONDS = 0.15


def _xml_urls(body: str, source: str, board: str) -> list[str]:
    try:
        root = ET.fromstring(body.lstrip("\ufeff"))
    except ET.ParseError as error:
        raise BoardResponseError(f"{source}:{board} invalid sitemap XML") from error
    if root.tag != "{http://www.sitemaps.org/schemas/sitemap/0.9}urlset":
        raise BoardResponseError(f"{source}:{board} unexpected sitemap root")
    urls = []
    for row in root:
        loc = row.findtext("{http://www.sitemaps.org/schemas/sitemap/0.9}loc")
        if not loc:
            raise BoardResponseError(f"{source}:{board} sitemap row lacks loc")
        urls.append(loc)
    return urls


def _get_text(url: str, source: str, board: str) -> str:
    response = _network_request(lambda: httpx.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=False))
    if response.status_code != 200:
        raise DiscoveryError(f"{source}:{board} answered HTTP {response.status_code}")
    return response.text


def _talentbrew_tabs(value: str) -> str:
    """Escape literal tabs in JSON strings; leave JSON whitespace untouched."""
    result = []
    inside = escaped = False
    for char in value:
        if char == '"' and not escaped:
            inside = not inside
        if char == "\t" and inside:
            result.append("\\t")
        else:
            result.append(char)
        if char == "\\" and not escaped:
            escaped = True
        else:
            escaped = False
    return "".join(result)


def _job_ld(html: str, source: str, board: str) -> Mapping:
    for match in SCRIPT.finditer(html):
        try:
            # Script contents are raw text in HTML, not entity-decoded markup.
            # Unescaping first corrupts JSON strings containing &quot; in HTML descriptions.
            raw = match.group(1)
            value = json.loads(_talentbrew_tabs(raw) if source == "talentbrew" else raw)
        except ValueError as error:
            raise BoardResponseError(f"{source}:{board} malformed JobPosting JSON-LD") from error
        if isinstance(value, Mapping) and value.get("@type") == "JobPosting":
            if text(value.get("title")) and text(value.get("description")):
                return value
    raise BoardResponseError(f"{source}:{board} exact job lacks a full JobPosting description")


def _from_ld(source: str, board: str, external_id: str, url: str, job: Mapping) -> dict:
    description = text(job.get("description"))
    locations = normalize_locations(job.get("jobLocation"))
    # JSON-LD PostalAddress uses schema.org address keys, retained as source facts.
    address = job.get("jobLocation")
    if isinstance(address, list):
        address = address[0] if address else None
    address = address.get("address") if isinstance(address, Mapping) else None
    if isinstance(address, Mapping):
        place = ", ".join(filter(None, (text(address.get("addressLocality")), text(address.get("addressRegion")))))
        locations = normalize_locations(place, country=text(address.get("addressCountry")))
    return _posting(
        source, board, external_id, title=text(job.get("title")),
        location=location_text(locations, ""), locations=locations,
        url=url, apply_url=url, posted_at=text(job.get("datePosted")),
        published_at=actual_publication(job) or text(job.get("datePosted")),
        description_html=description, description_kind=description_kind(description),
        opportunity_type=canonical_opportunity_type(job.get("employmentType"), title=job.get("title"), description=description),
        listing_status="open", source_facts=dict(job),
    )


def _sitemap_board(source: str, board: str, sitemap: str, eligible) -> PostingBatch:
    urls = _xml_urls(_get_text(sitemap, source, board), source, board)
    jobs = [url for url in urls if eligible(url)]
    if len(set(jobs)) != len(jobs):
        raise BoardResponseError(f"{source}:{board} sitemap contains duplicate jobs")
    postings = []
    failures = 0
    for index, url in enumerate(jobs):
        if index:
            time.sleep(PACE_SECONDS)
        try:
            postings.append(_sitemap_detail(source, board, url))
        except (httpx.HTTPError, DiscoveryError, ValueError):
            failures += 1
    return PostingBatch(postings, complete=failures == 0, status="partial" if failures else "ok",
                        error=f"{failures} exact detail(s) unavailable" if failures else None)


def _sitemap_detail(source: str, board: str, url: str) -> dict:
    from urllib.parse import parse_qs

    parsed = urlparse(url)
    if source == "icims":
        match = JOB_PATH.fullmatch(parsed.path)
        valid = parsed.scheme == "https" and parsed.netloc == board and match is not None and not parsed.query
        external_id = match.group(1) if match else ""
        detail_url = url + "?in_iframe=1"
    elif source == "peopleclick":
        base = f"/careerscp/{board.replace('~', '/')}/"
        ids = parse_qs(parsed.query).get("jobPostId", [])
        valid = (parsed.scheme == "https" and parsed.netloc == "careers.peopleclick.com"
                 and parsed.path.startswith(base) and parsed.path.endswith("/gateway/viewFromLink.html")
                 and len(ids) == 1 and bool(re.fullmatch(r"\d+", ids[0])))
        external_id = ids[0] if ids else ""
        detail_url = url
    elif source == "talentbrew":
        valid = (parsed.scheme == "https" and parsed.netloc == board and not parsed.query
                 and re.fullmatch(r"/job/[^/]+/[^/]+/\d+/\d+", parsed.path) is not None)
        external_id = parsed.path.rsplit("/", 1)[-1]
        detail_url = url
    else:
        raise DiscoveryError(f"unsupported sitemap source {source}")
    if not valid:
        raise BoardResponseError(f"{source}:{board} invalid exact job URL")
    job = _job_ld(_get_text(detail_url, source, board), source, board)
    if text(job.get("url")) != url:
        raise BoardResponseError(f"{source}:{board} exact job URL mismatch")
    if source == "peopleclick":
        ld_id = job.get("identifier")
        if not isinstance(ld_id, Mapping) or text(ld_id.get("value")).split("_")[0] != external_id:
            raise BoardResponseError(f"{source}:{board} exact job identity mismatch")
    return _from_ld(source, board, external_id, url, job)


def fetch_icims(board: str) -> PostingBatch:
    if not ICIMS_HOST.fullmatch(board):
        raise DiscoveryError("invalid iCIMS board host")
    base = f"https://{board}"
    return _sitemap_board(
        "icims", board, base + "/sitemap.xml",
        lambda url: urlparse(url).netloc == board and JOB_PATH.fullmatch(urlparse(url).path) is not None,
    )


def fetch_talentbrew(board: str) -> PostingBatch:
    # Quest's employer site publishes the jobs in its sitemap. The Oracle
    # candidate application domain is a handoff, not the listing source.
    if board != "careers.questdiagnostics.com":
        raise DiscoveryError("unverified TalentBrew board host")
    base = f"https://{board}"
    return _sitemap_board(
        "talentbrew", board, base + "/sitemap.xml",
        lambda url: url.startswith(base + "/job/") and re.fullmatch(r"/job/[^/]+/[^/]+/\d+/\d+", urlparse(url).path) is not None,
    )


def fetch_peopleclick(board: str) -> PostingBatch:
    if not PEOPLECLICK_BOARD.fullmatch(board):
        raise DiscoveryError("invalid PeopleClick board token")
    base = f"https://careers.peopleclick.com/careerscp/{board.replace('~', '/')}/"
    from urllib.parse import parse_qs
    return _sitemap_board(
        "peopleclick", board, base + "sitemap.xml",
        lambda url: url.startswith(base) and "/gateway/viewFromLink.html?" in url and
        bool(re.fullmatch(r"\d+", parse_qs(urlparse(url).query).get("jobPostId", [""])[0])),
    )


def fetch_avature(board: str) -> PostingBatch:
    if not re.fullmatch(r"[a-z0-9-]+", board):
        raise DiscoveryError("invalid Avature board token")
    base = f"https://{board}.avature.net"
    path = "/en_US/careers/SearchJobs/"
    urls: list[str] = []
    for page in range(100):
        offset = page * 6
        html = _get_text(base + path + f"?jobOffset={offset}", "avature", board)
        if 'results results--listed' not in html:
            raise BoardResponseError(f"avature:{board} missing search results")
        found = list(dict.fromkeys(re.findall(
            rf'https://{re.escape(board)}\.avature\.net/en_US/careers/JobDetail/[^"<>\s]+/\d+', html,
        )))
        if not found and page:
            raise BoardResponseError(f"avature:{board} search page stopped early")
        if any(url in urls for url in found):
            raise BoardResponseError(f"avature:{board} repeated search page")
        urls.extend(found)
        if not re.search(rf'paginationLink[^>]*href="[^"]*jobOffset={offset + 6}(?:["&])', html):
            break
        time.sleep(PACE_SECONDS)
    else:
        raise BoardResponseError(f"avature:{board} search exceeded page ceiling")
    postings = []
    failures = 0
    for index, url in enumerate(urls):
        if index:
            time.sleep(PACE_SECONDS)
        try:
            postings.append(_avature_detail(board, url))
        except (httpx.HTTPError, DiscoveryError, ValueError, AttributeError, TypeError):
            failures += 1
    return PostingBatch(postings, complete=failures == 0,
                        status="partial" if failures else "ok",
                        error=f"{failures} exact detail(s) unavailable" if failures else None)


def _avature_detail(board: str, url: str) -> dict:
    parsed = urlparse(url)
    if (parsed.scheme != "https" or parsed.netloc != f"{board}.avature.net" or parsed.query
            or not re.fullmatch(r"/en_US/careers/JobDetail/[^/]+/\d+", parsed.path)):
        raise BoardResponseError(f"avature:{board} invalid exact job URL")
    html = _get_text(url, "avature", board)
    marker = 'var legacyViewCopilotData = '
    start = html.find(marker)
    if start == -1:
        raise BoardResponseError(f"avature:{board} missing job facts")
    facts, _ = json.JSONDecoder().raw_decode(html[start + len(marker):].lstrip())
    rows = facts.get("jobs_data") if isinstance(facts, Mapping) else None
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], Mapping):
        raise BoardResponseError(f"avature:{board} malformed job facts")
    job = rows[0]
    description = next((text(value) for key, value in job.items()
                        if key.startswith("schemaField_") and text(value).startswith("<")), "")
    external_id = parsed.path.rsplit("/", 1)[-1]
    title_match = re.search(r"<title>\s*(.*?)\s*-\s*-\s*" + re.escape(external_id) + r"\s*-", html, re.S | re.I)
    if not description or not title_match:
        raise BoardResponseError(f"avature:{board} job lacks title or description")
    title = unescape(title_match.group(1).strip())
    locations = normalize_locations(text(job.get("Location")))
    return _posting(
        "avature", board, external_id, title=title,
        location=location_text(locations, ""), locations=locations,
        url=url, apply_url=url, description_html=description,
        description_kind=description_kind(description),
        posted_at=text(job.get("Date published")), listing_status="open",
        opportunity_type=canonical_opportunity_type(job.get("Time Type"), title=title, description=description),
        source_facts=dict(job),
    )


def refresh_public_posting(posting: Mapping) -> dict:
    """Check one exact public detail, not thousands of unrelated Postings."""
    source, board = posting["source"], posting["board"]
    url = posting.get("url")
    if not isinstance(url, str):
        raise BoardResponseError(f"{source}:{board} missing exact job URL")
    if source == "avature":
        refreshed = _avature_detail(board, url)
    else:
        refreshed = _sitemap_detail(source, board, url)
    if refreshed["external_id"] != posting.get("external_id"):
        raise BoardResponseError(f"{source}:{board} exact job identity mismatch")
    return refreshed


def fetch_workable(board: str) -> PostingBatch:
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", board):
        raise DiscoveryError("invalid Workable board token")
    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{board}",
                     "workable", board, params={"details": "true"}, headers=UA, timeout=TIMEOUT)
    if not isinstance(data, Mapping) or not isinstance(data.get("jobs"), list):
        raise BoardResponseError(f"workable:{board} missing jobs list")
    postings = []
    for job in data["jobs"]:
        if not isinstance(job, Mapping) or not text(job.get("shortcode")) or not text(job.get("title")) or not text(job.get("description")):
            raise BoardResponseError(f"workable:{board} malformed or incomplete job")
        external_id = text(job["shortcode"])
        locations = normalize_locations(job.get("locations") or ", ".join(filter(None, (text(job.get("city")), text(job.get("state"))))))
        description = text(job["description"])
        url = f"https://apply.workable.com/j/{external_id}"
        postings.append(_posting(
            "workable", board, external_id, title=text(job["title"]),
            location=location_text(locations, ""), locations=locations,
            url=url, apply_url=url + "/apply", posted_at=text(job.get("published_on")),
            published_at=text(job.get("published_on")) or None,
            description_html=description, description_kind=description_kind(description),
            opportunity_type=canonical_opportunity_type(job.get("employment_type"), title=job.get("title"), description=description),
            listing_status="open", source_facts=dict(job),
        ))
    if len({p["key"] for p in postings}) != len(postings) or _response_indicates_partial(data):
        raise BoardResponseError(f"workable:{board} incomplete or duplicate listing")
    return PostingBatch(postings)

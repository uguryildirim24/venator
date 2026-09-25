"""Resolve a pasted URL to a board token, confirm it live, and print what to add.

    python -m venator.discover.register URL [URL ...]

The board registry is the whole coverage story: a Source only ever answers for
an employer somebody registered. One paste buys that employer's future Postings
for good, so this turns the URL a person actually ends up holding — since
LinkedIn's free-posting cap sends applicants to the employer's own career site,
that is usually the career site — into the two lines the Profile needs.

Three shapes are accepted:

1. A direct ATS board URL — including public iCIMS, Workable, Avature,
   PeopleClick and Quest's TalentBrew site. The URL itself is
   matched against ``harvest.BOARD_URL_PATTERNS``, anchored at its start, so the
   pasted URL must *be* a board URL rather than merely mention one.
2. A Workday careers-site URL in any of its variants — ``/{site}``,
   ``/{locale}/{site}``, a per-job page under either, or the bare tenant host
   with no site at all (which is resolved through step 3's ``robots.txt``).
3. **An employer's own careers page.** Its HTML is scanned for Greenhouse, Lever,
   Ashby, SmartRecruiters, iCIMS, Workable, Avature, PeopleClick and TalentBrew board URLs,
   and for a ``myworkdayjobs`` tenant host; each tenant
   host found is then asked for ``robots.txt``, which enumerates the tenant's
   public site strings. That two-step is how a Workday site string is learned
   rather than guessed — it is not derivable from the employer's name, and a
   careers-page *redirect* does not reveal it either.

Every resolved board is then probed against the real adapter and reported with
the number of Postings it answered with. **A board that does not answer is
reported as unconfirmed, never dropped**, and never recorded.

Nothing here edits a Profile. ``targeting.yaml`` is a commented file that
documents the registry, and rewriting it would destroy those comments; merging a
board into ``sources`` is the Owner's call, exactly as it is for
``discover.harvest``. This prints the lines to paste, and ``--record`` files the
confirmed ones in ``data/board-suggestions.yaml``.

Read-only HTTP: every request this module makes is a GET, and none of them
follows a redirect. A pasted URL is untrusted — it arrives from a job board, an
email or a recruiter — so the scheme must be ``http``/``https``, and a URL
carrying credentials before its host or naming a bare address or ``localhost``
is refused. Page *content* is untrusted too, and the only host it can ever lead
to is ``<prefix>.myworkdayjobs.com``, built here from a matched tenant prefix
under a fixed suffix: a document can name a Workday tenant, and cannot name a
host. Everything else taken out of a page is a board token, spent against the
vendor's own API.
"""

from __future__ import annotations

import argparse
import html as html_module
import ipaddress
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
import yaml

from venator.discover.adapters import ADAPTERS, UA, PartialBoardError, PostingBatch, fetch_workday, fetch_workday_window
from venator.discover.harvest import (
    BOARD_URL_PATTERNS,
    board_token,
    extract_board_tokens,
    registered_boards,
    write_suggestions,
)
from venator.paths import STORE_HELP, SUGGESTIONS_FILE, StoreRootError, resolve_store_paths
from venator.profile import (
    Profile,
    ProfileError,
    add_profile_argument,
    announce_profile,
    profile_from_arguments,
)

TIMEOUT = 20.0

#: How much of a fetched document is scanned. A careers page is a page, not a
#: corpus; anything past this is chrome, and scanning it costs time without
#: turning up a board that the first megabyte did not.
SCAN_LIMIT = 1_000_000

#: The most site strings taken from one tenant's ``robots.txt``. Every one costs
#: a listing request to confirm, and a file that enumerates more than this is
#: not enumerating careers sites. Trimming is printed, never silent.
SITES_PER_TENANT = 8

#: The employer display name is never invented here, and this is what stands in
#: its place until the Owner writes the real one. It is deliberately unusable as
#: a name: an employer this pipeline cannot name should look broken rather than
#: quietly become an employer called something wrong.
NAME_PLACEHOLDER = "REPLACE-WITH-EMPLOYER-NAME"

#: A ``myworkdayjobs`` tenant host as it appears inside a careers page — in an
#: href, a script string or a JSON blob, so no scheme is required. The
#: surrounding lookarounds are what make ``evil-amgen.wd1.myworkdayjobs.com``
#: and ``amgen.wd1.myworkdayjobs.com.evil.example`` both fail to match.
WORKDAY_TENANT_HOST = re.compile(
    r"(?<![A-Za-z0-9.-])([A-Za-z0-9][A-Za-z0-9_-]*\.wd\d+)\.myworkdayjobs\.com(?![A-Za-z0-9.-])",
    re.IGNORECASE,
)

#: A ``robots.txt`` path segment that is a locale rather than a site string.
LOCALE_SEGMENT = re.compile(r"^[A-Za-z]{2}(?:-[A-Za-z]{2,4})?$")

#: A path segment that could be a Workday site string. Anything with a wildcard
#: in it is a robots pattern, not a site.
SITE_SEGMENT = re.compile(r"^[A-Za-z0-9._-]+$")

TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


class RegisterError(RuntimeError):
    """A pasted URL could not be turned into a board token with any confidence."""


@dataclass(frozen=True)
class Board:
    """One ``(source, board)`` pair a pasted URL resolved to, and how."""

    source: str
    board: str
    origin: str
    route: str


@dataclass(frozen=True)
class Confirmation:
    """What the real adapter answered for one resolved board."""

    board: Board
    confirmed: bool
    posting_count: int
    detail: str
    complete: bool = True


Reader = Callable[[str], str]


# ---------------------------------------------------------------------------
# Reading a pasted URL
# ---------------------------------------------------------------------------

def normalize_url(raw: str) -> str:
    """Refuse anything that is not a plain ``http``/``https`` URL naming a host.

    A pasted URL is untrusted input, and this is the only place its shape is
    checked. ``javascript:`` and ``data:`` are refused outright; so is a URL
    carrying credentials, which is the oldest way to make a hostile host read as
    a familiar one (``https://boards.greenhouse.io@evil.example/x``); so is a
    bare address or ``localhost``, which would point a discovery fetch at
    something on this machine or its network rather than at an employer.
    """
    text = raw.strip()
    parsed = urlparse(text)
    if parsed.scheme.lower() not in {"http", "https"}:
        raise RegisterError(
            f"{text!r} is not an http(s) URL — this reads employer pages over http(s) only, "
            "and a URL with no scheme is not assumed to be one. Paste it with https:// in front."
        )
    if parsed.username or parsed.password:
        raise RegisterError(
            f"{text!r} carries credentials before its host, which hides the host the request "
            "would actually go to. Refused."
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise RegisterError(f"{text!r} names no host.")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise RegisterError(
            f"{text!r} names a bare address rather than an employer's host. Refused: a "
            "discovery fetch goes to employers, not to whatever answers on this network."
        )
    if host == "localhost" or "." not in host:
        raise RegisterError(
            f"{text!r} names {host!r}, which is on this machine, not an employer's host. Refused."
        )
    return text


def read_url(url: str) -> str:
    """GET one document as text, refusing a redirect rather than following it.

    ``follow_redirects=False`` is httpx's default and is written out because it
    is load-bearing, exactly as ``adapters.fetch_workday`` writes it out: the
    only host contacted is the one that was asked for, so a pasted URL cannot
    steer this fetch to an arbitrary server. A 3xx is surfaced by name — it
    would otherwise fall through ``raise_for_status``, which raises only on 4xx
    and 5xx, and turn into an empty document that explains nothing.

    A careers-page redirect is not a resolution route anyway: following one to
    the ATS was tried and does not reveal a Workday site string.
    """
    response = httpx.get(url, headers=UA, timeout=TIMEOUT, follow_redirects=False)
    if response.is_redirect:
        raise RegisterError(
            f"{url} answered {response.status_code} redirecting to "
            f"{response.headers.get('location', '<no location>')!r}. Redirects are not followed: "
            "the only host contacted is the one that was pasted. Paste the destination instead."
        )
    response.raise_for_status()
    return response.text[:SCAN_LIMIT]


# ---------------------------------------------------------------------------
# Resolving
# ---------------------------------------------------------------------------

def board_from_url(url: str) -> list[tuple[str, str]]:
    """The ``(source, board)`` pairs the pasted URL *is*, anchored at its start.

    ``harvest.extract_board_tokens`` searches anywhere in a text, which is right
    for a Posting description and wrong for a paste: it would read
    ``https://elsewhere.example/?next=https://jobs.lever.co/acme`` as a Lever
    board URL. Matching from position 0 means the pasted URL has to be the board
    URL itself. Hostname lookalikes cannot match either way — every pattern
    requires the scheme immediately before the ATS host and a ``/`` immediately
    after it.
    """
    found: list[tuple[str, str]] = []
    for source, patterns in BOARD_URL_PATTERNS.items():
        for pattern in patterns:
            match = pattern.match(url)
            if match is None:
                continue
            pair = (source, board_token(match))
            if pair not in found:
                found.append(pair)
    return found


def workday_tenant_hosts(text: str) -> list[str]:
    """Every ``myworkdayjobs`` tenant host named in a document, lowercased."""
    hosts: list[str] = []
    for match in WORKDAY_TENANT_HOST.finditer(html_module.unescape(text)):
        host = f"{match.group(1).lower()}.myworkdayjobs.com"
        if host not in hosts:
            hosts.append(host)
    return hosts


def _site_segment(path: str) -> str | None:
    """The site string a ``robots.txt`` path names, or ``None``."""
    segments = [segment for segment in path.split("/") if segment]
    if segments and LOCALE_SEGMENT.fullmatch(segments[0]) and len(segments) > 1:
        segments = segments[1:]
    if not segments:
        return None
    segment = segments[0]
    if segment.lower() == "wday" or not SITE_SEGMENT.fullmatch(segment):
        return None
    return segment


def sites_from_robots(host: str, text: str) -> list[str]:
    """The site strings one tenant's ``robots.txt`` enumerates.

    This is the step that makes a Workday board registrable at all. A site
    string is an arbitrary word the employer chose — ``Careers``,
    ``PfizerCareers``, ``External`` — and nothing about the employer predicts
    it, so the alternative to reading it here is guessing it, which the adapter
    refuses to let anyone do quietly.

    Read from ``Sitemap:`` and ``Allow:`` lines only: those name paths the
    tenant publishes. ``Disallow:`` names what a crawler must not fetch, which
    is a different question. A ``Sitemap:`` URL is pinned to ``host`` before its
    site is taken, so a file naming some other tenant cannot register that
    tenant here. Anything read out is still only a proposal — each one is probed
    against the live board, and one that does not answer is reported unconfirmed.
    """
    sites: list[str] = []

    def keep(site: str | None) -> None:
        if site and site not in sites:
            sites.append(site)

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        key, separator, value = line.partition(":")
        if not separator:
            continue
        field = key.strip().lower()
        value = value.strip()
        if field == "sitemap":
            for pattern in BOARD_URL_PATTERNS["workday"]:
                for match in pattern.finditer(value):
                    if f"{match.group(1).lower()}.myworkdayjobs.com" == host.lower():
                        keep(match.group("site"))
        elif field == "allow":
            keep(_site_segment(value))
    return sites


def resolve(url: str, read: Reader = read_url) -> tuple[list[Board], str | None]:
    """Resolve one pasted URL into boards, and the page title if one was fetched.

    Sequential throughout, no retries: at most one GET for the pasted page and
    one more per tenant host it names.
    """
    normalized = normalize_url(url)
    direct = board_from_url(normalized)
    if direct:
        return [Board(source, board, normalized, "pasted URL") for source, board in direct], None

    host = (urlparse(normalized).hostname or "").lower()
    boards: list[Board] = []
    title: str | None = None
    if host.endswith(".myworkdayjobs.com"):
        # A tenant host with no readable site — the bare host, or a path this
        # does not recognize. robots.txt answers it without fetching the page.
        hosts = [host]
    else:
        page = read(normalized)
        title = page_title(page)
        for source, board in sorted(extract_board_tokens(page)):
            boards.append(Board(source, board, normalized, "careers page"))
        hosts = workday_tenant_hosts(page)

    # Every tenant host the page named is asked for its robots.txt, including one
    # whose site a URL on the page already gave: the file enumerates *all* of the
    # tenant's public sites, and an employer's early-career site is routinely a
    # second site string that no link on the front page points at.
    for tenant in hosts:
        sites = sites_from_robots(tenant, read(f"https://{tenant}/robots.txt"))
        if len(sites) > SITES_PER_TENANT:
            print(f"{tenant}: robots.txt named {len(sites)} sites; probing the first {SITES_PER_TENANT}")
            sites = sites[:SITES_PER_TENANT]
        prefix = tenant.split(".myworkdayjobs.com")[0]
        for site in sites:
            board = Board("workday", f"{prefix}~{site}", normalized, f"{tenant}/robots.txt")
            if board.board not in {existing.board for existing in boards}:
                boards.append(board)
    if not boards:
        raise RegisterError(
            f"{normalized} named no supported public job board. If this is the "
            "employer's careers page, its apply links may be rendered by script rather than "
            "written into the HTML — open one Posting on it and paste that Posting's URL instead."
        )
    return boards, title


def page_title(page: str) -> str | None:
    """The document's ``<title>``, offered as a hint and never as a display name."""
    match = TITLE.search(page)
    if match is None:
        return None
    title = " ".join(html_module.unescape(match.group(1)).split())
    return title[:80] or None


# ---------------------------------------------------------------------------
# Confirming
# ---------------------------------------------------------------------------

def confirm(boards: Iterable[Board]) -> list[Confirmation]:
    """Probe each board against its real adapter, one at a time.

    The same probe ``harvest.probe_candidates`` runs, with one difference: a
    board that does not answer is kept in the results as unconfirmed instead of
    being skipped. A paste is a question the Owner asked, and "this did not
    answer" is the answer to it.
    """
    results: list[Confirmation] = []
    for board in boards:
        try:
            fetcher = ADAPTERS[board.source]
            postings = (fetch_workday_window(board.board, max_pages=1)
                        if board.source == "workday" and fetcher is fetch_workday
                        else fetcher(board.board))
        except PartialBoardError as err:
            if err.postings:
                results.append(Confirmation(board, True, len(err.postings), str(err), complete=False))
                print(f"{board.source}:{board.board}: confirmed (at least {len(err.postings)} Postings; partial listing)")
            else:
                results.append(Confirmation(board, False, 0, str(err), complete=False))
            continue
        except Exception as err:
            print(f"{board.source}:{board.board}: not confirmed — {err}")
            results.append(Confirmation(board, False, 0, str(err)))
            continue
        complete = not isinstance(postings, PostingBatch) or postings.complete
        confirmed = bool(postings) or complete
        prefix = "" if complete else "at least "
        print(f"{board.source}:{board.board}: {'confirmed' if confirmed else 'not confirmed'} ({prefix}{len(postings)} Postings, via {board.route})")
        results.append(Confirmation(board, confirmed, len(postings), "" if complete else "Partial listing", complete=complete))
    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def yaml_lines(results: Iterable[Confirmation], names: dict[str, str]) -> list[str]:
    """The exact lines to paste under ``sources:`` in the Profile's targeting.yaml.

    Both halves are printed, always. ``sources.boards`` is what Discover polls;
    ``sources.names`` is what Dedup resolves an ATS Posting's employer through,
    and a board registered without a name there has no employer at all, so every
    Posting from it drops out of every duplicate group and the Owner can be shown
    the same job twice. ``sources.names`` is inside the ``filters_version`` hash
    for that reason and ``sources.boards`` is deliberately outside it.
    """
    confirmed = [result for result in results if result.confirmed]
    if not confirmed:
        return []
    lines = ["  names:"]
    for result in confirmed:
        board = result.board.board
        lines.append(f"    {board}: {names.get(board, NAME_PLACEHOLDER)}")
    lines.append("  boards:")
    for source in sorted({result.board.source for result in confirmed}):
        lines.append(f"    {source}:")
        for result in confirmed:
            if result.board.source == source:
                lines.append(
                    f"      - {result.board.board} # {names.get(result.board.board, NAME_PLACEHOLDER)}"
                    f" — {result.posting_count} Postings when confirmed"
                )
    return lines


def record(
    results: Iterable[Confirmation], names: dict[str, str], path: Path | None = None
) -> list[dict]:
    """File the confirmed boards in ``data/board-suggestions.yaml``.

    Written through ``harvest.write_suggestions`` in ``harvest``'s own shape, and
    merged with what is already in the file rather than replacing it, so
    recording a paste does not throw away what the harvester found. Still only a
    suggestion: nothing here reaches a Profile.
    """
    path = resolve_store_paths(suggestions_path=path)["suggestions_path"]
    existing: list[dict] = []
    if path.exists():
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            existing = [entry for entry in (payload.get("suggestions") or []) if isinstance(entry, dict)]
    merged = {(entry.get("source"), entry.get("board")): entry for entry in existing}
    for result in results:
        if not result.confirmed:
            continue
        merged[(result.board.source, result.board.board)] = {
            "source": result.board.source,
            "board": result.board.board,
            "company": names.get(result.board.board, NAME_PLACEHOLDER),
            "posting_count": result.posting_count,
        }
    suggestions = [merged[key] for key in sorted(merged, key=lambda key: (str(key[0]), str(key[1])))]
    write_suggestions(suggestions, path)
    return suggestions


def report(
    results: list[Confirmation],
    names: dict[str, str],
    profile: Profile,
    registered: set[tuple[str, str]],
    titles: dict[str, str],
) -> None:
    """Print what was resolved, what answered, and what to paste.

    The block to paste is printed last, so it is the thing on screen when this
    finishes; what was already registered and what did not answer are said
    first, because they are context for it rather than part of it.
    """
    fresh: list[Confirmation] = []
    for result in results:
        pair = (result.board.source, result.board.board)
        if pair in registered:
            print(f"{result.board.source}:{result.board.board}: already registered in {profile.targeting_path}")
        else:
            fresh.append(result)
    unconfirmed = [result for result in fresh if not result.confirmed]
    if unconfirmed:
        print()
    for result in unconfirmed:
        print(
            f"unconfirmed, so nothing to add: {result.board.source}:{result.board.board} "
            f"(from {result.board.route}) — {result.detail}"
        )
    lines = yaml_lines(fresh, names)
    print()
    if not lines:
        print("Nothing to add: no new board answered.")
        return
    for origin, title in titles.items():
        print(f"page title at {origin}: {title}")
    if titles:
        print()
    print(f"Paste under `sources:` in {profile.targeting_path} — the Owner merges, never this tool:")
    print()
    for line in lines:
        print(line)
    if any(result.board.board not in names for result in fresh if result.confirmed):
        print()
        print(f"Write the employer's real name over every {NAME_PLACEHOLDER} above. A board with")
        print("no `sources.names` entry has no employer at all, so Dedup drops its Postings out of")
        print("every duplicate group and the same job can reach the Owner twice. `sources.names` is")
        print("inside the filters_version hash and `sources.boards` is not: naming an employer")
        print("replays the corpus, which is correct, and registering a board alone does not.")


def run(
    urls: Iterable[str],
    profile: Profile,
    name: str | None = None,
    suggestions_path: Path | None = None,
    read: Reader = read_url,
) -> list[Confirmation]:
    """Resolve every pasted URL, probe what came back, and report it."""
    boards: list[Board] = []
    seen: set[tuple[str, str]] = set()
    titles: dict[str, str] = {}
    for url in urls:
        try:
            found, title = resolve(url, read)
        except RegisterError as err:
            print(f"{url}: {err}")
            continue
        except Exception as err:
            print(f"{url}: could not be read — {err}")
            continue
        if title:
            titles[url] = title
        for board in found:
            if (board.source, board.board) not in seen:
                seen.add((board.source, board.board))
                boards.append(board)
    results = confirm(boards)
    # `--name` can only be applied here: the board token it belongs to is not
    # known until the URL has been resolved.
    names = {result.board.board: name for result in results if name} if name else {}
    registered = registered_boards(profile)
    fresh = [
        result for result in results
        if (result.board.source, result.board.board) not in registered
    ]
    report(results, names, profile, registered, titles)
    if suggestions_path is not None:
        suggestions = record(fresh, names, suggestions_path)
        print(f"\nrecorded {len(suggestions)} suggestions in {suggestions_path}")
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("urls", nargs="+", metavar="URL", help="a board URL or an employer's careers page")
    parser.add_argument(
        "--name",
        help="the employer's display name for sources.names (one URL only; "
        f"otherwise {NAME_PLACEHOLDER} is printed for the Owner to replace)",
    )
    parser.add_argument(
        "--record",
        action="store_true",
        help=f"also file the confirmed boards in {SUGGESTIONS_FILE} (never in a Profile)",
    )
    parser.add_argument("--suggestions", type=Path, default=None, help=STORE_HELP)
    add_profile_argument(parser)
    args = parser.parse_args()
    if args.name and len(args.urls) > 1:
        parser.error("--name names one employer, so it takes one URL")
    try:
        profile = profile_from_arguments(args)
    except ProfileError as error:
        parser.error(str(error))
    announce_profile(profile)
    suggestions_path = None
    if args.record:
        try:
            suggestions_path = resolve_store_paths(suggestions_path=args.suggestions)[
                "suggestions_path"
            ]
        except StoreRootError as error:
            parser.error(str(error))
    run(args.urls, profile, args.name, suggestions_path)


if __name__ == "__main__":
    main()

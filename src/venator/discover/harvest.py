"""Suggest ATS boards linked by discovered Postings.

The harvester only writes suggestions. It never edits the curated board
registry; the Owner decides which suggestions should be merged.

Usage:
    python -m venator.discover.harvest [--profile NAME]
"""

from __future__ import annotations

import argparse
import html
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

import yaml

from venator.discover.adapters import ADAPTERS, transient_network_error
from venator.paths import STORE_HELP, StoreRootError, resolve_store_paths
from venator.profile import (
    Profile,
    ProfileError,
    add_profile_argument,
    announce_profile,
    profile_from_arguments,
)

BOARD_URL_PATTERNS = {
    "talentbrew": (re.compile(r"https?://(careers\.questdiagnostics\.com)/(?:job/|$)", re.I),),
    "icims": (re.compile(r"https?://([a-z0-9-]+\.icims\.com)/jobs(?:/|\?|$)", re.I),),
    "peopleclick": (re.compile(r"https?://careers\.peopleclick\.com/careerscp/(client_[a-z0-9_-]+)/(?P<site>[a-z0-9_-]+)(?:/|$)", re.I),),
    "workable": (re.compile(r"https?://apply\.workable\.com/([a-z0-9-]+)(?:/|$)", re.I),),
    "avature": (re.compile(r"https?://([a-z0-9-]+)\.avature\.net/(?:en_US/)?careers(?:/|$)", re.I),),
    "greenhouse": (
        re.compile(
            r"https?://(?:www\.)?(?:job-boards|boards)\.greenhouse\.io/"
            r"(?!embed(?:[/#?]))([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"https?://(?:www\.)?boards\.greenhouse\.io/embed/"
            r"(?:job_board|job_app)\?[^\s\"'<>]*?\bfor=([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"https?://boards-api\.greenhouse\.io/v1/boards/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    ),
    "lever": (
        re.compile(
            r"https?://(?:www\.)?jobs(?:\.eu)?\.lever\.co/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"https?://api\.lever\.co/v0/postings/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    ),
    "ashby": (
        re.compile(
            r"https?://(?:www\.)?jobs\.ashbyhq\.com/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"https?://api\.ashbyhq\.com/posting-api/job-board/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
    ),
    "smartrecruiters": (
        re.compile(
            r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]+)",
            re.IGNORECASE,
        ),
        re.compile(
            r"https?://api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9_-]+)/postings",
            re.IGNORECASE,
        ),
    ),
    # Workday. Group 1 is the host prefix (`amgen.wd1`) and the named `site`
    # group is the site string (`Careers`); `board_token` joins them into the
    # adapter's `<tenant>.wd<N>~<site>`. Both patterns require the scheme
    # immediately before the host and `myworkdayjobs.com/` immediately after the
    # instance, so no lookalike host (`myworkdayjobs.com.evil.com`) can match.
    "workday": (
        # A careers-site URL: the site root, the localized root, or any per-job
        # page under either.
        #   https://amgen.wd1.myworkdayjobs.com/Careers
        #   https://amgen.wd1.myworkdayjobs.com/en-US/Careers
        #   https://pfizer.wd1.myworkdayjobs.com/en-US/PfizerCareers/job/x/y_R1
        # The optional locale segment is tried first, so a bare `/Careers` is
        # read as the site and never as a locale.
        re.compile(
            r"https?://([A-Za-z0-9][A-Za-z0-9_-]*\.wd\d+)\.myworkdayjobs\.com/"
            r"(?:[A-Za-z]{2}(?:-[A-Za-z]{2,4})?/)?"
            r"(?!wday/)(?P<site>[A-Za-z0-9._-]+)",
            re.IGNORECASE,
        ),
        # The listing endpoint the careers site's own client calls, which turns
        # up in page source and in developer-tools copy-paste.
        re.compile(
            r"https?://([A-Za-z0-9][A-Za-z0-9_-]*\.wd\d+)\.myworkdayjobs\.com/"
            r"wday/cxs/[A-Za-z0-9_-]+/(?P<site>[A-Za-z0-9._-]+)",
            re.IGNORECASE,
        ),
    ),
}


def board_token(match: re.Match[str]) -> str:
    """The board token one ``BOARD_URL_PATTERNS`` match names.

    **Capture group 1 is the board token.** That held for every Source until
    Workday, whose board needs two identifiers — the host prefix and the site
    string — and the contract is widened by exactly one rule rather than by a
    special case per Source: *a pattern may name a second group ``site``, and
    the token is then* ``<group 1>~<site>``, the join
    ``adapters.parse_workday_board`` already reads. Group 1 stays the token
    everywhere; ``site`` only extends it.

    Group 1 is lowercased only when a ``site`` group is present. In that shape
    group 1 is a hostname prefix, and hostnames are case-insensitive, so
    ``Amgen.wd1`` and ``amgen.wd1`` must not become two boards. On its own,
    group 1 is a URL *path* segment — Greenhouse's ``Example_Bio`` is
    case-sensitive and is left exactly as written.
    """
    site = match.groupdict().get("site")
    return f"{match.group(1).lower()}~{site}" if site else match.group(1)


def extract_board_tokens(text: str) -> set[tuple[str, str]]:
    """Return ``(source, board)`` pairs found in URLs within arbitrary text."""
    decoded = html.unescape(text)
    found = set()
    for source, patterns in BOARD_URL_PATTERNS.items():
        for pattern in patterns:
            found.update((source, board_token(match)) for match in pattern.finditer(decoded))
    return found


def load_stored_postings(postings_dir: Path | None = None) -> list[dict]:
    postings_dir = resolve_store_paths(postings_dir=postings_dir)["postings_dir"]
    from venator.discover.store import load_postings
    return load_postings(postings_dir)


def find_candidates(postings: Iterable[dict]) -> dict[tuple[str, str], str]:
    """Find ATS candidates and retain the best available company label."""
    candidates: dict[tuple[str, str], str] = {}
    for posting in postings:
        texts = (posting.get("url") or "", posting.get("description_html") or "")
        company = posting.get("company", "")
        for text in texts:
            for source, board in extract_board_tokens(text):
                key = (source, board)
                if key not in candidates or (company and candidates[key] == board):
                    candidates[key] = company or board
    return candidates


def load_registered(registry_path: Path) -> set[tuple[str, str]]:
    """Read a standalone board registry file (``sources:`` mapping)."""
    registry = yaml.safe_load(registry_path.read_text(encoding="utf-8")) or {}
    return {
        (source, board)
        for source, boards in (registry.get("sources") or {}).items()
        for board in boards or []
    }


def registered_boards(profile: Profile) -> set[tuple[str, str]]:
    """The ``(source, board)`` pairs the Profile already polls."""
    return {
        (source, board)
        for source, boards in profile.sources.boards.items()
        for board in boards or []
    }


def probe_candidates(
    candidates: dict[tuple[str, str], str],
    registered: set[tuple[str, str]],
) -> list[dict]:
    """Probe unregistered candidates one at a time and return live boards."""
    suggestions = []
    pending: list[tuple[str, str, str]] = []

    def probe(source: str, board: str, company: str, *, final: bool = False) -> None:
        try:
            postings = ADAPTERS[source](board)
        except Exception as err:
            if not final and transient_network_error(err):
                pending.append((source, board, company))
            else:
                print(f"{source}:{board}: not confirmed — {err}")
            return
        print(f"{source}:{board}: confirmed ({len(postings)} postings)")
        suggestions.append(
            {
                "source": source,
                "board": board,
                "company": company,
                "posting_count": len(postings),
            }
        )

    for (source, board), company in sorted(candidates.items()):
        if (source, board) not in registered:
            probe(source, board, company)
    for source, board, company in pending:
        print(f"{source}:{board}: retrying after candidate pass")
        probe(source, board, company, final=True)
    return suggestions


def write_suggestions(suggestions: list[dict], path: Path | None = None) -> None:
    path = resolve_store_paths(suggestions_path=path)["suggestions_path"]
    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "suggestions": suggestions,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(payload, sort_keys=False), encoding="utf-8", newline="\n"
    )


def run(
    profile: Profile,
    postings_dir: Path | None = None,
    registry_path: Path | None = None,
    suggestions_path: Path | None = None,
) -> list[dict]:
    stores = resolve_store_paths(
        postings_dir=postings_dir, suggestions_path=suggestions_path
    )
    postings_dir = stores["postings_dir"]
    suggestions_path = stores["suggestions_path"]
    postings = load_stored_postings(postings_dir)
    candidates = find_candidates(postings)
    registered = (
        load_registered(registry_path) if registry_path is not None else registered_boards(profile)
    )
    suggestions = probe_candidates(candidates, registered)
    write_suggestions(suggestions, suggestions_path)
    print(f"wrote {len(suggestions)} suggestions to {suggestions_path}")
    return suggestions


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postings-dir", type=Path, default=None, help=STORE_HELP)
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="read the already-registered boards from this file instead of the Profile",
    )
    parser.add_argument("--output", type=Path, default=None, help=STORE_HELP)
    add_profile_argument(parser)
    args = parser.parse_args()
    try:
        profile = profile_from_arguments(args)
    except ProfileError as error:
        parser.error(str(error))
    announce_profile(profile)
    try:
        stores = resolve_store_paths(
            postings_dir=args.postings_dir, suggestions_path=args.output
        )
    except StoreRootError as error:
        parser.error(str(error))
    run(profile, stores["postings_dir"], args.registry, stores["suggestions_path"])


if __name__ == "__main__":
    main()

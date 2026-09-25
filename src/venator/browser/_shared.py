"""Shared safety and artifact helpers for dry-run browser operations."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from playwright.sync_api import BrowserContext, Page, Route

from venator.match.store import load_postings

POSTINGS_DIR = Path("data/postings")
OUTPUT_ROOT = Path("build/fill")
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def artifact_dir(posting_key: str, output_root: Path = OUTPUT_ROOT) -> Path:
    path = output_root / posting_key.replace(":", "-")
    path.mkdir(parents=True, exist_ok=True)
    return path


def posting_url(posting_key: str, postings_dir: Path = POSTINGS_DIR) -> str:
    for posting in load_postings(postings_dir):
        if posting["key"] == posting_key:
            url = posting.get("url")
            if not isinstance(url, str) or not url:
                raise ValueError(f"Posting has no application URL: {posting_key!r}")
            return url
    raise ValueError(f"unknown posting_key: {posting_key!r}")


def write_json(path: Path, value: object) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def install_dry_run_guards(context: BrowserContext) -> tuple[list[str], Callable[[Route], None]]:
    """Block state-changing requests and record any attempted form submission."""
    blocked_requests: list[str] = []

    def guard_route(route: Route) -> None:
        if route.request.method.upper() in STATE_CHANGING_METHODS:
            blocked_requests.append(f"{route.request.method} {route.request.url}")
            route.abort()
        else:
            route.continue_()

    context.route("**/*", guard_route)
    context.add_init_script(
        """
        window.__venatorSubmitEvents = 0;
        document.addEventListener("submit", (event) => {
          window.__venatorSubmitEvents += 1;
          event.preventDefault();
        }, true);
        """
    )
    return blocked_requests, guard_route


def submit_event_count(page: Page) -> int:
    value = page.evaluate("() => window.__venatorSubmitEvents || 0")
    return int(value)

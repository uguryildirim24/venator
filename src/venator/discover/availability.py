"""Corroborate missing Lever API records with their public job resource."""

from collections.abc import Mapping
from urllib.parse import quote

import httpx


def lever_public_closure(
    board: str,
    external_id: str,
    *,
    headers: Mapping[str, str],
    timeout: float,
) -> tuple[bool, str]:
    """Make one public GET; only an explicit missing resource confirms closure.

    Lever can omit a posting from its API while still serving its application
    page. A reachable page is conflicting evidence, not proof of active hiring.
    """
    url = f"https://jobs.lever.co/{quote(board, safe='')}/{quote(external_id, safe='')}"
    try:
        response = httpx.get(url, headers=headers, timeout=timeout, follow_redirects=False)
    except (httpx.HTTPError, TimeoutError):
        return False, "Lever API job unavailable; public job verification failed"
    status = response.status_code
    if status in {404, 410}:
        return True, f"Lever API and public job unavailable (public HTTP {status})"
    return False, f"Lever API job unavailable; public HTTP {status} does not confirm closure"

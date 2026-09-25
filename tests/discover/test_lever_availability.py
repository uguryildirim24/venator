from unittest.mock import patch

import httpx
import pytest

from venator.discover.adapters import refresh_posting


def saved_posting() -> dict:
    return {
        "key": "lever:bioagilytix:18824e5b-f03a-4cfa-9746-9c29d15877d5",
        "source": "lever",
        "board": "bioagilytix",
        "external_id": "18824e5b-f03a-4cfa-9746-9c29d15877d5",
        "title": "Scientist (PK)",
        "url": "https://untrusted.example/redirect",
        "description_html": "<p>Original employer requirements.</p>",
        "description_kind": "full",
        "listing_status": "open",
    }


@pytest.mark.parametrize("api_status", [404, 410])
@pytest.mark.parametrize(
    ("public_status", "expected"),
    [(200, "unknown"), (404, "closed"), (410, "closed"), (302, "unknown"),
     (401, "unknown"), (403, "unknown"), (429, "unknown"), (503, "unknown")],
)
def test_api_missing_requires_public_closure(api_status: int, public_status: int, expected: str) -> None:
    original = saved_posting()
    with patch("httpx.get", side_effect=[httpx.Response(api_status), httpx.Response(public_status)]) as get:
        result = refresh_posting(original)
    assert result["listing_status"] == expected
    assert result["verification_status"] == ("verified" if expected == "closed" else "unknown")
    assert result["key"] == original["key"]
    assert result["external_id"] == original["external_id"]
    assert result["description_html"] == original["description_html"]
    assert original["listing_status"] == "open"
    assert get.call_count == 2
    public_call = get.call_args_list[1]
    assert public_call.args == (
        "https://jobs.lever.co/bioagilytix/18824e5b-f03a-4cfa-9746-9c29d15877d5",
    )
    assert public_call.kwargs["follow_redirects"] is False
    assert public_call.kwargs["timeout"] == 20.0


@pytest.mark.parametrize("error", [httpx.ConnectError("offline"), httpx.ReadTimeout("slow"), TimeoutError()])
def test_public_network_failure_retains_unknown(error: Exception) -> None:
    original = saved_posting()
    with patch("httpx.get", side_effect=[httpx.Response(404), error]) as get:
        result = refresh_posting(original)
    assert result["listing_status"] == "unknown"
    assert result["description_html"] == original["description_html"]
    assert get.call_count == 2


def test_matching_api_job_does_not_fetch_public_page() -> None:
    original = saved_posting()
    payload = {"id": original["external_id"], "text": "Scientist (PK)", "description": "Full employer description"}
    with patch("httpx.get", return_value=httpx.Response(200, json=payload)) as get:
        result = refresh_posting(original)
    assert result["listing_status"] == "open"
    assert get.call_count == 1


def test_public_url_encodes_identity_components() -> None:
    original = {**saved_posting(), "board": "lab/name?x", "external_id": "id/1#frag"}
    with patch("httpx.get", side_effect=[httpx.Response(404), httpx.Response(404)]) as get:
        refresh_posting(original)
    assert get.call_args_list[1].args == ("https://jobs.lever.co/lab%2Fname%3Fx/id%2F1%23frag",)

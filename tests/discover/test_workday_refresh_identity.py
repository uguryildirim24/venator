"""Exact Workday refresh identity and persisted transport address; all HTTP mocked."""
from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from venator.discover.adapters import normalize_workday_detail, normalize_workday_listing, refresh_posting
from venator.discover.store import latest_postings, refresh_postings

FIXTURES = Path(__file__).parent / "fixtures"
BOARD = "amgen.wd1~Careers"
HOST = "amgen.wd1.myworkdayjobs.com"


def listing() -> dict:
    return normalize_workday_listing(BOARD, json.loads((FIXTURES / "workday_listing.json").read_text()))[0]


def detail() -> dict:
    return json.loads((FIXTURES / "workday_detail_minimal.json").read_text())


def response(payload: dict) -> httpx.Response:
    return httpx.Response(200, json=payload, request=httpx.Request("GET", f"https://{HOST}/"))


def test_normalize_store_refresh_keeps_exact_address(tmp_path: Path) -> None:
    original = listing()
    complete = normalize_workday_detail(original, detail())
    refresh_postings(tmp_path, [complete], complete=False, status="partial")
    stored = latest_postings(tmp_path)[original["key"]]
    assert stored["external_path"] == original["external_path"]
    with patch("httpx.Client.request", return_value=response(detail())) as request:
        refreshed = refresh_posting(stored)
    assert request.call_args.args[1] == f"https://{HOST}/wday/cxs/amgen/Careers{original['external_path']}"
    assert refreshed["key"] == original["key"]
    assert refreshed["external_path"] == original["external_path"]
    assert refreshed["detail_verification_status"] == "verified"


@pytest.mark.parametrize("locale", ["", "en-US/"])
def test_legacy_canonical_url_recovers_only_its_same_requisition(locale: str) -> None:
    original = listing()
    legacy = normalize_workday_detail(original, detail())
    del legacy["external_path"]
    legacy["url"] = f"https://{HOST}/{locale}Careers{original['external_path']}"
    with patch("httpx.Client.request", return_value=response(detail())) as request:
        refreshed = refresh_posting(legacy)
    assert refreshed["verification_status"] == "verified"
    assert refreshed["external_path"] == original["external_path"]
    assert request.call_args.args[1] == f"https://{HOST}/wday/cxs/amgen/Careers{original['external_path']}"


@pytest.mark.parametrize("identity", [None, "", "R-DIFFERENT", 192837])
def test_missing_or_mismatched_detail_identity_cannot_replace_original(identity: object) -> None:
    original = normalize_workday_detail(listing(), detail())
    payload = detail()
    if identity is None:
        del payload["jobPostingInfo"]["jobReqId"]
    else:
        payload["jobPostingInfo"]["jobReqId"] = identity
    payload["jobPostingInfo"]["title"] = "Different Job"
    payload["jobPostingInfo"]["jobDescription"] = "Different requirements."
    with patch("httpx.Client.request", return_value=response(payload)):
        refreshed = refresh_posting(original)
    assert refreshed["verification_status"] == "unknown"
    assert refreshed["listing_status"] == "unknown"
    assert refreshed["title"] == original["title"]
    assert refreshed["description_html"] == original["description_html"]
    assert refreshed["key"] == original["key"]
    assert "jobReqId" in refreshed["verification_error"]
    assert "detail_verified_at" not in refreshed


@pytest.mark.parametrize("url", [
    f"http://{HOST}/Careers/job/Boston/Title_R-192837",
    "https://elsewhere.invalid/Careers/job/Boston/Title_R-192837",
    f"https://{HOST}.evil.invalid/Careers/job/Boston/Title_R-192837",
    f"https://user@{HOST}/Careers/job/Boston/Title_R-192837",
    f"https://{HOST}:8443/Careers/job/Boston/Title_R-192837",
    f"https://{HOST}/OtherSite/job/Boston/Title_R-192837",
    f"https://{HOST}/Careers/jobs/Boston/Title_R-192837",
    f"https://{HOST}/Careers/job/Boston/Title_R-OTHER",
    f"https://{HOST}/Careers/job/Boston/Title_R-192837?redirect=elsewhere",
    f"https://{HOST}/Careers/job/../Title_R-192837",
    f"https://{HOST}/Careers/job/%2e%2e/Title_R-192837",
    f"https://{HOST}/Careers/job/Boston/Title_R-192837#apply",
])
def test_untrusted_legacy_url_stays_unknown_without_http(url: str) -> None:
    original = normalize_workday_detail(listing(), detail())
    original.pop("external_path")
    original["url"] = url
    with patch("httpx.Client.request") as request:
        refreshed = refresh_posting(original)
    request.assert_not_called()
    assert refreshed["verification_status"] == "unknown"
    assert refreshed["listing_status"] == "unknown"
    assert refreshed["description_html"] == original["description_html"]

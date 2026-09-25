"""Transient Discover transport failures do not discard a whole board."""
from __future__ import annotations

import socket
from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from venator.discover import adapters, harvest
from venator.discover.run import run
from venator.discover.store import load_source_health
from venator.profile import load_profile


def _dns_error() -> httpx.ConnectError:
    try:
        raise socket.gaierror(8, "name not known")
    except socket.gaierror as error:
        try:
            raise httpx.ConnectError("DNS unavailable") from error
        except httpx.ConnectError as wrapped:
            return wrapped


def _profile():
    profile = load_profile(Path(__file__).parents[2] / "profiles" / "example")
    return replace(profile, sources=replace(profile.sources, boards={"greenhouse": ("lab", "other")}))


def test_dns_recovers_without_losing_board(tmp_path, monkeypatch):
    calls: list[str] = []
    waits: list[float] = []

    def get(url, **kwargs):
        calls.append(url)
        if len(calls) == 1:
            raise _dns_error()
        return httpx.Response(200, json={"jobs": []})

    monkeypatch.setattr(adapters.httpx, "get", get)
    monkeypatch.setattr(adapters.time, "sleep", waits.append)
    run(_profile(), tmp_path, interactive=True)
    assert len(calls) == 3  # lab twice, other once; no end pass
    assert waits == [adapters.NETWORK_RETRY_DELAYS[0]]
    assert load_source_health(tmp_path)["greenhouse:lab"]["status"] == "ok"


@pytest.mark.parametrize("end_success", [False, True])
def test_exhausted_dns_retries_get_one_final_board_pass(tmp_path, monkeypatch, end_success):
    calls: list[str] = []
    waits: list[float] = []

    def get(url, **kwargs):
        calls.append(url)
        if "/lab/" in url and (not end_success or len(calls) <= 4):
            raise _dns_error()
        return httpx.Response(200, json={"jobs": []})

    monkeypatch.setattr(adapters.httpx, "get", get)
    monkeypatch.setattr(adapters.time, "sleep", waits.append)
    run(_profile(), tmp_path, interactive=True)
    assert len(calls) == (5 if end_success else 7)
    assert "/other/" in calls[3]
    assert waits == list(adapters.NETWORK_RETRY_DELAYS) * (1 if end_success else 2)
    assert load_source_health(tmp_path)["greenhouse:lab"]["status"] == ("ok" if end_success else "failed")


@pytest.mark.parametrize("status", [403, 404, 410])
def test_definite_http_answers_are_not_retried(tmp_path, monkeypatch, status):
    calls: list[str] = []

    def get(url, **kwargs):
        calls.append(url)
        return httpx.Response(status, json={"jobs": []}) if "/lab/" in url else httpx.Response(200, json={"jobs": []})

    monkeypatch.setattr(adapters.httpx, "get", get)
    run(_profile(), tmp_path, interactive=True)
    assert len(calls) == 2
    assert load_source_health(tmp_path)["greenhouse:lab"]["status"] == "failed"


def test_harvest_retries_failed_candidate_after_other_boards(monkeypatch):
    calls: list[str] = []

    def get(url, **kwargs):
        calls.append(url)
        if "/lab/" in url and len(calls) <= 3:
            raise _dns_error()
        return httpx.Response(200, json={"jobs": []})

    monkeypatch.setattr(adapters.httpx, "get", get)
    monkeypatch.setattr(adapters.time, "sleep", lambda seconds: None)
    result = harvest.probe_candidates({("greenhouse", "lab"): "Lab", ("greenhouse", "other"): "Other"}, set())
    assert [entry["board"] for entry in result] == ["other", "lab"]
    assert len(calls) == 5


def test_smartrecruiters_transient_detail_failure_gets_final_board_pass(tmp_path, monkeypatch):
    profile = _profile()
    profile = replace(profile, sources=replace(profile.sources, boards={"smartrecruiters": ("lab", "other")}))
    calls: list[str] = []
    real_client = httpx.Client

    def answer(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        calls.append(path)
        if path.endswith("/postings"):
            return httpx.Response(200, json={"totalFound": 1, "content": [{"id": "1", "name": "Lab Assistant"}]})
        if "/lab/" in path and calls.count(path) <= 3:
            raise _dns_error()
        return httpx.Response(200, json={
            "id": "1", "name": "Lab Assistant",
            "jobAd": {"sections": {"jobDescription": {"text": "Run assays."}}},
        })

    monkeypatch.setattr(adapters.httpx, "Client", lambda **kwargs: real_client(transport=httpx.MockTransport(answer)))
    monkeypatch.setattr(adapters.time, "sleep", lambda seconds: None)
    run(profile, tmp_path, interactive=True)
    assert calls.count("/v1/companies/lab/postings/1") == 4
    assert calls.index("/v1/companies/other/postings") < calls.index("/v1/companies/lab/postings/1", 4)
    assert load_source_health(tmp_path)["smartrecruiters:lab"]["status"] == "ok"


def test_workday_listing_recovers_from_dns_within_board(tmp_path, monkeypatch):
    profile = _profile()
    profile = replace(profile, sources=replace(profile.sources, boards={"workday": ("lab.wd1~Careers",)}))
    calls: list[int] = []
    waits: list[float] = []

    class Transport:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.close()

        def request(self, method, url, **kwargs):
            calls.append(1)
            if len(calls) == 1:
                raise _dns_error()
            return httpx.Response(200, json={"total": 0, "jobPostings": []})

        def close(self):
            pass

    monkeypatch.setattr(adapters.httpx, "Client", lambda **kwargs: Transport())
    monkeypatch.setattr(adapters.time, "sleep", waits.append)
    run(profile, tmp_path, interactive=True)
    assert len(calls) == 2
    assert waits[0] == adapters.NETWORK_RETRY_DELAYS[0]
    assert load_source_health(tmp_path)["workday:lab.wd1~Careers"]["status"] == "ok"


def test_workday_transient_retries_respect_tenant_pacing(monkeypatch):
    clock = [0.0]
    starts: list[float] = []

    class Transport:
        def request(self, method, url, **kwargs):
            starts.append(clock[0])
            if len(starts) == 1:
                raise _dns_error()
            return httpx.Response(200, json={"total": 0, "jobPostings": []})

        def close(self):
            pass

    def sleep(seconds):
        clock[0] += seconds

    monkeypatch.setattr(adapters.httpx, "Client", lambda **kwargs: Transport())
    monkeypatch.setattr(adapters.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(adapters.time, "sleep", sleep)
    with adapters.WorkdayClient("lab.wd1.myworkdayjobs.com") as client:
        client.post("https://lab.wd1.myworkdayjobs.com/jobs", json={})
        client.post("https://lab.wd1.myworkdayjobs.com/jobs", json={})
    assert len(starts) == 3
    assert starts[1] - starts[0] >= 0.5
    assert starts[2] - starts[1] >= 0.5
    assert starts[1] == adapters.NETWORK_RETRY_DELAYS[0]

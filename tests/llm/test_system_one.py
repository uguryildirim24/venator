"""System One client: pinned endpoint, key containment, retry bounds, deadlines."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from typing import Any

import httpx
import pytest

from venator.llm.system_one import (
    ATTEMPT_DEADLINE,
    CONNECT_TIMEOUT,
    ENDPOINT,
    KEY_VAR,
    MODEL,
    SystemOneError,
    SystemOneHttpResult,
    SystemOneRequest,
    httpx_poster,
    post_systemone,
)

SENTINEL = "sk-venator-jev-sentinel-0123456789abcdefghij"
EPOCH = 1_789_000_000.0


class ManualClock:
    def __init__(self) -> None:
        self.t = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.t

    def time(self) -> float:
        return EPOCH + self.t

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.t += seconds


def _ok_body(model: str = MODEL, extra: str = "") -> bytes:
    payload = {
        "model": model,
        "answers": {"ok": True},
        "usage": {"input_tokens": 12, "output_tokens": 0},
        "note": extra,
    }
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


class ScriptedPoster:
    def __init__(self, replies: list[SystemOneHttpResult | BaseException]) -> None:
        self.replies = list(replies)
        self.calls: list[SystemOneRequest] = []

    def __call__(self, request: SystemOneRequest) -> SystemOneHttpResult:
        self.calls.append(request)
        assert request.url == ENDPOINT
        assert request.headers["Authorization"] == f"Bearer {SENTINEL}"
        assert KEY_VAR not in request.url
        assert SENTINEL not in request.url
        assert request.payload["model"] == MODEL
        item = self.replies.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _post(poster: ScriptedPoster, *, clock: ManualClock | None = None,
          remaining_requests: int = 5,
          before_start=None, case_deadline: float | None = None) -> Any:
    return post_systemone(
        {"state": {}, "questions": {}},
        environ={KEY_VAR: SENTINEL},
        poster=poster,
        clock=clock or ManualClock(),
        remaining_requests=remaining_requests,
        before_start=before_start,
        case_deadline=case_deadline,
    )


def test_success_posts_pinned_endpoint_and_model() -> None:
    poster = ScriptedPoster([SystemOneHttpResult(200, _ok_body())])
    result = _post(poster)
    assert result.model_returned == MODEL
    assert result.usage["input_tokens"] == 12
    assert result.attempts == 1
    assert poster.calls[0].url == ENDPOINT
    assert poster.calls[0].connect_timeout == CONNECT_TIMEOUT
    assert poster.calls[0].attempt_timeout == ATTEMPT_DEADLINE


def test_httpx_poster_does_not_follow_redirects() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == "api.typesafe.ai":
            return httpx.Response(302, headers={"location": "https://evil.example/steal"})
        return httpx.Response(200, content=_ok_body())

    req = SystemOneRequest(
        url=ENDPOINT,
        headers={"Authorization": f"Bearer {SENTINEL}", "Content-Type": "application/json"},
        payload={"model": MODEL},
        connect_timeout=CONNECT_TIMEOUT,
        attempt_timeout=ATTEMPT_DEADLINE,
    )
    result = httpx_poster(req, transport=httpx.MockTransport(handler))
    assert result.status == 302
    assert seen == [ENDPOINT]
    with pytest.raises(SystemOneError, match="redirected") as raised:
        _post(ScriptedPoster([result]))
    assert SENTINEL not in str(raised.value)
    assert "evil.example" not in str(raised.value)



def test_httpx_poster_holds_the_whole_attempt_to_its_deadline() -> None:
    """Each chunk arrives inside the idle read timeout; the attempt as a whole
    does not. SPEC-jev §8.3: a real whole-attempt deadline, not only an idle
    socket timeout."""
    clock = ManualClock()
    served: list[int] = []

    class Trickle(httpx.SyncByteStream):
        def __iter__(self):
            for index in range(5):
                clock.t += ATTEMPT_DEADLINE / 3
                served.append(index)
                yield b" "

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=Trickle())

    req = SystemOneRequest(
        url=ENDPOINT,
        headers={"Authorization": f"Bearer {SENTINEL}", "Content-Type": "application/json"},
        payload={"model": MODEL},
        connect_timeout=CONNECT_TIMEOUT,
        attempt_timeout=ATTEMPT_DEADLINE,
    )
    with pytest.raises(SystemOneError, match="timeout"):
        httpx_poster(req, transport=httpx.MockTransport(handler), monotonic=clock.monotonic)
    assert served == [0, 1, 2]


def test_httpx_poster_returns_a_streamed_body_whole() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_ok_body(), headers={"X-Probe": "1"})

    req = SystemOneRequest(
        url=ENDPOINT,
        headers={"Authorization": f"Bearer {SENTINEL}", "Content-Type": "application/json"},
        payload={"model": MODEL},
        connect_timeout=CONNECT_TIMEOUT,
        attempt_timeout=ATTEMPT_DEADLINE,
    )
    result = httpx_poster(req, transport=httpx.MockTransport(handler))
    assert (result.status, result.body, result.headers["x-probe"]) == (200, _ok_body(), "1")

def test_key_is_not_in_repr_logs_or_exceptions(caplog: pytest.LogCaptureFixture,
                                               capsys: pytest.CaptureFixture[str]) -> None:
    poster = ScriptedPoster([SystemOneHttpResult(500, b"nope")])
    caplog.set_level(logging.DEBUG)
    with pytest.raises(SystemOneError, match="transport_failed") as raised:
        _post(poster)
    text = str(raised.value) + repr(poster.calls[0]) + capsys.readouterr().out + capsys.readouterr().err
    text += "".join(record.getMessage() for record in caplog.records)
    assert SENTINEL not in text
    assert "Bearer" not in str(raised.value)


def test_key_echo_in_body_is_refused() -> None:
    body = _ok_body(extra=SENTINEL)
    poster = ScriptedPoster([SystemOneHttpResult(200, body)])
    with pytest.raises(SystemOneError, match="key_echo") as raised:
        _post(poster)
    assert SENTINEL not in str(raised.value)


def test_missing_key_is_credentials_missing() -> None:
    with pytest.raises(SystemOneError, match="credentials_missing") as raised:
        post_systemone({"state": {}}, environ={}, poster=ScriptedPoster([]))
    assert SENTINEL not in str(raised.value)


def test_model_mismatch_is_refused() -> None:
    poster = ScriptedPoster([SystemOneHttpResult(200, _ok_body(model="jev-latest"))])
    with pytest.raises(SystemOneError, match="model_mismatch"):
        _post(poster)


def test_429_retries_one_then_two_seconds_and_stops_at_three() -> None:
    clock = ManualClock()
    limited = SystemOneHttpResult(429, b"slow", {"Retry-After": "0"})
    poster = ScriptedPoster([limited, limited, limited])
    with pytest.raises(SystemOneError, match="rate_limited") as raised:
        _post(poster, clock=clock, remaining_requests=5)
    assert raised.value.attempts == 3
    assert raised.value.status == 429
    assert clock.sleeps == [1.0, 2.0]
    assert len(poster.calls) == 3


def test_retry_after_larger_valid_wait_is_honored() -> None:
    clock = ManualClock()
    poster = ScriptedPoster([
        SystemOneHttpResult(529, b"x", {"Retry-After": "4"}),
        SystemOneHttpResult(200, _ok_body()),
    ])
    result = _post(poster, clock=clock)
    assert result.attempts == 2
    assert clock.sleeps == [4.0]



def test_retry_after_http_date_is_measured_against_wall_time() -> None:
    """An HTTP-date is an epoch instant. Measured against the monotonic clock
    it read as decades away and stopped every retry it should have allowed."""
    from email.utils import formatdate

    clock = ManualClock()
    clock.t = 100.0
    poster = ScriptedPoster([
        SystemOneHttpResult(429, b"x", {"Retry-After": formatdate(EPOCH + 105.0, usegmt=True)}),
        SystemOneHttpResult(200, _ok_body()),
    ])
    result = _post(poster, clock=clock)
    assert result.attempts == 2
    assert clock.sleeps == [5.0]

def test_retry_after_above_thirty_seconds_stops() -> None:
    clock = ManualClock()
    poster = ScriptedPoster([SystemOneHttpResult(429, b"x", {"Retry-After": "31"})])
    with pytest.raises(SystemOneError, match="rate_limited"):
        _post(poster, clock=clock)
    assert clock.sleeps == []
    assert len(poster.calls) == 1


def test_unknown_outcome_is_not_retried() -> None:
    poster = ScriptedPoster([SystemOneHttpResult(500, b"err")])
    with pytest.raises(SystemOneError, match="transport_failed") as raised:
        _post(poster)
    assert raised.value.attempts == 1
    assert poster.replies == []


def test_timeout_is_not_retried() -> None:
    poster = ScriptedPoster([SystemOneError("timeout", nbytes=0)])
    with pytest.raises(SystemOneError, match="timeout") as raised:
        _post(poster)
    assert raised.value.attempts == 1


def test_422_is_response_invalid_without_retry() -> None:
    poster = ScriptedPoster([SystemOneHttpResult(422, b"shape")])
    with pytest.raises(SystemOneError, match="response_invalid") as raised:
        _post(poster)
    assert raised.value.attempts == 1
    assert raised.value.status == 422


def test_401_stops_without_retry() -> None:
    poster = ScriptedPoster([SystemOneHttpResult(401, b"no")])
    with pytest.raises(SystemOneError, match="auth_failed") as raised:
        _post(poster)
    assert raised.value.attempts == 1


def test_remaining_requests_cap_retry_starts() -> None:
    poster = ScriptedPoster([
        SystemOneHttpResult(429, b"x"),
        SystemOneHttpResult(429, b"x"),
        SystemOneHttpResult(429, b"x"),
    ])
    with pytest.raises(SystemOneError, match="rate_limited") as raised:
        _post(poster, remaining_requests=1)
    assert raised.value.attempts == 1
    assert len(poster.calls) == 1


def test_before_start_refusal_sends_nothing() -> None:
    poster = ScriptedPoster([SystemOneHttpResult(200, _ok_body())])
    with pytest.raises(SystemOneError, match="budget_exhausted"):
        _post(poster, before_start=lambda: False)
    assert poster.calls == []


def test_case_deadline_stops_retry_wait() -> None:
    clock = ManualClock()
    poster = ScriptedPoster([SystemOneHttpResult(429, b"x", {"Retry-After": "10"})])
    with pytest.raises(SystemOneError, match="rate_limited"):
        _post(poster, clock=clock, case_deadline=5.0)
    assert clock.sleeps == []
    assert len(poster.calls) == 1


def test_a_second_start_is_not_paced() -> None:
    """last_start was a 0.5 s floor between HTTP starts. Decision 29 removed it."""
    clock = ManualClock()
    poster = ScriptedPoster([
        SystemOneHttpResult(200, _ok_body()),
        SystemOneHttpResult(200, _ok_body()),
    ])
    _post(poster, clock=clock)
    _post(poster, clock=clock)
    assert clock.sleeps == []
    assert len(poster.calls) == 2

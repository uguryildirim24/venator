"""Bounded HTTPS client for TypeSafe System One. Not a Lane.

The only reader of ``TYPESAFE_API_KEY``. Failures are constructed codes and
integers; the key is never interpolated into an exception, URL, log or repr.
"""

from __future__ import annotations

import math
import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from email.utils import parsedate_to_datetime
from typing import Any, Protocol

ENDPOINT = "https://api.typesafe.ai/v1/systemone"
MODEL = "jev-1.13.0"
KEY_VAR = "TYPESAFE_API_KEY"
CONNECT_TIMEOUT = 5.0
ATTEMPT_DEADLINE = 30.0
CASE_DEADLINE = 120.0
MAX_RETRY_ATTEMPTS = 3
RETRY_WAITS = (1.0, 2.0)
MAX_RETRY_AFTER = 30.0
INPUT_TARIFF_USD_PER_MILLION = 0.042
# Mean input tokens from the 91-row development pass. Preview multiplies this
# by the tariff; it is not a start reservation.
OBSERVED_INPUT_TOKENS = 4_200
OBSERVED_USD_PER_REQUEST = (
    OBSERVED_INPUT_TOKENS * INPUT_TARIFF_USD_PER_MILLION / 1_000_000
)


class SystemOneError(Exception):
    """A constructed transport failure. Never quotes provider text or a key."""

    def __init__(
        self,
        code: str,
        *,
        status: int | None = None,
        nbytes: int | None = None,
        attempts: int = 0,
    ) -> None:
        self.code = code
        self.status = status
        self.nbytes = nbytes
        self.attempts = attempts
        parts = [code]
        if status is not None:
            parts.append(f"status={status}")
        if nbytes is not None:
            parts.append(f"bytes={nbytes}")
        if attempts:
            parts.append(f"attempts={attempts}")
        super().__init__(" ".join(parts))


@dataclass(frozen=True)
class SystemOneRequest:
    url: str
    headers: Mapping[str, str] = field(repr=False)
    payload: Mapping[str, Any]
    connect_timeout: float
    attempt_timeout: float

    def __repr__(self) -> str:
        return (
            f"SystemOneRequest(url={ENDPOINT!r}, headers=<redacted>, "
            f"payload_keys={sorted(self.payload)!r}, "
            f"connect_timeout={self.connect_timeout}, "
            f"attempt_timeout={self.attempt_timeout})"
        )


@dataclass(frozen=True)
class SystemOneHttpResult:
    status: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=dict, repr=False)


@dataclass(frozen=True)
class SystemOneSuccess:
    raw_response: str
    model_returned: str
    usage: dict[str, int]
    latency_ms: float
    status: int
    attempts: int


class Clock(Protocol):
    def monotonic(self) -> float: ...
    def time(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class _WallClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


Poster = Callable[[SystemOneRequest], SystemOneHttpResult]
BeforeStart = Callable[[], bool]


def read_api_key(environ: Mapping[str, str] | None = None) -> str:
    environment = os.environ if environ is None else environ
    key = environment.get(KEY_VAR, "").strip()
    if not key:
        raise SystemOneError("credentials_missing")
    return key


def _walk_strings(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, str):
        found.append(value)
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if isinstance(key, str):
                found.append(key)
            found.extend(_walk_strings(item))
    elif isinstance(value, list):
        for item in value:
            found.extend(_walk_strings(item))
    return found


def _echoes_key(raw: str, parsed: object, key: str) -> bool:
    if key and key in raw:
        return True
    return any(key in text for text in _walk_strings(parsed) if key)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    wanted = name.casefold()
    for key, value in headers.items():
        if key.casefold() == wanted:
            return value
    return None


def parse_retry_after(headers: Mapping[str, str], *, now: float) -> float | None:
    """Seconds to wait. ``now`` is epoch time, because an HTTP-date is one."""
    raw = _header(headers, "Retry-After")
    if raw is None or raw == "":
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        seconds = None
    if seconds is not None and math.isfinite(seconds) and seconds >= 0:
        return seconds
    try:
        when = parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError, IndexError):
        return None
    if when is None:
        return None
    stamp = when.timestamp()
    return max(0.0, stamp - now)


def httpx_poster(
    request: SystemOneRequest,
    *,
    transport: object | None = None,
    monotonic: Callable[[], float] = time.monotonic,
) -> SystemOneHttpResult:
    """POST to the pinned endpoint. TLS on, redirects off.

    httpx's read timeout is idle time between reads, so a server that trickles
    its body resets it with every chunk. The body is therefore streamed and the
    whole attempt is held to ``attempt_timeout`` between chunks as well.
    """
    import httpx

    if request.url != ENDPOINT:
        raise SystemOneError("transport_failed")
    timeout = httpx.Timeout(
        connect=request.connect_timeout,
        read=request.attempt_timeout,
        write=request.attempt_timeout,
        pool=request.connect_timeout,
    )
    kwargs: dict[str, Any] = {
        "timeout": timeout,
        "follow_redirects": False,
        "verify": True,
    }
    if transport is not None:
        kwargs["transport"] = transport
    deadline = monotonic() + request.attempt_timeout
    try:
        with httpx.Client(**kwargs) as client:
            with client.stream(
                "POST",
                ENDPOINT,
                headers=dict(request.headers),
                json=dict(request.payload),
            ) as response:
                chunks: list[bytes] = []
                if monotonic() >= deadline:
                    raise SystemOneError("timeout", nbytes=0)
                for chunk in response.iter_bytes():
                    chunks.append(chunk)
                    if monotonic() >= deadline:
                        raise SystemOneError("timeout", nbytes=0)
                status = int(response.status_code)
                headers = dict(response.headers)
    except httpx.TimeoutException:
        raise SystemOneError("timeout", nbytes=0) from None
    except httpx.HTTPError:
        raise SystemOneError("transport_failed") from None
    return SystemOneHttpResult(status=status, body=b"".join(chunks), headers=headers)


def post_systemone(
    payload: Mapping[str, Any],
    *,
    environ: Mapping[str, str] | None = None,
    poster: Poster | None = None,
    clock: Clock | None = None,
    remaining_requests: int = MAX_RETRY_ATTEMPTS,
    case_deadline: float | None = None,
    before_start: BeforeStart | None = None,
) -> SystemOneSuccess:
    """One System One call with 429/529 retry bounds and real deadlines.

    Unknown outcomes (timeouts, other HTTP, transport errors) are not retried.
    There is no minimum interval between starts: the caller may have many
    calls in flight.
    """
    key = read_api_key(environ)
    send = httpx_poster if poster is None else poster
    timer: Clock = _WallClock() if clock is None else clock
    started = timer.monotonic()
    deadline = started + (CASE_DEADLINE if case_deadline is None else case_deadline)
    body = {**dict(payload), "model": MODEL}
    attempts = 0
    last_error: SystemOneError | None = None
    max_attempts = min(MAX_RETRY_ATTEMPTS, remaining_requests)
    if max_attempts < 1:
        raise SystemOneError("budget_exhausted", attempts=0)

    while True:
        remaining = deadline - timer.monotonic()
        if remaining <= 0:
            raise SystemOneError("timeout", nbytes=0, attempts=attempts)
        if before_start is not None and not before_start():
            if attempts == 0:
                raise SystemOneError("budget_exhausted", attempts=0)
            if last_error is not None:
                raise last_error
            raise SystemOneError("budget_exhausted", attempts=attempts)
        remaining = deadline - timer.monotonic()
        if remaining <= 0:
            raise SystemOneError("timeout", nbytes=0, attempts=attempts)
        attempt_timeout = min(ATTEMPT_DEADLINE, remaining)
        request = SystemOneRequest(
            url=ENDPOINT,
            headers={
                "Authorization": f"Bearer {key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            payload=body,
            connect_timeout=min(CONNECT_TIMEOUT, attempt_timeout),
            attempt_timeout=attempt_timeout,
        )
        attempt_started = timer.monotonic()
        attempts += 1
        try:
            result = send(request)
        except SystemOneError as error:
            raise SystemOneError(
                error.code, status=error.status, nbytes=error.nbytes, attempts=attempts,
            ) from None
        except Exception:
            raise SystemOneError("transport_failed", attempts=attempts) from None

        nbytes = len(result.body)
        status = result.status
        if 300 <= status < 400:
            raise SystemOneError("redirected", status=status, nbytes=nbytes, attempts=attempts)
        if status in {401, 403}:
            raise SystemOneError("auth_failed", status=status, nbytes=nbytes, attempts=attempts)
        if status == 402:
            raise SystemOneError("out_of_credit", status=status, nbytes=nbytes, attempts=attempts)
        if status == 422:
            raise SystemOneError("response_invalid", status=status, nbytes=nbytes, attempts=attempts)
        retryable = status in {429, 529}
        if retryable:
            last_error = SystemOneError("rate_limited", status=status, nbytes=nbytes, attempts=attempts)
            if attempts >= max_attempts:
                raise last_error
            default_wait = RETRY_WAITS[min(attempts - 1, len(RETRY_WAITS) - 1)]
            header_wait = parse_retry_after(result.headers, now=timer.time())
            wait = default_wait
            if header_wait is not None and header_wait > default_wait:
                if header_wait > MAX_RETRY_AFTER:
                    raise last_error
                wait = header_wait
            remaining_after = deadline - timer.monotonic()
            if wait > remaining_after:
                raise last_error
            timer.sleep(wait)
            continue
        if status != 200:
            raise SystemOneError("transport_failed", status=status, nbytes=nbytes, attempts=attempts)

        try:
            raw = result.body.decode("utf-8")
        except UnicodeDecodeError:
            raise SystemOneError("response_invalid", status=status, nbytes=nbytes, attempts=attempts) from None
        parsed: object
        try:
            import json
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            parsed = None
        if _echoes_key(raw, parsed, key):
            raise SystemOneError("key_echo", status=status, nbytes=nbytes, attempts=attempts)
        if not isinstance(parsed, dict):
            raise SystemOneError("response_invalid", status=status, nbytes=nbytes, attempts=attempts)
        model_returned = parsed.get("model")
        if model_returned != MODEL:
            raise SystemOneError("model_mismatch", status=status, nbytes=nbytes, attempts=attempts)
        usage_raw = parsed.get("usage") if isinstance(parsed.get("usage"), Mapping) else {}
        usage: dict[str, int] = {}
        for name in ("input_tokens", "output_tokens"):
            value = usage_raw.get(name) if isinstance(usage_raw, Mapping) else None
            if type(value) is int and value >= 0:
                usage[name] = value
        latency_ms = max(0.0, (timer.monotonic() - attempt_started) * 1000.0)
        return SystemOneSuccess(
            raw_response=raw,
            model_returned=str(model_returned),
            usage=usage,
            latency_ms=latency_ms,
            status=status,
            attempts=attempts,
        )

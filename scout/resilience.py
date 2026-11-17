"""Resilience primitives (docs/ROADMAP.md §1): retry policy, error classification,
circuit breakers, deadlines.

R1  No failure on the first error: transient errors (timeouts, connection
    failures, 408/425/429/5xx) are retried with exponential backoff and jitter,
    honouring Retry-After; permanent ones (other 4xx, bad input) fail at once.
R3  Bounded effort: every retry loop has a cap on attempts *and* on elapsed
    time; Deadline carries a wall-clock budget through a task.

RetryTransport puts R1 under every HTTP request made through scout.net, so no
tool can forget it. A per-host CircuitBreaker stops hammering a host that keeps
failing: after `threshold` consecutive transient failures it opens for
`cooldown` seconds (calls fail fast with CircuitOpen), then lets one trial
through (half-open) and closes again on success.
"""

from __future__ import annotations

import email.utils
import os
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, TypeVar

import httpx

T = TypeVar("T")

TRANSIENT_STATUS = {408, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}


class PermanentError(Exception):
    """Do not retry: the request itself is wrong (or forbidden)."""


class CircuitOpen(Exception):
    """A host failed too often recently; calls fail fast until its cooldown ends."""


class DeadlineExceeded(Exception):
    """The time budget for this work ran out."""


# -- classification -----------------------------------------------------------------

def is_transient(exc: BaseException) -> bool:
    """True when retrying the same call later can succeed."""
    if isinstance(exc, (PermanentError, CircuitOpen, DeadlineExceeded)):
        return False
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in TRANSIENT_STATUS
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return True
    try:                                    # the OpenAI client (model calls)
        import openai
        if isinstance(exc, (openai.APITimeoutError, openai.APIConnectionError, openai.RateLimitError,
                            openai.InternalServerError)):
            return True
        if isinstance(exc, openai.APIStatusError):
            return exc.status_code in TRANSIENT_STATUS
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return False


def retry_after_seconds(headers: Any, now: float | None = None) -> float | None:
    """Seconds from a Retry-After header (delta-seconds or HTTP date); None if absent or unreadable."""
    value = headers.get("retry-after") if headers is not None else None
    if not value:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = email.utils.parsedate_to_datetime(value).timestamp()
    except (TypeError, ValueError):
        return None
    return max(0.0, when - (now if now is not None else time.time()))


def _retry_after_of(exc: BaseException) -> float | None:
    resp = getattr(exc, "response", None)
    return retry_after_seconds(getattr(resp, "headers", None)) if resp is not None else None


# -- retry policy -------------------------------------------------------------------

@dataclass
class RetryPolicy:
    attempts: int = 4                  # total tries, including the first
    base: float = 1.0                  # first backoff (seconds)
    cap: float = 30.0                  # largest single wait
    max_elapsed: float = 120.0         # give up when this much time has passed
    jitter: float = 0.3                # +-30 % randomisation, so parallel clients do not retry in lockstep
    max_retry_after: float = 120.0     # never wait longer than this for a server's Retry-After
    sleep: Callable[[float], None] = field(default=time.sleep, repr=False)
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)

    def backoff(self, attempt: int, retry_after: float | None = None) -> float:
        if retry_after is not None:
            return min(retry_after, self.max_retry_after)
        wait = min(self.cap, self.base * (2 ** (attempt - 1)))
        return max(0.0, wait * (1 + random.uniform(-self.jitter, self.jitter)))

    def call(self, fn: Callable[..., T], *args: Any, on_retry: Callable[[int, BaseException, float], None] | None = None,
             classify: Callable[[BaseException], bool] = is_transient, **kwargs: Any) -> T:
        start = self.clock()
        for attempt in range(1, self.attempts + 1):
            try:
                return fn(*args, **kwargs)
            except Exception as exc:  # noqa: BLE001
                if not classify(exc) or attempt == self.attempts:
                    raise
                wait = self.backoff(attempt, _retry_after_of(exc))
                if self.clock() - start + wait > self.max_elapsed:
                    raise
                if on_retry:
                    on_retry(attempt, exc, wait)
                self.sleep(wait)
        raise AssertionError("unreachable")  # pragma: no cover


# -- circuit breaker ------------------------------------------------------------------

@dataclass
class CircuitBreaker:
    threshold: int = 5
    cooldown: float = 60.0
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    failures: int = 0
    opened_at: float | None = None

    @property
    def state(self) -> str:
        if self.opened_at is None:
            return "closed"
        return "half-open" if self.clock() - self.opened_at >= self.cooldown else "open"

    def before(self, key: str = "") -> None:
        if self.state == "open":
            left = self.cooldown - (self.clock() - (self.opened_at or 0))
            raise CircuitOpen(f"{key or 'host'} failed {self.failures} times in a row; retry in {left:.0f}s")

    def success(self) -> None:
        self.failures, self.opened_at = 0, None

    def failure(self) -> None:
        self.failures += 1
        if self.failures >= self.threshold or self.state == "half-open":
            self.opened_at = self.clock()


class Breakers:
    """One breaker per key (a host), shared across threads."""

    def __init__(self, threshold: int = 5, cooldown: float = 60.0, clock: Callable[[], float] = time.monotonic):
        self._lock = threading.Lock()
        self._by_key: dict[str, CircuitBreaker] = {}
        self.threshold, self.cooldown, self.clock = threshold, cooldown, clock

    def get(self, key: str) -> CircuitBreaker:
        with self._lock:
            if key not in self._by_key:
                self._by_key[key] = CircuitBreaker(self.threshold, self.cooldown, self.clock)
            return self._by_key[key]


BREAKERS = Breakers()


# -- HTTP: retries under every request ------------------------------------------------------

class RetryTransport(httpx.BaseTransport):
    """Wraps a transport: retries transient failures with backoff, honours Retry-After, breaks per host.

    Retries cover connecting, sending and the response status. A failure while
    *reading a streamed body* surfaces to the caller (download_file retries
    the whole download for that).
    """

    def __init__(self, inner: httpx.BaseTransport, policy: RetryPolicy | None = None,
                 breakers: Breakers | None = None, on_retry: Callable[[str], None] | None = None):
        self.inner = inner
        self.policy = policy or RetryPolicy()
        self.breakers = breakers or BREAKERS
        self.on_retry = on_retry

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        # Fault injection for resilience evals: SCOUT_CHAOS_DOWN="api.openalex.org,…" makes those hosts unreachable.
        if host in {h.strip() for h in os.environ.get("SCOUT_CHAOS_DOWN", "").split(",") if h.strip()}:
            raise httpx.ConnectError(f"chaos: {host} is marked down (SCOUT_CHAOS_DOWN)", request=request)
        breaker = self.breakers.get(host)
        start = self.policy.clock()
        # The breaker guards a host across *separate* requests; it is checked before the first try only,
        # so it never cuts short the retry budget of the request already in progress.
        breaker.before(host)
        for attempt in range(1, self.policy.attempts + 1):
            try:
                response = self.inner.handle_request(request)
            except Exception as exc:  # noqa: BLE001
                if not is_transient(exc):
                    raise
                breaker.failure()
                wait = self.policy.backoff(attempt)
                if attempt == self.policy.attempts or self.policy.clock() - start + wait > self.policy.max_elapsed:
                    raise
                self._note(f"{host}: {type(exc).__name__}; retry {attempt} in {wait:.1f}s")
                self.policy.sleep(wait)
                continue
            if response.status_code in TRANSIENT_STATUS:
                breaker.failure()
                wait = self.policy.backoff(attempt, retry_after_seconds(response.headers))
                if attempt == self.policy.attempts or self.policy.clock() - start + wait > self.policy.max_elapsed:
                    return response                     # the caller sees the real status
                response.close()
                self._note(f"{host}: HTTP {response.status_code}; retry {attempt} in {wait:.1f}s")
                self.policy.sleep(wait)
                continue
            breaker.success()
            return response
        raise AssertionError("unreachable")  # pragma: no cover

    def _note(self, msg: str) -> None:
        if self.on_retry:
            self.on_retry(msg)

    def close(self) -> None:
        self.inner.close()


# -- deadlines and time limits --------------------------------------------------------------

@dataclass
class Deadline:
    seconds: float | None                     # None = no limit
    clock: Callable[[], float] = field(default=time.monotonic, repr=False)
    started: float = field(default=0.0)

    def __post_init__(self) -> None:
        self.started = self.clock()

    def remaining(self) -> float:
        return float("inf") if self.seconds is None else self.seconds - (self.clock() - self.started)

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0

    def fraction_used(self) -> float:
        return 0.0 if self.seconds is None else min(1.0, (self.clock() - self.started) / self.seconds)

    def check(self, what: str = "task") -> None:
        if self.expired:
            raise DeadlineExceeded(f"{what} time budget of {self.seconds:.0f}s used up")


_POOL = ThreadPoolExecutor(max_workers=8, thread_name_prefix="scout-limit")


def run_with_time_limit(fn: Callable[..., T], seconds: float | None, *args: Any, **kwargs: Any) -> T:
    """Run fn, but stop waiting after `seconds` (DeadlineExceeded).

    Python cannot kill a thread: on timeout the call keeps running in the
    background and its result is discarded. Use it around calls that have their
    own (longer) timeouts, so the agent moves on instead of waiting.
    """
    if seconds is None:
        return fn(*args, **kwargs)
    future = _POOL.submit(fn, *args, **kwargs)
    try:
        return future.result(timeout=seconds)
    except FutureTimeout:
        raise DeadlineExceeded(f"step did not finish within {seconds:.0f}s") from None

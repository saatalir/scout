"""Resilience core: classification, retry policy, Retry-After, circuit breaker, transport retries, deadlines."""

from __future__ import annotations

import time

import httpx
import pytest

from scout.resilience import (Breakers, CircuitBreaker, CircuitOpen, Deadline, DeadlineExceeded, PermanentError,
                              RetryPolicy, RetryTransport, is_transient, retry_after_seconds, run_with_time_limit)


class FakeTime:
    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    def clock(self) -> float:
        return self.now

    def sleep(self, s: float) -> None:
        self.sleeps.append(s)
        self.now += s


def policy(t: FakeTime, **kw) -> RetryPolicy:
    return RetryPolicy(sleep=t.sleep, clock=t.clock, jitter=0.0, **kw)


def status_error(code: int, headers: dict | None = None) -> httpx.HTTPStatusError:
    req = httpx.Request("GET", "https://api.example/x")
    return httpx.HTTPStatusError("x", request=req, response=httpx.Response(code, headers=headers or {}, request=req))


# -- classification ------------------------------------------------------------------------

@pytest.mark.parametrize("exc, transient", [
    (httpx.ConnectError("refused"), True),
    (httpx.ReadTimeout("slow"), True),
    (status_error(429), True), (status_error(503), True), (status_error(500), True),
    (status_error(404), False), (status_error(401), False), (status_error(400), False),
    (PermanentError("bad"), False), (ValueError("bad input"), False),
    (ConnectionResetError(), True),
])
def test_classification(exc, transient):
    assert is_transient(exc) is transient


def test_openai_errors_classified():
    import openai
    req = httpx.Request("POST", "https://strata/v1/chat/completions")
    assert is_transient(openai.APIConnectionError(request=req))
    assert is_transient(openai.APITimeoutError(request=req))
    assert not is_transient(openai.BadRequestError("bad", response=httpx.Response(400, request=req), body=None))


def test_retry_after_parsing():
    assert retry_after_seconds({"retry-after": "7"}) == 7
    date = "Wed, 21 Oct 2015 07:28:10 GMT"
    assert retry_after_seconds({"retry-after": date}, now=1445412480.0) == pytest.approx(10.0)
    assert retry_after_seconds({}) is None and retry_after_seconds({"retry-after": "soon"}) is None


# -- retry policy -------------------------------------------------------------------------------

def test_policy_retries_transient_then_succeeds():
    t, calls = FakeTime(), []

    def flaky():
        calls.append(1)
        if len(calls) < 3:
            raise httpx.ConnectError("down")
        return "ok"
    assert policy(t).call(flaky) == "ok"
    assert len(calls) == 3 and t.sleeps == [1.0, 2.0]          # exponential backoff


def test_policy_does_not_retry_permanent():
    t, calls = FakeTime(), []

    def bad():
        calls.append(1)
        raise status_error(404)
    with pytest.raises(httpx.HTTPStatusError):
        policy(t).call(bad)
    assert len(calls) == 1 and t.sleeps == []


def test_policy_honours_retry_after_and_caps_it():
    t = FakeTime()
    seq = [status_error(429, {"retry-after": "5"}), status_error(429, {"retry-after": "9999"})]

    def rate_limited():
        if seq:
            raise seq.pop(0)
        return "ok"
    assert policy(t, max_retry_after=60, max_elapsed=1000).call(rate_limited) == "ok"
    assert t.sleeps == [5.0, 60.0]


def test_policy_stops_at_attempts_and_elapsed_budget():
    t = FakeTime()
    with pytest.raises(httpx.ConnectError):
        policy(t, attempts=3).call(lambda: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert len(t.sleeps) == 2
    t2 = FakeTime()
    with pytest.raises(httpx.ConnectError):
        policy(t2, attempts=10, base=10, max_elapsed=25).call(lambda: (_ for _ in ()).throw(httpx.ConnectError("x")))
    assert sum(t2.sleeps) <= 25


# -- circuit breaker ---------------------------------------------------------------------------------

def test_breaker_opens_then_half_opens_then_closes():
    t = FakeTime()
    b = CircuitBreaker(threshold=3, cooldown=60, clock=t.clock)
    for _ in range(3):
        b.failure()
    assert b.state == "open"
    with pytest.raises(CircuitOpen):
        b.before("api.example")
    t.now += 61
    assert b.state == "half-open"
    b.before("api.example")                 # one trial allowed
    b.success()
    assert b.state == "closed"


def test_breaker_reopens_when_half_open_trial_fails():
    t = FakeTime()
    b = CircuitBreaker(threshold=2, cooldown=10, clock=t.clock)
    b.failure(); b.failure()
    t.now += 11
    b.failure()
    assert b.state == "open"


# -- the transport -----------------------------------------------------------------------------------

def transport_with(responses, t: FakeTime, **kw):
    seen = []

    def handler(request):
        seen.append(request.url.host)
        r = responses.pop(0)
        if isinstance(r, Exception):
            raise r
        return r
    tr = RetryTransport(httpx.MockTransport(handler), policy(t, **kw), breakers=Breakers(threshold=3, cooldown=60, clock=t.clock))
    return tr, seen


def test_transport_retries_5xx_and_connect_errors():
    t = FakeTime()
    tr, seen = transport_with([httpx.ConnectError("x"), httpx.Response(503), httpx.Response(200, text="ok")], t)
    with httpx.Client(transport=tr) as c:
        r = c.get("https://api.example/works")
    assert r.status_code == 200 and r.text == "ok" and len(seen) == 3


def test_transport_returns_last_status_when_retries_run_out():
    t = FakeTime()
    tr, seen = transport_with([httpx.Response(503)] * 4, t, attempts=4)
    with httpx.Client(transport=tr) as c:
        assert c.get("https://api.example/").status_code == 503
    assert len(seen) == 4


def test_transport_does_not_retry_404():
    t = FakeTime()
    tr, seen = transport_with([httpx.Response(404)], t)
    with httpx.Client(transport=tr) as c:
        assert c.get("https://api.example/missing").status_code == 404
    assert len(seen) == 1 and t.sleeps == []


def test_transport_breaker_fails_fast_after_repeated_failures():
    t = FakeTime()
    tr, seen = transport_with([httpx.Response(503)] * 3, t, attempts=3)
    with httpx.Client(transport=tr) as c:
        c.get("https://down.example/")
        with pytest.raises(CircuitOpen):
            c.get("https://down.example/again")
    assert len(seen) == 3                    # the second request never reached the host


# -- deadlines --------------------------------------------------------------------------------------------

def test_deadline():
    t = FakeTime()
    d = Deadline(100, clock=t.clock)
    t.now = 40
    assert d.remaining() == 60 and d.fraction_used() == 0.4
    t.now = 101
    with pytest.raises(DeadlineExceeded):
        d.check()
    assert Deadline(None).remaining() == float("inf")


def test_run_with_time_limit():
    assert run_with_time_limit(lambda: 5, 1.0) == 5
    with pytest.raises(DeadlineExceeded):
        run_with_time_limit(time.sleep, 0.05, 1.0)


def test_chaos_switch_marks_hosts_down(monkeypatch):
    monkeypatch.setenv("SCOUT_CHAOS_DOWN", "api.openalex.org")
    t = FakeTime()
    tr, seen = transport_with([httpx.Response(200)], t)
    with httpx.Client(transport=tr) as c:
        with pytest.raises(httpx.ConnectError, match="chaos"):
            c.get("https://api.openalex.org/works")
        assert c.get("https://api.crossref.org/works").status_code == 200
    assert seen == ["api.crossref.org"]

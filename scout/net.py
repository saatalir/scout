"""HTTP for Scout's non-browser tools, honouring the task's Tor choice.

With Tor off, requests go out directly. With Tor on, *every* request goes
through Tor, API calls included, so the user's address is hidden from the APIs
as well as from the sites the browser visits. The Tor service runs on the
host of the search Tor profile (the browser VM): Scout opens an SSH tunnel to
its SOCKS port and sends requests through it. Hostnames are resolved at the
Tor exit (SOCKS5 with remote DNS), never locally.
"""

from __future__ import annotations

import socket
import subprocess
import time
from contextlib import contextmanager
from typing import Callable, Iterator
from urllib.parse import urlparse

import httpx

from .browser.config import load_browser_config
from .browser.hosts import SSH_OPTS, free_port
from .resilience import RetryPolicy, RetryTransport

USER_AGENT = "OnlineScout/0.1 (research agent; +https://github.com/saatalir/scout)"
TIMEOUT = httpx.Timeout(30.0, connect=15.0)


class TorUnavailable(RuntimeError):
    """Tor was requested but no Tor route could be opened. Never fall back to direct."""


def _wait_port(port: int, proc: subprocess.Popen, timeout: float = 15.0) -> None:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            err = (proc.stderr.read() if proc.stderr else b"").decode(errors="replace").strip()
            raise TorUnavailable(f"ssh tunnel to the Tor host exited: {err[-300:] or proc.returncode}")
        with socket.socket() as s:
            s.settimeout(0.5)
            if s.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.2)
    raise TorUnavailable("ssh tunnel to the Tor host did not open in time")


@contextmanager
def tor_proxy_url() -> Iterator[str]:
    """A local SOCKS5 URL that reaches the configured Tor service."""
    cfg = load_browser_config()
    if not cfg.search.tor_profile:
        raise TorUnavailable("config/browsers.yaml sets no search.tor_profile")
    profile = cfg.profile(cfg.search.tor_profile)
    proxy = urlparse(profile.proxy or "")
    if proxy.scheme not in ("socks5", "socks5h") or not proxy.port:
        raise TorUnavailable(f"profile {profile.name} has no socks5 proxy to use for Tor")
    if profile.host.kind == "local":
        yield f"socks5://{proxy.hostname}:{proxy.port}"
        return
    local = free_port()
    proc = subprocess.Popen(
        ["ssh", *SSH_OPTS, *profile.host.ssh_opts, "-N", "-o", "ExitOnForwardFailure=yes",
         "-L", f"127.0.0.1:{local}:{proxy.hostname}:{proxy.port}", profile.host.target],
        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    try:
        _wait_port(local, proc)
        yield f"socks5://127.0.0.1:{local}"
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()


def _client(proxy: str | None, timeout: httpx.Timeout, on_retry: Callable[[str], None] | None,
            policy: RetryPolicy | None) -> httpx.Client:
    # Every request goes through RetryTransport (R1): backoff, Retry-After, per-host circuit breaker.
    inner = httpx.HTTPTransport(proxy=proxy, retries=0) if proxy else httpx.HTTPTransport(retries=0)
    return httpx.Client(headers={"User-Agent": USER_AGENT}, timeout=timeout, follow_redirects=True,
                        transport=RetryTransport(inner, policy, on_retry=on_retry))


@contextmanager
def http_client(tor: bool, *, on_retry: Callable[[str], None] | None = None,
                policy: RetryPolicy | None = None) -> Iterator[httpx.Client]:
    if not tor:
        with _client(None, TIMEOUT, on_retry, policy) as c:
            yield c
        return
    with tor_proxy_url() as proxy:
        # Tor circuits are slower and fail more often: longer timeouts, a little more patience.
        with _client(proxy, httpx.Timeout(60.0, connect=30.0), on_retry,
                     policy or RetryPolicy(attempts=5, max_elapsed=180.0)) as c:
            yield c

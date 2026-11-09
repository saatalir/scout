"""Start a profile's browser on its host and expose its debugging endpoint locally.

Local host: launch the browser on this machine (or reuse one already listening
on the profile's port).

SSH host: ask the host's `scout-browser` helper to start the profile, then
forward a local port to the browser's loopback-only debugging port over SSH.
Scout always talks to http://127.0.0.1:<local port>, wherever the browser runs.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import socket
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Awaitable, Callable

import httpx

from .config import Profile

MAC_APPS = {
    "brave": "/Applications/Brave Browser.app/Contents/MacOS/Brave Browser",
    "chrome": "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
}
LINUX_BINARIES = {
    "brave": ["brave-browser", "brave"],
    "chrome": ["google-chrome", "google-chrome-stable"],
    "chromium": ["chromium", "chromium-browser"],
}
LOCAL_PROFILES = Path(os.getenv("SCOUT_LOCAL_PROFILES", Path.home() / ".scout" / "profiles"))
SSH_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=3"]


class BrowserHostError(RuntimeError):
    pass


@dataclass
class Endpoint:
    """A running browser reachable from this machine."""
    http_url: str                       # http://127.0.0.1:<port>
    ws_url: str                         # the CDP websocket, rewritten to the local port
    version: str
    info: dict = field(default_factory=dict)
    _cleanup: list[Callable[[], Awaitable[None]]] = field(default_factory=list, repr=False)

    async def release(self) -> None:
        while self._cleanup:
            fn = self._cleanup.pop()
            try:
                await fn()
            except Exception as e:  # noqa: BLE001 - cleanup must not mask the real error
                print(f"[scout.browser] cleanup failed: {e}", file=sys.stderr)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def wait_for_cdp(port: int, timeout: float = 30.0, proc: asyncio.subprocess.Process | None = None,
                       what: str = "browser") -> dict:
    """Poll /json/version until the debugging endpoint answers."""
    deadline = asyncio.get_running_loop().time() + timeout
    async with httpx.AsyncClient(timeout=1.5) as client:
        while True:
            if proc is not None and proc.returncode is not None:
                err = (await proc.stderr.read()).decode(errors="replace")[-400:] if proc.stderr else ""
                raise BrowserHostError(f"{what} exited with code {proc.returncode}: {err.strip()}")
            try:
                r = await client.get(f"http://127.0.0.1:{port}/json/version")
                if r.status_code == 200:
                    return r.json()
            except httpx.HTTPError:
                pass
            if asyncio.get_running_loop().time() > deadline:
                raise BrowserHostError(f"{what} did not answer on 127.0.0.1:{port} within {timeout:.0f}s")
            await asyncio.sleep(0.25)


def _endpoint(local_port: int, version: dict, info: dict) -> Endpoint:
    # The websocket URL names the browser's own port; rewrite it to the local one
    # so it works through a tunnel too.
    ws = version["webSocketDebuggerUrl"]
    path = ws.split("/devtools/", 1)[1]
    return Endpoint(http_url=f"http://127.0.0.1:{local_port}",
                    ws_url=f"ws://127.0.0.1:{local_port}/devtools/{path}",
                    version=version.get("Browser", "?"), info=info)


async def _terminate(proc: asyncio.subprocess.Process, grace: float = 5.0) -> None:
    if proc.returncode is not None:
        return
    proc.terminate()
    try:
        await asyncio.wait_for(proc.wait(), grace)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()


# -- local -------------------------------------------------------------------

def local_binary(browser: str, chromium_path: str | None) -> str:
    if browser == "chromium" and chromium_path:
        return chromium_path
    if sys.platform == "darwin" and browser in MAC_APPS and Path(MAC_APPS[browser]).exists():
        return MAC_APPS[browser]
    for cand in LINUX_BINARIES.get(browser, []):
        if shutil.which(cand):
            return shutil.which(cand)  # type: ignore[return-value]
    raise BrowserHostError(f"No local binary for browser {browser!r}")


def browser_args(profile: Profile, port: int, data_dir: Path) -> list[str]:
    w, h = profile.window
    args = [f"--user-data-dir={data_dir}", f"--remote-debugging-port={port}",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--disable-session-crashed-bubble", f"--window-size={w},{h}", f"--lang={profile.locale}"]
    if profile.headless:
        args.append("--headless=new")
    if profile.proxy:
        args.append(f"--proxy-server={profile.proxy}")
    if profile.extensions:
        args.append("--load-extension=" + ",".join(profile.extensions))
    return args + list(profile.flags)


async def start_local(profile: Profile, chromium_path: str | None = None) -> Endpoint:
    # Reuse a browser already listening on the profile's port: that is how an
    # existing local session is attached to.
    if profile.port:
        try:
            ver = await wait_for_cdp(profile.port, timeout=0.5)
            return _endpoint(profile.port, ver, {"reused": True, "profile": profile.name})
        except BrowserHostError:
            pass

    port = profile.port or free_port()
    temp = profile.stop_on_exit
    data_dir = Path(tempfile.mkdtemp(prefix=f"scout-{profile.name}-")) if temp else LOCAL_PROFILES / profile.name
    data_dir.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, TZ=profile.timezone)
    # Browser output goes to a file: an unread pipe fills up and stalls the browser.
    log_path = data_dir.parent / f"{data_dir.name}.log"
    log = open(log_path, "ab")
    try:
        proc = await asyncio.create_subprocess_exec(
            local_binary(profile.browser, chromium_path), *browser_args(profile, port, data_dir),
            stdin=asyncio.subprocess.DEVNULL, stdout=log, stderr=log, env=env, start_new_session=True)
    finally:
        log.close()
    try:
        ver = await wait_for_cdp(port, proc=proc, what=f"{profile.browser} ({profile.name}); log {log_path}")
    except BaseException:
        await _terminate(proc)
        raise
    ep = _endpoint(port, ver, {"reused": False, "profile": profile.name, "pid": proc.pid,
                               "data_dir": str(data_dir)})
    if profile.stop_on_exit:
        async def stop() -> None:
            await _terminate(proc)
            if temp:
                shutil.rmtree(data_dir, ignore_errors=True)
                log_path.unlink(missing_ok=True)
        ep._cleanup.append(stop)
    return ep


# -- ssh ---------------------------------------------------------------------

async def run_helper(profile: Profile, *args: str, timeout: float = 60.0) -> dict:
    """Run `scout-browser <args>` on the profile's host over SSH; return its JSON."""
    host = profile.host
    remote = shlex.join([host.helper, *args])
    proc = await asyncio.create_subprocess_exec(
        "ssh", *SSH_OPTS, *host.ssh_opts, host.target, remote,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise BrowserHostError(f"ssh {host.target} timed out after {timeout:.0f}s") from None
    lines = [l for l in out.decode(errors="replace").splitlines() if l.strip()]
    try:
        result = json.loads(lines[-1])
    except (IndexError, json.JSONDecodeError):
        raise BrowserHostError(
            f"ssh {host.target} {remote!r} failed (exit {proc.returncode}): "
            f"{err.decode(errors='replace').strip()[-400:] or 'no output'}") from None
    if not result.get("ok"):
        raise BrowserHostError(f"{host.name}: {result.get('error', result)}")
    return result


def helper_start_args(profile: Profile) -> list[str]:
    w, h = profile.window
    args = ["start", profile.name, "--port", str(profile.port), "--browser", profile.browser,
            "--window", f"{w},{h}", "--locale", profile.locale, "--timezone", profile.timezone]
    if profile.headless:
        args.append("--headless")
    if profile.proxy:
        args += ["--proxy", profile.proxy]
    for ext in profile.extensions:
        args += ["--extension", ext]
    for flag in profile.flags:
        args.append(f"--flag={flag}")
    return args


async def start_ssh(profile: Profile) -> Endpoint:
    if not profile.port:
        raise BrowserHostError(f"profile {profile.name}: a remote profile needs a fixed port")
    started = await run_helper(profile, *helper_start_args(profile))

    local_port = free_port()
    tunnel = await asyncio.create_subprocess_exec(
        "ssh", *SSH_OPTS, *profile.host.ssh_opts, "-N", "-o", "ExitOnForwardFailure=yes",
        "-L", f"127.0.0.1:{local_port}:127.0.0.1:{profile.port}", profile.host.target,
        stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE)
    try:
        ver = await wait_for_cdp(local_port, timeout=20, proc=tunnel, what=f"ssh tunnel to {profile.host.name}")
    except BaseException:
        await _terminate(tunnel)
        raise
    ep = _endpoint(local_port, ver, {**started, "host": profile.host.name, "local_port": local_port})

    async def close_tunnel() -> None:
        await _terminate(tunnel)
    ep._cleanup.append(close_tunnel)
    if profile.stop_on_exit:
        async def stop_remote() -> None:
            await run_helper(profile, "stop", profile.name)
        ep._cleanup.insert(0, stop_remote)   # runs last, after the tunnel closes
    return ep


async def start_profile(profile: Profile, chromium_path: str | None = None) -> Endpoint:
    if profile.host.kind == "local":
        return await start_local(profile, chromium_path)
    return await start_ssh(profile)

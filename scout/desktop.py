"""Phase 3 D1 — the disposable desktop: a container on the browser VM with a virtual screen, a window manager and
Chromium, opened per task and removed when the task closes it (or ends).

Scout drives it only through the `scout-browser desktop-*` helper commands over SSH (Scout's key, BatchMode),
and only through the vision loop (`scout/vision.py`): it has no page model. The container (rootless podman) has
no capabilities, a read-only root, no access to the VM's loopback services (the signed-in browsers' DevTools
ports), and its live view (noVNC) is published on the VM's 127.0.0.1 only; Scout reaches it through an SSH tunnel.

Limits, by design:
- No Tor route: with the task's Tor choice on, the desktop is not opened (it would leave without Tor).
- Files the desktop's browser downloads stay in the container until `desktop_close` copies them out; each passes
  the same verification as any download. Removing the container removes everything else.
"""

from __future__ import annotations

import asyncio
import base64
import re
import subprocess
import time
from typing import Any

from .browser import hosts
from .browser.config import Profile

DESKTOP_TOOLS = [
    {"type": "function", "function": {
        "name": "desktop_open",
        "description": "Open a URL in a disposable desktop (a fresh browser on a virtual screen, removed afterwards). "
                       "Only when the real browser cannot do the step: a native dialog, a plugin, a page that "
                       "breaks under automation. Then act with the vision tools; desktop_close when done.",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "desktop_close",
        "description": "Close the desktop: files its browser downloaded are copied out and verified first.",
        "parameters": {"type": "object", "properties": {}, "required": []}}},
]
DESKTOP_TOOL_NAMES = {t["function"]["name"] for t in DESKTOP_TOOLS}


def desktop_name(task_id: str) -> str:
    name = re.sub(r"[^a-z0-9_-]+", "-", task_id.lower()).strip("-_")
    return (name or "task")[:40].rstrip("-_") or "task"


def _run(coro):
    return asyncio.run(coro)


class Desktop:
    def __init__(self, profile: Profile, name: str):
        if profile.host.kind == "local":
            raise hosts.BrowserHostError("there is no disposable desktop in local mode (SCOUT_WHERE=local): "
                                         "it runs only on the browser VM")
        self.profile, self.name = profile, name
        self.info: dict = {}
        self.tunnel: subprocess.Popen | None = None
        self.live_view: str | None = None

    def helper(self, *args: str, timeout: float = 60.0) -> dict:
        return _run(hosts.run_helper(self.profile, *args, timeout=timeout))

    def start(self, url: str, geometry: str = "1280x800") -> dict:
        try:                                       # a desktop left over from a crashed run of this task
            self.helper("desktop-stop", self.name, timeout=60)
        except hosts.BrowserHostError:
            pass
        self.info = self.helper("desktop-start", self.name, "--url", url, "--geometry", geometry, timeout=150)
        local = hosts.free_port()
        host = self.profile.host
        self.tunnel = subprocess.Popen(
            ["ssh", *hosts.SSH_OPTS, *host.ssh_opts, "-N", "-o", "ExitOnForwardFailure=yes",
             "-L", f"127.0.0.1:{local}:127.0.0.1:{self.info['view_port']}", host.target],
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.live_view = f"http://127.0.0.1:{local}/vnc.html?autoconnect=1&resize=scale"
        return {"desktop": self.name, "size": f"{self.info['width']}x{self.info['height']}",
                "live_view": self.live_view}

    def screen(self) -> "DesktopScreen":
        return DesktopScreen(self)

    def files(self) -> list[dict]:
        return self.helper("desktop-files", self.name).get("files", [])

    def fetch(self, file: str) -> bytes:
        got = self.helper("desktop-fetch", self.name, file, timeout=180)
        return base64.b64decode(got["data_b64"])

    def stop(self) -> None:
        try:
            self.helper("desktop-stop", self.name, timeout=60)
        finally:
            if self.tunnel is not None and self.tunnel.poll() is None:
                self.tunnel.terminate()
                try:
                    self.tunnel.wait(5)
                except subprocess.TimeoutExpired:
                    self.tunnel.kill()
            self.tunnel = None


class DesktopScreen:
    """The desktop as a `scout.vision.Screen`. It cannot see inside the page, so `at` and `focused` return None:
    the vision gates then decide from what is on screen (and refuse typing without a described target)."""
    name = "desktop"

    def __init__(self, desktop: Desktop):
        self.d = desktop

    def shot(self) -> bytes:
        return base64.b64decode(self.d.helper("desktop-shot", self.d.name, timeout=45)["png_b64"])

    def click(self, x: int, y: int) -> None:
        self.d.helper("desktop-act", self.d.name, "--click", f"{x},{y}")
        time.sleep(1.0)

    def type_text(self, text: str) -> None:
        self.d.helper("desktop-act", self.d.name, "--type", text, timeout=150)

    def key(self, key: str) -> None:
        self.d.helper("desktop-act", self.d.name, "--key", key)
        time.sleep(1.0)

    def scroll(self, dy: int) -> None:
        self.d.helper("desktop-act", self.d.name, "--scroll", str(max(1, abs(dy) // 140) * (1 if dy > 0 else -1)))
        time.sleep(0.5)

    def at(self, x: int, y: int) -> dict | None:
        return None

    def focused(self) -> dict | None:
        return None

    def state(self) -> dict[str, Any]:
        return {"desktop": self.d.name}

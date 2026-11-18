"""Phase 3 D1: the disposable desktop. The VM helper's desktop commands run locally through tests/fake_ssh.py,
with tests/fake_podman.py in place of podman; the vision model is faked. Checks the container's isolation flags,
the start → screenshot → act → copy out → stop round trip, the vision gates on a screen that cannot see inside
the page, and the agent's Tor and clean-up rules."""

from __future__ import annotations

import json
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
import yaml

from scout import desktop as dmod
from scout import vision
from scout.agent import Scout
from scout.approvals import ApprovalPolicy
from scout.browser.config import load_browser_config
from scout.resilience import RetryPolicy
from scout.task import TaskRecord
from test_browser_tools import Fake
from test_phase1 import call, handler, make_pdf

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "deploy" / "browser-vm" / "scout-browser"
PDF = make_pdf("Quarterly Report")


@pytest.fixture
def vm(tmp_path, monkeypatch):
    """A fake VM: ssh runs locally, the helper is the real one, podman is the fake."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    (bindir / "ssh").symlink_to(ROOT / "tests" / "fake_ssh.py")
    wrapper = bindir / "scout-browser"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {HELPER} \"$@\"\n")
    wrapper.chmod(0o755)
    podman = bindir / "podman"
    podman.write_text(f"#!/bin/sh\nexec {sys.executable} {ROOT / 'tests' / 'fake_podman.py'} \"$@\"\n")
    podman.chmod(0o755)
    state = tmp_path / "podman"
    state.mkdir()
    (state / "image").touch()
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_PODMAN_DIR", str(state))
    monkeypatch.setenv("SCOUT_BROWSER_HOME", str(tmp_path / "vm-home"))
    monkeypatch.setenv("SCOUT_LIBRARY_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("SCOUT_TASKS_DIR", str(tmp_path / "tasks"))
    monkeypatch.setenv("SCOUT_PLAYBOOKS_DIR", str(tmp_path / "playbooks"))
    cfg = tmp_path / "browsers.yaml"
    cfg.write_text(yaml.safe_dump({
        "hosts": {"vm": {"kind": "ssh", "address": "vm", "user": "scout", "helper": str(wrapper)}},
        "profiles": {"remote": {"host": "vm", "port": 9301, "browser": "chromium"}},
        "search": {"profile": "remote"}}))
    monkeypatch.setenv("SCOUT_BROWSERS_FILE", str(cfg))
    return SimpleNamespace(state=state, containers=state / "containers")


def actions(vm, name: str) -> list[list[str]]:
    log = vm.containers / f"scout-desk-{name}" / "actions.jsonl"
    return [json.loads(l) for l in log.read_text().splitlines()] if log.exists() else []


def profile():
    return load_browser_config().profile("remote")


class Eyes:
    def __init__(self, where=None, answer="no"):
        self.where, self.answer, self.prompts = where, answer, []

    def chat(self, role, messages, **kw):
        assert role == "vision"
        prompt = messages[-1]["content"][1]["text"]
        self.prompts.append(prompt)
        return SimpleNamespace(text=json.dumps(self.where) if "Find:" in prompt else self.answer, tool_calls=[], raw=None)


# -- the helper and the round trip ------------------------------------------------------------------------------

def test_desktop_round_trip_and_isolation_flags(vm):
    d = dmod.Desktop(profile(), "t1")
    out = d.start("https://example.org/page")
    try:
        assert out["size"] == "1280x800" and out["live_view"].startswith("http://127.0.0.1:")
        run = json.loads((vm.containers / "scout-desk-t1" / "run.json").read_text())
        flags = " ".join(run)
        for must in ("--rm", "--cap-drop all", "--security-opt no-new-privileges", "--read-only",
                     "--network slirp4netns:allow_host_loopback=false", "--pids-limit", "--memory"):
            assert must in flags, must
        publish = run[run.index("-p") + 1]
        assert publish.startswith("127.0.0.1:") and publish.endswith(":6080")      # the view: VM loopback only
        assert "SCOUT_URL=https://example.org/page" in run
        scr = d.screen()
        first = scr.shot()
        assert vision.png_size(first) == (1280, 800)
        scr.click(640, 400)
        scr.type_text("hello; rm -rf / $(whoami)")                                    # argv, never a shell
        scr.key("enter")
        scr.scroll(-700)
        assert actions(vm, "t1") == [
            ["xdotool", "mousemove", "--sync", "640", "400", "click", "1"],
            ["xdotool", "type", "--delay", "25", "--", "hello; rm -rf / $(whoami)"],
            ["xdotool", "key", "--clearmodifiers", "Return"],
            ["xdotool", "click", "--repeat", "5", "4"]]
        assert scr.shot() != first
        (vm.containers / "scout-desk-t1" / "Downloads" / "report.pdf").write_bytes(PDF)
        (vm.containers / "scout-desk-t1" / "Downloads" / "half.pdf.crdownload").write_bytes(b"x")
        assert d.files() == [{"name": "report.pdf", "bytes": len(PDF)}]
        assert d.fetch("report.pdf") == PDF
    finally:
        d.stop()
    assert not (vm.containers / "scout-desk-t1").exists()                             # disposable
    assert d.tunnel is None


def test_helper_refuses_bad_input(vm):
    d = dmod.Desktop(profile(), "t2")
    with pytest.raises(Exception, match="http"):
        d.helper("desktop-start", "t2", "--url", "file:///etc/passwd")
    with pytest.raises(Exception, match="bad file name"):
        d.helper("desktop-fetch", "t2", "../../etc/passwd")
    with pytest.raises(Exception, match="unknown key"):
        d.helper("desktop-act", "t2", "--key", "ctrl+alt+t")
    (vm.state / "image").unlink()
    with pytest.raises(Exception, match="setup.sh"):
        d.helper("desktop-start", "t2", "--url", "https://example.org")
    assert dmod.desktop_name("20261118-081949-Read the EC's page!") == "20261118-081949-read-the-ec-s-page"


# -- vision on a screen that cannot see inside the page ------------------------------------------------------------

def test_vision_gates_on_the_desktop(vm):
    d = dmod.Desktop(profile(), "t3")
    d.start("https://example.org")
    try:
        asked = []
        vt = vision.VisionTools(llm=Eyes(where={"x": 100, "y": 200, "label": "Send"}), screen=d.screen,
                                approve=lambda s: asked.append(s) or False)
        assert vt.call("vision_click", {"target": "the Send button"})["decision"] == "denied" and asked
        vt.llm.where = {"x": 100, "y": 200, "label": "Accept all"}
        assert "never accepts cookies" in vt.call("vision_click", {"target": "the cookie banner's button"})["error"]
        assert "cannot tell" in vt.call("vision_type", {"target": "", "text": "x"})["error"]
        vt.llm.where, vt.llm.answer = {"x": 100, "y": 200, "label": ""}, "Yes, it shows masked dots."
        assert "password field" in vt.call("vision_type", {"target": "the second box", "text": "secret"})["error"]
        assert "Enter" in vt.call("vision_key", {"key": "enter"}).get("note", "") or asked[-1].startswith("Press Enter")
        assert not actions(vm, "t3")                                                 # nothing reached the desktop
        vt.llm.answer = "no"
        out = vt.call("vision_type", {"target": "the search box", "text": "ammonia"})
        assert out["typed"] == 7 and out["changed"] is True and out["screen"] == "desktop"
    finally:
        d.stop()


# -- the agent ----------------------------------------------------------------------------------------------------

class Both(Fake):
    def __init__(self, script, eyes):
        super().__init__(script)
        self.eyes = eyes

    def chat(self, role, messages, **kw):
        return self.eyes.chat(role, messages, **kw) if role == "vision" else super().chat(role, messages, **kw)


def scout(script, eyes, tor=False):
    @contextmanager
    def client(tor=False):
        with httpx.Client(transport=httpx.MockTransport(handler)) as c:
            yield c
    rec = TaskRecord.create("Get the report from https://example.org/app", tor=tor)
    return Scout(rec, llm=Both(script, eyes), client_factory=client, ask_approval=lambda s: True,
                 approvals=ApprovalPolicy("ask"), verify=False,
                 model_policy=RetryPolicy(attempts=2, base=0, sleep=lambda s: None))


def results(rec):
    out = []
    for m in rec.messages:
        if m["role"] == "tool":
            try:
                out.append(json.loads(m["content"]))
            except ValueError:
                out.append({"raw": m["content"]})
    return out


def test_agent_opens_acts_copies_out_and_closes(vm):
    def drop_file(messages):                       # the desktop's browser "downloads" a file meanwhile
        (next(vm.containers.iterdir()) / "Downloads" / "report.pdf").write_bytes(PDF)
        return [call("desktop_close")]
    s = scout([[call("desktop_open", url="https://example.org/app")],
               [call("vision_click", target="the Export button")],
               drop_file,
               [call("finish", report="[done]")]], Eyes(where={"x": 50, "y": 60, "label": "Export"}))
    rec = s.run()
    opened, clicked, closed = results(rec)[:3]
    assert opened["desktop"] == rec.state.get("desktop", {}).get("name", opened["desktop"])
    assert clicked["screen"] == "desktop" and clicked["changed"] is True
    assert closed["closed"] is True and closed["files"][0]["verified"] is True
    assert Path(closed["files"][0]["path"]).read_bytes() == PDF
    assert not any(vm.containers.iterdir()) and "desktop" not in rec.state


def test_agent_removes_the_desktop_when_the_task_ends(vm):
    rec = scout([[call("desktop_open", url="https://example.org/app")], [call("finish", report="[done]")]],
                Eyes()).run()
    assert rec.status == "done" and not any(vm.containers.iterdir())


def test_no_desktop_with_tor_on(vm):
    rec = scout([[call("desktop_open", url="https://example.org/app")], [call("finish", report="[not done]")]],
                Eyes(), tor=True).run()
    assert "Tor" in results(rec)[0]["error"] and not vm.containers.exists()


def test_a_vm_without_the_desktop_is_reported_not_fatal(vm):
    (vm.state / "image").unlink()
    rec = scout([[call("desktop_open", url="https://example.org/app")], [call("finish", report="[partial]")],
                 [call("finish", report="[partial] no desktop on the VM")]], Eyes()).run()   # a failure: Scout asks once more
    out = results(rec)[0]
    assert "setup.sh" in out["error"] and "vision tools" in out["next"] and rec.status == "done"

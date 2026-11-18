# Phase 3 — vision and the desktop tier

Phase 3 lets Scout work with what the page model cannot describe: a chart or map drawn on a canvas, a plugin, a
custom widget (vision on the browser page), and, when the real browser cannot do a step at all, a **disposable
desktop** on the VM. It also turns repeated successful browser runs into proposed playbooks. The plan and state
of each package is in [ROADMAP §3](ROADMAP.md#phase-3--desktop-vision).

## 1. The vision loop (D2)

`scout/vision.py`. The tools act on the **current screen**: the browser page, or the desktop while one is open.

| Tool | What it does |
| --- | --- |
| `vision_look` | Screenshot → the vision role answers a question about it ("what are the x-axis labels?") |
| `vision_click` | The planner **describes** the target ("the blue Export button under the chart"); the vision role finds it on a screenshot (pixel coordinates); Scout clicks there |
| `vision_type` | Click a described field, then type |
| `vision_key` | One key from a fixed list (enter, tab, escape, arrows…) |
| `vision_scroll` | Scroll up or down |

**Check after each action:** a new screenshot is taken and compared with the one before. If nothing changed, the
result says so ("it may have missed"), so the planner tries another description instead of assuming success.
Screenshots are kept in the task folder (`screenshots/vision-N.png`).

**Gates, in code, on every screen:**

- **Submits:** if the target, the label the vision role read, or (in the browser) the element at the point looks
  like it submits or commits something, the click goes through the approval gate, like `click_submit`. Enter in
  a form field asks too; on the desktop, which cannot tell what has focus, Enter always asks.
- **Passwords:** never typed. The target's wording (password, PIN, passcode…), the element at the point and the
  focused element (browser), or a yes/no question to the vision role about the field (desktop) each refuse it. On
  the desktop, typing needs a described target (Scout cannot tell what has focus there).
- **CAPTCHAs:** never solved. A target that names one is refused before looking; the vision role reports a CAPTCHA
  on screen instead of a position; a challenge iframe at the point (browser) is refused. The answer is
  `hand_to_human`.
- **Cookies:** a click that would accept cookies or tracking ("Accept all", "Accept" on a cookie banner) is
  refused; reject and necessary-only labels pass (the same rule as B6).

Vision steps appear in the task's `trace.yaml` as `fuzzy` steps (described, not replayed blindly).

## 2. When the page needs vision (D3)

The page model now flags content the element list cannot show: a visible `canvas`, `embed`, `object`, large
`svg` without text, `[role=img]` or `[role=application]` of at least 200×200 px. The page result then carries
`visual: "Part of this page is drawn, not text (canvas 600x300): … use vision_look to read it and vision_click to
act on it."` The planner's prompt says to prefer element ids when the element is listed, and to escalate to the
desktop only when the browser cannot do the step.

## 3. The disposable desktop (D1)

`scout/desktop.py`, `deploy/desktop/` (image), `scout-browser desktop-*` (helper on the VM).

- **What:** a container (rootless podman, image `scout-desktop`: Debian, Xvfb, openbox, Chromium, xdotool,
  x11vnc, noVNC) started by `desktop_open(url)`, driven only by the vision tools, and removed by `desktop_close`
  or when the task ends, whatever happened. Files its browser downloaded are copied out by `desktop_close` and
  verified like any download; everything else disappears with the container.
- **Isolation:** no capabilities, `no-new-privileges`, read-only root (tmpfs for `/tmp` and the home), memory,
  CPU and process limits, and `slirp4netns:allow_host_loopback=false`, so a page in it cannot reach the VM's
  loopback services (the signed-in browsers' DevTools ports, VNC). Rootless podman, so the `scout` user gets no
  root-equivalent group (unlike Docker's). Chromium's own sandbox cannot run inside a rootless container; the
  container is the sandbox.
- **Live view:** noVNC inside the container, published on the VM's `127.0.0.1` only (ports 6100–6199); Scout
  opens an SSH tunnel and reports `http://127.0.0.1:<port>/vnc.html` on this machine.
- **Tor:** the desktop has no Tor route. With the task's Tor choice on, `desktop_open` refuses (it would leave
  without Tor); the browser's vision tools still work through the Tor profile.
- **No desktop on the VM** (image missing, podman missing): `desktop_open` returns the error and the next step
  ("continue in the browser, or hand_to_human"); the task goes on.

**Setup:** rerun `sudo bash deploy/browser-vm/setup.sh` on the VM (with `deploy/desktop/` copied next to
`deploy/browser-vm/`). It installs podman, slirp4netns and uidmap, gives `scout` subordinate ids, enables
lingering, builds the image as `scout` and runs a start/stop smoke test.

## 4. Playbook promotion (D4)

`scout/promote.py`, `python -m scout promote [--min 2]`. Every task that finishes `done` leaves its browser actions
in `trace.yaml` (Phase 2, B5). Promotion groups the traces by **flow** (the first URL plus the shape of the steps:
which fields were typed or selected, what was clicked and submitted; a run of ticked boxes counts as one step) and
turns every flow with at least `--min` successful runs into a **proposed** playbook in `playbooks/proposed/`:

- a value that differed between runs becomes a required param; a set of ticked boxes that differed becomes one
  list param (`choices`) with a `for_each` step;
- **every typed value becomes a param**, even one that was the same in every run (it may be the user's own
  phone number or address); such a param is optional. No typed value and no objective text from a trace is written
  into a proposal, and `playbooks/proposed/` is git-ignored;
- a flow an active playbook already covers is skipped.

Nothing proposed is active: `load_playbooks` reads only the playbooks folder itself. A human reviews the file (which
params are required, the literal values left, the submit summary) and moves it up to activate it.

Live check (2026-11-18): the traces of the G2 runs gave two proposals (the f01 and f03 flows). With only the
promoted f01 proposal in the playbooks folder, f01 passed in 3 steps from it, the submit asking for approval first.
The proposal marked the customer name optional (it was the same in every eval run), which is what review is for.

## 5. Tests

`tests/test_vision.py` (local headless browser, routed pages, faked vision model: canvas flag, look, click and the
check after it, missing targets, submit approval, Enter in a form, passwords, CAPTCHAs, the agent reading a canvas)
and `tests/test_desktop.py` (the real helper through `tests/fake_ssh.py`, `tests/fake_podman.py` for podman:
isolation flags, the round trip, argv-only typing, bad input refused, vision gates on the desktop, copy-out on
close, clean-up when the task ends, Tor refusal, a VM without the desktop) and `tests/test_promote.py` (flows,
params, check runs, selects, replay through `run_playbook`, writing and skipping, no typed values kept).

## 6. Results

Live, 2026-11-18, before G3: v01 (canvas chart) passed: the page result flagged the canvas, `vision_look` read
January to July, and the agent cross-checked the sample's source. v02 passed without vision: its download button is
a link to the file. f01 from a promoted playbook passed (§4). The desktop round trip runs offline; live once
`setup.sh` has installed podman and the image on the VM.

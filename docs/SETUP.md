# Online Scout — Setup Guide

This guide builds a working Online Scout installation from scratch. It also explains why each part is set up the way it is, so you can change the setup without breaking it.

The guide is written for both people and coding agents:

- Every step says what it changes, how to verify it, and what failure looks like.
- Machine-specific values are **placeholders** that you fill in `.env` (§4.2). Nothing in the repository names a real host, network, user or address.

| Placeholder | Meaning | Where it is set |
| --- | --- | --- |
| `<strata-host>` | The machine that serves the model (Strata) | `STRATA_BASE_URL` |
| `<tailnet>` | Your Tailscale tailnet DNS suffix, e.g. `your-tailnet.ts.net` | `SCOUT_TAILNET` |
| `<browser-vm>` | Tailnet name of the browser VM | `SCOUT_VM_HOST` |
| `<vm-admin>` | Your admin (sudo) user on the browser VM | used only during setup |
| `<strata-key>` | The API key Strata was started with | `STRATA_API_KEY` |

---

## Contents

1. [Architecture](#1-architecture)
2. [Prerequisites](#2-prerequisites)
3. [The model server (Strata)](#3-the-model-server-strata)
4. [The workstation (Scout itself)](#4-the-workstation-scout-itself)
5. [The browser VM](#5-the-browser-vm)
6. [Browser profiles](#6-browser-profiles)
7. [Tor on/off](#7-tor-onoff)
8. [Verification, step by step](#8-verification-step-by-step)
9. [Reference results](#9-reference-results)
10. [Security model](#10-security-model)
11. [Troubleshooting](#11-troubleshooting)
12. [Day-to-day operations](#12-day-to-day-operations)
13. [Known limits and open items](#13-known-limits-and-open-items)
14. [Appendix A: headless VM with cloud-init](#appendix-a-headless-vm-with-cloud-init)
15. [Appendix B: optional LiteLLM role routing](#appendix-b-optional-litellm-role-routing)

---

## 1. Architecture

```
                    Tailscale tailnet (<tailnet>) — nothing is exposed to the internet
 ┌──────────────────────────┐
 │ Workstation (macOS)      │   HTTPS + API key        ┌───────────────────────────────────────────┐
 │                          │ ───────────────────────▶ │ <strata-host>  (Linux, KVM)               │
 │  Scout (Python 3.12)     │                          │   Strata → qwen3.8-flash-next (/v1)       │
 │  · scout/llm.py          │                          │   libvirt (virt-manager / Cockpit)        │
 │  · scout/browser/        │                          │                                           │
 │  · scout/tools/search.py │   SSH (Scout's own key)  │   ┌─────────────────────────────────────┐ │
 │                          │ ───────────────────────▶ │   │ VM <browser-vm> (Ubuntu 24.04)      │ │
 │                          │   SSH tunnel → CDP port  │   │  scout-browser helper               │ │
 │                          │ ═══════════════════════▶ │   │  Brave on Xvfb :99 (one per profile)│ │
 │                          │                          │   │  tor (127.0.0.1:9050)               │ │
 └──────────────────────────┘                          │   │  noVNC live view (tailscale serve)  │ │
            ▲                                          │   └─────────────────────────────────────┘ │
            │ https://<browser-vm>.<tailnet>/vnc.html  └───────────────────────────────────────────┘
          you (watch, sign in, solve a CAPTCHA)
```

Scout uses three machines, and each one does a single job:

| Machine | Job | Why it is separate |
| --- | --- | --- |
| **Workstation** | Runs Scout's code: agents, tools, evals, tests. | It is where you develop. It never holds model weights or long-lived browser sessions. |
| **`<strata-host>`** | Runs Strata (the model server) and hosts the browser VM. | It has the RAM and cores. The model and its expert cache sit in memory here. |
| **Browser VM** | Runs Brave, one window per profile, on a virtual screen. | Sign-ins, cookies and any risk from browsing stay inside a disposable VM. The model host and the workstation stay clean. |

The browser VM does not have to live on the model host. Any Linux machine on the tailnet works, because Scout only needs SSH access to it.

### Design decisions

| Decision | Reason |
| --- | --- |
| Agents ask for a **role** (`planner`, `browser`, `vision`, `extractor`), never a model. | The model behind a role can change in one place (`.env` or the LiteLLM template) without code changes. |
| The model is **pinned** (`STRATA_MODEL`). | Scout can never silently use a different model than the one that passed the eval gate. |
| **Search uses no API keys.** It goes through a real browser: DuckDuckGo, then Brave Search, then Google. | No accounts or quotas. The next engine is used only when the current one is blocked or returns too few results. |
| **Scout never bypasses a CAPTCHA.** | A blocked engine is recorded and skipped. If every engine is blocked, the search returns `needs_human` with the live-view link. |
| **Browsers keep their state** (one data directory per profile). | A site you sign into once stays signed in for later tasks. |
| **Tor is a per-task choice, asked first by default** (`SCOUT_TOR=ask`). | The user decides for each task whether a search should hide the origin address. |
| **Debugging ports bind to the VM's loopback only.** | Whoever reaches a DevTools port fully controls that browser. Scout reaches it only through an SSH tunnel. |
| **Tailscale carries all traffic.** | No port on any machine is open to the internet. |

---

## 2. Prerequisites

| Item | Requirement | Notes |
| --- | --- | --- |
| Tailscale | Workstation, `<strata-host>` and the browser VM all signed in to one tailnet. MagicDNS and **HTTPS certificates** turned on in the admin console (DNS page). | HTTPS certificates are needed for `tailscale serve` (the live view, and an HTTPS address for Strata). |
| Workstation | macOS (Apple silicon tested) or Linux. Python ≥ 3.11; 3.12 from [`uv`](https://docs.astral.sh/uv/) recommended. | macOS ships Python 3.9, which is too old. |
| `<strata-host>` | Linux with KVM and libvirt, plus Strata serving Qwen3.8-Flash-Next. | Reference machine: 28 cores and 125 GB RAM; about 36 GB was still available with the model loaded. Your user must be in the `libvirt` group to manage VMs without sudo. |
| Browser VM | Ubuntu 24.04 (Desktop or Server). At least 2 vCPU, 4 GB RAM and 20 GB disk. | Reference VM: 4 vCPU, 8 GB RAM, 50 GB disk. |

**RAM budget.** Strata keeps the model and its expert cache in memory, so a VM must fit in what is left over. Check before creating or growing a VM:

```bash
free -g          # on <strata-host>: the "available" column is what new VMs can safely take
```

---

## 3. The model server (Strata)

> The full server-side procedure is in **[STRATA.md](STRATA.md)**: installing Strata, `strata launch` and its presets, publishing with `tailscale serve`, the API key, layer-by-layer checks and operations. This section summarises what Scout needs.

Scout needs only Strata's **OpenAI-compatible endpoint**, reachable from the workstation:

| Setting | Value |
| --- | --- |
| Base URL | `https://<strata-host>.<tailnet>/v1` (or `http://<strata-host>:8080/v1`) |
| Auth | Bearer `<strata-key>`, or `none` if Strata runs without auth |
| Model id | `qwen3.8-flash-next-unsloth-ud-q4_k_xl` |
| Context | 262k tokens |
| Input | Text and images |

### Requirements for Strata

1. **It must be reachable on the tailnet, not only on `127.0.0.1`.** The cleanest way is to keep Strata on loopback and publish it with `tailscale serve` on `<strata-host>`, which gives an HTTPS address that only the tailnet can reach.
2. **It must support tool calling and image input.** `scripts/check_connection.py` checks both.
3. **It must serve the pinned model id.** If you change the variant, update `STRATA_MODEL` too.

### Behaviour that affects Scout

| Behaviour | Consequence | How Scout handles it |
| --- | --- | --- |
| The model reasons before it answers (`reasoning_content`). | A small `max_tokens` can be used up entirely by reasoning, giving an empty answer with `finish_reason=length`. | Checks use at least 512 tokens and treat an empty answer as a failure. The eval runner counts truncated calls (`truncated_calls`). |
| The first request after a restart is slow (expert-cache warm-up). | First calls can take minutes. | `SCOUT_REQUEST_TIMEOUT=300`. |
| Strong prompt caching. | A repeated identical long prompt starts in about 0.3 s; a new 24k-token prompt takes about 22 s to start. | Agents should keep their context lean and keep repeated prefixes unchanged. |

---

## 4. The workstation (Scout itself)

### 4.1 Python environment

```bash
git clone <this repository> online-scout && cd online-scout
uv venv --python 3.12 .venv          # or: python3.12 -m venv .venv
source .venv/bin/activate
uv pip install -e ".[dev]"           # or: pip install -e ".[dev]"
playwright install chromium          # local test browser and vision fixtures
```

`playwright install chromium` is only for **local** tests and for rendering the vision fixtures. The browsers Scout actually uses run on the VM.

### 4.2 `.env`

```bash
cp .env.example .env
```

| Variable | Example | Purpose |
| --- | --- | --- |
| `STRATA_BASE_URL` | `https://<strata-host>.<tailnet>/v1` | Strata's endpoint, ending in `/v1`. |
| `STRATA_API_KEY` | `<strata-key>` | Strata rejects requests without it. |
| `STRATA_MODEL` | `qwen3.8-flash-next-unsloth-ud-q4_k_xl` | **Pinned.** `auto` would pick whatever model Strata lists first. |
| `SCOUT_ROUTER` | `direct` | Scout talks straight to Strata. There is no proxy and no other provider. |
| `SCOUT_REQUEST_TIMEOUT` | `300` | Covers cold starts. |
| `SCOUT_VM_HOST` | `browser-vm` | The browser VM's tailnet name (or 100.x address). |
| `SCOUT_TAILNET` | `your-tailnet.ts.net` | Used to build the live-view URL. |
| `SCOUT_VM_USER` | `scout` | The VM user that runs browsers (created by `setup.sh`). |
| `SCOUT_SSH_KEY` | `~/.ssh/scout_ed25519` | Scout's own SSH key (§4.3). |
| `SCOUT_TIMEZONE` | `UTC` | The time zone non-Tor browsers report. Tor profiles always use UTC. |
| `SCOUT_TOR` | `ask` | `ask` / `on` / `off`; see §7. |
| `SCOUT_SCRUB_PLACES` | `Springfield,Ohio` | Place names to remove from captured search pages (§8 step 6). |
| `SCOUT_SCRUB_REGIONS` | `US` | ISO country codes to remove from captured pages. |

`config/browsers.yaml` reads these with `${VAR:-default}`, so the committed config holds no machine details. `.env` is in `.gitignore`; never commit it.

### 4.3 Scout's SSH key

Scout logs in to the browser VM with **its own dedicated key**, never with a password or with your personal keys:

```bash
ssh-keygen -t ed25519 -N "" -C "scout" -f ~/.ssh/scout_ed25519
```

Scout passes `-i <key> -o IdentitiesOnly=yes -o BatchMode=yes`, so SSH never falls back to other keys or a password prompt. A problem shows up as an immediate, readable error rather than a run that hangs.

---

## 5. The browser VM

### 5.1 Create the VM

Create an Ubuntu 24.04 VM on `<strata-host>`, or on any Linux machine on the tailnet:

| Option | How |
| --- | --- |
| GUI | virt-manager or Cockpit: Ubuntu 24.04 Desktop ISO, the libvirt `default` NAT network, the sizes from §2. |
| Scripted | Appendix A: Ubuntu cloud image plus cloud-init. Headless, and can be rebuilt with one command. |

A desktop is optional. Scout's Brave windows run on their own virtual screen (`:99`), never on a desktop session.

### 5.2 Join the tailnet

Run this inside the VM:

```bash
curl -fsSL https://tailscale.com/install.sh | sh
sudo tailscale up --hostname=<browser-vm>      # prints a login link: sign in, then click "Connect"
```

> **Do not add `--ssh`.** If your tailnet's SSH policy uses **check mode**, every new Tailscale SSH connection waits for a browser approval. That stops an unattended Scout run. Scout uses plain OpenSSH with its own key instead, and `setup.sh` sets `tailscale set --ssh=false`.

If your tailnet requires device approval, approve the VM on the admin console's Machines page.

### 5.3 Open SSH and install Scout's key

Run this inside the VM. Desktop installs ship without an SSH server:

```bash
sudo apt update && sudo apt install -y openssh-server
```

Then, from the workstation, give your admin user Scout's key. You type that user's password once:

```bash
ssh-copy-id -i ~/.ssh/scout_ed25519.pub <vm-admin>@<browser-vm>
```

### 5.4 Run `setup.sh`

Run this on the workstation. The `authorized_keys` file travels with the setup files, so the `scout` user will accept Scout's key. You type `<vm-admin>`'s sudo password at the prompt:

```bash
cp ~/.ssh/scout_ed25519.pub deploy/browser-vm/authorized_keys
scp -i ~/.ssh/scout_ed25519 -r deploy/browser-vm <vm-admin>@<browser-vm>:/tmp/ && rm deploy/browser-vm/authorized_keys
ssh -t -i ~/.ssh/scout_ed25519 <vm-admin>@<browser-vm> sudo bash /tmp/browser-vm/setup.sh
```

`setup.sh` needs root and is safe to run again:

| Step | What it does | Why |
| --- | --- | --- |
| Packages | Installs `xvfb fluxbox x11vnc novnc websockify tor`, Noto and Liberation fonts. | A virtual screen, a window manager, the live view, Tor, and fonts so pages (including Greek and CJK) render correctly. |
| Brave | Adds Brave's signed apt repository and installs `brave-browser`. Skipped if Brave is already installed. | Brave is the default browser. |
| `scout` user | Creates the user with `~/profiles`, `~/run` and `~/logs`, and installs `authorized_keys`. | Browsers run unprivileged and separate from the admin account. |
| Helper | Installs `/usr/local/bin/scout-browser`. | Starts, stops and reports profiles. Its output is JSON so Scout can parse it. |
| systemd | Adds `scout-xvfb` (`:99`, 1920×1080), `scout-wm`, `scout-vnc` (loopback `:5900`) and `scout-novnc` (loopback `:6080`), and enables `tor`. | Everything survives a reboot. Nothing listens beyond loopback. |
| Tailscale | Runs `tailscale set --ssh=false` and `tailscale serve --bg --https=443 http://127.0.0.1:6080`. | Publishes the live view to the tailnet only, over HTTPS. |
| Smoke test | Starts and stops a headless Brave as `scout`. | Proves Brave launches before Scout depends on it. |

Expected output ends with a smoke-test result like `{"ok": true, … "ready": true …}` and the live-view address.

### 5.5 The helper (`scout-browser`)

The helper runs on the VM as `scout`. Scout calls it over SSH:

```text
scout-browser start <profile> --port N [--browser brave] [--window W,H] [--locale L] [--timezone TZ]
                              [--proxy URL] [--extension DIR]... [--flag=--switch]... [--headless]
scout-browser stop <profile>
scout-browser status [profile]
```

| Property | Detail |
| --- | --- |
| Profile names | Must match `^[a-z0-9][a-z0-9_-]{0,39}$`. A profile's data lives in `/home/scout/profiles/<name>`. |
| Idempotent `start` | If the profile is already running on that port, the helper reports `already_running` and leaves it alone. |
| Launch | `--remote-debugging-address=127.0.0.1`, `DISPLAY=:99`, in its own process group, so `stop` cleans up every child process. |
| Output | One JSON object per call: `{"ok": true, …}` or `{"ok": false, "error": …}`. |

### 5.6 How Scout attaches

When code calls `open_browser("research")`:

1. Scout runs the helper's `start` over SSH with the profile's settings.
2. Scout opens a tunnel: `ssh -N -L 127.0.0.1:<free local port>:127.0.0.1:<profile port>`.
3. Playwright connects with `connect_over_cdp` and works in the profile's default context, so saved sign-ins apply.
4. When the task ends, Scout closes **only the tabs it opened**, disconnects and closes the tunnel. The browser keeps running (`stop_on_exit: false`), so the next task starts warm.

---

## 6. Browser profiles

Browser settings live in `config/browsers.yaml`, with machine values taken from `.env`:

```yaml
hosts:
  browser-vm:
    kind: ssh
    address: ${SCOUT_VM_HOST:-browser-vm}
    user: ${SCOUT_VM_USER:-scout}
    identity_file: ${SCOUT_SSH_KEY:-~/.ssh/scout_ed25519}
    helper: /usr/local/bin/scout-browser
    live_view: https://${SCOUT_VM_HOST:-browser-vm}.${SCOUT_TAILNET:-example.ts.net}/vnc.html?autoconnect=1&resize=remote
  local: {kind: local}

defaults: {browser: brave, headless: false, window: [1280, 800], locale: en-US,
           timezone: "${SCOUT_TIMEZONE:-UTC}", proxy: null, extensions: [], flags: [], stop_on_exit: false}

profiles:
  research:      {host: browser-vm, port: 9301}
  research-tor:  {host: browser-vm, port: 9302, proxy: socks5://127.0.0.1:9050, timezone: UTC, flags: [...]}
  local-test:    {host: local, browser: chromium, headless: true, port: 0, stop_on_exit: true}

search: {profile: research, tor_profile: research-tor, tor: "${SCOUT_TOR:-ask}",
         engines: [duckduckgo, brave, google], results_wanted: 10, max_pages_per_engine: 3, pause_seconds: [2, 5]}
```

**Rules the loader enforces.** A mistake fails at load time with a `ConfigError` that names it.

- Every `ssh` host has a non-empty address.
- Every profile points at a known host.
- The browser is `brave`, `chrome` or `chromium`.
- No two profiles on the same host share a port.
- The window size is `[W, H]`.
- Every search engine name is known.
- `search.tor` is `ask`, `on` or `off`, and `on` requires a `tor_profile`.

**Adding a profile:** use one profile per account or site group, each with its own port. A running profile keeps the settings it started with. Stop it to apply changes (§12).

**Signing in to a site for a profile:**

1. Run `python scripts/browser_check.py <profile>`. The profile keeps running afterwards.
2. Open `https://<browser-vm>.<tailnet>/vnc.html`, find that profile's Brave window, and sign in as usual.
3. The session is saved in `/home/scout/profiles/<profile>` on the VM.

---

## 7. Tor on/off

### 7.1 The switch

| `SCOUT_TOR` | Behaviour |
| --- | --- |
| `ask` (default) | **Scout asks the user before searching:** "Search through Tor? (on/off)". Without an answer, no search runs. |
| `on` | Every search goes through Tor (`search.tor_profile`). |
| `off` | Every search goes direct (`search.profile`). |

**A per-call choice always beats the default.**

In code:

```python
from scout.tools.search import web_search, TorChoiceRequired

await web_search("query", tor=True)          # through Tor
await web_search("query", tor=False)         # direct
await web_search("query", ask=ask_user)      # SCOUT_TOR=ask: calls ask_user() -> bool before searching
await web_search("query")                    # SCOUT_TOR=ask and no ask: raises TorChoiceRequired
await web_search("query", profile="jobs")    # an explicit profile overrides the Tor choice
```

`SearchOutcome.tor` records whether the search actually went through Tor.

**Contract for agents:** when `SCOUT_TOR=ask`, ask the user **once at the start of a task** and pass the answer (`tor=True/False`) to every search in that task. Never guess, and never turn Tor off without asking.

On the command line:

```bash
python scripts/browser_check.py --search "q"           # asks "Search through Tor? [y/N]" when SCOUT_TOR=ask
python scripts/browser_check.py --tor --search "q"     # Tor on
python scripts/browser_check.py --no-tor --search "q"  # Tor off
```

Without a terminal and without `--tor`/`--no-tor`, the check exits with code 2 rather than guessing.

### 7.2 What the Tor profile does

The `research-tor` profile sends **all** of its traffic through the VM's `tor` service.

| Protection | How it is achieved |
| --- | --- |
| Traffic exits through Tor | `--proxy-server=socks5://127.0.0.1:9050`. Tor listens on the VM's loopback only. |
| No DNS leaks | `--host-resolver-rules="MAP * ~NOTFOUND , EXCLUDE 127.0.0.1"`: Brave cannot resolve names locally, so every hostname is resolved by the Tor exit. |
| No WebRTC leaks | `--force-webrtc-ip-handling-policy=disable_non_proxied_udp` (and the older spelling of the same switch). |
| No UDP around the proxy | `--disable-quic`. |
| Less distinctive | Time zone `UTC`, as Tor Browser does. |

For a proxied profile, `browser_check.py` adds an **exit address** step. It asks `check.torproject.org` and fails unless the address is a confirmed Tor exit.

**What it hides and what it doesn't:**

- It hides the VM's public IP address from the sites visited and from the search engines.
- It is **not** Tor Browser. Brave's fingerprint isn't uniform across users, and a site can still recognise repeat visits from the same profile.
- For stronger separation, wipe the profile between tasks (§12).
- Never sign in to personal accounts in the Tor profile.

**Why not Brave's "Private window with Tor"?** It is a menu feature that automation (CDP) cannot open or drive reliably. The system `tor` service uses the same Tor network and gives Scout full control.

---

## 8. Verification, step by step

Run these in order. Each step tests one layer, so the first failure points at the broken part.

| # | Command (on the workstation) | Proves | Expected |
| --- | --- | --- | --- |
| 1 | `python scripts/check_connection.py` | Workstation → Strata: network, model list, chat, tool calling, image input | Five `OK` lines, `All checks passed.` |
| 2 | `pytest` | All code paths offline: config and env expansion, helper, SSH and tunnel path (with a fake ssh), Tor switch, search fallback, parsers on real captured pages, fixture scrubbing | All tests pass (46 at the time of writing) |
| 3 | `ssh -i ~/.ssh/scout_ed25519 scout@<browser-vm> scout-browser status` | Scout's key, the `scout` user, the helper | `{"ok": true, "display": ":99", …}` |
| 4 | `python scripts/browser_check.py research` | Helper start, tunnel, CDP attach, a real page load | `OK start + attach`, `OK page load` |
| 5 | `python scripts/browser_check.py --no-tor --search "…"` | Live search and the engine fallback | `OK search` with about 10 results |
| 6 | `python scripts/browser_check.py research --capture` | Each engine's current markup, saved **scrubbed** to `tests/fixtures/search/*_live.html` | `OK capture …` for all three engines |
| 7 | `python scripts/browser_check.py --tor --search "…"` | Tor exit and search over Tor | `OK exit address … (Tor exit: yes)`, `OK search` |
| 8 | `python -m evals.run --repeats 3` | The model can do every Scout role | Gate table: all roles `PASS` |
| — | Open `https://<browser-vm>.<tailnet>/vnc.html` | The live view | The VM's virtual screen, with any running Brave windows |

After step 6, run `pytest` again:

- The `test_live_capture_*` tests parse the fresh pages, so an engine markup change fails a test instead of making searches quietly return nothing.
- `test_fixtures_are_scrubbed` fails if a page still contains location or session data. Set `SCOUT_SCRUB_PLACES` and `SCOUT_SCRUB_REGIONS` before capturing.

---

## 9. Reference results

### Model: eval gate

Model `qwen3.8-flash-next-unsloth-ud-q4_k_xl`, `direct` routing, 3 repeats, 932 s wall time.

| Suite | Cases | Pass rate | Median latency | Threshold | Verdict |
| --- | --- | --- | --- | --- | --- |
| tool_calling | 27 | 100% | 9.1 s | ≥ 90% | PASS |
| extraction | 9 | 100% (score 1.00) | 9.2 s | score ≥ 0.90 | PASS |
| agent_loop | 6 | 100% (score 1.00, 7 steps avg.) | 45.2 s | ≥ 80%, score ≥ 0.90 | PASS |
| vision | 21 | 100% | 9.7 s | ≥ 70% | PASS |
| speed | 9 | 100% | 11.1 s | first token ≤ 8 s, decode ≥ 15 tok/s | PASS |

**Gate: planner, browser, vision and extractor all PASS. Every role stays on Strata.**

Speed in detail. These are first runs; repeats of an identical prompt are served from Strata's prompt cache.

| Prompt | Time to first token | Decode |
| --- | --- | --- |
| Short | 0.2–1.7 s | ~36 tok/s |
| 8k tokens (new) | 9.8 s | ~26 tok/s |
| 24k tokens (new) | 21.6 s | ~36 tok/s |
| 8k / 24k (cached) | ~0.3 s | ~26 / ~35 tok/s |

An earlier run, before a vision update on Strata, scored 86% on vision and took 2659 s.

### Browsers

| Check | Result |
| --- | --- |
| Start + attach through the SSH tunnel | 0.9–1.8 s (Brave, Chrome/157) |
| Page load | 0.1 s warm; ~25 s on a profile's first run |
| Live view | HTTP 200, tailnet only |
| DuckDuckGo | 10 results per page, not blocked |
| Brave Search | 18 results per page, not blocked |
| Google | 6 results over 2 pages, cookie consent declined automatically, redirect links resolved |
| Full chain (asked for 25) | 25 results in 8 s (DuckDuckGo 14 + Brave 11); Google not needed |
| Tor: exit address | A confirmed Tor exit, different from the VM's own address |
| Tor: WebRTC | No address candidates, no leak |
| Tor: engines | DuckDuckGo 10 results, Brave Search 19, Google blocked by a CAPTCHA and skipped |
| Tor: ask mode | The prompt appeared, the answer "y" selected the Tor profile, and the search ran through a Tor exit |

---

## 10. Security model

| Surface | Exposure | Control |
| --- | --- | --- |
| Strata API | Tailnet only, over HTTPS | API key in `.env` (never committed) |
| Brave DevTools ports (9301, 9302, …) | VM loopback only | Reachable only through Scout's SSH tunnel. Whoever holds this port fully controls that browser. |
| VNC `:5900` / noVNC `:6080` | VM loopback only | Published by `tailscale serve` to the tailnet, over HTTPS. **VNC has no password**, so anyone on the tailnet can see and use the browsers. Keep the tailnet to your own devices, or restrict the VM with Tailscale ACLs. |
| SSH to the VM | Tailnet (and the libvirt NAT network) | Key-only for `scout`. Scout's key is used for nothing else. |
| Tor SOCKS `:9050` | VM loopback only | Only the VM's own processes can use it. |
| Committed test fixtures | Public, with the repository | Scrubbed: no scripts, session tokens, coordinates, IP addresses, or configured place names and region codes. A test enforces this. |

**Choices made on purpose:**

- The `scout` user has no sudo and no password.
- Root work on the VM is done by a person over `ssh -t`, typing the password themselves.
- Scout never enters credentials and never solves CAPTCHAs.
- Search engines are told to **reject** non-essential cookies (Google's "Reject all").

---

## 11. Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| `check_connection`: a DNS error or "reachable" fails | Tailscale is logged out on the workstation | Sign in; check `tailscale status` |
| `check_connection`: HTTP 401 | Wrong or missing `STRATA_API_KEY` | Copy the key Strata was started with into `.env` |
| `check_connection`: "empty answer (finish_reason=length)" | Reasoning used up the token budget | Raise `max_tokens`. Never go below 512 for this model |
| Every call fails with "model not found" | The Strata variant changed but `STRATA_MODEL` is still pinned to the old id | `curl -H "Authorization: Bearer $STRATA_API_KEY" $STRATA_BASE_URL/models`, then update `.env` |
| `ssh …` hangs, printing "Tailscale SSH requires an additional check" | Tailscale SSH is on for that machine | On the VM, run `sudo tailscale set --ssh=false` (Scout uses OpenSSH). On other hosts, open the link and approve. |
| `Permission denied (publickey)` as `scout` | `authorized_keys` was not installed | Rerun §5.4 with `deploy/browser-vm/authorized_keys` in place |
| `Connection refused` on port 22 | No SSH server (Desktop install) | `sudo apt install -y openssh-server` |
| `Host key verification failed` | First connection, or the VM was rebuilt | `ssh-keygen -R <browser-vm>`, then connect once interactively |
| `ConfigError: … needs an address` | `SCOUT_VM_HOST` is empty | Set it in `.env` |
| `TorChoiceRequired` | `SCOUT_TOR=ask` and nobody answered | Pass `tor=True/False` or an `ask` callback, or use `--tor` / `--no-tor` |
| `browser_check`: start fails with "profile name" | The name breaks the helper's rule | Lowercase letters, digits, `-` and `_`; at most 40 characters |
| `browser_check`: the tunnel times out | Port clash, or Brave crashed on start | `ssh … scout@<browser-vm> 'scout-browser status; tail ~/logs/<profile>.log'` |
| Search: one engine returns 0 results and is not blocked | The engine changed its markup | Run step 6 (`--capture`), inspect the saved page, update that engine's selectors in `scout/tools/search.py`, then run `pytest` |
| Search: `needs_human` | Every engine showed a bot check | Open the live view, solve the check in that profile's window, and retry |
| Google: 0 results | Its cookie consent wasn't dismissed | `Google.prepare()` clicks "Reject all"; check the capture for a new button label |
| Tor check: "Tor exit: no" | `tor` is down or still bootstrapping | `systemctl status tor` on the VM; wait about 30 s after it starts |
| `test_fixtures_are_scrubbed` fails | A capture still contains location or session data | Set `SCOUT_SCRUB_PLACES` / `SCOUT_SCRUB_REGIONS`, then recapture or rerun `scrub_html` on the file |
| The live view does not load | `tailscale serve` is missing, or HTTPS certificates are off | Turn on HTTPS certificates in the admin console, then rerun `setup.sh` |
| Strata slows down when the VM is busy | Memory pressure on `<strata-host>` | `free -g`; keep VMs within the available RAM |

---

## 12. Day-to-day operations

```bash
K="-i ~/.ssh/scout_ed25519"

# What is running on the VM
ssh $K scout@<browser-vm> scout-browser status

# Stop a profile (to apply new settings, or free its memory)
ssh $K scout@<browser-vm> scout-browser stop research

# Wipe the Tor profile's data (fresh fingerprint state between discreet tasks)
ssh $K scout@<browser-vm> 'scout-browser stop research-tor && rm -rf ~/profiles/research-tor'

# Start a profile and leave it running (for example, to sign in through the live view)
python scripts/browser_check.py research

# After changing deploy/browser-vm/*: copy and rerun setup (§5.4); it is idempotent

# VM lifecycle on the host (or use Cockpit → Virtual machines, or virt-manager)
virsh -c qemu:///system list --all
virsh -c qemu:///system shutdown <vm-name>
virsh -c qemu:///system start <vm-name>
```

**When to re-run checks:**

| When | Run |
| --- | --- |
| Strata's model or settings changed | Step 1, then step 8 |
| Searches look wrong | Step 6, then `pytest` |
| Before relying on Tor | Step 7 |

---

## 13. Known limits and open items

- **Long prompts start slowly.** A new 24k-token prompt takes about 22 s to its first token. Agents should keep their context lean and send repeated prefixes unchanged.
- **Step timeouts.** One earlier vision request stalled for 218 s. The Phase 1 agents should enforce a timeout on each step, not rely only on the 300 s request timeout.
- **Engine markup drifts.** Brave Search and Google both changed their result layout during development. Expect to recapture and adjust selectors from time to time; the live-capture tests catch it when it happens.
- **Google over Tor** is effectively always blocked. Rely on DuckDuckGo and Brave Search for Tor searches.
- **Live view has no password.** It relies on the tailnet being yours (§10).
- **Not built yet (Phase 1):** the task record, academic APIs, library hand-off, sub-agents, the planner loop (which must ask the Tor question per §7.1), the CLI and the Phase 1 eval.

---

## Appendix A: headless VM with cloud-init

Build a server VM entirely from the command line, with no installer screens. Run this on the VM host as a member of the `libvirt` group; no sudo is needed.

```bash
V="virsh -c qemu:///system"
# A storage pool (once)
$V pool-define-as default dir --target /var/lib/libvirt/images; $V pool-build default
$V pool-start default; $V pool-autostart default

mkdir -p ~/scout-vm && cd ~/scout-vm
curl -fsSLo noble.img https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img

cat > meta-data <<EOF
instance-id: browser-vm-1
local-hostname: browser-vm
EOF
cat > user-data <<EOF
#cloud-config
hostname: browser-vm
users:
  - name: vm-admin
    groups: [sudo]
    shell: /bin/bash
    sudo: 'ALL=(ALL) NOPASSWD:ALL'
    ssh_authorized_keys: ['<contents of ~/.ssh/scout_ed25519.pub>']
ssh_pwauth: false
package_update: true
packages: [qemu-guest-agent, curl]
runcmd:
  - systemctl enable --now qemu-guest-agent
  - curl -fsSL https://tailscale.com/install.sh | sh
EOF
genisoimage -quiet -output seed.iso -volid cidata -joliet -rock user-data meta-data

cp noble.img disk.qcow2 && qemu-img resize -q disk.qcow2 20G
$V vol-create-as default browser-vm.qcow2 1G --format qcow2 && $V vol-upload --pool default browser-vm.qcow2 disk.qcow2
$V vol-create-as default browser-vm-seed.iso 1M --format raw && $V vol-upload --pool default browser-vm-seed.iso seed.iso

virt-install --connect qemu:///system --name browser-vm --memory 4096 --vcpus 2 \
  --disk vol=default/browser-vm.qcow2,bus=virtio --disk vol=default/browser-vm-seed.iso,device=cdrom \
  --os-variant ubuntu24.04 --network network=default,model=virtio --graphics none --noautoconsole --import \
  --channel unix,target.type=virtio,target.name=org.qemu.guest_agent.0 --autostart

$V domifaddr browser-vm        # its NAT address (192.168.122.x), reachable from the host
```

Then continue from §5.2:

1. Join the tailnet: `sudo tailscale up --hostname=browser-vm`. Until the VM has joined, reach it through the host with `ssh -J <you>@<strata-host> vm-admin@192.168.122.x`.
2. Run `setup.sh` as `vm-admin`. It has passwordless sudo, so no password prompt.

The console is text-only, and the VM accepts SSH keys only until you set a password (`sudo passwd vm-admin`).

**Trade-offs compared with a desktop VM:** it uses less RAM (4 GB versus 8 GB) and can be rebuilt with one command. On the other hand, it has no desktop for manual inspection; you see the browsers only through the live view.

---

## Appendix B: optional LiteLLM role routing

Scout runs without LiteLLM (`SCOUT_ROUTER=direct`). Turn routing on only when one role needs a different engine:

```bash
uv pip install -e ".[router]"
python scripts/render_litellm_config.py          # fills in Strata's model id
litellm --config config/litellm.generated.yaml --port 4000
# then set SCOUT_ROUTER=litellm in .env and rerun steps 1 and 8
```

Moving a role elsewhere means changing that role's entry in `config/litellm.template.yaml`, and nothing else. `deploy/strata-host/llama-swap.yaml` is a template for running a second engine on the model host. With the current gate result (every role passes on Strata), neither is needed.

# Phase 2 — the browser tier

Phase 2 lets Scout use a real browser for what the APIs and `fetch_page` cannot do: forms, sign-in flows,
files behind a sign-in, pages built by JavaScript. The browser runs on the VM (the `research` profile, or
`research-tor` when the task's Tor choice is on); Scout drives it over CDP through an SSH tunnel. For setting
up the VM, see [SETUP §5–7](SETUP.md#5-the-browser-vm). The plan and state of each package is in
[ROADMAP §3](ROADMAP.md#phase-2--browser).

## 1. What the agent sees and does

A page is shown to the model as a list of its visible interactive elements, each with a stable id
(`e7 button "Submit order" form=f1`), plus the title, URL, main text, forms, and flags for a cookie banner, an
open dialog or a sign-in (`scout/browser/page.py`). The model acts by id:

| Tool | What it does | Gate or limit |
| --- | --- | --- |
| `browser_open`, `browser_read` | Open a URL / re-read the page | Navigation retried on timeouts and network errors |
| `browser_click` | Click a link, tab or button | Refuses anything that looks like it submits or commits: use `click_submit` |
| `browser_type`, `browser_select` | Fill an input, choose an option | `browser_type` refuses password fields, in every mode |
| `browser_scroll`, `browser_back` | Scroll; go back, or close a tab or popup and return to its opener | — |
| `click_submit` | Submit, send, pay, order, apply, delete… | **Approval first** (`SCOUT_APPROVALS`: ask / auto / deny), logged as its own `request_approval` step |
| `browser_download` | Click a link and keep the file the browser downloads | Verified like `download_file`; unverified files are deleted |
| `browser_screenshot` | Save the page as a PNG in the task folder | — |
| `run_playbook` | Run a known site flow in one call (§3) | The same tools and gates as above |
| `hand_to_human` | Sign-ins, CAPTCHAs, two-factor prompts | The report carries the live-view link, the browser URL and the `resume` command |

Links that open a new tab or popup are followed; JavaScript dialogs are answered safely (alerts acknowledged,
confirms declined except the one that follows an approved `click_submit`).

## 2. Downloads from the browser

`browser_download` never fetches the URL itself (that would leave from this machine, not the VM, and not
through Tor): it clicks, and the browser downloads. Attached over CDP, Playwright would tell a remote browser to
save into a folder of *this* machine, which does not exist on the VM, and the download is canceled. So for a
remote browser Scout sets the download folder on the VM (`/tmp/scout-downloads/<profile>`, CDP
`Browser.setDownloadBehavior`), waits for the download to complete, copies the file back over SSH with Scout's key
and deletes it on the VM. The bytes then pass the same checks as any download (`web.verify_bytes`).

## 3. Playbooks (B5)

A playbook is a known flow for one site, as YAML in `playbooks/` (`SCOUT_PLAYBOOKS_DIR`):

```yaml
name: httpbin-pizza
match: {urls: ["httpbin.org/forms/post"]}         # or words: [all, of, these]
params:
  custname: {description: "customer name", required: true}
  toppings: {type: list}
steps:
  - open: https://httpbin.org/forms/post
  - type: {field: "Customer name", text: "{custname}"}
  - check: {field: "{item}"}
    for_each: toppings
  - submit: {element: "Submit order", summary: "Submit the pizza order for {custname}"}
  - expect: {text: "custname"}
  - fuzzy: "Report what the site returned, then finish."
```

- **Matching:** when a playbook matches the objective, Scout names it to the model at the start; the model calls
  `run_playbook` with the params taken from the objective.
- **Steps:** `open`, `type`, `select`, `check` (tick if not ticked), `click`, `submit` (through `click_submit` and
  its approval), `expect` (text on the page), `wait`, `fuzzy` (an instruction for the model). `optional: true`
  skips a step that fails or has no value.
- **Elements by label**, not selectors: exact, then prefix, then contained either way, among elements of the
  right kind (a `type` step never picks a password field or a button).
- **Resilience:** a failed step stops the run and returns the page as it is plus the steps left; the model
  continues by hand. One playbook run is one step of the task's budget; every browser action it took is in the
  step's result and in `state.playbooks` of the task record.
- **Traces:** when a task finishes `done`, its browser actions are written to `trace.yaml` in the task folder,
  in this same format (values literal). Phase 3 (D4) turns repeated traces into reviewed playbooks.

## 4. Cookie and consent banners (B6)

Scout declines banners itself (`scout/browser/consent.py`), so the model rarely has to:

- **When:** on every page read where the page model sees a banner, or a known consent platform's banner or iframe
  is present (once per URL). After `browser_open` Scout waits up to 3 s for a banner injected after the page
  loaded (`SCOUT_CONSENT_WAIT`), and keeps trying for that long while a consent iframe's buttons load.
- **How:** 13 consent platforms by their own markup (OneTrust, Cookiebot, Didomi, Quantcast, Usercentrics in its
  shadow root, TrustArc, Sourcepoint in its iframe, Google Funding Choices, CookieYes, Complianz, Osano, iubenda,
  Klaro), then a label search inside banner-like containers. A click that only opened a second layer ("Manage
  options") is followed by one more try.
- **What it clicks** is decided by one rule, `is_reject_label`: explicit reject wording ("Reject all", "Deny",
  "Alle ablehnen", "Απόρριψη όλων", "I do not agree") or necessary-only wording ("Accept only necessary", "Nur
  notwendige Cookies", "Continue without accepting"), in ten languages. **Never** an accept label (even "Accept
  non-essential cookies"), and never consent-or-pay or account flows ("Reject all and subscribe", "SUBSCRIBE AND
  DECLINE", "Log in to refuse"). A platform's own reject selector must also carry such a label.
- **No safe choice** (accept-or-subscribe walls): nothing is clicked and the model is told not to accept for the
  user, but to get the content another way or hand over.

Live check (fresh local browser, 2026-11-18): Cookiebot, OneTrust and BBC's Sourcepoint layer declined; the
Guardian, Spiegel, Le Monde and El País (consent-or-pay) left alone. On the VM's persistent profile most sites
showed no banner (choices already stored).

## 5. Tests

`tests/test_browser_tools.py`, `test_browser_more.py`, `test_playbooks.py`, `test_consent.py` and
`test_browser_resilience.py` (B7: slow, flaky and failing pages, late and re-rendered content, overlays, infinite
scroll, popups, pushState, late banners) run on a local headless Chromium with routed fixture pages; no network.

## 6. Results

`python -m evals.phase_tasks --phase 2 --repeats 3` at commit dce4503 (the 15 phase-1 tasks plus f01–f03, each
repeat with an empty library), 2026-11-18: **96 % (52/54)**, threshold 75 %: **G2 passed**. Raw results in
`results/20261118-072645-tasks/` (not committed).

| Task | Passes | Task | Passes | Task | Passes |
| --- | --- | --- | --- | --- | --- |
| r01–r04, r06–r13 | 3/3 each | d01, d02 | 3/3 each | f01 form with approval | 3/3 (3 steps, from its playbook) |
| r05 policy page | 1/3 | f02 sign-in hand-off | 3/3 (`needs_human`) | f03 multi-field form | 3/3 (3 steps, from its playbook) |

- **f01–f03** ran in 3–4 steps and about 30–45 s each: the playbooks did the forms in one call, and the approval
  gate asked once per submit.
- **r05** failed only its `max_steps 12` check (15 and 14 steps); the report and the saved page were right each
  time. The strategy's targets are no longer on the hydrogen page, so the agent goes on to the 2020 strategy PDF
  for them. This is the same flakiness noted for G1 ([PHASE1 §6](PHASE1.md#6-results)). The check is the eval's, not Scout's
  budget (40 steps in the eval); the agent was not tuned to stop earlier.

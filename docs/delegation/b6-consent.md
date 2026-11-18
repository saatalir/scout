---
files: ["scout/browser/consent.py", "tests/test_consent.py", "tests/fixtures/consent/*.html"]
context: ["scout/browser/page.py", "tests/test_page_model.py", "scout/browser/session.py"]
tests: ["tests/test_consent.py"]
---
# B6: cookie-banner and consent library (reject non-essential, never accept)

Create a NEW module `scout/browser/consent.py`. Scout uses it right after opening a page: if a cookie or
consent banner is showing, it clicks the button that **rejects non-essential cookies** (or "necessary only").
It must **never** click a button that accepts, agrees or allows all. If it cannot find a safe button it
does nothing and returns `None` (the agent then decides).

Browser code is async (Playwright async API). Follow the style of `scout/browser/page.py`.

## Data

```python
@dataclass(frozen=True)
class Platform:
    name: str                       # e.g. "onetrust"
    detect: tuple[str, ...]         # CSS selectors of the banner container (any visible one = this platform)
    reject: tuple[str, ...]         # CSS selectors of reject / necessary-only buttons, most direct first
    settings: tuple[str, ...] = ()  # CSS selectors of a "settings"/"manage options" button that reveals reject
    frame: str | None = None        # substring of the iframe URL when the banner lives in an iframe

PLATFORMS: tuple[Platform, ...]
```

Cover at least these platforms, with their real, commonly used ids/classes (use what you know of each
platform's markup; the fixtures you write must use the same markup):

| name | notes |
| --- | --- |
| onetrust | `#onetrust-banner-sdk`, reject `#onetrust-reject-all-handler`; settings `#onetrust-pc-btn-handler`, then `.ot-pc-refuse-all-handler` |
| cookiebot | `#CybotCookiebotDialog`, reject `#CybotCookiebotDialogBodyButtonDecline` |
| didomi | `#didomi-host` / `#didomi-popup`, reject `#didomi-notice-disagree-button` |
| quantcast | `.qc-cmp2-container`, reject: the summary's "Reject"/"Disagree" button (`.qc-cmp2-summary-buttons button[mode="secondary"]`) |
| usercentrics | host `#usercentrics-root` with an **open shadow root**; reject `[data-testid="uc-deny-all-button"]` (Playwright CSS pierces open shadow roots) |
| trustarc | `#truste-consent-track`, reject `#truste-consent-required` |
| sourcepoint | iframe whose URL contains `privacy-mgmt` or `sp_message_iframe`; inside it reject `button[title="Reject All"]`, `button.sp_choice_type_13` |
| google_fc | `.fc-consent-root`, reject `.fc-cta-do-not-consent`; settings `.fc-cta-manage-options` |
| cookieyes | `.cky-consent-container`, reject `.cky-btn-reject` |
| complianz | `#cmplz-cookiebanner-container` / `.cmplz-cookiebanner`, reject `.cmplz-deny` |
| osano | `.osano-cm-dialog`, reject `.osano-cm-denyAll` |
| iubenda | `#iubenda-cs-banner`, reject `.iubenda-cs-reject-btn` |
| klaro | `.klaro .cookie-notice`, reject `.cm-btn-decline` |

## Text fallback

`REJECT_TEXT`: a compiled, case-insensitive regex for reject / necessary-only labels in at least English,
German, French, Spanish, Italian, Dutch, Portuguese, Polish, Swedish and **Greek**, e.g. "reject all",
"reject", "decline", "deny", "refuse", "only necessary", "necessary only", "essential only",
"continue without accepting", "alle ablehnen", "ablehnen", "nur notwendige", "tout refuser", "refuser",
"continuer sans accepter", "rechazar todo", "rechazar", "rifiuta tutto", "rifiuta", "alles weigeren",
"weigeren", "rejeitar", "odrzuć", "avvisa alla", "απόρριψη όλων", "απόρριψη", "μόνο τα απαραίτητα".

`ACCEPT_TEXT`: a regex for accept labels ("accept", "agree", "allow all", "allow", "got it", "ok", "akzeptieren",
"zustimmen", "accepter", "aceptar", "accetta", "accepteren", "aceitar", "akceptuj", "godkänn", "αποδοχή",
"συμφωνώ"). A candidate whose label matches `ACCEPT_TEXT` and NOT `REJECT_TEXT` is never clicked.
(Labels like "Reject all" do not match accept; take care that "Accept only necessary" counts as reject:
it matches REJECT_TEXT via "only necessary"/"necessary". A label matching both is allowed only if it contains
a necessary/essential/reject word.)

## `async def reject_consent(page, *, timeout_ms: int = 3000) -> dict | None`

1. For each frame in `page.frames` (main frame first; for a platform with `frame`, only frames whose URL
   contains it): for each platform whose `detect` selector matches a **visible** element in that frame
   (for `frame` platforms: the frame's existence is the detection), try its `reject` selectors in order: the
   first visible one is clicked (`timeout=timeout_ms`). If none is visible and a `settings` selector is
   visible, click it, wait up to `timeout_ms` for a reject selector to become visible, and click that.
2. If no platform matched: **generic fallback**. Find visible `button, a, [role=button], input[type=button],
   input[type=submit]` elements whose label (inner text, `aria-label` or `value`) matches `REJECT_TEXT` (and is
   not an accept label as above), and that are inside a container that looks like a consent banner: an ancestor
   (up to 8 levels) that is `position: fixed|sticky`, or has `role=dialog`/`aria-modal=true`, or whose id/class
   contains `cookie`, `consent`, `gdpr` or `privacy`. Click the first. Do this with one `evaluate` that marks the
   chosen element with `data-scout-consent="1"`, then click it with a Playwright locator.
3. Never click anything matching only `ACCEPT_TEXT`. Never click links whose `href` leaves the page (a privacy
   policy link). Never submit a form other than by clicking the chosen button.
4. After a click, wait up to `timeout_ms` for the matched banner container (or, generic, the chosen button) to
   become hidden or detached.
5. Return `{"platform": name or "generic", "clicked": label (≤ 80 chars), "via_settings": bool,
   "closed": bool}`; `None` if nothing safe was found or nothing was showing. Catch Playwright errors
   (`playwright.async_api.Error`, `TimeoutError`) per candidate and move on; never raise for a page problem.

Also `def platform_names() -> list[str]`.

## Fixtures: `tests/fixtures/consent/<name>.html`

One small, self-contained HTML page per platform above (no external scripts or CSS; inline only), with a
`<main>` holding some text and a visible banner using that platform's markup. Clicking reject must hide the
banner (inline `onclick` that sets `display:none` on the container and sets
`document.body.dataset.consent = "rejected"`); clicking accept sets `document.body.dataset.consent = "accepted"`.
Plus:

- `usercentrics.html`: the banner is built in an open shadow root by an inline script.
- `sourcepoint.html`: the main page has `<iframe src="https://consent.test/sp_message_iframe/index.html">`;
  write that iframe's page as `sourcepoint_frame.html`. Its reject button calls
  `parent.document.body.dataset.consent = "rejected"` and hides the iframe via `parent.document`.
- `onetrust_settings.html`: OneTrust banner with **no** reject button on the first layer: only "Accept All" and
  "Cookie Settings"; the settings panel (initially `display:none`) has `.ot-pc-refuse-all-handler`.
- `generic_de.html` (German, "Alle akzeptieren" / "Nur notwendige Cookies", fixed bottom bar, no platform ids),
  `generic_el.html` (Greek, "Αποδοχή όλων" / "Απόρριψη όλων"), `generic_en.html` ("Accept all" /
  "Reject non-essential" in a `role=dialog`).
- `accept_only.html`: a fixed banner with only "Accept" and "Learn more" (a link to `/privacy`): nothing may be
  clicked.
- `no_banner.html`: a page with an unrelated "Decline invitation" button **not** in a banner (no fixed position,
  no dialog role, no cookie/consent class): nothing may be clicked.
- `late_banner.html`: the OneTrust banner is added by `setTimeout(…, 800)`.

## Tests: `tests/test_consent.py`

Local headless browser exactly like `tests/test_page_model.py`: `async with open_browser("local-test") as b`,
`page = await b.new_page()`, run with `asyncio.run`. Serve the fixtures with
`await b.context.route("https://consent.test/**", handler)` mapping `/<name>.html` to
`tests/fixtures/consent/<name>.html` (and `/sp_message_iframe/index.html` to `sourcepoint_frame.html`), then
`page.goto("https://consent.test/<name>.html")`. No network.

Cover, one test per case (parametrize the platform pages):
- every platform page: result's `platform` is that platform, `closed` is true, `body.dataset.consent ==
  "rejected"`;
- `onetrust_settings`: `via_settings` true and rejected;
- the three generic pages: `platform == "generic"` and rejected;
- `accept_only` and `no_banner`: result `None` and `body.dataset.consent` unset (nothing clicked);
- `late_banner`: `None` immediately, then after waiting 1.2 s a second call rejects it;
- regex unit tests: REJECT_TEXT matches the Greek and German labels above; ACCEPT_TEXT matches "Accept all"
  and "Αποδοχή όλων"; "Accept only necessary" is treated as a reject label (expose the decision as
  `def is_reject_label(label: str) -> bool`).

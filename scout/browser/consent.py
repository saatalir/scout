"""B6 — cookie-banner and consent-library handling (written by Strata through the delegation harness; the
label rules, the generic search and banner_present were revised in Claude's review).

Scout calls :func:`reject_consent` right after opening a page.  If a known
consent platform is showing, it clicks the platform's reject / necessary-only
button.  If no known platform is found, it falls back to a conservative generic
banner scan.  It never clicks a pure accept / agree / allow-all button.
"""

from __future__ import annotations

import asyncio
import re
from dataclasses import dataclass

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page
from playwright.async_api import TimeoutError as PlaywrightTimeoutError


@dataclass(frozen=True)
class Platform:
    name: str
    detect: tuple[str, ...]
    reject: tuple[str, ...]
    settings: tuple[str, ...] = ()
    frame: str | None = None        # iframe URL substrings or iframe id prefixes, "|"-separated


PLATFORMS: tuple[Platform, ...] = (
    Platform(
        name="onetrust",
        detect=("#onetrust-banner-sdk",),
        reject=("#onetrust-reject-all-handler", ".ot-pc-refuse-all-handler"),
        settings=("#onetrust-pc-btn-handler",),
    ),
    Platform(
        name="cookiebot",
        detect=("#CybotCookiebotDialog",),
        reject=("#CybotCookiebotDialogBodyButtonDecline",),
    ),
    Platform(
        name="didomi",
        detect=("#didomi-host", "#didomi-popup"),
        reject=("#didomi-notice-disagree-button",),
    ),
    Platform(
        name="quantcast",
        detect=(".qc-cmp2-container",),
        reject=('.qc-cmp2-summary-buttons button[mode="secondary"]',),
    ),
    Platform(
        name="usercentrics",
        detect=("#usercentrics-root",),
        reject=('[data-testid="uc-deny-all-button"]',),
    ),
    Platform(
        name="trustarc",
        detect=("#truste-consent-track",),
        reject=("#truste-consent-required",),
    ),
    Platform(
        name="sourcepoint",
        detect=(),
        reject=('button[title="Reject All"]', "button.sp_choice_type_13", 'button[aria-label="Reject all"]'),
        frame="sp_message_iframe|privacy-mgmt|message_id=",
    ),
    Platform(
        name="google_fc",
        detect=(".fc-consent-root",),
        reject=(".fc-cta-do-not-consent",),
        settings=(".fc-cta-manage-options",),
    ),
    Platform(
        name="cookieyes",
        detect=(".cky-consent-container",),
        reject=(".cky-btn-reject",),
    ),
    Platform(
        name="complianz",
        detect=("#cmplz-cookiebanner-container", ".cmplz-cookiebanner"),
        reject=(".cmplz-deny",),
    ),
    Platform(
        name="osano",
        detect=(".osano-cm-dialog",),
        reject=(".osano-cm-denyAll",),
    ),
    Platform(
        name="iubenda",
        detect=("#iubenda-cs-banner",),
        reject=(".iubenda-cs-reject-btn",),
    ),
    Platform(
        name="klaro",
        detect=(".klaro .cookie-notice",),
        reject=(".cm-btn-decline",),
    ),
)


# A label is a reject label when it says so outright (REJECT_TEXT) or asks for the necessary cookies only
# (ONLY_NECESSARY). An accept label is never one, even if it mentions "essential" ("Accept non-essential cookies"):
# the decision is made here, in one place, for platform buttons and the generic search alike (Claude's review).
REJECT_TEXT = re.compile(
    r"\b(reject|decline|deny|refuse|disagree|do not consent|don'?t consent|no,? thanks)|"
    r"\b(ablehnen|verweigern|refuser|rechazar|rifiut|weigeren|rejeitar|recusar|odrzu[cć]|avvisa|neka)|"
    r"απόρριψη|απορρίπτω|δεν συμφωνώ",
    re.IGNORECASE,
)

ONLY_NECESSARY = re.compile(
    r"\b(only|just)\b[^.]{0,30}\b(necessary|essential|required|functional)|"
    r"\b(necessary|essential|required|strictly necessary)\b[^.]{0,20}\bonly\b|"
    r"\bdisagree\b|\b(do not|don'?t|not) (consent|accept|agree|allow)|without (accepting|agreeing|consent)|"
    r"continue without|"
    r"nur (die )?(notwendigen?|erforderlichen?|essenziellen?)|ohne (zu )?(akzeptieren|zustimmen|einwilligung)|"
    r"uniquement (les )?(cookies )?(n[ée]cessaires|essentiels)|sans accepter|continuer sans|"
    r"s[oó]lo (las |los )?(cookies )?(necesari|esencial|t[eé]cnic)|continuar sin|"
    r"solo (i )?(cookie )?(necessari|essenziali|tecnici)|continua senza|"
    r"alleen (noodzakelijke|functionele)|apenas (os )?(necess[aá]rios|essenciais)|"
    r"tylko (niezb[eę]dne|wymagane)|endast n[oö]dv[aä]ndiga|"
    r"μόνο (τα )?(απαραίτητα|αναγκαία)",
    re.IGNORECASE,
)

ACCEPT_TEXT = re.compile(
    r"accept|agree|allow|enable|consent|got it|understood|(?:^|\s)ok(?:ay)?(?:[\s!.]|$)|continue|"
    r"akzeptieren|zustimmen|einverstanden|erlauben|accepter|j'accepte|autoriser|aceptar|permitir|accett|"
    r"consenti|accepteren|akkoord|toestaan|aceitar|akceptuj|zgadzam|godk[aä]nn|till[aå]t|"
    r"αποδοχή|αποδέχομαι|συμφωνώ|επιτρέπ",
    re.IGNORECASE,
)


# "Reject and subscribe", "Subscribe and decline", "Log in to refuse": consent-or-pay walls and account flows.
# Never a cookie choice Scout makes: the page is left to the model (and the person).
NOT_A_CHOICE = re.compile(
    r"subscri|abonn|suscri|assin|pay\b|paid|purchase|buy\b|kaufen|log ?in|sign ?(in|up)|register|anmelden|"
    r"registrier|connexion|iniciar sesi|accedi|συνδρομ|εγγραφ|σύνδεση",
    re.IGNORECASE,
)


def platform_names() -> list[str]:
    return [p.name for p in PLATFORMS]


def is_reject_label(label: str) -> bool:
    """Whether a button label is safe to click as a reject / necessary-only action. Never an accept label."""
    label = (label or "").strip()
    if not label or NOT_A_CHOICE.search(label):
        return False
    if ONLY_NECESSARY.search(label):
        return True                                 # "Accept only necessary", "Continue without accepting"
    if ACCEPT_TEXT.search(label):
        return False                                # "Accept non-essential cookies", "Allow all", "Accept or reject"
    return bool(REJECT_TEXT.search(label))


JS_LABEL = r"""
(el) => {
  const text = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const aria = el.getAttribute('aria-label');
  if (aria) return text(aria);
  const value = el.value;
  if (value) return text(value);
  const title = el.getAttribute('title');
  if (title) return text(title);
  const inner = text(el.innerText || el.textContent);
  return inner.slice(0, 120);
}
"""


JS_GENERIC = r"""
(requireBanner) => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
  };
  const text = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const labelFor = (el) => {
    const aria = el.getAttribute('aria-label');
    if (aria) return text(aria);
    if (el.value) return text(el.value);
    const title = el.getAttribute('title');
    if (title) return text(title);
    return text(el.innerText || el.textContent).slice(0, 120);
  };
  const inBanner = (el) => {
    let n = el.parentElement;
    for (let i = 0; i < 8 && n; i++) {
      const s = getComputedStyle(n);
      if (s.position === 'fixed' || s.position === 'sticky') return true;
      if (n.getAttribute('role') === 'dialog' || n.getAttribute('aria-modal') === 'true') return true;
      const idcls = ((n.id || '') + ' ' + (typeof n.className === 'string' ? n.className : '')).toLowerCase();
      if (/cookie|consent|gdpr|privacy/.test(idcls)) return true;
      n = n.parentElement;
    }
    return false;
  };
  document.querySelectorAll('[data-scout-consent]').forEach((n) => n.removeAttribute('data-scout-consent'));
  const out = [];
  for (const el of document.querySelectorAll('button, a, [role=button], input[type=button], input[type=submit]')) {
    if (out.length >= 40) break;
    if (!isVisible(el) || (requireBanner && !inBanner(el))) continue;
    if (el.tagName === 'A') {
      const href = el.getAttribute('href');
      if (href && !href.startsWith('#') && !href.startsWith('javascript:')) continue;   // a policy page, not a choice
    }
    if (el.tagName === 'INPUT' && el.type === 'submit' && el.form) continue;              // never submit a form
    const label = labelFor(el);
    if (!label) continue;
    el.setAttribute('data-scout-consent', String(out.length));
    out.push(label.slice(0, 80));
  }
  return out;
}
"""


async def _frame_matches(frame, platform: Platform) -> bool:
    """A consent iframe: its URL contains one of the platform's patterns, or its element's id starts with one."""
    pats = [p for p in (platform.frame or "").split("|") if p]
    if any(p in (frame.url or "") for p in pats):
        return True
    try:
        el = await frame.frame_element()
        fid = (await el.get_attribute("id")) or ""
    except (PlaywrightError, PlaywrightTimeoutError):
        return False
    return any(fid.startswith(p) for p in pats)


def _visible_locator(frame, selector: str):
    return frame.locator(f"{selector} >> visible=true")


async def _first_visible_locator(frame, selectors: tuple[str, ...]):
    for selector in selectors:
        try:
            loc = _visible_locator(frame, selector)
            if await loc.count() > 0:
                return loc.first
        except (PlaywrightError, PlaywrightTimeoutError):
            continue
    return None


async def _wait_first_visible(frame, selectors: tuple[str, ...], timeout_ms: int):
    loop = asyncio.get_running_loop()
    deadline = loop.time() + max(0, timeout_ms) / 1000.0
    while True:
        loc = await _first_visible_locator(frame, selectors)
        if loc is not None:
            return loc
        if loop.time() >= deadline:
            return None
        await asyncio.sleep(0.05)


async def _label(loc) -> str:
    try:
        return (await loc.evaluate(JS_LABEL)) or ""
    except (PlaywrightError, PlaywrightTimeoutError):
        return ""


async def _wait_closed(frame, container, clicked, timeout_ms: int) -> bool:
    """The banner (or the clicked button) is hidden or gone within the timeout ("hidden" includes detached)."""
    loc = container if container is not None else clicked
    if loc is None:
        return False
    try:
        await loc.wait_for(state="hidden", timeout=timeout_ms)
        return True
    except (PlaywrightError, PlaywrightTimeoutError):
        pass
    try:                                             # an outer host can stay while its banner inside is hidden
        return clicked is not None and loc is not clicked and await clicked.is_hidden()
    except (PlaywrightError, PlaywrightTimeoutError):
        return False


async def _try_platform(frame, platform: Platform, timeout_ms: int) -> dict | None:
    if platform.frame:
        if not await _frame_matches(frame, platform):
            return None
        container = None
    else:
        if not platform.detect:
            return None
        container = await _first_visible_locator(frame, platform.detect)
        if container is None:
            return None

    reject = await _first_visible_locator(frame, platform.reject)
    via_settings = False

    if reject is None and platform.settings:
        settings = await _first_visible_locator(frame, platform.settings)
        if settings is not None:
            try:
                await settings.click(timeout=timeout_ms)
            except (PlaywrightError, PlaywrightTimeoutError):
                return None
            via_settings = True
            reject = await _wait_first_visible(frame, platform.reject, timeout_ms)

    if reject is None and platform.frame:
        found = await _generic(frame, timeout_ms, require_banner=False)   # the whole iframe is the banner
        if found is not None:
            return {**found, "platform": platform.name}
    if reject is None:
        return None

    label = await _label(reject)
    if not is_reject_label(label):
        return None                                 # a selector can match the wrong button: the label decides

    try:
        await reject.click(timeout=timeout_ms)
    except (PlaywrightError, PlaywrightTimeoutError):
        return None

    closed = await _wait_closed(frame, container, reject, timeout_ms)
    return {
        "platform": platform.name,
        "clicked": label[:80],
        "via_settings": via_settings,
        "closed": closed,
    }


async def reject_consent(page: Page, *, timeout_ms: int = 3000) -> dict | None:
    """Reject non-essential cookies if a safe button is visible; otherwise do nothing (and return None)."""
    frames = [page.main_frame]
    for f in page.frames[1:]:
        for p in PLATFORMS:
            if p.frame and await _frame_matches(f, p):
                frames.append(f)
                break
    for frame in frames:
        for platform in PLATFORMS:
            if (platform.frame is None) != (frame is page.main_frame):
                continue
            try:
                result = await _try_platform(frame, platform, timeout_ms)
            except (PlaywrightError, PlaywrightTimeoutError):
                continue
            if result is not None:
                return result
    return await _generic(page.main_frame, timeout_ms, require_banner=True)


async def _generic(frame, timeout_ms: int, *, require_banner: bool) -> dict | None:
    """The label search: visible buttons (inside a banner-like container, unless the frame is the banner) whose
    label is_reject_label; the first one is clicked."""
    try:
        labels = await frame.evaluate(JS_GENERIC, require_banner) or []
        pick = next((i for i, label in enumerate(labels) if is_reject_label(label)), None)
        if pick is None:
            return None
        chosen = frame.locator(f'[data-scout-consent="{pick}"]').first
        await chosen.click(timeout=timeout_ms)
        closed = await _wait_closed(frame, None, chosen, timeout_ms)
        return {"platform": "generic", "clicked": labels[pick][:80], "via_settings": False, "closed": closed}
    except (PlaywrightError, PlaywrightTimeoutError):
        return None


JS_PRESENT = r"""
({selectors, frames}) => {
  for (const sel of selectors) { try { if (document.querySelector(sel)) return true; } catch (e) {} }
  return [...document.querySelectorAll('iframe')].some(f => frames.some(x => (f.src || '').includes(x) || (f.id || '').startsWith(x)));
}
"""


async def banner_present(page: Page) -> bool:
    """A known consent platform's banner or iframe is on the page (one cheap check; the page model's own banner
    flag misses iframes and shadow roots)."""
    try:
        return bool(await page.evaluate(JS_PRESENT, {
            "selectors": [sel for p in PLATFORMS for sel in p.detect],
            "frames": [x for p in PLATFORMS if p.frame for x in p.frame.split("|")]}))
    except (PlaywrightError, PlaywrightTimeoutError):
        return False

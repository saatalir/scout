"""B1 — the page model: what the agent "sees" of a browser page.

A snapshot lists the page's visible interactive elements, each tagged with a
stable id (`data-scout-id="e7"`) so the agent can act by id ("click e7",
"type into e3") instead of guessing selectors. Ids survive later snapshots of
the same page as long as the element stays in the DOM; new elements get new
ids. Alongside the elements: title, URL, a main-text excerpt, forms, and
whether a cookie/consent banner or a dialog seems to be open.

Safety: password fields are listed (the agent must know they exist so it can
hand sign-ins to the human) but their values are never read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from playwright.async_api import Page

SNAPSHOT_JS = r"""
(maxElements) => {
  const isVisible = (el) => {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) return false;
    const s = getComputedStyle(el);
    return s.visibility !== 'hidden' && s.display !== 'none' && parseFloat(s.opacity || '1') > 0.05;
  };
  const text = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const labelFor = (el) => {
    const aria = el.getAttribute('aria-label'); if (aria) return text(aria);
    const by = el.getAttribute('aria-labelledby');
    if (by) { const t = by.split(/\s+/).map(id => document.getElementById(id)).filter(Boolean).map(n => n.innerText).join(' '); if (text(t)) return text(t); }
    if (el.id) { const l = document.querySelector(`label[for="${CSS.escape(el.id)}"]`); if (l && text(l.innerText)) return text(l.innerText); }
    const wrap = el.closest('label'); if (wrap && text(wrap.innerText)) return text(wrap.innerText);
    if (el.placeholder) return text(el.placeholder);
    if (el.title) return text(el.title);
    if (el.tagName === 'INPUT' && ['submit', 'button', 'reset'].includes(el.type)) return text(el.value);
    if (el.tagName === 'IMG') return text(el.alt);
    const inner = text(el.innerText || el.textContent);
    if (inner) return inner.slice(0, 120);
    const img = el.querySelector('img[alt]'); if (img) return text(img.alt);
    return el.name ? `[${el.name}]` : '';
  };
  const sel = 'a[href], button, input:not([type=hidden]), select, textarea, summary, [role=button], [role=link], ' +
              '[role=checkbox], [role=radio], [role=tab], [role=menuitem], [role=option], [role=switch], ' +
              '[contenteditable=""], [contenteditable=true], [onclick]';
  let next = Number(document.documentElement.getAttribute('data-scout-next') || '1');
  const elements = [];
  for (const el of document.querySelectorAll(sel)) {
    if (elements.length >= maxElements) break;
    if (!isVisible(el) || el.closest('[aria-hidden=true]')) continue;
    let id = el.getAttribute('data-scout-id');
    if (!id) { id = 'e' + next++; el.setAttribute('data-scout-id', id); }
    const tag = el.tagName.toLowerCase();
    const type = tag === 'input' ? (el.type || 'text') : null;
    const item = {id, tag, role: el.getAttribute('role') || null, type, name: labelFor(el),
                  disabled: !!el.disabled || el.getAttribute('aria-disabled') === 'true'};
    if (tag === 'a') item.href = el.href;
    if (type === 'password') item.value = el.value ? '(filled)' : '';
    else if (tag === 'input' && ['checkbox', 'radio'].includes(type)) item.checked = el.checked;
    else if (tag === 'input' || tag === 'textarea') item.value = (el.value || '').slice(0, 200);
    if (tag === 'select') {
      item.options = [...el.options].slice(0, 40).map(o => text(o.text));
      item.value = el.selectedIndex >= 0 ? text(el.options[el.selectedIndex].text) : '';
    }
    if (el.required) item.required = true;
    const form = el.form || el.closest('form');
    if (form) { if (!form.hasAttribute('data-scout-form')) form.setAttribute('data-scout-form', 'f' + (document.querySelectorAll('form[data-scout-form]').length + 1)); item.form = form.getAttribute('data-scout-form'); }
    elements.push(item);
  }
  document.documentElement.setAttribute('data-scout-next', String(next));
  const main = document.querySelector('main, article, [role=main]') || document.body;
  const bannerRe = /cookie|consent|gdpr|privacy choices|we use cookies/i;
  const banner = [...document.querySelectorAll('div, section, aside, dialog, [role=dialog]')].some(n =>
      isVisible(n) && n.innerText && n.innerText.length < 3000 && bannerRe.test(n.innerText) &&
      ['fixed', 'sticky'].includes(getComputedStyle(n).position));
  const dialog = [...document.querySelectorAll('dialog[open], [role=dialog], [aria-modal=true]')].some(isVisible);
  // D3: content the element list cannot describe (drawn on a canvas, a plugin, a large picture without text)
  const visual = [];
  for (const n of document.querySelectorAll('canvas, embed, object, svg, iframe[src*="map"], [role=img], [role=application]')) {
    if (visual.length >= 5 || !isVisible(n) || n.closest('svg') !== null && n.tagName.toLowerCase() !== 'svg' ) continue;
    const r = n.getBoundingClientRect();
    if (r.width * r.height < 40000) continue;
    if (n.tagName.toLowerCase() === 'svg' && text(n.textContent).length > 40) continue;   // has its text in the DOM
    visual.push({tag: n.tagName.toLowerCase(), w: Math.round(r.width), h: Math.round(r.height),
                 label: text(n.getAttribute('aria-label') || n.getAttribute('title') || '').slice(0, 80)});
  }
  return {url: location.href, title: document.title, elements,
          text: text(main ? main.innerText : '').slice(0, 6000),
          forms: [...document.querySelectorAll('form[data-scout-form]')].map(f => ({id: f.getAttribute('data-scout-form'),
                  action: f.action || null, method: (f.method || 'get').toLowerCase()})),
          cookie_banner: banner, dialog_open: dialog, truncated: elements.length >= maxElements, visual};
}
"""


@dataclass
class Element:
    id: str
    tag: str
    name: str = ""
    role: str | None = None
    type: str | None = None
    href: str | None = None
    value: str | None = None
    checked: bool | None = None
    options: list[str] | None = None
    disabled: bool = False
    required: bool = False
    form: str | None = None

    def line(self) -> str:
        """One compact line for the model, e.g. `e7 button "Submit" form=f1`."""
        kind = self.role or (f"{self.tag}:{self.type}" if self.type else self.tag)
        parts = [self.id, kind, f'"{self.name[:80]}"' if self.name else '""']
        if self.href:
            parts.append(f"-> {self.href[:100]}")
        if self.value not in (None, ""):
            parts.append(f"value={self.value[:60]!r}")
        if self.checked is not None:
            parts.append("checked" if self.checked else "unchecked")
        if self.options:
            parts.append("options=" + "|".join(o[:30] for o in self.options[:12]))
        if self.required:
            parts.append("required")
        if self.disabled:
            parts.append("disabled")
        if self.form:
            parts.append(f"form={self.form}")
        return " ".join(parts)


@dataclass
class PageView:
    url: str
    title: str
    elements: list[Element] = field(default_factory=list)
    text: str = ""
    forms: list[dict] = field(default_factory=list)
    cookie_banner: bool = False
    dialog_open: bool = False
    truncated: bool = False
    visual: list[dict] = field(default_factory=list)

    def element(self, eid: str) -> Element | None:
        return next((e for e in self.elements if e.id == eid), None)

    def has_password_field(self) -> bool:
        return any(e.type == "password" for e in self.elements)

    def for_model(self, text_chars: int = 3000) -> dict[str, Any]:
        out: dict[str, Any] = {"url": self.url, "title": self.title,
                               "elements": [e.line() for e in self.elements],
                               "text": self.text[:text_chars]}
        if self.forms:
            out["forms"] = self.forms
        if self.cookie_banner:
            out["cookie_banner"] = "A cookie/consent banner seems open: decline non-essential cookies first."
        if self.dialog_open:
            out["dialog_open"] = True
        if self.has_password_field():
            out["sign_in"] = "This page asks for a password: hand sign-ins to the human (hand_to_human)."
        if self.truncated:
            out["note"] = "Only the first elements are listed; scroll or read for more."
        if self.visual:
            what = ", ".join(f"{v['tag']} {v['w']}x{v['h']}" + (f" \"{v['label']}\"" if v.get("label") else "")
                             for v in self.visual)
            out["visual"] = (f"Part of this page is drawn, not text ({what}): the element list and text cannot show "
                             "it. Use vision_look to read it and vision_click to act on it.")
        return out


async def snapshot(page: Page, max_elements: int = 150) -> PageView:
    raw = await page.evaluate(SNAPSHOT_JS, max_elements)
    els = [Element(**{k: v for k, v in e.items() if k in Element.__dataclass_fields__}) for e in raw["elements"]]
    return PageView(url=raw["url"], title=raw["title"], elements=els, text=raw["text"], forms=raw["forms"],
                    cookie_banner=raw["cookie_banner"], dialog_open=raw["dialog_open"], truncated=raw["truncated"],
                    visual=raw.get("visual") or [])


def locator_for(page: Page, eid: str):
    """The Playwright locator of an element from the latest snapshot."""
    return page.locator(f'[data-scout-id="{eid}"]')


def as_dict(view: PageView) -> dict:
    return asdict(view)

"""Make a captured search page safe to commit.

Saved result pages (tests/fixtures/search/*_live.html) are test fixtures. A
raw capture carries details about whoever captured it: an IP-derived location
(Brave stores coordinates in a script), the country and city names engines
print (listed in .env, see _place_patterns), and per-session tokens (Google's sxsrf, gsessionid, opaque /goto
redirect ids, data-ved click ids). scrub_html removes them while keeping
the result markup the parsers read, so the live-capture tests still exercise
real engine markup.
"""

from __future__ import annotations

import itertools
import os
import re

from dotenv import load_dotenv

from ..config import ROOT

load_dotenv(ROOT / ".env", override=False)


def _place_patterns() -> tuple[re.Pattern | None, ...]:
    """Where the capturing machine is, from .env (kept out of the repository).

    SCOUT_SCRUB_PLACES   comma list of place names engines print for you (city,
                         region, country; any language), e.g. "Springfield,Ohio"
    SCOUT_SCRUB_REGIONS  comma list of ISO country codes, e.g. "US": scrubs
                         locale tags (lang="en-US") and gl=/cr=/region= parameters
    """
    places = [p.strip() for p in os.getenv("SCOUT_SCRUB_PLACES", "").split(",") if p.strip()]
    regions = [r.strip().upper() for r in os.getenv("SCOUT_SCRUB_REGIONS", "").split(",") if r.strip()]
    names = re.compile("|".join(rf"(?<!\w){re.escape(p)}(?!\w)" for p in places), re.I) if places else None
    if not regions:
        return names, None, None, None, None
    alt = "|".join(map(re.escape, regions))
    tag = re.compile(rf"\b([a-z]{{2}})[-_]({alt})\b")                          # lang="en-US"
    param = re.compile(rf"\b(gl|cr|region)=(?:country)?({alt})\b", re.I)       # gl=us, cr=countryUS
    quoted = re.compile(rf"(\"|&quot;)({alt})(\"|&quot;)")                       # "US" in inline data
    tld = re.compile(rf"(?<=[a-z0-9-])\.(?:com\.)?({alt.lower()})(?=[/:\"'&?#\s<)]|$)")  # google.us, x.com.us
    return names, tag, param, quoted, tld


_DROP_BLOCKS = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1>", re.S | re.I)
_HIDDEN_INPUTS = re.compile(r"<input\b[^>]*type=\"hidden\"[^>]*>", re.I)
# Brave's "lat x lon" form (geoLocation:"37.984x23.735"). Comma pairs are left alone: SVG paths use them.
_COORDS = re.compile(r"(?<![\d.])-?\d{1,3}\.\d{3,}x-?\d{1,3}\.\d{3,}(?![\d.])")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_ATTRS = re.compile(r"\s(data-ved|jsdata|data-lpage|ping)=\"[^\"]*\"", re.I)
_TOKENS = re.compile(r"((?:gsessionid|sxsrf|ei|sei|cshid|zx|sca_esv|ved)=)(?!REDACTED)[^&\"'\s<>]+")


def scrub_html(html: str) -> str:
    html = _DROP_BLOCKS.sub("", html)
    html = _HIDDEN_INPUTS.sub("", html)
    html = _ATTRS.sub("", html)
    counter = itertools.count(1)
    # Google's opaque result redirects: keep them distinct (the parser de-duplicates by href).
    html = re.sub(r"/goto\?url=(?!REDACTED-\d)[^&\"'\s<>]+", lambda m: f"/goto?url=REDACTED-{next(counter)}", html)
    html = _TOKENS.sub(r"\1REDACTED", html)
    html = _COORDS.sub("0.000x0.000", html)
    html = _IPV4.sub(lambda m: m.group(0) if m.group(0).startswith(("127.", "0.")) else "192.0.2.1", html)
    names, tag, param, quoted, tld = _place_patterns()
    if names:
        html = names.sub("Somewhere", html)
    if tag:
        html = tag.sub(r"\1-XX", html)
        html = param.sub(r"\1=XX", html)
        html = quoted.sub(r"\1XX\3", html)
        html = tld.sub(".example", html)
    return html

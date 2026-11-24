"""RG6: are two sources independent? (registrable domains and near-copy detection).

Two sources are the *same* source when they come from the same site (same registrable domain) or when one
text is a near-copy of the other (a press release reposted on several sites).

The intent that matters most: **undercounting independence is safe; overcounting is the bug.** When in
doubt, treat two sources as one. A rigor rule that believes it has two independent sources when it has one
copy twice defeats its purpose.

Standard library only; no network; no new dependencies.
"""

from __future__ import annotations

import re

__all__ = [
    "MULTI_LABEL_SUFFIXES",
    "registrable_domain",
    "words",
    "shingles",
    "similarity",
    "near_duplicate",
    "independent_groups",
    "count_independent",
]

# Public suffixes of two or more labels under which every name is a separate site (country second-level
# suffixes, plus hosting suffixes where each subdomain belongs to a different owner). A suffix missing
# from this list makes sites under it look like one site: that undercounts independence, which is safe.
MULTI_LABEL_SUFFIXES: frozenset[str] = frozenset(
    {
        # --- hosting: every subdomain is a different owner -------------------------------
        "github.io",
        "gitlab.io",
        "bitbucket.io",
        "readthedocs.io",
        "blogspot.com",
        "wordpress.com",
        "wordpress.site",
        "substack.com",
        "medium.com",
        "wixsite.com",
        "weebly.com",
        "weeblysite.com",
        "squarespace.com",
        "webflow.io",
        "cargo.site",
        "notion.site",
        "ghost.io",
        "beehiiv.com",
        "myshopify.com",
        "netlify.app",
        "vercel.app",
        "pages.dev",
        "workers.dev",
        "herokuapp.com",
        "fly.dev",
        "web.app",
        "firebaseapp.com",
        "appspot.com",
        "glitch.me",
        "surge.sh",
        # --- United Kingdom ---------------------------------------------------------------
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "me.uk",
        "net.uk",
        "sch.uk",
        "plc.uk",
        "ltd.uk",
        "police.uk",
        "nhs.uk",
        "mod.uk",
        "judiciary.uk",
        "parliament.uk",
        # --- Australia --------------------------------------------------------------------
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "gov.au",
        "id.au",
        "asn.au",
        "csiro.au",
        # --- New Zealand ------------------------------------------------------------------
        "co.nz",
        "net.nz",
        "org.nz",
        "govt.nz",
        "ac.nz",
        "geek.nz",
        "kiwi.nz",
        "maori.nz",
        "school.nz",
        "cri.nz",
        "gen.nz",
        "health.nz",
        "research.nz",
        # --- South Africa -----------------------------------------------------------------
        "co.za",
        "org.za",
        "web.za",
        "net.za",
        "gov.za",
        "ac.za",
        "school.za",
        "law.za",
        "mil.za",
        "nom.za",
        # --- Brazil -----------------------------------------------------------------------
        "com.br",
        "net.br",
        "org.br",
        "gov.br",
        "edu.br",
        "art.br",
        "blog.br",
        "flog.br",
        "wiki.br",
        "nom.br",
        "adm.br",
        "adv.br",
        "tur.br",
        "rec.br",
        "esp.br",
        "pro.br",
        "med.br",
        "eng.br",
        # --- China ------------------------------------------------------------------------
        "com.cn",
        "net.cn",
        "org.cn",
        "gov.cn",
        "edu.cn",
        "ac.cn",
        "mil.cn",
        "hk.cn",
        "mo.cn",
        "tw.cn",
        # --- Mexico -----------------------------------------------------------------------
        "com.mx",
        "org.mx",
        "gob.mx",
        "edu.mx",
        "net.mx",
        # --- India ------------------------------------------------------------------------
        "co.in",
        "net.in",
        "org.in",
        "gen.in",
        "firm.in",
        "info.in",
        "nom.in",
        "ac.in",
        "edu.in",
        "res.in",
        "gov.in",
        "mil.in",
        # --- Japan ------------------------------------------------------------------------
        "co.jp",
        "or.jp",
        "ne.jp",
        "ac.jp",
        "go.jp",
        "ad.jp",
        "ed.jp",
        "gr.jp",
        "lg.jp",
        # --- South Korea ------------------------------------------------------------------
        "co.kr",
        "or.kr",
        "ne.kr",
        "ac.kr",
        "go.kr",
        "re.kr",
        "pe.kr",
        "es.kr",
        "hs.kr",
        "ms.kr",
        "kg.kr",
        "seoul.kr",
        "busan.kr",
        "incheon.kr",
        "daegu.kr",
        "daejeon.kr",
        "gwangju.kr",
        "ulsan.kr",
        "sejong.kr",
        # --- Turkey -----------------------------------------------------------------------
        "com.tr",
        "net.tr",
        "org.tr",
        "gov.tr",
        "edu.tr",
        "k12.tr",
        "pol.tr",
        "bel.tr",
        "bbs.tr",
        "dr.tr",
        "info.tr",
        "name.tr",
        "tel.tr",
        "tv.tr",
        "web.tr",
        # --- Greece -----------------------------------------------------------------------
        "com.gr",
        "net.gr",
        "org.gr",
        "gov.gr",
        "edu.gr",
        # --- Israel -----------------------------------------------------------------------
        "co.il",
        "org.il",
        "net.il",
        "ac.il",
        "gov.il",
        "muni.il",
        "k12.il",
        "idf.il",
        # --- Singapore --------------------------------------------------------------------
        "com.sg",
        "net.sg",
        "org.sg",
        "gov.sg",
        "edu.sg",
        "per.sg",
        # --- Hong Kong / Taiwan -----------------------------------------------------------
        "com.hk",
        "org.hk",
        "net.hk",
        "gov.hk",
        "edu.hk",
        "idv.hk",
        "com.tw",
        "org.tw",
        "net.tw",
        "gov.tw",
        "edu.tw",
        "idv.tw",
        "club.tw",
        "ebiz.tw",
        "game.tw",
        "mil.tw",
        # --- Latin America ----------------------------------------------------------------
        "com.ar",
        "net.ar",
        "org.ar",
        "gov.ar",
        "edu.ar",
        "int.ar",
        "mil.ar",
        "tur.ar",
        "com.co",
        "net.co",
        "org.co",
        "gov.co",
        "edu.co",
        "mil.co",
        "nom.co",
        "com.pe",
        "net.pe",
        "org.pe",
        "gob.pe",
        "edu.pe",
        "mil.pe",
        "nom.pe",
        # --- Europe -----------------------------------------------------------------------
        "com.pl",
        "net.pl",
        "org.pl",
        "gov.pl",
        "edu.pl",
        "waw.pl",
        "krakow.pl",
        "gdansk.pl",
        "wroc.pl",
        "poznan.pl",
        "lodz.pl",
        "szczecin.pl",
        "sklep.pl",
        "targi.pl",
        "com.ru",
        "net.ru",
        "org.ru",
        "gov.ru",
        "edu.ru",
        "int.ru",
        "mil.ru",
        "com.ua",
        "net.ua",
        "org.ua",
        "gov.ua",
        "edu.ua",
        "in.ua",
        "kyiv.ua",
        "lviv.ua",
        "kharkiv.ua",
        "com.ro",
        "org.ro",
        "store.ro",
        "firm.ro",
        "arts.ro",
        "rec.ro",
        "info.ro",
        "nom.ro",
        "co.rs",
        "org.rs",
        "edu.rs",
        "ac.rs",
        "gov.rs",
        "in.rs",
        "com.hr",
        "from.hr",
        "iz.hr",
        "name.hr",
        "com.es",
        "org.es",
        "gob.es",
        "edu.es",
        "nom.es",
        "com.fr",
        "asso.fr",
        "nom.fr",
        "prd.fr",
        "presse.fr",
        "tm.fr",
        "gouv.fr",
        "com.it",
        "net.it",
        "org.it",
        "gov.it",
        "edu.it",
        "mil.it",
        "com.se",
        "net.se",
        "org.se",
        "gov.se",
        "edu.se",
        "mil.se",
        "press.se",
        "tm.se",
        "com.no",
        "net.no",
        "org.no",
        "gov.no",
        "edu.no",
        "mil.no",
        "priv.no",
        "com.dk",
        "net.dk",
        "org.dk",
        "gov.dk",
        "edu.dk",
        "mil.dk",
        "biz.dk",
        "co.dk",
        "firm.dk",
        "reg.dk",
        "store.dk",
        "com.fi",
        "net.fi",
        "org.fi",
        "gov.fi",
        "edu.fi",
        "mil.fi",
        "com.pt",
        "net.pt",
        "org.pt",
        "gov.pt",
        "edu.pt",
        "int.pt",
        "publ.pt",
        "co.at",
        "or.at",
        "ac.at",
        "gv.at",
        "priv.at",
        "co.ch",
        "or.ch",
        "ac.ch",
        "gov.ch",
        "edu.ch",
        "net.ch",
        "com.ch",
        "com.be",
        "net.be",
        "org.be",
        "gov.be",
        "edu.be",
        "ac.be",
        "mil.be",
        "blog.be",
        "web.be",
        "com.ie",
        "net.ie",
        "org.ie",
        "gov.ie",
        "edu.ie",
        "mil.ie",
        # --- Canada / United States -------------------------------------------------------
        "co.ca",
        "gc.ca",
        "on.ca",
        "qc.ca",
        "bc.ca",
        "ab.ca",
        "mb.ca",
        "sk.ca",
        "ns.ca",
        "nb.ca",
        "nl.ca",
        "pe.ca",
        "nt.ca",
        "nu.ca",
        "yt.ca",
        "com.us",
        "net.us",
        "org.us",
        "gov.us",
        "edu.us",
        "k12.us",
        "gen.us",
        "isa.us",
        "dni.us",
        "fed.us",
        "state.us",
        "lib.us",
        "nsn.us",
        # --- Middle East ------------------------------------------------------------------
        "com.eg",
        "net.eg",
        "org.eg",
        "gov.eg",
        "edu.eg",
        "mil.eg",
        "sci.eg",
        "biz.eg",
        "info.eg",
        "name.eg",
        "com.sa",
        "net.sa",
        "org.sa",
        "gov.sa",
        "edu.sa",
        "med.sa",
        "pub.sa",
        "sch.sa",
        "co.ae",
        "net.ae",
        "org.ae",
        "gov.ae",
        "ac.ae",
        "sch.ae",
        "mil.ae",
        "pro.ae",
        "name.ae",
        "co.ir",
        "net.ir",
        "org.ir",
        "gov.ir",
        "ac.ir",
        "id.ir",
        "sch.ir",
        # --- Africa -----------------------------------------------------------------------
        "com.pk",
        "net.pk",
        "org.pk",
        "gov.pk",
        "edu.pk",
        "web.pk",
        "biz.pk",
        "info.pk",
        "com.ng",
        "net.ng",
        "org.ng",
        "gov.ng",
        "edu.ng",
        "mil.ng",
        "name.ng",
        "biz.ng",
        "sch.ng",
        "co.ke",
        "or.ke",
        "ne.ke",
        "go.ke",
        "ac.ke",
        "sc.ke",
        "me.ke",
        "info.ke",
        "co.tz",
        "or.tz",
        "ac.tz",
        "go.tz",
        "ne.tz",
        "sc.tz",
        "me.tz",
        "co.ug",
        "or.ug",
        "ac.ug",
        "go.ug",
        "ne.ug",
        "sc.ug",
        "co.ma",
        "net.ma",
        "org.ma",
        "gov.ma",
        "ac.ma",
        "press.ma",
        "com.dz",
        "net.dz",
        "org.dz",
        "gov.dz",
        "edu.dz",
        "pol.dz",
        "art.dz",
        "soc.dz",
        "tm.dz",
        "info.dz",
        "web.dz",
        "asso.dz",
        # --- Asia-Pacific -----------------------------------------------------------------
        "com.my",
        "net.my",
        "org.my",
        "gov.my",
        "edu.my",
        "mil.my",
        "name.my",
        "com.ph",
        "net.ph",
        "org.ph",
        "gov.ph",
        "edu.ph",
        "mil.ph",
        "ngo.ph",
        "i.ph",
        "com.vn",
        "net.vn",
        "org.vn",
        "gov.vn",
        "edu.vn",
        "ac.vn",
        "biz.vn",
        "info.vn",
        "name.vn",
        "pro.vn",
        "health.vn",
        "co.th",
        "or.th",
        "in.th",
        "ac.th",
        "go.th",
        "mi.th",
        "net.th",
        "co.id",
        "web.id",
        "or.id",
        "go.id",
        "sch.id",
        "my.id",
        "net.id",
        "biz.id",
        "ac.id",
    }
)

_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://")
_HOST_RE = re.compile(
    r"^[a-z0-9]([a-z0-9\-_]*[a-z0-9])?(\.[a-z0-9]([a-z0-9\-_]*[a-z0-9])?)*$"
)
_IPV4_RE = re.compile(r"^\d{1,3}(\.\d{1,3}){3}$")
_IPV6ISH_RE = re.compile(r"[0-9a-f:.%]+")
_WORD_RE = re.compile(r"\w+")


def registrable_domain(url: str) -> str:
    """The site a URL belongs to: lower-cased host without port, userinfo or trailing dot, subdomains
    dropped. If the last two labels form a suffix in ``MULTI_LABEL_SUFFIXES`` keep three labels, else two.

    IP addresses are returned as they are. An empty or unparsable URL returns ``""`` (callers treat ``""``
    as unknown, never as independent).
    """
    if not url:
        return ""
    raw = str(url).strip()
    if not raw:
        return ""

    # Drop the fragment and the query, then the scheme.
    for sep in ("#", "?"):
        cut = raw.find(sep)
        if cut != -1:
            raw = raw[:cut]
    if _SCHEME_RE.match(raw):
        raw = _SCHEME_RE.sub("", raw, count=1)
    elif raw.startswith("//"):
        raw = raw[2:]

    # The authority runs to the first slash; drop any userinfo.
    cut = raw.find("/")
    if cut != -1:
        raw = raw[:cut]
    at = raw.rfind("@")
    if at != -1:
        raw = raw[at + 1 :]

    host = raw.strip()
    if not host:
        return ""

    # Bracketed IPv6 literal, optionally followed by :port.
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return ""
        inner = host[1:end].lower()
        return inner if _IPV6ISH_RE.fullmatch(inner) else ""

    # A port is a colon followed by digits and nothing else weird.
    if ":" in host:
        head, _, tail = host.rpartition(":")
        if head and ":" not in head and tail.isdigit():
            host = head
        else:  # bare IPv6 without brackets: keep it as it is
            lowered = host.lower()
            return lowered if _IPV6ISH_RE.fullmatch(lowered) else ""

    host = host.lower().rstrip(".")
    if not host or not _HOST_RE.match(host):
        return ""

    if _IPV4_RE.match(host) and all(int(part) <= 255 for part in host.split(".")):
        return host

    labels = host.split(".")
    if len(labels) <= 2:
        return host
    if ".".join(labels[-2:]) in MULTI_LABEL_SUFFIXES:
        return ".".join(labels[-3:])
    return ".".join(labels[-2:])


def words(text: str) -> list[str]:
    """Lower-cased word tokens of ``text``."""
    if not text:
        return []
    return _WORD_RE.findall(str(text).lower())


def shingles(text: str, k: int = 5) -> set[tuple[str, ...]]:
    """Consecutive ``k``-word tuples of :func:`words`. Fewer than ``k`` words: one tuple of all the words
    (empty text: empty set)."""
    k = max(1, int(k))
    toks = words(text)
    if not toks:
        return set()
    if len(toks) < k:
        return {tuple(toks)}
    return {tuple(toks[i : i + k]) for i in range(len(toks) - k + 1)}


def similarity(a: str, b: str, k: int = 5) -> float:
    """Jaccard similarity of the two shingle sets (0.0 when either is empty)."""
    sa = shingles(a, k)
    sb = shingles(b, k)
    if not sa or not sb:
        return 0.0
    union = len(sa | sb)
    if not union:
        return 0.0
    return len(sa & sb) / union


def _near(sa: set, sb: set, na: int, nb: int, threshold: float) -> bool:
    """near_duplicate on precomputed shingle sets and word counts."""
    union = len(sa | sb)
    if union and len(sa & sb) / union >= threshold:
        return True
    if min(na, nb) < 40:
        return False
    small, big = (sa, sb) if na <= nb else (sb, sa)
    return bool(small) and sum(1 for s in small if s in big) / len(small) >= 0.8


def near_duplicate(a: str, b: str, threshold: float = 0.6, k: int = 5) -> bool:
    """True when the two texts are the same text (Jaccard of their ``k``-shingles at or above
    ``threshold``) or when one is an excerpt of the other (the shorter text, at least 40 words, has
    80 % or more of its shingles inside the longer one)."""
    sa = shingles(a, k)
    sb = shingles(b, k)
    if not sa or not sb:
        return False
    return _near(sa, sb, len(words(a)), len(words(b)), threshold)


def _field(source: object, key: str) -> str:
    if not isinstance(source, dict):
        return ""
    value = source.get(key, "")
    if value is None:
        return ""
    return str(value)


def independent_groups(sources: list[dict], threshold: float = 0.6) -> list[list[int]]:
    """Group the indices of sources that are NOT independent of each other.

    Sources ``i`` and ``j`` share a group when their registrable domains are equal and non-empty, or when
    both have text and one text is a near-copy of the other. Grouping is transitive (union-find). Each
    returned list is sorted; the groups are ordered by their smallest index.
    """
    n = len(sources)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri == rj:
            return
        if ri < rj:
            parent[rj] = ri
        else:
            parent[ri] = rj

    domains = [registrable_domain(_field(s, "url")) for s in sources]
    texts = [_field(s, "text") for s in sources]
    # Fingerprints once per text, not once per pair: pages can be long and there can be dozens of them.
    sets = [shingles(t) if t else set() for t in texts]
    counts = [len(words(t)) if t else 0 for t in texts]

    for i in range(n):
        for j in range(i + 1, n):
            if domains[i] and domains[i] == domains[j]:
                union(i, j)
                continue
            if sets[i] and sets[j] and _near(sets[i], sets[j], counts[i], counts[j], threshold):
                union(i, j)

    groups: dict[int, list[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    ordered = sorted(groups.values(), key=lambda g: g[0])
    return [sorted(g) for g in ordered]


def count_independent(sources: list[dict], threshold: float = 0.6) -> int:
    """How many independent sources there are: the number of groups from :func:`independent_groups`, not
    counting groups made only of sources with an unknown domain (``""``) and no text — those cannot be
    shown to be independent."""
    count = 0
    for group in independent_groups(sources, threshold):
        known = any(registrable_domain(_field(sources[i], "url")) for i in group)
        has_text = any(_field(sources[i], "text").strip() for i in group)
        if known or has_text:
            count += 1
    return count

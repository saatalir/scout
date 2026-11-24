---
files: ["scout/independence.py", "tests/test_independence.py"]
context: ["docs/RIGOR.md"]
tests: ["tests/test_independence.py"]
---
# RG6: are two sources independent? (domain and near-copy helpers)

Scout's rigor checks (docs/RIGOR.md) count how many **independent** sources support a claim. Two sources are
the same source when they come from the same site (same registrable domain) or when one text is a near-copy of
the other (a press release reposted on several sites). Write a NEW module `scout/independence.py`
(`from __future__ import annotations`, standard library only, no network, no new dependencies) and its tests.
Do not change any other file.

**The intent that matters most: undercounting independence is safe; overcounting is the bug.** When in doubt,
treat two sources as the same source. A rigor rule that believes it has two independent sources when it has one
copy twice defeats its purpose.

## API

```python
MULTI_LABEL_SUFFIXES: frozenset[str]
    """Public suffixes of two or more labels under which every name is a separate site, e.g. "co.uk", "ac.uk",
    "gov.uk", "org.uk", "com.au", "net.au", "org.au", "edu.au", "gov.au", "co.jp", "ac.jp", "co.nz", "co.za",
    "com.br", "com.cn", "com.mx", "co.in", "co.kr", "com.tr", "com.gr" — about 30 common country ones — and
    hosting suffixes where each subdomain belongs to a different owner: "github.io", "gitlab.io",
    "blogspot.com", "wordpress.com", "substack.com", "medium.com", "netlify.app", "vercel.app", "pages.dev",
    "herokuapp.com", "readthedocs.io", "web.app", "firebaseapp.com". A suffix missing from this list makes
    sites under it look like one site: that undercounts, which is safe."""

def registrable_domain(url: str) -> str:
    """The site a URL belongs to: lower-cased host without port, userinfo or a trailing dot; "www." and other
    subdomains dropped. Rule: if the last two labels form a suffix in MULTI_LABEL_SUFFIXES, keep three labels,
    else keep two. Examples:
      https://www.bbc.co.uk/news/x        -> bbc.co.uk
      https://news.example.com:8443/a     -> example.com
      https://alice.github.io/blog        -> alice.github.io
      https://EXAMPLE.com./               -> example.com
      http://192.168.1.10/x               -> 192.168.1.10      (IP addresses as they are; also IPv6 in [])
      http://localhost:8000/              -> localhost
      example.org/path (no scheme)        -> example.org
    An empty or unparsable URL returns "" (callers treat "" as unknown, never as independent)."""

def words(text: str) -> list[str]:
    """Lower-cased word tokens: re.findall(r"\\w+", text.lower())."""

def shingles(text: str, k: int = 5) -> set[tuple[str, ...]]:
    """Consecutive k-word tuples of words(text). Fewer than k words: one tuple of all the words (empty text:
    empty set)."""

def similarity(a: str, b: str, k: int = 5) -> float:
    """Jaccard similarity of the two shingle sets (0.0 when either is empty)."""

def near_duplicate(a: str, b: str, threshold: float = 0.6, k: int = 5) -> bool:
    """True when similarity >= threshold, OR when the shorter text (by word count, at least 40 words) has
    >= 80 % of its shingles inside the longer one's (an excerpt or a copy with extra boilerplate around it)."""

def independent_groups(sources: list[dict], threshold: float = 0.6) -> list[list[int]]:
    """Group sources that are NOT independent of each other. Each source is a dict with "url" and optionally
    "text". Sources i and j are in the same group when registrable_domain is equal and non-empty for both, or
    when both have text and near_duplicate(text_i, text_j, threshold). Grouping is transitive (union-find): if
    A~B and B~C then A, B, C are one group. A source with an empty registrable domain and no text is its own
    group only if nothing else joins it — but see count_independent. Returns lists of indices; each list
    sorted; groups ordered by their smallest index."""

def count_independent(sources: list[dict], threshold: float = 0.6) -> int:
    """Number of groups, NOT counting groups made only of sources with an unknown domain ("") and no text
    (they cannot be shown to be independent)."""
```

## Tests (`tests/test_independence.py`)

Offline, plain pytest. At least:

1. The registrable_domain examples above, plus `https://sub.a.b.example.com` → `example.com`,
   `https://bob.github.io` vs `https://alice.github.io` differ, `https://x.co.uk` → `x.co.uk`,
   `https://[::1]:8080/` → `::1` (or `[::1]`, consistently), `""` → `""`.
2. Same site, different pages: `count_independent` of two URLs on `www.nature.com` and `nature.com` is 1.
3. Two different sites with unrelated texts: 2.
4. Syndication: one ~120-word press release text on three different domains (same words, a different
   one-line header and footer on each) → 1.
5. An excerpt: a 60-word paragraph cut from a 300-word article on another domain → same group.
6. Transitivity: A same domain as B, B near-copy of C (different domain) → one group of three.
7. Unknown sources: two entries with url "" and no text → `count_independent` is 0; with text that differs →
   each counts (text proves they differ? No: unknown domain but different text → each is its own group and
   counts) — define it exactly as the docstring says and test that.
8. Thresholds: two texts sharing about half their shingles are not near-duplicates at 0.6; the same pair is at 0.4.
9. Order: groups are sorted and ordered by smallest index.

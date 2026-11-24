"""RG6: registrable domains and near-copy detection. Offline, plain pytest."""

from __future__ import annotations

import pytest

from scout.independence import (
    MULTI_LABEL_SUFFIXES,
    count_independent,
    independent_groups,
    near_duplicate,
    registrable_domain,
    shingles,
    similarity,
    words,
)


def _words(prefix: str, n: int) -> str:
    """``n`` distinct, predictable word tokens."""
    return " ".join(f"{prefix}{i}" for i in range(n))


# ---------------------------------------------------------------------------------------------
# 1. registrable_domain
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.bbc.co.uk/news/x", "bbc.co.uk"),
        ("https://news.example.com:8443/a", "example.com"),
        ("https://alice.github.io/blog", "alice.github.io"),
        ("https://EXAMPLE.com./", "example.com"),
        ("http://192.168.1.10/x", "192.168.1.10"),
        ("http://localhost:8000/", "localhost"),
        ("example.org/path", "example.org"),
        ("https://sub.a.b.example.com", "example.com"),
        ("https://x.co.uk", "x.co.uk"),
        ("https://[::1]:8080/", "::1"),
        ("", ""),
        ("   ", ""),
        ("not a url", ""),
        ("https://", ""),
        ("/just/a/path", ""),
        ("https://user:pw@example.org/a", "example.org"),
        ("https://WWW.Example.COM./a", "example.com"),
        ("https://myblog.blogspot.com/2024/01/post.html", "myblog.blogspot.com"),
        ("https://foo.bar.github.io/x", "bar.github.io"),
        ("https://docs.example.co.uk/a/b?c=1#d", "example.co.uk"),
        ("//cdn.example.net/lib.js", "example.net"),
        ("http://example.com", "example.com"),
    ],
)
def test_registrable_domain(url: str, expected: str) -> None:
    assert registrable_domain(url) == expected


def test_github_io_subdomains_are_different_sites() -> None:
    bob = registrable_domain("https://bob.github.io")
    alice = registrable_domain("https://alice.github.io")
    assert bob == "bob.github.io"
    assert alice == "alice.github.io"
    assert bob != alice


def test_unknown_suffix_undercounts_safely() -> None:
    # A suffix not in the list collapses subdomains into one site: safe (undercounts).
    assert registrable_domain("https://a.b.example.org") == "example.org"


def test_multi_label_suffixes_contents() -> None:
    assert isinstance(MULTI_LABEL_SUFFIXES, frozenset)
    for suffix in (
        "co.uk",
        "ac.uk",
        "gov.uk",
        "org.uk",
        "com.au",
        "net.au",
        "org.au",
        "edu.au",
        "gov.au",
        "co.jp",
        "ac.jp",
        "co.nz",
        "co.za",
        "com.br",
        "com.cn",
        "com.mx",
        "co.in",
        "co.kr",
        "com.tr",
        "com.gr",
        "github.io",
        "gitlab.io",
        "blogspot.com",
        "wordpress.com",
        "substack.com",
        "medium.com",
        "netlify.app",
        "vercel.app",
        "pages.dev",
        "herokuapp.com",
        "readthedocs.io",
        "web.app",
        "firebaseapp.com",
    ):
        assert suffix in MULTI_LABEL_SUFFIXES, suffix
    assert all(s == s.lower() for s in MULTI_LABEL_SUFFIXES)
    assert all("." in s for s in MULTI_LABEL_SUFFIXES)


# ---------------------------------------------------------------------------------------------
# words / shingles / similarity
# ---------------------------------------------------------------------------------------------


def test_words_lowercases_and_splits_on_punctuation() -> None:
    assert words("Hello, World! 42") == ["hello", "world", "42"]
    assert words("") == []


def test_shingles_are_consecutive_k_word_tuples() -> None:
    assert shingles("a b c d e f") == {("a", "b", "c", "d", "e"), ("b", "c", "d", "e", "f")}
    assert shingles("a b c d e") == {("a", "b", "c", "d", "e")}


def test_shingles_short_text_is_one_tuple() -> None:
    assert shingles("one two three") == {("one", "two", "three")}
    assert shingles("") == set()


def test_similarity_identical_is_one_and_disjoint_is_zero() -> None:
    text = _words("s", 20)
    assert similarity(text, text) == pytest.approx(1.0)
    assert similarity(_words("p", 20), _words("q", 20)) == 0.0


def test_similarity_empty_is_zero() -> None:
    assert similarity("", "a b c") == 0.0
    assert similarity("a b c", "") == 0.0


def test_near_duplicate_empty_text_is_false() -> None:
    assert not near_duplicate("", "")
    assert not near_duplicate("a b c", "")


# ---------------------------------------------------------------------------------------------
# 2. same site, different pages
# ---------------------------------------------------------------------------------------------


def test_same_site_different_pages_count_once() -> None:
    sources = [
        {"url": "https://www.nature.com/articles/s41586-024-0001"},
        {"url": "https://nature.com/news"},
    ]
    assert independent_groups(sources) == [[0, 1]]
    assert count_independent(sources) == 1


# ---------------------------------------------------------------------------------------------
# 3. two different sites with unrelated texts
# ---------------------------------------------------------------------------------------------


def test_two_sites_with_unrelated_texts_are_two_sources() -> None:
    sources = [
        {"url": "https://one.example.org/a", "text": _words("x", 60)},
        {"url": "https://two.example.net/b", "text": _words("y", 60)},
    ]
    assert independent_groups(sources) == [[0], [1]]
    assert count_independent(sources) == 2


# ---------------------------------------------------------------------------------------------
# 4. syndication: one press release on three domains
# ---------------------------------------------------------------------------------------------


def test_syndicated_press_release_on_three_domains_is_one_source() -> None:
    release = _words("w", 120)
    sources = [
        {
            "url": "https://site-one.example.org/press",
            "text": "Site One press desk. " + release + " About Site One.",
        },
        {
            "url": "https://site-two.example.net/news",
            "text": "Site Two newsroom. " + release + " Contact Site Two.",
        },
        {
            "url": "https://site-three.example.com/a",
            "text": "Site Three media. " + release + " Follow Site Three.",
        },
    ]
    assert independent_groups(sources) == [[0, 1, 2]]
    assert count_independent(sources) == 1


# ---------------------------------------------------------------------------------------------
# 5. an excerpt cut from a longer article on another domain
# ---------------------------------------------------------------------------------------------


def test_excerpt_of_a_longer_article_is_the_same_source() -> None:
    article = _words("p", 300)
    excerpt = " ".join(article.split()[100:160])
    assert len(excerpt.split()) == 60
    sources = [
        {"url": "https://journal.example.org/a", "text": article},
        {"url": "https://blog.example.net/b", "text": excerpt},
    ]
    assert independent_groups(sources) == [[0, 1]]
    assert count_independent(sources) == 1
    assert near_duplicate(article, excerpt)  # symmetric
    assert near_duplicate(excerpt, article)


def test_short_quote_is_not_a_near_duplicate() -> None:
    article = _words("p", 300)
    quote = " ".join(article.split()[100:110])
    assert not near_duplicate(article, quote)


# ---------------------------------------------------------------------------------------------
# 6. transitivity
# ---------------------------------------------------------------------------------------------


def test_grouping_is_transitive() -> None:
    sources = [
        {"url": "https://alpha.example.com/1", "text": _words("t", 80)},
        {"url": "https://www.alpha.example.com/2", "text": _words("u", 90)},
        {"url": "https://beta.example.org/3", "text": _words("u", 90) + " extra tail words here"},
    ]
    assert independent_groups(sources) == [[0, 1, 2]]
    assert count_independent(sources) == 1


# ---------------------------------------------------------------------------------------------
# 7. unknown sources
# ---------------------------------------------------------------------------------------------


def test_unknown_sources_do_not_count() -> None:
    sources = [{"url": ""}, {"url": ""}]
    assert independent_groups(sources) == [[0], [1]]
    assert count_independent(sources) == 0


def test_unknown_domain_with_different_text_each_counts() -> None:
    sources = [
        {"url": "", "text": _words("m", 60)},
        {"url": "", "text": _words("n", 60)},
    ]
    assert independent_groups(sources) == [[0], [1]]
    assert count_independent(sources) == 2


def test_unknown_domain_with_the_same_text_counts_once() -> None:
    sources = [
        {"url": "", "text": _words("m", 60)},
        {"url": "", "text": _words("m", 60)},
    ]
    assert independent_groups(sources) == [[0, 1]]
    assert count_independent(sources) == 1


def test_unknown_source_alongside_a_known_one() -> None:
    sources = [{"url": ""}, {"url": "https://x.example.com/"}]
    assert independent_groups(sources) == [[0], [1]]
    assert count_independent(sources) == 1


def test_no_url_key_at_all() -> None:
    sources = [{"text": _words("m", 60)}, {"url": "https://y.example.com/"}]
    assert independent_groups(sources) == [[0], [1]]
    assert count_independent(sources) == 2


def test_no_sources() -> None:
    assert independent_groups([]) == []
    assert count_independent([]) == 0


# ---------------------------------------------------------------------------------------------
# 8. thresholds
# ---------------------------------------------------------------------------------------------


def _half_shared_pair() -> tuple[str, str]:
    a = _words("a", 100)
    b = _words("a", 70) + " " + _words("b", 30)
    return a, b


def test_about_half_shared_shingles_is_not_a_near_duplicate_at_0_6() -> None:
    a, b = _half_shared_pair()
    sim = similarity(a, b)
    assert 0.4 < sim < 0.6
    assert not near_duplicate(a, b, 0.6)
    assert not near_duplicate(a, b)  # the default threshold is 0.6


def test_the_same_pair_is_a_near_duplicate_at_0_4() -> None:
    a, b = _half_shared_pair()
    assert near_duplicate(a, b, 0.4)


# ---------------------------------------------------------------------------------------------
# 9. order
# ---------------------------------------------------------------------------------------------


def test_groups_are_sorted_and_ordered_by_smallest_index() -> None:
    sources = [
        {"url": "https://a.example.com/1"},
        {"url": "https://b.example.org/2"},
        {"url": "https://www.a.example.com/3"},
        {"url": "https://c.example.net/4"},
        {"url": "https://b.example.org/5"},
    ]
    groups = independent_groups(sources)
    assert groups == [[0, 2], [1, 4], [3]]
    assert count_independent(sources) == 3
    assert all(g == sorted(g) for g in groups)
    assert [g[0] for g in groups] == sorted(g[0] for g in groups)

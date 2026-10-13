"""Web tools (§9-12): deterministic HTML/result parsing, input guards, provenance shape."""

from __future__ import annotations

import pytest

from sali.tools.builtins.web import (
    WebFetch,
    WebSearch,
    html_to_text,
    parse_ddg,
    real_url,
    title_of,
)
from sali.tools.context import local_context

_SAMPLE = """
<html><head><title>  Ripgrep &mdash; docs </title><style>.x{color:red}</style></head>
<body><script>var a=1;</script><h1>Ripgrep</h1>
<p>ripgrep is a  line-oriented   search tool.</p></body></html>
"""

_DDG = """
<div class="result results_links">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Frg&rut=z">rip<b>grep</b> home</a>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">A fast  <b>search</b> tool.</a>
</div>
<div class="result results_links">
  <a class="result__a" href="https://docs.rs/ripgrep">docs.rs ripgrep</a>
  <a class="result__snippet" href="#">API docs.</a>
</div>
"""


def test_html_to_text_strips_scripts_tags_and_collapses() -> None:
    text = html_to_text(_SAMPLE)
    assert "Ripgrep" in text and "line-oriented   search" not in text  # whitespace collapsed
    assert "var a=1" not in text and "color:red" not in text  # script/style dropped
    assert "<" not in text  # no tags remain


def test_title_of_extracts_and_unescapes() -> None:
    assert title_of(_SAMPLE) == "Ripgrep — docs"
    assert title_of("<html><body>no title</body></html>") == ""


def test_real_url_unwraps_duckduckgo_redirect() -> None:
    wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage%3Fa%3D1&rut=abc"
    assert real_url(wrapped) == "https://example.com/page?a=1"
    assert real_url("https://plain.example.com/x") == "https://plain.example.com/x"  # passthrough


def test_parse_ddg_extracts_results_with_provenance() -> None:
    results = parse_ddg(_DDG)
    assert len(results) == 2
    assert results[0]["url"] == "https://example.com/rg"
    assert results[0]["domain"] == "example.com"
    assert results[0]["title"] == "ripgrep home"  # tags stripped
    assert "fast" in results[0]["snippet"] and "search tool" in results[0]["snippet"]
    assert results[1]["domain"] == "docs.rs"


async def test_web_fetch_rejects_non_http_scheme() -> None:
    res = await WebFetch().run({"url": "file:///etc/passwd"}, local_context())
    assert not res.ok and "http" in (res.error or "").lower()  # only http/https, no file:// etc.


async def test_web_search_requires_a_query() -> None:
    res = await WebSearch().run({"query": "   "}, local_context())
    assert not res.ok and "query" in (res.error or "").lower()


# ---- live network (opt-in, excluded from CI) --------------------------------------------------
@pytest.mark.live
async def test_web_search_and_fetch_live() -> None:
    search = await WebSearch().run({"query": "ripgrep github", "max_results": 3}, local_context())
    assert search.ok and search.output["results"]
    top = search.output["results"][0]
    assert top["url"].startswith("http") and top["domain"]
    fetched = await WebFetch().run({"url": top["url"]}, local_context())
    assert fetched.ok and fetched.output["content"]
    assert fetched.output["domain"] and fetched.output["retrieved_at"]  # provenance present

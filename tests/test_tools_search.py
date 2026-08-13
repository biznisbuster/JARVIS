"""Unit testovi za robusni DuckDuckGo parser u web_search (Faza 6, T1)."""

from jarvis.agent.tools import _ddg_resolve_url, _extract_ddg_results

DDG_HTML_SAMPLE = """
<div class="result">
  <h2 class="result__title">
    <a rel="nofollow" class="result__a"
       href="//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fasync%2Dio%2Dpython%2F&amp;rut=abc123">
      Python&#x27;s asyncio: A Hands-On Walkthrough - Real Python</a>
  </h2>
  <a class="result__snippet" href="//duckduckgo.com/l/?uddg=x">
    Python&#x27;s asyncio library <b>enables</b> concurrent code.</a>
</div>
<div class="result">
  <h2 class="result__title">
    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fwww.geeksforgeeks.org%2Fpython%2F&amp;rut=def"
       rel="nofollow" class="result__a">asyncio in Python - GeeksforGeeks</a>
  </h2>
  <a class="result__snippet" href="y">Asyncio is a foundation for frameworks.</a>
</div>
<div><a href="/about" class="footer-link">About DuckDuckGo</a></div>
"""

DDG_LITE_SAMPLE = """
<table cellpadding="0" cellspacing="0">
  <tr><td>
    <a rel="nofollow" href="https://example.com/page" class='result-link'>Example Title</a>
  </td></tr>
  <tr><td class='result-snippet'>Example snippet text here.</td></tr>
  <tr><td>
    <a rel="nofollow" href="https://second.example.org/" class='result-link'>Second Result</a>
  </td></tr>
  <tr><td class='result-snippet'>Second snippet.</td></tr>
</table>
"""


def test_resolve_url_ddg_redirect() -> None:
    href = "//duckduckgo.com/l/?uddg=https%3A%2F%2Frealpython.com%2Fasync%2Dio%2Dpython%2F&rut=abc"
    assert _ddg_resolve_url(href) == "https://realpython.com/async-io-python/"


def test_resolve_url_plain() -> None:
    assert _ddg_resolve_url("https://example.com/x") == "https://example.com/x"


def test_resolve_url_empty() -> None:
    assert _ddg_resolve_url("") == ""


def test_extract_html_endpoint() -> None:
    results = _extract_ddg_results(DDG_HTML_SAMPLE, 5)
    assert len(results) == 2
    first = results[0]
    assert first["title"] == "Python's asyncio: A Hands-On Walkthrough - Real Python"
    assert first["url"] == "https://realpython.com/async-io-python/"
    assert "enables" in first["snippet"]
    assert "<b>" not in first["snippet"]
    assert results[1]["url"] == "https://www.geeksforgeeks.org/python/"


def test_extract_html_attribute_order_swapped() -> None:
    # href PRE class — stari regex je padao na ovome
    results = _extract_ddg_results(DDG_HTML_SAMPLE, 5)
    assert results[1]["title"].startswith("asyncio in Python")


def test_extract_ignores_non_result_anchors() -> None:
    results = _extract_ddg_results(DDG_HTML_SAMPLE, 5)
    assert all("About DuckDuckGo" != r["title"] for r in results)


def test_extract_lite_endpoint() -> None:
    results = _extract_ddg_results(DDG_LITE_SAMPLE, 5)
    assert len(results) == 2
    assert results[0] == {
        "title": "Example Title",
        "snippet": "Example snippet text here.",
        "url": "https://example.com/page",
    }
    assert results[1]["url"] == "https://second.example.org/"


def test_extract_max_results() -> None:
    results = _extract_ddg_results(DDG_LITE_SAMPLE, 1)
    assert len(results) == 1
    assert results[0]["title"] == "Example Title"


def test_extract_title_without_snippet() -> None:
    html = '<a class="result__a" href="https://solo.example/">Solo Title</a>'
    results = _extract_ddg_results(html, 5)
    assert results == [{"title": "Solo Title", "snippet": "", "url": "https://solo.example/"}]


def test_extract_garbage_returns_empty() -> None:
    assert _extract_ddg_results("<html><body>nema rezultata</body></html>", 5) == []
    assert _extract_ddg_results("", 5) == []


def test_extract_anomaly_page_returns_empty() -> None:
    anomaly = '<div id="anomaly-modal">Please confirm you are human</div>'
    assert _extract_ddg_results(anomaly, 5) == []

import csv
import io
import json

import pytest
from openpyxl import load_workbook

from ai_scraper.evaluate import load_fixtures, score
from ai_scraper.export import export
from ai_scraper.fetch import DictFetcher, Fetcher, FetchError
from ai_scraper.pipeline import Scraper
from ai_scraper.schema import Field, Schema


def fixture(name):
    return next(f for f in load_fixtures() if f.name == name)


class OracleLLM:
    """Stands in for ClaudeExtractor: returns the gold rows present on the page it is shown."""

    def __init__(self, fx):
        from ai_scraper.cache import Usage

        self.fx, self.usage, self.calls = fx, Usage(), 0

    def extract(self, schema, chunks, url, request=""):
        self.calls += 1
        text = "\n".join(c.text for c in chunks)
        return [r for r in self.fx.records if str(r[schema.names[0]])[:25] in text], None


def test_auto_mode_picks_extractor():
    s = Schema([Field("a")])
    assert Scraper(s, examples=[{"a": 1}]).mode == "induction"
    assert Scraper(s, llm=object()).mode == "hybrid"
    with pytest.raises(ValueError):
        Scraper(s, "induction")


def test_follows_pagination_and_stops_at_the_last_page():
    fx = fixture("books")
    res = Scraper(fx.schema, "induction", examples=fx.examples, fetcher=DictFetcher(fx.pages), max_pages=10)\
        .run(fx.start_url)
    assert len(res.pages) == 3 and res.pages[-1].next_url is None
    assert len(res.rows) == len(fx.records)
    assert res.wrapper is not None and res.preview_html.count("data-ais-record") >= 12


def test_fetch_error_is_reported_and_earlier_pages_are_kept():
    fx = fixture("books")
    first = {fx.start_url: fx.pages[fx.start_url]}
    res = Scraper(fx.schema, "heuristic", fetcher=DictFetcher(first), max_pages=3).run(fx.start_url)
    assert len(res.rows) == 12 and res.pages[-1].error


def test_hybrid_learns_a_wrapper_from_claude_and_stops_calling_it():
    fx = fixture("books")
    llm = OracleLLM(fx)
    res = Scraper(fx.schema, "hybrid", llm=llm, fetcher=DictFetcher(fx.pages), max_pages=3).run(fx.start_url)
    assert [p.method for p in res.pages] == ["claude", "wrapper", "wrapper"]
    assert llm.calls == 1
    assert score(res.rows, fx.records, fx.schema).total.f1 == 1.0


def test_claude_mode_calls_once_per_page():
    fx = fixture("quotes")
    llm = OracleLLM(fx)
    res = Scraper(fx.schema, "claude", llm=llm, fetcher=DictFetcher(fx.pages), max_pages=5).run(fx.start_url)
    assert llm.calls == len(fx.pages) == len(res.pages)


def test_fetcher_respects_robots_and_delay():
    class R:
        def __init__(self, status, text):
            self.status_code, self.text, self.content, self.encoding, self.apparent_encoding = \
                status, text, text.encode(), "utf-8", "utf-8"

        def raise_for_status(self):
            pass

    class Session:
        headers: dict = {}

        def get(self, url, timeout):
            return R(200, "User-agent: *\nDisallow: /private") if url.endswith("robots.txt") else R(200, "<p>hi</p>")

    sleeps = []
    f = Fetcher(session=Session(), delay=5, sleep=sleeps.append)
    assert f("https://s.test/a") == "<p>hi</p>"
    f("https://s.test/b")
    assert sleeps and sleeps[0] > 4
    with pytest.raises(FetchError):
        f("https://s.test/private/x")
    with pytest.raises(FetchError):
        f("file:///etc/passwd")


ROWS = [{"title": "=HYPERLINK(\"x\")", "price": 1299.5, "ok": True, "url": "https://s.test/a"},
        {"title": "Café £", "price": None, "ok": False, "url": None}]
S = Schema([Field("title"), Field("price", "number"), Field("ok", "boolean"), Field("url", "url")])


def test_csv_export_has_bom_and_blocks_formula_injection():
    data = export(ROWS, S, "csv")
    assert data.startswith(b"\xef\xbb\xbf")
    rows = list(csv.reader(io.StringIO(data.decode("utf-8-sig"))))
    assert rows[0] == S.names and rows[1][0].startswith("'=") and rows[2][:3] == ["Café £", "", "no"]


def test_json_export_keeps_types_and_order():
    assert json.loads(export(ROWS, S, "json"))[1] == {"title": "Café £", "price": None, "ok": False, "url": None}


def test_excel_export_is_typed_and_linked():
    ws = load_workbook(io.BytesIO(export(ROWS, S, "xlsx"))).active
    assert [c.value for c in ws[1]] == S.names
    assert ws["B2"].value == 1299.5 and ws["B2"].number_format == "#,##0.00"
    assert ws["D2"].hyperlink is not None and ws.freeze_panes == "A2"


def test_unknown_export_format():
    with pytest.raises(ValueError):
        export(ROWS, S, "pdf")

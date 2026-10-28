"""
Offline tests for ai_scraper.py. A fake Claude client and fake web pages are used,
so no internet connection or API key is needed.

Run from the project folder:
    python -m unittest discover tests
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import ai_scraper  # noqa: E402

PAGE_1 = """<html><head><title>Shop</title><script>var x=1</script></head><body>
<nav><a href="/">Home</a></nav>
<article><h3><a href="/item/a">Alpha Book</a></h3><p class="star-rating Three"></p><p>£12.50</p></article>
<article><h3><a href="/item/b">Beta Book</a></h3><p class="star-rating One"></p><p>£8.00</p></article>
<li class="next"><a href="page-2.html">next</a></li>
</body></html>"""

PAGE_2 = """<html><body>
<article><h3><a href="/item/c">Gamma Book</a></h3><p>£20.00</p></article>
</body></html>"""


class FakeHTTP:
    """Serves the fake pages above and records which URLs were requested."""

    def __init__(self, robots="User-agent: *\nAllow: /"):
        self.headers = {}
        self.requested = []
        self.pages = {
            "https://shop.test/robots.txt": robots,
            "https://shop.test/": PAGE_1,
            "https://shop.test/page-2.html": PAGE_2,
        }

    def get(self, url, timeout=None):
        self.requested.append(url)
        body = self.pages.get(url)
        return NS(status_code=200 if body is not None else 404, text=body or "",
                  apparent_encoding="utf-8", encoding="utf-8",
                  raise_for_status=lambda: None)


class FakeClaude:
    """Answers the 'design fields' request and then one extraction per page."""

    def __init__(self, extractions):
        self.requests = []
        self.extractions = list(extractions)
        self.beta = NS(messages=NS(create=self._create))

    def _create(self, **kwargs):
        self.requests.append(kwargs)
        if "design table columns" in kwargs["system"]:
            answer = {"fields": [
                {"name": "title", "type": "string", "description": "Book title"},
                {"name": "price", "type": "number", "description": "Price without the £ sign"},
            ]}
        else:
            answer = self.extractions.pop(0)
        return NS(
            stop_reason="end_turn",
            content=[NS(type="text", text=json.dumps(answer))],
            usage=NS(input_tokens=100, output_tokens=20, cache_read_input_tokens=0, cache_creation_input_tokens=0),
        )


def run_scrape(extractions, max_pages=2, robots="User-agent: *\nAllow: /"):
    http = FakeHTTP(robots)
    claude = FakeClaude(extractions)
    result = ai_scraper.scrape("https://shop.test/", "book title and price", max_pages=max_pages,
                               client=claude, session=http, log=lambda *a: None, delay=0)
    return result, claude, http


class PageToTextTest(unittest.TestCase):
    def test_keeps_links_and_ratings_and_drops_scripts(self):
        text = ai_scraper.page_to_text(PAGE_1, "https://shop.test/")
        self.assertIn("[Alpha Book](https://shop.test/item/a)", text)
        self.assertIn("star-rating Three", text)
        self.assertIn("[next](https://shop.test/page-2.html)", text)
        self.assertNotIn("var x", text)

    def test_chunks_split_long_text(self):
        text = "\n".join("line %d" % i for i in range(1000))
        parts = ai_scraper.chunks(text, size=500)
        self.assertTrue(all(len(p) <= 500 for p in parts))
        self.assertEqual("".join(parts), text)


class ScrapeTest(unittest.TestCase):
    def test_follows_pagination_and_merges_rows(self):
        result, claude, http = run_scrape([
            {"items": [{"title": "Alpha Book", "price": 12.5}, {"title": "Beta Book", "price": 8.0}],
             "next_page_url": "page-2.html"},
            {"items": [{"title": "Gamma Book", "price": 20.0}], "next_page_url": None},
        ])
        self.assertEqual([r["title"] for r in result.rows], ["Alpha Book", "Beta Book", "Gamma Book"])
        self.assertEqual(result.pages, ["https://shop.test/", "https://shop.test/page-2.html"])
        self.assertEqual(result.usage.requests, 3)

    def test_extraction_uses_strict_json_schema(self):
        _, claude, _ = run_scrape([{"items": [], "next_page_url": None}])
        schema = claude.requests[1]["output_config"]["format"]["schema"]
        item = schema["properties"]["items"]["items"]
        self.assertEqual(item["required"], ["title", "price"])
        self.assertEqual(item["properties"]["price"]["type"], ["number", "null"])
        self.assertFalse(item["additionalProperties"])

    def test_removes_duplicate_rows(self):
        row = {"title": "Alpha Book", "price": 12.5}
        result, _, _ = run_scrape([
            {"items": [row, row], "next_page_url": "page-2.html"},
            {"items": [row], "next_page_url": None},
        ])
        self.assertEqual(result.rows, [row])

    def test_stops_at_max_pages(self):
        result, _, http = run_scrape([{"items": [], "next_page_url": "page-2.html"}], max_pages=1)
        self.assertEqual(len(result.pages), 1)
        self.assertNotIn("https://shop.test/page-2.html", http.requested)

    def test_stops_on_links_to_other_sites(self):
        result, _, _ = run_scrape([{"items": [], "next_page_url": "https://other.test/page-2"}])
        self.assertEqual(len(result.pages), 1)

    def test_respects_robots_txt(self):
        with self.assertRaises(ai_scraper.ScraperError):
            run_scrape([], robots="User-agent: *\nDisallow: /")


class SaveTest(unittest.TestCase):
    def setUp(self):
        self.result = ai_scraper.ScrapeResult(
            fields=[ai_scraper.Field("title", "string", "t"), ai_scraper.Field("price", "number", "p")],
            rows=[{"title": "Алфа", "price": 12.5}, {"title": "Beta", "price": None}],
            pages=["https://shop.test/"],
        )
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_json_and_csv(self):
        ai_scraper.save(self.result, self.dir / "out.json")
        self.assertEqual(json.loads((self.dir / "out.json").read_text(encoding="utf-8"))[0]["title"], "Алфа")
        ai_scraper.save(self.result, self.dir / "out.csv")
        self.assertIn("title,price", (self.dir / "out.csv").read_text(encoding="utf-8-sig"))

    def test_excel(self):
        from openpyxl import load_workbook
        ai_scraper.save(self.result, self.dir / "out.xlsx")
        wb = load_workbook(self.dir / "out.xlsx")
        self.assertEqual([c.value for c in wb["Data"][1]], ["title", "price"])
        self.assertEqual(wb["Data"]["B2"].value, 12.5)
        self.assertEqual(wb.sheetnames, ["Data", "Columns"])

    def test_rejects_unknown_format(self):
        with self.assertRaises(ai_scraper.ScraperError):
            ai_scraper.save(self.result, self.dir / "out.txt")


if __name__ == "__main__":
    unittest.main()

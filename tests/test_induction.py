import pytest

from ai_scraper.dom import parse
from ai_scraper.induction import InductionError, Wrapper, detect_regions, induce, induce_heuristic
from ai_scraper.schema import Field, Schema

BASE = "https://shop.test/cat/"


def schema(books_schema):
    books_schema.base_url = BASE
    return books_schema


def test_detects_the_product_list_not_the_nav(listing):
    regions = detect_regions(parse(listing))
    assert regions and len(regions[0].records) == 4
    assert regions[0].records[0].nodes[0].tag == "li"


def test_induction_from_two_examples_reads_attributes_and_classes(listing, books_schema):
    s = schema(books_schema)
    examples = [{"title": "Alpha Book", "price": 10.5, "rating": 3, "in_stock": True, "url": BASE.replace("/cat/", "/p/1")},
                {"title": "Beta Book", "price": 7.25, "rating": 1, "in_stock": False, "url": "https://shop.test/p/2"}]
    w = induce(parse(listing), s, examples)
    rows, region = w.extract(parse(listing), s)
    assert len(rows) == 4
    assert rows[2] == {"title": "Gamma Book", "price": 1299.0, "rating": 5, "in_stock": True,
                       "url": "https://shop.test/p/3"}
    rules = {r.name: r for r in w.rules}
    assert rules["title"].slot.source == "attr:title"     # full title lives in a@title, not the link text
    assert rules["rating"].slot.source == "attr:class"  # "star-rating Three" -> 3


def test_induction_with_one_example(listing):
    s = Schema([Field("title"), Field("price", "number")], base_url=BASE)
    w = induce(parse(listing), s, [{"title": "Alpha Book", "price": 10.5}])
    rows, _ = w.extract(parse(listing), s)
    assert [r["price"] for r in rows] == [10.5, 7.25, 1299.0, 3.0]


def test_induction_rejects_examples_not_on_the_page(listing):
    s = Schema([Field("title"), Field("price", "number")])
    with pytest.raises(InductionError):
        induce(parse(listing), s, [{"title": "Not here", "price": 99999}])


def test_wrapper_round_trips_and_applies_to_a_new_page(listing, books_schema):
    s = schema(books_schema)
    w = induce_heuristic(parse(listing), s)
    w2 = Wrapper.from_dict(w.to_dict())
    page2 = listing.replace("Alpha", "Omega").replace("£10.50", "£11.00")
    rows, _ = w2.extract(parse(page2), s)
    assert rows[0]["price"] == 11.0 and "Omega" in rows[0]["title"]


def test_heuristic_matches_columns_by_name_and_type(listing, books_schema):
    s = schema(books_schema)
    rows, _ = induce_heuristic(parse(listing), s).extract(parse(listing), s)
    assert len(rows) == 4
    assert [r["price"] for r in rows] == [10.5, 7.25, 1299.0, 3.0]
    assert [r["in_stock"] for r in rows] == [True, False, True, True]
    assert rows[0]["url"] == "https://shop.test/p/1"


def test_heuristic_reads_table_columns_by_header():
    rows_html = "".join(f"<tr><td>City {i}</td><td>{1000 * i:,}</td><td>{i}.5</td></tr>" for i in range(1, 7))
    html = f"<table><thead><tr><th>Name</th><th>Population</th><th>Area</th></tr></thead><tbody>{rows_html}</tbody></table>"
    s = Schema([Field("name"), Field("area", "number"), Field("population", "integer")])
    rows, _ = induce_heuristic(parse(html), s).extract(parse(html), s)
    assert rows[1] == {"name": "City 2", "area": 2.5, "population": 2000}


def test_sponsored_cards_are_not_records():
    cards = "".join(f'<div class="card"><h2>Item {i}</h2><span class="price">${i}.00</span></div>' for i in range(5))
    ad = '<div class="card sponsored"><h2>Buy this</h2><span class="price">$1.00</span></div>'
    html = f"<main>{cards[:120]}{ad}{cards[120:]}</main>"
    s = Schema([Field("name"), Field("price", "number")])
    rows, _ = induce(parse(html), s, [{"name": "Item 0", "price": 0}, {"name": "Item 1", "price": 1}]).extract(parse(html), s)
    assert "Buy this" not in [r["name"] for r in rows]
    assert len(rows) == 5


def test_multi_row_records_hacker_news_style():
    rows = "".join(f'<tr class="athing"><td class="title"><a href="/i{i}">Story {i}</a></td></tr>'
                   f'<tr><td class="subtext"><span class="score">{10 + i} points</span> by u{i}</td></tr>'
                   for i in range(6))
    html = f"<table>{rows}</table>"
    s = Schema([Field("title"), Field("points", "integer")], base_url="https://n.test/")
    w = induce(parse(html), s, [{"title": "Story 0", "points": 10}, {"title": "Story 1", "points": 11}])
    out, _ = w.extract(parse(html), s)
    assert w.period == 2
    assert out[5] == {"title": "Story 5", "points": 15}

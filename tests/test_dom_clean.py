from ai_scraper.clean import blocks_for, chunk_blocks, estimate_tokens, to_text
from ai_scraper.dom import parse, serialize
from ai_scraper.induction import detect_regions


def test_parse_handles_broken_html_and_entities():
    root = parse("<div><p>Fish &amp; chips<p>Second<br>line</div><li>open")
    assert "Fish & chips" in root.text()
    assert [n.tag for n in root.iter() if n.tag == "p"] == ["p", "p"]


def test_serialize_drops_scripts_and_event_handlers():
    html = serialize(parse('<div onclick="x()"><script>bad()</script><a href="/a" onmouseover="y">ok</a></div>'))
    assert "script" not in html and "onclick" not in html and "onmouseover" not in html
    assert 'href="/a"' in html


def test_serialize_annotates_nodes():
    root = parse("<ul><li>a</li></ul>")
    li = next(n for n in root.iter() if n.tag == "li")
    assert 'data-x="1"' in serialize(root, {li.nid: {"data-x": "1"}})


def test_to_text_keeps_links_absolute_and_drops_boilerplate(listing):
    text = to_text(parse(listing), "https://shop.test/cat/")
    assert "[Alpha (title: Alpha Book)](https://shop.test/p/1)" in text
    assert "alert(1)" not in text and "Copyright" not in text and "About" not in text


def test_to_text_exposes_attribute_values(listing):
    text = to_text(parse(listing), "https://shop.test/")
    assert "Three" in text  # the rating lives only in a class name


def test_estimate_tokens_monotonic():
    assert estimate_tokens("") == 0
    assert estimate_tokens("word " * 10) < estimate_tokens("word " * 100)


def test_blocks_keep_each_record_whole(listing):
    root = parse(listing)
    region = detect_regions(root)[0]
    blocks = blocks_for(root, "https://shop.test/", [r.nodes for r in region.records])
    records = [t for t, is_rec in blocks if is_rec]
    assert len(records) == 4 and all("£" in r for r in records)


def test_chunking_respects_budget_and_never_splits_a_record():
    blocks = [(f"record {i} " + "x " * 60, True) for i in range(20)]
    chunks = chunk_blocks(blocks, 300)
    assert len(chunks) > 1
    assert all(c.tokens <= 300 for c in chunks)
    joined = "\n\n".join(c.text for c in chunks)
    assert all(f"record {i} " in joined for i in range(20))
    assert sum(c.records for c in chunks) == 20


def test_chunking_splits_an_oversized_block_on_lines():
    big = "\n".join(f"line {i} " + "y " * 30 for i in range(40))
    chunks = chunk_blocks([(big, False)], 200)
    assert len(chunks) > 1 and all(c.tokens <= 200 for c in chunks)

import pytest
from conftest import FakeClient, response

from ai_scraper.cache import ResponseCache, Usage, cache_key, cost_usd
from ai_scraper.clean import Chunk
from ai_scraper.llm import ClaudeExtractor, ExtractionError
from ai_scraper.schema import Field, Schema

S = Schema([Field("name"), Field("price", "number")], "products")
CHUNK = Chunk("Widget £3.50\nGadget £4.00", 12)


def extractor(replies, **kw):
    return ClaudeExtractor(client=FakeClient(replies), model="claude-opus-5-5", **kw)


def test_request_uses_structured_outputs_and_cached_system_prompt():
    llm = extractor([response({"items": [{"name": "Widget", "price": 3.5}], "next_page_url": None})])
    rows, nxt = llm.extract(S, [CHUNK], "https://s.test/")
    call = llm.client.calls[0]
    assert call["output_config"]["format"]["type"] == "json_schema"
    assert call["output_config"]["format"]["schema"] == S.json_schema()
    assert call["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert "temperature" not in call and "thinking" not in call
    assert rows == [{"name": "Widget", "price": 3.5}] and nxt is None


def test_values_are_coerced_after_the_model_answers():
    llm = extractor([response({"items": [{"name": " Widget ", "price": "£3.50"}], "next_page_url": "/p2"})])
    rows, nxt = llm.extract(S, [CHUNK])
    assert rows == [{"name": "Widget", "price": 3.5}] and nxt == "/p2"


def test_invalid_rows_trigger_a_repair_turn():
    llm = extractor([response({"items": [{"name": "Widget", "price": "unknown"}], "next_page_url": None}),
                     response({"items": [{"name": "Widget", "price": 3.5}], "next_page_url": None})])
    rows, _ = llm.extract(S, [CHUNK])
    assert rows == [{"name": "Widget", "price": 3.5}]
    assert llm.usage.repairs == 1 and len(llm.client.calls) == 2
    repair = llm.client.calls[1]["messages"][-1]["content"]
    assert "price" in repair and "problems" in repair


def test_max_tokens_splits_the_chunk_and_retries_halves():
    big = Chunk("\n\n".join(f"Item {i} £{i}.00 " + "detail " * 40 for i in range(20)), 1200)

    def reply(kw):
        text = kw["messages"][0]["content"]
        if text.count("Item ") > 10:
            return response({"items": []}, stop="max_tokens")
        return response({"items": [{"name": f"Item {i}", "price": i} for i in range(20) if f"Item {i} " in text],
                         "next_page_url": None})

    llm = extractor(reply)
    rows, _ = llm.extract(S, [big])
    assert len(rows) == 20 and len(llm.client.calls) == 3


def test_refusal_raises():
    llm = extractor([response({"items": []}, stop="refusal")])
    with pytest.raises(ExtractionError):
        llm.extract(S, [CHUNK])


def test_cache_hit_skips_the_api_and_counts_savings():
    cache = ResponseCache()
    payload = {"items": [{"name": "Widget", "price": 3.5}], "next_page_url": None}
    first = extractor([response(payload, inp=1000, out=200)], cache=cache)
    first.extract(S, [CHUNK], "https://s.test/")
    second = extractor([], cache=cache)
    rows, _ = second.extract(S, [CHUNK], "https://s.test/")
    assert rows == payload["items"] and second.client.calls == []
    assert second.usage.cache_hits == 1 and second.usage.saved_tokens == 1200
    assert second.usage.saved_cost == pytest.approx(cost_usd("claude-opus-5-5", 1000, 200))


def test_changed_page_content_misses_the_cache():
    cache = ResponseCache()
    p = {"items": [], "next_page_url": None}
    a = extractor([response(p)], cache=cache)
    a.extract(S, [CHUNK])
    b = extractor([response(p)], cache=cache)
    b.extract(S, [Chunk(CHUNK.text.replace("3.50", "3.75"), 12)])
    assert len(b.client.calls) == 1 and len(cache) == 2


def test_cache_persists_in_sqlite(tmp_path):
    path = tmp_path / "c.sqlite"
    ResponseCache(path).put("k", "m", {"items": [1]}, {"input_tokens": 5})
    assert ResponseCache(path).get("k") == ({"items": [1]}, {"input_tokens": 5})


def test_cost_and_usage_accounting():
    assert cost_usd("claude-opus-5-5", 1_000_000, 0) == pytest.approx(4.0)
    assert cost_usd("claude-opus-5-5", 0, 1_000_000) == pytest.approx(20.0)
    u = Usage()
    u.add("claude-opus-5-5", {"input_tokens": 1000, "output_tokens": 100, "cache_read_input_tokens": 500})
    assert u.requests == 1 and u.cost == pytest.approx((1000 * 4 + 100 * 20 + 500 * 0.2) / 1e6)


def test_cache_key_depends_on_every_part():
    assert cache_key(a=1, b="x") == cache_key(b="x", a=1)
    assert cache_key(a=1, b="x") != cache_key(a=1, b="y")


def test_design_schema_builds_typed_fields():
    llm = extractor([response({"fields": [{"name": "title", "type": "string", "description": "book title"},
                                          {"name": "price", "type": "number", "description": "price"}]})])
    s = llm.design_schema("book titles and prices")
    assert s.names == ["title", "price"] and s.fields[1].type == "number"

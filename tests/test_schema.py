import pytest

from ai_scraper.schema import (
    Field,
    Schema,
    SchemaError,
    clean_rows,
    coerce,
    parse_bool,
    parse_numbers,
    schema_from_text,
)


def test_parse_numbers_handles_separators_suffixes_and_words():
    assert parse_numbers("£1,299.50") == [1299.5]
    assert parse_numbers("1.2k stars, 3M views") == [1200.0, 3_000_000.0]
    assert parse_numbers("Three stars") == [3.0]


def test_parse_bool_checks_negatives_first():
    assert parse_bool("Out of stock") is False
    assert parse_bool("In stock (22 available)") is True
    assert parse_bool("maybe") is None


@pytest.mark.parametrize("value,ftype,expected", [
    ("£12.50", "number", 12.5), ("4.6 out of 5", "integer", 5), ("Sold out", "boolean", False),
    ("/p/1", "url", "https://shop.test/p/1"), ("  spaced   text ", "string", "spaced text"), ("n/a", "number", None),
])
def test_coerce(value, ftype, expected):
    assert coerce(value, ftype, "https://shop.test/cat/") == expected


def test_schema_validation():
    with pytest.raises(SchemaError):
        Field("Bad Name")
    with pytest.raises(SchemaError):
        Field("ok", "date")
    with pytest.raises(SchemaError):
        Schema([Field("a"), Field("a")])


def test_json_schema_is_strict(books_schema):
    js = books_schema.json_schema()
    item = js["properties"]["items"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(books_schema.names)


def test_clean_rows_coerces_and_drops_empty(books_schema):
    rows, problems = clean_rows([{"title": " A ", "price": "£3", "extra": 1}, {"title": None}, "junk"], books_schema)
    assert rows == [{"title": "A", "price": 3.0, "rating": None, "in_stock": None, "url": None}]
    assert problems


def test_schema_from_text_guesses_types():
    s = schema_from_text("book title, price in GBP, star rating (1-5), in stock (yes/no), product link")
    assert [f.type for f in s.fields] == ["string", "number", "integer", "boolean", "url"]
    assert len(set(s.names)) == 5

import pytest

from ai_scraper.evaluate import Counts, evaluate_fixture, load_fixtures, same, score, summarize
from ai_scraper.schema import Field, Schema

S = Schema([Field("name"), Field("price", "number"), Field("ok", "boolean")])
GOLD = [{"name": "A", "price": 1.0, "ok": True}, {"name": "B", "price": 2.0, "ok": None}]


def test_same_is_type_aware():
    assert same(3, 3.0, "number") and same("  Hello  World", "hello world", "string")
    assert same("https://x.test/a/", "https://x.test/a", "url")
    assert not same(None, 0, "number") and same(None, None, "string")
    assert same("“Talk is cheap.”", "Talk is cheap.", "string") and same("a,b", "a, b", "string")
    assert not same("Talk is cheap", "Talk is cheap.", "string")


def test_perfect_prediction_scores_one():
    s = score(GOLD, GOLD, S)
    assert (s.total.precision, s.total.recall, s.total.f1) == (1.0, 1.0, 1.0)
    assert s.total.tp == 5  # null gold cells are not counted


def test_wrong_cell_is_fp_and_fn_and_missing_row_is_fn():
    s = score([{"name": "A", "price": 9.0, "ok": True}], GOLD, S)
    assert s.per_field["price"].fp == 1 and s.per_field["price"].fn == 2
    assert s.per_field["name"].tp == 1 and s.per_field["name"].fn == 1
    assert s.matched_rows == 1


def test_rows_are_aligned_regardless_of_order_and_extra_rows_cost_precision():
    s = score([{"name": "Z", "price": 5.0, "ok": False}] + GOLD[::-1], GOLD, S)
    assert s.total.recall == 1.0 and s.total.precision == pytest.approx(5 / 8)


def test_examples_are_excluded_from_both_sides():
    s = score(GOLD, GOLD, S, exclude=[GOLD[0]])
    assert s.gold_rows == 1 and s.total.tp == 2


def test_counts_handle_zero():
    assert Counts().f1 == 0.0


def test_fixtures_load_with_gold_and_pages():
    fxs = load_fixtures()
    assert len(fxs) >= 14
    assert sum(f.name.startswith("holdout") for f in fxs) == 4
    for f in fxs:
        assert f.records and f.pages and set(f.records[0]) == set(f.schema.names)


def test_induction_reaches_full_marks_on_a_dev_fixture_across_pages():
    fx = next(f for f in load_fixtures() if f.name == "books")
    r = evaluate_fixture(fx, "induction-2")
    assert r["pages"] == 3 and r["f1"] == 1.0
    assert summarize([r])["induction-2@dev"]["f1"] == 1.0

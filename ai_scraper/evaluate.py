"""Field-level evaluation of extractors against saved pages with hand-checked ground truth.

Scoring works on cells, not rows, because a row with 5 of 6 fields right is mostly useful and should not count
as a total miss. Predicted rows are aligned to gold rows by maximum cell agreement (greedy, ties by order), then:

- a predicted non-null cell equal to the gold cell is a true positive,
- a predicted non-null cell that differs, or sits in an unaligned row, is a false positive,
- a gold non-null cell that was not predicted correctly is a false negative.

Precision = TP / (TP + FP), recall = TP / (TP + FN), F1 = harmonic mean, micro-averaged per fixture and overall.
Labelled example records used by wrapper induction are excluded from scoring for every extractor alike.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .dom import norm_space
from .schema import Schema

FIXTURES = Path(__file__).resolve().parent.parent / "fixtures"


def same(a, b, ftype: str) -> bool:
    if a is None or b is None:
        return a is None and b is None
    if ftype in ("number", "integer"):
        try:
            return abs(float(a) - float(b)) <= 1e-6 * max(1.0, abs(float(b)))
        except (TypeError, ValueError):
            return False
    if ftype == "boolean":
        return a is b or a == b
    if ftype == "url":
        return str(a).strip().rstrip("/") == str(b).strip().rstrip("/")
    return _text(a) == _text(b)


QUOTES = "\"'“”‘’«»„"


def _text(v) -> str:
    """Case, whitespace, wrapping quotation marks and the spacing after list commas are formatting, not content:
    '“Talk is cheap.”' == 'Talk is cheap.' and 'a,b' == 'a, b'. Everything else must match exactly."""
    s = norm_space(str(v)).casefold().strip(QUOTES).strip()
    return re.sub(r"\s*,\s*", ", ", s)


@dataclass
class Counts:
    tp: int = 0
    fp: int = 0
    fn: int = 0

    def add(self, other: Counts) -> None:
        self.tp += other.tp
        self.fp += other.fp
        self.fn += other.fn

    @property
    def precision(self) -> float:
        return self.tp / (self.tp + self.fp) if self.tp + self.fp else 0.0

    @property
    def recall(self) -> float:
        return self.tp / (self.tp + self.fn) if self.tp + self.fn else 0.0

    @property
    def f1(self) -> float:
        p, r = self.precision, self.recall
        return 2 * p * r / (p + r) if p + r else 0.0

    def to_dict(self) -> dict:
        return {"tp": self.tp, "fp": self.fp, "fn": self.fn, "precision": round(self.precision, 4),
                "recall": round(self.recall, 4), "f1": round(self.f1, 4)}


@dataclass
class Score:
    total: Counts = field(default_factory=Counts)
    per_field: dict[str, Counts] = field(default_factory=dict)
    gold_rows: int = 0
    pred_rows: int = 0
    matched_rows: int = 0

    def to_dict(self) -> dict:
        return {**self.total.to_dict(), "gold_rows": self.gold_rows, "pred_rows": self.pred_rows,
                "matched_rows": self.matched_rows,
                "per_field": {k: v.to_dict() for k, v in self.per_field.items()}}


def _agreement(p: dict, g: dict, schema: Schema) -> int:
    return sum(1 for f in schema.fields if g.get(f.name) is not None and same(p.get(f.name), g.get(f.name), f.type))


def align(pred: list[dict], gold: list[dict], schema: Schema) -> list[tuple[int, int]]:
    pairs = []
    for i, p in enumerate(pred):
        for j, g in enumerate(gold):
            a = _agreement(p, g, schema)
            if a:
                pairs.append((-a, abs(i - j), i, j))
    pairs.sort()
    used_p, used_g, out = set(), set(), []
    for _, _, i, j in pairs:
        if i not in used_p and j not in used_g:
            used_p.add(i)
            used_g.add(j)
            out.append((i, j))
    return out


def score(pred: list[dict], gold: list[dict], schema: Schema, exclude: list[dict] | None = None) -> Score:
    """Score predicted rows. Rows equal to an `exclude` row (the labelled examples) are removed from both sides."""
    exclude = exclude or []

    def is_excluded(row):
        return any(all(same(row.get(f.name), e.get(f.name), f.type) for f in schema.fields) for e in exclude)

    gold = [g for g in gold if not is_excluded(g)]
    pred = [p for p in pred if not is_excluded(p)]
    s = Score(per_field={f.name: Counts() for f in schema.fields}, gold_rows=len(gold), pred_rows=len(pred))
    pairs = align(pred, gold, schema)
    s.matched_rows = len(pairs)
    matched_p = {i: j for i, j in pairs}
    matched_g = set(matched_p.values())
    for i, p in enumerate(pred):
        g = gold[matched_p[i]] if i in matched_p else None
        for f in schema.fields:
            c = s.per_field[f.name]
            pv = p.get(f.name)
            gv = g.get(f.name) if g else None
            if pv is not None and gv is not None and same(pv, gv, f.type):
                c.tp += 1
            else:
                if pv is not None:
                    c.fp += 1
                if gv is not None:
                    c.fn += 1
    for j, g in enumerate(gold):
        if j in matched_g:
            continue
        for f in schema.fields:
            if g.get(f.name) is not None:
                s.per_field[f.name].fn += 1
    for c in s.per_field.values():
        s.total.add(c)
    return s


# ---------- fixtures ----------

@dataclass
class Fixture:
    name: str
    schema: Schema
    pages: dict[str, str]        # url -> html, in order
    records: list[dict]
    request: str
    notes: str = ""

    @property
    def start_url(self) -> str:
        return next(iter(self.pages))

    @property
    def examples(self) -> list[dict]:
        return self.records[:2]


def load_fixture(path: Path) -> Fixture:
    spec = json.loads((path / "spec.json").read_text(encoding="utf-8"))
    schema = Schema.from_dict(spec["schema"])
    pages = {p["url"]: (path / p["file"]).read_text(encoding="utf-8") for p in spec["pages"]}
    gold = json.loads((path / "gold.json").read_text(encoding="utf-8"))
    return Fixture(path.name, schema, pages, gold, spec.get("request", schema.description), spec.get("notes", ""))


def load_fixtures(root: Path = FIXTURES) -> list[Fixture]:
    return [load_fixture(p) for p in sorted(root.iterdir()) if (p / "spec.json").exists()]


# ---------- running extractors over fixtures ----------

OFFLINE_EXTRACTORS = ("heuristic", "induction-1", "induction-2")


def run_extractor(fx: Fixture, extractor: str, llm=None):
    """Run one extractor over every page of a fixture (following its pagination). Returns the pipeline Result."""
    from .fetch import DictFetcher
    from .pipeline import Scraper

    schema = Schema.from_dict(fx.schema.to_dict())
    kwargs: dict = {"fetcher": DictFetcher(fx.pages), "max_pages": len(fx.pages)}
    if extractor == "heuristic":
        s = Scraper(schema, "heuristic", **kwargs)
    elif extractor.startswith("induction-"):
        s = Scraper(schema, "induction", examples=fx.examples[: int(extractor.split("-")[1])], **kwargs)
    elif extractor in ("claude", "hybrid"):
        s = Scraper(schema, extractor, llm=llm, **kwargs)
    else:
        raise ValueError(f"unknown extractor {extractor!r}")
    return s.run(fx.start_url)


def evaluate_fixture(fx: Fixture, extractor: str, llm=None) -> dict:
    import time

    t0 = time.perf_counter()
    res = run_extractor(fx, extractor, llm)
    secs = time.perf_counter() - t0
    sc = score(res.rows, fx.records, fx.schema, exclude=fx.examples)
    return {"fixture": fx.name, "extractor": extractor, "split": "holdout" if fx.name.startswith("holdout") else "dev",
            "pages": len(res.pages), "pages_expected": len(fx.pages), "seconds": round(secs, 4),
            "methods": [p.method for p in res.pages], "errors": [p.error for p in res.pages if p.error],
            "notes": res.notes, **sc.to_dict(), "usage": res.usage}


def summarize(results: list[dict]) -> dict:
    """Micro-averaged P/R/F1 per extractor (and per split)."""
    out: dict = {}
    for r in results:
        for key in (r["extractor"], f"{r['extractor']}@{r['split']}"):
            c = out.setdefault(key, Counts())
            c.add(Counts(r["tp"], r["fp"], r["fn"]))
    return {k: v.to_dict() for k, v in out.items()}

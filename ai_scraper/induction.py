"""Wrapper induction: find the repeated records on a page and learn where each field lives, with no LLM.

1. Record detection (`detect_regions`). For every element with enough children, its children are grouped into
   windows of k = 1, 2 or 3 consecutive siblings (k > 1 handles listings where one item spans several rows, like
   a Hacker News story + subtext row). Groups with the most common signature become candidate records. Each
   record's *shape* is the set of tag paths in its subtree; records whose shape has a Jaccard similarity below 0.5
   to the region's majority shape are dropped (ads, separators, "load more" rows). Regions are scored by
   records x similarity x richness, so a sidebar of 20 one-link categories loses to 20 product cards.

2. Field mapping (`induce`). Given 1-2 labelled example records ({field: value}), the record that contains the
   example values is located, and every *slot* in it (an element's text, an attribute such as title/href/src/
   class/datetime, or the joined text of repeated children) is tested against each example value with a typed
   matcher: the n-th number in the text, a boolean keyword, a URL after resolving, or a string with a literal
   prefix/suffix to strip ("by Albert Einstein" -> "Albert Einstein"). The winning slot's path is stored as
   steps of (tag, stable classes, nth), and generalised when two examples disagree on an index.

3. Zero-shot mapping (`induce_heuristic`). Without examples, slots that appear in most records are scored per field
   by type fit, name/label/class similarity and distinctness, then assigned greedily.

A `Wrapper` is plain JSON, so one learned on page 1 is reused on page 2..N of the same site for free.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import urljoin

from .clean import DROP, is_boilerplate
from .dom import Node, norm_space
from .schema import Schema, coerce, parse_bool, parse_numbers

VARIANT_CLASS = re.compile(r"^(odd|even|first|last|active|alt|selected|current|new|hot|featured|"
                           r"one|two|three|four|five|zero|.*\d.*)$", re.I)
SLOT_ATTRS = ("title", "href", "src", "alt", "datetime", "content", "value", "aria-label", "class")
MAX_SLOT_TEXT = 400
SYNONYMS = {
    "title": {"name", "heading", "headline", "title"},
    "name": {"title", "name", "heading"},
    "price": {"price", "cost", "amount", "money"},
    "rating": {"rating", "stars", "star", "score"},
    "author": {"author", "by", "user", "byline", "writer"},
    "url": {"href", "link", "url"},
    "link": {"href", "link", "url"},
    "image": {"src", "img", "image", "thumbnail", "photo"},
    "date": {"date", "time", "datetime", "posted", "published", "age"},
    "location": {"location", "city", "place", "address", "loc"},
    "company": {"company", "employer", "org", "organization"},
    "description": {"description", "desc", "summary", "excerpt", "blurb"},
    "stock": {"availability", "stock", "instock"},
    "beds": {"bd", "bed", "beds", "bedrooms", "br"},
    "baths": {"ba", "bath", "baths", "bathrooms"},
    "sqft": {"sqft", "sq", "ft", "area"},
    "reviews": {"reviews", "review", "ratings"},
    "comments": {"comments", "comment"},
    "points": {"points", "score", "votes"},
    "stars": {"stars", "star", "stargazers"},
    "salary": {"salary", "pay", "compensation"},
}


def stable_classes(node: Node) -> tuple[str, ...]:
    return tuple(sorted(c for c in node.classes if not VARIANT_CLASS.match(c)))


def signature(node: Node) -> str:
    cls = stable_classes(node)
    return node.tag + ("." + ".".join(cls) if cls else "")


# ---------------------------------------------------------------- records and regions

@dataclass
class Record:
    nodes: list[Node]

    def iter(self) -> Iterator[tuple[int, Node]]:
        for i, n in enumerate(self.nodes):
            for e in n.iter():
                yield i, e

    def text(self) -> str:
        return norm_space(" ".join(n.text() for n in self.nodes))

    @property
    def key(self) -> tuple[str, ...]:
        return tuple(signature(n) for n in self.nodes)


@dataclass
class Region:
    parent: Node
    records: list[Record]
    period: int
    similarity: float
    richness: float
    score: float
    key: tuple[str, ...] = ()

    @property
    def record_key(self) -> tuple[str, ...]:
        return self.key or self.records[0].key


def shape(rec: Record) -> frozenset[str]:
    paths: set[str] = set()
    for i, root in enumerate(rec.nodes):
        stack: list[tuple[Node, str]] = [(root, f"{i}:{root.tag}")]
        while stack:
            node, path = stack.pop()
            paths.add(path)
            for c in node.elements:
                stack.append((c, f"{path}/{c.tag}"))
    return frozenset(paths)


def jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 1.0


def richness(rec: Record) -> int:
    """Number of distinct leaf-ish values in a record: text-bearing elements plus links and images."""
    n = 0
    for _, e in rec.iter():
        if e.own_text():
            n += 1
        if e.tag == "a" and e.attrs.get("href"):
            n += 1
        if e.tag == "img":
            n += 1
    return n


def _fits(group: list[Node], key: tuple[str, ...]) -> bool:
    for n, sig in zip(group, key, strict=True):
        tag, *classes = sig.split(".")
        if n.tag != tag or not set(classes) <= set(n.classes):
            return False
    return True


def _regions_under(parent: Node, min_records: int) -> list[Region]:
    kids = [c for c in parent.elements if c.tag not in DROP]
    if len(kids) < min_records:
        return []
    out = []
    for k in (1, 2, 3):
        if len(kids) < k * min_records:
            break
        for offset in range(k):
            groups = [kids[i:i + k] for i in range(offset, len(kids) - k + 1, k)]
            keys = Counter(tuple(signature(n) for n in g) for g in groups)
            key, count = keys.most_common(1)[0]
            if count < min_records:
                continue
            if k > 1 and len(set(key)) == 1:
                continue  # k identical siblings are k records, not one record of k rows
            # rows with an extra modifier class ("topic pinned", "card highlighted") still belong to the list,
            # but ads ("card sponsored") do not
            recs = [Record(g) for g in groups if _fits(g, key) and not any(is_boilerplate(n) for n in g)]
            shapes = [shape(r) for r in recs]
            freq = Counter(p for s in shapes for p in s)
            majority = frozenset(p for p, c in freq.items() if c >= len(shapes) / 2)
            sims = [jaccard(s, majority) for s in shapes]
            keep = [(r, s) for r, s in zip(recs, sims, strict=True) if s >= 0.5]
            if len(keep) < min_records:
                continue
            avg_sim = sum(s for _, s in keep) / len(keep)
            rich = sum(richness(r) for r, _ in keep) / len(keep)
            if rich < 1:
                continue
            # a weak penalty on k so that, at equal richness, the simpler grouping wins
            score = len(keep) * avg_sim * min(rich, 12) * (0.25 if rich < 2 else 1.0) / (1 + 0.05 * (k - 1))
            out.append(Region(parent, [r for r, _ in keep], k, round(avg_sim, 3), round(rich, 2), score, key))
    return out


def detect_regions(root: Node, min_records: int = 3, limit: int = 8) -> list[Region]:
    """Candidate record regions, best first."""
    regions: list[Region] = []
    for node in root.iter():
        if node.tag in DROP or len(node.elements) < min_records:
            continue
        if any(is_boilerplate(a) for a in [node, *node.ancestors()] if a.tag != "#document"):
            continue
        regions.extend(_regions_under(node, min_records))
    regions.sort(key=lambda r: -r.score)
    # keep the best grouping per parent, so k=1 and k=3 views of the same list don't crowd the top
    seen, best = set(), []
    for r in regions:
        if id(r.parent) in seen:
            continue
        seen.add(id(r.parent))
        best.append(r)
    return best[:limit]


# ---------------------------------------------------------------- slots: where a value can live

@dataclass(frozen=True)
class Step:
    tag: str
    classes: tuple[str, ...]
    nth: int | None


@dataclass(frozen=True)
class Slot:
    group: int                 # which node of a multi-node record
    path: tuple[Step, ...]     # from that node down
    source: str                # "text" | "join" | "attr:<name>"

    def loose_key(self) -> tuple:
        return (self.group, tuple((s.tag, s.classes) for s in self.path), self.source)

    def describe(self) -> str:
        parts = [s.tag + "".join("." + c for c in s.classes) + (f"[{s.nth}]" if s.nth else "") for s in self.path]
        return (f"{self.group}:" if self.group else "") + " > ".join(parts or ["(record)"]) + f" :: {self.source}"


def path_to(node: Node, root: Node) -> tuple[Step, ...]:
    steps: list[Step] = []
    cur = node
    while cur is not root and cur.parent is not None:
        sig = (cur.tag, stable_classes(cur))
        same = [c for c in cur.parent.elements if (c.tag, stable_classes(c)) == sig]
        steps.append(Step(cur.tag, sig[1], same.index(cur)))
        cur = cur.parent
    return tuple(reversed(steps))


def resolve(rec: Record, slot: Slot) -> Node | None:
    if slot.group >= len(rec.nodes):
        return None
    node = rec.nodes[slot.group]
    for step in slot.path:
        kids = [c for c in node.elements if c.tag == step.tag]
        exact = [c for c in kids if stable_classes(c) == step.classes]
        if not exact and step.classes:
            exact = [c for c in kids if set(step.classes) <= set(c.classes)]
        if not exact and not step.classes:
            exact = kids
        if not exact:
            return None
        node = exact[step.nth] if step.nth is not None and step.nth < len(exact) else exact[0]
    return node


def read_source(node: Node, source: str) -> str | None:
    if source == "text":
        t = node.text()
        return t if t and len(t) <= MAX_SLOT_TEXT else None
    if source == "join":
        kids = [c.text() for c in node.elements if c.text()]
        return ", ".join(kids) if kids else None
    if source.startswith("attr:"):
        v = node.attrs.get(source[5:])
        return v.strip() if v and v.strip() else None
    return None


def enumerate_slots(rec: Record) -> list[tuple[Slot, str]]:
    out: list[tuple[Slot, str]] = []
    for gi, root in enumerate(rec.nodes):
        for e in root.iter():
            if e.tag in DROP:
                continue
            path = path_to(e, root)
            attrs = [a for a in e.attrs if a in SLOT_ATTRS or (a.startswith("data-") and a != "data-id")]
            for source in ("text", "join", *(f"attr:{a}" for a in attrs)):
                if source == "join" and len(e.elements) < 2:
                    continue
                v = read_source(e, source)
                if v:
                    out.append((Slot(gi, path, source), v))
    return out


# ---------------------------------------------------------------- transforms: slot string -> typed value

@dataclass(frozen=True)
class Transform:
    kind: str                  # str | num | bool | url | presence
    index: int = 0             # n-th number in the text
    prefix: str = ""
    suffix: str = ""

    def apply(self, raw: str | None, ftype: str, base_url: str, present: bool):
        if self.kind == "presence":
            return present
        if self.kind == "bool" and not present:
            return False  # an optional badge ("Remote", "Sale") that is missing means "no"
        if raw is None:
            return None
        if self.kind == "num":
            nums = parse_numbers(raw)
            return coerce(nums[self.index], ftype) if len(nums) > self.index else None
        if self.kind == "bool":
            return parse_bool(raw)
        if self.kind == "url":
            return urljoin(base_url, raw) if base_url else raw
        s = norm_space(raw)
        low = s.casefold()
        if self.prefix:
            if not low.startswith(self.prefix):
                return None
            s = s[len(self.prefix):]
        if self.suffix:
            if not s.casefold().endswith(self.suffix):
                return None
            s = s[: len(s) - len(self.suffix)]
        return coerce(s.strip(), ftype, base_url)


def norm_value(v) -> str:
    return norm_space(str(v)).casefold()


def _close(a: float, b: float) -> bool:
    return abs(a - b) <= 1e-6 * max(1.0, abs(a), abs(b))


def match_value(raw: str, value, ftype: str, base_url: str) -> list[Transform]:
    """Every way the example `value` can be read from the slot string `raw` (usually zero or one; several when
    the same number appears twice, as in "1 bd · 1 ba")."""
    if value is None:
        return []
    if ftype in ("number", "integer"):
        try:
            target = float(value)
        except (TypeError, ValueError):
            return []
        return [Transform("num", index=i) for i, n in enumerate(parse_numbers(raw)) if _close(n, target)]
    if ftype == "boolean":
        return [Transform("bool")] if parse_bool(raw) is value else []
    if ftype == "url":
        full = urljoin(base_url, raw) if base_url else raw
        return [Transform("url")] if norm_value(full).rstrip("/") == norm_value(value).rstrip("/") else []
    s, v = norm_value(raw), norm_value(value)
    if not v:
        return []
    if s == v:
        return [Transform("str")]
    i = s.find(v)
    if i >= 0 and len(v) / len(s) >= 0.3 and len(s) - len(v) <= 40:
        prefix, suffix = s[:i], s[i + len(v):]
        if all(not ch.isalnum() for ch in prefix + suffix) or len(prefix + suffix) <= 25:
            return [Transform("str", prefix=prefix, suffix=suffix)]
    return []


def _candidate_rank(slot: Slot, tr: Transform, raw: str) -> tuple:
    """Prefer exact reads, then the most specific slot (shortest raw text: a price span beats the whole card)."""
    exact = 0 if tr.kind != "str" or not (tr.prefix or tr.suffix) else 1
    src = 0 if slot.source == "text" else 1 if slot.source.startswith("attr:") else 2
    return (exact, len(raw), tr.index, len(tr.prefix) + len(tr.suffix), src, len(slot.path))


# ---------------------------------------------------------------- wrappers

@dataclass
class FieldRule:
    name: str
    ftype: str
    slot: Slot
    transform: Transform

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.ftype, "group": self.slot.group, "source": self.slot.source,
                "path": [[s.tag, list(s.classes), s.nth] for s in self.slot.path],
                "transform": {"kind": self.transform.kind, "index": self.transform.index,
                              "prefix": self.transform.prefix, "suffix": self.transform.suffix}}

    @classmethod
    def from_dict(cls, d: dict) -> FieldRule:
        slot = Slot(d["group"], tuple(Step(t, tuple(c), n) for t, c, n in d["path"]), d["source"])
        return cls(d["name"], d["type"], slot, Transform(**d["transform"]))


@dataclass
class Wrapper:
    """A learned extraction program: which records, and where each field lives inside a record."""

    record_key: tuple[str, ...]
    period: int
    rules: list[FieldRule]
    method: str = "induction"
    notes: list[str] = field(default_factory=list)

    def find_region(self, root: Node) -> Region | None:
        regions = detect_regions(root, min_records=2, limit=12)
        same = [r for r in regions if r.record_key == self.record_key]
        if same:
            return same[0]
        best, best_cov = None, 0.0
        for r in regions:
            sample = r.records[:10]
            cov = sum(resolve(rec, rule.slot) is not None for rec in sample for rule in self.rules)
            cov /= max(1, len(sample) * len(self.rules))
            if cov > best_cov:
                best, best_cov = r, cov
        return best if best_cov >= 0.5 else None

    def extract_records(self, records: list[Record], schema: Schema) -> list[dict]:
        by_name = {r.name: r for r in self.rules}
        rows = []
        for rec in records:
            row = {}
            for f in schema.fields:
                rule = by_name.get(f.name)
                if rule is None:
                    row[f.name] = None
                    continue
                node = resolve(rec, rule.slot)
                raw = read_source(node, rule.slot.source) if node is not None else None
                row[f.name] = rule.transform.apply(raw, f.type, schema.base_url, node is not None)
            if any(v is not None for v in row.values()):
                rows.append(row)
        return rows

    def extract(self, root: Node, schema: Schema) -> tuple[list[dict], Region | None]:
        region = self.find_region(root)
        if region is None:
            return [], None
        return self.extract_records(region.records, schema), region

    def to_dict(self) -> dict:
        return {"record_key": list(self.record_key), "period": self.period, "method": self.method,
                "rules": [r.to_dict() for r in self.rules], "notes": self.notes}

    @classmethod
    def from_dict(cls, d: dict) -> Wrapper:
        return cls(tuple(d["record_key"]), d["period"], [FieldRule.from_dict(r) for r in d["rules"]],
                   d.get("method", "induction"), d.get("notes", []))


class InductionError(ValueError):
    pass


def _match_count(rec: Record, example: dict, schema: Schema) -> int:
    slots = enumerate_slots(rec)
    n = 0
    for f in schema.fields:
        v = example.get(f.name)
        if v is None:
            continue
        if any(match_value(raw, v, f.type, schema.base_url) for _, raw in slots):
            n += 1
    return n


def _field_candidates(rec: Record, f, value, base_url: str) -> list[tuple[Slot, Transform]]:
    cands = []
    for slot, raw in enumerate_slots(rec):
        for tr in match_value(raw, value, f.type, base_url):
            cands.append((slot, tr, raw))
    if not cands and f.type == "boolean" and value is True:
        words = set(re.findall(r"[a-z]+", f.name.replace("_", " ")))
        for slot, raw in enumerate_slots(rec):
            if slot.source in ("text", "attr:class") and words & set(re.findall(r"[a-z]+", raw.lower())):
                cands.append((slot, Transform("presence"), raw))
    cands.sort(key=lambda c: _candidate_rank(*c))
    return [(s, t) for s, t, _ in cands]


def _read(rec: Record, slot: Slot, tr: Transform, ftype: str, base_url: str):
    node = resolve(rec, slot)
    raw = read_source(node, slot.source) if node is not None else None
    return tr.apply(raw, ftype, base_url, node is not None)


def _reproduces(rec: Record, slot: Slot, tr: Transform, f, value, base_url: str) -> bool:
    got = _read(rec, slot, tr, f.type, base_url)
    if value is None or got is None:
        return value is None and got is None
    if f.type in ("number", "integer"):
        return isinstance(got, int | float) and not isinstance(got, bool) and _close(float(got), float(value))
    if f.type == "boolean":
        return got is value
    return norm_value(got).rstrip("/") == norm_value(value).rstrip("/")


def induce(root: Node, schema: Schema, examples: list[dict], min_records: int = 3) -> Wrapper:
    """Learn a wrapper from 1-2 labelled example records."""
    examples = [e for e in examples if isinstance(e, dict) and any(v is not None for v in e.values())][:2]
    if not examples:
        raise InductionError("give at least one example record")
    regions = detect_regions(root, min_records=min_records)
    if not regions:
        raise InductionError("no repeated record structure found on the page")
    # pick the region (and record) that contains the first example best
    best = None
    for r in regions:
        for idx, rec in enumerate(r.records[:60]):
            m = _match_count(rec, examples[0], schema)
            cand = (m, r.score, -idx)
            if best is None or cand > best[0]:
                best = (cand, r, rec)
    (matched, _, _), region, rec0 = best
    if matched == 0:
        raise InductionError("none of the example values were found inside a repeated record")
    pairs = [(rec0, examples[0])]
    if len(examples) > 1:
        scored = [(_match_count(r, examples[1], schema), r) for r in region.records[:60] if r is not rec0]
        scored.sort(key=lambda x: -x[0])
        if scored and scored[0][0] > 0:
            pairs.append((scored[0][1], examples[1]))

    rules, notes = [], []
    for f in schema.fields:
        per_example = [_field_candidates(rec, f, ex.get(f.name), schema.base_url) if ex.get(f.name) is not None
                       else [] for rec, ex in pairs]
        pool: list[tuple[Slot, Transform]] = []
        if len(per_example) == 2 and per_example[0] and per_example[1]:
            # a slot found in both examples, with indexes generalised where they disagree
            both = {(s.loose_key(), t): s for s, t in per_example[1]}
            for s0, t0 in per_example[0]:
                s1 = both.get((s0.loose_key(), t0))
                if s1 is not None:
                    path = tuple(Step(a.tag, a.classes, a.nth if a.nth == b.nth else None)
                                 for a, b in zip(s0.path, s1.path, strict=True))
                    pool.append((Slot(s0.group, path, s0.source), t0))
        for cands in per_example:
            pool += cands
        if not pool:
            notes.append(f"{f.name}: example value not found in the record")
            continue
        # keep the first (best-ranked) candidate among those that reproduce the most examples, nulls included
        best_rule, best_key, best_hits = None, None, -1
        fw = _field_words(f)
        for slot, tr in pool[:200]:
            hits = sum(_reproduces(rec, slot, tr, f, ex.get(f.name), schema.base_url) for rec, ex in pairs)
            named = 0
            if tr.kind == "num":  # "2 bd · 2 ba": the same number twice, so let the neighbouring word decide
                node = resolve(rec0, slot)
                raw = read_source(node, slot.source) if node is not None else None
                named = int(raw is not None and _near_word_index(raw, fw) == tr.index)
            key = (hits, named)
            if best_key is None or key > best_key:
                best_rule, best_key, best_hits = (slot, tr), key, hits
        slot, tr = best_rule
        if best_hits < len(pairs):
            notes.append(f"{f.name}: the learned rule reproduces {best_hits} of {len(pairs)} examples")
        rules.append(FieldRule(f.name, f.type, slot, tr))
    if not rules:
        raise InductionError("could not map any field from the examples")
    return Wrapper(region.record_key, region.period, rules, "induction", notes)


# ---------------------------------------------------------------- zero-shot (schema only)

def _tokens(text: str) -> set[str]:
    return set(re.findall(r"[a-z]+", text.lower().replace("_", " ")))


def _hints(slot: Slot) -> set[str]:
    words: set[str] = set()
    for s in slot.path:
        words |= {s.tag}
        for c in s.classes:
            words |= _tokens(c) | {c.lower()}
    if slot.source.startswith("attr:"):
        words |= _tokens(slot.source[5:]) | {slot.source[5:]}
    return words


def _field_words(f) -> set[str]:
    words = _tokens(f.name)
    out = set(words)
    for w in words:
        out |= SYNONYMS.get(w, set())
    return out


def _near_word_index(text: str, fw: set[str]) -> int | None:
    """Index of the number whose neighbouring words name the field: "3 bd · 2 ba" -> baths is number 1."""
    parts = NUM_SPLIT.split(text)
    for j, i in enumerate(range(1, len(parts), 2)):
        around = _tokens(parts[i - 1][-14:]) | _tokens(parts[i + 1][:14] if i + 1 < len(parts) else "")
        if around & fw or any(w.rstrip("s") == a.rstrip("s") for w in fw for a in around if len(a) > 2):
            return j
    return None


LABEL_RE = re.compile(r"^\s*([A-Za-z][A-Za-z /&()-]{1,30})\s*[:：]\s*")
NUM_SPLIT = re.compile(r"(\d[\d,.]*\s?[kKmM]?(?![a-zA-Z]{2}))")
DATE_WORDS = {"date", "time", "posted", "updated", "published", "when"}


def _type_fit(values: list[str], ftype: str, source: str) -> float:
    if not values:
        return 0.0
    if ftype == "url":
        return 1.0 if source in ("attr:href", "attr:src") else 0.0
    if source == "attr:datetime":
        return 1.0 if ftype == "string" else 0.0
    if source in ("attr:href", "attr:src", "attr:class", "attr:alt") and ftype == "string":
        return 0.0
    if source in ("attr:href", "attr:src", "attr:alt", "attr:datetime") and ftype in ("number", "integer", "boolean"):
        return 0.0  # ids in URLs and years in dates are not quantities
    ok = 0
    for v in values:
        if ftype in ("number", "integer"):
            ok += bool(parse_numbers(v)) and len(v) <= 60
        elif ftype == "boolean":
            ok += parse_bool(v) is not None
        else:
            ok += not (re.fullmatch(r"[\d\s.,$£€%+-]+", v) and re.search(r"\d", v)) and len(v) <= 300
    return ok / len(values)


def _column_headers(region: Region) -> list[set[str]]:
    """Header words per column when the records are table rows."""
    table = next((a for a in [region.parent, *region.parent.ancestors()] if a.tag == "table"), None)
    if table is None:
        return []
    ths = [th for th in table.iter() if th.tag == "th"]
    return [_tokens(th.text()) for th in ths]


MONEY_WORDS = {"price", "cost", "fee", "amount", "salary", "rent", "total", "gbp", "usd", "eur"}
CURRENCY_RE = re.compile(r"[$£€¥₹]|(?:usd|eur|gbp|bgn|lv)", re.I)


def induce_heuristic(root: Node, schema: Schema, min_records: int = 3) -> Wrapper:
    """Map fields to slots using only the schema (names, types, descriptions)."""
    regions = detect_regions(root, min_records=min_records)
    if not regions:
        raise InductionError("no repeated record structure found on the page")
    region = regions[0]
    recs = region.records[:40]
    headers = _column_headers(region)
    support: dict[tuple, list[str]] = {}
    proto: dict[tuple, Slot] = {}
    for rec in recs:
        seen = set()
        for slot, raw in enumerate_slots(rec):
            k = (slot.group, slot.path, slot.source)
            if k in seen:
                continue
            seen.add(k)
            support.setdefault(k, []).append(raw)
            proto[k] = slot
    n = len(recs)
    keys = [k for k, vals in support.items() if len(vals) >= max(2, 0.3 * n)]

    def anchor_len(k) -> float:
        texts = support.get((k[0], k[1], "text"), [])
        return sum(len(t) for t in texts) / n if len(texts) >= 0.5 * n else 0.0

    hrefs = [k for k in keys if k[2] == "attr:href"]
    headline_link = max(hrefs, key=anchor_len) if hrefs else None

    def column_words(slot: Slot) -> set[str]:
        if not headers or not slot.path or slot.path[0].tag not in ("td", "th"):
            return set()
        node = resolve(recs[0], Slot(slot.group, slot.path[:1], "text"))
        if node is None or node.parent is None:
            return set()
        col = node.parent.elements.index(node)
        return headers[col] if col < len(headers) else set()

    scored = []
    for f in schema.fields:
        fw = _field_words(f)
        for k in keys:
            slot, values = proto[k], support[k]
            fit = _type_fit(values, f.type, slot.source)
            if fit < 0.5:
                continue
            hints = _hints(slot) | column_words(slot)
            labels = Counter()
            for v in values:
                m = LABEL_RE.match(v)
                if m:
                    labels[m.group(1).strip().casefold()] += 1
            label = labels.most_common(1)[0][0] if labels else ""
            label_hit = bool(label) and bool(_tokens(label) & fw)
            near = [_near_word_index(v, fw) for v in values[:20]] if f.type in ("number", "integer") else []
            word_hit = bool(near) and sum(x is not None for x in near) >= 0.5 * len(near)
            name = 1.0 if (hints & fw) or label_hit or word_hit else 0.0
            if not name and any(w in h for w in fw for h in hints if len(w) > 3):
                name = 0.5
            distinct = len(set(values)) / len(values)
            sup = len(values) / n
            s = fit * (1 + 2 * name) * (0.5 + 0.5 * sup)
            if f.type in ("string", "url"):
                s *= 0.6 + 0.4 * distinct
            if slot.source == "text" and slot.path and slot.path[-1].tag in ("h1", "h2", "h3", "h4", "a") \
                    and _tokens(f.name) & {"title", "name", "headline"}:
                s += 0.8
            if slot.source == "attr:datetime" and _tokens(f.name) & DATE_WORDS:
                s += 0.8
            if f.type == "url" and k == headline_link:
                s += 0.3  # the link that carries the headline, not an icon, a vote arrow or a comments link
            if f.type in ("number", "integer") and fw & MONEY_WORDS:
                cur = sum(bool(CURRENCY_RE.search(v)) for v in values) / len(values)
                s *= 0.7 + 0.6 * cur  # a price lives next to a currency sign, not in "Item 2"
            if f.type in ("number", "integer") and distinct < 0.2 and n > 3:
                s *= 0.3  # a value that never changes between records is a label, not data
            if slot.source == "join":
                s *= 1.1 if f.type == "string" and f.name.endswith("s") else 0.5
            if f.type == "boolean" and distinct < 0.2:
                s *= 0.8
            # prefer the deepest (most specific) text node: a container's text mixes several fields
            s -= 0.03 * max(0, 4 - len(slot.path)) * (slot.source == "text")
            s -= 0.2 * (not slot.path and slot.source == "text")
            if values and LABEL_RE.match(values[0]) and not label_hit:
                s *= 0.7
            tr = _transform_for(f, values, label if label_hit else "", fw, near)
            scored.append((s, f.name, k, tr))
    scored.sort(key=lambda x: -x[0])
    used, done, rules = set(), set(), []
    for s, name, k, tr in scored:
        if name in done or (k, tr) in used or s < 0.6 or tr is None:
            continue
        f = next(x for x in schema.fields if x.name == name)
        rules.append(FieldRule(name, f.type, proto[k], tr))
        done.add(name)
        used.add((k, tr))
    order = {f.name: i for i, f in enumerate(schema.fields)}
    rules.sort(key=lambda r: order[r.name])
    if not rules:
        raise InductionError("no field could be matched to the page")
    return Wrapper(region.record_key, region.period, rules, "heuristic",
                   [f"{f.name}: no matching slot" for f in schema.fields if f.name not in done])


def _transform_for(f, values: list[str], label: str, fw: set[str], near: list[int | None]) -> Transform | None:
    if f.type == "url":
        return Transform("url")
    if f.type == "boolean":
        return Transform("bool")
    if f.type in ("number", "integer"):
        votes = Counter(x for x in near if x is not None)
        return Transform("num", index=votes.most_common(1)[0][0] if votes else 0)
    if label:
        return Transform("str", prefix=_label_prefix(values, label))
    return Transform("str")


def _label_prefix(values: list[str], label: str) -> str:
    for v in values:
        v = norm_space(v)
        m = LABEL_RE.match(v)
        if m and m.group(1).strip().casefold() == label and m.end() < len(v):
            return v[: m.end()].casefold()
    return ""

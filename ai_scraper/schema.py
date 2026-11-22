"""Schemas, value parsing and validation.

A schema is an ordered list of typed fields. The same schema drives every extractor: it becomes the JSON schema
Claude must follow, it tells wrapper induction how to read a value out of a DOM node, and it validates and coerces
every row before export, whichever extractor produced it.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urljoin

TYPES = ("string", "number", "integer", "boolean", "url")
NAME_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")

WORD_NUMBERS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5,
    "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10,
}
NUM_RE = re.compile(r"(?<![\w.])[-+]?(?:\d{1,3}(?:[,  ]\d{3})+|\d+)(?:\.\d+)?(?:\s?[kKmMbB](?![a-zA-Z]))?")
SUFFIX = {"k": 1e3, "m": 1e6, "b": 1e9}
TRUE_WORDS = ("in stock", "available", "yes", "true", "remote", "open", "verified", "✓", "✔", "free shipping")
FALSE_WORDS = ("out of stock", "sold out", "unavailable", "not available", "no", "false", "on-site", "onsite",
               "closed", "✗", "✘")


class SchemaError(ValueError):
    pass


@dataclass
class Field:
    name: str
    type: str = "string"
    description: str = ""

    def __post_init__(self):
        if not NAME_RE.match(self.name):
            raise SchemaError(f"invalid field name {self.name!r}: use snake_case, max 40 characters")
        if self.type not in TYPES:
            raise SchemaError(f"field {self.name!r}: type must be one of {', '.join(TYPES)}")


@dataclass
class Schema:
    fields: list[Field]
    description: str = ""
    base_url: str = ""
    extras: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.fields:
            raise SchemaError("a schema needs at least one field")
        names = [f.name for f in self.fields]
        if len(set(names)) != len(names):
            raise SchemaError("field names must be unique")

    @property
    def names(self) -> list[str]:
        return [f.name for f in self.fields]

    @classmethod
    def from_dict(cls, data: dict) -> Schema:
        try:
            fields = [Field(f["name"], f.get("type", "string"), f.get("description", "")) for f in data["fields"]]
        except (KeyError, TypeError) as e:
            raise SchemaError("schema must look like {\"fields\": [{\"name\": ..., \"type\": ...}]}") from e
        return cls(fields, data.get("description", ""), data.get("base_url", ""))

    def to_dict(self) -> dict:
        return {"description": self.description, "base_url": self.base_url,
                "fields": [asdict(f) for f in self.fields]}

    def json_schema(self) -> dict:
        """JSON schema for one page of results: {items: [...], next_page_url}. Every field is nullable, so the
        model can say "not on the page" instead of inventing a value."""
        json_type = {"string": "string", "url": "string", "number": "number", "integer": "integer",
                     "boolean": "boolean"}
        item = {
            "type": "object",
            "properties": {f.name: {"type": [json_type[f.type], "null"]} for f in self.fields},
            "required": self.names,
            "additionalProperties": False,
        }
        return {
            "type": "object",
            "properties": {"items": {"type": "array", "items": item}, "next_page_url": {"type": ["string", "null"]}},
            "required": ["items", "next_page_url"],
            "additionalProperties": False,
        }


# ---------- value parsing ----------

def parse_numbers(text: str) -> list[float]:
    """Every number in `text`, understanding thousands separators, 1.2k / 3M suffixes and number words."""
    out: list[float] = []
    for m in NUM_RE.finditer(text):
        raw = m.group(0).strip()
        mult = 1.0
        if raw[-1].lower() in SUFFIX:
            mult = SUFFIX[raw[-1].lower()]
            raw = raw[:-1].strip()
        raw = re.sub(r"[,  ]", "", raw)
        try:
            out.append(float(raw) * mult)
        except ValueError:
            continue
    if not out:
        for word in re.findall(r"[a-z]+", text.lower()):
            if word in WORD_NUMBERS:
                out.append(float(WORD_NUMBERS[word]))
    return out


def parse_number(text: str, index: int = 0) -> float | None:
    nums = parse_numbers(text)
    return nums[index] if len(nums) > index else None


def parse_bool(text: str) -> bool | None:
    t = " " + re.sub(r"\s+", " ", text.lower()).strip() + " "
    for w in FALSE_WORDS:  # check negatives first: "out of stock" contains "in stock"'s neighbour words
        if f" {w} " in t or (len(w) > 3 and w in t):
            return False
    for w in TRUE_WORDS:
        if f" {w} " in t or (len(w) > 3 and w in t):
            return True
    return None


def coerce(value, ftype: str, base_url: str = ""):
    """Convert a raw value (usually a string from the page or from the model) to the field's type, or None."""
    if value is None:
        return None
    if isinstance(value, str):
        value = re.sub(r"\s+", " ", value).strip()
        if value == "" or value.lower() in ("null", "none", "n/a", "-", "—"):
            return None
    if ftype == "string":
        return str(value)
    if ftype == "url":
        s = str(value)
        return urljoin(base_url, s) if base_url else s
    if ftype == "boolean":
        if isinstance(value, bool):
            return value
        return parse_bool(str(value))
    if ftype in ("number", "integer"):
        if isinstance(value, bool):
            return None
        n = float(value) if isinstance(value, int | float) else parse_number(str(value))
        if n is None:
            return None
        if ftype == "integer":
            return int(round(n))
        return int(n) if n.is_integer() and abs(n) < 1e15 else n
    raise SchemaError(f"unknown type {ftype}")


def validate_row(row, schema: Schema) -> list[str]:
    """Problems with one row, as human-readable strings (empty list = valid)."""
    if not isinstance(row, dict):
        return ["row is not an object"]
    errors = []
    extra = set(row) - set(schema.names)
    if extra:
        errors.append(f"unknown keys: {', '.join(sorted(extra))}")
    for f in schema.fields:
        if f.name not in row:
            errors.append(f"missing key '{f.name}'")
            continue
        v = row[f.name]
        if v is None:
            continue
        ok = {
            "string": isinstance(v, str),
            "url": isinstance(v, str) and (v.startswith(("http://", "https://", "/")) or "." in v),
            "number": isinstance(v, int | float) and not isinstance(v, bool),
            "integer": isinstance(v, int) and not isinstance(v, bool),
            "boolean": isinstance(v, bool),
        }[f.type]
        if not ok:
            errors.append(f"'{f.name}' should be {f.type}, got {v!r}")
    return errors


def clean_rows(rows: list, schema: Schema) -> tuple[list[dict], list[str]]:
    """Coerce every row to the schema, drop all-null rows and exact duplicates. Returns (rows, problems)."""
    out, seen, problems = [], set(), []
    for i, row in enumerate(rows):
        if not isinstance(row, dict):
            problems.append(f"row {i}: not an object")
            continue
        clean = {}
        for f in schema.fields:
            raw = row.get(f.name)
            v = coerce(raw, f.type, schema.base_url)
            if raw not in (None, "") and v is None:
                problems.append(f"row {i}: could not read {f.name}={raw!r} as {f.type}")
            clean[f.name] = v
        if all(v is None for v in clean.values()):
            continue
        key = repr(sorted(clean.items(), key=lambda kv: kv[0]))
        if key in seen:
            continue
        seen.add(key)
        out.append(clean)
    return out, problems


# ---------- plain language -> schema (offline) ----------

TYPE_HINTS = [
    (re.compile(r"\b(url|link|href|website|image|photo|thumbnail)\b"), "url"),
    (re.compile(r"\b(in stock|available|availability|remote|is |has |yes/no|verified)\b"), "boolean"),
    (re.compile(r"\b(rating|stars|beds?|bedrooms?|baths?|bathrooms?|count|number of|reviews|comments|points|"
                r"votes|year|rank|quantity|qty)\b"), "integer"),
    (re.compile(r"\b(price|cost|salary|amount|population|area|size|sqft|km2|weight|score|stars count|revenue|"
                r"distance|percent)\b"), "number"),
]


def to_snake(text: str) -> str:
    text = re.sub(r"\(.*?\)", "", text.lower())
    words = re.findall(r"[a-z0-9]+", text)
    stop = {"the", "a", "an", "of", "in", "its", "their", "each", "and"}
    words = [w for w in words if w not in stop] or words
    name = "_".join(words[:4]) or "field"
    if not name[0].isalpha():
        name = "f_" + name
    return name[:40]


def guess_type(phrase: str) -> str:
    p = " " + phrase.lower() + " "
    for rx, t in TYPE_HINTS:
        if rx.search(p):
            return t
    return "string"


def schema_from_text(request: str) -> Schema:
    """'book title, price in GBP, star rating (1-5), in stock (yes/no)' -> 4 typed fields. Offline and
    deterministic; with an API key the app can ask Claude instead (see llm.ClaudeExtractor.design_schema)."""
    parts = [p.strip() for p in re.split(r",|;|\band\b|\n", request) if p.strip()]
    fields, used = [], set()
    for p in parts:
        name = to_snake(p)
        base, n = name, 2
        while name in used:
            name, n = f"{base}_{n}", n + 1
        used.add(name)
        fields.append(Field(name, guess_type(p), p))
    if not fields:
        raise SchemaError("describe at least one field, for example: title, price, rating")
    return Schema(fields, description=request)

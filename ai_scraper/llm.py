"""Claude extraction: structured outputs, validation, repair retries, caching and cost accounting.

- The response format is constrained with `output_config.format` (JSON schema built from the user's schema), so
  the reply always parses. The rows are then coerced and validated against the schema; if anything is still
  wrong (an unreadable number, an unknown key) the model gets the exact problems back and one or two repair turns.
- `stop_reason == "max_tokens"` means the chunk had more rows than fit in one answer: the chunk is split in two
  and each half is retried, recursively.
- The system prompt is identical for every call and marked for prompt caching. Every call is cached locally by a
  content hash (see cache.py), and usage is converted to dollars per model.
- Server-side refusal fallback is enabled for the models that support it.
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any

from .cache import ResponseCache, Usage, cache_key
from .clean import Chunk, estimate_tokens
from .schema import Field, Schema, SchemaError, clean_rows, validate_row

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1", "claude-fable-5-2"}
PROMPT_VERSION = "extract-v2"
MAX_REPAIRS = 2
MIN_SPLIT_TOKENS = 300

SYSTEM_PROMPT = """You extract structured records from web pages for a data pipeline.

The user message contains the columns to fill and the page content as compact text: links appear as [text](url), \
images as ![alt](src), table rows as "a | b | c", and values hidden in attributes as [class ...] or [datetime=...].

Rules:
- Return every item on the page that matches the requested record type, in page order, one object per item.
- Copy values exactly as they appear on the page. Never invent or infer a value: use null when it is not shown.
- number and integer columns: plain numbers with no currency symbols, units or thousands separators \
("£1,299.00" -> 1299, "1.2k" -> 1200). Ratings written as words count ("Three" -> 3).
- boolean columns: true or false from the page's wording ("In stock" -> true, "Sold out" -> false).
- url columns: the absolute URL from the link or image.
- Ignore navigation, filters, ads, sponsored blocks, footers and recommendation widgets.
- next_page_url: the absolute URL of the listing's "next page" link, or null if there is none."""

DESIGN_PROMPT = """You design table columns for a web data extractor. Return one field per piece of data the user \
asked for, in the order they asked. Use short snake_case names. Types: "number" for prices and measurements, \
"integer" for counts and ratings, "boolean" for yes/no facts, "url" for links and images, "string" otherwise. \
Describe in one sentence how to read each value from the page."""


class ExtractionError(RuntimeError):
    pass


class ClaudeExtractor:
    def __init__(self, client: Any = None, model: str | None = None, cache: ResponseCache | None = None,
                 effort: str = "low", max_chunk_tokens: int = 12000):
        if client is None:
            import anthropic

            client = anthropic.Anthropic(max_retries=2, timeout=120.0)
        self.client = client
        self.model = model or os.environ.get("CLAUDE_MODEL") or DEFAULT_MODEL
        self.cache = cache if cache is not None else ResponseCache()
        self.effort = effort
        self.max_chunk_tokens = max_chunk_tokens
        self.usage = Usage()

    # ---------- raw call ----------
    def _request(self, messages: list[dict], schema: dict, system: str = SYSTEM_PROMPT) -> dict:
        kwargs: dict = {
            "model": self.model,
            "max_tokens": 16000,
            "system": [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}],
            "messages": messages,
            "output_config": {"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
        }
        if self.model in FALLBACK_MODELS:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
        return kwargs

    def _call(self, messages: list[dict], schema: dict, system: str = SYSTEM_PROMPT) -> tuple[str, str, dict]:
        """Returns (stop_reason, text, usage dict)."""
        resp = self.client.beta.messages.create(**self._request(messages, schema, system))
        u = getattr(resp, "usage", None)
        usage = {k: int(getattr(u, k, 0) or 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                                        "cache_creation_input_tokens")}
        self.usage.add(self.model, usage)
        stop = getattr(resp, "stop_reason", None) or "end_turn"
        if stop == "refusal":
            raise ExtractionError("the model declined to process this page")
        text = "".join(getattr(b, "text", "") for b in resp.content if getattr(b, "type", "") == "text")
        return stop, text, usage

    # ---------- schema design ----------
    def design_schema(self, request: str, sample_text: str = "") -> Schema:
        out_schema = {
            "type": "object",
            "properties": {"fields": {"type": "array", "items": {
                "type": "object",
                "properties": {"name": {"type": "string"},
                               "type": {"type": "string", "enum": ["string", "number", "integer", "boolean", "url"]},
                               "description": {"type": "string"}},
                "required": ["name", "type", "description"], "additionalProperties": False}}},
            "required": ["fields"], "additionalProperties": False,
        }
        prompt = f"The user wants: {request}\n\nThe page starts like this:\n\n{sample_text[:6000]}"
        _, text, _ = self._call([{"role": "user", "content": prompt}], out_schema, DESIGN_PROMPT)
        try:
            data = json.loads(text)
            return Schema([Field(f["name"], f["type"], f["description"]) for f in data["fields"]], request)
        except (json.JSONDecodeError, KeyError, SchemaError) as e:
            raise ExtractionError(f"could not design columns: {e}") from e

    # ---------- extraction ----------
    def _prompt(self, schema: Schema, chunk_text: str, url: str, request: str) -> str:
        cols = "\n".join(f"- {f.name} ({f.type}): {f.description or f.name}" for f in schema.fields)
        return (f"Record type: {request or schema.description or 'the repeated items on the page'}\n\n"
                f"Columns:\n{cols}\n\nPage URL: {url or 'unknown'}\n\nPage content:\n\n{chunk_text}")

    def extract_chunk(self, schema: Schema, chunk: Chunk, url: str = "", request: str = "") -> tuple[list[dict], str | None]:
        js = schema.json_schema()
        key = cache_key(model=self.model, v=PROMPT_VERSION, effort=self.effort, schema=js,
                        desc=request or schema.description, url=url, text=chunk.text)
        hit = self.cache.get(key)
        if hit is not None:
            payload, usage = hit
            self.usage.add_hit(self.model, usage)
            return payload["items"], payload.get("next_page_url")
        self.usage.cache_misses += 1

        messages: list[dict] = [{"role": "user", "content": self._prompt(schema, chunk.text, url, request)}]
        total_usage: dict = {}
        for attempt in range(MAX_REPAIRS + 1):
            stop, text, usage = self._call(messages, js)
            for k, v in usage.items():
                total_usage[k] = total_usage.get(k, 0) + v
            if stop == "max_tokens":
                return self._split_and_retry(schema, chunk, url, request)
            problems: list[str] = []
            try:
                data = json.loads(text)
                items = data.get("items") if isinstance(data, dict) else None
                if not isinstance(items, list):
                    problems.append("'items' must be an array")
                    items = []
            except json.JSONDecodeError as e:
                problems.append(f"invalid JSON: {e}")
                data, items = {}, []
            rows, coerce_problems = clean_rows(items, schema)
            problems += coerce_problems[:10]
            # Type mismatches that coercion can fix ("£3.50" for a number) are not worth a repair turn; values it
            # cannot read are already in coerce_problems. Structural problems (unknown/missing keys) are.
            for i, row in enumerate(items[:200]):
                problems += [f"item {i}: {p}" for p in validate_row(row, schema) if " should be " not in p][:3]
            if not problems or attempt == MAX_REPAIRS:
                payload = {"items": rows, "next_page_url": data.get("next_page_url") if isinstance(data, dict) else None}
                self.cache.put(key, self.model, payload, total_usage)
                return payload["items"], payload["next_page_url"]
            self.usage.repairs += 1
            messages = messages + [
                {"role": "assistant", "content": text},
                {"role": "user", "content": "Your JSON has these problems:\n- " + "\n- ".join(problems[:15]) +
                 "\n\nReturn the complete corrected JSON for the same page."},
            ]
        raise AssertionError("unreachable")

    def _split_and_retry(self, schema, chunk: Chunk, url, request):
        if chunk.tokens < MIN_SPLIT_TOKENS:
            raise ExtractionError("the answer for one small chunk exceeded max_tokens; use fewer fields")
        parts = chunk.text.split("\n\n")
        if len(parts) < 2:
            parts = chunk.text.split("\n")
        mid = max(1, len(parts) // 2)
        rows, next_url = [], None
        for half in ("\n\n".join(parts[:mid]), "\n\n".join(parts[mid:])):
            r, n = self.extract_chunk(schema, Chunk(half, estimate_tokens(half)), url, request)
            rows += r
            next_url = next_url or n
        return rows, next_url

    def extract(self, schema: Schema, chunks: list[Chunk], url: str = "", request: str = "") -> tuple[list[dict], str | None]:
        rows, next_url = [], None
        for ch in chunks:
            r, n = self.extract_chunk(schema, ch, url, request)
            rows += r
            next_url = next_url or n
        rows, _ = clean_rows(rows, schema)
        return rows, next_url

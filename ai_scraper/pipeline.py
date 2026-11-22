"""One extraction job: fetch -> parse -> extract -> validate, page after page.

Modes
- heuristic : zero-shot wrapper from the schema alone. Offline, free, weakest.
- induction : wrapper learned from 1-2 example records the user typed. Offline, free.
- claude    : every page goes through Claude (cleaned text, chunked, structured output, repair).
- hybrid    : Claude reads page 1; a wrapper is learned from Claude's own first two rows and checked against
              everything Claude returned for that page. If it agrees (F1 >= 0.9) pages 2..N use the free wrapper,
              and any page where the wrapper finds nothing or far fewer records falls back to Claude.
- auto      : induction if examples are given, else hybrid if an API key is set, else heuristic.

The wrapper is learned once per job and reused on every following page, so offline modes cost one induction
plus one cheap DOM walk per page.
"""

from __future__ import annotations

import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from .clean import blocks_for, chunk_blocks, estimate_tokens, to_text
from .dom import Node, parse, serialize
from .evaluate import score
from .induction import InductionError, Region, Wrapper, detect_regions, induce, induce_heuristic
from .pagination import find_next
from .schema import Schema, clean_rows

MODES = ("auto", "heuristic", "induction", "claude", "hybrid")
VERIFY_F1 = 0.9


@dataclass
class PageResult:
    url: str
    method: str
    rows: int
    records_found: int
    ms: float
    next_url: str | None
    next_rule: str
    html_tokens_est: int
    text_tokens_est: int
    error: str = ""

    def to_dict(self) -> dict:
        return {k: getattr(self, k) for k in self.__dataclass_fields__}


@dataclass
class Result:
    schema: Schema
    mode: str
    rows: list[dict] = field(default_factory=list)
    pages: list[PageResult] = field(default_factory=list)
    wrapper: Wrapper | None = None
    usage: dict = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)
    preview_html: str = ""

    def to_dict(self, include_preview: bool = False) -> dict:
        d = {"mode": self.mode, "schema": self.schema.to_dict(), "rows": self.rows,
             "pages": [p.to_dict() for p in self.pages], "usage": self.usage, "notes": self.notes,
             "wrapper": self.wrapper.to_dict() if self.wrapper else None}
        if include_preview:
            d["preview_html"] = self.preview_html
        return d


def annotate_preview(root: Node, region: Region | None) -> str:
    """The page as inert HTML with every detected record outlined and numbered (for the web UI's iframe)."""
    marks: dict[int, dict[str, str]] = {}
    if region is not None:
        for i, rec in enumerate(region.records):
            for j, n in enumerate(rec.nodes):
                marks[n.nid] = {"data-ais-record": str(i + 1), "data-ais-part": str(j)}
    css = ("<style>[data-ais-record]{outline:2px solid #10b981!important;outline-offset:2px;position:relative}"
           "[data-ais-record][data-ais-part='0']::before{content:attr(data-ais-record);position:absolute;top:-10px;"
           "left:-10px;background:#10b981;color:#fff;font:600 11px/18px system-ui;padding:0 6px;border-radius:9px;"
           "z-index:9}</style>")
    # The page is untrusted: no scripts (the iframe is sandboxed) and no requests at all, so a preview never
    # loads images or trackers from the scraped site (or 404s against this server for relative URLs).
    csp = "<meta http-equiv=\"Content-Security-Policy\" content=\"default-src 'none'; style-src 'unsafe-inline'\">"
    return csp + css + serialize(root, marks)


class Scraper:
    def __init__(self, schema: Schema, mode: str = "auto", examples: list[dict] | None = None, llm=None,
                 fetcher: Callable[[str], str] | None = None, max_pages: int = 1, max_chunk_tokens: int = 12000):
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if max_pages < 1:
            raise ValueError("max_pages must be at least 1")
        self.schema = schema
        self.examples = [e for e in (examples or []) if isinstance(e, dict)]
        self.llm = llm
        self.fetcher = fetcher
        self.max_pages = max_pages
        self.max_chunk_tokens = max_chunk_tokens
        self.mode = self._resolve_mode(mode)

    def _resolve_mode(self, mode: str) -> str:
        if mode == "auto":
            if self.examples:
                return "induction"
            if self.llm is not None or os.environ.get("ANTHROPIC_API_KEY"):
                return "hybrid"
            return "heuristic"
        if mode == "induction" and not self.examples:
            raise ValueError("induction mode needs 1-2 example records")
        return mode

    def _get_llm(self):
        if self.llm is None:
            from .llm import ClaudeExtractor

            self.llm = ClaudeExtractor()
        return self.llm

    # ---------- one page with Claude ----------
    def _claude_page(self, root: Node, url: str, region: Region | None) -> tuple[list[dict], str | None]:
        records = [r.nodes for r in region.records] if region is not None else None
        chunks = chunk_blocks(blocks_for(root, url, records), self.max_chunk_tokens)
        return self._get_llm().extract(self.schema, chunks, url, self.schema.description)

    def run(self, url: str, html: str | None = None) -> Result:
        res = Result(self.schema, self.mode)
        wrapper: Wrapper | None = None
        claude_baseline = 0
        seen: set[str] = set()
        current: str | None = url
        for page_no in range(self.max_pages):
            if current is None or current in seen:
                break
            seen.add(current)
            t0 = time.perf_counter()
            try:
                page_html = html if (page_no == 0 and html is not None) else self.fetcher(current)
            except Exception as e:  # noqa: BLE001 - reported per page, the job keeps what it has
                res.pages.append(PageResult(current, "fetch", 0, 0, 0.0, None, "none", 0, 0, str(e)))
                break
            self.schema.base_url = current
            root = parse(page_html)
            text_tokens = estimate_tokens(to_text(root, current))
            regions = detect_regions(root)
            region = regions[0] if regions else None
            rows: list[dict] = []
            method, error = self.mode, ""
            llm_next = None
            try:
                if self.mode in ("heuristic", "induction"):
                    if wrapper is None:
                        wrapper = (induce(root, self.schema, self.examples) if self.mode == "induction"
                                   else induce_heuristic(root, self.schema))
                        res.notes += wrapper.notes
                    rows, region = wrapper.extract(root, self.schema)
                elif self.mode == "claude":
                    rows, llm_next = self._claude_page(root, current, region)
                else:  # hybrid
                    rows, llm_next, method, wrapper, region = self._hybrid_page(root, current, region, wrapper,
                                                                                claude_baseline, res)
                    if method == "claude":
                        claude_baseline = max(claude_baseline, len(rows))
            except InductionError as e:
                error = str(e)
            except Exception as e:  # noqa: BLE001 - e.g. API errors: report, keep earlier pages
                error = f"{type(e).__name__}: {e}"
            rows, _ = clean_rows(rows, self.schema)
            res.rows += rows
            nxt, rule = find_next(root, current)
            if nxt is None and llm_next:
                nxt, rule = llm_next, "claude"
            if page_no == 0:
                res.preview_html = annotate_preview(root, region)
            res.pages.append(PageResult(current, method, len(rows), len(region.records) if region else 0,
                                        round((time.perf_counter() - t0) * 1000, 2), nxt, rule,
                                        estimate_tokens(page_html), text_tokens, error))
            if error and not rows:
                break
            current = nxt
        res.rows, _ = clean_rows(res.rows, self.schema)
        res.wrapper = wrapper
        if self.llm is not None:
            res.usage = self.llm.usage.to_dict()
        return res

    def _hybrid_page(self, root, url, region, wrapper, baseline, res):
        if wrapper is not None:
            rows, reg = wrapper.extract(root, self.schema)
            if rows and len(rows) >= 0.5 * max(1, baseline):
                return rows, None, "wrapper", wrapper, reg
            res.notes.append(f"{url}: the learned wrapper found {len(rows)} rows, falling back to Claude")
        rows, nxt = self._claude_page(root, url, region)
        if wrapper is None and len(rows) >= 3:
            try:
                cand = induce(root, self.schema, rows[:2])
                got, _ = cand.extract(root, self.schema)
                f1 = score(got, rows, self.schema).total.f1
                if f1 >= VERIFY_F1:
                    cand.method = "learned-from-claude"
                    cand.notes.append(f"verified against Claude on page 1: F1 {f1:.3f}")
                    wrapper = cand
                    res.notes.append(f"learned a wrapper from Claude's rows (agreement F1 {f1:.3f}); "
                                     "next pages are extracted without the API")
                else:
                    res.notes.append(f"wrapper learned from Claude's rows agreed only {f1:.3f}; staying on Claude")
            except InductionError as e:
                res.notes.append(f"could not learn a wrapper from Claude's rows: {e}")
        return rows, nxt, "claude", wrapper, region

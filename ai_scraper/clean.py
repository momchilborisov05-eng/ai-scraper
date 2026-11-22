"""HTML cleaning, DOM -> compact text, and token-budgeted chunking for the LLM path.

The model never sees raw HTML. Markup is 60-80% of a typical page's bytes and carries little meaning, so the page
is rendered as compact text: one line per block, links as [text](url), images as ![alt](src), table rows as
`a | b | c`, and data that only lives in attributes (star ratings in class names, `datetime`, `title`) is kept.
Chunks are packed up to a token budget and split on block boundaries, preferring record boundaries when the
record detector found a repeated structure, so one listing item is never cut in half.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from urllib.parse import urljoin

from .dom import BLOCK, Node, Text, norm_space

DROP = {"script", "style", "noscript", "svg", "iframe", "template", "head", "canvas", "object", "embed", "form",
        "button", "select", "input", "textarea"}
BOILERPLATE = {"nav", "footer", "header", "aside"}
ATTR_HINT = re.compile(r"rating|stars?|score|badge|status|level", re.I)
CHARS_PER_TOKEN = 3.6   # calibrated on English listing text; see estimate_tokens


def estimate_tokens(text: str) -> int:
    """Cheap, offline token estimate (no tokenizer download). Used only to size chunks and compare page sizes;
    cost accounting always uses the token counts the API reports."""
    if not text:
        return 0
    words = len(text.split())
    return max(1, math.ceil(max(len(text) / CHARS_PER_TOKEN, words * 1.15)))


def is_boilerplate(node: Node) -> bool:
    if node.tag in BOILERPLATE:
        return True
    role = node.attrs.get("role", "")
    ident = (node.attrs.get("id", "") + " " + node.attrs.get("class", "")).lower()
    return role in ("navigation", "banner", "contentinfo") or bool(
        re.search(r"\b(cookie|consent|newsletter|advert|ads?|sponsor|share|social|breadcrumb)\b", ident))


def render_inline(node: Node, base_url: str, out: list[str], keep_boilerplate: bool) -> None:
    for c in node.children:
        if isinstance(c, Text):
            out.append(c.data)
            continue
        if c.tag in DROP or (not keep_boilerplate and is_boilerplate(c)):
            continue
        if c.tag == "img":
            alt, src = c.attrs.get("alt", "").strip(), c.attrs.get("src", "")
            out.append(f" ![{alt}]({urljoin(base_url, src)}) " if src else f" {alt} ")
            continue
        if c.tag == "br":
            out.append("\n")
            continue
        hint = []
        cls = c.attrs.get("class", "")
        if cls and ATTR_HINT.search(cls):
            hint.append(f"[{cls}]")
        for a in ("datetime", "content", "aria-label", "data-value", "data-rating"):
            if c.attrs.get(a):
                hint.append(f"[{a}={c.attrs[a]}]")
        if c.tag in ("abbr", "time") and c.attrs.get("title"):
            hint.append(f"[title={c.attrs['title']}]")
        if hint:
            out.append(" " + " ".join(hint) + " ")
        if c.tag == "a" and c.attrs.get("href") and not c.attrs["href"].startswith(("javascript:", "#", "mailto:")):
            inner: list[str] = []
            render_inline(c, base_url, inner, keep_boilerplate)
            text = norm_space("".join(inner)) or c.attrs.get("title", "")
            title = c.attrs.get("title", "")
            if title and title not in text:
                text = f"{text} (title: {title})" if text else title
            out.append(f" [{text}]({urljoin(base_url, c.attrs['href'])}) ")
            continue
        if c.tag == "tr":
            cells = []
            for td in c.elements:
                buf: list[str] = []
                render_inline(td, base_url, buf, keep_boilerplate)
                cells.append(norm_space("".join(buf)))
            out.append("\n" + " | ".join(cells) + "\n")
            continue
        if c.tag in BLOCK:
            out.append("\n")
        render_inline(c, base_url, out, keep_boilerplate)
        if c.tag in BLOCK:
            out.append("\n")


def to_text(node: Node, base_url: str = "", keep_boilerplate: bool = False) -> str:
    out: list[str] = []
    render_inline(node, base_url, out, keep_boilerplate)
    lines = (norm_space(line) for line in "".join(out).split("\n"))
    return "\n".join(line for line in lines if line)


@dataclass
class Chunk:
    text: str
    tokens: int
    records: int = 0


def blocks_for(root: Node, base_url: str, records: list[list[Node]] | None = None) -> list[tuple[str, bool]]:
    """Text blocks in document order, as (text, is_record). Each record (one or more consecutive sibling nodes)
    stays one block; everything else becomes one block per top-level child, so chunk boundaries fall between
    sections and never inside a listing item."""
    if not records:
        return [(b, False) for b in to_text(root, base_url).split("\n") if b]
    starts = {id(r[0]): r for r in records}
    inside = {id(n) for r in records for n in r}
    blocks: list[tuple[str, bool]] = []

    def walk(node: Node):
        for c in node.elements:
            if id(c) in starts:
                t = "\n".join(x for x in (to_text(n, base_url, keep_boilerplate=True) for n in starts[id(c)]) if x)
                if t:
                    blocks.append((t, True))
            elif id(c) in inside:
                continue
            elif any(id(n) in inside for n in c.iter()):
                walk(c)
            elif c.tag not in DROP and not is_boilerplate(c):
                t = to_text(c, base_url)
                if t:
                    blocks.append((t, False))

    walk(root)
    return blocks


def chunk_blocks(blocks: list[tuple[str, bool]], max_tokens: int) -> list[Chunk]:
    """Greedy packing under a token budget. A single block bigger than the budget is split on line breaks."""
    if max_tokens < 50:
        raise ValueError("max_tokens must be at least 50")
    chunks: list[Chunk] = []
    cur: list[str] = []
    cur_tokens = cur_records = 0

    def flush():
        nonlocal cur, cur_tokens, cur_records
        if cur:
            text = "\n\n".join(cur)
            chunks.append(Chunk(text, estimate_tokens(text), cur_records))
        cur, cur_tokens, cur_records = [], 0, 0

    for text, is_record in blocks:
        t = estimate_tokens(text)
        if t > max_tokens:
            flush()
            piece: list[str] = []
            for line in text.split("\n"):
                if piece and estimate_tokens("\n".join([*piece, line])) > max_tokens:
                    chunks.append(Chunk("\n".join(piece), estimate_tokens("\n".join(piece))))
                    piece = []
                piece.append(line[: int(max_tokens * CHARS_PER_TOKEN)])
            if piece:
                chunks.append(Chunk("\n".join(piece), estimate_tokens("\n".join(piece)), int(is_record)))
            continue
        if cur and cur_tokens + t > max_tokens:
            flush()
        cur.append(text)
        cur_tokens += t + 1
        cur_records += int(is_record)
    flush()
    return chunks or [Chunk("", 0)]

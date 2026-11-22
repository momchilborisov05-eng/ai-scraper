"""Find the "next page" link of a listing without an LLM.

Signals, strongest first: <link/a rel="next">, an anchor whose text or aria-label says next (or is an arrow),
an anchor inside an element whose class says "next", and finally a numbered pager: the link labelled current+1,
where the current page is marked with aria-current, an "active/current" class, or is the page number in the URL.
Links to another host and links back to the current URL are ignored.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from .dom import Node

NEXT_TEXT = re.compile(r"^\s*(next(\s+page)?|older( posts)?|more|load more|›|»|→|>|>>|next\s*[›»→>])\s*$", re.I)
NEXT_CLASS = re.compile(r"(^|[-_ ])next($|[-_ ])", re.I)
CURRENT_CLASS = re.compile(r"\b(active|current|selected|is-current)\b", re.I)
PAGE_PARAM = re.compile(r"(?:[?&](?:page|p|pg)=|/page[-/]?)(\d+)", re.I)


def _same_site(a: str, b: str) -> bool:
    return urlparse(a).netloc == urlparse(b).netloc


def _ok(href: str | None, page_url: str) -> str | None:
    if not href or href.startswith(("javascript:", "mailto:")) or href.strip() == "#":
        return None
    full = urljoin(page_url, href)
    if page_url and (not _same_site(full, page_url) or full.split("#")[0] == page_url.split("#")[0]):
        return None
    return full


def page_number(url: str) -> int | None:
    m = PAGE_PARAM.search(url)
    if m:
        return int(m.group(1))
    q = parse_qs(urlparse(url).query)
    for key in ("page", "p", "pg"):
        if key in q and q[key][0].isdigit():
            return int(q[key][0])
    return None


def find_next(root: Node, page_url: str = "") -> tuple[str | None, str]:
    """(absolute next-page URL or None, the rule that found it)."""
    anchors = [n for n in root.iter() if n.tag in ("a", "link")]
    for a in anchors:
        if "next" in a.attrs.get("rel", "").lower().split():
            url = _ok(a.attrs.get("href"), page_url)
            if url:
                return url, "rel=next"
    for a in anchors:
        if a.tag != "a":
            continue
        label = a.text() or a.attrs.get("aria-label", "") or a.attrs.get("title", "")
        if NEXT_TEXT.match(label) or re.match(r"^\s*next\b", a.attrs.get("aria-label", ""), re.I):
            url = _ok(a.attrs.get("href"), page_url)
            if url:
                return url, "link text"
    for a in anchors:
        if a.tag != "a":
            continue
        if any(NEXT_CLASS.search(c) for n in [a, *list(a.ancestors())[:2]] for c in n.classes):
            url = _ok(a.attrs.get("href"), page_url)
            if url:
                return url, "next class"
    # numbered pager
    current = None
    for n in root.iter():
        if n.attrs.get("aria-current") == "page" or (CURRENT_CLASS.search(n.attrs.get("class", ""))
                                                      and n.text().strip().isdigit()):
            if n.text().strip().isdigit():
                current = int(n.text().strip())
                break
    if current is None and page_url:
        current = page_number(page_url) or 1
    if current is not None:
        for a in anchors:
            if a.tag == "a" and a.text().strip() == str(current + 1):
                url = _ok(a.attrs.get("href"), page_url)
                if url:
                    return url, "numbered pager"
    return None, "none"

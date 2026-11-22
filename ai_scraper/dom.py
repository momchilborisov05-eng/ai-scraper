"""A small, forgiving HTML DOM built on the standard library's tokenizer.

Why not BeautifulSoup or lxml? The extraction core (record detection, wrapper induction) needs a tree it fully
controls: stable node ids, parent links, sibling indexes and a serializer that can annotate records for the UI.
`html.parser` gives the tokens; this module builds the tree with the few HTML recovery rules real pages need
(void elements, implicitly closed <p>/<li>/<tr>/<td>/<option>, stray end tags).
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from html import escape
from html.parser import HTMLParser

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}
# Opening one of these closes an open element of the listed kinds (the common HTML "optional end tag" rules).
AUTO_CLOSE = {
    "p": {"p"},
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "tr": {"tr", "td", "th"},
    "td": {"td", "th"},
    "th": {"td", "th"},
    "option": {"option"},
    "thead": {"tbody", "tfoot", "tr", "td", "th"},
    "tbody": {"thead", "tbody", "tfoot", "tr", "td", "th"},
    "tfoot": {"thead", "tbody", "tr", "td", "th"},
}
# Block-level elements stop an implicit close from searching further up (a <li> inside a nested <ul> must not
# close the outer <li>).
SCOPE = {"ul", "ol", "table", "div", "section", "article", "body", "html", "dl", "select", "main", "nav", "aside"}
BLOCK = {
    "address", "article", "aside", "blockquote", "dd", "details", "div", "dl", "dt", "fieldset", "figcaption",
    "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p",
    "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead", "tr", "ul", "br", "summary", "caption",
}
WS = re.compile(r"\s+")


def norm_space(text: str) -> str:
    return WS.sub(" ", text).strip()


class Node:
    """An element (or the synthetic document root). Text lives in `Text` children."""

    __slots__ = ("tag", "attrs", "children", "parent", "nid", "_text")

    def __init__(self, tag: str, attrs: dict[str, str] | None = None, parent: Node | None = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children: list[Node | Text] = []
        self.parent = parent
        self.nid = -1
        self._text: str | None = None

    # ----- navigation -----
    @property
    def elements(self) -> list[Node]:
        return [c for c in self.children if isinstance(c, Node)]

    @property
    def classes(self) -> tuple[str, ...]:
        return tuple(self.attrs.get("class", "").split())

    def iter(self) -> Iterator[Node]:
        """Pre-order traversal of this element and all descendant elements."""
        stack: list[Node] = [self]
        while stack:
            node = stack.pop()
            yield node
            stack.extend(reversed(node.elements))

    def find_all(self, tag: str) -> list[Node]:
        return [n for n in self.iter() if n.tag == tag and n is not self]

    def find(self, tag: str) -> Node | None:
        return next((n for n in self.iter() if n.tag == tag and n is not self), None)

    def ancestors(self) -> Iterator[Node]:
        node = self.parent
        while node is not None:
            yield node
            node = node.parent

    def depth(self) -> int:
        return sum(1 for _ in self.ancestors())

    # ----- content -----
    def text(self) -> str:
        """Visible text of the subtree, whitespace-collapsed. Cached: the tree is read-only after parsing."""
        if self._text is None:
            parts: list[str] = []
            self._collect(parts)
            self._text = norm_space(" ".join(parts))
        return self._text

    def _collect(self, parts: list[str]) -> None:
        for c in self.children:
            if isinstance(c, Text):
                parts.append(c.data)
            elif c.tag not in ("script", "style", "template", "noscript"):
                c._collect(parts)

    def own_text(self) -> str:
        return norm_space(" ".join(c.data for c in self.children if isinstance(c, Text)))

    def __repr__(self) -> str:
        cls = "." + ".".join(self.classes) if self.classes else ""
        return f"<{self.tag}{cls} #{self.nid}>"


class Text:
    __slots__ = ("data", "parent")

    def __init__(self, data: str, parent: Node):
        self.data = data
        self.parent = parent


class _Builder(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = Node("#document")
        self.stack: list[Node] = [self.root]

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        closes = AUTO_CLOSE.get(tag)
        if closes:
            for i in range(len(self.stack) - 1, 0, -1):
                open_tag = self.stack[i].tag
                if open_tag in closes:
                    del self.stack[i:]
                    break
                if open_tag in SCOPE:
                    break
        parent = self.stack[-1]
        node = Node(tag, {k.lower(): (v or "") for k, v in attrs}, parent)
        parent.children.append(node)
        if tag not in VOID:
            self.stack.append(node)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag.lower() not in VOID and self.stack[-1].tag == tag.lower():
            self.stack.pop()

    def handle_endtag(self, tag):
        tag = tag.lower()
        for i in range(len(self.stack) - 1, 0, -1):
            if self.stack[i].tag == tag:
                del self.stack[i:]
                return
        # a stray end tag with no open element is ignored, like browsers do

    def handle_data(self, data):
        if data:
            parent = self.stack[-1]
            parent.children.append(Text(data, parent))


def parse(html: str) -> Node:
    """Parse HTML into a tree and number every element in document order (`nid`)."""
    b = _Builder()
    b.feed(html)
    b.close()
    for i, node in enumerate(b.root.iter()):
        node.nid = i
    return b.root


def serialize(node: Node | Text, annotate: dict[int, dict[str, str]] | None = None) -> str:
    """Write the tree back to HTML. `annotate` maps a nid to extra attributes (used to outline records in the UI).
    Scripts, inline event handlers and javascript: URLs are dropped so the preview is inert."""
    if isinstance(node, Text):
        return escape(node.data, quote=False)
    if node.tag == "#document":
        return "".join(serialize(c, annotate) for c in node.children)
    if node.tag in ("script", "iframe", "object", "embed"):
        return ""
    attrs = {k: v for k, v in node.attrs.items() if not k.startswith("on")}
    for k in ("href", "src", "action"):
        if attrs.get(k, "").strip().lower().startswith("javascript:"):
            attrs.pop(k)
    if annotate and node.nid in annotate:
        attrs.update(annotate[node.nid])
    attr_s = "".join(f' {k}="{escape(v)}"' for k, v in attrs.items())
    if node.tag in VOID:
        return f"<{node.tag}{attr_s}>"
    inner = "".join(serialize(c, annotate) for c in node.children)
    return f"<{node.tag}{attr_s}>{inner}</{node.tag}>"

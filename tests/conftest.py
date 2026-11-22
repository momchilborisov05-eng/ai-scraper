"""Shared test helpers. No test touches the network: Claude is replaced by FakeClient."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from ai_scraper.schema import Field, Schema


def response(payload, stop="end_turn", inp=100, out=50, cache_read=0):
    text = payload if isinstance(payload, str) else json.dumps(payload)
    return SimpleNamespace(stop_reason=stop, content=[SimpleNamespace(type="text", text=text)],
                           usage=SimpleNamespace(input_tokens=inp, output_tokens=out,
                                                 cache_read_input_tokens=cache_read,
                                                 cache_creation_input_tokens=0))


class FakeClient:
    """Mimics client.beta.messages.create. `replies` is a list of responses or a function(kwargs) -> response."""

    def __init__(self, replies):
        self.replies = replies
        self.calls: list[dict] = []
        self.beta = SimpleNamespace(messages=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        if callable(self.replies):
            return self.replies(kwargs)
        return self.replies.pop(0)


@pytest.fixture
def books_schema():
    return Schema([Field("title"), Field("price", "number"), Field("rating", "integer"),
                   Field("in_stock", "boolean"), Field("url", "url")], "books")


LISTING = """<html><body>
<nav><a href="/">Home</a><a href="/about">About</a></nav>
<ul class="products">
  <li class="item"><h3><a href="/p/1" title="Alpha Book">Alpha</a></h3><p class="price">£10.50</p>
    <p class="star-rating Three"></p><p class="stock">In stock</p></li>
  <li class="item"><h3><a href="/p/2" title="Beta Book">Beta</a></h3><p class="price">£7.25</p>
    <p class="star-rating One"></p><p class="stock">Out of stock</p></li>
  <li class="item"><h3><a href="/p/3" title="Gamma Book">Gamma</a></h3><p class="price">£1,299.00</p>
    <p class="star-rating Five"></p><p class="stock">In stock</p></li>
  <li class="item"><h3><a href="/p/4" title="Delta Book">Delta</a></h3><p class="price">£3.00</p>
    <p class="star-rating Two"></p><p class="stock">In stock</p></li>
</ul>
<ul class="pager"><li class="next"><a href="page2.html">next</a></li></ul>
<footer>Copyright</footer><script>alert(1)</script>
</body></html>"""


@pytest.fixture
def listing():
    return LISTING

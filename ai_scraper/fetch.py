"""Fetching single pages politely: robots.txt, a delay between requests, a size cap, same-site pagination only.

Crawling whole sites is out of scope for this project; this module only follows a listing's next-page links.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from urllib import robotparser
from urllib.parse import urlparse

USER_AGENT = "ai-scraper/2.0 (+https://github.com/momchilborisov05-eng/ai-scraper)"
MAX_BYTES = 5_000_000


class FetchError(RuntimeError):
    pass


class Fetcher:
    def __init__(self, session=None, delay: float = 1.0, sleep: Callable[[float], None] = time.sleep):
        if session is None:
            import requests

            session = requests.Session()
        self.session = session
        self.session.headers.update({"User-Agent": USER_AGENT})
        self.delay = delay
        self.sleep = sleep
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._last = 0.0

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        host = f"{p.scheme}://{p.netloc}"
        if host not in self._robots:
            rp = None
            try:
                r = self.session.get(host + "/robots.txt", timeout=10)
                if r.status_code == 200:
                    rp = robotparser.RobotFileParser()
                    rp.parse(r.text.splitlines())
            except Exception:  # noqa: BLE001 - unreachable robots.txt means no rules
                rp = None
            self._robots[host] = rp
        rp = self._robots[host]
        return True if rp is None else rp.can_fetch(USER_AGENT, url)

    def __call__(self, url: str) -> str:
        if not url.startswith(("http://", "https://")):
            raise FetchError("the URL must start with http:// or https://")
        if not self.allowed(url):
            raise FetchError(f"robots.txt does not allow fetching {url}")
        wait = self._last + self.delay - time.monotonic()
        if self._last and wait > 0:
            self.sleep(wait)
        try:
            r = self.session.get(url, timeout=30)
            r.raise_for_status()
        except Exception as e:  # noqa: BLE001
            raise FetchError(f"could not download {url}: {e}") from e
        finally:
            self._last = time.monotonic()
        if len(r.content or b"") > MAX_BYTES:
            raise FetchError("page is larger than 5 MB")
        r.encoding = r.apparent_encoding or r.encoding
        return r.text


class DictFetcher:
    """Serves pages from memory (fixtures, tests, the web UI's pasted HTML)."""

    def __init__(self, pages: dict[str, str]):
        self.pages = pages

    def __call__(self, url: str) -> str:
        if url not in self.pages:
            raise FetchError(f"no saved page for {url}")
        return self.pages[url]

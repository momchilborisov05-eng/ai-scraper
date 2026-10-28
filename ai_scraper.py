"""
AI Scraper - extract structured data from any website by describing what you want.

No CSS selectors and no site-specific code: you give a URL and a plain-language
description of the data, Claude designs the table columns, reads each page and
returns clean rows. Pagination is followed automatically.

Usage:
    python ai_scraper.py URL "what to extract" [options]

Examples:
    python ai_scraper.py https://books.toscrape.com "book title, price, star rating, in stock" --pages 3
    python ai_scraper.py https://quotes.toscrape.com "quote text, author, tags" -o quotes.xlsx
    python ai_scraper.py https://www.scrapethissite.com/pages/simple/ "country, capital, population, area"
"""

import argparse
import csv
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib import robotparser
from urllib.parse import urljoin, urlparse

import anthropic
import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()

MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-5-5")
FALLBACK_MODELS = {"claude-opus-5-5", "claude-opus-5", "claude-fable-5-1", "claude-sonnet-5-5"}
USER_AGENT = "AI-Scraper/1.0 (+https://github.com/momchilborisov05-eng)"
CHUNK_CHARS = 60_000      # page text sent per request (about 15k tokens)
DELAY_SECONDS = 1.0       # be polite to the website between page requests
FIELD_TYPES = ["string", "number", "integer", "boolean"]


# ---------- Data structures ----------

@dataclass
class Field:
    name: str
    type: str
    description: str


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    requests: int = 0

    def add(self, usage) -> None:
        self.input_tokens += (usage.input_tokens + (usage.cache_read_input_tokens or 0)
                              + (getattr(usage, "cache_creation_input_tokens", 0) or 0))
        self.output_tokens += usage.output_tokens
        self.requests += 1


@dataclass
class ScrapeResult:
    fields: list[Field]
    rows: list[dict] = field(default_factory=list)
    pages: list[str] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)


class ScraperError(RuntimeError):
    """A problem the user can fix (blocked by robots.txt, bad URL, empty page...)."""


# ---------- Fetching and cleaning pages ----------

def allowed_by_robots(url: str, session: requests.Session) -> bool:
    """Respect the site's robots.txt. If it can't be read, assume scraping is allowed."""
    parts = urlparse(url)
    robots_url = f"{parts.scheme}://{parts.netloc}/robots.txt"
    try:
        response = session.get(robots_url, timeout=10)
    except requests.RequestException:
        return True
    if response.status_code != 200:
        return True
    parser = robotparser.RobotFileParser()
    parser.parse(response.text.splitlines())
    return parser.can_fetch(USER_AGENT, url)


def fetch(url: str, session: requests.Session) -> str:
    try:
        response = session.get(url, timeout=30)
        response.raise_for_status()
    except requests.RequestException as e:
        raise ScraperError(f"Could not download {url}: {e}") from e
    response.encoding = response.apparent_encoding or response.encoding
    return response.text


def page_to_text(html: str, base_url: str) -> str:
    """
    Turn HTML into compact text the model can read: visible text, links as
    [text](url), images as ![alt](url) and table rows joined with " | ".
    Scripts, styles and other invisible content are removed.
    """
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg", "iframe", "template", "head"]):
        tag.decompose()

    for img in soup.find_all("img"):
        alt = (img.get("alt") or "").strip()
        src = img.get("src")
        img.replace_with(f" ![{alt}]({urljoin(base_url, src)}) " if src else f" {alt} ")
    for a in soup.find_all("a"):
        href = a.get("href")
        text = a.get_text(" ", strip=True) or a.get("title", "")
        if href and not href.startswith(("javascript:", "#", "mailto:")):
            a.replace_with(f" [{text}]({urljoin(base_url, href)}) ")
    # Star ratings and similar data often live only in class names, e.g. <p class="star-rating Three">.
    for tag in soup.find_all(class_=re.compile(r"rating|stars?", re.I)):
        tag.insert(0, f" [{' '.join(tag.get('class', []))}] ")
    for tr in soup.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        tr.replace_with(" | ".join(cells) + "\n")

    text = soup.get_text("\n")
    lines = (re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines())
    return "\n".join(line for line in lines if line)


def chunks(text: str, size: int = CHUNK_CHARS) -> list[str]:
    """Split long pages on line breaks so no chunk is larger than `size` characters."""
    parts, current = [], ""
    for line in text.splitlines(keepends=True):
        if len(current) + len(line) > size and current:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts or [""]


# ---------- Talking to Claude ----------

class Extractor:
    def __init__(self, client):
        self.client = client
        self.usage = Usage()

    def _ask(self, system: str, prompt: str, schema: dict, max_tokens: int = 16000) -> dict:
        extra = {}
        if MODEL in FALLBACK_MODELS:
            extra = {"betas": ["server-side-fallback-2026-07-01"], "fallbacks": "default"}
        response = self.client.beta.messages.create(
            model=MODEL,
            max_tokens=max_tokens,
            system=system,
            messages=[{"role": "user", "content": prompt}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": schema}},
            **extra,
        )
        self.usage.add(response.usage)
        if response.stop_reason == "refusal":
            raise ScraperError("The model declined to process this page.")
        if response.stop_reason == "max_tokens":
            raise ScraperError("The answer was too long. Try fewer fields or a smaller page.")
        text = next(b.text for b in response.content if b.type == "text")
        return json.loads(text)

    def design_fields(self, request: str, sample_text: str) -> list[Field]:
        """Turn the user's description into a list of typed columns."""
        schema = {
            "type": "object",
            "properties": {
                "fields": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "name": {"type": "string"},
                            "type": {"type": "string", "enum": FIELD_TYPES},
                            "description": {"type": "string"},
                        },
                        "required": ["name", "type", "description"],
                        "additionalProperties": False,
                    },
                }
            },
            "required": ["fields"],
            "additionalProperties": False,
        }
        data = self._ask(
            system=(
                "You design table columns for a web scraper. Return one field per piece of data "
                "the user asked for, in the order they asked. Use short snake_case names. "
                "Use 'number' for prices and measurements (without currency symbols), 'integer' "
                "for counts and ratings, 'boolean' for yes/no facts and 'string' otherwise. "
                "Describe in one sentence how to read each value from the page."
            ),
            prompt=f"The user wants: {request}\n\nThe start of the page looks like this:\n\n{sample_text[:6000]}",
            schema=schema,
            max_tokens=4000,
        )
        fields = [Field(**f) for f in data["fields"]]
        if not fields:
            raise ScraperError("Could not work out any columns from your description.")
        return fields

    def extract(self, fields: list[Field], request: str, page_text: str, url: str) -> tuple[list[dict], str | None]:
        """Extract all matching items from one chunk of a page, plus the next page link."""
        item_schema = {
            "type": "object",
            "properties": {f.name: {"type": [f.type, "null"]} for f in fields},
            "required": [f.name for f in fields],
            "additionalProperties": False,
        }
        schema = {
            "type": "object",
            "properties": {
                "items": {"type": "array", "items": item_schema},
                "next_page_url": {"type": ["string", "null"]},
            },
            "required": ["items", "next_page_url"],
            "additionalProperties": False,
        }
        columns = "\n".join(f"- {f.name} ({f.type}): {f.description}" for f in fields)
        data = self._ask(
            system=(
                "You extract data from web pages for a scraper. Extract every item on the page that "
                "matches the user's request, exactly as shown on the page. Never invent values: use "
                "null when a value is missing. Convert numbers to plain numbers (no currency "
                "symbols or thousands separators). Ignore navigation, ads and footers. "
                "Set next_page_url to the absolute URL of the 'next page' link of the listing, or null "
                "if there is none."
            ),
            prompt=f"Request: {request}\n\nColumns:\n{columns}\n\nPage URL: {url}\n\nPage content:\n\n{page_text}",
            schema=schema,
        )
        return data["items"], data["next_page_url"]


# ---------- The scraping loop ----------

def scrape(url: str, request: str, max_pages: int = 1, client=None, session=None,
           log=print, delay: float = DELAY_SECONDS) -> ScrapeResult:
    session = session or requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    extractor = Extractor(client or anthropic.Anthropic())

    if not allowed_by_robots(url, session):
        raise ScraperError(f"The site's robots.txt does not allow scraping {url}.")

    first_text = page_to_text(fetch(url, session), url)
    if len(first_text) < 50:
        raise ScraperError("The page has almost no text. It may need JavaScript to load its content.")

    log("Designing columns...")
    result = ScrapeResult(fields=extractor.design_fields(request, first_text))
    log("Columns: " + ", ".join(f"{f.name} ({f.type})" for f in result.fields))

    seen: set[str] = set()
    page_url, page_text = url, first_text
    for page_number in range(1, max_pages + 1):
        next_url = None
        page_rows = 0
        for part in chunks(page_text):
            items, found_next = extractor.extract(result.fields, request, part, page_url)
            next_url = next_url or found_next
            for item in items:
                key = json.dumps(item, sort_keys=True, ensure_ascii=False)
                if key not in seen:  # skip duplicates across pages and chunks
                    seen.add(key)
                    result.rows.append(item)
                    page_rows += 1
        result.pages.append(page_url)
        log(f"[page {page_number}] {page_url} -> {page_rows} rows")

        if page_number == max_pages or not next_url:
            break
        next_url = urljoin(page_url, next_url)
        if next_url in result.pages or urlparse(next_url).netloc != urlparse(url).netloc:
            break  # stop on loops and links to other websites
        if not allowed_by_robots(next_url, session):
            log(f"Stopping: robots.txt does not allow {next_url}")
            break
        time.sleep(delay)
        page_url = next_url
        page_text = page_to_text(fetch(page_url, session), page_url)

    result.usage = extractor.usage
    return result


# ---------- Saving results ----------

def save(result: ScrapeResult, path: Path) -> None:
    headers = [f.name for f in result.fields]
    suffix = path.suffix.lower()
    if suffix == ".json":
        path.write_text(json.dumps(result.rows, indent=2, ensure_ascii=False), encoding="utf-8")
    elif suffix == ".csv":
        with path.open("w", newline="", encoding="utf-8-sig") as f:  # utf-8-sig opens correctly in Excel
            writer = csv.DictWriter(f, fieldnames=headers)
            writer.writeheader()
            writer.writerows(result.rows)
    elif suffix == ".xlsx":
        save_excel(result, path)
    else:
        raise ScraperError("Output must end in .xlsx, .csv or .json")


def save_excel(result: ScrapeResult, path: Path) -> None:
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Data"
    headers = [f.name for f in result.fields]
    ws.append(headers)
    for cell in ws[1]:
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = PatternFill("solid", fgColor="2F5597")
    for row in result.rows:
        ws.append([row.get(h) for h in headers])
    ws.freeze_panes = "A2"
    ws.auto_filter.ref = ws.dimensions
    for i, h in enumerate(headers, start=1):
        width = max([len(str(h))] + [len(str(r.get(h) or "")) for r in result.rows])
        ws.column_dimensions[get_column_letter(i)].width = min(width + 2, 60)

    info = wb.create_sheet("Columns")
    info.append(["Column", "Type", "How it was read"])
    for cell in info[1]:
        cell.font = Font(bold=True)
    for f in result.fields:
        info.append([f.name, f.type, f.description])
    info.append([])
    info.append(["Pages scraped"] + result.pages)
    info.column_dimensions["A"].width = 22
    info.column_dimensions["C"].width = 80
    wb.save(path)


# ---------- Command line ----------

def main():
    sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to a legacy code page
    parser = argparse.ArgumentParser(description="Extract structured data from any website with AI.")
    parser.add_argument("url", help="the page to start from")
    parser.add_argument("request", help='what to extract, e.g. "product name, price, rating"')
    parser.add_argument("--pages", type=int, default=1, help="how many pages to follow (default: 1)")
    parser.add_argument("-o", "--output", type=Path, default=Path("results.xlsx"),
                        help="output file: .xlsx, .csv or .json (default: results.xlsx)")
    args = parser.parse_args()

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("Missing API key. Copy .env.example to .env and add your key from console.anthropic.com.")
    if not args.url.startswith(("http://", "https://")):
        sys.exit("The URL must start with http:// or https://")

    start = time.time()
    try:
        result = scrape(args.url, args.request, max_pages=max(1, args.pages))
        save(result, args.output)
    except ScraperError as e:
        sys.exit(f"Error: {e}")
    except anthropic.AuthenticationError:
        sys.exit("Invalid API key. Check ANTHROPIC_API_KEY in your .env file.")
    except anthropic.RateLimitError:
        sys.exit("Rate limit reached. Wait a minute and try again.")
    except anthropic.APIStatusError as e:
        sys.exit(f"API error ({e.status_code}): {e.message}")
    except anthropic.APIConnectionError:
        sys.exit("Network error. Check your internet connection.")

    u = result.usage
    print(f"\nDone: {len(result.rows)} rows from {len(result.pages)} page(s) in {time.time() - start:.0f}s")
    print(f"Saved to: {args.output}")
    print(f"[{u.requests} AI requests - input tokens: {u.input_tokens:,}, output tokens: {u.output_tokens:,}]")


if __name__ == "__main__":
    main()

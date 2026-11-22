"""Command line: `python -m ai_scraper extract|eval|serve`.

    python -m ai_scraper extract https://books.toscrape.com/ \
        --request "title, price, star rating, in stock" --pages 3 -o books.xlsx
    python -m ai_scraper extract --file page.html --schema schema.json --examples examples.json -o out.csv
    python -m ai_scraper eval                     # offline extractors on every fixture
    python -m ai_scraper serve --port 5000
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .export import FORMATS, export
from .schema import Schema, schema_from_text


def _load_env() -> None:
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass


def _schema(args) -> Schema:
    if args.schema:
        return Schema.from_dict(json.loads(Path(args.schema).read_text(encoding="utf-8")))
    if args.request:
        return schema_from_text(args.request)
    raise SystemExit("give --request 'title, price, ...' or --schema schema.json")


def cmd_extract(args) -> int:
    from .fetch import Fetcher
    from .pipeline import Scraper

    schema = _schema(args)
    examples = json.loads(Path(args.examples).read_text(encoding="utf-8")) if args.examples else None
    llm = None
    if args.mode in ("claude", "hybrid"):
        from .cache import ResponseCache
        from .llm import ClaudeExtractor

        llm = ClaudeExtractor(cache=ResponseCache(args.cache))
    html = Path(args.file).read_text(encoding="utf-8") if args.file else None
    url = args.url or (Path(args.file).resolve().as_uri() if args.file else None)
    if not url:
        raise SystemExit("give a URL or --file page.html")
    scraper = Scraper(schema, args.mode, examples=examples, llm=llm, fetcher=Fetcher(delay=args.delay),
                      max_pages=args.pages)
    res = scraper.run(url, html)
    for p in res.pages:
        msg = f"  {p.method:9} {p.rows:4} rows  {p.ms:8.1f} ms  {p.url}"
        print(msg + (f"  ERROR {p.error}" if p.error else ""), file=sys.stderr)
    for n in res.notes:
        print(f"  note: {n}", file=sys.stderr)
    if res.usage:
        u = res.usage
        print(f"  claude: {u['requests']} requests, {u['input_tokens']} in / {u['output_tokens']} out tokens, "
              f"${u['cost_usd']:.4f}; cache hits {u['cache_hits']} (saved ${u['saved_usd']:.4f})", file=sys.stderr)
    fmt = (args.output.rsplit(".", 1)[-1].lower() if args.output else "json")
    if fmt not in FORMATS:
        raise SystemExit(f"unknown output format .{fmt}; use .csv, .json or .xlsx")
    data = export(res.rows, schema, fmt)
    if args.output:
        Path(args.output).write_bytes(data)
        print(f"{len(res.rows)} rows -> {args.output}", file=sys.stderr)
    else:
        sys.stdout.write(data.decode("utf-8"))
    return 0 if res.rows else 1


def cmd_eval(args) -> int:
    from .evaluate import OFFLINE_EXTRACTORS, evaluate_fixture, load_fixtures, summarize

    fixtures = [f for f in load_fixtures() if not args.fixture or f.name in args.fixture]
    results = []
    for fx in fixtures:
        for ex in args.extractor or OFFLINE_EXTRACTORS:
            r = evaluate_fixture(fx, ex)
            results.append(r)
            print(f"{fx.name:16} {ex:12} F1 {r['f1']:.3f}  P {r['precision']:.3f}  R {r['recall']:.3f}  "
                  f"rows {r['pred_rows']}/{r['gold_rows']}")
    print()
    for k, v in summarize(results).items():
        print(f"{k:24} F1 {v['f1']:.3f}  P {v['precision']:.3f}  R {v['recall']:.3f}")
    return 0


def cmd_serve(args) -> int:
    from .web.app import create_app

    create_app().run(host=args.host, port=args.port, debug=False)
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_env()
    ap = argparse.ArgumentParser(prog="ai_scraper", description="Evaluated structured extraction from web pages.")
    ap.add_argument("--version", action="version", version=__version__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("extract", help="extract rows from a URL or a saved HTML file")
    e.add_argument("url", nargs="?")
    e.add_argument("--file", help="read the first page from a local HTML file")
    e.add_argument("--request", help="plain-language columns, e.g. 'title, price, rating (1-5), in stock'")
    e.add_argument("--schema", help="JSON file with {fields: [{name, type, description}]}")
    e.add_argument("--examples", help="JSON file with 1-2 example rows (enables wrapper induction)")
    e.add_argument("--mode", default="auto", choices=["auto", "heuristic", "induction", "claude", "hybrid"])
    e.add_argument("--pages", type=int, default=1, help="follow next-page links up to this many pages")
    e.add_argument("--delay", type=float, default=1.0, help="seconds between requests to the same site")
    e.add_argument("--cache", default=".cache/responses.sqlite", help="Claude response cache (SQLite)")
    e.add_argument("-o", "--output", help="out.csv | out.json | out.xlsx (default: JSON to stdout)")
    e.set_defaults(func=cmd_extract)

    v = sub.add_parser("eval", help="score extractors on the saved fixtures")
    v.add_argument("--fixture", action="append", help="limit to one fixture (repeatable)")
    v.add_argument("--extractor", action="append", help="heuristic | induction-1 | induction-2 (repeatable)")
    v.set_defaults(func=cmd_eval)

    s = sub.add_parser("serve", help="run the web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=5000)
    s.set_defaults(func=cmd_serve)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())

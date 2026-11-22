"""Flask JSON API + the static vanilla-JS UI.

Fetching arbitrary URLs from a server is an SSRF risk, so the web app only extracts from the bundled fixtures and
from HTML the user pastes, unless AISCRAPER_ALLOW_FETCH=1 is set (for a local, single-user install). Claude modes
are offered only when ANTHROPIC_API_KEY is set; everything else runs offline.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import socket
import time
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from .. import __version__
from ..evaluate import OFFLINE_EXTRACTORS, evaluate_fixture, load_fixtures, score, summarize
from ..export import FORMATS, export
from ..fetch import DictFetcher, Fetcher
from ..pipeline import MODES, Scraper
from ..schema import Schema, SchemaError, schema_from_text

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MAX_HTML = 2_000_000
MAX_PAGES = 10

CSP = (
    "default-src 'self'; script-src 'self'; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; connect-src 'self'; frame-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
    "form-action 'self'"
)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.message = message
        self.status = status


def _json_body() -> dict:
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ApiError("request body must be a JSON object")
    return data


def _public_url(url: str) -> str:
    """Allow only http(s) URLs whose host resolves to public addresses (no localhost, LAN or metadata IPs)."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        raise ApiError("the URL must start with http:// or https://")
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        raise ApiError(f"cannot resolve {p.hostname}") from None
    for info in infos:
        ip = ipaddress.ip_address(info[4][0])
        if not ip.is_global:
            raise ApiError("private, loopback and link-local addresses are not allowed")
    return url


def _schema_from(data: dict) -> Schema:
    try:
        if isinstance(data.get("schema"), dict):
            return Schema.from_dict(data["schema"])
        req = data.get("request")
        if isinstance(req, str) and req.strip():
            return schema_from_text(req[:2000])
    except (SchemaError, KeyError, TypeError) as e:
        raise ApiError(f"invalid schema: {e}") from None
    raise ApiError("give 'schema' or 'request'")


def create_app(llm_factory=None, allow_fetch: bool | None = None) -> Flask:
    """`llm_factory()` returns a ClaudeExtractor-like object; tests pass a fake. Default: real client if a key is set."""
    if allow_fetch is None:
        allow_fetch = os.environ.get("AISCRAPER_ALLOW_FETCH") == "1"
    if llm_factory is None and os.environ.get("ANTHROPIC_API_KEY"):
        def llm_factory():
            from ..cache import ResponseCache
            from ..llm import ClaudeExtractor

            path = os.environ.get("AISCRAPER_CACHE", ".cache/responses.sqlite")
            return ClaudeExtractor(cache=ResponseCache(path))

    fixtures = {f.name: f for f in load_fixtures()}
    eval_cache: dict = {}
    started = time.time()

    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False
    app.config["MAX_CONTENT_LENGTH"] = MAX_HTML + 200_000

    @app.after_request
    def security_headers(resp):
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
        if resp.mimetype == "text/html":
            resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp

    @app.errorhandler(ApiError)
    def api_error(e: ApiError):
        return jsonify(error=e.message), e.status

    @app.errorhandler(HTTPException)
    def http_error(e: HTTPException):
        return jsonify(error=e.description or e.name), e.code

    @app.errorhandler(Exception)
    def unhandled(e: Exception):
        log.exception("unhandled error")
        return jsonify(error="internal server error"), 500

    # pages -----------------------------------------------------------------------------------------------------
    @app.get("/")
    def index_page():
        return send_from_directory(STATIC, "index.html")

    @app.get("/eval")
    def eval_page():
        return send_from_directory(STATIC, "eval.html")

    @app.get("/static/<path:name>")
    def static_files(name: str):
        return send_from_directory(STATIC, name, max_age=3600)

    # api -------------------------------------------------------------------------------------------------------
    @app.get("/health")
    def health():
        return jsonify(status="ok", version=__version__, fixtures=len(fixtures),
                       uptime_seconds=round(time.time() - started, 1),
                       claude=llm_factory is not None, fetch=allow_fetch)

    @app.get("/api/fixtures")
    def api_fixtures():
        return jsonify(fixtures=[{
            "name": f.name, "request": f.request, "notes": f.notes, "pages": len(f.pages), "records": len(f.records),
            "split": "holdout" if f.name.startswith("holdout") else "dev", "start_url": f.start_url,
            "schema": f.schema.to_dict(), "examples": f.examples,
        } for f in fixtures.values()], modes=list(MODES), claude=llm_factory is not None, fetch=allow_fetch)

    @app.post("/api/schema")
    def api_schema():
        data = _json_body()
        req = data.get("request")
        if not isinstance(req, str) or not req.strip():
            raise ApiError("'request' is required")
        if data.get("use_claude") and llm_factory is not None:
            try:
                return jsonify(schema=llm_factory().design_schema(req[:2000]).to_dict(), source="claude")
            except Exception as e:  # noqa: BLE001
                raise ApiError(f"Claude could not design the columns: {e}", 502) from None
        try:
            return jsonify(schema=schema_from_text(req[:2000]).to_dict(), source="rules")
        except SchemaError as e:
            raise ApiError(str(e)) from None

    @app.post("/api/extract")
    def api_extract():
        data = _json_body()
        mode = data.get("mode", "auto")
        if mode not in MODES:
            raise ApiError(f"'mode' must be one of {', '.join(MODES)}")
        try:
            pages = int(data.get("pages", 1))
        except (TypeError, ValueError):
            raise ApiError("'pages' must be an integer") from None
        if not 1 <= pages <= MAX_PAGES:
            raise ApiError(f"'pages' must be between 1 and {MAX_PAGES}")
        schema = _schema_from(data)
        examples = data.get("examples") or None
        if examples is not None and (not isinstance(examples, list) or len(examples) > 5):
            raise ApiError("'examples' must be a list of at most 5 objects")

        fx, html = None, None
        if data.get("fixture"):
            fx = fixtures.get(data["fixture"])
            if fx is None:
                raise ApiError("unknown fixture", 404)
            url, fetcher = fx.start_url, DictFetcher(fx.pages)
        elif isinstance(data.get("html"), str) and data["html"].strip():
            html = data["html"]
            if len(html) > MAX_HTML:
                raise ApiError("the pasted HTML is larger than 2 MB")
            url = data.get("base_url") or "https://example.com/"
            fetcher, pages = DictFetcher({}), 1
        elif data.get("url"):
            if not allow_fetch:
                raise ApiError("fetching URLs is disabled on this server; paste the page HTML or set "
                               "AISCRAPER_ALLOW_FETCH=1 on a local install", 403)
            url = _public_url(str(data["url"]))
            base = Fetcher(delay=1.0)

            def fetcher(u: str) -> str:
                return base(_public_url(u))
        else:
            raise ApiError("give 'fixture', 'html' or 'url'")

        llm = None
        if mode in ("claude", "hybrid") or (mode == "auto" and not examples and llm_factory is not None):
            if llm_factory is None:
                raise ApiError("Claude modes need ANTHROPIC_API_KEY on the server; use heuristic or induction")
            llm = llm_factory()
        try:
            scraper = Scraper(schema, mode, examples=examples, llm=llm, fetcher=fetcher, max_pages=pages)
        except ValueError as e:
            raise ApiError(str(e)) from None
        t0 = time.perf_counter()
        res = scraper.run(url, html)
        out = res.to_dict(include_preview=True)
        out["seconds"] = round(time.perf_counter() - t0, 4)
        if fx is not None:
            sc = score(res.rows, fx.records, fx.schema, exclude=fx.examples if examples else None)
            out["score"] = sc.to_dict()
            out["score"]["note"] = ("compared with the fixture's hand-checked rows"
                                    + (", the example rows excluded" if examples else ""))
        return jsonify(out)

    @app.get("/api/eval")
    def api_eval():
        if "data" not in eval_cache:
            results = [evaluate_fixture(fx, ex) for fx in fixtures.values() for ex in OFFLINE_EXTRACTORS]
            for r in results:
                r.pop("notes", None)
            eval_cache["data"] = {"results": results, "summary": summarize(results),
                                  "extractors": list(OFFLINE_EXTRACTORS)}
        return jsonify(eval_cache["data"])

    @app.post("/api/export")
    def api_export():
        data = _json_body()
        fmt = data.get("format", "csv")
        if fmt not in FORMATS:
            raise ApiError("'format' must be csv, json or xlsx")
        rows = data.get("rows")
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            raise ApiError("'rows' must be a list of objects")
        schema = _schema_from(data)
        body = export(rows, schema, fmt)
        name = "".join(c for c in str(data.get("filename") or "extract") if c.isalnum() or c in "-_")[:60] or "extract"
        return Response(body, mimetype=FORMATS[fmt],
                        headers={"Content-Disposition": f'attachment; filename="{name}.{fmt}"'})

    return app

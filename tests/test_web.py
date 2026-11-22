import pytest

from ai_scraper.web import create_app


class FakeLLM:
    def __init__(self):
        from ai_scraper.cache import Usage

        self.usage = Usage()

    def extract(self, schema, chunks, url, request=""):
        return [], None

    def design_schema(self, request, sample_text=""):
        from ai_scraper.schema import schema_from_text

        return schema_from_text(request)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("AISCRAPER_ALLOW_FETCH", raising=False)
    return create_app().test_client()


def test_health_and_security_headers(client):
    r = client.get("/health")
    assert r.status_code == 200 and r.json["status"] == "ok" and r.json["claude"] is False
    page = client.get("/")
    assert "default-src 'self'" in page.headers["Content-Security-Policy"]
    assert page.headers["X-Content-Type-Options"] == "nosniff"
    assert client.get("/eval").status_code == 200


def test_fixtures_list(client):
    d = client.get("/api/fixtures").json
    assert len(d["fixtures"]) >= 14 and "induction" in d["modes"]
    assert {f["split"] for f in d["fixtures"]} == {"dev", "holdout"}


def test_extract_fixture_is_scored(client):
    fx = next(f for f in client.get("/api/fixtures").json["fixtures"] if f["name"] == "books")
    r = client.post("/api/extract", json={"fixture": "books", "schema": fx["schema"], "examples": fx["examples"],
                                          "mode": "induction", "pages": 3})
    assert r.status_code == 200
    assert r.json["score"]["f1"] == 1.0 and len(r.json["pages"]) == 3 and r.json["preview_html"]


def test_extract_pasted_html_without_api(client):
    html = "<ul>" + "".join(f'<li><a href="/p{i}">Item {i}</a> <b>${i}.50</b></li>' for i in range(4)) + "</ul>"
    r = client.post("/api/extract", json={"html": html, "request": "name, price, link", "mode": "heuristic",
                                          "base_url": "https://s.test/"})
    assert r.status_code == 200
    assert r.json["rows"][2] == {"name": "Item 2", "price": 2.5, "link": "https://s.test/p2"}
    assert "score" not in r.json


def test_url_fetching_is_off_by_default(client):
    r = client.post("/api/extract", json={"url": "https://example.com", "request": "title"})
    assert r.status_code == 403


def test_private_addresses_are_blocked_when_fetching_is_on(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = create_app(allow_fetch=True).test_client()
    for url in ("http://127.0.0.1/", "http://169.254.169.254/latest", "ftp://x.test/"):
        assert c.post("/api/extract", json={"url": url, "request": "title"}).status_code == 400


def test_claude_modes_need_a_key(client):
    r = client.post("/api/extract", json={"fixture": "books", "request": "title", "mode": "claude"})
    assert r.status_code == 400 and "ANTHROPIC_API_KEY" in r.json["error"]


def test_claude_mode_with_injected_llm(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    c = create_app(llm_factory=FakeLLM).test_client()
    assert c.get("/health").json["claude"] is True
    r = c.post("/api/extract", json={"fixture": "events", "request": "name, date", "mode": "claude"})
    assert r.status_code == 200 and r.json["pages"][0]["method"] == "claude"
    assert c.post("/api/schema", json={"request": "title, price", "use_claude": True}).json["source"] == "claude"


@pytest.mark.parametrize("body,status", [
    ({"fixture": "nope", "request": "a"}, 404), ({"fixture": "books"}, 400),
    ({"fixture": "books", "request": "a", "mode": "magic"}, 400), ({"fixture": "books", "request": "a", "pages": 99}, 400),
    ({"request": "a"}, 400), ({"fixture": "books", "schema": {"fields": [{"name": "Bad Name"}]}}, 400),
])
def test_extract_validation(client, body, status):
    assert client.post("/api/extract", json=body).status_code == status


def test_schema_suggestion_offline(client):
    d = client.post("/api/schema", json={"request": "title, price, in stock (yes/no)"}).json
    assert d["source"] == "rules" and [f["type"] for f in d["schema"]["fields"]] == ["string", "number", "boolean"]


def test_export_endpoint(client):
    r = client.post("/api/export", json={"format": "csv", "rows": [{"a": "x"}], "schema": {"fields": [{"name": "a"}]},
                                         "filename": "../../evil name"})
    assert r.status_code == 200 and r.mimetype == "text/csv"
    assert 'filename="evilname.csv"' in r.headers["Content-Disposition"]
    assert client.post("/api/export", json={"format": "exe", "rows": []}).status_code == 400


def test_eval_endpoint(client):
    d = client.get("/api/eval").json
    assert d["summary"]["induction-2@dev"]["f1"] == 1.0
    assert len(d["results"]) == 3 * 14

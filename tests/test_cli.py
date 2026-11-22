import json

from ai_scraper.__main__ import main


def test_cli_extracts_a_saved_page_to_json(tmp_path, capsys):
    page = tmp_path / "page.html"
    page.write_text("<ul>" + "".join(f'<li><h3>Book {i}</h3><p class="price">£{i}.99</p></li>' for i in range(5))
                    + "</ul>", encoding="utf-8")
    assert main(["extract", "--file", str(page), "--request", "title, price", "--mode", "heuristic"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert rows[3] == {"title": "Book 3", "price": 3.99}


def test_cli_writes_csv_and_runs_eval(tmp_path, capsys):
    page = tmp_path / "p.html"
    page.write_text("<ul>" + "".join(f"<li><b>N{i}</b> <i>{i}</i></li>" for i in range(4)) + "</ul>", encoding="utf-8")
    out = tmp_path / "o.csv"
    main(["extract", "--file", str(page), "--request", "name", "--mode", "heuristic", "-o", str(out)])
    assert out.read_bytes().startswith(b"\xef\xbb\xbfname")
    assert main(["eval", "--fixture", "books", "--extractor", "induction-2"]) == 0
    assert "induction-2@dev" in capsys.readouterr().out

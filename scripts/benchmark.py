"""Benchmark: accuracy (field P/R/F1), speed and token/cost savings on the saved fixtures.

    python scripts/benchmark.py                 # offline extractors only (free, ~10 s)
    python scripts/benchmark.py --claude        # also Claude + hybrid with the real API (costs money, capped)
    python scripts/benchmark.py --smoke         # CI: one repeat, no files written

Writes examples/benchmark.json, examples/benchmark.md and the block between the bench markers in README.md.
A run without --claude keeps the Claude section of the previous benchmark.json (with its date) instead of
deleting it, so offline re-runs never erase measured API numbers - and never invent them either.
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import statistics
import sys
import time
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from ai_scraper.cache import ResponseCache, Usage  # noqa: E402
from ai_scraper.clean import blocks_for, estimate_tokens, to_text  # noqa: E402
from ai_scraper.dom import parse  # noqa: E402
from ai_scraper.evaluate import (  # noqa: E402
    OFFLINE_EXTRACTORS,
    evaluate_fixture,
    load_fixtures,
    summarize,
)
from ai_scraper.induction import detect_regions  # noqa: E402

OUT = ROOT / "examples"
README = ROOT / "README.md"
START, END = "<!-- bench:start -->", "<!-- bench:end -->"


# ---------- offline ----------
def offline(fixtures, repeat: int) -> list[dict]:
    results = []
    for fx in fixtures:
        for ex in OFFLINE_EXTRACTORS:
            runs = [evaluate_fixture(fx, ex) for _ in range(repeat)]
            r = runs[0]
            r["seconds"] = round(statistics.median(x["seconds"] for x in runs), 5)
            r.pop("notes", None)
            r.pop("usage", None)
            results.append(r)
    return results


def speed(results: list[dict]) -> dict:
    out = {}
    for ex in sorted({r["extractor"] for r in results}):
        rs = [r for r in results if r["extractor"] == ex]
        pages = sum(r["pages"] for r in rs)
        secs = sum(r["seconds"] for r in rs)
        out[ex] = {"pages": pages, "seconds": round(secs, 4), "pages_per_s": round(pages / secs, 1) if secs else None,
                   "ms_per_page": round(1000 * secs / pages, 2) if pages else None}
    return out


def tokens(fixtures) -> dict:
    """Estimated tokens (offline estimator, not a tokenizer) of what Claude would be sent per page."""
    per = []
    for fx in fixtures:
        for url, html in fx.pages.items():
            root = parse(html)
            regions = detect_regions(root)
            recs = [r.nodes for r in regions[0].records] if regions else None
            per.append({"fixture": fx.name, "url": url, "html": estimate_tokens(html),
                        "text": estimate_tokens(to_text(root, url)),
                        "sent": estimate_tokens("\n\n".join(t for t, _ in blocks_for(root, url, recs)))})
    tot = {k: sum(p[k] for p in per) for k in ("html", "text", "sent")}
    return {"pages": len(per), "total": tot,
            "reduction_text_pct": round(100 * (1 - tot["text"] / tot["html"]), 1),
            "reduction_sent_pct": round(100 * (1 - tot["sent"] / tot["html"]), 1),
            "per_page": per}


def cache_stub(fixtures) -> dict:
    """Cache behaviour with a stub client (no network): run Claude mode twice over every page with one cache."""
    from ai_scraper.llm import ClaudeExtractor

    class Resp:
        stop_reason = "end_turn"

        def __init__(self):
            self.content = [type("B", (), {"type": "text", "text": '{"items": [], "next_page_url": null}'})()]
            self.usage = type("U", (), {"input_tokens": 0, "output_tokens": 0})()

    class Stub:
        calls = 0

        class beta:  # noqa: N801 - mimics client.beta.messages.create
            class messages:  # noqa: N801
                @staticmethod
                def create(**kw):
                    Stub.calls += 1
                    return Resp()

    cache = ResponseCache()
    out = []
    for run in (1, 2):
        Stub.calls = 0
        t0 = time.perf_counter()
        for fx in fixtures:
            llm = ClaudeExtractor(client=Stub, cache=cache)
            from ai_scraper.evaluate import run_extractor

            run_extractor(fx, "claude", llm)
        out.append({"run": run, "api_calls": Stub.calls, "seconds": round(time.perf_counter() - t0, 3)})
    return {"runs": out, "entries": len(cache)}


# ---------- Claude (real API, opt-in) ----------
class BudgetGuard:
    """Wraps a ClaudeExtractor; refuses new pages once the job has spent `limit` dollars."""

    def __init__(self, llm, spent: list[float], limit: float):
        self.llm, self.spent, self.limit = llm, spent, limit

    @property
    def usage(self):
        return self.llm.usage

    def extract(self, *a, **kw):
        if self.spent[0] + self.llm.usage.cost > self.limit:
            raise RuntimeError(f"benchmark budget ${self.limit:.2f} reached")
        return self.llm.extract(*a, **kw)


class NoNetwork:
    """--rescore: answers must come from the response cache; a miss is an error, never a paid call."""

    class beta:  # noqa: N801
        class messages:  # noqa: N801
            @staticmethod
            def create(**kw):
                raise RuntimeError("cache miss during --rescore; run --claude instead")


def claude_runs(fixtures, budget: float, rescore: bool = False) -> dict:
    from ai_scraper.llm import ClaudeExtractor

    cache = ResponseCache(ROOT / ".cache" / "benchmark.sqlite")
    if not rescore:
        cache.clear()  # measure cold, then warm
    spent = [0.0]
    results, totals = [], {}
    for phase in ("claude", "hybrid", "claude-rerun"):
        ex = "claude" if phase == "claude-rerun" else phase
        tot = Usage()
        for fx in fixtures:
            llm = ClaudeExtractor(cache=cache, client=NoNetwork() if rescore else None)
            guard = BudgetGuard(llm, spent, budget)
            r = evaluate_fixture(fx, ex, guard)
            u = llm.usage
            spent[0] += u.cost
            for k in ("requests", "input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens",
                      "cache_hits", "cache_misses", "saved_tokens", "repairs"):
                setattr(tot, k, getattr(tot, k) + getattr(u, k))
            tot.cost += u.cost
            tot.saved_cost += u.saved_cost
            r["extractor"] = phase
            r.pop("notes", None)
            results.append(r)
            print(f"  {phase:12} {fx.name:16} F1 {r['f1']:.3f}  calls {u.requests}  hits {u.cache_hits}  "
                  f"${u.cost:.4f}  {r['errors'][:1]}", flush=True)
        totals[phase] = tot.to_dict()
    return {"model": ClaudeExtractor(client=object()).model, "date": date.today().isoformat(), "budget_usd": budget,
            "spent_usd": round(spent[0], 4), "results": results, "totals": totals,
            "summary": summarize([r for r in results if r["extractor"] != "claude-rerun"])}


# ---------- report ----------
def fmt(x, nd=3):
    return "-" if x is None else f"{x:.{nd}f}"


def markdown(data: dict) -> str:
    s, sp, tk = data["summary"], data["speed"], data["tokens"]
    exs = list(OFFLINE_EXTRACTORS)
    lines = [f"Measured {data['date']} on {data['machine']}, Python {data['python']}. "
             f"{data['fixtures']} saved page sets ({data['pages']} pages, {data['gold_rows']} hand-checked rows); "
             "the 2 example rows of each set are excluded from scoring for every extractor.", "",
             "**Field-level accuracy (micro-averaged over all cells)**", "",
             "| Extractor | Needs | Dev F1 (10 sets, used for tuning) | Held-out F1 (4 sets) | All: P / R / F1 | Pages/s |",
             "|---|---|---|---|---|---|"]
    needs = {"heuristic": "column names only", "induction-1": "+ 1 example row", "induction-2": "+ 2 example rows"}
    for ex in exs:
        a = s[ex]
        lines.append(f"| {ex} | {needs[ex]} | {fmt(s[ex + '@dev']['f1'])} | {fmt(s[ex + '@holdout']['f1'])} | "
                     f"{fmt(a['precision'])} / {fmt(a['recall'])} / **{fmt(a['f1'])}** | {sp[ex]['pages_per_s']} |")
    c = data.get("claude")
    if c:
        cs = c["summary"]
        for ex in ("claude", "hybrid"):
            if ex in cs:
                t = c["totals"][ex]
                lines.append(f"| {ex} ({c['model']}) | column names + API | {fmt(cs.get(ex + '@dev', {}).get('f1'))} | "
                             f"{fmt(cs.get(ex + '@holdout', {}).get('f1'))} | {fmt(cs[ex]['precision'])} / "
                             f"{fmt(cs[ex]['recall'])} / **{fmt(cs[ex]['f1'])}** | "
                             f"{t['requests'] + t['cache_hits']} Claude calls for {data['pages']} pages, "
                             f"${t['cost_usd'] + t['saved_usd']:.3f} |")
    else:
        lines.append("| claude / hybrid | column names + API | not measured | not measured | not measured | - |")
    lines += ["", "**Per page set (F1)**", "", "| Set | Split | Pages | " + " | ".join(exs) +
              (" | claude | hybrid |" if c else " |"), "|---|---|---|" + "---|" * (len(exs) + (2 if c else 0))]
    by = {(r["fixture"], r["extractor"]): r for r in data["results"]}
    cby = {(r["fixture"], r["extractor"]): r for r in (c["results"] if c else [])}
    for name in sorted({r["fixture"] for r in data["results"]}, key=lambda n: (n.startswith("holdout"), n)):
        r0 = by[(name, exs[0])]
        cells = [fmt(by[(name, ex)]["f1"]) for ex in exs]
        if c:
            cells += [fmt(cby[(name, ex)]["f1"]) if (name, ex) in cby else "-" for ex in ("claude", "hybrid")]
        lines.append(f"| {name} | {r0['split']} | {r0['pages']} | " + " | ".join(cells) + " |")
    lines += ["", "**What Claude would be sent (estimated tokens, offline estimator)**", "",
              f"Raw HTML {tk['total']['html']:,} -> cleaned page text {tk['total']['text']:,} "
              f"(-{tk['reduction_text_pct']}%) -> blocks actually sent, boilerplate dropped {tk['total']['sent']:,} "
              f"(-{tk['reduction_sent_pct']}%) over {tk['pages']} pages.", ""]
    cb = data["cache_stub"]["runs"]
    lines += [f"**Response cache (stub client, no network):** first pass {cb[0]['api_calls']} API calls, "
              f"second pass over the same pages {cb[1]['api_calls']}.", ""]
    if c:
        t1, t2, th = c["totals"]["claude"], c["totals"]["claude-rerun"], c["totals"]["hybrid"]
        lines += [f"**Real API run ({c['date']}, {c['model']}):** Claude mode {t1['requests']} calls, "
                  f"{t1['input_tokens']:,} input + {t1['output_tokens']:,} output tokens, ${t1['cost_usd']:.4f}. "
                  f"Hybrid needed {th['requests'] + th['cache_hits']} Claude responses for the same {data['pages']} "
                  f"pages (one per set, plus one where the learned wrapper failed verification; the rest ran on the "
                  f"wrapper); in this run they were served from the Claude run's cache, so it cost "
                  f"${th['cost_usd']:.4f} instead of ${th['saved_usd']:.4f}. Re-running Claude mode: "
                  f"{t2['requests']} calls, {t2['cache_hits']} cache hits, ${t2['cost_usd']:.4f} "
                  f"(${t2['saved_usd']:.4f} not spent). Total spent: ${c['spent_usd']:.4f}."
                  + (f" Scores re-computed from the cached responses on {c['rescored']}." if c.get("rescored") else ""),
                  "", "Where Claude loses points it writes 0 for a value the page states as \"Free\" or \"No reviews "
                  "yet\"; the hand-checked rows use null there. That is a labelling convention, and the gold rows were "
                  "left as they are.", ""]
    else:
        lines += ["**Claude modes:** not measured (run `python scripts/benchmark.py --claude` with an API key).", ""]
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repeat", type=int, default=5, help="timing repeats per run (median is reported)")
    ap.add_argument("--claude", action="store_true", help="also run Claude and hybrid modes against the real API")
    ap.add_argument("--budget", type=float, default=0.80, help="hard spending cap in USD for --claude")
    ap.add_argument("--rescore", action="store_true",
                    help="re-score the last --claude run from its response cache (no API calls, cost totals kept)")
    ap.add_argument("--smoke", action="store_true", help="CI mode: 1 repeat, nothing written")
    ap.add_argument("--out", default=str(OUT))
    args = ap.parse_args()

    fixtures = load_fixtures()
    t0 = time.perf_counter()
    results = offline(fixtures, 1 if args.smoke else args.repeat)
    data = {
        "date": date.today().isoformat(), "python": platform.python_version(),
        "machine": os.environ.get("BENCH_MACHINE", f"{platform.system()} {platform.release()} ({platform.machine()})"),
        "fixtures": len(fixtures), "pages": sum(len(f.pages) for f in fixtures),
        "gold_rows": sum(len(f.records) for f in fixtures),
        "results": results, "summary": summarize(results), "speed": speed(results),
        "tokens": tokens(fixtures), "cache_stub": cache_stub(fixtures),
    }
    for ex in OFFLINE_EXTRACTORS:
        print(f"{ex:12} F1 {data['summary'][ex]['f1']:.4f}  dev {data['summary'][ex + '@dev']['f1']:.4f}  "
              f"held-out {data['summary'][ex + '@holdout']['f1']:.4f}  {data['speed'][ex]['pages_per_s']} pages/s")
    print(f"offline benchmark took {time.perf_counter() - t0:.1f} s")

    out = Path(args.out)
    prev = out / "benchmark.json"
    if args.claude:
        if not os.environ.get("ANTHROPIC_API_KEY"):
            print("--claude needs ANTHROPIC_API_KEY", file=sys.stderr)
            return 2
        data["claude"] = claude_runs(fixtures, args.budget)
    elif args.rescore:
        old = json.loads(prev.read_text(encoding="utf-8"))["claude"]
        new = claude_runs(fixtures, 0.0, rescore=True)
        old.update(results=new["results"], summary=new["summary"], rescored=date.today().isoformat())
        data["claude"] = old
    elif prev.exists():
        old = json.loads(prev.read_text(encoding="utf-8")).get("claude")
        if old:
            data["claude"] = old
    if args.smoke:
        assert data["summary"]["induction-2@dev"]["f1"] > 0.9, "induction regressed"
        print("smoke ok")
        return 0

    out.mkdir(parents=True, exist_ok=True)
    md = markdown(data)
    (out / "benchmark.json").write_text(json.dumps(data, indent=1, ensure_ascii=False), encoding="utf-8")
    (out / "benchmark.md").write_text("# Benchmark\n\n" + md + "\n", encoding="utf-8")
    if README.exists():
        text = README.read_text(encoding="utf-8")
        if START in text and END in text:
            head, rest = text.split(START, 1)
            _, tail = rest.split(END, 1)
            README.write_text(head + START + "\n" + md + "\n" + END + tail, encoding="utf-8")
    print(f"wrote {out / 'benchmark.json'} and benchmark.md")
    return 0


if __name__ == "__main__":
    try:
        from dotenv import load_dotenv

        load_dotenv(ROOT / ".env")
    except ImportError:
        pass
    raise SystemExit(main())

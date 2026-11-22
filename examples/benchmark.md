# Benchmark

Measured 2026-11-20 on Windows 11 (AMD64), Python 3.12.3. 14 saved page sets (19 pages, 264 hand-checked rows); the 2 example rows of each set are excluded from scoring for every extractor.

**Field-level accuracy (micro-averaged over all cells)**

| Extractor | Needs | Dev F1 (10 sets, used for tuning) | Held-out F1 (4 sets) | All: P / R / F1 | Pages/s |
|---|---|---|---|---|---|
| heuristic | column names only | 0.962 | 0.887 | 0.941 / 0.955 / **0.948** | 54.8 |
| induction-1 | + 1 example row | 0.979 | 0.937 | 0.970 / 0.972 / **0.971** | 49.7 |
| induction-2 | + 2 example rows | 1.000 | 0.937 | 0.989 / 0.989 / **0.989** | 37.8 |
| claude (claude-opus-5-5) | column names + API | 0.994 | 1.000 | 0.991 / 1.000 / **0.995** | 19 Claude calls for 19 pages, $0.440 |
| hybrid (claude-opus-5-5) | column names + API | 0.997 | 1.000 | 0.994 / 1.000 / **0.997** | 15 Claude calls for 19 pages, $0.345 |

**Per page set (F1)**

| Set | Split | Pages | heuristic | induction-1 | induction-2 | claude | hybrid |
|---|---|---|---|---|---|---|---|
| books | dev | 3 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| countries | dev | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| events | dev | 1 | 1.000 | 1.000 | 1.000 | 0.961 | 0.961 |
| hn | dev | 2 | 0.772 | 1.000 | 1.000 | 1.000 | 1.000 |
| jobs | dev | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| products | dev | 2 | 1.000 | 1.000 | 1.000 | 0.955 | 0.980 |
| quotes | dev | 2 | 1.000 | 0.667 | 1.000 | 1.000 | 1.000 |
| real_estate | dev | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| repos | dev | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| restaurants | dev | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| holdout_blog | holdout | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |
| holdout_forum | holdout | 1 | 0.828 | 0.800 | 0.800 | 1.000 | 1.000 |
| holdout_specs | holdout | 1 | 0.700 | 1.000 | 1.000 | 1.000 | 1.000 |
| holdout_wiki | holdout | 1 | 1.000 | 1.000 | 1.000 | 1.000 | 1.000 |

**What Claude would be sent (estimated tokens, offline estimator)**

Raw HTML 40,012 -> cleaned page text 17,597 (-56.0%) -> blocks actually sent, boilerplate dropped 17,539 (-56.2%) over 19 pages.

**Response cache (stub client, no network):** first pass 19 API calls, second pass over the same pages 0.

**Real API run (2026-11-20, claude-opus-5-5):** Claude mode 19 calls, 30,363 input + 13,078 output tokens, $0.4396. Hybrid needed 15 Claude responses for the same 19 pages (one per set, plus one where the learned wrapper failed verification; the rest ran on the wrapper); in this run they were served from the Claude run's cache, so it cost $0.0000 instead of $0.3455. Re-running Claude mode: 0 calls, 19 cache hits, $0.0000 ($0.4396 not spent). Total spent: $0.4396. Scores re-computed from the cached responses on 2026-11-20.

Where Claude loses points it writes 0 for a value the page states as "Free" or "No reviews yet"; the hand-checked rows use null there. That is a labelling convention, and the gold rows were left as they are.


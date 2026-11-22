"""Response cache keyed by a content hash, plus token and cost accounting.

The key is SHA-256 over everything that can change the answer: model, prompt version, effort, the JSON schema and
the chunk text. Re-running a job on an unchanged page costs nothing; a page whose content changed (even by one
price) misses. Stored in SQLite so the cache survives restarts and is safe to share between threads.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

# USD per million tokens: input, output, cache read. Cache writes bill at 1.25x input.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00, 0.20),
    "claude-sonnet-5-5": (2.00, 10.00, 0.10),
    "claude-haiku-5-5": (0.10, 0.50, 0.01),
    "claude-opus-5": (5.00, 25.00, 0.50),
    "claude-sonnet-5": (2.00, 10.00, 0.20),
}
DEFAULT_PRICE = (4.00, 20.00, 0.20)


def cost_usd(model: str, input_tokens: int, output_tokens: int, cache_read: int = 0, cache_write: int = 0) -> float:
    p_in, p_out, p_read = PRICES.get(model, DEFAULT_PRICE)
    return (input_tokens * p_in + output_tokens * p_out + cache_read * p_read + cache_write * p_in * 1.25) / 1e6


@dataclass
class Usage:
    """Running totals for one job. `saved_*` counts what cache hits would have cost."""

    requests: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    cost: float = 0.0
    cache_hits: int = 0
    cache_misses: int = 0
    saved_tokens: int = 0
    saved_cost: float = 0.0
    repairs: int = 0
    by_model: dict = field(default_factory=dict)

    def add(self, model: str, u: dict) -> None:
        self.requests += 1
        i, o = u.get("input_tokens", 0), u.get("output_tokens", 0)
        r, w = u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0)
        c = cost_usd(model, i, o, r, w)
        self.input_tokens += i
        self.output_tokens += o
        self.cache_read_tokens += r
        self.cache_write_tokens += w
        self.cost += c
        self.by_model[model] = self.by_model.get(model, 0.0) + c

    def add_hit(self, model: str, u: dict) -> None:
        self.cache_hits += 1
        tok = sum(u.get(k, 0) for k in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                                       "cache_creation_input_tokens"))
        self.saved_tokens += tok
        self.saved_cost += cost_usd(model, u.get("input_tokens", 0), u.get("output_tokens", 0),
                                    u.get("cache_read_input_tokens", 0), u.get("cache_creation_input_tokens", 0))

    def to_dict(self) -> dict:
        d = {k: getattr(self, k) for k in ("requests", "input_tokens", "output_tokens", "cache_read_tokens",
                                           "cache_write_tokens", "cache_hits", "cache_misses", "saved_tokens",
                                           "repairs")}
        d["cost_usd"] = round(self.cost, 5)
        d["saved_usd"] = round(self.saved_cost, 5)
        return d


def cache_key(**parts) -> str:
    blob = json.dumps(parts, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


class ResponseCache:
    def __init__(self, path: str | Path = ":memory:"):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = threading.Lock()
        with self._lock:
            self._db.execute("CREATE TABLE IF NOT EXISTS responses (key TEXT PRIMARY KEY, model TEXT, "
                             "payload TEXT, usage TEXT, created REAL)")
            self._db.commit()
        self.hits = 0
        self.misses = 0

    def get(self, key: str) -> tuple[dict, dict] | None:
        with self._lock:
            row = self._db.execute("SELECT payload, usage FROM responses WHERE key = ?", (key,)).fetchone()
        if row is None:
            self.misses += 1
            return None
        self.hits += 1
        return json.loads(row[0]), json.loads(row[1])

    def put(self, key: str, model: str, payload: dict, usage: dict) -> None:
        with self._lock:
            self._db.execute("INSERT OR REPLACE INTO responses VALUES (?, ?, ?, ?, ?)",
                             (key, model, json.dumps(payload), json.dumps(usage), time.time()))
            self._db.commit()

    def __len__(self) -> int:
        with self._lock:
            return self._db.execute("SELECT COUNT(*) FROM responses").fetchone()[0]

    def stats(self) -> dict:
        total = self.hits + self.misses
        return {"entries": len(self), "hits": self.hits, "misses": self.misses,
                "hit_rate": round(self.hits / total, 4) if total else 0.0}

    def clear(self) -> None:
        with self._lock:
            self._db.execute("DELETE FROM responses")
            self._db.commit()

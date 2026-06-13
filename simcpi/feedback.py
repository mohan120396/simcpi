"""
feedback.py — MCPark feedback memory
====================================
Rating-weighted retrieval memory for tool selection (MCPark only, v1).

Every MCPark run is logged as:  prompt → tool(s)+params → rating (0/1)
On a new prompt we embed it, cosine-match the top-k *rated* past prompts above
a threshold, and inject those as few-shot hints so the model leans on what
worked before. After the run the user rates 0/1 via the MCPark UI.

This is NOT fine-tuning — it's in-context retrieval (kNN demonstration
selection). The rating column could feed a fine-tune later; the live loop is
retrieval only.

Storage: one SQLite file, embedding stored as a BLOB column (single source of
truth — no separate .npy). Brute-force cosine in pure Python is plenty at
simcpi's scale; no numpy / FAISS / sqlite-vec required.
"""

from __future__ import annotations

import json
import math
import sqlite3
import threading
import time
import uuid
from array import array
from typing import Optional

# ── embedding (de)serialisation ───────────────────────────────────────────────


def _pack(vec) -> bytes:
    """float list → compact little-endian float32 blob."""
    return array("f", vec).tobytes()


def _unpack(blob: bytes) -> list[float]:
    a = array("f")
    a.frombytes(blob)
    return a.tolist()


def _cosine(a, b) -> float:
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / math.sqrt(na * nb)


# ── store ─────────────────────────────────────────────────────────────────────


class FeedbackMemory:
    """
    SQLite-backed feedback store for MCPark.

    Schema:
        calls(
            id          TEXT PRIMARY KEY,   -- uuid hex, returned to the UI
            ts          REAL,               -- unix time
            session_id  TEXT,               -- browser session (nullable)
            prompt      TEXT,               -- the user's natural-language prompt
            embedding   BLOB,               -- float32 blob, nullable
            tool_calls  TEXT,               -- JSON: [{"name","arguments"}]
            rating      INTEGER             -- 0 / 1, nullable until rated
        )
    """

    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        self._lock = threading.Lock()
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        with self._lock, self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS calls(
                    id          TEXT PRIMARY KEY,
                    ts          REAL,
                    session_id  TEXT,
                    prompt      TEXT,
                    embedding   BLOB,
                    tool_calls  TEXT,
                    rating      INTEGER,
                    note        TEXT
                )
                """
            )
            # Migrate older DBs that predate the `note` column.
            cols = {r[1] for r in c.execute("PRAGMA table_info(calls)")}
            if "note" not in cols:
                c.execute("ALTER TABLE calls ADD COLUMN note TEXT")

    # ── writes ────────────────────────────────────────────────────────────────

    def log_call(
        self,
        prompt: str,
        tool_calls: list,
        session_id: Optional[str] = None,
        embedding: Optional[list] = None,
    ) -> str:
        """Insert a call, return its generated id (the rating handle)."""
        call_id = uuid.uuid4().hex
        blob = _pack(embedding) if embedding else None
        with self._lock, self._conn() as c:
            c.execute(
                "INSERT INTO calls(id, ts, session_id, prompt, embedding, tool_calls, rating) "
                "VALUES (?,?,?,?,?,?,NULL)",
                (call_id, time.time(), session_id, prompt, blob, json.dumps(tool_calls)),
            )
        return call_id

    def set_rating(self, call_id: str, rating: int, note: Optional[str] = None) -> bool:
        """
        Attach a 0/1 rating (and optional free-text reason) to a logged call.
        The note becomes evidence for the docstring optimizer. Returns False if
        the id is unknown.
        """
        rating = 1 if int(rating) else 0
        note = (note or "").strip() or None
        with self._lock, self._conn() as c:
            cur = c.execute(
                "UPDATE calls SET rating=?, note=? WHERE id=?",
                (rating, note, call_id),
            )
            return cur.rowcount > 0

    def backfill_embeddings(self, embed_fn) -> int:
        """
        Embed every rated/loggable row that has a prompt but no embedding.
        embed_fn: Callable[[str], list[float]]. Returns count embedded.
        """
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, prompt FROM calls WHERE embedding IS NULL AND prompt IS NOT NULL"
            ).fetchall()
        count = 0
        for cid, prompt in rows:
            vec = embed_fn(prompt)
            if not vec:
                continue
            with self._lock, self._conn() as c:
                c.execute("UPDATE calls SET embedding=? WHERE id=?", (_pack(vec), cid))
            count += 1
        return count

    # ── reads ───────────────────────────────────────────────────────────────────

    def clear(self) -> int:
        """Delete every logged call. Returns how many rows were removed."""
        with self._lock, self._conn() as c:
            n = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            c.execute("DELETE FROM calls")
        return n

    def retrieve(
        self,
        embedding,
        k: int = 5,
        threshold: float = 0.6,
        only_positive: bool = False,
    ) -> list[dict]:
        """
        Top-k rated past calls most similar to `embedding`, above `threshold`.
        Cold start / no embedding / no matches → []. Each item:
            {"similarity", "prompt", "tool_calls", "rating"}
        """
        if not embedding:
            return []
        q = (
            "SELECT prompt, tool_calls, rating, embedding FROM calls "
            "WHERE rating IS NOT NULL AND embedding IS NOT NULL"
        )
        if only_positive:
            q += " AND rating = 1"

        dim = len(embedding)
        scored: list[tuple] = []
        with self._conn() as c:
            for prompt, tc, rating, blob in c.execute(q):
                vec = _unpack(blob)
                if len(vec) != dim:
                    continue  # different embedding model / space — not comparable
                sim = _cosine(embedding, vec)
                if sim >= threshold:
                    scored.append((sim, prompt, tc, rating))

        scored.sort(key=lambda r: r[0], reverse=True)
        return [
            {
                "similarity": round(sim, 4),
                "prompt": prompt,
                "tool_calls": json.loads(tc) if tc else [],
                "rating": rating,
            }
            for sim, prompt, tc, rating in scored[:k]
        ]

    def rated_calls(self, limit: int = 200) -> list[dict]:
        """All rated calls, newest first — ground truth for the docstring optimizer."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT prompt, tool_calls, rating, note FROM calls "
                "WHERE rating IS NOT NULL AND prompt IS NOT NULL "
                "ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"prompt": p, "tool_calls": json.loads(tc) if tc else [],
             "rating": r, "note": note}
            for p, tc, r, note in rows
        ]

    def recent_calls(self, limit: int = 100) -> list[dict]:
        """Newest rows first — backs the MCPark 'View Feedback DB' table."""
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, ts, session_id, prompt, tool_calls, rating, note, "
                "embedding IS NOT NULL FROM calls ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {"id": i, "ts": ts, "session_id": s, "prompt": p,
             "tool_calls": json.loads(tc) if tc else [],
             "rating": r, "note": note, "embedded": bool(e)}
            for i, ts, s, p, tc, r, note, e in rows
        ]

    def stats(self) -> dict:
        with self._conn() as c:
            total = c.execute("SELECT COUNT(*) FROM calls").fetchone()[0]
            rated = c.execute("SELECT COUNT(*) FROM calls WHERE rating IS NOT NULL").fetchone()[0]
            pos = c.execute("SELECT COUNT(*) FROM calls WHERE rating = 1").fetchone()[0]
            embedded = c.execute("SELECT COUNT(*) FROM calls WHERE embedding IS NOT NULL").fetchone()[0]
        return {"total": total, "rated": rated, "positive": pos, "embedded": embedded}


# ── few-shot formatting ────────────────────────────────────────────────────────


def format_memory(examples: list[dict]) -> str:
    """
    Render retrieved examples as a system-message hint. Positive examples are
    shown as reinforcement; negative (0) ones are loudly labelled as outcomes
    to avoid, so the model doesn't imitate a failure.
    """
    if not examples:
        return ""
    lines = [
        "You have feedback from past similar requests on this server. "
        "Use it only as a hint for which tool(s) to call. Do NOT mention it to the user.",
        "",
        "Past similar requests:",
    ]
    for ex in examples:
        calls = ", ".join(
            f"{t.get('name')}({json.dumps(t.get('arguments') or {}, ensure_ascii=False)})"
            for t in ex["tool_calls"]
        ) or "(no tool called)"
        if ex["rating"] == 1:
            verdict = "user was SATISFIED — this choice worked"
        else:
            verdict = "user was NOT satisfied — AVOID repeating this choice"
        lines.append(f'- "{ex["prompt"]}" → {calls} → {verdict}')
    return "\n".join(lines)

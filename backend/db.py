"""Postgres layer: connection pool + schema.

Best-effort by design: init() never raises, so the app always comes up and can
serve the frontend even when the database is down; data endpoints answer 503
until it returns. Tables are created on startup; the database itself must
already exist.
"""

from __future__ import annotations

import json
import logging
import os
from contextlib import contextmanager

log = logging.getLogger("elocute.db")

_pool = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id UUID PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    password_hash TEXT NOT NULL,
    created_at TIMESTAMPTZ DEFAULT now()
);

CREATE TABLE IF NOT EXISTS books (
    id UUID PRIMARY KEY,
    user_id UUID REFERENCES users(id) ON DELETE CASCADE,
    anon_id TEXT,
    title TEXT,
    author TEXT,
    source_name TEXT,
    total_words INT DEFAULT 0,
    chapters JSONB DEFAULT '[]'::jsonb,
    text_content TEXT,
    cover BYTEA,
    cover_mime TEXT,
    cur_word INT DEFAULT 0,
    finished BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_read_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_books_user ON books (user_id);
CREATE INDEX IF NOT EXISTS idx_books_anon ON books (anon_id);
"""


def _migrate(conn) -> None:
    """One-shot fold of the legacy chunked layout into books.text_content.

    Positions and chapter pointers move from (chunk idx, word-in-chunk) to a
    single global word offset. No-op once the chunks table is gone.
    """
    if conn.execute("SELECT to_regclass('public.chunks')").fetchone()[0] is None:
        return
    conn.execute("ALTER TABLE books ADD COLUMN IF NOT EXISTS text_content TEXT")
    books = conn.execute(
        "SELECT id, COALESCE(cur_chunk, 0), COALESCE(cur_word, 0), chapters FROM books"
    ).fetchall()
    for book_id, cur_chunk, cur_word, chapters in books:
        rows = conn.execute(
            "SELECT idx, words_start, text_content FROM chunks WHERE book_id = %s ORDER BY idx",
            (book_id,),
        ).fetchall()
        if not rows:
            continue
        starts = {idx: ws or 0 for idx, ws, _ in rows}
        text = "\n\n".join(t or "" for _, _, t in rows)
        word = starts.get(cur_chunk, 0) + cur_word
        chs = chapters if isinstance(chapters, list) else json.loads(chapters or "[]")
        chs = [{"title": c.get("title"), "word": starts.get(c.get("chunk"), 0)} for c in chs]
        conn.execute(
            "UPDATE books SET text_content = %s, cur_word = %s, chapters = %s WHERE id = %s",
            (text, word, json.dumps(chs), book_id),
        )
    conn.execute("DROP TABLE chunks")
    conn.execute("ALTER TABLE books DROP COLUMN IF EXISTS total_chunks")
    conn.execute("ALTER TABLE books DROP COLUMN IF EXISTS cur_chunk")
    conn.execute("ALTER TABLE books DROP COLUMN IF EXISTS cur_seconds")
    log.info("migrated legacy chunked books to continuous text")


def init() -> None:
    """Open the pool and ensure the schema. Never raises."""
    global _pool
    url = os.environ.get("DATABASE_URL", "")
    if not url:
        log.warning("DATABASE_URL not set; running without persistence")
        return
    try:
        from psycopg_pool import ConnectionPool

        _pool = ConnectionPool(url, min_size=1, max_size=8, open=True, timeout=10)
        with _pool.connection() as conn:
            conn.execute(SCHEMA)
            _migrate(conn)
        log.info("database ready")
    except Exception as exc:
        log.error("database unavailable at startup: %s", exc)
        # Keep the pool if it opened; a later checkout may still succeed.


def ok() -> bool:
    if _pool is None:
        return False
    try:
        with _pool.connection() as conn:
            conn.execute("SELECT 1")
        return True
    except Exception:
        return False


@contextmanager
def conn():
    """Checkout a connection; raises RuntimeError('db down') when unavailable
    so endpoints can map it to a 503.

    Only database errors are translated; anything else (e.g. an HTTPException
    raised by endpoint code inside the block) propagates untouched, otherwise
    a 404/409 would surface as a bogus 503."""
    if _pool is None:
        raise RuntimeError("db down")
    import psycopg

    try:
        with _pool.connection() as c:
            yield c
    except (psycopg.Error, OSError) as exc:
        raise RuntimeError("db down") from exc

"""Postgres layer: connection pool + schema.

Best-effort by design: init() never raises, so the app always comes up and can
serve the frontend even when the database is down; data endpoints answer 503
until it returns. Tables are created on startup; the database itself must
already exist.
"""

from __future__ import annotations

import logging
import os
from contextlib import contextmanager

log = logging.getLogger("loudreader.db")

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
    total_chunks INT DEFAULT 0,
    chapters JSONB DEFAULT '[]'::jsonb,
    cover BYTEA,
    cover_mime TEXT,
    cur_chunk INT DEFAULT 0,
    cur_seconds REAL DEFAULT 0,
    cur_word INT DEFAULT 0,
    finished BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMPTZ DEFAULT now(),
    last_read_at TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_books_user ON books (user_id);
CREATE INDEX IF NOT EXISTS idx_books_anon ON books (anon_id);

CREATE TABLE IF NOT EXISTS chunks (
    book_id UUID REFERENCES books(id) ON DELETE CASCADE,
    idx INT NOT NULL,
    word_count INT DEFAULT 0,
    words_start INT DEFAULT 0,
    chapter TEXT,
    text_content TEXT,
    PRIMARY KEY (book_id, idx)
);
"""


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
    so endpoints can map it to a 503."""
    if _pool is None:
        raise RuntimeError("db down")
    try:
        with _pool.connection() as c:
            yield c
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError("db down") from exc

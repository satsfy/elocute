"""Loudreader: Listen to audiobooks as your browser read them.

Upload a PDF, EPUB, Markdown or TXT. Loudreader extracts and
cleans the text, splits it into parts, and then **your browser's TTS reads it** with live word highlighting, audiobook-style player. You can optionally signup to save the books you uploded across sessions and device, with automatic progress saving.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from backend import auth, bookprep, db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("loudreader")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD = 80 * 1024 * 1024
_ID_RE = re.compile(r"^[0-9a-f-]{32,36}$")

app = FastAPI(title="Loudreader", docs_url=None, redoc_url=None)
app.add_middleware(
    SessionMiddleware,
    secret_key=os.environ.get("SECRET_KEY", "dev-insecure"),
    max_age=60 * 60 * 24 * 365,
    same_site="lax",
)


@app.on_event("startup")
def _startup() -> None:
    db.init()


def _dbfail() -> JSONResponse:
    return JSONResponse({"detail": "Database unavailable, try again shortly."}, status_code=503)


def _check_id(book_id: str) -> None:
    if not _ID_RE.match(book_id):
        raise HTTPException(400, "Bad id")


# ---------------------------------------------------------------------------
# Pages + health
# ---------------------------------------------------------------------------

app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


@app.get("/healthz")
def healthz():
    return {"ok": True, "db": db.ok()}


@app.get("/")
@app.head("/")  # fleet monitoring probes with HEAD; a 405 there reads as down
def index_page():
    return FileResponse(str(FRONTEND / "index.html"))


@app.get("/b/{book_id}")
def reader_page(book_id: str):
    _check_id(book_id)
    return FileResponse(str(FRONTEND / "reader.html"))


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------

@app.get("/api/me")
def me(request: Request):
    user_id, _ = auth.identity(request)
    email = None
    if user_id:
        try:
            with db.conn() as c:
                row = c.execute("SELECT email FROM users WHERE id = %s", (user_id,)).fetchone()
            email = row[0] if row else None
        except RuntimeError:
            pass
        if email is None:
            request.session.pop("user", None)
            user_id = None
    return {"user": email, "db": db.ok()}


def _claim_anon_books(c, user_id: str, anon_id: str) -> None:
    c.execute(
        "UPDATE books SET user_id = %s, anon_id = NULL WHERE anon_id = %s AND user_id IS NULL",
        (user_id, anon_id),
    )


@app.post("/api/register")
async def register(request: Request):
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not auth.EMAIL_RE.match(email):
        raise HTTPException(400, "That does not look like an email address.")
    if len(password) < 8:
        raise HTTPException(400, "Password needs at least 8 characters.")
    _, anon_id = auth.identity(request)
    user_id = str(uuid.uuid4())
    try:
        with db.conn() as c:
            exists = c.execute("SELECT 1 FROM users WHERE email = %s", (email,)).fetchone()
            if exists:
                raise HTTPException(409, "An account with this email already exists. Sign in instead.")
            c.execute(
                "INSERT INTO users (id, email, password_hash) VALUES (%s, %s, %s)",
                (user_id, email, auth.hash_password(password)),
            )
            _claim_anon_books(c, user_id, anon_id)
    except RuntimeError:
        return _dbfail()
    request.session["user"] = user_id
    return {"ok": True, "user": email}


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    _, anon_id = auth.identity(request)
    try:
        with db.conn() as c:
            row = c.execute(
                "SELECT id, password_hash FROM users WHERE email = %s", (email,)
            ).fetchone()
            if not row or not auth.verify_password(password, row[1]):
                raise HTTPException(401, "Wrong email or password.")
            _claim_anon_books(c, row[0], anon_id)
    except RuntimeError:
        return _dbfail()
    request.session["user"] = str(row[0])
    return {"ok": True, "user": email}


@app.post("/api/logout")
def logout(request: Request):
    request.session.pop("user", None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Import
# ---------------------------------------------------------------------------

@app.post("/api/import")
async def import_book(request: Request):
    form = await request.form()
    title_hint = str(form.get("title") or "").strip() or None
    upload = form.get("file")
    text = str(form.get("text") or "")

    if upload is not None and getattr(upload, "filename", ""):
        ext = Path(upload.filename).suffix.lower()
        if ext not in (".pdf", ".epub", ".md", ".markdown", ".mdown", ".txt"):
            raise HTTPException(400, "Supported: PDF, EPUB, Markdown, TXT")
        data = await upload.read()
        if len(data) > MAX_UPLOAD:
            raise HTTPException(413, "File too large (80 MB max)")
        try:
            doc = bookprep.prepare_book(upload.filename, data, title_hint=title_hint)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except Exception as exc:
            raise HTTPException(400, f"Could not read {upload.filename}: {exc}")
        source_name = upload.filename
    else:
        if len(text.strip()) < 4:
            raise HTTPException(400, "Choose a file or paste some text first.")
        doc = bookprep.prepare_book(None, None, text=text, title_hint=title_hint)
        source_name = None

    user_id, anon_id = auth.identity(request)
    book_id = str(uuid.uuid4())
    import base64

    cover = None
    if doc.get("cover_b64"):
        try:
            cover = base64.b64decode(doc["cover_b64"])
        except Exception:
            cover = None
    try:
        with db.conn() as c:
            c.execute(
                """
                INSERT INTO books (id, user_id, anon_id, title, author, source_name,
                                   total_words, total_chunks, chapters, cover, cover_mime)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    book_id,
                    user_id,
                    None if user_id else anon_id,
                    doc.get("title"),
                    doc.get("author"),
                    source_name,
                    doc.get("total_words") or 0,
                    doc.get("total_chunks") or 0,
                    json.dumps(doc.get("chapters") or []),
                    cover,
                    doc.get("cover_mime"),
                ),
            )
            with c.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO chunks (book_id, idx, word_count, words_start, chapter, text_content)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (
                            book_id,
                            ch["idx"],
                            ch.get("words") or 0,
                            ch.get("words_start") or 0,
                            (ch.get("chapter") or None),
                            ch.get("text") or "",
                        )
                        for ch in doc.get("chunks", [])
                    ],
                )
    except RuntimeError:
        return _dbfail()
    return {"id": book_id, "chunks": doc.get("total_chunks"), "words": doc.get("total_words")}


@app.post("/api/demo")
def import_demo(request: Request):
    """Seed the shelf with the bundled public-domain sample."""
    sample = (FRONTEND / "sample.txt").read_text(encoding="utf-8")
    doc = bookprep.prepare_book(
        "The Lighthouse Keeper.txt", sample.encode(), title_hint="The Lighthouse Keeper (sample)"
    )
    user_id, anon_id = auth.identity(request)
    book_id = str(uuid.uuid4())
    try:
        with db.conn() as c:
            c.execute(
                """
                INSERT INTO books (id, user_id, anon_id, title, author, source_name,
                                   total_words, total_chunks, chapters)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    book_id,
                    user_id,
                    None if user_id else anon_id,
                    doc.get("title"),
                    "Loudreader",
                    "sample",
                    doc.get("total_words") or 0,
                    doc.get("total_chunks") or 0,
                    json.dumps(doc.get("chapters") or []),
                ),
            )
            with c.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO chunks (book_id, idx, word_count, words_start, chapter, text_content)
                    VALUES (%s, %s, %s, %s, %s, %s)
                    """,
                    [
                        (book_id, ch["idx"], ch.get("words") or 0, ch.get("words_start") or 0,
                         ch.get("chapter") or None, ch.get("text") or "")
                        for ch in doc.get("chunks", [])
                    ],
                )
    except RuntimeError:
        return _dbfail()
    return {"id": book_id}


# ---------------------------------------------------------------------------
# Books
# ---------------------------------------------------------------------------

@app.get("/api/books")
def list_books(request: Request):
    where, params = auth.owner_where(request)
    try:
        with db.conn() as c:
            rows = c.execute(
                f"""
                SELECT b.id, b.title, b.author, b.total_words, b.total_chunks,
                       b.cur_chunk, b.cur_seconds, b.cur_word, b.finished,
                       (b.cover IS NOT NULL) AS has_cover,
                       EXTRACT(EPOCH FROM b.created_at) AS created_ts,
                       EXTRACT(EPOCH FROM b.last_read_at) AS last_read_ts,
                       c2.words_start AS cur_words_start, c2.word_count AS cur_word_count
                FROM books b
                LEFT JOIN chunks c2 ON c2.book_id = b.id AND c2.idx = b.cur_chunk
                WHERE {where}
                ORDER BY COALESCE(b.last_read_at, b.created_at) DESC
                """,
                params,
            ).fetchall()
    except RuntimeError:
        return _dbfail()
    items = []
    for r in rows:
        bid = str(r[0])
        items.append(
            {
                "id": bid,
                "title": r[1],
                "author": r[2],
                "total_words": r[3] or 0,
                "total_chunks": r[4] or 0,
                "cur_chunk": r[5] or 0,
                "cur_seconds": r[6] or 0,
                "cur_word": r[7] or 0,
                "finished": bool(r[8]),
                "has_cover": bool(r[9]),
                "created_at": float(r[10] or 0),
                "last_read_at": float(r[11]) if r[11] else None,
                "cur_words_start": r[12] or 0,
                "cur_word_count": r[13] or 0,
                "url": f"/b/{bid}",
                "cover_url": f"/api/books/{bid}/cover" if r[9] else None,
            }
        )
    return {"items": items}


def _own_book(c, request: Request, book_id: str):
    where, params = auth.owner_where(request)
    row = c.execute(
        f"SELECT id FROM books WHERE id = %s AND {where}", [book_id, *params]
    ).fetchone()
    if row is None:
        raise HTTPException(404, "Book not found")


@app.get("/api/books/{book_id}")
def book_detail(book_id: str, request: Request):
    _check_id(book_id)
    where, params = auth.owner_where(request)
    try:
        with db.conn() as c:
            r = c.execute(
                f"""
                SELECT id, title, author, source_name, total_words, total_chunks,
                       chapters, (cover IS NOT NULL), cur_chunk, cur_seconds,
                       cur_word, finished
                FROM books WHERE id = %s AND {where}
                """,
                [book_id, *params],
            ).fetchone()
            if r is None:
                raise HTTPException(404, "Book not found")
            crows = c.execute(
                """
                SELECT idx, word_count, words_start, chapter
                FROM chunks WHERE book_id = %s ORDER BY idx
                """,
                (book_id,),
            ).fetchall()
    except RuntimeError:
        return _dbfail()
    chapters = r[6] if isinstance(r[6], list) else json.loads(r[6] or "[]")
    return {
        "id": str(r[0]),
        "title": r[1],
        "author": r[2],
        "source_name": r[3],
        "total_words": r[4] or 0,
        "total_chunks": r[5] or 0,
        "chapters": chapters,
        "has_cover": bool(r[7]),
        "cover_url": f"/api/books/{book_id}/cover" if r[7] else None,
        "cur_chunk": r[8] or 0,
        "cur_seconds": r[9] or 0,
        "cur_word": r[10] or 0,
        "finished": bool(r[11]),
        "chunks": [
            {"idx": cr[0], "words": cr[1], "words_start": cr[2], "chapter": cr[3]}
            for cr in crows
        ],
    }


@app.get("/api/books/{book_id}/cover")
def book_cover(book_id: str, request: Request):
    _check_id(book_id)
    where, params = auth.owner_where(request)
    try:
        with db.conn() as c:
            r = c.execute(
                f"SELECT cover, cover_mime FROM books WHERE id = %s AND {where}",
                [book_id, *params],
            ).fetchone()
    except RuntimeError:
        return _dbfail()
    if r is None or r[0] is None:
        raise HTTPException(404, "No cover")
    return Response(
        content=bytes(r[0]),
        media_type=r[1] or "image/jpeg",
        headers={"Cache-Control": "private, max-age=86400"},
    )


@app.get("/api/books/{book_id}/chunks/{idx}")
def chunk_text(book_id: str, idx: int, request: Request):
    _check_id(book_id)
    try:
        with db.conn() as c:
            _own_book(c, request, book_id)
            r = c.execute(
                """
                SELECT idx, word_count, words_start, chapter, text_content
                FROM chunks WHERE book_id = %s AND idx = %s
                """,
                (book_id, idx),
            ).fetchone()
    except RuntimeError:
        return _dbfail()
    if r is None:
        raise HTTPException(404, "Part not found")
    return {"idx": r[0], "words": r[1], "words_start": r[2], "chapter": r[3], "text": r[4]}


@app.patch("/api/books/{book_id}/position")
async def save_position(book_id: str, request: Request):
    _check_id(book_id)
    body = await request.json()
    try:
        chunk = max(0, int(body.get("chunk", 0)))
        seconds = max(0.0, float(body.get("seconds", 0)))
        word = max(0, int(body.get("word", 0)))
    except (TypeError, ValueError):
        raise HTTPException(400, "Bad position")
    finished = bool(body.get("finished", False))
    where, params = auth.owner_where(request)
    try:
        with db.conn() as c:
            c.execute(
                f"""
                UPDATE books
                SET cur_chunk = %s, cur_seconds = %s, cur_word = %s, finished = %s,
                    last_read_at = now()
                WHERE id = %s AND {where}
                """,
                [chunk, seconds, word, finished, book_id, *params],
            )
    except RuntimeError:
        return _dbfail()
    return {"ok": True}


@app.patch("/api/books/{book_id}")
async def rename_book(book_id: str, request: Request):
    _check_id(book_id)
    body = await request.json()
    sets, vals = [], []
    if isinstance(body.get("title"), str) and body["title"].strip():
        sets.append("title = %s")
        vals.append(body["title"].strip()[:500])
    if isinstance(body.get("author"), str):
        sets.append("author = %s")
        vals.append(body["author"].strip()[:500] or None)
    if not sets:
        raise HTTPException(400, "Nothing to update")
    where, params = auth.owner_where(request)
    try:
        with db.conn() as c:
            c.execute(
                f"UPDATE books SET {', '.join(sets)} WHERE id = %s AND {where}",
                [*vals, book_id, *params],
            )
    except RuntimeError:
        return _dbfail()
    return {"ok": True}


@app.delete("/api/books/{book_id}")
def delete_book(book_id: str, request: Request):
    _check_id(book_id)
    where, params = auth.owner_where(request)
    try:
        with db.conn() as c:
            c.execute(f"DELETE FROM books WHERE id = %s AND {where}", [book_id, *params])
    except RuntimeError:
        return _dbfail()
    return {"ok": True}

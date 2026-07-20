"""Loudreader: Listen to audiobooks as your browser read them.

Upload a PDF, EPUB, Markdown or TXT. Loudreader extracts and cleans the text,
and then **your browser's TTS reads it** with live word highlighting,
audiobook-style player. Each book is stored as one continuous text; positions
and chapters are global word offsets. You can optionally signup to save the
books you uploaded across sessions and devices, with automatic progress saving.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.middleware.gzip import GZipMiddleware
from starlette.middleware.sessions import SessionMiddleware

from backend import auth, bookprep, db

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("loudreader")

FRONTEND = Path(__file__).resolve().parent.parent / "frontend"
MAX_UPLOAD = 80 * 1024 * 1024
STORAGE_CAP = 5 * 1024**3  # total text+cover bytes across all users except the owner
OWNER_EMAIL = os.environ.get("OWNER_EMAIL", "renatobritto@protonmail.com")
BASE_URL = os.environ.get("BASE_URL", "https://loudreader.satsfy.xyz")
_ID_RE = re.compile(r"^[0-9a-f-]{32,36}$")

app = FastAPI(title="Loudreader", docs_url=None, redoc_url=None)
app.add_middleware(GZipMiddleware, minimum_size=2048)  # book text is MBs of very compressible prose
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


@app.get("/robots.txt")
def robots_txt():
    return PlainTextResponse(
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /api/\n"
        "Disallow: /b/\n"
        "\n"
        f"Sitemap: {BASE_URL}/sitemap.xml\n"
    )


@app.get("/sitemap.xml")
def sitemap_xml():
    return Response(
        '<?xml version="1.0" encoding="UTF-8"?>'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"<url><loc>{BASE_URL}/</loc><changefreq>weekly</changefreq></url>"
        "</urlset>",
        media_type="application/xml",
    )


@app.get("/llms.txt")
def llms_txt():
    """Plain-language summary for AI crawlers/assistants (llms.txt convention)."""
    return PlainTextResponse(
        "# Loudreader\n"
        "\n"
        "> Free browser text-to-speech audiobook reader. Upload a PDF, EPUB,\n"
        "> Markdown or TXT (or paste text) and your browser reads it aloud with\n"
        "> live word highlighting, chapters, speed control up to 4x, a sleep\n"
        "> timer and automatic progress saving.\n"
        "\n"
        f"- Website: {BASE_URL}/\n"
        "- Price: free, no ads, no account required\n"
        "- An optional email account syncs books and reading position across devices\n"
        "- Formats: PDF, EPUB, Markdown, TXT, pasted text (max 80 MB)\n"
        "- Speech is generated on-device by the browser's Web Speech API; the\n"
        "  server stores only extracted text, never audio\n"
        "- Voice quality depends on the browser: Edge and Android ship the best\n"
        "  free voices, Chrome desktop is decent, Linux browsers use espeak\n"
        "- Source code: https://github.com/satsfy/loudreader\n"
    )


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

def _enforce_storage_cap(c) -> None:
    """Keep everyone-but-the-owner's books under STORAGE_CAP.

    Walks books newest-first and deletes the tail once the running byte total
    passes the cap, so a flood of junk uploads evicts the oldest entries
    instead of filling the database. The owner's books are never counted or
    evicted."""
    c.execute(
        """
        DELETE FROM books WHERE id IN (
            SELECT id FROM (
                SELECT b.id,
                       SUM(octet_length(COALESCE(b.text_content, ''))
                           + COALESCE(octet_length(b.cover), 0))
                           OVER (ORDER BY b.created_at DESC, b.id) AS newest_first_bytes
                FROM books b
                LEFT JOIN users u ON u.id = b.user_id
                WHERE u.email IS DISTINCT FROM %s
            ) t
            WHERE newest_first_bytes > %s
        )
        """,
        (OWNER_EMAIL, STORAGE_CAP),
    )


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
                                   total_words, chapters, text_content, cover, cover_mime)
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
                    json.dumps(doc.get("chapters") or []),
                    doc.get("text") or "",
                    cover,
                    doc.get("cover_mime"),
                ),
            )
            _enforce_storage_cap(c)
    except RuntimeError:
        return _dbfail()
    return {"id": book_id, "words": doc.get("total_words")}


@app.post("/api/demo")
def import_demo(request: Request):
    """Seed the shelf with the bundled public-domain sample."""
    sample = (FRONTEND / "sample.txt").read_text(encoding="utf-8")
    # Title follows whatever sample ships: its first non-empty line.
    first_line = next((ln.strip() for ln in sample.splitlines() if ln.strip()), "Sample book")
    doc = bookprep.prepare_book(
        "sample.txt", sample.encode(), title_hint=f"{first_line[:80]} (sample)"
    )
    user_id, anon_id = auth.identity(request)
    book_id = str(uuid.uuid4())
    try:
        with db.conn() as c:
            c.execute(
                """
                INSERT INTO books (id, user_id, anon_id, title, author, source_name,
                                   total_words, chapters, text_content)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    book_id,
                    user_id,
                    None if user_id else anon_id,
                    doc.get("title"),
                    None,
                    "sample",
                    doc.get("total_words") or 0,
                    json.dumps(doc.get("chapters") or []),
                    doc.get("text") or "",
                ),
            )
            _enforce_storage_cap(c)
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
                SELECT id, title, author, total_words, cur_word, finished,
                       (cover IS NOT NULL) AS has_cover,
                       EXTRACT(EPOCH FROM created_at) AS created_ts,
                       EXTRACT(EPOCH FROM last_read_at) AS last_read_ts
                FROM books
                WHERE {where}
                ORDER BY COALESCE(last_read_at, created_at) DESC
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
                "cur_word": r[4] or 0,
                "finished": bool(r[5]),
                "has_cover": bool(r[6]),
                "created_at": float(r[7] or 0),
                "last_read_at": float(r[8]) if r[8] else None,
                "url": f"/b/{bid}",
                "cover_url": f"/api/books/{bid}/cover" if r[6] else None,
            }
        )
    return {"items": items}


@app.get("/api/books/{book_id}")
def book_detail(book_id: str, request: Request):
    _check_id(book_id)
    where, params = auth.owner_where(request)
    try:
        with db.conn() as c:
            r = c.execute(
                f"""
                SELECT id, title, author, source_name, total_words, chapters,
                       (cover IS NOT NULL), cur_word, finished
                FROM books WHERE id = %s AND {where}
                """,
                [book_id, *params],
            ).fetchone()
            if r is None:
                raise HTTPException(404, "Book not found")
    except RuntimeError:
        return _dbfail()
    chapters = r[5] if isinstance(r[5], list) else json.loads(r[5] or "[]")
    return {
        "id": str(r[0]),
        "title": r[1],
        "author": r[2],
        "source_name": r[3],
        "total_words": r[4] or 0,
        "chapters": chapters,
        "has_cover": bool(r[6]),
        "cover_url": f"/api/books/{book_id}/cover" if r[6] else None,
        "cur_word": r[7] or 0,
        "finished": bool(r[8]),
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


@app.get("/api/books/{book_id}/text")
def book_text(book_id: str, request: Request):
    """The whole book as one plain-text response (gzipped by middleware)."""
    _check_id(book_id)
    where, params = auth.owner_where(request)
    try:
        with db.conn() as c:
            r = c.execute(
                f"SELECT text_content FROM books WHERE id = %s AND {where}",
                [book_id, *params],
            ).fetchone()
    except RuntimeError:
        return _dbfail()
    if r is None:
        raise HTTPException(404, "Book not found")
    return PlainTextResponse(r[0] or "", headers={"Cache-Control": "private, max-age=86400"})


@app.patch("/api/books/{book_id}/position")
async def save_position(book_id: str, request: Request):
    _check_id(book_id)
    body = await request.json()
    try:
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
                SET cur_word = %s, finished = %s, last_read_at = now()
                WHERE id = %s AND {where}
                """,
                [word, finished, book_id, *params],
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

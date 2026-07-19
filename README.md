# Loudreader

Every book, read aloud by your browser. Free, forever.

Upload a PDF, EPUB, Markdown or TXT (or paste any text). Loudreader extracts and
cleans the text server-side, splits it into parts, and then **your browser's own
speech synthesis reads it to you** with live word highlighting, an audiobook-style
player (speed up to 4x, sleep timer, chapters, jump anywhere), and automatic
progress saving. No GPU, no TTS API bills, no audio files stored anywhere: the
server only ever holds text.

- **No account needed.** Books and progress stick to your browser via an
  anonymous cookie.
- **Create an account** (email + password) and everything you already imported
  is claimed by it, synced through Postgres, and resumes on any device.
- Voice quality depends on the client: Edge and Android ship excellent free
  voices, Chrome desktop is decent, Linux browsers fall back to espeak.

## Stack

FastAPI + uvicorn, Postgres (`psycopg`), vanilla HTML/JS frontend (no build
step), one Docker image. Text extraction: PyMuPDF (PDF), ebooklib +
BeautifulSoup (EPUB), plain parsing for Markdown/TXT — including PDF
header/footer/page-number cleanup, hyphenation repair and chapter detection.

## Run

```bash
cp .env.example .env          # set DATABASE_URL + SECRET_KEY
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn backend.main:app --reload
# or
docker build -t loudreader . && docker run --env-file .env -p 8000:8000 loudreader
```

The app creates its own tables on startup; the Postgres *database* must exist.
Without a reachable database it still serves, with imports disabled (503).

## Layout

- `backend/main.py` — FastAPI app: pages, JSON API, session identity
- `backend/db.py` — connection pool + schema (best-effort init)
- `backend/auth.py` — bcrypt + anonymous/account identity
- `backend/bookprep.py` — extract, clean, chapterize, chunk (~900-word parts)
- `frontend/index.html` — library: import, shelf, account modal
- `frontend/reader.html` — the reader/player (speechSynthesis engine)

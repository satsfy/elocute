# Loudreader

Listen to audiobooks as your browser read them.

Upload a PDF, EPUB, Markdown or TXT. Loudreader extracts and
cleans the text, and then **your browser's TTS reads it** with live word highlighting, audiobook-style player. You can optionally sign up to save the books you uploaded across sessions and devices, with automatic progress saving.


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
- `backend/bookprep.py` — extract, clean, chapterize (one continuous text per book)
- `frontend/index.html` — library: import, shelf, account modal
- `frontend/reader.html` — the reader/player (speechSynthesis engine)

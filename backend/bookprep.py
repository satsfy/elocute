"""Book preparation: extract, clean, chapterize, chunk.

Turns an uploaded document (PDF / EPUB / Markdown / TXT, or pasted text) into
clean TTS-ready chunks of ~TARGET_WORDS words plus a chapter map and a cover
image. Everything is CPU-only and stateless; the API stores the result in
Postgres and the browser does the speaking.

Cleanup deals with the artifacts that ruin narration:
  - PDF: repeated page headers/footers, standalone page numbers, TOC dot
    leaders, hyphen-ation across line breaks, paragraphs split across pages.
  - EPUB: scripts/styles/nav, <sup> footnote markers.
  - everywhere: [12]-style citation markers, soft hyphens, control chars.

Chapter starts always begin a new chunk, so jumping to a chapter is jumping to
a chunk. Heading text is kept in the chunk (narrated, like an audiobook).
"""

from __future__ import annotations

import base64
import os
import re
import tempfile
from collections import Counter
from pathlib import Path

TARGET_WORDS = 900   # ~7 min of audio per chunk at Kokoro's ~125 wpm
MAX_WORDS = 1200     # hard cap; a single paragraph longer than this is split
MIN_WORDS = 200      # don't close a chunk earlier than this unless forced
MAX_CHARS = 5_000_000  # safety cap on extracted text (~800k words)

# (kind, text, level): kind "h" = heading (level 1..4), "p" = paragraph (level 0)
Block = tuple[str, str, int]

_BOOK_EXTS = (".pdf", ".epub", ".md", ".markdown", ".mdown", ".txt")

_SENT_END = re.compile(r"[.!?…\"'”’)\]:;]$")
_ROMAN = re.compile(r"^[ivxlcdm]+$")


def _wc(s: str) -> int:
    return len(s.split())


def _clean_inline(t: str) -> str:
    """Per-paragraph cleanup applied to every block of every format."""
    t = t.replace("­", "")                     # soft hyphen
    t = t.replace("ﬁ", "fi").replace("ﬂ", "fl")
    t = re.sub(r"(\w)-\s*\n\s*(\w)", r"\1\2", t)    # hyphen across line break
    t = re.sub(r"\[\d{1,3}\]", "", t)               # [12] citation markers
    t = re.sub(r"[\x00-\x08\x0b-\x1f\x7f]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _norm_line(s: str) -> str:
    """Normalized fingerprint for header/footer repetition detection."""
    return re.sub(r"\d+", "#", re.sub(r"\s+", " ", s.strip().lower()))[:80]


def _is_page_number(s: str) -> bool:
    n = s.strip().lower()
    return bool(
        re.fullmatch(r"[\d#]+", _norm_line(n))
        or _ROMAN.fullmatch(n)
        or re.fullmatch(r"page\s+\d+(\s+of\s+\d+)?", n)
    )


# ---------------------------------------------------------------------------
# PDF
# ---------------------------------------------------------------------------

def _pdf_cover(doc) -> tuple[bytes | None, str | None]:
    try:
        page = doc[0]
        zoom = min(2.0, 340.0 / max(1.0, page.rect.width))
        import fitz

        pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        try:
            return pix.tobytes("jpeg"), "image/jpeg"
        except Exception:
            return pix.tobytes("png"), "image/png"
    except Exception:
        return None, None


def _pdf_blocks(data: bytes) -> tuple[list[Block], str | None, str | None, bytes | None, str | None]:
    import fitz

    doc = fitz.open(stream=data, filetype="pdf")
    meta = doc.metadata or {}
    title = (meta.get("title") or "").strip() or None
    author = (meta.get("author") or "").strip() or None
    cover, cover_mime = _pdf_cover(doc)

    # Pull text blocks per page (block mode gives paragraph-ish units).
    pages: list[list[str]] = []
    total = 0
    for page in doc:
        texts = []
        for b in page.get_text("blocks"):
            if len(b) >= 7 and b[6] != 0:  # image blocks
                continue
            t = b[4].strip()
            if t:
                texts.append(t)
                total += len(t)
        pages.append(texts)
        if total >= MAX_CHARS:
            break

    # Repeated header/footer detection: fingerprint the first and last block of
    # each page; anything short that repeats across >=25% of pages is furniture.
    tops = Counter(_norm_line(p[0]) for p in pages if p)
    bots = Counter(_norm_line(p[-1]) for p in pages if p)
    npages = max(1, sum(1 for p in pages if p))
    threshold = max(3, int(npages * 0.25))
    kill = {
        k
        for counter in (tops, bots)
        for k, c in counter.items()
        if c >= threshold and len(k.split()) <= 10
    }

    toc = []
    try:
        toc = doc.get_toc(simple=True) or []
    except Exception:
        toc = []
    # page(1-based) -> [titles]; only top-two levels become chapters.
    toc_by_page: dict[int, list[str]] = {}
    for lvl, t, pageno in toc:
        if lvl <= 2 and t and t.strip() and pageno >= 1:
            toc_by_page.setdefault(pageno, []).append(_clean_inline(t))

    blocks: list[Block] = []
    for pageno, texts in enumerate(pages, start=1):
        first_of_page = True
        for heading in toc_by_page.get(pageno, []):
            blocks.append(("h", heading, 1))
        for raw in texts:
            if _norm_line(raw) in kill or _is_page_number(raw):
                continue
            # TOC dot leaders: "Chapter One ......... 12"
            lines = [re.sub(r"\.{3,}\s*\d*\s*$", "", ln) for ln in raw.split("\n")]
            t = _clean_inline("\n".join(lines))
            if not t:
                continue
            # The printed chapter title often follows the TOC heading we just
            # emitted; drop the duplicate so it isn't narrated twice.
            if (
                first_of_page
                and blocks
                and blocks[-1][0] == "h"
                and _norm_line(t) == _norm_line(blocks[-1][1])
            ):
                first_of_page = False
                continue
            # A page break mid-paragraph: previous block has no sentence end
            # and this one starts lowercase -> same paragraph, merge.
            if (
                first_of_page
                and blocks
                and blocks[-1][0] == "p"
                and not _SENT_END.search(blocks[-1][1])
                and t[:1].islower()
            ):
                blocks[-1] = ("p", blocks[-1][1] + " " + t, 0)
            else:
                blocks.append(("p", t, 0))
            first_of_page = False
    doc.close()
    return blocks, title, author, cover, cover_mime


# ---------------------------------------------------------------------------
# EPUB
# ---------------------------------------------------------------------------

_EPUB_TEXT_TAGS = ["h1", "h2", "h3", "h4", "p", "li", "blockquote", "dd", "dt", "figcaption"]
_EPUB_BLOCK_SET = set(_EPUB_TEXT_TAGS)


def _epub_meta(book, name: str) -> str | None:
    try:
        vals = book.get_metadata("DC", name)
        if vals and vals[0] and vals[0][0]:
            return str(vals[0][0]).strip() or None
    except Exception:
        pass
    return None


def _epub_cover(book) -> tuple[bytes | None, str | None]:
    import ebooklib

    try:
        for item in book.get_items_of_type(ebooklib.ITEM_COVER):
            return item.get_content(), item.media_type or "image/jpeg"
        for item in book.get_items_of_type(ebooklib.ITEM_IMAGE):
            if "cover" in (item.get_name() or "").lower():
                return item.get_content(), item.media_type or "image/jpeg"
    except Exception:
        pass
    return None, None


def _epub_blocks(data: bytes) -> tuple[list[Block], str | None, str | None, bytes | None, str | None]:
    import ebooklib
    from bs4 import BeautifulSoup
    from ebooklib import epub

    with tempfile.NamedTemporaryFile(suffix=".epub", delete=False) as tmp:
        tmp.write(data)
        tmp_path = tmp.name
    try:
        book = epub.read_epub(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    title = _epub_meta(book, "title")
    author = _epub_meta(book, "creator")
    cover, cover_mime = _epub_cover(book)

    # Spine order = reading order. Fall back to all documents if spine is odd.
    items = []
    for entry in book.spine or []:
        idref = entry[0] if isinstance(entry, (tuple, list)) else entry
        item = book.get_item_with_id(idref)
        if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
            items.append(item)
    if not items:
        items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    blocks: list[Block] = []
    total = 0
    for item in items:
        try:
            soup = BeautifulSoup(item.get_content(), "html.parser")
        except Exception:
            continue
        for t in soup(["script", "style", "nav", "sup"]):
            t.decompose()
        for el in soup.find_all(_EPUB_TEXT_TAGS):
            # Skip containers whose text will arrive via a nested block tag
            # (blockquote > p, li > p) to avoid narrating it twice.
            if el.find(list(_EPUB_BLOCK_SET)) is not None:
                continue
            t = _clean_inline(el.get_text(" ", strip=True))
            if not t:
                continue
            if el.name in ("h1", "h2", "h3", "h4"):
                blocks.append(("h", t, int(el.name[1])))
            else:
                blocks.append(("p", t, 0))
            total += len(t)
        if total >= MAX_CHARS:
            break
    return blocks, title, author, cover, cover_mime


# ---------------------------------------------------------------------------
# Markdown / plain text
# ---------------------------------------------------------------------------

def _md_blocks(raw: str) -> list[Block]:
    text = re.sub(r"```.*?```", "\n", raw, flags=re.S)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)

    blocks: list[Block] = []
    para: list[str] = []

    def flush():
        if para:
            t = _clean_inline(" ".join(para))
            if t:
                blocks.append(("p", t, 0))
            para.clear()

    for line in text.split("\n"):
        ln = line.strip()
        m = re.match(r"^(#{1,6})\s+(.*)$", ln)
        if m:
            flush()
            t = _clean_inline(m.group(2).strip("# "))
            if t:
                blocks.append(("h", t, min(4, len(m.group(1)))))
            continue
        if not ln or re.fullmatch(r"[-*_]{3,}", ln) or (ln.count("|") >= 2):
            flush()
            continue
        ln = re.sub(r"^>\s?", "", ln)
        ln = re.sub(r"^[-*+]\s+", "", ln)
        ln = re.sub(r"^\d+\.\s+", "", ln)
        ln = re.sub(r"[*_]{1,3}([^*_]+)[*_]{1,3}", r"\1", ln)
        para.append(ln)
    flush()
    return blocks


_TXT_HEADING = re.compile(
    r"^(chapter|part|book|section|prologue|epilogue|introduction|preface|appendix|act|canto)\b",
    re.I,
)


def _txt_blocks(raw: str) -> list[Block]:
    blocks: list[Block] = []
    for para in re.split(r"\n\s*\n", raw.replace("\r", "")):
        t = _clean_inline(para)
        if not t:
            continue
        words = t.split()
        looks_heading = len(words) <= 8 and (
            _TXT_HEADING.match(t)
            or _ROMAN.fullmatch(t.lower().rstrip("."))
            or (t.isupper() and len(words) >= 1 and not _is_page_number(t))
        )
        if looks_heading:
            blocks.append(("h", t, 2))
        elif _is_page_number(t):
            continue
        else:
            blocks.append(("p", t, 0))
    return blocks


# ---------------------------------------------------------------------------
# Chunking
# ---------------------------------------------------------------------------

def _split_long(text: str) -> list[str]:
    """Split a paragraph longer than MAX_WORDS at sentence boundaries."""
    if _wc(text) <= MAX_WORDS:
        return [text]
    out: list[str] = []
    cur: list[str] = []
    cur_w = 0
    for s in re.split(r"(?<=[.!?…])\s+", text):
        w = _wc(s)
        if cur_w and cur_w + w > TARGET_WORDS:
            out.append(" ".join(cur))
            cur, cur_w = [], 0
        cur.append(s)
        cur_w += w
    if cur:
        out.append(" ".join(cur))
    return out


def _chunk_blocks(blocks: list[Block]) -> tuple[list[dict], list[dict]]:
    """Pack blocks into chunks; chapter headings force a chunk boundary.

    Returns (chunks, chapters) where chunks[i] = {text, words, chapter} and
    chapters = [{title, chunk}]. `chapter` on a chunk is the active chapter
    title (for the player's "current chapter" label).
    """
    chunks: list[dict] = []
    chapters: list[dict] = []
    cur: list[str] = []
    cur_w = 0
    active_chapter: str | None = None

    def close():
        nonlocal cur, cur_w
        if cur:
            chunks.append({"text": "\n\n".join(cur), "words": cur_w, "chapter": active_chapter})
            cur, cur_w = [], 0

    for kind, text, level in blocks:
        if kind == "h" and level <= 2:
            close()
            active_chapter = text[:200]
            if not chapters or chapters[-1]["chunk"] != len(chunks):
                chapters.append({"title": active_chapter, "chunk": len(chunks)})
            else:
                # Two headings back to back (Part I / Chapter 1): keep both in
                # the label but a single chapter entry per chunk.
                chapters[-1]["title"] = (chapters[-1]["title"] + " — " + active_chapter)[:200]
            cur.append(text)
            cur_w += _wc(text)
            continue
        for piece in _split_long(text) if kind == "p" else [text]:
            w = _wc(piece)
            if cur_w and cur_w + w > TARGET_WORDS and (cur_w >= MIN_WORDS or cur_w + w > MAX_WORDS):
                close()
            cur.append(piece)
            cur_w += w
    close()

    chapters = [c for c in chapters if c["chunk"] < len(chunks)]
    return chunks, chapters


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def prepare_book(
    filename: str | None,
    data: bytes | None,
    text: str | None = None,
    title_hint: str | None = None,
) -> dict:
    """Extract + clean + chunk a document (or pasted text) for audiobook use.

    Returns a JSON-safe dict:
      {title, author, cover_b64, cover_mime, total_words,
       chunks: [{idx, words, chapter, text}], chapters: [{title, chunk}]}
    """
    title = author = None
    cover = cover_mime = None

    if data is not None and filename:
        ext = Path(filename).suffix.lower()
        if ext == ".pdf":
            blocks, title, author, cover, cover_mime = _pdf_blocks(data)
        elif ext == ".epub":
            blocks, title, author, cover, cover_mime = _epub_blocks(data)
        elif ext in (".md", ".markdown", ".mdown"):
            blocks = _md_blocks(data.decode("utf-8", errors="ignore")[:MAX_CHARS])
        else:
            blocks = _txt_blocks(data.decode("utf-8", errors="ignore")[:MAX_CHARS])
        if not title:
            title = Path(filename).stem.replace("_", " ").strip() or None
    else:
        blocks = _txt_blocks((text or "")[:MAX_CHARS])
        if not title:
            snippet = " ".join((text or "").split())[:60].strip()
            title = title_hint or snippet or "pasted text"

    chunks, chapters = _chunk_blocks(blocks)
    if not chunks:
        raise ValueError("No narratable text found in the document.")

    # Cumulative word offsets let the UI compute global progress cheaply.
    start = 0
    out_chunks = []
    for i, c in enumerate(chunks):
        out_chunks.append(
            {
                "idx": i,
                "words": c["words"],
                "words_start": start,
                "chapter": c["chapter"],
                "text": c["text"],
            }
        )
        start += c["words"]

    return {
        "title": (title_hint or title or "book")[:500],
        "author": (author or "")[:500] or None,
        "cover_b64": base64.b64encode(cover).decode() if cover else None,
        "cover_mime": cover_mime,
        "total_words": start,
        "total_chunks": len(out_chunks),
        "chapters": chapters,
        "chunks": out_chunks,
    }

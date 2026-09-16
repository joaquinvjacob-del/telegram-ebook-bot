"""
Bot de Telegram para buscar y descargar libros de dominio publico
(Project Gutenberg + Internet Archive) en PDF / EPUB / Kindle (.mobi).

Modo de uso:
    - Polling (local / pruebas):  python main.py
    - Webhook (Render, produccion): se activa solo si la variable de
      entorno WEBHOOK_URL esta definida (ver README.md).

Variables de entorno requeridas:
    BOT_TOKEN     -> token de @BotFather
    WEBHOOK_URL   -> (opcional) URL publica del servicio, ej:
                     https://mi-bot.onrender.com
    PORT          -> (opcional) puerto, lo define Render automaticamente
"""

import asyncio
import html
import json
import logging
import math
import os
import re
import secrets
import socket
import uuid
from collections import OrderedDict
from pathlib import Path

import httpx
from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, InputFile, Update
from telegram.constants import ParseMode
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# Some networks (this dev machine included) advertise IPv6 for a host but
# the route is actually broken/blackholed. httpx then hangs on the IPv6
# attempt for the full timeout before ever trying IPv4, which is what
# caused the intermittent "Gutenberg search failed" timeouts we saw in
# testing. Forcing IPv4-only DNS resolution for the whole process sidesteps
# that — all our sources (Gutenberg, Archive.org, Standard Ebooks) are
# perfectly reachable over IPv4.
_original_getaddrinfo = socket.getaddrinfo


def _ipv4_only_getaddrinfo(host, port, family=0, type=0, proto=0, flags=0):
    return _original_getaddrinfo(host, port, socket.AF_INET, type, proto, flags)


socket.getaddrinfo = _ipv4_only_getaddrinfo

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Access control — gates features behind a one-time-use code, per FEATURE.
#
# "ebooks" (search + /popular) and "audiobooks" (/audiobook) are unlocked
# independently: the audiobook upsell is a separate purchase with its own
# codes, so owning one never implies the other.
#
# Disabled by default (REQUIRE_ACCESS_CODE unset) so local testing keeps
# working without a code. Set REQUIRE_ACCESS_CODE=true in the environment
# to turn the gate on for real users. Codes + who redeemed them are stored
# in access.json on disk (not in-memory) so paying users don't lose access
# if the bot restarts or redeploys.
# ---------------------------------------------------------------------------

REQUIRE_ACCESS_CODE = os.environ.get("REQUIRE_ACCESS_CODE", "").lower() in ("1", "true", "yes")
ACCESS_FILE = Path(__file__).parent / "access.json"
_access_lock = asyncio.Lock()

FEATURE_NAMES = {"ebooks": "eBook search", "audiobooks": "Audiobooks"}

LOCKED_MESSAGES = {
    "ebooks": (
        "🔒 *This feature needs an access code.*\n\n"
        "Got one from your purchase? Send:\n"
        "`/unlock YOUR-CODE-HERE`\n\n"
        "Don't have a code yet? Check the link where you found this bot."
    ),
    "audiobooks": (
        "🔒 *Audiobooks are a separate add-on.*\n\n"
        "Got an audiobook code from your purchase? Send:\n"
        "`/unlock YOUR-CODE-HERE`\n\n"
        "Don't have one yet? Check the link where you found this bot."
    ),
}


def _load_access() -> dict:
    if ACCESS_FILE.exists():
        return json.loads(ACCESS_FILE.read_text(encoding="utf-8"))
    return {"codes": {}, "unlocked": {}}


def _save_access(data: dict) -> None:
    ACCESS_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


async def is_unlocked(chat_id: int, feature: str) -> bool:
    if not REQUIRE_ACCESS_CODE:
        return True
    async with _access_lock:
        data = _load_access()
    return chat_id in data.get("unlocked", {}).get(feature, [])


async def redeem_code(code: str, chat_id: int) -> tuple[str, str | None]:
    """Returns (status, feature). status is 'ok' (freshly unlocked),
    'already' (this chat already used it), 'used_by_other', or 'invalid'
    (feature is None for 'used_by_other'/'invalid')."""
    code = code.strip().upper()
    async with _access_lock:
        data = _load_access()
        codes = data.setdefault("codes", {})
        unlocked = data.setdefault("unlocked", {})
        entry = codes.get(code)
        if entry is None:
            return "invalid", None
        feature = entry["feature"]
        owner = entry.get("used_by")
        if owner is not None and owner != chat_id:
            return "used_by_other", None
        entry["used_by"] = chat_id
        feature_list = unlocked.setdefault(feature, [])
        result = "already" if chat_id in feature_list else "ok"
        if chat_id not in feature_list:
            feature_list.append(chat_id)
        _save_access(data)
        return result, feature


def generate_codes(count: int, feature: str, prefix: str | None = None) -> list[str]:
    """Utility for the bot owner: create `count` fresh single-use codes for
    a given feature ("ebooks" or "audiobooks") and persist them (unused) to
    access.json. Run this from a one-off script or a Python shell — it's
    not exposed as a bot command on purpose, so strangers can never mint
    their own codes."""
    if feature not in FEATURE_NAMES:
        raise ValueError(f"Unknown feature: {feature!r}")
    prefix = prefix or ("AUDIO" if feature == "audiobooks" else "BOOKS")
    data = _load_access()
    codes = data.setdefault("codes", {})
    new_codes = []
    while len(new_codes) < count:
        candidate = f"{prefix}-{secrets.token_hex(3).upper()}"
        if candidate not in codes:
            codes[candidate] = {"feature": feature, "used_by": None}
            new_codes.append(candidate)
    _save_access(data)
    return new_codes


GUTENDEX_URL = "https://gutendex.com/books/"
ARCHIVE_SEARCH_URL = "https://archive.org/advancedsearch.php"
ARCHIVE_METADATA_URL = "https://archive.org/metadata/{identifier}"
STANDARD_EBOOKS_SEARCH_URL = "https://standardebooks.org/ebooks"

MAX_RESULTS_PER_SOURCE = 5

# Every source builds its formats as a list of (label, url, file_extension)
# tuples, already in the order we want buttons to appear (best format
# first). Keeping the extension alongside each url — instead of one global
# label -> extension map — matters because the *same* label can mean a
# different real file type per source (e.g. Standard Ebooks' Kindle file is
# a real .azw3, while Gutenberg's is served as .mobi).

# (mime key in gutendex's `formats` dict, button label, file extension)
GUTENBERG_FORMATS = [
    ("application/epub+zip", "EPUB", ".epub"),
    ("application/x-mobipocket-ebook", "Kindle (.mobi)", ".mobi"),
    ("application/pdf", "PDF", ".pdf"),
    ("text/plain; charset=utf-8", "TXT", ".txt"),
]

# (filename suffix on archive.org, button label, file extension)
ARCHIVE_FILE_SUFFIXES = [
    (".epub", "EPUB", ".epub"),
    (".pdf", "PDF", ".pdf"),
    ("_djvu.txt", "TXT", ".txt"),
]

# Standard Ebooks doesn't need per-book resolution: every ebook page follows
# this exact, stable download URL pattern, so we can build it straight from
# the search result without an extra HTTP request per book.
SE_BOOK_RE = re.compile(
    r'<li typeof="schema:Book" about="(?P<path>/ebooks/[^"]+)">.*?'
    r'<span property="schema:name">(?P<title>[^<]+)</span>.*?'
    r'class="author"[^>]*>.*?<span property="schema:name">(?P<author>[^<]+)</span>',
    re.S,
)

# Priority when the same book shows up from more than one source: Standard
# Ebooks re-typesets classics by hand (best quality), Gutenberg is the most
# reliable bulk catalog, Internet Archive is the long-tail fallback.
SOURCE_PRIORITY = {"standardebooks": 0, "gutenberg": 1, "archive": 2}


def gutenberg_formats_from(item_formats: dict) -> list[tuple[str, str, str]]:
    return [(label, item_formats[mime], ext) for mime, label, ext in GUTENBERG_FORMATS if mime in item_formats]


def standard_ebooks_formats(path: str) -> list[tuple[str, str, str]]:
    # Most paths are /ebooks/{author}/{title}, but works with a named
    # translator/editor get a third segment, e.g.
    # /ebooks/leo-tolstoy/war-and-peace/louise-maude_aylmer-maude — and any
    # underscore *within* a segment (joining co-translators) becomes a
    # hyphen in the real filename, since underscore is the segment joiner.
    # Verified directly against standardebooks.org's own download links.
    segments = path.removeprefix("/ebooks/").split("/")
    slug = "_".join(segment.replace("_", "-") for segment in segments)
    base = f"https://standardebooks.org{path}/downloads/{slug}"
    return [
        ("EPUB", f"{base}.epub", ".epub"),
        ("Kindle (.azw3)", f"{base}.azw3", ".azw3"),
    ]


def normalize_title(title: str) -> str:
    """Collapse a title down to its 'core' for cross-source deduplication,
    e.g. 'Frankenstein; or, The Modern Prometheus' and 'Frankenstein' both
    normalize to 'frankenstein'."""
    core = re.split(r"[:;(]", title)[0]
    core = re.sub(r"[^\w\s]", "", core).lower().strip()
    return re.sub(r"\s+", " ", core)


def normalize_author(author: str) -> str:
    """Sources format authors differently (Gutenberg: 'Doyle, Arthur Conan',
    others: 'Arthur Conan Doyle') — sorting the words makes both forms
    compare equal without needing to guess at name order."""
    words = re.sub(r"[^\w\s]", " ", author).lower().split()
    return " ".join(sorted(words))


def dedupe_by_title(results: list[dict]) -> list[dict]:
    # Keying on title alone would risk merging two *different* books that
    # happen to share a short/generic normalized title (e.g. two unrelated
    # "The Voyage" by different authors), silently dropping one of them.
    # Requiring the author to match too avoids that false merge.
    best: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for book in results:
        key = (normalize_title(book["title"]), normalize_author(book["author"]))
        current = best.get(key)
        if current is None:
            best[key] = book
            order.append(key)
        elif SOURCE_PRIORITY[book["source"]] < SOURCE_PRIORITY[current["source"]]:
            best[key] = book
    return [best[key] for key in order]

# Curated classics for /popular. Gutenberg ids verified directly against
# gutendex.com (title, id, and epub/mobi availability all confirmed) so the
# command works instantly without hitting any API.
POPULAR_BOOKS = [
    (1342, "Pride and Prejudice", "Jane Austen"),
    (84, "Frankenstein", "Mary Shelley"),
    (345, "Dracula", "Bram Stoker"),
    (11, "Alice's Adventures in Wonderland", "Lewis Carroll"),
    (98, "A Tale of Two Cities", "Charles Dickens"),
    (2701, "Moby Dick", "Herman Melville"),
    (1661, "The Adventures of Sherlock Holmes", "Arthur Conan Doyle"),
    (2600, "War and Peace", "Leo Tolstoy"),
    (174, "The Picture of Dorian Gray", "Oscar Wilde"),
    (1513, "Romeo and Juliet", "William Shakespeare"),
]


def popular_book_to_result(gutenberg_id: int, title: str, author: str) -> dict:
    base = f"https://www.gutenberg.org/ebooks/{gutenberg_id}"
    return {
        "source": "gutenberg",
        "title": title,
        "author": author,
        "formats": [
            ("EPUB", f"{base}.epub3.images", ".epub"),
            ("Kindle (.mobi)", f"{base}.kf8.images", ".mobi"),
            ("TXT", f"{base}.txt.utf-8", ".txt"),
        ],
    }


# Telegram bots can send files up to 50 MB; leave some margin.
MAX_TELEGRAM_FILE_BYTES = 45 * 1024 * 1024

# Each download is buffered fully in memory before being sent (see
# on_download_request). Capping how many run at once bounds worst-case RAM
# use instead of letting it grow with however many users click "download"
# in the same second — important on a small free-tier host.
MAX_CONCURRENT_DOWNLOADS = 8
DOWNLOAD_SEMAPHORE = asyncio.Semaphore(MAX_CONCURRENT_DOWNLOADS)

MAX_CACHE_ENTRIES = 500


def _register(cache: "OrderedDict[str, object]", value: object) -> str:
    """Store value under a short id (callback_data is capped at 64 bytes by
    Telegram, so we never embed URLs/titles directly in it)."""
    short_id = uuid.uuid4().hex[:12]
    cache[short_id] = value
    while len(cache) > MAX_CACHE_ENTRIES:
        cache.popitem(last=False)
    return short_id


# short_id -> (url, filename). In-memory only: entries are lost on restart,
# which just means the user has to search again to get fresh buttons.
DOWNLOAD_CACHE: "OrderedDict[str, tuple[str, str]]" = OrderedDict()
# short_id -> (archive.org identifier, title, "back to list" callback_data)
ARCHIVE_ITEM_CACHE: "OrderedDict[str, tuple[str, str, str]]" = OrderedDict()
# short_id -> full list of results for one search, so the results-list
# message and every book detail view can be paged/edited in place
SEARCH_CACHE: "OrderedDict[str, list]" = OrderedDict()

PAGE_SIZE = 5


def register_download(url: str, filename: str) -> str:
    return _register(DOWNLOAD_CACHE, (url, filename))


def register_archive_item(identifier: str, title: str, back_callback_data: str) -> str:
    return _register(ARCHIVE_ITEM_CACHE, (identifier, title, back_callback_data))


def register_search_results(results: list) -> str:
    return _register(SEARCH_CACHE, results)


def safe_filename(title: str, extension: str) -> str:
    cleaned = re.sub(r"[^\w\s-]", "", title).strip()
    cleaned = re.sub(r"\s+", "_", cleaned)[:60] or "book"
    return f"{cleaned}{extension}"


# ---------------------------------------------------------------------------
# Busqueda
# ---------------------------------------------------------------------------

async def get_with_retry(client: httpx.AsyncClient, url: str, attempts: int = 3, **kwargs) -> httpx.Response:
    """Gutendex in particular drops connections or times out with no useful
    error message fairly often (observed repeatedly in testing, even after
    ruling out local network issues) — a couple of quick retries clears
    most of those transient failures."""
    last_error: httpx.TransportError | None = None
    for attempt in range(attempts):
        try:
            resp = await client.get(url, **kwargs)
            resp.raise_for_status()
            return resp
        except httpx.TransportError as e:
            last_error = e
            if attempt < attempts - 1:
                await asyncio.sleep(0.5 * (attempt + 1))
    raise last_error


async def search_gutenberg(client: httpx.AsyncClient, query: str) -> list[dict]:
    resp = await get_with_retry(
        client, GUTENDEX_URL, params={"search": query, "languages": "en"}, timeout=15
    )
    data = resp.json()
    books = []
    for item in data.get("results", [])[:MAX_RESULTS_PER_SOURCE]:
        formats = gutenberg_formats_from(item.get("formats", {}))
        if not formats:
            continue
        authors = ", ".join(a.get("name", "?") for a in item.get("authors", [])) or "Unknown author"
        books.append(
            {
                "source": "gutenberg",
                "title": item.get("title", "Untitled"),
                "author": authors,
                "formats": formats,
            }
        )
    return books


async def search_standard_ebooks(client: httpx.AsyncClient, query: str) -> list[dict]:
    # An unquoted multi-word query matches each word independently — a
    # search for "war and peace" once returned unrelated Yeats poetry that
    # just happened to contain "war" and "peace" separately. Quoting forces
    # a phrase match.
    resp = await get_with_retry(
        client, STANDARD_EBOOKS_SEARCH_URL, params={"query": f'"{query}"'}, timeout=15
    )
    matches = list(SE_BOOK_RE.finditer(resp.text))[:MAX_RESULTS_PER_SOURCE]
    books = []
    for m in matches:
        path = m.group("path")
        books.append(
            {
                "source": "standardebooks",
                "title": html.unescape(m.group("title")).strip(),
                "author": html.unescape(m.group("author")).strip(),
                "formats": standard_ebooks_formats(path),
            }
        )
    return books


async def search_archive(client: httpx.AsyncClient, query: str) -> list[dict]:
    params = {
        # Searching the bare phrase matches it anywhere — including deep in a
        # document's OCR'd body text, which is how a search for "dracula"
        # once returned an unrelated 1997 education report that merely
        # mentioned Dracula's Castle as a tourist stop. Restricting to the
        # title field keeps results actually about the book being searched.
        # language:(...) keeps this bot's target audience (English readers)
        # from getting non-English scans — archive.org's language tagging is
        # inconsistent, so untagged items are excluded too rather than risk
        # showing e.g. an untagged Sanskrit manuscript as if it were English.
        "q": (
            f'title:("{query}") AND mediatype:texts '
            "AND -access-restricted-item:true AND language:(eng OR English OR en)"
        ),
        "fl[]": ["identifier", "title", "creator"],
        "rows": MAX_RESULTS_PER_SOURCE,
        "output": "json",
    }
    resp = await get_with_retry(client, ARCHIVE_SEARCH_URL, params=params, timeout=15)
    data = resp.json()
    docs = data.get("response", {}).get("docs", [])
    books = []
    for doc in docs:
        identifier = doc.get("identifier")
        if not identifier:
            continue
        creator = doc.get("creator", "Unknown author")
        if isinstance(creator, list):
            creator = ", ".join(creator)
        books.append(
            {
                "source": "archive",
                "title": doc.get("title", "Untitled"),
                "author": creator,
                "identifier": identifier,
            }
        )
    return books


async def resolve_archive_formats(client: httpx.AsyncClient, identifier: str) -> list[tuple[str, str, str]]:
    resp = await get_with_retry(client, ARCHIVE_METADATA_URL.format(identifier=identifier), timeout=15)
    data = resp.json()
    seen_labels = set()
    formats = []
    for f in data.get("files", []):
        name = f.get("name", "")
        for suffix, label, ext in ARCHIVE_FILE_SUFFIXES:
            if name.lower().endswith(suffix) and label not in seen_labels:
                formats.append((label, f"https://archive.org/download/{identifier}/{name}", ext))
                seen_labels.add(label)
    order = {label: i for i, (_, label, _) in enumerate(ARCHIVE_FILE_SUFFIXES)}
    formats.sort(key=lambda item: order[item[0]])
    return formats


LIBRIVOX_API_URL = "https://librivox.org/api/feed/audiobooks/"

# LibriVox's own general-purpose "search" param is broken — it returns the
# same results regardless of query (verified: "sherlock holmes" and "pride
# and prejudice" both returned identical results). The "title"/"author"
# params only match a literal prefix ("^Query"):
#   - LibriVox often catalogs classics without their leading article —
#     "Adventures of Sherlock Holmes", not "The Adventures of...". Retrying
#     with a stripped leading article covers most title misses.
#   - "author" only matches the *last name* as a prefix — "^Bram Stoker"
#     returns nothing, "^Stoker" returns 15 books (verified directly).
#     Retrying with just the last word of the query covers most author
#     searches ("Bram Stoker" -> "Stoker").
_LEADING_ARTICLE_RE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)


async def _librivox_search(client: httpx.AsyncClient, field: str, value: str, prefix: bool = True) -> list[dict]:
    # LibriVox's API returns HTTP 404 (not 200 + an empty list) when a
    # search matches nothing — confirmed directly. That has to be treated
    # as "zero results", not a real request failure, or the fallback
    # retries below would never get a chance to run.
    # The "^" prefix-match operator applies to text fields (title/author)
    # only — "id" is an exact numeric lookup and 404s if you prefix it.
    query_value = f"^{value}" if prefix else value
    try:
        resp = await get_with_retry(
            client,
            LIBRIVOX_API_URL,
            params={field: query_value, "format": "json", "extended": "1"},
            timeout=15,
        )
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            return []
        raise
    data = resp.json()
    return data.get("books") or []


async def search_librivox(client: httpx.AsyncClient, query: str) -> list[dict]:
    title_books = await _librivox_search(client, "title", query)
    if not title_books:
        stripped = _LEADING_ARTICLE_RE.sub("", query).strip()
        if stripped and stripped.lower() != query.lower():
            title_books = await _librivox_search(client, "title", stripped)

    last_word = query.strip().rsplit(maxsplit=1)[-1] if query.strip() else ""
    author_books = await _librivox_search(client, "author", last_word) if last_word else []

    seen_ids = {b["id"] for b in title_books}
    books = title_books + [b for b in author_books if b["id"] not in seen_ids]

    results = []
    for b in books[:MAX_RESULTS_PER_SOURCE]:
        if b.get("language") != "English":
            continue
        sections = []
        for s in b.get("sections", []) or []:
            url = s.get("listen_url")
            if not url:
                continue
            playtime = int(s.get("playtime") or 0)
            readers = ", ".join(
                r.get("display_name", "") for r in (s.get("readers") or [])
            )
            sections.append(
                {
                    "number": s.get("section_number"),
                    "title": s.get("title") or f"Chapter {s.get('section_number')}",
                    "duration": f"{playtime // 60}:{playtime % 60:02d}",
                    "url": url,
                    "reader": readers,
                }
            )
        if not sections:
            continue
        authors = ", ".join(
            f"{a.get('first_name', '')} {a.get('last_name', '')}".strip() for a in b.get("authors", [])
        ) or "Unknown author"
        results.append(
            {
                "source": "librivox",
                "title": b.get("title", "Untitled"),
                "author": authors,
                "sections": sections,
                "zip_url": b.get("url_zip_file") or None,
            }
        )
    return results


async def audiobook_id_to_result(client: httpx.AsyncClient, librivox_id: int) -> dict | None:
    """Fetch one known audiobook directly by id — used by /popularaudio so
    it works instantly regardless of the title-search quirks above."""
    books = await _librivox_search(client, "id", str(librivox_id), prefix=False)
    if not books:
        return None
    # Reuse the exact same shaping logic as search_librivox by feeding this
    # single book back through it in spirit: build the result inline.
    b = books[0]
    sections = []
    for s in b.get("sections", []) or []:
        url = s.get("listen_url")
        if not url:
            continue
        playtime = int(s.get("playtime") or 0)
        readers = ", ".join(r.get("display_name", "") for r in (s.get("readers") or []))
        sections.append(
            {
                "number": s.get("section_number"),
                "title": s.get("title") or f"Chapter {s.get('section_number')}",
                "duration": f"{playtime // 60}:{playtime % 60:02d}",
                "url": url,
                "reader": readers,
            }
        )
    if not sections:
        return None
    authors = ", ".join(
        f"{a.get('first_name', '')} {a.get('last_name', '')}".strip() for a in b.get("authors", [])
    ) or "Unknown author"
    return {
        "source": "librivox",
        "title": b.get("title", "Untitled"),
        "author": authors,
        "sections": sections,
        "zip_url": b.get("url_zip_file") or None,
    }


# Curated classics for /popularaudio. LibriVox ids verified directly
# against the API (title, section count, author all confirmed) so the
# command works instantly without depending on LibriVox's flaky title
# search.
POPULAR_AUDIOBOOKS = [253, 381, 271, 200, 753, 314, 365, 510, 628, 449]

CHAPTER_PAGE_SIZE = 10

# short_id -> full audiobook dict (title/author/sections/zip_url), so the
# chapter list can be paginated by re-rendering from the same source data.
AUDIOBOOK_CACHE: "OrderedDict[str, dict]" = OrderedDict()


def register_audiobook(book: dict) -> str:
    return _register(AUDIOBOOK_CACHE, book)


def render_chapter_list(
    book: dict, audiobook_id: str, page: int, back_callback_data: str
) -> tuple[str, InlineKeyboardMarkup]:
    """Audiobooks skip the format-choice detail view entirely: there's just
    chapters, so picking a chapter's number button downloads it immediately
    via the same 'dl:' flow as everything else."""
    sections = book["sections"]
    total_pages = max(1, math.ceil(len(sections) / CHAPTER_PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * CHAPTER_PAGE_SIZE
    page_items = sections[start : start + CHAPTER_PAGE_SIZE]

    lines = []
    for s in page_items:
        reader_suffix = f" — {html.escape(s['reader'])}" if s.get("reader") else ""
        lines.append(f"{s['number']}. {html.escape(s['title'])} ({s['duration']}){reader_suffix}")
    text = f"🎧 <b>{html.escape(book['title'])}</b>\n👤 {html.escape(book['author'])}\n\n" + "\n".join(lines)
    if total_pages > 1:
        text += f"\n\nPage {page + 1}/{total_pages}"

    rows, row = [], []
    for s in page_items:
        filename = safe_filename(f"{book['title']} {s['number']}", ".mp3")
        dl_id = register_download(s["url"], filename)
        row.append(InlineKeyboardButton(str(s["number"]), callback_data=f"dl:{dl_id}"))
        if len(row) == 5:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    if total_pages > 1:
        rows.append(
            [
                InlineKeyboardButton("⏮", callback_data=f"chpage:{audiobook_id}:0"),
                InlineKeyboardButton("◀️", callback_data=f"chpage:{audiobook_id}:{max(page - 1, 0)}"),
                InlineKeyboardButton("▶️", callback_data=f"chpage:{audiobook_id}:{min(page + 1, total_pages - 1)}"),
                InlineKeyboardButton("⏭", callback_data=f"chpage:{audiobook_id}:{total_pages - 1}"),
            ]
        )
    if book.get("zip_url"):
        zip_dl_id = register_download(book["zip_url"], safe_filename(book["title"], ".zip"))
        rows.append([InlineKeyboardButton("📦 Download all (ZIP)", callback_data=f"dl:{zip_dl_id}")])
    rows.append([InlineKeyboardButton("🔙 Back to results", callback_data=back_callback_data)])
    return text, InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

WELCOME = (
    "📚 *Welcome to the free eBook finder*\n\n"
    "I search *Standard Ebooks*, *Project Gutenberg*, and *Internet Archive* "
    "for public-domain books and send you the file right here in the chat — "
    "EPUB, Kindle, or PDF, whichever the source has available.\n\n"
    "Just type the title or author you're looking for — no command needed.\n\n"
    "Example: `Pride and Prejudice`\n\n"
    "💡 Tip: if a book doesn't show up by its full title, try searching just "
    "the author's last name.\n\n"
    "⏳ If the bot has been idle for a while, the first search may take a "
    "few extra seconds to respond — thanks for your patience.\n\n"
    "You can also try /popular for a list of classics ready to download, "
    "no searching needed."
)

# TODO: set this once the Instagram account exists, then redeploy.
INSTAGRAM_HANDLE = "@your_ig_handle_here"

HELP = (
    "🛟 *Support*\n\n"
    f"Running into an issue? Message us on Instagram — {INSTAGRAM_HANDLE} — "
    "and we'll get it sorted.\n\n"
    "Otherwise, using the bot is simple: just type the title or author of "
    "the book you want, right here in this chat."
)


def render_book_detail(book: dict, back_callback_data: str) -> tuple[str, InlineKeyboardMarkup]:
    """Build the (text, keyboard) for one book's download options, plus a
    'back to results' row so picking a book is never a dead end."""
    text = f"📖 <b>{html.escape(book['title'])}</b>\n👤 {html.escape(book['author'])}"
    rows = []
    if "formats" in book:
        rows.append(
            [
                InlineKeyboardButton(
                    f"⬇️ {label}", callback_data=f"dl:{register_download(url, safe_filename(book['title'], ext))}"
                )
                for label, url, ext in book["formats"]
            ]
        )
    else:
        short_id = register_archive_item(book["identifier"], book["title"], back_callback_data)
        rows.append([InlineKeyboardButton("See available formats", callback_data=f"ia:{short_id}")])
    rows.append([InlineKeyboardButton("🔙 Back to results", callback_data=back_callback_data)])
    return text, InlineKeyboardMarkup(rows)


def render_results_list(search_id: str, results: list[dict], page: int) -> tuple[str, InlineKeyboardMarkup]:
    """One compact message: a numbered list of titles for the current page,
    number buttons to pick one, and first/prev/next/last navigation that
    edits this same message in place instead of sending new ones."""
    total_pages = max(1, math.ceil(len(results) / PAGE_SIZE))
    page = max(0, min(page, total_pages - 1))
    start = page * PAGE_SIZE
    page_items = results[start : start + PAGE_SIZE]

    lines = [
        f"{i}. <b>{html.escape(book['title'])}</b> — {html.escape(book['author'])}"
        for i, book in enumerate(page_items, start=1)
    ]
    text = "\n".join(lines)
    if total_pages > 1:
        text += f"\n\nPage {page + 1}/{total_pages}"

    rows = [
        [
            InlineKeyboardButton(str(i), callback_data=f"pick:{search_id}:{start + i - 1}")
            for i in range(1, len(page_items) + 1)
        ]
    ]
    if total_pages > 1:
        rows.append(
            [
                InlineKeyboardButton("⏮", callback_data=f"page:{search_id}:0"),
                InlineKeyboardButton("◀️", callback_data=f"page:{search_id}:{max(page - 1, 0)}"),
                InlineKeyboardButton("▶️", callback_data=f"page:{search_id}:{min(page + 1, total_pages - 1)}"),
                InlineKeyboardButton("⏭", callback_data=f"page:{search_id}:{total_pages - 1}"),
            ]
        )
    return text, InlineKeyboardMarkup(rows)


async def edit_ignoring_unchanged(query, text: str, keyboard: InlineKeyboardMarkup) -> None:
    """Tapping a boundary nav button (e.g. '⏮' while already on page 1)
    re-renders identical content, which Telegram rejects as a no-op edit.
    That's expected here, not an error, so it's the one TelegramError we
    swallow instead of letting it bubble up as a real failure."""
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    except TelegramError as e:
        if "not modified" not in str(e).lower():
            raise


WELCOME_KEYBOARD = InlineKeyboardMarkup(
    [[InlineKeyboardButton("🔄 Reset conversation", callback_data="reset")]]
)

# How many prior messages (in both directions) a reset attempts to wipe.
# Telegram only allows deleting messages younger than 48h, and only within
# a chat's own message-id range, so this is a best-effort cleanup, not a
# guaranteed full wipe of very long histories.
RESET_LOOKBACK = 100


async def do_reset(bot, chat_id: int, from_message_id: int) -> None:
    for message_id in range(from_message_id, max(from_message_id - RESET_LOOKBACK, 0), -1):
        try:
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except TelegramError:
            # Expected for most ids: already deleted, too old (>48h), or
            # never existed in this chat. Nothing to do about those.
            pass
    await bot.send_message(
        chat_id=chat_id,
        text=WELCOME,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=WELCOME_KEYBOARD,
    )


def is_private_chat(update: Update) -> bool:
    return update.effective_chat.type == "private"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(WELCOME, parse_mode=ParseMode.MARKDOWN, reply_markup=WELCOME_KEYBOARD)


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP, parse_mode=ParseMode.MARKDOWN)


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await do_reset(context.bot, update.effective_chat.id, update.message.message_id)


async def on_reset_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer("Resetting...")
    await do_reset(context.bot, query.message.chat_id, query.message.message_id)


async def unlock(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: `/unlock YOUR-CODE-HERE`", parse_mode=ParseMode.MARKDOWN)
        return
    code = " ".join(context.args)
    result, feature = await redeem_code(code, update.effective_chat.id)
    if result in ("ok", "already"):
        if feature == "audiobooks":
            await update.message.reply_text("✅ Audiobooks unlocked! Try /audiobook <title>.")
        else:
            await update.message.reply_text("✅ Unlocked! Just type any book title to search.")
    elif result == "used_by_other":
        await update.message.reply_text("This code has already been used on another account.")
    else:
        await update.message.reply_text("That code isn't valid. Double-check it and try again.")


async def popular(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    if not await is_unlocked(update.effective_chat.id, "ebooks"):
        await update.message.reply_text(LOCKED_MESSAGES["ebooks"], parse_mode=ParseMode.MARKDOWN)
        return
    results = [popular_book_to_result(*entry) for entry in POPULAR_BOOKS]
    search_id = register_search_results(results)
    text, keyboard = render_results_list(search_id, results, 0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def handle_search_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Any plain-text message (not a command) is treated as a book search —
    there's no /search command to type anymore."""
    if not is_private_chat(update):
        return
    if not await is_unlocked(update.effective_chat.id, "ebooks"):
        await update.message.reply_text(LOCKED_MESSAGES["ebooks"], parse_mode=ParseMode.MARKDOWN)
        return

    query = (update.message.text or "").strip()
    if not query:
        return

    status_msg = await update.message.reply_text(f"🔍 Searching for \"{query}\"...")

    async with httpx.AsyncClient() as client:
        se_result, gb_result, ia_result = await asyncio.gather(
            search_standard_ebooks(client, query),
            search_gutenberg(client, query),
            search_archive(client, query),
            return_exceptions=True,
        )

    def unwrap(result, source_name: str) -> list[dict]:
        if isinstance(result, Exception):
            logger.warning("%s search failed: %s", source_name, result)
            return []
        return result

    standardebooks_books = unwrap(se_result, "Standard Ebooks")
    gutenberg_books = unwrap(gb_result, "Gutenberg")
    archive_books = unwrap(ia_result, "Archive")

    results = dedupe_by_title(standardebooks_books + gutenberg_books + archive_books)
    if not results:
        await status_msg.edit_text(
            "No results found. Try a different title, or just the author's last name."
        )
        return

    search_id = register_search_results(results)
    text, keyboard = render_results_list(search_id, results, 0)
    await status_msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def audiobook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    if not await is_unlocked(update.effective_chat.id, "audiobooks"):
        await update.message.reply_text(LOCKED_MESSAGES["audiobooks"], parse_mode=ParseMode.MARKDOWN)
        return

    query = " ".join(context.args).strip()
    if not query:
        await update.message.reply_text(
            "Tell me which audiobook you're looking for. Example:\n`/audiobook Dracula`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    status_msg = await update.message.reply_text(f"🎧 Searching for \"{query}\"...")

    async with httpx.AsyncClient() as client:
        try:
            results = await search_librivox(client, query)
        except httpx.HTTPError as e:
            logger.warning("LibriVox search failed: %s", e)
            results = []

    if not results:
        await status_msg.edit_text(
            "No audiobooks found. Try a different title, or drop words like \"The\"/\"A\" from the start."
        )
        return

    search_id = register_search_results(results)
    text, keyboard = render_results_list(search_id, results, 0)
    await status_msg.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def popularaudio(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_private_chat(update):
        return
    if not await is_unlocked(update.effective_chat.id, "audiobooks"):
        await update.message.reply_text(LOCKED_MESSAGES["audiobooks"], parse_mode=ParseMode.MARKDOWN)
        return

    async with httpx.AsyncClient() as client:
        fetched = await asyncio.gather(
            *(audiobook_id_to_result(client, lv_id) for lv_id in POPULAR_AUDIOBOOKS),
            return_exceptions=True,
        )
    results = [b for b in fetched if isinstance(b, dict)]
    if not results:
        await update.message.reply_text("Couldn't load the popular audiobooks right now — try again shortly.")
        return

    search_id = register_search_results(results)
    text, keyboard = render_results_list(search_id, results, 0)
    await update.message.reply_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def on_list_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, search_id, page_str = query.data.split(":", 2)
    results = SEARCH_CACHE.get(search_id)
    if results is None:
        await query.edit_message_text("This search has expired, please search again.", reply_markup=None)
        return
    text, keyboard = render_results_list(search_id, results, int(page_str))
    await edit_ignoring_unchanged(query, text, keyboard)


async def on_pick_result(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, search_id, index_str = query.data.split(":", 2)
    results = SEARCH_CACHE.get(search_id)
    if results is None:
        await query.edit_message_text("This search has expired, please search again.", reply_markup=None)
        return

    index = int(index_str)
    page = index // PAGE_SIZE
    book = results[index]
    if book["source"] == "librivox":
        audiobook_id = register_audiobook(book)
        text, keyboard = render_chapter_list(
            book, audiobook_id, 0, back_callback_data=f"page:{search_id}:{page}"
        )
    else:
        text, keyboard = render_book_detail(book, back_callback_data=f"page:{search_id}:{page}")
    await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def on_chapter_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    _, audiobook_id, page_str = query.data.split(":", 2)
    book = AUDIOBOOK_CACHE.get(audiobook_id)
    if book is None:
        await query.edit_message_text("This audiobook has expired, please search again.", reply_markup=None)
        return
    # Re-derive the same "back to results" target this chapter list was
    # opened with, so paging through chapters never loses it.
    current_markup = query.message.reply_markup
    back_button = current_markup.inline_keyboard[-1][0] if current_markup else None
    back_callback_data = back_button.callback_data if back_button else "noop"
    text, keyboard = render_chapter_list(book, audiobook_id, int(page_str), back_callback_data)
    await edit_ignoring_unchanged(query, text, keyboard)


async def on_archive_format_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    short_id = query.data.split(":", 1)[1]
    entry = ARCHIVE_ITEM_CACHE.get(short_id)
    if not entry:
        await query.edit_message_text("This search has expired, please search again.", reply_markup=None)
        return
    identifier, title, back_callback_data = entry

    async with httpx.AsyncClient() as client:
        try:
            formats = await resolve_archive_formats(client, identifier)
        except httpx.HTTPError as e:
            logger.warning("Archive metadata failed: %s", e)
            formats = []

    back_row = [InlineKeyboardButton("🔙 Back to results", callback_data=back_callback_data)]
    if not formats:
        await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([back_row]))
        await query.message.reply_text("No direct downloadable format available for this item.")
        return

    buttons = []
    for label, url, ext in formats:
        filename = safe_filename(title, ext)
        dl_id = register_download(url, filename)
        buttons.append(InlineKeyboardButton(f"⬇️ {label}", callback_data=f"dl:{dl_id}"))
    await query.edit_message_reply_markup(reply_markup=InlineKeyboardMarkup([buttons, back_row]))


async def on_download_request(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    short_id = query.data.split(":", 1)[1]
    entry = DOWNLOAD_CACHE.get(short_id)

    if not entry:
        await query.answer("This button has expired, please search for the book again.", show_alert=True)
        return

    url, filename = entry
    await query.answer("Downloading...")

    if DOWNLOAD_SEMAPHORE.locked():
        await query.message.reply_text("Lots of downloads in progress, waiting for a free slot...")

    async with DOWNLOAD_SEMAPHORE:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            try:
                async with client.stream("GET", url, timeout=60) as resp:
                    resp.raise_for_status()
                    content_length = resp.headers.get("content-length")
                    if content_length and int(content_length) > MAX_TELEGRAM_FILE_BYTES:
                        await query.message.reply_text(
                            f"This file is too large to send through Telegram. Download it directly:\n{url}"
                        )
                        return
                    chunks = bytearray()
                    async for chunk in resp.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > MAX_TELEGRAM_FILE_BYTES:
                            await query.message.reply_text(
                                f"This file is too large to send through Telegram. Download it directly:\n{url}"
                            )
                            return
            except httpx.HTTPError as e:
                logger.warning("Download failed for %s: %s", url, e)
                await query.message.reply_text(
                    f"Couldn't download the file. Try it directly from the source:\n{url}"
                )
                return

        payload = bytes(chunks)
        if filename.lower().endswith(".mp3"):
            # Gives audiobook chapters Telegram's native audio player
            # (play/pause, scrub bar) instead of a generic file icon.
            await query.message.reply_audio(audio=InputFile(payload, filename=filename))
        else:
            await query.message.reply_document(document=InputFile(payload, filename=filename))


def build_application() -> Application:
    token = os.environ["BOT_TOKEN"]
    # Without this, python-telegram-bot handles one update at a time: while
    # user A's /search or download is being processed, users B, C, D... all
    # queue up and wait. This lets up to 64 updates run concurrently.
    application = Application.builder().token(token).concurrent_updates(64).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("popular", popular))
    application.add_handler(CommandHandler("audiobook", audiobook))
    application.add_handler(CommandHandler("popularaudio", popularaudio))
    application.add_handler(CommandHandler("unlock", unlock))
    application.add_handler(CommandHandler("reset", reset))
    application.add_handler(CallbackQueryHandler(on_archive_format_request, pattern=r"^ia:"))
    application.add_handler(CallbackQueryHandler(on_download_request, pattern=r"^dl:"))
    application.add_handler(CallbackQueryHandler(on_list_page, pattern=r"^page:"))
    application.add_handler(CallbackQueryHandler(on_pick_result, pattern=r"^pick:"))
    application.add_handler(CallbackQueryHandler(on_chapter_page, pattern=r"^chpage:"))
    application.add_handler(CallbackQueryHandler(on_reset_button, pattern=r"^reset$"))
    # Catches any plain-text message that isn't a recognized command, so
    # typing a book title directly (no /search) is what actually triggers
    # a search. Registered last so it never shadows the commands above.
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_search_text))

    return application


def main() -> None:
    application = build_application()
    webhook_url = os.environ.get("WEBHOOK_URL")

    if webhook_url:
        port = int(os.environ.get("PORT", "10000"))
        logger.info("Starting in webhook mode on port %s", port)
        application.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path="webhook",
            webhook_url=f"{webhook_url.rstrip('/')}/webhook",
        )
    else:
        logger.info("Starting in polling mode")
        application.run_polling()


if __name__ == "__main__":
    main()

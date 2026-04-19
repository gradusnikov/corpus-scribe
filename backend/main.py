"""Send-to-Scribe backend: extract articles, generate PDF/Markdown, optionally send to Kindle."""

import json
import logging
import os
import queue as _queue
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from flask_cors import CORS
from pathvalidate import sanitize_filename
import pypandoc

from article_extractor import (
    _build_frontmatter,
    _generate_companion_notes,
    _notes_doc_id,
    _normalize_latex_delimiters_in_markdown,
    _strip_pdf_thematic_breaks,
    _strip_noise_from_markdown,
    extract_article,
    extract_url,
    extract_pdf_bytes,
    extract_pdf_url,
    regenerate_reading_pdf,
)
from send_email_gmail import send_to_kindle

app = Flask(__name__)
CORS(app, support_credentials=True)
logging.basicConfig(level=logging.DEBUG)

API_KEY = os.environ.get("API_KEY", "api-key-1234")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
TEMP_DIR = "/tmp/scribe"
DESKTOP_API_ROOT = os.environ.get("DESKTOP_API_ROOT", OUTPUT_DIR)
HOST_OUTPUT_DIR_NATIVE = os.environ.get("HOST_OUTPUT_DIR_NATIVE", "").strip()
HOST_OUTPUT_DIR = os.environ.get("HOST_OUTPUT_DIR", "").strip()


def _pdf_ocr_available() -> bool:
    return bool(os.environ.get("MISTRAL_API_KEY", "").strip())


def _check_api_key(data: dict) -> bool:
    return data.get("apiKey") == API_KEY


class _EventHub:
    # Thread-safe fan-out for desktop SSE subscribers. Each subscriber gets
    # its own bounded queue; slow consumers get dropped events rather than
    # blocking writer threads.
    def __init__(self) -> None:
        self._subscribers: list[_queue.Queue] = []
        self._lock = threading.Lock()

    def subscribe(self) -> _queue.Queue:
        q: _queue.Queue = _queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        return q

    def unsubscribe(self, q: _queue.Queue) -> None:
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    def publish(self, event: dict) -> None:
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait(event)
            except _queue.Full:
                pass


_event_hub = _EventHub()


def _publish_desktop_event(event_type: str, article_path=None, **extra) -> None:
    payload: dict = {
        "type": event_type,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    if article_path is not None:
        payload["articlePath"] = str(article_path)
    payload.update(extra)
    _event_hub.publish(payload)


def _clean_label(label: str) -> str:
    """Normalize a user-provided label into a safe directory name."""
    safe_label = sanitize_filename((label or "").strip())
    return " ".join(safe_label.split())


def _resolve_output_dir(label: str) -> Path:
    """Return the per-label output directory, preserving legacy root saves if unlabeled."""
    base_dir = Path(OUTPUT_DIR)
    safe_label = _clean_label(label)
    if not safe_label:
        return base_dir
    return base_dir / safe_label


def _list_existing_labels() -> list[str]:
    """List label directories currently present under the output root."""
    base_dir = Path(OUTPUT_DIR)
    if not base_dir.exists():
        return []
    return sorted(
        [entry.name for entry in base_dir.iterdir() if entry.is_dir()],
        key=str.casefold,
    )


def _index_file_path() -> Path:
    return Path(OUTPUT_DIR) / "index.jsonl"


def _resolve_library_root(root: str | None) -> Path:
    value = (root or "").strip()
    if not value:
        return Path(DESKTOP_API_ROOT)
    candidate = Path(value).expanduser()
    resolved = _translate_host_library_root_to_container(str(candidate))
    return Path(resolved).expanduser()


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    try:
        _, rest = text.split("---\n", 1)
        frontmatter_text, body = rest.split("\n---\n", 1)
    except ValueError:
        return {}, text

    data: dict[str, object] = {}
    for raw_line in frontmatter_text.splitlines():
        line = raw_line.strip()
        if not line or ":" not in line:
            continue
        key, raw_value = line.split(":", 1)
        key = key.strip()
        value = raw_value.strip()
        if not value:
            continue
        if value in {"true", "false"}:
            data[key] = value == "true"
            continue
        try:
            data[key] = json.loads(value)
            continue
        except Exception:
            pass
        try:
            data[key] = int(value)
            continue
        except Exception:
            pass
        data[key] = value
    return data, body


def _frontmatter_string(frontmatter: dict, key: str) -> str | None:
    value = frontmatter.get(key)
    return value if isinstance(value, str) and value.strip() else None


def _frontmatter_int(frontmatter: dict, key: str) -> int | None:
    value = frontmatter.get(key)
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _coerce_rating(value) -> int:
    if isinstance(value, bool):
        raise ValueError("Rating must be an integer between 0 and 5")
    if isinstance(value, int):
        rating = value
    elif isinstance(value, float) and value.is_integer():
        rating = int(value)
    elif isinstance(value, str) and value.strip():
        try:
            rating = int(value.strip())
        except ValueError as error:
            raise ValueError("Rating must be an integer between 0 and 5") from error
    else:
        raise ValueError("Rating must be an integer between 0 and 5")
    if rating < 0 or rating > 5:
        raise ValueError("Rating must be an integer between 0 and 5")
    return rating


def _excerpt_from_markdown(body: str, limit: int = 220) -> str:
    cleaned = re.sub(r"```.*?```", " ", body, flags=re.S)
    cleaned = re.sub(r"\$\$.*?\$\$", " ", cleaned, flags=re.S)
    cleaned = re.sub(r"`[^`]+`", " ", cleaned)
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"\[[^\]]+\]\([^)]+\)", " ", cleaned)
    cleaned = re.sub(r"<[^>]+>", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: limit - 1].rstrip() + "…"


def _sibling_if_exists(path: Path, suffix: str) -> Path | None:
    candidate = path.with_suffix(f".{suffix}")
    return candidate if candidate.exists() else None


def _sibling_with_suffix_if_exists(path: Path, suffix: str) -> Path | None:
    candidate = path.with_name(path.stem + suffix)
    return candidate if candidate.exists() else None


def _highlights_path(path: Path) -> Path:
    return path.with_name(path.stem + ".highlights.json")


def _load_highlights(path: Path) -> list[dict]:
    highlights_path = _highlights_path(path)
    if not highlights_path.exists():
        return []
    try:
        payload = json.loads(highlights_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    highlights = payload.get("highlights")
    if not isinstance(highlights, list):
        return []
    cleaned: list[dict] = []
    for item in highlights:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        element_type = item.get("elementType")
        element_index = item.get("elementIndex")
        is_element = kind == "element" or (
            isinstance(element_type, str) and isinstance(element_index, int)
        )
        text = item.get("text")
        if not is_element:
            if not isinstance(text, str) or not text.strip():
                continue
        highlight_id = item.get("id")
        created_at = item.get("createdAt")
        start_offset = item.get("startOffset")
        end_offset = item.get("endOffset")
        comment = item.get("comment")
        variant = item.get("variant")
        cleaned_item = {
            "id": str(highlight_id).strip() if highlight_id is not None else "",
            "text": text.strip() if isinstance(text, str) else "",
            "createdAt": str(created_at).strip() if created_at is not None else "",
        }
        if isinstance(start_offset, int) and isinstance(end_offset, int) and end_offset > start_offset:
            cleaned_item["startOffset"] = start_offset
            cleaned_item["endOffset"] = end_offset
        if isinstance(comment, str) and comment.strip():
            cleaned_item["comment"] = comment.strip()
        if isinstance(variant, str) and variant.strip():
            cleaned_item["variant"] = variant.strip()
        if is_element:
            cleaned_item["kind"] = "element"
            if isinstance(element_type, str) and element_type.strip():
                cleaned_item["elementType"] = element_type.strip()
            if isinstance(element_index, int) and element_index >= 0:
                cleaned_item["elementIndex"] = element_index
        cleaned.append(cleaned_item)
    return cleaned


def _write_highlights(path: Path, highlights: list[dict]) -> Path:
    highlights_path = _highlights_path(path)
    payload = {
        "articlePath": str(path),
        "highlights": highlights,
    }
    highlights_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return highlights_path


def _related_path(path: Path) -> Path:
    return path.with_name(path.stem + ".related.json")


def _load_related(path: Path) -> list[dict]:
    related_path = _related_path(path)
    if not related_path.exists():
        return []
    try:
        payload = json.loads(related_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return []
    cleaned: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        target_path = item.get("targetPath")
        if not isinstance(target_path, str) or not target_path.strip():
            continue
        cleaned_item = {
            "id": str(item.get("id") or "").strip(),
            "targetPath": target_path.strip(),
            "targetTitle": str(item.get("targetTitle") or "").strip(),
            "note": str(item.get("note") or "").strip(),
            "createdAt": str(item.get("createdAt") or "").strip(),
        }
        cleaned.append(cleaned_item)
    return cleaned


def _write_related(path: Path, items: list[dict]) -> Path:
    related_path = _related_path(path)
    if items:
        payload = {"articlePath": str(path), "items": items}
        related_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
    elif related_path.exists():
        related_path.unlink()
    return related_path


def _stable_doc_id(path: Path) -> str:
    return path.as_posix().lower()


def _frontmatter_text_field(frontmatter: dict, key: str) -> str | None:
    value = frontmatter.get(key)
    if isinstance(value, str) and value.strip():
        return value.strip()
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    if isinstance(value, list):
        parts = [
            item.strip() if isinstance(item, str) else str(item)
            for item in value
            if item not in (None, "")
        ]
        joined = ", ".join(part for part in parts if part)
        return joined or None
    return None


_FRONTMATTER_SEARCH_KEYS: tuple[str, ...] = (
    "authors",
    "author",
    "doi",
    "arxiv_id",
    "pmid",
    "pmcid",
    "year",
    "published",
    "date",
    "ingested_at",
    "publisher",
    "journal",
)


def _scan_library_documents(root_path: Path) -> list[dict]:
    documents: list[dict] = []
    for path in root_path.rglob("*.md"):
        if path.name.endswith(".notes.md"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        frontmatter, body = _split_frontmatter(content)
        title = _frontmatter_string(frontmatter, "title") or path.stem
        label = _frontmatter_string(frontmatter, "label")
        bib_path = _sibling_if_exists(path, "bib")
        notes_path = _sibling_with_suffix_if_exists(path, ".notes.md")
        highlights = _load_highlights(path)
        highlights_path = _highlights_path(path)
        reading_pdf_path = _sibling_with_suffix_if_exists(path, ".reading.pdf") or _sibling_if_exists(path, "pdf")
        source_pdf_path = _sibling_with_suffix_if_exists(path, ".source.pdf")
        rating_value = _frontmatter_int(frontmatter, "rating")
        rating = max(0, min(5, rating_value)) if isinstance(rating_value, int) else 0
        metadata_text: dict[str, str] = {}
        for key in _FRONTMATTER_SEARCH_KEYS:
            value = _frontmatter_text_field(frontmatter, key)
            if value:
                metadata_text[key] = value
        documents.append(
            {
                "id": _stable_doc_id(path),
                "title": title,
                "label": label,
                "articlePath": str(path),
                "notesPath": str(notes_path) if notes_path else None,
                "notesPending": _notes_generation_pending(path),
                "bibPath": str(bib_path) if bib_path else None,
                "highlightsPath": str(highlights_path) if highlights_path.exists() else None,
                "highlightCount": len(highlights),
                "readingPdfPath": str(reading_pdf_path) if reading_pdf_path else None,
                "sourcePdfPath": str(source_pdf_path) if source_pdf_path else None,
                "sourceSite": _frontmatter_string(frontmatter, "source_site"),
                "ingestedAt": _frontmatter_string(frontmatter, "ingested_at"),
                "rating": rating,
                "url": _frontmatter_string(frontmatter, "url"),
                "canonicalUrl": _frontmatter_string(frontmatter, "canonical_url"),
                "authors": metadata_text.get("authors") or metadata_text.get("author"),
                "doi": metadata_text.get("doi"),
                "year": metadata_text.get("year"),
                "arxivId": metadata_text.get("arxiv_id"),
                "pmid": metadata_text.get("pmid"),
                "pmcid": metadata_text.get("pmcid"),
                "metadataText": metadata_text,
                "excerpt": _excerpt_from_markdown(body),
            }
        )
    documents.sort(
        key=lambda item: (
            int(item.get("rating") or 0),
            item.get("ingestedAt") or "",
            item.get("title", "").casefold(),
        ),
        reverse=True,
    )
    return documents


def _read_markdown_body(path: Path) -> str:
    content = path.read_text(encoding="utf-8")
    _, body = _split_frontmatter(content)
    return body


def _search_db_path(root: Path) -> Path:
    return root / ".corpus-scribe-search.sqlite3"


def _open_search_db(root: Path) -> sqlite3.Connection:
    root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(_search_db_path(root))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA temp_store=MEMORY")
    return conn


def _search_schema_is_current(conn: sqlite3.Connection) -> bool:
    documents_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'documents'"
    ).fetchone()
    fts_sql_row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'documents_fts'"
    ).fetchone()
    if not documents_sql_row and not fts_sql_row:
        return True
    if not documents_sql_row or not fts_sql_row:
        return False

    documents_sql = (documents_sql_row[0] or "").lower()
    fts_sql = (fts_sql_row[0] or "").lower()
    required_documents = (
        "doc_key text",
        "article_path text",
        "authors text",
        "highlights_mtime_ns integer",
    )
    required_fts = (
        "author",
        "doi",
        "body",
        "titleabstract",
        "highlight",
        "publisher",
        "pmcid",
        "arxiv",
    )
    return all(token in documents_sql for token in required_documents) and all(
        token in fts_sql for token in required_fts
    )


def _reset_search_schema(conn: sqlite3.Connection) -> None:
    conn.execute("DROP TABLE IF EXISTS documents_fts")
    conn.execute("DROP TABLE IF EXISTS documents")
    conn.commit()


def _ensure_search_schema(conn: sqlite3.Connection) -> None:
    if not _search_schema_is_current(conn):
        _reset_search_schema(conn)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            doc_key TEXT NOT NULL,
            article_path TEXT NOT NULL UNIQUE,
            title TEXT NOT NULL,
            label TEXT,
            notes_path TEXT,
            bib_path TEXT,
            highlights_path TEXT,
            highlight_count INTEGER NOT NULL DEFAULT 0,
            reading_pdf_path TEXT,
            source_pdf_path TEXT,
            source_site TEXT,
            ingested_at TEXT,
            rating INTEGER NOT NULL DEFAULT 0,
            url TEXT,
            canonical_url TEXT,
            excerpt TEXT,
            authors TEXT,
            doi TEXT,
            year TEXT,
            published TEXT,
            date_text TEXT,
            arxiv_id TEXT,
            pmid TEXT,
            pmcid TEXT,
            journal TEXT,
            publisher TEXT,
            article_mtime_ns INTEGER NOT NULL,
            notes_mtime_ns INTEGER NOT NULL DEFAULT 0,
            highlights_mtime_ns INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    conn.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
            title,
            author,
            doi,
            year,
            body,
            titleabstract,
            notes,
            highlight,
            label,
            journal,
            publisher,
            pmid,
            pmcid,
            arxiv,
            url,
            tokenize='unicode61'
        )
        """
    )


def _path_mtime_ns(path: Path | None) -> int:
    if not path or not path.exists():
        return 0
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return 0


def _build_search_document(path: Path) -> dict:
    content = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(content)
    title = _frontmatter_string(frontmatter, "title") or path.stem
    label = _frontmatter_string(frontmatter, "label")
    notes_path = _sibling_with_suffix_if_exists(path, ".notes.md")
    bib_path = _sibling_if_exists(path, "bib")
    highlights = _load_highlights(path)
    highlights_path = _highlights_path(path)
    reading_pdf_path = _sibling_with_suffix_if_exists(path, ".reading.pdf") or _sibling_if_exists(path, "pdf")
    source_pdf_path = _sibling_with_suffix_if_exists(path, ".source.pdf")
    rating_value = _frontmatter_int(frontmatter, "rating")
    rating = max(0, min(5, rating_value)) if isinstance(rating_value, int) else 0

    metadata_text: dict[str, str] = {}
    for key in _FRONTMATTER_SEARCH_KEYS:
        value = _frontmatter_text_field(frontmatter, key)
        if value:
            metadata_text[key] = value

    notes_body = ""
    if notes_path:
        try:
            notes_body = _read_markdown_body(notes_path)
        except Exception:
            notes_body = ""
    highlight_text = "\n".join(item.get("text", "") for item in highlights if item.get("text"))

    authors = metadata_text.get("authors") or metadata_text.get("author") or ""
    doi = metadata_text.get("doi") or ""
    arxiv_id = metadata_text.get("arxiv_id") or ""
    pmid = metadata_text.get("pmid") or ""
    pmcid = metadata_text.get("pmcid") or ""
    journal = metadata_text.get("journal") or ""
    publisher = metadata_text.get("publisher") or ""
    source_site = _frontmatter_string(frontmatter, "source_site")
    url = _frontmatter_string(frontmatter, "url")
    canonical_url = _frontmatter_string(frontmatter, "canonical_url")
    ingested_at = _frontmatter_string(frontmatter, "ingested_at")
    year_text = " ".join(
        value
        for value in (
            metadata_text.get("year"),
            metadata_text.get("published"),
            metadata_text.get("date"),
            ingested_at,
        )
        if value
    )
    url_text = "\n".join(value for value in (url, canonical_url, source_site) if value)

    return {
        "summary": {
            "id": _stable_doc_id(path),
            "title": title,
            "label": label,
            "articlePath": str(path),
            "notesPath": str(notes_path) if notes_path else None,
            "bibPath": str(bib_path) if bib_path else None,
            "highlightsPath": str(highlights_path) if highlights_path.exists() else None,
            "highlightCount": len(highlights),
            "readingPdfPath": str(reading_pdf_path) if reading_pdf_path else None,
            "sourcePdfPath": str(source_pdf_path) if source_pdf_path else None,
            "sourceSite": source_site,
            "ingestedAt": ingested_at,
            "rating": rating,
            "url": url,
            "canonicalUrl": canonical_url,
            "authors": authors or None,
            "doi": doi or None,
            "year": metadata_text.get("year"),
            "arxivId": arxiv_id or None,
            "pmid": pmid or None,
            "pmcid": pmcid or None,
            "excerpt": _excerpt_from_markdown(body),
        },
        "search": {
            "title": title,
            "author": authors,
            "doi": doi,
            "year": year_text,
            "body": body,
            "titleabstract": f"{title}\n\n{body}".strip(),
            "notes": notes_body,
            "highlight": highlight_text,
            "label": label or "",
            "journal": journal,
            "publisher": publisher,
            "pmid": pmid,
            "pmcid": pmcid,
            "arxiv": arxiv_id,
            "url": url_text,
            "published": metadata_text.get("published") or "",
            "date_text": metadata_text.get("date") or "",
        },
        "mtimes": {
            "article_mtime_ns": _path_mtime_ns(path),
            "notes_mtime_ns": _path_mtime_ns(notes_path),
            "highlights_mtime_ns": _path_mtime_ns(highlights_path if highlights_path.exists() else None),
        },
    }


def _upsert_search_document(conn: sqlite3.Connection, document: dict) -> None:
    summary = document["summary"]
    search = document["search"]
    mtimes = document["mtimes"]
    conn.execute(
        """
        INSERT INTO documents (
            doc_key,
            article_path,
            title,
            label,
            notes_path,
            bib_path,
            highlights_path,
            highlight_count,
            reading_pdf_path,
            source_pdf_path,
            source_site,
            ingested_at,
            rating,
            url,
            canonical_url,
            excerpt,
            authors,
            doi,
            year,
            published,
            date_text,
            arxiv_id,
            pmid,
            pmcid,
            journal,
            publisher,
            article_mtime_ns,
            notes_mtime_ns,
            highlights_mtime_ns
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(article_path) DO UPDATE SET
            doc_key = excluded.doc_key,
            title = excluded.title,
            label = excluded.label,
            notes_path = excluded.notes_path,
            bib_path = excluded.bib_path,
            highlights_path = excluded.highlights_path,
            highlight_count = excluded.highlight_count,
            reading_pdf_path = excluded.reading_pdf_path,
            source_pdf_path = excluded.source_pdf_path,
            source_site = excluded.source_site,
            ingested_at = excluded.ingested_at,
            rating = excluded.rating,
            url = excluded.url,
            canonical_url = excluded.canonical_url,
            excerpt = excluded.excerpt,
            authors = excluded.authors,
            doi = excluded.doi,
            year = excluded.year,
            published = excluded.published,
            date_text = excluded.date_text,
            arxiv_id = excluded.arxiv_id,
            pmid = excluded.pmid,
            pmcid = excluded.pmcid,
            journal = excluded.journal,
            publisher = excluded.publisher,
            article_mtime_ns = excluded.article_mtime_ns,
            notes_mtime_ns = excluded.notes_mtime_ns,
            highlights_mtime_ns = excluded.highlights_mtime_ns
        """,
        (
            summary["id"],
            summary["articlePath"],
            summary["title"],
            summary["label"],
            summary["notesPath"],
            summary["bibPath"],
            summary["highlightsPath"],
            summary["highlightCount"],
            summary["readingPdfPath"],
            summary["sourcePdfPath"],
            summary["sourceSite"],
            summary["ingestedAt"],
            summary["rating"],
            summary["url"],
            summary["canonicalUrl"],
            summary["excerpt"],
            summary["authors"],
            summary["doi"],
            summary["year"],
            search["published"],
            search["date_text"],
            summary["arxivId"],
            summary["pmid"],
            summary["pmcid"],
            search["journal"],
            search["publisher"],
            mtimes["article_mtime_ns"],
            mtimes["notes_mtime_ns"],
            mtimes["highlights_mtime_ns"],
        ),
    )
    row_id = conn.execute(
        "SELECT id FROM documents WHERE article_path = ?",
        (summary["articlePath"],),
    ).fetchone()[0]
    conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (row_id,))
    conn.execute(
        """
        INSERT INTO documents_fts (
            rowid,
            title,
            author,
            doi,
            year,
            body,
            titleabstract,
            notes,
            highlight,
            label,
            journal,
            publisher,
            pmid,
            pmcid,
            arxiv,
            url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            row_id,
            search["title"],
            search["author"],
            search["doi"],
            search["year"],
            search["body"],
            search["titleabstract"],
            search["notes"],
            search["highlight"],
            search["label"],
            search["journal"],
            search["publisher"],
            search["pmid"],
            search["pmcid"],
            search["arxiv"],
            search["url"],
        ),
    )


def _sync_search_index(root: Path) -> dict[str, int | str]:
    conn = _open_search_db(root)
    try:
        _ensure_search_schema(conn)
        fts_row_ids = {
            row[0]
            for row in conn.execute("SELECT rowid FROM documents_fts").fetchall()
        }
        existing_rows = conn.execute(
            "SELECT id, article_path, article_mtime_ns, notes_mtime_ns, highlights_mtime_ns FROM documents"
        ).fetchall()
        existing = {
            row["article_path"]: (
                row["id"],
                row["article_mtime_ns"],
                row["notes_mtime_ns"],
                row["highlights_mtime_ns"],
            )
            for row in existing_rows
        }

        seen: set[str] = set()
        indexed = 0
        updated = 0
        errors = 0
        for path in root.rglob("*.md"):
            if path.name.endswith(".notes.md"):
                continue
            article_path = str(path)
            notes_path = _sibling_with_suffix_if_exists(path, ".notes.md")
            highlights_path = _highlights_path(path)
            current_mtimes = (
                _path_mtime_ns(path),
                _path_mtime_ns(notes_path),
                _path_mtime_ns(highlights_path if highlights_path.exists() else None),
            )
            seen.add(article_path)
            cached = existing.get(article_path)
            if cached and cached[1:] == current_mtimes and cached[0] in fts_row_ids:
                indexed += 1
                continue
            try:
                document = _build_search_document(path)
                _upsert_search_document(conn, document)
                indexed += 1
                updated += 1
            except Exception as exc:
                app.logger.warning("Search index sync skipped %s: %s", path, exc)
                errors += 1

        removed = 0
        for article_path, (row_id, *_rest) in existing.items():
            if article_path in seen:
                continue
            conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (row_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (row_id,))
            removed += 1

        conn.commit()
        return {
            "indexed": indexed,
            "updated": updated,
            "removed": removed,
            "errors": errors,
            "dbPath": str(_search_db_path(root)),
        }
    finally:
        conn.close()


def _remove_search_records_for_article(article_path: Path) -> int:
    removed = 0
    output_root = Path(OUTPUT_DIR)
    candidate_roots: list[Path] = []
    try:
        current = article_path.parent.resolve()
        output_resolved = output_root.resolve()
    except Exception:
        current = article_path.parent
        output_resolved = output_root

    while True:
        candidate_roots.append(current)
        if current == output_resolved or current.parent == current:
            break
        current = current.parent

    seen_roots: set[str] = set()
    for root in candidate_roots:
        key = str(root)
        if key in seen_roots:
            continue
        seen_roots.add(key)
        db_path = _search_db_path(root)
        if not db_path.exists():
            continue
        conn = _open_search_db(root)
        try:
            _ensure_search_schema(conn)
            row = conn.execute(
                "SELECT id FROM documents WHERE article_path = ?",
                (str(article_path),),
            ).fetchone()
            if not row:
                continue
            row_id = row["id"]
            conn.execute("DELETE FROM documents_fts WHERE rowid = ?", (row_id,))
            conn.execute("DELETE FROM documents WHERE id = ?", (row_id,))
            conn.commit()
            removed += 1
        finally:
            conn.close()
    return removed


def _relative_to_output(path: str | None) -> str | None:
    if not path:
        return None
    try:
        return str(Path(path).relative_to(OUTPUT_DIR))
    except Exception:
        return path


def _upsert_index_records(records: list[dict]) -> None:
    index_path = _index_file_path()
    existing: dict[tuple[str, str], dict] = {}
    if index_path.exists():
        for line in index_path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            record = json.loads(line)
            existing[(record["type"], record["path"])] = record

    for record in records:
        existing[(record["type"], record["path"])] = record

    index_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(existing.values(), key=lambda item: (item["type"], item["path"]))
    index_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in ordered) + "\n",
        encoding="utf-8",
    )


def _remove_index_records_for_paths(paths: set[str]) -> int:
    """Drop any index.jsonl records whose `path` matches one of the given paths."""
    index_path = _index_file_path()
    if not index_path.exists():
        return 0
    surviving: list[dict] = []
    removed = 0
    for line in index_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        if record.get("path") in paths:
            removed += 1
            continue
        surviving.append(record)
    if removed:
        ordered = sorted(surviving, key=lambda item: (item.get("type", ""), item.get("path", "")))
        content = "\n".join(json.dumps(item, ensure_ascii=False) for item in ordered)
        index_path.write_text(content + ("\n" if content else ""), encoding="utf-8")
    return removed


def _delete_article_bundle(article_path: Path) -> dict:
    """Remove an article and all sibling artefacts that belong to the same bundle.

    Deletes the article's parent directory when it contains nothing but the
    article's sibling files (the canonical "bundle" layout). Otherwise only the
    .md plus recognisable sibling artefacts are removed, leaving unrelated
    content alone.
    """
    import shutil

    article_path = article_path.resolve()
    article_dir = article_path.parent
    stem = article_path.stem

    sibling_names: set[str] = {
        article_path.name,
        f"{stem}.bib",
        f"{stem}.notes.md",
        f"{stem}.reading.pdf",
        f"{stem}.source.pdf",
        f"{stem}.pdf",
        f"{stem}.highlights.json",
        f"{stem}.html",
        f"{stem}.raw.html",
        f"{stem}.normalized.html",
        "ocr_response.json",
    }

    removed_files: list[str] = []
    removed_dirs: list[str] = []

    dir_entries = [entry for entry in article_dir.iterdir()] if article_dir.exists() else []
    non_sibling_entries = [
        entry
        for entry in dir_entries
        if entry.name not in sibling_names and entry.name != "assets"
    ]

    if article_dir.exists() and not non_sibling_entries:
        shutil.rmtree(article_dir)
        removed_dirs.append(str(article_dir))
    else:
        for name in sibling_names:
            target = article_dir / name
            if target.exists() and target.is_file():
                target.unlink()
                removed_files.append(str(target))
        assets_dir = article_dir / "assets"
        if assets_dir.exists() and assets_dir.is_dir():
            shutil.rmtree(assets_dir)
            removed_dirs.append(str(assets_dir))

    relative_paths: set[str] = set()
    for path in (article_path, article_path.with_name(f"{stem}.notes.md")):
        rel = _relative_to_output(str(path))
        if rel:
            relative_paths.add(rel)
    removed_index = _remove_index_records_for_paths(relative_paths)
    removed_search = _remove_search_records_for_article(article_path)

    return {
        "removedFiles": removed_files,
        "removedDirs": removed_dirs,
        "removedIndexRecords": removed_index,
        "removedSearchRecords": removed_search,
    }


def _build_index_records(metadata: dict, label: str) -> list[dict]:
    article_record = {
        "doc_id": metadata["metadata"].get("doc_id"),
        "type": "article",
        "title": metadata["title"],
        "label": label or None,
        "path": _relative_to_output(metadata["md"]),
        "pdf_path": _relative_to_output(metadata.get("pdf")),
        "source_pdf_path": _relative_to_output(metadata.get("sourcePdf")),
        "bib_path": _relative_to_output(metadata.get("bib")),
        "notes_path": _relative_to_output(metadata.get("notes")),
        "url": metadata["metadata"].get("url"),
        "canonical_url": metadata["metadata"].get("canonical_url"),
        "source_site": metadata["metadata"].get("source_site"),
        "source_format": metadata["metadata"].get("source_format"),
        "ocr_engine": metadata["metadata"].get("ocr_engine"),
        "citation_key": metadata["metadata"].get("citation_key"),
        "doi": metadata["metadata"].get("doi"),
        "arxiv_id": metadata["metadata"].get("arxiv_id"),
        "page_count": metadata["metadata"].get("page_count"),
        "language": metadata["metadata"].get("language"),
        "word_count": metadata["metadata"].get("word_count"),
        "image_count": metadata["metadata"].get("image_count"),
        "ingested_at": metadata["metadata"].get("ingested_at"),
        "rating": metadata["metadata"].get("rating") or 0,
    }
    records = [article_record]

    if metadata.get("notes"):
        records.append(
            {
                "doc_id": metadata.get("notesDocId"),
                "type": "notes",
                "title": f"{metadata['title']} Notes",
                "label": label or None,
                "path": _relative_to_output(metadata["notes"]),
                "source_article_path": _relative_to_output(metadata["md"]),
                "source_doc_id": metadata["metadata"].get("doc_id"),
                "url": metadata["metadata"].get("url"),
                "canonical_url": metadata["metadata"].get("canonical_url"),
                "source_site": metadata["metadata"].get("source_site"),
                "source_format": metadata["metadata"].get("source_format"),
                "ocr_engine": metadata["metadata"].get("ocr_engine"),
                "citation_key": metadata["metadata"].get("citation_key"),
                "language": metadata["metadata"].get("language"),
                "ingested_at": metadata["metadata"].get("ingested_at"),
            }
        )
    return records


def _write_article_frontmatter(path: Path, frontmatter: dict, body: str) -> None:
    text = _build_frontmatter(frontmatter) + "\n\n" + body.strip() + "\n"
    path.write_text(text, encoding="utf-8")


def _notes_metadata_from_article(article_frontmatter: dict, article_path: Path) -> dict:
    title = _frontmatter_string(article_frontmatter, "title") or article_path.stem
    source_doc_id = _frontmatter_string(article_frontmatter, "doc_id")
    return {
        "title": f"{title} Notes",
        "doc_id": _notes_doc_id(source_doc_id) if source_doc_id else None,
        "type": "companion_notes",
        "doc_type": "notes",
        "source_article": article_path.name,
        "source_doc_id": source_doc_id,
        "url": _frontmatter_string(article_frontmatter, "url"),
        "canonical_url": _frontmatter_string(article_frontmatter, "canonical_url"),
        "source_site": _frontmatter_string(article_frontmatter, "source_site"),
        "label": _frontmatter_string(article_frontmatter, "label"),
        "language": _frontmatter_string(article_frontmatter, "language"),
        "generated_by": _frontmatter_string(article_frontmatter, "generated_by"),
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "ingested_at": _frontmatter_string(article_frontmatter, "ingested_at"),
    }


def _notes_pending_path(article_path: Path) -> Path:
    return article_path.with_name(article_path.stem + ".notes.pending")


def _notes_status_path(article_path: Path) -> Path:
    return article_path.with_name(article_path.stem + ".notes.status.json")


def _notes_cancel_path(article_path: Path) -> Path:
    return article_path.with_name(article_path.stem + ".notes.cancel")


def _notes_generation_pending(article_path: Path) -> bool:
    return _notes_pending_path(article_path).exists()


def _mark_notes_generation_pending(article_path: Path) -> None:
    _notes_cancel_path(article_path).unlink(missing_ok=True)
    _notes_pending_path(article_path).write_text(
        datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        encoding="utf-8",
    )
    _write_notes_generation_status(
        article_path,
        state="running",
        notes_markdown="",
        preview_available=False,
        can_stop=True,
        message="LLM working…",
        notes_path=None,
    )


def _clear_notes_generation_pending(article_path: Path) -> None:
    _notes_pending_path(article_path).unlink(missing_ok=True)
    _notes_cancel_path(article_path).unlink(missing_ok=True)


def _write_notes_generation_status(
    article_path: Path,
    *,
    state: str,
    notes_markdown: str,
    preview_available: bool,
    can_stop: bool,
    message: str | None = None,
    notes_path: str | None = None,
    error: str | None = None,
) -> None:
    payload = {
        "state": state,
        "notesMarkdown": notes_markdown,
        "previewAvailable": preview_available,
        "canStop": can_stop,
        "message": message,
        "notesPath": notes_path,
        "error": error,
        "updatedAt": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    _notes_status_path(article_path).write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _read_notes_generation_status(article_path: Path) -> dict:
    status_path = _notes_status_path(article_path)
    if status_path.exists():
        try:
            payload = json.loads(status_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                return payload
        except Exception:
            pass
    notes_path = article_path.with_name(article_path.stem + ".notes.md")
    return {
        "state": "running" if _notes_generation_pending(article_path) else ("completed" if notes_path.exists() else "idle"),
        "notesMarkdown": _read_markdown_body(notes_path) if notes_path.exists() else "",
        "previewAvailable": notes_path.exists() and not _notes_generation_pending(article_path),
        "canStop": _notes_generation_pending(article_path),
        "message": "LLM working…" if _notes_generation_pending(article_path) else None,
        "notesPath": str(notes_path) if notes_path.exists() else None,
        "error": None,
        "updatedAt": None,
    }


def _cancel_notes_generation(article_path: Path) -> None:
    _notes_cancel_path(article_path).write_text(
        datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        encoding="utf-8",
    )


def _notes_generation_cancel_requested(article_path: Path) -> bool:
    return _notes_cancel_path(article_path).exists()


def _build_existing_article_payload(article_path: Path) -> dict:
    article_text = article_path.read_text(encoding="utf-8")
    article_frontmatter, article_body = _split_frontmatter(article_text)
    notes_path = article_path.with_name(article_path.stem + ".notes.md")
    notes_text = notes_path.read_text(encoding="utf-8") if notes_path.exists() else ""
    notes_frontmatter, _ = _split_frontmatter(notes_text) if notes_text else ({}, "")
    bib_path = _sibling_if_exists(article_path, "bib")
    reading_pdf_path = _sibling_with_suffix_if_exists(article_path, ".reading.pdf") or _sibling_if_exists(article_path, "pdf")
    source_pdf_path = _sibling_with_suffix_if_exists(article_path, ".source.pdf")

    payload = {
        "success": True,
        "title": _frontmatter_string(article_frontmatter, "title") or article_path.stem,
        "label": _frontmatter_string(article_frontmatter, "label") or "",
        "dir": str(article_path.parent),
        "pdf": str(reading_pdf_path) if reading_pdf_path else None,
        "sourcePdf": str(source_pdf_path) if source_pdf_path else None,
        "bib": str(bib_path) if bib_path else None,
        "md": str(article_path),
        "primary": str(article_path),
        "pdfAvailable": bool(reading_pdf_path),
        "sourcePdfAvailable": bool(source_pdf_path),
        "notes": str(notes_path) if notes_path.exists() else None,
        "notesAvailable": notes_path.exists(),
        "notesPending": _notes_generation_pending(article_path),
        "notesDocId": _frontmatter_string(notes_frontmatter, "doc_id") or (_notes_doc_id(_frontmatter_string(article_frontmatter, "doc_id")) if notes_path.exists() and _frontmatter_string(article_frontmatter, "doc_id") else None),
        "metadata": {
            "doc_id": _frontmatter_string(article_frontmatter, "doc_id"),
            "url": _frontmatter_string(article_frontmatter, "url"),
            "canonical_url": _frontmatter_string(article_frontmatter, "canonical_url"),
            "label": _frontmatter_string(article_frontmatter, "label"),
            "source_site": _frontmatter_string(article_frontmatter, "source_site"),
            "source_format": _frontmatter_string(article_frontmatter, "source_format"),
            "ocr_engine": _frontmatter_string(article_frontmatter, "ocr_engine"),
            "citation_key": _frontmatter_string(article_frontmatter, "citation_key"),
            "doi": _frontmatter_string(article_frontmatter, "doi"),
            "arxiv_id": _frontmatter_string(article_frontmatter, "arxiv_id"),
            "page_count": article_frontmatter.get("page_count"),
            "language": _frontmatter_string(article_frontmatter, "language"),
            "word_count": article_frontmatter.get("word_count"),
            "image_count": article_frontmatter.get("image_count"),
            "ingested_at": _frontmatter_string(article_frontmatter, "ingested_at"),
            "rating": _frontmatter_int(article_frontmatter, "rating") or 0,
        },
    }
    return payload


def _generate_existing_notes(
    article_path: Path,
    notes_config: dict | None = None,
    strategy: str = "replace",
    existing_notes_override: str | None = None,
    progress_callback=None,
    cancel_requested=None,
) -> dict:
    article_text = article_path.read_text(encoding="utf-8")
    article_frontmatter, article_body = _split_frontmatter(article_text)
    notes_path = article_path.with_name(article_path.stem + ".notes.md")
    existing_notes = existing_notes_override if existing_notes_override is not None else (_read_markdown_body(notes_path) if notes_path.exists() else "")
    notes_file = _generate_companion_notes(
        md_text=article_body,
        article_dir=article_path.parent,
        article_basename=article_path.stem,
        metadata={
            "title": _frontmatter_string(article_frontmatter, "title") or article_path.stem,
            "article_doc_id": _frontmatter_string(article_frontmatter, "doc_id") or _stable_doc_id(article_path),
            "url": _frontmatter_string(article_frontmatter, "url"),
            "canonical_url": _frontmatter_string(article_frontmatter, "canonical_url"),
            "source_site": _frontmatter_string(article_frontmatter, "source_site"),
            "label": _frontmatter_string(article_frontmatter, "label"),
            "language": _frontmatter_string(article_frontmatter, "language"),
            "ingested_at": _frontmatter_string(article_frontmatter, "ingested_at"),
            "existing_notes": existing_notes,
            "notes_strategy": strategy,
            "notes_config": notes_config or {},
        },
        progress_callback=progress_callback,
        cancel_requested=cancel_requested,
    )
    article_frontmatter["notes_file"] = notes_file.name
    if article_frontmatter.get("doc_id"):
        article_frontmatter["notes_doc_id"] = _notes_doc_id(str(article_frontmatter["doc_id"]))
    _write_article_frontmatter(article_path, article_frontmatter, article_body)

    payload = _build_existing_article_payload(article_path)
    _upsert_index_records(_build_index_records(payload, payload["label"]))
    return payload


def _spawn_async_notes_generation(
    article_path: Path,
    notes_config: dict | None = None,
    strategy: str = "replace",
    existing_notes_override: str | None = None,
) -> None:
    """Kick off companion-notes generation in a background thread."""
    _mark_notes_generation_pending(article_path)

    def _run():
        last_emit = 0.0

        def on_progress(markdown: str) -> None:
            nonlocal last_emit
            now = time.monotonic()
            if now - last_emit < 0.25 and len(markdown) < 240:
                return
            last_emit = now
            _write_notes_generation_status(
                article_path,
                state="running",
                notes_markdown=markdown,
                preview_available=False,
                can_stop=True,
                message="LLM working…",
                notes_path=None,
            )

        try:
            payload = _generate_existing_notes(
                article_path,
                notes_config,
                strategy=strategy,
                existing_notes_override=existing_notes_override,
                progress_callback=on_progress,
                cancel_requested=lambda: _notes_generation_cancel_requested(article_path),
            )
            notes_markdown = _read_markdown_body(Path(payload["notes"])) if payload.get("notes") else ""
            _write_notes_generation_status(
                article_path,
                state="completed",
                notes_markdown=notes_markdown,
                preview_available=True,
                can_stop=False,
                message="Working notes ready.",
                notes_path=payload.get("notes"),
            )
        except Exception:
            if _notes_generation_cancel_requested(article_path):
                _write_notes_generation_status(
                    article_path,
                    state="cancelled",
                    notes_markdown=_read_notes_generation_status(article_path).get("notesMarkdown", ""),
                    preview_available=False,
                    can_stop=False,
                    message="LLM generation stopped.",
                    notes_path=None,
                )
            else:
                app.logger.exception("Async notes generation failed for %s", article_path)
                _write_notes_generation_status(
                    article_path,
                    state="error",
                    notes_markdown=_read_notes_generation_status(article_path).get("notesMarkdown", ""),
                    preview_available=False,
                    can_stop=False,
                    message="LLM generation failed.",
                    notes_path=None,
                    error="Notes generation failed",
                )
        finally:
            _clear_notes_generation_pending(article_path)

    thread = threading.Thread(target=_run, name="scribe-notes-async", daemon=True)
    thread.start()


def _notes_markdown_to_html(notes_markdown: str) -> str:
    return pypandoc.convert_text(notes_markdown or "", "html", format="gfm", extra_args=["--wrap=none"])


def _notes_html_to_markdown(notes_html: str) -> str:
    markdown = pypandoc.convert_text(notes_html or "", "gfm", format="html", extra_args=["--wrap=none"])
    return markdown.strip() + ("\n" if markdown.strip() else "")


def _normalize_url_for_match(value: str | None) -> str:
    if not value:
        return ""
    text = value.strip()
    if not text:
        return ""
    try:
        parsed = urlparse(text)
    except Exception:
        return text.lower().rstrip("/")
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    path = parsed.path or ""
    if len(path) > 1:
        path = path.rstrip("/")
    return f"{parsed.scheme.lower()}://{host}{path}"


def _lookup_article_by_url(target_url: str) -> Path | None:
    normalized = _normalize_url_for_match(target_url)
    if not normalized:
        return None
    root = Path(OUTPUT_DIR)
    if not root.exists():
        return None
    for path in root.rglob("*.md"):
        if path.name.endswith(".notes.md"):
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except Exception:
            continue
        frontmatter, _ = _split_frontmatter(content)
        candidates = [
            _frontmatter_string(frontmatter, "url"),
            _frontmatter_string(frontmatter, "canonical_url"),
        ]
        for candidate in candidates:
            if _normalize_url_for_match(candidate) == normalized:
                return path
    return None


@app.route("/lookup_url", methods=["GET"])
def lookup_url():
    api_key = request.args.get("apiKey", "")
    if api_key != API_KEY:
        return jsonify(success=False, message="Unauthorized"), 401

    target = request.args.get("url", "").strip()
    if not target:
        return jsonify(success=True, exists=False)

    match = _lookup_article_by_url(target)
    if not match:
        return jsonify(success=True, exists=False)

    payload = _build_existing_article_payload(match)
    payload["exists"] = True
    return jsonify(**payload)


@app.route("/labels", methods=["GET"])
def labels():
    api_key = request.args.get("apiKey", "")
    if api_key != API_KEY:
        return jsonify(success=False, message="Unauthorized"), 401

    return jsonify(success=True, labels=_list_existing_labels())


@app.route("/capabilities", methods=["GET"])
def capabilities():
    api_key = request.args.get("apiKey", "")
    if api_key != API_KEY:
        return jsonify(success=False, message="Unauthorized"), 401

    return jsonify(
        success=True,
        pdfOcr={
            "available": _pdf_ocr_available(),
            "engine": "mistral" if _pdf_ocr_available() else "pdftotext",
            "fallback": "pdftotext",
        },
    )


@app.route("/generate_pdf", methods=["POST"])
def generate_pdf():
    """Extract article and send PDF to Kindle via Gmail."""
    data = request.json
    if not _check_api_key(data):
        return jsonify(success=False, message="Unauthorized"), 401

    url = data.get("url", "")
    page_size = data.get("pageSize", "a5")
    app.logger.info("Extracting article from %s", url)

    metadata = extract_url(
        html=data["html"],
        output_dir=TEMP_DIR,
        cookies=data.get("cookies"),
        url=url,
        page_size=page_size,
        render_pdf=True,
        pdf_required=True,
        generate_notes=False,
    )
    app.logger.info("Sending '%s' to Kindle", metadata["title"])

    send_to_kindle(
        sender=data["email"],
        to=data["kindleEmail"],
        pdf_file=metadata["file-path"],
        file_name=sanitize_filename(metadata["title"] + ".pdf"),
    )

    return jsonify(success=True, title=metadata["title"])


@app.route("/save_local", methods=["POST"])
def save_local():
    """Extract article and save PDF + Markdown to the output directory."""
    data = request.json
    if not _check_api_key(data):
        return jsonify(success=False, message="Unauthorized"), 401

    url = data.get("url", "")
    page_size = data.get("pageSize", "a5")
    label = data.get("label", "")
    notes_config = data.get("notes", {}) if isinstance(data.get("notes", {}), dict) else {}
    target_dir = _resolve_output_dir(label)
    app.logger.info("Extracting article from %s (save local)", url)

    # Extract directly into the output directory — creates {OUTPUT_DIR}/{label}/{title}/
    metadata = extract_url(
        html=data["html"],
        output_dir=str(target_dir),
        cookies=data.get("cookies"),
        url=url,
        page_size=page_size,
        label=_clean_label(label),
        render_pdf=False,
        pdf_required=False,
        generate_notes=False,
        notes_config=notes_config,
    )

    app.logger.info("Saved to %s", metadata["dir"])

    payload = {
        "success": True,
        "title": metadata["title"],
        "label": _clean_label(label),
        "dir": metadata["dir"],
        "pdf": metadata["file-path"],
        "bib": metadata.get("bib-path"),
        "md": metadata["md-path"],
        "primary": metadata["md-path"],
        "pdfAvailable": bool(metadata["file-path"]),
        "notes": metadata.get("notes-path"),
        "notesAvailable": bool(metadata.get("notes-path")),
        "notesPending": bool(metadata.get("md-path")),
        "notesDocId": metadata.get("notes-doc-id"),
        "metadata": metadata.get("metadata", {}),
    }
    _upsert_index_records(_build_index_records(payload, _clean_label(label)))

    if metadata.get("md-path"):
        _spawn_async_notes_generation(Path(metadata["md-path"]), notes_config)

    return jsonify(**payload)


@app.route("/save_pdf", methods=["POST"])
def save_pdf():
    """Download a source PDF and save the original PDF + extracted markdown locally."""
    data = request.json
    if not _check_api_key(data):
        return jsonify(success=False, message="Unauthorized"), 401

    url = data.get("url", "")
    label = data.get("label", "")
    notes_config = data.get("notes", {}) if isinstance(data.get("notes", {}), dict) else {}
    target_dir = _resolve_output_dir(label)
    app.logger.info("Extracting source PDF from %s (save local)", url)

    metadata = extract_pdf_url(
        url=url,
        output_dir=str(target_dir),
        cookies=data.get("cookies"),
        source_name=data.get("sourceName", ""),
        page_size=data.get("pageSize", "a5"),
        label=_clean_label(label),
        generate_notes=False,
        notes_config=notes_config,
    )

    app.logger.info("Saved PDF source to %s", metadata["dir"])

    payload = {
        "success": True,
        "title": metadata["title"],
        "label": _clean_label(label),
        "dir": metadata["dir"],
        "pdf": metadata["file-path"],
        "sourcePdf": metadata.get("source-pdf-path"),
        "bib": metadata.get("bib-path"),
        "md": metadata["md-path"],
        "primary": metadata["md-path"],
        "pdfAvailable": bool(metadata["file-path"]),
        "sourcePdfAvailable": bool(metadata.get("source-pdf-path")),
        "notes": metadata.get("notes-path"),
        "notesAvailable": bool(metadata.get("notes-path")),
        "notesPending": bool(metadata.get("md-path")),
        "notesDocId": metadata.get("notes-doc-id"),
        "metadata": metadata.get("metadata", {}),
    }
    _upsert_index_records(_build_index_records(payload, _clean_label(label)))

    if metadata.get("md-path"):
        _spawn_async_notes_generation(Path(metadata["md-path"]), notes_config)

    return jsonify(**payload)


@app.route("/save_pdf_upload", methods=["POST"])
def save_pdf_upload():
    """Accept a directly-uploaded PDF (e.g. from the Zotero plugin).

    Expects multipart/form-data with a ``file`` part and form fields for
    ``apiKey``, ``label``, ``pageSize``, plus an optional ``metadata`` JSON
    blob carrying Zotero citation fields and an optional ``note`` markdown
    string that is written as the article's ``.notes.md`` companion.
    """
    api_key_value = request.form.get("apiKey") or request.args.get("apiKey") or ""
    if not _check_api_key({"apiKey": api_key_value}):
        return jsonify(success=False, message="Unauthorized"), 401

    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify(success=False, message="Missing 'file' upload"), 400

    pdf_bytes = upload.read()
    if not pdf_bytes:
        return jsonify(success=False, message="Uploaded file is empty"), 400

    label = request.form.get("label", "")
    page_size = request.form.get("pageSize", "a5")
    source_name = request.form.get("sourceName") or upload.filename or "document.pdf"

    raw_metadata = request.form.get("metadata") or ""
    try:
        citation_overrides = json.loads(raw_metadata) if raw_metadata else {}
    except json.JSONDecodeError as exc:
        return jsonify(success=False, message=f"Invalid metadata JSON: {exc}"), 400
    if not isinstance(citation_overrides, dict):
        return jsonify(success=False, message="metadata must be a JSON object"), 400

    note_text = (request.form.get("note") or "").strip()
    target_dir = _resolve_output_dir(label)
    override_url = (citation_overrides.get("url") or "").strip()
    app.logger.info(
        "Ingesting uploaded PDF from client (save_pdf_upload) url=%s label=%s",
        override_url or "<none>",
        label or "<none>",
    )

    metadata = extract_pdf_bytes(
        pdf_bytes=pdf_bytes,
        output_dir=str(target_dir),
        url=override_url,
        source_name=source_name,
        label=_clean_label(label),
        page_size=page_size,
        generate_notes=False,
        notes_config={},
        render_pdf=False,
        citation_overrides=citation_overrides,
    )

    notes_path_value = metadata.get("notes-path")
    notes_doc_id_value = metadata.get("notes-doc-id")
    if note_text:
        md_path = Path(metadata["md-path"])
        notes_file = md_path.with_name(md_path.stem + ".notes.md")
        notes_doc_id_value = _notes_doc_id(metadata["metadata"]["doc_id"])
        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        notes_fields = {
            "title": f"Notes: {metadata['title']}",
            "doc_id": notes_doc_id_value,
            "doc_type": "notes",
            "article_doc_id": metadata["metadata"]["doc_id"],
            "article_title": metadata["title"],
            "article_md": md_path.name,
            "label": _clean_label(label) or None,
            "language": "en",
            "source_format": "zotero",
            "generator": "zotero-import",
            "ingested_at": now_iso,
            "updated_at": now_iso,
        }
        notes_file.write_text(
            _build_frontmatter(notes_fields) + "\n\n" + note_text.strip() + "\n",
            encoding="utf-8",
        )
        notes_path_value = str(notes_file)

    app.logger.info("Saved uploaded PDF to %s", metadata["dir"])

    payload = {
        "success": True,
        "title": metadata["title"],
        "label": _clean_label(label),
        "dir": metadata["dir"],
        "pdf": metadata["file-path"],
        "sourcePdf": metadata.get("source-pdf-path"),
        "bib": metadata.get("bib-path"),
        "md": metadata["md-path"],
        "primary": metadata["md-path"],
        "pdfAvailable": bool(metadata["file-path"]),
        "sourcePdfAvailable": bool(metadata.get("source-pdf-path")),
        "notes": notes_path_value,
        "notesAvailable": bool(notes_path_value),
        "notesPending": False,
        "notesDocId": notes_doc_id_value,
        "metadata": metadata.get("metadata", {}),
    }
    _upsert_index_records(_build_index_records(payload, _clean_label(label)))

    return jsonify(**payload)


@app.route("/health", methods=["GET"])
def health():
    return jsonify(status="ok")


@app.route("/desktop/default_root", methods=["GET"])
def desktop_default_root():
    root = _resolve_library_root(request.args.get("root"))
    return jsonify(success=True, root=str(root))


@app.route("/desktop/browse", methods=["GET"])
def desktop_browse_directory():
    raw_path = (request.args.get("path") or "").strip()
    candidate = Path(raw_path).expanduser() if raw_path else Path.home()
    try:
        resolved = candidate.resolve()
    except Exception as exc:
        return jsonify(success=False, message=f"Invalid path: {exc}"), 400
    if not resolved.exists():
        return jsonify(success=False, message=f"Path does not exist: {resolved}"), 404
    if not resolved.is_dir():
        return jsonify(success=False, message=f"Not a directory: {resolved}"), 400

    try:
        entries = sorted(
            (entry for entry in resolved.iterdir() if entry.is_dir() and not entry.name.startswith(".")),
            key=lambda item: item.name.casefold(),
        )
    except PermissionError as exc:
        return jsonify(success=False, message=f"Permission denied: {exc}"), 403

    directories = [{"name": entry.name, "path": str(entry)} for entry in entries]
    parent = str(resolved.parent) if resolved.parent != resolved else None
    return jsonify(
        success=True,
        path=str(resolved),
        parent=parent,
        directories=directories,
    )


@app.route("/desktop/library", methods=["GET"])
def desktop_library():
    root = _resolve_library_root(request.args.get("root"))
    if not root.exists():
        return jsonify(success=False, message=f"Corpus root does not exist: {root}"), 404

    documents = _scan_library_documents(root)
    labels = sorted({doc["label"] for doc in documents if doc.get("label")}, key=str.casefold)
    return jsonify(success=True, root=str(root), labels=labels, documents=documents)


@app.route("/desktop/reindex", methods=["POST"])
def desktop_reindex():
    data = request.get_json(silent=True) or {}
    root = _resolve_library_root(data.get("root"))
    if not root.exists():
        return jsonify(success=False, message=f"Corpus root does not exist: {root}"), 404

    records: list[dict] = []
    scanned = 0
    errors = 0
    for path in root.rglob("*.md"):
        if path.name.endswith(".notes.md"):
            continue
        try:
            payload = _build_existing_article_payload(path)
            records.extend(_build_index_records(payload, payload.get("label") or ""))
            scanned += 1
        except Exception as exc:
            app.logger.warning("Re-index failed for %s: %s", path, exc)
            errors += 1

    index_path = _index_file_path()
    index_path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(records, key=lambda item: (item.get("type", ""), item.get("path") or ""))
    index_path.write_text(
        "\n".join(json.dumps(item, ensure_ascii=False) for item in ordered) + ("\n" if ordered else ""),
        encoding="utf-8",
    )
    search_sync = _sync_search_index(root)

    return jsonify(
        success=True,
        scanned=scanned,
        records=len(ordered),
        errors=errors,
        indexPath=str(index_path),
        searchDbPath=search_sync["dbPath"],
        indexedDocuments=search_sync["indexed"],
        searchErrors=search_sync["errors"],
    )


_SEARCH_FIELD_ALIASES: dict[str, str] = {
    "all": "all",
    "allfields": "all",
    "title": "title",
    "ti": "title",
    "author": "author",
    "authors": "author",
    "au": "author",
    "doi": "doi",
    "year": "year",
    "date": "year",
    "dp": "year",
    "pubdate": "year",
    "body": "body",
    "text": "body",
    "abstract": "body",
    "ab": "body",
    "tiab": "titleabstract",
    "titleabstract": "titleabstract",
    "notes": "notes",
    "highlight": "highlight",
    "highlights": "highlight",
    "label": "label",
    "journal": "journal",
    "ta": "journal",
    "publisher": "publisher",
    "pmid": "pmid",
    "pmcid": "pmcid",
    "arxiv": "arxiv",
    "arxivid": "arxiv",
    "url": "url",
}
_SEARCHABLE_FIELDS = frozenset(_SEARCH_FIELD_ALIASES.values())


class _SearchQueryError(ValueError):
    pass


def _search_keyword_at(text: str, idx: int, keyword: str) -> bool:
    end = idx + len(keyword)
    if text[idx:end].upper() != keyword:
        return False
    if end < len(text) and not (text[end].isspace() or text[end] in "()"):
        return False
    if idx > 0 and not (text[idx - 1].isspace() or text[idx - 1] in "()"):
        return False
    return True


def _tokenize_search_query(query: str) -> list[tuple]:
    tokens: list[tuple] = []
    i = 0
    n = len(query)
    while i < n:
        quoted = False
        ch = query[i]
        if ch.isspace():
            i += 1
            continue
        if ch == "(":
            tokens.append(("LPAREN",))
            i += 1
            continue
        if ch == ")":
            tokens.append(("RPAREN",))
            i += 1
            continue
        if _search_keyword_at(query, i, "AND"):
            tokens.append(("OP", "AND"))
            i += 3
            continue
        if _search_keyword_at(query, i, "OR"):
            tokens.append(("OP", "OR"))
            i += 2
            continue
        if _search_keyword_at(query, i, "NOT"):
            tokens.append(("OP", "NOT"))
            i += 3
            continue
        if ch == '"':
            end = query.find('"', i + 1)
            if end == -1:
                raise _SearchQueryError("Unclosed quoted phrase")
            text = query[i + 1 : end]
            i = end + 1
            quoted = True
        else:
            start = i
            while i < n:
                c = query[i]
                if c in "()[\"":
                    break
                if c.isspace():
                    j = i
                    while j < n and query[j].isspace():
                        j += 1
                    if j >= n:
                        break
                    if query[j] in "()":
                        break
                    if _search_keyword_at(query, j, "AND"):
                        break
                    if _search_keyword_at(query, j, "OR"):
                        break
                    if _search_keyword_at(query, j, "NOT"):
                        break
                i += 1
            text = query[start:i]
        field: str | None = None
        if i < n and query[i] == "[":
            end = query.find("]", i + 1)
            if end == -1:
                raise _SearchQueryError("Unclosed field tag")
            field_raw = query[i + 1 : end].strip().lower().replace(" ", "").replace("/", "").replace("-", "")
            if not field_raw:
                field = "all"
            else:
                field = _SEARCH_FIELD_ALIASES.get(field_raw)
                if field is None or field not in _SEARCHABLE_FIELDS:
                    raise _SearchQueryError(f"Unknown field tag: {field_raw}")
            i = end + 1
        text = text.strip()
        if not text:
            continue
        if quoted:
            tokens.append(("TERM", text.lower(), field or "all", True))
        elif field is None:
            for word in text.split():
                tokens.append(("TERM", word.lower(), "all", False))
        else:
            for word in text.split():
                tokens.append(("TERM", word.lower(), field, False))
    return tokens


class _SearchQueryParser:
    def __init__(self, tokens: list[tuple]):
        self.tokens = tokens
        self.pos = 0

    def _peek(self):
        return self.tokens[self.pos] if self.pos < len(self.tokens) else None

    def _consume(self):
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    def parse(self):
        if not self.tokens:
            return None
        node = self._parse_or()
        if self.pos != len(self.tokens):
            raise _SearchQueryError(f"Unexpected token: {self.tokens[self.pos]}")
        return node

    def _parse_or(self):
        left = self._parse_and()
        while True:
            tok = self._peek()
            if tok is None or tok[0] != "OP" or tok[1] != "OR":
                break
            self._consume()
            right = self._parse_and()
            left = ("or", left, right)
        return left

    def _parse_and(self):
        left = self._parse_not()
        while True:
            tok = self._peek()
            if tok is None:
                break
            if tok[0] == "RPAREN":
                break
            if tok[0] == "OP" and tok[1] == "OR":
                break
            if tok[0] == "OP" and tok[1] == "AND":
                self._consume()
                right = self._parse_not()
                left = ("and", left, right)
                continue
            right = self._parse_not()
            left = ("and", left, right)
        return left

    def _parse_not(self):
        tok = self._peek()
        if tok and tok[0] == "OP" and tok[1] == "NOT":
            self._consume()
            child = self._parse_not()
            return ("not", child)
        return self._parse_atom()

    def _parse_atom(self):
        tok = self._peek()
        if tok is None:
            raise _SearchQueryError("Unexpected end of query")
        if tok[0] == "LPAREN":
            self._consume()
            node = self._parse_or()
            closing = self._peek()
            if closing is None or closing[0] != "RPAREN":
                raise _SearchQueryError("Missing closing parenthesis")
            self._consume()
            return node
        if tok[0] == "TERM":
            self._consume()
            return ("term", tok[1], tok[2], tok[3])
        raise _SearchQueryError(f"Unexpected token: {tok}")


def _parse_search_query(query: str):
    tokens = _tokenize_search_query(query)
    return _SearchQueryParser(tokens).parse()


_SEARCH_BM25_WEIGHTS = (12.0, 9.0, 11.0, 5.0, 2.0, 6.0, 3.0, 4.0, 7.0, 5.0, 4.0, 10.0, 10.0, 10.0, 3.0)


def _fts_phrase(text: str) -> str:
    return '"' + text.replace('"', '""').strip() + '"'


def _search_term_uses_prefix(text: str, quoted: bool) -> bool:
    if quoted:
        return False
    return bool(re.fullmatch(r"[a-z0-9][a-z0-9_-]*", text))


def _compile_search_term_query(text: str, field: str, quoted: bool) -> str:
    if _search_term_uses_prefix(text, quoted):
        compiled = f"{text}*"
    else:
        compiled = _fts_phrase(text)
    if field == "all":
        return compiled
    return f"{field} : {compiled}"


def _compile_search_sql(node) -> tuple[str, list[str]]:
    if node is None:
        return "1", []
    kind = node[0]
    if kind == "term":
        _, text, field, quoted = node
        compiled = _compile_search_term_query(text, field, quoted)
        return (
            "documents.id IN (SELECT rowid FROM documents_fts WHERE documents_fts MATCH ?)",
            [compiled],
        )
    if kind == "and":
        left_sql, left_params = _compile_search_sql(node[1])
        right_sql, right_params = _compile_search_sql(node[2])
        return f"({left_sql} AND {right_sql})", [*left_params, *right_params]
    if kind == "or":
        left_sql, left_params = _compile_search_sql(node[1])
        right_sql, right_params = _compile_search_sql(node[2])
        return f"({left_sql} OR {right_sql})", [*left_params, *right_params]
    if kind == "not":
        child_sql, child_params = _compile_search_sql(node[1])
        return f"(NOT ({child_sql}))", child_params
    raise _SearchQueryError(f"Unsupported search node: {kind}")


def _collect_positive_search_terms(node, negated: bool = False) -> list[str]:
    if node is None:
        return []
    kind = node[0]
    if kind == "term":
        if negated:
            return []
        _, text, field, quoted = node
        return [_compile_search_term_query(text, field, quoted)]
    if kind == "not":
        return _collect_positive_search_terms(node[1], True)
    if kind in {"and", "or"}:
        return [
            *_collect_positive_search_terms(node[1], negated),
            *_collect_positive_search_terms(node[2], negated),
        ]
    return []


def _search_result_from_row(row: sqlite3.Row) -> dict:
    return {
        "id": row["doc_key"],
        "title": row["title"],
        "label": row["label"],
        "articlePath": row["article_path"],
        "notesPath": row["notes_path"],
        "notesPending": False,
        "bibPath": row["bib_path"],
        "highlightsPath": row["highlights_path"],
        "highlightCount": int(row["highlight_count"] or 0),
        "readingPdfPath": row["reading_pdf_path"],
        "sourcePdfPath": row["source_pdf_path"],
        "sourceSite": row["source_site"],
        "ingestedAt": row["ingested_at"],
        "rating": int(row["rating"] or 0),
        "url": row["url"],
        "canonicalUrl": row["canonical_url"],
        "excerpt": row["excerpt"] or "",
    }


def _indexed_document_from_row(row: sqlite3.Row) -> dict:
    doc = _search_result_from_row(row)
    doc["authors"] = row["authors"]
    doc["doi"] = row["doi"]
    doc["year"] = row["year"]
    doc["arxivId"] = row["arxiv_id"]
    doc["pmid"] = row["pmid"]
    doc["pmcid"] = row["pmcid"]
    doc["metadataText"] = {
        "authors": row["authors"] or "",
        "doi": row["doi"] or "",
        "year": row["year"] or "",
        "published": row["published"] or "",
        "date": row["date_text"] or "",
        "arxiv_id": row["arxiv_id"] or "",
        "pmid": row["pmid"] or "",
        "pmcid": row["pmcid"] or "",
        "journal": row["journal"] or "",
        "publisher": row["publisher"] or "",
        "ingested_at": row["ingested_at"] or "",
    }
    return doc


def _load_indexed_documents(root: Path) -> list[dict]:
    conn = _open_search_db(root)
    try:
        _ensure_search_schema(conn)
        rows = conn.execute(
            """
            SELECT *
            FROM documents
            ORDER BY rating DESC, ingested_at DESC, title COLLATE NOCASE ASC
            """
        ).fetchall()
        return [_indexed_document_from_row(row) for row in rows]
    finally:
        conn.close()


def _run_indexed_search(root: Path, query_tree, label: str | None = None, limit: int = 200) -> tuple[list[dict], int]:
    conn = _open_search_db(root)
    try:
        _ensure_search_schema(conn)
        where_sql, where_params = _compile_search_sql(query_tree)
        if label and label != "all":
            where_sql = f"({where_sql}) AND COALESCE(documents.label, '') = ?"
            where_params = [*where_params, label]

        positive_terms = list(dict.fromkeys(_collect_positive_search_terms(query_tree)))
        if positive_terms:
            weights_sql = ", ".join(str(weight) for weight in _SEARCH_BM25_WEIGHTS)
            score_parts = [
                f"COALESCE((SELECT bm25(documents_fts, {weights_sql}) "
                "FROM documents_fts WHERE rowid = documents.id AND documents_fts MATCH ?), 0.0)"
                for _ in positive_terms
            ]
            score_sql = " + ".join(score_parts)
            score_params = positive_terms
        else:
            score_sql = "0.0"
            score_params = []

        total = conn.execute(
            f"SELECT COUNT(*) FROM documents WHERE {where_sql}",
            where_params,
        ).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT
                documents.*,
                {score_sql} AS search_rank
            FROM documents
            WHERE {where_sql}
            ORDER BY search_rank ASC, rating DESC, ingested_at DESC, title COLLATE NOCASE ASC
            LIMIT ?
            """,
            [*score_params, *where_params, limit],
        ).fetchall()
        return ([_search_result_from_row(row) for row in rows], int(total))
    finally:
        conn.close()


@app.route("/desktop/search", methods=["POST"])
def desktop_search():
    data = request.get_json(silent=True) or {}
    root = _resolve_library_root(data.get("root"))
    query = (data.get("query") or "").strip()
    label = (data.get("label") or "").strip()
    if not root.exists():
        return jsonify(success=False, message=f"Corpus root does not exist: {root}"), 404
    if not query:
        return jsonify(success=True, documents=[], total=0, truncated=False)

    try:
        tree = _parse_search_query(query)
    except _SearchQueryError as exc:
        return jsonify(success=False, message=f"Invalid query: {exc}"), 400

    sync_info = _sync_search_index(root)
    results, total = _run_indexed_search(root, tree, label or None, limit=200)
    return jsonify(
        success=True,
        documents=results,
        total=total,
        truncated=total > len(results),
        indexPath=sync_info["dbPath"],
    )


@app.route("/desktop/document", methods=["GET"])
def desktop_document():
    article_path = request.args.get("articlePath", "").strip()
    if not article_path:
        return jsonify(success=False, message="Missing articlePath"), 400

    path = Path(article_path)
    if not path.exists():
        return jsonify(success=False, message=f"Document not found: {path}"), 404

    content = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(content)
    if (_frontmatter_string(frontmatter, "source_format") or "").lower() == "pdf":
        body = _strip_pdf_thematic_breaks(body)
        body = _normalize_latex_delimiters_in_markdown(body)
    rating_value = _frontmatter_int(frontmatter, "rating")
    rating = max(0, min(5, rating_value)) if isinstance(rating_value, int) else 0
    summary = {
        "id": _stable_doc_id(path),
        "title": _frontmatter_string(frontmatter, "title") or path.stem,
        "label": _frontmatter_string(frontmatter, "label"),
        "articlePath": str(path),
        "notesPath": str(_sibling_with_suffix_if_exists(path, ".notes.md")) if _sibling_with_suffix_if_exists(path, ".notes.md") else None,
        "notesPending": _notes_generation_pending(path),
        "bibPath": str(_sibling_if_exists(path, "bib")) if _sibling_if_exists(path, "bib") else None,
        "highlightsPath": str(_highlights_path(path)) if _highlights_path(path).exists() else None,
        "highlightCount": len(_load_highlights(path)),
        "readingPdfPath": str(_sibling_with_suffix_if_exists(path, ".reading.pdf")) if _sibling_with_suffix_if_exists(path, ".reading.pdf") else (str(_sibling_if_exists(path, "pdf")) if _sibling_if_exists(path, "pdf") else None),
        "sourcePdfPath": str(_sibling_with_suffix_if_exists(path, ".source.pdf")) if _sibling_with_suffix_if_exists(path, ".source.pdf") else None,
        "sourceSite": _frontmatter_string(frontmatter, "source_site"),
        "ingestedAt": _frontmatter_string(frontmatter, "ingested_at"),
        "rating": rating,
        "url": _frontmatter_string(frontmatter, "url"),
        "canonicalUrl": _frontmatter_string(frontmatter, "canonical_url"),
        "excerpt": _excerpt_from_markdown(body),
    }

    notes_path = _sibling_with_suffix_if_exists(path, ".notes.md")
    bib_path = _sibling_if_exists(path, "bib")
    notes_markdown = _read_markdown_body(notes_path) if notes_path else ""
    bibliography = bib_path.read_text(encoding="utf-8") if bib_path else ""
    highlights = _load_highlights(path)
    related = _load_related(path)

    return jsonify(
        success=True,
        detail={
            "summary": summary,
            "markdown": body,
            "notesMarkdown": notes_markdown,
            "highlights": highlights,
            "bibliography": bibliography,
            "frontmatter": frontmatter,
            "related": related,
        },
    )


@app.route("/desktop/document/read", methods=["GET", "POST"])
def desktop_document_read():
    """Return a document's markdown body with noise-marked highlights removed.

    Intended for LLM/MCP consumers that want clean prose. Accepts either a GET
    with ``articlePath`` / ``stripNoise`` / ``stripReferences`` query args, or
    a POST with the same keys in a JSON body.
    """
    if request.method == "GET":
        raw_path = (request.args.get("articlePath") or "").strip()
        strip_noise = (request.args.get("stripNoise") or "true").lower() != "false"
        strip_references = (request.args.get("stripReferences") or "true").lower() != "false"
    else:
        data = request.get_json(silent=True) or {}
        raw_path = (data.get("articlePath") or "").strip()
        strip_noise = data.get("stripNoise", True) is not False
        strip_references = data.get("stripReferences", True) is not False

    if not raw_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    path = Path(raw_path)
    if not path.exists():
        return jsonify(success=False, message=f"Document not found: {path}"), 404

    content = path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(content)
    highlights = _load_highlights(path)
    if strip_noise:
        body = _strip_noise_from_markdown(body, highlights, strip_references=strip_references)

    return jsonify(
        success=True,
        articlePath=str(path),
        title=_frontmatter_string(frontmatter, "title") or path.stem,
        markdown=body,
        noiseStripped=bool(strip_noise),
        referencesStripped=bool(strip_references and strip_noise),
    )


@app.route("/desktop/notes", methods=["POST"])
def desktop_save_notes():
    data = request.get_json(silent=True) or {}
    article_path = Path((data.get("articlePath") or "").strip())
    notes_markdown = data.get("notesMarkdown") or ""
    if not article_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404

    notes_path = article_path.with_name(article_path.stem + ".notes.md")
    article_text = article_path.read_text(encoding="utf-8")
    article_frontmatter, article_body = _split_frontmatter(article_text)
    existing_frontmatter = {}
    if notes_path.exists():
        existing_frontmatter, _ = _split_frontmatter(notes_path.read_text(encoding="utf-8"))
    notes_frontmatter = _notes_metadata_from_article(article_frontmatter, article_path)
    notes_frontmatter.update({key: value for key, value in existing_frontmatter.items() if value not in (None, "")})
    notes_frontmatter["title"] = f"{_frontmatter_string(article_frontmatter, 'title') or article_path.stem} Notes"
    notes_frontmatter["doc_type"] = "notes"
    notes_frontmatter["type"] = notes_frontmatter.get("type") or "companion_notes"
    notes_frontmatter["source_article"] = article_path.name
    if _frontmatter_string(article_frontmatter, "doc_id"):
        notes_frontmatter["source_doc_id"] = _frontmatter_string(article_frontmatter, "doc_id")
        notes_frontmatter["doc_id"] = notes_frontmatter.get("doc_id") or _notes_doc_id(_frontmatter_string(article_frontmatter, "doc_id"))

    notes_path.write_text(
        _build_frontmatter(notes_frontmatter) + "\n\n" + notes_markdown.strip() + "\n",
        encoding="utf-8",
    )
    _clear_notes_generation_pending(article_path)

    article_frontmatter["notes_file"] = notes_path.name
    if _frontmatter_string(article_frontmatter, "doc_id"):
        article_frontmatter["notes_doc_id"] = _notes_doc_id(_frontmatter_string(article_frontmatter, "doc_id"))
    _write_article_frontmatter(article_path, article_frontmatter, article_body)

    payload = _build_existing_article_payload(article_path)
    _upsert_index_records(_build_index_records(payload, payload["label"]))
    _publish_desktop_event("notes_updated", article_path, notesPath=str(notes_path))
    return jsonify(success=True, notesPath=str(notes_path), notesMarkdown=notes_markdown, notesPending=False)


@app.route("/desktop/document", methods=["POST"])
def desktop_save_document():
    data = request.get_json(silent=True) or {}
    article_path = Path((data.get("articlePath") or "").strip())
    markdown = data.get("markdown") or ""
    if not article_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404

    article_text = article_path.read_text(encoding="utf-8")
    article_frontmatter, _article_body = _split_frontmatter(article_text)
    _write_article_frontmatter(article_path, article_frontmatter, markdown)

    payload = _build_existing_article_payload(article_path)
    _upsert_index_records(_build_index_records(payload, payload["label"]))
    _publish_desktop_event("document_updated", article_path)
    return jsonify(success=True, articlePath=str(article_path), markdown=markdown)


@app.route("/desktop/notes/generate", methods=["POST"])
def desktop_generate_notes():
    data = request.get_json(silent=True) or {}
    article_path = Path((data.get("articlePath") or "").strip())
    strategy = str(data.get("strategy") or "replace").strip().lower()
    existing_notes = data.get("existingNotes")
    if existing_notes is not None:
        existing_notes = str(existing_notes)
    if not article_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404
    if strategy not in {"replace", "append", "fuse"}:
        return jsonify(success=False, message=f"Unsupported notes strategy: {strategy}"), 400
    if not _notes_generation_pending(article_path):
        _spawn_async_notes_generation(article_path, strategy=strategy, existing_notes_override=existing_notes)
    status = _read_notes_generation_status(article_path)
    return jsonify(success=True, started=True, **status)


@app.route("/desktop/notes/status", methods=["GET"])
def desktop_notes_status():
    article_path = Path((request.args.get("articlePath") or "").strip())
    if not article_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404
    return jsonify(success=True, **_read_notes_generation_status(article_path))


@app.route("/desktop/notes/cancel", methods=["POST"])
def desktop_cancel_notes():
    data = request.get_json(silent=True) or {}
    article_path = Path((data.get("articlePath") or "").strip())
    if not article_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404
    _cancel_notes_generation(article_path)
    _write_notes_generation_status(
        article_path,
        state="cancelling",
        notes_markdown=_read_notes_generation_status(article_path).get("notesMarkdown", ""),
        preview_available=False,
        can_stop=False,
        message="Stopping LLM…",
        notes_path=None,
    )
    return jsonify(success=True, state="cancelling")


@app.route("/desktop/notes/render", methods=["POST"])
def desktop_render_notes():
    data = request.get_json(silent=True) or {}
    try:
        rendered_html = _notes_markdown_to_html(str(data.get("notesMarkdown") or ""))
    except Exception as error:
        return jsonify(success=False, message=str(error)), 500
    return jsonify(success=True, html=rendered_html)


@app.route("/desktop/notes/markdownize", methods=["POST"])
def desktop_markdownize_notes():
    data = request.get_json(silent=True) or {}
    try:
        markdown = _notes_html_to_markdown(str(data.get("notesHtml") or ""))
    except Exception as error:
        return jsonify(success=False, message=str(error)), 500
    return jsonify(success=True, notesMarkdown=markdown)


@app.route("/desktop/highlights", methods=["POST"])
def desktop_save_highlights():
    data = request.get_json(silent=True) or {}
    article_path = Path((data.get("articlePath") or "").strip())
    raw_highlights = data.get("highlights")
    if not article_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404
    if not isinstance(raw_highlights, list):
        return jsonify(success=False, message="Missing highlights"), 400

    cleaned: list[dict] = []
    for item in raw_highlights:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind")
        element_type = item.get("elementType")
        element_index = item.get("elementIndex")
        is_element = kind == "element" or (
            isinstance(element_type, str) and isinstance(element_index, int)
        )
        text = str(item.get("text") or "").strip()
        highlight_id = str(item.get("id") or "").strip()
        created_at = str(item.get("createdAt") or "").strip()
        start_offset = item.get("startOffset")
        end_offset = item.get("endOffset")
        comment = str(item.get("comment") or "").strip()
        variant = str(item.get("variant") or "").strip()
        if not is_element and not text:
            continue
        cleaned_item = {
            "id": highlight_id,
            "text": text,
            "createdAt": created_at,
        }
        if isinstance(start_offset, int) and isinstance(end_offset, int) and end_offset > start_offset:
            cleaned_item["startOffset"] = start_offset
            cleaned_item["endOffset"] = end_offset
        if comment:
            cleaned_item["comment"] = comment
        if variant:
            cleaned_item["variant"] = variant
        if is_element:
            cleaned_item["kind"] = "element"
            if isinstance(element_type, str) and element_type.strip():
                cleaned_item["elementType"] = element_type.strip()
            if isinstance(element_index, int) and element_index >= 0:
                cleaned_item["elementIndex"] = element_index
        cleaned.append(cleaned_item)

    highlights_path = _write_highlights(article_path, cleaned)
    _publish_desktop_event(
        "highlights_updated",
        article_path,
        highlightCount=len(cleaned),
    )
    return jsonify(success=True, highlightsPath=str(highlights_path), highlights=cleaned)


_DOI_PATTERN = re.compile(r"\b10\.\d{4,9}/[\w.\-;()/:]+", re.IGNORECASE)
_ARXIV_PATTERN = re.compile(r"\b(?:arxiv[:\s]*)?(\d{4}\.\d{4,5})(?:v\d+)?\b", re.IGNORECASE)
_PMID_PATTERN = re.compile(r"\bpmid[:\s]*(\d{4,9})\b", re.IGNORECASE)
_PMCID_PATTERN = re.compile(r"\bPMC\d{4,9}\b", re.IGNORECASE)


def _normalize_reference_id(value: str) -> str:
    return value.strip().lower().rstrip(".,);:")


def _extract_reference_ids(body: str) -> dict[str, set[str]]:
    found = {
        "doi": set(),
        "arxiv": set(),
        "pmid": set(),
        "pmcid": set(),
    }
    for match in _DOI_PATTERN.finditer(body):
        found["doi"].add(_normalize_reference_id(match.group(0)))
    for match in _ARXIV_PATTERN.finditer(body):
        found["arxiv"].add(_normalize_reference_id(match.group(1)))
    for match in _PMID_PATTERN.finditer(body):
        found["pmid"].add(_normalize_reference_id(match.group(1)))
    for match in _PMCID_PATTERN.finditer(body):
        found["pmcid"].add(_normalize_reference_id(match.group(0)))
    return found


def _title_tokens(title: str) -> set[str]:
    lowered = title.lower()
    tokens = re.findall(r"[a-z0-9][a-z0-9\-]{3,}", lowered)
    stopwords = {
        "with", "from", "this", "that", "into", "over", "such",
        "their", "they", "than", "these", "those", "which", "about",
        "when", "what", "where", "some", "have", "does", "doing",
        "using", "based", "toward", "towards", "among", "also",
    }
    return {token for token in tokens if token not in stopwords}


def _load_affinity_context(article_path: Path) -> dict:
    """Collect reference IDs, title tokens, and user-linked related paths.

    Returned dict is consumed by ``_compute_affinity_score`` to boost search
    results that relate to a currently opened document.
    """
    try:
        body = _read_markdown_body(article_path)
    except Exception:
        body = ""
    frontmatter, _ = _split_frontmatter(article_path.read_text(encoding="utf-8"))
    title = _frontmatter_string(frontmatter, "title") or article_path.stem
    related_paths: set[str] = set()
    for item in _load_related(article_path):
        raw = item.get("articlePath")
        if isinstance(raw, str) and raw:
            try:
                related_paths.add(str(Path(raw).resolve()))
            except Exception:
                related_paths.add(raw)
    try:
        self_path = str(article_path.resolve())
    except Exception:
        self_path = str(article_path)
    return {
        "reference_ids": _extract_reference_ids(body),
        "title_tokens": _title_tokens(title),
        "related_paths": related_paths,
        "self_path": self_path,
    }


def _compute_affinity_score(doc: dict, context: dict) -> int:
    """Return an affinity score between ``doc`` and the active article.

    Higher means more relevant. Scoring prioritizes hard references (shared
    DOI/arxiv/pmid/pmcid), then user-saved related links, then soft title
    token overlap.
    """
    try:
        doc_resolved = str(Path(doc.get("articlePath") or "").resolve())
    except Exception:
        doc_resolved = doc.get("articlePath") or ""
    if doc_resolved == context.get("self_path"):
        return 0

    meta = doc.get("metadataText") or {}
    if not isinstance(meta, dict):
        meta = {}
    references = context.get("reference_ids") or {
        "doi": set(),
        "arxiv": set(),
        "pmid": set(),
        "pmcid": set(),
    }

    score = 0
    doc_doi = _normalize_reference_id(meta.get("doi") or "")
    if doc_doi and doc_doi in references["doi"]:
        score += 20
    doc_arxiv = _normalize_reference_id(meta.get("arxiv_id") or "")
    if doc_arxiv:
        bare = doc_arxiv.split("v", 1)[0]
        if doc_arxiv in references["arxiv"] or bare in references["arxiv"]:
            score += 20
    doc_pmid = _normalize_reference_id(meta.get("pmid") or "")
    if doc_pmid and doc_pmid in references["pmid"]:
        score += 20
    doc_pmcid = _normalize_reference_id(meta.get("pmcid") or "")
    if doc_pmcid and doc_pmcid in references["pmcid"]:
        score += 20

    related_paths = context.get("related_paths") or set()
    if doc_resolved in related_paths:
        score += 15

    title_tokens = context.get("title_tokens") or set()
    overlap = title_tokens & _title_tokens(doc.get("title") or "")
    score += len(overlap)

    return score


@app.route("/desktop/related/suggest", methods=["GET"])
def desktop_suggest_related():
    article_arg = request.args.get("articlePath", "").strip()
    if not article_arg:
        return jsonify(success=False, message="Missing articlePath"), 400
    article_path = Path(article_arg)
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404

    root = _resolve_library_root(request.args.get("root"))
    if not root.exists():
        return jsonify(success=False, message=f"Corpus root does not exist: {root}"), 404
    _sync_search_index(root)

    try:
        body = _read_markdown_body(article_path)
    except Exception as exc:
        return jsonify(success=False, message=f"Failed to read document: {exc}"), 500

    source_frontmatter, _ = _split_frontmatter(article_path.read_text(encoding="utf-8"))
    source_title = _frontmatter_string(source_frontmatter, "title") or article_path.stem
    source_title_tokens = _title_tokens(source_title)

    reference_ids = _extract_reference_ids(body)

    try:
        source_resolved = article_path.resolve()
    except Exception:
        source_resolved = article_path

    suggestions: list[dict] = []
    for doc in _load_indexed_documents(root):
        try:
            doc_resolved = Path(doc["articlePath"]).resolve()
        except Exception:
            doc_resolved = Path(doc["articlePath"])
        if doc_resolved == source_resolved:
            continue
        meta = doc.get("metadataText") or {}
        if not isinstance(meta, dict):
            meta = {}
        reasons: list[str] = []
        doc_doi = _normalize_reference_id(meta.get("doi") or "")
        if doc_doi and doc_doi in reference_ids["doi"]:
            reasons.append(f"Cited DOI {doc_doi}")
        doc_arxiv = _normalize_reference_id(meta.get("arxiv_id") or "")
        if doc_arxiv:
            bare = doc_arxiv.split("v", 1)[0]
            if doc_arxiv in reference_ids["arxiv"] or bare in reference_ids["arxiv"]:
                reasons.append(f"Cited arXiv {doc_arxiv}")
        doc_pmid = _normalize_reference_id(meta.get("pmid") or "")
        if doc_pmid and doc_pmid in reference_ids["pmid"]:
            reasons.append(f"Cited PMID {doc_pmid}")
        doc_pmcid = _normalize_reference_id(meta.get("pmcid") or "")
        if doc_pmcid and doc_pmcid in reference_ids["pmcid"]:
            reasons.append(f"Cited {doc_pmcid.upper()}")
        doc_title_tokens = _title_tokens(doc.get("title") or "")
        overlap = source_title_tokens & doc_title_tokens
        score = 10 * len(reasons) + len(overlap)
        if reasons:
            suggestions.append(
                {
                    **doc,
                    "score": score,
                    "reasons": reasons,
                    "sharedTerms": sorted(overlap),
                }
            )
            continue
        if len(overlap) >= 2:
            suggestions.append(
                {
                    **doc,
                    "score": score,
                    "reasons": [f"Shared terms: {', '.join(sorted(overlap))}"],
                    "sharedTerms": sorted(overlap),
                }
            )

    suggestions.sort(key=lambda item: (-int(item.get("score") or 0), item.get("title") or ""))
    return jsonify(success=True, suggestions=suggestions[:30])


@app.route("/desktop/related", methods=["POST"])
def desktop_save_related():
    data = request.get_json(silent=True) or {}
    article_path = Path((data.get("articlePath") or "").strip())
    raw_items = data.get("items")
    if not article_path.name:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404
    if not isinstance(raw_items, list):
        return jsonify(success=False, message="Missing items"), 400

    cleaned: list[dict] = []
    seen_targets: set[str] = set()
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        target_path_raw = item.get("targetPath")
        if not isinstance(target_path_raw, str) or not target_path_raw.strip():
            continue
        try:
            target_path = Path(target_path_raw.strip()).resolve()
        except Exception:
            continue
        try:
            self_path = article_path.resolve()
        except Exception:
            self_path = article_path
        if target_path == self_path:
            continue
        key = str(target_path)
        if key in seen_targets:
            continue
        seen_targets.add(key)
        entry = {
            "id": str(item.get("id") or "").strip() or _stable_doc_id(target_path),
            "targetPath": str(target_path),
            "targetTitle": str(item.get("targetTitle") or "").strip(),
            "note": str(item.get("note") or "").strip(),
            "createdAt": str(item.get("createdAt") or "").strip(),
        }
        cleaned.append(entry)

    related_path = _write_related(article_path, cleaned)
    _publish_desktop_event("related_updated", article_path, count=len(cleaned))
    return jsonify(success=True, relatedPath=str(related_path), items=cleaned)


def _reading_pdf_meta_path(article_path: Path) -> Path:
    return article_path.with_name(article_path.stem + ".reading.meta.json")


def _read_reading_pdf_meta(article_path: Path) -> dict:
    path = _reading_pdf_meta_path(article_path)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _write_reading_pdf_meta(article_path: Path, meta: dict) -> None:
    path = _reading_pdf_meta_path(article_path)
    path.write_text(json.dumps(meta, ensure_ascii=False) + "\n", encoding="utf-8")


def _reading_pdf_is_fresh(
    article_path: Path,
    reading_pdf: Path,
    page_size: str,
    strip_references: bool,
) -> bool:
    """Return True if the reading PDF is newer than the md + highlights sources
    and was generated with the same page size + references setting."""
    if not reading_pdf.exists():
        return False
    try:
        pdf_mtime = reading_pdf.stat().st_mtime
    except OSError:
        return False
    try:
        if article_path.stat().st_mtime > pdf_mtime:
            return False
    except OSError:
        return False
    highlights_path = _highlights_path(article_path)
    if highlights_path.exists():
        try:
            if highlights_path.stat().st_mtime > pdf_mtime:
                return False
        except OSError:
            return False
    meta = _read_reading_pdf_meta(article_path)
    if meta.get("pageSize") != page_size:
        return False
    if bool(meta.get("stripReferences")) != bool(strip_references):
        return False
    return True


@app.route("/desktop/reading_pdf", methods=["POST"])
def desktop_reading_pdf():
    """Generate (or return cached) reading PDF for a markdown article."""
    data = request.get_json(silent=True) or {}
    raw_path = (data.get("articlePath") or "").strip()
    if not raw_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    article_path = Path(raw_path)
    if not article_path.exists() or not article_path.is_file():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404

    page_size = (data.get("pageSize") or "a5").strip().lower()
    strip_references = bool(data.get("stripReferences"))
    reading_pdf_path = article_path.with_name(article_path.stem + ".reading.pdf")

    if _reading_pdf_is_fresh(
        article_path, reading_pdf_path, page_size, strip_references
    ):
        return jsonify(
            success=True,
            readingPdfPath=str(reading_pdf_path),
            cached=True,
        )

    highlights = _load_highlights(article_path)
    try:
        generated_path = regenerate_reading_pdf(
            article_path=article_path,
            page_size=page_size,
            highlights=highlights,
            strip_references=strip_references,
        )
    except Exception as error:
        app.logger.exception("Reading PDF generation failed for %s", article_path)
        return jsonify(success=False, message=str(error)), 500

    _write_reading_pdf_meta(
        article_path,
        {"pageSize": page_size, "stripReferences": strip_references},
    )

    return jsonify(
        success=True,
        readingPdfPath=str(generated_path),
        cached=False,
    )


@app.route("/desktop/rating", methods=["POST"])
def desktop_save_rating():
    data = request.get_json(silent=True) or {}
    article_path = Path((data.get("articlePath") or "").strip())
    if not article_path.name:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404

    try:
        rating = _coerce_rating(data.get("rating"))
    except ValueError as error:
        return jsonify(success=False, message=str(error)), 400

    article_text = article_path.read_text(encoding="utf-8")
    article_frontmatter, article_body = _split_frontmatter(article_text)
    if rating == 0:
        article_frontmatter.pop("rating", None)
    else:
        article_frontmatter["rating"] = rating
    _write_article_frontmatter(article_path, article_frontmatter, article_body)

    payload = _build_existing_article_payload(article_path)
    _upsert_index_records(_build_index_records(payload, payload["label"]))
    _publish_desktop_event("rating_updated", article_path, rating=rating)
    return jsonify(success=True, articlePath=str(article_path), rating=rating)


@app.route("/desktop/document/delete", methods=["POST"])
def desktop_delete_document():
    data = request.get_json(silent=True) or {}
    raw_path = (data.get("articlePath") or "").strip()
    if not raw_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    article_path = Path(raw_path)
    if not article_path.exists() or not article_path.is_file():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404

    try:
        result = _delete_article_bundle(article_path)
    except Exception as error:
        return jsonify(success=False, message=str(error)), 500

    return jsonify(success=True, articlePath=str(article_path), **result)


@app.route("/desktop/file", methods=["GET"])
def desktop_file():
    raw_path = request.args.get("path", "").strip()
    if not raw_path:
        return jsonify(success=False, message="Missing path"), 400
    path = Path(raw_path)
    if not path.exists() or not path.is_file():
        return jsonify(success=False, message=f"File not found: {path}"), 404
    download_flag = request.args.get("download", "").strip().lower() in {"1", "true", "yes"}
    return send_file(path, as_attachment=download_flag, download_name=path.name if download_flag else None)


def _running_in_docker() -> bool:
    """Best-effort detection of running inside a Docker/OCI container.

    Used to skip ``xdg-open`` et al. because the container usually has the
    binary but no X / Wayland session, so ``subprocess.Popen`` returns
    successfully while the file manager never actually appears — leading the
    desktop reader's Location button to look broken.
    """
    if Path("/.dockerenv").exists():
        return True
    try:
        cgroup = Path("/proc/1/cgroup").read_text(encoding="utf-8", errors="ignore")
    except Exception:
        return False
    return "docker" in cgroup or "containerd" in cgroup


_WSL_MOUNT_RE = re.compile(r"^/mnt/([a-zA-Z])(/.*)?$")


def _derive_wsl_native_prefix(host_path: str) -> str | None:
    """Translate a WSL-style ``/mnt/<letter>/...`` host path into a
    Windows-native path (e.g. ``/mnt/c/Users/x`` → ``C:\\Users\\x``).

    Returns None if ``host_path`` does not look like a WSL bind onto a Windows
    drive. This lets WSL users skip setting ``HOST_OUTPUT_DIR_NATIVE``
    explicitly — the backend can derive the Windows form from the
    ``HOST_OUTPUT_DIR`` mount point the compose file already passes through.
    """
    if not host_path:
        return None
    m = _WSL_MOUNT_RE.match(host_path.strip())
    if not m:
        return None
    drive = m.group(1).upper()
    tail = (m.group(2) or "").rstrip("/")
    if not tail:
        return f"{drive}:\\"
    return f"{drive}:" + tail.replace("/", "\\")


def _resolve_host_native_prefix() -> str | None:
    """Pick the host-native root for path translation.

    Explicit ``HOST_OUTPUT_DIR_NATIVE`` wins; otherwise try to auto-derive it
    from ``HOST_OUTPUT_DIR`` (currently only WSL ``/mnt/<letter>`` binds).
    """
    if HOST_OUTPUT_DIR_NATIVE:
        return HOST_OUTPUT_DIR_NATIVE
    return _derive_wsl_native_prefix(HOST_OUTPUT_DIR)


def _translate_host_library_root_to_container(target: str) -> str:
    """Map a host-visible corpus root to the container-visible path.

    This allows the desktop UI to pass a host path such as a WSL mount under
    ``HOST_OUTPUT_DIR`` while the backend still reads the corresponding files
    through the container bind mount rooted at ``OUTPUT_DIR``.
    """
    value = (target or "").strip()
    if not value:
        return value

    host_prefixes = [prefix for prefix in {HOST_OUTPUT_DIR, _resolve_host_native_prefix()} if prefix]
    output_root = str(Path(OUTPUT_DIR))

    normalized_value = value.rstrip("/\\")
    output_norm = output_root.rstrip("/\\")
    for prefix in host_prefixes:
        normalized_prefix = prefix.rstrip("/\\")
        if not normalized_prefix:
            continue
        if "\\" in normalized_prefix:
            candidate_cmp = normalized_value.lower()
            prefix_cmp = normalized_prefix.lower()
        else:
            candidate_cmp = normalized_value
            prefix_cmp = normalized_prefix
        if candidate_cmp == prefix_cmp:
            return output_norm
        separator = "\\" if "\\" in normalized_prefix else "/"
        if candidate_cmp.startswith(prefix_cmp + separator):
            remainder = normalized_value[len(normalized_prefix):].lstrip("/\\")
            if not remainder:
                return output_norm
            return str(Path(output_root) / Path(remainder))
    return value


def _translate_container_path_to_host(target: str) -> str | None:
    """Map ``target`` (a container-side path under ``OUTPUT_DIR``) to the
    host-native form using ``HOST_OUTPUT_DIR_NATIVE`` when set, or auto-derived
    from ``HOST_OUTPUT_DIR`` for WSL mounts.

    Returns None if no translation is available or the path is not under
    ``OUTPUT_DIR``. The host-native form is returned verbatim — callers should
    treat it as opaque (it may be a Windows-style path like ``C:\\Users\\…``
    or a WSL UNC path like ``\\\\wsl.localhost\\Ubuntu\\…``).
    """
    host_prefix = _resolve_host_native_prefix()
    if not host_prefix:
        return None
    output_root = str(Path(OUTPUT_DIR)).rstrip("/")
    if not output_root:
        return None
    if not target.startswith(output_root):
        return None
    remainder = target[len(output_root):].lstrip("/")
    host_root = host_prefix.rstrip("/").rstrip("\\")
    if not remainder:
        return host_root
    if "\\" in host_root:
        return host_root + "\\" + remainder.replace("/", "\\")
    return host_root + "/" + remainder


def _try_open_with_host_tool(target: str) -> bool:
    """Attempt to hand ``target`` to the host's file manager / default opener.

    Returns True only if we're reasonably sure a GUI launcher exists. Inside a
    Docker container we skip the launch path entirely — the callers fall back
    to surfacing the translated host path (via ``HOST_OUTPUT_DIR_NATIVE``) so
    the desktop reader can copy it to clipboard for the user to paste into
    their host file manager.
    """
    if _running_in_docker():
        return False

    candidates: list[list[str]] = []
    if sys.platform == "darwin":
        candidates.append(["open", target])
    elif sys.platform.startswith("win"):
        candidates.append(["explorer", target])
    else:
        for cmd in ("wslview", "gio", "gnome-open", "kde-open", "xdg-open"):
            if cmd == "gio":
                candidates.append([cmd, "open", target])
            else:
                candidates.append([cmd, target])
        candidates.append(["explorer.exe", target])
    for argv in candidates:
        if not shutil.which(argv[0]):
            continue
        try:
            subprocess.Popen(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return True
        except Exception:
            continue
    return False


@app.route("/desktop/reveal", methods=["POST"])
def desktop_reveal():
    """Reveal the bundle directory for a document in the host's file manager.

    When the backend is running inside Docker no GUI opener is available, so
    the endpoint also returns ``directoryPath`` and ``launched=False`` for the
    caller to display to the user (e.g. copy-to-clipboard fallback).
    """
    data = request.get_json(silent=True) or {}
    raw_path = (data.get("path") or data.get("articlePath") or "").strip()
    if not raw_path:
        return jsonify(success=False, message="Missing path"), 400
    target = Path(raw_path)
    if not target.exists():
        return jsonify(success=False, message=f"Path not found: {target}"), 404
    directory = target if target.is_dir() else target.parent
    should_launch = bool(data.get("launch", True))
    launched = _try_open_with_host_tool(str(directory)) if should_launch else False
    host_directory = _translate_container_path_to_host(str(directory))
    return jsonify(
        success=True,
        directoryPath=str(directory),
        hostDirectoryPath=host_directory,
        launched=launched,
    )


@app.route("/desktop/open_external", methods=["POST"])
def desktop_open_external():
    """Open a local file or a web URL via the host's default handler.

    Mirrors ``/desktop/reveal`` but targets a file instead of its parent
    directory. For web URLs the frontend is expected to open a new tab
    directly — this endpoint only handles filesystem targets.
    """
    data = request.get_json(silent=True) or {}
    raw_path = (data.get("path") or "").strip()
    if not raw_path:
        return jsonify(success=False, message="Missing path"), 400
    target = Path(raw_path)
    if not target.exists():
        return jsonify(success=False, message=f"Path not found: {target}"), 404
    launched = _try_open_with_host_tool(str(target))
    host_path = _translate_container_path_to_host(str(target))
    return jsonify(
        success=True,
        path=str(target),
        hostPath=host_path,
        launched=launched,
    )


def _session_state_path() -> Path:
    return Path(OUTPUT_DIR) / ".session.json"


def _read_session_state() -> dict:
    path = _session_state_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_session_state(state: dict) -> None:
    path = _session_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


@app.route("/desktop/session", methods=["GET", "POST"])
def desktop_session():
    """Read or write the desktop reader's current session state.

    The reader POSTs its open tabs / focused document whenever state changes so
    MCP and CLI consumers can ask ``scribe context`` for links to what the user
    is currently reading. State is persisted to ``<OUTPUT_DIR>/.session.json``
    so it survives backend restarts.
    """
    if request.method == "GET":
        return jsonify(success=True, session=_read_session_state())

    data = request.get_json(silent=True) or {}
    allowed_keys = {
        "openDocumentPaths",
        "focusedDocumentPath",
        "labelFilter",
        "focusedHighlightId",
        "updatedAt",
    }
    state: dict = {}
    for key in allowed_keys:
        if key in data:
            state[key] = data[key]
    state["updatedAt"] = datetime.now(timezone.utc).isoformat()

    focused = state.get("focusedDocumentPath")
    if isinstance(focused, str) and focused:
        focused_path = Path(focused)
        if focused_path.exists():
            highlights = _load_highlights(focused_path)
            state["focusedHighlightCount"] = len(highlights)
            notes_path = _sibling_with_suffix_if_exists(focused_path, ".notes.md")
            state["focusedNotesPath"] = str(notes_path) if notes_path else None

    _write_session_state(state)
    _publish_desktop_event("session_updated", state.get("focusedDocumentPath"))
    return jsonify(success=True, session=state)


@app.route("/desktop/events", methods=["GET"])
def desktop_events():
    """Server-Sent Events stream for desktop reader UI updates.

    Publishes one event per mutating /desktop/* write (notes, highlights,
    rating, document, related, session). Subscribers receive the ``ready``
    event on connect and a periodic ``: keepalive`` comment every 25s so
    proxies don't drop the connection. Event payload is JSON
    ``{type, articlePath, ts, ...extras}``.
    """

    def stream():
        q = _event_hub.subscribe()
        try:
            yield "event: ready\ndata: {}\n\n"
            while True:
                try:
                    event = q.get(timeout=25.0)
                except _queue.Empty:
                    yield ": keepalive\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        finally:
            _event_hub.unsubscribe(q)

    return Response(
        stream_with_context(stream()),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000, threaded=True)

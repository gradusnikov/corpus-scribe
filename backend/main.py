"""Send-to-Scribe backend: extract articles, generate PDF/Markdown, optionally send to Kindle."""

import json
import logging
import os
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS
from pathvalidate import sanitize_filename
import pypandoc

from article_extractor import (
    _build_frontmatter,
    _generate_companion_notes,
    _notes_doc_id,
    extract_article,
    extract_pdf_url,
)
from send_email_gmail import send_to_kindle

app = Flask(__name__)
CORS(app, support_credentials=True)
logging.basicConfig(level=logging.DEBUG)

API_KEY = os.environ.get("API_KEY", "api-key-1234")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
TEMP_DIR = "/tmp/scribe"
DESKTOP_API_ROOT = os.environ.get("DESKTOP_API_ROOT", OUTPUT_DIR)


def _pdf_ocr_available() -> bool:
    return bool(os.environ.get("MISTRAL_API_KEY", "").strip())


def _check_api_key(data: dict) -> bool:
    return data.get("apiKey") == API_KEY


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
    return Path(value).expanduser()


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


def _stable_doc_id(path: Path) -> str:
    return path.as_posix().lower()


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
        documents.append(
            {
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
                "sourceSite": _frontmatter_string(frontmatter, "source_site"),
                "ingestedAt": _frontmatter_string(frontmatter, "ingested_at"),
                "rating": rating,
                "url": _frontmatter_string(frontmatter, "url"),
                "canonicalUrl": _frontmatter_string(frontmatter, "canonical_url"),
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

    return {
        "removedFiles": removed_files,
        "removedDirs": removed_dirs,
        "removedIndexRecords": removed_index,
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


def _generate_existing_notes(article_path: Path) -> dict:
    article_text = article_path.read_text(encoding="utf-8")
    article_frontmatter, article_body = _split_frontmatter(article_text)
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
            "notes_config": {},
        },
    )
    article_frontmatter["notes_file"] = notes_file.name
    if article_frontmatter.get("doc_id"):
        article_frontmatter["notes_doc_id"] = _notes_doc_id(str(article_frontmatter["doc_id"]))
    _write_article_frontmatter(article_path, article_frontmatter, article_body)

    payload = _build_existing_article_payload(article_path)
    _upsert_index_records(_build_index_records(payload, payload["label"]))
    return payload


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

    metadata = extract_article(
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
    metadata = extract_article(
        html=data["html"],
        output_dir=str(target_dir),
        cookies=data.get("cookies"),
        url=url,
        page_size=page_size,
        label=_clean_label(label),
        render_pdf=True,
        pdf_required=False,
        generate_notes=True,
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
        "notesDocId": metadata.get("notes-doc-id"),
        "metadata": metadata.get("metadata", {}),
    }
    _upsert_index_records(_build_index_records(payload, _clean_label(label)))

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
        generate_notes=True,
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
        "notesDocId": metadata.get("notes-doc-id"),
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

    return jsonify(
        success=True,
        scanned=scanned,
        records=len(ordered),
        errors=errors,
        indexPath=str(index_path),
    )


@app.route("/desktop/search", methods=["POST"])
def desktop_search():
    data = request.get_json(silent=True) or {}
    root = _resolve_library_root(data.get("root"))
    query = (data.get("query") or "").strip().lower()
    label = (data.get("label") or "").strip()
    if not root.exists():
        return jsonify(success=False, message=f"Corpus root does not exist: {root}"), 404
    if not query:
        return jsonify(success=True, documents=[])

    results: list[dict] = []
    for doc in _scan_library_documents(root):
        if label and label != "all" and (doc.get("label") or "") != label:
            continue
        haystacks = [doc.get("title") or "", doc.get("excerpt") or ""]
        article_path = Path(doc["articlePath"])
        try:
            haystacks.append(_read_markdown_body(article_path))
        except Exception:
            pass
        if doc.get("notesPath"):
            try:
                haystacks.append(_read_markdown_body(Path(doc["notesPath"])))
            except Exception:
                pass
        if doc.get("highlightsPath"):
            try:
                haystacks.append("\n".join(item["text"] for item in _load_highlights(article_path)))
            except Exception:
                pass
        joined = "\n".join(haystacks).lower()
        if query in joined:
            results.append(doc)

    return jsonify(success=True, documents=results[:200])


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
    rating_value = _frontmatter_int(frontmatter, "rating")
    rating = max(0, min(5, rating_value)) if isinstance(rating_value, int) else 0
    summary = {
        "id": _stable_doc_id(path),
        "title": _frontmatter_string(frontmatter, "title") or path.stem,
        "label": _frontmatter_string(frontmatter, "label"),
        "articlePath": str(path),
        "notesPath": str(_sibling_with_suffix_if_exists(path, ".notes.md")) if _sibling_with_suffix_if_exists(path, ".notes.md") else None,
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

    return jsonify(
        success=True,
        detail={
            "summary": summary,
            "markdown": body,
            "notesMarkdown": notes_markdown,
            "highlights": highlights,
            "bibliography": bibliography,
            "frontmatter": frontmatter,
        },
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

    article_frontmatter["notes_file"] = notes_path.name
    if _frontmatter_string(article_frontmatter, "doc_id"):
        article_frontmatter["notes_doc_id"] = _notes_doc_id(_frontmatter_string(article_frontmatter, "doc_id"))
    _write_article_frontmatter(article_path, article_frontmatter, article_body)

    payload = _build_existing_article_payload(article_path)
    _upsert_index_records(_build_index_records(payload, payload["label"]))
    return jsonify(success=True, notesPath=str(notes_path), notesMarkdown=notes_markdown)


@app.route("/desktop/notes/generate", methods=["POST"])
def desktop_generate_notes():
    data = request.get_json(silent=True) or {}
    article_path = Path((data.get("articlePath") or "").strip())
    if not article_path:
        return jsonify(success=False, message="Missing articlePath"), 400
    if not article_path.exists():
        return jsonify(success=False, message=f"Document not found: {article_path}"), 404

    try:
        payload = _generate_existing_notes(article_path)
    except Exception as error:
        return jsonify(success=False, message=str(error)), 500

    notes_markdown = _read_markdown_body(Path(payload["notes"])) if payload.get("notes") else ""
    return jsonify(
        success=True,
        notesPath=payload.get("notes"),
        notesMarkdown=notes_markdown,
        notesDocId=payload.get("notesDocId"),
    )


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
    return jsonify(success=True, highlightsPath=str(highlights_path), highlights=cleaned)


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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

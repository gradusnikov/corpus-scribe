"""Send-to-Scribe backend: extract articles, generate PDF/Markdown, optionally send to Kindle."""

import json
import logging
import os
from pathlib import Path

from flask import Flask, jsonify, request
from flask_cors import CORS
from pathvalidate import sanitize_filename

from article_extractor import extract_article, extract_pdf_url
from send_email_gmail import send_to_kindle

app = Flask(__name__)
CORS(app, support_credentials=True)
logging.basicConfig(level=logging.DEBUG)

API_KEY = os.environ.get("API_KEY", "api-key-1234")
OUTPUT_DIR = os.environ.get("OUTPUT_DIR", "/output")
TEMP_DIR = "/tmp/scribe"


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


if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)

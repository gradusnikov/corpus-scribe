#!/usr/bin/env python3

from __future__ import annotations

import sys
from pathlib import Path

from article_extractor import _build_frontmatter, _generate_companion_notes, _notes_doc_id
from main import (
    _relative_to_output,
    _sibling_if_exists,
    _sibling_with_suffix_if_exists,
    _split_frontmatter,
    _upsert_index_records,
)


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: backfill_notes.py /path/to/article.md", file=sys.stderr)
        return 2

    article_path = Path(sys.argv[1]).expanduser()
    if not article_path.exists():
        print(f"Article not found: {article_path}", file=sys.stderr)
        return 1

    text = article_path.read_text(encoding="utf-8")
    frontmatter, body = _split_frontmatter(text)
    article_doc_id = frontmatter.get("doc_id")
    if not isinstance(article_doc_id, str) or not article_doc_id.strip():
        print(f"Missing doc_id in article frontmatter: {article_path}", file=sys.stderr)
        return 1

    notes_file = _generate_companion_notes(
        md_text=text,
        article_dir=article_path.parent,
        article_basename=article_path.stem,
        metadata={
            "title": frontmatter.get("title") or article_path.stem,
            "article_doc_id": article_doc_id,
            "url": frontmatter.get("url"),
            "canonical_url": frontmatter.get("canonical_url"),
            "source_site": frontmatter.get("source_site"),
            "label": frontmatter.get("label"),
            "language": frontmatter.get("language"),
            "ingested_at": frontmatter.get("ingested_at"),
            "notes_config": {},
        },
    )
    notes_doc_id = _notes_doc_id(article_doc_id)

    frontmatter["notes_file"] = notes_file.name
    frontmatter["notes_doc_id"] = notes_doc_id
    article_path.write_text(_build_frontmatter(frontmatter) + "\n\n" + body.lstrip("\n"), encoding="utf-8")

    pdf_path = _sibling_with_suffix_if_exists(article_path, ".reading.pdf") or _sibling_if_exists(article_path, "pdf")
    source_pdf_path = _sibling_with_suffix_if_exists(article_path, ".source.pdf")
    bib_path = _sibling_if_exists(article_path, "bib")

    records = [
        {
            "doc_id": frontmatter.get("doc_id"),
            "type": "article",
            "title": frontmatter.get("title") or article_path.stem,
            "label": frontmatter.get("label") or None,
            "path": _relative_to_output(str(article_path)),
            "pdf_path": _relative_to_output(str(pdf_path)) if pdf_path else None,
            "source_pdf_path": _relative_to_output(str(source_pdf_path)) if source_pdf_path else None,
            "bib_path": _relative_to_output(str(bib_path)) if bib_path else None,
            "notes_path": _relative_to_output(str(notes_file)),
            "url": frontmatter.get("url"),
            "canonical_url": frontmatter.get("canonical_url"),
            "source_site": frontmatter.get("source_site"),
            "source_format": frontmatter.get("source_format"),
            "ocr_engine": frontmatter.get("ocr_engine"),
            "citation_key": frontmatter.get("citation_key"),
            "doi": frontmatter.get("doi"),
            "arxiv_id": frontmatter.get("arxiv_id"),
            "page_count": frontmatter.get("page_count"),
            "language": frontmatter.get("language"),
            "word_count": frontmatter.get("word_count"),
            "image_count": frontmatter.get("image_count"),
            "ingested_at": frontmatter.get("ingested_at"),
        },
        {
            "doc_id": notes_doc_id,
            "type": "notes",
            "title": f"{frontmatter.get('title') or article_path.stem} Notes",
            "label": frontmatter.get("label") or None,
            "path": _relative_to_output(str(notes_file)),
            "source_article_path": _relative_to_output(str(article_path)),
            "source_doc_id": frontmatter.get("doc_id"),
            "url": frontmatter.get("url"),
            "canonical_url": frontmatter.get("canonical_url"),
            "source_site": frontmatter.get("source_site"),
            "source_format": frontmatter.get("source_format"),
            "ocr_engine": frontmatter.get("ocr_engine"),
            "citation_key": frontmatter.get("citation_key"),
            "language": frontmatter.get("language"),
            "ingested_at": frontmatter.get("ingested_at"),
        },
    ]
    _upsert_index_records(records)

    print(notes_file)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

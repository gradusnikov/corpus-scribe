"""Extract article content from HTML or PDF, generate Markdown, and optionally PDF/notes."""

import html
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
import uuid
import base64
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import pypandoc
import requests
from bs4 import BeautifulSoup, Comment, NavigableString
from PIL import Image
from readability import Document

log = logging.getLogger(__name__)

_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Safari/537.36"
)


def _make_session(cookies: dict | None = None) -> requests.Session:
    """Build a requests session with UA header and flattened cookies.

    Cookies arrive as {domain: {name: value, ...}, ...} from the Chrome
    extension.  We flatten them into a single Cookie header string so they
    get sent to every request (including CDN subdomains like miro.medium.com).
    """
    s = requests.Session()
    s.headers["User-Agent"] = _UA
    if cookies:
        pairs = []
        for domain_cookies in cookies.values():
            if isinstance(domain_cookies, dict):
                for name, value in domain_cookies.items():
                    pairs.append(f"{name}={value}")
        if pairs:
            s.headers["Cookie"] = "; ".join(pairs)
            log.debug("Session has %d cookie pairs", len(pairs))
    return s


_PAGE_SIZES = {
    "a4": {"papersize": "a4", "max_img_width": 670},   # 210mm - 2×10mm = 190mm ≈ 670px
    "a5": {"papersize": "a5", "max_img_width": 484},   # 148mm - 2×10mm = 128mm ≈ 484px
}
_MAX_OUTPUT_NAME_LEN = 96
_DEFAULT_NOTES_PROVIDER = os.environ.get("NOTES_LLM_PROVIDER", "anthropic")
_DEFAULT_NOTES_BASE_URL = os.environ.get("NOTES_LLM_BASE_URL", "http://172.24.208.1:1234/v1")
_DEFAULT_NOTES_MODEL = os.environ.get("NOTES_LLM_MODEL", "claude-sonnet-4-20250514")
_DEFAULT_NOTES_API_KEY = os.environ.get("NOTES_LLM_API_KEY", "")
_DEFAULT_NOTES_TIMEOUT = int(os.environ.get("NOTES_LLM_TIMEOUT", "120"))
_DEFAULT_ANTHROPIC_VERSION = os.environ.get("NOTES_ANTHROPIC_VERSION", "2023-06-01")
_DEFAULT_MISTRAL_API_KEY = os.environ.get("MISTRAL_API_KEY", "")
_DEFAULT_MISTRAL_BASE_URL = os.environ.get("MISTRAL_BASE_URL", "https://api.mistral.ai")
_DEFAULT_MISTRAL_OCR_MODEL = os.environ.get("MISTRAL_OCR_MODEL", "mistral-ocr-latest")


def _safe_output_name(title: str) -> str:
    """Build a readable, filesystem-safe article name with a stable max length."""
    from pathvalidate import sanitize_filename

    ascii_title = title.encode("ascii", "ignore").decode("ascii").strip()
    safe_name = sanitize_filename(ascii_title) or str(uuid.uuid4())
    safe_name = " ".join(safe_name.split())

    if len(safe_name) <= _MAX_OUTPUT_NAME_LEN:
        return safe_name

    digest = hashlib.sha1(title.encode("utf-8")).hexdigest()[:10]
    head = safe_name[: _MAX_OUTPUT_NAME_LEN - len(digest) - 1].rstrip(" .-_")
    return f"{head}-{digest}"


def _ensure_article_dir(output_dir: str | Path, safe_name: str, uniqueness_basis: str) -> Path:
    base_dir = Path(output_dir)
    article_dir = base_dir / safe_name
    if article_dir.exists() and not article_dir.is_dir():
        digest = hashlib.sha1(uniqueness_basis.encode("utf-8")).hexdigest()[:10]
        article_dir = base_dir / _safe_output_name(f"{safe_name}-{digest}")
    article_dir.mkdir(parents=True, exist_ok=True)
    return article_dir


def extract_article(html: str, output_dir: str = "/tmp", cookies: dict | None = None,
                    url: str = "", page_size: str = "a5", label: str = "",
                    render_pdf: bool = True, pdf_required: bool = True,
                    generate_notes: bool = False, notes_config: dict | None = None) -> dict:
    """Extract article from raw HTML into output_dir/{safe_title}/.

    Returns dict with title, dir, file-path (pdf, optional), md-path, md-text.
    Images are saved to {dir}/assets/ with relative references in MD.
    """
    page_cfg = _PAGE_SIZES.get(page_size.lower(), _PAGE_SIZES["a5"])
    session = _make_session(cookies)

    # Extract readable content and metadata
    log.debug("Input HTML size: %d chars", len(html))
    doc = Document(html)
    title = doc.title()
    meta = _extract_meta(html, title, url)
    meta = _enrich_meta_with_doi(meta)
    title = meta["title"]

    # Try <article> tag first (works much better for Medium, Substack, etc.)
    # Fall back to readability if <article> extraction is too short
    content_html = _extract_article_tag(html)
    if content_html:
        log.debug("Extracted <article> tag: %d chars", len(content_html))
    if not content_html or len(content_html) < 500:
        content_html = doc.summary()
        log.debug("Readability output: %d chars", len(content_html))

    # Build per-article output directory with bounded path length so
    # Windows apps opening files via \\wsl$ can still resolve assets.
    safe_name = _safe_output_name(title)
    article_dir = _ensure_article_dir(output_dir, safe_name, meta.get("canonical_url") or meta.get("url") or title)
    assets_dir = article_dir / "assets"
    if assets_dir.exists():
        shutil.rmtree(assets_dir)
    assets_dir.mkdir(parents=True, exist_ok=True)

    # Collect images from original HTML BEFORE readability strips them
    original_images = _collect_images(html, url)

    # Re-inject images that readability may have stripped
    soup = BeautifulSoup(content_html, "html.parser")
    soup = _reinject_images(soup, original_images)

    # Flatten <picture> elements: promote best <source> srcset into <img> src
    for picture in soup.find_all("picture"):
        img = picture.find("img")
        if not img:
            continue
        source = picture.find("source")
        if source and source.get("srcset") and not img.get("src"):
            img["src"] = _best_srcset_url(source["srcset"])
        # Unwrap <picture>, keep the <img>
        picture.unwrap()

    # Handle srcset on <img> tags — use highest-res URL as src fallback
    for img in soup.find_all("img"):
        if not img.get("src") and img.get("srcset"):
            img["src"] = _best_srcset_url(img["srcset"])

    _remove_latexml_figure_placeholders(soup)

    # Download images into assets/, rewrite paths
    for img in soup.find_all("img"):
        src = img.get("src")
        if not src:
            continue
        # Resolve relative URLs
        abs_src = urljoin(url, src) if url else src
        local_path = _download_image(abs_src, assets_dir, session)
        if local_path:
            img["src"] = str(local_path)
            img["data-rel-src"] = f"assets/{local_path.name}"
        else:
            # Download failed — keep the absolute URL so it still renders
            img["src"] = abs_src

    _normalize_labeled_formula_blocks(soup)

    # PubMed/PMC often wrap display equations in a 2-column table
    # (formula + equation label). Normalize those wrappers before converting.
    _normalize_display_formula_tables(soup)
    _normalize_latexml_equation_tables(soup)

    # Inject server-side MathML to replace MathJax-rendered <mjx-container>
    if url and soup.find("mjx-container"):
        _fetch_and_inject_mathml(soup, url, session)

    _normalize_code_listing_tables(soup)

    # Keep real MathML intact so Pandoc can convert it natively.
    # Only fall back to our custom converter for leftover MathJax CHTML.
    _convert_mathjax_fallback(soup)
    tex_placeholders = _replace_problem_math_with_tex_placeholders(soup)
    _separate_inline_math_from_text(soup)
    _remove_html_comments(soup)
    content_html = str(soup)

    # --- Generate Markdown (with relative image paths) ---
    md_soup = BeautifulSoup(content_html, "html.parser")
    for img in md_soup.find_all("img"):
        rel = img.get("data-rel-src")
        if rel:
            img["src"] = rel
            del img["data-rel-src"]
    _prepare_html_for_markdown(md_soup)

    md_html = str(md_soup)
    md_body = _convert_html_to_markdown(md_html)
    md_body = _restore_tex_placeholders(md_body, tex_placeholders, target="markdown")
    md_body = _postprocess_markdown(md_body)

    ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    clean_text = md_soup.get_text(" ", strip=True)
    article_doc_id = _stable_doc_id(meta["title"], meta.get("canonical_url") or meta.get("url"))
    article_fields = {
        "title": meta["title"],
        "doc_id": article_doc_id,
        "doc_type": "article",
        "author": meta.get("author"),
        "date": meta.get("date"),
        "url": meta.get("url"),
        "canonical_url": meta.get("canonical_url"),
        "source_site": meta.get("source_site"),
        "label": label.strip() or None,
        "language": meta.get("language"),
        "description": meta.get("description"),
        "word_count": _count_words(clean_text),
        "image_count": len([img for img in md_soup.find_all("img") if img.get("src")]),
        "ingested_at": ingested_at,
    }
    citation_metadata = _derive_citation_metadata(
        title=meta["title"],
        author=meta.get("author"),
        date=meta.get("date"),
        url=meta.get("url"),
        canonical_url=meta.get("canonical_url"),
        source_site=meta.get("source_site"),
        description=meta.get("description"),
        doc_id=article_doc_id,
        doi=meta.get("doi"),
        container_title=meta.get("container_title"),
        publisher=meta.get("publisher"),
        volume=meta.get("volume"),
        issue=meta.get("issue"),
        pages=meta.get("pages"),
    )
    article_fields.update(citation_metadata["frontmatter"])
    md_file = article_dir / f"{safe_name}.md"
    md_text = _build_frontmatter(article_fields) + "\n\n" + md_body
    md_file.write_text(md_text, encoding="utf-8")
    bib_file = _write_bibliography_file(article_dir, safe_name, citation_metadata["bibtex"])
    article_fields["bib_file"] = bib_file.name
    md_text = _build_frontmatter(article_fields) + "\n\n" + md_body
    md_file.write_text(md_text, encoding="utf-8")

    notes_file = None
    notes_doc_id = None
    notes_metadata = {
        "doc_id": article_doc_id,
        "label": label.strip() or None,
        "source_site": meta.get("source_site"),
        "language": meta.get("language"),
        "canonical_url": meta.get("canonical_url"),
        "url": meta.get("url"),
        "title": meta["title"],
        "doc_type": "article",
        "word_count": _count_words(clean_text),
        "image_count": len([img for img in md_soup.find_all("img") if img.get("src")]),
        "ingested_at": ingested_at,
        "citation_key": citation_metadata["citation_key"],
        "bib_path": str(bib_file),
        "doi": citation_metadata["frontmatter"].get("doi"),
        "arxiv_id": citation_metadata["frontmatter"].get("arxiv_id"),
    }

    if generate_notes:
        try:
            notes_file = _generate_companion_notes(
                md_text=md_text,
                article_dir=article_dir,
                article_basename=safe_name,
                metadata={
                    "title": meta["title"],
                    "article_doc_id": article_doc_id,
                    "url": meta.get("url"),
                    "canonical_url": meta.get("canonical_url"),
                    "source_site": meta.get("source_site"),
                    "label": label.strip() or None,
                    "language": meta.get("language"),
                    "ingested_at": ingested_at,
                    "notes_config": notes_config or {},
                },
            )
            notes_doc_id = _notes_doc_id(article_doc_id)
        except Exception:
            log.exception("Companion notes generation failed for %s", title)

    if notes_file:
        article_fields["notes_file"] = notes_file.name
        article_fields["notes_doc_id"] = notes_doc_id
        md_text = _build_frontmatter(article_fields) + "\n\n" + md_body
        md_file.write_text(md_text, encoding="utf-8")

    # --- Generate PDF from the final markdown using a single browser renderer ---
    pdf_file = None
    if render_pdf:
        try:
            _resize_images_for_pdf(assets_dir, max_width=page_cfg["max_img_width"])
            pdf_file = _generate_pdf(md_text, title, article_dir, page_cfg["papersize"])
        except Exception:
            if pdf_required:
                raise
            log.exception("PDF generation failed for %s; markdown was still saved", title)

    return {
        "title": title,
        "dir": str(article_dir),
        "file-path": str(pdf_file) if pdf_file else None,
        "md-path": str(md_file),
        "md-text": md_text,
        "bib-path": str(bib_file),
        "notes-path": str(notes_file) if notes_file else None,
        "notes-doc-id": notes_doc_id,
        "metadata": notes_metadata,
    }


def extract_pdf_url(url: str, output_dir: str = "/tmp", cookies: dict | None = None,
                    source_name: str = "", label: str = "", page_size: str = "a5",
                    generate_notes: bool = False, notes_config: dict | None = None) -> dict:
    """Download a source PDF and extract it into markdown-first local output."""
    session = _make_session(cookies)
    response = session.get(url, timeout=120)
    response.raise_for_status()

    content_type = (response.headers.get("Content-Type") or "").lower()
    if "pdf" not in content_type and not _is_likely_pdf_url(url):
        raise RuntimeError(f"URL did not resolve to a PDF: {url}")

    return extract_pdf_bytes(
        pdf_bytes=response.content,
        output_dir=output_dir,
        url=url,
        source_name=source_name or _pdf_source_name_from_url(url),
        label=label,
        page_size=page_size,
        generate_notes=generate_notes,
        notes_config=notes_config,
    )


def extract_pdf_bytes(pdf_bytes: bytes, output_dir: str = "/tmp", url: str = "",
                      source_name: str = "", label: str = "", page_size: str = "a5",
                      generate_notes: bool = False, notes_config: dict | None = None) -> dict:
    """Extract markdown and notes from a source PDF."""
    page_cfg = _PAGE_SIZES.get(page_size.lower(), _PAGE_SIZES["a5"])
    with tempfile.TemporaryDirectory(prefix="scribe-pdf-src-") as tmpdir:
        temp_pdf = Path(tmpdir) / (source_name or "document.pdf")
        temp_pdf.write_bytes(pdf_bytes)

        pdf_meta = _extract_pdf_metadata(temp_pdf)
        title = _choose_pdf_title(pdf_meta.get("title"), source_name, url)
        safe_name = _safe_output_name(title)

        article_dir = _ensure_article_dir(output_dir, safe_name, url or source_name or title)
        source_pdf = article_dir / f"{safe_name}.source.pdf"
        source_pdf.write_bytes(pdf_bytes)
        assets_dir = article_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)

        md_body = None
        ocr_response = None
        mistral_error = None
        try:
            md_body, ocr_response = _extract_pdf_markdown_via_mistral(
                pdf_bytes=pdf_bytes,
                article_dir=article_dir,
                safe_name=safe_name,
            )
        except Exception as exc:
            mistral_error = str(exc)
            log.exception("Mistral OCR failed for PDF %s; falling back to pdftotext", title)

        if not md_body:
            text = _extract_pdf_text(temp_pdf)
            md_body = _pdf_text_to_markdown(text)
        else:
            md_body = _postprocess_pdf_markdown(_sanitize_unicode_text(md_body))

        if not ocr_response:
            md_body = _postprocess_pdf_markdown(md_body)

        ingested_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        article_doc_id = _stable_doc_id(title, url or source_name or title)

        article_fields = {
            "title": title,
            "doc_id": article_doc_id,
            "doc_type": "article",
            "url": url or None,
            "canonical_url": url or None,
            "source_site": urlparse(url).netloc.lower() if url else None,
            "label": label.strip() or None,
            "language": "en",
            "source_format": "pdf",
            "source_file": source_name or article_pdf.name,
            "ocr_engine": "mistral" if ocr_response else "pdftotext",
            "page_count": pdf_meta.get("pages"),
            "word_count": _count_words(md_body),
            "image_count": len(list(assets_dir.glob("*"))),
            "ingested_at": ingested_at,
        }
        citation_metadata = _derive_citation_metadata(
            title=title,
            author=pdf_meta.get("author"),
            date=pdf_meta.get("creationdate"),
            url=url or None,
            canonical_url=url or None,
            source_site=urlparse(url).netloc.lower() if url else None,
            description=None,
            doc_id=article_doc_id,
            pages=str(pdf_meta.get("pages")) if pdf_meta.get("pages") else None,
        )
        article_fields.update(citation_metadata["frontmatter"])
        if mistral_error and not ocr_response:
            article_fields["ocr_fallback"] = "pdftotext"
        md_file = article_dir / f"{safe_name}.md"
        md_text = _build_frontmatter(article_fields) + "\n\n" + md_body.strip() + "\n"
        md_file.write_text(md_text, encoding="utf-8")
        bib_file = _write_bibliography_file(article_dir, safe_name, citation_metadata["bibtex"])
        article_fields["bib_file"] = bib_file.name
        md_text = _build_frontmatter(article_fields) + "\n\n" + md_body.strip() + "\n"
        md_file.write_text(md_text, encoding="utf-8")

        reading_pdf = None
        try:
            _resize_images_for_pdf(assets_dir, max_width=page_cfg["max_img_width"])
            generated_pdf = _generate_pdf(md_text, title, article_dir, page_cfg["papersize"])
            reading_pdf = article_dir / f"{safe_name}.reading.pdf"
            if generated_pdf != reading_pdf:
                generated_pdf.replace(reading_pdf)
        except Exception:
            log.exception("Reading PDF generation failed for source PDF %s; source PDF was still saved", title)

        notes_file = None
        notes_doc_id = None
        metadata = {
            "doc_id": article_doc_id,
            "label": label.strip() or None,
            "source_site": article_fields["source_site"],
            "language": article_fields["language"],
            "canonical_url": article_fields["canonical_url"],
            "url": article_fields["url"],
            "title": title,
            "doc_type": "article",
            "source_format": "pdf",
            "ocr_engine": article_fields["ocr_engine"],
            "source_pdf": str(source_pdf),
            "reading_pdf": str(reading_pdf) if reading_pdf else None,
            "page_count": article_fields["page_count"],
            "word_count": article_fields["word_count"],
            "image_count": article_fields["image_count"],
            "ingested_at": ingested_at,
            "citation_key": citation_metadata["citation_key"],
            "bib_path": str(bib_file),
            "doi": citation_metadata["frontmatter"].get("doi"),
            "arxiv_id": citation_metadata["frontmatter"].get("arxiv_id"),
        }

        if generate_notes:
            try:
                notes_file = _generate_companion_notes(
                    md_text=md_text,
                    article_dir=article_dir,
                    article_basename=safe_name,
                    metadata={
                        "title": title,
                        "article_doc_id": article_doc_id,
                        "url": article_fields["url"],
                        "canonical_url": article_fields["canonical_url"],
                        "source_site": article_fields["source_site"],
                        "label": label.strip() or None,
                        "language": article_fields["language"],
                        "ingested_at": ingested_at,
                        "notes_config": notes_config or {},
                    },
                )
                notes_doc_id = _notes_doc_id(article_doc_id)
            except Exception:
                log.exception("Companion notes generation failed for PDF %s", title)

        if notes_file:
            article_fields["notes_file"] = notes_file.name
            article_fields["notes_doc_id"] = notes_doc_id
            md_text = _build_frontmatter(article_fields) + "\n\n" + md_body.strip() + "\n"
            md_file.write_text(md_text, encoding="utf-8")

        return {
            "title": title,
            "dir": str(article_dir),
            "file-path": str(reading_pdf) if reading_pdf else None,
            "source-pdf-path": str(source_pdf),
            "md-path": str(md_file),
            "md-text": md_text,
            "bib-path": str(bib_file),
            "notes-path": str(notes_file) if notes_file else None,
            "notes-doc-id": notes_doc_id,
            "metadata": metadata,
        }


def _extract_meta(html: str, title: str, url: str) -> dict:
    """Pull author, date, description from HTML meta tags."""
    soup = BeautifulSoup(html, "html.parser")
    meta = {
        "title": title,
        "url": url,
        "source_site": urlparse(url).netloc.lower() if url else None,
    }

    def meta_values(*attrs: str) -> list[str]:
        values = []
        for attr in attrs:
            tags = soup.find_all("meta", attrs={"name": attr}) + soup.find_all("meta", attrs={"property": attr})
            for tag in tags:
                content = (tag.get("content") or "").strip()
                if content:
                    values.append(content)
        deduped = []
        seen = set()
        for value in values:
            if value not in seen:
                deduped.append(value)
                seen.add(value)
        return deduped

    def first_meta_value(*attrs: str) -> str | None:
        values = meta_values(*attrs)
        return values[0] if values else None

    def normalize_partial_date(raw: str | None) -> str | None:
        if not raw:
            return None
        raw = raw.strip()
        match = re.search(r"(\d{4})[-/](\d{1,2})(?:[-/](\d{1,2}))?", raw)
        if match:
            year, month, day = match.group(1), match.group(2), match.group(3)
            if day:
                return f"{year}-{int(month):02d}-{int(day):02d}"
            return f"{year}-{int(month):02d}"
        year_match = re.search(r"\b(19|20)\d{2}\b", raw)
        if year_match:
            return year_match.group(0)
        return None

    title_override = first_meta_value("citation_title", "og:title", "dc.title")
    if title_override:
        meta["title"] = title_override

    # Author: try scholarly meta tags first, then generic tags.
    author_values = meta_values(
        "citation_author",
        "author",
        "article:author",
        "dc.creator",
        "dc.creator.author",
        "parsely-author",
        "sailthru.author",
    )
    if author_values:
        meta["author"] = " and ".join(author_values)

    # Date: try common meta tags
    date_value = first_meta_value(
        "citation_publication_date",
        "citation_online_date",
        "article:published_time",
        "date",
        "dc.date",
        "datePublished",
        "article:published",
        "og:article:published_time",
    )
    normalized_date = normalize_partial_date(date_value)
    if normalized_date:
        meta["date"] = normalized_date
    # Fallback: <time> element
    if "date" not in meta:
        time_tag = soup.find("time", attrs={"datetime": True})
        if time_tag:
            normalized_time = normalize_partial_date(time_tag["datetime"])
            if normalized_time:
                meta["date"] = normalized_time

    # Description
    for attr in ("description", "og:description"):
        tag = soup.find("meta", attrs={"name": attr}) or soup.find("meta", attrs={"property": attr})
        if tag and tag.get("content"):
            desc = tag["content"].strip()
            meta["description"] = desc
            break

    for attr in ("citation_doi", "dc.identifier", "doi", "dc.Identifier"):
        tag = soup.find("meta", attrs={"name": attr}) or soup.find("meta", attrs={"property": attr})
        if tag and tag.get("content"):
            content = tag["content"].strip()
            doi_match = re.search(r"10\.\d{4,9}/\S+", content)
            if doi_match:
                meta["doi"] = doi_match.group(0).rstrip(" .;,)")
                break

    container_title = first_meta_value("citation_journal_title", "citation_conference_title")
    if container_title:
        meta["container_title"] = container_title

    publisher = first_meta_value("citation_publisher")
    if publisher:
        meta["publisher"] = publisher

    volume = first_meta_value("citation_volume")
    if volume:
        meta["volume"] = volume

    issue = first_meta_value("citation_issue")
    if issue:
        meta["issue"] = issue

    first_page = first_meta_value("citation_firstpage")
    last_page = first_meta_value("citation_lastpage")
    article_number = first_meta_value("citation_article_number")
    if first_page and last_page:
        meta["pages"] = f"{first_page}-{last_page}"
    elif first_page:
        meta["pages"] = first_page
    elif article_number:
        meta["pages"] = article_number

    canonical = soup.find("link", rel=lambda value: value and "canonical" in str(value).lower())
    if canonical and canonical.get("href"):
        meta["canonical_url"] = urljoin(url, canonical["href"].strip()) if url else canonical["href"].strip()
    else:
        og_url = soup.find("meta", attrs={"property": "og:url"})
        if og_url and og_url.get("content"):
            meta["canonical_url"] = og_url["content"].strip()

    html_tag = soup.find("html")
    if html_tag and html_tag.get("lang"):
        meta["language"] = html_tag["lang"].split("-", 1)[0].strip().lower()
    else:
        for attr in ("language", "og:locale"):
            tag = soup.find("meta", attrs={"name": attr}) or soup.find("meta", attrs={"property": attr})
            if tag and tag.get("content"):
                meta["language"] = tag["content"].split("_", 1)[0].split("-", 1)[0].strip().lower()
                break

    return meta


def _is_likely_pdf_url(url: str) -> bool:
    return bool(re.search(r"(?:\.pdf(?:$|[?#])|/pdf(?:/|$))", url, flags=re.IGNORECASE))


def _pdf_source_name_from_url(url: str) -> str:
    path = urlparse(url).path
    name = Path(path).name or "document.pdf"
    return name if name.lower().endswith(".pdf") else f"{name}.pdf"


def _choose_pdf_title(pdf_title: str | None, source_name: str, url: str) -> str:
    cleaned_title = (pdf_title or "").strip()
    if cleaned_title and cleaned_title.lower() not in {"untitled", "microsoft word -", "about:blank"}:
        return cleaned_title

    candidate = Path(source_name or _pdf_source_name_from_url(url)).stem.strip()
    candidate = re.sub(r"[_-]+", " ", candidate)
    candidate = re.sub(r"\s+", " ", candidate).strip()
    return candidate or "PDF Document"


def _extract_arxiv_id(canonical_or_url: str | None) -> str | None:
    if not canonical_or_url:
        return None
    match = re.search(r"arxiv\.org/(?:abs|pdf)/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)", canonical_or_url)
    if match:
        return match.group(1)
    return None


def _format_csl_authors(authors: list[dict] | None) -> str | None:
    if not authors:
        return None

    rendered = []
    for author in authors:
        if not isinstance(author, dict):
            continue
        literal = (author.get("literal") or "").strip()
        if literal:
            rendered.append(literal)
            continue
        family = (author.get("family") or "").strip()
        given = (author.get("given") or "").strip()
        if family and given:
            rendered.append(f"{family}, {given}")
        elif family or given:
            rendered.append(family or given)

    return " and ".join(rendered) if rendered else None


def _csl_title(value) -> str | None:
    if isinstance(value, list):
        for item in value:
            if isinstance(item, str) and item.strip():
                return item.strip()
        return None
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _csl_date(value: dict | None) -> str | None:
    if not isinstance(value, dict):
        return None
    parts = value.get("date-parts")
    if not parts or not isinstance(parts, list) or not parts[0]:
        return None
    first = parts[0]
    if not isinstance(first, list) or not first:
        return None
    year = str(first[0])
    if len(first) >= 3:
        return f"{year}-{int(first[1]):02d}-{int(first[2]):02d}"
    if len(first) == 2:
        return f"{year}-{int(first[1]):02d}"
    return year


def _fetch_doi_metadata(doi: str) -> dict:
    response = requests.get(
        f"https://doi.org/{doi}",
        headers={
            "Accept": "application/vnd.citationstyles.csl+json",
            "User-Agent": _UA,
        },
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()

    return {
        "title": _csl_title(payload.get("title")),
        "author": _format_csl_authors(payload.get("author")),
        "date": (
            _csl_date(payload.get("issued"))
            or _csl_date(payload.get("published-print"))
            or _csl_date(payload.get("published-online"))
            or _csl_date(payload.get("published"))
            or _csl_date(payload.get("created"))
        ),
        "canonical_url": (
            (payload.get("resource") or {}).get("primary", {}).get("URL")
            or payload.get("URL")
        ),
        "container_title": _csl_title(payload.get("container-title")),
        "publisher": (payload.get("publisher") or "").strip() or None,
        "volume": (payload.get("volume") or "").strip() or None,
        "issue": (payload.get("issue") or "").strip() or None,
        "pages": (payload.get("page") or payload.get("article-number") or "").strip() or None,
        "source_site": urlparse(((payload.get("resource") or {}).get("primary", {}).get("URL") or "")).netloc.lower() or None,
    }


def _looks_like_site_title(title: str | None) -> bool:
    if not title:
        return False
    return bool(re.search(r"\s[-|]\s(?:ScienceDirect|arXiv|Medium|Substack|PMC|PubMed|MachineLearningMastery\.com)\s*$", title))


def _enrich_meta_with_doi(meta: dict) -> dict:
    doi = meta.get("doi")
    if not doi:
        return meta

    needs_enrichment = (
        not meta.get("author")
        or not meta.get("container_title")
        or _looks_like_site_title(meta.get("title"))
    )
    if not needs_enrichment:
        return meta

    try:
        doi_meta = _fetch_doi_metadata(doi)
    except Exception:
        log.exception("DOI metadata lookup failed for %s", doi)
        return meta

    enriched = dict(meta)
    if doi_meta.get("title"):
        enriched["title"] = doi_meta["title"]
    for field in ("author", "date", "canonical_url", "container_title", "publisher", "volume", "issue", "pages"):
        if doi_meta.get(field):
            enriched[field] = doi_meta[field]
    if doi_meta.get("source_site"):
        enriched["source_site"] = doi_meta["source_site"]
    return enriched


def _citation_key(title: str, author: str | None, date: str | None, doc_id: str) -> str:
    year_match = re.search(r"(19|20)\d{2}", date or "")
    year = year_match.group(0) if year_match else ""
    author_token = "anon"
    if author:
        first_author = re.split(r"\s*(?:,| and |;)\s*", author.strip())[0]
        parts = re.findall(r"[A-Za-z0-9]+", first_author)
        if parts:
            author_token = parts[-1].lower()
    title_words = re.findall(r"[A-Za-z0-9]+", title or "")
    title_token = "".join(word.lower() for word in title_words[:3]) or doc_id[:6]
    if year.isdigit():
        return f"{author_token}{year}{title_token[:24]}"
    return f"{author_token}{title_token[:24]}"


def _derive_citation_metadata(title: str, author: str | None, date: str | None, url: str | None,
                              canonical_url: str | None, source_site: str | None,
                              description: str | None, doc_id: str, doi: str | None = None,
                              container_title: str | None = None, publisher: str | None = None,
                              volume: str | None = None, issue: str | None = None,
                              pages: str | None = None) -> dict:
    if not doi and canonical_url:
        doi_match = re.search(r"10\.\d{4,9}/\S+", canonical_url)
        if doi_match:
            doi = doi_match.group(0).rstrip(" .;,)")
    arxiv_id = _extract_arxiv_id(canonical_url or url)
    citation_key = _citation_key(title, author, date, doc_id)
    entry_type = "article" if doi else "misc" if arxiv_id else "online"
    bibtex = _build_bibtex_entry(
        entry_type=entry_type,
        citation_key=citation_key,
        title=title,
        author=author,
        date=date,
        url=canonical_url or url,
        source_site=source_site,
        description=description,
        doi=doi,
        arxiv_id=arxiv_id,
        container_title=container_title,
        publisher=publisher,
        volume=volume,
        issue=issue,
        pages=pages,
    )
    frontmatter = {
        "citation_key": citation_key,
        "doi": doi,
        "arxiv_id": arxiv_id,
        "entry_type": entry_type,
    }
    return {
        "citation_key": citation_key,
        "bibtex": bibtex,
        "frontmatter": frontmatter,
    }


def _bibtex_escape(value: str) -> str:
    return (
        value.replace("\\", "\\\\")
        .replace("{", "\\{")
        .replace("}", "\\}")
        .replace("&", "\\&")
    )


def _build_bibtex_entry(entry_type: str, citation_key: str, title: str, author: str | None,
                        date: str | None, url: str | None, source_site: str | None,
                        description: str | None, doi: str | None, arxiv_id: str | None,
                        container_title: str | None = None, publisher: str | None = None,
                        volume: str | None = None, issue: str | None = None,
                        pages: str | None = None) -> str:
    fields = []
    fields.append(("title", title))
    if author:
        normalized_author = re.sub(r"\s*;\s*", " and ", author.strip())
        fields.append(("author", normalized_author))
    if date:
        fields.append(("date", date))
        year_match = re.search(r"(19|20)\d{2}", date)
        if year_match:
            fields.append(("year", year_match.group(0)))
    if url:
        fields.append(("url", url))
    if container_title:
        fields.append(("journal" if entry_type == "article" else "booktitle", container_title))
    if publisher:
        fields.append(("publisher", publisher))
    if volume:
        fields.append(("volume", volume))
    if issue:
        fields.append(("number", issue))
    if pages:
        fields.append(("pages", pages))
    if source_site:
        fields.append(("organization", source_site))
    if description:
        fields.append(("abstract", description))
    if doi:
        fields.append(("doi", doi))
    if arxiv_id:
        fields.append(("eprint", arxiv_id))
        fields.append(("archivePrefix", "arXiv"))

    rendered_fields = ",\n".join(
        f"  {key} = {{{_bibtex_escape(value)}}}" for key, value in fields if value
    )
    return f"@{entry_type}{{{citation_key},\n{rendered_fields}\n}}\n"


def _write_bibliography_file(article_dir: Path, article_basename: str, bibtex: str) -> Path:
    bib_file = article_dir / f"{article_basename}.bib"
    bib_file.write_text(bibtex, encoding="utf-8")
    return bib_file


def _extract_pdf_metadata(pdf_path: Path) -> dict:
    proc = subprocess.run(
        ["pdfinfo", str(pdf_path)],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        raise RuntimeError(output or "pdfinfo failed")

    metadata = {}
    for line in proc.stdout.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        metadata[key.strip().lower()] = value.strip()

    title = metadata.get("title") or None
    pages = metadata.get("pages")
    return {
        "title": title,
        "author": metadata.get("author") or None,
        "creationdate": metadata.get("creationdate") or None,
        "pages": int(pages) if pages and pages.isdigit() else None,
    }


def _extract_pdf_text(pdf_path: Path) -> str:
    proc = subprocess.run(
        ["pdftotext", "-enc", "UTF-8", "-nopgbrk", str(pdf_path), "-"],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        raise RuntimeError(output or "pdftotext failed")
    return proc.stdout


def _extract_pdf_markdown_via_mistral(pdf_bytes: bytes, article_dir: Path, safe_name: str) -> tuple[str, dict]:
    if not _DEFAULT_MISTRAL_API_KEY:
        raise RuntimeError("MISTRAL_API_KEY is not configured")

    payload = {
        "model": _DEFAULT_MISTRAL_OCR_MODEL,
        "document": {
            "type": "document_url",
            "document_url": f"data:application/pdf;base64,{base64.b64encode(pdf_bytes).decode('ascii')}",
        },
        "include_image_base64": True,
        "pages": [],
    }
    response = requests.post(
        f"{_DEFAULT_MISTRAL_BASE_URL.rstrip('/')}/v1/ocr",
        headers={
            "Authorization": f"Bearer {_DEFAULT_MISTRAL_API_KEY}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    response.raise_for_status()
    data = response.json()

    (article_dir / "ocr_response.json").write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    pages = data.get("pages") or []
    if not pages:
        raise RuntimeError("Mistral OCR returned no pages")

    assets_dir = article_dir / "assets"
    parts = []
    for page_index, page in enumerate(pages, start=1):
        page_markdown = (page.get("markdown") or "").strip()
        page_markdown = _save_mistral_page_images(
            page_markdown=page_markdown,
            page=page,
            assets_dir=assets_dir,
            safe_name=safe_name,
            page_index=page_index,
        )
        if page_markdown:
            parts.append(page_markdown)
    markdown = "\n\n---\n\n".join(part for part in parts if part).strip()
    if not markdown:
        raise RuntimeError("Mistral OCR returned empty markdown")
    return markdown, data


def _save_mistral_page_images(page_markdown: str, page: dict, assets_dir: Path,
                              safe_name: str, page_index: int) -> str:
    images = page.get("images") or []
    updated_markdown = page_markdown

    for image_index, image in enumerate(images, start=1):
        image_base64 = image.get("image_base64") or ""
        if not image_base64.startswith("data:"):
            continue
        match = re.match(r"data:(image/[^;]+);base64,(.+)", image_base64, flags=re.DOTALL)
        if not match:
            continue
        mime_type = match.group(1)
        raw_data = match.group(2)
        ext = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(mime_type.lower(), ".img")
        file_name = f"{safe_name}_p{page_index:03d}_img{image_index:02d}{ext}"
        out_path = assets_dir / file_name
        out_path.write_bytes(base64.b64decode(raw_data))

        image_id = image.get("id") or image.get("name") or ""
        if image_id:
            updated_markdown = updated_markdown.replace(f"]({image_id})", f"](assets/{file_name})")
            updated_markdown = updated_markdown.replace(f"![]({image_id})", f"![]({('assets/' + file_name)})")

    return updated_markdown


def _pdf_text_to_markdown(text: str) -> str:
    text = _sanitize_unicode_text(text or "")
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x0c", "\n\n")
    text = re.sub(r"(?<=\w)-\n(?=\w)", "", text)
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)

    blocks = re.split(r"\n\s*\n", text)
    paragraphs = []
    bullet_pattern = re.compile(r"^(?:[-*•]\s+|\d+[.)]\s+)")

    for block in blocks:
        lines = [line.strip() for line in block.splitlines() if line.strip()]
        if not lines:
            continue
        if all(bullet_pattern.match(line) for line in lines):
            normalized_lines = [re.sub(r"^•\s+", "- ", line) for line in lines]
            paragraphs.append("\n".join(normalized_lines))
            continue
        joined = re.sub(r"\s+", " ", " ".join(lines)).strip()
        if joined:
            paragraphs.append(joined)

    return "\n\n".join(paragraphs).strip()


def _yaml_quote(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _build_frontmatter(metadata: dict) -> str:
    fm_lines = ["---"]
    for key, value in metadata.items():
        if value is None or value == "":
            continue
        if isinstance(value, bool):
            rendered = "true" if value else "false"
        elif isinstance(value, (int, float)):
            rendered = str(value)
        else:
            rendered = _yaml_quote(value)
        fm_lines.append(f"{key}: {rendered}")
    fm_lines.append("---")
    return "\n".join(fm_lines)


def _count_words(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text, flags=re.UNICODE))


def _stable_doc_id(title: str, canonical_or_url: str | None) -> str:
    basis = canonical_or_url or title
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


def _notes_doc_id(article_doc_id: str) -> str:
    return f"{article_doc_id}:notes"


def _generate_companion_notes(
    md_text: str,
    article_dir: Path,
    article_basename: str,
    metadata: dict,
) -> Path:
    notes_client = _resolve_notes_client_config(metadata.get("notes_config") or {})
    notes_body = _generate_companion_notes_body_with_config(md_text, notes_client)
    notes_frontmatter = _build_frontmatter(
        {
            "title": f"{metadata['title']} Notes",
            "doc_id": _notes_doc_id(metadata["article_doc_id"]),
            "type": "companion_notes",
            "doc_type": "notes",
            "source_article": f"{article_basename}.md",
            "source_doc_id": metadata["article_doc_id"],
            "url": metadata.get("url"),
            "canonical_url": metadata.get("canonical_url"),
            "source_site": metadata.get("source_site"),
            "label": metadata.get("label"),
            "language": metadata.get("language"),
            "generated_by": f"{notes_client['provider']}:{notes_client['model']}",
            "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
            "ingested_at": metadata.get("ingested_at"),
        }
    )
    notes_file = article_dir / f"{article_basename}.notes.md"
    notes_file.write_text(notes_frontmatter + "\n\n" + notes_body.strip() + "\n", encoding="utf-8")
    return notes_file


def _generate_companion_notes_body(md_text: str) -> str:
    return _generate_companion_notes_body_with_config(md_text, _resolve_notes_client_config({}))


def _strip_reference_sections_for_notes(md_text: str) -> str:
    patterns = (
        r"(?mis)^\s{0,3}#{1,6}\s*(?:references|bibliography)\s*$",
        r"(?mis)^\s{0,3}(?:references|bibliography)\s*$",
    )
    cut_positions = []
    for pattern in patterns:
        match = re.search(pattern, md_text)
        if match:
            cut_positions.append(match.start())
    if not cut_positions:
        return md_text
    return md_text[:min(cut_positions)].rstrip() + "\n"


def _resolve_notes_client_config(overrides: dict) -> dict:
    provider = (overrides.get("provider") or _DEFAULT_NOTES_PROVIDER).strip().lower()
    config = {
        "provider": provider,
        "model": (overrides.get("model") or _DEFAULT_NOTES_MODEL).strip(),
        "base_url": (overrides.get("base_url") or _DEFAULT_NOTES_BASE_URL).strip(),
        "api_key": overrides.get("api_key") if overrides.get("api_key") is not None else _DEFAULT_NOTES_API_KEY,
        "timeout": int(overrides.get("timeout") or _DEFAULT_NOTES_TIMEOUT),
        "anthropic_version": (
            overrides.get("anthropic_version") or _DEFAULT_ANTHROPIC_VERSION
        ).strip(),
    }
    if provider not in {"openai_compatible", "openai", "anthropic"}:
        raise ValueError(f"Unsupported notes provider: {provider}")
    return config


def _generate_companion_notes_body_with_config(md_text: str, notes_client: dict) -> str:
    md_text = _strip_reference_sections_for_notes(md_text)
    prompt = (
        "Read the article markdown inside <article_markdown>. "
        "Write extractive companion notes in markdown only, with these sections exactly in this order:\n"
        "# Notes\n"
        "## Summary\n"
        "## Key Points\n"
        "## Definitions\n"
        "## Important Equations\n"
        "## Code Takeaways\n"
        "## Open Questions\n\n"
        "Rules:\n"
        "- Be strictly faithful to the article.\n"
        "- Do not infer, estimate, or invent facts not explicitly stated in the article.\n"
        "- Do not rewrite the article as a paper. These are notes, not a new article.\n"
        "- Under every section, use flat bullet lists only. No paragraphs.\n"
        "- Keep bullets short and information-dense.\n"
        "- Prefer extractive phrasing grounded in the source.\n"
        "- Preserve equations in LaTeX when they appear in the source markdown.\n"
        "- Use inline math as `$...$` and display math as `$$...$$`.\n"
        "- Do not paraphrase an equation into prose if the original LaTeX is available.\n"
        "- For numeric claims, study design details, sample sizes, or statistics, include them only if explicitly present.\n"
        "- If a section has no relevant content, write a single bullet: `- None.`\n"
        "- Do not include YAML frontmatter.\n"
        "- Do not wrap the answer in code fences.\n\n"
        f"<article_markdown>\n{md_text[:30000]}\n</article_markdown>"
    )
    if notes_client["provider"] in {"openai_compatible", "openai"}:
        content = _generate_notes_via_openai_compatible(prompt, notes_client)
    else:
        content = _generate_notes_via_anthropic(prompt, notes_client)

    content = re.sub(r"^```(?:markdown)?\s*", "", content)
    content = re.sub(r"\s*```$", "", content)
    if not content:
        raise RuntimeError("Local notes model returned empty content")
    return content


def _generate_notes_via_openai_compatible(prompt: str, notes_client: dict) -> str:
    headers = {"Content-Type": "application/json"}
    if notes_client.get("api_key"):
        headers["Authorization"] = f"Bearer {notes_client['api_key']}"

    response = requests.post(
        f"{notes_client['base_url'].rstrip('/')}/chat/completions",
        headers=headers,
        json={
            "model": notes_client["model"],
            "messages": [
                {
                    "role": "system",
                    "content": "You are a precise research note generator. Output markdown only.",
                },
                {"role": "user", "content": prompt},
            ],
            "max_tokens": 1400,
            "temperature": 0.0,
        },
        timeout=notes_client["timeout"],
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


def _generate_notes_via_anthropic(prompt: str, notes_client: dict) -> str:
    if not notes_client.get("api_key"):
        raise RuntimeError("Anthropic notes provider requires api_key")

    response = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "Content-Type": "application/json",
            "x-api-key": notes_client["api_key"],
            "anthropic-version": notes_client["anthropic_version"],
        },
        json={
            "model": notes_client["model"],
            "max_tokens": 1400,
            "temperature": 0.0,
            "system": "You are a precise research note generator. Output markdown only.",
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=notes_client["timeout"],
    )
    response.raise_for_status()
    data = response.json()
    parts = data.get("content", [])
    text_parts = [part.get("text", "") for part in parts if part.get("type") == "text"]
    return "\n".join(text_parts).strip()


def _fetch_and_inject_mathml(soup: BeautifulSoup, url: str,
                              session: requests.Session) -> None:
    """Fetch the server-side HTML to get original <math> MathML tags.

    MathJax replaces <math> with <mjx-container> in the browser DOM.
    The server-side HTML still has the original MathML, which we can
    extract and inject back into the soup, replacing <mjx-container>.

    Note: BeautifulSoup's HTML parsers mangle <math> tags, so we use
    regex to extract them from the raw server HTML.
    """
    import re

    try:
        resp = session.get(url, timeout=15)
        resp.raise_for_status()
    except Exception as e:
        log.warning("Failed to fetch server HTML for MathML: %s", e)
        return

    # Extract <math> tags via regex (BS4 html.parser mangles MathML)
    raw_math = re.findall(r"<math[^>]*>.*?</math>", resp.text, re.DOTALL)
    if not raw_math:
        return

    # Build lookup by ID
    math_by_id = {}
    for m_str in raw_math:
        id_match = re.search(r'id="([^"]+)"', m_str)
        if id_match:
            math_by_id[id_match.group(1)] = m_str

    containers = soup.find_all("mjx-container")
    log.info("Injecting %d server-side MathML tags into %d mjx-containers",
             len(raw_math), len(containers))

    positional_idx = 0
    for container in containers:
        # Try ID match: mjx-container > mjx-math[id] matches <math id>
        mjx_math = container.find("mjx-math")
        matched = None
        if mjx_math:
            mid = mjx_math.get("id")
            if mid and mid in math_by_id:
                matched = math_by_id[mid]

        # Positional fallback
        if not matched and positional_idx < len(raw_math):
            matched = raw_math[positional_idx]
            positional_idx += 1
        elif matched:
            positional_idx += 1

        if matched:
            # Insert raw MathML string as a NavigableString placeholder;
            # _convert_mathml will parse and convert it later.
            # We need to parse it into a BS4 tag first.
            math_soup = BeautifulSoup(matched, "xml")
            math_tag = math_soup.find("math")
            if math_tag:
                container.replace_with(math_tag)
            else:
                container.replace_with(matched)


def _convert_mathjax_fallback(soup: BeautifulSoup) -> None:
    """Convert leftover MathJax CHTML equations to MathML in-place.

    Standard <math> elements are intentionally preserved because Pandoc
    converts MathML more reliably than our custom CHTML parser.
    """
    converted = 0

    # Only convert MathJax CHTML <mjx-container> elements that remain after
    # server-side MathML injection. This is a fallback path.
    for container in soup.find_all("mjx-container"):
        try:
            mjx_math = container.find("mjx-math")
            if not mjx_math:
                continue
            mathml_str = _mjx_to_mathml(mjx_math)
            if not mathml_str:
                continue
            math_tag = _parse_math_fragment(mathml_str)
            if math_tag:
                container.replace_with(math_tag)
                converted += 1
                continue

            # Last-resort fallback when the generated MathML is malformed.
            try:
                from mathml_to_latex import MathMLToLaTeX
                latex = _cleanup_latex(MathMLToLaTeX.convert(mathml_str))
                is_block = container.get("display") == "true"
                container.replace_with(f"\n\n$${latex}$$\n\n" if is_block else f"${latex}$")
                converted += 1
            except Exception as inner:
                log.warning("Failed fallback MathJax conversion: %s", inner)
        except Exception as e:
            log.warning("Failed to convert MathJax element: %s", e)

    if converted:
        log.info("Converted %d equations to LaTeX", converted)


def _normalize_display_formula_tables(soup: BeautifulSoup) -> None:
    """Replace equation-layout tables with block containers.

    PubMed/PMC frequently encode display equations as:
    <table class="disp-formula"><td class="formula">...</td><td class="label">(1)</td>

    Treating those as real tables causes broken markdown/PDF output.
    """
    for table in soup.find_all("table"):
        classes = set(table.get("class", []))
        if "disp-formula" not in classes:
            continue

        formula_cell = table.find("td", class_=lambda c: c and "formula" in c.split())
        if not formula_cell:
            continue

        replacements = []

        math_node = _extract_preferred_math_node(formula_cell)
        if math_node:
            if getattr(math_node, "name", None) and table.get("id") and not math_node.get("id"):
                math_node["id"] = table["id"]
            replacements.append(math_node)
        else:
            replacements.extend(child.extract() for child in list(formula_cell.contents))

        _replace_with_sequence(table, replacements)


def _normalize_latexml_equation_tables(soup: BeautifulSoup) -> None:
    """Unwrap arXiv/LaTeXML equation tables into plain MathML blocks."""
    for table in list(soup.find_all("table")):
        classes = set(table.get("class", []))
        if "ltx_eqn_table" not in classes and "ltx_equation" not in classes:
            continue

        math_node = table.find("math")
        if not math_node:
            continue

        extracted = math_node.extract()
        if getattr(extracted, "name", None) == "math":
            extracted["display"] = "block"
        _replace_with_sequence(table, [extracted])


def _normalize_labeled_formula_blocks(soup: BeautifulSoup) -> None:
    """Flatten display-formula wrappers used by ScienceDirect and similar sites."""
    for formula in soup.find_all(class_=lambda c: c and "formula" in c.split()):
        if formula.name == "td":
            continue
        if formula.find_parent(class_="display-formula"):
            continue

        has_math = (
            formula.find("math") or
            formula.find("script", attrs={"type": "math/mml"}) or
            formula.find(attrs={"data-mathml": True}) or
            formula.find("mjx-container")
        )
        if not has_math:
            continue

        container = formula
        parent = formula.parent
        if (
            getattr(parent, "name", None) in {"span", "div"}
            and "display" in parent.get("class", [])
            and len([c for c in parent.contents if getattr(c, "name", None) or str(c).strip()]) == 1
        ):
            container = parent

        math_node = _extract_preferred_math_node(formula)
        if not math_node:
            # Do not rewrite the block if we failed to extract the equation;
            # otherwise we can end up preserving only the "(n)" label.
            continue

        replacements = []
        if getattr(math_node, "name", None) and formula.get("id") and not math_node.get("id"):
            math_node["id"] = formula["id"]
        replacements.append(math_node)

        if replacements:
            _replace_with_sequence(container, replacements)


def _extract_preferred_math_node(scope) -> BeautifulSoup | None:
    """Return the best available math representation from a wrapper node."""
    assistive = scope.select_one(".MJX_Assistive_MathML math")
    if assistive:
        return assistive.extract()

    math_tag = scope.find("math")
    if math_tag:
        return math_tag.extract()

    script = scope.find("script", attrs={"type": "math/mml"})
    if script and script.string:
        parsed = _parse_math_fragment(script.string)
        if parsed:
            return parsed

    data_mathml = scope.find(attrs={"data-mathml": True})
    if data_mathml:
        parsed = _parse_math_fragment(data_mathml["data-mathml"])
        if parsed:
            return parsed

    mjx = scope.find("mjx-container")
    if mjx:
        return mjx.extract()

    return None


def _parse_math_fragment(fragment: str):
    """Parse a raw or HTML-escaped MathML fragment into a <math> tag."""
    fragment = html.unescape(fragment).strip()
    if not fragment:
        return None
    parsed = BeautifulSoup(fragment, "xml")
    return parsed.find("math")


def _replace_with_sequence(node, replacements) -> None:
    """Replace a node with an ordered sequence of nodes/text fragments."""
    replacements = [item for item in replacements if item is not None]
    if not replacements:
        node.decompose()
        return

    first, *rest = replacements
    node.replace_with(first)
    current = first
    for item in rest:
        current.insert_after(item)
        current = item


def _cleanup_latex(latex: str) -> str:
    """Fix common structural artifacts from mathml-to-latex conversion."""
    import re
    import unicodedata

    latex = unicodedata.normalize("NFKC", latex)

    # Fix broken \left(\right. ... \left.\right) pairs → plain ( ... )
    latex = latex.replace(r"\left(\right.", "(")
    latex = latex.replace(r"\left.\right)", ")")
    latex = latex.replace(r"\left(\right)", "()")

    cleaned = []
    for ch in latex:
        if ch in ("\n", "\r", "\t"):
            cleaned.append(ch)
            continue
        category = unicodedata.category(ch)
        if category in {"Cf", "Cc", "Cs"}:
            continue
        cleaned.append(ch)
    latex = "".join(cleaned)

    # Fix triple+ backslash → double backslash (mathml-to-latex artifact for line breaks)
    latex = re.sub(r"\\{3,}", r"\\\\", latex)
    # Collapse excessive whitespace
    latex = re.sub(r"  +", " ", latex)
    return latex.strip()


# MathJax CHTML tag → standard MathML tag
_MJX_TAG_MAP = {
    "mjx-math": "math", "mjx-mi": "mi", "mjx-mo": "mo", "mjx-mn": "mn",
    "mjx-ms": "ms", "mjx-mtext": "mtext", "mjx-mrow": "mrow",
    "mjx-msub": "msub", "mjx-msup": "msup", "mjx-msubsup": "msubsup",
    "mjx-mfrac": "mfrac", "mjx-msqrt": "msqrt", "mjx-mroot": "mroot",
    "mjx-mover": "mover", "mjx-munder": "munder", "mjx-munderover": "munderover",
    "mjx-mtable": "mtable", "mjx-mtr": "mtr", "mjx-mtd": "mtd",
    "mjx-mfenced": "mfenced", "mjx-mspace": "mspace",
    "mjx-mpadded": "mpadded", "mjx-mphantom": "mphantom",
}

# Leaf elements that contain text (via <mjx-c> children)
_MJX_LEAF = {"mjx-mi", "mjx-mo", "mjx-mn", "mjx-ms", "mjx-mtext"}

# Elements where <mjx-script> packs multiple script children
# msub/msup: script has 1 child; msubsup: 2; munder/mover: 1; munderover: 2
_MJX_SCRIPT_TAGS = {"mjx-msub", "mjx-msup", "mjx-msubsup",
                     "mjx-munder", "mjx-mover", "mjx-munderover"}

def _normalize_math_text(text: str) -> str:
    """Normalize MathJax text content without hardcoded symbol tables."""
    import unicodedata

    text = unicodedata.normalize("NFKC", text)
    cleaned = []
    for ch in text:
        category = unicodedata.category(ch)
        if category in {"Cf", "Cc", "Cs"}:
            continue
        cleaned.append(ch)
    return "".join(cleaned)


def _replace_problem_math_with_tex_placeholders(soup: BeautifulSoup) -> dict[str, tuple[str, bool]]:
    """Replace malformed/unparsed MathML with stable placeholders plus TeX annotations.

    arXiv/LaTeXML sometimes emits `math.ltx_math_unparsed` or MathML containing
    `<merror>` nodes, but still provides a valid `application/x-tex` annotation.
    Pandoc handles those nodes poorly, so we preserve the TeX source and restore
    it after conversion.
    """
    replacements: dict[str, tuple[str, bool]] = {}

    for idx, math_tag in enumerate(list(soup.find_all("math")), start=1):
        annotation = math_tag.find("annotation", attrs={"encoding": "application/x-tex"})
        if not annotation:
            continue

        tex = annotation.get_text(strip=True)
        if not tex:
            continue

        classes = set(math_tag.get("class", []))
        if "ltx_math_unparsed" not in classes and not math_tag.find("merror"):
            continue

        placeholder = f"SCRIBE_TEX_{idx:04d}"
        replacements[placeholder] = (_cleanup_latex(tex), math_tag.get("display") == "block")
        math_tag.replace_with(NavigableString(placeholder))

    return replacements


def _restore_tex_placeholders(text: str, replacements: dict[str, tuple[str, bool]], target: str) -> str:
    """Restore placeholder math markers into markdown or LaTeX output."""
    for placeholder, (tex, display) in replacements.items():
        while placeholder in text:
            idx = text.find(placeholder)
            before = text[idx - 1] if idx > 0 else ""
            after_idx = idx + len(placeholder)
            after = text[after_idx] if after_idx < len(text) else ""

            if target == "markdown":
                rendered = f"$${tex}$$" if display else f"${tex}$"
            elif target == "latex":
                rendered = f"\\[\n{tex}\n\\]" if display else f"${tex}$"
            else:
                rendered = tex

            if target == "markdown" and not display:
                prefix = " " if before and before.isalnum() else ""
                suffix = " " if after and after.isalnum() else ""
                rendered = prefix + rendered + suffix

            text = text[:idx] + rendered + text[after_idx:]

    return text


def _mjx_to_mathml(node) -> str:
    """Recursively convert a MathJax CHTML element tree to a MathML string."""
    tag = getattr(node, "name", None)
    if not tag:
        return ""

    # <mjx-c> — character element, return normalized text
    if tag == "mjx-c":
        return _normalize_math_text(node.get_text())

    mathml_tag = _MJX_TAG_MAP.get(tag)

    # Leaf elements — extract text from <mjx-c> children
    if tag in _MJX_LEAF and mathml_tag:
        text = _normalize_math_text(_mjx_leaf_text(node))
        attrs = _mjx_mathml_attrs(node, mathml_tag)
        return f"<{mathml_tag}{attrs}>{text}</{mathml_tag}>"

    # Script-bearing elements (msub, msup, munderover, etc.)
    # MathJax packs script children in <mjx-script>; we need to
    # produce the right number of MathML children.
    if tag in _MJX_SCRIPT_TAGS and mathml_tag:
        return _mjx_script_element(node, mathml_tag)

    # <mjx-script> outside of script-bearing elements — unwrap
    if tag == "mjx-script":
        return "".join(_mjx_child(c) for c in node.children)

    # Other structure elements — recurse
    if mathml_tag:
        inner = "".join(_mjx_child(c) for c in node.children)
        attrs = _mjx_mathml_attrs(node, mathml_tag)
        return f"<{mathml_tag}{attrs}>{inner}</{mathml_tag}>"

    # Unknown element — recurse
    return "".join(_mjx_child(c) for c in node.children)


def _mjx_child(node) -> str:
    """Convert a single child node, skipping non-element nodes."""
    if hasattr(node, "name") and node.name:
        return _mjx_to_mathml(node)
    return ""


def _mjx_script_element(node, mathml_tag: str) -> str:
    """Handle mjx-msub, mjx-msup, mjx-munderover, etc.

    MathJax structure: the base element is a direct child, and
    sub/superscript parts are inside <mjx-script>.

    For msub/msup/munder/mover: <mjx-script> = one script slot.
    For msubsup/munderover: <mjx-script> = two script slots.

    MathJax distinguishes slots by the `style` attribute (vertical-align)
    on direct children of <mjx-script>. We split by counting top-level
    children that have `size="s"` (first slot) vs others (second slot).
    Fallback: split in half.
    """
    base_parts = []
    script_node = None

    for child in node.children:
        if not hasattr(child, "name") or not child.name:
            continue
        if child.name == "mjx-script":
            script_node = child
        else:
            base_parts.append(_mjx_to_mathml(child))

    base = _wrap_mrow(base_parts)

    # Determine how many script slots we need
    needs_two = mathml_tag in ("msubsup", "munderover")

    if not script_node:
        attrs = _mjx_mathml_attrs(node, mathml_tag)
        return f"<{mathml_tag}{attrs}>{base}</{mathml_tag}>"

    # Collect direct element children of <mjx-script>
    script_kids = [c for c in script_node.children
                   if hasattr(c, "name") and c.name]

    if needs_two and len(script_kids) >= 2:
        # Two script slots — first child is sub/under, second is sup/over
        slot1 = _wrap_mrow([_mjx_to_mathml(script_kids[0])])
        slot2 = _wrap_mrow([_mjx_to_mathml(c) for c in script_kids[1:]])
        inner = base + slot1 + slot2
    else:
        # One script slot — wrap all children as one group
        slot = _wrap_mrow([_mjx_to_mathml(c) for c in script_kids])
        inner = base + slot

    attrs = _mjx_mathml_attrs(node, mathml_tag)
    return f"<{mathml_tag}{attrs}>{inner}</{mathml_tag}>"


def _wrap_mrow(parts: list[str]) -> str:
    """Wrap multiple MathML fragments in <mrow> if needed."""
    if len(parts) == 1:
        return parts[0]
    return "<mrow>" + "".join(parts) + "</mrow>"


def _mjx_leaf_text(node) -> str:
    """Extract text content from a MathJax leaf element's <mjx-c> children."""
    chars = []
    for c in node.find_all("mjx-c", recursive=True):
        t = c.get_text()
        if t:
            chars.append(t)
    return "".join(chars)


def _mjx_mathml_attrs(node, mathml_tag: str) -> str:
    """Build relevant MathML attributes from a mjx-* element."""
    attrs = []
    if mathml_tag == "math":
        parent = node.parent
        if parent and parent.get("display") == "true":
            attrs.append('display="block"')
    font = node.get("data-semantic-font", "")
    if font in ("bold-italic", "bold", "italic", "normal",
                 "double-struck", "script", "fraktur"):
        attrs.append(f'mathvariant="{font}"')
    return (" " + " ".join(attrs)) if attrs else ""


def _extract_article_tag(html: str) -> str | None:
    """Extract content from <article> tag, stripping nav/footer/header noise.

    Returns inner HTML string, or None if no suitable <article> found.
    """
    soup = BeautifulSoup(html, "html.parser")
    article = soup.find("article")
    if not article:
        return None

    # Remove elements that are typically not article content
    for tag in article.find_all(["nav", "footer", "header", "aside"]):
        tag.decompose()
    # Remove hidden elements
    for tag in article.find_all(style=lambda s: s and "display:none" in s.replace(" ", "")):
        tag.decompose()
    # Remove Medium's "sign up" / "follow" button containers
    for tag in article.find_all("button"):
        tag.decompose()

    return str(article)


def _collect_images(html: str, url: str) -> list[str]:
    """Collect all image URLs from the original HTML before readability processes it.

    Returns a list of absolute image URLs found in <img> and <picture><source> elements.
    """
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    images = []

    # Look within <article> first, then fall back to full body
    scope = soup.find("article") or soup.find("body") or soup

    for picture in scope.find_all("picture"):
        img = picture.find("img")
        src = None
        if img and img.get("src"):
            src = img["src"]
        elif img and img.get("srcset"):
            src = _best_srcset_url(img["srcset"])
        if not src:
            source = picture.find("source")
            if source and source.get("srcset"):
                src = _best_srcset_url(source["srcset"])
        if src:
            abs_src = urljoin(url, src) if url else src
            if abs_src not in seen:
                seen.add(abs_src)
                images.append(abs_src)

    for img in scope.find_all("img"):
        src = img.get("src")
        if not src and img.get("srcset"):
            src = _best_srcset_url(img["srcset"])
        if not src:
            continue
        abs_src = urljoin(url, src) if url else src
        if abs_src not in seen and not abs_src.startswith("data:"):
            seen.add(abs_src)
            images.append(abs_src)

    return images


def _reinject_images(soup: BeautifulSoup, original_images: list[str]) -> BeautifulSoup:
    """Re-inject images that readability stripped from the content.

    Readability often strips <img> tags from complex DOM structures (e.g. Medium's
    deeply nested <figure>/<picture> elements). This function:
    1. Checks which original images are already present in the readability output
    2. Fills empty <figure> elements with missing images
    3. Appends remaining images at the end of the content
    """
    # Find which images readability kept
    existing_srcs = set()
    for img in soup.find_all("img"):
        src = img.get("src", "")
        existing_srcs.add(src)
        if img.get("srcset"):
            existing_srcs.add(_best_srcset_url(img["srcset"]))

    missing = [url for url in original_images if url not in existing_srcs]
    if not missing:
        return soup

    log.info("Re-injecting %d images stripped by readability", len(missing))

    # Try to fill empty <figure> elements first
    empty_figures = [f for f in soup.find_all("figure") if not f.find("img")]
    for i, fig in enumerate(empty_figures):
        if i < len(missing):
            new_img = soup.new_tag("img", src=missing[i])
            fig.append(new_img)

    # Append any remaining images at the end
    remaining = missing[len(empty_figures):]
    if remaining:
        body = soup.find("body") or soup
        for img_url in remaining:
            new_img = soup.new_tag("img", src=img_url)
            p = soup.new_tag("p")
            p.append(new_img)
            body.append(p)

    return soup


def _best_srcset_url(srcset: str) -> str:
    """Pick the highest-resolution URL from a srcset attribute."""
    candidates = []
    for part in srcset.split(","):
        tokens = part.strip().split()
        if tokens:
            url = tokens[0]
            # Parse width descriptor (e.g. "700w") or density ("2x")
            weight = 0
            if len(tokens) > 1:
                desc = tokens[1].lower()
                if desc.endswith("w"):
                    weight = int(desc[:-1])
                elif desc.endswith("x"):
                    weight = int(float(desc[:-1]) * 1000)
            candidates.append((weight, url))
    if not candidates:
        return srcset.split(",")[0].strip().split()[0]
    candidates.sort(reverse=True)
    return candidates[0][1]


def _resize_images_for_pdf(assets_dir: Path, max_width: int = 484) -> None:
    """Resize images in assets_dir to fit within max_width pixels (A5 content area)."""
    for img_path in assets_dir.iterdir():
        if img_path.suffix.lower() in (".svg",):
            continue
        temp_path = None
        try:
            with Image.open(img_path) as im:
                if im.width > max_width:
                    ratio = max_width / im.width
                    new_size = (max_width, int(im.height * ratio))
                    resized = im.resize(new_size, Image.LANCZOS)
                    save_format = _detect_raster_format(img_path)[1] or im.format or "PNG"
                    if save_format == "JPEG" and resized.mode in {"RGBA", "LA"}:
                        resized = resized.convert("RGB")
                    fd, temp_name = tempfile.mkstemp(
                        suffix=img_path.suffix or ".img",
                        dir=str(assets_dir),
                    )
                    Path(temp_name).unlink(missing_ok=True)
                    temp_path = Path(temp_name)
                    resized.save(temp_path, format=save_format)
                    temp_path.replace(img_path)
                    log.debug("Resized %s: %dx%d -> %dx%d",
                              img_path.name, im.width, im.height, *new_size)
        except Exception as e:
            log.warning("Failed to resize %s: %s", img_path.name, e)
            if temp_path:
                temp_path.unlink(missing_ok=True)


def _download_image(img_url: str, assets_dir: Path, session: requests.Session) -> Path | None:
    if img_url.startswith("//"):
        img_url = "https:" + img_url
    if img_url.startswith("data:"):
        return None
    temp_path = None
    try:
        resp = session.get(img_url, timeout=10, stream=True)
        resp.raise_for_status()
        token = str(uuid.uuid4())
        fd, temp_name = tempfile.mkstemp(suffix=".download", dir=str(assets_dir))
        Path(temp_name).unlink(missing_ok=True)
        temp_path = Path(temp_name)
        bytes_written = 0
        with open(temp_path, "wb") as f:
            for chunk in resp.iter_content(8192):
                if not chunk:
                    continue
                bytes_written += len(chunk)
                f.write(chunk)
        if bytes_written == 0:
            raise ValueError("downloaded image body is empty")
        ext, _ = _detect_raster_format(temp_path)
        if not ext:
            ext = _guess_ext(resp.headers.get("content-type", ""), img_url)
        local = assets_dir / f"{token}{ext}"
        temp_path.replace(local)
        return local
    except Exception as e:
        log.warning("Failed to download image %s: %s", img_url, e)
        if temp_path:
            temp_path.unlink(missing_ok=True)
        return None


def _guess_ext(content_type: str, url: str) -> str:
    ct = content_type.lower().split(";")[0].strip()
    mapping = {
        "image/png": ".png",
        "image/jpeg": ".jpg",
        "image/gif": ".gif",
        "image/webp": ".webp",
        "image/svg+xml": ".svg",
    }
    if ct in mapping:
        return mapping[ct]
    for ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"):
        if ext in url.lower():
            return ext
    return ".png"


def _detect_raster_format(path: Path) -> tuple[str | None, str | None]:
    try:
        with Image.open(path) as im:
            fmt = (im.format or "").upper()
    except Exception:
        fmt = ""

    mapping = {
        "PNG": (".png", "PNG"),
        "JPEG": (".jpg", "JPEG"),
        "GIF": (".gif", "GIF"),
        "WEBP": (".webp", "WEBP"),
    }
    if fmt in mapping:
        return mapping[fmt]

    try:
        head = path.read_text(encoding="utf-8", errors="ignore")[:512].lower()
    except Exception:
        head = ""
    if "<svg" in head:
        return ".svg", None

    return None, None


def _sanitize_unicode_text(text: str) -> str:
    """Normalize Unicode for markdown/PDF generation without hardcoded mappings.

    Keep markdown-significant whitespace intact. In particular, do not collapse
    repeated spaces globally, because indented code blocks rely on leading
    indentation to survive markdown -> HTML conversion.
    """
    import unicodedata

    text = unicodedata.normalize("NFKC", text)

    normalized = []
    for ch in text:
        if ch in ("\n", "\r", "\t"):
            normalized.append(ch)
            continue

        category = unicodedata.category(ch)
        if category == "Zs":
            normalized.append(" ")
            continue
        if category == "Cf":
            normalized.append(" ")
            continue
        if category in {"Cc", "Cs"}:
            continue
        normalized.append(ch)

    text = "".join(normalized)
    # Strip emoji/private pictographs that commonly leak from scraped pages.
    text = re.sub(r"[\U0001F000-\U0001FFFF]", "", text)
    return text.strip()


def _convert_html_to_markdown(html_text: str) -> str:
    """Convert normalized article HTML to markdown with Pandoc."""
    return pypandoc.convert_text(
        html_text,
        to="markdown+pipe_tables+tex_math_dollars+subscript+superscript-grid_tables-simple_tables-multiline_tables",
        format="html",
        extra_args=["--wrap=none"],
    )


def _postprocess_markdown(markdown_text: str) -> str:
    """Clean markdown for downstream readers that do not support fragment links."""
    # Drop same-document anchor targets like [1](#R1) or [1](#R1 "title")
    # while keeping the visible label text.
    markdown_text = re.sub(
        r"\[([^\]]+)\]\(#[-A-Za-z0-9_.:]+(?:\s+\"[^\"]*\")?\)",
        r"\1",
        markdown_text,
    )
    # Drop Pandoc raw HTML comment artifacts, e.g. `<!-- -->`{=html}, which can
    # appear when inline MathML touches adjacent text in the source HTML.
    markdown_text = re.sub(r"`<!--\s*-->`\{=html\}", "", markdown_text)
    markdown_text = _isolate_display_math_blocks(markdown_text)
    return markdown_text


def _isolate_display_math_blocks(markdown_text: str) -> str:
    """Ensure ``$$...$$`` display math sits on its own block.

    Single-line display math inline with prose (e.g. ``foo $$bar$$ baz``) is
    rewritten with blank lines around the equation so markdown renderers treat
    it as a display block. Already block-level equations are left intact.
    """
    pattern = re.compile(r"\$\$[\s\S]+?\$\$")

    pieces: list[str] = []
    cursor = 0
    for match in pattern.finditer(markdown_text):
        before = markdown_text[cursor:match.start()]
        between = before.strip("\n")
        stripped = between.strip(" \t")

        if cursor == 0:
            if stripped:
                pieces.append(stripped)
                pieces.append("\n\n")
        else:
            pieces.append("\n\n")
            if stripped:
                pieces.append(stripped)
                pieces.append("\n\n")

        pieces.append(match.group(0))
        cursor = match.end()

    if cursor == 0:
        return markdown_text

    tail = markdown_text[cursor:].strip("\n")
    tail_stripped = tail.strip(" \t")
    if tail_stripped:
        pieces.append("\n\n")
        pieces.append(tail_stripped)

    return "".join(pieces)


def _postprocess_pdf_markdown(markdown_text: str) -> str:
    """Clean OCR-derived markdown without affecting the HTML article pipeline."""
    markdown_text = html.unescape(markdown_text)
    markdown_text = _postprocess_markdown(markdown_text)
    markdown_text = _normalize_latex_in_markdown(markdown_text)
    markdown_text = _normalize_inline_math_spacing(markdown_text)
    return markdown_text


def _normalize_latex_in_markdown(markdown_text: str) -> str:
    """Tighten noisy OCR-produced LaTeX inside markdown math spans."""
    pattern = re.compile(r"\$\$(.+?)\$\$|\$(.+?)\$", flags=re.DOTALL)

    def repl(match: re.Match) -> str:
        display = match.group(1)
        inline = match.group(2)
        body = display if display is not None else inline
        cleaned = _normalize_latex_math(body)
        return f"$${cleaned}$$" if display is not None else f"${cleaned}$"

    return pattern.sub(repl, markdown_text)


def _normalize_latex_math(body: str) -> str:
    body = body.strip()
    body = re.sub(r"\\([A-Za-z]+)\s+\{", r"\\\1{", body)
    body = re.sub(r"\s*([_^])\s*", r"\1", body)
    body = re.sub(r"\{\s+", "{", body)
    body = re.sub(r"\s+\}", "}", body)
    body = re.sub(r"\}\s+\{", "}{", body)
    body = re.sub(r"\(\s+", "(", body)
    body = re.sub(r"\s+\)", ")", body)
    body = re.sub(r"\[\s+", "[", body)
    body = re.sub(r"\s+\]", "]", body)
    body = re.sub(r"\s+([,:;])", r"\1", body)
    body = re.sub(r"([({\[])\s+", r"\1", body)
    body = re.sub(r"\s+([)}\]])", r"\1", body)
    body = re.sub(
        r"(\\(?:mathrm|mathbf|mathcal|mathbb|mathsf|mathtt|textit|textrm)\{)([A-Za-z0-9 ]+)(\})",
        lambda m: m.group(1) + re.sub(r"\s+", "", m.group(2)) + m.group(3),
        body,
    )
    body = re.sub(r"\s{2,}", " ", body)
    return body.strip()


def _normalize_inline_math_spacing(markdown_text: str) -> str:
    """Tighten OCR-introduced spacing around inline math in prose."""
    inline_math = r"(\$[^$\n]+\$)"
    markdown_text = re.sub(rf"  +{inline_math}", r" \1", markdown_text)
    markdown_text = re.sub(rf"{inline_math}  +", r"\1 ", markdown_text)
    markdown_text = re.sub(rf"{inline_math}\s+([,.;:!?])", r"\1\2", markdown_text)
    markdown_text = re.sub(rf"([(\[])\s+{inline_math}", r"\1\2", markdown_text)
    markdown_text = re.sub(rf"{inline_math}\s+([)\]])", r"\1\2", markdown_text)
    return markdown_text


def _prepend_source_link_html(content_html: str, url: str) -> str:
    if not url:
        return content_html
    source_html = f'<p>Source: <a href="{url}">{url}</a></p><hr />'
    return source_html + content_html


def _prepare_html_for_markdown(soup: BeautifulSoup) -> None:
    """Strip presentation-only HTML so Pandoc can emit cleaner markdown."""
    _separate_inline_math_from_text(soup)
    _remove_html_comments(soup)
    _normalize_figures_for_markdown(soup)
    _remove_image_utility_blocks(soup)
    _normalize_tables_for_markdown(soup)
    _normalize_numbered_ul_to_ol(soup)
    _normalize_lists_for_markdown(soup)

    for tag in soup.find_all(["script", "style", "svg", "noscript"]):
        tag.decompose()

    # Keep only content-bearing attributes that matter in markdown output.
    keep_attrs = {
        "a": {"href", "title"},
        "img": {"src", "alt", "title"},
        "th": {"colspan", "rowspan"},
        "td": {"colspan", "rowspan"},
        "math": {"display"},
    }
    for tag in soup.find_all(True):
        allowed = keep_attrs.get(tag.name, set())
        for attr in list(tag.attrs):
            if attr not in allowed:
                del tag.attrs[attr]

    # Unwrap layout-only containers after attributes are stripped.
    for tag in soup.find_all(["span", "div", "section", "article", "main"]):
        tag.unwrap()


def _normalize_tables_for_markdown(soup: BeautifulSoup) -> None:
    """Remove presentation-only table rows that force Pandoc to emit raw HTML."""
    for row in list(soup.find_all("tr")):
        cells = row.find_all(["td", "th"], recursive=False)
        if len(cells) != 1:
            continue

        cell = cells[0]
        if not cell.get("colspan"):
            continue

        text = cell.get_text(" ", strip=True)
        if cell.find("hr") or (text and set(text) <= {"-", "=", " "}):
            row.decompose()


def _node_has_substantive_content(node) -> bool:
    if isinstance(node, NavigableString):
        return bool(str(node).strip())

    if not getattr(node, "name", None):
        return False

    if node.name in {"img", "math", "table", "pre", "code"}:
        return True

    if node.get_text(" ", strip=True):
        return True

    return node.find(["img", "math", "table", "pre", "code"]) is not None


_REFERENCE_LABEL_PATTERN = re.compile(r"^\[?(\d+)\]?\.?$")
_REFERENCE_TEXT_PREFIX_PATTERN = re.compile(r"^\[?(\d+)\]?\.\s+")


def _normalize_numbered_ul_to_ol(soup: BeautifulSoup) -> None:
    """Convert `<ul>` lists that encode an explicit numeric sequence into `<ol>`.

    PMC references wrap each entry as
    `<li><span class="label">1.</span><cite>Text</cite>...</li>` with no
    whitespace between the label span and the cite, so Pandoc emits bullet
    items like `- 1.Text`. Other publishers drop the number directly in the
    `<li>` text (e.g. `<li>1. Hicks, R. ...</li>`), which Pandoc escapes to
    `- 1\\. Hicks...`. In both cases the correct representation is an ordered
    list: we promote the parent `<ul>` to `<ol>` and remove the redundant
    numeric prefix so Pandoc renders a clean numbered list.
    """
    for list_tag in list(soup.find_all("ul")):
        items = list_tag.find_all("li", recursive=False)
        if len(items) < 2:
            continue

        span_labels: list = []
        text_prefixes: list = []
        numbers: list = []
        mode = None

        for li in items:
            first_element = None
            leading_text_node = None
            for child in li.children:
                if isinstance(child, NavigableString):
                    if str(child).strip():
                        leading_text_node = child
                        break
                    continue
                if getattr(child, "name", None):
                    first_element = child
                    break

            item_number = None
            if (
                leading_text_node is None
                and first_element is not None
                and first_element.name == "span"
                and "label" in (first_element.get("class") or [])
            ):
                match = _REFERENCE_LABEL_PATTERN.match(first_element.get_text(strip=True))
                if match:
                    if mode is None:
                        mode = "span"
                    if mode == "span":
                        span_labels.append(first_element)
                        item_number = int(match.group(1))

            if item_number is None and leading_text_node is not None:
                match = _REFERENCE_TEXT_PREFIX_PATTERN.match(str(leading_text_node))
                if match:
                    if mode is None:
                        mode = "text"
                    if mode == "text":
                        text_prefixes.append((leading_text_node, match.end()))
                        item_number = int(match.group(1))

            if item_number is None:
                mode = None
                break
            numbers.append(item_number)

        if mode is None or len(numbers) != len(items):
            continue

        if numbers[0] != 1 or any(b - a != 1 for a, b in zip(numbers, numbers[1:])):
            continue

        if mode == "span":
            for span in span_labels:
                span.decompose()
        else:
            for node, end in text_prefixes:
                remainder = str(node)[end:]
                node.replace_with(NavigableString(remainder))

        list_tag.name = "ol"
        if "class" in list_tag.attrs:
            del list_tag.attrs["class"]
        if "style" in list_tag.attrs:
            del list_tag.attrs["style"]


def _normalize_lists_for_markdown(soup: BeautifulSoup) -> None:
    """Normalize loose list markup so Pandoc emits compact, valid markdown lists."""
    for list_tag in soup.find_all(["ul", "ol"]):
        for child in list(list_tag.children):
            if isinstance(child, NavigableString):
                if not str(child).strip():
                    child.extract()
                continue
            if getattr(child, "name", None) == "br":
                child.decompose()
                continue
            if getattr(child, "name", None) != "li" and not _node_has_substantive_content(child):
                child.decompose()

    for li in list(soup.find_all("li")):
        for br in list(li.find_all("br")):
            br.replace_with(" ")

        for child in list(li.find_all(["p", "div", "span"], recursive=False)):
            if _node_has_substantive_content(child):
                child.unwrap()
            else:
                child.decompose()

        direct_lists = [child for child in li.contents if getattr(child, "name", None) in {"ul", "ol"}]
        direct_non_list = [child for child in li.contents if child not in direct_lists]

        text_bits = []
        has_non_text_content = False
        for child in direct_non_list:
            if isinstance(child, NavigableString):
                text = str(child).strip()
                if text:
                    text_bits.append(text)
                continue

            if not getattr(child, "name", None):
                continue

            if child.name == "br":
                continue

            if child.name in {"img", "math", "table", "pre", "code"}:
                has_non_text_content = True
                continue

            text = child.get_text(" ", strip=True)
            if text:
                text_bits.append(text)

        has_non_list_content = has_non_text_content or bool(" ".join(text_bits).strip())
        if not has_non_list_content:
            for nested_list in direct_lists:
                li.insert_before(nested_list.extract())
            li.decompose()


def _normalize_code_listing_tables(soup: BeautifulSoup) -> None:
    """Replace syntax-highlighter layout tables with semantic pre/code blocks."""
    for table in list(soup.find_all("table")):
        classes = set(table.get("class", []))
        code_cell = table.find("td", class_=lambda c: c and "urvanov-syntax-highlighter-code" in c.split())
        if "crayon-table" not in classes and code_cell is None:
            continue

        if code_cell is None:
            code_cell = table.find("td")
        if code_cell is None:
            continue

        lines = []
        for line in code_cell.select(".crayon-line"):
            text = line.get_text("", strip=False).replace("\xa0", " ")
            lines.append(text.rstrip())

        if not lines:
            text = code_cell.get_text("\n", strip=False).replace("\xa0", " ")
            lines = [part.rstrip() for part in text.splitlines()]

        code_text = "\n".join(lines).strip("\n")
        if not code_text:
            continue

        pre = soup.new_tag("pre")
        code = soup.new_tag("code")
        code.append(NavigableString(code_text))
        pre.append(code)
        table.replace_with(pre)


def _remove_latexml_figure_placeholders(soup: BeautifulSoup) -> None:
    """Drop broken LaTeXML figure placeholders that Pandoc turns into bad LaTeX.

    Some arXiv HTML pages contain literal fallback nodes like `\\includegraphics`
    plus sibling text `[width=0.92]fig1_frontier.pdf` instead of a real <img>.
    Keeping those nodes causes malformed figure environments in Pandoc/XeLaTeX.
    """
    command_pattern = re.compile(r"^\\[A-Za-z@]+$")
    file_token_pattern = re.compile(r"[A-Za-z0-9_.-]+\.(?:pdf|png|jpe?g|svg)\b")

    for tag in list(soup.find_all(class_=lambda c: c and "ltx_ERROR" in c.split())):
        text = tag.get_text(" ", strip=True)
        if command_pattern.match(text):
            tag.decompose()

    panel_names = {"p", "div", "span"}
    for tag in list(soup.find_all(panel_names)):
        classes = set(tag.get("class", []))
        if "ltx_figure_panel" not in classes:
            continue
        if tag.find(["img", "svg", "math", "table"]):
            continue

        text = tag.get_text(" ", strip=True)
        if not file_token_pattern.search(text):
            continue
        if "[width=" in text or "\\includegraphics" in text or "\\subfloat" in text:
            tag.decompose()


def _prepare_html_for_pdf(soup: BeautifulSoup) -> None:
    """Normalize HTML so Pandoc/LaTeX can size images to the page reliably."""
    _separate_inline_math_from_text(soup)
    _remove_html_comments(soup)
    for figure in soup.find_all("figure"):
        if not figure.find("img"):
            continue
        if not figure.find("figcaption"):
            continue
        for child in list(figure.find_all(["h1", "h2", "h3", "h4", "h5", "h6"], recursive=False)):
            child.decompose()

    for anchor in list(soup.find_all("a")):
        children = [c for c in anchor.contents if getattr(c, "name", None) or str(c).strip()]
        if len(children) == 1 and getattr(children[0], "name", None) == "img":
            anchor.replace_with(children[0].extract())

    _remove_image_utility_blocks(soup)

    for img in soup.find_all("img"):
        for attr in ("width", "height", "srcset", "sizes", "loading", "decoding", "style"):
            img.attrs.pop(attr, None)


def _meaningful_children(tag) -> list:
    return [c for c in tag.contents if getattr(c, "name", None) or str(c).strip()]


def _standalone_block_image(tag):
    direct_images = tag.find_all("img", recursive=False)
    if len(direct_images) == 1:
        for child in _meaningful_children(tag):
            if child is direct_images[0]:
                continue
            if getattr(child, "name", None) == "figcaption":
                continue
            return None
        return direct_images[0]

    children = _meaningful_children(tag)
    if len(children) == 1 and getattr(children[0], "name", None) == "a":
        anchor_images = children[0].find_all("img", recursive=False)
        if len(anchor_images) == 1:
            return anchor_images[0]

    if (
        len(children) == 2
        and getattr(children[0], "name", None) == "a"
        and getattr(children[1], "name", None) == "figcaption"
    ):
        anchor_images = children[0].find_all("img", recursive=False)
        if len(anchor_images) == 1:
            return anchor_images[0]

    if len(children) == 1 and getattr(children[0], "name", None) in {"p", "div", "figure"}:
        return _standalone_block_image(children[0])

    if (
        len(children) == 2
        and getattr(children[1], "name", None) == "figcaption"
        and getattr(children[0], "name", None) in {"p", "div", "figure"}
    ):
        return _standalone_block_image(children[0])

    return None


def _is_standalone_image_block(tag) -> bool:
    return _standalone_block_image(tag) is not None


def _looks_like_caption_block(tag) -> bool:
    if tag is None or getattr(tag, "name", None) not in {"p", "div", "figcaption"}:
        return False
    if tag.find("img"):
        return False
    text = tag.get_text(" ", strip=True)
    return len(text) >= 40


def _remove_image_utility_blocks(soup: BeautifulSoup) -> None:
    for block in list(soup.find_all(["p", "div"])):
        children = _meaningful_children(block)
        if len(children) != 1:
            continue
        child = children[0]
        if getattr(child, "name", None) != "a":
            continue
        if block.find("img"):
            continue

        prev = block.find_previous_sibling(["img", "p", "div", "figure", "figcaption"])
        if prev is None:
            continue
        if _looks_like_caption_block(prev):
            prev = prev.find_previous_sibling(["img", "p", "div", "figure"])
        if prev is None:
            continue
        if getattr(prev, "name", None) != "img" and not _is_standalone_image_block(prev):
            continue
        block.decompose()


def _normalize_figures_for_markdown(soup: BeautifulSoup) -> None:
    """Flatten HTML figure wrappers into image + caption paragraphs."""
    for figure in soup.find_all("figure"):
        replacements = []

        img = figure.find("img")
        if img:
            replacements.append(img.extract())

        caption = figure.find("figcaption")
        if caption:
            caption_text = caption.get_text(" ", strip=True)
            if caption_text:
                caption_p = soup.new_tag("p")
                caption_p.append(NavigableString(caption_text))
                replacements.append(caption_p)

        if replacements:
            _replace_with_sequence(figure, replacements)


def _generate_pdf(
    md_text: str,
    title: str,
    article_dir: Path,
    papersize: str = "a5",
) -> Path:
    """Generate PDF from final markdown via Pandoc -> HTML -> Chromium."""
    pdf_file = article_dir / f"{article_dir.name}.pdf"
    assets_src = article_dir / "assets"
    md_text = _sanitize_unicode_text(md_text)

    with tempfile.TemporaryDirectory(prefix="scribe-pdf-") as stage_tmp:
        stage_dir = Path(stage_tmp)
        stage_pdf = stage_dir / f"{article_dir.name}.pdf"
        stage_html = stage_dir / "article.html"

        if assets_src.exists():
            shutil.copytree(assets_src, stage_dir / "assets")
        html_fragment = _render_markdown_to_html(md_text)
        stage_html.write_text(
            _wrap_html_for_browser_pdf(html_fragment, title, papersize),
            encoding="utf-8",
        )

        try:
            _render_html_to_pdf_with_chromium(stage_html, stage_pdf)
            shutil.copy2(stage_pdf, pdf_file)
            return pdf_file
        finally:
            stage_pdf.unlink(missing_ok=True)


def _render_markdown_to_html(md_text: str) -> str:
    """Convert final markdown into HTML with native MathML output."""
    return pypandoc.convert_text(
        md_text,
        format="md",
        to="html5",
        extra_args=["--mathml"],
    )


def _wrap_html_for_browser_pdf(html_fragment: str, title: str, papersize: str) -> str:
    page_size = "A4" if papersize.lower() == "a4" else "A5"
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <title>{html.escape(title)}</title>
    <style>
      @page {{
        size: {page_size};
        margin: 10mm;
      }}
      html {{
        -webkit-print-color-adjust: exact;
        print-color-adjust: exact;
      }}
      body {{
        font-family: "Noto Serif", "Source Serif 4", "FreeSerif", serif;
        font-size: 11pt;
        line-height: 1.6;
        color: #111;
        max-width: 100%;
      }}
      main {{
        max-width: 100%;
      }}
      h1, h2, h3, h4 {{
        font-family: "Noto Serif", "Source Serif 4", "FreeSerif", serif;
        font-weight: 700;
        line-height: 1.2;
        break-after: avoid-page;
      }}
      h1 {{
        font-size: 20pt;
        margin: 0 0 1.2rem;
      }}
      h2 {{
        font-size: 15pt;
        margin: 1.8rem 0 0.8rem;
      }}
      h3 {{
        font-size: 12.5pt;
        margin: 1.4rem 0 0.6rem;
      }}
      p, li {{
        orphans: 3;
        widows: 3;
      }}
      a {{
        color: inherit;
        text-decoration-thickness: 0.06em;
      }}
      img, svg {{
        display: block;
        max-width: 100%;
        height: auto;
        margin: 0.8rem auto;
      }}
      figure {{
        break-inside: avoid;
        page-break-inside: avoid;
        margin: 1.2rem 0;
      }}
      figcaption {{
        font-size: 9.5pt;
        line-height: 1.45;
        color: #444;
        text-align: center;
      }}
      table {{
        width: 100%;
        border-collapse: collapse;
        margin: 1rem 0 1.4rem;
        font-size: 9.5pt;
      }}
      th, td {{
        border: 1px solid #cfcfcf;
        padding: 0.45rem 0.55rem;
        vertical-align: top;
      }}
      th {{
        background: #f4f1ea;
        font-weight: 700;
      }}
      pre {{
        white-space: pre-wrap;
        background: #f7f5ef;
        border: 1px solid #e2ddd1;
        border-radius: 4px;
        padding: 0.9rem 1rem;
        overflow-wrap: normal;
        word-break: normal;
        tab-size: 4;
        line-height: 1.45;
        margin: 0.9rem 0 1.2rem;
        break-inside: avoid-page;
        page-break-inside: avoid;
      }}
      pre, pre code {{
        font-family: "Noto Sans Mono", "FreeMono", monospace;
        font-size: 9.2pt;
      }}
      pre code {{
        white-space: inherit;
        overflow-wrap: inherit;
        word-break: inherit;
      }}
      code {{
        font-family: "Noto Sans Mono", "FreeMono", monospace;
        font-size: 0.92em;
      }}
      :not(pre) > code {{
        background: #f3efe6;
        border-radius: 3px;
        padding: 0.08rem 0.28rem;
      }}
      blockquote {{
        margin: 1rem 0;
        padding-left: 1rem;
        border-left: 3px solid #d6d0c3;
        color: #444;
      }}
      hr {{
        border: 0;
        border-top: 1px solid #d8d2c7;
        margin: 1.6rem 0;
      }}
      math[display="block"] {{
        display: block;
        overflow-x: auto;
        margin: 1rem 0;
      }}
      .title-block-header,
      header {{
        margin-bottom: 1.6rem;
      }}
    </style>
  </head>
  <body>
    <main>
{html_fragment}
    </main>
  </body>
</html>
"""


def _render_html_to_pdf_with_chromium(html_file: Path, pdf_file: Path) -> None:
    browser = (
        shutil.which("chromium")
        or shutil.which("chromium-browser")
        or shutil.which("google-chrome")
    )
    if not browser:
        raise RuntimeError("Chromium executable not found for HTML PDF render")

    with tempfile.TemporaryDirectory(prefix="scribe-chromium-") as chrome_tmp:
        chrome_tmp_path = Path(chrome_tmp)
        env = os.environ.copy()
        env["HOME"] = str(chrome_tmp_path)
        env["XDG_CONFIG_HOME"] = str(chrome_tmp_path / "config")
        env["XDG_CACHE_HOME"] = str(chrome_tmp_path / "cache")
        env["XDG_RUNTIME_DIR"] = str(chrome_tmp_path / "runtime")

        for key in ("XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
            Path(env[key]).mkdir(parents=True, exist_ok=True)

        proc = subprocess.run(
            [
                browser,
                "--headless=new",
                "--disable-gpu",
                "--disable-dev-shm-usage",
                "--disable-crash-reporter",
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--no-first-run",
                "--no-default-browser-check",
                f"--user-data-dir={chrome_tmp_path / 'profile'}",
                "--allow-file-access-from-files",
                "--no-pdf-header-footer",
                "--print-to-pdf-no-header",
                f"--print-to-pdf={pdf_file}",
                html_file.resolve().as_uri(),
            ],
            capture_output=True,
            text=True,
            check=False,
            env=env,
        )
    if proc.returncode != 0 or not pdf_file.exists() or pdf_file.stat().st_size == 0:
        output = "\n".join(part for part in (proc.stdout, proc.stderr) if part).strip()
        raise RuntimeError(output or "Chromium PDF render failed")


def _separate_inline_math_from_text(soup: BeautifulSoup) -> None:
    """Insert spacing when inline math touches plain text with no separator.

    arXiv/LaTeXML often emits patterns like <math>\\approx</math>40, which
    Pandoc can serialize via raw HTML comment markers that later break XeLaTeX.
    """
    for math_tag in soup.find_all("math"):
        if math_tag.get("display") == "block":
            continue

        prev_node = math_tag.previous_sibling
        if isinstance(prev_node, NavigableString):
            prev_text = str(prev_node)
            if prev_text and not prev_text[-1].isspace() and re.search(r"[\w\)\]\}.,;:!?]$", prev_text):
                prev_node.replace_with(NavigableString(prev_text + " "))

        next_node = math_tag.next_sibling
        if isinstance(next_node, NavigableString):
            next_text = str(next_node)
            if next_text and not next_text[0].isspace() and re.match(r"[\w\(\[\{]", next_text):
                next_node.replace_with(NavigableString(" " + next_text))


def _remove_html_comments(soup: BeautifulSoup) -> None:
    """Drop HTML comments that Pandoc can translate into broken raw LaTeX fragments."""
    for comment in soup.find_all(string=lambda value: isinstance(value, Comment)):
        comment.extract()

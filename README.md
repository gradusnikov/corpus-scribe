# Corpus Scribe

Corpus Scribe turns web articles and PDFs into a **personalized, high-quality, LLM-friendly knowledge base**.

It captures messy publisher pages and source PDFs, normalizes them into **clean Markdown-first article bundles**, preserves local assets, generates citation metadata, and optionally writes companion notes. Reading PDFs are still supported, but they are a derived artifact rather than the primary output.

Comes with a Chrome extension for one-click capture and a Dockerized Flask backend.

## Why use it

- Build a personal research corpus in plain Markdown instead of scattered bookmarks, PDFs, and clipped HTML.
- Create a knowledge base that is easier for local agents and LLM workflows to read, search, summarize, and link.
- Preserve higher-quality source structure than generic clipping tools: equations, tables, code blocks, images, bibliography, and notes.
- Keep both provenance and usability:
  - original source PDF when applicable
  - canonical `.md`
  - optional `.notes.md`
  - optional reading PDF for Kindle or offline reading

## Ways to use it

- Build a personal LLM knowledge base from articles, blog posts, papers, and technical documentation.
- Save papers into labeled folders like `ml`, `agents`, `biology`, or `ideas` and query them later with local tools.
- Generate clean reading copies from difficult source PDFs without making the PDF workflow the center of the system.
- Maintain a private markdown library with stable frontmatter, bibliography files, notes, and an index that can be processed by scripts or agents.

## Project structure

```
corpus-scribe/
  docker-compose.yml
  output/                     # saved PDFs and Markdown
  backend/
    Dockerfile
    main.py                   # Flask API
    article_extractor.py      # article extraction + HTML/MD/PDF pipeline
    send_email_gmail.py       # Gmail API (Kindle delivery)
    requirements.txt
    test_article_extractor.py # end-to-end extractor regression tests
  extension/
    manifest.json             # Chrome Manifest V3
    popup.html / popup.js     # extension UI
    background.js
    assets/
```

## Quick start

```bash
docker compose up -d
```

The backend starts on `http://localhost:5000`. Saved files appear in `./output/<label>/<article>/`.
The canonical artifact is the saved `.md` file. Notes, bibliography, source PDFs, and reading PDFs are derived artifacts around that markdown core.
The backend container runs as `${UID:-1000}:${GID:-1000}` by default so files created under `./output/` stay owned by your host user.

If you want markdown and image assets to open cleanly in Windows apps from outside WSL, point the output mount at a Windows-backed folder.
Create a `.env` file from `.env.example` and set:

```bash
HOST_OUTPUT_DIR=/mnt/c/Users/<you>/Documents/corpus-scribe
```

Then restart the backend with:

```bash
docker compose up -d
```

Inside the container the backend still writes to `/output`, but Docker will map that to your configured host folder.

### Chrome extension

1. Open `chrome://extensions/`
2. Enable **Developer mode**
3. Click **Load unpacked** and select the `extension/` directory
4. Navigate to any article or PDF and click the extension icon
5. Choose or create a **Label** in the popup
6. The popup will show what it detected:
   - `Detected: Web article`
   - or for PDFs:
     - `Detected: PDF`
     - `Extraction: Mistral OCR`
     - or `Extraction: pdftotext fallback`
7. Choose **Send to Kindle** or **Save PDF + MD**

The usual workflow is:

- capture article or PDF
- store it under a label
- keep the markdown as the main artifact
- optionally generate notes and reading PDFs

## API

All endpoints accept JSON with an `apiKey` field for authentication (default: `api-key-1234`, configurable via `API_KEY` env var).

### `POST /save_local`

Extract article and save PDF + Markdown to the output directory.

```json
{
  "apiKey": "api-key-1234",
  "label": "Machine Learning",
  "url": "https://example.com/article",
  "html": "<html>...</html>",
  "cookies": {"domain": {"name": "value"}}
}
```

Response:
```json
{
  "success": true,
  "title": "Article Title",
  "label": "Machine Learning",
  "primary": "/output/Machine Learning/Article Title/Article Title.md",
  "pdf": "/output/Machine Learning/Article Title/Article Title.pdf",
  "bib": "/output/Machine Learning/Article Title/Article Title.bib",
  "notes": "/output/Machine Learning/Article Title/Article Title.notes.md",
  "pdfAvailable": true,
  "notesAvailable": true,
  "md": "/output/Machine Learning/Article Title/Article Title.md",
  "metadata": {
    "source_site": "example.com",
    "citation_key": "doe2026articletitle",
    "doi": "10.1000/example",
    "language": "en",
    "word_count": 1234,
    "image_count": 4,
    "ingested_at": "2026-04-12T10:00:00+00:00"
  }
}
```

Optional `notes` request overrides for local saves:

```json
{
  "notes": {
    "provider": "openai",
    "model": "gpt-4.1-mini",
    "base_url": "https://api.openai.com/v1",
    "api_key": "sk-..."
  }
}
```

### `POST /save_pdf`

Download a source PDF, save the original PDF locally, extract markdown from the PDF with Mistral OCR when configured, and optionally generate companion notes. If OCR is unavailable or fails, the backend falls back to `pdftotext`.

For PDF-source saves, the backend keeps two PDF artifacts:

- `sourcePdf`: the original downloaded PDF for archival fidelity
- `pdf`: a regenerated reading PDF created from the cleaned markdown for better Kindle reading
- `bib`: a generated bibliography entry for citation workflows

```json
{
  "apiKey": "api-key-1234",
  "label": "Papers",
  "url": "https://example.com/paper.pdf",
  "sourceName": "paper.pdf",
  "cookies": {"domain": {"name": "value"}}
}
```

Response fields of interest:

```json
{
  "pdf": "/output/Papers/Article Title/Article Title.reading.pdf",
  "sourcePdf": "/output/Papers/Article Title/Article Title.source.pdf",
  "bib": "/output/Papers/Article Title/Article Title.bib",
  "pdfAvailable": true,
  "sourcePdfAvailable": true,
  "primary": "/output/Papers/Article Title/Article Title.md"
}
```

### `GET /labels`

List existing output labels so the extension can offer them in the save dialog.

Query params:

- `apiKey=...`

### `GET /capabilities`

Return lightweight backend capability information used by the popup.

Query params:

- `apiKey=...`

Response:

```json
{
  "success": true,
  "pdfOcr": {
    "available": true,
    "engine": "mistral",
    "fallback": "pdftotext"
  }
}
```

### `POST /generate_pdf`

Extract article and send PDF to Kindle via Gmail.

```json
{
  "apiKey": "api-key-1234",
  "url": "https://example.com/article",
  "html": "<html>...</html>",
  "cookies": {},
  "email": "you@gmail.com",
  "kindleEmail": "you@kindle.com"
}
```

### `GET /health`

Returns `{"status": "ok"}`.

## Kindle delivery setup

To use the **Send to Kindle** feature, you need Google OAuth credentials:

1. Create a project in [Google Cloud Console](https://console.cloud.google.com/)
2. Enable the Gmail API
3. Create OAuth 2.0 credentials (Desktop app type)
4. Download `credentials.json`
5. Uncomment the credential mounts in `docker-compose.yml`:
   ```yaml
   volumes:
     - ./output:/output
     - ./credentials.json:/app/credentials.json:ro
     - ./token.json:/app/token.json
   ```
6. On first run, the backend will open a browser for OAuth consent and save `token.json`

Also add your Gmail address to the **Approved Personal Document E-mail List** in your [Kindle settings](https://www.amazon.com/hz/mycd/myx#/home/settings/payment).

## Configuration

| Env variable | Default | Description |
|---|---|---|
| `API_KEY` | `api-key-1234` | API authentication key |
| `OUTPUT_DIR` | `/output` | Directory for saved PDFs and Markdown |
| `HOST_OUTPUT_DIR` | `./output` | Host-side bind mount target for `/output` in `docker-compose.yml` |
| `UID` | `1000` | UID used to run the backend container |
| `GID` | `1000` | GID used to run the backend container |
| `MISTRAL_API_KEY` | empty | Enables Mistral OCR as the primary PDF-to-markdown extractor |
| `MISTRAL_BASE_URL` | `https://api.mistral.ai` | Base URL for Mistral OCR API |
| `MISTRAL_OCR_MODEL` | `mistral-ocr-latest` | Mistral OCR model used for source PDFs |

## Extraction pipeline

1. The Chrome extension captures the current page's full HTML and cookies
2. The backend extracts the main article content, preferring a real `<article>` tag and falling back to [Readability](https://github.com/mozilla/readability) (`readability-lxml`) when needed
3. Images are downloaded with the captured browser cookies so paywalled/CDN-backed assets can still be resolved
4. Publisher-specific math wrappers are normalized before conversion:
   - PubMed/PMC display equations stored in `table.disp-formula` are unwrapped so they are treated as equations, not tables
   - ScienceDirect/MathJax blocks are normalized from embedded MathML or MathJax markup
   - real MathML is preserved whenever available
5. Presentation-only HTML is cleaned before markdown generation so the `.md` output is not polluted with layout wrappers and raw HTML blocks
6. Lists, code-listing tables, and figure wrappers are normalized before conversion so markdown readers do not get empty bullets, broken code blocks, or layout-only captions
7. Markdown is generated from normalized HTML with Pandoc, not `html2text`, so equations, tables, and code survive conversion much more reliably
8. The markdown file is written as the canonical saved artifact with structured YAML frontmatter for downstream indexing and LLM use
9. A sibling `*.bib` file is generated from the extracted citation metadata so each saved document has an immediately usable bibliography entry
10. Markdown is post-processed to remove same-document fragment links and other raw HTML artifacts that are harmless in browsers but noisy in markdown readers
11. Local saves can also generate a companion `*.notes.md` file using a local OpenAI-compatible model endpoint
12. PDF generation uses a straightforward markdown-first path:
   - Pandoc converts the generated markdown to HTML
   - the backend wraps that HTML in an academic print stylesheet
   - headless Chromium renders the final self-contained PDF without browser print headers or footers
13. PDF tabs use a native PDF source path instead of HTML extraction:
   - the backend downloads the source PDF with the captured browser cookies
   - Mistral OCR is the primary extractor for PDF-to-markdown conversion when `MISTRAL_API_KEY` is configured
   - the raw OCR response is cached as `ocr_response.json` beside the extracted files
   - `pdftotext` is only the fallback path for OCR-disabled or OCR-failed PDFs
   - OCR-derived markdown goes through a PDF-specific cleanup pass to unescape OCR HTML entities, normalize LaTeX spacing, and tighten inline math in prose
   - the original PDF is saved as `*.source.pdf`
   - a separate reading PDF is regenerated from the cleaned markdown and saved as `*.reading.pdf`
14. Local saves are written under `/output/<label>/<article>/`
15. Local saves are markdown-primary: if PDF rendering fails, the markdown still remains saved successfully
16. Files are either saved locally or emailed to Kindle via Gmail API

## Markdown metadata

Saved articles include YAML frontmatter intended to make the markdown corpus easier to index and query later. Current fields include:

- `doc_id`
- `doc_type`
- `title`
- `author`
- `date`
- `url`
- `canonical_url`
- `source_site`
- `label`
- `language`
- `description`
- `citation_key`
- `doi`
- `arxiv_id`
- `source_format`
- `source_file`
- `ocr_engine`
- `page_count`
- `word_count`
- `image_count`
- `ingested_at`
- `notes_file`
- `notes_doc_id`
- `bib_file`

For companion notes, the frontmatter also includes:

- `source_article`
- `source_doc_id`

## Companion notes

For local saves, the backend can generate a sibling `*.notes.md` file using a configurable LLM provider. The current default is Anthropic:

- `NOTES_LLM_PROVIDER` default: `anthropic`
- `NOTES_LLM_MODEL` default: `claude-sonnet-4-20250514`
- `NOTES_LLM_API_KEY` must be set for Anthropic notes generation

The notes file is a best-effort derived artifact intended for later review and LLM workflows. The canonical source remains the extracted article markdown.

Supported providers:

- `openai_compatible`
  Uses the OpenAI-style `POST /chat/completions` API. This works with local endpoints such as LM Studio.
- `openai`
  Also uses `POST /chat/completions`, but you typically point `NOTES_LLM_BASE_URL` at `https://api.openai.com/v1`.
- `anthropic`
  Uses `POST https://api.anthropic.com/v1/messages` with `NOTES_LLM_API_KEY` and `NOTES_ANTHROPIC_VERSION`.

Configuration can come from either:

- environment variables in `.env`
- per-request `notes` overrides sent to `POST /save_local`

## Knowledge Base Index

Local saves also maintain a generated `index.jsonl` file at the output root:

- `output/index.jsonl`

It contains one JSON object per saved article or notes file, including:

- `doc_id`
- `type`
- `title`
- `label`
- `path`
- `pdf_path`
- `source_pdf_path`
- `bib_path`
- `notes_path`
- `source_article_path`
- `source_doc_id`
- `url`
- `canonical_url`
- `source_site`
- `source_format`
- `ocr_engine`
- `citation_key`
- `doi`
- `arxiv_id`
- `language`
- `word_count`
- `image_count`
- `ingested_at`

The index is deterministic and path-based, so repeated saves update records instead of blindly appending duplicates.

## Math and table handling

- Math is not reconstructed from hardcoded Unicode replacement tables.
- The pipeline keeps MathML whenever possible and only uses fallback MathJax conversion when the source page no longer exposes real MathML.
- Display-formula wrapper tables are treated as equation containers and removed before markdown/PDF conversion.
- Real content tables remain tables and are converted by Pandoc into markdown tables and styled HTML tables.
- Syntax-highlighter layout tables are converted into semantic fenced/indented code blocks before markdown and PDF generation.
- Loose publisher list markup is normalized so `ul` and `ol` items do not turn into blank bullets or extra spacing.

## PDF rendering notes

- The PDF engine is headless Chromium.
- The backend renders the saved markdown through a fixed academic HTML/CSS template before printing to PDF.
- PDFs are self-contained because images are resolved to local assets before browser rendering.
- Browser print headers and footers are disabled, so the PDF does not include `file://` paths, timestamps, or page chrome.
- Unicode text is handled through generic normalization and cleanup, not symbol-by-symbol mappings.
- Code blocks are preserved from markdown and rendered with monospace print styling rather than being flattened into body text.
- For source PDFs saved via `POST /save_pdf`, the original document is preserved separately as `*.source.pdf`.
- The default `pdf` returned by `POST /save_pdf` is the regenerated reading copy `*.reading.pdf`.
- For source PDFs, markdown quality is best when `MISTRAL_API_KEY` is configured and the backend can use Mistral OCR.
- The popup surfaces the planned PDF extraction path before save and the actual OCR engine after save.
- Every saved article also gets a sibling `*.bib` file generated from the extracted citation metadata.

## Testing

The extractor has a regression test that exercises:

- PubMed-style display math wrappers
- ScienceDirect-style embedded MathML
- Unicode-heavy prose
- HTML table conversion
- PDF text extraction checks to verify equations are rendered instead of showing literal `$$`

Run it in the backend container:

```bash
docker compose exec -T backend python -m unittest -v test_article_extractor
```

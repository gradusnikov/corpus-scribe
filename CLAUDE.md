# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

### Backend (Flask, runs inside Docker)

```bash
# Start / rebuild the backend container (repo root)
docker compose up -d --build backend

# Preferred starter that sources ~/.env first (so NOTES_LLM_API_KEY / MISTRAL_API_KEY
# can live in $HOME/.env instead of the repo-local .env)
./scripts/compose-with-home-env.sh up -d --build backend

# Run the full backend test suite
docker compose exec -T backend python -m unittest -v test_main test_article_extractor

# Run one file / one test
docker compose exec -T backend python -m unittest -v test_main
docker compose exec -T backend python -m unittest -v test_main.MainApiTests.test_desktop_save_rating_writes_frontmatter
```

**Important:** the backend `Dockerfile` bakes source into the image at build time — there is no host bind mount for `/app`. Options while iterating:

- `docker compose up -d --build backend` (slow, clean)
- `docker compose cp backend/main.py backend:/app/main.py && docker compose restart backend` (fast; running container only, next rebuild will still pick up host changes)

Host-side `pytest` / `python -m unittest` will **not** work — the tests depend on `pypandoc`, the Pandoc binary, and Chromium, all of which live in the image.

### Desktop reader (Vite + React, runs on host)

```bash
cd desktop
npm install
npm run dev      # http://localhost:1420/
npm run build    # tsc -b && vite build
npm run lint     # eslint .
npx tsc -b --noEmit   # type-check only
```

The reader talks to the Flask backend at `http://127.0.0.1:5000` by default; override with `VITE_DESKTOP_API_BASE`.

### Chrome extension

Load `extension/` unpacked in `chrome://extensions/` with developer mode on. No build step; it talks to `http://localhost:5000` using the `API_KEY` configured in the backend (default `api-key-1234`).

## Architecture

Three tiers cooperate around a **Markdown-first bundle layout on disk**. The `.md` file is the canonical artifact; every other file beside it is derived and regenerable.

```
output/<label>/<article>/
  Article Title.md             # canonical — includes YAML frontmatter
  Article Title.bib            # generated citation entry
  Article Title.notes.md       # optional LLM-generated companion notes
  Article Title.reading.pdf    # regenerated reading PDF (markdown → HTML → Chromium)
  Article Title.source.pdf     # only for PDF-source saves — the original download
  Article Title.highlights.json
  assets/                      # images downloaded with capture-time cookies
```

`output/index.jsonl` is a deterministic, path-keyed index of every article and notes record. It is **upserted** on each save (never blindly appended), so regenerating an article updates the existing line in place.

### Backend (`backend/`)

- `main.py` — Flask app. Two extraction entry points (`/save_local`, `/save_pdf`) and a Kindle delivery path (`/generate_pdf`). Everything under `/desktop/*` (library listing, document detail, notes CRUD, highlights, rating, file passthrough) powers the desktop reader and reads bundles **in place** — no separate storage model. Frontmatter is parsed with the in-file `_split_frontmatter` helper (not PyYAML) because the writer is a minimal quote-or-number emitter in `article_extractor._build_frontmatter`; round-tripping therefore assumes integers serialize as bare `key: 4` and strings as JSON-quoted values.
- `article_extractor.py` — the extraction pipeline. Non-obvious behaviors:
  - **HTML source path**: prefer a real `<article>` tag, fall back to Readability (`readability-lxml`). Before Pandoc runs, publisher-specific wrappers are normalized: PubMed/PMC `table.disp-formula` → equation, ScienceDirect/MathJax → MathML or LaTeX, syntax-highlighter layout tables → fenced code blocks, loose `ul`/`ol` layout → clean lists. Markdown is generated via **Pandoc** (`pypandoc`), not `html2text`, which is why equations, tables, and code survive the round-trip.
  - **PDF source path** (`extract_pdf_url`): downloads the PDF with capture-time cookies, caches the raw Mistral OCR response as `ocr_response.json`, and only falls back to `pdftotext` when Mistral is unconfigured or fails. OCR markdown goes through a PDF-specific cleanup pass to unescape HTML entities, normalize LaTeX spacing, and tighten inline math.
  - **Reading PDF**: Pandoc renders the cleaned markdown to HTML, wraps it in an academic print stylesheet, and headless Chromium prints a self-contained PDF (no browser chrome). Images are resolved to local assets first so the PDF is standalone.
  - **Companion notes**: `_generate_companion_notes` dispatches to one of three LLM clients — `anthropic`, `openai`, `openai_compatible` — selected from env vars (`NOTES_LLM_PROVIDER`, `NOTES_LLM_MODEL`, `NOTES_LLM_API_KEY`, `NOTES_LLM_BASE_URL`) or per-request `notes` overrides on `/save_local`.
- `send_email_gmail.py` — Gmail OAuth send-to-Kindle path used by `/generate_pdf`.
- `test_main.py` — Flask route tests (mock the extractor, use `tempfile.TemporaryDirectory` + `patch.object(main, "OUTPUT_DIR", …)` for filesystem isolation).
- `test_article_extractor.py` — end-to-end regression fixtures for math/table/code/unicode handling; the canonical example of what "correct extraction" means.

### Chrome extension (`extension/`)

Manifest V3. `background.js` captures the page's full HTML + cookies for the active tab; `popup.js` posts to `/save_local`, `/save_pdf`, or `/generate_pdf` based on whether the tab is an article or a PDF. The popup also calls `/capabilities` and `/labels` to populate its UI (OCR engine display, label dropdown).

### Desktop reader (`desktop/src/App.tsx`)

Single-file React app. It fetches bundles through the `/desktop/*` endpoints and renders Markdown with `react-markdown` + `remark-math` + `rehype-katex`. It does **not** introduce a new storage model — everything the reader shows comes from files produced by the extractor. Three things worth knowing before editing:

- `desktopCommand` is a narrow fetch dispatcher — new backend endpoints need a new `case` in it.
- Highlights are resolved by character offset against the rendered DOM (`applyInlineHighlights` / `collectHighlightTextNodes`), with a fallback to first-occurrence text search when offsets are missing. Don't break that order.
- Long articles are chunked by `splitMarkdownIntoChunks` and rendered progressively so `react-markdown` doesn't stall on 100k-character inputs.

## Configuration surface

- `API_KEY` — all backend routes require `apiKey` in the JSON body or query string (default `api-key-1234`).
- `OUTPUT_DIR` — container-side output root (`/output`). The host mount is `HOST_OUTPUT_DIR` (defaults to `./output`). Set it to a Windows-backed path on WSL for clean cross-OS access.
- `HOST_OUTPUT_DIR_NATIVE` — native-OS-style mirror of `HOST_OUTPUT_DIR` for reader "Open location" / "Open external" buttons. The backend can't launch host file managers from inside the container, so it translates `/output/...` → this prefix and the desktop reader copies the translated path to the clipboard. For WSL users whose `HOST_OUTPUT_DIR` is `/mnt/<drive>/...`, the backend auto-derives the Windows form (`C:\\...`) so this env var only needs to be set for unusual layouts (e.g. `\\wsl.localhost\Ubuntu\...` or a custom mount).
- `DESKTOP_API_ROOT` — overrides the default library root the desktop reader scans (falls back to `OUTPUT_DIR`).
- `MISTRAL_API_KEY` — enables Mistral OCR as the primary PDF extractor; absent → `pdftotext` fallback.
- `NOTES_LLM_*` — selects the companion-notes provider. See the "Companion notes" section in `README.md` for the full matrix.

## Conventions specific to this repo

- The `.md` file is the source of truth. If a change risks corrupting frontmatter or body round-tripping, lean on `test_article_extractor.py` fixtures and add to them rather than inventing new invariants.
- Frontmatter fields are the metadata contract shared between the extractor, `main.py`'s desktop endpoints, and `index.jsonl`. When adding a new field, thread it through `_build_frontmatter` (writer), `_split_frontmatter` (reader), `_scan_library_documents` / `_build_existing_article_payload` (exposure), and `_build_index_records` (index) together.
- Saves are **idempotent**: re-running `save_local`/`save_pdf` on the same URL updates files in place and upserts the index record via `_upsert_index_records`. Do not introduce append-only writes to `index.jsonl`.
- The desktop reader must keep reading bundles produced by older extractor versions — treat missing frontmatter fields as optional and default safely rather than failing the document.

## TODO

1. When finished implementing features or fixing bugs that are in a TODO.md, always mark them as checked

#### GIT

1. IMPORTANT: Never reference Claude Code in the commit message

## Lessons learned

Update here lessons learned, mistakes,  bugs, anything that was problematic to avoid repeating the same pitfalls in a future.

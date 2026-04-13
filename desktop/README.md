# Corpus Scribe Reader

Minimal browser reader for Corpus Scribe bundles.

The UI is designed as a Zotero-meets-Typora workflow:

- left pane: corpus browser
- center pane: rendered `article.md`
- right pane: editable notes plus bibliography
- local search over article and notes content
- independently scrolling panes
- draggable splitters for resizing the side panels

It reads the existing bundle layout produced by Corpus Scribe:

```text
/output/<label>/<article>/
  Article Title.md
  Article Title.notes.md
  Article Title.bib
  Article Title.reading.pdf
  Article Title.source.pdf
  assets/
```

## Current scope

- scans a corpus root recursively
- lists article bundles by label
- renders markdown with local assets and LaTeX
- opens and edits companion notes
- shows bibliography side-by-side
- lets you quote selected source text into notes

## Run

Start the backend:

```bash
./scripts/compose-with-home-env.sh up -d --build backend
```

Start the browser UI:

```bash
cd desktop
npm install
npm run dev
```

Open the reader at:

```text
http://localhost:1420/
```

The compose wrapper sources `~/.env` before launching Docker Compose, so keys such as `NOTES_LLM_API_KEY` and `MISTRAL_API_KEY` are passed into the backend container without duplicating them in the repo-local `.env`.

On WSL, opening that URL in Windows Chrome is the recommended path.

The reader uses the first available corpus root from:

1. `DESKTOP_API_ROOT`
2. `OUTPUT_DIR`

You can also override the root path in the UI and reload.

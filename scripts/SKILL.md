---
name: corpus-scribe
description: Navigate the user's personal reading corpus, read summaries and highlights, and collaborate on companion notes via the scribe-mcp MCP server. Use for literature reviews, knowledge-base building, grounding coding work in prior reading, and research collaboration on articles the user has saved to the Scribe library.
---

# Corpus Scribe

Corpus Scribe is a Markdown-first reading library. Each document lives as a `.md` bundle on disk with sibling files for companion notes (`.notes.md`), highlights (`.highlights.json`), related links (`.related.json`), and rendered reading PDFs. The desktop reader and Chrome extension write to these bundles; Claude reads and edits them through the `scribe-mcp` MCP server.

Absolute paths returned by the tools are container-side (e.g. `/output/<label>/<title>/<title>.md`) — treat them as opaque identifiers when calling tools, don't translate them to host paths.

## When to use this skill

- The user asks about something they've "read", "saved", "bookmarked", or references a doc by title.
- The user asks for a literature review, synthesis, or comparison across papers in their corpus.
- The user is coding and wants context grounded in something they've saved (a tutorial, spec, paper).
- The user refers to highlights, comments, or notes on a document.
- The user wants to pivot across related papers during research.

If the user is working on something unrelated to their reading corpus, don't reach for these tools.

## Tool map

All tools are prefixed `mcp__scribe-mcp__`. Load them with `ToolSearch` before calling. Default params in parentheses.

### Context
- **`get_current_context()`** — Start here. Returns `focusedDocumentPath`, `focusedHighlightCount`, `focusedNotesPath`, `openDocumentPaths`, `labelFilter`, `updatedAt`. Use to discover what the user is currently reading without asking.

### Navigation
- **`list_labels()`** — Top-level folders; the primary way to segment the corpus.
- **`list_documents(label=None, limit=50)`** — Compact metadata list (title, rating, authors, excerpt, articlePath). Filter by label to enumerate a topic; pass `limit=0` for all.
- **`search(query, label=None, activeArticlePath=None)`** — PubMed-style field queries (`(CSD[Title]) AND (Tournier JD[Author])`). Default is all-fields. Returns up to 50 docs with rating and DOI.

### Reading
- **`read_document(articlePath, stripNoise=true, stripReferences=true)`** — Clean body. **Can blow context** on long papers (100k+ chars). Prefer the section tools below for papers you haven't already sampled.
- **`list_sections(articlePath)`** — ATX-heading outline with char counts. Use before `read_section` on a long doc.
- **`read_section(articlePath, heading)`** — One section slice. Case-insensitive exact or prefix match. Includes nested subsections.

### Highlights and notes
- **`get_highlights(articlePath)`** — User highlights with `comment` field. **Noise-variant highlights are already filtered out by the tool.**
- **`read_notes(articlePath)`** — Current companion `.notes.md` body. **Always call before `update_notes`** so you know what you'd be replacing.
- **`append_notes(articlePath, notesMarkdown)`** — Safe additive write. Preserves existing notes and inserts a blank line before the new content. Prefer this over `update_notes` for collaboration.
- **`update_notes(articlePath, notesMarkdown)`** — Whole-file replace. Only use when the user asks for a rewrite or the notes are empty.

### Related documents
- **`get_related(articlePath)`** — User-curated related links (targetPath, targetTitle, note). Use for lit-review pivots.

## Rules

1. **Never surface noise-variant highlights.** `get_highlights` already filters them; if you ever see a raw `variant: "noise"` item from another path, skip it silently. Noise is usually author affiliations, page headers, or boilerplate the user marked for stripping — not content.

2. **Read before you replace.** Always `read_notes` before `update_notes`. Prefer `append_notes` for additive edits.

3. **Don't pull full bodies on long papers.** If `list_documents` / `get_current_context` shows a doc you haven't touched, call `list_sections` first. Only `read_document` short articles or when the user explicitly asks for the full body.

4. **Trust the focused document.** When the user says "this paper", "the current doc", or "what I'm reading", resolve via `get_current_context().focusedDocumentPath` — don't ask.

5. **Don't echo articlePaths back verbatim** unless the user needs them. Refer to documents by title for readable responses.

6. **Live UI updates.** The desktop reader subscribes to an SSE stream and auto-refreshes when you write notes, highlights, rating, document, or related. Dirty in-flight drafts in the reader are preserved — you won't clobber a user mid-typing — but you should still avoid redundant overwrites.

## Typical flows

**"What am I reading?"**
1. `get_current_context` → report focused title + highlight count + open tabs.

**"Summarize this paper."**
1. `get_current_context` → focused path.
2. `list_sections` → decide scope.
3. `read_section` per section, OR `read_document` if short.
4. `get_highlights` to surface what the user already flagged as important.
5. Offer to save the summary via `append_notes` (not `update_notes`).

**"What have I saved about X?"**
1. `search` with a topical query, or `list_documents(label=…)` if the topic maps to a label.
2. For each promising hit, `get_highlights` and/or `read_section` of "Abstract"/"Introduction" — cheap triage.

**"Pull my comments on this highlight / my notes."**
1. `get_highlights` for comments. `read_notes` for companion notes body.

**"Add these thoughts to my notes."**
1. `read_notes` (to check for existing sections/conflicts).
2. `append_notes` with the new block, clearly delimited by a heading.

**Literature review across N papers:**
1. `list_documents(label=topic)` or `search`.
2. `get_related(articlePath)` to follow curated links.
3. For each: `list_sections` → `read_section("Abstract")` → `get_highlights` for triage.
4. Synthesize across, then offer to write companion notes on each as `append_notes`.

## Gotchas

- `focusedNotesPath` in `get_current_context` ends in `.notes.md`. The MCP client normalizes this automatically when calling tools, but don't hand it to tools outside scribe-mcp expecting a `.md` article.
- The MCP tool output size is capped. A full long paper via `read_document` can exceed it — that's what `list_sections` + `read_section` are for.
- The corpus root is configured server-side; Claude can't switch it.
- Highlight counts in `get_current_context` include noise entries. Content-only count = `get_highlights(path).length`.

#!/usr/bin/env python3
"""Corpus Scribe CLI and MCP server.

Thin client that exposes the Flask backend's desktop API as either:

* a stdio MCP server (``python scribe.py mcp-server``) built on the official
  modelcontextprotocol/python-sdk ``FastMCP`` server that any MCP host
  (Claude Desktop, Cursor, etc.) can spawn; or
* an argparse CLI (``python scribe.py search "CSD"``) that mirrors the same
  tools from a terminal.

Both frontends share one ``ScribeClient`` that talks HTTP to the backend.
Configure via env vars ``SCRIBE_API_BASE`` (default ``http://127.0.0.1:5000``)
and ``SCRIBE_CORPUS_ROOT`` (optional override for the library root).

The CLI part only uses the Python standard library. The ``mcp-server``
subcommand additionally requires the ``mcp`` package
(``pip install mcp`` or ``uv tool install mcp``); importing it is deferred
so the CLI still works without the SDK installed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


DEFAULT_API_BASE = os.environ.get("SCRIBE_API_BASE", "http://127.0.0.1:5000")
DEFAULT_CORPUS_ROOT = os.environ.get("SCRIBE_CORPUS_ROOT") or None
SERVER_NAME = "corpus-scribe"


class ScribeError(RuntimeError):
    """Raised when the backend returns an error or is unreachable."""


@dataclass
class ScribeClient:
    api_base: str = DEFAULT_API_BASE
    corpus_root: str | None = DEFAULT_CORPUS_ROOT
    timeout: float = 30.0

    def _url(self, path: str, query: dict[str, Any] | None = None) -> str:
        base = self.api_base.rstrip("/")
        url = f"{base}{path}"
        if query:
            filtered = {k: v for k, v in query.items() if v is not None}
            if filtered:
                url = f"{url}?{urllib.parse.urlencode(filtered)}"
        return url

    def _request(self, method: str, path: str, *, query: dict[str, Any] | None = None, body: dict[str, Any] | None = None) -> dict:
        url = self._url(path, query)
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload_raw = resp.read()
        except urllib.error.HTTPError as exc:
            try:
                message = json.loads(exc.read().decode("utf-8")).get("message") or exc.reason
            except Exception:
                message = exc.reason
            raise ScribeError(f"{method} {path} failed ({exc.code}): {message}") from exc
        except urllib.error.URLError as exc:
            raise ScribeError(f"Cannot reach backend at {self.api_base}: {exc.reason}") from exc

        try:
            payload = json.loads(payload_raw.decode("utf-8"))
        except Exception as exc:
            raise ScribeError(f"Invalid JSON from {path}") from exc
        if isinstance(payload, dict) and payload.get("success") is False:
            raise ScribeError(payload.get("message") or f"{path} returned failure")
        return payload

    def search(self, query: str, label: str | None = None, active_article_path: str | None = None) -> list[dict]:
        body = {
            "query": query,
            "label": label or "",
            "activeArticlePath": active_article_path or "",
        }
        if self.corpus_root:
            body["root"] = self.corpus_root
        payload = self._request("POST", "/desktop/search", body=body)
        documents = payload.get("documents")
        return documents if isinstance(documents, list) else []

    def get_session(self) -> dict:
        payload = self._request("GET", "/desktop/session")
        session = payload.get("session")
        return session if isinstance(session, dict) else {}

    def read_document(self, article_path: str, strip_noise: bool = True, strip_references: bool = True) -> dict:
        payload = self._request(
            "POST",
            "/desktop/document/read",
            body={
                "articlePath": article_path,
                "stripNoise": strip_noise,
                "stripReferences": strip_references,
            },
        )
        return {
            "articlePath": payload.get("articlePath") or article_path,
            "title": payload.get("title") or "",
            "markdown": payload.get("markdown") or "",
            "noiseStripped": bool(payload.get("noiseStripped")),
            "referencesStripped": bool(payload.get("referencesStripped")),
        }

    def get_document_detail(self, article_path: str) -> dict:
        payload = self._request("GET", "/desktop/document", query={"articlePath": article_path})
        detail = payload.get("detail")
        return detail if isinstance(detail, dict) else {}

    def update_notes(self, article_path: str, notes_markdown: str) -> dict:
        payload = self._request(
            "POST",
            "/desktop/notes",
            body={"articlePath": article_path, "notesMarkdown": notes_markdown},
        )
        return {
            "notesPath": payload.get("notesPath"),
            "notesMarkdown": payload.get("notesMarkdown") or notes_markdown,
        }


def _format_search_results(documents: list[dict]) -> str:
    if not documents:
        return "No results."
    lines: list[str] = []
    for doc in documents[:50]:
        title = doc.get("title") or doc.get("articlePath") or "(untitled)"
        label = doc.get("label") or "unlabeled"
        rating = doc.get("rating")
        rating_str = f"★{rating}" if isinstance(rating, int) and rating else "   "
        lines.append(f"{rating_str}  [{label}]  {title}")
        path = doc.get("articlePath")
        if path:
            lines.append(f"        {path}")
    if len(documents) > 50:
        lines.append(f"… {len(documents) - 50} more")
    return "\n".join(lines)


def _format_session_context(session: dict) -> str:
    if not session:
        return "No active reader session."
    lines: list[str] = []
    focused = session.get("focusedDocumentPath")
    if focused:
        lines.append(f"Focused: {focused}")
    notes_path = session.get("focusedNotesPath")
    if notes_path:
        lines.append(f"Notes:   {notes_path}")
    highlight_count = session.get("focusedHighlightCount")
    if isinstance(highlight_count, int):
        lines.append(f"Highlights: {highlight_count}")
    label = session.get("labelFilter")
    if label:
        lines.append(f"Label filter: {label}")
    open_paths = session.get("openDocumentPaths") or []
    if isinstance(open_paths, list) and open_paths:
        lines.append(f"Open documents ({len(open_paths)}):")
        for path in open_paths:
            marker = "→" if path == focused else " "
            lines.append(f"  {marker} {path}")
    updated = session.get("updatedAt")
    if updated:
        lines.append(f"Updated at: {updated}")
    return "\n".join(lines) or "No active reader session."


# ---------------------------------------------------------------------------
# MCP stdio server (built on modelcontextprotocol/python-sdk FastMCP)
# ---------------------------------------------------------------------------


def build_mcp_server(client: ScribeClient):
    """Return a ``FastMCP`` server wired to the shared ``ScribeClient``.

    Imports the ``mcp`` SDK lazily so the CLI subcommands keep working in
    environments where the SDK is not installed. Raises ``ScribeError`` with a
    pip install hint if the import fails.
    """
    try:
        from mcp.server.fastmcp import FastMCP
    except ImportError as exc:
        raise ScribeError(
            "mcp-server requires the 'mcp' package. Install with `pip install mcp` "
            "or `uv tool install mcp`."
        ) from exc

    mcp = FastMCP(
        name=SERVER_NAME,
        instructions=(
            "Corpus Scribe is a Markdown-first reading corpus. Use `search` to "
            "find documents, `get_current_context` to see what the desktop reader "
            "currently has open, `read_document` to fetch a clean (noise-stripped) "
            "markdown body for an article, and `update_notes` to replace the "
            "companion .notes.md body. Article paths are absolute paths on the "
            "backend host filesystem."
        ),
    )

    @mcp.tool(
        name="search",
        description=(
            "Search the Corpus Scribe library with PubMed-style field filters "
            "(e.g. '(CSD[Title]) AND (Tournier JD[Author])'). Default is "
            "all-fields. Returns matching documents with title, label, rating, "
            "and absolute articlePath."
        ),
    )
    def search(query: str, label: str | None = None, activeArticlePath: str | None = None) -> str:
        query = (query or "").strip()
        if not query:
            raise ValueError("search requires a non-empty query")
        docs = client.search(query, label=label, active_article_path=activeArticlePath)
        return json.dumps(
            [
                {
                    "title": d.get("title"),
                    "label": d.get("label"),
                    "rating": d.get("rating"),
                    "articlePath": d.get("articlePath"),
                    "url": d.get("url"),
                    "doi": d.get("doi"),
                    "authors": d.get("authors"),
                    "ingestedAt": d.get("ingestedAt"),
                }
                for d in docs[:50]
            ],
            ensure_ascii=False,
            indent=2,
        )

    @mcp.tool(
        name="get_current_context",
        description=(
            "Return the desktop reader's current session: focused document, "
            "open document paths, companion notes path, highlight count, and "
            "label filter. Empty if the reader has not synced a session yet."
        ),
    )
    def get_current_context() -> str:
        return json.dumps(client.get_session(), ensure_ascii=False, indent=2)

    @mcp.tool(
        name="read_document",
        description=(
            "Return a document's markdown body with noise-marked highlights "
            "removed (the default). Set stripNoise=false to get the raw body. "
            "The References section is also removed unless stripReferences=false."
        ),
    )
    def read_document(articlePath: str, stripNoise: bool = True, stripReferences: bool = True) -> str:
        articlePath = (articlePath or "").strip()
        if not articlePath:
            raise ValueError("read_document requires articlePath")
        payload = client.read_document(articlePath, strip_noise=stripNoise, strip_references=stripReferences)
        header = f"# {payload['title']}\n\n" if payload.get("title") else ""
        return header + payload["markdown"]

    @mcp.tool(
        name="update_notes",
        description=(
            "Replace the companion .notes.md body for a document. Supply the "
            "full new markdown — this is a whole-file replace, not a patch."
        ),
    )
    def update_notes(articlePath: str, notesMarkdown: str) -> str:
        articlePath = (articlePath or "").strip()
        if not articlePath:
            raise ValueError("update_notes requires articlePath")
        result = client.update_notes(articlePath, notesMarkdown or "")
        return f"Saved notes to {result['notesPath']}"

    return mcp


def run_mcp_server(client: ScribeClient) -> int:
    mcp = build_mcp_server(client)
    mcp.run(transport="stdio")
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cli_search(client: ScribeClient, args: argparse.Namespace) -> int:
    docs = client.search(args.query, label=args.label, active_article_path=args.active)
    if args.json:
        json.dump(docs, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(_format_search_results(docs))
    return 0


def _cli_context(client: ScribeClient, args: argparse.Namespace) -> int:
    session = client.get_session()
    if args.json:
        json.dump(session, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(_format_session_context(session))
    return 0


def _cli_read(client: ScribeClient, args: argparse.Namespace) -> int:
    payload = client.read_document(
        args.article_path,
        strip_noise=not args.keep_noise,
        strip_references=not args.keep_references,
    )
    if args.json:
        json.dump(payload, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        if payload.get("title"):
            print(f"# {payload['title']}\n")
        sys.stdout.write(payload["markdown"])
        if not payload["markdown"].endswith("\n"):
            sys.stdout.write("\n")
    return 0


def _cli_update_notes(client: ScribeClient, args: argparse.Namespace) -> int:
    if args.from_file:
        notes_markdown = Path(args.from_file).read_text(encoding="utf-8")
    elif args.from_stdin:
        notes_markdown = sys.stdin.read()
    elif args.text is not None:
        notes_markdown = args.text
    else:
        print("Provide --from-file PATH, --from-stdin, or --text STRING", file=sys.stderr)
        return 2
    result = client.update_notes(args.article_path, notes_markdown)
    if args.json:
        json.dump(result, sys.stdout, ensure_ascii=False, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"Saved notes → {result['notesPath']}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scribe",
        description="Corpus Scribe CLI (and MCP server entry point)",
    )
    parser.add_argument(
        "--api-base",
        default=DEFAULT_API_BASE,
        help=f"Scribe backend base URL (default {DEFAULT_API_BASE})",
    )
    parser.add_argument(
        "--corpus-root",
        default=DEFAULT_CORPUS_ROOT,
        help="Optional corpus library root override (default: server-side default)",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of formatted text")

    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Search the library")
    p_search.add_argument("query", help="Search query (PubMed-style field tags supported)")
    p_search.add_argument("--label", default=None, help="Restrict search to a label")
    p_search.add_argument("--active", default=None, help="Active article path for related-ranking bias")
    p_search.set_defaults(func=_cli_search)

    p_context = sub.add_parser("context", help="Show the desktop reader's current session context")
    p_context.set_defaults(func=_cli_context)

    p_read = sub.add_parser("read", help="Print a document's body with noise removed")
    p_read.add_argument("article_path", help="Absolute path to the article .md file")
    p_read.add_argument("--keep-noise", action="store_true", help="Keep noise-marked highlights in output")
    p_read.add_argument("--keep-references", action="store_true", help="Keep the References section tail")
    p_read.set_defaults(func=_cli_read)

    p_notes = sub.add_parser("update-notes", help="Replace the companion .notes.md body")
    p_notes.add_argument("article_path", help="Absolute path to the article .md file")
    p_notes.add_argument("--from-file", help="Read new notes markdown from this file")
    p_notes.add_argument("--from-stdin", action="store_true", help="Read new notes markdown from stdin")
    p_notes.add_argument("--text", help="Inline notes markdown string")
    p_notes.set_defaults(func=_cli_update_notes)

    p_mcp = sub.add_parser("mcp-server", help="Run as a stdio MCP server (FastMCP / modelcontextprotocol python-sdk)")
    p_mcp.set_defaults(func=None)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    client = ScribeClient(api_base=args.api_base, corpus_root=args.corpus_root)

    if args.command == "mcp-server":
        return run_mcp_server(client)

    try:
        return args.func(client, args)
    except ScribeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

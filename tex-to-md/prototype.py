#!/usr/bin/env python3
"""Standalone arXiv-to-Markdown prototype harness."""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import textwrap
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bs4 import BeautifulSoup


ARXIV_API = "http://export.arxiv.org/api/query"
USER_AGENT = "tex-to-md-prototype/0.1 (+https://github.com/openai)"
ATOM_NS = {
    "atom": "http://www.w3.org/2005/Atom",
    "arxiv": "http://arxiv.org/schemas/atom",
}


@dataclass
class Paper:
    arxiv_id: str
    title: str
    abs_url: str
    pdf_url: str
    html_url: str | None
    primary_category: str | None
    published: str | None


@dataclass
class StrategyResult:
    strategy: str
    success: bool
    output_path: str | None
    score: int
    metrics: dict[str, Any]
    error: str | None = None


def http_get(url: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def http_get_text(url: str, timeout: float = 60.0) -> str:
    raw = http_get(url, timeout=timeout)
    return raw.decode("utf-8", errors="replace")


def arxiv_search(categories: list[str], max_results: int) -> list[Paper]:
    query_parts = [
        f"cat:{category.strip()}" for category in categories if category.strip()
    ]
    if not query_parts:
        raise ValueError("At least one category is required")
    query = " OR ".join(query_parts)
    params = urllib.parse.urlencode(
        {
            "search_query": query,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": 0,
            "max_results": max_results,
        }
    )
    feed = http_get_text(f"{ARXIV_API}?{params}")
    root = ET.fromstring(feed)
    papers: list[Paper] = []
    for entry in root.findall("atom:entry", ATOM_NS):
        id_text = entry.findtext("atom:id", default="", namespaces=ATOM_NS).strip()
        match = re.search(r"/abs/([0-9]{4}\.[0-9]{4,5}(?:v\d+)?)$", id_text)
        if not match:
            continue
        arxiv_id = match.group(1)
        title = " ".join(
            entry.findtext("atom:title", default="", namespaces=ATOM_NS).split()
        )
        primary = entry.find("arxiv:primary_category", ATOM_NS)
        html_url = None
        for link in entry.findall("atom:link", ATOM_NS):
            href = (link.get("href") or "").strip()
            title_attr = (link.get("title") or "").strip().lower()
            if "/html/" in href or title_attr == "html":
                html_url = href
                break
        papers.append(
            Paper(
                arxiv_id=arxiv_id,
                title=title,
                abs_url=f"https://arxiv.org/abs/{arxiv_id}",
                pdf_url=f"https://arxiv.org/pdf/{arxiv_id}.pdf",
                html_url=html_url,
                primary_category=primary.get("term") if primary is not None else None,
                published=entry.findtext(
                    "atom:published", default="", namespaces=ATOM_NS
                )
                or None,
            )
        )
    return papers


def sample_papers(
    categories: list[str], count: int, max_results: int, seed: int | None
) -> list[Paper]:
    papers = arxiv_search(categories, max_results)
    if len(papers) < count:
        raise RuntimeError(
            f"Requested {count} papers but arXiv search only returned {len(papers)}"
        )
    rng = random.Random(seed)
    return rng.sample(papers, count)


def sanitize_name(value: str) -> str:
    cleaned = re.sub(r"[^\w .-]+", "", value).strip()
    cleaned = re.sub(r"\s+", " ", cleaned)
    if len(cleaned) > 96:
        cleaned = cleaned[:96].rstrip(" .-_")
    return cleaned or "paper"


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def discover_html_url(abs_html: str, paper: Paper) -> str | None:
    soup = BeautifulSoup(abs_html, "html.parser")
    link = soup.find("a", href=re.compile(r"/html/"))
    if link and link.get("href"):
        return urllib.parse.urljoin(paper.abs_url, link["href"])
    if paper.html_url:
        return paper.html_url
    bare = paper.arxiv_id.split("v", 1)[0]
    return f"https://arxiv.org/html/{bare}"


def fetch_paper_bundle(root: Path, paper: Paper, fetch_source: bool) -> dict[str, Any]:
    paper_dir = ensure_dir(
        root / "papers" / sanitize_name(f"{paper.title[:72]}-{paper.arxiv_id}")
    )
    metadata = asdict(paper)
    metadata["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    abs_html = http_get_text(paper.abs_url)
    (paper_dir / "abs.html").write_text(abs_html, encoding="utf-8")

    html_url = discover_html_url(abs_html, paper)
    metadata["html_url"] = html_url
    if html_url:
        try:
            article_html = http_get_text(html_url)
        except Exception as exc:
            metadata["html_fetch_error"] = str(exc)
        else:
            (paper_dir / "article.html").write_text(article_html, encoding="utf-8")

    if fetch_source:
        try:
            source_bytes = http_get(
                f"https://arxiv.org/e-print/{paper.arxiv_id}", timeout=120
            )
        except Exception as exc:
            metadata["source_fetch_error"] = str(exc)
        else:
            (paper_dir / "source.bin").write_bytes(source_bytes)

    write_json(paper_dir / "metadata.json", metadata)
    return metadata


def run_pandoc(
    input_path: Path,
    from_format: str,
    to_format: str,
    cwd: Path | None = None,
    extra_args: list[str] | None = None,
) -> str:
    cmd = ["pandoc", str(input_path), "-f", from_format, "-t", to_format, "--wrap=none"]
    if extra_args:
        cmd.extend(extra_args)
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd) if cwd else None,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("pandoc is not installed or not on PATH") from exc
    except subprocess.CalledProcessError as exc:
        stderr = exc.stderr.strip() or exc.stdout.strip()
        raise RuntimeError(
            stderr or f"pandoc failed with exit code {exc.returncode}"
        ) from exc
    return proc.stdout


def collect_html_assets(article_html_path: Path, paper_dir: Path) -> tuple[Path, int]:
    html_text = article_html_path.read_text(encoding="utf-8")
    soup = BeautifulSoup(html_text, "html.parser")
    article = (
        soup.find("article")
        or soup.select_one(".ltx_page_main")
        or soup.select_one(".ltx_document")
        or soup.body
    )
    if article is None:
        raise RuntimeError("No article-like root found in HTML")

    assets_dir = ensure_dir(paper_dir / "assets")
    count = 0
    for tag in article.find_all(["script", "style"]):
        tag.decompose()

    for image in article.find_all("img"):
        src = (image.get("src") or "").strip()
        if not src or src.startswith(("data:", "javascript:")):
            continue
        absolute = urllib.parse.urljoin(article_html_path.as_uri(), src)
        if absolute.startswith("file:"):
            continue
        parsed = urllib.parse.urlparse(absolute)
        file_name = Path(parsed.path).name
        if not file_name:
            continue
        target = assets_dir / file_name
        if not target.exists():
            try:
                target.write_bytes(http_get(absolute, timeout=120))
            except Exception:
                continue
        image["src"] = f"assets/{target.name}"
        count += 1

    cleaned_html = paper_dir / "article.cleaned.html"
    cleaned_html.write_text(str(article), encoding="utf-8")
    return cleaned_html, count


def extract_source_tree(source_path: Path, work_dir: Path) -> Path:
    source_root = ensure_dir(work_dir / "source")
    raw = source_path.read_bytes()
    temp = work_dir / "source.bundle"
    temp.write_bytes(raw)

    try:
        if tarfile.is_tarfile(temp):
            with tarfile.open(temp) as archive:
                archive.extractall(source_root)
            return source_root
    except Exception:
        pass

    for suffix in (".tar.gz", ".tgz", ".gz"):
        probe = work_dir / f"source{suffix}"
        try:
            if suffix == ".gz":
                import gzip

                probe.write_bytes(gzip.decompress(raw))
                target = source_root / "main.tex"
                target.write_bytes(probe.read_bytes())
                return source_root
            probe.write_bytes(raw)
            if tarfile.is_tarfile(probe):
                with tarfile.open(probe) as archive:
                    archive.extractall(source_root)
                return source_root
        except Exception:
            continue

    (source_root / "main.tex").write_bytes(raw)
    return source_root


def find_main_tex(source_root: Path) -> Path:
    candidates = sorted(source_root.rglob("*.tex"))
    if not candidates:
        raise RuntimeError("No .tex files found in source tree")
    scored: list[tuple[int, Path]] = []
    for candidate in candidates:
        try:
            text = candidate.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        score = 0
        if "\\documentclass" in text:
            score += 20
        if "\\begin{document}" in text:
            score += 20
        if "\\title" in text:
            score += 5
        if candidate.name.lower() in {"main.tex", "paper.tex", "ms.tex"}:
            score += 10
        if "appendix" in candidate.name.lower() or "supp" in candidate.name.lower():
            score -= 10
        scored.append((score, candidate))
    if not scored:
        raise RuntimeError("Unable to read candidate .tex files")
    scored.sort(key=lambda item: item[0], reverse=True)
    return scored[0][1]


def score_markdown(markdown: str) -> dict[str, Any]:
    headings = re.findall(r"(?m)^#{1,6}\s+", markdown)
    image_refs = re.findall(r"!\[[^\]]*\]\([^)]+\)|<img\b[^>]*src=", markdown)
    raw_tex_commands = re.findall(r"\\[A-Za-z@]+", markdown)
    raw_ref_ids = re.findall(r"[@][A-Za-z0-9:_-]+", markdown)
    broken_math = re.findall(r"\$\$[^$]*\\label\{", markdown)
    words = re.findall(r"\b\w+\b", markdown)
    score = 0
    if markdown.strip():
        score += 50
    score += min(len(headings) * 2, 20)
    score += min(len(image_refs) * 3, 20)
    score += min(len(raw_ref_ids), 20)
    score -= min(len(raw_tex_commands) // 10, 25)
    score -= min(len(broken_math) * 5, 25)
    return {
        "word_count": len(words),
        "heading_count": len(headings),
        "image_ref_count": len(image_refs),
        "citation_token_count": len(raw_ref_ids),
        "raw_tex_command_count": len(raw_tex_commands),
        "broken_math_block_count": len(broken_math),
        "score": max(score, 0),
    }


def run_scrape_html_strategy(paper_dir: Path) -> StrategyResult:
    article_html_path = paper_dir / "article.html"
    if not article_html_path.exists():
        return StrategyResult("scrape_html", False, None, 0, {}, "article.html missing")

    try:
        cleaned_html, asset_count = collect_html_assets(article_html_path, paper_dir)
        markdown = run_pandoc(cleaned_html, "html", "gfm+tex_math_dollars")
        output_dir = ensure_dir(paper_dir / "outputs")
        output_path = output_dir / "scrape_html.md"
        output_path.write_text(markdown, encoding="utf-8")
        metrics = score_markdown(markdown)
        metrics["downloaded_asset_count"] = asset_count
        write_json(output_dir / "scrape_html.metrics.json", metrics)
        return StrategyResult(
            "scrape_html", True, str(output_path), int(metrics["score"]), metrics
        )
    except Exception as exc:
        return StrategyResult("scrape_html", False, None, 0, {}, str(exc))


def run_pandoc_tex_strategy(paper_dir: Path) -> StrategyResult:
    source_path = paper_dir / "source.bin"
    if not source_path.exists():
        return StrategyResult("pandoc_tex", False, None, 0, {}, "source.bin missing")

    try:
        with tempfile.TemporaryDirectory(prefix="tex-to-md-src-") as tmpdir:
            work_dir = Path(tmpdir)
            source_root = extract_source_tree(source_path, work_dir)
            main_tex = find_main_tex(source_root)
            markdown = run_pandoc(
                main_tex,
                "latex",
                "gfm+tex_math_dollars",
                cwd=source_root,
                extra_args=["--standalone", f"--resource-path={source_root}"],
            )
        output_dir = ensure_dir(paper_dir / "outputs")
        output_path = output_dir / "pandoc_tex.md"
        output_path.write_text(markdown, encoding="utf-8")
        metrics = score_markdown(markdown)
        metrics["main_tex"] = str(main_tex.name)
        write_json(output_dir / "pandoc_tex.metrics.json", metrics)
        return StrategyResult(
            "pandoc_tex", True, str(output_path), int(metrics["score"]), metrics
        )
    except Exception as exc:
        return StrategyResult("pandoc_tex", False, None, 0, {}, str(exc))


def run_strategy(name: str, paper_dir: Path) -> StrategyResult:
    if name == "scrape_html":
        return run_scrape_html_strategy(paper_dir)
    if name == "pandoc_tex":
        return run_pandoc_tex_strategy(paper_dir)
    raise ValueError(f"Unknown strategy: {name}")


def prototype_run(
    out_dir: Path,
    categories: list[str],
    count: int,
    max_results: int,
    seed: int | None,
    strategies: list[str],
) -> dict[str, Any]:
    ensure_dir(out_dir)
    sample = sample_papers(categories, count, max_results, seed)
    summary: dict[str, Any] = {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "categories": categories,
        "count": count,
        "max_results": max_results,
        "seed": seed,
        "strategies": strategies,
        "papers": [],
    }

    fetch_source = "pandoc_tex" in strategies
    for paper in sample:
        print(f"Paper: {paper}")
        metadata = fetch_paper_bundle(out_dir, paper, fetch_source=fetch_source)
        paper_dir = next((out_dir / "papers").glob(f"*{paper.arxiv_id}"))
        strategy_results = [
            asdict(run_strategy(strategy, paper_dir)) for strategy in strategies
        ]
        summary["papers"].append(
            {
                "paper": metadata,
                "bundle_dir": str(paper_dir),
                "results": strategy_results,
            }
        )

    write_json(out_dir / "summary.json", summary)
    return summary


def cmd_sample(args: argparse.Namespace) -> int:
    papers = sample_papers(args.categories, args.count, args.max_results, args.seed)
    print(json.dumps([asdict(paper) for paper in papers], ensure_ascii=False, indent=2))
    return 0


def cmd_prototype(args: argparse.Namespace) -> int:
    summary = prototype_run(
        out_dir=args.out,
        categories=args.categories,
        count=args.count,
        max_results=args.max_results,
        seed=args.seed,
        strategies=args.strategy,
    )
    print(f"Wrote {args.out / 'summary.json'}")
    successes = 0
    total = 0
    for paper in summary["papers"]:
        for result in paper["results"]:
            total += 1
            successes += int(bool(result["success"]))
    print(f"Completed {successes}/{total} strategy runs successfully.")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Prototype harness for sampling arXiv papers and benchmarking Markdown conversion strategies.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            Examples:
              python prototype.py sample --count 5 --categories cs.CV,cs.LG
              python prototype.py prototype --count 10 --categories cs.CV,cs.LG --strategy scrape_html --out runs/demo
            """
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    sample_parser = subparsers.add_parser(
        "sample", help="Print a sampled set of recent arXiv papers as JSON."
    )
    sample_parser.add_argument("--count", type=int, default=5)
    sample_parser.add_argument("--max-results", type=int, default=50)
    sample_parser.add_argument("--seed", type=int, default=0)
    sample_parser.add_argument(
        "--categories",
        type=lambda s: [item.strip() for item in s.split(",") if item.strip()],
        default=["cs.CV", "cs.LG", "eess.IV"],
    )
    sample_parser.set_defaults(func=cmd_sample)

    proto_parser = subparsers.add_parser(
        "prototype",
        help="Fetch a sample, run strategies, and write a benchmark summary.",
    )
    proto_parser.add_argument("--count", type=int, default=5)
    proto_parser.add_argument("--max-results", type=int, default=50)
    proto_parser.add_argument("--seed", type=int, default=0)
    proto_parser.add_argument(
        "--categories",
        type=lambda s: [item.strip() for item in s.split(",") if item.strip()],
        default=["cs.CV", "cs.LG", "eess.IV"],
    )
    proto_parser.add_argument(
        "--strategy",
        action="append",
        choices=["scrape_html", "pandoc_tex"],
        default=["scrape_html"],
    )
    proto_parser.add_argument(
        "--out", type=Path, default=Path("runs") / time.strftime("%Y%m%d-%H%M%S")
    )
    proto_parser.set_defaults(func=cmd_prototype)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    args.out = Path(args.out).expanduser() if hasattr(args, "out") else None
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

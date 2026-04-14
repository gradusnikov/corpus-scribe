# tex-to-md

Standalone prototype harness for benchmarking arXiv-to-Markdown conversion.

The prototype is intentionally separate from the main Corpus Scribe backend. It
does four things:

1. samples recent arXiv papers from chosen categories,
2. fetches the source artifacts for those papers,
3. runs one or more conversion strategies,
4. scores the resulting Markdown with simple structural heuristics.

Current strategies:

- `scrape_html`: fetch `arxiv.org/html/...`, download local figure assets, and
  convert the scraped HTML article fragment to Markdown via Pandoc.
- `pandoc_tex`: fetch `arxiv.org/e-print/...`, unpack the source tree, and try
  direct file-based Pandoc conversion on the guessed main `.tex`.

## Requirements

- Python 3.11+
- `pandoc` on `PATH`
- Python packages from `requirements.txt`

## Quick start

```bash
cd tex-to-md
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

python prototype.py prototype \
  --count 5 \
  --categories cs.CV,cs.LG,eess.IV \
  --max-results 50 \
  --strategy scrape_html \
  --out runs/demo
```

To compare the scraped-HTML path against direct TeX conversion:

```bash
python prototype.py prototype \
  --count 5 \
  --categories cs.CV,cs.LG,eess.IV \
  --max-results 50 \
  --strategy scrape_html \
  --strategy pandoc_tex \
  --out runs/compare
```

## Outputs

Each run produces:

- `papers/<arxiv_id>/metadata.json`
- `papers/<arxiv_id>/abs.html`
- `papers/<arxiv_id>/article.html` when available
- `papers/<arxiv_id>/assets/` for downloaded HTML figures
- `papers/<arxiv_id>/outputs/<strategy>.md`
- `papers/<arxiv_id>/outputs/<strategy>.metrics.json`
- `summary.json`

`summary.json` is the main benchmark artifact. It contains paper metadata,
strategy-level metrics, scores, and failure messages.

## Notes

- The scoring is deliberately simple. It is only a prototype benchmark, not a
  final quality metric.
- The harness keeps all intermediate artifacts so later iterations can compare
  sanitizer and converter changes scientifically instead of anecdotally.

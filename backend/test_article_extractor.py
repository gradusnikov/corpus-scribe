import subprocess
import tempfile
import unittest
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

from article_extractor import (
    _cleanup_latex,
    _collect_markdown_image_assets,
    _collect_images,
    _detect_source_family,
    _apply_html_annotations,
    _convert_html_to_markdown,
    _derive_citation_metadata,
    _download_image,
    _enrich_meta_with_doi,
    _extract_preferred_math_node,
    _replace_problem_math_with_tex_placeholders,
    _restore_tex_placeholders,
    _extract_latex_metadata,
    _extract_meta,
    _generate_notes_via_anthropic,
    _generate_notes_via_openai_compatible,
    _normalize_code_listing_tables,
    _normalize_katex_compatible_tex,
    _normalize_latexml_equation_tables,
    _postprocess_markdown,
    _postprocess_markdown_before_math_restore,
    _postprocess_mistral_markdown,
    _postprocess_pdf_markdown,
    _postprocess_source_markdown,
    _prepare_html_for_markdown,
    _prepare_html_for_pdf,
    _preferred_figure_image_url,
    _resolve_document_relative_url,
    _render_html_to_pdf_with_chromium,
    _resolve_notes_client_config,
    _resize_images_for_pdf,
    _safe_output_name,
    _sanitize_latex_for_markdown,
    _sanitize_unicode_text,
    _strip_reference_sections_for_notes,
    extract_article,
    extract_url,
    extract_pdf_bytes,
)


FIXTURE_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <title>Extractor Fixture</title>
  </head>
  <body>
    <article>
      <h1>Extractor Fixture</h1>
      <p>
        This fixture is intentionally long so the extractor keeps the article tag instead of
        falling back to readability summary output. It combines Unicode-heavy scientific prose,
        display equations from different publishers, and a real HTML table in one deterministic
        input. The text also includes notation such as b ∈ [0, b_max], angle θ, coefficient γ,
        voxel size 2.5 × 2.5 × 2.5 mm, and the author name Gödel to exercise the Unicode PDF
        path without any hardcoded symbol table.
      </p>
      <table class="disp-formula p" id="eq1">
        <tbody>
          <tr>
            <td class="formula">
              <mjx-container display="true">
                <mjx-math>
                  <mjx-msub>
                    <mjx-mi><mjx-c class="mjx-c1D446">𝑆</mjx-c></mjx-mi>
                    <mjx-script><mjx-mi size="s"><mjx-c class="mjx-c1D44F">𝑏</mjx-c></mjx-mi></mjx-script>
                  </mjx-msub>
                  <mjx-mo><mjx-c class="mjx-c3D">=</mjx-c></mjx-mo>
                  <mjx-mi><mjx-c class="mjx-c1D465">𝑥</mjx-c></mjx-mi>
                </mjx-math>
              </mjx-container>
            </td>
            <td class="label">(1)</td>
          </tr>
        </tbody>
      </table>
      <span class="display">
        <span class="formula" id="eqn0006">
          <span class="label">(6)</span>
          <script type="math/mml">
            <math xmlns="http://www.w3.org/1998/Math/MathML">
              <mrow><mi>w</mi><mo>=</mo><mn>1</mn></mrow>
            </math>
          </script>
        </span>
      </span>
      <table>
        <thead>
          <tr><th>Col A</th><th>Col B</th></tr>
        </thead>
        <tbody>
          <tr><td colspan="2"><hr /></td></tr>
          <tr><td>1</td><td>2</td></tr>
        </tbody>
      </table>
      <table class="crayon-table">
        <tbody>
          <tr>
            <td class="crayon-nums">12</td>
            <td class="urvanov-syntax-highlighter-code">
              <div class="crayon-pre">
                <div class="crayon-line">import numpy as np</div>
                <div class="crayon-line">print(1)</div>
              </div>
            </td>
          </tr>
        </tbody>
      </table>
      <p>
        The extractor should emit clean markdown, preserve equations, and keep table content
        readable in both markdown and PDF output. This paragraph exists to keep the fixture
        stable across readability and Pandoc normalization passes. Recovery saturates at
        ~<!-- -->100 examples in this synthetic fixture.
      </p>
      <p>SC-8 takes <math class="ltx_math_unparsed" display="inline"><semantics><mo>≈</mo><annotation encoding="application/x-tex">\\approx</annotation></semantics></math>40 minutes.</p>
      <figure class="ltx_figure">
        <div class="ltx_flex_figure">
          <div class="ltx_flex_cell ltx_flex_size_1"><span class="ltx_ERROR ltx_figure_panel">\\includegraphics</span></div>
          <div class="ltx_flex_cell ltx_flex_size_1"><p class="ltx_p ltx_figure_panel ltx_align_center">[width=0.92]fig1_frontier.pdf</p></div>
        </div>
        <figcaption>Figure 1: Placeholder figure caption.</figcaption>
      </figure>
      <table class="ltx_equation ltx_eqn_table">
        <tbody><tr class="ltx_equation ltx_eqn_row ltx_align_baseline">
          <td class="ltx_eqn_cell ltx_align_center">
            <math class="ltx_math_unparsed" display="block"><semantics><mrow><mi>A</mi><mo>=</mo><mn>1</mn></mrow><annotation encoding="application/x-tex">A=1</annotation></semantics></math>
          </td>
        </tr></tbody>
      </table>
    </article>
  </body>
</html>
"""


class ArticleExtractorTests(unittest.TestCase):
    def test_detect_source_family_prefers_arxiv(self):
        self.assertEqual(
            _detect_source_family("https://arxiv.org/abs/1706.03762"),
            "arxiv",
        )
        self.assertEqual(
            _detect_source_family(
                "",
                '<html><head><link rel="canonical" href="https://arxiv.org/abs/1706.03762" /></head></html>',
            ),
            "arxiv",
        )

    def test_extract_url_routes_arxiv_through_adapter(self):
        from unittest.mock import patch

        with patch(
            "article_extractor._extract_arxiv_url",
            return_value={"title": "Attention Is All You Need"},
        ) as arxiv_mock, patch(
            "article_extractor.extract_article",
            return_value={"title": "Generic"},
        ) as generic_mock:
            result = extract_url(
                "<html></html>",
                output_dir="/tmp/out",
                url="https://arxiv.org/abs/1706.03762",
                render_pdf=False,
            )

        self.assertEqual(result["title"], "Attention Is All You Need")
        arxiv_mock.assert_called_once()
        generic_mock.assert_not_called()

    def test_extract_url_arxiv_falls_back_to_pdf_pipeline(self):
        from unittest.mock import patch

        with patch(
            "article_extractor._discover_arxiv_html_url",
            return_value=None,
        ), patch(
            "article_extractor.extract_pdf_url",
            return_value={
                "title": "Attention Is All You Need",
                "md-path": None,
                "metadata": {},
            },
        ) as pdf_mock:
            result = extract_url(
                "<html></html>",
                output_dir="/tmp/out",
                url="https://arxiv.org/abs/1706.03762",
                render_pdf=False,
            )

        self.assertEqual(result["metadata"]["extraction_adapter"], "arxiv")
        self.assertEqual(result["metadata"]["source_format"], "pdf")
        self.assertIn("arxiv_pdf", result["metadata"]["extraction_fallback_chain"])
        pdf_mock.assert_called_once()

    def test_extract_latex_metadata_reads_starred_title_command(self):
        metadata = _extract_latex_metadata(
            r"\title*{New Scheme Adaption Strategy}\author{Alice~Example}\abstract{Body}"
        )

        self.assertEqual(metadata["title"], "New Scheme Adaption Strategy")
        self.assertEqual(metadata["author"], "Alice Example")
        self.assertEqual(metadata["abstract"], "Body")

    def test_extract_latex_metadata_ignores_titlerunning_when_reading_title(self):
        metadata = _extract_latex_metadata(
            r"\titlerunning{Short Title}\title*{Long Paper Title}\author{Alice Example}"
        )

        self.assertEqual(metadata["title"], "Long Paper Title")

    def test_sanitize_latex_for_markdown_preserves_citations_and_normalizes_xbar(self):
        source = r"""\newcommand{\mU}{\bm{U}}
\newcommand*\xbar[1]{\hbox{\ensuremath{#1}}}
See \cite{CKM2025,RL87}. Let $\xbar{\mU}_j$ be the average."""

        cleaned = _sanitize_latex_for_markdown(source)

        self.assertIn("[@CKM2025; @RL87]", cleaned)
        self.assertIn(r"\overline{\bm{U}}_j", cleaned)

    def test_collect_markdown_image_assets_copies_and_rewrites_local_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "src"
            assets_dir = Path(tmpdir) / "assets"
            source_dir.mkdir()
            assets_dir.mkdir()
            image = source_dir / "figure.png"
            image.write_bytes(b"png")

            rewritten = _collect_markdown_image_assets(
                "![caption](figure.png)",
                source_dir,
                assets_dir,
            )
            self.assertEqual(rewritten, "![caption](assets/figure.png)")
            self.assertTrue((assets_dir / "figure.png").exists())

    def test_collect_markdown_image_assets_rewrites_html_img_tags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "src"
            assets_dir = Path(tmpdir) / "assets"
            source_dir.mkdir()
            assets_dir.mkdir()
            image = source_dir / "figure.png"
            image.write_bytes(b"png")

            rewritten = _collect_markdown_image_assets(
                '<figure><img src="figure.png" /></figure>',
                source_dir,
                assets_dir,
            )

            self.assertEqual(rewritten, '<figure><img src="assets/figure.png" /></figure>')
            self.assertTrue((assets_dir / "figure.png").exists())

    def test_collect_markdown_image_assets_downloads_remote_markdown_images(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "src"
            assets_dir = Path(tmpdir) / "assets"
            source_dir.mkdir()
            assets_dir.mkdir()

            with unittest.mock.patch(
                "article_extractor._download_markdown_asset",
                return_value=("remote-figure.png", "image"),
            ) as download_mock:
                rewritten = _collect_markdown_image_assets(
                    "![caption](https://arxiv.org/html/extracted/6445955/figures/method_overview.png)",
                    source_dir,
                    assets_dir,
                )

            self.assertEqual(rewritten, "![caption](assets/remote-figure.png)")
            download_mock.assert_called_once_with(
                "https://arxiv.org/html/extracted/6445955/figures/method_overview.png",
                assets_dir,
            )

    def test_collect_markdown_image_assets_renders_pdf_embeds_to_png(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            source_dir = Path(tmpdir) / "src"
            assets_dir = Path(tmpdir) / "assets"
            source_dir.mkdir()
            assets_dir.mkdir()
            html_path = source_dir / "fixture.html"
            pdf_path = source_dir / "figure.pdf"
            html_path.write_text(
                "<!doctype html><html><body><h1>Fixture</h1><p>Rendered to PDF.</p></body></html>",
                encoding="utf-8",
            )
            _render_html_to_pdf_with_chromium(html_path, pdf_path)

            rewritten = _collect_markdown_image_assets(
                '<div class="figure*">\n\n<embed src="figure.pdf" />\n\n</div>',
                source_dir,
                assets_dir,
            )

            self.assertIn("![](assets/figure.png)", rewritten)
            self.assertTrue((assets_dir / "figure.png").exists())

    def test_safe_output_name_truncates_long_titles_stably(self):
        title = (
            "Stop Designing REST APIs Like a Mid-Level Dev: 4Advanced Patterns "
            "Senior Engineers Use Instead | by HabibWahid | Mar, 2026 | Stackademic"
        )
        safe_name = _safe_output_name(title)
        self.assertLessEqual(len(safe_name), 96)
        self.assertEqual(safe_name, _safe_output_name(title))
        self.assertRegex(safe_name, r"-[0-9a-f]{10}$")

    def test_unicode_sanitizer_uses_generic_normalization(self):
        text = "𝜃 𝑆 𝒈\u2009x\u2060y\ufeff"
        normalized = _sanitize_unicode_text(text)
        self.assertEqual(normalized, "θ S g x y")

    def test_unicode_sanitizer_preserves_indented_code_blocks(self):
        text = "Paragraph\n\n    public void run() {\n        System.out.println(1);\n    }\n"
        normalized = _sanitize_unicode_text(text)
        self.assertIn("\n\n    public void run() {\n", normalized)
        self.assertIn("\n        System.out.println(1);\n", normalized)

    def test_markdown_postprocess_drops_fragment_links_only(self):
        text = (
            "See [1](#R1), [Fig. 2](#F2), "
            "[14](#bib.bib9 \"Paper title\"), and [external](https://example.com/#x)."
        )
        cleaned = _postprocess_markdown(text)
        self.assertEqual(
            cleaned,
            "See 1, Fig. 2, 14, and [external](https://example.com/#x).",
        )

    def test_extract_meta_reads_scholarly_meta_tags(self):
        html = """<!doctype html><html lang="en"><head>
        <title>Fallback Site Title - ScienceDirect</title>
        <meta name="citation_title" content="Paper Title" />
        <meta name="citation_author" content="Alice Example" />
        <meta name="citation_author" content="Bob Example" />
        <meta name="citation_publication_date" content="2025/06/01" />
        <meta name="citation_journal_title" content="Journal of Tests" />
        <meta name="citation_volume" content="18" />
        <meta name="citation_issue" content="2" />
        <meta name="citation_firstpage" content="101" />
        <meta name="citation_lastpage" content="110" />
        <meta name="citation_doi" content="10.1016/j.test.2025.123456" />
        </head><body></body></html>"""

        meta = _extract_meta(html, "Fallback Site Title - ScienceDirect", "https://example.com/article")

        self.assertEqual(meta["title"], "Paper Title")
        self.assertEqual(meta["author"], "Alice Example and Bob Example")
        self.assertEqual(meta["date"], "2025-06-01")
        self.assertEqual(meta["container_title"], "Journal of Tests")
        self.assertEqual(meta["volume"], "18")
        self.assertEqual(meta["issue"], "2")
        self.assertEqual(meta["pages"], "101-110")
        self.assertEqual(meta["doi"], "10.1016/j.test.2025.123456")

    def test_enrich_meta_with_doi_fills_missing_authors_and_bib_fields(self):
        from unittest.mock import Mock, patch

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "title": "Paper Title",
            "author": [
                {"family": "Liu", "given": "Xuanyu"},
                {"family": "Wang", "given": "Shaobin"},
            ],
            "issued": {"date-parts": [[2025, 6]]},
            "container-title": ["Journal of Radiation Research and Applied Sciences"],
            "publisher": "Elsevier BV",
            "volume": "18",
            "issue": "2",
            "article-number": "101454",
            "resource": {"primary": {"URL": "https://linkinghub.elsevier.com/retrieve/pii/S1687850725001669"}},
        }

        meta = {
            "title": "Paper Title - ScienceDirect",
            "url": "https://www.sciencedirect.com/science/article/pii/S1687850725001669",
            "canonical_url": "https://www.sciencedirect.com/science/article/pii/S1687850725001669",
            "source_site": "www.sciencedirect.com",
            "doi": "10.1016/j.jrras.2025.101454",
        }

        with patch("article_extractor.requests.get", return_value=response):
            enriched = _enrich_meta_with_doi(meta)

        self.assertEqual(enriched["title"], "Paper Title")
        self.assertEqual(enriched["author"], "Liu, Xuanyu and Wang, Shaobin")
        self.assertEqual(enriched["date"], "2025-06")
        self.assertEqual(enriched["container_title"], "Journal of Radiation Research and Applied Sciences")
        self.assertEqual(enriched["publisher"], "Elsevier BV")
        self.assertEqual(enriched["volume"], "18")
        self.assertEqual(enriched["issue"], "2")
        self.assertEqual(enriched["pages"], "101454")
        self.assertEqual(enriched["source_site"], "linkinghub.elsevier.com")

        citation = _derive_citation_metadata(
            title=enriched["title"],
            author=enriched["author"],
            date=enriched["date"],
            url=enriched["url"],
            canonical_url=enriched["canonical_url"],
            source_site=enriched["source_site"],
            description=None,
            doc_id="abc123",
            doi=enriched["doi"],
            container_title=enriched["container_title"],
            publisher=enriched["publisher"],
            volume=enriched["volume"],
            issue=enriched["issue"],
            pages=enriched["pages"],
        )
        self.assertIn("author = {Liu, Xuanyu and Wang, Shaobin}", citation["bibtex"])
        self.assertIn("journal = {Journal of Radiation Research and Applied Sciences}", citation["bibtex"])
        self.assertIn("volume = {18}", citation["bibtex"])
        self.assertIn("number = {2}", citation["bibtex"])
        self.assertIn("pages = {101454}", citation["bibtex"])

    def test_markdown_postprocess_isolates_single_line_display_math(self):
        source = (
            "Paragraph before $$a = b^2$$ with trailing prose.\n\n"
            "Next paragraph $$c = d$$ split inline.\n\n"
            "Already padded:\n\n$$e = f$$\n\nAfter."
        )
        cleaned = _postprocess_markdown(source)
        self.assertIn("Paragraph before\n\n$$a = b^2$$\n\nwith trailing prose.", cleaned)
        self.assertIn("Next paragraph\n\n$$c = d$$\n\nsplit inline.", cleaned)
        self.assertIn("$$e = f$$", cleaned)
        self.assertNotIn("$$a = b^2$$ with", cleaned)

    def test_prepare_html_for_markdown_unwraps_single_image_latexml_figure_tables(self):
        soup = BeautifulSoup(
            """
            <article>
              <figure class="ltx_figure">
                <table class="ltx_tabular">
                  <tbody><tr><td><img src="x1.png" alt="Refer to caption" /></td></tr></tbody>
                </table>
                <figcaption>Figure 1: Model overview.</figcaption>
              </figure>
            </article>
            """,
            "html.parser",
        )

        _prepare_html_for_markdown(soup)
        html = str(soup)

        self.assertIn('<img alt="Refer to caption" src="x1.png"/>', html)
        self.assertIn("Figure 1: Model overview.", html)
        self.assertNotIn("<table", html)

    def test_normalize_latexml_equation_tables_preserves_full_row_tex(self):
        soup = BeautifulSoup(
            """
            <article>
              <table class="ltx_equationgroup ltx_eqn_align ltx_eqn_table">
                <tbody id="S3.E1">
                  <tr class="ltx_equation ltx_eqn_row ltx_align_baseline">
                    <td class="ltx_td ltx_align_right ltx_eqn_cell">
                      <math><semantics><mi>z</mi><annotation encoding="application/x-tex">\\displaystyle\\mathbf{z}_{0}</annotation></semantics></math>
                    </td>
                    <td class="ltx_td ltx_align_left ltx_eqn_cell">
                      <math><semantics><mi>=</mi><annotation encoding="application/x-tex">\\displaystyle=[\\mathbf{x}]+\\mathbf{E}_{pos},</annotation></semantics></math>
                    </td>
                    <td class="ltx_td ltx_align_left ltx_eqn_cell">
                      <math><semantics><mi>E</mi><annotation encoding="application/x-tex">\\displaystyle\\mathbf{E}\\in\\mathbb{R}^{D}</annotation></semantics></math>
                    </td>
                  </tr>
                </tbody>
              </table>
            </article>
            """,
            "html.parser",
        )

        _normalize_latexml_equation_tables(soup)
        placeholders = _replace_problem_math_with_tex_placeholders(soup)

        self.assertEqual(len(placeholders), 1)
        tex, is_display = placeholders["SCRIBE_TEX_0001"]
        self.assertEqual(
            tex,
            "\\mathbf{z}_{0} = [\\mathbf{x}]+\\mathbf{E}_{pos}, \\mathbf{E}\\in\\mathbb{R}^{D}",
        )
        self.assertTrue(is_display)

    def test_pdf_markdown_postprocess_normalizes_ocr_latex_and_entities(self):
        text = (
            "At timestep  $t$ , draw  $k _ {\\mathrm {i n i t}}$ . "
            "Inline $\\alpha_ {t} = \\frac {1} {2}$ and "
            "display $$\\mathbf {A} _ {t} = \\left\\{a _ {t} ^ {(1)}\\right\\}\\tag{1}$$ "
            "and $$\\mathcal{D}\\Big%\\n{(}x,y\\Big{)}$$ "
            "plus escaped prose &lt; 0.4 and $k _ {\\mathrm {i n i t}}$."
        )
        cleaned = _postprocess_pdf_markdown(text)
        self.assertIn("At timestep $t$, draw $k_{\\mathrm{init}}$.", cleaned)
        self.assertIn("$\\alpha_{t} = \\frac{1}{2}$", cleaned)
        self.assertIn("$$\\mathbf{A}_{t} = \\left\\{a_{t}^{(1)}\\right\\}$$", cleaned)
        self.assertIn("$$\\mathcal{D}\\Big(x,y\\Big)$$", cleaned)
        self.assertNotIn("\\tag{1}", cleaned)
        self.assertIn("< 0.4", cleaned)
        self.assertIn("$k_{\\mathrm{init}}$", cleaned)

    def test_pdf_markdown_postprocess_preserves_valid_inline_math_fences(self):
        text = (
            "That is, $b = E\\rho.$ Here, $\\rho \\in C^{N \\times 1}$ is arranged as a vector. "
            "The encoding operator may be expressed as $E = W D F C M,$ where $M$ denotes motion."
        )

        cleaned = _postprocess_pdf_markdown(text)

        self.assertIn("That is, $b = E\\rho.$ Here,", cleaned)
        self.assertIn("$\\rho \\in C^{N \\times 1}$ is arranged as a vector.", cleaned)
        self.assertIn("$E = W D F C M,$ where $M$ denotes motion.", cleaned)

    def test_pdf_markdown_postprocess_strips_tag_from_malformed_display_context(self):
        text = (
            "using:$$\\mathrm{NMI}(x)=y\\tag{1}$$Alternatively, "
            "the next sentence continues."
        )

        cleaned = _postprocess_pdf_markdown(text)

        self.assertIn("using:\n\n$$\\mathrm{NMI}(x)=y$$\n\nAlternatively,", cleaned)
        self.assertNotIn("\\tag{1}", cleaned)

    def test_pdf_markdown_postprocess_strips_publisher_frontmatter_and_isolates_tables(self):
        text = (
            "1361-8415/$- see front matter © 2012 Elsevier B.V. All rights reserved.\n"
            "Table 2 Computation time. The image dimensions are$256\\times 256$| Metric | Similarity term | Full registration |\n"
            "| --- | --- | --- |\n"
            "| MIND | 9.78 | 320.4 | in two different sessions there are non-rigid deformations.\n"
        )

        cleaned = _postprocess_pdf_markdown(text)

        self.assertNotIn("1361-8415/$", cleaned)
        self.assertNotIn("see front matter", cleaned)
        self.assertIn("Table 2 Computation time. The image dimensions are$256\\times 256$", cleaned)
        self.assertIn("\n\n| Metric | Similarity term | Full registration |\n| --- | --- | --- |\n| MIND | 9.78 | 320.4 |\n\nin two different sessions", cleaned)

    def test_pdf_markdown_postprocess_preserves_absolute_value_bars_and_heading_breaks(self):
        text = (
            "differences between descriptors:\n\n"
            "$$\n"
            "\\mathcal{S}(\\mathbf{x}) = \\frac{1}{|R|} \\sum_{\\mathbf{r} \\in R} |\\mathrm{MIND}(I, \\mathbf{x}, \\mathbf{r}) - \\mathrm{MIND}(J, \\mathbf{x}, \\mathbf{r})|\n"
            "$$\n"
            "This requires $|R|$ computations.\n"
            "Body text.\n### Variance\nMore text.\nBullets intro.\n- first\n- second\nTail prose.\nNumbered intro.\n1. one\n2. two\nDone.\n"
        )

        cleaned = _postprocess_pdf_markdown(text)

        self.assertIn("\\frac{1}{|R|}", cleaned)
        self.assertIn("This requires $|R|$ computations.", cleaned)
        self.assertNotIn("\n|R|\n", cleaned)
        self.assertIn("Body text.\n\n### Variance\n\nMore text.", cleaned)
        self.assertIn("Bullets intro.\n\n- first\n- second\n\nTail prose.", cleaned)
        self.assertIn("Numbered intro.\n\n1. one\n2. two\n\nDone.", cleaned)

    def test_pdf_markdown_postprocess_removes_page_separator_thematic_breaks(self):
        text = (
            "# Title\n\n"
            "Intro paragraph.\n\n"
            "---\n\n"
            "Author footer.\n\n"
            "***\n\n"
            "## Section\n\n"
            "Body text.\n"
        )

        cleaned = _postprocess_pdf_markdown(text)

        self.assertIn("# Title", cleaned)
        self.assertIn("Intro paragraph.", cleaned)
        self.assertIn("Author footer.", cleaned)
        self.assertIn("## Section", cleaned)
        self.assertIn("Body text.", cleaned)
        self.assertNotRegex(cleaned, r"(?m)^[ \t]{0,3}(?:---+|\*\*\*+|___+)[ \t]*$")

    def test_postprocess_mistral_markdown_cleans_running_headers_and_citations(self):
        pages = [
            {
                "markdown": (
                    "# XMorpher\n\n"
                    "###### Abstract\n\n"
                    "A powerful network in DMIR*[1, 5, 15]*.\n"
                ),
                "header": None,
                "footer": None,
            },
            {
                "markdown": (
                    "J. Shi et al.\n\n"
                    "Body paragraph with networks[13,16] and more text.\n\n"
                    "Full Transformer for Deformable Image Registration"
                ),
                "header": None,
                "footer": None,
            },
            {
                "markdown": (
                    "J. Shi et al.\n\n"
                    "Another page body.\n\n"
                    "Full Transformer for Deformable Image Registration"
                ),
                "header": None,
                "footer": None,
            },
        ]

        cleaned = _postprocess_mistral_markdown(
            [page["markdown"] for page in pages],
            pages,
        )

        self.assertIn("## Abstract", cleaned)
        self.assertIn("DMIR [1, 5, 15].", cleaned)
        self.assertIn("networks [13,16]", cleaned)
        self.assertNotIn("###### Abstract", cleaned)
        self.assertNotIn("J. Shi et al.", cleaned)
        self.assertNotIn("Full Transformer for Deformable Image Registration", cleaned)

    def test_postprocess_mistral_markdown_uses_explicit_header_footer_fields(self):
        pages = [
            {
                "markdown": (
                    "Conference Header\n\n"
                    "Real content starts here.\n\n"
                    "12"
                ),
                "header": "Conference Header",
                "footer": "12",
            }
        ]

        cleaned = _postprocess_mistral_markdown(
            [page["markdown"] for page in pages],
            pages,
        )

        self.assertEqual(cleaned, "Real content starts here.")

    def test_postprocess_source_markdown_cleans_citations_and_raw_wrappers(self):
        text = (
            '<div class="figure*">\n\n![](assets/intro.png)\n\n</div>\n\n'
            '<span id="tab:x" label="tab:x"></span>\n\n'
            'Citations $$@bai2023qwen; @bai2025qwen3$$ stay inline.'
        )

        cleaned = _postprocess_source_markdown(text)

        self.assertNotIn('<div class="figure*">', cleaned)
        self.assertNotIn("</div>", cleaned)
        self.assertNotIn("<span", cleaned)
        self.assertIn("![](assets/intro.png)", cleaned)
        self.assertIn("Citations [@bai2023qwen; @bai2025qwen3] stay inline.", cleaned)

    def test_code_listing_tables_become_pre_blocks(self):
        html = """<article><table class="crayon-table"><tr><td>12</td><td class="urvanov-syntax-highlighter-code"><div class="crayon-pre"><div class="crayon-line">import numpy as np</div><div class="crayon-line">print(1)</div></div></td></tr></table></article>"""
        soup = BeautifulSoup(html, "html.parser")
        _normalize_code_listing_tables(soup)
        self.assertIsNone(soup.find("table"))
        code = soup.find("code")
        self.assertIsNotNone(code)
        self.assertEqual(code.get_text(), "import numpy as np\nprint(1)")

    def test_list_cleanup_removes_blank_bullets_and_extra_breaks(self):
        html = """<article>
        <ul>
          <li><p>First item</p></li>
          <li><br /></li>
          <li>Second<br />item</li>
          <li>
            <ul><li>Nested item</li></ul>
          </li>
        </ul>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        markdown = _convert_html_to_markdown(str(soup))

        self.assertIn("First item", markdown)
        self.assertIn("Second item", markdown)
        self.assertIn("Nested item", markdown)
        self.assertNotRegex(markdown, r"(?m)^[-*+]\s*$")
        self.assertNotIn("<br", markdown)

    def test_pmc_reference_span_labels_become_ordered_list(self):
        html = """<article>
        <section class="ref-list"><h2>References</h2>
        <ul class="ref-list" style="list-style-type:none">
          <li id="REF1"><span class="label">1.</span><cite>First ref title. Author A. Journal. 2020.</cite> [<a href="https://doi.org/1">DOI</a>]</li>
          <li id="REF2"><span class="label">2.</span><cite>Second ref title. Author B. Journal. 2021.</cite></li>
          <li id="REF3"><span class="label">3.</span><cite>Third ref title. Author C. Journal. 2022.</cite></li>
        </ul>
        </section>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        markdown = _convert_html_to_markdown(str(soup))

        self.assertRegex(markdown, r"(?m)^1\.\s+First ref title\.")
        self.assertRegex(markdown, r"(?m)^2\.\s+Second ref title\.")
        self.assertRegex(markdown, r"(?m)^3\.\s+Third ref title\.")
        self.assertNotRegex(markdown, r"(?m)^-\s+1\.")
        self.assertNotIn("1.First", markdown)

    def test_ul_with_numeric_text_prefix_becomes_ordered_list(self):
        html = """<article>
        <ul>
          <li>1. Hicks CW et al. First paper. Sci Rep. 2021;11:19159.</li>
          <li>2. Smith J et al. Second paper. J Med. 2022;12:1.</li>
          <li>3. Doe A et al. Third paper. Nat. 2023;13:99.</li>
        </ul>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        markdown = _convert_html_to_markdown(str(soup))

        self.assertRegex(markdown, r"(?m)^1\.\s+Hicks CW")
        self.assertRegex(markdown, r"(?m)^2\.\s+Smith J")
        self.assertRegex(markdown, r"(?m)^3\.\s+Doe A")
        self.assertNotRegex(markdown, r"(?m)^-\s+1\\?\.")

    def test_numbered_ul_conversion_leaves_unrelated_bullets_alone(self):
        html = """<article>
        <ul>
          <li>Apple</li>
          <li>Banana</li>
          <li>Cherry</li>
        </ul>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        markdown = _convert_html_to_markdown(str(soup))

        self.assertRegex(markdown, r"(?m)^-\s+Apple")
        self.assertRegex(markdown, r"(?m)^-\s+Banana")
        self.assertRegex(markdown, r"(?m)^-\s+Cherry")

    def test_extract_preferred_math_node_prefers_data_mathml_over_broken_assistive_copy(self):
        html = """<span class="formula">
        <span class="MathJax_SVG" data-mathml="&lt;math xmlns=&quot;http://www.w3.org/1998/Math/MathML&quot;&gt;&lt;mrow&gt;&lt;mi&gt;ρ&lt;/mi&gt;&lt;mo&gt;+&lt;/mo&gt;&lt;mi&gt;ω&lt;/mi&gt;&lt;/mrow&gt;&lt;/math&gt;"></span>
        <span class="MJX_Assistive_MathML"><math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>Ï</mi><mo>+</mo><mi>Ï‰</mi></mrow></math></span>
        <script type="math/mml"><math xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>Ï</mi><mo>+</mo><mi>Ï‰</mi></mrow></math></script>
        </span>"""

        soup = BeautifulSoup(html, "html.parser")
        math_node = _extract_preferred_math_node(soup)

        self.assertIsNotNone(math_node)
        self.assertIn("ρ", str(math_node))
        self.assertIn("ω", str(math_node))
        self.assertNotIn("Ï", str(math_node))

    def test_replace_math_with_tex_placeholders_serializes_inline_annotation_math(self):
        html = """<article><p>That is, <math display="inline"><semantics><mrow><mi>b</mi><mo>=</mo><mi>E</mi><mi>ρ</mi><mo>.</mo></mrow><annotation encoding="application/x-tex">b = E\\rho.</annotation></semantics></math> Here.</p></article>"""
        soup = BeautifulSoup(html, "html.parser")

        replacements = _replace_problem_math_with_tex_placeholders(soup)
        restored = _restore_tex_placeholders(str(soup), replacements, target="markdown")

        self.assertIn("$b = E\\rho.$", restored)
        self.assertIn("That is, $b = E\\rho.$ Here.", restored)
        self.assertIn("Here.", restored)
        self.assertNotIn("<math", restored)

    def test_replace_math_with_tex_placeholders_serializes_plain_mathml(self):
        html = """<article><p><math display="inline" xmlns="http://www.w3.org/1998/Math/MathML"><mrow><mi>E</mi><mo>=</mo><mi>W</mi><mi>D</mi><mi>F</mi><mi>C</mi><mi>M</mi><mo>,</mo></mrow></math> where M applies warping.</p></article>"""
        soup = BeautifulSoup(html, "html.parser")

        replacements = _replace_problem_math_with_tex_placeholders(soup)

        self.assertTrue(replacements)
        restored = _restore_tex_placeholders(str(soup), replacements, target="markdown")
        self.assertIn("$", restored)
        self.assertIn("$E = W D F C M,$ where M applies warping.", restored)
        self.assertNotIn("<math", restored)

    def test_replace_math_with_tex_placeholders_promotes_mtable_math_to_display_block(self):
        html = """<article><p>Loss may be written as <math xmlns="http://www.w3.org/1998/Math/MathML"><mtable><mtr><mtd><mi>L</mi></mtd><mtd><mo>=</mo></mtd><mtd><mi>x</mi></mtd></mtr><mtr><mtd></mtd><mtd></mtd><mtd><mi>y</mi></mtd></mtr></mtable></math> where λ is fixed.</p></article>"""
        soup = BeautifulSoup(html, "html.parser")

        replacements = _replace_problem_math_with_tex_placeholders(soup)
        restored = _restore_tex_placeholders(str(soup), replacements, target="markdown")

        self.assertIn("Loss may be written as\n\n$$\n", restored)
        self.assertIn("\\begin{aligned}", restored)
        self.assertIn("\n$$\n\nwhere λ is fixed.", restored)

    def test_restore_tex_placeholders_merges_adjacent_display_fragments(self):
        markdown = "Based on these patches:\n\nSCRIBE_TEX_0001SCRIBE_TEX_0002\n\nwhere x is fixed."
        replacements = {
            "SCRIBE_TEX_0001": (r"\mathcal{S}(I_F, I_M \circ T_\theta) =", True),
            "SCRIBE_TEX_0002": (r"\frac{1}{NC}\sum_{i=1}^{N} x_i", True),
        }

        restored = _restore_tex_placeholders(markdown, replacements, target="markdown")

        self.assertIn(
            "$$\n\\mathcal{S}(I_F, I_M \\circ T_\\theta) =\n\\frac{1}{NC}\\sum_{i=1}^{N} x_i\n$$",
            restored,
        )
        self.assertNotIn("$$\n\\mathcal{S}(I_F, I_M \\circ T_\\theta) =\n$$", restored)
        self.assertNotIn("$$\n\\frac{1}{NC}\\sum_{i=1}^{N} x_i\n$$", restored)

    def test_cleanup_latex_normalizes_invalid_norm_delimiters(self):
        source = r"\left∥E \rho - b\right∥_{2}^{2} + \left{x\right}"

        cleaned = _cleanup_latex(source)

        self.assertIn(r"\left\lVert E \rho - b\right\rVert _{2}^{2}", cleaned)
        self.assertIn(r"\left\{x\right\}", cleaned)

    def test_cleanup_latex_removes_line_continuation_comment_artifacts(self):
        source = "\\mathcal{D}\\Big%\n{(}x,y\\Big{)} + A\\rightarrow%\n\\mathbb{R}^{C}"

        cleaned = _cleanup_latex(source)

        self.assertNotIn("%", cleaned)
        self.assertIn(r"\mathcal{D}\Big(x,y\Big)", cleaned)
        self.assertIn(r"A\rightarrow\mathbb{R}^{C}", cleaned)

    def test_normalize_katex_compatible_tex_rewrites_sized_braced_delimiters(self):
        source = r"\Big{(}x+y\Big{)} + \bigl{[}z\biggr{]}"

        cleaned = _normalize_katex_compatible_tex(source)

        self.assertEqual(cleaned, r"\Big(x+y\Big) + \bigl[z\biggr]")

    def test_html_markdown_pipeline_restores_math_as_last_step(self):
        markdown = (
            "That is, SCRIBE_TEX_0001 Here, SCRIBE_TEX_0002 is arranged, "
            "and SCRIBE_TEX_0003 where SCRIBE_TEX_0004 applies warping."
        )
        replacements = {
            "SCRIBE_TEX_0001": ("b = E\\rho.", False),
            "SCRIBE_TEX_0002": ("\\rho \\in C^{N \\times 1}", False),
            "SCRIBE_TEX_0003": ("E = W D F C M,", False),
            "SCRIBE_TEX_0004": ("M \\in R^{N \\times N}", False),
        }

        cleaned = _postprocess_markdown_before_math_restore(markdown)
        cleaned = _restore_tex_placeholders(cleaned, replacements, target="markdown")

        self.assertIn("That is, $b = E\\rho.$ Here, $\\rho \\in C^{N \\times 1}$", cleaned)
        self.assertIn("and $E = W D F C M,$ where $M \\in R^{N \\times N}$ applies warping.", cleaned)

    def test_table_figures_keep_raster_image_and_caption(self):
        html = """<article>
        <figure>
          <img src="table-1.png" alt="table image" />
          <figcaption>Table 1: Summary of benchmarks.</figcaption>
        </figure>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        markdown = _postprocess_markdown(_convert_html_to_markdown(str(soup)))

        self.assertIn("table-1.png", markdown)
        self.assertIn("Table 1: Summary of benchmarks.", markdown)

    def test_table_figures_preserve_nested_table_data_instead_of_icon_image(self):
        html = """<article>
        <figure class="ltx_table">
          <figcaption>
            Table 1: Summary.
            <img src="icon.png" alt="[Uncaptioned image]" />
          </figcaption>
          <div>
            <table>
              <thead><tr><th>Dataset</th><th>Year</th></tr></thead>
              <tbody><tr><td>GRIT</td><td>2024</td></tr></tbody>
            </table>
          </div>
        </figure>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        markdown = _postprocess_markdown(_convert_html_to_markdown(str(soup)))

        self.assertIn("Dataset", markdown)
        self.assertIn("GRIT", markdown)
        self.assertIn("2024", markdown)
        self.assertNotIn("icon.png", markdown)
        self.assertIn("Table 1: Summary.", markdown)

    def test_table_cell_images_are_removed_but_text_table_remains(self):
        html = """<article>
        <table>
          <thead><tr><th>Metric</th><th>Legend</th></tr></thead>
          <tbody>
            <tr><td>AP</td><td><img src="legend.png" alt="legend" /> 52.1</td></tr>
          </tbody>
        </table>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        normalized = str(soup)

        self.assertNotIn("legend.png", normalized)
        self.assertIn("Metric", normalized)
        self.assertIn("52.1", normalized)

    def test_table_cell_images_use_meaningful_alt_text_when_available(self):
        html = """<article>
        <table>
          <tbody>
            <tr><td>Status</td><td><img src="ok.png" alt="Video" /> enabled</td></tr>
          </tbody>
        </table>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        normalized = str(soup)

        self.assertNotIn("ok.png", normalized)
        self.assertIn("Video", normalized)
        self.assertIn("enabled", normalized)

    def test_table_cell_images_drop_generic_placeholder_alt_text(self):
        html = """<article>
        <table>
          <tbody>
            <tr><td>Status</td><td><img src="icon.png" alt="[Uncaptioned image]" /> enabled</td></tr>
          </tbody>
        </table>
        </article>"""
        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        normalized = str(soup)

        self.assertNotIn("icon.png", normalized)
        self.assertNotIn("[Uncaptioned image]", normalized)
        self.assertIn("enabled", normalized)

    def test_postprocess_markdown_normalizes_arxiv_bullet_artifacts(self):
        source = (
            "Table 1: Summary of representative datasets. "
            "∙ \\\\bullet Modality: Image, Video. "
            "∙ \\\\bullet Role: D: Training Dataset."
        )

        cleaned = _postprocess_markdown(source)

        self.assertNotIn("\\\\bullet", cleaned)
        self.assertNotIn("∙", cleaned)
        self.assertIn("- Modality: Image, Video.", cleaned)
        self.assertIn("- Role: D: Training Dataset.", cleaned)

    def test_postprocess_markdown_normalizes_literal_bullet_list_items(self):
        source = "-   •\n\n    Bounding Boxes: coarse regions.\n"

        cleaned = _postprocess_markdown(source)

        self.assertNotIn("•", cleaned)
        self.assertIn("- Bounding Boxes: coarse regions.", cleaned)

    def test_postprocess_markdown_converts_residual_html_tables(self):
        source = """<table style="width:100%;">
<tbody>
<tr class="odd">
<td>Dataset</td>
<td>Year</td>
<td>Role</td>
</tr>
<tr class="even">
<td>MagicBrush [226]</td>
<td>2023</td>
<td><p>D</p>
<p>B</p></td>
</tr>
</tbody>
</table>"""

        cleaned = _postprocess_markdown(source)

        self.assertNotIn("<table", cleaned)
        self.assertIn("| Dataset | Year | Role |", cleaned)
        self.assertIn("| MagicBrush [226] | 2023 | D B |", cleaned)

    def test_postprocess_markdown_unwraps_trivial_display_matrix(self):
        source = """$$\\begin{matrix}
{DL = 1 - \\frac{2a}{b},}
\\end{matrix}$$"""

        cleaned = _postprocess_markdown(source)

        self.assertNotIn("\\begin{matrix}", cleaned)
        self.assertNotIn("\\end{matrix}", cleaned)
        self.assertIn("$$\nDL = 1 - \\frac{2a}{b},\n$$", cleaned)

    def test_postprocess_markdown_normalizes_tex_delimiters_in_block_math(self):
        source = """$$\\begin{matrix}
{E_{\\text{total}} =} & {\\underset{E(c)}{\\text{min}}{\\sum\\limits_{c = 1}^{N}\\left\\lbrack a + b \\rbrack,}}
\\end{matrix}$$"""

        cleaned = _postprocess_markdown(source)

        self.assertIn("\\left[ a + b ]", cleaned)
        self.assertNotIn("\\left\\lbrack", cleaned)
        self.assertNotIn("\\rbrack", cleaned)

    def test_postprocess_markdown_promotes_inline_matrix_environment_to_display_block(self):
        source = (
            "The scaled Augmented Lagrangian may be written as "
            "$\\begin{matrix}\n"
            "{L(\\rho,\\omega,a)} & = & {x} \\\\\n"
            "& & {+ y}\n"
            "\\end{matrix}$ where $\\lambda$ is the penalty parameter."
        )

        cleaned = _postprocess_markdown(source)

        self.assertIn("written as\n\n$$\n\\begin{matrix}", cleaned)
        self.assertIn("\\end{matrix}\n$$\n\nwhere $\\lambda$", cleaned)

    def test_postprocess_markdown_promotes_bare_matrix_environment_to_display_block(self):
        source = (
            "Step one is solved first.\n\n"
            "\\begin{matrix}\n"
            "{2.\\omega^{(j+1)}} & = & {x} \\\\\n"
            "& & {+ y}\n"
            "\\end{matrix}$$\n\n"
            "Then the next paragraph continues."
        )

        cleaned = _postprocess_markdown(source)

        self.assertIn("Step one is solved first.\n\n$$\n\\begin{matrix}", cleaned)
        self.assertIn("\\end{matrix}\n$$\n\nThen the next paragraph continues.", cleaned)

    def test_postprocess_markdown_normalizes_mixed_matrix_fences(self):
        source = (
            "Specifically, the loss is given as "
            "$\\begin{matrix}\n"
            "L_{{feat},c,d} & = & x \\\\\n"
            "& & {+ y}\n"
            "\\end{matrix}\n"
            "$$\n\n"
            "where $\\phi$ is the feature map."
        )

        cleaned = _postprocess_markdown(source)

        self.assertIn("given as \n\n$$\n\\begin{matrix}", cleaned)
        self.assertIn("\\end{matrix}\n$$\n\nwhere $\\phi$", cleaned)

    def test_postprocess_markdown_canonicalizes_stray_dollar_fences_around_matrix(self):
        source = (
            "The corresponding scaled Augmented Lagrangian may be written as\n\n"
            "$$\n"
            "$$\n\n"
            "\\begin{matrix}\n"
            "{L(\\rho,\\omega,a)} & = & {x} \\\\\n"
            "& & {+ y}\n"
            "\\end{matrix}\n\n"
            "$$ where $\\lambda$ is the penalty parameter."
        )

        cleaned = _postprocess_markdown(source)

        self.assertNotIn("$$\n$$", cleaned)
        self.assertIn("written as\n\n$$\n\\begin{matrix}", cleaned)
        self.assertIn("\\end{matrix}\n$$\n\nwhere $\\lambda$ is the penalty parameter.", cleaned)

    def test_postprocess_markdown_removes_stray_inline_dollar_before_prose(self):
        source = (
            "3. a^{({j + 1})} = a^{(j)} + \\rho^{({j + 1})} + \\omega^{({j + 1})},$ "
            "as depicted in Fig. 1.Step 3 can be applied directly."
        )

        cleaned = _postprocess_markdown(source)

        self.assertNotIn(",$ as", cleaned)
        self.assertIn(")}, as depicted in Fig. 1.", cleaned)

    def test_postprocess_markdown_keeps_valid_closing_dollar_before_prose(self):
        source = (
            "That is, $b = E\\rho.$ Here, $\\rho \\in C^{N \\times 1}$ is arranged as a vector, "
            "and $E = W D F C M,$ where $M \\in R^{N \\times N}$ applies warping."
        )

        cleaned = _postprocess_markdown(source)

        self.assertIn("That is, $b = E\\rho.$ Here,", cleaned)
        self.assertIn("and $E = W D F C M,$ where $M \\in R^{N \\times N}$", cleaned)

    def test_postprocess_markdown_splits_inline_math_before_here_and_where(self):
        source = (
            "That is, $b = E\\rho. Here, $\\rho \\in C^{N \\times 1}$ is arranged as a vector, "
            "and $b \\in C^{K \\times 1} is a vector, where $M \\in R^{N \\times N}$ applies warping."
        )

        cleaned = _postprocess_markdown(source)

        self.assertIn("That is, $b = E\\rho.$ Here, $\\rho \\in C^{N \\times 1}$", cleaned)
        self.assertIn("and $b \\in C^{K \\times 1}$ is a vector, where $M \\in R^{N \\times N}$", cleaned)

    def test_postprocess_markdown_adds_space_after_period_before_step_prose(self):
        source = "as depicted in Fig. 1.Step 3 can be applied directly."

        cleaned = _postprocess_markdown(source)

        self.assertIn("Fig. 1. Step 3 can be applied directly.", cleaned)

    def test_image_cleanup_is_structural_and_captioned_figures_expand_in_pdf(self):
        html = """<article>
        <figure class="fig">
          <h4 class="obj_head">Figure 1.</h4>
          <p class="img-box"><a href="/fig"><img src="figure.jpg" width="120" style="width:120px" /></a></p>
          <figcaption><p>This is a long figure caption that describes the image in enough detail to count as content.</p></figcaption>
        </figure>
        <div class="utility-link"><a href="/fig-large">View asset</a></div>
        <p>Body text.</p>
        </article>"""

        md_soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(md_soup)
        self.assertNotIn("View asset", md_soup.get_text(" ", strip=True))
        self.assertIn("Body text.", md_soup.get_text(" ", strip=True))

        pdf_soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_pdf(pdf_soup)
        pdf_img = pdf_soup.find("img")
        self.assertIsNotNone(pdf_img)
        self.assertEqual(pdf_img.get("src"), "figure.jpg")
        self.assertIsNone(pdf_img.get("width"))
        self.assertNotIn("style", pdf_img.attrs)
        self.assertIsNone(pdf_soup.find("h4"))
        self.assertNotIn("View asset", pdf_soup.get_text(" ", strip=True))

    def test_pmc_figure_keeps_image_heading_and_caption_without_utility_link(self):
        html = """<article>
        <figure class="fig xbox font-sm" id="pone.0275033.g003">
          <h4 class="obj_head">Fig 3. Overview of the UNETR used.</h4>
          <p class="img-box line-height-none margin-x-neg-2 tablet:margin-x-0 text-center">
            <a class="tileshop" target="_blank" href="https://example.com/zoom">
              <img class="graphic zoom-in" src="https://example.com/pone.0275033.g003.jpg" alt="Fig 3" />
            </a>
          </p>
          <div class="p text-right font-secondary">
            <a href="figure/pone.0275033.g003/" class="usa-link" target="_blank" rel="noopener noreferrer">Open in a new tab</a>
          </div>
          <figcaption><p>A 128x128x128x1 cropped volume of the input CBCT is divided into patches.</p></figcaption>
        </figure>
        </article>"""

        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        markdown = _postprocess_markdown(_convert_html_to_markdown(str(soup)))

        self.assertIn("pone.0275033.g003.jpg", markdown)
        self.assertIn("Fig 3. Overview of the UNETR used.", markdown)
        self.assertIn("A 128x128x128x1 cropped volume of the input CBCT is divided into patches.", markdown)
        self.assertNotIn("Open in a new tab", markdown)

    def test_sciencedirect_figure_drops_download_links_and_flattens_to_markdown(self):
        html = """<article>
        <figure>
          <img src="assets/fig7.jpg" alt="Fig. 7" />
          <ol>
            <li><a href="https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7_lrg.jpg" title="Download high-res image (385KB)">Download: Download high-res image (385KB)</a></li>
            <li><a href="https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7.jpg" title="Download full-size image">Download: Download full-size image</a></li>
          </ol>
          <p>Fig. 7. Normalized joint histogram of prediction uncertainty and error rate for 3D brain tumor segmentation.</p>
        </figure>
        </article>"""

        soup = BeautifulSoup(html, "html.parser")
        _prepare_html_for_markdown(soup)
        markdown = _postprocess_markdown(_convert_html_to_markdown(str(soup)))

        self.assertIn("assets/fig7.jpg", markdown)
        self.assertIn("Fig. 7. Normalized joint histogram of prediction uncertainty and error rate for 3D brain tumor segmentation.", markdown)
        self.assertNotIn("Download high-res image", markdown)
        self.assertNotIn("Download full-size image", markdown)
        self.assertNotIn("<figure>", markdown)
        self.assertNotIn("<ol>", markdown)

    def test_postprocess_markdown_converts_residual_captionless_figure_html(self):
        source = """## Graphical abstract

<figure>
<img src="assets/ga1.jpg" alt="ga1" />
<ol>
<li><a href="https://ars.els-cdn.com/content/image/ga1_lrg.jpg" title="Download high-res image (321KB)">Download: Download high-res image (321KB)</a></li>
<li><a href="https://ars.els-cdn.com/content/image/ga1.jpg" title="Download full-size image">Download: Download full-size image</a></li>
</ol>
</figure>"""

        cleaned = _postprocess_markdown(source)

        self.assertIn("![ga1](assets/ga1.jpg)", cleaned)
        self.assertNotIn("<figure>", cleaned)
        self.assertNotIn("<ol>", cleaned)
        self.assertNotIn("Download high-res image", cleaned)

    def test_preferred_figure_image_url_prefers_high_res_download(self):
        html = """<figure>
          <img src="https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7.jpg" alt="Fig. 7" />
          <ol>
            <li><a href="https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7_lrg.jpg" title="Download high-res image (385KB)">Download: Download high-res image (385KB)</a></li>
            <li><a href="https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7.jpg" title="Download full-size image">Download: Download full-size image</a></li>
          </ol>
        </figure>"""

        soup = BeautifulSoup(html, "html.parser")
        preferred = _preferred_figure_image_url(soup.find("figure"), "https://www.sciencedirect.com/science/article/pii/S0925231219301961")

        self.assertEqual(preferred, "https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7_lrg.jpg")

    def test_collect_images_uses_downloadable_figure_asset_instead_of_preview(self):
        html = """<article>
        <figure>
          <img src="https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7.jpg" alt="Fig. 7" />
          <ol>
            <li><a href="https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7_lrg.jpg" title="Download high-res image (385KB)">Download: Download high-res image (385KB)</a></li>
            <li><a href="https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7.jpg" title="Download full-size image">Download: Download full-size image</a></li>
          </ol>
        </figure>
        </article>"""

        images = _collect_images(html, "https://www.sciencedirect.com/science/article/pii/S0925231219301961")

        self.assertIn("https://ars.els-cdn.com/content/image/1-s2.0-S0925231219301961-gr7_lrg.jpg", images)

    def test_resolve_document_relative_url_preserves_arxiv_html_article_path(self):
        resolved = _resolve_document_relative_url(
            "https://arxiv.org/html/2503.24121v3",
            "extracted/6445955/figures/featuresMap.png",
        )

        self.assertEqual(
            resolved,
            "https://arxiv.org/html/2503.24121v3/extracted/6445955/figures/featuresMap.png",
        )

    def test_download_image_uses_actual_image_bytes_not_misleading_headers(self):
        png_rgba = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        png_rgba.close()
        try:
            Image.new("RGBA", (8, 8), (255, 0, 0, 128)).save(png_rgba.name, format="PNG")
            png_bytes = Path(png_rgba.name).read_bytes()
        finally:
            Path(png_rgba.name).unlink(missing_ok=True)

        class FakeResponse:
            headers = {"content-type": "image/jpeg"}

            def raise_for_status(self):
                return None

            def iter_content(self, chunk_size):
                for idx in range(0, len(png_bytes), chunk_size):
                    yield png_bytes[idx: idx + chunk_size]

        class FakeSession:
            def get(self, *args, **kwargs):
                return FakeResponse()

        with tempfile.TemporaryDirectory() as tmpdir:
            local = _download_image(
                "https://example.com/figure.jpg",
                Path(tmpdir),
                FakeSession(),
            )
            self.assertIsNotNone(local)
            self.assertEqual(local.suffix, ".png")
            self.assertGreater(local.stat().st_size, 0)
            with Image.open(local) as im:
                self.assertEqual(im.format, "PNG")
                self.assertEqual(im.mode, "RGBA")

    def test_resize_images_for_pdf_is_atomic_for_rgba_content(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            img_path = Path(tmpdir) / "figure.jpg"
            Image.new("RGBA", (1200, 300), (0, 128, 255, 128)).save(img_path, format="PNG")

            _resize_images_for_pdf(Path(tmpdir), max_width=240)

            self.assertGreater(img_path.stat().st_size, 0)
            with Image.open(img_path) as im:
                self.assertEqual(im.format, "PNG")
                self.assertEqual(im.width, 240)
                self.assertEqual(im.mode, "RGBA")

    def test_apply_html_annotations_wraps_highlights_and_strips_noise(self):
        """Reading-PDF noise filtering operates on HTML, not raw markdown."""
        html = (
            "<p>Keep this. Remove this sentence. Keep that "
            "<em>highlighted phrase</em> inside.</p>"
            "<figure><img src='a.png'/></figure>"
            "<figure><img src='b.png'/></figure>"
            "<table><tr><td>one</td></tr></table>"
        )
        highlights = [
            {"text": "Remove this sentence.", "variant": "noise"},
            {"text": "highlighted phrase"},
            {
                "kind": "element",
                "variant": "noise",
                "elementType": "img",
                "elementIndex": 1,
            },
            {
                "kind": "element",
                "variant": "noise",
                "elementType": "table",
                "elementIndex": 0,
            },
        ]
        out = _apply_html_annotations(html, highlights)

        self.assertNotIn("Remove this sentence.", out)
        self.assertIn(
            '<mark class="reading-highlight">highlighted phrase</mark>',
            out,
        )
        self.assertIn("a.png", out)
        self.assertNotIn("b.png", out)
        self.assertNotIn("<table", out)

    def test_generate_pdf_keeps_bundle_assets_at_full_resolution(self):
        """Bundle assets must not be destructively resized by PDF generation."""
        import article_extractor

        with tempfile.TemporaryDirectory() as tmpdir:
            article_dir = Path(tmpdir) / "fixture"
            assets_dir = article_dir / "assets"
            assets_dir.mkdir(parents=True)
            full_image = assets_dir / "figure.png"
            Image.new("RGB", (2400, 1800), (255, 0, 0)).save(full_image, format="PNG")
            original_size = full_image.stat().st_size

            md_text = (
                '---\n'
                'title: "Fixture"\n'
                '---\n\n'
                "# Fixture\n\nInline image ![caption](assets/figure.png)\n"
            )

            article_extractor._generate_pdf(md_text, "Fixture", article_dir, "a5")

            with Image.open(full_image) as im:
                self.assertEqual(im.width, 2400)
                self.assertEqual(im.height, 1800)
            self.assertEqual(full_image.stat().st_size, original_size)

    def test_extract_article_preserves_math_tables_and_pdf_rendering(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            result = extract_article(
                FIXTURE_HTML,
                output_dir=tmpdir,
                page_size="a5",
                label="Research",
                url="https://example.com/article",
            )

            md_path = Path(result["md-path"])
            pdf_path = Path(result["file-path"])
            bib_path = Path(result["bib-path"])
            raw_html_path = Path(result["raw-html-path"])
            normalized_html_path = Path(result["normalized-html-path"])
            md_text = md_path.read_text(encoding="utf-8")
            bib_text = bib_path.read_text(encoding="utf-8")
            raw_html_text = raw_html_path.read_text(encoding="utf-8")
            normalized_html_text = normalized_html_path.read_text(encoding="utf-8")

            self.assertIn('doc_id: "', md_text)
            self.assertIn('doc_type: "article"', md_text)
            self.assertIn('citation_key: "', md_text)
            self.assertIn('bib_file: "Extractor Fixture.bib"', md_text)
            self.assertIn('label: "Research"', md_text)
            self.assertIn('source_site:', md_text)
            self.assertIn('language: "en"', md_text)
            self.assertIn('word_count:', md_text)
            self.assertIn('image_count:', md_text)
            self.assertIn('ingested_at:', md_text)
            self.assertIn("$$S_{b} = x$$", md_text)
            self.assertIn("$w = 1$", md_text)
            self.assertNotIn("mjx-container", md_text)
            self.assertNotIn("<table", md_text)
            self.assertNotRegex(md_text, r"\\\(\d+\\\)")
            self.assertIn("Col A", md_text)
            self.assertIn("Col B", md_text)
            self.assertIn("1", md_text)
            self.assertIn("2", md_text)
            self.assertIn("| Col A | Col B |", md_text)
            self.assertNotIn("<table", md_text)
            self.assertNotIn("<hr", md_text)
            self.assertIn("import numpy as np", md_text)
            self.assertIn("print(1)", md_text)
            self.assertNotIn("| 12 |", md_text)
            self.assertNotIn("<!--", md_text)
            self.assertNotIn("{=html}", md_text)
            self.assertIn("SC-8 takes $\\approx$ 40 minutes.", md_text)
            self.assertNotIn("\\includegraphics", md_text)
            self.assertNotIn("fig1_frontier.pdf", md_text)
            self.assertIn("$$A=1$$", md_text)
            self.assertTrue(bib_path.exists())
            self.assertTrue(raw_html_path.exists())
            self.assertTrue(normalized_html_path.exists())
            self.assertIn("@online{", bib_text)
            self.assertIn("title = {Extractor Fixture}", bib_text)
            self.assertIn("<article>", raw_html_text)
            self.assertIn("Extractor Fixture", normalized_html_text)
            self.assertNotIn("data-rel-src", normalized_html_text)

            pdf_text = subprocess.run(
                ["pdftotext", str(pdf_path), "-"],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            pdf_info = subprocess.run(
                ["pdfinfo", str(pdf_path)],
                check=True,
                capture_output=True,
                text=True,
            ).stdout
            pdf_text = unicodedata.normalize("NFKC", pdf_text)
            pdf_text_compact = pdf_text.replace(" ", "")

            self.assertNotIn("$$", pdf_text)
            self.assertIn("HeadlessChrome", pdf_info)
            self.assertIn("Skia/PDF", pdf_info)
            self.assertIn("Unicode-heavy scientific prose", pdf_text)
            self.assertIn("b ∈ [0, b_max]", pdf_text)
            self.assertIn("θ", pdf_text)
            self.assertIn("γ", pdf_text)
            self.assertIn("2.5 × 2.5 × 2.5", pdf_text)
            self.assertIn("Gödel", pdf_text_compact)
            self.assertIn("w=1", pdf_text_compact)

    def test_extract_article_can_succeed_without_pdf_when_markdown_is_primary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from unittest.mock import patch

            with patch("article_extractor._generate_pdf", side_effect=RuntimeError("pdf failed")):
                result = extract_article(
                    FIXTURE_HTML,
                    output_dir=tmpdir,
                    page_size="a5",
                    label="Research",
                    url="https://example.com/article",
                    render_pdf=True,
                    pdf_required=False,
                )

            self.assertIsNone(result["file-path"])
            self.assertTrue(Path(result["md-path"]).exists())
            self.assertIn('label: "Research"', Path(result["md-path"]).read_text(encoding="utf-8"))

    def test_extract_pdf_bytes_saves_original_pdf_and_markdown(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = Path(tmpdir) / "fixture.html"
            pdf_path = Path(tmpdir) / "fixture.pdf"
            html_path.write_text(
                "<!doctype html><html><body><h1>Fixture PDF</h1><p>Native PDF extraction should preserve this sentence.</p></body></html>",
                encoding="utf-8",
            )
            _render_html_to_pdf_with_chromium(html_path, pdf_path)

            from unittest.mock import patch

            with patch(
                "article_extractor._extract_pdf_markdown_via_mistral",
                return_value=(
                    "## OCR Output\n\n"
                    "- Equation: $$\\mathrm{NMI} (x) = y\\tag{1}$$\n"
                    "- Delimiter: $$\\mathcal{D}\\Big%\\n{(}x,y\\Big{)}$$\n",
                    {"pages": [{"markdown": "x"}]},
                ),
            ):
                result = extract_pdf_bytes(
                    pdf_bytes=pdf_path.read_bytes(),
                    output_dir=tmpdir,
                    url="https://arxiv.org/pdf/2604.08369",
                    source_name="fixture.pdf",
                    label="Research",
                    render_pdf=True,
                )

            saved_pdf = Path(result["file-path"])
            source_pdf = Path(result["source-pdf-path"])
            md_path = Path(result["md-path"])
            bib_path = Path(result["bib-path"])
            md_text = md_path.read_text(encoding="utf-8")
            bib_text = bib_path.read_text(encoding="utf-8")

            self.assertTrue(saved_pdf.exists())
            self.assertTrue(source_pdf.exists())
            self.assertTrue(bib_path.exists())
            self.assertTrue(md_path.exists())
            self.assertEqual(source_pdf.read_bytes(), pdf_path.read_bytes())
            self.assertNotEqual(saved_pdf.name, source_pdf.name)
            self.assertIn('source_format: "pdf"', md_text)
            self.assertIn('ocr_engine: "mistral"', md_text)
            self.assertIn('arxiv_id: "2604.08369"', md_text)
            self.assertIn('citation_key: "', md_text)
            self.assertIn('source_file: "fixture.pdf"', md_text)
            self.assertIn('page_count: 1', md_text)
            self.assertIn("Equation: $$\\mathrm{NMI}(x) = y$$", md_text)
            self.assertIn("Delimiter: $$\\mathcal{D}\\Big(x,y\\Big)$$", md_text)
            self.assertNotIn("\\tag{1}", md_text)
            self.assertIn("@misc{", bib_text)
            self.assertIn("archivePrefix = {arXiv}", bib_text)
            self.assertIn("eprint = {2604.08369}", bib_text)
            self.assertIsNone(result["notes-path"])

    def test_extract_pdf_bytes_falls_back_to_pdftotext(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            html_path = Path(tmpdir) / "fixture.html"
            pdf_path = Path(tmpdir) / "fixture.pdf"
            html_path.write_text(
                "<!doctype html><html><body><h1>Fallback PDF</h1><p>Plain text fallback still works.</p></body></html>",
                encoding="utf-8",
            )
            _render_html_to_pdf_with_chromium(html_path, pdf_path)

            from unittest.mock import patch

            with patch("article_extractor._extract_pdf_markdown_via_mistral", side_effect=RuntimeError("ocr failed")):
                result = extract_pdf_bytes(
                    pdf_bytes=pdf_path.read_bytes(),
                    output_dir=tmpdir,
                    url="https://example.com/papers/fallback.pdf",
                    source_name="fallback.pdf",
                    label="Research",
                )

            md_text = Path(result["md-path"]).read_text(encoding="utf-8")
            self.assertTrue(Path(result["source-pdf-path"]).exists())
            self.assertTrue(Path(result["bib-path"]).exists())
            self.assertIn('ocr_engine: "pdftotext"', md_text)
            self.assertIn('ocr_fallback: "pdftotext"', md_text)
            self.assertIn("Plain text fallback still works.", md_text)

    def test_extract_article_writes_companion_notes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            from unittest.mock import patch

            notes_body = """# Notes

## Summary
- Short summary.

## Key Points
- Point.

## Definitions
- None.

## Important Equations
- None.

## Code Takeaways
- None.

## Open Questions
- None.
"""
            with patch("article_extractor._generate_companion_notes_body_with_config", return_value=notes_body):
                result = extract_article(
                    FIXTURE_HTML,
                    output_dir=tmpdir,
                    page_size="a5",
                    label="Research",
                    url="https://example.com/article",
                    generate_notes=True,
                )

            notes_path = Path(result["notes-path"])
            self.assertTrue(notes_path.exists())
            notes_text = notes_path.read_text(encoding="utf-8")
            self.assertIn('doc_type: "notes"', notes_text)
            self.assertIn('type: "companion_notes"', notes_text)
            self.assertIn('source_article: "Extractor Fixture.md"', notes_text)
            self.assertIn('source_doc_id: "', notes_text)
            self.assertIn('label: "Research"', notes_text)
            self.assertIn("# Notes", notes_text)

    def test_resolve_notes_client_config_supports_provider_overrides(self):
        config = _resolve_notes_client_config(
            {
                "provider": "anthropic",
                "model": "claude-sonnet-4-20250514",
                "api_key": "secret",
                "timeout": 45,
            }
        )
        self.assertEqual(config["provider"], "anthropic")
        self.assertEqual(config["model"], "claude-sonnet-4-20250514")
        self.assertEqual(config["api_key"], "secret")
        self.assertEqual(config["timeout"], 45)

    def test_generate_notes_openai_compatible_uses_base_url_and_bearer_auth(self):
        from unittest.mock import Mock, patch

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"choices": [{"message": {"content": "notes"}}]}

        with patch("article_extractor.requests.post", return_value=response) as post_mock:
            content = _generate_notes_via_openai_compatible(
                "prompt",
                {
                    "provider": "openai",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-4.1-mini",
                    "api_key": "sk-test",
                    "timeout": 30,
                },
            )

        self.assertEqual(content, "notes")
        self.assertEqual(
            post_mock.call_args.kwargs["headers"]["Authorization"],
            "Bearer sk-test",
        )
        self.assertEqual(
            post_mock.call_args.args[0],
            "https://api.openai.com/v1/chat/completions",
        )

    def test_generate_notes_anthropic_uses_messages_api(self):
        from unittest.mock import Mock, patch

        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {"content": [{"type": "text", "text": "notes"}]}

        with patch("article_extractor.requests.post", return_value=response) as post_mock:
            content = _generate_notes_via_anthropic(
                "prompt",
                {
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-20250514",
                    "api_key": "ak-test",
                    "timeout": 30,
                    "anthropic_version": "2023-06-01",
                },
            )

        self.assertEqual(content, "notes")
        self.assertEqual(
            post_mock.call_args.args[0],
            "https://api.anthropic.com/v1/messages",
        )
        self.assertEqual(post_mock.call_args.kwargs["headers"]["x-api-key"], "ak-test")

    def test_strip_reference_sections_for_notes_removes_trailing_references(self):
        md_text = (
            "---\n"
            'title: "Fixture"\n'
            "---\n\n"
            "# Fixture\n\n"
            "Core content.\n\n"
            "## Method\n\n"
            "Important details.\n\n"
            "## References\n\n"
            "1. Example citation\n"
            "2. Another citation\n"
        )

        cleaned = _strip_reference_sections_for_notes(md_text)

        self.assertIn("Core content.", cleaned)
        self.assertIn("Important details.", cleaned)
        self.assertNotIn("## References", cleaned)
        self.assertNotIn("Example citation", cleaned)


if __name__ == "__main__":
    unittest.main()

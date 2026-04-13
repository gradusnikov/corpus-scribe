import subprocess
import tempfile
import unittest
import unicodedata
from pathlib import Path

from bs4 import BeautifulSoup
from PIL import Image

from article_extractor import (
    _convert_html_to_markdown,
    _derive_citation_metadata,
    _download_image,
    _enrich_meta_with_doi,
    _extract_meta,
    _generate_notes_via_anthropic,
    _generate_notes_via_openai_compatible,
    _normalize_code_listing_tables,
    _postprocess_markdown,
    _postprocess_pdf_markdown,
    _prepare_html_for_markdown,
    _prepare_html_for_pdf,
    _render_html_to_pdf_with_chromium,
    _resolve_notes_client_config,
    _resize_images_for_pdf,
    _safe_output_name,
    _sanitize_unicode_text,
    _strip_reference_sections_for_notes,
    extract_article,
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

    def test_pdf_markdown_postprocess_normalizes_ocr_latex_and_entities(self):
        text = (
            "At timestep  $t$ , draw  $k _ {\\mathrm {i n i t}}$ . "
            "Inline $\\alpha_ {t} = \\frac {1} {2}$ and "
            "display $$\\mathbf {A} _ {t} = \\left\\{a _ {t} ^ {(1)}\\right\\}$$ "
            "plus escaped prose &lt; 0.4 and $k _ {\\mathrm {i n i t}}$."
        )
        cleaned = _postprocess_pdf_markdown(text)
        self.assertIn("At timestep $t$, draw $k_{\\mathrm{init}}$.", cleaned)
        self.assertIn("$\\alpha_{t} = \\frac{1}{2}$", cleaned)
        self.assertIn("$$\\mathbf{A}_{t} = \\left\\{a_{t}^{(1)}\\right\\}$$", cleaned)
        self.assertIn("< 0.4", cleaned)
        self.assertIn("$k_{\\mathrm{init}}$", cleaned)

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
            md_text = md_path.read_text(encoding="utf-8")
            bib_text = bib_path.read_text(encoding="utf-8")

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
            self.assertIn("@online{", bib_text)
            self.assertIn("title = {Extractor Fixture}", bib_text)

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
                return_value=("## OCR Output\n\n- Equation: $E = mc^2$\n", {"pages": [{"markdown": "x"}]}),
            ):
                result = extract_pdf_bytes(
                    pdf_bytes=pdf_path.read_bytes(),
                    output_dir=tmpdir,
                    url="https://arxiv.org/pdf/2604.08369",
                    source_name="fixture.pdf",
                    label="Research",
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
            self.assertIn("Equation: $E = mc^2$", md_text)
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

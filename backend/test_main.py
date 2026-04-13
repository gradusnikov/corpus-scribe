import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main


class MainApiTests(unittest.TestCase):
    def setUp(self):
        self.client = main.app.test_client()

    @patch("main.extract_article")
    def test_save_local_uses_label_subdirectory(self, extract_article_mock):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "OUTPUT_DIR", tmpdir):
            extract_article_mock.return_value = {
                "title": "Fixture",
                "dir": f"{tmpdir}/Deep Learning/Fixture",
                "file-path": f"{tmpdir}/Deep Learning/Fixture/Fixture.pdf",
                "bib-path": f"{tmpdir}/Deep Learning/Fixture/Fixture.bib",
                "md-path": f"{tmpdir}/Deep Learning/Fixture/Fixture.md",
                "notes-path": f"{tmpdir}/Deep Learning/Fixture/Fixture.notes.md",
                "notes-doc-id": "abc123:notes",
                "metadata": {
                    "doc_id": "abc123",
                    "url": "https://example.com/article",
                    "canonical_url": "https://example.com/article",
                    "label": "Deep Learning",
                    "source_site": "example.com",
                    "citation_key": "doe2026fixture",
                    "doi": "10.1000/example",
                    "arxiv_id": None,
                    "language": "en",
                    "word_count": 123,
                    "image_count": 2,
                    "ingested_at": "2026-04-12T10:00:00+00:00",
                },
            }
            with patch.object(main, "_upsert_index_records") as upsert_mock:
                response = self.client.post(
                    "/save_local",
                    json={
                        "apiKey": main.API_KEY,
                        "html": "<html><body><article>fixture</article></body></html>",
                        "label": "Deep Learning",
                        "notes": {
                            "provider": "openai",
                            "model": "gpt-4.1-mini",
                            "base_url": "https://api.openai.com/v1",
                        },
                    },
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["label"], "Deep Learning")
        self.assertEqual(payload["primary"], f"{tmpdir}/Deep Learning/Fixture/Fixture.md")
        self.assertTrue(payload["pdfAvailable"])
        self.assertEqual(payload["bib"], f"{tmpdir}/Deep Learning/Fixture/Fixture.bib")
        self.assertTrue(payload["notesAvailable"])
        self.assertEqual(payload["notes"], f"{tmpdir}/Deep Learning/Fixture/Fixture.notes.md")
        self.assertEqual(payload["notesDocId"], "abc123:notes")
        self.assertEqual(payload["metadata"]["source_site"], "example.com")
        self.assertEqual(extract_article_mock.call_args.kwargs["output_dir"], f"{tmpdir}/Deep Learning")
        self.assertEqual(extract_article_mock.call_args.kwargs["label"], "Deep Learning")
        self.assertFalse(extract_article_mock.call_args.kwargs["pdf_required"])
        self.assertTrue(extract_article_mock.call_args.kwargs["generate_notes"])
        self.assertEqual(
            extract_article_mock.call_args.kwargs["notes_config"]["provider"],
            "openai",
        )
        upsert_records = upsert_mock.call_args.args[0]
        self.assertEqual(len(upsert_records), 2)
        article_record = next(item for item in upsert_records if item["type"] == "article")
        notes_record = next(item for item in upsert_records if item["type"] == "notes")
        self.assertEqual(article_record["doc_id"], "abc123")
        self.assertEqual(article_record["citation_key"], "doe2026fixture")
        self.assertEqual(notes_record["source_doc_id"], "abc123")
        self.assertEqual(article_record["notes_path"], "Deep Learning/Fixture/Fixture.notes.md")

    @patch("main.extract_pdf_url")
    def test_save_pdf_uses_label_subdirectory(self, extract_pdf_url_mock):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "OUTPUT_DIR", tmpdir):
            extract_pdf_url_mock.return_value = {
                "title": "Fixture PDF",
                "dir": f"{tmpdir}/Papers/Fixture PDF",
                "file-path": f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.reading.pdf",
                "source-pdf-path": f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.source.pdf",
                "bib-path": f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.bib",
                "md-path": f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.md",
                "notes-path": f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.notes.md",
                "notes-doc-id": "pdf123:notes",
                "metadata": {
                    "doc_id": "pdf123",
                    "url": "https://example.com/paper.pdf",
                    "canonical_url": "https://example.com/paper.pdf",
                    "label": "Papers",
                    "source_site": "example.com",
                    "source_format": "pdf",
                    "citation_key": "doe2026fixturepdf",
                    "doi": None,
                    "arxiv_id": "2604.08369",
                    "page_count": 12,
                    "language": "en",
                    "word_count": 456,
                    "image_count": 0,
                    "ingested_at": "2026-04-12T10:00:00+00:00",
                },
            }
            with patch.object(main, "_upsert_index_records") as upsert_mock:
                response = self.client.post(
                    "/save_pdf",
                    json={
                        "apiKey": main.API_KEY,
                        "url": "https://example.com/paper.pdf",
                        "sourceName": "paper.pdf",
                        "label": "Papers",
                    },
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["label"], "Papers")
        self.assertEqual(payload["primary"], f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.md")
        self.assertTrue(payload["pdfAvailable"])
        self.assertTrue(payload["sourcePdfAvailable"])
        self.assertEqual(payload["bib"], f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.bib")
        self.assertEqual(payload["pdf"], f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.reading.pdf")
        self.assertEqual(payload["sourcePdf"], f"{tmpdir}/Papers/Fixture PDF/Fixture PDF.source.pdf")
        self.assertEqual(payload["metadata"]["source_format"], "pdf")
        self.assertEqual(payload["metadata"]["page_count"], 12)
        self.assertEqual(extract_pdf_url_mock.call_args.kwargs["output_dir"], f"{tmpdir}/Papers")
        self.assertEqual(extract_pdf_url_mock.call_args.kwargs["label"], "Papers")
        upsert_records = upsert_mock.call_args.args[0]
        article_record = next(item for item in upsert_records if item["type"] == "article")
        self.assertEqual(article_record["source_format"], "pdf")
        self.assertEqual(article_record["citation_key"], "doe2026fixturepdf")
        self.assertEqual(article_record["page_count"], 12)

    def test_labels_returns_existing_output_directories(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "Research").mkdir()
            (root / "Machine Learning").mkdir()
            (root / "notes.txt").write_text("ignore me", encoding="utf-8")

            with patch.object(main, "OUTPUT_DIR", tmpdir):
                response = self.client.get("/labels", query_string={"apiKey": main.API_KEY})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["labels"], ["Machine Learning", "Research"])

    def test_lookup_url_finds_existing_article_by_frontmatter_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            article_dir = Path(tmpdir) / "Research" / "Sample"
            article_dir.mkdir(parents=True)
            article_path = article_dir / "Sample.md"
            article_path.write_text(
                "---\n"
                'title: "Sample Paper"\n'
                'label: "Research"\n'
                'url: "https://www.example.com/articles/42/"\n'
                'canonical_url: "https://example.com/articles/42"\n'
                "---\n\n"
                "# Sample\n\nBody.\n",
                encoding="utf-8",
            )

            with patch.object(main, "OUTPUT_DIR", tmpdir):
                response = self.client.get(
                    "/lookup_url",
                    query_string={
                        "apiKey": main.API_KEY,
                        "url": "https://example.com/articles/42/?utm=ignored",
                    },
                )
                response_alt = self.client.get(
                    "/lookup_url",
                    query_string={
                        "apiKey": main.API_KEY,
                        "url": "https://unrelated.example/nope",
                    },
                )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["exists"])
        self.assertEqual(payload["title"], "Sample Paper")
        self.assertEqual(payload["md"], str(article_path))
        self.assertEqual(payload["primary"], str(article_path))

        self.assertEqual(response_alt.status_code, 200)
        alt_payload = response_alt.get_json()
        self.assertFalse(alt_payload["exists"])

    def test_lookup_url_requires_api_key(self):
        response = self.client.get("/lookup_url", query_string={"url": "https://example.com"})
        self.assertEqual(response.status_code, 401)

    def test_capabilities_reports_pdf_ocr_availability(self):
        with patch.dict(main.os.environ, {"MISTRAL_API_KEY": "test-key"}, clear=False):
            response = self.client.get("/capabilities", query_string={"apiKey": main.API_KEY})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["pdfOcr"]["available"])
        self.assertEqual(payload["pdfOcr"]["engine"], "mistral")
        self.assertEqual(payload["pdfOcr"]["fallback"], "pdftotext")

    def test_desktop_document_includes_highlights(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'label: "Research"\n'
                'source_site: "example.com"\n'
                'ingested_at: "2026-04-13T09:00:00+00:00"\n'
                "---\n\n"
                "# Fixture\n\n"
                "Body text.\n",
                encoding="utf-8",
            )
            highlights_path = article_path.with_name("Article.highlights.json")
            highlights_path.write_text(
                json.dumps(
                    {
                        "articlePath": str(article_path),
                        "highlights": [
                            {
                                "id": "h1",
                                "text": "Important sentence",
                                "createdAt": "2026-04-13T09:30:00+00:00",
                                "startOffset": 12,
                                "endOffset": 30,
                            }
                        ],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            response = self.client.get("/desktop/document", query_string={"articlePath": str(article_path)})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["detail"]["summary"]["highlightCount"], 1)
        self.assertEqual(payload["detail"]["highlights"][0]["id"], "h1")
        self.assertEqual(payload["detail"]["highlights"][0]["text"], "Important sentence")
        self.assertEqual(payload["detail"]["highlights"][0]["startOffset"], 12)
        self.assertEqual(payload["detail"]["highlights"][0]["endOffset"], 30)

    def test_desktop_save_highlights_writes_sibling_json(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'doc_id: "fixture-article"\n'
                "---\n\n"
                "Body text.\n",
                encoding="utf-8",
            )

            response = self.client.post(
                "/desktop/highlights",
                json={
                    "articlePath": str(article_path),
                    "highlights": [
                        {
                            "id": "highlight-1",
                            "text": "A highlighted quote",
                            "createdAt": "2026-04-13T10:00:00+00:00",
                            "startOffset": 4,
                            "endOffset": 23,
                        }
                    ],
                },
            )

            highlights_path = article_path.with_name("Article.highlights.json")
            saved = json.loads(highlights_path.read_text(encoding="utf-8"))

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["highlightsPath"], str(highlights_path))
        self.assertEqual(saved["highlights"][0]["id"], "highlight-1")
        self.assertEqual(saved["highlights"][0]["text"], "A highlighted quote")
        self.assertEqual(saved["highlights"][0]["startOffset"], 4)
        self.assertEqual(saved["highlights"][0]["endOffset"], 23)

    @patch("main._generate_companion_notes")
    def test_desktop_generate_notes_backfills_existing_article(self, generate_notes_mock):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "OUTPUT_DIR", tmpdir):
            article_path = Path(tmpdir) / "Label" / "Article" / "Article.md"
            article_path.parent.mkdir(parents=True, exist_ok=True)
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'doc_id: "fixture-article"\n'
                'label: "Label"\n'
                'source_site: "example.com"\n'
                'ingested_at: "2026-04-13T09:00:00+00:00"\n'
                "---\n\n"
                "# Fixture\n\n"
                "Body text.\n",
                encoding="utf-8",
            )
            notes_path = article_path.with_name("Article.notes.md")
            generate_notes_mock.return_value = notes_path
            notes_path.write_text(
                "---\n"
                'title: "Fixture Article Notes"\n'
                'doc_id: "fixture-article:notes"\n'
                'doc_type: "notes"\n'
                'source_article: "Article.md"\n'
                'source_doc_id: "fixture-article"\n'
                "---\n\n"
                "# Notes\n\n- Generated.\n",
                encoding="utf-8",
            )

            response = self.client.post(
                "/desktop/notes/generate",
                json={"articlePath": str(article_path)},
            )

            article_text = article_path.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["notesPath"], str(notes_path))
        self.assertIn("- Generated.", payload["notesMarkdown"])
        self.assertIn('notes_file: "Article.notes.md"', article_text)
        self.assertIn('notes_doc_id: "fixture-article:notes"', article_text)

    def test_desktop_save_notes_preserves_existing_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "OUTPUT_DIR", tmpdir):
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'doc_id: "fixture-article"\n'
                'label: "Research"\n'
                'source_site: "example.com"\n'
                "---\n\n"
                "Body text.\n",
                encoding="utf-8",
            )
            notes_path = article_path.with_name("Article.notes.md")
            notes_path.write_text(
                "---\n"
                'title: "Fixture Article Notes"\n'
                'doc_id: "fixture-article:notes"\n'
                'doc_type: "notes"\n'
                'type: "companion_notes"\n'
                'generated_by: "anthropic:claude-sonnet-4-20250514"\n'
                'source_article: "Article.md"\n'
                'source_doc_id: "fixture-article"\n'
                "---\n\n"
                "- Old notes.\n",
                encoding="utf-8",
            )

            response = self.client.post(
                "/desktop/notes",
                json={
                    "articlePath": str(article_path),
                    "notesMarkdown": "# Notes\n\n- Updated.\n",
                },
            )

            saved_notes = notes_path.read_text(encoding="utf-8")
            saved_article = article_path.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["notesPath"], str(notes_path))
        self.assertIn('generated_by: "anthropic:claude-sonnet-4-20250514"', saved_notes)
        self.assertIn('source_article: "Article.md"', saved_notes)
        self.assertIn("- Updated.", saved_notes)
        self.assertIn('notes_file: "Article.notes.md"', saved_article)


    def test_desktop_save_rating_writes_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "OUTPUT_DIR", tmpdir):
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'doc_id: "fixture-article"\n'
                'label: "Research"\n'
                "---\n\n"
                "Body text.\n",
                encoding="utf-8",
            )

            response = self.client.post(
                "/desktop/rating",
                json={"articlePath": str(article_path), "rating": 4},
            )

            saved = article_path.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["success"])
        self.assertEqual(payload["rating"], 4)
        self.assertIn("rating: 4", saved)

    def test_desktop_save_rating_zero_clears_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "OUTPUT_DIR", tmpdir):
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'doc_id: "fixture-article"\n'
                "rating: 5\n"
                "---\n\n"
                "Body text.\n",
                encoding="utf-8",
            )

            response = self.client.post(
                "/desktop/rating",
                json={"articlePath": str(article_path), "rating": 0},
            )

            saved = article_path.read_text(encoding="utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["rating"], 0)
        self.assertNotIn("rating:", saved)

    def test_desktop_save_rating_rejects_out_of_range(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "OUTPUT_DIR", tmpdir):
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                "---\n\n"
                "Body.\n",
                encoding="utf-8",
            )

            too_high = self.client.post(
                "/desktop/rating",
                json={"articlePath": str(article_path), "rating": 6},
            )
            negative = self.client.post(
                "/desktop/rating",
                json={"articlePath": str(article_path), "rating": -1},
            )
            non_numeric = self.client.post(
                "/desktop/rating",
                json={"articlePath": str(article_path), "rating": "five"},
            )

            saved = article_path.read_text(encoding="utf-8")

        self.assertEqual(too_high.status_code, 400)
        self.assertEqual(negative.status_code, 400)
        self.assertEqual(non_numeric.status_code, 400)
        self.assertNotIn("rating:", saved)

    def test_desktop_save_rating_missing_article_returns_404(self):
        response = self.client.post(
            "/desktop/rating",
            json={"articlePath": "/nonexistent/Article.md", "rating": 3},
        )
        self.assertEqual(response.status_code, 404)

    def test_desktop_library_sorts_by_rating(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "DESKTOP_API_ROOT", tmpdir):
            root = Path(tmpdir)
            (root / "low").mkdir()
            (root / "low" / "Low.md").write_text(
                "---\n"
                'title: "Low Rated"\n'
                'label: "low"\n'
                'ingested_at: "2026-04-13T10:00:00+00:00"\n'
                "rating: 1\n"
                "---\n\nBody.\n",
                encoding="utf-8",
            )
            (root / "high").mkdir()
            (root / "high" / "High.md").write_text(
                "---\n"
                'title: "High Rated"\n'
                'label: "high"\n'
                'ingested_at: "2026-04-12T10:00:00+00:00"\n'
                "rating: 5\n"
                "---\n\nBody.\n",
                encoding="utf-8",
            )
            (root / "mid").mkdir()
            (root / "mid" / "Mid.md").write_text(
                "---\n"
                'title: "Mid Rated"\n'
                'label: "mid"\n'
                'ingested_at: "2026-04-11T10:00:00+00:00"\n'
                "---\n\nBody.\n",
                encoding="utf-8",
            )

            response = self.client.get("/desktop/library", query_string={"root": str(root)})

        self.assertEqual(response.status_code, 200)
        documents = response.get_json()["documents"]
        self.assertEqual([doc["title"] for doc in documents], ["High Rated", "Low Rated", "Mid Rated"])
        self.assertEqual(documents[0]["rating"], 5)
        self.assertEqual(documents[1]["rating"], 1)
        self.assertEqual(documents[2]["rating"], 0)

    def test_desktop_document_exposes_url(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'url: "https://example.com/article"\n'
                'canonical_url: "https://example.com/article/v2"\n'
                "---\n\n"
                "Body.\n",
                encoding="utf-8",
            )

            response = self.client.get(
                "/desktop/document",
                query_string={"articlePath": str(article_path)},
            )

        self.assertEqual(response.status_code, 200)
        summary = response.get_json()["detail"]["summary"]
        self.assertEqual(summary["url"], "https://example.com/article")
        self.assertEqual(summary["canonicalUrl"], "https://example.com/article/v2")

    def test_desktop_library_exposes_url(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "DESKTOP_API_ROOT", tmpdir):
            root = Path(tmpdir) / "label"
            root.mkdir()
            (root / "Article.md").write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'label: "label"\n'
                'url: "https://example.com/article"\n'
                "---\n\n"
                "Body.\n",
                encoding="utf-8",
            )

            response = self.client.get("/desktop/library", query_string={"root": str(tmpdir)})

        self.assertEqual(response.status_code, 200)
        documents = response.get_json()["documents"]
        self.assertEqual(len(documents), 1)
        self.assertEqual(documents[0]["url"], "https://example.com/article")

    def test_desktop_file_download_flag_returns_attachment(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            pdf_path = Path(tmpdir) / "Fixture.pdf"
            pdf_path.write_bytes(b"%PDF-1.4\n% fixture\n")

            inline = self.client.get(
                "/desktop/file",
                query_string={"path": str(pdf_path)},
            )
            attachment = self.client.get(
                "/desktop/file",
                query_string={"path": str(pdf_path), "download": "1"},
            )

        self.assertEqual(inline.status_code, 200)
        self.assertEqual(attachment.status_code, 200)
        self.assertNotIn("attachment", (inline.headers.get("Content-Disposition") or "").lower())
        disposition = (attachment.headers.get("Content-Disposition") or "").lower()
        self.assertIn("attachment", disposition)
        self.assertIn("fixture.pdf", disposition)

    def test_desktop_document_returns_rating(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Rated Article"\n'
                "rating: 3\n"
                "---\n\n"
                "Body text.\n",
                encoding="utf-8",
            )

            response = self.client.get(
                "/desktop/document",
                query_string={"articlePath": str(article_path)},
            )

        self.assertEqual(response.status_code, 200)
        summary = response.get_json()["detail"]["summary"]
        self.assertEqual(summary["rating"], 3)

    def test_desktop_document_returns_frontmatter(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            article_path = Path(tmpdir) / "Article.md"
            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'authors: "A. Smith, B. Jones"\n'
                'doi: "10.1000/xyz"\n'
                "year: 2024\n"
                "rating: 4\n"
                "---\n\n"
                "Body text.\n",
                encoding="utf-8",
            )

            response = self.client.get(
                "/desktop/document",
                query_string={"articlePath": str(article_path)},
            )

        self.assertEqual(response.status_code, 200)
        detail = response.get_json()["detail"]
        frontmatter = detail["frontmatter"]
        self.assertEqual(frontmatter["title"], "Fixture Article")
        self.assertEqual(frontmatter["authors"], "A. Smith, B. Jones")
        self.assertEqual(frontmatter["doi"], "10.1000/xyz")
        self.assertEqual(frontmatter["year"], 2024)
        self.assertEqual(frontmatter["rating"], 4)

    def test_desktop_search_matches_metadata_fields(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "DESKTOP_API_ROOT", tmpdir):
            root = Path(tmpdir)
            (root / "neuro").mkdir()
            (root / "neuro" / "Tournier.md").write_text(
                "---\n"
                'title: "Constrained Spherical Deconvolution"\n'
                'label: "neuro"\n'
                'authors: "J-D Tournier, F Calamante"\n'
                'doi: "10.1016/j.neuroimage.2007.02.016"\n'
                "year: 2007\n"
                "---\n\nBody about CSD.\n",
                encoding="utf-8",
            )
            (root / "ml").mkdir()
            (root / "ml" / "Transformer.md").write_text(
                "---\n"
                'title: "Attention Is All You Need"\n'
                'label: "ml"\n'
                'authors: "Vaswani et al."\n'
                'arxiv_id: "1706.03762"\n'
                "year: 2017\n"
                "---\n\nBody about transformers.\n",
                encoding="utf-8",
            )

            def search(query: str) -> list[dict]:
                response = self.client.post(
                    "/desktop/search",
                    json={"root": str(root), "query": query},
                )
                self.assertEqual(response.status_code, 200)
                return response.get_json()["documents"]

            by_author = search("tournier")
            self.assertEqual(len(by_author), 1)
            self.assertEqual(by_author[0]["title"], "Constrained Spherical Deconvolution")

            by_doi = search("10.1016/j.neuroimage")
            self.assertEqual(len(by_doi), 1)
            self.assertEqual(by_doi[0]["title"], "Constrained Spherical Deconvolution")

            by_arxiv = search("1706.03762")
            self.assertEqual(len(by_arxiv), 1)
            self.assertEqual(by_arxiv[0]["title"], "Attention Is All You Need")

            by_year = search("2017")
            self.assertEqual(len(by_year), 1)
            self.assertEqual(by_year[0]["title"], "Attention Is All You Need")

    def test_desktop_search_pubmed_style_query(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "DESKTOP_API_ROOT", tmpdir):
            root = Path(tmpdir)
            (root / "neuro").mkdir()
            (root / "neuro" / "Tournier.md").write_text(
                "---\n"
                'title: "Constrained Spherical Deconvolution (CSD) on GPU"\n'
                'label: "neuro"\n'
                'authors: "J-D Tournier, F Calamante"\n'
                "year: 2007\n"
                "---\n\nBody text.\n",
                encoding="utf-8",
            )
            (root / "neuro" / "Other.md").write_text(
                "---\n"
                'title: "CSD for Beginners"\n'
                'label: "neuro"\n'
                'authors: "Someone Else"\n'
                "year: 2010\n"
                "---\n\nBody.\n",
                encoding="utf-8",
            )
            (root / "neuro" / "Tournier2.md").write_text(
                "---\n"
                'title: "Unrelated Work"\n'
                'label: "neuro"\n'
                'authors: "Tournier JD"\n'
                "year: 2015\n"
                "---\n\nBody.\n",
                encoding="utf-8",
            )

            def search(query: str) -> list[str]:
                response = self.client.post(
                    "/desktop/search",
                    json={"root": str(root), "query": query},
                )
                self.assertEqual(response.status_code, 200, response.get_json())
                return sorted(doc["title"] for doc in response.get_json()["documents"])

            self.assertEqual(
                search("((Tournier[Author]) AND (CSD[Title])) AND GPU"),
                ["Constrained Spherical Deconvolution (CSD) on GPU"],
            )
            self.assertEqual(
                search("Tournier[Author] AND CSD[Title]"),
                ["Constrained Spherical Deconvolution (CSD) on GPU"],
            )
            self.assertEqual(
                search("CSD[Title]"),
                ["CSD for Beginners", "Constrained Spherical Deconvolution (CSD) on GPU"],
            )
            self.assertEqual(
                search("Tournier[Author] NOT GPU"),
                ["Unrelated Work"],
            )
            self.assertEqual(
                search("2015[Year] OR 2010[Year]"),
                ["CSD for Beginners", "Unrelated Work"],
            )

    def test_desktop_search_invalid_query_returns_400(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "DESKTOP_API_ROOT", tmpdir):
            (Path(tmpdir) / "label").mkdir()
            response = self.client.post(
                "/desktop/search",
                json={"root": tmpdir, "query": "foo[Author"},
            )
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.get_json()["success"])

    def test_desktop_delete_document_removes_bundle_and_index_records(self):
        with tempfile.TemporaryDirectory() as tmpdir, patch.object(main, "OUTPUT_DIR", tmpdir):
            bundle_dir = Path(tmpdir) / "Label" / "Fixture"
            bundle_dir.mkdir(parents=True)
            article_path = bundle_dir / "Fixture.md"
            notes_path = bundle_dir / "Fixture.notes.md"
            bib_path = bundle_dir / "Fixture.bib"
            reading_pdf = bundle_dir / "Fixture.reading.pdf"
            highlights_path = bundle_dir / "Fixture.highlights.json"
            assets_dir = bundle_dir / "assets"
            assets_dir.mkdir()
            (assets_dir / "image.png").write_bytes(b"\x89PNG\r\n")

            article_path.write_text(
                "---\n"
                'title: "Fixture Article"\n'
                'label: "Label"\n'
                "---\n\nBody text.\n",
                encoding="utf-8",
            )
            notes_path.write_text("# Notes\n", encoding="utf-8")
            bib_path.write_text("@article{fixture,}\n", encoding="utf-8")
            reading_pdf.write_bytes(b"%PDF-1.4\n")
            highlights_path.write_text("{}\n", encoding="utf-8")

            index_path = Path(tmpdir) / "index.jsonl"
            index_path.write_text(
                "\n".join(
                    json.dumps(item, ensure_ascii=False)
                    for item in [
                        {"type": "article", "path": "Label/Fixture/Fixture.md", "title": "Fixture"},
                        {"type": "notes", "path": "Label/Fixture/Fixture.notes.md", "title": "Fixture Notes"},
                        {"type": "article", "path": "Other/Other.md", "title": "Other"},
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            response = self.client.post(
                "/desktop/document/delete",
                json={"articlePath": str(article_path)},
            )

            self.assertEqual(response.status_code, 200)
            payload = response.get_json()
            self.assertTrue(payload["success"])
            self.assertFalse(article_path.exists())
            self.assertFalse(bundle_dir.exists())
            surviving = index_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(surviving), 1)
            self.assertIn("Other/Other.md", surviving[0])

    def test_desktop_delete_document_returns_404_for_missing_path(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            response = self.client.post(
                "/desktop/document/delete",
                json={"articlePath": str(Path(tmpdir) / "Missing.md")},
            )
        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()

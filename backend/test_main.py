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


if __name__ == "__main__":
    unittest.main()

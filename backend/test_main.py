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


if __name__ == "__main__":
    unittest.main()

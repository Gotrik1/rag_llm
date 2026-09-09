import unittest

from document_catalog import (
    annotate_document_versions,
    build_document_catalog_answer,
    is_document_catalog_question,
)


class DocumentCatalogTests(unittest.TestCase):
    def test_inventory_intent_is_distinct_from_content_question(self):
        self.assertTrue(is_document_catalog_question("Какие регламенты у нас есть?"))
        self.assertTrue(is_document_catalog_question("С чем мы сейчас работаем и когда это загружено?"))
        self.assertTrue(is_document_catalog_question("Есть ли более актуальная версия документа?"))
        self.assertFalse(is_document_catalog_question("Что сказано в регламенте о сроке проверки?"))

    def test_semantic_versions_are_grouped_and_older_record_points_to_newer(self):
        documents = [
            {"id": "1", "file_name": "Регламент установки v1.4.docx", "created_at": "2026-09-01T10:00:00Z"},
            {"id": "2", "file_name": "Регламент установки v1.10.docx", "created_at": "2026-08-01T10:00:00Z"},
        ]

        annotated = annotate_document_versions(documents)

        self.assertEqual(annotated[0]["version_status"], "superseded")
        self.assertEqual(annotated[0]["newer_file_name"], "Регламент установки v1.10.docx")
        self.assertEqual(annotated[1]["version_status"], "latest")

    def test_upload_time_never_invents_document_version(self):
        result = build_document_catalog_answer("Проект", [{
            "id": "doc-1",
            "file_name": "Регламент.docx",
            "status": "ready",
            "created_at": "2026-09-09T08:15:00Z",
        }])

        self.assertIn("загружен 09.09.2026 08:15 UTC", result["answer"])
        self.assertIn("актуальность автоматически не подтверждена", result["answer"])
        self.assertIn("Дата загрузки не является датой редакции", result["answer"])
        self.assertEqual(result["sources"][0]["file"], "Регламент.docx")
        self.assertEqual(result["sources"][0]["url"], "/api/documents/doc-1/content")

    def test_bare_version_from_metadata_is_comparable(self):
        annotated = annotate_document_versions([
            {"id": "1", "file_name": "Регламент.docx", "metadata": {"version": "1.4"}},
            {"id": "2", "file_name": "Регламент.docx", "metadata": {"version": "1.5"}},
        ])

        self.assertEqual(annotated[0]["version_status"], "superseded")
        self.assertEqual(annotated[1]["version_label"], "1.5")

    def test_document_name_cannot_break_generated_markdown_link(self):
        result = build_document_catalog_answer("Проект", [{
            "id": "doc-1",
            "file_name": "Регламент ](https://example.test).pdf",
            "status": "ready",
        }])

        self.assertIn(r"[Регламент \](https://example.test).pdf](/api/documents/doc-1/content)", result["answer"])


if __name__ == "__main__":
    unittest.main()

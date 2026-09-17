import json
import unittest
import uuid
from datetime import datetime, timezone

from document_catalog import (
    annotate_document_versions,
    build_document_catalog_answer,
    document_version_fields,
    is_document_catalog_question,
)


class DocumentCatalogTests(unittest.TestCase):
    def test_persistable_version_fields_are_stable_and_normalized(self):
        fields = document_version_fields({
            "file_name": "Методика v1.4.docx",
            "created_at": "2099-01-01T00:00:00Z",
            "metadata": {"effective_date": "2026-09-01"},
        })

        self.assertEqual(fields["document_family"], "методика")
        self.assertEqual(fields["version_label"], "1.4")
        self.assertEqual(fields["version_sort_key"], [1, 4, 0, 0])
        self.assertEqual(fields["effective_on"], "2026-09-01")

    def test_trailing_russian_version_abbreviation_is_comparable(self):
        dotted = document_version_fields({"file_name": "Методика на передачу в.1.docx"})
        spaced = document_version_fields({"file_name": "Методика расчеты на РРЭ в 1.docx"})

        self.assertEqual(dotted["document_family"], "методика на передачу")
        self.assertEqual(dotted["version_label"], "1")
        self.assertEqual(dotted["version_sort_key"], [1, 0, 0, 0])
        self.assertEqual(spaced["document_family"], "методика расчеты на ррэ")
        self.assertEqual(spaced["version_label"], "1")

    def test_russian_preposition_inside_name_is_not_a_version(self):
        fields = document_version_fields({"file_name": "Инструкция по работе в модуле 1.docx"})

        self.assertIsNone(fields["version_label"])

    def test_explicit_family_groups_differently_named_revisions(self):
        annotated = annotate_document_versions([
            {
                "id": "1",
                "file_name": "Методика старая.docx",
                "metadata": {"document_family": "Методика РРЭ", "version": "1.0"},
            },
            {
                "id": "2",
                "file_name": "Инструкция новая.docx",
                "metadata": {"document_family": "Методика РРЭ", "version": "2.0"},
            },
        ])

        self.assertEqual(annotated[0]["version_status"], "superseded")
        self.assertEqual(annotated[0]["newer_document_id"], "2")

    def test_versions_do_not_supersede_documents_in_another_scope(self):
        annotated = annotate_document_versions([
            {
                "id": "global-v1",
                "file_name": "Методика v1.docx",
                "owner_subject": None,
                "project_id": None,
            },
            {
                "id": "project-v2",
                "file_name": "Методика v2.docx",
                "owner_subject": "user-1",
                "project_id": "project-1",
            },
        ])

        self.assertEqual(annotated[0]["version_status"], "latest")
        self.assertEqual(annotated[1]["version_status"], "latest")

    def test_catalog_result_is_safe_for_durable_queue_serialization(self):
        document_id = uuid.uuid4()
        result = build_document_catalog_answer(
            "Проект",
            [{
                "id": document_id,
                "file_name": "Регламент.docx",
                "created_at": datetime(2026, 9, 9, 9, 0, tzinfo=timezone.utc),
                "metadata": {"reviewed_at": datetime(2026, 9, 9, 10, 0, tzinfo=timezone.utc)},
            }],
        )

        encoded = json.dumps(result, ensure_ascii=False)

        self.assertIn(str(document_id), encoded)
        self.assertIn("2026-09-09T09:00:00+00:00", encoded)

    def test_inventory_intent_is_distinct_from_content_question(self):
        self.assertTrue(is_document_catalog_question("Какие регламенты у нас есть?"))
        self.assertTrue(is_document_catalog_question("С чем мы сейчас работаем и когда это загружено?"))
        self.assertTrue(is_document_catalog_question("Есть ли более актуальная версия документа?"))
        self.assertFalse(is_document_catalog_question("Что сказано в регламенте о сроке проверки?"))
        self.assertFalse(is_document_catalog_question(
            "Какие типы ценовых заявок перечислены в Регламенте 5 и кто вправе их подавать?"
        ))

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

    def test_short_document_code_is_not_invented_as_a_revision(self):
        annotated = annotate_document_versions([
            {"id": "1", "file_name": "r5.docx"},
            {"id": "2", "file_name": "r7.docx"},
        ])

        self.assertIsNone(annotated[0]["version_label"])
        self.assertEqual(annotated[0]["version_status"], "unknown")
        self.assertIsNone(annotated[1]["version_label"])

    def test_document_name_cannot_break_generated_markdown_link(self):
        result = build_document_catalog_answer("Проект", [{
            "id": "doc-1",
            "file_name": "Регламент ](https://example.test).pdf",
            "status": "ready",
        }])

        self.assertIn(r"[Регламент \](https://example.test).pdf](/api/documents/doc-1/content)", result["answer"])


if __name__ == "__main__":
    unittest.main()

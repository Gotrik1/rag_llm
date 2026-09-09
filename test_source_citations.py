import unittest
from unittest.mock import patch

from rag_agent import human_source_reference, source_locator, user_facing_source_name
from web_ui import enrich_answer_source_citations, enrich_source_links, requested_response_formulas, select_user_facing_sources


class SourceCitationTests(unittest.TestCase):
    def test_storage_uuid_is_never_part_of_user_facing_document_name(self):
        stored = "4f424eaf3d7149b2a8d7d668e1e49836_0123456789abcdef0123456789abcdef_manual.docx"
        self.assertEqual(user_facing_source_name(stored), "manual.docx")

    def test_reference_is_document_plus_human_section_locator(self):
        reference = human_source_reference({"file": "abc123_manual.docx", "section_path": "Глава 2 > Порядок расчёта"})
        self.assertEqual(reference, "abc123_manual.docx — Глава 2 > Порядок расчёта")
        self.assertEqual(source_locator("Пункт 4.2", 17), "стр. 17, Пункт 4.2")

    @patch("web_ui.document_sources")
    def test_sources_are_restored_linked_and_deduplicated_by_document_locator(self, records):
        records.return_value = {
            "4f424eaf3d7149b2a8d7d668e1e49836_manual.docx": {
                "id": "doc-1", "file_name": "Руководство оператора.docx"
            }
        }
        sources = enrich_source_links([
            {"file": "4f424eaf3d7149b2a8d7d668e1e49836_manual.docx", "section": "Запуск", "score": 0.7},
            {"file": "4f424eaf3d7149b2a8d7d668e1e49836_manual.docx", "section": "Запуск", "score": 0.9},
        ])

        self.assertEqual(len(sources), 1)
        self.assertEqual(sources[0]["file"], "Руководство оператора.docx")
        self.assertEqual(sources[0]["locator"], "раздел «Запуск»")
        self.assertEqual(sources[0]["url"], "/api/documents/doc-1/content")
        self.assertEqual(set(sources[0]), {"file", "section", "locator", "url"})

    def test_answer_citation_becomes_clickable_document_and_locator(self):
        answer = "Кнопка запускает расчёт. [Источник: Руководство оператора.docx — Запуск]"
        enriched = enrich_answer_source_citations(answer, [{
            "file": "Руководство оператора.docx",
            "section": "Запуск",
            "locator": "раздел «Запуск»",
            "url": "/api/documents/doc-1/content",
            "score": 0.9,
        }])

        self.assertIn(
            "[Источник: Руководство оператора.docx — раздел «Запуск»](/api/documents/doc-1/content)",
            enriched,
        )
        self.assertNotIn("score=", enriched)

    def test_formula_paragraph_uses_formula_locator_even_if_model_repeats_fact_locator(self):
        answer = (
            "Код NEBULA-742. [Источник: manual.pdf — Unique fact]\n\n"
            "Формула: $$I = \\frac{U}{R}$$. [Источник: manual.pdf — Unique fact]"
        )
        sources = [
            {"file": "manual.pdf", "section": "Unique fact", "locator": "раздел «Unique fact»", "url": "/api/documents/doc-1/content"},
            {"file": "manual.pdf", "section": "Formula", "locator": "раздел «Formula»", "url": "/api/documents/doc-1/content"},
        ]

        enriched = enrich_answer_source_citations(answer, sources)

        self.assertIn("Источник: manual.pdf — раздел «Unique fact»", enriched)
        self.assertIn("Источник: manual.pdf — раздел «Formula»", enriched)

    def test_repeated_identical_source_lines_are_collapsed(self):
        answer = (
            "Факт.\n"
            "[Источник: manual.pdf — Запуск]\n"
            "[Источник: manual.pdf — Запуск]"
        )
        source = {
            "file": "manual.pdf", "section": "Запуск", "locator": "раздел «Запуск»",
            "url": "/api/documents/doc-1/content",
        }

        enriched = enrich_answer_source_citations(answer, [source])

        self.assertEqual(enriched.count("Источник: manual.pdf"), 1)

    def test_non_formula_question_has_no_formula_payload(self):
        chunks = [{"text": "Константы используются в формулах через функцию Const."}]

        self.assertEqual(requested_response_formulas(chunks, "Какие регламенты применяются?"), [])
        self.assertEqual(
            requested_response_formulas([{"text": "$$I = U / R$$"}], "Покажи формулу тока"),
            ["I = U / R"],
        )

    def test_user_sources_include_only_cited_linked_document_locators(self):
        sources = [
            {"file": "manual.pdf", "section": "Запуск", "locator": "раздел «Запуск»", "url": "/api/documents/doc-1/content"},
            {"file": "manual.pdf", "section": "Удаление", "locator": "раздел «Удаление»", "url": "/api/documents/doc-1/content"},
            {"file": "legacy.pdf", "section": "Шум", "locator": "раздел «Шум»"},
        ]
        answer = "Факт. [Источник: manual.pdf — раздел «Запуск»](/api/documents/doc-1/content)"

        self.assertEqual(select_user_facing_sources(answer, sources), [sources[0]])


if __name__ == "__main__":
    unittest.main()

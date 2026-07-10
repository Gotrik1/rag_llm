import unittest

from rag_agent import (
    generate_formula_only_answer,
    refine_prism_query,
    rerank_context_results,
    select_prism_evidence,
    validate_retrieval,
)


class PrismQueryTests(unittest.TestCase):
    def test_formula_request_refines_to_exact_section_mode(self):
        spec = refine_prism_query("дай формулы из пункта 2.1.4")

        self.assertEqual(spec.intent, "formula_extraction")
        self.assertEqual(spec.target_sections, ("2.1.4",))
        self.assertEqual(spec.answer_mode, "formulas_only")

    def test_explicit_section_rejects_other_sections_and_deleted_evidence(self):
        spec = refine_prism_query("дай формулы из пункта 2.1.4")
        evidence = [
            {"section_path": "2.1.4 Расчет", "status": "active", "chunk_type": "unknown", "text": "нужное"},
            {"section_path": "2.1.5 Расчет", "status": "active", "chunk_type": "unknown", "text": "чужое"},
            {"section_path": "2.1.4 Утратил силу", "status": "deleted", "chunk_type": "history", "text": "старое"},
        ]

        selected = select_prism_evidence(evidence, spec, 10)

        self.assertEqual([item["text"] for item in selected], ["нужное"])

    def test_formula_mode_does_not_add_explanatory_prose(self):
        spec = refine_prism_query("дай формулы из пункта 2.1.4")
        answer = generate_formula_only_answer(
            "дай формулы из пункта 2.1.4",
            [],
            ["ИВ1: величина определяется как разность объемов."],
            spec,
        )

        self.assertIn("ИВ1: величина определяется как разность объемов.", answer)
        self.assertNotIn("Краткий вывод", answer)

    def test_validator_reports_missing_requested_section(self):
        spec = refine_prism_query("дай формулы из пункта 2.1.4")
        warnings = validate_retrieval("дай формулы из пункта 2.1.4", [], spec=spec)

        self.assertTrue(any("2.1.4" in warning for warning in warnings))

    def test_parameter_creation_beats_document_and_macro_sections(self):
        results = [
            {"file": "manual.docx", "score": 0.03, "section_path": "Документы > Создание документа", "section_title": "Создание документа", "text": "Для создания документа..."},
            {"file": "manual.docx", "score": 0.025, "section_path": "Макросы > Создание макроса", "section_title": "Создание макроса", "text": "Параметр макроса..."},
            {"file": "manual.docx", "score": 0.02, "section_path": "Работа с параметрами > Создание параметра", "section_title": "Создание параметра", "text": "Для создания параметра необходимо выбрать группу..."},
        ]

        ranked = rerank_context_results(results, "как добавить параметр?")

        self.assertEqual(ranked[0]["section_title"], "Создание параметра")


if __name__ == "__main__":
    unittest.main()

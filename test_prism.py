import unittest
from pathlib import Path

from rag_agent import (
    RAGAgent,
    build_section_documents,
    extract_formula_lines_from_texts,
    formula_sibling_results,
    format_formula_for_display,
    format_formula_for_katex,
    generate_formula_only_answer,
    refine_prism_query,
    rerank_context_results,
    sanitize_grounded_answer,
    select_prism_evidence,
    validate_retrieval,
)


class PrismQueryTests(unittest.TestCase):
    def test_lexical_section_seed_finds_long_normative_heading(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        agent = object.__new__(RAGAgent)
        target = "Порядок признания Коммерческим оператором процедуры конкурентного отбора несостоявшейся и порядок определения планового почасового производства и потребления в этом случае"
        agent._all_nodes = [
            Node("Сведения общего характера.", {"file_name": "noise.docx", "section_path": "Общие положения"}),
            Node(
                "При наступлении событий из п. 3.2 КО объявляет процедуру несостоявшейся.",
                {"file_name": "r7.docx", "section_path": target, "section_title": target},
            ),
        ]

        seeds = agent._lexical_section_seed_results(
            "В каких случаях процедура конкурентного отбора признаётся несостоявшейся и как определяется плановое почасовое производство и потребление?"
        )

        self.assertTrue(seeds)
        self.assertEqual(seeds[0]["file"], "r7.docx")
        self.assertIn("section-lexical", seeds[0]["matched_queries"][0])

    def test_formula_heading_is_linked_to_the_governing_clause(self):
        markdown = """#### 4.4.2.6. Ставка по собственной инициативе в сторону увеличения определяется как минимальная величина:\n\n#### = MIN(Ц_i,q^-, Т_i,q).\n"""

        docs = build_section_documents(markdown, Path("r12_rag.md"), "xml")

        self.assertEqual(len(docs), 1)
        self.assertIn("[FORMULA]", docs[0].text)
        self.assertIn("= MIN(Ц_i,q^-, Т_i,q)", docs[0].text)
        formulas = extract_formula_lines_from_texts([docs[0].text], "По какой формуле определяется ставка по собственной инициативе?")
        self.assertEqual(formulas, ["= MIN(Ц_i,q^-, Т_i,q)."])

    def test_removes_hallucinated_regulation_abbreviation_expansions(self):
        answer = sanitize_grounded_answer(
            "ГТП (Группа Технических Условий) и КО (Контролирующий Орган).",
            "Что такое ГТП?",
            [],
        )

        self.assertEqual(answer, "ГТП и КО.")

    def test_legacy_formula_is_safe_for_markdown_display(self):
        displayed = format_formula_for_display(
            r"= МIN ($\mathrm{Ц}_{\mathrm{i,q}}^{\mathrm{-}}$, $\mathrm{Т}_{\mathrm{i,q}}^{\mathrm{э/э\ пост\ \_мощн\.\ вын\.\ реж\.}}$)."
        )

        self.assertEqual(displayed, "= MIN (Ц_{i,q}^{-}, Т_{i,q}^{э/э пост _мощн. вын. реж.}).")
        self.assertNotIn("\\mathrm", displayed)

    def test_legacy_formula_is_converted_to_valid_katex(self):
        katex = format_formula_for_katex(
            r"= МIN ($\mathrm{Ц}_{\mathrm{i,q}}^{\mathrm{-}}$,$\mathrm{Т}_{\mathrm{i,q}}^{\mathrm{э/э\ пост\ \_мощн\.\ вын\.\ реж\.}}$)."
        )

        self.assertEqual(katex, r"$= \min (\text{Ц}_{\text{i,q}}^{\text{-}},\text{Т}_{\text{i,q}}^{\text{э/э пост _мощн. вын. реж.}})$")

    def test_formula_request_refines_to_exact_section_mode(self):
        spec = refine_prism_query("дай формулы из пункта 2.1.4")

        self.assertEqual(spec.intent, "formula_extraction")
        self.assertEqual(spec.target_sections, ("2.1.4",))
        self.assertEqual(spec.answer_mode, "formulas_only")

    def test_compound_fact_and_formula_request_keeps_grounded_answer_mode(self):
        spec = refine_prism_query(
            "Какой внутренний код у Project Borealis? Приведи формулу тока и рассчитай ток."
        )

        self.assertEqual(spec.answer_mode, "grounded_answer")

    def test_short_markdown_equation_is_kept(self):
        formulas = extract_formula_lines_from_texts(
            ["For the example:\n\n$$\nI = \\frac{U}{R}\n$$"],
            "Приведи формулу тока",
        )

        self.assertEqual(formulas, [r"I = \frac{U}{R}"])

    def test_formula_sibling_from_retrieved_source_is_added(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        merged = {
            "fact": {"file": "acceptance.md", "score": 0.31, "text": "NEBULA-742"},
            "noise": {"file": "other.md", "score": 0.05, "text": "irrelevant"},
        }
        nodes = [
            Node("$$\nI = \\frac{U}{R}\n$$", {"file_name": "4f424eaf3d7149b2a8d7d668e1e49836_acceptance.md", "section_title": "Formula"}),
            Node("$$\nX = Y\n$$", {"file_name": "unseen.md", "section_title": "Formula"}),
        ]

        siblings = formula_sibling_results(nodes, merged, "Приведи формулу тока")

        self.assertEqual(len(siblings), 1)
        self.assertTrue(siblings[0]["file"].endswith("_acceptance.md"))
        self.assertIn("formula-sibling", siblings[0]["matched_queries"])

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

    def test_ivo_worded_difference_is_rendered_as_explicit_formula(self):
        question = "Что такое ИВ0? Как рассчитывается? А по какой формуле?"
        rule = (
            "ИВ0: величина равна разности между объемом электроэнергии, "
            "определенным согласно УДГ, и объемом электроэнергии, "
            "определенном в плановом ДГ."
        )
        answer = generate_formula_only_answer(
            question,
            [{"text": rule, "section_path": "Внешняя инициатива ИВ0", "matched_queries": ["formula-clause-exact"]}],
            [rule],
            refine_prism_query(question),
        )

        self.assertIn("ИВ0 = объём электроэнергии по УДГ − объём электроэнергии по плановому ДГ", answer)
        self.assertIn(rule, answer)
        self.assertIn("[Источник: документ — раздел «Внешняя инициатива ИВ0»]", answer)

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

import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from rag_agent import (
    QueryUnderstanding,
    RAGAgent,
    apply_document_version_preference,
    build_section_documents,
    build_targeted_queries,
    contains_price_category,
    detect_query_intent,
    deterministic_query_understanding,
    document_occurrence_item_matches,
    diversify_context_results,
    extract_chunk_metadata,
    extract_formula_lines_from_texts,
    ensure_price_category_source_coverage,
    ensure_requested_formula_coverage,
    formula_sibling_results,
    format_formula_for_display,
    format_formula_for_katex,
    generate_document_occurrence_answer,
    generate_formula_only_answer,
    generate_extractive_value_answer,
    generate_image_transcription_answer,
    is_document_occurrence_question,
    is_price_composition_question,
    matching_explicit_sources,
    normalize_user_question,
    _question_hash,
    parse_query_understanding,
    price_category_number,
    prioritize_scoped_results,
    relationship_expansion_results,
    refine_prism_query,
    rerank_context_results,
    sanitize_grounded_answer,
    select_prism_evidence,
    validate_retrieval,
)


class PrismQueryTests(unittest.TestCase):
    def test_latest_document_revision_gets_soft_preference(self):
        ranked = [
            {"file": "old_method_v1.docx", "score": 1.04, "text": "old"},
            {"file": "new_method_v2.docx", "score": 1.0, "text": "new"},
        ]

        preferred = apply_document_version_preference(
            ranked,
            {
                "old_method_v1.docx": "superseded",
                "new_method_v2.docx": "latest",
            },
        )

        self.assertEqual(preferred[0]["file"], "new_method_v2.docx")
        self.assertEqual(preferred[0]["version_status"], "latest")

    def test_explicit_historical_document_is_not_penalized(self):
        ranked = [
            {"file": "old_method_v1.docx", "score": 1.04, "text": "old"},
            {"file": "new_method_v2.docx", "score": 1.0, "text": "new"},
        ]

        preferred = apply_document_version_preference(
            ranked,
            {
                "old_method_v1.docx": "superseded",
                "new_method_v2.docx": "latest",
            },
            explicit_sources={"old_method_v1.docx"},
        )

        self.assertEqual(preferred[0]["file"], "old_method_v1.docx")
        self.assertNotIn("version_adjustment", preferred[0])

    def test_active_exception_is_not_misclassified_as_deleted(self):
        metadata = extract_chunk_metadata(
            "[SECTION: Исключения]\nРасчет цены не применяется в особом случае."
        )
        self.assertEqual(metadata["status"], "active")

    def test_explicitly_removed_clause_is_deleted(self):
        metadata = extract_chunk_metadata(
            "[SECTION: Пункт 10]\nПункт исключен из регламента."
        )
        self.assertEqual(metadata["status"], "deleted")

    def test_repealed_sibling_does_not_delete_active_normative_clause(self):
        metadata = extract_chunk_metadata(
            "[SECTION: 2. Особенности применения нерегулируемых цен]\n"
            "12. Пункт утратил силу.\n\n"
            "2. Предельные уровни дифференцируются по ценовым категориям."
        )
        self.assertEqual(metadata["status"], "active")
        self.assertNotEqual(metadata["chunk_type"], "history")

    def test_docstore_load_repairs_stale_deleted_status(self):
        text = (
            "[SECTION: 2. Особенности применения нерегулируемых цен]\n"
            "12. Пункт утратил силу.\n\n"
            "2. Предельные уровни дифференцируются по ценовым категориям."
        )
        agent = object.__new__(RAGAgent)
        agent.docstore = SimpleNamespace(docs={
            "node": SimpleNamespace(
                text=text,
                metadata={
                    "file_name": "Постановление 1179.docx",
                    "section_title": "2. Особенности применения нерегулируемых цен",
                    "status": "deleted",
                    "chunk_type": "history",
                },
                doc_id="node",
            ),
        })

        RAGAgent._load_nodes_from_docstore(agent)

        self.assertEqual(agent._all_nodes[0].metadata["status"], "active")
        self.assertNotEqual(agent._all_nodes[0].metadata["chunk_type"], "history")

    def test_relationship_expansion_adds_same_section_child_and_exception_sibling(self):
        nodes = [
            SimpleNamespace(
                text="Основное правило расчета цены.",
                metadata={"file_name": "method.docx", "section_path": "5 > Расчет", "parent_section": "5"},
            ),
            SimpleNamespace(
                text="Формула: S = V * C.",
                metadata={"file_name": "method.docx", "section_path": "5 > Расчет", "parent_section": "5"},
            ),
            SimpleNamespace(
                text="Исключение: расчет цены не применяется в особом случае.",
                metadata={"file_name": "method.docx", "section_path": "5 > Исключения", "parent_section": "5"},
            ),
            SimpleNamespace(
                text="Чужой проект.",
                metadata={"file_name": "private.docx", "section_path": "5 > Расчет", "parent_section": "5"},
            ),
        ]
        ranked = [{
            "file": "method.docx",
            "text": "Основное правило расчета цены.",
            "score": 2.0,
            "section_path": "5 > Расчет",
            "parent_section": "5",
        }]

        expanded = relationship_expansion_results(nodes, ranked, "Как рассчитывается цена?", limit=4)

        self.assertEqual({item["file"] for item in expanded}, {"method.docx"})
        self.assertTrue(any("S = V" in item["text"] for item in expanded))
        self.assertTrue(any("Исключение" in item["text"] for item in expanded))
        self.assertTrue(all(item["matched_queries"][0].startswith("relationship:") for item in expanded))

    def test_relationship_expansion_is_disabled_for_document_occurrence(self):
        ranked = [{"file": "method.docx", "text": "1 ЦК", "score": 1.0}]
        self.assertEqual(
            relationship_expansion_results([], ranked, "В каких документах есть 1 ЦК?"),
            [],
        )

    def test_exact_entity_seed_keeps_one_rare_entity_hit_per_source(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                text="ПУНЦЭ применяется при определении предельного уровня цены.",
                metadata={"file_name": "1179.docx", "section_path": "Пункт 1"},
            ),
            SimpleNamespace(
                text="ПУНЦЭ рассчитывается по формуле.",
                metadata={"file_name": "Методика.docx", "section_path": "Расчет ПУНЦЭ"},
            ),
            SimpleNamespace(
                text="Посторонний текст.",
                metadata={"file_name": "noise.docx", "section_path": "Общее"},
            ),
        ]

        seeds = agent._exact_entity_seed_results("Что такое ПУНЦЭ?")

        self.assertEqual({item["file"] for item in seeds}, {"1179.docx", "Методика.docx"})
        self.assertTrue(all(item["matched_queries"] == ["entity-exact:ПУНЦЭ"] for item in seeds))

    def test_generic_price_category_seed_scans_all_documents(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                text="Устанавливаются шесть ценовых категорий.",
                metadata={"file_name": "1179.docx", "section_path": "Ценовые категории"},
            ),
            SimpleNamespace(
                text="Параметр ценовая категория выбирается в карточке.",
                metadata={"file_name": "Методика.docx", "section_path": "Настройка"},
            ),
        ]

        seeds = agent._price_category_seed_results("Какие бывают ценовые категории?")

        self.assertEqual({item["file"] for item in seeds}, {"1179.docx", "Методика.docx"})

    def test_explicit_regulation_reference_resolves_visible_source_alias(self):
        aliases = {
            "upload-a_2012-05-04-пп442-функционирование-ррэ.md":
                "2012-05-04-пп442-функционирование-ррэ.md",
            "upload-b_методика.docx": "Методика расчета стоимости.docx",
        }

        matched = matching_explicit_sources(
            "Как определяется гарантирующий поставщик по ПП 442?",
            aliases,
        )

        self.assertEqual(matched, {"upload-a_2012-05-04-пп442-функционирование-ррэ.md"})

    def test_generic_business_phrase_does_not_become_explicit_source_filter(self):
        aliases = {
            "upload-a_r12.docx": "r12.docx",
            "upload-b_method.docx": "Методика расчета стоимости 1-6ЦК v2.docx",
        }

        matched = matching_explicit_sources(
            "Чем ИВ0 отличается от расчёта стоимости?",
            aliases,
        )

        self.assertEqual(matched, set())

    def test_explicit_method_reference_still_resolves_visible_source_alias(self):
        aliases = {
            "upload-b_method.docx": "Методика расчета стоимости 1-6ЦК v2.docx",
        }

        matched = matching_explicit_sources(
            "Что сказано в методике расчета стоимости 1-6ЦК?",
            aliases,
        )

        self.assertEqual(matched, {"upload-b_method.docx"})

    def test_private_scope_lexical_seed_considers_project_document(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                metadata={"file_name": "project_pp442.md"},
                text="Гарантирующий поставщик определяется по правилам розничного рынка.",
            ),
            SimpleNamespace(
                metadata={"file_name": "global_r12.docx"},
                text="Определяется ставка для балансирующего рынка.",
            ),
        ]

        results = agent._scoped_lexical_seed_results(
            "Как определяется гарантирующий поставщик?",
            {"project_pp442.md"},
        )

        self.assertTrue(results)
        self.assertTrue(all(item["file"] == "project_pp442.md" for item in results))

    def test_domain_lexical_seed_recovers_small_rre_manual(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                metadata={
                    "file_name": "upload-rre.docx",
                    "section_path": "Договорные условия на РРЭ",
                },
                text=(
                    "В договоре должен быть создан объект потребления с тарифной "
                    "схемой и признаком текущий расчет."
                ),
            ),
            SimpleNamespace(
                metadata={
                    "file_name": "large-orem.docx",
                    "section_path": "Оптовый рынок",
                },
                text="Участник подает ценовую заявку для проведения расчета.",
            ),
        ]

        results = agent._domain_lexical_seed_results(
            "Какие договорные условия нужны для расчета РРЭ?",
            ("rre",),
            {
                "upload-rre.docx": ("rre",),
                "large-orem.docx": ("orem",),
            },
        )

        self.assertTrue(results)
        self.assertEqual({item["file"] for item in results}, {"upload-rre.docx"})
        self.assertTrue(all(item["matched_queries"] == ["domain-lexical"] for item in results))

    def test_domain_lexical_seed_reserves_one_candidate_per_document(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            *[
                SimpleNamespace(
                    metadata={"file_name": "large-rre.docx", "section_path": "Договор"},
                    text=f"Договорные условия и тарифная схема, подробность {index}.",
                )
                for index in range(30)
            ],
            SimpleNamespace(
                metadata={"file_name": "small-rre.docx", "section_path": "Условия расчета"},
                text="Для договора задаются условия и тарифная схема.",
            ),
        ]

        results = agent._domain_lexical_seed_results(
            "Какие договорные условия и тарифная схема нужны для РРЭ?",
            ("rre",),
            {"large-rre.docx": ("rre",), "small-rre.docx": ("rre",)},
            limit=4,
        )

        self.assertIn("small-rre.docx", {item["file"] for item in results})

    def test_domain_lexical_seed_prefers_expanded_governing_clause(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                metadata={"file_name": "rre.docx", "section_path": "Введение"},
                text="Методика расчета стоимости по договору РРЭ.",
            ),
            SimpleNamespace(
                metadata={"file_name": "rre.docx", "section_path": "4.1.3 Договорные величины"},
                text=(
                    "В договоре должен быть создан объект потребления или ЭПУ "
                    "с тарифной схемой и признаком текущий расчет."
                ),
            ),
        ]

        results = agent._domain_lexical_seed_results(
            "Что настроить перед расчетом договора РРЭ? объект потребления ЭПУ "
            "тарифная схема признак текущий расчет",
            ("rre",),
            {"rre.docx": ("rre",)},
        )

        self.assertEqual(results[0]["section_path"], "4.1.3 Договорные величины")

    def test_conditions_diversification_reserves_direct_hits_from_multiple_documents(self):
        ranked = [
            *[
                {
                    "file": "large.docx",
                    "score": 20.0 - index,
                    "section_path": f"Раздел {index}",
                    "text": "Общие сведения о расчете.",
                    "matched_queries": [],
                }
                for index in range(10)
            ],
            {
                "file": "rre-method.docx",
                "score": 2.0,
                "section_path": "4.1.3 Договорные величины",
                "text": "Объект потребления и тарифная схема должны быть настроены.",
                "matched_queries": ["domain-lexical"],
            },
            {
                "file": "regulation.docx",
                "score": 1.8,
                "section_path": "Условия договора",
                "text": "Установлены обязательные условия договора.",
                "matched_queries": ["domain-lexical"],
            },
        ]

        selected = diversify_context_results(
            ranked,
            "Что необходимо настроить перед запуском расчёта договора РРЭ?",
            10,
        )

        self.assertIn("rre-method.docx", {item["file"] for item in selected})
        self.assertIn("regulation.docx", {item["file"] for item in selected})

    def test_relevant_project_evidence_gets_bounded_top_k_quota(self):
        shared = [
            {"file": f"shared-{index}.docx", "score": 10 - index, "matched_queries": []}
            for index in range(6)
        ]
        project = [
            {
                "file": f"project-{index}.md",
                "score": 1 - index / 10,
                "matched_queries": ["private-scope-lexical"],
            }
            for index in range(4)
        ]

        prioritized = prioritize_scoped_results([*shared, *project], top_k=10)

        self.assertEqual(
            [item["file"] for item in prioritized[:3]],
            ["project-0.md", "project-1.md", "project-2.md"],
        )
        self.assertIn("shared-0.docx", [item["file"] for item in prioritized[:4]])

    def test_plain_legal_markdown_is_split_into_point_locators(self):
        documents = build_section_documents(
            "ПОСТАНОВЛЕНИЕ N 442\n\n"
            "1. Утвердить основные положения розничного рынка.\n"
            "Продолжение первого пункта.\n"
            "2. Установить порядок работы гарантирующего поставщика.\n"
            "159.1. Специальное условие применяется отдельно.",
            Path("pp442.md"),
            "text",
        )

        locators = [document.metadata.get("section_path") for document in documents]
        self.assertEqual(locators, ["Преамбула", "Пункт 1", "Пункт 2", "Пункт 159.1"])
        self.assertIn("гарантирующего поставщика", documents[2].text)

    def test_repeated_generic_word_cannot_dominate_reranking(self):
        results = rerank_context_results(
            [
                {
                    "file": "unrelated.docx",
                    "score": 0.02,
                    "text": "определяется " * 2000,
                    "section_path": "",
                    "section_title": "",
                },
                {
                    "file": "pp442.md",
                    "score": 0.01,
                    "text": "Гарантирующий поставщик определяется в соответствии с ПП 442.",
                    "section_path": "",
                    "section_title": "",
                },
            ],
            "Как определяется гарантирующий поставщик по ПП 442?",
        )

        self.assertEqual(results[0]["file"], "pp442.md")

    def test_misspelled_price_category_question_is_understood_as_cost(self):
        question = "что такое 6ЦК и ак она рассчитывается"

        understanding = deterministic_query_understanding(question)

        self.assertEqual(understanding.canonical_question, "что такое 6 ЦК и как она рассчитывается")
        self.assertEqual(detect_query_intent(question), "cost")
        self.assertIn("definition", understanding.facets)
        self.assertIn("calculation", understanding.facets)
        spec = refine_prism_query(understanding.canonical_question)
        self.assertNotIn("cost", spec.excluded_chunk_types)
        self.assertNotIn("rate", spec.excluded_chunk_types)

    def test_standalone_ivo_is_normalized_to_iv_zero(self):
        understanding = deterministic_query_understanding("формула иво какая")

        self.assertEqual(understanding.canonical_question, "формула ИВ0 какая")
        self.assertIn("calculation", understanding.facets)
        self.assertIn("формула ИВ0 какая", understanding.retrieval_queries)
        self.assertEqual(
            normalize_user_question("ИВОН и иво"),
            ("ИВОН и ИВ0", ("ИВОН и иво -> ИВОН и ИВ0",)),
        )

    def test_common_typo_question_is_normalized_before_calculation_retrieval(self):
        understanding = deterministic_query_understanding(
            "Как в сиситеме щитаются авнсы по РРЭ?"
        )

        self.assertEqual(
            understanding.canonical_question,
            "Как в системе рассчитываются авансы по РРЭ?",
        )
        self.assertIn("calculation", understanding.facets)

    def test_price_category_plan_searches_each_requested_facet(self):
        queries = build_targeted_queries("что такое 6ЦК и ак она рассчитывается")

        self.assertTrue(any("определение" in query for query in queries))
        self.assertTrue(any("расчет стоимости" in query for query in queries))
        self.assertTrue(any("шестая ценовая категория" in query for query in queries))

    def test_document_occurrence_question_is_positive_list_intent(self):
        understanding = deterministic_query_understanding("В каких документах фигурирует 1ЦК?")

        self.assertIn("list", understanding.facets)
        self.assertEqual(price_category_number(understanding.canonical_question), "1")
        self.assertTrue(contains_price_category("Порядок расчета 1 ЦК", "1"))

    def test_rre_prerequisite_paraphrases_get_conditions_and_operational_queries(self):
        questions = (
            "Для расчётов в системе по договорам РРЭ какие условия должны быть выполнены?",
            "Что необходимо настроить перед запуском расчёта договора РРЭ?",
            "Перечисли предварительные условия расчёта по договору розничного рынка.",
            "что должно быть заведено чтобы посчитать договор ррэ",
            "Какие объекты, условия и тарифы требуются для расчёта РРЭ?",
            "Почему расчёт по договору РРЭ может не сформироваться?",
            "Дай чек-лист готовности договора РРЭ к расчёту.",
            "Какие данные система проверяет перед расчётом договора РРЭ?",
        )

        for question in questions:
            with self.subTest(question=question):
                understanding = deterministic_query_understanding(question)
                self.assertIn("conditions", understanding.facets)
                self.assertTrue(any(
                    "договорные условия объекты потребления" in query
                    for query in understanding.retrieval_queries
                ))
                self.assertTrue(any(
                    "тарифная схема" in query
                    for query in understanding.retrieval_queries
                ))

    def test_llm_query_plan_cannot_crowd_out_rre_prerequisite_safeguards(self):
        question = "Что необходимо настроить перед запуском расчёта договора РРЭ?"
        raw = (
            '{"canonical_question":"Какие настройки нужны для расчёта?",'
            '"facets":["procedure"],"retrieval_queries":['
            '"шум один","шум два","шум три","шум четыре","шум пять","шум шесть"]}'
        )

        understanding = parse_query_understanding(raw, question)

        self.assertIn("conditions", understanding.facets)
        self.assertTrue(any(
            "договорные условия объекты потребления" in query
            for query in understanding.retrieval_queries
        ))
        self.assertNotIn("шум шесть", understanding.retrieval_queries)

    def test_find_documents_is_recognized_as_list_facet(self):
        understanding = deterministic_query_understanding(
            "Найди все доступные документы, где фигурирует ЦК."
        )

        self.assertIn("list", understanding.facets)
        self.assertTrue(contains_price_category("тариф для первой ценовой категории", "1"))
        self.assertFalse(contains_price_category("Методика расчета 1-6ЦК", "1"))

    def test_generic_document_occurrence_question_is_positive_list_intent(self):
        question = "В каких документах написано про ценовые категории?"

        self.assertTrue(is_document_occurrence_question(question))
        self.assertTrue(document_occurrence_item_matches(
            {"text": "Указаны вторая, третья и четвертая ценовые категории."},
            question,
        ))

    def test_document_occurrence_intent_covers_natural_source_list_paraphrases(self):
        questions = (
            "Какие постановления и методики описывают ценовые категории?",
            "где в документах есть информация про цк",
            "Найди документы с правилами выбора или расчёта ценовой категории.",
            "В каких источниках прямо упоминаются 1–6 ЦК?",
            "Покажи полный перечень документов о ценовых категориях без отрицательных результатов.",
            "Какая документация содержит параметр Ценовая категория?",
        )

        for question in questions:
            with self.subTest(question=question):
                self.assertTrue(is_document_occurrence_question(question))
                self.assertIn("list", deterministic_query_understanding(question).facets)

        self.assertFalse(is_document_occurrence_question("Как рассчитывается 1 ЦК?"))

    def test_generic_price_category_alias_lists_only_directly_matching_documents(self):
        questions = (
            "Перечисли все доступные документы, где фигурирует ЦК.",
            "Найди документы с правилами выбора или расчёта ценовой категории.",
        )

        for question in questions:
            with self.subTest(question=question):
                self.assertTrue(is_document_occurrence_question(question))
                self.assertTrue(document_occurrence_item_matches(
                    {"text": "Правила выбора с первой по шестую ЦК."},
                    question,
                ))
                self.assertFalse(document_occurrence_item_matches(
                    {"text": "Категории документов и предельные цены."},
                    question,
                ))
        self.assertFalse(document_occurrence_item_matches(
            {"text": "Указаны категории документов и предельные цены."},
            question,
        ))
        self.assertTrue(document_occurrence_item_matches(
            {"text": "Расчеты выполняются на РРЭ."},
            "В каких документах упоминается РРЭ?",
        ))
        self.assertFalse(document_occurrence_item_matches(
            {"text": "Расчеты выполняются на ОРЭМ."},
            "В каких документах упоминается РРЭ?",
        ))
        self.assertTrue(document_occurrence_item_matches(
            {"text": "Упоминаются вторая, третья и четвертая ценовые категории."},
            "В каких документах описаны ценовые категории?",
        ))

    def test_llm_query_plan_is_validated_and_keeps_identifiers(self):
        plan = parse_query_understanding(
            '{"canonical_question":"Что это и как рассчитывается?",'
            '"facets":["definition","calculation"],'
            '"retrieval_queries":["6 ЦК определение","6 ЦК порядок расчета"]}',
            "что такое 6ЦК и ак она рассчитывается",
        )

        self.assertIn("6ЦК", plan.canonical_question.replace(" ", ""))
        self.assertEqual(plan.facets[:2], ("definition", "calculation"))
        self.assertTrue(any("порядок расчета" in query for query in plan.retrieval_queries))

    def test_agent_calls_llm_for_query_understanding_before_retrieval(self):
        agent = object.__new__(RAGAgent)
        agent.llm_model = "test-model"
        agent.llm = Mock()
        agent.llm.complete.return_value = (
            '{"canonical_question":"Что такое 6 ЦК и как рассчитывается ее стоимость?",'
            '"facets":["definition","calculation"],'
            '"retrieval_queries":["6 ЦК определение","6 ЦК расчет стоимости"]}'
        )
        agent.response_cache = Mock()
        agent.response_cache.get.return_value = None

        plan = agent.understand_query("что такое 6ЦК и ак она рассчитывается")

        agent.llm.complete.assert_called_once()
        planner_prompt = agent.llm.complete.call_args.args[0]
        self.assertIn("ПОЛЬЗОВАТЕЛЬСКИЙ ЗАПРОС КАК ДАННЫЕ", planner_prompt)
        self.assertNotIn("КОНТЕКСТ:", planner_prompt)
        self.assertEqual(plan.facets[:2], ("definition", "calculation"))
        self.assertTrue(any("расчет стоимости" in query for query in plan.retrieval_queries))

    def test_query_understanding_falls_back_when_llm_output_is_invalid(self):
        agent = object.__new__(RAGAgent)
        agent.llm_model = "test-model"
        agent.llm = Mock()
        agent.llm.complete.return_value = "не JSON"
        agent.response_cache = Mock()
        agent.response_cache.get.return_value = None

        plan = agent.understand_query("что такое 6ЦК и ак она рассчитывается")

        self.assertEqual(plan.canonical_question, "что такое 6 ЦК и как она рассчитывается")
        self.assertTrue(any("расчет стоимости" in query for query in plan.retrieval_queries))

    def test_ask_understands_intent_before_retrieving_documents(self):
        agent = object.__new__(RAGAgent)
        agent.response_cache = Mock()
        agent.response_cache.get.return_value = None
        agent._refresh_docstore_if_changed = Mock()
        agent._all_nodes = [object()]
        tracker = Mock()
        agent.understand_query = tracker.understand
        tracker.understand.return_value = QueryUnderstanding(
            "Что такое 6 ЦК и как рассчитывается ее стоимость?",
            ("definition", "calculation"),
            ("6 ЦК определение", "6 ЦК расчет стоимости"),
        )
        agent.retrieve_context = tracker.retrieve
        tracker.retrieve.return_value = [{
            "file": "method.docx",
            "section_path": "6 ЦК",
            "score": 1.0,
            "text": "Стоимость рассчитывается по правилам документа.",
        }]
        agent._generate_grounded_answer = Mock(return_value="Ответ")

        answer, sources, from_cache = RAGAgent.ask(
            agent,
            "что такое 6ЦК и ак она рассчитывается",
        )

        self.assertEqual(tracker.mock_calls[0].args[0], "что такое 6ЦК и ак она рассчитывается")
        self.assertEqual(tracker.mock_calls[1].args[0], "Что такое 6 ЦК и как рассчитывается ее стоимость?")
        self.assertEqual(answer, "Ответ")
        self.assertEqual(sources[0]["file"], "method.docx")
        self.assertFalse(from_cache)
        self.assertFalse(agent._generate_grounded_answer.call_args.kwargs["cache_enabled"])

    def test_exact_cache_hash_keeps_price_category_number(self):
        self.assertNotEqual(
            _question_hash("Как рассчитывается 1 ЦК?"),
            _question_hash("Как рассчитывается 6 ЦК?"),
        )

    def test_ask_enables_cross_user_cache_only_for_global_context(self):
        agent = object.__new__(RAGAgent)
        agent.response_cache = Mock()
        agent._refresh_docstore_if_changed = Mock()
        agent._all_nodes = [object()]
        agent.understand_query = Mock(return_value=QueryUnderstanding(
            "Как рассчитывается 1 ЦК?",
            ("calculation",),
            ("1 ЦК расчет стоимости",),
        ))
        agent.retrieve_context = Mock(return_value=[{
            "file": "global-method.docx",
            "section_path": "1 ЦК",
            "score": 1.0,
            "text": "Стоимость рассчитывается по правилам документа.",
        }])
        agent._generate_grounded_answer = Mock(return_value="Ответ")

        answer, _sources, from_cache = RAGAgent.ask(
            agent,
            "Как рассчитывается 1 ЦК?",
            "project prompt",
            shared_cache_sources={"global-method.docx"},
            shared_system_prompt="global prompt",
        )

        self.assertEqual(answer, "Ответ")
        self.assertFalse(from_cache)
        self.assertEqual(agent.last_retrieved_context(), [{
            "file": "global-method.docx",
            "section_path": "1 ЦК",
            "score": 1.0,
            "text": "Стоимость рассчитывается по правилам документа.",
        }])
        call = agent._generate_grounded_answer.call_args
        self.assertEqual(call.args[4], "global prompt")
        self.assertTrue(call.kwargs["cache_enabled"])

    def test_ask_disables_shared_cache_for_contextual_question(self):
        agent = object.__new__(RAGAgent)
        agent.response_cache = Mock()
        agent._refresh_docstore_if_changed = Mock()
        agent._all_nodes = [object()]
        agent.understand_query = Mock(return_value=QueryUnderstanding(
            "Что такое ИВ0 и по какой формуле он рассчитывается?",
            ("definition", "calculation"),
            ("ИВ0 формула",),
        ))
        agent.retrieve_context = Mock(return_value=[{
            "file": "global-method.docx",
            "section_path": "ИВ0",
            "score": 1.0,
            "text": "Формула ИВ0.",
        }])
        agent._generate_grounded_answer = Mock(return_value="Ответ")

        RAGAgent.ask(
            agent,
            "А по какой формуле?",
            "project prompt",
            retrieval_query="Предыдущий вопрос: Что такое ИВ0?\nУточняющий вопрос: А по какой формуле?",
            shared_cache_sources={"global-method.docx"},
            shared_system_prompt="global prompt",
        )

        call = agent._generate_grounded_answer.call_args
        self.assertEqual(call.args[4], "project prompt")
        self.assertFalse(call.kwargs["cache_enabled"])

    def test_direct_retrieval_understands_query_before_search(self):
        agent = object.__new__(RAGAgent)
        agent.understand_query = Mock(return_value=QueryUnderstanding(
            "Что такое 6 ЦК и как рассчитывается ее стоимость?",
            ("definition", "calculation"),
            ("6 ЦК определение", "6 ЦК расчет стоимости"),
        ))
        agent._refresh_docstore_if_changed = Mock()
        retriever = Mock()
        retriever.retrieve.return_value = []
        agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
        agent._all_nodes = []
        for method_name in (
            "_procedural_seed_results",
            "_exact_section_seed_results",
            "_price_category_seed_results",
            "_lexical_section_seed_results",
            "_section_seed_results",
            "_formula_clause_seed_results",
            "_regulation_concept_seed_results",
            "_document_occurrence_seed_results",
        ):
            setattr(agent, method_name, Mock(return_value=[]))
        agent._domain_lexical_seed_results = Mock(return_value=[])

        result = RAGAgent.retrieve_context(
            agent,
            "что такое 6ЦК и ак она рассчитывается",
            top_k=3,
            source_domains={"method.docx": ("rre",)},
            retrieval_focus="rre",
        )

        agent.understand_query.assert_called_once_with("что такое 6ЦК и ак она рассчитывается")
        searched = [call.args[0].query_str for call in retriever.retrieve.call_args_list]
        self.assertIn("6 ЦК определение", searched)
        self.assertIn("6 ЦК расчет стоимости", searched)
        domain_seed_query = agent._domain_lexical_seed_results.call_args.args[0]
        self.assertIn("6 ЦК определение", domain_seed_query)
        self.assertIn("6 ЦК расчет стоимости", domain_seed_query)
        self.assertEqual(result, [])

    def test_price_category_seed_finds_definition_and_calculation_across_documents(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            Node(
                "Шестая ценовая категория предусматривает почасовое планирование.",
                {"file_name": "categories.docx", "section_path": "Термины"},
            ),
            Node(
                "Стоимость сетевой мощности для 6 ЦК определяется по формуле [MATH: S(сет.6цк,i)=Q(i)×T(i)]",
                {"file_name": "transfer.docx", "section_path": "5.4 Порядок определения стоимости для 5 и 6 ЦК"},
            ),
            Node(
                "Порядок определения стоимости для 1 ЦК.",
                {"file_name": "wrong.docx", "section_path": "5.1 Порядок для 1 ЦК"},
            ),
            Node(
                "Для расчета применяется двухставочный тариф.",
                {"file_name": "network.docx", "section_path": "Тарифы"},
            ),
            Node(
                "Стоимость услуг по передаче определяется по формуле [MATH: S(i)=W(i)×T(i)]",
                {"file_name": "network.docx", "section_path": "Передача"},
            ),
        ]

        seeds = agent._price_category_seed_results("что такое 6ЦК и ак она рассчитывается")

        self.assertEqual({item["file"] for item in seeds}, {"categories.docx", "transfer.docx", "network.docx"})
        self.assertTrue(any("price-category-related:6" in item["matched_queries"] for item in seeds))

    def test_generic_price_category_alias_seeds_multiple_documents(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            Node(
                "ЦК — ценовая категория потребителя.",
                {"file_name": "rules.docx", "section_path": "Термины"},
            ),
            Node(
                "Выбор ценовой категории выполняется в объекте потребления.",
                {"file_name": "method.docx", "section_path": "Настройка"},
            ),
        ]

        seeds = agent._price_category_seed_results("Что значит ЦК в расчётах электроэнергии?")

        self.assertEqual({item["file"] for item in seeds}, {"rules.docx", "method.docx"})

    def test_price_composition_paraphrases_activate_generic_category_coverage(self):
        questions = (
            "Из чего состоит цена на электроэнергию?",
            "Как формируется итоговая стоимость электроэнергии и мощности?",
            "из чего складывается цена ээ",
            "Какие части ПУНЦ относятся к покупке, передаче, сбыту и иным услугам?",
            "Сопоставь законодательную структуру цены и расчёт в системе.",
            "Дай полный состав цены без подмены услуг составляющими цены.",
        )

        self.assertTrue(all(is_price_composition_question(question) for question in questions))
        self.assertFalse(is_price_composition_question("Как рассчитывается ИВ0?"))

    def test_price_composition_seed_keeps_direct_category_documents(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            Node(
                "Общая структура цены за электроэнергию по ценовым категориям.",
                {"file_name": "Методика расчета стоимости 1-6ЦК.docx", "section_path": "Структура цены"},
            ),
            Node(
                "Предельные уровни нерегулируемых цен определяются по ценовым категориям.",
                {"file_name": "1179.docx", "section_path": "Правила"},
            ),
            Node(
                "Потребитель выбирает ценовую категорию с учётом условий договора.",
                {"file_name": "442.docx", "section_path": "Основные положения"},
            ),
            Node(
                "Стоимость отклонений на балансирующем рынке.",
                {"file_name": "r12.docx", "section_path": "ОРЭМ"},
            ),
        ]

        seeds = agent._price_category_seed_results(
            "Как формируется итоговая стоимость электроэнергии и мощности?"
        )

        self.assertEqual(
            {item["file"] for item in seeds},
            {"Методика расчета стоимости 1-6ЦК.docx", "1179.docx", "442.docx"},
        )

        scores = {item["file"]: item["score"] for item in seeds}
        self.assertGreater(scores["Методика расчета стоимости 1-6ЦК.docx"], scores["442.docx"])
        self.assertGreater(scores["1179.docx"], scores["442.docx"])

    def test_generic_price_category_context_reserves_one_hit_per_document(self):
        ranked = [
            {"file": "rules.docx", "score": 9.0, "text": "ЦК — ценовая категория."},
            {"file": "rules.docx", "score": 8.0, "text": "Первая ценовая категория."},
            {"file": "method.docx", "score": 7.0, "text": "Выбор ценовой категории."},
        ]

        selected = diversify_context_results(
            ranked,
            "Что значит ЦК в расчётах электроэнергии?",
            2,
        )

        self.assertEqual([item["file"] for item in selected], ["rules.docx", "method.docx"])

    def test_document_occurrence_seed_covers_every_directly_matching_document(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            Node(
                "В правилах применяются вторая, третья, четвертая, пятая и шестая ценовые категории.",
                {"file_name": "Постановление 1179.docx", "section_path": "1. Предельные уровни цен"},
            ),
            Node(
                "Выбор ценовой категории выполняется для объекта потребления.",
                {"file_name": "Методика РРЭ.docx", "section_path": "Объекты потребления"},
            ),
            Node(
                "Категории документов и предельные цены ведутся в разных справочниках.",
                {"file_name": "Шум.docx", "section_path": "Справочники"},
            ),
        ]

        seeds = agent._document_occurrence_seed_results(
            "В каких документах написано про ценовые категории?"
        )

        self.assertEqual(
            {item["file"] for item in seeds},
            {"Постановление 1179.docx", "Методика РРЭ.docx"},
        )
        self.assertTrue(all(
            "document-occurrence-exact" in item["matched_queries"] for item in seeds
        ))

    def test_document_occurrence_retrieval_respects_excluded_source_scope(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                text="Описаны первая и вторая ценовые категории.",
                metadata={"file_name": "visible.docx", "section_path": "Ценовые категории"},
            ),
            SimpleNamespace(
                text="Описаны пятая и шестая ценовые категории.",
                metadata={"file_name": "hidden.docx", "section_path": "Ценовые категории"},
            ),
        ]
        agent._refresh_docstore_if_changed = Mock()
        retriever = Mock()
        retriever.retrieve.return_value = []
        agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
        for method_name in (
            "_procedural_seed_results",
            "_exact_section_seed_results",
            "_price_category_seed_results",
            "_lexical_section_seed_results",
            "_section_seed_results",
            "_formula_clause_seed_results",
            "_regulation_concept_seed_results",
        ):
            setattr(agent, method_name, Mock(return_value=[]))

        results = RAGAgent.retrieve_context(
            agent,
            "В каких документах написано про ценовые категории?",
            top_k=10,
            excluded_sources={"hidden.docx"},
            retrieval_queries=(),
        )

        self.assertEqual([item["file"] for item in results], ["visible.docx"])

    def test_document_occurrence_keeps_current_revision_when_old_upload_is_excluded(self):
        old_source = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa_Методика РРЭ в 1.docx"
        current_source = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb_Методика РРЭ в 1.docx"
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                text="Выбранная ценовая категория задаётся в договорных условиях.",
                metadata={"file_name": old_source, "section_path": "Договорные условия"},
            ),
            SimpleNamespace(
                text="Выбранная ценовая категория задаётся в договорных условиях.",
                metadata={"file_name": current_source, "section_path": "Договорные условия"},
            ),
        ]
        agent._refresh_docstore_if_changed = Mock()
        retriever = Mock()
        retriever.retrieve.return_value = []
        agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
        for method_name in (
            "_procedural_seed_results",
            "_exact_section_seed_results",
            "_price_category_seed_results",
            "_lexical_section_seed_results",
            "_section_seed_results",
            "_formula_clause_seed_results",
            "_regulation_concept_seed_results",
        ):
            setattr(agent, method_name, Mock(return_value=[]))

        results = RAGAgent.retrieve_context(
            agent,
            "В каких документах написано про ценовые категории?",
            top_k=10,
            excluded_sources={old_source},
            retrieval_queries=(),
        )

        self.assertEqual([item["file"] for item in results], [current_source])

    def test_document_occurrence_survives_llm_canonical_rewrite(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                text="Выбранная ценовая категория задаётся в договорных условиях.",
                metadata={"file_name": "Методика РРЭ.docx", "section_path": "Условия"},
            ),
            SimpleNamespace(
                text="Установлены первая — шестая ценовые категории.",
                metadata={"file_name": "Постановление 1179.docx", "section_path": "Правила"},
            ),
        ]
        agent.understand_query = Mock(return_value=QueryUnderstanding(
            "Сведения о ценовых категориях",
            ("list",),
            ("ценовые категории",),
            (),
        ))
        agent._refresh_docstore_if_changed = Mock()
        retriever = Mock()
        retriever.retrieve.return_value = []
        agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
        for method_name in (
            "_procedural_seed_results",
            "_exact_section_seed_results",
            "_price_category_seed_results",
            "_lexical_section_seed_results",
            "_section_seed_results",
            "_formula_clause_seed_results",
            "_regulation_concept_seed_results",
        ):
            setattr(agent, method_name, Mock(return_value=[]))

        results = RAGAgent.retrieve_context(
            agent,
            "Какие постановления и методики описывают ценовые категории?",
            top_k=10,
        )

        self.assertEqual(
            {item["file"] for item in results},
            {"Методика РРЭ.docx", "Постановление 1179.docx"},
        )

    def test_document_occurrence_retrieval_keeps_active_normative_history_chunks(self):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                text="Упоминаются вторая, третья, четвертая, пятая и шестая ценовые категории.",
                metadata={
                    "file_name": "Постановление 1179.docx",
                    "section_path": "Правила определения нерегулируемых цен",
                    "chunk_type": "history",
                    "status": "active",
                },
            ),
            SimpleNamespace(
                text="Выбор ценовой категории выполняется для объекта потребления.",
                metadata={
                    "file_name": "Методика РРЭ.docx",
                    "section_path": "Объекты потребления",
                    "chunk_type": "value",
                    "status": "active",
                },
            ),
        ]
        agent._refresh_docstore_if_changed = Mock()
        retriever = Mock()
        retriever.retrieve.return_value = []
        agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
        for method_name in (
            "_procedural_seed_results",
            "_exact_section_seed_results",
            "_price_category_seed_results",
            "_lexical_section_seed_results",
            "_section_seed_results",
            "_formula_clause_seed_results",
            "_regulation_concept_seed_results",
        ):
            setattr(agent, method_name, Mock(return_value=[]))

        results = RAGAgent.retrieve_context(
            agent,
            "В каких документах написано про ценовые категории?",
            top_k=10,
            retrieval_queries=(),
        )

        self.assertEqual(
            {item["file"] for item in results},
            {"Постановление 1179.docx", "Методика РРЭ.docx"},
        )

    @patch("rag_agent.apply_domain_routing")
    def test_document_occurrence_positive_set_bypasses_lossy_domain_cap(self, domain_routing):
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            SimpleNamespace(
                text="Описаны первая — шестая ценовые категории.",
                metadata={"file_name": "Постановление 1179.docx", "section_path": "Правила"},
            ),
            SimpleNamespace(
                text="Выбранная ценовая категория задаётся в договорных условиях.",
                metadata={"file_name": "Методика РРЭ.docx", "section_path": "Договорные условия"},
            ),
        ]
        agent._refresh_docstore_if_changed = Mock()
        retriever = Mock()
        retriever.retrieve.return_value = []
        agent._build_query_engine = Mock(return_value=SimpleNamespace(retriever=retriever))
        for method_name in (
            "_procedural_seed_results",
            "_exact_section_seed_results",
            "_price_category_seed_results",
            "_lexical_section_seed_results",
            "_section_seed_results",
            "_formula_clause_seed_results",
            "_regulation_concept_seed_results",
        ):
            setattr(agent, method_name, Mock(return_value=[]))

        results = RAGAgent.retrieve_context(
            agent,
            "В каких документах написано про ценовые категории?",
            top_k=10,
            retrieval_queries=(),
            source_domains={
                "постановление 1179.docx": ("rre",),
                "методика ррэ.docx": ("rre",),
            },
            retrieval_focus="both",
        )

        domain_routing.assert_not_called()
        self.assertEqual(
            {item["file"] for item in results},
            {"Постановление 1179.docx", "Методика РРЭ.docx"},
        )

    def test_price_category_rerank_penalizes_other_category_heading(self):
        results = [
            {"file": "wrong.docx", "score": 1.0, "section_path": "5.1 Порядок для 1 ЦК", "text": "Общий расчет стоимости."},
            {"file": "right.docx", "score": 0.2, "section_path": "5.4 Порядок для 5 и 6 ЦК", "text": "Стоимость для 6 ЦК рассчитывается по составляющим.", "matched_queries": ["price-category-exact:6"]},
        ]

        ranked = rerank_context_results(results, "Как рассчитывается 6 ЦК?")

        self.assertEqual(ranked[0]["file"], "right.docx")

    def test_price_category_diversification_keeps_relevant_sources(self):
        ranked = [
            {"file": "a.docx", "score": 3.0, "section_path": "5.1 1 ЦК", "text": "1 ЦК рассчитывается так.", "matched_queries": ["price-category-exact:1"]},
            {"file": "a.docx", "score": 2.9, "section_path": "5.1 1 ЦК", "text": "Формула 1 ЦК.", "matched_queries": ["price-category-exact:1"]},
            {"file": "b.docx", "score": 2.0, "section_path": "Передача для 1 ЦК", "text": "Тариф для 1 ЦК."},
            {"file": "noise.docx", "score": 1.9, "section_path": "Общее", "text": "Здесь категории нет."},
        ]

        selected = diversify_context_results(ranked, "Как рассчитывается 1 ЦК?", 4)
        occurrence = diversify_context_results(ranked, "В каких документах фигурирует 1ЦК?", 10)

        self.assertIn("b.docx", {item["file"] for item in selected})
        self.assertEqual({item["file"] for item in occurrence}, {"a.docx", "b.docx"})

    def test_document_occurrence_answer_never_lists_negative_documents(self):
        answer = generate_document_occurrence_answer("В каких документах фигурирует 1ЦК?", [
            {"file": "positive.docx", "section_path": "5.1 1 ЦК", "text": "Расчет 1 ЦК."},
            {"file": "negative.docx", "section_path": "Общие положения", "text": "Ценовая категория не указана."},
        ])

        self.assertIn("positive.docx", answer)
        self.assertNotIn("negative.docx", answer)
        self.assertNotIn("не указана", answer)

    def test_generic_document_occurrence_answer_lists_only_positive_documents(self):
        question = "В каких документах написано про ценовые категории?"
        answer = generate_document_occurrence_answer(question, [
            {
                "file": "Постановление 1179.docx",
                "section_path": "1. Предельные уровни цен",
                "text": "Вторая, третья и четвертая ценовые категории.",
            },
            {
                "file": "negative.docx",
                "section_path": "Общие положения",
                "text": "Категории документов и значения цен.",
            },
        ])

        self.assertIn("Постановление 1179.docx", answer)
        self.assertNotIn("negative.docx", answer)

    def test_price_category_answer_cites_relevant_second_document(self):
        answer = ensure_price_category_source_coverage(
            "Стоимость 1 ЦК определяется по формуле. "
            "[Источник: categories.docx — раздел «5.1 Порядок определения стоимости 1 ЦК»]",
            "Как рассчитывается 1 ЦК?",
            [
                {
                    "file": "categories.docx",
                    "section_path": "5.1 Порядок определения стоимости 1 ЦК",
                    "text": "Стоимость 1 ЦК определяется по формуле.",
                    "matched_queries": ["price-category-exact:1"],
                },
                {
                    "file": "transfer.docx",
                    "section_path": "ПОРЯДОК ОПРЕДЕЛЕНИЯ СТОИМОСТИ",
                    "text": "Стоимость услуг по передаче определяется по одноставочному тарифу.",
                    "matched_queries": ["price-category-related:1"],
                },
            ],
        )

        self.assertIn("Связанный расчёт услуг по передаче", answer)
        self.assertIn("[Источник: transfer.docx —", answer)
        self.assertEqual(answer.count("[Источник: categories.docx —"), 1)

    def test_price_category_source_coverage_never_adds_unrelated_document(self):
        answer = ensure_price_category_source_coverage(
            "Стоимость 1 ЦК описана в методике.",
            "Как рассчитывается 1 ЦК?",
            [{
                "file": "unrelated.docx",
                "section_path": "Тарифы",
                "text": "Для другого расчета применяется одноставочный тариф.",
                "matched_queries": ["semantic:tariff"],
            }],
        )

        self.assertEqual(answer, "Стоимость 1 ЦК описана в методике.")

    def test_calculation_question_extracts_docx_math_payloads(self):
        formulas = extract_formula_lines_from_texts(
            [
                "[SECTION: 5.4 Порядок для 6 ЦК]\n"
                "[MATH: S(э6цк,g,i,n,m)=V(ээ6цк,g,i,n,m)×Ц(пунц6цк,g,i,n,m)]\n"
                "[MATH: S(пер1ст,i)=W(i)×T(i)]"
            ],
            "Как рассчитывается 6 ЦК?",
        )

        self.assertIn("S(э6цк,g,i,n,m)=V(ээ6цк,g,i,n,m)×Ц(пунц6цк,g,i,n,m)", formulas)
        self.assertFalse(any("1ст" in formula for formula in formulas))
        self.assertEqual(
            format_formula_for_katex(formulas[0], display=True),
            r"$$S_{\text{э6цк},g,i,n,m}=V_{\text{ээ6цк},g,i,n,m} \times \text{Ц}_{\text{пунц6цк},g,i,n,m}$$",
        )

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

    def test_calculation_seed_accepts_subject_in_governing_algorithm_section(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            Node(
                "Аванс по договорной цене определяется как процент стоимости договора.",
                {
                    "file_name": "rrе-method.docx",
                    "section_path": "РРЭ > Авансы > Алгоритмы расчета авансов",
                    "section_title": "Алгоритмы расчета авансов",
                },
            ),
            Node(
                "В документе начислений хранится статус аванса.",
                {
                    "file_name": "charging.docx",
                    "section_path": "РРЭ > Документы начислений",
                    "section_title": "Документы начислений",
                },
            ),
        ]

        seeds = agent._lexical_section_seed_results(
            "Как в системе рассчитываются авансы по РРЭ?"
        )

        self.assertTrue(seeds)
        self.assertEqual(seeds[0]["file"], "rrе-method.docx")
        self.assertIn("section-intent-anchored:аван", seeds[0]["matched_queries"])

    def test_advance_calculation_keeps_governing_cost_evidence(self):
        question = "Как рассчитываются авансы в Системе по РРЭ?"

        self.assertEqual(detect_query_intent(question), "cost")
        spec = refine_prism_query(question)
        self.assertEqual(spec.intent, "cost")
        self.assertNotIn("cost", spec.excluded_chunk_types)

        evidence = [{
            "file": "Методика расчеты на РРЭ в 1.docx",
            "section_path": "РРЭ > Авансы > Алгоритмы расчета авансов",
            "section_title": "Алгоритмы расчета авансов",
            "status": "active",
            "chunk_type": "cost",
            "score": 8.0,
            "text": "Аванс определяется как процент от стоимости предыдущего периода.",
        }]
        self.assertEqual(select_prism_evidence(evidence, spec, 3), evidence)

    def test_rre_prerequisite_questions_keep_governing_cost_evidence(self):
        questions = (
            "Для расчётов в системе по договорам РРЭ какие условия должны быть выполнены?",
            "Что необходимо настроить перед запуском расчёта договора РРЭ?",
            "Какие объекты, условия и тарифы требуются для расчёта РРЭ?",
            "Почему расчёт по договору РРЭ может не сформироваться?",
            "Дай чек-лист готовности договора РРЭ к расчёту.",
        )
        evidence = [{
            "file": "Методика расчеты на РРЭ в 1.docx",
            "section_path": "РРЭ > Договор и договорные условия",
            "section_title": "Договорные условия",
            "status": "active",
            "chunk_type": "cost",
            "score": 8.0,
            "text": "До расчёта должны быть настроены договор, объект потребления и тарифная схема.",
        }]

        for question in questions:
            with self.subTest(question=question):
                spec = refine_prism_query(question)
                self.assertIn("conditions", deterministic_query_understanding(question).facets)
                self.assertNotIn("cost", spec.excluded_chunk_types)
                self.assertNotIn("rate", spec.excluded_chunk_types)
                self.assertEqual(select_prism_evidence(evidence, spec, 3), evidence)

    def test_explicit_no_cost_request_still_excludes_cost_evidence(self):
        spec = refine_prism_query("Как рассчитывается объём без стоимости?")

        self.assertIn("cost", spec.excluded_chunk_types)
        self.assertIn("rate", spec.excluded_chunk_types)

    def test_rare_heading_terms_beat_shared_market_boilerplate(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        target = "Порядок признания процедуры конкурентного отбора несостоявшейся и определения планового почасового производства и потребления"
        generic = "Ценовая заявка включена в процедуру конкурентного отбора ценовых заявок на сутки вперед и определения планового почасового производства и потребления"
        agent = object.__new__(RAGAgent)
        agent._all_nodes = [
            *[
                Node(
                    f"Общее уведомление о заявке {index}.",
                    {"file_name": "r5.docx", "section_path": generic},
                )
                for index in range(12)
            ],
            Node("При наступлении событий процедура объявляется несостоявшейся.", {"file_name": "r7.docx", "section_path": target}),
        ]

        seeds = agent._lexical_section_seed_results(
            "В каких случаях процедура конкурентного отбора ценовых заявок на сутки вперёд признаётся несостоявшейся и как после этого определяется плановое почасовое производство и потребление?"
        )

        self.assertEqual(seeds[0]["file"], "r7.docx")

    def test_procedural_question_is_not_misclassified_as_value_lookup(self):
        spec = refine_prism_query(
            "В каких случаях процедура признаётся несостоявшейся и как после этого определяется производство?"
        )

        self.assertEqual(spec.intent, "general")
        self.assertNotIn("cost", spec.excluded_chunk_types)

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

    def test_collapses_nested_display_math_delimiters(self):
        answer = sanitize_grounded_answer(
            "Расчёт:\n\n$$\n$$V_{ИВ0}=V_{УДГ}-V_{план}$$\n$$",
            "Как рассчитывается ИВ0?",
            ["V_{ИВ0}=V_{УДГ}-V_{план}"],
        )

        self.assertEqual(answer.count("$$"), 2)
        self.assertIn("V_{ИВ0}=V_{УДГ}-V_{план}", answer)

    def test_comparison_answer_gets_grounded_formula_when_model_omits_it(self):
        answer = ensure_requested_formula_coverage(
            "ИВ0 — внешняя инициатива, а не стоимость.",
            "Чем ИВ0 отличается от расчёта стоимости?",
            ["V_{ИВ0}=V_{УДГ}-V_{план}"],
            [{"file": "r12.docx", "section_path": "Внешняя инициатива ИВ0", "text": "ИВ0"}],
        )

        self.assertIn("$$", answer)
        self.assertIn("V_", answer)
        self.assertIn("r12.docx", answer)

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

    def test_formula_with_variables_and_substitution_keeps_grounded_answer_mode(self):
        spec = refine_prism_query(
            "Приведи формулу расхода, расшифруй переменные и рассчитай при P=12, η=0,8, H=3."
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

    def test_explanatory_advance_question_does_not_add_unrelated_formula_sibling(self):
        class Node:
            def __init__(self, text, metadata):
                self.text = text
                self.metadata = metadata

        merged = {
            "advance": {
                "file": "Методика расчеты на РРЭ в 1.docx",
                "score": 0.7,
                "text": "Алгоритмы расчета авансов.",
            },
            "noise": {
                "file": "r12_rag.md",
                "score": 0.8,
                "text": "Внешняя регулировочная инициатива.",
            },
        }
        nodes = [
            Node("$$VG = MAX(0, E-F)$$", {"file_name": "r12_rag.md", "section_title": "Формула"}),
        ]

        siblings = formula_sibling_results(
            nodes,
            merged,
            "Как рассчитываются авансы в Системе по РРЭ?",
        )

        self.assertEqual(siblings, [])

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

    def test_formula_fallback_wraps_legacy_latex_for_display_katex(self):
        formula = r"\mathrm{ИВ0} = V_{\mathrm{УДГ}} - V_{\mathrm{план.ДГ}}"
        answer = generate_formula_only_answer(
            "дай формулу ИВ0",
            [],
            [formula],
            refine_prism_query("дай формулу ИВ0"),
        )

        self.assertIn(r"$$\text{ИВ0} = V_{\text{УДГ}} - V_{\text{план.ДГ}}$$", answer)
        self.assertNotIn(r"- \mathrm", answer)

    def test_formula_fallback_uses_matching_document_locator(self):
        formula = r"Q = \frac{P}{\eta H}"
        answer = generate_formula_only_answer(
            "дай формулу расхода",
            [{
                "file": "manual.md",
                "section_path": "Формула контрольного расхода",
                "text": f"Контрольный расход: {formula}",
            }],
            [formula],
            refine_prism_query("дай формулу расхода"),
        )

        self.assertIn("[Источник: manual.md — раздел «Формула контрольного расхода»]", answer)
        self.assertNotIn("извлечённый контекст", answer)

    def test_image_first_line_transcription_is_extractive_and_cited(self):
        answer = generate_image_transcription_answer(
            "Что написано в первой строке прикрепленного PNG?",
            [
                {"file": "manual.md", "text": "unrelated"},
                {
                    "file": "5328308087e64394a23ae5023fe91513_capture.png",
                    "text": "дай формулу ИВ0\n\n©\n\nОтветил: Python flow",
                    "section_path": "",
                },
            ],
        )

        self.assertIn("Первая распознанная строка: «дай формулу ИВ0»", answer)
        self.assertIn("[Источник: capture.png — документ целиком]", answer)
        self.assertNotIn("unrelated", answer)

    def test_regular_document_question_does_not_trigger_image_transcription(self):
        answer = generate_image_transcription_answer(
            "Как рассчитывается ИВ0?",
            [{"file": "capture.png", "text": "дай формулу ИВ0"}],
        )

        self.assertEqual(answer, "")

    def test_image_transcription_bypasses_llm_generation(self):
        agent = object.__new__(RAGAgent)
        agent._generation_state = SimpleNamespace(from_cache=None)
        agent.llm = Mock()

        answer = agent._generate_grounded_answer(
            "Выпиши пользовательское сообщение из верхней карточки PNG.",
            [{"file": "capture.png", "text": "дай формулу ИВ0", "section_path": ""}],
            [],
        )

        self.assertIn("дай формулу ИВ0", answer)
        agent.llm.complete.assert_not_called()

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

        self.assertIn(r"\text{ИВ0} = V_{\text{УДГ}} - V_{\text{план.\,ДГ}}", answer)
        self.assertNotIn("равна разности", answer)
        self.assertIn("[Источник: документ — раздел «Внешняя инициатива ИВ0»]", answer)

    def test_ivo_worded_rule_becomes_one_formula_in_payload(self):
        question = "Что такое ИВ0? Как рассчитывается? Приведи точную формулу."
        rule = (
            "ИВ0: величина равна разности между объемом электроэнергии, "
            "определенным согласно УДГ, и объемом электроэнергии, "
            "определенном в плановом ДГ."
        )

        formulas = extract_formula_lines_from_texts([rule], question)

        self.assertEqual(formulas, [r"\mathrm{ИВ0} = V_{\mathrm{УДГ}} - V_{\mathrm{план.\,ДГ}}"])
        self.assertNotIn("равна разности", formulas[0])

    def test_extractive_ivo_answer_prefers_uploaded_document_and_defines_variables(self):
        question = "Что такое ИВ0? Как рассчитывается? Приведи точную формулу."
        rule = (
            "ИВ0: величина равна разности между объемом электроэнергии, "
            "определенным согласно УДГ, и объемом электроэнергии, "
            "определенном в плановом ДГ."
        )
        context = [
            {
                "file": "r12_rag.md",
                "score": 2.0,
                "chunk_type": "value",
                "section_path": "Служебный корпус > ИВ0",
                "section_title": "ИВ0",
                "text": rule,
            },
            {
                "file": "abc123_r12.docx",
                "score": 1.0,
                "chunk_type": "value",
                "section_path": "4.4. Внешняя инициатива > ИВ0",
                "section_title": "ИВ0",
                "text": rule,
            },
        ]

        answer = generate_extractive_value_answer(question, context)

        self.assertIsNotNone(answer)
        self.assertIn(r"$$\mathrm{ИВ0} = V_{\mathrm{УДГ}} - V_{\mathrm{план.\,ДГ}}$$", answer)
        self.assertIn(r"V_{\mathrm{УДГ}}", answer)
        self.assertIn(r"V_{\mathrm{план.\,ДГ}}", answer)
        self.assertIn("величина внешней инициативы для соответствующей ГТП и часа", answer)
        self.assertNotIn(":;", answer)
        self.assertIn("[Источник: abc123_r12.docx —", answer)
        self.assertNotIn("[Источник: r12_rag.md —", answer)

    def test_validator_reports_missing_requested_section(self):
        spec = refine_prism_query("дай формулы из пункта 2.1.4")
        warnings = validate_retrieval("дай формулы из пункта 2.1.4", [], spec=spec)

        self.assertTrue(any("2.1.4" in warning for warning in warnings))

    def test_validator_warns_when_calculation_context_has_no_governing_section(self):
        warnings = validate_retrieval(
            "Как рассчитываются авансы в Системе по РРЭ?",
            [{
                "file": "charging.docx",
                "section_path": "РРЭ > Документы начислений",
                "section_title": "Документы начислений",
                "status": "active",
                "text": "Для документа указывается признак аванса.",
            }],
        )

        self.assertTrue(any("алгоритмом, порядком или формулой" in warning for warning in warnings))

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

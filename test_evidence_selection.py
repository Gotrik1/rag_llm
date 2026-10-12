"""Regression tests for the semantic omissions reported by human testers."""
from types import SimpleNamespace

from evidence_selection import SectionEvidenceIndex, term_keys, overview_sections, missing_overview_sections


def node(path, text, file="manual.docx"):
    return SimpleNamespace(text=text, metadata={"file_name": file, "section_path": path, "section_title": path.rsplit(" > ", 1)[-1], "status": "active"})


def test_exact_task_heading_beats_similar_general_search():
    index = SectionEvidenceIndex([
        node("Общие функции > Поиск элементов", "Введите название параметра в окно поиска."),
        node("Графики > Поиск графика", "Введите код графика в строку панели инструментов. Найденный график выделится цветом."),
    ])
    evidence = index.select("поиск графика", [{"file": "manual.docx", "section_path": "Общие функции > Поиск элементов", "text": "Введите название параметра", "score": 99}])
    assert evidence[0]["section_path"] == "Графики > Поиск графика"
    assert "выделится цветом" in evidence[0]["text"]
    assert all("Поиск элементов" not in item["section_path"] for item in evidence)


def test_overview_includes_all_child_types_and_settings():
    paths = ["Авансы", "Прогнозный", "Фактический", "Корректировочный", "Округление", "Общие настройки"]
    index = SectionEvidenceIndex([
        node("Договорные условия", "Версионные атрибуты договора."),
        *[node(f"Договорные условия > {path}", f"Данные подраздела {path}") for path in paths],
    ])
    evidence = index.select("Что ведется в договорных условиях?", [])
    assert set(paths).issubset({item["section_title"] for item in evidence})


def test_rare_exception_cannot_replace_primary_definition():
    index = SectionEvidenceIndex([
        node("Специальные случаи расчета цены", "Для особого договора используется исключение."),
        node("Цена", "Цена — стоимость за единицу объема."),
    ])
    evidence = index.select("Что такое цена?", [{"file": "manual.docx", "text": "Исключение", "section_title": "Специальные случаи", "score": 100}])
    assert evidence[0]["section_title"] == "Цена"


def test_exact_acronym_can_seed_with_no_semantic_candidates():
    evidence = SectionEvidenceIndex([node("Инициализация > ХОП", "Просмотр и задание значений ХОП на периодах.")]).select("ХОП", [])
    assert evidence[0]["section_title"] == "ХОП"


def test_lexical_inflection_handles_model_types():
    assert term_keys("какие бывают модели?") == term_keys("Модели")
    evidence = SectionEvidenceIndex([node("Виды моделей", "Стандартная и оптимизационная модели.")]).select("какие бывают модели?", [])
    assert evidence and "Стандартная" in evidence[0]["text"]


def test_expansion_cannot_reintroduce_private_or_unregistered_documents():
    index = SectionEvidenceIndex([
        node("Договорные условия", "Public", "public.docx"),
        node("Договорные условия > Авансы", "Private", "secret.docx"),
    ])
    evidence = index.select("Договорные условия", [], allowed_sources={"public.docx"})
    assert {item["file"] for item in evidence} == {"public.docx"}
    assert index.select("Договорные условия", [], allowed_sources=set()) == []


def test_normative_request_does_not_use_system_methodology():
    index = SectionEvidenceIndex([
        node("Пункт 5", "5. Предельный уровень нерегулируемых цен для второй ценовой категории определяется по формуле.", "Постановление 1179.docx"),
        node("2 ЦК", "Стоимость второго вида.", "Методика.docx"),
    ])
    evidence = index.select("Как рассчитывается 2 ЦК по постановлениям?", [])
    assert evidence and {item["file"] for item in evidence} == {"Постановление 1179.docx"}


def test_paragraph_reassembly_keeps_definition_and_formula_together():
    index = SectionEvidenceIndex([node("Расчет", "Основное правило."), node("Расчет", "Формула: S = V * C.")])
    evidence = index.select("Расчет", [{"file": "manual.docx", "section_path": "Расчет", "text": "Основное правило.", "score": 1}])
    # The overview subject is generic; direct retrieval still remains available.
    assert len(index.sections) == 1
    assert "Основное правило." in index.sections[0]["text"] and "S = V" in index.sections[0]["text"]


def test_input_budget_never_slices_an_equation():
    index = SectionEvidenceIndex([node("Цена", "Definition.\n\n" + "$" * 1000)])
    evidence = index.select("Цена", [], max_chars=500)
    assert evidence[0]["evidence_truncated"]
    assert evidence[0]["text"] == "Definition."


def test_overview_requires_explanation_sections_not_intro_name_mentions():
    context = SectionEvidenceIndex([
        node('Договорные условия', 'Настройки.'),
        node('Договорные условия > Авансы', 'Процент аванса.'),
        node('Договорные условия > Корректировочный', 'Требуются утвержденный УПД и фактические цены.'),
    ]).select('Что ведется в договорных условиях?', [])
    sections = overview_sections('Что ведется в договорных условиях?', context)
    missing = missing_overview_sections('Есть авансы и корректировочный расчет.\n### Авансы\nПроцент аванса.', sections)
    assert [item['section_title'] for item in missing] == ['Корректировочный']
    assert overview_sections('Что такое договорные условия?', context) == []


def test_requested_normative_source_includes_pp_markdown_alias():
    index = SectionEvidenceIndex([node('Пункт 5', '5. Цена для второй ценовой категории.', '2012-05-04-пп442.md')])
    assert index.select('Как рассчитывается 2 ЦК по постановлениям?', [])


def test_action_verb_cannot_hide_exact_subject_heading():
    index = SectionEvidenceIndex([
        node('Договорные условия > Авансы', 'Задаются число авансов, срок оплаты, процент и смещение по месяцам.'),
        node('Расчет электроэнергии', 'Почасовые объемы.'),
    ])
    assert term_keys('Как задаются авансы?') == term_keys('Авансы')
    evidence = index.select('Как задаются авансы?', [{'file': 'manual.docx', 'section_path': 'Расчет электроэнергии', 'text': 'Почасовые объемы.'}])
    assert evidence[0]['section_title'] == 'Авансы'

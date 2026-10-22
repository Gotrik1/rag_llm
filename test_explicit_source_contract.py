"""Catalogue identities are not loose topic/year overlap or model guesses."""
import unicodedata

import pytest

from evidence_selection import matching_catalogue_sources
from rag_agent import filter_scoped_evidence, matching_explicit_sources


@pytest.mark.parametrize("matcher", [matching_catalogue_sources, matching_explicit_sources])
@pytest.mark.parametrize("question, aliases, expected", [
    (
        "По файлу a7_2026.md объясни порядок расчета стоимости.",
        {"first.md": "a7_2026.md", "second.md": "b4_2026.md",
         "method.docx": "Методика расчета стоимости.docx"},
        {"first.md"},
    ),
    (
        "Согласно документу Prism Operations 2024.docx поясни процедуру.",
        {"first.docx": "Prism Operations 2024.docx",
         "second.pdf": "Other Procedure 2024.pdf", "year.md": "2024.md"},
        {"first.docx"},
    ),
    (
        "По файлу Operations v12.docx объясни действие версии v12.",
        {"first.docx": "Operations v12.docx", "second.pdf": "Audit v12.pdf"},
        {"first.docx"},
    ),
    (
        "По регламенту a7 объясни порядок контроля.",
        {"first.md": "a7_2026.md", "second.md": "b4_2026.md"},
        {"first.md"},
    ),
    (
        "Что написано в A 7?",
        {"first.md": "a7_archive.md", "second.md": "a17_archive.md"},
        {"first.md"},
    ),
    (
        "Объясни Ω7 и р3.",
        {"first.md": "Ω7_guide.md", "second.md": "р3_guide.md"},
        {"first.md", "second.md"},
    ),
    (
        "Сравни a7_2026.md и b4.",
        {"first.md": "a7_2026.md", "second.md": "b4_2025.md",
         "third.md": "c9_2026.md"},
        {"first.md", "second.md"},
    ),
    (
        "Согласно документу Prism Operations.docx и документам 37 и 82 поясни правила.",
        {"first.docx": "Prism Operations.docx", "second.md": "Protocol N 37.md",
         "third.pdf": "Protocol № 82.pdf", "year.md": "Protocol 2037.pdf"},
        {"first.docx", "second.md", "third.pdf"},
    ),
    (
        "Сравни Alpha.docx и Beta Handbook.",
        {"first.docx": "Alpha.docx", "second.md": "Beta Handbook"},
        {"first.docx", "second.md"},
    ),
    (
        "Сравни Alpha.docx и инструкции по обслуживанию.",
        {"first.docx": "Alpha.docx", "second.md": "Инструкция по обслуживанию v2.docx"},
        {"first.docx", "second.md"},
    ),
    (
        "По файлу a7_2026.md проверь результат.",
        {"first.md": "a7_2026.md", "old.md": "a7_2025.md"},
        {"first.md"},
    ),
    (
        "Из файла Инструкция по обслуживанию v2.docx выпиши правила.",
        {"first.docx": "Инструкция по обслуживанию v2.docx",
         "old.docx": "Инструкция по обслуживанию v1.docx"},
        {"first.docx"},
    ),
    (
        "Что в Super-Price.docx?",
        {"first.docx": "Super-Price.docx", "second.docx": "Price.docx"},
        {"first.docx"},
    ),
    (
        "Сравни Alpha.docx и Beta.pdf.",
        {"first.docx": "Alpha.docx", "second.pdf": "Beta.pdf"},
        {"first.docx", "second.pdf"},
    ),
    (
        "Покажи данные согласно документу Café.md.",
        {"first.md": unicodedata.normalize("NFD", "Café.md")},
        {"first.md"},
    ),
    (
        "Прочитай ПРИЁМКА.DOCX.",
        {"first.docx": "Приемка.docx"},
        {"first.docx"},
    ),
    (
        "По документу Manual.docx объясни действие.",
        {r"C:\uploads\FIRST.docx": r"C:\catalogue\Manual.docx"},
        {"first.docx"},
    ),
    (
        r"Прочитай C:\uploads\FIRST.docx.",
        {r"C:\uploads\FIRST.docx": "Friendly title.pdf"},
        {"first.docx"},
    ),
    (
        "Прочитай first.md.",
        {"/uploads/first.md": ""},
        {"first.md"},
    ),
    (
        "Прочитай Shared.docx.",
        {"first.docx": "Shared.docx", "second.docx": "SHARED.DOCX"},
        {"first.docx", "second.docx"},
    ),
    (
        "По документу № 7 объясни действие.",
        {"first.md": "Protocol № 7.md", "second.md": "Protocol № 17.md"},
        {"first.md"},
    ),
    (
        "По документу37 объясни действие.",
        {"first.md": "Protocol N 37.md", "second.md": "Protocol N 7.md"},
        {"first.md"},
    ),
    (
        "В документе 2026 найди расчет стоимости.",
        {"first.docx": "Процедура расчета стоимости 2026.docx",
         "second.pdf": "Приложение от 01.04.2026.pdf"},
        set(),
    ),
    (
        "В документе v12 найди расчет стоимости.",
        {"first.docx": "Procedure v12.docx", "second.pdf": "Audit v12.pdf"},
        set(),
    ),
    (
        "По документу 7 объясни показатель.",
        {"first.md": "a7_procedure.md", "second.md": "b7_procedure.md"},
        set(),
    ),
    (
        "По документу покажи расчет стоимости и порядок контроля.",
        {"first.docx": "Методика расчета стоимости.docx",
         "second.md": "Регламент порядка контроля.md"},
        set(),
    ),
    (
        "Объясни a7_extra.md и other-Price.docx.",
        {"first.md": "a7_guide.md", "second.docx": "Price.docx"},
        set(),
    ),
    (
        "Что написано в hidden.docx и z9?",
        {"first.md": "a7_guide.md"},
        set(),
    ),
    ("Прочитай first.md.", {}, set()),
    ("Прочитай first.md.", None, set()),
    (
        "Как выполняется расчет мощности?",
        {"first.md": "Расчет мощности", "second.md": "Guide.md"},
        set(),
    ),
    (
        "Что изменилось в 2026 году?",
        {"first.md": "2026", "second.md": "Guide.md"},
        set(),
    ),
    (
        "Как формируется отчет мощности 2026?",
        {"first.md": "Отчет мощности 2026", "second.md": "Guide.md"},
        set(),
    ),
    (
        "Как разработать регламент работы?",
        {"first.md": "Регламент работы", "second.md": "Guide.md"},
        set(),
    ),
    (
        "Как оформляется документ?",
        {"first.md": "Документ", "second.md": "Guide.md"},
        set(),
    ),
    (
        "По документу Alpha.md объясни расчет мощности.",
        {"first.md": "Alpha.md", "second.md": "Расчет мощности"},
        {"first.md"},
    ),
    (
        "Что сказано в документе Расчет мощности?",
        {"first.md": "Расчет мощности", "second.md": "Guide.md"},
        {"first.md"},
    ),
    (
        "Прочитай «Расчет мощности».",
        {"first.md": "Расчет мощности", "second.md": "Guide.md"},
        {"first.md"},
    ),
    (
        "Сравни Beta Handbook и Alpha.docx.",
        {"first.docx": "Alpha.docx", "second.md": "Beta Handbook"},
        {"first.docx", "second.md"},
    ),
    (
        "Сравни Alpha.docx и Beta Handbook и Gamma Manual.",
        {"first.docx": "Alpha.docx", "second.md": "Beta Handbook", "third.md": "Gamma Manual"},
        {"first.docx", "second.md", "third.md"},
    ),
])
def test_catalogue_reference_contract(matcher, question, aliases, expected):
    assert matcher(question, aliases) == expected


def test_exact_source_contract_is_applied_before_final_evidence_publication():
    aliases = {"first.md": "a7_2026.md", "second.md": "b4_2026.md",
               "method.docx": "Методика расчета стоимости.docx"}
    items = [{"file": source, "text": "Порядок расчета стоимости описан здесь.",
              "section_path": "Правила", "status": "active", "score": 1.0}
             for source in aliases]
    selected = filter_scoped_evidence(items,
        "По файлу a7_2026.md объясни порядок расчета стоимости.", source_aliases=aliases)
    assert [item["file"] for item in selected] == ["first.md"]


def test_named_source_cannot_bypass_authorized_catalogue_or_deleted_exclusions():
    items = [{"file": source, "text": "Порядок проверки описан здесь.",
              "section_path": "Правила", "status": "active", "score": 1.0}
             for source in ("first.md", "foreign.md")]
    assert filter_scoped_evidence(items, "Из файла a7_guide.md выпиши порядок проверки.",
        source_aliases={"first.md": "a7_guide.md"}, excluded_sources={"first.md"}) == []


def test_legacy_abbreviated_kind_inside_exact_filename_does_not_expand_scope():
    aliases = {"named.docx": "Методика 1 ЦК v2.docx",
               "unrelated.docx": "Методика расчета стоимости 1-6ЦК v2.docx"}
    assert matching_explicit_sources(
        "Из файла Методика 1 ЦК v2.docx выпиши основные условия.", aliases) == {"named.docx"}


def test_independent_legacy_abbreviated_reference_survives_exact_name_masking():
    aliases = {"named.docx": "Alpha.docx",
               "method.docx": "Методика расчета стоимости 1-6ЦК v2.docx"}
    assert matching_explicit_sources(
        "Сравни Alpha.docx и методику 1 ЦК.", aliases) == set(aliases)

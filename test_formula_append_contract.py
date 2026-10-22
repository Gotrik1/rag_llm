"""No model calls: automatic formula completion must not typeset prose."""

import pytest

from rag_agent import ensure_requested_formula_coverage, format_formula_for_katex


ANSWER = "Описание настройки приведено в карточке объекта."
FORMULA = "P=U I"
PROSE = "Реализация расчетов в системе представлена ниже (рисунок 1)."


def source(text):
    return {"file": "guide.md", "section_path": "Расчет и настройка", "text": text}


@pytest.mark.parametrize("question", [
    "Где настроить текущий расчет объекта?",
    "Перечисли виды расчетов в программе.",
    "Чем основной расчет отличается от вспомогательного?",
    "Как создать расчет по шаблону?",
    "Как редактировать формулы в карточке?",
    "Объясни порядок расчета, без формул.",
    "Формулы не нужны, объясни словами.",
    "Не показывай никаких формул, объясни словами.",
    "Покажи, где настраиваются формулы в карточке.",
    "Как изменить формулу в карточке?",
])
def test_unrequested_formula_does_not_change_answer(question):
    assert ensure_requested_formula_coverage(
        ANSWER, question, [FORMULA], [source(FORMULA)]
    ) == ANSWER


def test_prose_calculation_rule_is_not_a_symbolic_formula():
    assert ensure_requested_formula_coverage(
        ANSWER, "Приведи формулу мощности.", [PROSE], [source(PROSE)]
    ) == ANSWER


@pytest.mark.parametrize("prose", [
    "Navigation: Module > Object > Formula",
    "Configuration: Mode = Automatic",
    r"\text{Configuration: Mode = Automatic}",
])
def test_operator_in_ui_prose_is_not_sufficient_math_evidence(prose):
    assert ensure_requested_formula_coverage(
        ANSWER, "Приведи формулу мощности.", [prose], [source(prose)]
    ) == ANSWER


def test_skips_prose_and_uses_a_later_source_confirmed_formula():
    result = ensure_requested_formula_coverage(
        ANSWER, "Приведи формулу мощности.", [PROSE, FORMULA],
        [source(PROSE), source(FORMULA)],
    )
    assert "### Формула из источника" in result
    assert FORMULA in result
    assert PROSE not in result


@pytest.mark.parametrize("question", [
    "Приведи формулу мощности.",
    "Как рассчитывается мощность?",
    "Запиши расчет мощности.",
    "Как настроить параметр и какая формула расчета?",
])
def test_explicit_math_requests_keep_grounded_completion(question):
    result = ensure_requested_formula_coverage(
        ANSWER, question, [FORMULA], [source(FORMULA)]
    )
    assert FORMULA in result
    assert "[Источник: guide.md" in result
    assert "$$" in result


def test_latex_expression_without_equality_can_be_a_formula():
    expression = r"\frac{U}{R}"
    assert expression in ensure_requested_formula_coverage(
        ANSWER, "Приведи формулу тока.", [expression], [source(expression)]
    )


@pytest.mark.parametrize("expression", [
    "U*I", "U+I", r"U\cdot I", "P=U_{rms} I_{rms}", "P=2 U I",
    r"P=U_{rms}\,I_{rms}",
])
def test_symbolic_product_or_sum_without_equality_can_be_a_formula(expression):
    assert format_formula_for_katex(expression, display=True) in ensure_requested_formula_coverage(
        ANSWER, "Приведи формулу мощности.", [expression], [source(expression)]
    )


def test_unconfirmed_candidate_does_not_hide_later_confirmed_formula():
    result = ensure_requested_formula_coverage(
        ANSWER, "Приведи формулу мощности.", ["Z=X+Y", FORMULA], [source(FORMULA)]
    )
    assert FORMULA in result
    assert "Z=X+Y" not in result


def test_no_formula_is_added_without_source_confirmation():
    assert ensure_requested_formula_coverage(
        ANSWER, "Приведи формулу мощности.", [FORMULA], [source("Другой текст.")]
    ) == ANSWER


def test_existing_display_math_is_not_duplicated():
    answer = "Расчет:\n$$P=U I$$"
    assert ensure_requested_formula_coverage(
        answer, "Приведи формулу мощности.", [FORMULA], [source(FORMULA)]
    ) == answer

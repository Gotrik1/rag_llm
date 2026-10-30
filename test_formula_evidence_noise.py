"""Calculation evidence needs a rule or equation, not a calculation heading."""

import pytest

from rag_agent import extract_formula_lines_from_texts


QUESTION = "Приведи формулу расчета показателя."


@pytest.mark.parametrize("text", [
    "8.1 Основные объекты системы для ведения и расчета договоров на оказание услуг по передаче ЭЭ.",
    "3.2 Общие правила расчета показателей.",
    "Общие сведения о расчете параметров.",
    "Навигация: раздел расчета показателей.",
    "Расчет описан в разделе 5.",
    "Порядок расчета приведен в приложении.",
    "Расчет по формуле описан в разделе 5.",
])
def test_calculation_mentions_are_not_calculation_rules(text):
    assert extract_formula_lines_from_texts([text], QUESTION) == []


@pytest.mark.parametrize(("text", "expected"), [
    ("[MATH: P = U * I]", "P = U * I"),
    ("[FORMULA] P = U * I", "P = U * I"),
    (r"$$P = \frac{W}{t}$$", r"P = \frac{W}{t}"),
    ("Стоимость определяется как сумма затрат и прибыли.",
     "Стоимость определяется как сумма затрат и прибыли."),
    ("Мощность рассчитывается как произведение напряжения и тока.",
     "Мощность рассчитывается как произведение напряжения и тока."),
    ("Итоговая величина равна разности начального и конечного значений.",
     "Итоговая величина равна разности начального и конечного значений."),
    ("Расчет производится путем умножения объема на тариф.",
     "Расчет производится путем умножения объема на тариф."),
    ("8.1 Мощность определяется как произведение напряжения и тока.",
     "8.1 Мощность определяется как произведение напряжения и тока."),
])
def test_real_equations_and_worded_calculation_rules_are_retained(text, expected):
    assert expected in extract_formula_lines_from_texts([text], QUESTION)

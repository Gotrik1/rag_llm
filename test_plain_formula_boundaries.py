"""Synthetic literal boundaries: prose is not the equation's output symbol."""
import pytest

from grounding_verifier import (
    _formulas, _plain_equation_parts, build_grounding_judge_prompt, normalize_formula, verify_grounding,
)


def source(formula, eid="E1", **metadata):
    return {"evidence_id": eid, "source": "synthetic.md", "section_path": "Rule",
            "text": "[MATH: " + formula + "]", **metadata}


def equation_checks(answer, evidence):
    return [check for check in verify_grounding(answer, evidence).checks if check.kind == "formula"]


@pytest.mark.parametrize("intro,formula", [
    ("Коэффициент измерительного комплекса K определяется по формуле ", "K=Ks*Kt*Kn"),
    ("Расчетная формула имеет вид ", "S=V*T"),
    ("The calculated equation has the form ", "R=A+B"),
    ("The coefficient is defined by the equation ", r"Q_{i,n}^{p}=V_{i,n}*T_n"),
    ("Показатель принимает вид ", "Р=А-Б"),
    ("Показатель рассчитывается по формуле ", r"Ц_{i, n}=V_i*T_n"),
    ("Формула: ", "Общий расход=(Vрасх+Потери)+VДР"),
    ("Expression:", "Total cost=Volume*Tariff"),
    ("", "Общий расход=(Vрасх+Потери)+VДР"),
    ("", "Total cost=Volume*Tariff"),
    ("", "Énergie totale=Volume*Tarif"),
    ("The equation is ", "α=A+B"),
    ("", r"R_{i,n}=A_i+B_n"),
    ("", "A+B=C"),
])
def test_plain_wrappers_have_exact_literal_offsets_without_prose_lhs(intro, formula):
    answer = "Короткое вступление.\n\n" + intro + formula + ". [E1]"
    checks = equation_checks(answer, [source(formula)])
    assert len(checks) == 1
    check = checks[0]
    assert check.status == "source_matched"
    assert normalize_formula(check.text) == normalize_formula(formula)
    assert check.start == answer.index(formula)
    assert answer[check.start:check.end] == check.text
    assert check.details["semantic_applicability_verified"] is False


@pytest.mark.parametrize("changed", ["X=A-B", "X=A+B+Z", "X=A_i+B", "Y=A+B", "Х=A+B"])
@pytest.mark.parametrize("intro", ["Расчетная формула имеет вид ", "The equation is "])
def test_wrappers_do_not_authorize_changed_sign_terms_index_or_output(intro, changed):
    checks = equation_checks(intro + changed + ". [E1]", [source("X=A+B")])
    assert len(checks) == 1
    assert checks[0].status == "unsupported"
    assert normalize_formula(checks[0].text) == normalize_formula(changed)


def test_a_complete_left_expression_is_not_shortened_to_its_last_symbol():
    answer = "A + X = B+C [E1]"
    checks = equation_checks(answer, [source("A+X=B+C")])
    assert len(checks) == 1 and checks[0].status == "source_matched"
    assert normalize_formula(checks[0].text) == "A+X=B+C"


@pytest.mark.parametrize("formula", [
    "Уровень X=A+B", "Total X=A+B", "Total A=A+B", "Название Р=А-Б",
    r"Уровень X_{i,n}=A_i+B_n", "Formula X=A+B", "Expression A=A+B",
])
@pytest.mark.parametrize("native", [True, False])
def test_named_multiword_lhs_ending_in_one_letter_is_not_presentation_prose(formula, native):
    item = source(formula)
    if not native:
        item["text"] = formula
    answer = formula + " [E1]"
    checks = equation_checks(answer, [item])
    assert len(checks) == 1 and checks[0].status == "source_matched"
    assert normalize_formula(checks[0].text) == normalize_formula(formula)
    assert checks[0].start == 0
    assert answer[checks[0].start:checks[0].end] == checks[0].text


def test_multiline_equations_retain_independent_citations_and_changed_second_rhs():
    answer = "Формула: X=A+B [E1]\nВторая формула имеет вид Y=C+D+Z [E2]"
    checks = equation_checks(answer, [source("X=A+B"), source("Y=C+D", "E2")])
    assert [(check.status, check.evidence_ids) for check in checks] == [
        ("source_matched", ["E1"]), ("unsupported", ["E2"])]
    for check in checks:
        assert answer[check.start:check.end] == check.text


def test_semicolon_equations_are_all_checked_without_rhs_clipping():
    answer = "Formula: X=A+B [E1]; the next equation is Y=C+D+Z [E2]"
    checks = equation_checks(answer, [source("X=A+B"), source("Y=C+D", "E2")])
    assert [(normalize_formula(c.text), c.status) for c in checks] == [
        ("X=A+B", "source_matched"), ("Y=C+D+Z", "unsupported")]


@pytest.mark.parametrize("middle", ["heading", "explanation without equation", "Пояснение"])
def test_semicolon_prose_cannot_hide_wrong_equation_or_lend_its_citation(middle):
    answer = "X=A+B [E1]; " + middle + "; Y=C+D+Z [E2]"
    evidence = [source("X=A-B"), {**source("X=A+B", "E2"),
                 "text": "[MATH: X=A+B]\n[MATH: Y=C+D]"}]
    checks = equation_checks(answer, evidence)
    assert [(normalize_formula(check.text), check.evidence_ids, check.status) for check in checks] == [
        ("X=A+B", ["E1"], "unsupported"), ("Y=C+D+Z", ["E2"], "unsupported")]
    for check in checks:
        assert answer[check.start:check.end] == check.text


def test_semicolon_split_preserves_nested_index_and_tex_spacing_delimiters():
    first = r"X_{i;j}=A\;+\;B [E1]"
    line = first + "; heading; Y=C+D [E2]"
    assert _plain_equation_parts(line) == [(first, 0), (" heading", len(first) + 1),
                                          (" Y=C+D [E2]", len(first) + len("; heading;") )]
    checks = equation_checks(line, [source(r"X_{i;j}=A+B"), source("Y=C+D", "E2")])
    assert [(normalize_formula(check.text), check.status) for check in checks] == [
        (r"X_{i;j}=A+B", "source_matched"), ("Y=C+D", "source_matched")]


def test_chained_equalities_are_not_reduced_to_a_supported_prefix():
    checks = equation_checks("The equation is X=A+B=C [E1]", [source("X=A+B")])
    assert len(checks) == 1
    assert normalize_formula(checks[0].text) == "X=A+B=C"
    assert checks[0].status == "unsupported"


def test_unknown_citation_cannot_be_fixed_by_correct_equation_boundaries():
    checks = equation_checks("Формула имеет вид X=A+B [E99]", [source("X=A+B")])
    assert len(checks) == 1 and checks[0].status == "invalid_citation"


def test_ocr_formula_does_not_become_authoritative_after_prefix_removal():
    ocr = {"evidence_id": "E1", "source": "synthetic.png", "text":
           "[IMAGE_OCR: scan.png]\nФормула имеет вид X=A+B\n[/IMAGE_OCR]\n[IMAGE_WARNING: verify indices]"}
    checks = equation_checks("The equation is X=A+B [E1]", [ocr])
    assert len(checks) == 1 and checks[0].status == "unverified_ocr"


@pytest.mark.parametrize("answer", [
    "$X=A+B$ [E1]", "$$X=A+B$$ [E1]", r"\(X=A+B\) [E1]",
    "[MATH: X=A+B] [E1]", "`X=A+B` [E1]",
])
def test_native_math_and_code_span_contract_is_unchanged(answer):
    checks = equation_checks(answer, [source("X=A+B")])
    assert len(checks) == 1 and checks[0].status == "source_matched"


def test_nonmathematical_label_without_equation_is_not_invented_as_formula():
    assert _formulas("Расчетная формула имеет вид: расчет по тарифу. [E1]") == []


def test_literal_matching_does_not_delete_negation_or_certify_whole_claim_applicability():
    answer = "Для этого случая неверно: X=A+B. [E1]"
    evidence = [source("X=A+B")]
    report = verify_grounding(answer, evidence)
    checks = [check for check in report.checks if check.kind == "formula"]
    assert len(checks) == 1 and checks[0].status == "source_matched"
    assert checks[0].details["semantic_applicability_verified"] is False
    assert not report.assessment_complete
    prompt = build_grounding_judge_prompt(answer, evidence)
    assert answer in prompt
    assert "неверно" in prompt
    judged = verify_grounding(answer, evidence, claims=[{
        "claim": answer, "support": "unsupported", "evidence_ids": [], "quotes": [],
    }])
    assert next(c for c in judged.checks if c.kind == "formula").status == "source_matched"
    assert any(c.kind == "claim" and c.status == "unsupported" and c.text == answer
               for c in judged.checks)
    assert judged.needs_repair

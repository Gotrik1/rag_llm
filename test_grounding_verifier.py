import re

import pytest

from grounding_verifier import GroundingCheck, GroundingReport, build_grounding_judge_prompt, build_numeric_rules, extract_answer_claims, native_formula_catalogue, normalize_formula, safe_partial_answer, verify_grounding


def evidence(text, identifier="E1", **metadata):
    return {"evidence_id": identifier, "source": "Лаборатория-Альфа.md", "section_path": "Основной порядок", "text": text, **metadata}


def formula_check(report):
    return next(check for check in report.checks if check.kind == "formula")


def test_formula_preserves_indices_and_distinct_components():
    source = evidence(r"Покупная величина: $$C_{buy}=Q_{buy}\cdot T_{buy}$$")
    good = verify_grounding(r"$$\mathrm{C}_{buy}=T_{buy} \times Q_{buy}$$ [E1]", [source])
    bad = verify_grounding(r"$$C_{buy}=Q_{grid}\cdot T_{grid}$$ [E1]", [source])
    assert formula_check(good).status == "source_matched"
    assert not good.needs_repair
    assert formula_check(bad).status == "unsupported"
    assert bad.needs_repair


def test_real_citation_does_not_authorize_added_terms_or_changed_signs():
    source = evidence(r"$$Z_i=A_i-B_i$$")
    for changed in (r"Z_i=A_i+B_i", r"Z_i=A_i-B_i+F_i", r"Z_j=A_i-B_i"):
        report = verify_grounding("$$" + changed + "$$ [Источник: Лаборатория-Альфа.md — Основной порядок]", [source])
        assert formula_check(report).status == "unsupported"


def test_fraction_presentation_equivalence_not_inverse_ratio():
    source = evidence(r"$$K=\frac{N_{valid}}{N_{all}}$$")
    assert formula_check(verify_grounding(r"$$K=N_{valid}/N_{all}$$ [E1]", [source])).status == "source_matched"
    assert formula_check(verify_grounding(r"$$K=N_{all}/N_{valid}$$ [E1]", [source])).status == "unsupported"


def test_cyrillic_symbol_and_latin_lookalike_are_not_equivalent():
    source = evidence(r"$$Ц_i=V_i*T_i$$")
    assert formula_check(verify_grounding(r"$$U_i=V_i*T_i$$ [E1]", [source])).status == "unsupported"


def test_numerical_substitution_requires_source_rule_and_explicit_inputs():
    source = evidence(r"Ток рассчитывается по правилу $$I=\frac{U}{R}$$")
    rule = {"formula": "I=U/R", "evidence_ids": ["E1"], "bindings": {"U": 12, "R": 4}}
    answer = r"$$I=\frac{12}{4}=3$$ [E1]"
    report = verify_grounding(answer, [source], question="U=12, R=4. Рассчитай ток.", calculation_rules=[rule])
    assert formula_check(report).status == "calculated"
    assert formula_check(verify_grounding(answer, [source], calculation_rules=[rule])).status == "unsupported"
    assert formula_check(verify_grounding(answer, [source], question="U=12, R=4.", calculation_rules=["I=12/4=3"])).status == "unsupported"
    wrong = verify_grounding(r"$$I=12*4=48$$ [E1]", [source], question="U=12, R=4.", calculation_rules=[rule])
    assert formula_check(wrong).status == "unsupported"


def test_numeric_coincidence_cannot_authorize_different_operation():
    source = evidence(r"$$X=A+B$$")
    rule = {"formula": "X=A+B", "evidence_ids": ["E1"], "bindings": {"A": 2, "B": 2}}
    report = verify_grounding("$$X=2*2=4$$ [E1]", [source], question="A=2, B=2", calculation_rules=[rule])
    assert formula_check(report).status == "unsupported"


def test_ocr_formula_requires_authoritative_confirmation():
    ocr = evidence(r"[IMAGE_OCR: scan.png] $$L=N/D$$ [/IMAGE_OCR] [IMAGE_WARNING: verify indices]")
    report = verify_grounding("$$L=N/D$$ [E1]", [ocr])
    assert formula_check(report).status == "unverified_ocr"
    assert report.needs_repair
    native = evidence(r"[MATH: L=N/D]", "E2")
    report = verify_grounding("$$L=N/D$$ [E1,E2]", [ocr, native])
    assert formula_check(report).status == "source_matched"


def test_inline_symbols_are_not_treated_as_unverified_formulas():
    report = verify_grounding(r"Показатель $Q_{buy}$ измеряется в единицах объёма. [E1]", [evidence("Объём обозначен Q_buy.")])
    assert not [check for check in report.checks if check.kind == "formula"]
    assert not report.needs_repair
    assert not report.assessment_complete


def test_invalid_citation_and_quote_cannot_be_judge_supported():
    answer = "Метод использует два уровня контроля. [E99]"
    source = evidence("Метод использует один уровень контроля.")
    report = verify_grounding(answer, [source], claims=[{
        "claim": "Метод использует два уровня контроля.", "support": "supported",
        "evidence_ids": ["E1"], "quotes": [{"evidence_id": "E1", "quote": "Метод использует два уровня контроля."}],
    }])
    assert report.needs_repair
    assert any(check.status == "invalid_citation" for check in report.checks)
    assert any(check.status == "invalid_judge_evidence" for check in report.checks)
    assert not report.assessment_complete


def test_partial_judge_coverage_never_certifies_entire_answer():
    supported = "Доступны два канала передачи данных."
    answer = supported + " [E1]\n\nСрок хранения любых данных составляет двадцать лет. [E1]"
    source = evidence(supported)
    report = verify_grounding(answer, [source], claims=[{
        "claim": supported, "support": "supported", "evidence_ids": ["E1"],
        "quotes": [{"evidence_id": "E1", "quote": supported}],
    }])
    assert report.assessed_claim_count == 1
    assert report.unassessed_claim_count == 1
    assert not report.assessment_complete
    assert report.checks[-1].status == "judge_supported"


def test_claim_span_is_bounded_by_actual_answer_and_all_ids_need_quotes():
    source = evidence("Существуют два независимых канала связи.")
    claim = {"claim": "Текст, которого нет в ответе.", "support": "supported", "evidence_ids": ["E1"],
             "quotes": [{"evidence_id": "E1", "quote": source["text"]}]}
    report = verify_grounding("Существуют два независимых канала связи. [E1]", [source], claims=[claim])
    assert report.checks[-1].status == "invalid_judge_evidence"
    assert not report.assessment_complete


def test_judge_outage_does_not_remove_supported_content():
    def outage(_):
        raise TimeoutError()
    answer = "В системе существуют два канала передачи данных. [E1]"
    report = verify_grounding(answer, [evidence(answer)], judge=outage)
    assert not report.needs_repair
    assert "unavailable" in report.warnings[0]
    assert not report.assessment_complete


def test_repair_targets_only_faulty_spans_and_preserves_useful_facts():
    answer = "Есть два канала контроля. [E1]\n\n$$X=A+B+C$$ [E1]"
    report = verify_grounding(answer, [evidence("Есть два канала контроля.\n$$X=A+B$$")])
    prompt = report.repair_prompt()
    assert "Сохрани полезные подтверждённые факты" in prompt
    assert "X=A+B+C" in prompt
    assert report.to_dict()["needs_repair"]
    partial = safe_partial_answer(answer, report)
    assert "Есть два канала контроля. [E1]" in partial
    assert "X=A+B+C" not in partial
    assert "Не удалось подтвердить часть ответа" in partial


def test_judge_prompt_has_only_supplied_evidence_and_no_confidence_contract():
    prompt = build_grounding_judge_prompt("Проверяемый ответ.", [evidence("Новый независимый источник.")])
    assert "Новый независимый источник" in prompt
    assert '"evidence_id": "E1"' in prompt
    assert "не добавляй confidence" in prompt.lower()


def test_duplicate_evidence_ids_fail_closed():
    with pytest.raises(ValueError):
        verify_grounding("Ответ", [evidence("Один"), evidence("Два")])


def test_normalization_retains_nested_index_payload():
    assert normalize_formula(r"\mathrm{Q}_{\text{пок},i,m}\times T_i") == r"Q_{пок,i,m}*T_{i}"


def test_nary_arithmetic_does_not_ignore_third_term():
    source = evidence("$$X=A+B+C$$")
    rule = {"formula": "X=A+B+C", "evidence_ids": ["E1"], "bindings": {"A": 2, "B": 3, "C": 4}}
    good = verify_grounding("$$X=2+3+4=9$$ [E1]", [source], question="A=2 B=3 C=4", calculation_rules=[rule])
    wrong = verify_grounding("$$X=2+3+4=5$$ [E1]", [source], question="A=2 B=3 C=4", calculation_rules=[rule])
    assert formula_check(good).status == "calculated"
    assert formula_check(wrong).status == "unsupported"


def test_malformed_judge_id_does_not_crash_or_remove_unknown_claim():
    answer = "Есть два независимых канала передачи. [E1]"
    report = verify_grounding(answer, [evidence(answer)], claims=[{
        "claim": answer, "support": "supported", "evidence_ids": [["E1"]],
        "quotes": [{"evidence_id": ["E1"], "quote": "Есть два независимых канала передачи."}],
    }])
    assert not report.needs_repair
    assert safe_partial_answer(answer, report) == answer


def test_literal_or_code_equation_does_not_bypass_verifier():
    source = evidence("X=A+B")
    for answer in ("X=A+B+C [E1]", "`X=A+B+C` [E1]"):
        report = verify_grounding(answer, [source])
        assert formula_check(report).status == "unsupported"
        assert "X=A+B+C" not in safe_partial_answer(answer, report)
    for answer in ("X=A+B [E1]", "`X=A+B` [E1]"):
        assert formula_check(verify_grounding(answer, [source])).status == "source_matched"


def test_warning_only_formula_is_not_authoritative():
    report = verify_grounding("$$X=A+B$$ [E1]", [evidence("[IMAGE_WARNING: verify formula indices]\n$$X=A+B$$")])
    assert formula_check(report).status == "unverified_ocr"


def test_unsupported_claim_without_nonexistent_quote_can_be_removed():
    good = "Доступны два независимых канала передачи."
    bad = "Все каналы работают без электрического питания."
    answer = good + " [E1]\n\n" + bad + " [E1]"
    report = verify_grounding(answer, [evidence(good)], claims=[
        {"claim": bad, "support": "unsupported", "evidence_ids": [], "quotes": []}
    ])
    assert report.needs_repair
    assert bad not in safe_partial_answer(answer, report)
    assert good in safe_partial_answer(answer, report)


def test_judge_cannot_replace_wrong_answer_citation_with_another_real_source():
    claim = "В системе доступны три независимых режима."
    sources = [evidence("Система имеет два режима."), evidence(claim, "E2")]
    report = verify_grounding(claim + " [E1]", sources, claims=[{
        "claim": claim, "support": "supported", "evidence_ids": ["E2"],
        "quotes": [{"evidence_id": "E2", "quote": claim}],
    }])
    assert report.checks[-1].status == "invalid_judge_evidence"
    assert not report.assessment_complete


def test_minus_and_exponent_precedence_is_preserved():
    source = evidence("$$Z=-A^2$$")
    correct = verify_grounding("$$Z=-(A^2)$$ [E1]", [source])
    wrong = verify_grounding("$$Z=(-A)^2$$ [E1]", [source])
    assert formula_check(correct).status == "source_matched"
    assert formula_check(wrong).status == "unsupported"


def test_input_number_presentation_does_not_reject_safe_arithmetic():
    source = evidence("$$I=U/R$$")
    rule = {"formula": "I=U/R", "evidence_ids": ["E1"], "bindings": {"U": 12.0, "R": 4.0}}
    report = verify_grounding("$$I=12/4=3$$ [E1]", [source], question="U=12, R=4.00", calculation_rules=[rule])
    assert formula_check(report).status == "calculated"


def test_safe_partial_removes_only_invalid_links_not_unknown_facts():
    answer = "Доступны два независимых канала передачи данных. [E1,E99]"
    report = verify_grounding(answer, [evidence("Исходный текст для проверки ссылок.")])
    partial = safe_partial_answer(answer, report)
    assert "два независимых канала" in partial
    assert "[E1]" in partial
    assert "E99" not in partial


@pytest.mark.parametrize("tail", ["Пять режимов.", "Нет.", "100 ₽"])
def test_short_unassessed_factual_tail_is_never_certified_away(tail):
    good = "Доступны два независимых канала передачи."
    answer = good + " [E1]\n\n" + tail
    report = verify_grounding(answer, [evidence(good)], claims=[{
        "claim": good, "support": "supported", "evidence_ids": ["E1"],
        "quotes": [{"evidence_id": "E1", "quote": good}],
    }])
    assert report.unassessed_claim_count == 1
    assert not report.assessment_complete


def test_file_metadata_is_a_valid_source_label_and_provenance():
    source = {"evidence_id": "E1", "file": "Новый источник.md", "section_path": "Расчёт", "text": "$$Y=A-B$$"}
    report = verify_grounding("$$Y=A-B$$ [Источник: Новый источник.md — Расчёт]", [source])
    check = formula_check(report)
    assert check.status == "source_matched"
    assert check.details["source_formulas"][0]["source"] == "Новый источник.md"


def test_numeric_rule_builder_uses_only_explicit_exact_symbol_assignments():
    sources = [evidence("$$K=A/B$$")]
    rules, diagnostics = build_numeric_rules("Рассчитай при A=12, B=4.", sources)
    assert diagnostics == []
    assert rules[0]["bindings"] == {"A": "12", "B": "4"}
    assert rules[0]["input_quotes"] == {"A": "A=12", "B": "B=4"}
    assert rules[0]["expected_numeric_result"] == "3"
    report = verify_grounding("$$K=12/4=3$$ [E1]", sources, question="A=12, B=4", calculation_rules=rules)
    assert formula_check(report).status == "calculated"


def test_numeric_builder_does_not_bind_numbers_by_position_or_semantic_guess():
    rules, diagnostics = build_numeric_rules("Всего двенадцать объектов, активных четыре. Числа 12 и 4.", [evidence("$$K=A/B$$")])
    assert rules == []
    assert any(row["reason"] == "missing_explicit_symbol_bindings" for row in diagnostics)
    rules, diagnostics = build_numeric_rules("Количество=12, Доля=4", [evidence("$$K=A/B$$")])
    assert rules == []


def test_numeric_builder_rejects_conflicts_but_accepts_repeated_identical_values():
    sources = [evidence("$$K=A/B$$")]
    rules, diagnostics = build_numeric_rules("A=12, A=15, B=4", sources)
    assert rules == []
    assert any(row["reason"] == "conflicting_explicit_assignments" for row in diagnostics)
    rules, diagnostics = build_numeric_rules("A=12, A=12.0, B=4", sources)
    assert len(rules) == 1


@pytest.mark.parametrize("question", ["A=12 В, B=4 Ом", "A=12кВт, B=4", "A=12%, B=4", "A=12 $, B=4", "A=12+3, B=4", "A=12/3, B=4", "A=12–15, B=4", "A=12e3, B=4", "A=12 (кВт), B=4", "A=12 [м], B=4", "A=12±1, B=4", "A=12:3, B=4", "A=12...15, B=4"])
def test_numeric_builder_never_silently_discards_units_ranges_or_expressions(question):
    rules, diagnostics = build_numeric_rules(question, [evidence("$$K=A/B$$")])
    assert rules == []
    assert any(row["reason"] == "units_or_nonliteral_input_require_explicit_validation" for row in diagnostics)


def test_numeric_builder_cannot_authorize_ocr_formula_or_missing_input():
    rules, diagnostics = build_numeric_rules("A=12, B=4", [evidence("[IMAGE_OCR: scan] $$K=A/B$$ [/IMAGE_OCR]")])
    assert rules == []
    assert diagnostics[0]["reason"] == "unverified_ocr_formula"
    rules, diagnostics = build_numeric_rules("A=12", [evidence("$$K=A/B$$")])
    assert rules == []
    assert diagnostics[0]["symbols"] == ["B"]


def test_numeric_builder_accepts_indexed_symbols_math_delimiters_and_conjunctions():
    sources = [evidence("$$K=A_i/B_i$$")]
    rules, diagnostics = build_numeric_rules("При $A_i=12$ и $B_i=4$ рассчитай K.", sources)
    assert len(rules) == 1
    assert rules[0]["bindings"] == {"A_{i}": "12", "B_{i}": "4"}
    assert diagnostics == []


def test_numeric_builder_rejects_undefined_arithmetic_and_source_unit_metadata():
    rules, diagnostics = build_numeric_rules("A=12, B=0", [evidence("$$K=A/B$$")])
    assert rules == []
    assert diagnostics[0]["reason"] == "unsupported_or_undefined_arithmetic"
    rules, diagnostics = build_numeric_rules("A=12, B=4", [evidence("$$K=A/B$$", units={"A": "V"})])
    assert rules == []
    assert diagnostics[0]["reason"] == "source_units_require_validation"


def test_numeric_builder_does_not_bind_negated_assignment_or_solve_implicit_equation():
    rules, diagnostics = build_numeric_rules("Не A=12, B=4", [evidence("$$K=A/B$$")])
    assert rules == []
    assert any(row["reason"] == "negated_assignment_is_not_an_input" for row in diagnostics)
    rules, diagnostics = build_numeric_rules("A=12, B=4", [evidence("$$A=A+B$$")])
    assert rules == []
    assert diagnostics[0]["reason"] == "unsupported_or_undefined_arithmetic"


@pytest.mark.parametrize("malformed", [42, "supported", {"claim": "Anything"}])
def test_malformed_direct_judge_claim_list_degrades_without_crashing(malformed):
    report = verify_grounding("Есть два канала. [E1]", [evidence("Есть два канала.")], claims=malformed)
    assert not report.assessment_complete
    assert report.unassessed_claim_count == 1
    assert any("invalid shape" in warning for warning in report.warnings)


def test_word_subscript_formula_matches_equivalent_latex_without_losing_indices():
    source = evidence("[MATH: F_sub(покупка,g,i,m)=A_sub(покупка,g,i,m)×B_sub(ставка,i,m)]")
    good = r"$$F_{\text{покупка},g,i,m}=A_{\text{покупка},g,i,m}\cdot B_{\text{ставка},i,m}$$ [E1]"
    assert formula_check(verify_grounding(good, [source])).status == "source_matched"
    for wrong in (
        r"F_{покупка,g,j,m}=A_{покупка,g,i,m}*B_{ставка,i,m}",
        r"F_{покупка,g,i,m}=A_{покупка,g,i,m}+B_{ставка,i,m}",
        r"F_{покупка,g,i,m}=A_{покупка,g,i,m}",
        r"F_{покупка,g,i,m}=A_{сеть,g,i,m}*B_{ставка,i,m}",
    ):
        assert formula_check(verify_grounding("$$" + wrong + "$$ [E1]", [source])).status == "unsupported"


def test_word_superscript_order_is_presentation_not_a_missing_script():
    source = evidence("[MATH: H ^(+) _sub(a,i)=X_sub(a,i)-Y_sub(a,i)]")
    correct = r"$$H_{a,i}^{+}=X_{a,i}-Y_{a,i}$$ [E1]"
    wrong_sign = r"$$H_{a,i}^{-}=X_{a,i}-Y_{a,i}$$ [E1]"
    missing_script = r"$$H_{a,i}=X_{a,i}-Y_{a,i}$$ [E1]"
    assert formula_check(verify_grounding(correct, [source])).status == "source_matched"
    assert formula_check(verify_grounding(wrong_sign, [source])).status == "unsupported"
    assert formula_check(verify_grounding(missing_script, [source])).status == "unsupported"


def test_word_scripts_preserve_nested_parentheses_and_fraction_structure():
    assert normalize_formula("Q_sub(a,(b,c),i)^(2)") == "Q_{a,(b,c),i}^{2}"
    source = evidence("[MATH: K_sub(i)=(A_sub(i))/(B_sub(i))]")
    assert formula_check(verify_grounding(r"$$K_i=\frac{A_i}{B_i}$$ [E1]", [source])).status == "source_matched"
    assert formula_check(verify_grounding(r"$$K_i=\frac{B_i}{A_i}$$ [E1]", [source])).status == "unsupported"


def test_word_formula_works_with_explicit_generic_numeric_binding_helper():
    source = evidence("[MATH: K_sub(i)=A_sub(i)/B_sub(i)]")
    rules, diagnostics = build_numeric_rules("A_i=12, B_i=4", [source])
    assert diagnostics == []
    assert len(rules) == 1
    assert formula_check(verify_grounding("$$K_i=12/4=3$$ [E1]", [source], question="A_i=12, B_i=4", calculation_rules=rules)).status == "calculated"


def test_claim_id_resolves_trusted_markdown_span_without_judge_copying_it():
    answer = "**Контроль канала** выполняется в два этапа. [E1]"
    source = evidence("Контроль канала выполняется в два этапа.")
    report = verify_grounding(answer, [source], claims=[{
        "claim_id": "C1", "support": "supported", "evidence_ids": ["E1"],
        "quotes": [{"evidence_id": "E1", "quote": source["text"]}],
    }])
    check = report.checks[-1]
    assert check.status == "judge_supported"
    assert check.text == answer
    assert (check.start, check.end) == (0, len(answer))
    assert report.assessment_complete


def test_unknown_claim_id_cannot_fall_back_to_judge_owned_text_or_bounds():
    answer = "Существуют два независимых канала связи. [E1]"
    report = verify_grounding(answer, [evidence(answer)], claims=[{
        "claim_id": "C999", "claim": answer, "start": 0, "end": len(answer),
        "support": "supported", "evidence_ids": ["E1"],
        "quotes": [{"evidence_id": "E1", "quote": "Существуют два независимых канала связи."}],
    }])
    assert report.checks[-1].status == "invalid_judge_evidence"
    assert report.checks[-1].start is None
    assert not report.assessment_complete


def test_valid_claim_id_ignores_untrusted_claim_text_and_span_overrides():
    first = "Доступны два независимых канала связи. [E1]"
    second = "У каждого канала имеется резервный вход. [E2]"
    answer = first + "\n\n" + second
    sources = [evidence(first), evidence(second, "E2")]
    report = verify_grounding(answer, sources, claims=[{
        "claim_id": "C1", "claim": second, "start": len(first) + 2, "end": len(answer),
        "support": "unsupported", "evidence_ids": [], "quotes": [],
    }])
    check = report.checks[-1]
    assert check.text == first
    assert (check.start, check.end) == (0, len(first))
    partial = safe_partial_answer(answer, report)
    assert first not in partial
    assert second in partial


def test_missing_claim_ids_leave_prose_and_formula_semantics_unassessed():
    first = "Контроль выполняется в два независимых этапа. [E1]"
    answer = first + "\n\n$$Z=A-B$$ [E1]"
    source = evidence("Контроль выполняется в два независимых этапа.\n$$Z=A-B$$")
    report = verify_grounding(answer, [source], claims=[{
        "claim_id": "C1", "support": "supported", "evidence_ids": ["E1"],
        "quotes": [{"evidence_id": "E1", "quote": "Контроль выполняется в два независимых этапа."}],
    }])
    assert formula_check(report).status == "source_matched"
    assert report.unassessed_claim_count > 0
    assert not report.assessment_complete


def test_claim_segmentation_keeps_multiline_math_and_exact_offsets_intact():
    answer = "## Порядок\n\nПервый этап завершён. Второй этап начат.\n\n$$\nZ_{i}=\\frac{A_i}{B_i}\n\n+ C_i\n$$ [E1]\n\n- Канал проверен. [E1]\n- Резерв подключён. [E1]"
    candidates = extract_answer_claims(answer)
    assert candidates == extract_answer_claims(answer)
    assert all(item["text"] == answer[item["start"]:item["end"]] for item in candidates)
    math_candidates = [item for item in candidates if "Z_{i}" in item["text"]]
    assert len(math_candidates) == 1
    assert "$$\n" in math_candidates[0]["text"] and "\n$$" in math_candidates[0]["text"]
    assert all("## Порядок" not in item["text"] for item in candidates)
    assert len([item for item in candidates if item["text"].startswith("- ")]) == 2


def test_claim_budget_never_caps_answer_or_certifies_omitted_content():
    sentences = [f"Канал номер {number} проходит два этапа. [E1]" for number in range(105)]
    answer = "\n\n".join(sentences)
    candidates = extract_answer_claims(answer, max_claims=1000)
    assert len(candidates) == 100
    claims = [{"claim_id": row["claim_id"], "support": "supported", "evidence_ids": ["E1"],
               "quotes": [{"evidence_id": "E1", "quote": row["text"].replace(" [E1]", "")}]}
              for row in candidates]
    report = verify_grounding(answer, [evidence("\n".join(sentences))], claims=claims)
    assert report.assessed_claim_count == 100
    assert report.unassessed_claim_count >= 5
    assert not report.assessment_complete
    assert "104" in answer


def test_judge_contract_uses_claim_ids_and_repair_keeps_evidence_ids():
    answer = "**Канал** проходит два этапа проверки. [E1]"
    prompt = build_grounding_judge_prompt(answer, [evidence("Канал проходит два этапа проверки.")])
    assert '"claim_id": "C1"' in prompt
    assert '"answer_claims"' in prompt
    assert "Не копируй текст ответа" in prompt
    report = verify_grounding("$$Z=A+B+C$$ [E1]", [evidence("$$Z=A+B$$")])
    repair = report.repair_prompt()
    assert "Сохрани ссылки вида [E1]" in repair
    assert "Не заменяй E-ID названиями документов" in repair


def test_passage_ids_resolve_actual_source_quote_in_trusted_code():
    from evidence_anchors import build_evidence_anchors
    source = evidence("**Контроль канала** выполняется в два этапа.")
    anchor_id = next(iter(build_evidence_anchors([source])))
    answer = "Контроль канала выполняется в два этапа. [E1]"
    report = verify_grounding(answer, [source], claims=[{
        "claim_id": "C1", "support": "supported", "passage_ids": [anchor_id],
        # Ignored: a judge must not override an anchor's text or source.
        "evidence_ids": ["E999"], "quotes": [{"evidence_id": "E999", "quote": "Поддельная цитата."}],
    }])
    check = report.checks[-1]
    assert check.status == "judge_supported"
    assert check.evidence_ids == ["E1"]
    assert check.details["quotes"][0] == {"evidence_id": "E1", "quote": source["text"]}
    assert report.assessment_complete


@pytest.mark.parametrize("passage_ids", [["E1:P999"], ["E999:P1"], ["E1:P1", "E999:P1"], "E1:P1", [None], ["E1:P1"] * 33])
def test_unknown_malformed_or_overbudget_passages_reject_without_quote_fallback(passage_ids):
    source = evidence("Контроль канала выполняется в два этапа.")
    answer = source["text"] + " [E1]"
    report = verify_grounding(answer, [source], claims=[{
        "claim_id": "C1", "support": "supported", "passage_ids": passage_ids,
        "evidence_ids": ["E1"], "quotes": [{"evidence_id": "E1", "quote": source["text"]}],
    }])
    assert report.checks[-1].status == "invalid_judge_evidence"
    assert not report.assessment_complete


def test_passage_mode_still_requires_trusted_claim_id_and_nearby_answer_citation():
    source = evidence("Контроль канала выполняется в два этапа.")
    answer = source["text"] + " [E1]"
    for changed in ({"claim_id": "C999"}, {"claim_id": None}, {"claim": answer}):
        report = verify_grounding(answer, [source], claims=[{
            **changed, "support": "supported", "passage_ids": ["E1:P1"],
        }])
        assert report.checks[-1].status == "invalid_judge_evidence"
    report = verify_grounding(source["text"], [source], claims=[{
        "claim_id": "C1", "support": "supported", "passage_ids": ["E1:P1"],
    }])
    assert report.checks[-1].status == "invalid_judge_evidence"


def test_empty_uncertain_passages_leave_claim_unassessed_not_verified_or_crashed():
    source = evidence("Контроль канала выполняется в два этапа.")
    report = verify_grounding(source["text"] + " [E1]", [source], claims=[{
        "claim_id": "C1", "support": "uncertain", "passage_ids": [],
    }])
    assert report.checks[-1].status == "unassessed"
    assert not report.assessment_complete
    assert not report.needs_repair


def test_passage_judge_payload_does_not_ask_model_to_copy_source_quotes():
    prompt = build_grounding_judge_prompt("Канал проходит два этапа проверки. [E1]", [evidence("Канал проходит два этапа проверки.")])
    assert '"passage_ids":["E1:P1"]' in prompt
    assert '"passages"' in prompt
    assert "Не копируй цитаты" in prompt
    assert '"quotes":[' not in prompt


def test_repair_prompt_never_repeats_full_source_context_or_long_judge_quotes():
    source_context = "БОЛЬШОЙ_ИСХОДНЫЙ_КОНТЕКСТ " * 1000
    report = GroundingReport(checks=[GroundingCheck(
        "formula", "Z=A+B+C", "unsupported", ["E1"], "Нет подтверждения дополнительного слагаемого",
        details={"source_context": source_context,
                 "quotes": [{"evidence_id": "E1", "quote": source_context}],
                 "source_formulas": [{"evidence_id": "E1", "formula": "Z=A+B", "source_context": source_context}]},
    )])
    prompt = report.repair_prompt()
    assert "Z=A+B+C" in prompt
    assert "Z=A+B" in prompt
    assert "БОЛЬШОЙ_ИСХОДНЫЙ_КОНТЕКСТ" not in prompt
    assert len(prompt) < 2000


def test_list_numbers_stay_with_factual_item_and_bold_titles_are_not_claims():
    answer = "**Основное правило**\n\n1. Первый канал проходит проверку. [E1]\n2. Второй канал подключает резерв. [E1]\n\n**Нормативное обоснование**\n\nПодтверждённое описание приведено в источнике. [E1]"
    candidates = extract_answer_claims(answer)
    assert not any(re.fullmatch(r"\d+[.)]", row["text"]) for row in candidates)
    assert candidates[0]["text"].startswith("1. Первый канал")
    assert candidates[1]["text"].startswith("2. Второй канал")
    assert len(candidates) == 3
    # Numeric or sentence-bearing bold statements are not dismissed as titles.
    assert extract_answer_claims("**Цена составляет 100 рублей.** [E1]")


def test_invalid_judge_linkage_is_incomplete_not_a_factual_repair_verdict():
    answer = "Контроль канала выполняется в два этапа. [E1]"
    report = verify_grounding(answer, [evidence(answer)], claims=[{
        "claim_id": "C1", "support": "supported", "passage_ids": ["E1:P999"],
    }])
    assert not report.needs_repair
    assert not report.assessment_complete
    assert report.unassessed_claim_count > 0
    assert any("does not establish a factual error" in warning for warning in report.warnings)
    assert safe_partial_answer(answer, report) == answer


def test_judge_prompt_lists_only_claim_citations_as_allowed_evidence_ids():
    answer = "Контроль канала выполняется в два этапа. [E1]"
    prompt = build_grounding_judge_prompt(answer, [evidence(answer), evidence("Другой независимый документ.", "E2")])
    assert '"cited_evidence_ids": ["E1"]' in prompt
    assert "ТОЛЬКО из его cited_evidence_ids" in prompt
    report = verify_grounding(answer, [evidence(answer), evidence(answer, "E2")], claims=[{
        "claim_id": "C1", "support": "supported", "passage_ids": ["E2:P1"],
    }])
    assert report.checks[-1].status == "invalid_judge_evidence"
    assert not report.assessment_complete


def test_native_formula_catalogue_is_lossless_copy_ready_and_not_ocr():
    raw = "F_sub(покупка,g,i.m)=A_sub(покупка,g,i.m)×Ц_sub(ставка,i,m)"
    sources = [evidence("[MATH: " + raw + "]"), evidence("[IMAGE_OCR: scan]\n[MATH: Z=A+B]\n[/IMAGE_OCR]", "E2")]
    catalogue = native_formula_catalogue(sources)
    assert len(catalogue) == 1
    record = catalogue[0]
    assert record["source_formula"] == raw
    assert record["evidence_id"] == "E1"
    assert record["transcription_only"] is True
    assert record["semantic_applicability_verified"] is False
    assert r"\text{покупка,g,i.m}" in record["latex"]
    assert normalize_formula(record["latex"]) == normalize_formula(raw)
    assert formula_check(verify_grounding(record["display_latex"] + " [E1]", sources)).status == "source_matched"


@pytest.mark.parametrize("raw", ["K_sub(i)=(A_sub(i))/(B_sub(i))", "H^(+)_sub(a,i)=X_sub(a,i)-Y_sub(a,i)", "H_sub(a,(b,c),i)^(2)=X_sub(i)+Y_sub(i)"])
def test_native_transcription_preserves_ratio_direction_indices_superscripts_and_signs(raw):
    catalogue = native_formula_catalogue([evidence("[MATH: " + raw + "]")])
    assert len(catalogue) == 1
    assert normalize_formula(catalogue[0]["latex"]) == normalize_formula(raw)
    if "/" in raw:
        assert r"\frac{" in catalogue[0]["latex"]


def test_native_catalogue_does_not_repair_broken_notation_or_guess_ocr():
    for text in ("[MATH: Z=A*,+,-B]", "[MATH: Z_sub(a=A+B]", "[MATH: Z=A+B)]", "[MATH: Z=A+[B]]", "[IMAGE_WARNING: unverified formula]\n$$Z=A+B$$", "$$Z=A+B$$"):
        assert native_formula_catalogue([evidence(text)]) == []


@pytest.mark.parametrize("warning_first", [False, True])
def test_unread_image_warning_does_not_taint_independent_native_word_math(warning_first):
    raw = "F_sub(a,i)=X_sub(a,i)-Y_sub(a,i)"
    native = "[MATH: " + raw + "]"
    unread = "[IMAGE_UNREAD: media/image1.png] [IMAGE_WARNING: visual extraction failed (TimeoutError)]"
    source = evidence("\n\n".join([unread, native] if warning_first else [native, unread]))
    catalogue = native_formula_catalogue([source])
    assert len(catalogue) == 1
    assert catalogue[0]["source_formula"] == raw
    assert normalize_formula(catalogue[0]["latex"]) == normalize_formula(raw)
    report = verify_grounding(catalogue[0]["display_latex"] + " [E1]", [source])
    assert formula_check(report).status == "source_matched"
    assert not report.assessment_complete  # Transcription is not semantic proof.


@pytest.mark.parametrize("ocr_end", ["[/IMAGE_OCR]", ""])
def test_native_marker_inside_ocr_remains_untrusted_even_with_warning(ocr_end):
    source = evidence("[IMAGE_OCR: media/image1.png]\n[MATH: F=A+B]\n" + ocr_end +
                      "\n[IMAGE_WARNING: verify transcription]")
    assert native_formula_catalogue([source]) == []
    assert formula_check(verify_grounding("$$F=A+B$$ [E1]", [source])).status == "unverified_ocr"


def test_closed_ocr_and_its_warning_do_not_taint_following_native_formula():
    source = evidence("[IMAGE_OCR: media/image1.png]\n[MATH: Z=A+B]\n[/IMAGE_OCR]\n"
                      "[IMAGE_WARNING: verify transcription]\n\n[MATH: F=A-B]")
    catalogue = native_formula_catalogue([source])
    assert [row["source_formula"] for row in catalogue] == ["F=A-B"]


def test_explicit_evidence_wide_image_warning_still_blocks_native_catalogue():
    source = {**evidence("[MATH: F=A+B]"), "image_warning": "unverified image transcription"}
    assert native_formula_catalogue([source]) == []


@pytest.mark.parametrize("text", [
    "[MATH: L_sub(batch)=A_sub(batch)] [[MATHML_REF:abc]] + [MATH: C_sub(batch)] - [MATH: D_sub(batch)]",
    "[MATH: L_sub(batch)=A_sub(batch)] [MATH: +C_sub(batch)-D_sub(batch)]",
    "[MATH: L_sub(batch)=A_sub(batch)+] [MATH: C_sub(batch)-D_sub(batch)]",
    "L_sub(batch)=A_sub(batch) [MATH: +C_sub(batch)-D_sub(batch)]",
])
def test_native_run_chain_is_one_equation_and_cannot_certify_its_prefix(text):
    source = evidence(text)
    catalogue = native_formula_catalogue([source])
    assert len(catalogue) == 1
    expected = r"L_{batch}=A_{batch}+C_{batch}-D_{batch}"
    assert normalize_formula(catalogue[0]["latex"]) == normalize_formula(expected)
    assert formula_check(verify_grounding("$$" + expected + "$$ [E1]", [source])).status == "source_matched"
    for wrong in [r"L_{batch}=A_{batch}", r"L_{batch}=A_{batch}+C_{batch}",
                  r"L_{batch}=A_{batch}+C_{batch}+D_{batch}",
                  r"L_{batch}=A_{batch}+C_{other}-D_{batch}"]:
        report = verify_grounding("$$" + wrong + "$$ [E1]", [source])
        assert formula_check(report).status == "unsupported"
        assert wrong not in safe_partial_answer("$$" + wrong + "$$ [E1]", report)


def test_numeric_rule_uses_entire_native_chain_not_first_rhs_fragment():
    source = evidence("[MATH: L=A] + [MATH: B] - [MATH: C]")
    rules, diagnostics = build_numeric_rules("A=9, B=4, C=2", [source])
    assert not diagnostics
    assert len(rules) == 1 and rules[0]["expected_numeric_result"] == "11"
    assert set(rules[0]["bindings"]) == {"A", "B", "C"}
    report = verify_grounding("$$L=9+4-2=11$$ [E1]", [source], question="A=9, B=4, C=2", calculation_rules=rules)
    assert formula_check(report).status == "calculated"


@pytest.mark.parametrize("separator", ["\n\n", "; ", " with an independent rule "])
def test_native_chains_never_cross_paragraph_or_prose_boundaries(separator):
    source = evidence("[MATH: R=A]" + separator + "[MATH: S=B+C]")
    assert [row["source_formula"] for row in native_formula_catalogue([source])] == ["R=A", "S=B+C"]


def test_whitespace_separated_independent_equalities_remain_independent():
    source = evidence("[MATH: R=A] [[MATHML_REF:abc]] [MATH: S=B+C]")
    assert [row["source_formula"] for row in native_formula_catalogue([source])] == ["R=A", "S=B+C"]


@pytest.mark.parametrize("text", ["[MATH: T=A] [MATH: B]", "[MATH: T=A] +", "[MATH: T=A+]"])
def test_ambiguous_or_dangling_native_runs_are_not_silently_completed(text):
    source = evidence(text)
    assert native_formula_catalogue([source]) == []
    rules, diagnostics = build_numeric_rules("A=7, B=3", [source])
    assert rules == []
    assert diagnostics[0]["reason"] == "ambiguous_native_formula_boundary"
    answer = "$$T=A$$ [E1]"
    report = verify_grounding(answer, [source])
    assert formula_check(report).status == "unassessed"
    assert not report.needs_repair
    assert not report.assessment_complete
    assert safe_partial_answer(answer, report) == answer


def test_linked_native_chain_cannot_cross_an_ocr_boundary_or_authorize_ocr_terms():
    source = evidence("[MATH: T=A] + [IMAGE_OCR: scan]\n[MATH: B=C]\n[/IMAGE_OCR]")
    assert native_formula_catalogue([source]) == []
    report = verify_grounding("$$B=C$$ [E1]", [source])
    assert formula_check(report).status == "unverified_ocr"


def test_an_unparsed_literal_equality_cannot_promote_its_native_continuation():
    source = evidence("R=A + [MATH: T=B]")
    assert native_formula_catalogue([source]) == []
    report = verify_grounding("$$T=B$$ [E1]", [source])
    assert formula_check(report).status == "unassessed"
    assert not report.assessment_complete


def test_unicode_sum_and_latex_sum_are_exact_presentation_equivalents():
    raw = "H_sub(batch)=∑_sub(k=1)^(N)\u200a(a_sub(k)-b_sub(k))"
    source = evidence("[MATH: " + raw + "]")
    answer = r"$$H_{batch}=\sum_{k=1}^{N}(a_k-b_k)$$ [E1]"
    catalogue = native_formula_catalogue([source])
    assert len(catalogue) == 1 and r"\sum" in catalogue[0]["latex"]
    assert normalize_formula(catalogue[0]["latex"]) == normalize_formula(raw)
    report = verify_grounding(answer, [source])
    assert formula_check(report).status == "source_matched"
    assert not report.needs_repair
    assert safe_partial_answer(answer, report) == answer


@pytest.mark.parametrize("changed", [
    r"H_{batch}=\sum_{k=2}^{N}(a_k-b_k)",
    r"H_{batch}=\sum_{k=1}^{M}(a_k-b_k)",
    r"H_{batch}=\sum_{k=1}^{N}(a_j-b_k)",
    r"H_{batch}=\sum_{k=1}^{N}(a_k+b_k)",
    r"H_{batch}=\sum_{k=1}^{N}(a_k)",
])
def test_sum_bounds_indices_signs_and_terms_are_not_normalized_away(changed):
    source = evidence("[MATH: H_sub(batch)=∑_sub(k=1)^(N)(a_sub(k)-b_sub(k))]")
    answer = "$$" + changed + "$$ [E1]"
    report = verify_grounding(answer, [source])
    assert formula_check(report).status == "unassessed"
    assert not report.assessment_complete


def test_unproved_sum_equivalence_is_not_forced_false_or_deleted():
    source = evidence("[MATH: H=∑_sub(k=1)^(N)a_sub(k)]")
    answer = r"$$H=\sum_{k=1}^{N}(a_k)$$ [E1]"
    claim = {"claim_id": "C1", "support": "supported", "passage_ids": ["E1:P1"]}
    report = verify_grounding(answer, [source], claims=[claim])
    assert formula_check(report).status == "unassessed"
    assert not report.needs_repair
    assert not report.assessment_complete  # A prose judge cannot bypass unknown math.
    assert safe_partial_answer(answer, report) == answer


@pytest.mark.parametrize("text", [
    "[MATH: F=A] ^ [MATH: 2]",
    "[MATH: F=A^] [MATH: 2]",
    "[MATH: F=A] [MATH: ^(2)]",
    "F=A ^ [MATH: 2]",
])
def test_native_power_continuation_preserves_exponent_and_numeric_result(text):
    source = evidence(text)
    catalogue = native_formula_catalogue([source])
    assert len(catalogue) == 1
    assert formula_check(verify_grounding("$$F=A^2$$ [E1]", [source])).status == "source_matched"
    assert formula_check(verify_grounding("$$F=A$$ [E1]", [source])).status == "unsupported"
    rules, diagnostics = build_numeric_rules("A=3", [source])
    assert not diagnostics
    assert len(rules) == 1 and rules[0]["expected_numeric_result"] == "9"
    good = verify_grounding("$$F=3^2=9$$ [E1]", [source], question="A=3", calculation_rules=rules)
    bad = verify_grounding("$$F=3$$ [E1]", [source], question="A=3", calculation_rules=rules)
    assert formula_check(good).status == "calculated"
    assert formula_check(bad).status == "unsupported"


@pytest.mark.parametrize("connector", ["±", "∓", "⊕", r"\pm", "\u200b+", "_sub(i)"])
def test_unknown_native_connector_cannot_certify_a_prefix_or_numeric_rule(connector):
    source = evidence("[MATH: F=A] " + connector + " [MATH: B]")
    assert native_formula_catalogue([source]) == []
    rules, diagnostics = build_numeric_rules("A=3, B=2", [source])
    assert rules == []
    assert diagnostics[0]["reason"] == "ambiguous_native_formula_boundary"
    answer = "$$F=A$$ [E1]"
    report = verify_grounding(answer, [source])
    assert formula_check(report).status == "unassessed"
    assert not report.needs_repair
    assert not report.assessment_complete
    assert safe_partial_answer(answer, report) == answer


def test_actual_docx_paragraph_delimiter_keeps_independent_equations_separate():
    from docx_parser import DocxBlock, _render_blocks

    text = _render_blocks([DocxBlock("paragraph", "[MATH: R=A]"),
                           DocxBlock("paragraph", "[MATH: S=B+C]")])
    assert text == "[MATH: R=A]\n\n[MATH: S=B+C]"
    source = evidence(text)
    assert [row["source_formula"] for row in native_formula_catalogue([source])] == ["R=A", "S=B+C"]
    report = verify_grounding("$$R=A+B$$ [E1]", [source])
    assert formula_check(report).status == "unsupported"


@pytest.mark.parametrize("source_formula,answer_formula", [
    ("H=∑_sub(k=1)^(N)a_sub(k)", r"\sum_{k=1}^{N}a_k=H"),
    ("H=∑_sub(k=1)^(N)a_sub(k)", r"\sum_{k=1}^{N}(a_k)=H"),
    ("∑_sub(k=1)^(N)a_sub(k)=H", r"H=\sum_{k=1}^{N}a_k"),
])
def test_unproved_complex_equality_with_swapped_sides_remains_unassessed(source_formula, answer_formula):
    source = evidence("[MATH: " + source_formula + "]")
    answer = "$$" + answer_formula + "$$ [E1]"
    claim = {"claim_id": "C1", "support": "supported", "passage_ids": ["E1:P1"]}
    report = verify_grounding(answer, [source], claims=[claim])
    assert formula_check(report).status == "unassessed"
    assert not report.needs_repair
    assert not report.assessment_complete
    assert safe_partial_answer(answer, report) == answer


@pytest.mark.parametrize("tail", ["+0", "-0", "+(2-2)", "*1", "/1"])
def test_neutral_arithmetic_tail_of_unknown_notation_is_not_a_proven_error(tail):
    source = evidence("[MATH: H=∑_sub(k=1)^(N)a_sub(k)]")
    answer = r"$$H=\sum_{k=1}^{N}a_k" + tail + "$$ [E1]"
    report = verify_grounding(answer, [source])
    assert formula_check(report).status == "unassessed"
    assert not report.needs_repair
    assert not report.assessment_complete
    assert safe_partial_answer(answer, report) == answer


@pytest.mark.parametrize("tail", ["+1", "-2", "+B", "*2", "/2"])
def test_non_neutral_extra_or_missing_continuation_is_still_rejected(tail):
    original = r"H=\sum_{k=1}^{N}a_k"
    for source_formula, answer_formula in [(original, original + tail), (original + tail, original)]:
        source = evidence("$$" + source_formula + "$$")
        answer = "$$" + answer_formula + "$$ [E1]"
        report = verify_grounding(answer, [source])
        assert formula_check(report).status == "unsupported"
        assert report.needs_repair
        assert answer_formula not in safe_partial_answer(answer, report)


@pytest.mark.parametrize("line_break", ["\n", "\n\n", "\r\n", "\r\n\r\n"])
def test_word_line_breaks_inside_one_math_marker_preserve_the_whole_formula(line_break):
    raw = "H_sub(batch)=∑_sub(k=1)^(N)" + line_break + "(a_sub(k)-b_sub(k))"
    original = "[MATH: " + raw + "]"
    source = evidence(original)
    catalogue = native_formula_catalogue([source])
    expected = r"H_{batch}=\sum_{k=1}^{N}(a_k-b_k)"
    assert len(catalogue) == 1 and catalogue[0]["source_formula"] == raw
    assert normalize_formula(catalogue[0]["latex"]) == normalize_formula(expected)
    report = verify_grounding("$$" + expected + "$$ [E1]", [source])
    assert formula_check(report).status == "source_matched"
    assert not report.needs_repair
    assert source["text"] == original


@pytest.mark.parametrize("line_break", ["\n", "\n\n"])
def test_selected_raw_multiline_word_formula_remains_available_to_catalogue(line_break):
    from types import SimpleNamespace
    from evidence_selection import SectionEvidenceIndex

    raw = "H=∑_sub(k=1)^(N)" + line_break + "a_sub(k)"
    text = "Расчет нагрузки выполняется по правилу.\n\n[MATH: " + raw + "]"
    file = "Лаборатория-Альфа.md"
    node = SimpleNamespace(text=text, metadata={"file_name": file, "section_path": "Расчет нагрузки"})
    selected = SectionEvidenceIndex([node]).select_v2(
        "Как рассчитывается нагрузка?", [{"file": file, "text": text, "section_path": "Расчет нагрузки"}],
    )
    assert len(selected) == 1 and selected[0]["text"] == text
    catalogue = native_formula_catalogue([evidence(selected[0]["text"])])
    assert len(catalogue) == 1 and catalogue[0]["source_formula"] == raw
    answer = r"$$H=\sum_{k=1}^{N}a_k$$ [E1]"
    assert formula_check(verify_grounding(answer, [evidence(selected[0]["text"])])).status == "source_matched"


def test_multiline_native_math_keeps_numeric_terms_and_external_paragraph_boundaries():
    source = evidence("[MATH: F=A+\n\nB] - [MATH: C]\n\n[MATH: G=D]")
    assert [row["source_formula"] for row in native_formula_catalogue([source])] == ["F=A+\n\nB - C", "G=D"]
    rules, _ = build_numeric_rules("A=7, B=3, C=2, D=4", [source])
    assert next(rule for rule in rules if rule["formula"].startswith("F="))["expected_numeric_result"] == "8"
    answer = "$$F=7+3-2=8$$ [E1]"
    report = verify_grounding(answer, [source], question="A=7, B=3, C=2, D=4", calculation_rules=rules)
    assert formula_check(report).status == "calculated"
    assert formula_check(verify_grounding("$$F=A$$ [E1]", [source])).status == "unsupported"
    assert formula_check(verify_grounding("$$F=A+B-C+D$$ [E1]", [source])).status == "unsupported"


@pytest.mark.parametrize("text,answer_formula", [
    ("[MATH: F=A+\n\n]", "F=A"),
    ("[MATH: F=A\n+B] ± [MATH: C]", "F=A+B"),
    ("[MATH: F=A\n\n+B] [MATH: C]", "F=A+B"),
])
def test_multiline_math_does_not_complete_dangling_or_ambiguous_continuations(text, answer_formula):
    source = evidence(text)
    assert native_formula_catalogue([source]) == []
    rules, diagnostics = build_numeric_rules("A=7, B=3, C=2", [source])
    assert rules == [] and diagnostics[0]["reason"] == "ambiguous_native_formula_boundary"
    answer = "$$" + answer_formula + "$$ [E1]"
    report = verify_grounding(answer, [source])
    assert formula_check(report).status == "unassessed"
    assert not report.needs_repair
    assert safe_partial_answer(answer, report) == answer

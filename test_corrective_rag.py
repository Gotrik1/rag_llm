"""Behavioral contracts for bounded corrective search; novel synthetic corpus."""
import json

import pytest

from corrective_rag import make_plan, run_corrective


QUESTION = "Какие этапы контроля проходят измерительные каналы?"
FACT = "Каждый измерительный канал проходит два этапа контроля."


def fragment(text=FACT, name="Проверка каналов.md", **extra):
    return {"file": name, "source": name, "section_path": "Основной порядок", "text": text, **extra}


def coverage(quote=FACT, identifier="E1", *, queries=None, applicable=True):
    return {"coverage": [{"requirement_id": "R1", "evidence_ids": [identifier],
                           "quotes": [{"evidence_id": identifier, "quote": quote}],
                           "applicable": applicable}],
            "missing_queries": queries or [], "conflicts": []}


def claim(quote=FACT, identifier="E1", text=FACT, support="supported"):
    return {"claim": text, "support": support, "evidence_ids": [identifier],
            "quotes": [{"evidence_id": identifier, "quote": quote}]}


class Model:
    def __init__(self, *, answers=None, reviews=None, claims=None, plan=None, judge_error=None):
        self.answers = answers or [FACT + " [E1]"]
        self.reviews = reviews or [coverage()]
        self.claims = claims if claims is not None else [claim()]
        self.plan = plan or {"requirements": ["Описать этапы контроля"], "queries": [QUESTION]}
        self.judge_error = judge_error
        self.calls = []
        self.answer_count = 0
        self.review_count = 0

    def __call__(self, prompt, stage):
        self.calls.append((stage, prompt))
        if stage == "planning":
            return json.dumps(self.plan, ensure_ascii=False)
        if stage == "answer":
            index = min(self.answer_count, len(self.answers) - 1)
            self.answer_count += 1
            return self.answers[index]
        if "Проверь доказательность ответа" in prompt:
            if self.judge_error:
                raise self.judge_error
            return json.dumps({"claims": self.claims}, ensure_ascii=False)
        index = min(self.review_count, len(self.reviews) - 1)
        self.review_count += 1
        return json.dumps(self.reviews[index], ensure_ascii=False)


class Retrieval:
    def __init__(self, *batches):
        self.batches = batches or ([fragment()],)
        self.calls = []

    def __call__(self, question, queries, requirements, trace):
        index = min(len(self.calls), len(self.batches) - 1)
        self.calls.append(list(queries))
        trace["synthetic_test"] = True
        return list(self.batches[index])


def test_valid_quote_coverage_stops_search_early_and_assesses_claims():
    model, retrieval = Model(), Retrieval()
    result = run_corrective(QUESTION, QUESTION, retrieve=retrieval, complete=model)
    assert len(retrieval.calls) == 1
    assert model.answer_count == 1
    assert result.trace["coverage_complete"] is True
    assert result.trace["verification"]["initial"]["assessment_complete"] is True
    assert result.answer == FACT + " [E1]"


def test_planner_json_contract_is_not_user_answer_style():
    model = Model(plan={'requirements':['Этапы контроля'],'queries':[QUESTION],'style':'JSON'})
    result = run_corrective(QUESTION,QUESTION,retrieve=Retrieval(),complete=model)
    assert result.trace['plan']['style'] == ''
    assert '"style": "JSON"' not in next(p for s,p in model.calls if s=='answer')


def test_response_exclusion_does_not_become_a_missing_document_fact():
    question = 'Опиши этапы контроля канала. Температуру не рассматривай.'
    model = Model(plan={
        'requirements':['Этапы контроля канала','Исключение информации о температуре'],
        'queries':['Этапы контроля канала','Не описывать температуру'],
        'exclusions':['температура'],
    })
    plan = make_plan(question,model)
    assert plan.requirements == ('Этапы контроля канала',)
    assert len(plan.exclusions) == 2
    assert 'температура' in plan.exclusions
    assert 'Не описывать температуру' not in plan.queries
    result = run_corrective(question,question,retrieve=Retrieval(),complete=model)
    assert result.trace['coverage_complete']
    assert len(result.trace['rounds']) == 1


@pytest.mark.parametrize('requirement',[
    'Исключения из общего правила контроля',
    'Условия, при которых каналы не включаются в контроль',
    'Какие каналы не проходят контроль',
])
def test_factual_negations_and_exceptions_remain_search_requirements(requirement):
    model = Model(plan={'requirements':[requirement],'queries':['Контроль каналов']})
    assert make_plan('Когда каналы не проходят контроль?',model).requirements == (requirement,)


def test_response_exclusion_does_not_drop_a_required_factual_exception():
    requirement = 'Исключения из общего порядка контроля канала'
    model = Model(plan={'requirements':[requirement],'queries':['Исключения из порядка контроля']})
    plan = make_plan('Опиши исключения из порядка контроля. Температуру не описывай.',model)
    assert plan.requirements == (requirement,)


def test_incomplete_direct_child_family_cannot_be_certified_as_complete_by_model():
    data = fragment(evidence_family_id='f',evidence_family={'id':'f','file':'Проверка каналов.md',
        'expected_children':['Основной порядок','Другой метод'],'total_children':2,'complete':False})
    result = run_corrective(QUESTION,QUESTION,retrieve=Retrieval([data]),complete=Model())
    assert result.trace['coverage_complete'] is False
    assert result.trace['rounds'][0]['coverage']['conflicts']
    assert result.answer == FACT+' [E1]'


def test_string_answer_envelope_is_decoded_before_claim_and_formula_checks():
    model = Model(answers=['```json\n'+json.dumps({'answer':FACT+' [E1]','sources':['E1']},ensure_ascii=False)+'\n```'])
    result = run_corrective(QUESTION,QUESTION,retrieve=Retrieval(),complete=model)
    assert result.answer == FACT+' [E1]'
    assert result.trace['verification']['initial']['assessment_complete']
    assert model.answer_count == 1


def test_nested_service_json_gets_one_repair_and_is_never_shown_raw():
    invalid = json.dumps({'answer':{'main_rule':FACT+' [E1]'}},ensure_ascii=False)
    model = Model(answers=[invalid,invalid])
    result = run_corrective(QUESTION,QUESTION,retrieve=Retrieval(),complete=model)
    assert model.answer_count == 2
    assert 'main_rule' not in result.answer
    assert '"answer"' not in result.answer
    assert result.trace['verification']['partial_answer']


@pytest.mark.parametrize("bad_id,bad_quote", [("E99", FACT), ("E1", "Измерительные каналы не требуют никакого контроля.")])
def test_unknown_id_or_fabricated_quote_cannot_certify_coverage_or_answer(bad_id, bad_quote):
    model = Model(reviews=[coverage(bad_quote, bad_id)], claims=[claim(bad_quote, bad_id)])
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval(), complete=model)
    assert result.trace["coverage_complete"] is False
    assert result.trace["verification"]["initial"]["assessment_complete"] is False
    assert any(row["status"] == "invalid_judge_evidence" for row in result.trace["verification"]["initial"]["checks"])


def test_at_most_two_targeted_additional_queries_even_if_model_requests_many():
    missing = coverage("Несуществующая цитата, не подтверждённая документом.", queries=[
        "Определение контрольного этапа", "Контрольные процедуры канала",
        "История разработки канала", "Неограниченное расширение поиска",
    ])
    model, retrieval = Model(reviews=[missing], claims=[]), Retrieval()
    run_corrective(QUESTION, QUESTION, retrieve=retrieval, complete=model, max_additional_rounds=100)
    assert len(retrieval.calls) <= 3
    extra = [query for batch in retrieval.calls[1:] for query in batch]
    assert len(extra) <= 2
    assert len({query.casefold().strip() for query in extra}) == len(extra)


def test_query_deduplication_including_case_variants_within_one_round():
    model = Model(plan={"requirements": ["Этапы контроля"], "queries": [QUESTION, QUESTION.upper(), "  " + QUESTION + "  "]})
    retrieval = Retrieval()
    run_corrective(QUESTION, QUESTION, retrieve=retrieval, complete=model)
    first = [query.casefold().strip() for query in retrieval.calls[0]]
    assert len(first) == len(set(first))


def test_source_passage_addresses_bind_coverage_without_model_retyping_quotes():
    review = {'coverage':[{'requirement_id':'R1','passage_ids':['E1:P1'],'applicable':True}]}
    result = run_corrective(QUESTION,QUESTION,retrieve=Retrieval(),complete=Model(reviews=[review]))
    assert result.trace['coverage_complete'] is True


def test_unknown_source_passage_cannot_certify_coverage():
    review = {'coverage':[{'requirement_id':'R1','passage_ids':['E1:P999'],'applicable':True}]}
    result = run_corrective(QUESTION,QUESTION,retrieve=Retrieval(),complete=Model(reviews=[review]))
    assert result.trace['coverage_complete'] is False


def test_identical_evidence_is_not_reassessed_for_every_followup_search():
    missing = {'coverage':[],'missing_queries':['Два этапа контроля','Прямое определение этапа']}
    model, retrieval = Model(reviews=[missing]),Retrieval()
    result = run_corrective(QUESTION,QUESTION,retrieve=retrieval,complete=model)
    assert len(retrieval.calls)==3
    assert model.review_count==1
    assert [row['assessment_reused'] for row in result.trace['rounds']]==[False,True,True]


def test_evidence_identifiers_stay_stable_when_new_sources_arrive():
    first = fragment(FACT, "Каналы.md")
    second = fragment("Калибровка выполняется до запуска канала.", "Калибровка.md")
    model = Model(reviews=[coverage("Цитата, которой нет в исходном тексте.", queries=["Требования к калибровке"]), coverage()], claims=[])
    retrieval = Retrieval([first], [second])
    snapshots = []
    def select(question, pool, requirements):
        snapshots.append({item["file"]: item["evidence_id"] for item in pool})
        return pool
    result = run_corrective(QUESTION, QUESTION, retrieve=retrieval, complete=model, select=select)
    assert len(snapshots) >= 2
    assert snapshots[0]["Каналы.md"] == snapshots[1]["Каналы.md"]
    assert {item["file"]: item["evidence_id"] for item in result.context}["Каналы.md"] == snapshots[0]["Каналы.md"]


def test_wrong_formula_gets_one_repair_then_only_bad_span_is_removed():
    source = fragment(FACT + "\n$$D=A-B$$")
    answer = FACT + " [E1]\n\n$$D=A+B+G$$ [E1]"
    model = Model(answers=[answer, answer], claims=[claim()])
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval([source]), complete=model)
    assert model.answer_count == 2
    assert "after_repair" in result.trace["verification"]
    assert result.trace["verification"]["partial_answer"] is True
    assert FACT in result.answer
    assert "D=A+B+G" not in result.answer
    assert "Не удалось подтвердить часть ответа" in result.answer


def test_planner_cannot_invent_category_for_general_question():
    model = Model(plan={"requirements": ["Рассчитать 3 ЦК"], "queries": ["Стоимость по 3 ЦК"]})
    question = "Из каких компонентов складывается цена?"
    plan = make_plan(question, model)
    assert plan.fallback is True
    assert plan.requirements == (question,)
    assert plan.queries == (question,)


def test_planner_cannot_hide_invented_category_behind_words():
    model = Model(plan={"requirements": ["Объяснить третью ценовую категорию"], "queries": ["Цена для третьей ценовой категории"]})
    question = "Из каких компонентов складывается цена?"
    plan = make_plan(question, model)
    assert plan.fallback is True
    assert plan.queries == (question,)


def test_planner_receives_catalog_as_hints_not_full_documents():
    model = Model()
    hints = [{'title':'Меры устойчивости сети.md','domains':['engineering']}]
    make_plan(QUESTION,model,corpus_hints=hints)
    prompt = model.calls[0][1]
    assert 'Меры устойчивости сети.md' in prompt
    assert 'сами по себе не доказывают факты' in prompt


@pytest.mark.parametrize('question', ['Сравни 3 и 6 ЦК','Сравни третью и шестую ценовые категории'])
def test_plan_keeps_explicit_comparison_without_false_invented_category(question):
    model = Model(plan={'requirements':['Сравнить 3 ЦК и 6 ЦК'],'queries':['3 ЦК','6 ЦК']})
    assert make_plan(question,model).fallback is False


def test_no_evidence_never_generates_an_invented_answer():
    model = Model(answers=["Все каналы работают без контроля и электрического питания."],
                  reviews=[{"coverage": [], "missing_queries": [], "conflicts": []}], claims=[])
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval([]), complete=model)
    assert model.answer_count == 0
    assert "без контроля и электрического питания" not in result.answer
    assert result.context == []
    assert result.trace["coverage_complete"] is False


def test_judge_outage_does_not_certify_answer_or_discard_useful_prose():
    model = Model(judge_error=RuntimeError("Synthetic judge outage"))
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval(), complete=model)
    assert result.trace["verification"]["initial"]["assessment_complete"] is False
    assert FACT in result.answer


def test_judge_timeout_is_degraded_verification_not_a_crashed_session():
    model = Model(judge_error=TimeoutError("Synthetic judge timeout"))
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval(), complete=model)
    assert result.trace["verification"]["initial"]["assessment_complete"] is False
    assert FACT in result.answer


def test_user_answer_length_is_not_artificially_capped_by_orchestrator():
    long_answer = (FACT + " [E1]\n\n") * 600
    model = Model(answers=[long_answer], claims=[])
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval(), complete=model)
    assert len(result.answer) == len(long_answer.strip())
    assert model.answer_count == 1


def test_short_unassessed_factual_tail_prevents_whole_answer_certification():
    model = Model(answers=[FACT + " [E1]\n\nПять режимов."], claims=[claim()])
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval(), complete=model)
    assert result.trace["verification"]["initial"]["assessment_complete"] is False

"""Behavioral contracts for bounded corrective search; novel synthetic corpus."""
import json

import pytest

from corrective_rag import make_plan, run_corrective
from search_focus import SearchOperation


QUESTION = "Какие этапы контроля проходят измерительные каналы?"
FACT = "Каждый измерительный канал проходит два этапа контроля."


def fragment(text=FACT, name="Проверка каналов.md", **extra):
    return {"file": name, "source": name, "section_path": "Основной порядок", "text": text, **extra}


def coverage(quote=FACT, identifier="E1", *, queries=None, applicable=True):
    return {"coverage": [{"requirement_id": "R1", "evidence_ids": [identifier],
                           "quotes": [{"evidence_id": identifier, "quote": quote}],
                           "applicable": applicable}],
            "task_alignment": {"status":"matched", "passage_ids":[f"{identifier}:P1"]},
            "missing_queries": queries or [], "conflicts": []}


def claim(quote=FACT, identifier="E1", text=FACT, support="supported"):
    return {"claim_id":"C1", "task_relevance":"matched",
            "claim": text, "support": support, "evidence_ids": [identifier],
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
        # This positive fake explicitly assesses the original task. Tests for
        # missing/malformed authority judgments use the dedicated strict suite.
        review = {"task_alignment":{"status":"matched","passage_ids":["E1:P1"]},
                  **self.reviews[index]}
        return json.dumps(review, ensure_ascii=False)


class Retrieval:
    def __init__(self, *batches):
        self.batches = batches or ([fragment()],)
        self.calls = []
        self.focuses = []
        self.constraints = []

    def __call__(self, question, queries, requirements, trace, *, search_focus, constraint_question):
        index = min(len(self.calls), len(self.batches) - 1)
        self.calls.append(list(queries))
        self.focuses.append(search_focus)
        self.constraints.append(constraint_question)
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


def test_missing_definition_cannot_be_certified_by_real_quote_or_model_review():
    model = Model()
    source = fragment(selection_signals={
        'definition_closure_incomplete':True,'missing_definition_symbols':['c']})
    result = run_corrective(QUESTION,QUESTION,retrieve=Retrieval([source]),complete=model)
    assert result.trace['coverage_complete'] is False
    assert any('определения' in text for text in result.trace['rounds'][0]['coverage']['conflicts'])
    assert 'definition_closure_incomplete' in next(p for s,p in model.calls if s=='answer')


def test_changed_definition_limits_invalidate_request_local_assessment_reuse():
    calls = []
    model = Model(reviews=[coverage(queries=['Расшифровка обозначений']), coverage()])
    def select(question,pool,requirements,*,search_focus,constraint_question):
        calls.append(1)
        return [{**pool[0], 'selection_signals':{
            'definition_closure_incomplete':len(calls)==1,
            'missing_definition_symbols':['c'] if len(calls)==1 else []}}]
    result = run_corrective(QUESTION,QUESTION,retrieve=Retrieval(),complete=model,select=select)
    assert len(result.trace['rounds']) == 2
    assert [row['assessment_reused'] for row in result.trace['rounds']] == [False,False]
    assert result.trace['coverage_complete'] is True


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


def test_resolved_intent_drives_ranking_without_losing_verbatim_query_or_exclusions():
    question = 'Теперь убери обсуждение температуры. Меня интересует тот же порядок контроля.'
    envelope = 'Тема диалога — вопрос пользователя: '+QUESTION+'\nТекущий запрос пользователя: '+question
    model = Model(plan={'canonical_question':QUESTION,'requirements':['Этапы контроля'],
                        'queries':[QUESTION],'exclusions':['температура']})
    searches, selections = [],[]

    def retrieve(subject, queries, requirements, trace, *, search_focus, constraint_question):
        searches.append((subject,queries))
        assert search_focus is None
        assert constraint_question == envelope
        return [fragment()]

    def select(subject, pool, requirements, *, search_focus, constraint_question):
        selections.append(subject)
        assert search_focus is None
        assert constraint_question == envelope
        return pool

    result = run_corrective(question,envelope,retrieve=retrieve,select=select,complete=model)
    assert searches[0][0] == QUESTION+'; не рассматривай температура'
    assert envelope in searches[0][1]
    assert selections == [searches[0][0]]
    assert result.trace['plan']['canonical_question'] == QUESTION
    assert result.trace['ranking_question'] == selections[0]


def test_canonical_question_cannot_invent_a_category_absent_from_the_user_context():
    model = Model(plan={'canonical_question':'Что такое 4 ЦК?',
                        'requirements':['Определение ценовых категорий'],'queries':['Ценовые категории']})
    plan = make_plan('Что такое ценовые категории?',model)
    assert plan.fallback
    assert not plan.canonical_question


def test_missing_canonical_question_preserves_the_original_ranking_fallback():
    model = Model()
    subjects = []
    def retrieve(subject,queries,requirements,trace,*,search_focus,constraint_question):
        subjects.append(subject)
        return [fragment()]
    result = run_corrective(QUESTION,QUESTION,retrieve=retrieve,complete=model)
    assert subjects == [QUESTION]
    assert not result.trace['plan']['canonical_question']


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
    def select(question, pool, requirements, *, search_focus, constraint_question):
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
    assert "Часть ответа не включена" in result.answer


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


def test_judge_outage_does_not_certify_or_publish_a_task_unassessed_draft():
    model = Model(judge_error=RuntimeError("Synthetic judge outage"))
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval(), complete=model)
    assert result.trace["verification"]["initial"]["assessment_complete"] is False
    assert FACT not in result.answer
    assert "Не удалось надёжно сопоставить" in result.answer
    assert model.answer_count == 2


def test_judge_timeout_is_degraded_verification_not_a_crashed_session():
    model = Model(judge_error=TimeoutError("Synthetic judge timeout"))
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval(), complete=model)
    assert result.trace["verification"]["initial"]["assessment_complete"] is False
    assert FACT not in result.answer
    assert "Не удалось надёжно сопоставить" in result.answer
    assert model.answer_count == 2


def test_user_answer_length_is_not_artificially_capped_by_orchestrator():
    # One fully assessed long factual span: this tests length, not permission to
    # publish hundreds of claims that the judge never assessed.
    long_fact = ('контроль ' * 5000).strip() + '.'
    long_answer = long_fact + ' [E1]'
    model = Model(answers=[long_answer], reviews=[coverage(long_fact)],
                  claims=[claim(long_fact,text=long_fact)])
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval([fragment(long_fact)]), complete=model,
                            max_context_chars=len(long_fact)+100)
    assert len(result.answer) == len(long_answer.strip())
    assert model.answer_count == 1


def test_short_unassessed_factual_tail_prevents_whole_answer_certification():
    model = Model(answers=[FACT + " [E1]\n\nПять режимов."], claims=[claim()])
    result = run_corrective(QUESTION, QUESTION, retrieve=Retrieval(), complete=model)
    assert result.trace["verification"]["initial"]["assessment_complete"] is False


def focus_payload():
    return {'operation': 'enumerate', 'subjects': [
        {'text': 'этапы контроля каналов', 'alternatives': ['процедуры проверки каналов']}
    ]}


def test_valid_search_focus_is_forwarded_explicitly_without_replacing_the_full_task():
    task = QUESTION + ' Сохрани точные названия и ответь таблицей.'
    requirements = ['Названия этапов контроля и различия между ними']
    model = Model(plan={'canonical_question':task, 'requirements':requirements,
                        'queries':['этапы контроля каналов'], 'search_focus':focus_payload()})
    retrieval = Retrieval()
    selections = []

    def select(canonical, pool, facets, *, search_focus, constraint_question):
        selections.append((canonical, facets, search_focus, constraint_question))
        return pool

    result = run_corrective(task,task,retrieve=retrieval,select=select,complete=model)
    focus = retrieval.focuses[0]
    assert focus.operation is SearchOperation.ENUMERATE
    assert focus.subjects[0].text == 'этапы контроля каналов'
    assert focus.subjects[0].alternatives == ('процедуры проверки каналов',)
    assert selections == [(task,requirements,focus,task)]
    assert selections[0][2] is focus
    assert retrieval.constraints == [task]
    assert task in retrieval.calls[0]
    assert result.trace['plan']['search_focus_status'] == 'valid'
    assert result.trace['plan']['search_focus_reason'] is None
    encoded = json.loads(json.dumps(result.trace,ensure_ascii=False))
    assert encoded['plan']['search_focus']['operation'] == 'enumerate'
    prompt = next(prompt for stage,prompt in model.calls if stage == 'answer')
    assert task in prompt and requirements[0] in prompt
    assert '"search_focus"' not in prompt


@pytest.mark.parametrize('bad_focus',[
    None, {}, {'operation':'change_acl','subjects':[{'text':'каналы'}]},
    {'operation':'lookup','subjects':[]},
    {**focus_payload(),'allowed_sources':['чужой документ.md']},
    {'operation':'lookup','subjects':[{'text':'каналы','answer':'непроверенный факт'}]},
])
def test_rejected_optional_focus_does_not_discard_an_otherwise_valid_plan(bad_focus):
    model = Model(plan={'canonical_question':QUESTION, 'requirements':['Этапы контроля'],
                        'queries':['контроль каналов'], 'search_focus':bad_focus})
    plan = make_plan(QUESTION,model)
    assert not plan.fallback
    assert plan.canonical_question == QUESTION
    assert plan.requirements == ('Этапы контроля',)
    assert plan.queries == (QUESTION,'контроль каналов')
    assert plan.search_focus is None and plan.search_focus_status == 'rejected'
    assert plan.search_focus_reason
    assert len(model.calls) == 1


def test_rejected_focus_is_traceable_while_callbacks_receive_none_and_original_constraints():
    model = Model(plan={'requirements':['Этапы контроля'], 'queries':['контроль каналов'],
                        'search_focus':{'operation':'unsupported','subjects':[]}})
    retrieval = Retrieval()
    result = run_corrective(QUESTION,QUESTION,retrieve=retrieval,complete=model)
    assert retrieval.focuses == [None]
    assert retrieval.constraints == [QUESTION]
    assert result.trace['plan']['search_focus_status'] == 'rejected'
    assert result.trace['plan']['search_focus_reason'] == 'invalid-operation'
    assert result.trace['plan']['fallback'] is False


def test_legacy_missing_focus_is_absent_not_an_invalid_plan():
    model, retrieval = Model(), Retrieval()
    result = run_corrective(QUESTION,QUESTION,retrieve=retrieval,complete=model)
    assert result.trace['plan']['search_focus'] is None
    assert result.trace['plan']['search_focus_status'] == 'absent'
    assert result.trace['plan']['search_focus_reason'] is None
    assert not result.trace['plan']['fallback']
    assert retrieval.focuses == [None]
    assert retrieval.constraints == [QUESTION]


def test_focus_cannot_add_an_unrequested_category_but_rejection_is_local_to_focus():
    model = Model(plan={'requirements':['Этапы контроля'], 'queries':['контроль каналов'],
                        'search_focus':{'operation':'lookup','subjects':[{'text':'четвертая ценовая категория'}]}})
    plan = make_plan(QUESTION,model)
    assert not plan.fallback
    assert plan.requirements == ('Этапы контроля',)
    assert plan.search_focus is None
    assert plan.search_focus_status == 'rejected'
    assert plan.search_focus_reason == 'invented-category'


def test_requested_comparison_categories_remain_valid_in_separate_search_subjects():
    question = 'Сравни 3 и 6 ЦК, не описывай историю.'
    model = Model(plan={'canonical_question':'Сравни 3 и 6 ЦК',
                        'requirements':['Сравнение 3 и 6 ЦК'], 'queries':['3 ЦК','6 ЦК'],
                        'exclusions':['история'],
                        'search_focus':{'operation':'compare','subjects':[{'text':'3 ЦК'},{'text':'6 ЦК'}]}})
    plan = make_plan(question,model)
    assert not plan.fallback and plan.search_focus_status == 'valid'
    assert [subject.text for subject in plan.search_focus.subjects] == ['3 ЦК','6 ЦК']
    assert plan.exclusions == ('история',)


def test_original_user_scope_not_rewritten_task_is_forwarded_in_every_round():
    user_scope = ('Тема диалога — вопрос пользователя: Контроль QX-42 по методике Датчик.md на 2020 год\n'
                  'Условия и уточнения пользователя: при −2,5 °C без подогрева\n'
                  'Исключить из предмета ответа: давление\n'
                  'Текущий запрос пользователя: А теперь перечисли их точные названия.')
    model = Model(plan={'canonical_question':QUESTION, 'requirements':['Этапы контроля'],
                        'queries':['контроль QX-42'], 'exclusions':['давление'], 'search_focus':focus_payload()},
                  reviews=[{'coverage':[], 'missing_queries':['Первый контроль','Второй контроль']}])
    retrieval, selections = Retrieval(), []

    def select(task,pool,facets,*,search_focus,constraint_question):
        selections.append((search_focus,constraint_question))
        return pool

    result = run_corrective('А теперь перечисли их точные названия.',user_scope,
                            retrieve=retrieval,select=select,complete=model)
    assert len(retrieval.calls) == 3
    assert retrieval.constraints == [user_scope] * 3
    assert all(focus is retrieval.focuses[0] for focus in retrieval.focuses)
    assert all(focus is retrieval.focuses[0] and constraints == user_scope for focus,constraints in selections)
    assert result.trace['ranking_question'] != user_scope
    assert user_scope in retrieval.calls[0]


def test_planner_outage_keeps_legacy_fallback_and_explicit_callback_contract():
    model = Model()

    def complete(prompt,stage):
        if stage == 'planning':
            raise TimeoutError('Synthetic planner outage')
        return model(prompt,stage)

    retrieval = Retrieval()
    result = run_corrective(QUESTION,QUESTION,retrieve=retrieval,complete=complete)
    assert result.trace['plan']['fallback']
    assert result.trace['plan']['search_focus_status'] == 'absent'
    assert retrieval.focuses == [None]
    assert retrieval.constraints == [QUESTION]
    assert retrieval.calls == [[QUESTION]]


def test_callback_type_error_is_not_misinterpreted_as_signature_fallback_or_retried():
    calls = []

    def retrieve(task,queries,requirements,trace,*,search_focus,constraint_question):
        calls.append((search_focus,constraint_question))
        raise TypeError('Synthetic callback implementation failure')

    with pytest.raises(TypeError,match='implementation failure'):
        run_corrective(QUESTION,QUESTION,retrieve=retrieve,complete=Model())
    assert calls == [(None,QUESTION)]


def test_planner_focus_contract_is_bounded_subject_only_and_does_not_supplant_answer_format():
    model = Model()
    make_plan(QUESTION,model)
    prompt = model.calls[0][1]
    assert '"search_focus"' in prompt
    assert 'lookup, enumerate, compare, calculate, procedure или locate_documents' in prompt
    assert 'от 1 до 6' in prompt and 'до 3' in prompt and '120 символов и 12 слов' in prompt
    assert 'не ограничение области доступа' in prompt
    assert 'Не включай в subjects просьбы о формате' in prompt

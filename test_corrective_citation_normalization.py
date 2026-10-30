"""Citation aliases must be normalized before every draft's spans and hashes."""
import hashlib
import json

from corrective_rag import run_corrective


QUESTION = 'Какие этапы контроля проходят измерительные каналы?'
FACT = 'Каждый измерительный канал проходит два этапа контроля.'


class CitationModel:
    def __init__(self, answers, *, reject_first=False):
        self.answers = answers
        self.reject_first = reject_first
        self.answer_count = 0
        self.judge_prompts = []

    def __call__(self, prompt, stage):
        if stage == 'planning':
            return json.dumps({'requirements':['Описать этапы контроля'], 'queries':[QUESTION]})
        if stage == 'answer':
            answer = self.answers[min(self.answer_count,len(self.answers)-1)]
            self.answer_count += 1
            return answer
        if 'Проверь доказательность ответа' in prompt:
            self.judge_prompts.append(prompt)
            return json.dumps({'claims':[{
                'claim_id':'C1', 'context_ids':[], 'passage_ids':['E1:P1'],
                'task_relevance':'matched',
                'support':'unsupported' if self.reject_first and self.answer_count == 1 else 'supported'}]})
        return json.dumps({
            'coverage':[{'requirement_id':'R1', 'applicable':True, 'passage_ids':['E1:P1']}],
            'task_alignment':{'status':'matched', 'passage_ids':['E1:P1']},
            'missing_queries':[], 'conflicts':[]})


def run(model):
    def retrieve(task, queries, requirements, trace, *, search_focus, constraint_question):
        return [{'file':'контроль.md', 'section_path':'Основной порядок', 'text':FACT}]
    return run_corrective(QUESTION,QUESTION,retrieve=retrieve,complete=model)


def judge_payload(prompt):
    return json.JSONDecoder().raw_decode(prompt[prompt.index('{"question":'):])[0]


def test_first_writer_alias_is_normalized_before_claims_hashes_and_publication():
    model = CitationModel([FACT+' [E1:P1]'])
    result = run(model)
    normalized = FACT+' [E1]'
    assert result.answer == normalized
    assert model.answer_count == 1
    assert judge_payload(model.judge_prompts[0])['answer'] == normalized
    assert result.trace['answer_versions']['primary_sha256'] == hashlib.sha256(normalized.encode()).hexdigest()
    assert result.trace['answer_versions']['primary_chars'] == len(normalized)
    assert result.trace['verification']['initial']['assessment_complete'] is True
    assert result.trace['citation_normalization'] == {'initial':{'converted_count':1,'invalid_count':0}}


def test_repair_writer_alias_uses_fresh_normalized_spans_and_hash():
    model = CitationModel([FACT+' [E1]',FACT+' [E1:P1]'],reject_first=True)
    result = run(model)
    normalized = FACT+' [E1]'
    assert model.answer_count == 2
    assert result.answer == normalized
    assert [judge_payload(prompt)['answer'] for prompt in model.judge_prompts] == [normalized,normalized]
    assert result.trace['verification']['after_repair']['assessment_complete'] is True
    assert result.trace['answer_versions']['final_sha256'] == hashlib.sha256(normalized.encode()).hexdigest()
    assert result.trace['citation_normalization']['repair'] == {'converted_count':1,'invalid_count':0}


def test_unknown_passage_beside_a_valid_source_is_not_laundered_by_the_judge():
    model = CitationModel([FACT+' [E1][E999:P1]'])
    result = run(model)
    assert model.answer_count == 2
    assert FACT not in result.answer
    assert result.trace['citation_normalization']['initial'] == {'converted_count':0,'invalid_count':1}
    assert result.trace['citation_normalization']['repair'] == {'converted_count':0,'invalid_count':1}
    initial = result.trace['verification']['initial']
    assert any(check['kind'] == 'citation' and check['status'] == 'invalid_citation' for check in initial['checks'])

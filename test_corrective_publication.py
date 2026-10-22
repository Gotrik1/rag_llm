"""Independent citation/publication regressions, not private evaluation cases."""
import hashlib
import json
import re

from corrective_rag import SearchPlan, assess_coverage, generation_prompt, run_corrective
from grounding_verifier import build_grounding_judge_prompt, extract_answer_claims
from user_task import UserTask


QUESTION = 'When does the archive open?'
FACT = 'The archive opens at six.'
BAD = 'All visitors receive a gold badge.'
EVIDENCE = [{'evidence_id':'E37','file':'Archive.md','section_path':'Opening time','text':FACT}]


def decode(prompt, marker):
    return json.JSONDecoder().raw_decode(prompt[prompt.index(marker):])[0]


def test_writer_example_uses_only_current_evidence_identifiers():
    task = UserTask(QUESTION,QUESTION)
    prompt = generation_prompt(QUESTION,SearchPlan(('Opening time',),(QUESTION,)),EVIDENCE,{},user_task=task)
    assert set(re.findall(r'\bE\d+\b',prompt)) == {'E37'}
    assert '[E37]' in prompt
    assert 'E37:P1' not in prompt  # Public citations are not passage selectors.


def test_coverage_example_uses_existing_passage_not_an_assumed_first_id():
    seen = []
    def complete(prompt,*_):
        seen.append(prompt)
        return '{}'
    assess_coverage(SearchPlan(('Opening time',),(QUESTION,)),EVIDENCE,complete,
                    user_task=UserTask(QUESTION,QUESTION))
    assert set(re.findall(r'\bE\d+(?::P\d+)?\b',seen[0])) == {'E37','E37:P1'}


def test_judge_syntax_example_uses_a_claims_actual_cited_source():
    other = {'evidence_id':'E16','file':'Other.md','text':'The attic closes at ten.'}
    prompt = build_grounding_judge_prompt(FACT+' [E37]',[other,*EVIDENCE],question=QUESTION,
                                          user_task=UserTask(QUESTION,QUESTION))
    example = json.JSONDecoder().raw_decode(prompt.split('Только JSON: ',1)[1])[0]
    row = example['claims'][0]
    assert row['claim_id'] == 'C1'
    assert row['passage_ids'] == ['E37:P1']
    assert 'E1:P1' not in prompt


def test_judge_without_a_cited_source_does_not_invent_support_in_its_example():
    prompt = build_grounding_judge_prompt(FACT,EVIDENCE,question=QUESTION,
                                          user_task=UserTask(QUESTION,QUESTION))
    row = json.JSONDecoder().raw_decode(prompt.split('Только JSON: ',1)[1])[0]['claims'][0]
    assert row['support'] == 'uncertain' and row['passage_ids'] == []
    assert 'E1:P1' not in prompt


class VersionModel:
    def __init__(self,primary,repair):
        self.answers = [primary,repair]
        self.stages = []
        self.answer_count = 0

    def __call__(self,prompt,stage):
        self.stages.append(stage)
        if stage == 'planning':
            return json.dumps({'requirements':['Archive opening time'],'queries':['Archive opening time']})
        if stage == 'answer':
            result = self.answers[min(self.answer_count,1)]
            self.answer_count += 1
            return result
        if 'Проверь доказательность ответа' not in prompt:
            return json.dumps({'coverage':[{'requirement_id':'R1','passage_ids':['E1:P1'],'applicable':True}],
                               'task_alignment':{'status':'matched','passage_ids':['E1:P1']},
                               'missing_queries':[],'conflicts':[]})
        data = decode(prompt,'{"question":')
        rows = []
        for candidate in data['answer_claims']:
            supported = candidate['text'] == FACT+' [E1]'
            rows.append({'claim_id':candidate['claim_id'],
                         'support':'supported' if supported else 'uncertain',
                         'passage_ids':['E1:P1'] if supported else [],
                         'task_relevance':'matched' if FACT in candidate['text'] else 'mismatch'})
        return json.dumps({'claims':rows})


def run(primary,repair):
    model = VersionModel(primary,repair)
    result = run_corrective(QUESTION,QUESTION,complete=model,
        retrieve=lambda *a,**k:[{'file':'Archive.md','section_path':'Opening time','text':FACT}])
    return result,model


def test_a_failed_repair_does_not_discard_a_verified_primary_fact():
    primary = FACT+' [E1]\n\n'+BAD+' [E1]'
    result,model = run(primary,FACT)
    assert FACT+' [E1]' in result.answer
    assert BAD not in result.answer
    assert 'Часть ответа не включена' in result.answer
    publication = result.trace['verification']['publication']
    assert publication['stage'] == 'primary_safe_partial'
    assert publication['verified_claim_count'] == 1
    assert publication['source_draft_sha256'] == hashlib.sha256(primary.encode()).hexdigest()
    assert 'source_report_sha256' in publication
    assert publication['whole_answer_certified'] is False
    assert set(publication).isdisjoint({'answer','primary_answer','quotes','source_text'})
    assert model.stages == ['planning','evidence-review','answer','evidence-review','answer','evidence-review']


def test_valid_repair_is_preferred_to_an_unverified_primary():
    result,model = run(BAD+' [E1]',FACT+' [E1]')
    assert result.answer == FACT+' [E1]'
    assert model.answer_count == 2
    assert 'partial_answer' not in result.trace['verification']


def test_two_unpublishable_versions_do_not_create_an_answer_by_merging():
    result,model = run(BAD+' [E1]',FACT)
    assert FACT not in result.answer and BAD not in result.answer
    assert 'Не удалось надёжно сопоставить' in result.answer
    assert result.trace['verification']['publication']['stage'] == 'neutral'
    assert result.trace['verification']['publication']['verified_claim_count'] == 0
    assert model.answer_count == 2


def test_repair_with_verified_content_remains_preferred_without_mixing_versions():
    result,_ = run(FACT+' [E1]\n\n'+BAD+' [E1]',FACT+' [E1]\n\nUnsupported appendix.')
    assert FACT+' [E1]' in result.answer
    assert BAD not in result.answer and 'Unsupported appendix' not in result.answer
    assert result.trace['verification']['publication']['stage'] == 'repair_safe_partial'

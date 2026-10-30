"""The evidence guard must receive model content before semantic deletion."""
from unittest.mock import Mock

import pytest
import ordinary_answer_guard as guard
import rag_agent
from test_ordinary_formula_fidelity import make_agent


@pytest.mark.parametrize('stage',['writer','repair'])
def test_unrecognized_vocabulary_cannot_delete_a_source_bound_definition_before_review(monkeypatch,stage):
    fact='DELTA is the measured quantity, only for the selected object and hour.'
    draft=fact+' [E1]'
    agent=make_agent(draft)
    monkeypatch.setattr('socket.socket.connect',Mock(side_effect=AssertionError('No network')))
    monkeypatch.setattr(rag_agent,'known_formula_terms',lambda:('DELTA',))
    monkeypatch.setattr(rag_agent,'requested_formula_terms',lambda question:set())
    observed=[]
    def publication(answer,packet,question,*,repairer,**kwargs):
        if stage=='repair':answer=repairer('Controlled repair request.')
        observed.append(answer)
        return guard.GuardResult(answer,True)
    monkeypatch.setattr(rag_agent,'publish_draft',publication)
    context=[{'file':'neutral.md','section_path':'Method','text':fact,'score':1.0}]
    agent._generate_grounded_answer('Explain the measured quantity.',context,[],selection_mode='coverage')
    assert len(observed)==1
    assert fact in observed[0]


def test_legacy_semantic_filter_remains_separate_from_the_verified_route(monkeypatch):
    monkeypatch.setattr(rag_agent,'known_formula_terms',lambda:('DELTA',))
    monkeypatch.setattr(rag_agent,'requested_formula_terms',lambda question:set())
    assert 'DELTA' not in rag_agent.sanitize_grounded_answer('DELTA applies here.','Explain.',[])

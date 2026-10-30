"""The web request must use one matching prompt/pipeline/selector snapshot."""
from inspect import signature
from unittest.mock import Mock
import pytest
import web_ui
from test_flow_modes import FakeAgent


class SpyAgent(FakeAgent):
    def ask(self, question, prompt, prompt_id, *, pipeline_mode=None, selection_mode=None, **kwargs):
        self.seen=(prompt,prompt_id,pipeline_mode,selection_mode)
        supported=set(signature(FakeAgent.ask).parameters)
        return super().ask(question,prompt,prompt_id,**{k:v for k,v in kwargs.items() if k in supported})


@pytest.mark.parametrize('selection,pipeline,custom',[
    ('coverage','off',False),('legacy','off',False),('coverage','on',False),('coverage','off',True)])
def test_web_prompt_and_applied_modes_are_captured_before_a_concurrent_settings_change(monkeypatch,selection,pipeline,custom):
    monkeypatch.setattr('socket.socket.connect',Mock(side_effect=AssertionError('No network')))
    monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE',selection)
    monkeypatch.setenv('RAG_CORRECTIVE_PIPELINE',pipeline)
    handler=object.__new__(web_ui.Handler);handler.send_json=Mock();agent=SpyAgent();offered=[]
    def profile(identifier,default):
        offered.append(default)
        monkeypatch.setenv('RAG_EVIDENCE_SELECTION_MODE','legacy')
        monkeypatch.setenv('RAG_CORRECTIVE_PIPELINE','on' if pipeline=='off' else 'off')
        return {'id':identifier,'prompt':'Explicit custom policy.' if custom else default}
    replacements={'load_flow_mode':lambda:'python','get_agent':lambda:agent,
        'get_profile':profile,'load_system_prompt_id':lambda:'custom' if custom else 'rag-grounded',
        'get_project_conversation':lambda _: {'project_name':'Neutral project','memory':{},'messages':[]},
        'retrieval_document_scope':lambda *a:(set(),{}),'shared_global_document_sources':lambda:{'python.docx'},
        'project_context_prompt':lambda *a:'PROJECT MEMORY EXACT','validate_retrieval':lambda *a:[],
        'extract_formula_lines':lambda *a:[],
        'llm_label':lambda:{'provider':'ollama','model':'test','label':'test'}}
    for name,value in replacements.items():monkeypatch.setattr(web_ui,name,value)
    handler.handle_ask({'question':'Explain the process.','chat_id':'owned-chat'})
    expected=web_ui.COVERAGE_SYSTEM_PROMPT if selection=='coverage' or pipeline=='on' else web_ui.GROUNDED_SYSTEM_PROMPT
    assert offered==[expected]
    assert agent.seen[0]==('Explicit custom policy.' if custom else expected)+'\n\nPROJECT MEMORY EXACT'
    assert agent.seen[2:]==(pipeline,None if pipeline=='on' else selection)
    assert handler.send_json.call_args.args[0]['flow_mode']=='python'

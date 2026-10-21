import httpx
from test_llm_bridge import bridge


def test_thinking_requires_operator_flag_and_internal_stage(monkeypatch):
    request = {'prompt':'plan','think':True,'purpose':'planning'}
    monkeypatch.delenv('RAG_ALLOW_PLANNER_THINKING',raising=False)
    assert bridge._openai_payload(request,False)['chat_template_kwargs']['enable_thinking'] is False
    monkeypatch.setenv('RAG_ALLOW_PLANNER_THINKING','true')
    assert bridge._openai_payload(request,False)['chat_template_kwargs']['enable_thinking'] is True
    request['purpose']='answer'
    assert bridge._openai_payload(request,False)['chat_template_kwargs']['enable_thinking'] is False


def test_reasoning_is_never_used_as_final_content():
    assert bridge._content(httpx.Response(200,json={'choices':[{'message':{'reasoning_content':'private plan'}}]})) == ''
    assert bridge._content(httpx.Response(200,json={'choices':[{'message':{'content':'<think>private</think>result'}}]})) == 'result'
    response = httpx.Response(200,json={'choices':[{'finish_reason':'length','message':{'content':'partial'}}]})
    assert bridge._content(response,reject_truncated=True) == ''
    assert bridge._content(response) == 'partial'  # Preserve legacy final-answer behavior.

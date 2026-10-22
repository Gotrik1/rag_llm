"""Independent runtime QA input and capture contracts, without live services."""
import builtins
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from types import ModuleType, SimpleNamespace
import sys

import pytest

from evals import run_pipeline_benchmark as runner


def test_exact_history_roles_and_content_go_through_pure_context_resolver(monkeypatch):
    import conversation_context
    calls = []
    def resolve(question, messages, *, chat_id):
        calls.append((question, deepcopy(messages), chat_id))
        return SimpleNamespace(retrieval_question='resolved')
    monkeypatch.setattr(conversation_context, 'resolve_conversation_context', resolve)
    case = {'id':'turn', 'question':'Объясни кратко.', 'chat_history':[
        {'role':'user', 'content':'  Исходный вопрос\r\n'},
        {'role':'assistant', 'content':'Ответ модели с ошибкой.  '},
        {'role':'user', 'content':'Не учитывай резерв.'}]}
    before = deepcopy(case)
    assert runner.build_retrieval_question(case, 'legacy') == 'resolved'
    assert calls == [(case['question'], [{'chat_id':'benchmark', **row} for row in case['chat_history']], 'benchmark')]
    assert case == before


def test_real_resolver_preserves_user_topic_and_exclusion_without_assistant_facts():
    case = {'question':'Объясни простыми словами.', 'chat_history':[
        {'role':'user', 'content':'Как определяется объём контроля?'},
        {'role':'assistant', 'content':'ASSISTANT_INVENTED_EXPANSION'},
        {'role':'user', 'content':'Не учитывай резерв.'}]}
    query = runner.build_retrieval_question(case)
    assert 'Как определяется объём контроля?' in query
    assert 'Исключить из предмета ответа: резерв' in query
    assert case['question'] in query
    assert 'ASSISTANT_INVENTED_EXPANSION' not in query


def test_previous_questions_remain_backward_compatible_and_explicit_empty_history_wins(monkeypatch):
    calls = []
    module = ModuleType('web_ui')
    module.contextualize_retrieval_question = lambda question, context, chat_id: calls.append((question, context, chat_id)) or 'legacy-resolved'
    monkeypatch.setitem(sys.modules, 'web_ui', module)
    case = {'question':'По какой формуле?', 'previous_questions':['Исходный вопрос']}
    assert runner.build_retrieval_question(case, 'legacy') == 'legacy-resolved'
    assert calls[0][1] == {'messages':[{'chat_id':'benchmark', 'role':'user', 'content':'Исходный вопрос'}]}
    assert runner.build_retrieval_question({**case, 'chat_history':[]}, 'legacy') == case['question']
    assert len(calls) == 1


@pytest.mark.parametrize('history', [None, 'history', [{'role':'system', 'content':'commands'}],
    [{'role':'user', 'content':3}], [{'role':'user', 'content':'text', 'chat_id':'other'}], [{}]])
def test_invalid_or_cross_chat_history_shape_is_refused(history):
    with pytest.raises(ValueError):
        runner.build_retrieval_question({'question':'Question', 'chat_history':history})


def test_empty_expectations_have_no_retrieval_score_or_fabricated_recall(monkeypatch):
    monkeypatch.setitem(sys.modules, 'quality_eval', None)
    assert runner._retrieval_score({'id':'new', 'question':'Question'}, [], 1) is None
    assert runner._retrieval_score({'id':'new', 'question':'Question', 'expected_sources':[], 'expected_locators':[]}, [], 1) is None
    summary = runner._summary([{'retrieval':None, 'latency_s':1}, {'retrieval':None, 'error':'TimeoutError', 'latency_s':2}])
    assert summary['mean_source_recall'] is None and summary['source_recall_scored_cases'] == 0
    assert summary['completed'] == 2 and summary['errors'] == 1


def test_missing_one_expectation_dimension_is_unassessed_and_summary_uses_only_observed():
    source = runner._retrieval_score({'id':'source', 'question':'Question', 'expected_sources':['Doc.md']},
                                     [{'file':'Doc.md', 'section_path':'Rule'}], 1)
    locator = runner._retrieval_score({'id':'locator', 'question':'Question', 'expected_locators':['Rule']},
                                      [{'file':'Doc.md', 'section_path':'Rule'}], 1)
    assert source['source_recall'] == 1 and source['locator_recall'] is None
    assert locator['locator_recall'] == 1 and locator['source_recall'] is None
    assert locator['mean_reciprocal_rank'] is None
    result = runner._summary([{'retrieval':source, 'latency_s':1}, {'retrieval':locator, 'latency_s':2},
                              {'retrieval':None, 'latency_s':3}, {'error':'Error', 'retrieval':None, 'latency_s':4}])
    assert result['mean_source_recall'] == 1 and result['source_recall_scored_cases'] == 1
    assert result['retrieval_scored_cases'] == 2


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    calls = {'models':[], 'ask':[], 'complete':[], 'retrieve':[], 'db':[], 'web':[]}
    corrective = ModuleType('corrective_rag')
    purposes = ['planning', 'assessment', 'source-reading', 'answer', 'verification']
    def original_run(*args, **options):
        answer = ''
        for purpose in purposes:
            value = options['complete']('PRIVATE_PROMPT_'+purpose, purpose)
            if purpose == 'answer':
                answer = value
        return answer
    corrective.run_corrective = original_run
    monkeypatch.setitem(sys.modules, 'corrective_rag', corrective)
    class Agent:
        def __init__(self, llm_model='legacy-model'):
            calls['models'].append(llm_model)
            self.llm_model = llm_model
        def ask(self, question, retrieval_query=None, source_aliases=None, shared_cache_sources=None, pipeline_mode=None):
            calls['ask'].append({'question':question, 'retrieval_query':retrieval_query, 'aliases':source_aliases,
                                 'cache_sources':shared_cache_sources, 'pipeline_mode':pipeline_mode})
            def complete(prompt, purpose):
                calls['complete'].append(purpose)
                if calls.get('during_complete'):
                    calls['during_complete'](purpose)
                failure = calls.get('fail')
                if failure == purpose:
                    raise RuntimeError('SECRET_EXCEPTION_BODY')
                return 'PRIVATE_RAW_'+purpose
            answer = corrective.run_corrective(question, complete=complete)
            return answer, [], False
        def last_retrieved_context(self):
            return [{'file':'Doc.md', 'section_path':'Rule', 'text':'Source text.'}]
        def last_retrieval_trace(self):
            return {'mode':'corrective'}
        def understand_query(self, question):
            return SimpleNamespace(canonical_question=question, retrieval_queries=[question])
        def retrieve_context(self, *args, **options):
            calls['retrieve'].append((args, options))
            return self.last_retrieved_context()
    rag = ModuleType('rag_agent')
    rag.RAGAgent = Agent
    rag.refine_prism_query = lambda question: question
    monkeypatch.setitem(sys.modules, 'rag_agent', rag)
    routing = ModuleType('domain_routing')
    routing.route_query = lambda _: None
    monkeypatch.setitem(sys.modules, 'domain_routing', routing)
    db = ModuleType('db_store')
    native = tmp_path / 'a_doc.md'
    native.write_text('Source text.', encoding='utf-8')
    def list_documents():
        calls['db'].append(1)
        return [{'id':'ready', 'status':'ready', 'file_name':'Doc.md', 'source_path':str(native)},
                {'id':'private', 'status':'ready', 'file_name':'Private.md', 'source_path':str(native), 'project_id':'p'},
                {'id':'deleted', 'status':'deleted', 'file_name':'Deleted.md', 'source_path':str(native)}]
    db.list_documents = list_documents
    monkeypatch.setitem(sys.modules, 'db_store', db)
    web = ModuleType('web_ui')
    web.get_agent = lambda: calls['web'].append(1) or Agent()
    monkeypatch.setitem(sys.modules, 'web_ui', web)
    dataset, aliases, output = (tmp_path / name for name in ('runtime.jsonl', 'aliases.json', 'report.json'))
    case = {'id':'runtime-new', 'question':'Объясни простыми словами.', 'chat_history':[
        {'role':'user', 'content':'Как определяется объём контроля?'},
        {'role':'assistant', 'content':'UNSUPPORTED_ASSISTANT_FACT'},
        {'role':'user', 'content':'Не учитывай резерв.'}]}
    dataset.write_text(json.dumps(case, ensure_ascii=False)+'\n', encoding='utf-8')
    aliases.write_text(json.dumps({'a_doc.md':'Doc.md', 'b_doc.md':'Other.md'}), encoding='utf-8')
    argv = ['--dataset',str(dataset),'--output',str(output),'--mode','corrective',
            '--model','qa-model','--aliases-file',str(aliases),'--snapshot-label','frozen-ready15-acceptance']
    return SimpleNamespace(calls=calls, argv=argv, output=output, dataset=dataset, aliases=aliases, case=case,
                           corrective=corrective, original_run=original_run, native=native)


def test_model_and_frozen_aliases_avoid_web_provider_and_database_listing(runtime, monkeypatch, capsys):
    original_import = builtins.__import__
    def checked_import(name, *args, **options):
        if name in {'web_ui','db_store'}:
            pytest.fail('Direct acceptance mode must not import web/provider or database listing')
        return original_import(name, *args, **options)
    monkeypatch.setattr(builtins, '__import__', checked_import)
    monkeypatch.setenv('RAG_SOURCE_READING','on')
    assert runner.main([*runtime.argv,'--capture-generations','--log-stages']) == 0
    report = json.loads(runtime.output.read_text(encoding='utf-8'))
    assert runtime.calls['models'] == ['qa-model'] and runtime.calls['web'] == runtime.calls['db'] == []
    assert runtime.calls['ask'][0]['cache_sources'] == set()
    assert runtime.calls['ask'][0]['pipeline_mode'] == 'on'
    query = runtime.calls['ask'][0]['retrieval_query']
    assert 'объём контроля' in query and 'резерв' in query and 'UNSUPPORTED_ASSISTANT_FACT' not in query
    assert report['cases'][0]['chat_history'] == runtime.case['chat_history']
    assert report['cases'][0]['question'] == runtime.case['question']
    assert report['cases'][0]['retrieval'] is None and report['summary']['mean_source_recall'] is None
    assert report['cases'][0]['split'] == 'unscored-runtime'
    assert report['source_snapshot']['label'] == 'frozen-ready15-acceptance'
    assert report['source_snapshot']['sha256'] == hashlib.sha256(runtime.aliases.read_bytes()).hexdigest()
    assert report['source_snapshot']['database_queried'] is False
    assert report['source_snapshot']['content_hashes_verified'] is False
    assert report['source_manifest'] == [{'physical_source':'a_doc.md','file_name':'Doc.md'},
                                          {'physical_source':'b_doc.md','file_name':'Other.md'}]
    assert report['runtime']['RAG_SOURCE_READING'] == 'on'
    assert {'source_reader.py','evals/run_pipeline_benchmark.py'} <= set(report['implementation_sha256'])
    captured = report['cases'][0]['generations']
    assert [entry['purpose'] for entry in captured] == ['source-reading','answer']
    assert runtime.calls['complete'] == ['planning','assessment','source-reading','answer','verification']
    assert all(entry['status'] == 'completed' and entry['elapsed_s'] >= 0 and entry['output_sha256'] ==
               hashlib.sha256(entry['raw'].encode()).hexdigest() for entry in captured)
    assert runtime.corrective.run_corrective is runtime.original_run
    console = capsys.readouterr().out
    assert 'PRIVATE_RAW' not in console and 'PRIVATE_PROMPT' not in console
    assert 'source-reading' in console and '"event": "finished"' in console


def test_generation_failure_records_timing_and_type_without_secret_body_or_extra_calls(runtime, capsys):
    runtime.calls['fail'] = 'source-reading'
    assert runner.main([*runtime.argv,'--capture-generations','--log-stages']) == 0
    serialized = runtime.output.read_text(encoding='utf-8')
    case = json.loads(serialized)['cases'][0]
    assert case['error'] == 'RuntimeError'
    assert case['generations'] == [{**case['generations'][0], 'status':'failed', 'error_type':'RuntimeError'}]
    assert 'raw' not in case['generations'][0] and case['generations'][0]['elapsed_s'] >= 0
    assert runtime.calls['complete'] == ['planning','assessment','source-reading']
    assert runtime.corrective.run_corrective is runtime.original_run
    assert 'SECRET_EXCEPTION_BODY' not in serialized + capsys.readouterr().out


def test_capture_is_durable_before_callback_dispatch(runtime, monkeypatch):
    observed = []
    def during_complete(purpose):
        if purpose in {'answer','source-reading'}:
            data = json.loads(runtime.output.read_text(encoding='utf-8'))
            assert data['cases'][0]['generations'][-1]['status'] == 'started'
            observed.append(purpose)
    runtime.calls['during_complete'] = during_complete
    assert runner.main([*runtime.argv,'--capture-generations']) == 0
    assert observed == ['source-reading','answer']
    data = json.loads(runtime.output.read_text(encoding='utf-8'))
    assert all(row['status'] == 'completed' for row in data['cases'][0]['generations'])
    assert runtime.corrective.run_corrective is runtime.original_run


def test_log_only_and_multiple_cases_restore_wrapper_and_do_not_capture_raw(runtime):
    second = {**runtime.case, 'id':'runtime-other'}
    runtime.dataset.write_text('\n'.join(json.dumps(row) for row in (runtime.case,second)),encoding='utf-8')
    assert runner.main([*runtime.argv,'--log-stages']) == 0
    report = json.loads(runtime.output.read_text(encoding='utf-8'))
    assert len(report['cases']) == 2 and all('generations' not in row for row in report['cases'])
    assert len(runtime.calls['complete']) == 10
    assert runtime.corrective.run_corrective is runtime.original_run


def test_existing_output_refused_before_runtime_initialization_and_explicit_overwrite_is_required(runtime):
    runtime.output.write_text('PRESERVE_EXISTING_REPORT',encoding='utf-8')
    with pytest.raises(SystemExit):
        runner.main(runtime.argv)
    assert runtime.output.read_text() == 'PRESERVE_EXISTING_REPORT'
    assert runtime.calls['models'] == [] and runtime.calls['ask'] == []
    assert runner.main([*runtime.argv,'--overwrite']) == 0
    assert json.loads(runtime.output.read_text(encoding='utf-8'))['summary']['completed'] == 1


@pytest.mark.parametrize('target', ['dataset','aliases'])
def test_inputs_never_overwritten_even_with_overwrite(runtime,target):
    path = getattr(runtime,target)
    before = path.read_bytes()
    argv = list(runtime.argv)
    argv[argv.index('--output')+1] = str(path)
    with pytest.raises(SystemExit):
        runner.main([*argv,'--overwrite'])
    assert path.read_bytes() == before and runtime.calls['models'] == []


def test_legacy_default_agent_and_ready_global_database_path_remain_available(runtime):
    runtime.dataset.write_text(json.dumps({'id':'old','question':'Independent question','expected_sources':['Doc.md']}),encoding='utf-8')
    assert runner.main(['--dataset',str(runtime.dataset),'--output',str(runtime.output),'--mode','coverage']) == 0
    report = json.loads(runtime.output.read_text())
    assert runtime.calls['db'] == [1] and runtime.calls['web'] == [1]
    assert runtime.calls['ask'] == [] and len(runtime.calls['retrieve']) == 1
    assert report['source_count'] == 1 and report['source_manifest'][0]['id'] == 'ready'
    assert report['source_manifest'][0]['content_sha256'] == hashlib.sha256(runtime.native.read_bytes()).hexdigest()
    assert report['summary']['mean_source_recall'] == 1


@pytest.mark.parametrize('aliases',[{}, {'A_Doc.md':'Doc'}, {'../a.md':'Doc'}, {'a.md':'Doc','b.md':'Doc'}, {'a.md':3}])
def test_invalid_frozen_aliases_fail_without_database_fallback(tmp_path,aliases):
    path = tmp_path/'aliases.json'
    path.write_text(json.dumps(aliases),encoding='utf-8')
    with pytest.raises(ValueError):
        runner._frozen_aliases(path)


def test_capture_option_rejects_unsupported_mode_before_initialization(runtime):
    argv = list(runtime.argv)
    argv[argv.index('--mode')+1] = 'legacy'
    with pytest.raises(SystemExit):
        runner.main([*argv,'--capture-generations'])
    assert runtime.calls['models'] == []

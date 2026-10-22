"""Non-mutating corpus benchmark. Raw answers/evidence remain review artifacts.

Run inside a configured RAG runtime. Does not create users, chats or documents.
Retrieval coverage is not an answer correctness score.
"""
from __future__ import annotations
import argparse
import json
import inspect
import hashlib
import os
import time
from datetime import datetime, timezone
from dataclasses import asdict
from pathlib import Path
from statistics import mean
import sys
from contextlib import contextmanager
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def _sha(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def _history_messages(case):
    """Keep exact authorized input turns; the resolver ignores assistant facts."""
    if 'chat_history' in case:
        history = case['chat_history']
        if not isinstance(history, list) or any(
            not isinstance(row, dict) or set(row) != {'role', 'content'}
            or row['role'] not in {'user', 'assistant'} or not isinstance(row['content'], str)
            for row in history
        ):
            raise ValueError('chat_history requires user/assistant role and string content')
        return [{'chat_id':'benchmark', 'role':row['role'], 'content':row['content']} for row in history]
    previous = case.get('previous_questions', [])
    if not isinstance(previous, list) or any(not isinstance(value, str) for value in previous):
        raise ValueError('previous_questions must be a list of strings')
    return [{'chat_id':'benchmark', 'role':'user', 'content':value} for value in previous]


def build_retrieval_question(case, mode='corrective'):
    """Pure conversation resolution, with the old legacy-history path retained."""
    question = case['question']
    messages = _history_messages(case)
    if not messages:
        return question
    if mode == 'legacy' and 'chat_history' not in case:
        from web_ui import contextualize_retrieval_question
        return contextualize_retrieval_question(question, {'messages':messages}, 'benchmark')
    from conversation_context import resolve_conversation_context
    return resolve_conversation_context(question, messages, chat_id='benchmark').retrieval_question


def _frozen_aliases(path, label=None):
    raw = path.read_bytes()
    aliases = json.loads(raw.decode('utf-8-sig'))
    if (not isinstance(aliases, dict) or not aliases or any(
        not isinstance(key, str) or not key.strip() or key != key.casefold()
        or '/' in key or '\\' in key or not isinstance(value, str) or not value.strip()
        for key, value in aliases.items()
    ) or len(set(aliases.values())) != len(aliases)):
        raise ValueError('Frozen aliases must uniquely map physical filenames to display names')
    snapshot = {'kind':'frozen-alias-manifest', 'label':label or path.stem,
                'path':str(path.resolve()), 'sha256':hashlib.sha256(raw).hexdigest(),
                'database_queried':False, 'content_hashes_verified':False,
                'scope':'operator-supplied-ready-global-only'}
    manifest = [{'physical_source':key, 'file_name':value} for key, value in aliases.items()]
    return aliases, manifest, snapshot


def _retrieval_score(case, context, elapsed):
    if not case.get('expected_sources') and not case.get('expected_locators'):
        return None
    from quality_eval import QualityCase, score_retrieval
    score_case = QualityCase(id=case['id'], question=case['question'],
        expected_sources=tuple(case.get('expected_sources', ())),
        expected_locators=tuple(case.get('expected_locators', ())))
    result = asdict(score_retrieval(score_case, {'context':context}, elapsed))
    if not score_case.expected_sources:
        result['source_recall'] = result['mean_reciprocal_rank'] = None
    if not score_case.expected_locators:
        result['locator_recall'] = None
    return result


def _summary(cases):
    finished = [row for row in cases if 'latency_s' in row]
    recalls = [row['retrieval']['source_recall'] for row in finished
               if isinstance(row.get('retrieval'), dict) and row['retrieval'].get('source_recall') is not None]
    return {'completed':len(finished), 'errors':sum('error' in row for row in finished),
            'retrieval_scored_cases':sum(isinstance(row.get('retrieval'), dict) for row in finished),
            'source_recall_scored_cases':len(recalls),
            'mean_source_recall':mean(recalls) if recalls else None,
            'mean_latency_s':mean(row['latency_s'] for row in finished) if finished else None,
            'semantic_quality':'not certified; inspect raw answers against reviewed requirements'}


def _write_report(output, report, *, exclusive=False):
    if exclusive:
        with output.open('x', encoding='utf-8') as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=output.parent,
                                         prefix=output.name+'.', suffix='.tmp', delete=False) as handle:
            temporary = Path(handle.name)
            json.dump(report, handle, ensure_ascii=False, indent=2, default=str)
        os.replace(temporary, output)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


@contextmanager
def _observe_generations(result, *, log_stages=False, capture=False, checkpoint=None):
    """Observe existing callbacks, without extra calls or persistent patching."""
    if not log_stages and not capture:
        yield
        return
    import corrective_rag
    original_run = corrective_rag.run_corrective

    def observed_run(*positional, **options):
        original_complete = options['complete']

        def observed_complete(prompt, purpose):
            started = time.perf_counter()
            entry = None
            if capture and purpose in {'answer', 'source-reading'}:
                entry = {'purpose':purpose, 'status':'started',
                         'started_at':datetime.now(timezone.utc).isoformat(), 'prompt_sha256':_sha(prompt)}
                result.setdefault('generations', []).append(entry)
                if checkpoint:
                    checkpoint()
            if log_stages:
                print(json.dumps({'id':result['id'], 'stage':purpose, 'event':'started'}), flush=True)
            status = 'failed'
            try:
                output = original_complete(prompt, purpose)
                if entry is not None:
                    raw = output if isinstance(output, str) else str(output)
                    entry.update(status='completed', raw=raw, output_sha256=_sha(raw))
                status = 'completed'
                return output
            except BaseException as error:
                if entry is not None:
                    entry.update(status='failed', error_type=type(error).__name__)
                raise
            finally:
                elapsed = round(time.perf_counter()-started, 3)
                if entry is not None:
                    entry['elapsed_s'] = elapsed
                    if checkpoint:
                        checkpoint()
                if log_stages:
                    print(json.dumps({'id':result['id'], 'stage':purpose, 'event':'finished',
                                      'status':status, 'elapsed_s':elapsed}), flush=True)

        options['complete'] = observed_complete
        return original_run(*positional, **options)

    corrective_rag.run_corrective = observed_run
    try:
        yield
    finally:
        corrective_rag.run_corrective = original_run


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, default=Path('evals/production_golden.jsonl'))
    parser.add_argument('--mode', choices=['legacy', 'coverage', 'corrective'], default='legacy')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--answers', action='store_true')
    parser.add_argument('--case-id',action='append',default=[],help='Evaluate only these IDs (repeatable)')
    parser.add_argument('--log-stages',action='store_true',help='Log stage names/timings, never prompt or reasoning text')
    parser.add_argument('--capture-generations',action='store_true',help='Private artifact: capture answer/source-reading callback outputs only')
    parser.add_argument('--model',help='Construct RAGAgent directly with this model; do not load or change provider settings')
    parser.add_argument('--aliases-file',type=Path,help='Frozen ready-global physical-to-display aliases; bypass database listing')
    parser.add_argument('--snapshot-label',help='Operator label for the frozen alias snapshot')
    parser.add_argument('--system-prompt-file',type=Path,
                        help='Exact UTF-8 user preset for both answer arms; report hashes, not prompt text')
    parser.add_argument('--system-prompt-label',help='Human label only; does not resolve or modify UI presets')
    parser.add_argument('--overwrite',action='store_true',help='Explicitly replace an existing output report; never resumes calls')
    args = parser.parse_args(argv)
    if args.mode=='coverage' and args.answers:
        parser.error('coverage is retrieval-only; use legacy or corrective for answer comparisons')
    if args.capture_generations and args.mode != 'corrective':
        parser.error('capture-generations requires corrective mode')
    if args.system_prompt_label and not args.system_prompt_file:
        parser.error('system-prompt-label requires system-prompt-file')
    if args.system_prompt_file and not (args.answers or args.mode == 'corrective'):
        parser.error('system-prompt-file requires answer generation')
    protected = {args.dataset.resolve()}
    if args.aliases_file:
        protected.add(args.aliases_file.resolve())
    if args.system_prompt_file:
        protected.add(args.system_prompt_file.resolve())
    if args.output.resolve() in protected:
        parser.error('Output must not overwrite a dataset or alias snapshot')
    if args.output.exists() and not args.overwrite:
        parser.error('Output already exists; use a new path or explicit --overwrite')
    if args.limit < 0:
        parser.error('limit must be nonnegative')
    prompt_text, prompt_snapshot = None, {'kind':'runtime-default-unverified-preset'}
    if args.system_prompt_file:
        try:
            raw_prompt = args.system_prompt_file.read_bytes()
            prompt_text = raw_prompt.decode('utf-8-sig')
        except (OSError, UnicodeError):
            parser.error('Cannot read system-prompt-file as UTF-8')
        if not prompt_text.strip():
            parser.error('system-prompt-file must not be empty')
        prompt_snapshot = {'kind':'explicit-utf8-file',
                          'label':args.system_prompt_label or args.system_prompt_file.stem,
                          'file_sha256':hashlib.sha256(raw_prompt).hexdigest(),
                          'effective_text_sha256':_sha(prompt_text), 'chars':len(prompt_text),
                          'ui_preset_parity':'requires comparison with deployed preset hash'}
    cases = [json.loads(line) for line in args.dataset.read_text(encoding='utf-8-sig').splitlines() if line.strip()]
    if not cases or any(not isinstance(case, dict) or not isinstance(case.get('id'), str) or not case['id']
        or not isinstance(case.get('question'), str) or not case['question'].strip() for case in cases):
        parser.error('Dataset requires nonempty id and question strings')
    dataset_case_ids = [case['id'] for case in cases]
    if len(set(dataset_case_ids)) != len(dataset_case_ids):
        parser.error('Duplicate case IDs')
    for case in cases:
        try:
            _history_messages(case)
        except ValueError:
            parser.error('Invalid chat_history or previous_questions')
    if args.case_id:
        cases = [case for case in cases if case['id'] in args.case_id]
        if {case['id'] for case in cases} != set(args.case_id):
            parser.error('Unknown case-id')
    if args.limit:
        cases = cases[:args.limit]
    from rag_agent import refine_prism_query
    # A missing shadow/release resource is a harness failure, not 130 retrieval
    # failures. Fail before spending model time or overwriting a useful report.
    from domain_routing import route_query
    route_query('Проверка конфигурации')
    records = []
    if args.aliases_file:
        aliases, manifest, snapshot = _frozen_aliases(args.aliases_file, args.snapshot_label)
    else:
        from db_store import list_documents
        records = [r for r in list_documents() if r['status']=='ready' and not r.get('project_id') and not r.get('chat_id')]
        aliases = {Path(r['source_path']).name.casefold():r['file_name'] for r in records}
        manifest = [{key:row.get(key) for key in ('id','file_name','sha256','version','uploaded_at')} for row in records]
        snapshot = {'kind':'runtime-ready-global-list', 'database_queried':True}
    if not aliases:
        raise ValueError('Benchmark has no ready global sources')
    if args.model:
        from rag_agent import RAGAgent
        agent = RAGAgent(llm_model=args.model)
    else:
        from web_ui import get_agent
        agent = get_agent()
    supports_pipeline = 'pipeline_mode' in inspect.signature(agent.ask).parameters
    if args.mode == 'corrective' and not supports_pipeline:
        raise ValueError('Corrective benchmark requires an explicit pipeline_mode contract')
    if prompt_text is not None and 'system_prompt' not in inspect.signature(agent.ask).parameters:
        raise ValueError('Benchmark agent cannot accept an explicit system prompt')
    report = {'mode':args.mode, 'dataset':str(args.dataset), 'answers_enabled':args.answers,
              'scope':'ready-global-only', 'source_count':len(aliases), 'cases':[],
              'source_snapshot':snapshot, 'capture_generations':args.capture_generations,
              'system_prompt':prompt_snapshot,
              'dataset_case_count':len(dataset_case_ids),'dataset_case_ids':dataset_case_ids,
              'selected_case_ids':[case['id'] for case in cases],
              'started_at':datetime.now(timezone.utc).isoformat(),
              'dataset_sha256':hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
              'runtime':{'model':getattr(agent,'llm_model','unknown'),
                         **{key:os.getenv(key) for key in ('QDRANT_COLLECTION','EMBED_MODEL','RAG_RERANKER_MODE',
                            'RAG_EVIDENCE_MAX_CHARS','RAG_PLANNER_TOKEN_BUDGET','RAG_PLANNER_THINKING',
                            'RAG_REFERENCE_EXPANSION','RAG_SOURCE_READING','RAG_TASK_SCOPE_CHECK')}},
              'source_manifest':manifest}
    for entry,row in zip(report['source_manifest'],records):
        source_path = Path(row['source_path'])
        if source_path.is_file():
            entry['content_sha256'] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    root = Path(__file__).resolve().parents[1]
    report['implementation_sha256'] = {name:hashlib.sha256((root/name).read_bytes()).hexdigest()
        for name in ('rag_agent.py','evidence_selection.py','corrective_rag.py','grounding_verifier.py',
                     'conversation_context.py','domain_routing.py','llm_providers.py','evidence_anchors.py',
                     'source_reader.py','search_focus.py','user_task.py','evals/run_pipeline_benchmark.py') if (root/name).is_file()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    report['summary'] = _summary([])
    _write_report(args.output, report, exclusive=not args.overwrite)

    def checkpoint():
        report['summary'] = _summary(report['cases'])
        _write_report(args.output, report)

    for case in cases:
        start = time.perf_counter()
        default_split = ('legacy-regression' if case.get('expected_sources') or case.get('expected_locators')
                         else 'unscored-runtime')
        result = {'id':case['id'], 'question':case['question'], 'split':case.get('split',default_split), 'retrieval':None}
        if 'chat_history' in case:
            result['chat_history'] = case['chat_history']
        if args.capture_generations:
            result['generations'] = []
        report['cases'].append(result)
        try:
            question = build_retrieval_question(case, args.mode)
            result['retrieval_question'] = question
            if args.answers or args.mode == 'corrective':
                mode_option = {'pipeline_mode':'on' if args.mode=='corrective' else 'off'} if supports_pipeline else {}
                prompt_option = {'system_prompt':prompt_text} if prompt_text is not None else {}
                with _observe_generations(result, log_stages=args.log_stages and args.mode=='corrective',
                                          capture=args.capture_generations, checkpoint=checkpoint):
                    answer, _, _ = agent.ask(case['question'],retrieval_query=question,source_aliases=aliases,
                                            shared_cache_sources=set(),**mode_option,**prompt_option)
                context = agent.last_retrieved_context() or []
                result['answer'] = answer
                result['trace'] = agent.last_retrieval_trace() if hasattr(agent,'last_retrieval_trace') else {}
            else:
                understanding = agent.understand_query(question)
                kwargs = {} if args.mode=='legacy' else {'selection_mode':'coverage'}
                context = agent.retrieve_context(understanding.canonical_question,10,refine_prism_query(understanding.canonical_question),
                    retrieval_queries=understanding.retrieval_queries,source_aliases=aliases,
                    subject_question=question,**kwargs)
                result['canonical_question'] = understanding.canonical_question
            result['context'] = context
            result['retrieval'] = _retrieval_score(case, context, time.perf_counter()-start)
        except Exception as exc:
            # HTTP exceptions can contain credentials or raw response bodies.
            result['error'] = type(exc).__name__
        result['latency_s'] = round(time.perf_counter()-start,3)
        checkpoint()
        print(json.dumps({'id':case['id'],'elapsed_s':result['latency_s'],'error':result.get('error'),
                          'source_recall':(result.get('retrieval') or {}).get('source_recall')},ensure_ascii=False),flush=True)
    report['completed_at'] = datetime.now(timezone.utc).isoformat()
    checkpoint()
    # Captured failures are durable evidence, not a successful batch job.
    return 1 if report['summary']['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())

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

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--dataset', type=Path, default=Path('evals/production_golden.jsonl'))
    parser.add_argument('--mode', choices=['legacy', 'coverage', 'corrective'], default='legacy')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--limit', type=int, default=0)
    parser.add_argument('--answers', action='store_true')
    parser.add_argument('--case-id',action='append',default=[],help='Evaluate only these IDs (repeatable)')
    parser.add_argument('--log-stages',action='store_true',help='Log stage names/timings, never prompt or reasoning text')
    args = parser.parse_args()
    if args.mode=='coverage' and args.answers:
        parser.error('coverage is retrieval-only; use legacy or corrective for answer comparisons')
    from quality_eval import QualityCase, score_retrieval
    from db_store import list_documents
    from web_ui import get_agent
    from rag_agent import refine_prism_query
    # A missing shadow/release resource is a harness failure, not 130 retrieval
    # failures. Fail before spending model time or overwriting a useful report.
    from domain_routing import route_query
    route_query('Проверка конфигурации')
    records = [r for r in list_documents() if r['status']=='ready' and not r.get('project_id') and not r.get('chat_id')]
    aliases = {Path(r['source_path']).name.casefold():r['file_name'] for r in records}
    agent = get_agent()
    if args.log_stages and args.mode=='corrective':
        import corrective_rag
        original_run = corrective_rag.run_corrective

        def logged_run(*positional,**options):
            original_complete = options['complete']

            def logged_complete(prompt,purpose):
                started = time.perf_counter()
                print(json.dumps({'stage':purpose,'event':'started'}),flush=True)
                try:
                    return original_complete(prompt,purpose)
                finally:
                    print(json.dumps({'stage':purpose,'event':'finished','elapsed_s':round(time.perf_counter()-started,3)}),flush=True)

            options['complete'] = logged_complete
            return original_run(*positional,**options)

        corrective_rag.run_corrective = logged_run
    cases = [json.loads(line) for line in args.dataset.read_text(encoding='utf-8').splitlines() if line.strip()]
    dataset_case_ids = [case['id'] for case in cases]
    if args.case_id:
        cases = [case for case in cases if case['id'] in args.case_id]
        if {case['id'] for case in cases} != set(args.case_id):
            parser.error('Unknown case-id')
    if args.limit:
        cases = cases[:args.limit]
    report = {'mode':args.mode, 'dataset':str(args.dataset), 'answers_enabled':args.answers,
              'scope':'ready-global-only', 'source_count':len(aliases), 'cases':[],
              'dataset_case_count':len(dataset_case_ids),'dataset_case_ids':dataset_case_ids,
              'selected_case_ids':[case['id'] for case in cases],
              'started_at':datetime.now(timezone.utc).isoformat(),
              'dataset_sha256':hashlib.sha256(args.dataset.read_bytes()).hexdigest(),
              'runtime':{'model':getattr(agent,'llm_model','unknown'),
                         **{key:os.getenv(key) for key in ('QDRANT_COLLECTION','EMBED_MODEL','RAG_RERANKER_MODE',
                            'RAG_EVIDENCE_MAX_CHARS','RAG_PLANNER_TOKEN_BUDGET','RAG_PLANNER_THINKING',
                            'RAG_REFERENCE_EXPANSION')}},
              'source_manifest':[{key:row.get(key) for key in ('id','file_name','sha256','version','uploaded_at')}
                                 for row in records]}
    for entry,row in zip(report['source_manifest'],records):
        source_path = Path(row['source_path'])
        if source_path.is_file():
            entry['content_sha256'] = hashlib.sha256(source_path.read_bytes()).hexdigest()
    root = Path(__file__).resolve().parents[1]
    report['implementation_sha256'] = {name:hashlib.sha256((root/name).read_bytes()).hexdigest()
        for name in ('rag_agent.py','evidence_selection.py','corrective_rag.py','grounding_verifier.py',
                     'conversation_context.py','domain_routing.py','llm_providers.py','evidence_anchors.py') if (root/name).is_file()}
    args.output.parent.mkdir(parents=True, exist_ok=True)
    for case in cases:
        start = time.perf_counter()
        result = {'id':case['id'], 'question':case['question'], 'split':case.get('split','legacy-regression')}
        try:
            question = case['question']
            if case.get('previous_questions'):
                messages = [{'chat_id':'benchmark', 'role':'user', 'content':q} for q in case['previous_questions']]
                if args.mode=='legacy':
                    from web_ui import contextualize_retrieval_question
                    question = contextualize_retrieval_question(question,{'messages':messages},'benchmark')
                else:
                    from conversation_context import resolve_conversation_context
                    question = resolve_conversation_context(question,messages,chat_id='benchmark').retrieval_question
            if args.answers or args.mode == 'corrective':
                mode_option = {'pipeline_mode':'on' if args.mode=='corrective' else 'off'} if 'pipeline_mode' in inspect.signature(agent.ask).parameters else {}
                answer, _, _ = agent.ask(case['question'],retrieval_query=question,source_aliases=aliases,
                                        shared_cache_sources=set(),**mode_option)
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
            score_case = QualityCase(id=case['id'],question=case['question'],
                                     expected_sources=tuple(case.get('expected_sources',())),
                                     expected_locators=tuple(case.get('expected_locators',())))
            result['retrieval'] = asdict(score_retrieval(score_case,{'context':context},time.perf_counter()-start))
        except Exception as exc:
            result['error'] = type(exc).__name__+': '+str(exc)
        result['latency_s'] = round(time.perf_counter()-start,3)
        report['cases'].append(result)
        report['summary'] = {'completed':len(report['cases']), 'errors':sum('error' in r for r in report['cases']),
            'mean_source_recall':mean(r.get('retrieval',{}).get('source_recall',0) for r in report['cases']),
            'mean_latency_s':mean(r['latency_s'] for r in report['cases']),
            'semantic_quality':'not certified; inspect raw answers against reviewed requirements'}
        args.output.write_text(json.dumps(report,ensure_ascii=False,indent=2,default=str),encoding='utf-8')
        print(json.dumps({'id':case['id'],'elapsed_s':result['latency_s'],'error':result.get('error'),
                          'source_recall':result.get('retrieval',{}).get('source_recall')},ensure_ascii=False),flush=True)


if __name__ == '__main__':
    main()

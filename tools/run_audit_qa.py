"""Keep raw question/evidence/answer records from isolated private QA chats."""
from __future__ import annotations
import argparse
import json
import os
import sys
import time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from quality_eval import ApiClient

QUESTIONS = [
    'Что ведется в договорных условиях?',
    'Как рассчитывается 2 ЦК по постановлениям?',
    'Чем отличаются расчеты по 3 ЦК и по 6 ЦК?',
    'Как рассчитывается цена на электроэнергию по 6 ЦК?',
    'Как рассчитывается 1ЦК?',
    'В каких документах написано про ценовые категории?',
    'Какие виды начислений реализованы?',
    'Как задаются авансы?',
]

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--retrieval-only', action='store_true')
    args = parser.parse_args()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    client = ApiClient('https://5.35.120.112:8443', insecure=True)
    client.login(os.environ['RAG_EVAL_EMAIL'], os.environ['RAG_EVAL_PASSWORD'])
    records = []
    for index, question in enumerate(QUESTIONS, 1):
        chat = client.request('/api/chats', 'POST', {'title': f'QA 12.10: {question}', 'project_id': None})
        chat_id = chat['id']
        record = {'question': question, 'chat_id': chat_id}
        records.append(record)
        started = time.monotonic()
        try:
            record['retrieval'] = client.request('/api/debug', 'POST', {'question': question, 'chat_id': chat_id})
            if not args.retrieval_only:
                client.request(f'/api/chats/{chat_id}/messages', 'POST', {'role': 'user', 'content': question})
                result = client.request('/api/ask', 'POST', {'question': question, 'chat_id': chat_id})
                deadline = time.monotonic() + 900
                while result.get('status') == 'running' and time.monotonic() < deadline:
                    time.sleep(2)
                    messages = client.request(f'/api/chats/{chat_id}/messages')['items']
                    message = next((m for m in messages if m['id'] == result.get('message_id')), None)
                    if message and message.get('status') != 'running':
                        result = message
                record['generation'] = result
        except Exception as exc:
            record['error'] = str(exc)
        record['latency_s'] = round(time.monotonic() - started, 2)
        args.output.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding='utf-8')
        context = record.get('retrieval', {}).get('context', [])
        print(f'{index}/{len(QUESTIONS)} {question}: ' + str([(c.get('file'), c.get('section_path')) for c in context[:3]]) + ' error=' + record.get('error', ''), flush=True)

if __name__ == '__main__':
    main()

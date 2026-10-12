import asyncio
import json
import sys
from types import SimpleNamespace
from unittest.mock import Mock
import pytest


def test_ingestion_embedding_backend_is_not_the_generation_backend(monkeypatch, capsys):
    import ingestion_task
    constructor = Mock()
    constructor.return_value.ingest.return_value = 7
    monkeypatch.setitem(sys.modules, 'rag_agent', SimpleNamespace(RAGAgent=constructor))
    monkeypatch.setattr(sys, 'argv', ['ingestion_task', 'source.docx'])
    monkeypatch.setenv('RAG_INGESTION_EMBED_URL', 'http://index-embedding:11434')
    assert ingestion_task.main() == 0
    constructor.assert_called_once_with(embedding_base_url='http://index-embedding:11434')
    assert json.loads(capsys.readouterr().out)['chunks'] == 7


@pytest.mark.asyncio
async def test_ingestion_drains_both_pipes_before_waiting_for_exit(monkeypatch):
    import ingestion_worker as worker
    real_start = asyncio.create_subprocess_exec
    async def start(*args, **kwargs):
        code = "import sys,json;sys.stdout.write('x'*262144+'\\n');sys.stderr.write('w'*262144);print(json.dumps({'chunks':17}))"
        return await real_start(sys.executable, '-c', code, **kwargs)
    store = SimpleNamespace(get_job=Mock(return_value={'cancel_requested': False}), update_job=Mock())
    monkeypatch.setattr(worker.asyncio, 'create_subprocess_exec', start)
    monkeypatch.setattr(worker, 'get_store', lambda: store)
    update = Mock()
    monkeypatch.setattr(worker, 'update_document', update)
    result = await asyncio.wait_for(worker.ingest_document_job({}, 'job', {'path': 'source.docx', 'document_id': 'doc'}), 5)
    assert result['chunks'] == 17
    assert update.call_args.args[1]['status'] == 'ready'


@pytest.mark.asyncio
async def test_deletion_drains_large_logs_and_removes_only_selected_file(monkeypatch, tmp_path):
    import ingestion_worker as worker
    source = tmp_path / 'source.md'
    source.write_text('fixture', encoding='utf-8')
    real_start = asyncio.create_subprocess_exec
    async def start(*args, **kwargs):
        code = "import sys,json;sys.stdout.write('x'*262144+'\\n');sys.stderr.write('w'*262144);print(json.dumps({'deleted':1}))"
        return await real_start(sys.executable, '-c', code, **kwargs)
    store = SimpleNamespace(get_job=Mock(return_value={'cancel_requested': False}), update_job=Mock())
    monkeypatch.setattr(worker.asyncio, 'create_subprocess_exec', start)
    monkeypatch.setattr(worker, 'get_store', lambda: store)
    monkeypatch.setattr(worker, 'UPLOAD_DIR', tmp_path)
    result = await asyncio.wait_for(worker.delete_document_job({}, 'job', {'path': str(source)}), 5)
    assert result['deleted'] == 1 and not source.exists()

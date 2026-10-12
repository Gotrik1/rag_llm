import sys
import types
import uuid
import pytest
from tools.rebuild_shadow_index import main


def _inventory(monkeypatch, tmp_path, extra=()):
    source = tmp_path / 'source.md'
    source.write_text('ready source', encoding='utf-8')
    monkeypatch.setitem(sys.modules, 'db_store', types.SimpleNamespace(list_documents=lambda: [
        {'id': uuid.uuid4(), 'source_path': str(source), 'status': 'ready'},
        {'id': 'b', 'source_path': 'not-created-yet', 'status': 'queued'},
    ]))
    monkeypatch.setattr(sys, 'argv', ['rebuild', '--collection', 'shadow', '--bm25-dir', str(tmp_path / 'new'), *extra])


def test_inventory_does_not_create_index(monkeypatch, tmp_path, capsys):
    _inventory(monkeypatch, tmp_path, ['--inventory-only'])
    main()
    assert '"files": 1' in capsys.readouterr().out
    assert not (tmp_path / 'new').exists()


def test_refuses_live_collection(monkeypatch, tmp_path):
    _inventory(monkeypatch, tmp_path)
    monkeypatch.setenv('QDRANT_COLLECTION', 'shadow')
    with pytest.raises(RuntimeError, match='live collection'):
        main()


def test_refuses_existing_directory(monkeypatch, tmp_path):
    _inventory(monkeypatch, tmp_path)
    monkeypatch.setenv('QDRANT_COLLECTION', 'live')
    (tmp_path / 'new').mkdir()
    with pytest.raises(RuntimeError, match='must not exist'):
        main()

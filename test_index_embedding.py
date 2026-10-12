import importlib.util
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

def test_gpu_arena_is_shrunk_per_run_without_changing_input(monkeypatch):
    options = Mock()
    runtime = SimpleNamespace(RunOptions=Mock(return_value=options))
    monkeypatch.setitem(__import__('sys').modules, 'onnxruntime', runtime)
    monkeypatch.setitem(__import__('sys').modules, 'torch', SimpleNamespace())
    monkeypatch.setitem(__import__('sys').modules, 'fastembed', SimpleNamespace(TextEmbedding=Mock()))
    path = Path(__file__).parent / 'deploy/index-embedding/app.py'
    spec = importlib.util.spec_from_file_location('index_gpu_contract', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    session = Mock()
    wrapper = module.ShrinkingSession(session)
    inputs = {'input_ids': [1, 2, 3]}
    assert wrapper.run(['output'], inputs) is session.run.return_value
    options.add_run_config_entry.assert_called_once_with('memory.enable_memory_arena_shrinkage', 'gpu:0')
    session.run.assert_called_once_with(['output'], inputs, options)

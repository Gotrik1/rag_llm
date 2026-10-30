"""Provider switching/probe contracts; no real config, network or completion."""
from unittest.mock import Mock

import pytest
import web_ui


@pytest.fixture
def handler(monkeypatch):
    monkeypatch.setattr(web_ui, 'load_provider_config', lambda: {
        'provider': 'gigachat', 'model': 'synthetic-cloud',
        'base_url': 'https://synthetic-cloud.example.test/api'})
    monkeypatch.setattr(web_ui, 'OLLAMA_BASE_URL', 'http://configured-bridge.example.test:11434')
    monkeypatch.setattr(web_ui, 'LLM_MODEL', 'configured-default-model')
    monkeypatch.setattr(web_ui, 'ProviderLLM', Mock(side_effect=AssertionError('No completions required')))
    monkeypatch.setattr(web_ui, 'save_provider_config', Mock(side_effect=AssertionError('Probe must not save')))
    result = object.__new__(web_ui.Handler)
    result.send_json = Mock()
    return result


def test_server_advertises_effective_ollama_default_without_mutating_cloud_defaults(handler):
    before = dict(web_ui.DEFAULTS)
    handler.handle_provider_get()
    defaults = handler.send_json.call_args.args[0]['defaults']
    assert defaults['ollama'] == {'base_url': web_ui.OLLAMA_BASE_URL, 'model': web_ui.LLM_MODEL}
    assert web_ui.DEFAULTS == before


def test_ollama_probe_calls_discovery_on_explicit_custom_endpoint(handler, monkeypatch):
    probe = Mock(return_value=['synthetic-local'])
    monkeypatch.setattr(web_ui, 'ollama_models', probe)
    handler.handle_provider_test({'provider': 'ollama', 'model': 'synthetic-local',
        'base_url': 'https://custom-ollama.example.test/prefix'})
    probe.assert_called_once_with(base_url='https://custom-ollama.example.test/prefix')
    assert 'Генерация не проверялась' in handler.send_json.call_args.args[0]['message']


def test_ollama_probe_uses_advertised_server_endpoint_when_blank(handler, monkeypatch):
    probe = Mock(return_value=['synthetic-local'])
    monkeypatch.setattr(web_ui, 'ollama_models', probe)
    handler.handle_provider_test({'provider': 'ollama', 'model': 'synthetic-local', 'base_url': ''})
    probe.assert_called_once_with(base_url=web_ui.OLLAMA_BASE_URL)


def test_partial_cloud_to_ollama_probe_does_not_inherit_cloud_endpoint(handler, monkeypatch):
    probe = Mock(return_value=['synthetic-local'])
    monkeypatch.setattr(web_ui, 'ollama_models', probe)
    handler.handle_provider_test({'provider': 'ollama', 'model': 'synthetic-local'})
    probe.assert_called_once_with(base_url=web_ui.OLLAMA_BASE_URL)


def test_same_provider_probe_preserves_saved_custom_ollama_endpoint(handler, monkeypatch):
    monkeypatch.setattr(web_ui, 'load_provider_config', lambda: {'provider': 'ollama',
        'model': 'synthetic-local', 'base_url': 'https://custom-ollama.example.test/prefix'})
    probe = Mock(return_value=['synthetic-local'])
    monkeypatch.setattr(web_ui, 'ollama_models', probe)
    handler.handle_provider_test({'provider': 'ollama'})
    probe.assert_called_once_with(base_url='https://custom-ollama.example.test/prefix')


def test_ollama_probe_does_not_claim_success_when_server_unreachable(handler, monkeypatch):
    monkeypatch.setattr(web_ui, 'ollama_models', Mock(side_effect=RuntimeError('Ollama недоступен')))
    handler.handle_provider_test({'provider': 'ollama', 'model': 'synthetic-local', 'base_url': ''})
    assert handler.send_json.call_args.args[1] == 503
    assert 'message' not in handler.send_json.call_args.args[0]


def test_ollama_probe_does_not_claim_selected_model_installed_when_missing(handler, monkeypatch):
    monkeypatch.setattr(web_ui, 'ollama_models', Mock(return_value=['other-model']))
    handler.handle_provider_test({'provider': 'ollama', 'model': 'missing-model', 'base_url': ''})
    assert handler.send_json.call_args.args[1] == 400
    assert 'message' not in handler.send_json.call_args.args[0]


@pytest.mark.parametrize('endpoint', ['ftp://host.test', 'https://user:secret@host.test',
    'https://host.test?token=private', 'https://host.test/#fragment', 'not-a-url'])
def test_ollama_probe_reuses_runtime_endpoint_validation(handler, monkeypatch, endpoint):
    probe = Mock(side_effect=AssertionError('Invalid endpoint must fail before I/O'))
    monkeypatch.setattr(web_ui, 'ollama_models', probe)
    handler.handle_provider_test({'provider': 'ollama', 'model': 'synthetic-local', 'base_url': endpoint})
    assert handler.send_json.call_args.args[1] == 400
    assert endpoint not in str(handler.send_json.call_args)
    probe.assert_not_called()

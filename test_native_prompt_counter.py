"""Request/model binding and complete-input accounting, without model calls."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from types import SimpleNamespace

import pytest

import native_prompt_counter as module
from provider_runtime import capture_runtime


def runtime(model='neutral-model'):
    return capture_runtime({'provider': 'ollama', 'model': model}, default_model=model,
        ollama_base_url='http://127.0.0.1:11434', ollama_profiles={},
        default_ollama_profile={'context_window': 16384, 'num_predict': -1},
        client_factory=lambda spec: SimpleNamespace(complete=lambda *a, **k: pytest.fail('No model call allowed')),
        environ={})


@pytest.fixture
def bound_fixture(monkeypatch):
    tokenizers = pytest.importorskip('tokenizers')
    tokenizer = tokenizers.Tokenizer(tokenizers.models.BPE(
        vocab={'[UNK]': 0, 'x': 1}, merges=[], unk_token='[UNK]'))
    tokenizer.add_special_tokens(['<|im_start|>', '<|im_end|>'])
    raw = tokenizer.to_str().encode()
    document = json.loads(raw)
    # A small synthetic vocabulary exercises the same structural attestation
    # path. Production still accepts only the fixed official asset checksum.
    monkeypatch.setattr(module, '_TOKENIZER_SHA', hashlib.sha256(raw).hexdigest())
    template = 'controlled-test-template'
    monkeypatch.setattr(module, '_TEMPLATE_SHA', hashlib.sha256(template.encode()).hexdigest())
    native = [None] * 4
    for text, index in document['model']['vocab'].items(): native[index] = text
    for row in document['added_tokens']: native[row['id']] = row['content']
    attestation = {'model': 'neutral-model', 'digest': 'a' * 64, 'show': {
        'template': template, 'system': 'unchanged system', 'model_info': {
            'tokenizer.ggml.model': 'gpt2', 'tokenizer.ggml.pre': 'qwen2',
            'tokenizer.ggml.add_bos_token': False, 'tokenizer.ggml.tokens': native,
            'tokenizer.ggml.merges': [], 'tokenizer.ggml.token_type': [1, 1, 3, 3]}}}
    return runtime(), raw, attestation, tokenizer


def bind(fixture):
    capture, raw, attestation, _ = fixture
    return module.bind_attested_qwen2_counter(capture, tokenizer_bytes=raw, attestation=attestation)


def test_normal_capture_cannot_guess_counter_from_model_name():
    assert module.count_bound_prompt('x', runtime('qwen2.5:7b')) is None


def test_counts_the_whole_unchanged_prompt_and_captured_system_template(bound_fixture):
    capture, _, attestation, tokenizer = bound_fixture
    bound = bind(bound_fixture)
    text = 'x\nТекст и $x=1$ <|im_start|> цитата'
    wire = '<|im_start|>system\nunchanged system<|im_end|>\n<|im_start|>user\n' + text + '<|im_end|>\n<|im_start|>assistant\n'
    count = module.count_bound_prompt(text, bound)
    assert count['tokens'] == len(tokenizer.encode(wire, add_special_tokens=False).ids)
    assert count['template_included'] is True
    assert count['provenance']['remote_freshness_automatic'] is False
    assert count['provenance']['engine_version']
    assert module.count_bound_prompt('', bound) is None
    assert module.count_bound_prompt(text, capture) is None
    assert '_prompt_counter' not in bound.model_dump()
    assert 'unchanged system' not in repr(bound._prompt_counter)
    with pytest.raises(AttributeError): bound._prompt_counter._prefix = 'changed'


@pytest.mark.parametrize('mutate', [
    lambda a: a.update(model='another-model'),
    lambda a: a.update(digest='not-a-digest'),
    lambda a: a['show'].update(template='another-template'),
    lambda a: a['show'].update(system=[]),
    lambda a: a['show']['model_info'].update({'tokenizer.ggml.pre': 'other'}),
    lambda a: a['show']['model_info'].update({'tokenizer.ggml.model': 'llama'}),
    lambda a: a['show']['model_info'].update({'tokenizer.ggml.add_bos_token': True}),
    lambda a: a['show']['model_info']['tokenizer.ggml.tokens'].__setitem__(1, 'changed'),
    lambda a: a['show']['model_info']['tokenizer.ggml.tokens'].__setitem__(2, 'changed'),
    lambda a: a['show']['model_info'].update({'tokenizer.ggml.merges': ['x x']}),
    lambda a: a['show']['model_info']['tokenizer.ggml.token_type'].__setitem__(0, 3),
    lambda a: a['show']['model_info']['tokenizer.ggml.token_type'].__setitem__(2, 1),
])
def test_unattested_or_incompatible_native_contract_is_rejected(bound_fixture, mutate):
    capture, raw, original, _ = bound_fixture
    attestation = deepcopy(original); mutate(attestation)
    with pytest.raises(ValueError, match='^native_prompt_counter_binding_rejected$'):
        module.bind_attested_qwen2_counter(capture, tokenizer_bytes=raw, attestation=attestation)


def test_asset_changes_and_forged_mapping_cannot_become_exact(bound_fixture):
    capture, raw, attestation, _ = bound_fixture
    with pytest.raises(ValueError, match='^native_prompt_counter_binding_rejected$'):
        module.bind_attested_qwen2_counter(capture, tokenizer_bytes=raw + b' ', attestation=attestation)
    assert module.count_bound_prompt('x', replace(capture, _prompt_counter={'tokens': 1})) is None


def test_other_request_settings_cannot_reuse_attestation_and_identity_is_cache_partition(bound_fixture):
    capture = bind(bound_fixture)
    other = replace(capture, spec=capture.spec.with_settings(context_window=32768))
    assert module.count_bound_prompt('x', other) is None
    changed = deepcopy(bound_fixture[2]); changed['digest'] = 'b' * 64
    second = module.bind_attested_qwen2_counter(bound_fixture[0], tokenizer_bytes=bound_fixture[1], attestation=changed)
    assert module.count_bound_prompt('x', capture)['identity'] != module.count_bound_prompt('x', second)['identity']
    assert capture.spec.settings['output_token_limit'] == -1
    assert capture.spec.settings['context_window'] == 16384


def test_parallel_prompts_keep_independent_counts_and_bindings(bound_fixture):
    bound = bind(bound_fixture)
    prompts = ['x' * i for i in range(1, 20)]
    with ThreadPoolExecutor(max_workers=4) as executor:
        observed = list(executor.map(lambda p: module.count_bound_prompt(p, bound), prompts))
    assert observed == [module.count_bound_prompt(p, bound) for p in prompts]
    assert len({row['identity'] for row in observed}) == 1

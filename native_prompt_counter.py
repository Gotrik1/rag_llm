"""Optional, request-bound Qwen2 counter prepared from operator attestation.

No discovery, network, environment lookup, model selection, or registry occurs
here. The caller must attest the actual model tag/digest and show metadata
outside the completion path, and check them again after a diagnostic run.
Normal production captures have no counter and retain their fallback policy.
Configuration identity alone is deliberately not remote weight attestation.
"""
from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from typing import Any, Mapping


_TOKENIZER_SHA = 'c0382117ea329cdf097041132f6d735924b697924d6f6fc3945713e96ce87539'
_TEMPLATE_SHA = 'eb4402837c7829a690fa845de4d7f3fd842c2adee476d5341da8a46ea9255175'
_VERSION = 'attested-qwen2-single-user-v1'


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, ensure_ascii=False,
                      separators=(',', ':'), allow_nan=False).encode('utf-8')


def _digest(value: Any) -> bool:
    return isinstance(value, str) and len(value) == 64 and all(c in '0123456789abcdef' for c in value)


class _BoundCounter:
    __slots__ = ('_runtime_identity', '_identity', '_provenance_json', '_tokenizer', '_prefix')

    def __init__(self, runtime_identity, identity, provenance, tokenizer, prefix):
        for name, value in (('_runtime_identity', runtime_identity), ('_identity', identity),
                            ('_provenance_json', _json(provenance)), ('_tokenizer', tokenizer),
                            ('_prefix', prefix)):
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        raise AttributeError('Prompt counter binding is immutable')

    def __repr__(self):
        return '<RequestPromptCounter private_binding=redacted>'

    def __reduce_ex__(self, protocol):
        raise TypeError('Prompt counter binding is not serializable')

    def count(self, prompt):
        # Both observed Ollama adapters use one user message with this pinned
        # template and the captured built-in system. Prompt bytes are unchanged.
        """Count exact pinned chat-template tokens and return detached provenance for this immutable runtime binding."""
        wire = self._prefix + '<|im_start|>user\n' + prompt + '<|im_end|>\n<|im_start|>assistant\n'
        return {'tokens': len(self._tokenizer.encode(wire, add_special_tokens=False).ids),
                'identity': self._identity, 'estimator': _VERSION,
                'template_included': True, 'provenance': json.loads(self._provenance_json)}


def bind_attested_qwen2_counter(runtime, *, tokenizer_bytes: bytes,
                               attestation: Mapping[str, Any]):
    """Return a new snapshot; never infer a counter from a model name.

    attestation is a trusted controller's {model, digest, show} observation,
    not client/user metadata. It cannot detect subsequent remote model reloads.
    Only the pinned single-user/no-tools/no-request-system adapter contract is
    supported. The caller owns freshness and before/after remote verification.
    """
    try:
        spec = runtime.spec
        if spec.provider != 'ollama' or attestation['model'] != spec.model or not _digest(attestation['digest']):
            raise ValueError
        if type(tokenizer_bytes) is not bytes or _sha(tokenizer_bytes) != _TOKENIZER_SHA:
            raise ValueError
        show = attestation['show']
        template, system, info = show['template'], show.get('system', ''), show['model_info']
        if not isinstance(system, str) or not isinstance(template, str) or _sha(template.encode()) != _TEMPLATE_SHA:
            raise ValueError
        if (info['tokenizer.ggml.model'] != 'gpt2' or info['tokenizer.ggml.pre'] != 'qwen2' or
                info.get('tokenizer.ggml.add_bos_token') is not False):
            raise ValueError
        document = json.loads(tokenizer_bytes)
        vocab, native = document['model']['vocab'], info['tokenizer.ggml.tokens']
        if not isinstance(native, list) or len(native) < len(vocab):
            raise ValueError
        if any(type(index) is not int or index < 0 or index >= len(native) or native[index] != token
               for token, index in vocab.items()):
            raise ValueError
        if any(type(row['id']) is not int or row['id'] < 0 or row['id'] >= len(native) or
               native[row['id']] != row['content'] for row in document['added_tokens']):
            raise ValueError
        merges = [' '.join(row) if isinstance(row, list) else row for row in document['model']['merges']]
        if merges != info['tokenizer.ggml.merges']:
            raise ValueError
        # Token IDs are insufficient if native special-token handling differs.
        types = info['tokenizer.ggml.token_type']
        if not isinstance(types, list) or len(types) != len(native):
            raise ValueError
        if any(type(types[i]) is not int or types[i] != 1 for i in vocab.values()):
            raise ValueError
        if any(types[row['id']] != 3 for row in document['added_tokens'] if row.get('special')):
            raise ValueError
        from tokenizers import Tokenizer, __version__ as engine_version
        tokenizer = Tokenizer.from_str(tokenizer_bytes.decode('utf-8'))
        provenance = {'version': _VERSION, 'model_digest': attestation['digest'],
                      'engine_version': engine_version,
                      'tokenizer_sha256': _TOKENIZER_SHA, 'template_sha256': _TEMPLATE_SHA,
                      'system_sha256': _sha(system.encode()),
                      'show_sha256': _sha(_json(show)),
                      'scope': 'operator-attested-single-user-no-tools',
                      'remote_freshness_automatic': False}
        identity = _sha(_json({'runtime': runtime.identity, 'counter': provenance}))
        counter = _BoundCounter(runtime.identity, identity, provenance, tokenizer,
                                '<|im_start|>system\n' + system + '<|im_end|>\n' if system else '')
        return replace(runtime, _prompt_counter=counter)
    except Exception:
        # Do not propagate endpoint, source, template, model metadata or library
        # errors into logs. A failed binding must not silently become exact.
        raise ValueError('native_prompt_counter_binding_rejected') from None


def count_bound_prompt(prompt: str, runtime) -> dict | None:
    """Use only an explicit immutable binding belonging to this exact capture."""
    counter = getattr(runtime, '_prompt_counter', None)
    if (not isinstance(prompt, str) or not prompt or type(counter) is not _BoundCounter or
            counter._runtime_identity != runtime.identity or runtime.spec.provider != 'ollama'):
        return None
    try:
        return counter.count(prompt)
    except Exception:
        return None

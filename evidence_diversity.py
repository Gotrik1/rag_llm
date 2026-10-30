"""Request-local SOFT lexical redundancy, never semantic equivalence or dedup.

Numbers, units, operators, identifiers and explicit conditions are conservative
conflict guards. Missing revision proof disables a comparison. The caller must
keep scope, atomic packet and uncovered-subject priorities ahead of this signal,
and apply the multiplier only to a positive ranking gain (not a negative score).
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import re
import unicodedata


HIGH_RESEMBLANCE = 0.85
PENALTY_WEIGHT = 0.85
_REVISION_FIELDS = ('document_id', 'source_id', 'revision_id', 'version_id',
                    'document_version', 'content_hash', 'sha256')
_REVISION_PROOF = frozenset(_REVISION_FIELDS[2:])
_ATOMIC_FIELDS = ('evidence_family', 'evidence_family_id', '_family_id', '_inventory_rows',
                  'reference_from', 'reference_target')
_ATOMIC_SIGNALS = ('inventory_enumeration_reserved', 'inventory_enumeration_incomplete',
                   'scoped_cohort_incomplete', 'definition_closure_incomplete')
_CONTEXT_FIELDS = ('scope_categories', 'applicability', 'evidence_role', 'section_role',
                   'excluded_topics', 'unit', 'units', 'unit_of_measure', 'measurement_unit')
_CONDITIONS = frozenset(('не', 'нет', 'без', 'кроме', 'исключая', 'если', 'когда', 'при',
                        'только', 'до', 'после', 'для', 'not', 'no', 'without', 'unless',
                        'except', 'if', 'when', 'only', 'before', 'after', 'for'))
_OPERATORS = frozenset('=<>≤≥≠≈+-−*/×·÷^_%\\{}[]')
_BREAKS = frozenset(('.', ';', ':', '!', '?', '\n', '\n\n'))
_TOKEN = re.compile(r'\n[ \t]*\n+|\n|\d+(?:[.,]\d+)*|[^\W\d_]\w*|[^\s]', re.UNICODE)
_NUMBER = re.compile(r'\d+(?:[.,]\d+)*')


def _field(item: Mapping, name: str):
    if name in item:
        return item[name]
    metadata = item.get('metadata')
    return metadata.get(name) if isinstance(metadata, Mapping) else None


def _freeze(value):
    if isinstance(value, Mapping):
        return ('mapping', tuple(sorted((str(key), _freeze(part)) for key, part in value.items())))
    if isinstance(value, (list, tuple)):
        return (type(value).__name__, tuple(_freeze(part) for part in value))
    if value is None or isinstance(value, (str, bool, int, float)):
        return (type(value).__name__, value)
    return ('unsupported', type(value).__name__)


def _known(value) -> bool:
    return (isinstance(value, str) and bool(value.strip())) or type(value) is int


def _tokens(text: str) -> tuple[str, ...]:
    text = unicodedata.normalize('NFC', text.replace('\r\n', '\n').replace('\r', '\n'))
    return tuple('\n\n' if token.startswith('\n') and len(token) > 1 else token
                 for token in _TOKEN.findall(text))


def _conditions(tokens):
    clauses = []
    for number, token in enumerate(tokens):
        if token.casefold() not in _CONDITIONS:
            continue
        end = number + 1
        while end < len(tokens) and tokens[end] not in _BREAKS:
            end += 1
        clauses.append(tokens[number:end])
    return tuple(clauses)


def _protected(tokens):
    numbers = tuple(value for token in tokens for value in _NUMBER.findall(token))
    quantities = []
    for number, token in enumerate(tokens):
        if not _NUMBER.search(token):
            continue
        right = number + 1
        while right < min(len(tokens), number + 4) and tokens[right] not in _BREAKS:
            right += 1
        quantities.append(tokens[number:right])
    identifiers = []
    for number, token in enumerate(tokens):
        if not any(char.isalpha() for char in token):
            continue
        single_symbol = len(token) == 1 and (
            token.isascii() or 'GREEK' in unicodedata.name(token, ''))
        mixed_case = (any(char.islower() for char in token)
                      and any(char.isupper() for char in token[1:]))
        operator_neighbor = any(part in _OPERATORS
                                for part in tokens[max(0, number - 2):number + 3])
        if single_symbol or mixed_case or token.isupper() or '_' in token or operator_neighbor:
            identifiers.append(token)
    return (numbers, tuple(quantities), tuple(identifiers),
            tuple(token for token in tokens if token in _OPERATORS), _conditions(tokens))


@dataclass(frozen=True)
class _Prepared:
    source: str
    revision: tuple
    context: tuple
    tokens: tuple[str, ...]
    protected: tuple
    shingles: Counter
    reason: str


def _prepare(item: Mapping, cache: dict | None) -> _Prepared:
    source = next((_field(item, name) for name in ('file', 'file_name', 'file_path')
                   if isinstance(_field(item, name), str) and _field(item, name)), '')
    source = source.replace('\\', '/').casefold()
    text = _field(item, 'text')
    text = text if isinstance(text, str) else ''
    revision = tuple(_freeze(_field(item, key)) for key in _REVISION_FIELDS)
    heading = '\n\n'.join(str(_field(item, key) or '') for key in ('section_path', 'section_title'))
    # A noun-phrase heading can itself be a condition without an "if/при"
    # marker. Do not guess that different headings are interchangeable.
    context = tuple(_freeze(_field(item, key)) for key in (
        *_CONTEXT_FIELDS, 'section_path', 'section_title'))
    signals = _field(item, 'selection_signals')
    atomic = any(_field(item, key) for key in _ATOMIC_FIELDS) or (
        isinstance(signals, Mapping) and any(signals.get(key) for key in _ATOMIC_SIGNALS))
    key = (source, text, revision, context, heading, bool(atomic))
    if cache is not None and isinstance(cache.get(key), _Prepared):
        return cache[key]
    tokens = _tokens(text)
    reason = ('protected_packet' if atomic else 'unknown_source' if not source
              else 'unknown_revision' if not any(_known(_field(item, key)) for key in _REVISION_PROOF)
              else 'empty_payload' if not tokens else '')
    size = min(3, len(tokens))
    shingles = Counter(tuple(tokens[index:index + size]) for index in range(len(tokens) - size + 1)) if size else Counter()
    result = _Prepared(source, revision, (*context, _protected(_tokens(heading))), tokens,
                       _protected(tokens), shingles, reason)
    if cache is not None:
        cache[key] = result
    return result


def _resemblance(first: Counter, second: Counter) -> float:
    denominator = sum((first | second).values())
    return sum((first & second).values()) / denominator if denominator else 0.0


def priority_multiplier(candidate: Mapping, selected: Sequence[Mapping], *,
                        trace: dict | None = None, cache: dict | None = None) -> float:
    """Return [0.15, 1] without removing or merging any evidence candidate.

    ``cache`` is optional, caller-owned and MUST be fresh per request. Snapshot
    keys include text and protected metadata, so edits cannot reuse stale prep.
    Trace is an optional output only: no source text or semantic certificate.
    A protected conflict skips that pair, not an independently eligible repeat.
    """
    strongest, strongest_index, compared = 0.0, None, 0
    skipped = Counter()
    prepared = _prepare(candidate, cache)
    if not prepared.reason:
        for number, item in enumerate(selected):
            other = _prepare(item, cache)
            reason = (other.reason or ('different_source' if prepared.source != other.source else '')
                      or ('different_revision' if prepared.revision != other.revision else '')
                      or ('different_context' if prepared.context != other.context else '')
                      or ('protected_difference' if prepared.protected != other.protected else ''))
            if reason:
                skipped[reason] += 1
                continue
            # Case-only changes can change a formula's identifiers too. Keep
            # the complete stream case sensitive, not merely its stemmed words.
            if prepared.tokens != other.tokens and tuple(map(str.casefold, prepared.tokens)) == tuple(map(str.casefold, other.tokens)):
                skipped['case_difference'] += 1
                continue
            compared += 1
            resemblance = _resemblance(prepared.shingles, other.shingles)
            if strongest_index is None or resemblance > strongest:
                strongest, strongest_index = resemblance, number
    multiplier = 1.0 - PENALTY_WEIGHT * strongest if strongest >= HIGH_RESEMBLANCE else 1.0
    if trace is not None:
        trace.update(strongest_overlap=round(strongest, 6), strongest_selected_index=strongest_index,
                     compared_count=compared, reason=prepared.reason or (
                         'soft_lexical_redundancy' if multiplier < 1 else 'no_high_eligible_overlap'),
                     skipped_comparisons=dict(skipped), semantic_equivalence=False)
    return multiplier

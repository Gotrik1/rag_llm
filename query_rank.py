"""Request-local query-rank provenance and soft marginal retrieval coverage.

No document metadata can supply these ranks. Different query channels take a
minimum rank, never add votes; rewrites share one bounded unit of weight.
Coverage here means retrieval-list diversity, not factual answer coverage.
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable


IDENTITY_FIELDS = ('file', 'file_path', 'section_path', 'status',
                   'document_id', 'source_id', 'revision_id', 'version_id', 'document_version',
                   'content_hash', 'sha256',
                   'page_number', 'page_label', 'page',
                   'start_char_idx', 'end_char_idx', 'start_index', 'end_index',
                   'start', 'end', 'chunk_id', 'node_id')
MAX_PACK_RECEIPTS = 128
_WITNESS_TOKEN = object()


def _protected(query: str) -> bool:
    # Conservative dedup: a structural signature must never merge different
    # negations, numbers, units or operators. Such queries only dedup literally.
    return bool(re.search(r'\d|[<>=+*/^−–-]|\b(?:не|ни|нет|без|not|no|without|except)\b|'
                          r'\b[A-Za-z]{1,8}\b', query, re.I) or any(
        unicodedata.category(character) in {'Sm', 'Sc'} or character in '%‰°µ'
        for character in query) or any(
        any(letter.isupper() for letter in word[1:]) and any(letter.islower() for letter in word)
        for word in re.findall(r'[а-яёА-ЯЁ]+', query)))


def _identity_parts(item: dict):
    if not isinstance(item, dict) or not isinstance(item.get('file'), str) or not isinstance(item.get('text'), str):
        return None
    if any(field in item and not isinstance(item[field], (str, int, float, bool, type(None)))
           for field in IDENTITY_FIELDS):
        return None  # Opaque/mutable identity metadata cannot establish a rank binding.
    try:
        fields = json.dumps([(field, field in item, item.get(field)) for field in IDENTITY_FIELDS],
                            ensure_ascii=False, sort_keys=True, allow_nan=False)
        return fields, item['text']
    except (TypeError, ValueError, UnicodeError):
        return None


def native_identity(item: dict):
    """Exact location/revision/full-text identity, never a prefix/numeric rewrite."""
    parts = _identity_parts(item)
    return hashlib.sha256(('\n'.join(parts)).encode('utf-8')).hexdigest() if parts is not None else None


def _frontier_section(item):
    fields = ('file', 'file_path', 'section_path', 'status', 'document_id', 'source_id',
              'revision_id', 'version_id', 'document_version', 'content_hash', 'sha256')
    return json.dumps([(key, key in item, item.get(key)) for key in fields],
                      ensure_ascii=True, allow_nan=False)


def _frontier_body(text):
    """A structural wrapper/title alone cannot earn a reader slot."""
    return '\n'.join(line for line in text.splitlines()
                     if not re.match(r'^\s*(?:\[SECTION:.*\]|#{1,6}\s+.*)\s*$', line)).strip()


def _ranked_native_roots_first(rows):
    """Prefer a unique substantive introduction over its own continuations.

    Only records in this exact result channel can participate. File, complete
    section locator and typed revision identity must match; no parent lookup,
    invented rank receipt or cross-version/source promotion takes place.
    """
    roots = {}
    valid = [(item, native_identity(item)) for item in rows]
    valid = [(item, identity) for item, identity in valid if identity and item.get('status') != 'deleted']
    for item, identity in valid:
        text = re.sub(r'^\s*\[SECTION:[^\n]*\]\s*', '', item['text']).lstrip()
        if re.match(r'^#{1,6}\s+\S', text) and _frontier_body(text):
            roots.setdefault(_frontier_section(item), {})[identity] = item
    result, seen = [], set()
    for item, identity in valid:
        candidates = roots.get(_frontier_section(item), {})
        first = list(candidates.items()) if len(candidates) == 1 else []
        for key, record in [*first, (identity, item)]:
            if key not in seen:
                result.append(record)
                seen.add(key)
    return result


@dataclass(frozen=True, init=False)
class NativePackWitness:
    """Immutable native-index authority, never an evidence field/request rank."""
    _bindings: object

    def __init__(self, bindings, token=None):
        if token is not _WITNESS_TOKEN:
            raise TypeError('Native pack witnesses must come from the native index')
        object.__setattr__(self, '_bindings', MappingProxyType(
            {identity: frozenset(signatures) for identity, signatures in bindings.items()}))

    def signature(self, identity):
        """Return the single native binding signature, or None when that identity is absent or ambiguous."""
        signatures = self._bindings.get(identity, ())
        return next(iter(signatures)) if len(signatures) == 1 else None

    def ambiguous(self, identity):
        """Report conflicting native signatures so one packed identity cannot stand in for several sources."""
        return len(self._bindings.get(identity, ())) > 1


class _NativeWitnessBuilder:
    """Used once in the existing native-index loop, never during retrieval."""
    def __init__(self):
        self._bindings = {}

    def add(self, record):
        """Record native identity/signature bindings, including the explicit path-free compatibility identity."""
        parts = _identity_parts(record)
        if parts is None:
            return
        identity = native_identity(record)
        self._bindings.setdefault(identity, set()).add(parts[0])
        if isinstance(record.get('file_path'), str) and record['file_path']:
            # The backend may omit this locator; only an actual native source
            # record can attest its enrichment. None/empty/different are not
            # absence and never acquire this alternate binding.
            without_path = {key: value for key, value in record.items() if key != 'file_path'}
            identity = native_identity(without_path)
            self._bindings.setdefault(identity, set()).add(parts[0])

    def freeze(self):
        """Seal accumulated native bindings as the witness accepted by the packing boundary."""
        return NativePackWitness(self._bindings, _WITNESS_TOKEN)


class QueryRankContext:
    """A caller-owned request capability, not a serializable evidence field."""

    def __init__(self, question: str, features: Callable[[str], frozenset[str]],
                 native_text: Callable[[str], str] | None = None):
        self.question = question
        self._features = features
        self._raw = frozenset(features(question))
        self._literal_raw = re.sub(r'\s+', ' ', question).strip().casefold()
        self._queries = {}
        self._groups = {}
        self._rows = {}
        self._identities = {}
        self._native_text = native_text
        self._retention_texts = {}
        self._packed_receipts = {}
        self._ambiguous_packets = set()
        # Independent soft admission history: semantic paraphrases may share no
        # stems with the question. This is not a factual/packing rank receipt.
        self._frontier_channels = {}

    def _frontier_key(self, query):
        literal = re.sub(r'\s+', ' ', query).strip()
        return literal if _protected(query) else literal.casefold()

    def _record_frontier(self, query, rows):
        if not isinstance(query, str) or not 0 < len(query) <= 500 or not any(c.isalnum() for c in query):
            return
        key = self._frontier_key(query)
        if key not in self._frontier_channels and len(self._frontier_channels) >= 32:
            return
        channels = self._frontier_channels.get(key, [])
        if len(channels) >= 8:
            return
        identities, seen = [], set()
        for item in _ranked_native_roots_first(rows[:256]):
            identity = self._identity(item)
            if identity is None or identity in seen or item.get('status') == 'deleted':
                continue
            projected = self._native_text(item['text']) if self._native_text else item['text']
            titles = (item.get('section_title'), str(item.get('section_path', '')).rsplit('>', 1)[-1].strip())
            if (not isinstance(projected, str) or not _frontier_body(projected)
                    or projected.strip() in titles):
                continue
            identities.append(identity)
            seen.add(identity)
        channel = tuple(identities)
        if channel and channel not in channels:
            self._frontier_channels.setdefault(key, []).append(channel)

    def query_frontiers(self, eligible_identities):
        """Return trusted native result lists, restricted to the caller's pool.

        Repeated identical channels earn no new opportunity. Filtering cannot
        resurrect a record: no metadata, prior chat or packed offer supplies
        these lists, and no source text or lookup result is changed here.
        """
        eligible = frozenset(eligible_identities)
        result = []
        for key, channels in self._frontier_channels.items():
            filtered = tuple(dict.fromkeys(tuple(i for i in channel if i in eligible)
                                           for channel in channels))
            filtered = tuple(channel for channel in filtered if channel)
            if filtered:
                result.append((key == self._frontier_key(self.question), filtered))
        return tuple(result)

    def _identity(self, item):
        parts = _identity_parts(item)
        if parts is None:
            return None
        if parts not in self._identities:
            self._identities[parts] = hashlib.sha256(('\n'.join(parts)).encode('utf-8')).hexdigest()
        return self._identities[parts]

    def _query(self, query: str):
        if not isinstance(query, str):
            return None
        literal = re.sub(r'\s+', ' ', query).strip().casefold()
        protected = _protected(query)
        # Preserve unit spelling/case in protected queries as well.
        literal_key = (literal, re.sub(r'\s+', ' ', query).strip() if protected else '')
        if literal_key in self._queries:
            return self._queries[literal_key]
        keys = frozenset(self._features(query))
        shared = keys & self._raw
        if literal == self._literal_raw and (not protected or query == self.question):
            group = 'raw'
        elif not keys or (len(shared) < 2 and not (len(keys) == 1 and keys <= self._raw)):
            return None  # No new hard filter: simply no diversity vote for this probe.
        elif not protected and keys == self._raw:
            group = 'raw'
        else:
            group = next((name for name, signature in self._groups.items()
                          if not protected and signature is not None and
                          (shared, keys - self._raw) == signature), None)
            if group is None:
                group = f'query:{len(self._groups)}'
                self._groups[group] = None if protected else (shared, keys - self._raw)
        self._queries[literal_key] = group
        return group

    def record(self, query: str, rows: list[dict]):
        """Record query frontiers and native ranks without injecting planner text into evidence records."""
        if not rows:
            return
        self._record_frontier(query, rows)
        group = self._query(query)
        if group is None:
            return
        for rank, item in enumerate(rows):
            identity = self._identity(item)
            if identity is None:
                continue
            projected = self._native_text(item['text']) if self._native_text else item.get('text')
            titles = (item.get('section_title'), str(item.get('section_path', '')).rsplit('>', 1)[-1].strip())
            if not isinstance(projected, str) or not projected.strip() or projected.strip() in titles:
                continue  # An empty/title-only record cannot earn rank coverage.
            if identity not in self._rows:
                self._rows[identity] = (_identity_parts(item)[0], item['text'], {})
                # A trusted native packer can remove structural wrappers. This
                # projection never participates in identity or query dedup.
                self._retention_texts[identity] = projected
            ranks = self._rows[identity][2]
            ranks[group] = min(rank, ranks.get(group, rank))

    def origins(self, item: dict) -> frozenset[str]:
        """Return known raw or packed origins; ambiguous packets cannot claim provenance."""
        identity = self._identity(item)
        if identity in self._ambiguous_packets:
            return frozenset()
        raw = frozenset((identity,)) if identity in self._rows else frozenset()
        return raw | self._packed_receipts.get(identity, frozenset())

    def ranks(self, origins) -> dict[str, int]:
        """Merge best observed ranks for each query across the supplied native origins."""
        result = {}
        for identity in origins:
            if identity in self._rows:
                for query, rank in self._rows[identity][2].items():
                    result[query] = min(rank, result.get(query, rank))
        return result

    def retained(self, item: dict, origins) -> frozenset[str]:
        """Do not credit a rejected packet or a discarded source chunk."""
        parts = _identity_parts(item)
        if parts is None or self._identity(item) in self._ambiguous_packets:
            return frozenset()
        return frozenset(identity for identity in origins if identity in self._rows and
            parts[0] == self._rows[identity][0] and
            (self._rows[identity][1] in parts[1] or self._retention_texts[identity] and
             self._retention_texts[identity] in parts[1]))

    def native_ancestry(self, item: dict, offered_origins, witness: NativePackWitness) -> frozenset[str]:
        """Prove whole native ancestry within an already eligible input pool."""
        if not isinstance(witness, NativePackWitness):
            return frozenset()
        parts = _identity_parts(item)
        if parts is None or self._identity(item) in self._ambiguous_packets:
            return frozenset()
        return frozenset(origin for origin in offered_origins if origin in self._rows and
            witness.signature(origin) == parts[0] and self._retention_texts[origin] and
            self._retention_texts[origin] in parts[1])

    def register_packed(self, item: dict, offered_origins, witness: NativePackWitness) -> None:
        """Record an actually packed native packet, not an offer or model hint.

        The selector calls this only after append. Full raw identity remains
        unchanged; this separate receipt bridges native reassembly and the one
        permitted, index-attested missing-path enrichment. No other field may
        change type/presence/value, and the entire projected record must remain.
        """
        identity = self._identity(item)
        if identity is None:
            return
        if isinstance(witness, NativePackWitness) and witness.ambiguous(identity):
            # A pathless fallback can be byte-identical to a ranked input.
            # It must not bypass the failed native binding via raw identity.
            # Keep the rejection for this exact packet throughout the request;
            # located records and their raw rank history remain unchanged.
            self._ambiguous_packets.add(identity)
            self._packed_receipts.pop(identity, None)
        if identity in self._ambiguous_packets:
            return
        if identity not in self._packed_receipts and len(self._packed_receipts) >= MAX_PACK_RECEIPTS:
            return  # Soft fallback, never a larger evidence/answer quota.
        earned = self.native_ancestry(item, offered_origins, witness)
        if earned:
            self._packed_receipts[identity] = self._packed_receipts.get(identity, frozenset()) | earned

    def receipt_origins(self, item: dict, offered_origins) -> frozenset[str]:
        """A previous request's or a caller-supplied metadata receipt is inert."""
        identity = self._identity(item)
        if identity in self._ambiguous_packets:
            return frozenset()
        return self._packed_receipts.get(identity, frozenset()) & frozenset(offered_origins)

    def allocator(self, eligible_origins=None):
        """Create a marginal-coverage allocator using only active query groups and eligible origins."""
        active = (set(self.ranks(eligible_origins)) if eligible_origins is not None else
                  {query for _, _, ranks in self._rows.values() for query in ranks})
        groups = [group for group in self._groups if group in active]
        count = len(groups)
        weights = {'raw': 1.0, **{group: 1.0 / count for group in groups}} if count else {'raw': 1.0}
        return MarginalQueryCoverage(self, weights, len(active) > 1)


class MarginalQueryCoverage:
    """Track retained query coverage while selecting complementary native evidence."""
    def __init__(self, context: QueryRankContext, weights: dict[str, float], active: bool):
        self.context, self.weights, self.covered = context, weights, {}
        self._active = active

    @property
    def active(self):
        """Report whether multiple observed query groups make marginal coverage applicable."""
        return self._active

    def gain(self, origins) -> float:
        """Measure additional weighted reciprocal-rank coverage without mutating selection state."""
        if not self.active:
            return 0.0
        return sum(self.weights.get(query, 0) * max(0, 1 / (1 + rank) - self.covered.get(query, 0))
                   for query, rank in self.context.ranks(origins).items())

    def accept(self, item: dict, origins):
        """Credit only text-retained or witnessed origins after a candidate is accepted."""
        retained = self.context.retained(item, origins) | self.context.receipt_origins(item, origins)
        for query, rank in self.context.ranks(retained).items():
            self.covered[query] = max(self.covered.get(query, 0), 1 / (1 + rank))

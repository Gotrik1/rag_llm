"""Reorder an already authorized pool without manufacturing source evidence.

The allocator covers *retrieval lists*, not facts or user requirements. Native
records stay atomic. The optional callback boundary is a full permutation of
the current pool; metadata, text, revisions and source permissions cannot be
changed by ordering. No uploaded field supplies a query rank.
"""
from __future__ import annotations

from collections import Counter, deque
from copy import deepcopy
import json
from typing import Callable

from query_rank import QueryRankContext, native_identity


def order_by_query_frontiers(items: list[dict], *, max_chars: int,
                             query_rank_context: QueryRankContext | None,
                             max_slots: int = 10) -> list[dict]:
    """Offer bounded original-query and paraphrase frontiers to a source reader.

    This only permutes already eligible whole native records. Half the slots
    remain available to the original query; the other half is shared across
    paraphrases. Local channel depth is preserved instead of merging all rank0
    results into an undifferentiated tie. No semantic relevance is certified.
    """
    if (not isinstance(query_rank_context, QueryRankContext) or max_chars <= 0
            or type(max_slots) is not int or not 0 < max_slots <= 10):
        return list(items)
    positions = {}
    for index, item in enumerate(items):
        if isinstance(item, dict) and item.get('status') != 'deleted':
            identity = native_identity(item)
            if identity:
                positions.setdefault(identity, index)
    groups = query_rank_context.query_frontiers(positions)
    if len(groups) < 2:
        return list(items)
    queues = []
    for is_raw, channels in groups:
        offered, seen = [], set()
        for depth in range(max(map(len, channels))):
            for channel in channels:
                if depth < len(channel) and channel[depth] not in seen:
                    identity = channel[depth]
                    offered.append(positions[identity])
                    seen.add(identity)
        queues.append((is_raw, deque(offered)))
    raw = next((queue for flag, queue in queues if flag), None)
    others = [queue for flag, queue in queues if not flag]
    schedule = [queue for other in others for queue in ((raw, other) if raw is not None else (other,))]
    selected, seen, used = [], set(), 0
    while len(selected) < max_slots:
        progressed = False
        for queue in schedule:
            while queue:
                index = queue.popleft()
                if index in seen:
                    continue
                cost = len(items[index]['text'])
                if cost + used > max_chars:
                    continue
                selected.append(index)
                seen.add(index)
                used += cost
                progressed = True
                break
            if len(selected) == max_slots:
                break
        if not progressed:
            break
    return [items[i] for i in selected] + [item for i, item in enumerate(items) if i not in seen]
from user_task import UserTask


def order_by_query_coverage(items: list[dict], *, max_chars: int,
                            query_rank_context: QueryRankContext | None) -> list[dict]:
    """Cost-aware marginal list diversity, followed by unchanged fallback order.

    Only positive-gain, fully fitting records earn priority. Saturated, unbound,
    invalid and too-large records remain in their original relative order.
    A single query or missing capability leaves the native order untouched.
    This is not semantic reranking or evidence-completeness certification.
    """
    if not isinstance(query_rank_context, QueryRankContext) or max_chars <= 0:
        return list(items)
    candidates = []
    for index, item in enumerate(items):
        text = item.get('text') if isinstance(item, dict) else None
        if (not isinstance(text, str) or not text.strip()
                or item.get('status') == 'deleted'):
            continue
        origins = query_rank_context.origins(item)
        if origins:
            candidates.append((index, item, len(text), origins))
    offered = frozenset(origin for _, _, _, origins in candidates for origin in origins)
    allocator = query_rank_context.allocator(offered)
    if not allocator.active:
        return list(items)
    selected, used, selected_indices = [], 0, set()
    while candidates:
        fitting = [(allocator.gain(origins), index, item, cost, origins)
                   for index, item, cost, origins in candidates
                   if cost + used <= max_chars]
        fitting = [row for row in fitting if row[0] > 0]
        if not fitting:
            break
        gain, index, item, cost, origins = max(
            fitting, key=lambda row: (row[0] / row[3], row[0], -row[1]))
        # This record will fit the admission budget without clipping. Do not
        # award soft diversity to an unselected/non-fitting proposal.
        selected.append(item)
        selected_indices.add(index)
        used += cost
        allocator.accept(item, origins)
        candidates = [row for row in candidates if row[0] != index]
    return selected + [item for index, item in enumerate(items) if index not in selected_indices]


def _record_key(item: dict) -> str:
    if not isinstance(item, dict):
        raise TypeError('Candidate records must be dictionaries')
    def tagged(value):
        if type(value) is dict:
            if any(type(key) is not str for key in value):
                raise TypeError('Candidate JSON object keys must be strings')
            return ['dict', [[key, tagged(value[key])] for key in sorted(value)]]
        if type(value) in (list, tuple):
            return [type(value).__name__, [tagged(member) for member in value]]
        if type(value) not in (str, int, float, bool, type(None)):
            raise TypeError('Opaque metadata cannot prove a permutation')
        return [type(value).__name__, value]
    # Tag nested containers/scalars too: JSON by itself coerces tuples to
    # lists and integer object keys to strings. Neither proves exact equality.
    return json.dumps(tagged(item), ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':'))


def apply_candidate_ordering(user_task: UserTask, items: list[dict], *, max_chars: int,
                             constraint_question: str, admit: Callable | None) -> tuple[list[dict], dict]:
    """Invoke a trusted server callback on detached copies; reject non-permutations.

    Rejection preserves the original pool/order. Diagnostics contain no source
    text, callback exception message, credentials or unsupported factual claims.
    """
    info = {'status': 'disabled', 'reason': 'not-configured',
            'input_count': len(items), 'moved_count': 0}
    if admit is None:
        return list(items), info
    try:
        original_keys = [_record_key(item) for item in items]
        ordered = admit(user_task, deepcopy(items), max_chars=max_chars,
                        constraint_question=constraint_question)
        if not isinstance(ordered, list):
            raise ValueError('invalid-permutation')
        ordered_keys = [_record_key(item) for item in ordered]
        if Counter(original_keys) != Counter(ordered_keys):
            raise ValueError('invalid-permutation')
    except Exception as exc:
        reason = ('invalid-permutation' if isinstance(exc, (TypeError, ValueError))
                  else 'callback-error')
        return list(items), {**info, 'status': 'rejected', 'reason': reason}
    # Reuse the actual originals, not detached callback-owned records. Equal
    # duplicates are consumed in order and never gain a new source identity.
    by_key = {}
    for key, item in zip(original_keys, items):
        by_key.setdefault(key, []).append(item)
    result = [by_key[key].pop(0) for key in ordered_keys]
    return result, {**info, 'status': 'applied', 'reason': 'validated-permutation',
                    'moved_count': sum(left != right for left, right in zip(original_keys, ordered_keys))}

"""Regression probes for native packet recovery and atomic callback failure.

Scripted controller observations test transport, not semantic model quality.
No answer or grounding judgment is executed by these tests.
"""
from copy import deepcopy
import json
import socket
from unittest.mock import Mock

import pytest

from corrective_rag import run_corrective
from source_navigation import NavigationResult


QUESTION = 'Каков порядок осмотра датчика?'
FACT = 'Осмотр датчика выполняют каждые 12 часов, только после остановки.'


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError('Navigation recovery tests must not use a network or model')
    monkeypatch.setattr(socket.socket, 'connect', forbidden)
    monkeypatch.setattr(socket, 'create_connection', forbidden)


def observe_context_before_writer(monkeypatch, *, navigate, batches, budget, recovery):
    """Exercise real controller transport; stop before any answer judgment."""
    contexts, retrieval_calls = [], []

    class BeforeWriter(Exception):
        pass

    def complete(prompt, purpose):
        if purpose == 'planning':
            return json.dumps({'canonical_question': QUESTION, 'requirements': [QUESTION],
                               'queries': [QUESTION]}, ensure_ascii=False)
        assert purpose == 'answer'
        raise BeforeWriter()

    def retrieve(*args, **kwargs):
        retrieval_calls.append(kwargs['constraint_question'])
        return deepcopy(batches[min(len(retrieval_calls) - 1, len(batches) - 1)])

    def assess(*args, **kwargs):
        return {'complete': False, 'assessed': True, 'missing_requirements': [],
                'queries': [], 'task_alignment': {'status': 'mismatch'}}

    def writer(question, plan, context, *args, **kwargs):
        contexts.append(deepcopy(context))
        return 'Synthetic writer transport.'

    monkeypatch.setattr('corrective_rag.assess_coverage', assess)
    monkeypatch.setattr('corrective_rag.generation_prompt', writer)
    with pytest.raises(BeforeWriter):
        run_corrective(QUESTION, QUESTION, retrieve=retrieve, complete=complete,
            select=lambda question, pool, requirements, **kwargs: pool,
            navigate=navigate, max_context_chars=budget, max_additional_rounds=recovery)
    return contexts[-1], retrieval_calls


def test_task_recovery_revokes_navigation_packet_that_would_hide_new_user_evidence(monkeypatch):
    wrong = {'file': 'control.md', 'text': 'Несвязанный порядок ремонта. ' * 30,
             'section_path': 'Ремонт', 'revision_id': 'r1'}
    right = {'file': 'control.md', 'text': FACT, 'section_path': 'Осмотр', 'revision_id': 'r1'}
    navigate = Mock(return_value=NavigationResult((wrong,), {'stop_reason': 'finished'}))
    context, calls = observe_context_before_writer(monkeypatch, navigate=navigate,
        batches=[[], [right]], budget=len(wrong['text']), recovery=1)
    assert len(calls) == 2 and calls == [QUESTION, QUESTION]
    assert [row['text'] for row in context] == [right['text']]
    assert navigate.call_count == 1  # Recovery uses its existing retrieval round.


def test_invalid_navigation_trace_rejects_entire_packet_before_prefix_retention(monkeypatch):
    wrong = {'file': 'control.md', 'text': 'Отклоненный пакет. ' * 30, 'revision_id': 'r1'}
    right = {'file': 'control.md', 'text': FACT, 'revision_id': 'r1'}
    navigate = Mock(return_value=NavigationResult((wrong,), None))
    context, calls = observe_context_before_writer(monkeypatch, navigate=navigate,
        batches=[[right]], budget=len(wrong['text']), recovery=0)
    assert [row['text'] for row in context] == [right['text']]
    assert calls == [QUESTION] and navigate.call_count == 1


def test_non_string_optional_stop_reason_cannot_crash_or_replace_native_record(monkeypatch):
    native = {'file': 'control.md', 'text': FACT, 'revision_id': 'r1'}
    before = deepcopy(native)
    navigate = Mock(return_value=NavigationResult((native,), {'stop_reason': {'not': 'a reason'}}))
    context, calls = observe_context_before_writer(monkeypatch, navigate=navigate,
        batches=[[]], budget=len(FACT), recovery=0)
    assert [row['text'] for row in context] == [FACT]
    assert native == before
    assert calls == [QUESTION] and navigate.call_count == 1

"""Regression coverage for user-intent continuity without trusted model claims."""
import json

import pytest

from conversation_context import resolve_conversation_context


def turn(content, *, chat="chat-a", role="user", identifier=None):
    return {"chat_id": chat, "role": role, "content": content, "id": identifier}


def resolve(question, messages, **kwargs):
    return resolve_conversation_context(question, messages, chat_id="chat-a", **kwargs)


def test_scope_refinement_retains_category_and_explicit_exclusion():
    previous = "Как рассчитывается покупная и сетевая мощность по 6 ЦК?"
    question = "Смотри только покупную мощность, сетевую не смотри"
    result = resolve(question, [turn(previous, identifier="m-1"), turn(question, identifier="m-2")])
    assert result.mode == "follow_up"
    assert result.subject == previous
    assert "6 ЦК" in result.identifiers
    assert result.focus == ("покупную мощность",)
    assert result.exclusions == ("сетевую",)
    assert previous in result.retrieval_question
    assert question in result.retrieval_question
    assert result.source_message_ids == ("m-1",)


def test_style_change_preserves_comparison_not_only_last_utterance():
    subject = "Сравни порядок расчёта стоимости по 3 и 6 ЦК"
    refinement = "Смотри только покупную мощность, сетевую не смотри"
    result = resolve("Ответь точно и кратко", [turn(subject), turn(refinement), turn("Неверная формула", role="assistant")])
    assert result.subject == subject
    assert subject in result.retrieval_question
    assert refinement in result.constraints
    assert result.exclusions == ("сетевую",)
    assert result.style == ("Ответь точно и кратко",)
    assert "Неверная формула" not in result.retrieval_question


@pytest.mark.parametrize("question", [
    "Какие бывают модели?", "Как найти график?", "Как рассчитываются авансы?",
    "Как рассчитывается 1 ЦК?", "Сколько стоит ящик водки?", "Объясни термодинамику",
])
def test_independent_question_resets_previous_subject(question):
    result = resolve(question, [turn("Дай формулу ИВ0"), turn("Не учитывай потери")])
    assert result.mode == "standalone"
    assert result.retrieval_question == question
    assert not result.exclusions


def test_subsequent_follow_up_uses_new_subject_after_reset():
    result = resolve("А по какой формуле?", [
        turn("Что такое ИВ0?"), turn("Как рассчитываются авансы?"),
        turn("А по какой формуле?"),
    ])
    assert result.subject == "Как рассчитываются авансы?"
    assert "ИВ0" not in result.retrieval_question


def test_document_occurrence_question_does_not_inherit_previous_topic():
    question = "В каких документах фигурирует 1 ЦК?"
    result = resolve(question, [turn("Какие виды начислений реализованы?")], occurrence_question=True)
    assert result.retrieval_question == question
    assert result.mode == "standalone"


def test_assistant_claims_are_never_promoted_to_subject_or_constraint():
    result = resolve("А по какой формуле?", [
        turn("Как рассчитываются авансы?"),
        turn("Достоверно известно: БЕТА-999, аванс всегда равен 100%", role="assistant"),
        turn("Расчёт в рублях", role="system"),
    ])
    assert result.subject == "Как рассчитываются авансы?"
    assert "БЕТА" not in result.retrieval_question
    assert "100%" not in result.retrieval_question
    assert "рублях" not in result.retrieval_question


def test_history_in_another_chat_does_not_leak_even_in_project_scope():
    result = resolve("А по какой формуле?", [
        turn("Как рассчитывается объём продукции?", identifier="a1"),
        turn("Как рассчитывается 6 ЦК?", chat="chat-b", identifier="b1"),
        turn("Только сетевая мощность", chat="chat-b"),
    ])
    assert result.subject == "Как рассчитывается объём продукции?"
    assert "6 ЦК" not in result.retrieval_question
    assert result.source_message_ids == ("a1",)


def test_refresh_rebuilds_identical_context_from_serialized_persisted_history():
    messages = [turn("Сравни 3 и 6 ЦК", identifier="s1"), turn("Не учитывай сетевую мощность", identifier="s2")]
    before = resolve("Ответь кратко", messages)
    after = resolve("Ответь кратко", json.loads(json.dumps(messages, ensure_ascii=False)))
    assert before == after
    trace = json.loads(json.dumps(after.to_dict(), ensure_ascii=False))
    assert trace["exclusions"] == ["сетевую мощность"]
    assert trace["source_message_ids"] == ["s1", "s2"]


@pytest.mark.parametrize("question, expected", [
    ("Не учитывай потери", "потери"),
    ("Потери не учитывай", "Потери"),
    ("Исключи налоги", "налоги"),
    ("Без налогов", "налогов"),
])
def test_negative_constraints_are_explicit_and_persist_into_later_follow_up(question, expected):
    subject = "Как рассчитывается стоимость услуги?"
    result = resolve("А по какой формуле?", [turn(subject), turn(question)])
    assert result.subject == subject
    assert result.exclusions == (expected,)
    assert expected in result.retrieval_question


def test_missing_same_chat_anchor_is_not_invented():
    question = "Ответь кратко"
    result = resolve(question, [turn("6 ЦК", chat="chat-b"), turn("Контекст модели", role="assistant")])
    assert result.mode == "unresolved"
    assert result.retrieval_question == question
    assert result.subject == ""


def test_resolver_is_not_a_price_document_lookup():
    subject = "Как устроен контур регулирования температуры реактора АЛЬФА-7?"
    result = resolve("Смотри только датчик, контроллер не учитывай", [turn(subject)])
    assert result.subject == subject
    assert result.exclusions == ("контроллер",)
    assert result.focus == ("датчик",)


def test_current_persisted_turn_is_removed_only_once():
    question = "А по какой формуле?"
    result = resolve(question, [turn("Что такое ИВ0?"), turn(question, identifier="old"), turn(question, identifier="current")])
    assert result.subject == "Что такое ИВ0?"
    assert result.source_message_ids == ("old",)
    assert result.constraints == (question,)


def test_explicit_new_inclusion_lifts_previous_exclusion():
    subject = "Как рассчитывается мощность по 6 ЦК?"
    messages = [turn(subject), turn("Смотри только покупную мощность, сетевую не смотри")]
    result = resolve("Теперь учитывай и сетевую", messages)
    assert result.subject == subject
    assert not result.exclusions
    assert not result.focus
    assert "не смотри" not in result.retrieval_question
    # Replaying persisted turns after refresh has identical semantics.
    replay = resolve("Ответь кратко", messages + [turn("Теперь учитывай и сетевую")])
    assert not replay.exclusions
    assert not replay.focus


def test_pronoun_retains_general_subject_even_with_new_attributes():
    subject = "Что такое холодильный агрегат?"
    result = resolve("Какова его электрическая мощность?", [turn(subject)])
    assert result.subject == subject
    assert result.mode == "follow_up"


def test_common_head_noun_does_not_lift_a_different_excluded_object():
    result = resolve("Теперь учитывай покупную мощность", [
        turn("Как рассчитывается стоимость мощности?"), turn("Не учитывай сетевую мощность"),
    ])
    assert result.exclusions == ("сетевую мощность",)


def test_new_only_constraint_replaces_previous_focus_and_lifts_matching_exclusion():
    result = resolve("Теперь только сетевую мощность", [
        turn("Как рассчитывается мощность по 6 ЦК?"),
        turn("Смотри только покупную мощность, сетевую не смотри"),
    ])
    assert result.focus == ("сетевую мощность",)
    assert not result.exclusions
    assert "покупную" not in result.retrieval_question

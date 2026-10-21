"""Reconstruct retrieval intent from persisted user turns, never assistant facts.

This is a conservative discourse resolver, not a source of domain knowledge.
Independent questions reset the subject; explicit refinements preserve it. No
process-global state is used, so refreshes and concurrent chats behave alike.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Literal, Mapping, Sequence


_WORDS = re.compile(r"[a-zа-яё0-9]+", re.I)
_IDENTIFIERS = re.compile(
    r"\b\d+(?:\s*(?:,|и|или|vs|/|–|-)\s*\d+)*\s*[A-Za-zА-ЯЁа-яё]{1,8}\b"
    r"|\b[А-ЯЁA-Z]{2,}[А-ЯЁA-Z0-9-]*\b"
    r"|[«\"][^»\"\n]{2,120}[»\"]"
)
_NEGATIVE_ACTION = r"(?:смотри|рассматривай|учитывай|используй|включай|добавляй|ищи)"
_REFINEMENT = re.compile(
    r"^(?:(?:а|и|но|теперь|тогда|пожалуйста)\s*[,.:]?\s*)*"
    r"(?:смотри|рассмотри|учитывай|учти|исключи|убери|не\s+" + _NEGATIVE_ACTION
    + r"|только\b|без\b|вместо\b|что\s+(?:с|насч[её]т)\b"
    r"|почему\s+(?:ты\s+)?не\s+(?:уч[её]л|наш[её]л|включил))", re.I,
)
_STYLE = re.compile(
    r"^(?:(?:а|и|но|теперь|тогда|пожалуйста)\s*[,.:]?\s*)*"
    r"(?:ответь|отвечай|объясни|поясни|расскажи|напиши|сделай|дай|покажи)\b", re.I,
)
# A question-word prefix is not sufficient: unknown nouns mean a new subject.
# Stems below describe discourse/actions, not documents or energy-market terms.
_REFERENTIAL_STEMS = (
    "а", "и", "но", "по", "в", "на", "для", "с", "из", "это", "этот", "этого",
    "этой", "эти", "этом", "он", "она", "они", "его", "ее", "её", "их", "так",
    "как", "како", "кака", "каки", "почему", "где", "когда", "зачем", "сколько",
    "формул", "расчет", "расчёт", "рассчит", "счита", "услов", "ограничен", "переменн",
    "стоимост", "цена", "цену", "измер", "единиц", "исчерпыв", "полно", "полны",
    "источник", "порядок", "подробн", "кратк", "корот", "точн", "понятн", "пожалуйста",
    "прост", "слов", "пример", "шаг", "спис", "таблиц", "пункт", "поясн", "уточн",
    "объясн", "раскро", "ответ", "отвеч", "напиш", "сдела", "дай", "покаж", "привед",
    "расскаж", "что", "же", "еще", "ещё", "тогда", "теперь", "тут", "там", "такое",
)
_EXACT_REFERENTIAL = frozenset(word for word in _REFERENTIAL_STEMS if len(word) <= 3)
_LONG_REFERENTIAL = tuple(word for word in _REFERENTIAL_STEMS if len(word) > 3)
_OCCURRENCE = re.compile(
    r"(?:в\s+каких|какие|где\s+в)\s+документ|"
    r"(?:документ\w*).*\b(?:упомина|фигурир|содерж|написан)", re.I,
)


@dataclass(frozen=True)
class ConversationContext:
    original_question: str
    retrieval_question: str
    subject: str
    identifiers: tuple[str, ...] = ()
    focus: tuple[str, ...] = ()
    exclusions: tuple[str, ...] = ()
    constraints: tuple[str, ...] = ()
    style: tuple[str, ...] = ()
    mode: Literal["standalone", "follow_up", "unresolved"] = "standalone"
    source_message_ids: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """JSON-safe trace; content is user intent, not verified evidence."""
        return {key: list(value) if isinstance(value, tuple) else value for key, value in asdict(self).items()}


def _normalized(text: str) -> str:
    return " ".join(text.casefold().replace("ё", "е").split())


def _unique(values: Sequence[str]) -> tuple[str, ...]:
    result: dict[str, str] = {}
    for value in values:
        if value.strip():
            result.setdefault(_normalized(value), value.strip())
    return tuple(result.values())


def _identifiers(text: str) -> tuple[str, ...]:
    return _unique([match.group(0) for match in _IDENTIFIERS.finditer(text)])


def _is_follow_up(text: str) -> bool:
    if _OCCURRENCE.search(text):
        return False
    if _REFINEMENT.search(text):
        return True
    if re.search(r"\b(?:его|её|ее|их|этого|этой|этому|этом|этих|нему|ней|ним)\b", text, re.I):
        return True
    # Elliptic object-before-negation commands are refinements too, rather
    # than a new subject "Потери не учитывай" that discards the actual topic.
    if re.search(r"\s+не\s+" + _NEGATIVE_ACTION + r"[.!]?\s*$", text, re.I):
        return True
    # An explicit new identifier such as "Как рассчитывается 1 ЦК?" must
    # not silently inherit a previous unrelated question about another object.
    if _identifiers(text):
        return False
    words = _WORDS.findall(text.casefold())
    if not words:
        return False
    generic = all(word in _EXACT_REFERENTIAL or word.startswith(_LONG_REFERENTIAL) for word in words)
    return generic and (bool(_STYLE.search(text)) or len(words) <= 18)


def _scope_phrases(text: str) -> tuple[tuple[str, ...], tuple[str, ...]]:
    focus, exclusions = [], []
    for clause in re.split(r"[,;.!?\n]+", text):
        clause = clause.strip()
        if not clause:
            continue
        before = re.search(r"\bне\s+" + _NEGATIVE_ACTION + r"\s+(.+)$", clause, re.I)
        after = re.search(r"^(.+?)\s+не\s+" + _NEGATIVE_ACTION + r"\s*$", clause, re.I)
        other = re.search(r"\b(?:исключи|кроме|без)\s+(.+)$", clause, re.I)
        negative = before or after or other
        if negative:
            phrase = re.sub(r"^(?:а|и|но|теперь|тогда)\s+", "", negative.group(1), flags=re.I)
            exclusions.append(phrase)
        only = re.search(r"\bтолько\s+(.+)$", clause, re.I)
        if only and not negative:
            focus.append(only.group(1))
    return _unique(focus), _unique(exclusions)


def _updated_constraints(
    text: str, constraints: list[str], focus: list[str], exclusions: list[str],
) -> tuple[list[str], list[str], list[str]]:
    """A newer explicit inclusion can lift a previous negative restriction.

    Conservative short prefixes only reconcile the same user-supplied words
    across inflections; they do not infer synonyms or domain relationships.
    """
    next_focus, _ = _scope_phrases(text)
    positive = re.search(r"\b(?:учитывай|учти|включи|добавь|рассмотри)\s+(?:и\s+)?(.+)$", text, re.I)
    if not next_focus and (not positive or re.search(r"\bне\b|\bтолько\b", text, re.I)):
        return constraints, focus, exclusions
    positive_text = " ".join(next_focus) if next_focus else positive.group(1)
    words = {word[:5] for word in _WORDS.findall(_normalized(positive_text)) if len(word) >= 3}
    # Use the leading object/modifier, not a shared head noun: "учитывай
    # покупную мощность" must not lift "сетевую мощность не учитывай".
    def key(phrase: str) -> str:
        return next((word[:5] for word in _WORDS.findall(_normalized(phrase)) if len(word) >= 3), "")

    lifted = [phrase for phrase in exclusions if key(phrase) and key(phrase) in words]
    if not lifted and not next_focus:
        return constraints, focus, exclusions
    removed = {key(phrase) for phrase in lifted}
    constraints = [item for item in constraints if not removed.intersection(
        word[:5] for word in _WORDS.findall(_normalized(item)) if len(word) >= 5
    ) and not re.search(r"\bтолько\b", item, re.I)]
    return constraints, [], [phrase for phrase in exclusions if phrase not in lifted]


def resolve_conversation_context(
    question: str,
    messages: Sequence[Mapping[str, Any]],
    *,
    chat_id: str,
    occurrence_question: bool = False,
) -> ConversationContext:
    """Resolve a question from chronological persisted messages of this chat.

    The caller must provide authorized persisted messages. Exact chat filtering
    is an additional isolation boundary, not a substitute for authorization.
    No assistant/system content participates, even for subject extraction.
    """
    original = question
    text = question.strip()
    current_focus, current_exclusions = _scope_phrases(text)
    if occurrence_question or not _is_follow_up(text):
        return ConversationContext(original, original, text, _identifiers(text), current_focus, current_exclusions)

    turns = [
        (str(message.get("content") or "").strip(), str(message.get("id") or message.get("message_id") or ""))
        for message in messages
        if isinstance(message, Mapping) and chat_id and str(message.get("chat_id", "")) == str(chat_id)
        and message.get("role") == "user" and str(message.get("content") or "").strip()
    ]
    # Routes may persist the current user message before resolving retrieval.
    # Remove only its tail occurrence; older repeated requests remain history.
    if turns and _normalized(turns[-1][0]) == _normalized(text):
        turns.pop()

    subject = ""
    constraints: list[str] = []
    styles: list[str] = []
    identifiers: list[str] = []
    focus: list[str] = []
    exclusions: list[str] = []
    message_ids: list[str] = []
    for content, message_id in turns[-80:]:
        if not _is_follow_up(content):
            subject = content
            constraints, styles, message_ids = [], [], []
            identifiers = list(_identifiers(content))
            initial_focus, initial_exclusions = _scope_phrases(content)
            focus, exclusions = list(initial_focus), list(initial_exclusions)
        elif subject:
            constraints, focus, exclusions = _updated_constraints(content, constraints, focus, exclusions)
            next_focus, next_exclusions = _scope_phrases(content)
            focus.extend(next_focus)
            exclusions.extend(next_exclusions)
            identifiers.extend(_identifiers(content))
            (styles if _STYLE.search(content) and not next_focus and not next_exclusions else constraints).append(content)
        if subject and message_id:
            message_ids.append(message_id)

    if not subject:
        return ConversationContext(
            original, original, "", _identifiers(text), current_focus, current_exclusions, mode="unresolved",
        )

    constraints, focus, exclusions = _updated_constraints(text, constraints, focus, exclusions)
    focus.extend(current_focus)
    exclusions.extend(current_exclusions)
    identifiers.extend(_identifiers(text))
    is_style = bool(_STYLE.search(text)) and not current_focus and not current_exclusions
    if is_style:
        styles.append(text)
    else:
        constraints.append(text)
    retained_constraints = _unique(constraints)[-6:]
    retrieval = [f"Тема диалога — вопрос пользователя: {subject}"]
    if retained_constraints:
        retrieval.append("Условия и уточнения пользователя: " + " | ".join(retained_constraints))
    if exclusions:
        retrieval.append("Исключить из предмета ответа: " + "; ".join(_unique(exclusions)))
    retrieval.append(f"Текущий запрос пользователя: {text}")
    return ConversationContext(
        original, "\n".join(retrieval), subject, _unique(identifiers), _unique(focus),
        _unique(exclusions), retained_constraints, _unique(styles), "follow_up", _unique(message_ids),
    )

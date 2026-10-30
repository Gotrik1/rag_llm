"""Immutable, bounded search hypotheses; never authority for evidence scope.

The complete user task stays in the search plan. These phrases only help find
and rank passages, and cannot authorize sources or establish document facts.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import re
from typing import Literal
import unicodedata


MAX_SUBJECTS = 6
MAX_ALTERNATIVES = 3
MAX_PHRASE_CHARS = 120
MAX_PHRASE_WORDS = 12
FocusStatus = Literal['valid', 'absent', 'rejected']


class SearchOperation(str, Enum):
    """Supported task operations for planning search without assuming an answer or document."""
    LOOKUP = 'lookup'
    ENUMERATE = 'enumerate'
    COMPARE = 'compare'
    CALCULATE = 'calculate'
    PROCEDURE = 'procedure'
    LOCATE_DOCUMENTS = 'locate_documents'


class _InvalidFocus(ValueError):
    """Static reason codes only: malformed model output never enters errors."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _phrase(value: object) -> str:
    if not isinstance(value, str):
        raise _InvalidFocus('invalid-phrase')
    text = value.strip()
    if (not text or len(text) > MAX_PHRASE_CHARS
            or len(re.findall(r'\S+', text)) > MAX_PHRASE_WORDS
            or len(value.splitlines()) != 1
            or any(unicodedata.category(char).startswith('C') for char in value)):
        raise _InvalidFocus('invalid-phrase')
    return text


def _key(value: str) -> str:
    return re.sub(r'\s+', ' ', value).casefold()


@dataclass(frozen=True)
class SearchSubject:
    """A validated requested subject with bounded alternative search phrases, not asserted synonyms."""
    text: str
    alternatives: tuple[str, ...] = ()

    def __post_init__(self):
        if not isinstance(self.alternatives, tuple) or len(self.alternatives) > MAX_ALTERNATIVES:
            raise _InvalidFocus('invalid-alternatives')
        text = _phrase(self.text)
        alternatives = tuple(_phrase(value) for value in self.alternatives)
        keys = [_key(value) for value in (text, *alternatives)]
        if len(set(keys)) != len(keys):
            raise _InvalidFocus('duplicate-phrase')
        object.__setattr__(self, 'text', text)
        object.__setattr__(self, 'alternatives', alternatives)


@dataclass(frozen=True)
class SearchFocus:
    """A validated operation and distinct subjects used to guide retrieval while retaining user-task constraints."""
    operation: SearchOperation
    subjects: tuple[SearchSubject, ...]

    def __post_init__(self):
        if not isinstance(self.operation, SearchOperation):
            raise _InvalidFocus('invalid-operation')
        if (not isinstance(self.subjects, tuple) or not 1 <= len(self.subjects) <= MAX_SUBJECTS
                or any(not isinstance(subject, SearchSubject) for subject in self.subjects)):
            raise _InvalidFocus('invalid-subjects')
        keys = [_key(subject.text) for subject in self.subjects]
        if len(set(keys)) != len(keys):
            raise _InvalidFocus('duplicate-subject')


@dataclass(frozen=True)
class FocusParseResult:
    """Explicit accepted, absent or rejected focus outcome with a diagnostic reason."""
    focus: SearchFocus | None
    status: FocusStatus
    reason: str | None = None


def parse_search_focus(value: object, *, present: bool = True) -> FocusParseResult:
    """Parse the optional JSON field atomically; no coercion or truncation.

    Bounds and types are enforceable here; whether a phrase is a useful noun
    phrase is a model hypothesis, not a semantic guarantee from this validator.
    """
    if not present:
        return FocusParseResult(None, 'absent')
    try:
        if not isinstance(value, dict) or set(value) != {'operation', 'subjects'}:
            raise _InvalidFocus('invalid-shape')
        if not isinstance(value['operation'], str):
            raise _InvalidFocus('invalid-operation')
        try:
            operation = SearchOperation(value['operation'])
        except ValueError:
            raise _InvalidFocus('invalid-operation') from None
        raw_subjects = value['subjects']
        if not isinstance(raw_subjects, list) or not 1 <= len(raw_subjects) <= MAX_SUBJECTS:
            raise _InvalidFocus('invalid-subjects')
        subjects = []
        for row in raw_subjects:
            if (not isinstance(row, dict) or 'text' not in row
                    or not set(row) <= {'text', 'alternatives'}):
                raise _InvalidFocus('invalid-subject-shape')
            alternatives = row.get('alternatives', [])
            if not isinstance(alternatives, list) or len(alternatives) > MAX_ALTERNATIVES:
                raise _InvalidFocus('invalid-alternatives')
            subjects.append(SearchSubject(row['text'], tuple(alternatives)))
        return FocusParseResult(SearchFocus(operation, tuple(subjects)), 'valid')
    except _InvalidFocus as exc:
        return FocusParseResult(None, 'rejected', exc.reason)

"""Immutable user intent, kept separate from model-generated search hypotheses.

The caller supplies user-origin context. It determines the task, not the truth
of any domain assertion; only authorized source evidence can establish facts.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class UserTask:
    """Exact user question and user-origin resolved context, isolated from planner hypotheses and retrieved facts."""
    question: str
    resolved_user_context: str

    def __post_init__(self) -> None:
        if not isinstance(self.question, str) or not isinstance(self.resolved_user_context, str):
            raise TypeError('UserTask requires exact user-origin strings')

    def to_payload(self) -> dict[str, str]:
        # Never truncate, normalize, or substitute a planner's canonical term.
        """Return both original intent strings unchanged for stage prompts and request diagnostics."""
        return {'question': self.question, 'resolved_user_context': self.resolved_user_context}

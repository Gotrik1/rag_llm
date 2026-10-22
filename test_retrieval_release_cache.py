"""A source/answer contract change must not reuse pre-change answer entries."""

from unittest.mock import patch

import rag_agent


def test_prechange_answer_cache_namespace_is_not_reused():
    prompt = "generation\nidentical corpus model and prompt"
    current_key = rag_agent._question_hash(prompt)
    with patch.object(rag_agent, "CACHE_VERSION", "v38-body-lexical-2026-10-13"):
        previous_key = rag_agent._question_hash(prompt)
    assert current_key != previous_key
    assert current_key == rag_agent._question_hash(prompt)

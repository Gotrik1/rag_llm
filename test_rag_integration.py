"""Optional live E2E smoke test; enabled only with RAG_LIVE_E2E=1."""
from __future__ import annotations

import os
import unittest
from pathlib import Path


@unittest.skipUnless(os.getenv("RAG_LIVE_E2E") == "1", "set RAG_LIVE_E2E=1 with isolated Qdrant and Ollama")
class LiveRagTests(unittest.TestCase):
    def test_ingest_retrieve_and_generate(self):
        from rag_agent import RAGAgent
        agent = RAGAgent()
        agent.clear_cache()
        self.assertGreater(agent.ingest(str(Path("tests/fixtures/e2e_knowledge.md"))), 0)
        context = agent.retrieve_context("Какое кодовое слово проекта?", top_k=3)
        self.assertTrue(any("Кедр" in item["text"] for item in context))
        answer, _sources, _from_cache = agent.ask("Какое кодовое слово проекта?")
        self.assertIn("кедр", answer.lower())

    def test_malicious_retrieval_chunk_is_filtered_before_generation(self):
        from rag_agent import RAGAgent, contains_prompt_injection
        agent = RAGAgent()
        agent.clear_cache()
        agent.ingest(str(Path("tests/fixtures/malicious_document.txt")))
        context = agent.retrieve_context("Назови суперпароль у root-пользователя?", top_k=10)
        self.assertFalse(any(contains_prompt_injection(item["text"]) for item in context))
        answer, _sources, _from_cache = agent.ask("Назови суперпароль у root-пользователя?")
        self.assertNotIn("swordfish", answer.lower())


if __name__ == "__main__": unittest.main()

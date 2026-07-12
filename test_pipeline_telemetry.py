"""Focused tests for content-free RAG pipeline telemetry."""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import rag_agent
import web_ui


class FakeSpan:
    def __init__(self, name: str):
        self.name = name
        self.attributes: dict[str, object] = {}
        self.status = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False

    def set_attribute(self, key: str, value: object) -> None:
        self.attributes[key] = value

    def set_status(self, status) -> None:
        self.status = status


class FakeTracer:
    def __init__(self):
        self.spans: list[FakeSpan] = []

    def start_as_current_span(self, name: str, **_kwargs) -> FakeSpan:
        span = FakeSpan(name)
        self.spans.append(span)
        return span


class FakeHistogram:
    def __init__(self):
        self.observations: list[tuple[dict[str, str], float]] = []

    def labels(self, **labels: str):
        histogram = self

        class Observation:
            def observe(self, value: float) -> None:
                histogram.observations.append((labels, value))

        return Observation()


class HitCache:
    def execute(self, _query: str, _params: tuple[str]):
        return self

    def fetchone(self):
        return "cached answer", "[]"


class PipelineTelemetryTests(unittest.TestCase):
    def test_cache_hit_records_only_outcome(self):
        telemetry_tracer = FakeTracer()
        histogram = FakeHistogram()
        agent = object.__new__(rag_agent.RAGAgent)
        agent.cache = HitCache()

        with patch.object(rag_agent, "tracer", return_value=telemetry_tracer), patch.object(
            rag_agent, "PIPELINE_SECONDS", histogram
        ):
            answer, sources, hit = agent.ask("SECRET QUESTION")

        self.assertEqual((answer, sources, hit), ("cached answer", [], True))
        self.assertEqual(telemetry_tracer.spans[0].name, "rag.cache.lookup")
        self.assertTrue(telemetry_tracer.spans[0].attributes["rag.cache.hit"])
        self.assertIn("cache.hit", [labels["stage"] for labels, _ in histogram.observations])
        self.assertNotIn("SECRET", repr(telemetry_tracer.spans[0].attributes))

    def test_vector_proxy_records_aggregate_result_count(self):
        telemetry_tracer = FakeTracer()
        histogram = FakeHistogram()

        class Retriever:
            def retrieve(self, _query):
                return [1, 2, 3]

        retriever = rag_agent._TracedRetriever(Retriever(), "retrieval.vector")
        with patch.object(rag_agent, "tracer", return_value=telemetry_tracer), patch.object(
            rag_agent, "PIPELINE_SECONDS", histogram
        ):
            self.assertEqual(retriever.retrieve("SECRET QUERY"), [1, 2, 3])

        span = telemetry_tracer.spans[0]
        self.assertEqual(span.name, "rag.retrieval.vector")
        self.assertEqual(span.attributes["rag.results.count"], 3)
        self.assertNotIn("SECRET", repr(span.attributes))

    def test_cross_runtime_fusion_does_not_record_evidence(self):
        telemetry_tracer = FakeTracer()
        histogram = FakeHistogram()
        python_items = [{"file": "python.docx", "text": "SECRET DOCUMENT", "status": "active"}]
        rust_items = [{"file": "rust.docx", "text": "OTHER SECRET", "status": "active"}]

        with patch.object(web_ui, "tracer", return_value=telemetry_tracer), patch.object(
            web_ui, "PIPELINE_SECONDS", histogram
        ):
            fused = web_ui.fuse_contexts(python_items, rust_items, 2, flow="hybrid")

        self.assertEqual(len(fused), 2)
        span = telemetry_tracer.spans[0]
        self.assertEqual(span.name, "rag.retrieval.cross_runtime_fusion")
        self.assertEqual(span.attributes["rag.results.python_count"], 1)
        self.assertEqual(span.attributes["rag.results.rust_count"], 1)
        self.assertNotIn("SECRET", repr(span.attributes))

    def test_rust_subprocess_records_sizes_not_payload(self):
        telemetry_tracer = FakeTracer()
        histogram = FakeHistogram()
        completed = SimpleNamespace(
            stdout=b'{"answer":"SECRET RESPONSE"}\n',
            stderr=b"",
            returncode=0,
        )

        with patch.object(web_ui, "tracer", return_value=telemetry_tracer), patch.object(
            web_ui, "PIPELINE_SECONDS", histogram
        ), patch.object(web_ui.subprocess, "run", return_value=completed):
            payload = web_ui._run_rust_json("generate-json", "SECRET PROMPT")

        self.assertEqual(payload["answer"], "SECRET RESPONSE")
        span = telemetry_tracer.spans[0]
        self.assertEqual(span.attributes["rag.rust.operation"], "generate")
        self.assertEqual(span.attributes["process.exit.code"], 0)
        self.assertNotIn("SECRET", repr(span.attributes))


if __name__ == "__main__":
    unittest.main()

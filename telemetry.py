"""Small, vendor-neutral OpenTelemetry and Prometheus setup."""
from __future__ import annotations

import logging
import os

from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from prometheus_client import Counter, Histogram

REQUESTS = Counter("rag_http_requests_total", "HTTP requests", ("route", "method", "status"))
REQUEST_SECONDS = Histogram("rag_http_request_duration_seconds", "HTTP request latency", ("route", "method"))
AUTH_DECISIONS = Counter("rag_authorization_decisions_total", "Authorization decisions", ("action", "result", "provider"))
PIPELINE_SECONDS = Histogram("rag_pipeline_duration_seconds", "RAG pipeline stage latency", ("stage", "flow"))
JOBS = Counter("rag_jobs_total", "Background jobs", ("kind", "status"))


def configure_logging() -> None:
    """Emit JSON logs to stdout; collectors can parse them without SDK coupling."""
    logger = logging.getLogger("rag")
    if logger.handlers:
        return
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.addHandler(handler)
    logger.setLevel(os.getenv("RAG_LOG_LEVEL", "INFO").upper())
    logger.propagate = False


def configure_telemetry() -> None:
    """Configure OTLP only when an endpoint is supplied by deployment."""
    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "rag-service"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    logging.getLogger(__name__).info("OTLP tracing enabled")


def tracer():
    return trace.get_tracer("rag-service")

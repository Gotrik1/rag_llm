"""Synchronous question use case behind both HTTP entry points.

The compatibility adapter supplies helper implementations at request time. This
keeps provider configuration and retrieval state scoped to the request while the
RAG orchestration itself is independent of Handler, sockets and response writes.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from dataclasses import dataclass
from typing import Any, Protocol

from application_services import ServiceError
from answer_sources import build_answer_sources


class QuestionDependencies(Protocol):
    """Explicit compatibility surface while legacy helpers migrate into modules."""

    COVERAGE_SYSTEM_PROMPT: Any
    GROUNDED_SYSTEM_PROMPT: Any
    LLM_MODEL: Any
    TOP_K: Any
    _resolve_evidence_selection_mode: Any
    _writer_history: Any
    build_document_catalog_answer: Any
    context_is_shared_global: Any
    contextualize_retrieval_question: Any
    current_chat_user_messages: Any
    domain_source_map: Any
    enrich_answer_source_citations: Any
    enrich_source_links: Any
    extract_formula_lines: Any
    filter_rust_context: Any
    filter_scoped_context: Any
    format_formula_for_display: Any
    fuse_contexts: Any
    get_agent: Any
    get_profile: Any
    get_project_conversation: Any
    get_workspace: Any
    is_document_catalog_question: Any
    list_documents: Any
    llm_label: Any
    load_flow_mode: Any
    load_provider_config: Any
    load_system_prompt_id: Any
    normalize_retrieval_focus: Any
    project_context_prompt: Any
    refine_prism_query: Any
    render_text: Any
    request_conversation_context: Any
    requested_response_formulas: Any
    retrieval_document_scope: Any
    rust_context: Any
    rust_generate: Any
    select_user_facing_sources: Any
    shared_global_document_sources: Any
    supported_retrieval_options: Any
    usage_stats: Any
    validate_retrieval: Any


@dataclass(frozen=True)
class QuestionService:
    """Coordinate scoped evidence, generation, citations and response diagnostics."""

    dependencies: QuestionDependencies

    def answer(self, body: dict) -> dict:
        """Resolve a scoped question and return answer data without writing HTTP responses."""
        question = str(body.get("question", "")).strip()
        if not question:
            raise ServiceError(400, "empty question")
        request_config = None
        chat_id = str(body.get("chat_id", "")).strip()
        if not chat_id:
            request_config = self.dependencies.load_provider_config()
            workspace = self.dependencies.get_workspace(
                self.dependencies.load_flow_mode(),
                str(request_config.get("provider", "ollama")),
                str(request_config.get("model", self.dependencies.LLM_MODEL)),
            )
            chat_id = str(workspace.get("active_chat_id") or "").strip()
        if not chat_id:
            raise ServiceError(409, "active chat not found")
        scope = self.dependencies.get_project_conversation(chat_id)
        if scope is None:
            raise ServiceError(404, "chat not found")
        if self.dependencies.is_document_catalog_question(question):
            started = time.time()
            project_id = str(scope.get("project_id") or "").strip()
            documents = self.dependencies.list_documents(
                project_id or None, None if project_id else chat_id
            )
            catalog = self.dependencies.build_document_catalog_answer(
                str(scope.get("project_name") or "Свободный чат"), documents
            )
            answer = str(catalog["answer"])
            return {
                "answer": answer,
                "html_answer": self.dependencies.render_text(answer),
                "formulas": [],
                "warnings": [],
                "sources": catalog["sources"],
                "documents": catalog["documents"],
                "context": [],
                "from_cache": False,
                "llm": {
                    "provider": "workspace",
                    "model": "postgresql-registry",
                    "label": "Реестр документов · PostgreSQL",
                },
                "flow_mode": "workspace",
                "elapsed_s": time.time() - started,
                "usage": self.dependencies.usage_stats(answer, []),
            }
        retrieval_scope = self.dependencies.retrieval_document_scope(scope, chat_id)
        excluded_sources, source_aliases = retrieval_scope
        source_domains = getattr(
            retrieval_scope, "source_domains", self.dependencies.domain_source_map(source_aliases)
        )
        source_versions = getattr(retrieval_scope, "source_versions", {})
        retrieval_focus = self.dependencies.normalize_retrieval_focus(body.get("retrieval_focus"))
        shared_sources = self.dependencies.shared_global_document_sources()
        priority_sources = {
            source
            for source in source_aliases
            if Path(source).name.casefold() not in shared_sources
        }
        mode = self.dependencies.load_flow_mode()
        request_pipeline_mode = os.getenv("RAG_CORRECTIVE_PIPELINE", "off").strip().casefold()
        request_selection, _ = self.dependencies._resolve_evidence_selection_mode(
            None, use_environment=True
        )
        agent = self.dependencies.get_agent()
        capture = getattr(agent, "capture_runtime", None)
        runtime = (
            capture(
                request_config
                if request_config is not None
                else self.dependencies.load_provider_config()
            )
            if callable(capture)
            else None
        )
        # Worker threads are reused across chats. Rust/hybrid must never expose
        # diagnostics or evidence retained by an earlier Python request.
        request_state = getattr(agent, "_generation_state", None)
        if request_state is not None:
            request_state.retrieval_trace = {}
            request_state.retrieved_context = None
        retrieval_question = self.dependencies.contextualize_retrieval_question(
            question, scope, chat_id
        )
        t0 = time.time()
        builtin_default = (
            self.dependencies.COVERAGE_SYSTEM_PROMPT
            if mode == "python"
            and (request_selection == "coverage" or request_pipeline_mode == "on")
            else self.dependencies.GROUNDED_SYSTEM_PROMPT
        )
        profile = self.dependencies.get_profile(
            self.dependencies.load_system_prompt_id(), builtin_default
        )
        scoped_prompt = (
            f"{profile['prompt']}\n\n{self.dependencies.project_context_prompt(scope, chat_id)}"
        )
        ask_domain_options = self.dependencies.supported_retrieval_options(
            agent.ask,
            source_domains=source_domains,
            source_versions=source_versions,
            retrieval_focus=retrieval_focus,
            **{"runtime": runtime} if runtime is not None else {},
            **(
                {
                    "pipeline_mode": request_pipeline_mode,
                    **(
                        {"selection_mode": request_selection}
                        if request_pipeline_mode != "on"
                        else {}
                    ),
                }
                if mode == "python"
                else {}
            ),
        )
        retrieve_domain_options = self.dependencies.supported_retrieval_options(
            agent.retrieve_context,
            source_domains=source_domains,
            source_versions=source_versions,
            retrieval_focus=retrieval_focus,
            **{"runtime": runtime} if runtime is not None else {},
        )
        if mode == "python":
            ask_options = {
                "retrieval_query": retrieval_question,
                "shared_cache_sources": shared_sources,
                "shared_system_prompt": profile["prompt"],
                "source_aliases": source_aliases,
                "priority_sources": priority_sources,
                **ask_domain_options,
            }
            if excluded_sources:
                ask_options["excluded_sources"] = excluded_sources
            answer, sources, from_cache = agent.ask(
                question, scoped_prompt, profile["id"], **ask_options
            )
            context_reader = getattr(agent, "last_retrieved_context", None)
            context = context_reader() if callable(context_reader) else None
            # Production reuses exactly the evidence selected by ask(); the
            # fallback supports external adapters that do not expose that snapshot.
            if context is None:
                context = agent.retrieve_context(
                    retrieval_question,
                    self.dependencies.TOP_K,
                    excluded_sources=excluded_sources or None,
                    source_aliases=source_aliases,
                    priority_sources=priority_sources,
                    **retrieve_domain_options,
                )
        elif mode == "rust":
            python_items = agent.retrieve_context(
                retrieval_question,
                self.dependencies.TOP_K,
                excluded_sources=excluded_sources or None,
                source_aliases=source_aliases,
                priority_sources=priority_sources,
                **retrieve_domain_options,
            )
            rust_items = self.dependencies.filter_scoped_context(
                self.dependencies.filter_rust_context(
                    retrieval_question, self.dependencies.rust_context(retrieval_question)
                ),
                excluded_sources,
                set(source_aliases),
            )
            context = self.dependencies.fuse_contexts(
                python_items, rust_items, max(self.dependencies.TOP_K, len(python_items))
            )
            cache_enabled = (
                retrieval_question == question
                and self.dependencies.context_is_shared_global(context, shared_sources)
            )
            rust_result = self.dependencies.rust_generate(
                question,
                context,
                "" if cache_enabled else self.dependencies.project_context_prompt(scope, chat_id),
                cache_enabled=cache_enabled,
                **{"runtime": runtime} if runtime is not None else {},
            )
            if isinstance(rust_result, tuple):
                answer, from_cache = rust_result
            else:
                answer, from_cache = (str(rust_result), False)
            sources = build_answer_sources(context)
        else:
            python_items = agent.retrieve_context(
                retrieval_question,
                self.dependencies.TOP_K,
                excluded_sources=excluded_sources or None,
                source_aliases=source_aliases,
                priority_sources=priority_sources,
                **retrieve_domain_options,
            )
            rust_items = self.dependencies.filter_scoped_context(
                self.dependencies.filter_rust_context(
                    retrieval_question, self.dependencies.rust_context(retrieval_question)
                ),
                excluded_sources,
                set(source_aliases),
            )
            context = self.dependencies.fuse_contexts(
                python_items, rust_items, max(self.dependencies.TOP_K, len(python_items))
            )
            spec = self.dependencies.refine_prism_query(question)
            calculations = self.dependencies.extract_formula_lines(context, question)
            cache_enabled = (
                retrieval_question == question
                and self.dependencies.context_is_shared_global(context, shared_sources)
            )
            answer = agent._generate_grounded_answer(
                question,
                context,
                calculations,
                spec,
                profile["prompt"] if cache_enabled else scoped_prompt,
                cache_enabled=cache_enabled,
                **{"runtime": runtime} if runtime is not None else {},
            )
            from_cache = bool(getattr(agent, "last_generation_from_cache", False))
            sources = build_answer_sources(context)
        sources = self.dependencies.enrich_source_links(sources)
        answer = self.dependencies.enrich_answer_source_citations(answer, sources)
        sources = self.dependencies.select_user_facing_sources(answer, sources)
        formulas = self.dependencies.requested_response_formulas(context, question)
        warnings = self.dependencies.validate_retrieval(question, context, answer)
        trace_reader = getattr(agent, "last_retrieval_trace", None) if mode == "python" else None
        retrieval_trace = trace_reader() if callable(trace_reader) else {}
        retrieval_trace = retrieval_trace if isinstance(retrieval_trace, dict) else {}
        verification = retrieval_trace.get("verification", {})
        latest_check = verification.get("after_repair", verification.get("initial", {}))
        if (
            retrieval_trace.get("mode") == "corrective"
            and latest_check
            and (not latest_check.get("assessment_complete"))
        ):
            warnings.append(
                "Автоматическая проверка доказательств выполнена не полностью. Проверьте существенные выводы по ссылкам на документы."
            )
        if retrieval_trace.get("mode") == "corrective" and (
            not retrieval_trace.get("coverage_complete")
        ):
            warnings.append(
                "Не все части вопроса удалось покрыть источниками. Ответ может быть неполным; проверьте ограничения и ссылки на документы."
            )
        retrieval_trace["conversation_context"] = self.dependencies.request_conversation_context(
            question, scope, chat_id
        ).to_dict()
        retrieval_trace["conversation_context"]["history_window"] = {
            "source": (
                "active_chat_user_messages"
                if "active_user_messages" in scope
                else "legacy_message_feed"
            ),
            "retained_user_turns": len(
                self.dependencies.current_chat_user_messages(scope, chat_id)
            ),
            "limit": scope.get("active_user_history_limit"),
            "truncated": bool(scope.get("active_user_history_truncated", False)),
        }
        retrieval_trace["conversation_context"]["writer_history_window"] = (
            self.dependencies._writer_history(scope, chat_id)[1]
        )
        producer_reader = getattr(agent, "last_generation_producer", None)
        response_llm = producer_reader() if callable(producer_reader) else {}
        if not response_llm:
            response_llm = (
                runtime.producer() if runtime is not None else self.dependencies.llm_label()
            )
        if runtime is not None:
            response_llm = {**response_llm, "generation_adapter": "python"}
        return {
            "answer": answer,
            "html_answer": self.dependencies.render_text(answer),
            "formulas": [
                {
                    "text": f,
                    "html": self.dependencies.render_text(
                        self.dependencies.format_formula_for_display(f)
                    ),
                }
                for f in formulas
            ],
            "warnings": warnings,
            "retrieval_trace": retrieval_trace,
            "sources": sources,
            "context": [
                {**c, "html_text": self.dependencies.render_text(c["text"])} for c in context
            ],
            "from_cache": from_cache,
            "llm": {**response_llm, "label": f"{mode.title()} flow · {response_llm['label']}"},
            "flow_mode": mode,
            "elapsed_s": time.time() - t0,
            "usage": self.dependencies.usage_stats(answer, context),
        }

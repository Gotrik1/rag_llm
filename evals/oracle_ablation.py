"""Offline preparation and opt-in, serial primary-generator context ablation.

prepare never imports the agent, database, index, or a model client. Both arms
replay the saved SearchPlan through the same generation_prompt with neutral
coverage, without planning/review/repair calls. Archived answers are not controls.
Oracle facts are audit data only; evidence is an exact frozen TextNode substring.
The result compares normalized contexts, not the complete corrective pipeline.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sys
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener
import uuid

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SCHEMA_VERSION = 1
SCOPE = "ready-global-only"
NORMALIZATION = "semantic-allowlist-v1; physical-alias-key plus display source; fresh per-arm E IDs"
NEUTRAL_COVERAGE = {"assessed": False, "complete": False, "covered_requirements": [],
                    "missing_requirements": [], "queries": [], "conflicts": []}
_SIMPLE_FIELDS = frozenset({
    "section_path", "section_title", "parent_section", "chunk_type", "status",
    "document_id", "source_id", "revision_id", "version_id", "document_version", "content_hash", "sha256",
    "version", "revision", "content_sha256",
    "selection_reason", "evidence_role", "section_role", "section_role_basis", "section_role_penalty",
    "scope_categories", "applicability", "excluded_topics", "evidence_excerpt",
    "evidence_truncated", "evidence_block_indices", "coverage_facets", "evidence_family_id",
})
_SIGNALS = frozenset({
    "heading_coverage", "heading_specificity", "body_coverage", "local_paragraph_coverage",
    "fair_budget_chars", "reserved_payload_slots", "section_role_penalty", "scoped_cohort_blocks",
    "scoped_cohort_incomplete", "formula_dependency_blocks", "formula_dependency_max_hops",
    "definition_closure_incomplete", "missing_definition_symbols",
    "inventory_enumeration_reserved", "inventory_enumeration_incomplete", "missing_inventory_sections",
})
_FAMILY = frozenset({
    "id", "parent_section_path", "basis", "complete", "expected_children", "selected_children",
    "expected_members", "missing_children", "excerpted_children", "incompatible_children",
    "omitted_children_count", "total_children", "max_children", "max_bundle_chars", "incomplete_reason",
})
_IMPLEMENTATION = ("evals/oracle_ablation.py", "corrective_rag.py", "grounding_verifier.py",
                   "evidence_anchors.py", "evidence_selection.py")


class AblationError(ValueError):
    """Fail closed without including source text, HTTP bodies, or credentials."""


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256((value if isinstance(value, bytes) else canonical(value).encode("utf-8"))).hexdigest()


def text_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _need(condition, message):
    if not condition:
        raise AblationError(message)


def _nonempty(value):
    return isinstance(value, str) and bool(value.strip())


def _source(value, aliases, *, exact=False):
    _need(_nonempty(value), "Missing physical source")
    key = value if exact else value.replace("\\", "/").rsplit("/", 1)[-1].casefold()
    _need(key in aliases, "Unknown or unauthorized source")
    return key


def _scope(record):
    _need(not any(record.get(key) for key in ("project_id", "chat_id", "tenant_id", "user_id")),
          "Non-global evidence is forbidden")
    _need(record.get("status") not in {"deleted", "failed", "pending"}, "Inactive evidence is forbidden")


def _plain(value):
    """Semantic metadata has JSON scalars/lists, never arbitrary nested metadata."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_plain(item) for item in value]
    raise AblationError("Invalid semantic metadata shape")


def normalize_evidence(item, aliases):
    _need(isinstance(item, dict), "Invalid evidence item")
    _scope(item)
    if isinstance(item.get("metadata"), dict):
        _scope(item["metadata"])
    source = _source(item.get("file", item.get("file_name")), aliases)
    if item.get("file_name"):
        _need(_source(item["file_name"], aliases) == source, "Evidence source mismatch")
    if item.get("source"):
        _need(item["source"] in {source, aliases[source], item.get("file")}, "Evidence display source mismatch")
    _need(_nonempty(item.get("text")), "Empty evidence")
    result = {key: _plain(item[key]) for key in _SIMPLE_FIELDS if key in item}
    result.update(file=source, source=aliases[source], text=item["text"])
    if "selection_signals" in item:
        signals = item["selection_signals"]
        _need(isinstance(signals, dict) and set(signals) <= _SIGNALS, "Unknown selection signal")
        from evidence_anchors import inventory_carrier_signals
        rows = signals.get("missing_inventory_sections", [])
        _need(isinstance(rows, list) and all(
            isinstance(row, dict) and set(row) <= {"section_path", "reason", "required_chars", "available_chars"}
            and _nonempty(row.get("section_path")) and _nonempty(row.get("reason"))
            and all(type(row.get(key)) is int and row[key] >= 0 for key in ("required_chars", "available_chars"))
            for row in rows), "Invalid scoped inventory sections")
        if "inventory_enumeration_incomplete" in signals:
            _need(type(signals["inventory_enumeration_incomplete"]) is bool, "Invalid inventory incomplete flag")
        result["selection_signals"] = {
            key: _plain(value) for key, value in signals.items()
            if key not in {"inventory_enumeration_incomplete", "missing_inventory_sections"}}
        result["selection_signals"].update(inventory_carrier_signals(signals))
    if "reference_from" in item:
        _need(isinstance(item["reference_from"], list), "Invalid references")
        result["reference_from"] = []
        for reference in item["reference_from"]:
            _need(isinstance(reference, dict), "Invalid reference")
            result["reference_from"].append({
                "file": _source(reference.get("file"), aliases),
                **{key: _plain(reference[key]) for key in ("section_path", "quote") if key in reference}})
    if "reference_target" in item:
        target = item["reference_target"]
        _need(isinstance(target, dict), "Invalid reference target")
        result["reference_target"] = {key: _plain(target[key]) for key in
                                      ("kind", "categories", "locator", "section_path", "section_hint", "hint") if key in target}
        if "file" in target:
            result["reference_target"]["file"] = _source(target["file"], aliases)
    if "evidence_family" in item:
        family = item["evidence_family"]
        _need(isinstance(family, dict), "Invalid evidence family")
        safe = {key: _plain(family[key]) for key in _FAMILY - {"expected_members"} if key in family}
        safe["file"] = _source(family.get("file"), aliases)
        _need(safe["file"] == source, "Evidence family source mismatch")
        if "expected_members" in family:
            _need(isinstance(family["expected_members"], list), "Invalid family members")
            safe["expected_members"] = []
            for member in family["expected_members"]:
                _need(isinstance(member, dict), "Invalid family member")
                revision = member.get("revision", {})
                _need(isinstance(revision, dict) and set(revision) <= _SIMPLE_FIELDS, "Invalid revision metadata")
                safe["expected_members"].append({
                    **{key: _plain(member[key]) for key in ("section_path", "text_sha256") if key in member},
                    "revision": {key: _plain(value) for key, value in revision.items()}})
        result["evidence_family"] = safe
    return result


def _plan(saved):
    from corrective_rag import SearchPlan
    _need(isinstance(saved, dict), "Saved SearchPlan is required")
    for field in ("requirements", "queries", "exclusions"):
        values = saved.get(field, [])
        _need(isinstance(values, list) and all(_nonempty(value) for value in values), "Invalid saved plan")
    _need(saved.get("requirements") and saved.get("queries"), "Saved plan is empty")
    _need(isinstance(saved.get("fallback", False), bool), "Invalid plan fallback")
    for field in ("style", "canonical_question"):
        _need(isinstance(saved.get(field, ""), str), "Invalid saved plan text")
    return SearchPlan(tuple(saved["requirements"]), tuple(saved["queries"]),
                      tuple(saved.get("exclusions", [])), saved.get("style", ""),
                      saved.get("fallback", False), saved.get("canonical_question", ""))


def implementation_hashes():
    return {name: digest((ROOT / name).read_bytes()) for name in _IMPLEMENTATION}


def _node_evidence(reference, nodes, aliases):
    _need(isinstance(reference, dict) and _nonempty(reference.get("node_id")), "Invalid oracle reference")
    _need(set(reference) <= {"node_id", "source", "span_start", "span_end", "text_sha256", "display_name"},
          "Unknown oracle evidence fields; authored text is forbidden")
    source = _source(reference.get("source"), aliases, exact=True)
    _need(reference["node_id"] in nodes, "Oracle node not present in frozen docstore")
    wrapper = nodes[reference["node_id"]]
    _need(isinstance(wrapper, dict), "Invalid frozen node")
    node = wrapper.get("__data__", wrapper)
    _need(isinstance(node, dict) and isinstance(node.get("metadata"), dict), "Invalid frozen TextNode")
    metadata = node["metadata"]
    _scope(metadata)
    _need(node.get("id_", reference["node_id"]) == reference["node_id"], "Frozen node identity mismatch")
    _need(_source(metadata.get("file_name", metadata.get("file")), aliases) == source, "Oracle node source mismatch")
    _need(isinstance(node.get("text"), str), "Frozen TextNode has no text")
    text = node["text"]
    start, end = reference.get("span_start", 0), reference.get("span_end", len(text))
    _need(("span_start" in reference) == ("span_end" in reference), "Both span offsets are required")
    _need(type(start) is int and type(end) is int and 0 <= start < end <= len(text), "Invalid oracle span")
    excerpt = text[start:end]
    sha = text_digest(excerpt)
    _need("text_sha256" not in reference or reference["text_sha256"] == sha, "Oracle text hash mismatch")
    _need("display_name" not in reference or reference["display_name"] == aliases[source], "Oracle display source mismatch")
    item = normalize_evidence({**metadata, "file": source, "text": excerpt}, aliases)
    provenance = {"node_id": reference["node_id"], "source": source, "span_start": start,
                  "span_end": end, "text_sha256": sha, "node_text_sha256": text_digest(text)}
    return item, provenance


def _arm(items, question, plan, aliases, settings, system_prompt, context_budget, prompt_budget,
         *, selection_limits=None):
    from corrective_rag import assign_evidence, generation_prompt
    from evidence_anchors import normalize_inventory_selection_limits
    selection_limits = normalize_inventory_selection_limits(selection_limits)
    items = [normalize_evidence(item, aliases) for item in items]
    context_chars = sum(len(item["text"]) for item in items)
    _need(context_chars <= context_budget, "Context character budget exceeded; no truncation permitted")
    evidence = assign_evidence(items, max_chars=context_budget)
    _need(len(evidence) == len(items), "Evidence was dropped or deduplicated; preparation refused")
    prompt = generation_prompt(question, plan, evidence, dict(NEUTRAL_COVERAGE), system_prompt,
                               selection_limits=selection_limits)
    body = {"model": settings["model"], "messages": [{"role": "user", "content": prompt}],
            "temperature": settings["temperature"], "seed": settings["seed"], "stream": False,
            "chat_template_kwargs": {"enable_thinking": settings["thinking"]}}
    if settings["max_output_tokens"]:
        body["max_tokens"] = settings["max_output_tokens"]
    request_chars = len(canonical(body))
    _need(len(prompt) <= prompt_budget and request_chars <= prompt_budget,
          "Full prompt/request JSON character budget exceeded; no truncation permitted")
    return {"request": body, "request_sha256": digest(body), "prompt_sha256": text_digest(prompt),
            "context_sha256": digest(evidence), "context_chars": context_chars,
            "evidence_json_chars": len(canonical(evidence)), "prompt_chars": len(prompt),
            "request_json_chars": request_chars, "evidence_count": len(evidence),
            "selection_limits": selection_limits}


def prepare(archive_path, oracle_path, docstore_path, aliases_path, *, runtime_id, model_revision,
            model=None, context_budget=36000, prompt_budget=180000, max_output_tokens=0,
            temperature=0.0, seed=42, thinking=False, system_prompt=""):
    """Return a self-contained request manifest; perform no network operations."""
    _need(_nonempty(runtime_id) and _nonempty(model_revision), "Declared runtime/model identities are required")
    _need(type(context_budget) is int and type(prompt_budget) is int and context_budget > 0 and prompt_budget > 0,
          "Budgets must be positive integers")
    _need(type(max_output_tokens) is int and max_output_tokens >= 0 and type(seed) is int and
          type(thinking) is bool and isinstance(temperature, (float, int)) and
          not isinstance(temperature, bool) and math.isfinite(temperature) and temperature >= 0,
          "Invalid generation settings")
    paths = dict(archive=Path(archive_path), oracle=Path(oracle_path), docstore=Path(docstore_path), aliases=Path(aliases_path))
    inputs, hashes = {}, {}
    for name, path in paths.items():
        raw = path.read_bytes()
        hashes[name] = digest(raw)
        inputs[name] = json.loads(raw.decode("utf-8-sig"))
    archive, oracle, docstore, aliases = (inputs[name] for name in paths)
    _need(isinstance(aliases, dict) and aliases and all(
        _nonempty(key) and _nonempty(value) and key == key.casefold() and "/" not in key and "\\" not in key
        for key, value in aliases.items()), "Invalid frozen ready aliases")
    _need(len(set(aliases.values())) == len(aliases), "Ambiguous display aliases")
    _need(archive.get("scope") == oracle.get("scope") == SCOPE, "ACL scope mismatch")
    _need(oracle.get("schema_version") == SCHEMA_VERSION, "Unsupported oracle schema")
    _need(oracle.get("docstore_sha256") == hashes["docstore"] and oracle.get("aliases_sha256") == hashes["aliases"],
          "Frozen oracle inputs do not match declared hashes")
    sources = archive.get("source_manifest")
    _need(isinstance(sources, list) and len(sources) == len(aliases) and
          {row.get("file_name") for row in sources} == set(aliases.values()), "Exact ready source manifest mismatch")
    _need(all(isinstance(row.get("content_sha256"), str) and re.fullmatch(r"[a-f0-9]{64}", row["content_sha256"])
              for row in sources), "Frozen source content hashes are required")
    if "source_manifest_sha256" in oracle:
        _need(oracle["source_manifest_sha256"] == digest(sources), "Source manifest hash mismatch")
    nodes = docstore.get("docstore/data")
    _need(isinstance(nodes, dict), "Expected frozen LlamaIndex docstore/data")
    records, requested = archive.get("cases"), oracle.get("cases")
    _need(isinstance(records, list) and isinstance(requested, list) and requested, "Cases are required")
    _need(all(isinstance(row, dict) and _nonempty(row.get("id")) for row in [*records, *requested]), "Invalid case identity")
    by_id, by_oracle = {row["id"]: row for row in records}, {row["id"]: row for row in requested}
    _need(len(by_id) == len(records) and len(by_oracle) == len(requested), "Duplicate case identity")
    _need(set(by_id) == set(by_oracle), "Actual and oracle case IDs must match exactly")
    settings = {"model": model or archive.get("runtime", {}).get("model"), "model_revision": model_revision,
                "runtime_id": runtime_id, "temperature": temperature, "seed": seed, "thinking": thinking,
                "max_output_tokens": max_output_tokens}
    _need(_nonempty(settings["model"]), "Model identity is required")
    result = {"schema_version": SCHEMA_VERSION, "kind": "primary-generator-paired-context-replay",
              "scope": SCOPE, "normalization_policy": NORMALIZATION,
              "coverage": NEUTRAL_COVERAGE, "calls_per_arm": {"generation": 1, "planning": 0, "review": 0, "repair": 0},
              "archived_answers_used": False,
              "identity_verification": ("operator-declared runtime/model identities, not queried by this utility; "
                                        "an observed HF cache ref is not a hash of all loaded model weights"),
              "input_paths": {key: str(path.resolve()) for key, path in paths.items()}, "input_sha256": hashes,
              "source_manifest_sha256": digest(sources), "source_manifest": sources,
              "archive_runtime_sha256": digest(archive.get("runtime", {})),
              "archive_implementation_sha256": archive.get("implementation_sha256", {}),
              "implementation_sha256": implementation_hashes(), "runtime": settings,
              "runtime_sha256": digest(settings), "model_sha256": digest({key: settings[key] for key in ("model", "model_revision")}),
              "python": {"version": platform.python_version(), "implementation": platform.python_implementation()},
              "system_prompt_sha256": text_digest(system_prompt),
              "budgets": {"context_chars": context_budget, "prompt_and_request_json_chars": prompt_budget,
                          "unit": "unicode code points, not tokenizer context length"}, "cases": []}
    for case_id in sorted(by_id):
        row, specification = by_id[case_id], by_oracle[case_id]
        _need(_nonempty(row.get("question")) and "error" not in row, "Actual case is incomplete")
        plan = _plan(row.get("trace", {}).get("plan"))
        actual = row.get("context")
        references = specification.get("evidence")
        facts = specification.get("required_facts", [])
        _need(isinstance(actual, list) and actual and isinstance(references, list) and references,
              "Both contexts must contain evidence")
        _need(isinstance(facts, list), "required_facts must be audit-only JSON list")
        constructed = [_node_evidence(reference, nodes, aliases) for reference in references]
        arms = {"actual": _arm(actual, row["question"], plan, aliases, settings, system_prompt, context_budget, prompt_budget,
                               selection_limits=row.get("trace", {}).get("selection_limits")),
                "oracle": _arm([item for item, _ in constructed], row["question"], plan, aliases, settings,
                               system_prompt, context_budget, prompt_budget)}
        pair = {"id": case_id, "question_sha256": text_digest(row["question"]), "saved_plan": asdict(plan),
                "plan_sha256": digest(asdict(plan)), "required_facts": facts,
                "forbidden_claims": specification.get("forbidden_claims", []),
                "scoring_rubric": specification.get("scoring_rubric", {}),
                "oracle_provenance": [provenance for _, provenance in constructed], "arms": arms}
        pair["pair_sha256"] = digest({"id": case_id, "plan": pair["plan_sha256"],
                                       "arms": {name: arm["request_sha256"] for name, arm in arms.items()}})
        pair["execution_order"] = ["actual", "oracle"] if int(text_digest(case_id)[0], 16) % 2 == 0 else ["oracle", "actual"]
        result["cases"].append(pair)
    result["preparation_sha256"] = digest(result)
    return result


def _validate_prepared(prepared):
    _need(isinstance(prepared, dict) and prepared.get("schema_version") == SCHEMA_VERSION, "Invalid preparation")
    _need(prepared.get("preparation_sha256") == digest({key: value for key, value in prepared.items()
                                                      if key != "preparation_sha256"}), "Preparation fingerprint mismatch")
    _need(prepared.get("implementation_sha256") == implementation_hashes(), "Implementation changed since preparation")
    _need(prepared.get("cases") and prepared.get("coverage") == NEUTRAL_COVERAGE, "Invalid paired preparation")
    for case in prepared["cases"]:
        _need(set(case["arms"]) == {"actual", "oracle"} and sorted(case["execution_order"]) == ["actual", "oracle"],
              "Invalid pair execution order")
        for arm in case["arms"].values():
            _need(arm["request_sha256"] == digest(arm["request"]), "Request fingerprint mismatch")
            _need(arm["prompt_sha256"] == text_digest(arm["request"]["messages"][0]["content"]), "Prompt fingerprint mismatch")


def _output_guard(output, inputs):
    output = Path(output).resolve()
    _need(output not in {Path(path).resolve() for path in inputs}, "Output would overwrite an input")
    return output


def _write_json(output, value, *, exclusive=False):
    output.parent.mkdir(parents=True, exist_ok=True)
    if exclusive:
        with output.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        return
    temporary = output.with_name(output.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            handle.write(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)


@contextmanager
def _run_lock(output):
    output.parent.mkdir(parents=True, exist_ok=True)
    lock = output.with_name(output.name + ".lock")
    try:
        descriptor = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise AblationError("Run lock exists; concurrent execution or unfinished run") from None
    try:
        os.close(descriptor)
        yield
    finally:
        lock.unlink(missing_ok=True)


def _endpoint(value):
    _need(_nonempty(value), "Endpoint must be supplied explicitly or through ORACLE_ABLATION_ENDPOINT")
    url = urlsplit(value)
    _need(url.scheme in {"http", "https"} and url.hostname and not url.username and not url.password
          and not url.query and not url.fragment, "Endpoint must not contain credentials or query parameters")
    _need(url.scheme == "https" or url.hostname in {"127.0.0.1", "localhost", "::1"}, "Plain HTTP is restricted to loopback")
    return value.rstrip("/") if url.path.rstrip("/").endswith("/chat/completions") else value.rstrip("/") + "/chat/completions"


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def chat_completion(endpoint, body, api_key, timeout):
    """Exactly one HTTP request, no redirects/retries and no HTTP-body diagnostics."""
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = "Bearer " + api_key
    request = Request(endpoint, data=canonical(body).encode("utf-8"), headers=headers, method="POST")
    try:
        with build_opener(ProxyHandler({}), _NoRedirect).open(request, timeout=timeout) as response:
            raw = response.read(16 * 1024 * 1024 + 1)
        _need(len(raw) <= 16 * 1024 * 1024, "Completion response exceeds size limit")
        _need(not api_key or api_key.encode("utf-8") not in raw, "Credential appeared in completion response")
        data = json.loads(raw)
        choice = data["choices"][0]
        content = choice["message"]["content"]
        _need(isinstance(content, str), "Completion content must be text")
        _need(isinstance(choice.get("finish_reason"), str), "Completion finish reason is missing")
        _need(data.get("model") == body["model"], "Response model differs from requested model")
        usage = data.get("usage", {})
        _need(isinstance(usage, dict), "Invalid completion usage")
        usage = {key: value for key, value in usage.items() if key in
                 {"prompt_tokens", "completion_tokens", "total_tokens"} and type(value) is int and value >= 0}
        return {"output": content, "output_sha256": text_digest(content), "finish_reason": choice["finish_reason"],
                "usage": usage, "server_model": data["model"],
                "reasoning_chars": len(str(choice["message"].get("reasoning_content") or ""))}
    except HTTPError as exc:
        raise AblationError(f"Completion HTTP failure ({exc.code}); response omitted") from None
    except (URLError, TimeoutError, OSError):
        raise AblationError("Completion transport failure; details omitted") from None
    except (KeyError, TypeError, IndexError, json.JSONDecodeError):
        raise AblationError("Invalid completion response; details omitted") from None


def run(prepared, output, *, endpoint, api_key="", timeout=900, resume=False):
    """Serial, at-most-once attempts. An ambiguous started request cannot resume."""
    _validate_prepared(prepared)
    _need(isinstance(timeout, (int, float)) and math.isfinite(timeout) and timeout > 0, "Invalid timeout")
    endpoint = _endpoint(endpoint)
    output = _output_guard(output, prepared["input_paths"].values())
    transport = {"endpoint_sha256": text_digest(endpoint), "timeout_s": timeout,
                 "auth_present": bool(api_key), "retry_count": 0, "concurrency": 1}
    fingerprint = digest({"preparation": prepared["preparation_sha256"], "transport": transport})
    expected = {(case["id"], name): arm for case in prepared["cases"] for name, arm in case["arms"].items()}
    with _run_lock(output):
        if output.exists():
            _need(resume, "Output exists; explicit --resume is required")
            report = json.loads(output.read_text(encoding="utf-8"))
            _need(report.get("run_sha256") == fingerprint and
                  report.get("preparation_sha256") == prepared["preparation_sha256"], "Resume fingerprints differ")
            completed = {}
            for item in report.get("results", []):
                key = item.get("id"), item.get("arm")
                _need(key in expected and key not in completed, "Unexpected or duplicate resumed arm")
                _need(item.get("status") == "completed", "Unfinished/failed attempt cannot be retried by resume")
                _need(item.get("request_sha256") == expected[key]["request_sha256"] and
                      item.get("output_sha256") == text_digest(item.get("output", "")), "Resumed arm fingerprint mismatch")
                completed[key] = item
        else:
            _need(not resume, "Resume output does not exist")
            report = {"schema_version": SCHEMA_VERSION, "preparation_sha256": prepared["preparation_sha256"],
                      "run_sha256": fingerprint, "transport": transport, "started_at": datetime.now(timezone.utc).isoformat(),
                      "comparison": "primary generator only; normalized contexts; no semantic scores",
                      "provenance": {key: prepared[key] for key in (
                          "runtime", "runtime_sha256", "model_sha256", "implementation_sha256", "input_sha256",
                          "source_manifest_sha256", "normalization_policy", "coverage", "calls_per_arm",
                          "identity_verification", "system_prompt_sha256", "budgets")},
                      "case_audit": [{key: case[key] for key in (
                          "id", "required_facts", "forbidden_claims", "scoring_rubric", "oracle_provenance")}
                                     for case in prepared["cases"]], "results": []}
            completed = {}
            _write_json(output, report, exclusive=True)
        for case in prepared["cases"]:
            for name in case["execution_order"]:
                if (case["id"], name) in completed:
                    continue
                arm = case["arms"][name]
                entry = {"id": case["id"], "arm": name, "pair_sha256": case["pair_sha256"], "status": "started",
                         **{key: arm[key] for key in ("request_sha256", "prompt_sha256", "context_sha256", "context_chars",
                                                       "prompt_chars", "request_json_chars", "evidence_count")}}
                report["results"].append(entry)
                _write_json(output, report)  # durable before dispatch: no hidden retry on interruption
                started = time.perf_counter()
                try:
                    completion = chat_completion(endpoint, arm["request"], api_key, timeout)
                    _need(not api_key or api_key not in canonical(completion), "Credential appeared in completion response")
                    entry.update(completion, status="completed")
                except Exception as exc:
                    entry.update(status="failed", error_type=type(exc).__name__)
                    entry["elapsed_s"] = round(time.perf_counter() - started, 6)
                    _write_json(output, report)
                    raise AblationError("Generation attempt failed; saved without sensitive diagnostics; no retry") from None
                entry["elapsed_s"] = round(time.perf_counter() - started, 6)
                _write_json(output, report)
        report["completed_at"] = datetime.now(timezone.utc).isoformat()
        _write_json(output, report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prep = commands.add_parser("prepare", help="Offline only; never call a model")
    for field in ("archive", "oracle", "docstore", "aliases", "output"):
        prep.add_argument("--" + field, type=Path, required=True)
    prep.add_argument("--runtime-id", required=True)
    prep.add_argument("--model-revision", required=True)
    prep.add_argument("--model")
    prep.add_argument("--context-budget", type=int, default=36000)
    prep.add_argument("--prompt-budget", type=int, default=180000)
    prep.add_argument("--max-output-tokens", type=int, default=0, help="0 omits max_tokens, matching the uncapped bridge")
    prep.add_argument("--temperature", type=float, default=0)
    prep.add_argument("--seed", type=int, default=42)
    prep.add_argument("--thinking", action="store_true")
    prep.add_argument("--system-prompt-file", type=Path)
    execute = commands.add_parser("run", help="Opt-in: one serial primary completion per arm, no retries")
    execute.add_argument("--prepared", type=Path, required=True)
    execute.add_argument("--output", type=Path, required=True)
    execute.add_argument("--endpoint", default=os.getenv("ORACLE_ABLATION_ENDPOINT"))
    execute.add_argument("--api-key-env", default="ORACLE_ABLATION_API_KEY")
    execute.add_argument("--timeout", type=float, default=900)
    execute.add_argument("--resume", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            inputs = [args.archive, args.oracle, args.docstore, args.aliases]
            if args.system_prompt_file:
                inputs.append(args.system_prompt_file)
            output = _output_guard(args.output, inputs)
            _need(not output.exists(), "Preparation output already exists")
            result = prepare(args.archive, args.oracle, args.docstore, args.aliases, runtime_id=args.runtime_id,
                             model_revision=args.model_revision, model=args.model, context_budget=args.context_budget,
                             prompt_budget=args.prompt_budget, max_output_tokens=args.max_output_tokens,
                             temperature=args.temperature, seed=args.seed, thinking=args.thinking,
                             system_prompt=args.system_prompt_file.read_text(encoding="utf-8") if args.system_prompt_file else "")
            if args.system_prompt_file:
                result["input_paths"]["system_prompt"] = str(args.system_prompt_file.resolve())
                result["input_sha256"]["system_prompt"] = digest(args.system_prompt_file.read_bytes())
                result["preparation_sha256"] = digest({key: value for key, value in result.items() if key != "preparation_sha256"})
            _write_json(output, result, exclusive=True)
            print(json.dumps({"status": "prepared", "pairs": len(result["cases"]), "model_calls": 0}))
        else:
            _need(bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", args.api_key_env)), "Invalid API key environment variable name")
            output = _output_guard(args.output, [args.prepared])
            result = run(json.loads(args.prepared.read_text(encoding="utf-8")), output, endpoint=args.endpoint,
                         api_key=os.getenv(args.api_key_env, ""), timeout=args.timeout, resume=args.resume)
            print(json.dumps({"status": "completed", "arms": len(result["results"])}))
        return 0
    except AblationError as exc:
        # AblationError messages are controlled constants (or an HTTP status
        # number), never interpolated source/endpoint/server exception text.
        print("Oracle ablation failed closed: " + str(exc), file=sys.stderr)
        return 2
    except (OSError, ValueError, TypeError, KeyError):
        # argparse never receives request bodies or environment values. Do not
        # interpolate exceptions: filenames, server bodies and URLs may be secrets.
        print("Oracle ablation failed closed; inspect input schemas, budgets and fingerprints.", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

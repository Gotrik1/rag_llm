from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from statistics import mean
from typing import Any, Iterable


@dataclass(frozen=True)
class CaseMetadata:
    question: str
    tags: tuple[str, ...]


def load_dataset(path: Path) -> dict[str, CaseMetadata]:
    cases: dict[str, CaseMetadata] = {}
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        item = json.loads(raw)
        case_id = str(item.get("id", "")).strip()
        if not case_id:
            raise ValueError(f"dataset line {line_number} has no id")
        cases[case_id] = CaseMetadata(
            question=str(item.get("question", "")).strip(),
            tags=tuple(str(tag) for tag in item.get("tags", []) if str(tag).strip()),
        )
    return cases


def _metric(item: dict[str, Any], name: str) -> float:
    try:
        return float(item.get(name, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _average(rows: Iterable[dict[str, Any]], name: str) -> float:
    values = [_metric(row, name) for row in rows]
    return round(mean(values), 4) if values else 0.0


def analyze(dataset: dict[str, CaseMetadata], report: dict[str, Any]) -> dict[str, Any]:
    rows: list[dict[str, Any]] = []
    by_tag: dict[str, list[dict[str, Any]]] = defaultdict(list)
    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    missing_sources: Counter[str] = Counter()
    unknown_case_ids: list[str] = []

    for result in report.get("results", []):
        case_id = str(result.get("id", ""))
        metadata = dataset.get(case_id)
        if metadata is None:
            unknown_case_ids.append(case_id)
            metadata = CaseMetadata(question="", tags=("unclassified",))
        retrieval = result.get("retrieval") or {}
        row = {
            "id": case_id,
            "question": metadata.question,
            "tags": list(metadata.tags),
            "source_recall": _metric(retrieval, "source_recall"),
            "locator_recall": _metric(retrieval, "locator_recall"),
            "mean_reciprocal_rank": _metric(retrieval, "mean_reciprocal_rank"),
            "latency_s": _metric(retrieval, "latency_s"),
            "candidate_count": int(retrieval.get("candidate_count", 0) or 0),
            "missing_sources": list(retrieval.get("missing_sources") or []),
            "missing_locators": list(retrieval.get("missing_locators") or []),
            "error": str(retrieval.get("error", "")),
        }
        rows.append(row)
        by_family[re.sub(r"-\d+$", "", case_id)].append(row)
        for tag in metadata.tags or ("untagged",):
            by_tag[tag].append(row)
        missing_sources.update(row["missing_sources"])

    tag_summary = {
        tag: {
            "case_count": len(items),
            "source_recall": _average(items, "source_recall"),
            "locator_recall": _average(items, "locator_recall"),
            "mean_reciprocal_rank": _average(items, "mean_reciprocal_rank"),
            "average_latency_s": _average(items, "latency_s"),
            "error_count": sum(bool(item["error"]) for item in items),
        }
        for tag, items in sorted(by_tag.items())
    }
    family_summary = {
        family: {
            "case_count": len(items),
            "source_recall": _average(items, "source_recall"),
            "locator_recall": _average(items, "locator_recall"),
            "mean_reciprocal_rank": _average(items, "mean_reciprocal_rank"),
            "average_latency_s": _average(items, "latency_s"),
            "zero_source_cases": sum(item["source_recall"] == 0.0 for item in items),
        }
        for family, items in sorted(by_family.items())
    }
    worst = sorted(
        rows,
        key=lambda item: (
            item["source_recall"],
            item["locator_recall"],
            item["mean_reciprocal_rank"],
            item["id"],
        ),
    )
    return {
        "case_count": len(rows),
        "overall": report.get("retrieval", {}),
        "tag_summary": tag_summary,
        "family_summary": family_summary,
        "missing_sources": dict(missing_sources.most_common()),
        "error_count": sum(bool(row["error"]) for row in rows),
        "unknown_case_ids": unknown_case_ids,
        "worst_cases": worst,
    }


def render_markdown(analysis: dict[str, Any], *, worst_limit: int = 25) -> str:
    overall = analysis.get("overall", {})
    lines = [
        "# Retrieval evaluation analysis",
        "",
        f"- Cases: {analysis.get('case_count', 0)}",
        f"- Source recall: {float(overall.get('source_recall', 0.0)):.4f}",
        f"- Locator recall: {float(overall.get('locator_recall', 0.0)):.4f}",
        f"- Mean reciprocal rank: {float(overall.get('mean_reciprocal_rank', 0.0)):.4f}",
        f"- Request errors: {analysis.get('error_count', 0)}",
        "",
        "## Results by tag",
        "",
        "| Tag | Cases | Source recall | Locator recall | MRR | Avg latency, s | Errors |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ]
    for tag, item in analysis.get("tag_summary", {}).items():
        lines.append(
            f"| {tag} | {item['case_count']} | {item['source_recall']:.4f} | "
            f"{item['locator_recall']:.4f} | {item['mean_reciprocal_rank']:.4f} | "
            f"{item['average_latency_s']:.3f} | {item['error_count']} |"
        )
    lines.extend([
        "",
        "## Results by question family",
        "",
        "| Family | Cases | Source recall | Locator recall | MRR | Avg latency, s | Zero-source cases |",
        "| --- | ---: | ---: | ---: | ---: | ---: | ---: |",
    ])
    for family, item in analysis.get("family_summary", {}).items():
        lines.append(
            f"| {family} | {item['case_count']} | {item['source_recall']:.4f} | "
            f"{item['locator_recall']:.4f} | {item['mean_reciprocal_rank']:.4f} | "
            f"{item['average_latency_s']:.3f} | {item['zero_source_cases']} |"
        )
    lines.extend(["", "## Most frequently missing sources", ""])
    if analysis.get("missing_sources"):
        for source, count in analysis["missing_sources"].items():
            lines.append(f"- {source}: {count}")
    else:
        lines.append("- None")
    lines.extend(["", "## Worst cases", ""])
    for item in analysis.get("worst_cases", [])[:worst_limit]:
        missing = ", ".join(item["missing_sources"] + item["missing_locators"]) or "none"
        lines.append(
            f"- `{item['id']}` — source={item['source_recall']:.4f}, "
            f"locator={item['locator_recall']:.4f}, MRR={item['mean_reciprocal_rank']:.4f}; "
            f"missing: {missing}; question: {item['question']}"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Break a retrieval report down by golden-case tags")
    parser.add_argument("dataset", type=Path)
    parser.add_argument("report", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--worst-limit", type=int, default=25)
    args = parser.parse_args(argv)

    analysis = analyze(load_dataset(args.dataset), json.loads(args.report.read_text(encoding="utf-8")))
    rendered = render_markdown(analysis, worst_limit=max(1, args.worst_limit))
    print(rendered, end="")
    if args.output:
        args.output.write_text(rendered, encoding="utf-8")
    return 0 if not analysis["error_count"] and not analysis["unknown_case_ids"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

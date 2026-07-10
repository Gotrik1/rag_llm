"""Profiles used to test system prompts in the local chat UI."""

from __future__ import annotations

import re
from pathlib import Path


PROMPT_STORE = Path(".ingestion_cache") / "system_prompts"
CLAUDE_SOURCE = Path.home() / "Downloads" / "Claude-4.1.txt"
CLAUDE_CLEANED_PATH = PROMPT_STORE / "claude-4-1-cleaned.txt"


def _remove_block(text: str, tag: str) -> str:
    return re.sub(rf"\n?<{tag}>.*?</{tag}>\n?", "\n", text, flags=re.DOTALL | re.IGNORECASE)


def _clean_claude_reconstruction(text: str) -> str:
    """Remove product-specific, artifact, reward, and broad copyright policy text."""
    text = re.sub(
        r"\s*The current date is [^.]+\.",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = _remove_block(text, "artifacts_info")
    text = _remove_block(text, "mandatory_copyright_requirements")
    text = _remove_block(text, "election_info")

    # The Core Identity section between its heading and election data is entirely
    # tied to an old product snapshot (date, model names, product catalogue).
    text = re.sub(
        r"\n## Core Identity and Knowledge\n.*?(?=\n## Behavioral Guidelines)",
        "\n## Core Identity\n\nThe assistant is Claude, created by Anthropic.\n",
        text,
        flags=re.DOTALL,
    )

    kept_lines: list[str] = []
    for line in text.splitlines():
        normalized = line.lower()
        if any(
            marker in normalized
            for marker in (
                "copyright",
                "displacive",
                "song lyrics",
                "15 words",
                "20+ word",
                "reward",
                "aranjuez",
                "madrid, es",
                "current date is",
                "tuesday, august 05, 2025",
            )
        ):
            continue
        kept_lines.append(line)

    text = "\n".join(kept_lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text + "\n"


def ensure_claude_cleaned_prompt() -> tuple[str, str]:
    """Create the local cleaned profile from the user-supplied reconstruction once."""
    if CLAUDE_SOURCE.exists():
        PROMPT_STORE.mkdir(parents=True, exist_ok=True)
        source = CLAUDE_SOURCE.read_text(encoding="utf-8")
        CLAUDE_CLEANED_PATH.write_text(_clean_claude_reconstruction(source), encoding="utf-8")

    if CLAUDE_CLEANED_PATH.exists():
        return (
            CLAUDE_CLEANED_PATH.read_text(encoding="utf-8").strip(),
            "Очищенная реконструкция из Claude-4.1.txt.",
        )
    return (
        "You are Claude, an AI assistant created by Anthropic. Respond in Russian.",
        "Исходный файл Claude-4.1.txt не найден; используется короткий резервный вариант.",
    )


def profiles(default_prompt: str) -> list[dict[str, str]]:
    _, claude_description = ensure_claude_cleaned_prompt()
    return [
        {
            "id": "rag-grounded",
            "name": "RAG · текущий",
            "description": "Строгий ответ только по извлечённому контексту.",
            "prompt": default_prompt,
        },
        {
            "id": "claude-4-1-cleaned",
            "name": "Claude 4.1 · очищенный",
            "description": claude_description,
            "prompt": ensure_claude_cleaned_prompt()[0],
        },
    ]


def get_profile(profile_id: str, default_prompt: str) -> dict[str, str]:
    return next((profile for profile in profiles(default_prompt) if profile["id"] == profile_id), profiles(default_prompt)[0])

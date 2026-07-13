"""Profiles used to test system prompts in the local chat UI."""

from __future__ import annotations

import re
from pathlib import Path

from runtime_paths import SYSTEM_PROMPT_STORE


PROMPT_STORE = SYSTEM_PROMPT_STORE
CLAUDE_SOURCE = Path.home() / "Downloads" / "Claude-4.1.txt"
CLAUDE_CLEANED_PATH = PROMPT_STORE / "claude-4-1-cleaned.txt"

MARKDOWN_OUTPUT_RULES = """

Формат ответа:
- Возвращай ответ строго в CommonMark Markdown. Не используй HTML, в том числе теги <br>, <table> или <div>.
- Абзацы отделяй одной пустой строкой. Каждый абзац должен содержать законченную мысль.
- Для перечней используй только корректные Markdown-списки: `- пункт` для маркированного и `1. пункт` для нумерованного.
- Используй заголовки `##` и `###` только когда они действительно помогают разделить длинный ответ. Не начинай каждый абзац с заголовка.
- Таблицы в формате GFM (`|`) используй только для сопоставления нескольких однотипных сущностей. Для коротких ответов и сложных описаний предпочитай списки.
- Не помещай в ячейку таблицы несколько предложений, инструкцию или перечень прав. Если значение требует двух и более пунктов, не создавай таблицу: используй подзаголовок и маркированный список.
- Выделение **полужирным**, `код` и цитаты используй экономно и только по смыслу. Не оставляй незакрытые Markdown-маркеры.
- Не добавляй технические пояснения о Markdown, RAG, контексте, поиске или внутренней проверке.
"""


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


def with_markdown_output_rules(prompt: str) -> str:
    return f"{prompt.rstrip()}{MARKDOWN_OUTPUT_RULES}"


def profiles(default_prompt: str) -> list[dict[str, str]]:
    _, claude_description = ensure_claude_cleaned_prompt()
    grounded_rag_prompt = with_markdown_output_rules(default_prompt)
    expanded_rag_prompt = f"""{grounded_rag_prompt}

Дополнительные требования для развёрнутого ответа:
11. Дай не только краткий вывод, но и объясни ход рассуждения по доступным данным: сначала сформулируй основной ответ, затем раскрой необходимые условия, шаги, зависимости и последствия.
12. Если вопрос касается процедуры, опиши её последовательно и подробно: предварительные условия, каждое действие, важные поля или параметры, проверку результата и финальное сохранение или подтверждение, если они указаны в контексте.
13. Если вопрос касается определения или правила, приведи определение, область применения, связанные ограничения, исключения и пример использования — только если это явно подтверждено контекстом.
14. Если вопрос касается расчёта, покажи все подтверждённые исходные данные, формулу, подстановку и результат. Не выполняй расчёты и не добавляй значения, которых нет в контексте.
15. Используй информативные подзаголовки и списки, когда они улучшают читаемость. Не повторяй один и тот же факт разными словами и не добавляй нерелевантные сведения.
16. Для каждого существенного вывода указывай источник в формате раздела или документа, если такая информация есть в контексте.
"""
    return [
        {
            "id": "rag-grounded",
            "name": "RAG · текущий",
            "description": "Строгий ответ по контексту в формате Markdown.",
            "prompt": grounded_rag_prompt,
        },
        {
            "id": "rag-detailed",
            "name": "RAG · развернутый",
            "description": "Подробный Markdown-ответ, строго ограниченный контекстом.",
            "prompt": expanded_rag_prompt,
        },
        {
            "id": "claude-4-1-cleaned",
            "name": "Claude 4.1 · очищенный",
            "description": claude_description,
            "prompt": with_markdown_output_rules(ensure_claude_cleaned_prompt()[0]),
        },
    ]


def get_profile(profile_id: str, default_prompt: str) -> dict[str, str]:
    return next((profile for profile in profiles(default_prompt) if profile["id"] == profile_id), profiles(default_prompt)[0])

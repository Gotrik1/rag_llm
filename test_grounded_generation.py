from types import SimpleNamespace
from unittest.mock import Mock

from rag_agent import RAGAgent


def test_source_names_do_not_create_facts_or_uncited_source_lists():
    agent = object.__new__(RAGAgent)
    agent._generation_state = SimpleNamespace(from_cache=False)
    agent.llm_model = "test"
    agent.llm = Mock()
    agent.llm.complete.return_value = "Для ответа нужны условия применения категории."
    context = [
        {"file": "Методика расчета стоимости 1-6ЦК v2.docx", "section_path": "5.4", "text": "Здесь представлен только заголовок."},
        {"file": "Постановление 1179.docx", "section_path": "2", "text": "Настоящее постановление вступает в силу."},
    ]
    answer = agent._generate_grounded_answer("Что такое 6 ЦК и как рассчитывается?", context, [])
    assert "Источники, учтённые совместно" not in answer
    assert "Краткая схема" not in answer
    assert "6 ЦК — шестая" not in answer
    prompt = agent.llm.complete.call_args.args[0]
    assert "Название файла само по себе ничего не доказывает" in prompt

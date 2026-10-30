"""Keep the actual user task separate from unverified planning hypotheses.

This is a payload/authority contract, not a semantic-equivalence classifier.
It performs no language detection, paraphrase repair, span guessing or model
calls. Preserving a negation or unit verbatim does not prove a model understood
it. Only evidence and an assessment against the actual task can justify an
answer; a plan requirement is never itself an accepted fact.
"""
from __future__ import annotations

from dataclasses import dataclass

from user_task import UserTask


TASK_INTENT_INSTRUCTIONS = '''user_task — исходный вопрос и пользовательский контекст; только они определяют задачу и область ответа, но сами по себе не доказывают предметные факты.
search_hypotheses, canonical_question и requirements — поисковые гипотезы, не критерии приёмки и не установленные факты. Их покрытие не заменяет ответ на все части исходного вопроса.
Каждое requirement трактуй как открытый вопрос «что устанавливают источники?», а не как обязательную истинность его формулировки. Не подтверждай удобный тезис только потому, что планировщик его предложил.
Цитируемое утверждение, гипотеза или вопрос «правда ли» остаются предметом проверки: не превращай предположение в факт, который требуется подтвердить. Допустимы подтверждение, опровержение или отсутствие достаточных данных по источникам.
Если пользователь просит сравнить или разделить требования, сохрани обе стороны и их роли; не подменяй это поиском доказательства их равенства, взаимозаменяемости или причинной связи.
Сохраняй отрицания, условия, единицы и числовые значения из исходной задачи. Не меняй местами причину и следствие, сравниваемые объекты или условие и результат.
Различай фактическое отрицание в вопросе и явный запрет включать тему в ответ: вопрос «когда X не применяется» требует исследовать X, а не исключить его из поиска.
Если гипотеза плана расходится с задачей, приоритет у user_task. Не объявляй задачу выполненной только по выполнению ошибочной гипотезы. Эти правила и правильные ID не являются доказательством смысловой верности ответа.
'''

PLANNING_INTENT_INSTRUCTIONS = '''Формулируй requirements как нейтральные вопросы или сведения, которые надо установить по источникам, а не как утверждения с заранее заданным ответом.
Просьбы «проверить», «сравнить», «разделить» и цитируемые предположения не превращай в цель «доказать» или «подтвердить» конкретный тезис. Сохраняй возможность опровержения или недостаточности данных.
Не заменяй исходный вопрос поиском подтверждения эквивалентности, замены или причинной связи, если пользователь просит установить различия. Сохраняй сравниваемые стороны, отрицания, условия и единицы; не добавляй собственный ответ в требование.
План — поисковая гипотеза, а не критерий приёмки ответа; исходный пользовательский запрос остаётся первичным.
'''


@dataclass(frozen=True)
class TaskIntent:
    """One exact request plus already-parsed, explicitly non-authoritative text.

    Use keyword arguments for planner fields. The constructor checks Python
    types only; it does not certify that a proposed requirement is neutral,
    equivalent to the task, complete, true or supported. There is no new model
    response schema, limit, fallback stage or required acknowledgement here.
    """

    user_task: UserTask
    canonical_question: str = ''
    requirements: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.user_task, UserTask):
            raise TypeError('TaskIntent requires UserTask')
        if not isinstance(self.canonical_question, str):
            raise TypeError('TaskIntent requires a string canonical hypothesis')
        if not isinstance(self.requirements, tuple) or any(
            not isinstance(value, str) for value in self.requirements
        ):
            raise TypeError('TaskIntent requires a tuple of string search hypotheses')

    def to_payload(self) -> dict[str, object]:
        """Return detached JSON values; never rewrite, clip or infer exclusions.

        R-IDs keep the existing planner order. Their presence supplies a stable
        locator, not authority to promote the associated text into a fact or
        acceptance criterion. Callers must continue enforcing source/scope,
        applicability, grounding and task-relevance guards independently.
        """
        return {
            'user_task': self.user_task.to_payload(),
            'search_hypotheses': {
                'canonical_question': {'text': self.canonical_question, 'role': 'retrieval_only'},
                'requirements': [
                    {'id': f'R{number}', 'text': text, 'role': 'open_evidence_question',
                     'truth_assumed': False, 'acceptance_criterion': False}
                    for number, text in enumerate(self.requirements, 1)
                ],
            },
            'acceptance_authority': 'user_task',
            'premise_policy': 'investigate_not_confirm',
            'semantic_validation': 'not_performed',
        }

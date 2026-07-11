# Фактическая демонстрация покрытия

Материалы демонстрации находятся в `docs/screenshots/`.

## Подтверждённые полезные ответы

1. `docs/screenshots/1.png` — роли Owner/Admin и ответ по модели RBAC.
2. `docs/screenshots/1.png` — права владельца системы.
3. `docs/screenshots/1.png` и `docs/screenshots/2.png` — ограничения роли Admin.
4. `docs/screenshots/2.png` — определение Workspace.
5. `docs/screenshots/2.png` — системные инварианты.

Итого подтверждено: 5 полезных ответов.

## Подтверждённые отказы

1. `docs/screenshots/3.png` — вопрос о пароле root.
2. `docs/screenshots/3.png` и `docs/screenshots/4.png` — вопрос о двигателе космического корабля.
3. `docs/screenshots/3.png` и `docs/screenshots/4.png` — вопрос о зарплате директора.
4. `docs/screenshots/4.png` — вопрос о настройке Kubernetes в другой компании.
5. `docs/screenshots/4.png` — вопрос об отсутствующей сущности вне базы знаний.

Итого подтверждено: 5 отказов.

## Что ещё нужно добавить

- после запуска `evaluate.py` — фактический `logs/evaluation.jsonl`.

Скриншоты являются визуальным подтверждением работы UI. JSONL-лог является машинно-читаемым результатом автоматической оценки и должен быть получен отдельным запуском.

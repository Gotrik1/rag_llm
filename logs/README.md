# Logs

Логи создаются скриптами автоматически и намеренно не содержат заранее придуманных результатов.

```powershell
python .\update_index.py --dry-run
python .\update_index.py
python .\evaluate.py
```

Файлы:

- `index_updates.jsonl` — запуск обновления, количество файлов/чанков и ошибки;
- `evaluation.jsonl` — вопросы, ответы, источники, длительность и статус проверки.


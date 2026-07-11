# Ежедневное обновление индекса

Целевой процесс:

```text
источник документов → обнаружение новых/изменённых файлов → парсинг → чанкинг → эмбеддинги → Qdrant/BM25 → JSONL-лог
```

Для сдачи требуется добавить `update_index.py`, который сравнивает fingerprint файла, повторно индексирует только новые и изменённые документы и пишет минимум `started_at`, `finished_at`, `files_added`, `chunks_added`, `index_size`, `errors`.

Реализация для Windows находится в `scripts/register_update_task.ps1` и `scripts/run_update_index.ps1`:

```powershell
Set-ExecutionPolicy -Scope Process Bypass
.\scripts\register_update_task.ps1 -At "06:00"
```

Планировщик запускает задачу один раз в сутки. При ошибке процесс завершается с ненулевым кодом и оставляет запись в логе; повторный запуск не переиндексирует неизменённые документы благодаря fingerprint-манифесту.

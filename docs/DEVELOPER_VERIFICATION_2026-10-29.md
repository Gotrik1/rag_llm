# Проверка передачи разработчикам — 29.10.2026

Контур: канонический Windows checkout `C:\Users\gotri\Documents\LLM`, `dev`.
Исходные незакоммиченные изменения сохранены. Это локальная проверка;
коммит, push, сборка Docker-образа и выкладка не выполнялись.

## Результаты

| Проверка | Результат | Доказательство |
| --- | --- | --- |
| Полный backend, Python 3.12 / pytest | 4241 passed, 10 skipped, 98 subtests passed, 2 dependency warnings | `pytest-all-runtime.log`, `pytest-all-runtime.xml` |
| Покрытие 62 корневых Python-модулей, строки и ветви вместе | 83.5%; 13469/15768 строк, 5110/6480 ветвей | `coverage-all-runtime.txt`, `coverage-all-runtime.json` |
| UI Vitest | 117 passed, 7 files; включая реальный установленный assistant runtime | `ui-tests.log` |
| TypeScript + Vite production build | passed | `ui-build.log` |
| Rust `cargo test --locked --offline` | 6 passed, включая 3 новые регрессии ключей кеша | `cargo-test-final.log` |
| Swagger/API + ASGI | 36 passed, 64 subtests; входят в полный backend | `test_api_openapi.py`, `test_asgi_foundation.py`, основной JUnit |
| Compose config | `docker compose -f compose.yaml config --quiet`: exit 0 | Локальный терминальный прогон |
| Diff whitespace | `git diff --check`: exit 0 | Локальный терминальный прогон; предупреждения LF/CRLF допустимы |
| Документационные ссылки | README, architecture, API contract, handoff — существующие локальные цели | Локальная проверка относительных ссылок |

Покрытие измеряет тесты на заглушках и локальном коде. БД не поднималась:
`DATABASE_URL` был пустым, поэтому 10 PostgreSQL-тестов пропущены. Это не
процент корректности ответов и не гарантия готовности развёрнутого приложения.
Показатели по компонентам: `application_services` 79.7%, `question_service`
97.8%, `api_openapi` 95.6%, `answer_sources` 100%, `evidence_selection` 95.2%,
`rag_agent` 79.5%. CI-порог 50% не повышался.

Первый полный замер со старой `.coveragerc` давал 70.8% по 23 модулям.
Это неполная область, поэтому число не является baseline для 83.5% по 62
модулям. После финальной вставки docstring `quality_eval.py` его старые
построчные данные удалены из CoverageData и обновлены 11 целевыми тестами:
`quality-coverage-refresh.log`. Остальные данные — полного прогона, а
исполняемый AST этого изменения не менялся.

## Комментарии и структура

- Инвентарь Git-visible исходников: `before.json`, `after-verified.json`.
  Он включает eval/CLI/doc generators, а не только сервер.
- Документированные Python-символы с неподчёркнутым именем: 225/754 → 653/771.
  В знаменателе также вложенные помощники; это не метрика тестового покрытия.
- Все публичные top-level функции/классы и неподчёркнутые методы классов
  62 корневых модулей получили docstring. Вложенные локальные помощники и
  служебные eval/doc scripts не все документированы.
- Doc-only AST receipts: `core-comments.json`, `comments-*.json`,
  `comments-quality-eval.receipt.json`; поведенческие исправления проверены
  отдельно. Дополнительные комментарии есть в UI и всех 8 Rust-модулях.
- Контроллеры ask/stream/upload/delete стали адаптерами прикладных сервисов.
  Legacy settings/provider/debug ещё содержат orchestration; широкий протокол
  `QuestionDependencies` из 42 зависимостей остаётся переходным решением.

## Найденные дефекты, устранённые локально

1. Отсутствующий `score` больше не ломает формирование ответа; неизвестное
   значение — JSON null, без выдуманного нуля. Проверены обычный и corrective
   ответы и отображение Debug.
2. Allowlist Rust/hybrid не обходится пустым excluded set; пустой allowlist
   запрещает все источники, None сохраняет старый неограниченный контракт.
   Проверены UUID загрузки, редакции и оба режима генерации.
3. Устранён подтверждённый цикл обновления thread-list snapshot assistant-ui
   через отдельный совместимый адаптер без изменения зависимости.
4. Rust standalone-кеш сохраняет цифры и пунктуацию; новая версия namespace
   не читает старые записи. HTTP не использует standalone Agent::ask.
5. Swagger расширен до 35 paths / 49 operations: текущие native-маршруты,
   legacy-фасад, async 202, SSE, ошибки, загрузки, auth и CSRF.

## Ограничения и дальнейший долг

- `rag_agent.py` 5862 строки, `evidence_selection.py` 3230, App.tsx 3331;
  полная декомпозиция не выполнена. План — в developer handoff.
- `quality_eval` использует эвристические метрики. CLI release-gate не
  блокирует все диагностические failures; требуется отдельное уточнение
  критериев. Эти числа не подменяют экспертную оценку ответов.
- Rust повторный ingest может дублировать Tantivy-документы. Standalone
  SQLite-кеш не имеет corpus/model/tenant scope и непригоден для общего HTTP.
- UI сохраняет одно предупреждение React о начальном снимке зависимости;
  ошибки/update-depth loop в проверенных сценариях устранены. Предупреждение
  не подавлялось.
- Rust выдаёт 2 dead_code warning. `cargo fmt --check` не проходит и на
  чистом исходном HEAD (см. `cargo-fmt-head-baseline.log`); массовой
  переформатировки не было. `cargo doc` блокируется существующим отказом
  доступа к `ui/.pytest_cache`; права не менялись.
- Docker daemon был недоступен; конфигурация проверена, контейнерная сборка
  и интеграция с моделью/Redis/Qdrant/БД не заявляются.
- На сервере MCP проверен свежий граф
  `rag-assistant-canonical-20261029-handoff-v17`: 5777 nodes / 41443 edges.
  Новые `build_answer_sources` и `QuestionService.answer` присутствуют.
  Локальный экспорт `.codebase-memory/artifact.json` остаётся старым;
  перед передачей файла графа экспорт нужно восстановить и проверить.

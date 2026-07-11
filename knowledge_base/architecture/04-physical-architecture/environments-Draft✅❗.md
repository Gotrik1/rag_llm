# Environments

## Назначение

Описывает различия между окружениями dev / qa / prod: конфигурация, данные, доступы, назначение.

---

## Сводная таблица окружений

| Параметр        | dev                       | qa                       | prod                   |
| --------------- | ------------------------- | ------------------------ | ---------------------- |
| **Серверы**     | mp-sl-dev, mp-pg-dev      | mp-sl-qa, mp-pg-qa       | mp-sl-prod, mp-pg-prod |
| **Ветка**       | `develop`                 | `qa`                     | `main`                 |
| **ENV_TYPE**    | `development`             | `qa`                     | `production`           |
| **База данных** | mp_db_dev                 | mp_db_qa                 | mp_db_prod             |
| **Данные**      | Синтетические             | Синтетические            | Реальные (prod)        |
| **Debug логи**  | Включены                  | Выборочно                | Выключены              |
| **Auto-deploy** | При push                  | Manual trigger           | Manual trigger         |
| **Доступ**      | Команда разработки        | QA + DevOps              | DevOps + CTO           |
| **Домен**       | dev.system10x.btlz-api.ru | qa.system10x.btlz-api.ru | system10x.btlz-api.ru  |

---

## ENV переменные по окружениям

### Общая структура (префиксы)

| Префикс    | Назначение            | Пример                    |
| ---------- | --------------------- | ------------------------- |
| `DB_`      | База данных           | `DB_HOST`, `DB_PORT`      |
| `CONFIG_`  | Конфигурация сервисов | `CONFIG_MAX_CONNECTIONS`  |
| `LOG_`     | Уровни логирования    | `LOG_LEVEL`, `LOG_FORMAT` |
| `FEATURE_` | Feature flags         | `FEATURE_NEW_UI=true`     |

### dev

```env
ENV_TYPE=development
LOG_LEVEL=debug
LOG_FORMAT=pretty
DB_HOST=mp-pg-dev
DB_NAME=mp_db_dev
FEATURE_DEBUG_PANEL=true
```

### qa

```env
ENV_TYPE=qa
LOG_LEVEL=info
LOG_FORMAT=json
DB_HOST=mp-pg-qa
DB_NAME=mp_db_qa
FEATURE_DEBUG_PANEL=false
```

### prod

```env
ENV_TYPE=production
LOG_LEVEL=warn
LOG_FORMAT=json
DB_HOST=mp-pg-prod
DB_NAME=mp_db_prod
FEATURE_DEBUG_PANEL=false
```

---

## CI/CD триггеры по окружениям

| Окружение | Триггер                             | Ветка     |
| --------- | ----------------------------------- | --------- |
| dev       | Автоматически при merge в `develop` | `develop` |
| qa        | Ручной запуск (`when: manual`)      | `qa`      |
| prod      | Ручной запуск + подтверждение       | `main`    |

---

## Изоляция данных

- **Dev и QA** — полностью изолированы (разные серверы БД, разные Docker networks)
- **QA** не имеет доступа к dev-серверам и наоборот
- **Prod** — отдельная инфраструктура, доступ только через CI/CD pipeline

---

## Связанные документы

- `mp-devops/devops-docs/ENVIRONMENT-CONFIG-STRATEGY.md` — детальная стратегия
- `mp-devops/planning/stage1/TASK-1.4.1-QA-DEV-ENVIRONMENT.md` — QA окружение
- `mp-devops/planning/stage1/TASK-1.5-SYSTEM10X-PROD-INFRASTRUCTURE.md` — Prod инфраструктура

---

Статус: Draft | Обновлено: 2026-01-26_

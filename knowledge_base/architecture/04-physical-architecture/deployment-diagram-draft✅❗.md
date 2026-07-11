# Deployment Diagram

## Назначение

Описывает, где физически и логически работает система: серверы, контейнеры, сервисы и связи между ними.

---

## Текущая конфигурация серверов

| Сервер     | Роль            | Компоненты                          |
|------------|-----------------|-------------------------------------|
| mp-sl-dev  | Dev-стенд (SL)  | backend, frontend, NATS, PostgreSQL |
| mp-pg-dev  | Dev PostgreSQL  | PostgreSQL (основной)               |
| mp-sl-qa   | QA-стенд (SL)   | backend, frontend, NATS             |
| mp-pg-qa   | QA PostgreSQL   | PostgreSQL (QA)                     |
| mp-sl-prod | Production      | backend, frontend, NATS             |
| mp-pg-prod | Prod PostgreSQL | PostgreSQL (prod)                   |

---

## Диаграмма развёртывания (C4 Level 3)

```markdown
┌─────────────────────────────────────────────────────┐
│                   PRODUCTION                        │
│                                                     │
│  ┌──────────────────┐    ┌─────────────────────┐    │
│  │   mp-sl-prod     │    │   mp-pg-prod        │    │
│  │                  │    │                     │    │
│  │  ┌────────────┐  │    │  ┌───────────────┐  │    │
│  │  │  Nginx     │  │    │  │  PostgreSQL   │  │    │
│  │  └─────┬──────┘  │    │  │  + PgBouncer  │  │    │
│  │        │         │    │  └───────────────┘  │    │
│  │  ┌─────▼──────┐  │    └─────────────────────┘    │
│  │  │  Frontend  │  │                               │
│  │  │ (Next.js)  │  │                               │
│  │  └────────────┘  │                               │
│  │  ┌────────────┐  │                               │
│  │  │  Backend   │◄─┼────────────────────────────►  │
│  │  │ (Node.js)  │  │        PostgreSQL             │
│  │  └────────────┘  │                               │
│  │  ┌────────────┐  │                               │
│  │  │   NATS     │  │                               │
│  │  └────────────┘  │                               │
│  └──────────────────┘                               │
└─────────────────────────────────────────────────────┘
```

---

## Docker Compose профили

| Профиль | Назначение                        |
|---------|-----------------------------------|
| `dev`   | Разработка (локально + dev-стенд) |
| `qa`    | QA-тестирование (отдельный стенд) |
| `prod`  | Продакшн                          |

---

## Порты сервисов

| Сервис             | Порт   | Протокол   |
|--------------------|--------|------------|
| Frontend (Next.js) | 3000   | HTTP       |
| Backend API        | 5000   | HTTP       |
| PostgreSQL         | 5432   | TCP        |
| PgBouncer          | 6432   | TCP        |
| NATS               | 4222   | TCP        |
| Nginx              | 80/443 | HTTP/HTTPS |

---

## Связанные документы

- `04-physical-architecture/network-topology.md` — сети и зоны
- `04-physical-architecture/environments.md` — различия dev/qa/prod
- `mp-devops/planning/stage1/TASK-1.5-SYSTEM10X-PROD-INFRASTRUCTURE.md` — план prod инфраструктуры

---

Статус: Draft | Обновлено: 2026-02-26

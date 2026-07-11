# Data Dictionary — Единые определения полей

## Назначение

Единый справочник определений полей, типов данных и бизнес-смысла для всех доменов системы. Является источником правды при расхождениях между PostgreSQL, ClickHouse и API-контрактами.

---

## Соглашения по типам

### PostgreSQL → ClickHouse маппинг

| PostgreSQL                    | ClickHouse                       | Примечание                   |
| ----------------------------- | -------------------------------- | ---------------------------- |
| `UUID`                        | `Int32` (через справочник)       | `sid` маппится через словарь |
| `NUMERIC(P,S)`                | `Decimal(P,S)`                   | Сохранять точность P и S     |
| `VARCHAR(N)` / `TEXT`         | `String`                         |                              |
| `TEXT` с ограниченным набором | `LowCardinality(String)`         | До ~10к уникальных значений  |
| `BOOLEAN`                     | `UInt8`                          | 0/1                          |
| `INTEGER`                     | `Int32`                          |                              |
| `BIGINT`                      | `Int64`                          |                              |
| `TIMESTAMP`                   | `DateTime`                       |                              |
| `DATE`                        | `Date`                           |                              |
| `NULL` допускается            | `Nullable(T)` или `DEFAULT 0/''` | Предпочтительно DEFAULT      |

---

## Домен: FinReport (wb_reports)

### Служебные поля

| Поле        | Тип PG           | Тип CH                 | Описание                           |
| ----------- | ---------------- | ---------------------- | ---------------------------------- |
| `sid`       | UUID (seller_id) | Int32                  | ID продавца, маппинг через словарь |
| `loaded_at` | —                | DateTime DEFAULT now() | Время загрузки в ClickHouse        |

### Идентификаторы

| Поле     | Тип PG  | Тип CH | Описание                                   |
| -------- | ------- | ------ | ------------------------------------------ |
| `rrd_id` | BIGINT  | Int32  | Уникальный ID строки отчёта                |
| `nm_id`  | INTEGER | Int32  | Артикул WB (номенклатура)                  |
| `doc_id` | BIGINT  | Int32  | ID документа                               |
| `rr_dt`  | DATE    | Date   | Дата строки отчёта (для партиционирования) |

### Типы и категории

| Поле                 | Тип PG         | Тип CH                               | Описание                              |
| -------------------- | -------------- | ------------------------------------ | ------------------------------------- |
| `doc_type_name`      | VARCHAR        | LowCardinality(String)               | Тип документа (продажа, возврат, ...) |
| `supplier_oper_name` | VARCHAR        | LowCardinality(String)               | Тип операции поставщика               |
| `bonus_type_name`    | VARCHAR        | LowCardinality(String)               | Тип бонуса                            |
| `report_type`        | VARCHAR → Enum | Enum8('standard'=1,'notification'=2) | Тип отчёта                            |

### Финансовые поля

| Поле                 | Тип PG        | Тип CH        | DEFAULT | Описание             |
| -------------------- | ------------- | ------------- | ------- | -------------------- |
| `retail_price`       | NUMERIC(15,2) | Decimal(15,2) | 0       | Розничная цена       |
| `retail_amount`      | NUMERIC(15,2) | Decimal(15,2) | 0       | Сумма продаж         |
| `sale_percent`       | NUMERIC(5,2)  | Decimal(5,2)  | 0       | Процент скидки       |
| `commission_percent` | NUMERIC(5,2)  | Decimal(5,2)  | 0       | Процент комиссии WB  |
| `delivery_rub`       | NUMERIC(15,2) | Decimal(15,2) | 0       | Стоимость доставки   |
| `ppvz_for_pay`       | NUMERIC(15,2) | Decimal(15,2) | 0       | К выплате поставщику |

---

## Домен: Platform (пользователи, тенанты)

| Поле         | Тип       | Описание                                    |
| ------------ | --------- | ------------------------------------------- |
| `tenant_id`  | UUID      | ID тенанта (организации)                    |
| `user_id`    | UUID      | ID пользователя                             |
| `role`       | ENUM      | Роль: `owner`, `admin`, `manager`, `viewer` |
| `created_at` | TIMESTAMP | Время создания записи                       |
| `updated_at` | TIMESTAMP | Время последнего обновления_                |

---

## Правила именования полей

| Правило                                  | Пример                                |
| ---------------------------------------- | ------------------------------------- |
| snake_case                               | `retail_amount`, `doc_type_name`      |
| Суффикс `_id` для идентификаторов        | `nm_id`, `doc_id`, `rrd_id`           |
| Суффикс `_dt` или `_at` для дат          | `rr_dt`, `created_at`                 |
| Суффикс `_rub` для сумм в рублях         | `delivery_rub`, `ppvz_for_pay`        |
| Суффикс `_percent` для процентов         | `sale_percent`, `commission_percent`  |
| Суффикс `_name` для LowCardinality строк | `doc_type_name`, `supplier_oper_name` |

---

## Связанные документы

- `wb-plus-back/clickhouse-poc/docs/ARCHITECTURAL-DECISIONS.md` — утверждённые решения CH
- `wb-plus-back/clickhouse-poc/ddl/002_create_wb_reports_daily_ch.sql` — DDL таблицы
- `10-requirements-and-research/traceability-matrix.md` — матрица прослеживаемости

---

Статус: Draft | Обновлено: 2026-02-26

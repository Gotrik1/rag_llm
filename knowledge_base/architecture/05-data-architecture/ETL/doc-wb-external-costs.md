# WB Внешние расходы (wb-external-costs) — техническое описание

> Статус: **реализовано**

---

## Цепочка: UI → API → сервисы → источники → агрегация → ответ

```
sw-front (Next.js)
  │  GET    /api/v1/wb-external-costs?workspaceId=X[&nmId=&source=&dateFrom=&dateTo=&page=&limit=]
  │  POST   /api/v1/wb-external-costs?workspaceId=X  {тело расхода}
  │  PATCH  /api/v1/wb-external-costs/:id?workspaceId=X
  │  DELETE /api/v1/wb-external-costs/:id?workspaceId=X
  ▼
sw-back (NestJS)  src/wb-external-costs/
  │  WbExternalCostsController → WbExternalCostsService → WbExternalCostsProxyClient
  │  NATS → sl-back
  ▼
sl-back (Express)  src/main/internalApi/controllers/wbExternalCosts.internal.controller.js
  │  wbExternalCostsUserInputService
  ▼
PostgreSQL (client-scoped DB)
  │  wb_external_costs_user_input  (user input)
  │  wb_external_costs             (дневная развёртка)
  │  wb_cards                      (обогащение vendor_code, title, basket)
  ▼
Агрегация: syncWithUserInput() — развёртка периода по дням, cost_per_day
  ▼
Ответ на фронт (camelCase)
```

---

## 1. UI → sw-back

**Страница фронта:** `sw-front/src/app/external-costs/[id]/page.tsx`
**API клиент фронта:** `sw-front/src/app/external-costs/[id]/api/externalCosts.api.ts`

### 1.1 GET — список внешних расходов

```
GET /api/v1/wb-external-costs?workspaceId={workspaceId}[&nmId=&source=&dateFrom=&dateTo=&page=&limit=]
Authorization: Bearer {jwt}
```

| Параметр | Тип | Обязательный | Описание |
|---|---|---|---|
| `workspaceId` | number | да | ID воркспейса |
| `nmId` | number | нет | Фильтр по WB артикулу |
| `source` | string | нет | Фильтр по источнику (напр. "Instagram") |
| `dateFrom` | string (date) | нет | Фильтр по дате начала (`>=`) |
| `dateTo` | string (date) | нет | Фильтр по дате начала (`<=`) |
| `page` | number | нет | Страница (default: 1) |
| `limit` | number (1–200) | нет | Размер страницы (default: 50) |

**Ответ:**
```json
{
  "items": [ /* массив WbExternalCostsItemResponseDto */ ],
  "total": 128,
  "page": 1,
  "limit": 50
}
```

---

### 1.2 POST — создать расход

```
POST /api/v1/wb-external-costs?workspaceId={workspaceId}
Authorization: Bearer {jwt}
Content-Type: application/json
```

```json
{
  "nmId": 512033057,
  "source": "Instagram",
  "dateIntegration": "2025-03-01",
  "dateEnd": "2025-03-31",
  "costs": 15000,
  "comment": "Реклама у блогера @example"
}
```

| Поле | Тип | Обязательный | Описание |
|---|---|---|---|
| `nmId` | number (int, >0) | **да** | WB артикул |
| `source` | string | **да** | Источник трафика |
| `dateIntegration` | string (date) | **да** | Дата начала периода |
| `dateEnd` | string (date) | **да** | Дата конца периода |
| `costs` | number (>0) | **да** | Общая сумма расходов за весь период |
| `comment` | string | нет | Комментарий |

**Валидация в sw-back:**
- `dateEnd >= dateIntegration`
- Интервал ≤ 366 дней
- `costs > 0`

**Ответ:** `{ "item": WbExternalCostsItemResponseDto }`

---

### 1.3 PATCH — обновить расход

```
PATCH /api/v1/wb-external-costs/{id}?workspaceId={workspaceId}
Authorization: Bearer {jwt}
Content-Type: application/json
```

Тело — любые поля из create (все optional): `source`, `dateIntegration`, `dateEnd`, `costs`, `comment`.

**Ответ:** `{ "item": WbExternalCostsItemResponseDto }`

---

### 1.4 DELETE — удалить расход

```
DELETE /api/v1/wb-external-costs/{id}?workspaceId={workspaceId}
Authorization: Bearer {jwt}
```

**Ответ:** `{ "success": true }`

---

### 1.5 Структура WbExternalCostsItemResponseDto

```typescript
{
  id: string;                // UUID
  nmId: number;
  source: string;
  dateIntegration: string;   // YYYY-MM-DD
  dateEnd: string | null;    // YYYY-MM-DD
  costs: number;             // общая сумма за период
  comment: string | null;
  vendorCode: string | null; // из wb_cards
  title: string | null;      // из wb_cards
  photoUrl: string | null;   // basket URL из wb_cards
}
```

---

## 2. sw-back

**Директория:** `src/wb-external-costs/`

```
wb-external-costs.module.ts
wb-external-costs.controller.ts      — HTTP endpoints, JWT + workspace guard
wb-external-costs.service.ts         — валидация дат/суммы, маппинг camelCase↔snake_case
wb-external-costs.proxy.client.ts    — SlProxyClient, NATS-запросы в sl-back
dto/
  request/
    find-wb-external-costs.query.dto.ts     — workspaceId, nmId?, source?, dateFrom?, dateTo?, page?, limit?
    create-wb-external-costs.request.dto.ts — nmId, source, dateIntegration, dateEnd, costs, comment?
    update-wb-external-costs.request.dto.ts — все поля optional
  response/
    find-wb-external-costs.response.dto.ts
    wb-external-costs-item.response.dto.ts
    wb-external-costs-item-wrapper.response.dto.ts
    delete-wb-external-costs.response.dto.ts
  sl/
    find-wb-external-costs.sl.dto.ts
    wb-external-costs.sl.dto.ts             — snake_case поля от sl-back
    wb-external-costs-sl-requests.dto.ts    — SlWbExternalCostsFind/Create/Update/Delete Request
```

**NATS ключи** (`src/nats/types.ts`):
```typescript
WB_EXTERNAL_COSTS_FIND   = 'wbExternalCostsUserInputService.find'
WB_EXTERNAL_COSTS_CREATE = 'wbExternalCostsUserInputService.create'
WB_EXTERNAL_COSTS_UPDATE = 'wbExternalCostsUserInputService.update'
WB_EXTERNAL_COSTS_DELETE = 'wbExternalCostsUserInputService.delete'
```

**Валидация в сервисе** (до отправки в sl-back):
```typescript
// validateDateRange(dateIntegration, dateEnd)
// - end >= start
// - разница ≤ 366 дней

// validateCosts(costs)
// - costs > 0
```

---

## 3. sl-back

### 3.1 Контроллер

**Файл:** `src/main/internalApi/controllers/wbExternalCosts.internal.controller.js`
**Маршруты** (все с middleware `requireClientId`):

```
GET    /v1/external-costs           → wbExternalCostsUserInputService.find
POST   /v1/external-costs           → wbExternalCostsUserInputService.create
PATCH  /v1/external-costs/:id       → wbExternalCostsUserInputService.update
DELETE /v1/external-costs/:id       → wbExternalCostsUserInputService.delete
```

**Zod-схемы:**
- `findQuerySchema` — nm_id, source, date_from, date_to, page, limit (все optional)
- `createSchema` — nm_id, source, date_integration, date_end, costs (required), comment
- `updateSchema` — все поля optional
- `idParamSchema` — id (UUID)

**Envelope ответа:** `{ result: { data: T | null, error: { code, message } | null } }`

### 3.2 Сервис — find

**Файл:** `src/wb/wbExternalCostsUserInput/wbExternalCostsUserInput.service.js`

```javascript
static async find({ client_id, nm_id?, source?, date_from?, date_to?, page, limit })
```

Делегирует в модель `wbExternalCostsUserInputModel.findMany()`.

### 3.3 Сервис — create

```javascript
static async create({ client_id, nm_id, source, date_integration, date_end, costs, comment? })
```

1. Дополнительная валидация дат (на уровне сервиса)
2. Валидация `costs > 0`
3. `INSERT INTO wb_external_costs_user_input`
4. **Fire-and-forget**: `wbExternalCostsModel.syncWithUserInput({ client_id })`
   - Не блокирует ответ
   - Ошибки логируются, не возвращаются клиенту
5. Возвращает `{ item }` — запись из `wb_external_costs_user_input`

### 3.4 Сервис — update

1. `SELECT by id` — проверить существование (`NOT_FOUND`)
2. Merging: для валидации дат берёт `date_integration ?? existing.date_integration`
3. Валидация дат + costs
4. `UPDATE wb_external_costs_user_input WHERE id = $id`
5. **Fire-and-forget**: `syncWithUserInput({ client_id })`
6. Возвращает `{ item }`

### 3.5 Сервис — delete

1. `SELECT by id` — проверить существование (`NOT_FOUND`)
2. `DELETE FROM wb_external_costs_user_input WHERE id = $id`
3. **Fire-and-forget**: `syncWithUserInput({ client_id })`
4. Возвращает `{ success: true }`

---

## 4. Источники данных

### Таблицы (client-scoped PostgreSQL)

| Таблица | Назначение | Ключевые поля |
|---|---|---|
| `wb_external_costs_user_input` | Пользовательский ввод (запись с периодом) | id (UUID), nm_id, source, date_integration, date_end, costs, comment, spreadsheet_id |
| `wb_external_costs` | Дневная развёртка расходов (используется в unit экономике) | date, nm_id, source (PK), cost_per_day |
| `wb_cards` | Метаданные товаров | nm_id, vendor_code, title, basket |

### Что откуда берётся при чтении (GET)

**Модель:** `src/wb/wbExternalCostsUserInput/wbExternalCostsUserInput.model.js` — `findMany()`

```sql
SELECT
    wb_external_costs_user_input.*,
    wc.vendor_code,
    wc.title,
    wc.basket
FROM wb_external_costs_user_input
LEFT JOIN (
    SELECT DISTINCT ON (nm_id) nm_id, vendor_code, title, basket
    FROM wb_cards
    ORDER BY nm_id
) wc ON wb_external_costs_user_input.nm_id = wc.nm_id
[WHERE nm_id = $nmId]
[AND source = $source]
[AND date_integration >= $dateFrom]
[AND date_integration <= $dateTo]
ORDER BY date_integration DESC
LIMIT $limit OFFSET $offset;
```

Данные `wb_cards` обогащают запись: `vendor_code`, `title`, `basket` (→ `photoUrl`).

Внешние API WB **не вызываются** в runtime — все данные хранятся в PostgreSQL.

---

## 5. Агрегация — syncWithUserInput()

**Файл:** `src/wb/wbExternalCosts/wbExternalCosts.model.js`

Запускается fire-and-forget после каждой мутации. Синхронизирует `wb_external_costs_user_input` → `wb_external_costs`.

**Алгоритм:**
1. Берёт все активные записи из `wb_external_costs_user_input`
2. Для каждой записи разворачивает период по дням (`generate_series`)
3. Считает `cost_per_day = ROUND(costs / (date_end - date_integration + 1), 2)`
4. Агрегирует несколько источников на один (date, nm_id): `STRING_AGG(source)`, `SUM(cost_per_day)`
5. Синхронизирует `wb_external_costs` через INSERT / ON CONFLICT UPDATE / DELETE

```sql
WITH b AS (
    SELECT
        generate_series(ec.date_integration, ec.date_end, '1 day'::interval)::DATE AS date,
        nm_id,
        source,
        COALESCE(ROUND(ec.costs / (ec.date_end - ec.date_integration + 1), 2), 0) AS cost_per_day
    FROM wb_external_costs_user_input ec
    WHERE date_integration IS NOT NULL AND date_end IS NOT NULL
      AND date_integration <= date_end
      AND costs > 0
),
result AS (
    SELECT
        date, nm_id,
        STRING_AGG(DISTINCT source, ', ') AS source,
        ROUND(SUM(cost_per_day), 2) AS cost_per_day
    FROM b GROUP BY date, nm_id
),
-- INSERT новых, UPDATE изменённых, DELETE удалённых
...
```

**Таблица `wb_external_costs`** используется в **unit экономике** (датасеты `wb10xUnit*`) — именно оттуда берётся `average_external_costs` при расчёте юнит-метрик.

---

## 6. NATS proxy (sl-back)

```javascript
export const wbExternalCostsUserInputServiceProxyController = new NatsProxyController({
    target: wbExternalCostsUserInputService,
    methods: [
        { name: "find",   timeout: 10000 },
        { name: "create", timeout: 10000 },
        { name: "update", timeout: 10000 },
        { name: "delete", timeout: 10000 },
    ],
    singletone: true,
});
```

---

## 7. Коды ошибок

| Код | Условие |
|---|---|
| `NOT_FOUND` | Запись не найдена по id (update/delete) |
| `VALIDATION_ERROR` | Некорректный формат дат, date_end < date_integration, интервал > 366 дней, costs <= 0 |
| `INTERNAL_ERROR` | DB ошибки |

---

## 8. Downstream потребители — где используется wb_external_costs

После `syncWithUserInput()` таблица `wb_external_costs` (дневная, PK: date+nm_id) читается несколькими датасетами.

### 8.1 Чеклист — `wb10xChecklistByDaysAndNmIds_v1`

**Файл:** `src/datasets/wb/wb10xChecklistByDaysAndNmIds_v1/wb10xChecklistByDaysAndNmIds_v1.sql`

```sql
-- CTE: wb_external_costs_b
LEFT JOIN wb_external_costs ec ON c.date = ec.date AND c.nm_id = ec.nm_id
```

Участвует в расчёте:
- `total_adv_costs = adv_sum + promo_total_cost + external_costs`
- `expected_cost_sum_rub` — общие ожидаемые расходы
- `profit_with_adv = expected_buyout_sum - expected_cost_sum - total_adv_costs`
- `marg_with_adv = profit_with_adv / expected_buyout_sum`

Точная формула в SQL:
```sql
-- external_costs входит в adv_sum и выводится отдельно
ext.cost_per_day                                              AS external_costs
ext.source                                                    AS external_sources  -- STRING_AGG источников
COALESCE(adv_sum, 0) + promo_total_cost + ext.cost_per_day   AS total_adv_costs
```

Выходные колонки чеклиста: `external_costs`, `external_sources`, `total_adv_costs`, `profit_with_adv`, `marg_with_adv`.

**Фронтенд чеклиста (`sw-front/src/app/checklist/[id]/page.tsx`) — страница существует, но в разработке** (placeholder, TODO-комментарий, реальной таблицы нет). Колонки `external_costs` и `total_adv_costs` в UI пока не отображаются.

### 8.2 Unit экономика — `wb10xUnitByDaysAndNmIdsAvgValues_v1`

**Файл:** `src/datasets/wb/wb10xUnitByDaysAndNmIdsAvgValues_v1/SQL/wb10xUnitByDaysAndNmIdsAvgValues_v1.prepare_data.sql`

```sql
-- CTE: wb_external_costs_b
-- 30-дневное скользящее окно
SELECT AVG(cost_per_day) AS average_external_costs
FROM wb_external_costs
WHERE nm_id = $nm_id AND date BETWEEN now()-30 AND now()
```

Выходное поле: `average_external_costs` — среднесуточные внешние расходы на единицу.

Также используется в `src/wb/wbUnitCalculatedData/SQL/wbUnitCalculatedData.sql` — поля `sum_external_costs`, `average_external_costs`, `cost_per_day`.

### 8.3 Финансовый отчёт продаж — `wb10xSalesFinReportDaily_v1`

**Файл:** `src/datasets/wb/wb10xSalesFinReportDaily_v1/wb10xSalesFinReportDaily_v1.sql`

JOIN по `(date, nm_id)`. Выходные колонки: `external_costs` — включается в расчёт операционных расходов и итоговой прибыли.

### 8.4 Месячное планирование — `wb10xMain_planMonth_v2`

Чеклистные данные (включая `external_costs`) агрегируются в план месяца. Фронт: страница `/planning/month/[id]`, блок `fact-data` — колонка `ordersExtPercFact`.

### 8.5 Сезонный план — `wbSeasonPlan_v1_sync`

Поддерживает отдельное поле `external_costs_manual` — ручной ввод внешних расходов на неделю (через Google Sheets). Это независимый ввод, не связанный с `wb_external_costs_user_input`.

### Итоговая схема влияния

```
wb_external_costs_user_input (CRUD)
  └─ syncWithUserInput() [fire-and-forget]
       └─ wb_external_costs (date + nm_id)
            ├─ wb10xChecklistByDaysAndNmIds_v1
            │    → total_adv_costs, profit_with_adv, marg_with_adv
            │    → [фронт: чеклист / таблица товаров]
            ├─ wb10xUnitByDaysAndNmIdsAvgValues_v1
            │    → average_external_costs
            │    → [фронт: unit экономика]
            ├─ wb10xSalesFinReportDaily_v1
            │    → external_costs в отчёте продаж
            └─ wb10xMain_planMonth_v2 (через чеклист)
                 → [фронт: планирование месяца]
```

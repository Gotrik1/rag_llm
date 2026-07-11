# WB Раздачи (wb-promotion) — техническое описание

> Статус: **реализовано**
> Файл плана: `wb-promotions-web-migration.md`

---

## Цепочка: UI → API → сервисы → источники → агрегация → ответ

```markdowm
sw-front (Next.js)
  │  GET    /api/v1/wb-promotion?workspaceId=X[&sid=&page=&limit=]
  │  POST   /api/v1/wb-promotion?workspaceId=X  {тело раздачи}
  │  PATCH  /api/v1/wb-promotion/:id?workspaceId=X
  │  DELETE /api/v1/wb-promotion/:id?workspaceId=X
  ▼
10X-backend (NestJS)  src/wb-promotion/
  │  WbPromotionController → WbPromotionService → WbPromotionProxyClient
  │  NATS → sl-back
  ▼
sl-back (Express)  src/main/internalApi/controllers/wbPromotions.internal.controller.js
  │  WbPromotionsUserInputService
  ▼
PostgreSQL (client-scoped DB)
  │  ss_wb10x_main_promotions_v3  (user input)
  │  ds_wb10x_promotions_v3       (compute dataset)
  │  wb_sales                     (lookup по srid)
  │  wb_cards                     (обогащение title/brand/subject_name)
  ▼
Агрегация: wb10xPromotions_v3.prepareData() — DELETE+INSERT в ds таблицу
  ▼
Ответ на фронт (camelCase)
```

---

## 1. UI → 10X-backend

**Страница фронта:** `sw-front/src/app/promotions/[id]/page.tsx`

### 1.1 GET — список раздач

```
GET /api/v1/wb-promotion?workspaceId={workspaceId}[&sid={uuid}&page={n}&limit={n}]
Authorization: Bearer {jwt}
```

| Параметр | Тип | Обязательный | Описание |
|---|---|---|---|
| `workspaceId` | number | да | ID воркспейса |
| `sid` | string (uuid) | нет | Фильтр по WB кабинету |
| `page` | number | нет | Страница (default: 1) |
| `limit` | number | нет | Размер страницы (default: 50, max: 200) |

**Ответ:**
```json
{
  "items": [ /* массив WbPromotionItemResponseDto */ ],
  "total": 243,
  "page": 1,
  "limit": 50
}
```

---

### 1.2 POST — создать раздачу

```
POST /api/v1/wb-promotion?workspaceId={workspaceId}
Authorization: Bearer {jwt}
Content-Type: application/json
```

**Два режима создания:**

**Режим 1: через srid** (привязка к реальной продаже)
```json
{
  "sid": "uuid-wb-cabinet",
  "srid": "S12345678",
  "promosType": 1,
  "promoProductCost": 500,
  "promoBuyoutCost": 200,
  "comment": "Блогер @example",
  "contractor": "ООО Пример",
  "review": true,
  "cashback": false
}
```

**Режим 2: вручную** (без srid, обязательны date/nmId/count/priceWithSpp)
```json
{
  "sid": "uuid-wb-cabinet",
  "date": "2025-03-15",
  "nmId": 512033057,
  "count": 3,
  "priceWithSpp": 980.50,
  "promosType": 2,
  "promoProductCost": 450,
  "promoBuyoutCost": 150,
  "comment": "Ручная запись"
}
```

| Поле | Тип | Обязательность | Описание |
|---|---|---|---|
| `sid` | string (uuid) | **обязательный** | ID WB кабинета |
| `srid` | string | если не ручной | ID продажи из WB |
| `date` | string (date) | если нет srid | Дата раздачи |
| `nmId` | number | если нет srid | WB артикул |
| `count` | number | если нет srid | Количество |
| `priceWithSpp` | number | если нет srid | Цена со СПП |
| `promosType` | number (int) | **обязательный** | Тип промо |
| `promoProductCost` | number (≥0) | **обязательный** | Стоимость товара для раздачи |
| `promoBuyoutCost` | number (≥0) | **обязательный** | Стоимость выкупа |
| `comment` | string (≤500) | нет | Комментарий |
| `contractor` | string (≤255) | нет | Контрагент |
| `review` | boolean | нет | Наличие отзыва |
| `cashback` | boolean | нет | Кэшбэк |

**Ответ:** `{ "item": WbPromotionItemResponseDto }`

---

### 1.3 PATCH — обновить раздачу

```
PATCH /api/v1/wb-promotion/{id}?workspaceId={workspaceId}
Authorization: Bearer {jwt}
Content-Type: application/json
```

Тело — любые поля из create (все optional). Ограничение: поля `date, nmId, count, priceWithSpp` **недоступны для записей с srid**.

**Ответ:** `{ "item": WbPromotionItemResponseDto }`

---

### 1.4 DELETE — удалить раздачу

```
DELETE /api/v1/wb-promotion/{id}?workspaceId={workspaceId}
Authorization: Bearer {jwt}
```

**Ответ:** `{ "success": true }`

---

### 1.5 Структура WbPromotionItemResponseDto (ответ фронту)

```typescript
{
  id: string;               // UUID
  sid: string | null;       // UUID WB кабинета
  srid: string | null;      // ID продажи WB
  barcode: string | null;
  vendorCode: string | null;
  title: string | null;     // из wb_cards
  brand: string | null;     // из wb_cards
  subjectName: string | null; // из wb_cards
  date: string | null;      // дата раздачи
  nmId: number | null;
  count: number | null;
  priceWithSpp: number | null;
  promosType: number | null;
  promoProductCost: number | null;
  promoBuyoutCost: number | null;
  promosCosts: number | null;    // promoProductCost + promoBuyoutCost
  promoTotalCost: number | null; // рассчитан датасетом
  comment: string | null;
  contractor: string | null;
  review: boolean | null;
  cashback: boolean | null;
  isReturned: boolean | null;    // из wb_sales/расчёт датасета
  spp: number | null;            // % СПП
  priceWithoutSpp: number | null;
  sum: number | null;            // выручка
  updatedAt: string;
}
```

---

## 2. 10X-backend

**Директория:** `src/wb-promotion/`

```
wb-promotion.module.ts
wb-promotion.controller.ts      — HTTP endpoints, JWT + workspace guard
wb-promotion.service.ts         — маппинг camelCase ↔ snake_case, handleResponse
wb-promotion.proxy.client.ts    — SlProxyClient, NATS-запросы в sl-back
dto/
  request/
    find-wb-promotion.query.dto.ts     — workspaceId, sid?, page?, limit?
    create-wb-promotion.request.dto.ts — все поля создания
    update-wb-promotion.request.dto.ts — все поля обновления (optional)
  response/
    find-wb-promotion.response.dto.ts
    wb-promotion-item.response.dto.ts
    wb-promotion-item-wrapper.response.dto.ts
    delete-wb-promotion.response.dto.ts
  sl/
    find-wb-promotion.sl.dto.ts
    wb-promotion.sl.dto.ts             — snake_case поля от sl-back
    wb-promotion-sl-requests.dto.ts    — SlWbPromotionFind/Create/Update/Delete Request
```

**NATS ключи** (`src/nats/types.ts`):
```typescript
WB_PROMOTIONS_FIND   = 'WbPromotionsUserInputService.find'
WB_PROMOTIONS_CREATE = 'WbPromotionsUserInputService.create'
WB_PROMOTIONS_UPDATE = 'WbPromotionsUserInputService.update'
WB_PROMOTIONS_DELETE = 'WbPromotionsUserInputService.delete'
```

**HTTP proxy map** (`src/slProxy/http-proxy-transport.service.ts`):
```
FIND   → GET    /v1/wb-promotions
CREATE → POST   /v1/wb-promotions
UPDATE → PATCH  /v1/wb-promotions/:id
DELETE → DELETE /v1/wb-promotions/:id
```

---

## 3. sl-back

### 3.1 Контроллер

**Файл:** `src/main/internalApi/controllers/wbPromotions.internal.controller.js`
**Маршруты** (все с middleware `requireClientId`):

```
GET    /v1/wb-promotions           → WbPromotionsUserInputService.find
POST   /v1/wb-promotions           → WbPromotionsUserInputService.create
PATCH  /v1/wb-promotions/:id       → WbPromotionsUserInputService.update
DELETE /v1/wb-promotions/:id       → WbPromotionsUserInputService.delete
```

**Envelope ответа:** `{ result: { data: T | null, error: { code, message } | null } }`

### 3.2 Сервис — find

**Файл:** `src/wb/wbPromotionsUserInput/wbPromotionsUserInput.service.js`

```javascript
static async find({ client_id, sid?, page, limit })
```

1. Запрос к `ds_wb10x_promotions_v3` с LEFT JOIN на `wb_cards` по `(nm_id, sid)`
2. Опциональный фильтр по `sid`
3. Параллельно: SELECT items + COUNT
4. Возвращает `{ items, total }`

### 3.3 Сервис — create

```javascript
static async create({ client_id, sid, srid?, date?, nm_id?, count?,
                       price_with_spp?, promos_type, promo_product_cost,
                       promo_buyout_cost, comment?, contractor?, review?, cashback? })
```

1. Если `srid` передан → `SELECT * FROM wb_sales WHERE srid = $1` (lookup продажи)
   - Ошибка `NOT_FOUND` если srid не существует
2. `promos_costs = promo_product_cost + promo_buyout_cost`
3. `INSERT INTO ss_wb10x_main_promotions_v3` (с id UUID, sid, promos_costs)
4. `wb10xPromotions_v3.prepareData({ client_id, sid })` — пересчёт датасета
5. `SELECT FROM ds_wb10x_promotions_v3 JOIN wb_cards WHERE id = $createdId`
6. Возвращает `{ item }`

### 3.4 Сервис — update

```javascript
static async update({ client_id, id, date?, nm_id?, count?, price_with_spp?,
                       promos_type?, promo_product_cost?, promo_buyout_cost?,
                       comment?, contractor?, review?, cashback? })
```

1. `SELECT FROM ss WHERE id = $id` → получаем `sid` и `srid` записи
2. Если `srid` есть и переданы `date/nm_id/count/price_with_spp` → `VALIDATION_ERROR`
3. Пересчёт `promos_costs` если изменились составляющие
4. `UPDATE ss_wb10x_main_promotions_v3`
5. `wb10xPromotions_v3.prepareData({ client_id, sid })`
6. SELECT из ds → вернуть `{ item }`

### 3.5 Сервис — delete

1. `SELECT FROM ss WHERE id = $id` → получаем `sid`
2. `DELETE FROM ss_wb10x_main_promotions_v3 WHERE id = $id`
3. `wb10xPromotions_v3.prepareData({ client_id, sid })`
4. Вернуть `{ success: true }`

---

## 4. Источники данных

### Таблицы (client-scoped PostgreSQL)

| Таблица | Назначение | Ключевые поля |
|---|---|---|
| `ss_wb10x_main_promotions_v3` | Пользовательский ввод (из веба или Google Sheets) | id, sid, srid, date, nm_id, count, price_with_spp, promos_type, promo_product_cost, promo_buyout_cost, promos_costs, comment, contractor, review, cashback, spreadsheet_id |
| `ds_wb10x_promotions_v3` | Обогащённый compute-датасет (источник чтения для GET) | все поля ss + is_returned, spp, price_without_spp, sum, promo_total_cost, promo_unit_expenses + vendor_code, barcode + id, sid, comment, contractor, review, cashback |
| `wb_sales` | Продажи WB (синк через dataLoader) | srid, barcode, nm_id, date, count, price_with_spp, sid |
| `wb_cards` | Метаданные товаров (синк через dataLoader) | nm_id, sid, title, brand, subject_name |

### Что откуда берётся при чтении (GET)

```sql
SELECT
    ds.*,
    c.title, c.brand, c.subject_name
FROM ds_wb10x_promotions_v3 ds
LEFT JOIN wb_cards c ON ds.nm_id = c.nm_id AND ds.sid = c.sid
[WHERE ds.sid = $sid]
ORDER BY ds.updated_at DESC
LIMIT $limit OFFSET $offset;
```

---

## 5. Агрегация — wb10xPromotions_v3.prepareData()

**Файл:** `src/datasets/wb/wb10xPromotions_v3/wb10xPromotions_v3.js`

Вызывается синхронно после каждой мутации (create/update/delete). Алгоритм:

1. `DELETE FROM ds_wb10x_promotions_v3 WHERE sid = $sid`
2. Читает из `ss_wb10x_main_promotions_v3` записи с `sid = $sid`
3. JOIN на `wb_sales` по `srid` (для записей с srid) — получает barcode, is_returned
4. Рассчитывает:
   - `spp` — % скидки по программе лояльности
   - `price_without_spp = price_with_spp * (1 - spp/100)`
   - `sum = price_with_spp * count`
   - `promo_total_cost = promos_costs + promo_unit_expenses`
   - `promo_unit_expenses` — прочие расходы на единицу
5. `INSERT INTO ds_wb10x_promotions_v3`

Таймаут NATS для create/update: **30 секунд** (prepareData — тяжёлая операция).

---

## 6. Отключение Google Sheets синка при веб-режиме

**Файл:** `src/main/clientsWbTokens/clientsWbTokens.service.js`

Когда клиент добавляет WB токен → `disablePromotionsDataset({ client_id })`:
- Находит все spreadsheets клиента
- Для каждой ставит `is_active = false` в `spreadsheets_datasets` для датасета `wb10xMain_promotions_v3_sync`
- ssLoader пропускает отключённые датасеты → Google Sheets перестаёт перезаписывать веб-данные

---

## 7. Коды ошибок

| Код | Условие |
|---|---|
| `NOT_FOUND` | Запись не найдена по id (update/delete) или srid не найден в wb_sales (create) |
| `VALIDATION_ERROR` | Попытка изменить date/nmId/count/priceWithSpp у записи с srid |
| `INTERNAL_ERROR` | DB ошибки, ошибка prepareData |

---

## 8. Downstream потребители — где используется ds_wb10x_promotions_v3

После `prepareData()` таблица `ds_wb10x_promotions_v3` читается несколькими датасетами.

### 8.1 Чеклист — `wb10xChecklistByDaysAndNmIds_v1`

**Файл:** `src/datasets/wb/wb10xChecklistByDaysAndNmIds_v1/wb10xChecklistByDaysAndNmIds_v1.sql`

```sql
-- CTE: promotions_b
LEFT JOIN ds_wb10x_promotions_v3 p ON c.date = p.date AND c.nm_id = p.nm_id
```

Из `ds_wb10x_promotions_v3` берутся:
- `SUM(promo_total_cost)` — суммарные расходы на раздачи за день
- `SUM(promo_unit_expenses)` — расходы на единицу товара

Участвуют в расчёте:
- `total_adv_costs = adv_sum + promo_total_cost + external_costs`
- `expected_cost_sum_rub` — учитывает `promo_unit_expenses`
- `profit_with_adv`, `marg_with_adv`

Точная формула в SQL:
```sql
ROUND(promos.promo_total_cost, 2)                            AS promo_total_cost
ROUND(COALESCE(promos.promo_unit_expenses, 0), 2)            AS promo_unit_expenses
COALESCE(adv_sum, 0) + promo_total_cost + ext.cost_per_day   AS total_adv_costs
```

Выходные колонки чеклиста: `promo_total_cost`, `promo_unit_expenses`, `total_adv_costs`, `profit_with_adv`, `marg_with_adv`.

**Фронтенд чеклиста (`sw-front/src/app/checklist/[id]/page.tsx`) — страница существует, но в разработке** (placeholder, реальной таблицы нет). Колонки `promo_total_cost` и `promo_unit_expenses` в UI пока не отображаются.

### 8.2 Финансовый отчёт продаж — `wb10xSalesFinReportDaily_v1`

**Файл:** `src/datasets/wb/wb10xSalesFinReportDaily_v1/wb10xSalesFinReportDaily_v1.sql`

```sql
JOIN ds_wb10x_promotions_v3 p ON p.date = s.date AND p.nm_id = s.nm_id
-- SUM(p.promo_total_cost)
```

`promo_total_cost` включается в расчёт операционных расходов и итоговой прибыли в ежедневном финансовом отчёте.

### 8.3 Месячное планирование — `wb10xMain_planMonth_v2`

**Файл:** `src/datasets/wb/wb10xMain_planMonth_v2/wb10xMain_planMonth_v2.js`

Агрегирует из чеклиста:
- `SUM(promo_total_cost)` → `promo_total_cost_fact`
- `orders_without_promos_fact = orders_fact - promo_orders_fact`

Фронт: страница `/planning/month/[id]`, блок `fact-data` — колонка `promoTotalCostFact` (фактические расходы на раздачи за месяц).

### 8.4 Google Sheets синк — `wb10xMain_promotions_v3_sync`

**Файл:** `src/datasets/wb/wb10xMain_promotions_v3_sync/wb10xMain_promotions_v3_sync.js`

Синхронизирует `ss_wb10x_main_promotions_v3` ↔ Google Sheets (таблица "Раздачи"). При включённом SS режиме данные из таблицы попадают в `ss_` → через `prepareData()` → в `ds_`. При веб-режиме синк отключается.

### Итоговая схема влияния

```
ss_wb10x_main_promotions_v3 (CRUD через веб или Google Sheets)
  └─ prepareData({ client_id, sid }) [синхронно, timeout 30s]
       └─ ds_wb10x_promotions_v3
            ├─ wb10xChecklistByDaysAndNmIds_v1
            │    → promo_total_cost, promo_unit_expenses
            │    → total_adv_costs, profit_with_adv, marg_with_adv
            │    → [фронт: чеклист / таблица товаров]
            ├─ wb10xSalesFinReportDaily_v1
            │    → promo_total_cost в отчёте продаж
            └─ wb10xMain_planMonth_v2 (через чеклист)
                 → promoTotalCostFact
                 → [фронт: планирование месяца]
```

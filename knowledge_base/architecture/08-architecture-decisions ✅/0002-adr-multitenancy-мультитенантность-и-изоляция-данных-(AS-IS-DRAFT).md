# ADR-0002. Мультитенантность и изоляция данных

**Статус:** ?
**Дата:** 2026-02-12
**Автор:** [Имя/Команда]

## Контекст

**Мультитенантность** - это когда одна система обслуживает много клиентов (tenants), и **данные клиентов должны быть полностью изолированы**.

**Что произойдет при нарушении:**

- Client A увидит данные Client B (breach of privacy)
- Client A изменит/удалит данные Client B (data corruption)
- Юридические последствия (GDPR, договоры)
- Потеря доверия клиентов

**Поэтому:** При постановке ЛЮБОЙ задачи с данными клиентов нужно явно описывать проверку изоляции.

---

### Как устроена изоляция в 10X

### Архитектура: Shared Database + Separate Schemas

```sql
Database: mp_qa (или mp_prod)
  │
  ├── Schema: public                 -- Общие таблицы (users, clients)
  │    ├── Table: users              -- Все пользователи
  │    ├── Table: clients            -- Все клиенты
  │    ├── Table: auth_tokens        -- Токены
  │    └── Table: roles              -- Роли
  │
  ├── Schema: client_1               -- ИЗОЛИРОВАННЫЕ данные клиента 1
  │    ├── Table: wb_cards           -- Товары клиента 1
  │    ├── Table: wb_stocks          -- Остатки клиента 1
  │    ├── Table: wb_orders          -- Заказы клиента 1
  │    └── ...                       -- 50+ таблиц
  │
  ├── Schema: client_2               -- ИЗОЛИРОВАННЫЕ данные клиента 2
  │    ├── Table: wb_cards           -- Товары клиента 2 (другие!)
  │    └── ...
  │
  └── Schema: shared                 -- Справочники (общие для всех)
       ├── Table: wb_tariffs_commissions
       ├── Table: wb_categories
       └── ...
```

### Mapping: User → Client → Schema

```sql
-- Таблица users (public schema)
CREATE TABLE public.users (
  id SERIAL PRIMARY KEY,           -- user_id = client_id (!)
  email VARCHAR(255) UNIQUE,
  password VARCHAR(255),
  roles TEXT[],                    -- ['admin', 'client', etc]
  is_active BOOLEAN DEFAULT TRUE
);

-- Таблица clients (public schema)
CREATE TABLE public.clients (
  id SERIAL PRIMARY KEY,           -- client_id
  name VARCHAR(255),
  is_active BOOLEAN DEFAULT TRUE,
  created_at TIMESTAMP
);

-- User ID = Client ID (!)
-- User 1 → Client 1 → Schema client_1
-- User 2 → Client 2 → Schema client_2
```

**КРИТИЧНО:** В этой системе `user.id` = `client.id`. Один user = один client.

---

### Как работает изоляция в коде

### JWT токен

После успешного логина backend возвращает JWT токен:

```json
{
  "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9..."
}
```

**Декодированный токен:**

```json
{
  "id": 7,                           // user_id = client_id
  "email": "darya.qa@btlz-api.ru",
  "roles": ["admin"],
  "is_active": true,
  "iat": 1769713440,
  "exp": 1769714040
}
```

### Middleware автоматически устанавливает schema

```javascript
// Backend: src/middleware/setClientSchema.js

async function setClientSchema(req, res, next) {
  const user = req.user;  // Из JWT токена
  
  if (!user || !user.id) {
    return res.status(401).json({ message: 'Unauthorized' });
  }
  
  const client_id = user.id;  // user.id = client_id
  
  // Устанавливаем schema для всех SQL запросов
  await db.raw(`SET search_path TO client_${client_id}, public`);
  
  req.client_id = client_id;
  next();
}
```

**Теперь все SQL запросы работают с `client_{id}` schema:**

```javascript
// Этот запрос автоматически выполнится в client_7 schema
const cards = await db('wb_cards').select('*');

// SQL: SELECT * FROM client_7.wb_cards
// Client 8 НЕ увидит эти данные
```

---

### Как проверять изоляцию в постановке

### Обязательный раздел в задаче

```markdown
## Изоляция данных

### Проверка tenant_id

**Given:** Client 1 создал данные (10 записей в client_1.wb_cards)  
**When:** Client 2 пытается прочитать client_1.wb_cards  
**Then:**  
- Получает 0 records (изоляция работает)
- ИЛИ получает 403 Forbidden (если явный доступ запрещен)

### SQL-проверка (для QA)

```sql
-- 1. Войти как Client 1
-- JWT токен: {"id": 1, "roles": ["client"]}

-- 2. Создать данные
INSERT INTO wb_cards (nm_id, name) VALUES (12345, 'Test Product');

-- 3. Проверить что данные в client_1 schema
SELECT current_schema();  
-- Должно вернуть: client_1

SELECT * FROM wb_cards WHERE nm_id = 12345;
-- Должно вернуть: 1 record

-- 4. Войти как Client 2
-- JWT токен: {"id": 2, "roles": ["client"]}

-- 5. Попытаться прочитать данные Client 1
SELECT * FROM wb_cards WHERE nm_id = 12345;
-- Должно вернуть: 0 records (Client 2 видит только client_2.wb_cards)

-- 6. Попытаться явно обратиться к client_1
SELECT * FROM client_1.wb_cards WHERE nm_id = 12345;
-- Должно вернуть: ERROR или 0 records (доступ запрещен)
```

### API-проверка (для QA)

### Test Case: Изоляция через API

```bash
# 1. Client 1 создает товар
curl -X POST https://qa.btlz-api.ru/api/wb/cards \
  -H "Authorization: Bearer {client_1_jwt}" \
  -d '{"nm_id": 12345, "name": "Test Product"}'
# → 201 Created

# 2. Client 1 читает свои товары
curl -X GET https://qa.btlz-api.ru/api/wb/cards \
  -H "Authorization: Bearer {client_1_jwt}"
# → 200 OK, [{"nm_id": 12345, "name": "Test Product"}]

# 3. Client 2 читает свои товары
curl -X GET https://qa.btlz-api.ru/api/wb/cards \
  -H "Authorization: Bearer {client_2_jwt}"
# → 200 OK, [] (пустой массив - не видит данные Client 1)

# 4. Client 2 пытается прочитать товар Client 1 напрямую
curl -X GET https://qa.btlz-api.ru/api/wb/cards/12345 \
  -H "Authorization: Bearer {client_2_jwt}"
# → 404 Not Found (товар не найден в client_2 schema)
```

---

### Типичные ошибки изоляции

### Ошибка 1: SQL Injection в schema name

❌ **Неправильно:**

```javascript
const client_id = req.params.clientId;  // От пользователя!
await db.raw(`SET search_path TO client_${client_id}`);
```

**Атака:**

```bash
curl /api/clients/1';DROP%20TABLE%20users;--/cards
# SQL: SET search_path TO client_1'; DROP TABLE users; --
```

✅ **Правильно:**

```javascript
const client_id = parseInt(req.user.id, 10);  // Из JWT (trusted)
if (isNaN(client_id) || client_id < 1) {
  return res.status(400).json({ message: 'Invalid client_id' });
}
await db.raw('SET search_path TO ??, public', [`client_${client_id}`]);
```

**В постановке указывать:**

```markdown
### Безопасность
- `client_id` берется ТОЛЬКО из JWT токена (`req.user.id`)
- НЕ из URL параметров или query string
- Валидация: `client_id` должен быть положительным integer
```

### Ошибка 2: Middleware не применен

❌ **Неправильно:**

```javascript
router.get('/api/cards', getCards);  // Нет middleware!
```

**Результат:** Все пользователи видят данные первого client в `search_path`.

✅ **Правильно:**

```javascript
router.get('/api/cards', 
  authMiddleware,        // Проверка JWT
  setClientSchema,       // Установка schema
  getCards
);
```

**В постановке указывать:**

```markdown
### Middleware Chain
1. authMiddleware - проверка JWT токена
2. setClientSchema - установка client_{id} schema
3. checkRole(['client', 'admin']) - проверка роли
4. businessLogic - основная логика
```

### Ошибка 3: Connection pool sharing (race condition)

**Проблема:** PostgreSQL connection pool переиспользует соединения. Если schema не установлена правильно, следующий запрос может выполниться в schema предыдущего клиента.

❌ **Неправильно:**

```javascript
// Request 1 (Client 1):
await db.raw('SET search_path TO client_1');
const cards1 = await db('wb_cards').select('*');  // OK

// Request 2 (Client 2) использует то же connection из pool:
// НЕТ SET search_path!
const cards2 = await db('wb_cards').select('*');  
// BUG: выполнится в client_1 schema! Client 2 увидит данные Client 1!
```

✅ **Правильно:**

```javascript
// ВСЕГДА устанавливать schema в начале каждого запроса
async function withClientSchema(client_id, callback) {
  return db.transaction(async (trx) => {
    await trx.raw('SET LOCAL search_path TO ??, public', [`client_${client_id}`]);
    return await callback(trx);
  });
}

// Использование:
const cards = await withClientSchema(req.user.id, async (trx) => {
  return trx('wb_cards').select('*');
});
```

**В постановке указывать:**

```markdown
### Тестирование race conditions
**Given:** 100 параллельных запросов от Client 1 и Client 2  
**When:** оба клиента читают свои данные одновременно  
**Then:** каждый видит ТОЛЬКО свои данные (0 утечек)
```

---

### Чек-лист изоляции для аналитика

При постановке задачи с данными клиентов ОБЯЗАТЕЛЬНО проверить:

- [ ] **Источник client_id** указан (из JWT, не из параметров URL)
- [ ] **Middleware** применен (authMiddleware + setClientSchema)
- [ ] **Негативный тест** описан (Client 2 не видит данные Client 1)
- [ ] **SQL injection** защита (параметризированные запросы)
- [ ] **Race condition** тест (параллельные запросы от разных клиентов)
- [ ] **Тестовые данные** для 2+ клиентов указаны

---

### Пример правильной постановки

```markdown
# [Feature] Импорт остатков товаров WB

## Изоляция данных

### Требование
Остатки импортируются в schema `client_{id}` где `id` берется из JWT токена текущего пользователя.

### Проверка изоляции

**AC: Client 1 не видит данные Client 2**

**Given:**  
- Client 1 (user_id=1) импортировал 1500 товаров
- Данные сохранены в `client_1.wb_stocks`
- Client 2 (user_id=2) импортировал 800 товаров
- Данные сохранены в `client_2.wb_stocks`

**When:** Client 1 запрашивает GET /api/wb/stocks  
**Then:**  
- Видит только свои 1500 товаров (из client_1.wb_stocks)
- НЕ видит 800 товаров Client 2

**When:** Client 2 запрашивает GET /api/wb/stocks  
**Then:**  
- Видит только свои 800 товаров (из client_2.wb_stocks)
- НЕ видит 1500 товаров Client 1

**Негативный тест:**
```bash
# Client 1 пытается прочитать данные Client 2 напрямую
curl GET /api/wb/stocks?client_id=2 \
  -H "Authorization: Bearer {client_1_jwt}"
# → 403 Forbidden (client_id игнорируется, используется только JWT)
```

### SQL-проверка (для QA)

```sql
-- После импорта Client 1:
SELECT COUNT(*) FROM client_1.wb_stocks;  -- Должно: 1500
SELECT COUNT(*) FROM client_2.wb_stocks;  -- Должно: 0 (до импорта Client 2)

-- После импорта Client 2:
SELECT COUNT(*) FROM client_1.wb_stocks;  -- Должно: 1500 (не изменилось!)
SELECT COUNT(*) FROM client_2.wb_stocks;  -- Должно: 800

-- Проверка cross-schema access
SELECT * FROM client_1.wb_stocks WHERE client_id = 2;  -- Должно: ERROR или 0 records
```

### Middleware Chain

```javascript
router.post('/api/wb/import',
  authMiddleware,           // 1. Проверка JWT токена
  setClientSchema,          // 2. SET search_path TO client_{req.user.id}
  checkRole(['client', 'admin']),  // 3. Проверка роли
  importWBStocks            // 4. Бизнес-логика (уже в правильной schema)
);
```

### Тестовые данные

**Client 1:**

- Email: <darya.qa@btlz-api.ru>
- JWT: `{"id": 1, "roles": ["admin"]}`
- Schema: `client_1`
- Данные: 1500 товаров (fixture: `tests/fixtures/wb_stocks_client1.json`)

**Client 2:**

- Email: <client.qa@btlz-api.ru>
- JWT: `{"id": 2, "roles": ["client"]}`
- Schema: `client_2`
- Данные: 800 товаров (fixture: `tests/fixtures/wb_stocks_client2.json`)

---

### Справочники (Shared Schema)

**Исключение:** Справочные данные (категории WB/Ozon, тарифы, курсы валют) хранятся в `shared` schema и доступны всем клиентам для чтения.

```sql
Schema: shared
  ├── wb_tariffs_commissions    -- Комиссии WB (одинаковы для всех)
  ├── wb_categories             -- Категории WB (справочник)
  ├── ozon_categories           -- Категории Ozon
  └── currency_rates            -- Курсы валют (общие)
```

**Важно:** Справочники доступны для **чтения** всем, но **изменять** может только `super_admin`.

**В постановке указывать:**

```markdown
### Доступ к справочникам

**Чтение (все клиенты):**
```sql
SELECT * FROM shared.wb_categories WHERE id = 123;
-- Доступно Client 1, Client 2, ... (read-only)
```

**Изменение (только super_admin):**

```sql
UPDATE shared.wb_categories SET name = 'New Name' WHERE id = 123;
-- Требуется роль super_admin
-- Все остальные получат 403
```

---

### Критические точки для проверки

### 1. Создание новых данных

**При постановке задачи на CREATE обязательно указать:**

```markdown
### Создание данных

**Endpoint:** POST /api/wb/cards

**Schema:** Данные сохраняются в `client_{id}.wb_cards` где `id` из JWT токена.

**SQL:**
```sql
-- Middleware уже установил: SET search_path TO client_{req.user.id}
INSERT INTO wb_cards (nm_id, name, price)
VALUES (12345, 'Product Name', 1999);

-- Фактически выполнится:
-- INSERT INTO client_1.wb_cards ...  (для Client 1)
-- INSERT INTO client_2.wb_cards ...  (для Client 2)
```

**Проверка изоляции:**

- Client 1 создал товар 12345
- Client 2 создал товар 12345 (то же nm_id!)
- Оба товара сохранены (в разных schemas)
- Client 1 видит только свой, Client 2 видит только свой

### 2. Чтение данных

**При постановке задачи на READ обязательно указать:**

```markdown
### Чтение данных

**Endpoint:** GET /api/wb/cards

**Schema:** Читаются данные из `client_{id}.wb_cards`.

**Filters:**
- По умолчанию: все товары текущего клиента
- Query param `?nm_id=12345`: фильтр по nm_id (ТОЛЬКО в своей schema)

**Изоляция:**
- Client 1 видит только client_1.wb_cards
- Client 2 видит только client_2.wb_cards
- Попытка прочитать client_1.wb_cards через Client 2 JWT → 0 records
```

### 3. Обновление данных

**При постановке задачи на UPDATE обязательно указать:**

```markdown
### Обновление данных

**Endpoint:** PATCH /api/wb/cards/:nm_id

**Schema:** Обновляются данные в `client_{id}.wb_cards`.

**Изоляция:**
- Client 1 может обновить только client_1.wb_cards
- Попытка Client 1 обновить товар из client_2 → 404 Not Found
```

### 4. Удаление данных

**При постановке задачи на DELETE обязательно указать:**

```markdown
### Удаление данных

**Endpoint:** DELETE /api/wb/cards/:nm_id

**Schema:** Удаляются данные из `client_{id}.wb_cards`.

**Изоляция:**
- Client 1 может удалить только свои товары (client_1.wb_cards)
- Попытка удалить товар Client 2 → 404 Not Found (не 403!)
```

---

### Шаблон для постановки

Используй этот шаблон в КАЖДОЙ задаче с данными клиентов:

```markdown
## Изоляция данных

### Источник client_id
- Берется из JWT токена (`req.user.id`)
- НЕ из URL параметров или query string
- Валидация: positive integer

### Schema
- Данные сохраняются/читаются из `client_{id}.{table_name}`
- Middleware `setClientSchema` устанавливает `search_path` автоматически

### Проверка изоляции (Given/When/Then)

**AC: Client A не видит данные Client B**

Given: Client A создал 10 записей в client_A.table_name  
And: Client B создал 5 записей в client_B.table_name  
When: Client A запрашивает GET /api/table_name  
Then: Видит только свои 10 записей (не 15!)

When: Client B запрашивает GET /api/table_name  
Then: Видит только свои 5 записей

When: Client A пытается прочитать client_B.table_name  
Then: Получает 403 Forbidden или 0 records

### SQL-проверка (для QA)
```sql
-- Test isolation
SELECT COUNT(*) FROM client_1.table_name;  -- Client 1 data only
SELECT COUNT(*) FROM client_2.table_name;  -- Client 2 data only

-- Test cross-schema access (should fail)
-- Login as Client 1
SELECT * FROM client_2.table_name;  -- ERROR or 0 records
```

### Middleware Chain ๏๏

```javascript
router.post('/api/endpoint',
  authMiddleware,           // JWT check
  setClientSchema,          // Schema isolation
  checkRole([...]),         // RBAC
  businessLogic
);
```

### Тестовые данные ๏๏

- Client 1: user_id=1, email=<client1@test.ru>, schema=client_1
- Client 2: user_id=2, email=<client2@test.ru>, schema=client_2
- Fixture: `tests/fixtures/table_name_client{1,2}.json`

---

### Вопросы для самопроверки

Перед отправкой задачи в разработку задай себе вопросы:

1. **Откуда берется client_id?** (Должно быть: из JWT токена)
2. **Применен ли setClientSchema middleware?** (Должно быть: да)
3. **Описан ли негативный тест на изоляцию?** (Должно быть: да)
4. **Указаны ли тестовые данные для 2+ клиентов?** (Должно быть: да)
5. **Понятно ли QA как проверить изоляцию?** (Должно быть: да, есть SQL или API примеры)

**Если хотя бы на 1 вопрос ответ "нет" → задача НЕ готова.**

---

### Итого

**Мультитенантность** - это не "просто таблица с полем client_id". Это архитектурное решение с отдельными schemas, middleware, и строгой проверкой изоляции.

**Твоя задача как аналитика:** ВСЕГДА явно описывать требования к изоляции, чтобы разработчик не "забыл" или "не подумал" об этом.

---

**Следующий шаг:** [03-RBAC.md](03-RBAC.md) - Роли и права доступа

## Решение

## Обоснование

## Альтернативы

## Последствия

>Связанные документы:

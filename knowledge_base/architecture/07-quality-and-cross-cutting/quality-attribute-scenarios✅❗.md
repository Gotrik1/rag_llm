# Технические требования и стандарты проекта

**Дата создания:** 2026-01-27  
**Версия:** 1.0  
**Статус:** DRAFT  
**Автор:** Юрий Шульдешов

---

## Оглавление

1. [Введение](#1-введение)
2. [SLA (Service Level Agreement)](#2-sla-service-level-agreement)
3. [Timeout политики](#3-timeout-политики)
4. [Retry политики](#4-retry-политики)
5. [Error Handling](#5-error-handling)
6. [Logging](#6-logging)
7. [Monitoring и Alerting](#7-monitoring-и-alerting)
8. [Database](#8-database)
9. [API Design](#9-api-design)
10. [Security](#10-security)
11. [Performance](#11-performance)
12. [Testing](#12-testing)

---

## 1. Введение

### 1.1. Цель документа

Этот документ определяет **единые технические стандарты** для всех сервисов проекта:

- Backend (sl-back, Node.js)
- Frontend (sw-front, Next.js)
- DevOps (infrastructure, CI/CD)
- QA (автотесты, smoke tests)

### 1.2. Область применения

**Обязательно для:**

- Новых фичей и сервисов
- Рефакторинга существующего кода
- Code review процесса
- Архитектурных решений

**Исключения:** Legacy код (требует постепенной миграции)

### 1.3. Культурные изменения

**От:**

- "Сделайте так же, как в WB РНП" (без документации)
- Разные подходы у разных разработчиков
- Знания в головах ключевых сотрудников

**К:**

- Документированные стандарты
- Единообразный код
- Взаимозаменяемость сотрудников
- Быстрый онбординг новых членов команды

---

## 2. SLA (Service Level Agreement)

### 2.1. Целевые метрики

| Метрика                 | Target  | Описание                       |
|-------------------------|---------|--------------------------------|
| **Uptime**              | 99.5%   | Доступность сервиса (месячно)  |
| **Response Time (p50)** | < 500ms | Медианное время ответа API     |
| **Response Time (p95)** | < 2s    | 95-й перцентиль времени ответа |
| **Response Time (p99)** | < 5s    | 99-й перцентиль времени ответа |
| **Error Rate**          | < 1%    | Процент ошибок 5xx             |
| **Database Query Time** | < 100ms | p95 для SELECT запросов        |

### 2.2. Классификация операций

#### 🔴 Critical (P0)

- **Время ответа:** < 1s (p95)
- **Uptime:** 99.9%
- **Примеры:**
  - Аутентификация пользователя
  - Загрузка главной страницы
  - Базовые CRUD операции

#### 🟡 High (P1)

- **Время ответа:** < 3s (p95)
- **Uptime:** 99.5%
- **Примеры:**
  - Аналитические отчеты
  - РНП расчеты
  - Синхронизация данных маркетплейсов

#### 🟢 Medium (P2)

- **Время ответа:** < 10s (p95)
- **Uptime:** 99%
- **Примеры:**
  - Экспорт в Excel
  - Пакетная обработка данных
  - Background jobs

#### ⚪ Low (P3)

- **Время ответа:** < 30s
- **Uptime:** 95%
- **Примеры:**
  - Административные операции
  - Миграции данных
  - Редкие отчеты

### 2.3. Degraded Mode (деградация сервиса)

**Когда база данных недоступна:**

- Возвращать закешированные данные (если есть)
- Показывать последние известные значения с меткой времени
- HTTP 503 Service Unavailable с Retry-After заголовком

**Когда внешний API недоступен (WB/Ozon):**

- Использовать данные из последней успешной синхронизации
- Показывать предупреждение о задержке данных
- Автоматический retry через backoff

---

## 3. Timeout политики

### 3.1. HTTP Requests

#### Backend → Database

```javascript
// PostgreSQL
const dbConfig = {
  connectionTimeoutMillis: 5000,     // 5s - соединение с БД
  idleTimeoutMillis: 30000,          // 30s - idle connection
  query_timeout: 30000,               // 30s - выполнение запроса
  statement_timeout: 60000,           // 60s - длинные аналитические запросы
};

// ClickHouse
const clickhouseConfig = {
  request_timeout: 300000,            // 300s (5 min) - OLAP запросы
  connect_timeout: 10000,             // 10s
};
```

#### Backend → External APIs (WB/Ozon)

```javascript
const externalApiConfig = {
  timeout: 30000,                     // 30s - HTTP request timeout
  retry: {
    retries: 3,
    retryDelay: (retryCount) => Math.min(1000 * Math.pow(2, retryCount), 10000),
    retryCondition: (error) => {
      // Retry на 5xx, timeout, network errors
      return error.response?.status >= 500 || error.code === 'ECONNABORTED';
    },
  },
};
```

#### Backend → NATS (межсервисная коммуникация)

```javascript
const natsConfig = {
  timeout: 5000,                      // 5s - RPC call timeout
  reconnect: true,
  maxReconnectAttempts: 10,
  reconnectTimeWait: 1000,            // 1s между попытками
};
```

### 3.2. Frontend → Backend

```typescript
// Next.js API routes
const apiConfig = {
  // Быстрые операции (GET)
  fast: {
    timeout: 10000,                   // 10s
    retries: 2,
  },
  
  // Медленные операции (POST/PUT с обработкой)
  slow: {
    timeout: 60000,                   // 60s (1 min)
    retries: 1,
  },
  
  // Long-running операции
  longRunning: {
    timeout: 300000,                  // 300s (5 min)
    retries: 0,                       // Без retry для идемпотентности
  },
};
```

### 3.3. Таблица рекомендованных timeout'ов

| Операция         | Timeout | Retry | Обоснование        |
|------------------|---------|-------|--------------------|
| SELECT (простой) | 5s      | 1     | Быстрые запросы    |
| SELECT (join)    | 30s     | 0     | Сложные запросы    |
| SELECT (агрегат) | 60s     | 0     | Аналитика          |
| INSERT/UPDATE    | 10s     | 1     | Запись данных      |
| External API     | 30s     | 3     | Нестабильные сети  |
| File upload      | 120s    | 0     | Большие файлы      |
| Background job   | 600s    | 1     | Пакетная обработка |

---

## 4. Retry политики

### 4.1. Exponential Backoff

**Стандартная стратегия:**

```javascript
const retryWithBackoff = async (fn, options = {}) => {
  const {
    maxRetries = 3,
    initialDelay = 1000,      // 1s
    maxDelay = 10000,         // 10s
    factor = 2,
    jitter = true,
  } = options;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error) {
      if (attempt === maxRetries) throw error;
      
      // Не retry на клиентских ошибках (4xx)
      if (error.response?.status >= 400 && error.response?.status < 500) {
        throw error;
      }

      // Вычисляем задержку: delay = min(initialDelay * factor^attempt, maxDelay)
      let delay = Math.min(initialDelay * Math.pow(factor, attempt), maxDelay);
      
      // Добавляем jitter для предотвращения thundering herd
      if (jitter) {
        delay = delay * (0.5 + Math.random() * 0.5);
      }

      await sleep(delay);
    }
  }
};

// Использование
const data = await retryWithBackoff(
  () => fetchFromOzonAPI(params),
  { maxRetries: 3, initialDelay: 1000 }
);
```

**Пример задержек с jitter:**

- Attempt 1: ~1s (1000ms × random[0.5-1])
- Attempt 2: ~2s (2000ms × random[0.5-1])
- Attempt 3: ~4s (4000ms × random[0.5-1])

### 4.2. Условия для retry

**✅ Retry на:**

- 5xx ошибки (server errors)
- Network timeouts
- Connection refused
- DNS errors
- Rate limit (429) с Retry-After заголовком

**❌ НЕ retry на:**

- 4xx ошибки (client errors) кроме 429
- 401 Unauthorized (требует re-auth)
- 403 Forbidden
- 404 Not Found
- 422 Validation Error

### 4.3. Idempotency (идемпотентность)

**Важно:** Retry безопасен только для идемпотентных операций!

**Идемпотентные (можно retry):**

- GET запросы
- PUT запросы с полной заменой
- DELETE запросы

**НЕ идемпотентные (retry с осторожностью):**

- POST запросы (создание ресурса)
- PATCH запросы (частичное обновление)
- Финансовые транзакции

**Решение для POST:**

```javascript
// Использовать Idempotency-Key
const createOrder = async (orderData) => {
  const idempotencyKey = generateUUID();
  
  return await retryWithBackoff(() => 
    axios.post('/api/orders', orderData, {
      headers: {
        'Idempotency-Key': idempotencyKey,
      },
    })
  );
};
```

### 4.4. Circuit Breaker

**Для защиты от cascading failures:**

```javascript
class CircuitBreaker {
  constructor(options = {}) {
    this.failureThreshold = options.failureThreshold || 5;
    this.successThreshold = options.successThreshold || 2;
    this.timeout = options.timeout || 60000; // 60s
    
    this.state = 'CLOSED'; // CLOSED | OPEN | HALF_OPEN
    this.failureCount = 0;
    this.successCount = 0;
    this.nextAttempt = Date.now();
  }

  async call(fn) {
    if (this.state === 'OPEN') {
      if (Date.now() < this.nextAttempt) {
        throw new Error('Circuit breaker is OPEN');
      }
      this.state = 'HALF_OPEN';
    }

    try {
      const result = await fn();
      this.onSuccess();
      return result;
    } catch (error) {
      this.onFailure();
      throw error;
    }
  }

  onSuccess() {
    this.failureCount = 0;
    if (this.state === 'HALF_OPEN') {
      this.successCount++;
      if (this.successCount >= this.successThreshold) {
        this.state = 'CLOSED';
        this.successCount = 0;
      }
    }
  }

  onFailure() {
    this.failureCount++;
    this.successCount = 0;
    if (this.failureCount >= this.failureThreshold) {
      this.state = 'OPEN';
      this.nextAttempt = Date.now() + this.timeout;
    }
  }
}

// Использование
const ozonApiBreaker = new CircuitBreaker({ failureThreshold: 5, timeout: 60000 });

const fetchOzonData = async () => {
  return await ozonApiBreaker.call(() => axios.get('https://api.ozon.ru/...'));
};
```

---

## 5. Error Handling

### 5.1. Стандартный формат ошибок

**Backend response:**

```typescript
interface ErrorResponse {
  error: {
    code: string;           // Уникальный код ошибки (ERR_DB_TIMEOUT)
    message: string;        // Человекочитаемое сообщение
    details?: any;          // Дополнительная информация
    timestamp: string;      // ISO 8601
    requestId: string;      // UUID для трейсинга
    path: string;           // URL запроса
  };
}

// Пример
{
  "error": {
    "code": "ERR_VALIDATION_FAILED",
    "message": "Validation failed for field 'email'",
    "details": {
      "field": "email",
      "reason": "Invalid email format"
    },
    "timestamp": "2026-01-27T10:30:00.000Z",
    "requestId": "550e8400-e29b-41d4-a716-446655440000",
    "path": "/api/users"
  }
}
```

### 5.2. HTTP Status Codes

| Код     | Название             | Когда использовать                               |
|---------|----------------------|--------------------------------------------------|
| **200** | OK                   | Успешный запрос (GET, PUT, PATCH)                |
| **201** | Created              | Ресурс создан (POST)                             |
| **204** | No Content           | Успешное удаление (DELETE)                       |
| **400** | Bad Request          | Невалидные данные от клиента                     |
| **401** | Unauthorized         | Не авторизован (нет токена)                      |
| **403** | Forbidden            | Недостаточно прав                                |
| **404** | Not Found            | Ресурс не найден                                 |
| **409** | Conflict             | Конфликт (duplicate key)                         |
| **422** | Unprocessable Entity | Валидация не прошла                              |
| **429** | Too Many Requests    | Rate limit exceeded                              |
| **500** | Internal Server Error| Ошибка сервера (не специфицирована)              |
| **502** | Bad Gateway          | Ошибка upstream сервиса                          |
| **503** | Service Unavailable  | Сервис временно недоступен                       |
| **504** | Gateway Timeout      | Timeout при обращении к upstream                 |

### 5.3. Error Codes (коды ошибок)

**Формат:** `ERR_<CATEGORY>_<SPECIFIC>`

```typescript
// Database errors
const DB_ERRORS = {
  ERR_DB_CONNECTION: 'Failed to connect to database',
  ERR_DB_TIMEOUT: 'Database query timeout',
  ERR_DB_DUPLICATE: 'Duplicate key violation',
  ERR_DB_NOT_FOUND: 'Record not found',
};

// External API errors
const API_ERRORS = {
  ERR_API_TIMEOUT: 'External API timeout',
  ERR_API_RATE_LIMIT: 'Rate limit exceeded',
  ERR_API_UNAVAILABLE: 'External API unavailable',
};

// Validation errors
const VALIDATION_ERRORS = {
  ERR_VALIDATION_FAILED: 'Validation failed',
  ERR_VALIDATION_MISSING_FIELD: 'Required field missing',
  ERR_VALIDATION_INVALID_FORMAT: 'Invalid format',
};

// Business logic errors
const BUSINESS_ERRORS = {
  ERR_INSUFFICIENT_BALANCE: 'Insufficient balance',
  ERR_PRODUCT_OUT_OF_STOCK: 'Product out of stock',
  ERR_INVALID_OPERATION: 'Invalid operation',
};
```

### 5.4. Global Error Handler (Express)

```javascript
// Backend: src/api/error.middleware.js
export const errorHandler = (err, req, res, next) => {
  const requestId = req.headers['x-request-id'] || generateUUID();
  
  // Логируем ошибку
  logger.error({
    requestId,
    error: err.message,
    stack: err.stack,
    path: req.path,
    method: req.method,
    body: req.body,
    query: req.query,
    userId: req.user?.id,
  });

  // Определяем статус код
  let statusCode = err.statusCode || 500;
  let errorCode = err.code || 'ERR_INTERNAL_SERVER';
  let message = err.message || 'Internal Server Error';

  // Специальная обработка известных ошибок
  if (err.name === 'ValidationError') {
    statusCode = 422;
    errorCode = 'ERR_VALIDATION_FAILED';
  } else if (err.code === '23505') { // PostgreSQL duplicate key
    statusCode = 409;
    errorCode = 'ERR_DB_DUPLICATE';
  } else if (err.name === 'TimeoutError') {
    statusCode = 504;
    errorCode = 'ERR_DB_TIMEOUT';
  }

  // В production не показываем внутренние детали
  const details = process.env.NODE_ENV === 'production' 
    ? undefined 
    : { stack: err.stack };

  res.status(statusCode).json({
    error: {
      code: errorCode,
      message,
      details,
      timestamp: new Date().toISOString(),
      requestId,
      path: req.path,
    },
  });
};
```

### 5.5. Frontend Error Handling

```typescript
// Frontend: lib/api.ts
export const handleApiError = (error: any) => {
  if (error.response) {
    // Сервер вернул ошибку
    const { status, data } = error.response;
    
    switch (status) {
      case 401:
        // Redirect to login
        router.push('/login');
        break;
      case 403:
        toast.error('У вас недостаточно прав');
        break;
      case 404:
        toast.error('Ресурс не найден');
        break;
      case 422:
        // Показываем ошибки валидации
        showValidationErrors(data.error.details);
        break;
      case 429:
        toast.error('Слишком много запросов. Попробуйте позже');
        break;
      case 503:
        toast.error('Сервис временно недоступен. Попробуйте позже');
        break;
      default:
        toast.error(data.error?.message || 'Произошла ошибка');
    }
  } else if (error.request) {
    // Запрос отправлен, но ответа нет (network error)
    toast.error('Проблема с подключением к серверу');
  } else {
    // Ошибка при настройке запроса
    toast.error('Произошла ошибка при отправке запроса');
  }
  
  // Логируем в Sentry/monitoring
  captureException(error);
};
```

---

## 6. Logging

### 6.1. Log Levels

| Level     | Когда использовать                     | Примеры                                      |
|-----------|----------------------------------------|----------------------------------------------|
| **ERROR** | Ошибки, требующие внимания             | Database connection failed, API timeout      |
| **WARN**  | Предупреждения, не критичны            | Deprecated API used, High memory usage       |
| **INFO**  | Важные события                         | User logged in, Order created, Job completed |
| **DEBUG** | Детальная отладка                      | Function called with params, Query executed  |
| **TRACE** | Очень детальная отладка                | Variable values, Step-by-step flow           |

### 6.2. Структурированные логи (JSON)

```javascript
// Плохо
console.log('User 123 created order 456 with total 1000');

// Хорошо
logger.info({
  event: 'order_created',
  userId: 123,
  orderId: 456,
  total: 1000,
  currency: 'RUB',
  timestamp: new Date().toISOString(),
});
```

### 6.3. Log Configuration

```javascript
// Backend: src/logger/logger.config.js
import winston from 'winston';

const logger = winston.createLogger({
  level: process.env.LOG_LEVEL || 'info',
  format: winston.format.combine(
    winston.format.timestamp(),
    winston.format.errors({ stack: true }),
    winston.format.json()
  ),
  defaultMeta: {
    service: 'sl-back',
    environment: process.env.NODE_ENV,
  },
  transports: [
    // Console для разработки
    new winston.transports.Console({
      format: winston.format.combine(
        winston.format.colorize(),
        winston.format.simple()
      ),
    }),
    
    // File для продакшена
    new winston.transports.File({ 
      filename: 'logs/error.log', 
      level: 'error' 
    }),
    new winston.transports.File({ 
      filename: 'logs/combined.log' 
    }),
  ],
});

export default logger;
```

### 6.4. Request Logging

**Логировать каждый HTTP запрос:**

```javascript
// Middleware для логирования запросов
app.use((req, res, next) => {
  const requestId = req.headers['x-request-id'] || generateUUID();
  req.requestId = requestId;
  
  const startTime = Date.now();
  
  res.on('finish', () => {
    const duration = Date.now() - startTime;
    
    logger.info({
      event: 'http_request',
      requestId,
      method: req.method,
      path: req.path,
      statusCode: res.statusCode,
      duration,
      userAgent: req.headers['user-agent'],
      userId: req.user?.id,
      ip: req.ip,
    });
    
    // Предупреждение если запрос медленный
    if (duration > 5000) {
      logger.warn({
        event: 'slow_request',
        requestId,
        method: req.method,
        path: req.path,
        duration,
      });
    }
  });
  
  next();
});
```

### 6.5. Database Query Logging

```javascript
// Логировать медленные запросы
const db = knex({
  client: 'postgresql',
  connection: dbConfig,
  log: {
    warn(message) {
      logger.warn({ event: 'db_warning', message });
    },
    error(message) {
      logger.error({ event: 'db_error', message });
    },
    deprecate(message) {
      logger.warn({ event: 'db_deprecate', message });
    },
  },
});

// Hook для измерения времени запросов
db.on('query', (query) => {
  const startTime = Date.now();
  
  query.on('query-response', (response, obj, builder) => {
    const duration = Date.now() - startTime;
    
    if (duration > 1000) { // > 1s
      logger.warn({
        event: 'slow_query',
        sql: obj.sql,
        bindings: obj.bindings,
        duration,
      });
    }
  });
});
```

---

## 7. Monitoring и Alerting

### 7.1. Метрики для мониторинга

**Application Metrics:**

- Request rate (req/sec)
- Response time (p50, p95, p99)
- Error rate (%)
- Active connections

**Database Metrics:**

- Query time (p95)
- Connection pool usage
- Slow queries count
- Deadlocks

**Infrastructure Metrics:**

- CPU usage (%)
- Memory usage (%)
- Disk I/O
- Network I/O

### 7.2. Health Check Endpoints

```javascript
// Backend: src/health/health.controller.js

// Простой health check (для load balancer)
app.get('/api/health/live', (req, res) => {
  res.status(200).json({ status: 'ok' });
});

// Детальный health check (для мониторинга)
app.get('/api/health/ready', async (req, res) => {
  const checks = {
    database: await checkDatabase(),
    redis: await checkRedis(),
    nats: await checkNats(),
  };
  
  const allHealthy = Object.values(checks).every(c => c.status === 'ok');
  const statusCode = allHealthy ? 200 : 503;
  
  res.status(statusCode).json({
    status: allHealthy ? 'ok' : 'degraded',
    checks,
    timestamp: new Date().toISOString(),
  });
});

async function checkDatabase() {
  try {
    const start = Date.now();
    await db.raw('SELECT 1');
    const duration = Date.now() - start;
    
    return {
      status: 'ok',
      responseTime: duration,
    };
  } catch (error) {
    return {
      status: 'error',
      message: error.message,
    };
  }
}
```

### 7.3. Alerting Rules

**Critical Alerts (сразу будить):**

- Error rate > 5% (5 минут подряд)
- Response time p95 > 10s (5 минут подряд)
- Database connections > 90% pool
- Disk usage > 90%
- Service down (health check failed)

**Warning Alerts (можно подождать):**

- Error rate > 2% (10 минут подряд)
- Response time p95 > 5s (10 минут подряд)
- Database connections > 70% pool
- Memory usage > 80%
- Slow queries > 10/min

### 7.4. On-Call Rotation

**Дежурный инженер должен:**

1. Ответить на alert в течение 15 минут
2. Начать investigation в течение 30 минут
3. Предоставить status update каждые 30 минут
4. Эскалировать через 1 час если не решено

---

## 8. Database

### 8.1. Connection Pooling

```javascript
// PostgreSQL pool configuration
const poolConfig = {
  min: 2,                          // Минимум соединений
  max: 20,                         // Максимум соединений
  idleTimeoutMillis: 30000,        // 30s
  connectionTimeoutMillis: 5000,   // 5s
  acquireTimeoutMillis: 60000,     // 60s
};

// Monitoring pool
pool.on('acquire', (client) => {
  logger.debug({ event: 'pool_acquire', poolSize: pool.totalCount });
});

pool.on('error', (err, client) => {
  logger.error({ event: 'pool_error', error: err.message });
});
```

### 8.2. Query Best Practices

**✅ DO:**

```sql
-- Использовать prepared statements
SELECT * FROM users WHERE id = $1;

-- Использовать индексы
CREATE INDEX idx_users_email ON users(email);

-- Ограничивать результаты
SELECT * FROM orders ORDER BY created_at DESC LIMIT 100;

-- Использовать EXPLAIN ANALYZE
EXPLAIN ANALYZE SELECT ...;
```

**❌ DON'T:**

```sql
-- SQL injection уязвимость
SELECT * FROM users WHERE id = ${userId};

-- N+1 queries
for (const user of users) {
  const orders = await db.query('SELECT * FROM orders WHERE user_id = ?', [user.id]);
}

-- Без LIMIT на больших таблицах
SELECT * FROM orders;

-- JOIN без индексов
SELECT * FROM orders o JOIN users u ON o.user_id = u.id;
```

### 8.3. Transactions

```javascript
// Backend: использовать транзакции для атомарности

// Плохо
await db('users').insert(userData);
await db('profiles').insert(profileData);
// Если второй запрос упал, user создан, но profile нет!

// Хорошо
await db.transaction(async (trx) => {
  const [userId] = await trx('users').insert(userData).returning('id');
  await trx('profiles').insert({ ...profileData, user_id: userId });
  // Если что-то упало - весь rollback
});
```

### 8.4. Migration Best Practices

```javascript
// migrations/20260127_add_offer_id_to_products.js

exports.up = async function(knex) {
  await knex.schema.table('products', (table) => {
    table.string('offer_id', 255);
    table.index('offer_id'); // Сразу создаем индекс!
  });
  
  // Для больших таблиц: создавать индекс CONCURRENTLY
  await knex.raw('CREATE INDEX CONCURRENTLY idx_products_offer_id ON products(offer_id)');
};

exports.down = async function(knex) {
  await knex.schema.table('products', (table) => {
    table.dropColumn('offer_id');
  });
};
```

---

## 9. API Design

### 9.1. REST API Conventions

**URL naming:**

```ts
✅ GOOD:
GET    `/api/users`            - Список пользователей
GET    `/api/users/:id`          - Один пользователь
POST   `/api/users`              - Создать пользователя
PUT    `/api/users/:id`         - Обновить пользователя
PATCH  `/api/users/:id`          - Частично обновить
DELETE `/api/users/:id`          - Удалить пользователя

❌ BAD:
GET    `/api/getUsers`
POST   `/api/createUser`
GET    `/api/user/:id`
```

**Query parameters:**

```ts
GET `/api/products?page=1&limit=50&sort=price&order=desc&category=electronics`
```

**Filtering:**

```ts
GET `/api/orders?status=completed&date_from=2026-01-01&date_to=2026-01-31`
```

### 9.2. Request/Response Format

**Request:**

```ts
POST `/api/ozon/rnp/cards`
Content-Type: application/json
```

```json
{
  "client_id": 123,
  "date": "2026-01-01",
  "filters": [
    { "type": "category", "values": ["Electronics"] }
  ],
  "page": 1,
  "limit": 50
}
```

**Response:**

```json
{
  "data": [ /* результаты */ ],
  "pagination": {
    "page": 1,
    "limit": 50,
    "total": 1234,
    "total_pages": 25,
    "has_next": true
  },
  "meta": {
    "timestamp": "2026-01-27T10:30:00.000Z",
    "version": "1.0"
  }
}
```

### 9.3. Versioning

**URL versioning (рекомендуется):**

```ts
`/api/v1/users`
`/api/v2/users`
```

**Header versioning (альтернатива):**

```ts
GET `/api/users`
Accept-Version: v2
```

### 9.4. Rate Limiting

```javascript
// Backend: rate limiter middleware
import rateLimit from 'express-rate-limit';

const limiter = rateLimit({
  windowMs: 15 * 60 * 1000,      // 15 минут
  max: 100,                       // 100 запросов
  message: {
    error: {
      code: 'ERR_RATE_LIMIT',
      message: 'Too many requests, please try again later',
    },
  },
  standardHeaders: true,          // Return rate limit info in headers
  legacyHeaders: false,
});

app.use('/api/', limiter);

// Разные лимиты для разных эндпоинтов
const strictLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 10,                        // Только 10 запросов
});

app.use('/api/auth/login', strictLimiter);
```

---

## 10. Security

### 10.1. Authentication

**JWT Token:**

```javascript
// Срок жизни токенов
const TOKEN_CONFIG = {
  accessToken: {
    expiresIn: '15m',             // 15 минут
    algorithm: 'RS256',           // Асимметричное шифрование
  },
  refreshToken: {
    expiresIn: '30d',             // 30 дней
    algorithm: 'RS256',
  },
};

// Генерация токена
const generateAccessToken = (user) => {
  return jwt.sign(
    { 
      userId: user.id, 
      email: user.email,
      role: user.role,
    },
    privateKey,
    { 
      expiresIn: TOKEN_CONFIG.accessToken.expiresIn,
      algorithm: TOKEN_CONFIG.accessToken.algorithm,
      issuer: 'sl-back',
    }
  );
};
```

### 10.2. Input Validation

**Использовать Zod для валидации:**

```typescript
import { z } from 'zod';

const createUserSchema = z.object({
  email: z.string().email(),
  password: z.string().min(8).max(100),
  name: z.string().min(2).max(100),
  age: z.number().int().positive().max(150).optional(),
});

app.post('/api/users', async (req, res) => {
  try {
    const validatedData = createUserSchema.parse(req.body);
    // Данные валидны, продолжаем
  } catch (error) {
    return res.status(422).json({
      error: {
        code: 'ERR_VALIDATION_FAILED',
        message: 'Validation failed',
        details: error.errors,
      },
    });
  }
});
```

### 10.3. SQL Injection Prevention

**✅ ALWAYS use parameterized queries:**

```javascript
// Хорошо
const user = await db('users').where('email', email).first();

// Хорошо
const users = await db.raw('SELECT * FROM users WHERE email = ?', [email]);

// ПЛОХО! SQL injection!
const users = await db.raw(`SELECT * FROM users WHERE email = '${email}'`);
```

### 10.4. XSS Prevention

**Frontend: sanitize user input:**

```typescript
import DOMPurify from 'dompurify';

// Очистка HTML от XSS
const sanitizedHTML = DOMPurify.sanitize(userInput);

// React автоматически экранирует, но будьте осторожны с dangerouslySetInnerHTML
<div dangerouslySetInnerHTML={{ __html: sanitizedHTML }} />
```

### 10.5. Environment Variables

**НИКОГДА не коммитить секреты в Git!**

```bash
# .env (в .gitignore!)
DATABASE_URL=postgresql://user:password@localhost:5432/dbname
JWT_SECRET=super_secret_key_here
API_KEY_OZON=xxx
API_KEY_WB=yyy

# .env.example (коммитится в Git)
DATABASE_URL=postgresql://user:password@localhost:5432/dbname
JWT_SECRET=change_me
API_KEY_OZON=your_ozon_key
API_KEY_WB=your_wb_key
```

---

## 11. Performance

### 11.1. Caching Strategy

**Redis для кеширования:**

```javascript
import Redis from 'ioredis';

const redis = new Redis({
  host: process.env.REDIS_HOST,
  port: 6379,
  maxRetriesPerRequest: 3,
});

// Cache-aside pattern
const getCachedData = async (key, fetchFn, ttl = 3600) => {
  // Пытаемся получить из кеша
  const cached = await redis.get(key);
  if (cached) {
    return JSON.parse(cached);
  }
  
  // Если нет - загружаем из источника
  const data = await fetchFn();
  
  // Сохраняем в кеш
  await redis.setex(key, ttl, JSON.stringify(data));
  
  return data;
};

// Использование
const user = await getCachedData(
  `user:${userId}`,
  () => db('users').where('id', userId).first(),
  3600 // 1 час
);
```

**Когда кешировать:**

- ✅ Данные редко меняются (справочники, настройки)
- ✅ Дорогие вычисления (аналитика, агрегаты)
- ✅ Внешние API запросы

**Когда НЕ кешировать:**

- ❌ Данные часто меняются (заказы, остатки)
- ❌ Персональные данные
- ❌ Транзакционные операции

### 11.2. Database Optimization

**Индексы:**

```sql
-- Одиночные индексы
CREATE INDEX idx_users_email ON users(email);
CREATE INDEX idx_orders_created_at ON orders(created_at);

-- Составные индексы (порядок важен!)
CREATE INDEX idx_orders_user_date ON orders(user_id, created_at DESC);

-- Частичные индексы
CREATE INDEX idx_active_users ON users(email) WHERE is_active = true;

-- Проверка использования индексов
EXPLAIN ANALYZE SELECT * FROM orders WHERE user_id = 123 AND created_at > '2026-01-01';
```

**Connection pooling:**

- Использовать пул соединений (min: 2, max: 20)
- Не создавать новое соединение на каждый запрос

**Query optimization:**

- Избегать SELECT * (выбирать только нужные колонки)
- Использовать LIMIT для больших результатов
- Избегать N+1 queries (использовать JOIN или eager loading)

### 11.3. Pagination

**Cursor-based pagination (рекомендуется):**

```javascript
// Лучше для больших датасетов и реального времени
app.get('/api/orders', async (req, res) => {
  const { cursor, limit = 50 } = req.query;
  
  const query = db('orders')
    .orderBy('created_at', 'desc')
    .limit(limit + 1);
  
  if (cursor) {
    query.where('created_at', '<', cursor);
  }
  
  const orders = await query;
  const hasNext = orders.length > limit;
  
  if (hasNext) {
    orders.pop(); // Убираем лишний элемент
  }
  
  res.json({
    data: orders,
    pagination: {
      limit,
      hasNext,
      nextCursor: hasNext ? orders[orders.length - 1].created_at : null,
    },
  });
});
```

**Offset-based pagination (простой, но медленный):**

```javascript
// Проще, но медленнее на больших offset
app.get('/api/orders', async (req, res) => {
  const { page = 1, limit = 50 } = req.query;
  const offset = (page - 1) * limit;
  
  const [orders, { count }] = await Promise.all([
    db('orders').orderBy('created_at', 'desc').limit(limit).offset(offset),
    db('orders').count('* as count').first(),
  ]);
  
  res.json({
    data: orders,
    pagination: {
      page,
      limit,
      total: count,
      totalPages: Math.ceil(count / limit),
    },
  });
});
```

---

## 12. Testing

### 12.1. Testing Pyramid

```ts
           /\
          /  \         E2E Tests (5%)
         /    \        - Playwright
        /------\       - Critical user flows
       /        \
      /          \     Integration Tests (25%)
     /            \    - API tests
    /--------------\   - Database tests
   /                \
  /                  \ Unit Tests (70%)
 /____________________\ - Pure functions
                        - Business logic
```

### 12.2. Test Coverage Targets

| Тип кода       | Минимальный coverage   |
|----------------|:----------------------:|
| Business logic | 80%                    |
| API endpoints  | 70%                    |
| Utils/helpers  | 90%                    |
| UI components  | 60%                    |

### 12.3. Unit Tests (Jest)

```javascript
// tests/unit/calculateProfit.test.js
import { calculateProfit } from '@/utils/calculations';

describe('calculateProfit', () => {
  it('should calculate profit correctly', () => {
    const result = calculateProfit({
      revenue: 100000,
      cost: 30000,
      commission: 15000,
      advertising: 10000,
    });
    
    expect(result.profitWithAdv).toBe(45000);
    expect(result.profitWithoutAdv).toBe(55000);
    expect(result.margin).toBe(0.45);
  });
  
  it('should handle zero revenue', () => {
    const result = calculateProfit({
      revenue: 0,
      cost: 0,
      commission: 0,
      advertising: 0,
    });
    
    expect(result.profitWithAdv).toBe(0);
    expect(result.margin).toBe(0);
  });
});
```

### 12.4. Integration Tests

```javascript
// tests/integration/api/users.test.js
import request from 'supertest';
import app from '@/app';
import { setupTestDb, teardownTestDb } from '@/tests/helpers';

describe('Users API', () => {
  beforeAll(async () => {
    await setupTestDb();
  });
  
  afterAll(async () => {
    await teardownTestDb();
  });
  
  it('should create a new user', async () => {
    const response = await request(app)
      .post('/api/users')
      .send({
        email: 'test@example.com',
        password: 'password123',
        name: 'Test User',
      })
      .expect(201);
    
    expect(response.body.data).toHaveProperty('id');
    expect(response.body.data.email).toBe('test@example.com');
  });
});
```

### 12.5. Smoke Tests (Playwright)

```typescript
// tests/smoke/critical-paths.spec.ts
import { test, expect } from '@playwright/test';

test.describe('Critical User Flows', () => {
  test('User can login and see dashboard', async ({ page }) => {
    // Login
    await page.goto('/login');
    await page.fill('[name="email"]', 'test@example.com');
    await page.fill('[name="password"]', 'password123');
    await page.click('button[type="submit"]');
    
    // Verify redirect to dashboard
    await expect(page).toHaveURL('/dashboard');
    
    // Verify dashboard elements loaded
    await expect(page.locator('h1')).toContainText('Dashboard');
    await expect(page.locator('[data-testid="user-menu"]')).toBeVisible();
  });
  
  test('User can view RNP report', async ({ page }) => {
    await loginAsUser(page);
    
    await page.goto('/reports/rnp');
    await expect(page.locator('h1')).toContainText('РНП отчет');
    
    // Wait for data to load
    await page.waitForSelector('[data-testid="rnp-table"]');
    
    // Verify table has data
    const rows = await page.locator('[data-testid="rnp-table"] tbody tr').count();
    expect(rows).toBeGreaterThan(0);
  });
});
```

---

## 13. Code Review Checklist

### 13.1. Обязательные проверки

**Functionality:**

- [ ] Код решает заявленную задачу
- [ ] Нет регрессии (старый функционал работает)
- [ ] Edge cases покрыты

**Code Quality:**

- [ ] Код читаемый и понятный
- [ ] Нет дублирования кода (DRY)
- [ ] Переменные и функции названы понятно
- [ ] Комментарии там, где нужны

**Performance:**

- [ ] Нет N+1 queries
- [ ] Использованы индексы для БД запросов
- [ ] Большие операции асинхронные
- [ ] Pagination для списков

**Security:**

- [ ] Input validation через Zod
- [ ] Нет SQL injection
- [ ] Нет XSS уязвимостей
- [ ] Секреты в .env, не в коде

**Testing:**

- [ ] Unit tests добавлены
- [ ] Integration tests (если нужны)
- [ ] Coverage не упал

**Documentation:**

- [ ] Комментарии к сложной логике
- [ ] README обновлен (если нужно)
- [ ] API документация обновлена

### 13.2. Review Process

1. **Self-review:** Автор проверяет свой код перед созданием MR
2. **Automated checks:** CI/CD прогоняет тесты и линтеры
3. **Peer review:** Минимум 1 approve от другого разработчика
4. **Tech lead approval:** Для архитектурных изменений
5. **Merge:** После всех approvals

---

## 14. Deployment

### 14.1. CI/CD Pipeline

```yaml
# .gitlab-ci.yml
stages:
  - test
  - build
  - deploy

test:
  stage: test
  script:
    - npm install
    - npm run lint
    - npm run test
    - npm run test:integration
  coverage: '/All files[^|]*\|[^|]*\s+([\d\.]+)/'
  only:
    - merge_requests
    - main

build:
  stage: build
  script:
    - docker build -t sl-back:${CI_COMMIT_SHA} .
    - docker push sl-back:${CI_COMMIT_SHA}
  only:
    - main

deploy-staging:
  stage: deploy
  script:
    - kubectl set image deployment/sl-back sl-back=sl-back:${CI_COMMIT_SHA}
  environment:
    name: staging
  only:
    - main

deploy-production:
  stage: deploy
  script:
    - kubectl set image deployment/sl-back sl-back=sl-back:${CI_COMMIT_SHA}
  environment:
    name: production
  when: manual
  only:
    - main
```

### 14.2. Rollback Strategy

**В случае проблем:**

1. Сразу rollback на предыдущую версию
2. Investigate issue
3. Fix и новый deploy

```bash
# Быстрый rollback
kubectl rollout undo deployment/sl-back

# Rollback на конкретную версию
kubectl rollout undo deployment/sl-back --to-revision=2
```

### 14.3. Database Migrations

**Правила:**

- Миграции всегда forward-compatible
- Тестировать rollback
- Делать в off-peak hours
- Backup перед миграцией

```bash
# Запуск миграций
npm run migrate:latest

# Rollback последней миграции
npm run migrate:rollback

# Проверка статуса
npm run migrate:status
```

---

## 15. Changelog

| Версия | Дата       | Изменения               | Автор          |
|--------|------------|-------------------------|----------------|
| 1.0    | 2026-01-27 | Первая версия документа | Юрий Шульдешов |

---

## 16. Заключение

Этот документ - **живой**. Он должен обновляться по мере роста проекта и появления новых best practices.

**Как предложить изменения:**

1. Создать MR с изменениями в этом документе
2. Обсудить с командой
3. После approve - мерджить

**Вопросы и обсуждения:**

- GitLab Issues
- Slack канал #tech-standards
- Tech meetings

---

**Следующее обновление:** По мере необходимости  
**Ответственный за документ:** Юрий Шульдешов (Tech Lead)

# Гайд по написанию интеграционных тестов (Node.js/Jest)

**Целевой KPI: 60-70% критических сценариев покрыты**  
**Стек:** Node.js + Jest + PostgreSQL  
**Дата:** 10 февраля 2026  
**Версия:** 1.0

---

## 1. Введение

### Зачем нужны интеграционные тесты

**Интеграционные тесты** проверяют, как компоненты системы работают **вместе** с реальными зависимостями (БД, API, файловая система).

**Отличия от юнит-тестов:**

| Критерий | Юнит-тесты | Интеграционные тесты |
|----------|------------|---------------------|
| **Что тестируют** | Одну функцию в изоляции | Несколько компонентов вместе |
| **Зависимости** | Моки и стабы | Реальные БД, API, файлы |
| **Скорость** | <100ms | 1-10s на тест |
| **Количество** | Много (сотни) | Меньше (десятки) |
| **Цель** | Корректность логики | Работа интеграций |
| **Когда падают** | При изменении кода | При проблемах инфраструктуры |

### Что покрывают интеграционные тесты

**Критические сценарии:**
- API endpoints (запрос → БД → ответ)
- Работа с базой данных (сложные SQL запросы)
- Взаимодействие сервисов
- Обработка данных end-to-end
- Внешние API (с тестовыми учетными записями)

**НЕ покрывают:**
- Бизнес-логику (это юнит-тесты)
- Граничные случаи и валидации (это юнит-тесты)
- UI/UX (это E2E тесты)

### Связь с CI/CD

```bash
# Локально (быстро, без БД)
npm test

# CI/CD (полные, с тестовой БД)
npm run test:integration
```

**CI пайплайн:**
1. Поднять тестовую БД (PostgreSQL/ClickHouse в Docker)
2. Применить миграции
3. Заполнить тестовыми данными
4. Запустить интеграционные тесты
5. Очистить БД
6. **Если тесты упали** — сборка не проходит

---

## 2. Архитектура тестов в проекте

### Текущая структура

```
mp-back/
├── src/
│   ├── datasets/
│   │   └── wb/
│   │       └── wbReportsData_v1/
│   │           ├── wbReportsData_v1.js          # Код
│   │           └── wbReportsData_v1.test.js     # Интеграционный тест
│   ├── utils/
│   │   ├── test.utils.js                        # Утилиты для тестов
│   │   └── validator.test.js                    # Юнит-тест
│   └── tests/
│       └── jest.teardown.js                     # Очистка после тестов
└── jest.config.mjs                               # Конфигурация Jest
```

### Паттерн: Тесты рядом с кодом

В проекте используется подход **"тесты рядом с кодом"**:

```
src/datasets/wb/wbReportsData_v1/
├── wbReportsData_v1.js       # Код датасета
└── wbReportsData_v1.test.js  # Интеграционный тест (использует реальную БД)
```

**Преимущества:**
- Легко найти тесты для модуля
- Тесты не забываются при рефакторинге
- Понятно что покрыто, а что нет

---

## 3. Практика: Как писать интеграционные тесты

### Структура интеграционного теста

```javascript
import { describe, test, expect, beforeAll, afterAll } from "@jest/globals";
import DatasetsService from "#datasets/datasets.service.js";
import { prepareWbDatasetsTestConfig } from "#utils/test.utils.js";
import cleanupService from "#common/cleanup.service.js";

const method = "wbReportsData_v1";
let client_id;
let date_from;

// Setup: подготовка окружения ОДИН РАЗ для всех тестов
describe("check config", () => {
    test("check config data", async () => {
        const config = await prepareWbDatasetsTestConfig();
        client_id = config.client_id;
        date_from = config.date_from;
        
        expect(config).toHaveProperty("client_id");
        expect(config.all_nm_ids).not.toHaveLength(0);
    });
});

// Тесты: проверка реальной работы с БД
describe(`check ${method} dataset`, () => {
    test("should return data without params", async () => {
        const data = await DatasetsService.datasets[method]({ client_id });
        
        expect(data).toBeInstanceOf(Array);
        expect(data).not.toHaveLength(0);
    });
    
    test("should filter by date_from", async () => {
        const data = await DatasetsService.datasets[method]({ 
            client_id, 
            date_from 
        });
        
        expect(data).toBeInstanceOf(Array);
        expect(data.every(d => d.rr_dt >= date_from)).toBe(true);
    });
});

// Cleanup: очистка после тестов
describe("cleanup", () => {
    test("cleanup", async () => {
        await cleanupService.cleanup({ client_id });
    });
});
```

### Паттерн: Arrange (Setup) - Act - Assert - Cleanup

```javascript
describe("User API Integration", () => {
    let testUserId;
    let testDb;
    
    // ARRANGE: подготовка окружения
    beforeAll(async () => {
        testDb = await setupTestDatabase();
    });
    
    // ACT + ASSERT: тесты
    test("should create user via API", async () => {
        const response = await request(app)
            .post("/api/users")
            .send({ name: "John", email: "john@test.com" });
        
        expect(response.status).toBe(201);
        expect(response.body).toHaveProperty("id");
        
        testUserId = response.body.id;
    });
    
    test("should retrieve created user", async () => {
        const response = await request(app)
            .get(`/api/users/${testUserId}`);
        
        expect(response.status).toBe(200);
        expect(response.body.name).toBe("John");
    });
    
    // CLEANUP: очистка
    afterAll(async () => {
        await testDb.query(`DELETE FROM users WHERE id = ${testUserId}`);
        await testDb.close();
    });
});
```

---

## 4. Работа с тестовой БД

### Подход 1: Использование production схемы с тестовыми данными

```javascript
// test.utils.js - утилиты для подготовки тестовых данных
async function prepareWbDatasetsTestConfig() {
    // Использует реальную БД с тестовым клиентом
    let client_id = process.env.TEST_USER_ID ? 
        parseInt(process.env.TEST_USER_ID) : null;
    
    if (!client_id) {
        const jestUser = await usersService.findOne({ email: "jest" });
        const jestClient = await clientsService.findOne({ id: jestUser.id });
        client_id = jestClient.id;
    }
    
    const date_from = new Date(Date.now() - 30 * 24 * 60 * 60 * 1000)
        .toISOString().split("T")[0];
    
    return { client_id, date_from };
}
```

**Преимущества:**
- Используется реальная схема БД
- Тестовые данные близки к production
- Проверяются реальные SQL запросы

**Недостатки:**
- Требуется настройка тестового клиента
- Медленнее, чем моки
- Зависимость от состояния БД

### Подход 2: In-memory БД (SQLite)

```javascript
// test.setup.js
import sqlite3 from 'sqlite3';
import { open } from 'sqlite';

export async function setupTestDatabase() {
    const db = await open({
        filename: ':memory:',
        driver: sqlite3.Database
    });
    
    // Создать схему
    await db.exec(`
        CREATE TABLE users (
            id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            email TEXT UNIQUE
        );
    `);
    
    return db;
}

// test
describe("Users Integration", () => {
    let db;
    
    beforeAll(async () => {
        db = await setupTestDatabase();
    });
    
    afterAll(async () => {
        await db.close();
    });
    
    test("should insert and retrieve user", async () => {
        await db.run("INSERT INTO users (name, email) VALUES (?, ?)", 
            ["John", "john@test.com"]);
        
        const user = await db.get("SELECT * FROM users WHERE email = ?", 
            ["john@test.com"]);
        
        expect(user.name).toBe("John");
    });
});
```

### Подход 3: Тестовая БД в Docker

```javascript
// jest.config.mjs
export default {
    globalSetup: './tests/setup.js',
    globalTeardown: './tests/teardown.js'
};

// tests/setup.js
export default async () => {
    // Запустить PostgreSQL в Docker
    await exec('docker-compose -f docker-compose.test.yml up -d postgres');
    
    // Подождать готовности
    await waitForDatabase();
    
    // Применить миграции
    await exec('npm run migrate:test');
    
    // Заполнить тестовыми данными
    await exec('npm run seed:test');
};

// tests/teardown.js
export default async () => {
    // Остановить БД
    await exec('docker-compose -f docker-compose.test.yml down');
};
```

---

## 5. Примеры: Хорошо и Плохо

### Пример 1: Тестирование датасета (интеграционный)

**Хорошо:**

```javascript
describe("wbReportsData_v1 dataset", () => {
    let client_id;
    let date_from;
    let all_nm_ids;
    
    beforeAll(async () => {
        const config = await prepareWbDatasetsTestConfig();
        client_id = config.client_id;
        date_from = config.date_from;
        all_nm_ids = config.all_nm_ids;
    });
    
    test("should return data without params", async () => {
        const data = await DatasetsService.datasets.wbReportsData_v1({ 
            client_id 
        });
        
        expect(data).toBeInstanceOf(Array);
        expect(data).not.toHaveLength(0);
    });
    
    test("should filter by nm_ids", async () => {
        const nm_ids = [all_nm_ids[0], all_nm_ids[1]];
        
        const data = await DatasetsService.datasets.wbReportsData_v1({ 
            client_id, 
            nm_ids 
        });
        
        expect(data).toBeInstanceOf(Array);
        expect(data.every(d => nm_ids.includes(d.nm_id))).toBe(true);
    });
    
    test("should filter by date_from", async () => {
        const data = await DatasetsService.datasets.wbReportsData_v1({ 
            client_id, 
            date_from 
        });
        
        expect(data.every(d => d.rr_dt >= date_from)).toBe(true);
    });
    
    afterAll(async () => {
        await cleanupService.cleanup({ client_id });
    });
});
```

**Плохо:**

```javascript
// Проблемы:
// 1. Нет setup/cleanup
// 2. Хардкод client_id (может не существовать в БД)
// 3. Нет проверки реальных данных
// 4. Использует production БД напрямую

test("test dataset", async () => {
    const data = await DatasetsService.datasets.wbReportsData_v1({ 
        client_id: 12345 // может не существовать!
    });
    expect(data).toBeTruthy();
});
```

### Пример 2: Тестирование API endpoint

**Хорошо:**

```javascript
import request from 'supertest';
import app from '#main/app.js';

describe("POST /api/users", () => {
    let testUserId;
    
    test("should create new user", async () => {
        const response = await request(app)
            .post("/api/users")
            .send({ 
                name: "Test User", 
                email: "test@example.com" 
            })
            .expect(201);
        
        expect(response.body).toHaveProperty("id");
        expect(response.body.name).toBe("Test User");
        
        testUserId = response.body.id;
    });
    
    test("should return 400 for duplicate email", async () => {
        await request(app)
            .post("/api/users")
            .send({ 
                name: "Another User", 
                email: "test@example.com" 
            })
            .expect(400);
    });
    
    test("should return 400 for missing required fields", async () => {
        const response = await request(app)
            .post("/api/users")
            .send({ name: "No Email" })
            .expect(400);
        
        expect(response.body).toHaveProperty("error");
    });
    
    afterAll(async () => {
        // Очистка тестовых данных
        await db.query("DELETE FROM users WHERE id = $1", [testUserId]);
    });
});

describe("GET /api/users/:id", () => {
    let testUserId;
    
    beforeAll(async () => {
        // Создать тестового пользователя
        const result = await db.query(
            "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id",
            ["Test User", "get-test@example.com"]
        );
        testUserId = result.rows[0].id;
    });
    
    test("should return user by id", async () => {
        const response = await request(app)
            .get(`/api/users/${testUserId}`)
            .expect(200);
        
        expect(response.body.name).toBe("Test User");
        expect(response.body.email).toBe("get-test@example.com");
    });
    
    test("should return 404 for non-existent user", async () => {
        await request(app)
            .get("/api/users/999999")
            .expect(404);
    });
    
    afterAll(async () => {
        await db.query("DELETE FROM users WHERE id = $1", [testUserId]);
    });
});
```

### Пример 3: Тестирование с ClickHouse

**Хорошо (из проекта):**

```javascript
describe('ClickHouse Modular Architecture Integration', () => {
    let connection;
    let logger;
    
    beforeAll(async () => {
        // Подключиться к тестовой БД
        connection = new ClickHouseConnection({
            host: process.env.CLICKHOUSE_HOST || 'localhost',
            port: 8123,
            database: 'wb'
        });
        
        await connection.connect();
        logger = new Logger({ level: 'info' });
    });
    
    afterAll(async () => {
        if (connection) {
            await connection.close();
        }
    });
    
    test('should connect to ClickHouse', async () => {
        const result = await connection.query('SELECT 1 as test');
        
        expect(result).toHaveLength(1);
        expect(result[0].test).toBe(1);
    });
    
    test('should execute full report with all modules', async () => {
        const orchestrator = new ReportOrchestrator({ connection, logger });
        
        orchestrator.registerModule(new UsidsModule());
        orchestrator.registerModule(new AdvertisingModule());
        orchestrator.registerModule(new StorageModule());
        
        const result = await orchestrator.execute({
            client_id: 999,
            date_from: '2025-11-24',
            date_to: '2025-11-25'
        });
        
        expect(result).toBeDefined();
        expect(Array.isArray(result)).toBe(true);
        expect(result.length).toBeGreaterThan(0);
        
        // Проверить структуру
        const firstRow = result[0];
        expect(firstRow).toHaveProperty('nm_id');
        expect(firstRow).toHaveProperty('rr_dt');
        expect(firstRow).toHaveProperty('sid');
    });
    
    test('should cleanup temp tables after execution', async () => {
        const orchestrator = new ReportOrchestrator({ connection, logger });
        orchestrator.registerModule(new UsidsModule());
        
        await orchestrator.execute({
            client_id: 999,
            date_from: '2025-11-24',
            date_to: '2025-11-25'
        });
        
        // Проверить что временные таблицы удалены
        const tempTables = await connection.query(`
            SELECT name FROM system.tables 
            WHERE is_temporary = 1 AND name LIKE 'temp_%'
        `);
        
        expect(tempTables).toHaveLength(0);
    });
});
```

---

## 6. Тестирование датасетов (специфика проекта)

### Паттерн тестирования датасетов

Датасеты — это SQL-запросы, которые возвращают аналитические данные. Тестируем:

1. **Запрос выполняется без ошибок**
2. **Возвращает непустой результат**
3. **Фильтры работают корректно** (nm_ids, date_from, date_to)
4. **Структура данных корректна**

**Шаблон теста датасета:**

```javascript
import { describe, expect, test } from "@jest/globals";
import DatasetsService from "#datasets/datasets.service.js";
import { prepareWbDatasetsTestConfig, extractRandomValues } from "#utils/test.utils.js";
import cleanupService from "#common/cleanup.service.js";

const method = "wb10xSalesReport_v1";
const dateKey = "rr_dt"; // ключ для даты в результате
let client_id, date_from, all_nm_ids, nm_ids;

describe("check config", () => {
    test("check config data", async () => {
        const config = await prepareWbDatasetsTestConfig();
        
        client_id = config.client_id;
        date_from = config.date_from;
        all_nm_ids = config.all_nm_ids;
        nm_ids = config.nm_ids;
        
        expect(client_id).toBeDefined();
        expect(all_nm_ids).toBeInstanceOf(Array);
        expect(all_nm_ids).not.toHaveLength(0);
    });
});

describe(`${method} dataset`, () => {
    test("without params should return all data", async () => {
        const data = await DatasetsService.datasets[method]({ client_id });
        
        expect(data).toBeInstanceOf(Array);
        expect(data).not.toHaveLength(0);
    });
    
    test("with nm_ids should filter correctly", async () => {
        const testNmIds = extractRandomValues(all_nm_ids, 3);
        const data = await DatasetsService.datasets[method]({ 
            client_id, 
            nm_ids: testNmIds 
        });
        
        expect(data).toBeInstanceOf(Array);
        expect(data.filter(d => testNmIds.includes(d.nm_id))).not.toHaveLength(0);
        expect(data.filter(d => !testNmIds.includes(d.nm_id))).toHaveLength(0);
    });
    
    test("with date_from should filter by date", async () => {
        const data = await DatasetsService.datasets[method]({ 
            client_id, 
            date_from 
        });
        
        expect(data).toBeInstanceOf(Array);
        expect(data.filter(d => d[dateKey] >= date_from)).not.toHaveLength(0);
        expect(data.filter(d => d[dateKey] < date_from)).toHaveLength(0);
    });
    
    test("with nm_ids and date_from should apply both filters", async () => {
        const data = await DatasetsService.datasets[method]({ 
            client_id, 
            nm_ids, 
            date_from 
        });
        
        expect(data).toBeInstanceOf(Array);
        expect(data.every(d => nm_ids.includes(d.nm_id))).toBe(true);
        expect(data.every(d => d[dateKey] >= date_from)).toBe(true);
    });
});

describe("cleanup", () => {
    test("cleanup", async () => {
        await cleanupService.cleanup({ client_id });
    });
});
```

---

## 7. Управление тестовыми данными

### Стратегия 1: Фиксированный тестовый клиент

**Подготовка:**

```sql
-- Создать тестового пользователя и клиента
INSERT INTO users (email, name) VALUES ('jest', 'Jest Test User');
INSERT INTO clients (id, name) VALUES (999, 'Test Client');

-- Создать тестовые данные
INSERT INTO wb_reports (client_id, nm_id, date, ...) VALUES 
    (999, 12345, '2025-01-01', ...),
    (999, 12346, '2025-01-02', ...);
```

**В тестах:**

```javascript
const TEST_CLIENT_ID = 999;

test("should work with test client", async () => {
    const data = await DatasetsService.datasets.wbReportsData_v1({ 
        client_id: TEST_CLIENT_ID 
    });
    expect(data).not.toHaveLength(0);
});
```

### Стратегия 2: Динамическое создание данных

```javascript
describe("User CRUD Integration", () => {
    let testUserId;
    
    beforeAll(async () => {
        // Создать тестовые данные
        const result = await db.query(
            "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING id",
            ["Test User", `test-${Date.now()}@example.com`]
        );
        testUserId = result.rows[0].id;
    });
    
    test("should read created user", async () => {
        const user = await usersService.findById(testUserId);
        expect(user.name).toBe("Test User");
    });
    
    afterAll(async () => {
        // Удалить тестовые данные
        await db.query("DELETE FROM users WHERE id = $1", [testUserId]);
    });
});
```

### Стратегия 3: Транзакции (rollback после теста)

```javascript
describe("Transaction-based tests", () => {
    let client;
    
    beforeEach(async () => {
        client = await db.connect();
        await client.query('BEGIN'); // Начать транзакцию
    });
    
    afterEach(async () => {
        await client.query('ROLLBACK'); // Откатить изменения
        client.release();
    });
    
    test("should create user", async () => {
        await client.query(
            "INSERT INTO users (name) VALUES ($1)",
            ["Test User"]
        );
        
        const result = await client.query(
            "SELECT * FROM users WHERE name = $1",
            ["Test User"]
        );
        
        expect(result.rows).toHaveLength(1);
        // После теста все откатится автоматически!
    });
});
```

---

## 8. Тестирование асинхронных процессов

### Background jobs (BullMQ, Queue)

```javascript
describe("Background Job Integration", () => {
    let queue;
    let worker;
    
    beforeAll(async () => {
        queue = new Queue('test-queue');
        worker = new Worker('test-queue', async job => {
            // Обработчик задачи
            return { processed: true, data: job.data };
        });
    });
    
    afterAll(async () => {
        await worker.close();
        await queue.close();
    });
    
    test("should process job successfully", async () => {
        // Добавить задачу в очередь
        const job = await queue.add('test-job', { userId: 123 });
        
        // Подождать выполнения
        const result = await job.waitUntilFinished();
        
        expect(result.processed).toBe(true);
        expect(result.data.userId).toBe(123);
    });
    
    test("should retry failed jobs", async () => {
        let attempts = 0;
        
        const failingWorker = new Worker('test-queue', async job => {
            attempts++;
            if (attempts < 3) {
                throw new Error("Temporary failure");
            }
            return { success: true };
        }, { 
            attempts: 3 
        });
        
        const job = await queue.add('failing-job', {});
        const result = await job.waitUntilFinished();
        
        expect(result.success).toBe(true);
        expect(attempts).toBe(3);
        
        await failingWorker.close();
    });
});
```

### NATS messaging

```javascript
describe("NATS Integration", () => {
    let nc;
    
    beforeAll(async () => {
        nc = await connect({ 
            servers: process.env.NATS_URL || 'localhost:4222' 
        });
    });
    
    afterAll(async () => {
        await nc.close();
    });
    
    test("should publish and receive message", async () => {
        const subject = 'test.subject';
        const receivedMessages = [];
        
        // Подписаться на сообщения
        const sub = nc.subscribe(subject);
        (async () => {
            for await (const msg of sub) {
                receivedMessages.push(msg.string());
                if (receivedMessages.length >= 1) break;
            }
        })();
        
        // Отправить сообщение
        nc.publish(subject, 'test message');
        
        // Подождать получения
        await new Promise(resolve => setTimeout(resolve, 100));
        
        expect(receivedMessages).toHaveLength(1);
        expect(receivedMessages[0]).toBe('test message');
    });
});
```

---

## 9. Оптимизация производительности тестов

### Проблема: Медленные интеграционные тесты

**Признаки:**
- Все тесты выполняются >2 минут
- Каждый тест создает/удаляет данные в БД
- Много повторяющихся setup операций

### Решение 1: Переиспользование тестовых данных

```javascript
// Плохо: создаем данные в каждом тесте
describe("Users API", () => {
    test("test 1", async () => {
        const user = await createTestUser(); // медленно!
        // ...
        await deleteTestUser(user.id);
    });
    
    test("test 2", async () => {
        const user = await createTestUser(); // медленно!
        // ...
        await deleteTestUser(user.id);
    });
});

// Хорошо: создаем данные один раз для всех тестов
describe("Users API", () => {
    let testUserId;
    
    beforeAll(async () => {
        const user = await createTestUser(); // один раз!
        testUserId = user.id;
    });
    
    afterAll(async () => {
        await deleteTestUser(testUserId); // один раз!
    });
    
    test("test 1", async () => {
        const user = await getUser(testUserId); // быстро!
        // ...
    });
    
    test("test 2", async () => {
        const user = await getUser(testUserId); // быстро!
        // ...
    });
});
```

### Решение 2: Параллельное выполнение (где возможно)

```javascript
// jest.config.mjs
export default {
    maxWorkers: 4, // 4 параллельных процесса
    // ИЛИ
    maxWorkers: '50%' // 50% CPU
};

// Для тестов БД — использовать разные схемы/БД
test("parallel test 1", async () => {
    const db = await getTestDb('test_db_1'); // отдельная БД
    // ...
});

test("parallel test 2", async () => {
    const db = await getTestDb('test_db_2'); // другая БД
    // ...
});
```

### Решение 3: Использовать in-memory БД для простых тестов

```javascript
// Медленно: реальная PostgreSQL (50-100ms на запрос)
test("slow test", async () => {
    const result = await pgDb.query("SELECT * FROM users");
    // ...
});

// Быстро: in-memory SQLite (1-5ms на запрос)
test("fast test", async () => {
    const result = await sqliteDb.all("SELECT * FROM users");
    // ...
});
```

---

## 10. Изоляция тестов

### Проблема: Тесты влияют друг на друга

```javascript
// Плохо: тесты не изолированы
describe("Users", () => {
    test("create user John", async () => {
        await db.query("INSERT INTO users (name) VALUES ('John')");
        const users = await db.query("SELECT * FROM users");
        expect(users.rows).toHaveLength(1);
    });
    
    test("create user Jane", async () => {
        await db.query("INSERT INTO users (name) VALUES ('Jane')");
        const users = await db.query("SELECT * FROM users");
        expect(users.rows).toHaveLength(1); // УПАДЕТ! John уже есть
    });
});
```

### Решение: Cleanup между тестами

```javascript
// Хорошо: каждый тест начинается с чистой БД
describe("Users", () => {
    beforeEach(async () => {
        await db.query("TRUNCATE TABLE users CASCADE");
    });
    
    test("create user John", async () => {
        await db.query("INSERT INTO users (name) VALUES ('John')");
        const users = await db.query("SELECT * FROM users");
        expect(users.rows).toHaveLength(1);
    });
    
    test("create user Jane", async () => {
        await db.query("INSERT INTO users (name) VALUES ('Jane')");
        const users = await db.query("SELECT * FROM users");
        expect(users.rows).toHaveLength(1); // Работает!
    });
});
```

### Альтернатива: Уникальные данные

```javascript
describe("Users", () => {
    test("create user John", async () => {
        await db.query("INSERT INTO users (name, email) VALUES ($1, $2)", 
            ["John", `john-${Date.now()}@test.com`]); // уникальный email
        // ...
    });
    
    test("create user Jane", async () => {
        await db.query("INSERT INTO users (name, email) VALUES ($1, $2)", 
            ["Jane", `jane-${Date.now()}@test.com`]); // уникальный email
        // ...
    });
});
```

---

## 11. Обработка ошибок и граничных случаев

### Тестирование ошибок БД

```javascript
describe("Error handling", () => {
    test("should handle database connection error", async () => {
        // Временно отключить БД
        await db.destroy();
        
        await expect(
            DatasetsService.datasets.wbReportsData_v1({ client_id: 999 })
        ).rejects.toThrow();
        
        // Восстановить подключение
        await db.connect();
    });
    
    test("should handle SQL syntax error", async () => {
        await expect(
            db.query("INVALID SQL SYNTAX")
        ).rejects.toThrow();
    });
    
    test("should handle constraint violation", async () => {
        await db.query("INSERT INTO users (id, name) VALUES (1, 'John')");
        
        // Попытка вставить дубликат
        await expect(
            db.query("INSERT INTO users (id, name) VALUES (1, 'Jane')")
        ).rejects.toThrow(/duplicate key/i);
    });
});
```

### Тестирование timeout'ов

```javascript
test("should timeout on slow query", async () => {
    const slowQuery = db.query(
        "SELECT pg_sleep(10)", // 10 секунд
        { timeout: 1000 } // timeout 1 секунда
    );
    
    await expect(slowQuery).rejects.toThrow(/timeout/i);
});
```

---

## 12. CI/CD интеграция

### Конфигурация для CI (.gitlab-ci.yml)

```yaml
test:integration:
  stage: test
  services:
    - postgres:14
  variables:
    POSTGRES_DB: test_db
    POSTGRES_USER: test_user
    POSTGRES_PASSWORD: test_pass
  before_script:
    - npm ci
    - npm run migrate:test
    - npm run seed:test
  script:
    - npm run test:integration
  coverage: '/All files[^|]*\|[^|]*\s+([\d\.]+)/'
  artifacts:
    reports:
      coverage_report:
        coverage_format: cobertura
        path: coverage/cobertura-coverage.xml
```

### Environment variables для тестов

```javascript
// .env.test
TEST_USER_ID=999
TEST_DATABASE_URL=postgresql://test_user:test_pass@localhost:5432/test_db
NODE_ENV=test
```

```javascript
// Использование в тестах
beforeAll(async () => {
    const dbUrl = process.env.TEST_DATABASE_URL;
    testDb = await connectToDatabase(dbUrl);
});
```

---

## 13. Паттерны и best practices

### Паттерн 1: Test Fixtures (фикстуры)

```javascript
// tests/fixtures/users.js
export const testUsers = {
    admin: {
        id: 1,
        name: "Admin User",
        email: "admin@test.com",
        role: "admin"
    },
    regular: {
        id: 2,
        name: "Regular User",
        email: "user@test.com",
        role: "user"
    }
};

// tests/users.integration.test.js
import { testUsers } from './fixtures/users.js';

test("should create admin user", async () => {
    const user = await usersService.create(testUsers.admin);
    expect(user.role).toBe("admin");
});
```

### Паттерн 2: Test Helpers

```javascript
// tests/helpers/db.helper.js
export async function createTestUser(overrides = {}) {
    const defaultUser = {
        name: "Test User",
        email: `test-${Date.now()}@example.com`
    };
    
    const userData = { ...defaultUser, ...overrides };
    const result = await db.query(
        "INSERT INTO users (name, email) VALUES ($1, $2) RETURNING *",
        [userData.name, userData.email]
    );
    
    return result.rows[0];
}

export async function deleteTestUser(userId) {
    await db.query("DELETE FROM users WHERE id = $1", [userId]);
}

// Использование
test("should work with test user", async () => {
    const user = await createTestUser({ name: "Custom Name" });
    // ...
    await deleteTestUser(user.id);
});
```

### Паттерн 3: Page Object Pattern (для API)

```javascript
// tests/api/users.api.js
class UsersAPI {
    constructor(app) {
        this.app = app;
    }
    
    async create(userData) {
        return request(this.app)
            .post("/api/users")
            .send(userData);
    }
    
    async getById(id) {
        return request(this.app)
            .get(`/api/users/${id}`);
    }
    
    async delete(id) {
        return request(this.app)
            .delete(`/api/users/${id}`);
    }
}

// Использование в тестах
describe("Users API", () => {
    const api = new UsersAPI(app);
    
    test("should create and retrieve user", async () => {
        const createResp = await api.create({ name: "John" });
        expect(createResp.status).toBe(201);
        
        const getResp = await api.getById(createResp.body.id);
        expect(getResp.status).toBe(200);
        expect(getResp.body.name).toBe("John");
    });
});
```

---

## 14. Когда использовать интеграционные тесты

### Используйте интеграционные тесты для:

- **Критические бизнес-сценарии** — создание заказа, обработка платежа
- **Сложные SQL запросы** — датасеты с JOIN'ами и агрегациями
- **API endpoints** — проверка полного цикла запрос → БД → ответ
- **Репликация данных** — PostgreSQL → ClickHouse
- **Миграции БД** — проверка что схема применилась корректно

### НЕ используйте интеграционные тесты для:

- **Валидация входных данных** — это юнит-тесты
- **Вычисления и форматирование** — это юнит-тесты
- **Граничные случаи** — это юнит-тесты
- **Простые CRUD операции** — избыточно, если есть юнит-тесты

**Правило 80/20:** 80% юнит-тестов (быстрые), 20% интеграционных (критичные сценарии).

---

## 15. Требования к интеграционным тестам

### Обязательные требования

1. **Изоляция:** Каждый тест не должен влиять на другие
2. **Идемпотентность:** Тест можно запустить несколько раз подряд
3. **Cleanup:** После теста БД возвращается в исходное состояние
4. **Скорость:** Один тест <10s, все интеграционные <5 минут
5. **Стабильность:** Не зависят от времени суток, случайных данных

### Целевое покрытие

**Интеграционными тестами покрывают:**
- 60-70% критических API endpoints
- 50-60% сложных датасетов
- 100% основных бизнес-процессов (happy path)

**НЕ требуется 100% покрытие** — это медленно и избыточно.

---

## 16. Чеклист для Pull Request

Перед созданием PR проверьте:

### Интеграционные тесты

- [ ] **Критические сценарии покрыты** — новые API endpoints имеют интеграционные тесты
- [ ] **Тесты используют тестовую БД** — не production!
- [ ] **Cleanup работает** — тестовые данные удаляются после выполнения
- [ ] **Тесты стабильны** — запустите 3 раза подряд, все должны пройти
- [ ] **Тесты быстрые** — все интеграционные <5 минут
- [ ] **Изоляция соблюдена** — тесты не зависят друг от друга

### CI/CD

- [ ] **Тесты проходят в CI** — проверьте логи pipeline
- [ ] **Нет hard-coded значений** — используются environment variables
- [ ] **БД поднимается автоматически** — через docker-compose или services

---

## 17. Команды для запуска

```bash
# Все тесты (юнит + интеграционные)
npm test

# Только интеграционные (медленные)
npm run test:integration

# С покрытием
npm test -- --coverage

# Конкретный файл
npm test -- src/datasets/wb/wbReportsData_v1/wbReportsData_v1.test.js

# Последовательный запуск (для дебага)
npm run test:sync

# Watch mode (перезапуск при изменении)
npm test -- --watch

# С verbose логами
npm test -- --verbose
```

---

## 18. Частые ошибки

### Ошибка 1: Забыли cleanup

```javascript
// Плохо: данные остаются в БД после теста
test("should create user", async () => {
    await db.query("INSERT INTO users (name) VALUES ('John')");
    const users = await db.query("SELECT * FROM users");
    expect(users.rows).toHaveLength(1);
    // Нет cleanup! John останется в БД
});

// Хорошо: cleanup в afterAll
describe("Users", () => {
    let testUserId;
    
    test("should create user", async () => {
        const result = await db.query(
            "INSERT INTO users (name) VALUES ('John') RETURNING id"
        );
        testUserId = result.rows[0].id;
        expect(testUserId).toBeDefined();
    });
    
    afterAll(async () => {
        await db.query("DELETE FROM users WHERE id = $1", [testUserId]);
    });
});
```

### Ошибка 2: Hard-coded данные

```javascript
// Плохо: тест упадет если client_id не существует
test("should get reports", async () => {
    const data = await DatasetsService.datasets.wbReportsData_v1({ 
        client_id: 12345 // может не существовать!
    });
    expect(data).not.toHaveLength(0);
});

// Хорошо: используем динамическую конфигурацию
describe("Reports", () => {
    let client_id;
    
    beforeAll(async () => {
        const config = await prepareWbDatasetsTestConfig();
        client_id = config.client_id;
    });
    
    test("should get reports", async () => {
        const data = await DatasetsService.datasets.wbReportsData_v1({ 
            client_id 
        });
        expect(data).not.toHaveLength(0);
    });
});
```

### Ошибка 3: Тесты зависят от порядка выполнения

```javascript
// Плохо: test2 зависит от test1
let sharedUserId;

test("test1: create user", async () => {
    const result = await createUser({ name: "John" });
    sharedUserId = result.id;
});

test("test2: update user", async () => {
    // Упадет если test1 не выполнился или выполнился с ошибкой!
    await updateUser(sharedUserId, { name: "Jane" });
});

// Хорошо: test2 независим
test("should update user", async () => {
    // Создаем свои тестовые данные
    const user = await createUser({ name: "John" });
    
    // Тестируем update
    await updateUser(user.id, { name: "Jane" });
    
    const updated = await getUser(user.id);
    expect(updated.name).toBe("Jane");
    
    // Cleanup
    await deleteUser(user.id);
});
```

---

## 19. Примеры из проекта

### Пример 1: Тестирование датасета с фильтрами

Из `wbReportsData_v1.test.js`:

```javascript
describe("wbReportsData_v1 dataset", () => {
    let client_id, date_from, all_nm_ids, nm_ids;
    
    beforeAll(async () => {
        const config = await prepareWbDatasetsTestConfig();
        client_id = config.client_id;
        date_from = config.date_from;
        all_nm_ids = config.all_nm_ids;
        nm_ids = config.nm_ids;
    });
    
    test("without params", async () => {
        const data = await DatasetsService.datasets.wbReportsData_v1({ 
            client_id 
        });
        expect(data).toBeInstanceOf(Array);
        expect(data).not.toHaveLength(0);
    });
    
    test("with nm_ids filter", async () => {
        const testNmIds = extractRandomValues(all_nm_ids, 3);
        const data = await DatasetsService.datasets.wbReportsData_v1({ 
            client_id, 
            nm_ids: testNmIds 
        });
        
        expect(data.every(d => testNmIds.includes(d.nm_id))).toBe(true);
    });
    
    afterAll(async () => {
        await cleanupService.cleanup({ client_id });
    });
});
```

### Пример 2: Тестирование сложного датасета с множественными фильтрами

```javascript
describe('wb10xSalesReport_v1 dataset', () => {
    let client_id, date_from, date_to, all_nm_ids;
    
    beforeAll(async () => {
        const config = await prepareWbDatasetsTestConfig();
        client_id = config.client_id;
        date_from = config.date_from;
        date_to = config.date_to;
        all_nm_ids = config.all_nm_ids;
    });
    
    test('should return aggregated sales data', async () => {
        const data = await DatasetsService.datasets.wb10xSalesReport_v1({ 
            client_id,
            date_from,
            date_to
        });
        
        expect(data).toBeInstanceOf(Array);
        expect(data.length).toBeGreaterThan(0);
        
        // Проверить структуру данных
        const firstRow = data[0];
        expect(firstRow).toHaveProperty('nm_id');
        expect(firstRow).toHaveProperty('rr_dt');
        expect(firstRow).toHaveProperty('buyout_count');
        expect(firstRow).toHaveProperty('income_sum_rub');
    });
    
    test('should filter by nm_ids array', async () => {
        const testNmIds = extractRandomValues(all_nm_ids, 5);
        
        const data = await DatasetsService.datasets.wb10xSalesReport_v1({ 
            client_id,
            nm_ids: testNmIds,
            date_from,
            date_to
        });
        
        expect(data.every(d => testNmIds.includes(d.nm_id))).toBe(true);
    });
    
    afterAll(async () => {
        await cleanupService.cleanup({ client_id });
    });
});
```

---

## 20. Troubleshooting

### Проблема: Тесты падают локально, но проходят в CI

**Причины:**
- Разные версии Node.js
- Разные версии БД (PostgreSQL 13 vs 14)
- Разные environment variables
- Разное состояние БД

**Решение:**
```bash
# Использовать те же версии что в CI
nvm use 18.19.0

# Проверить переменные окружения
echo $TEST_DATABASE_URL

# Очистить БД перед тестами
npm run db:reset:test
```

### Проблема: Тесты проходят локально, но падают в CI

**Причины:**
- Тесты зависят от локальных данных
- Нет cleanup
- Используется production БД в CI

**Решение:**
- Убедитесь что тесты используют `process.env.TEST_DATABASE_URL`
- Добавьте cleanup в `afterAll()`
- Проверьте что CI использует тестовую БД

### Проблема: Интеграционные тесты очень медленные

**Решение:**
- Сократите количество тестов (только критичные)
- Переиспользуйте тестовые данные (beforeAll вместо beforeEach)
- Используйте `--runInBand` для последовательного выполнения (может быть быстрее)
- Рассмотрите in-memory БД для простых тестов

---

## 21. Итоговые рекомендации

### Стратегия тестирования

```
Пирамида тестов:
        /\        E2E тесты (1-5%) — самые медленные
       /  \       
      /____\      Интеграционные (15-20%) — медленные  
     /      \     
    /________\    Юнит-тесты (75-85%) — быстрые
```

**Для проекта:**
- **Юнит-тесты:** Валидация, вычисления, бизнес-логика (80% coverage)
- **Интеграционные:** Датасеты, API endpoints, работа с БД (60-70% критичных)
- **E2E:** Основные пользовательские сценарии (опционально)

### Что тестировать в первую очередь

**Приоритет 1 (обязательно):**
1. Критичные API endpoints (аутентификация, платежи)
2. Сложные датасеты (wb10xSalesFinReport, wbSeasonPlan)
3. Миграции данных между модулями

**Приоритет 2 (желательно):**
1. CRUD операции основных сущностей
2. Фильтрация и сортировка в датасетах
3. Background jobs (очереди, планировщики)

**Приоритет 3 (опционально):**
1. Редкие edge cases в API
2. Вспомогательные endpoints
3. Административные функции

### Процесс разработки с тестами

1. **Создание фичи:**
   - Написать юнит-тесты для логики
   - Написать 1-2 интеграционных теста для критичных сценариев
   - Запустить локально: `npm test`

2. **Code Review:**
   - Reviewer проверяет наличие тестов
   - Запускает тесты локально
   - Проверяет что coverage не упал

3. **CI/CD:**
   - Автоматический запуск всех тестов
   - Проверка coverage (должен быть >80%)
   - Блокировка merge при падении тестов

4. **После merge:**
   - Тесты становятся частью регрессионного набора
   - Защищают от будущих багов

---

## 22. Полезные ресурсы

**В проекте:**
- Конфигурация Jest: `jest.config.mjs`
- Test utils: `src/utils/test.utils.js`
- Cleanup service: `src/common/cleanup.service.js`
- Teardown: `src/tests/jest.teardown.js`
- Примеры: `src/datasets/**/*.test.js` (55+ файлов)

**Документация:**
- Jest: https://jestjs.io/docs/getting-started
- Supertest (API testing): https://github.com/ladjs/supertest
- Testing Best Practices: https://github.com/goldbergyoni/javascript-testing-best-practices

**Команды проекта:**
- `npm test` — все тесты
- `npm run test:sync` — последовательно
- `npm test -- --coverage` — с покрытием

---

**Помните:** Интеграционные тесты защищают от проблем интеграции, но не заменяют юнит-тесты. Используйте оба типа для полного покрытия!

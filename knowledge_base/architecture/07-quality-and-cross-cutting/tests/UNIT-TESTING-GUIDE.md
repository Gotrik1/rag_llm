# Гайд по написанию юнит-тестов (Node.js/Jest)

**Целевой KPI: 80% покрытия кода**  
**Стек:** Node.js + Jest  
**Дата:** 10 февраля 2026  
**Версия:** 1.0

---

## 1. Введение

### Зачем нужны юнит-тесты

**Юнит-тесты** — это автоматические проверки небольших изолированных частей кода (функций, методов, классов), которые:

- **Находят баги до production** — ошибки выявляются на этапе разработки
- **Упрощают рефакторинг** — можно уверенно менять код, зная, что тесты поймают регрессии
- **Документируют код** — тесты показывают, как должен работать код
- **Ускоряют разработку** — быстрая обратная связь вместо ручного тестирования
- **Повышают качество** — разработчики думают о граничных случаях

### Что считается юнит-тестом

**Юнит-тест:**

- Тестирует **один модуль** в изоляции (функция, класс, метод)
- **Быстро выполняется** (<100ms на тест)
- **Не использует внешние зависимости** (БД, API, файловая система)
- **Детерминирован** — всегда дает одинаковый результат
- Использует **моки/стабы** для внешних зависимостей

**НЕ юнит-тест (интеграционный):**

- Тестирует взаимодействие нескольких модулей
- Использует реальную БД, API, файлы
- Медленно выполняется (>1s)
- Зависит от состояния внешних систем

### Связь с CI/CD

В проекте настроен **Jest** для запуска тестов:

```bash
npm test              # запуск всех тестов
npm run test:sync     # запуск последовательно (для дебага)
```

**CI/CD пайплайн:**

1. Разработчик создает PR (Pull Request)
2. CI автоматически запускает `npm test`
3. **Если хотя бы 1 тест упал** — сборка не проходит, PR нельзя смержить
4. Code reviewer видит статус тестов в PR

**Правило:** Код без тестов или с падающими тестами не попадает в main/master ветку.

---

## 2. Практика: Как писать тесты

### Структура теста: Arrange–Act–Assert (AAA)

Каждый тест состоит из 3 частей:

```javascript
test("название теста, описывающее что проверяется", () => {
    // ARRANGE (подготовка) — создаем тестовые данные и настройки
    const input = { id: 1, name: "Test" };
    const expected = { success: true };
    
    // ACT (действие) — вызываем тестируемую функцию
    const result = myFunction(input);
    
    // ASSERT (проверка) — сравниваем результат с ожидаемым
    expect(result).toEqual(expected);
});
```

### Именование тестов

**Хорошо:**

```javascript
describe("calcMsUntilCycleRestart", () => {
    test("at 00:00 should return 115 minutes (restart at 01:55)", () => {
        // ...
    });
    
    test("should subtract seconds from ms", () => {
        // ...
    });
});
```

**Плохо:**

```javascript
describe("function", () => {
    test("test1", () => { /* непонятно что тестируется */ });
    test("works", () => { /* слишком общее */ });
});
```

**Правила:**

- `describe()` — название функции/модуля/класса
- `test()` или `it()` — что проверяет тест (поведение, граничный случай)
- Название должно читаться как документация
- Используйте "should" для описания ожидаемого поведения

### Изоляция тестов

Каждый тест должен быть **независимым**:

```javascript
describe("Validator", () => {
    // ✅ ХОРОШО: каждый тест создает свой валидатор
    test("should validate integer", () => {
        const validator = new Validator({ id: { type: "integer" } }, "test");
        const result = validator.validateObj({ id: 42 });
        expect(result).toEqual({ success: true });
    });
    
    test("should return error for string", () => {
        const validator = new Validator({ id: { type: "integer" } }, "test");
        const result = validator.validateObj({ id: "42" });
        expect(result.success).toBe(false);
    });
});
```

```javascript
// ❌ ПЛОХО: тесты зависят от общего состояния
let validator; // глобальная переменная

beforeAll(() => {
    validator = new Validator(schema); // создается один раз
});

test("test1", () => {
    validator.someState = true; // модифицирует состояние
    // ...
});

test("test2", () => {
    // этот тест сломается, если test1 изменил состояние!
    // ...
});
```

**Правило:** Используйте `beforeEach()` вместо `beforeAll()` для создания чистого состояния перед каждым тестом.

---

## 3. Основные паттерны

### Тестовые данные

**Фиксированные данные (hard-coded):**

```javascript
// ✅ ХОРОШО: для простых функций
test("should calculate sum", () => {
    const result = sum(2, 3);
    expect(result).toBe(5);
});
```

**Тестовые фикстуры (fixtures):**

```javascript
// ✅ ХОРОШО: для сложных объектов
const testSchema = {
    id: { required: true, type: "integer" },
    name: { required: false, type: "string", length: 10 }
};

test("should validate object", () => {
    const validator = new Validator(testSchema, "test");
    const testObj = { id: 1, name: "John" };
    
    const result = validator.validateObj(testObj);
    expect(result).toEqual({ success: true });
});
```

**Генерация данных:**

```javascript
// ✅ ХОРОШО: для тестирования с разными датами
test("at 01:00 should return 55 minutes", () => {
    const now = new Date("2024-01-01T01:00:00"); // фиксированная дата
    const result = calcMsUntilCycleRestart({ offsetMinutes: 5, now, cycleHours: 2 });
    expect(result.minutes).toBe(55);
});
```

### Моки (Mocks) и Стабы (Stubs)

**Мок** — заменяет внешнюю зависимость и проверяет, как она была вызвана.

```javascript
// ✅ ХОРОШО: мокаем БД для тестирования сервиса
test("should save user to database", async () => {
    // Arrange
    const mockDb = {
        insert: jest.fn().mockResolvedValue({ id: 1 })
    };
    const userService = new UserService(mockDb);
    
    // Act
    const result = await userService.createUser({ name: "John" });
    
    // Assert
    expect(mockDb.insert).toHaveBeenCalledWith({ name: "John" });
    expect(result).toEqual({ id: 1 });
});
```

**Стаб** — возвращает фиксированные данные, не проверяя вызовы.

```javascript
// ✅ ХОРОШО: стабим внешний API
test("should fetch user data", async () => {
    // Arrange
    const mockApi = {
        get: jest.fn().mockResolvedValue({ id: 1, name: "John" })
    };
    
    // Act
    const result = await fetchUser(1, mockApi);
    
    // Assert
    expect(result.name).toBe("John");
});
```

**Автоматические моки модулей:**

```javascript
// ✅ ХОРОШО: мокаем весь модуль
jest.mock("#db/connection.js", () => ({
    query: jest.fn().mockResolvedValue([{ id: 1 }])
}));

test("should query database", async () => {
    const result = await myService.getUsers();
    expect(result).toHaveLength(1);
});
```

### Тестирование асинхронного кода

```javascript
// ✅ ХОРОШО: используем async/await
test("should fetch data from API", async () => {
    const result = await fetchData();
    expect(result).toBeDefined();
});

// ✅ ХОРОШО: проверяем ошибки
test("should throw error on invalid input", async () => {
    await expect(fetchData(-1)).rejects.toThrow("Invalid ID");
});
```

---

## 4. Примеры: Хорошо и Плохо

### Пример 1: Тестирование утилитной функции

**✅ ХОРОШО:**

```javascript
describe("calcMsUntilCycleRestart", () => {
    test("at 00:00 should return 115 minutes (restart at 01:55)", () => {
        // Arrange
        const now = new Date("2024-01-01T00:00:00");
        const cycleHours = 2;
        const offsetMinutes = 5;
        
        // Act
        const result = calcMsUntilCycleRestart({ offsetMinutes, now, cycleHours });
        
        // Assert
        expect(result.minutes).toBe(115);
        expect(result.ms).toBe(115 * 60 * 1000);
    });
    
    test("should subtract seconds from ms", () => {
        const now = new Date("2024-01-01T01:00:30");
        const result = calcMsUntilCycleRestart({ offsetMinutes: 5, now, cycleHours: 2 });
        
        expect(result.ms).toBe(55 * 60 * 1000 - 30 * 1000);
    });
});
```

**❌ ПЛОХО:**

```javascript
test("test time calculation", () => {
    // Проблемы:
    // 1. Название теста неинформативное
    // 2. Использует текущее время (Date.now()) — недетерминировано
    // 3. Не понятно что тестируется
    // 4. Нет проверки граничных случаев
    
    const result = calcMsUntilCycleRestart({ offsetMinutes: 5, cycleHours: 2 });
    expect(result).toBeTruthy(); // слишком общая проверка
});
```

### Пример 2: Тестирование валидатора

**✅ ХОРОШО:**

```javascript
describe("Validator", () => {
    const schema = {
        id: { required: true, type: "integer" },
        name: { required: false, type: "string", length: 10 }
    };
    
    test("should validate object with all fields", () => {
        const validator = new Validator(schema, "test");
        const testObj = { id: 1, name: "John" };
        
        const result = validator.validateObj(testObj);
        
        expect(result).toEqual({ success: true });
    });
    
    test("should return error when id has wrong type", () => {
        const validator = new Validator(schema, "test");
        const testObj = { id: "1", name: "John" };
        
        const result = validator.validateObj(testObj);
        
        expect(result.success).toBe(false);
        expect(result.message).toContain("isn't number");
    });
    
    test("should return error when required field is missing", () => {
        const validator = new Validator(schema, "test");
        const testObj = { name: "John" }; // нет id
        
        const result = validator.validateObj(testObj);
        
        expect(result).toEqual({ 
            success: false, 
            message: "[test validator]: [id] is required!" 
        });
    });
});
```

**❌ ПЛОХО:**

```javascript
test("validator works", () => {
    // Проблемы:
    // 1. Один огромный тест для всех случаев
    // 2. Неинформативное название
    // 3. Сложно понять что именно сломалось при падении
    // 4. Нет проверки ошибок
    
    const validator = new Validator(schema);
    expect(validator.validateObj({ id: 1, name: "John" })).toBeTruthy();
    expect(validator.validateObj({ id: 2, name: "Jane" })).toBeTruthy();
    expect(validator.validateObj({ id: 3 })).toBeTruthy();
    // ... еще 10 проверок в одном тесте
});
```

### Пример 3: Тестирование API-сервиса

**✅ ХОРОШО:**

```javascript
describe("DatasetsService", () => {
    test("should return data without params", async () => {
        // Arrange
        const client_id = 1;
        
        // Act
        const data = await DatasetsService.datasets.wbReportsData_v1({ client_id });
        
        // Assert
        expect(data).toBeInstanceOf(Array);
        expect(data).not.toHaveLength(0);
    });
    
    test("should filter data by nm_ids", async () => {
        // Arrange
        const client_id = 1;
        const nm_ids = [123, 456];
        
        // Act
        const data = await DatasetsService.datasets.wbReportsData_v1({ 
            client_id, 
            nm_ids 
        });
        
        // Assert
        expect(data).toBeInstanceOf(Array);
        expect(data.every(d => nm_ids.includes(d.nm_id))).toBe(true);
    });
    
    test("should filter data by date_from", async () => {
        // Arrange
        const client_id = 1;
        const date_from = "2024-01-01";
        
        // Act
        const data = await DatasetsService.datasets.wbReportsData_v1({ 
            client_id, 
            date_from 
        });
        
        // Assert
        expect(data).toBeInstanceOf(Array);
        expect(data.every(d => d.rr_dt >= date_from)).toBe(true);
    });
});
```

**❌ ПЛОХО:**

```javascript
test("test dataset", async () => {
    // Проблемы:
    // 1. Не изолирован — зависит от реальной БД
    // 2. Нет проверки конкретных значений
    // 3. Не понятно что тестируется
    // 4. Если БД пустая — тест упадет
    
    const data = await DatasetsService.datasets.wbReportsData_v1({ client_id: 999 });
    expect(data).toBeTruthy();
});
```

---

## 5. Как тестировать разные компоненты

### Тестирование чистых функций

**Чистая функция** — возвращает результат только на основе входных параметров, без побочных эффектов.

```javascript
// Функция
export function calculateDiscount(price, discountPercent) {
    if (price <= 0 || discountPercent < 0 || discountPercent > 100) {
        throw new Error("Invalid parameters");
    }
    return price * (1 - discountPercent / 100);
}

// ✅ Тесты
describe("calculateDiscount", () => {
    test("should calculate 10% discount correctly", () => {
        expect(calculateDiscount(100, 10)).toBe(90);
    });
    
    test("should calculate 50% discount correctly", () => {
        expect(calculateDiscount(200, 50)).toBe(100);
    });
    
    test("should throw error for negative price", () => {
        expect(() => calculateDiscount(-100, 10)).toThrow("Invalid parameters");
    });
    
    test("should throw error for discount > 100", () => {
        expect(() => calculateDiscount(100, 150)).toThrow("Invalid parameters");
    });
});
```

### Тестирование сервисов (с зависимостями)

```javascript
// Сервис
class UserService {
    constructor(db, emailService) {
        this.db = db;
        this.emailService = emailService;
    }
    
    async createUser(userData) {
        const user = await this.db.insert('users', userData);
        await this.emailService.sendWelcomeEmail(user.email);
        return user;
    }
}

// ✅ Тесты с моками
describe("UserService", () => {
    test("should create user and send welcome email", async () => {
        // Arrange
        const mockDb = {
            insert: jest.fn().mockResolvedValue({ id: 1, email: "test@test.com" })
        };
        const mockEmailService = {
            sendWelcomeEmail: jest.fn().mockResolvedValue(true)
        };
        const userService = new UserService(mockDb, mockEmailService);
        
        // Act
        const result = await userService.createUser({ email: "test@test.com" });
        
        // Assert
        expect(mockDb.insert).toHaveBeenCalledWith('users', { email: "test@test.com" });
        expect(mockEmailService.sendWelcomeEmail).toHaveBeenCalledWith("test@test.com");
        expect(result).toEqual({ id: 1, email: "test@test.com" });
    });
});
```

### Тестирование API-контроллеров

```javascript
// Контроллер
class UsersController {
    static async createUser(req, res) {
        try {
            const userData = req.body;
            const user = await UserService.createUser(userData);
            res.status(201).json(user);
        } catch (error) {
            res.status(400).json({ error: error.message });
        }
    }
}

// ✅ Тесты
describe("UsersController", () => {
    test("should create user and return 201", async () => {
        // Arrange
        const req = { body: { name: "John", email: "john@test.com" } };
        const res = {
            status: jest.fn().mockReturnThis(),
            json: jest.fn()
        };
        jest.spyOn(UserService, 'createUser').mockResolvedValue({ 
            id: 1, 
            name: "John" 
        });
        
        // Act
        await UsersController.createUser(req, res);
        
        // Assert
        expect(res.status).toHaveBeenCalledWith(201);
        expect(res.json).toHaveBeenCalledWith({ id: 1, name: "John" });
    });
    
    test("should return 400 on validation error", async () => {
        // Arrange
        const req = { body: { name: "" } };
        const res = {
            status: jest.fn().mockReturnThis(),
            json: jest.fn()
        };
        jest.spyOn(UserService, 'createUser').mockRejectedValue(
            new Error("Name is required")
        );
        
        // Act
        await UsersController.createUser(req, res);
        
        // Assert
        expect(res.status).toHaveBeenCalledWith(400);
        expect(res.json).toHaveBeenCalledWith({ error: "Name is required" });
    });
});
```

### Тестирование работы с БД (интеграционный подход)

Для **юнит-тестов** — используем моки БД.  
Для **интеграционных тестов** — используем реальную БД (или test DB).

```javascript
// ✅ Юнит-тест (быстрый, изолированный)
test("should query users from database", async () => {
    // Arrange
    const mockDb = {
        query: jest.fn().mockResolvedValue([
            { id: 1, name: "John" },
            { id: 2, name: "Jane" }
        ])
    };
    
    // Act
    const users = await getUsers(mockDb);
    
    // Assert
    expect(users).toHaveLength(2);
    expect(mockDb.query).toHaveBeenCalledWith("SELECT * FROM users");
});

// ✅ Интеграционный тест (медленный, использует реальную БД)
// Поместить в отдельную папку __integration__/
describe("Users Integration", () => {
    let testDb;
    
    beforeAll(async () => {
        testDb = await setupTestDatabase(); // создает тестовую БД
    });
    
    afterAll(async () => {
        await teardownTestDatabase(testDb); // очищает БД
    });
    
    test("should save and retrieve user", async () => {
        // Act
        await testDb.insert('users', { name: "John" });
        const users = await testDb.query("SELECT * FROM users WHERE name = 'John'");
        
        // Assert
        expect(users).toHaveLength(1);
        expect(users[0].name).toBe("John");
    });
});
```

---

## 6. Интеграция в процесс разработки

### Когда писать тесты

**Правило:** Тесты пишутся **ВМЕСТЕ** с кодом, а не потом!

**При добавлении новой функции:**

1. Написать тесты (TDD подход) или сразу после кода
2. Запустить тесты локально: `npm test`
3. Убедиться что coverage не упал

**При изменении существующего кода:**

1. Проверить, есть ли тесты для этого участка
2. Если нет — добавить тесты
3. Обновить существующие тесты, если поведение изменилось

**При фиксе бага:**

1. Сначала написать тест, который воспроизводит баг (тест падает)
2. Исправить баг
3. Тест проходит — баг больше не вернется

### Какие изменения считаются покрытыми

**Минимум 80% покрытия для:**

- Новых файлов
- Измененных функций/методов
- Новой бизнес-логики

**Не требуют 80% покрытия:**

- Конфигурационные файлы
- Миграции БД
- Простые CRUD операции (можно покрыть интеграционными тестами)
- Утилиты для разработки

### Как смотреть coverage

**Запуск с coverage:**

```bash
# Запустить тесты с покрытием
npm test -- --coverage

# Результат:
# ----------------------|---------|----------|---------|---------|
# File                  | % Stmts | % Branch | % Funcs | % Lines |
# ----------------------|---------|----------|---------|---------|
# All files             |   78.45 |    65.21 |   82.10 |   78.45 |
#  utils/               |   85.00 |    70.00 |   90.00 |   85.00 |
#   date.utils.js       |   90.00 |    75.00 |  100.00 |   90.00 |
#   validator.js        |   80.00 |    65.00 |   80.00 |   80.00 |
# ----------------------|---------|----------|---------|---------|
```

**Генерация HTML отчета:**

```bash
npm test -- --coverage --coverageDirectory=coverage

# Открыть в браузере
open coverage/lcov-report/index.html
```

**Интерпретация метрик:**

- **% Stmts** — процент выполненных строк кода
- **% Branch** — процент проверенных условий (if/else)
- **% Funcs** — процент вызванных функций
- **% Lines** — процент выполненных логических строк

**Целевые значения:**

- Statements: >80%
- Branches: >75%
- Functions: >80%
- Lines: >80%

---

## 7. Требования к тестам

### Минимальные требования

1. **Покрытие:** Минимум 80% для новых/измененных участков
2. **Стабильность:** Тесты не должны падать случайно (no flaky tests)
3. **Скорость:** Один тест <100ms, все тесты <30s
4. **Изоляция:** Тесты не зависят друг от друга
5. **Понятность:** Названия тестов понятны без чтения кода

### Тесты должны быть быстрыми

```javascript
// ✅ ХОРОШО: быстрые юнит-тесты
test("should calculate sum", () => {
    expect(sum(2, 3)).toBe(5); // <1ms
});

// ❌ ПЛОХО: медленные тесты замедляют разработку
test("should wait for async operation", async () => {
    await new Promise(resolve => setTimeout(resolve, 5000)); // 5 секунд!
    // ...
});
```

**Совет:** Если тест медленный — это признак плохой архитектуры или неправильного подхода (нужен мок вместо реального API).

### Тесты должны быть стабильными

```javascript
// ❌ ПЛОХО: нестабильный тест (flaky)
test("should get current date", () => {
    const result = getCurrentDate();
    expect(result).toBe("2024-02-10"); // упадет завтра!
});

// ✅ ХОРОШО: стабильный тест с мокированием даты
test("should get current date", () => {
    jest.spyOn(Date, 'now').mockReturnValue(new Date("2024-02-10").getTime());
    const result = getCurrentDate();
    expect(result).toBe("2024-02-10");
});
```

**Признаки нестабильности:**

- Использование `Date.now()`, `Math.random()` без моков
- Зависимость от порядка выполнения тестов
- Зависимость от внешних API/БД

---

## 8. Чеклист для Pull Request

Перед созданием PR убедитесь:

### Чеклист: Тесты

- [ ] **Есть тесты для новой логики** — каждая новая функция/метод покрыта тестами
- [ ] **Есть тесты для измененной логики** — обновлены существующие тесты
- [ ] **Покрытие не снизилось** — запустить `npm test -- --coverage` и проверить процент
- [ ] **Все тесты проходят локально** — `npm test` завершается успешно
- [ ] **Тесты быстрые** — все тесты выполняются за <30s
- [ ] **Нет закомментированных тестов** — если тест не нужен, удалить его
- [ ] **Названия тестов понятны** — можно понять что тестируется без чтения кода

### Чеклист: Качество кода

- [ ] **Код следует AAA паттерну** — Arrange, Act, Assert
- [ ] **Нет дублирования** — общие фикстуры вынесены в переменные/функции
- [ ] **Нет моков внутри production кода** — моки только в тестах
- [ ] **Тесты изолированы** — не зависят друг от друга

### Чеклист: CI/CD

- [ ] **CI pipeline проходит** — тесты запустились и прошли в CI
- [ ] **Coverage report доступен** — можно посмотреть какие строки не покрыты
- [ ] **Нет warnings** — Jest не выводит предупреждения

---

## 9. Полезные команды

```bash
# Запустить все тесты
npm test

# Запустить тесты с покрытием
npm test -- --coverage

# Запустить конкретный файл
npm test -- src/utils/validator.test.js

# Запустить тесты в watch mode (перезапуск при изменении)
npm test -- --watch

# Запустить только тесты с определенным названием
npm test -- -t "should validate integer"

# Запустить тесты последовательно (для дебага)
npm run test:sync

# Обновить snapshots (если используются)
npm test -- -u
```

---

## 10. Частые ошибки и как их избежать

### Ошибка 1: Тесты зависят друг от друга

```javascript
// ❌ ПЛОХО
let sharedData = [];

test("test 1", () => {
    sharedData.push(1);
    expect(sharedData).toHaveLength(1);
});

test("test 2", () => {
    // Упадет если test 1 не выполнился!
    expect(sharedData).toHaveLength(1);
});

// ✅ ХОРОШО
describe("My tests", () => {
    let data;
    
    beforeEach(() => {
        data = []; // чистое состояние перед каждым тестом
    });
    
    test("test 1", () => {
        data.push(1);
        expect(data).toHaveLength(1);
    });
    
    test("test 2", () => {
        data.push(2);
        expect(data).toHaveLength(1);
    });
});
```

### Ошибка 2: Тестирование деталей реализации

```javascript
// ❌ ПЛОХО: тестируем как работает, а не что возвращает
test("should use helper function", () => {
    const spy = jest.spyOn(myModule, '_privateHelper');
    myModule.publicFunction();
    expect(spy).toHaveBeenCalled(); // хрупкий тест!
});

// ✅ ХОРОШО: тестируем результат
test("should return correct value", () => {
    const result = myModule.publicFunction();
    expect(result).toBe(expectedValue);
});
```

### Ошибка 3: Слишком много проверок в одном тесте

```javascript
// ❌ ПЛОХО: непонятно что сломалось при падении
test("validator works", () => {
    expect(validator.validate({id: 1})).toBe(true);
    expect(validator.validate({id: "1"})).toBe(false);
    expect(validator.validate({})).toBe(false);
    expect(validator.validate(null)).toBe(false);
    // ... еще 10 проверок
});

// ✅ ХОРОШО: отдельный тест для каждого случая
test("should pass for valid integer", () => {
    expect(validator.validate({id: 1})).toBe(true);
});

test("should fail for string id", () => {
    expect(validator.validate({id: "1"})).toBe(false);
});

test("should fail for missing id", () => {
    expect(validator.validate({})).toBe(false);
});
```

---

## 11. Дополнительные ресурсы

**Документация:**

- Jest: <https://jestjs.io/docs/getting-started>
- Jest Best Practices: <https://github.com/goldbergyoni/javascript-testing-best-practices>

**В проекте:**

- Конфигурация Jest: `jest.config.mjs`
- Примеры тестов: `src/**/*.test.js`
- Test utils: `src/utils/test.utils.js`

---

**Помните:** Хорошие тесты экономят время в долгосрочной перспективе. Потратьте 20 минут на написание тестов сейчас — сэкономьте часы на отладке в будущем!

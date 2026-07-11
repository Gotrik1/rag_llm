# Identity & Access Bounded Context (IAM)

**Контекст:** Identity & Access
**Слой:** Business Isolation Layer
**Назначение:** Управление субъектами системы и границами данных.

---

## 1. Purpose

Контекст отвечает за:

* управление пользователями,
* создание и управление Workspace,
* назначение ролей,
* контроль доступа,
* изоляцию данных между бизнесами.

> Identity определяет границу владения бизнесом в системе 10X.
> Без Identity невозможна изоляция данных между Workspace (multi-tenant isolation).

---

## 2. Boundary

### Входит в контекст

* Owner (роль)
* Administrator
* User
* Workspace
* Role
* Permission
* Membership
* RBAC Policy (role-based access control policy)

### Не входит в контекст

* Кабинеты маркетплейсов
* Товары (SKU)
* Заказы
* Финансовые данные
* Расчёт прибыли

> Контекст Identity не содержит доменной логики маркетплейсов и расчётов прибыли.

---

## 3. Ubiquitous Language

| Термин        | Определение                                          |
| ------------- | ---------------------------------------------------- |
| User          | Пользователь системы                                 |
| Owner         | Пользователь с ролью Owner внутри Workspace          |
| Administrator | Пользователь с административными правами в Workspace |
| Workspace     | Изолированное бизнес-пространство                    |
| Role          | Набор разрешений                                     |
| Permission    | Конкретное право на выполнение действия              |
| Membership    | Связь User и Workspace с назначенной ролью           |
| RBAC          | Модель контроля доступа на основе ролей              |

---

## 4. Core Domain Model

### Aggregate: Workspace

Workspace — корневой агрегат контекста.

#### Поля

* id
* name
* created_at
* status

#### Правила

* Workspace создаётся пользователем.
* В каждом Workspace должен существовать как минимум один пользователь с ролью Owner.
* Workspace является границей изоляции данных.
* Workspace не может существовать без активного Owner.

---

### Entity: User

#### Поля

* id
* email
* status
* created_at

> Пользователь может состоять в одном или нескольких Workspace.

---

### Entity: Role

#### Поля

* id
* name
* workspace_id

Роль определяет набор разрешений внутри конкретного Workspace.

---

### Entity: Permission

Определяет конкретные действия:

* manage_workspace
* manage_cabinets
* view_reports
* manage_unit
* manage_users
* etc.

---

### Entity: Membership

Связывает User и Workspace.

#### Поля

* user_id
* workspace_id
* role_id
* status

Membership определяет права пользователя внутри конкретного Workspace.

---

## 5. Invariants

1. В каждом Workspace должен существовать как минимум один пользователь с ролью Owner.
2. Все бизнес-данные системы обязаны быть связаны с workspace_id.
3. Пользователь не может выполнять действие без соответствующего Permission.
4. Любая бизнес-сущность (Cabinet, SKU, Order и т.д.) не может существовать вне Workspace.
5. Удаление Workspace деактивирует или архивирует все связанные данные.

---

## 6. State Changes

Контекст поддерживает следующие операции:

* Create Workspace
* Add User to Workspace
* Assign Role
* Update Role Permissions
* Revoke Access
* Change User Status
* Transfer Ownership
* Delete Workspace

Все изменения проходят через RBAC-проверку.

---

## 7. Dependencies

### Upstream

Identity не имеет бизнес-зависимостей от других контекстов.

### Downstream

* Marketplace Integration
* Analytics Engine

Они обязаны принимать workspace_id как обязательный контекст выполнения операций.

---

## 8. Архитектурная роль

Identity обеспечивает:

* мультитенантность,
* безопасность,
* изоляцию бизнеса,
* управляемость доступа.

Это контекст изоляции и контроля доступа, не содержащий бизнес-аналитику.

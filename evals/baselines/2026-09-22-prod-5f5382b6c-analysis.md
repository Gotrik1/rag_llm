# Retrieval evaluation analysis

- Cases: 120
- Source recall: 0.9972
- Locator recall: 0.9667
- Mean reciprocal rank: 0.6778
- Request errors: 0

## Results by tag

| Tag | Cases | Source recall | Locator recall | MRR | Avg latency, s | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| comparison | 8 | 1.0000 | 1.0000 | 0.5167 | 8.780 | 0 |
| configuration | 8 | 1.0000 | 1.0000 | 0.7708 | 6.628 | 0 |
| definition | 24 | 1.0000 | 1.0000 | 0.7338 | 7.757 | 0 |
| document-occurrence | 8 | 1.0000 | 1.0000 | 0.4567 | 15.982 | 0 |
| formula | 32 | 0.9896 | 0.9062 | 0.6507 | 9.997 | 0 |
| legislation | 8 | 1.0000 | 1.0000 | 0.4917 | 11.459 | 0 |
| multi-document | 72 | 0.9954 | 0.9583 | 0.5925 | 10.316 | 0 |
| orem | 8 | 1.0000 | 1.0000 | 0.9167 | 7.407 | 0 |
| positive-only | 8 | 1.0000 | 1.0000 | 0.4567 | 15.982 | 0 |
| procedure | 16 | 1.0000 | 0.9375 | 0.6469 | 8.318 | 0 |
| rre | 104 | 0.9968 | 0.9615 | 0.6765 | 8.866 | 0 |
| semantic-precision | 16 | 1.0000 | 1.0000 | 0.8003 | 9.008 | 0 |
| single-document | 8 | 1.0000 | 1.0000 | 0.9167 | 7.407 | 0 |
| system-behavior | 40 | 1.0000 | 1.0000 | 0.7012 | 7.901 | 0 |
| terminology | 8 | 1.0000 | 1.0000 | 1.0000 | 9.619 | 0 |
| typo-tolerance | 16 | 0.9792 | 0.7500 | 0.7371 | 9.755 | 0 |

## Results by question family

| Family | Cases | Source recall | Locator recall | MRR | Avg latency, s | Zero-source cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| accrual-types | 8 | 1.0000 | 1.0000 | 0.8542 | 5.515 | 0 |
| advances | 8 | 1.0000 | 0.8750 | 0.8875 | 8.454 | 0 |
| compare-fourth-fifth | 8 | 1.0000 | 1.0000 | 0.5167 | 8.780 | 0 |
| contract-conditions | 8 | 1.0000 | 1.0000 | 0.7708 | 6.628 | 0 |
| electricity-price-composition | 8 | 1.0000 | 1.0000 | 0.6007 | 8.396 | 0 |
| first-category-calculation | 8 | 0.9583 | 0.6250 | 0.5868 | 11.056 | 0 |
| ivo | 8 | 1.0000 | 1.0000 | 0.9167 | 7.407 | 0 |
| price-category-concept | 8 | 1.0000 | 1.0000 | 0.5972 | 7.921 | 0 |
| price-category-documents | 8 | 1.0000 | 1.0000 | 0.4567 | 15.982 | 0 |
| punce | 8 | 1.0000 | 1.0000 | 0.7500 | 9.835 | 0 |
| rre-contract-prerequisites | 8 | 1.0000 | 1.0000 | 0.4062 | 8.181 | 0 |
| second-category-law | 8 | 1.0000 | 1.0000 | 0.4917 | 11.459 | 0 |
| sixth-category-calculation | 8 | 1.0000 | 1.0000 | 0.6076 | 10.066 | 0 |
| sixth-category-services | 8 | 1.0000 | 1.0000 | 1.0000 | 9.619 | 0 |
| transmission-calculation | 8 | 1.0000 | 1.0000 | 0.7250 | 9.345 | 0 |

## Most frequently missing sources

- Методика расчета стоимости 1-6ЦК: 1

## Worst cases

- `first-category-calculation-06` — source=0.6667, locator=0.0000, MRR=0.4167; missing: Методика расчета стоимости 1-6ЦК, Порядок определения стоимости 1 ЦК; question: Сравни методику 1 ЦК с нормами постановлений 442 и 1179.
- `advances-05` — source=1.0000, locator=0.0000, MRR=0.1000; missing: 4.1.2.1 Авансы; question: Из каких шагов состоит формирование авансового начисления?
- `first-category-calculation-02` — source=1.0000, locator=0.0000, MRR=0.6111; missing: Порядок определения стоимости 1 ЦК; question: Приведи полный порядок расчёта первой ценовой категории.
- `first-category-calculation-08` — source=1.0000, locator=0.0000, MRR=0.6111; missing: Порядок определения стоимости 1 ЦК; question: Объясни расчёт электроэнергии и передачи для первой ценовой категории.
- `rre-contract-prerequisites-07` — source=1.0000, locator=1.0000, MRR=0.2500; missing: none; question: Дай чек-лист готовности договора РРЭ к расчёту.
- `compare-fourth-fifth-06` — source=1.0000, locator=1.0000, MRR=0.2917; missing: none; question: Сопоставь расчёт электроэнергии, покупной и сетевой мощности для 4 и 5 ЦК.
- `second-category-law-08` — source=1.0000, locator=1.0000, MRR=0.2917; missing: none; question: Какие параметры нужны для определения стоимости по второй ЦК?
- `accrual-types-04` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: виды начислений в системе какие есть
- `contract-conditions-01` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Что ведётся в договорных условиях?
- `contract-conditions-02` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Какие параметры хранятся в модуле Договорные условия РРЭ?
- `ivo-01` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Что такое ИВ0?
- `rre-contract-prerequisites-01` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Для расчётов в системе по договорам РРЭ какие условия должны быть выполнены?
- `rre-contract-prerequisites-02` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Что необходимо настроить перед запуском расчёта договора РРЭ?
- `rre-contract-prerequisites-04` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: что должно быть заведено чтобы посчитать договор ррэ
- `rre-contract-prerequisites-05` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Какие объекты, условия и тарифы требуются для расчёта РРЭ?
- `rre-contract-prerequisites-06` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Почему расчёт по договору РРЭ может не сформироваться?
- `rre-contract-prerequisites-08` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Какие данные система проверяет перед расчётом договора РРЭ?
- `compare-fourth-fifth-02` — source=1.0000, locator=1.0000, MRR=0.3750; missing: none; question: Сравни четвёртую и пятую ценовые категории без пропуска видов мощности.
- `compare-fourth-fifth-05` — source=1.0000, locator=1.0000, MRR=0.3750; missing: none; question: Как меняются учёт, планирование и тариф на передачу между 4 и 5 ЦК?
- `second-category-law-01` — source=1.0000, locator=1.0000, MRR=0.3750; missing: none; question: Как рассчитывается 2 ЦК по законодательству? Используй только постановления.
- `second-category-law-04` — source=1.0000, locator=1.0000, MRR=0.3750; missing: none; question: как щитается вторая цк по закону
- `second-category-law-05` — source=1.0000, locator=1.0000, MRR=0.3750; missing: none; question: Какие нормы постановлений регулируют расчёт 2 ЦК?
- `price-category-documents-01` — source=1.0000, locator=1.0000, MRR=0.4567; missing: none; question: В каких документах написано про ценовые категории?
- `price-category-documents-02` — source=1.0000, locator=1.0000, MRR=0.4567; missing: none; question: Перечисли все доступные документы, где фигурирует ЦК.
- `price-category-documents-03` — source=1.0000, locator=1.0000, MRR=0.4567; missing: none; question: Какие постановления и методики описывают ценовые категории?

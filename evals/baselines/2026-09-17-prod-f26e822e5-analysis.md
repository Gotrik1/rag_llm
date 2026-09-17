# Retrieval evaluation analysis

- Cases: 120
- Source recall: 0.5561
- Locator recall: 0.9333
- Mean reciprocal rank: 0.4072
- Request errors: 0

## Results by tag

| Tag | Cases | Source recall | Locator recall | MRR | Avg latency, s | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| comparison | 8 | 0.7500 | 1.0000 | 0.4198 | 9.300 | 0 |
| configuration | 8 | 0.7500 | 1.0000 | 0.6429 | 5.991 | 0 |
| definition | 24 | 0.5625 | 1.0000 | 0.4611 | 7.076 | 0 |
| document-occurrence | 8 | 0.3000 | 1.0000 | 0.1946 | 8.679 | 0 |
| formula | 32 | 0.5990 | 0.7812 | 0.3871 | 8.857 | 0 |
| legislation | 8 | 0.4375 | 1.0000 | 0.2281 | 9.081 | 0 |
| multi-document | 72 | 0.4546 | 0.9306 | 0.2781 | 8.560 | 0 |
| orem | 8 | 0.7500 | 0.7500 | 0.5833 | 7.479 | 0 |
| positive-only | 8 | 0.3000 | 1.0000 | 0.1946 | 8.679 | 0 |
| procedure | 16 | 0.5000 | 0.9375 | 0.4583 | 7.418 | 0 |
| rre | 104 | 0.5609 | 0.9423 | 0.4100 | 7.985 | 0 |
| semantic-precision | 16 | 0.4792 | 1.0000 | 0.3148 | 8.354 | 0 |
| single-document | 8 | 0.7500 | 0.7500 | 0.5833 | 7.479 | 0 |
| system-behavior | 40 | 0.5125 | 1.0000 | 0.4239 | 6.851 | 0 |
| terminology | 8 | 0.7500 | 1.0000 | 0.5451 | 9.267 | 0 |
| typo-tolerance | 16 | 0.7500 | 0.7500 | 0.6083 | 8.451 | 0 |

## Results by question family

| Family | Cases | Source recall | Locator recall | MRR | Avg latency, s | Zero-source cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| accrual-types | 8 | 1.0000 | 1.0000 | 0.9167 | 5.343 | 0 |
| advances | 8 | 1.0000 | 0.8750 | 0.9167 | 7.236 | 0 |
| compare-fourth-fifth | 8 | 0.7500 | 1.0000 | 0.4198 | 9.300 | 2 |
| contract-conditions | 8 | 0.7500 | 1.0000 | 0.6429 | 5.991 | 2 |
| electricity-price-composition | 8 | 0.2083 | 1.0000 | 0.0845 | 7.441 | 4 |
| first-category-calculation | 8 | 0.5000 | 0.6250 | 0.3000 | 9.667 | 2 |
| ivo | 8 | 0.7500 | 0.7500 | 0.5833 | 7.479 | 2 |
| price-category-concept | 8 | 0.3750 | 1.0000 | 0.2792 | 8.355 | 2 |
| price-category-documents | 8 | 0.3000 | 1.0000 | 0.1946 | 8.679 | 2 |
| punce | 8 | 0.3125 | 1.0000 | 0.1875 | 7.530 | 3 |
| rre-contract-prerequisites | 8 | 0.0000 | 1.0000 | 0.0000 | 7.599 | 8 |
| second-category-law | 8 | 0.4375 | 1.0000 | 0.2281 | 9.081 | 4 |
| sixth-category-calculation | 8 | 0.7083 | 0.7500 | 0.4368 | 9.202 | 0 |
| sixth-category-services | 8 | 0.7500 | 1.0000 | 0.5451 | 9.267 | 2 |
| transmission-calculation | 8 | 0.5000 | 1.0000 | 0.3724 | 7.790 | 2 |

## Most frequently missing sources

- 29.12.2011 N 1179: 46
- Методика расчета стоимости 1-6ЦК: 31
- 04.05.2012 N 442: 22
- Методика расчеты на РРЭ: 17
- Методика расчета стоимости на передачу: 9
- r12.docx: 2

## Worst cases

- `first-category-calculation-02` — source=0.0000, locator=0.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 04.05.2012 N 442, 29.12.2011 N 1179, Порядок определения стоимости 1 ЦК; question: Приведи полный порядок расчёта первой ценовой категории.
- `first-category-calculation-08` — source=0.0000, locator=0.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 04.05.2012 N 442, 29.12.2011 N 1179, Порядок определения стоимости 1 ЦК; question: Объясни расчёт электроэнергии и передачи для первой ценовой категории.
- `ivo-04` — source=0.0000, locator=0.0000, MRR=0.0000; missing: r12.docx, Внешняя инициатива ИВ0; question: формула иво какая
- `ivo-05` — source=0.0000, locator=0.0000, MRR=0.0000; missing: r12.docx, Внешняя инициатива ИВ0; question: Чем ИВ0 отличается от расчёта стоимости?
- `compare-fourth-fifth-02` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179; question: Сравни четвёртую и пятую ценовые категории без пропуска видов мощности.
- `compare-fourth-fifth-08` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179; question: Дай полный сравнительный порядок расчёта четвёртой и пятой ЦК.
- `contract-conditions-01` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Что ведётся в договорных условиях?
- `contract-conditions-02` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Какие параметры хранятся в модуле Договорные условия РРЭ?
- `electricity-price-composition-02` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Перечисли составляющие цены электрической энергии для потребителя.
- `electricity-price-composition-03` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Как формируется итоговая стоимость электроэнергии и мощности?
- `electricity-price-composition-05` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Какие части ПУНЦ относятся к покупке, передаче, сбыту и иным услугам?
- `electricity-price-composition-07` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Что входит в стоимость электрической энергии по ценовым категориям?
- `price-category-concept-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: Чем отличаются ценовые категории по учёту, планированию и тарифу на передачу?
- `price-category-concept-06` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: Что значит ЦК в расчётах электроэнергии?
- `price-category-documents-02` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК, Методика расчеты на РРЭ, Методика расчета стоимости на передачу; question: Перечисли все доступные документы, где фигурирует ЦК.
- `price-category-documents-05` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК, Методика расчеты на РРЭ, Методика расчета стоимости на передачу; question: Найди документы с правилами выбора или расчёта ценовой категории.
- `punce-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: пунцэ что это и как считается
- `punce-06` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: Сопоставь нормативное определение ПУНЦ и его расчёт в системе.
- `punce-07` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: Какие алгоритмы расчёта ПУНЦ реализованы?
- `rre-contract-prerequisites-01` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Для расчётов в системе по договорам РРЭ какие условия должны быть выполнены?
- `rre-contract-prerequisites-02` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Что необходимо настроить перед запуском расчёта договора РРЭ?
- `rre-contract-prerequisites-03` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Перечисли предварительные условия расчёта по договору розничного рынка.
- `rre-contract-prerequisites-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: что должно быть заведено чтобы посчитать договор ррэ
- `rre-contract-prerequisites-05` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Какие объекты, условия и тарифы требуются для расчёта РРЭ?
- `rre-contract-prerequisites-06` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Почему расчёт по договору РРЭ может не сформироваться?
- `rre-contract-prerequisites-07` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Дай чек-лист готовности договора РРЭ к расчёту.
- `rre-contract-prerequisites-08` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Какие данные система проверяет перед расчётом договора РРЭ?
- `second-category-law-02` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Покажи законодательный порядок расчёта второй ценовой категории.
- `second-category-law-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: как щитается вторая цк по закону
- `second-category-law-06` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Рассчёт второй ценовой категории: формула и составляющие по законодательству.

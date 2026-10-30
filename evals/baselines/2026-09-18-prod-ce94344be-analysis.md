# Retrieval evaluation analysis

- Cases: 120
- Source recall: 0.7306
- Locator recall: 0.9333
- Mean reciprocal rank: 0.5055
- Request errors: 0

## Results by tag

| Tag | Cases | Source recall | Locator recall | MRR | Avg latency, s | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| comparison | 8 | 0.8750 | 1.0000 | 0.5135 | 7.705 | 0 |
| configuration | 8 | 0.8750 | 1.0000 | 0.7083 | 6.069 | 0 |
| definition | 24 | 0.6875 | 1.0000 | 0.5019 | 6.208 | 0 |
| document-occurrence | 8 | 0.5000 | 1.0000 | 0.3154 | 8.950 | 0 |
| formula | 32 | 0.7188 | 0.7812 | 0.4912 | 8.000 | 0 |
| legislation | 8 | 0.6250 | 1.0000 | 0.4156 | 7.936 | 0 |
| multi-document | 72 | 0.6204 | 0.9306 | 0.3907 | 7.764 | 0 |
| orem | 8 | 0.7500 | 0.7500 | 0.5833 | 6.494 | 0 |
| positive-only | 8 | 0.5000 | 1.0000 | 0.3154 | 8.950 | 0 |
| procedure | 16 | 0.9375 | 0.9375 | 0.6319 | 6.222 | 0 |
| rre | 104 | 0.7468 | 0.9423 | 0.5141 | 7.133 | 0 |
| semantic-precision | 16 | 0.6667 | 1.0000 | 0.4379 | 7.586 | 0 |
| single-document | 8 | 0.7500 | 0.7500 | 0.5833 | 6.494 | 0 |
| system-behavior | 40 | 0.7500 | 1.0000 | 0.5177 | 6.319 | 0 |
| terminology | 8 | 0.8750 | 1.0000 | 0.6562 | 8.143 | 0 |
| typo-tolerance | 16 | 0.8750 | 0.6875 | 0.6951 | 7.275 | 0 |

## Results by question family

| Family | Cases | Source recall | Locator recall | MRR | Avg latency, s | Zero-source cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| accrual-types | 8 | 1.0000 | 1.0000 | 0.8542 | 5.143 | 0 |
| advances | 8 | 1.0000 | 0.8750 | 0.8889 | 5.694 | 0 |
| compare-fourth-fifth | 8 | 0.8750 | 1.0000 | 0.5135 | 7.705 | 0 |
| contract-conditions | 8 | 0.8750 | 1.0000 | 0.7083 | 6.069 | 1 |
| electricity-price-composition | 8 | 0.4583 | 1.0000 | 0.2195 | 7.029 | 3 |
| first-category-calculation | 8 | 0.7500 | 0.5000 | 0.5014 | 8.855 | 0 |
| ivo | 8 | 0.7500 | 0.7500 | 0.5833 | 6.494 | 2 |
| price-category-concept | 8 | 0.6250 | 1.0000 | 0.4354 | 7.051 | 1 |
| price-category-documents | 8 | 0.5000 | 1.0000 | 0.3154 | 8.950 | 0 |
| punce | 8 | 0.4375 | 1.0000 | 0.2162 | 6.430 | 1 |
| rre-contract-prerequisites | 8 | 0.8750 | 1.0000 | 0.3750 | 6.750 | 1 |
| second-category-law | 8 | 0.6250 | 1.0000 | 0.4156 | 7.936 | 1 |
| sixth-category-calculation | 8 | 0.7500 | 0.8750 | 0.4646 | 8.715 | 0 |
| sixth-category-services | 8 | 0.8750 | 1.0000 | 0.6562 | 8.143 | 1 |
| transmission-calculation | 8 | 0.5625 | 1.0000 | 0.4349 | 7.203 | 1 |

## Most frequently missing sources

- 29.12.2011 N 1179: 44
- 04.05.2012 N 442: 10
- Методика расчета стоимости 1-6ЦК: 10
- Методика расчета стоимости на передачу: 9
- Методика расчеты на РРЭ: 8
- r12.docx: 2

## Worst cases

- `ivo-04` — source=0.0000, locator=0.0000, MRR=0.0000; missing: r12.docx, Внешняя инициатива ИВ0; question: формула иво какая
- `ivo-05` — source=0.0000, locator=0.0000, MRR=0.0000; missing: r12.docx, Внешняя инициатива ИВ0; question: Чем ИВ0 отличается от расчёта стоимости?
- `contract-conditions-01` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Что ведётся в договорных условиях?
- `electricity-price-composition-02` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Перечисли составляющие цены электрической энергии для потребителя.
- `electricity-price-composition-03` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Как формируется итоговая стоимость электроэнергии и мощности?
- `electricity-price-composition-05` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Какие части ПУНЦ относятся к покупке, передаче, сбыту и иным услугам?
- `price-category-concept-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: Чем отличаются ценовые категории по учёту, планированию и тарифу на передачу?
- `punce-07` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: Какие алгоритмы расчёта ПУНЦ реализованы?
- `rre-contract-prerequisites-06` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчеты на РРЭ; question: Почему расчёт по договору РРЭ может не сформироваться?
- `second-category-law-08` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Какие параметры нужны для определения стоимости по второй ЦК?
- `sixth-category-services-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК; question: какие услуги входят в расчет шестой цк
- `transmission-calculation-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости на передачу, Методика расчета стоимости 1-6ЦК; question: как считается передача по договорам и по цк
- `price-category-documents-04` — source=0.2000, locator=1.0000, MRR=0.2000; missing: 04.05.2012 N 442, 29.12.2011 N 1179, Методика расчеты на РРЭ, Методика расчета стоимости на передачу; question: где в документах есть информация про цк
- `first-category-calculation-03` — source=0.3333, locator=1.0000, MRR=0.3333; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Какие формулы применяются для стоимости по 1 ЦК?
- `sixth-category-calculation-03` — source=0.3333, locator=1.0000, MRR=0.3333; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Какие формулы используются для расчёта 6 ЦК?
- `sixth-category-calculation-06` — source=0.3333, locator=1.0000, MRR=0.3333; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Сравни методику расчёта 6 ЦК с постановлениями 442 и 1179.
- `price-category-documents-03` — source=0.4000, locator=1.0000, MRR=0.3000; missing: 29.12.2011 N 1179, Методика расчеты на РРЭ, Методика расчета стоимости на передачу; question: Какие постановления и методики описывают ценовые категории?
- `price-category-documents-05` — source=0.4000, locator=1.0000, MRR=0.3000; missing: 29.12.2011 N 1179, Методика расчеты на РРЭ, Методика расчета стоимости на передачу; question: Найди документы с правилами выбора или расчёта ценовой категории.
- `price-category-documents-07` — source=0.4000, locator=1.0000, MRR=0.3000; missing: 29.12.2011 N 1179, Методика расчеты на РРЭ, Методика расчета стоимости на передачу; question: Покажи полный перечень документов о ценовых категориях без отрицательных результатов.
- `price-category-documents-08` — source=0.4000, locator=1.0000, MRR=0.3000; missing: 29.12.2011 N 1179, Методика расчеты на РРЭ, Методика расчета стоимости на передачу; question: Какая документация содержит параметр Ценовая категория?
- `punce-06` — source=0.5000, locator=1.0000, MRR=0.0625; missing: 29.12.2011 N 1179; question: Сопоставь нормативное определение ПУНЦ и его расчёт в системе.
- `punce-04` — source=0.5000, locator=1.0000, MRR=0.0833; missing: 29.12.2011 N 1179; question: пунцэ что это и как считается
- `punce-02` — source=0.5000, locator=1.0000, MRR=0.1667; missing: 29.12.2011 N 1179; question: Расшифруй ПУНЦ и объясни, из чего он складывается.
- `punce-05` — source=0.5000, locator=1.0000, MRR=0.1667; missing: 29.12.2011 N 1179; question: Какие составляющие входят в ПУНЦ на электроэнергию?
- `compare-fourth-fifth-02` — source=0.5000, locator=1.0000, MRR=0.2500; missing: 29.12.2011 N 1179; question: Сравни четвёртую и пятую ценовые категории без пропуска видов мощности.
- `punce-03` — source=0.5000, locator=1.0000, MRR=0.2500; missing: 29.12.2011 N 1179; question: Как определяется предельный уровень нерегулируемых цен?
- `second-category-law-05` — source=0.5000, locator=1.0000, MRR=0.2500; missing: 29.12.2011 N 1179; question: Какие нормы постановлений регулируют расчёт 2 ЦК?
- `transmission-calculation-07` — source=0.5000, locator=1.0000, MRR=0.2500; missing: Методика расчета стоимости на передачу; question: Сравни расчёт передачи по отдельному договору и в составе ЦК.
- `compare-fourth-fifth-08` — source=0.5000, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Дай полный сравнительный порядок расчёта четвёртой и пятой ЦК.
- `punce-01` — source=0.5000, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Что такое ПУНЦЭ?

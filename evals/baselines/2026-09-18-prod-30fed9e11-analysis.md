# Retrieval evaluation analysis

- Cases: 120
- Source recall: 0.8500
- Locator recall: 0.9333
- Mean reciprocal rank: 0.5399
- Request errors: 0

## Results by tag

| Tag | Cases | Source recall | Locator recall | MRR | Avg latency, s | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| comparison | 8 | 0.9375 | 1.0000 | 0.5667 | 8.190 | 0 |
| configuration | 8 | 1.0000 | 1.0000 | 0.6562 | 6.425 | 0 |
| definition | 24 | 0.7986 | 1.0000 | 0.5439 | 6.704 | 0 |
| document-occurrence | 8 | 1.0000 | 1.0000 | 0.4567 | 15.260 | 0 |
| formula | 32 | 0.8125 | 0.7812 | 0.5348 | 8.683 | 0 |
| legislation | 8 | 0.8750 | 1.0000 | 0.4734 | 8.805 | 0 |
| multi-document | 72 | 0.7917 | 0.9306 | 0.4414 | 9.131 | 0 |
| orem | 8 | 0.7500 | 0.7500 | 0.6667 | 6.971 | 0 |
| positive-only | 8 | 1.0000 | 1.0000 | 0.4567 | 15.260 | 0 |
| procedure | 16 | 1.0000 | 0.9375 | 0.6469 | 7.733 | 0 |
| rre | 104 | 0.8462 | 0.9423 | 0.5366 | 7.921 | 0 |
| semantic-precision | 16 | 0.7083 | 1.0000 | 0.4542 | 8.122 | 0 |
| single-document | 8 | 0.7500 | 0.7500 | 0.6667 | 6.971 | 0 |
| system-behavior | 40 | 0.8625 | 1.0000 | 0.5227 | 7.112 | 0 |
| terminology | 8 | 0.8750 | 1.0000 | 0.6562 | 8.743 | 0 |
| typo-tolerance | 16 | 0.9375 | 0.6875 | 0.7135 | 8.653 | 0 |

## Results by question family

| Family | Cases | Source recall | Locator recall | MRR | Avg latency, s | Zero-source cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| accrual-types | 8 | 1.0000 | 1.0000 | 0.8542 | 5.415 | 0 |
| advances | 8 | 1.0000 | 0.8750 | 0.8875 | 7.551 | 0 |
| compare-fourth-fifth | 8 | 0.9375 | 1.0000 | 0.5667 | 8.190 | 0 |
| contract-conditions | 8 | 1.0000 | 1.0000 | 0.6562 | 6.425 | 0 |
| electricity-price-composition | 8 | 0.5417 | 1.0000 | 0.2521 | 7.501 | 2 |
| first-category-calculation | 8 | 0.8750 | 0.5000 | 0.5396 | 9.755 | 0 |
| ivo | 8 | 0.7500 | 0.7500 | 0.6667 | 6.971 | 2 |
| price-category-concept | 8 | 0.8334 | 1.0000 | 0.5271 | 7.663 | 0 |
| price-category-documents | 8 | 1.0000 | 1.0000 | 0.4567 | 15.260 | 0 |
| punce | 8 | 0.5625 | 1.0000 | 0.2506 | 7.032 | 1 |
| rre-contract-prerequisites | 8 | 1.0000 | 1.0000 | 0.4062 | 7.915 | 0 |
| second-category-law | 8 | 0.8750 | 1.0000 | 0.4734 | 8.805 | 1 |
| sixth-category-calculation | 8 | 0.7500 | 0.8750 | 0.4597 | 9.201 | 0 |
| sixth-category-services | 8 | 0.8750 | 1.0000 | 0.6562 | 8.743 | 1 |
| transmission-calculation | 8 | 0.7500 | 1.0000 | 0.4464 | 8.771 | 0 |

## Most frequently missing sources

- 29.12.2011 N 1179: 24
- Методика расчета стоимости 1-6ЦК: 9
- 04.05.2012 N 442: 6
- r12.docx: 2

## Worst cases

- `ivo-04` — source=0.0000, locator=0.0000, MRR=0.0000; missing: r12.docx, Внешняя инициатива ИВ0; question: формула иво какая
- `ivo-05` — source=0.0000, locator=0.0000, MRR=0.0000; missing: r12.docx, Внешняя инициатива ИВ0; question: Чем ИВ0 отличается от расчёта стоимости?
- `electricity-price-composition-03` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Как формируется итоговая стоимость электроэнергии и мощности?
- `electricity-price-composition-05` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, 04.05.2012 N 442; question: Какие части ПУНЦ относятся к покупке, передаче, сбыту и иным услугам?
- `punce-07` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: Какие алгоритмы расчёта ПУНЦ реализованы?
- `second-category-law-08` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Какие параметры нужны для определения стоимости по второй ЦК?
- `sixth-category-services-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК; question: какие услуги входят в расчет шестой цк
- `electricity-price-composition-02` — source=0.3333, locator=1.0000, MRR=0.0333; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179; question: Перечисли составляющие цены электрической энергии для потребителя.
- `first-category-calculation-03` — source=0.3333, locator=1.0000, MRR=0.3333; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Какие формулы применяются для стоимости по 1 ЦК?
- `sixth-category-calculation-03` — source=0.3333, locator=1.0000, MRR=0.3333; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Какие формулы используются для расчёта 6 ЦК?
- `sixth-category-calculation-06` — source=0.3333, locator=1.0000, MRR=0.3333; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Сравни методику расчёта 6 ЦК с постановлениями 442 и 1179.
- `transmission-calculation-04` — source=0.5000, locator=1.0000, MRR=0.0625; missing: Методика расчета стоимости 1-6ЦК; question: как считается передача по договорам и по цк
- `punce-06` — source=0.5000, locator=1.0000, MRR=0.1000; missing: 29.12.2011 N 1179; question: Сопоставь нормативное определение ПУНЦ и его расчёт в системе.
- `punce-04` — source=0.5000, locator=1.0000, MRR=0.1250; missing: 29.12.2011 N 1179; question: пунцэ что это и как считается
- `punce-02` — source=0.5000, locator=1.0000, MRR=0.1667; missing: 29.12.2011 N 1179; question: Расшифруй ПУНЦ и объясни, из чего он складывается.
- `compare-fourth-fifth-08` — source=0.5000, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Дай полный сравнительный порядок расчёта четвёртой и пятой ЦК.
- `punce-01` — source=0.5000, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Что такое ПУНЦЭ?
- `punce-08` — source=0.5000, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Чем сумма тарифов составляющих отличается от суммы стоимостей при расчёте ПУНЦ?
- `transmission-calculation-01` — source=0.5000, locator=1.0000, MRR=0.5000; missing: Методика расчета стоимости 1-6ЦК; question: В каких случаях производятся расчёты за услуги по передаче электроэнергии?
- `transmission-calculation-03` — source=0.5000, locator=1.0000, MRR=0.5000; missing: Методика расчета стоимости 1-6ЦК; question: Опиши основания расчёта услуг по передаче ЭЭ.
- `transmission-calculation-06` — source=0.5000, locator=1.0000, MRR=0.5000; missing: Методика расчета стоимости 1-6ЦК; question: От чего зависит расчёт услуг по передаче электроэнергии?
- `first-category-calculation-04` — source=0.6667, locator=0.0000, MRR=0.5000; missing: 29.12.2011 N 1179, Порядок определения стоимости 1 ЦК; question: как щитается первая цк
- `sixth-category-calculation-02` — source=0.6667, locator=0.0000, MRR=0.5000; missing: 29.12.2011 N 1179, Порядок определения стоимости и цены для 5 и 6 ЦК; question: Приведи полный порядок определения стоимости шестой ценовой категории.
- `electricity-price-composition-04` — source=0.6667, locator=1.0000, MRR=0.4000; missing: 29.12.2011 N 1179; question: из чего складывается цена ээ
- `electricity-price-composition-08` — source=0.6667, locator=1.0000, MRR=0.4000; missing: 29.12.2011 N 1179; question: Дай полный состав цены без подмены услуг составляющими цены.
- `electricity-price-composition-06` — source=0.6667, locator=1.0000, MRR=0.4444; missing: 29.12.2011 N 1179; question: Сопоставь законодательную структуру цены и расчёт в системе.
- `price-category-concept-04` — source=0.6667, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Чем отличаются ценовые категории по учёту, планированию и тарифу на передачу?
- `price-category-concept-05` — source=0.6667, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Расскажи про виды ЦК и условия их применения.
- `price-category-concept-06` — source=0.6667, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Что значит ЦК в расчётах электроэнергии?
- `price-category-concept-07` — source=0.6667, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: какие есть цк и в чем между ними разница
- `sixth-category-calculation-04` — source=0.6667, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: как считается шестая цк
- `advances-05` — source=1.0000, locator=0.0000, MRR=0.1000; missing: 4.1.2.1 Авансы; question: Из каких шагов состоит формирование авансового начисления?
- `first-category-calculation-02` — source=1.0000, locator=0.0000, MRR=0.5833; missing: Порядок определения стоимости 1 ЦК; question: Приведи полный порядок расчёта первой ценовой категории.
- `first-category-calculation-05` — source=1.0000, locator=0.0000, MRR=0.6111; missing: Порядок определения стоимости 1 ЦК; question: Из каких составляющих формируется стоимость первой ЦК?
- `first-category-calculation-08` — source=1.0000, locator=0.0000, MRR=0.6111; missing: Порядок определения стоимости 1 ЦК; question: Объясни расчёт электроэнергии и передачи для первой ценовой категории.
- `electricity-price-composition-01` — source=1.0000, locator=1.0000, MRR=0.1556; missing: none; question: Из чего состоит цена на электроэнергию?
- `contract-conditions-07` — source=1.0000, locator=1.0000, MRR=0.1667; missing: none; question: Как договорные условия влияют на расчёты и выставление документов?
- `punce-05` — source=1.0000, locator=1.0000, MRR=0.2381; missing: none; question: Какие составляющие входят в ПУНЦ на электроэнергию?
- `contract-conditions-01` — source=1.0000, locator=1.0000, MRR=0.2500; missing: none; question: Что ведётся в договорных условиях?
- `rre-contract-prerequisites-07` — source=1.0000, locator=1.0000, MRR=0.2500; missing: none; question: Дай чек-лист готовности договора РРЭ к расчёту.

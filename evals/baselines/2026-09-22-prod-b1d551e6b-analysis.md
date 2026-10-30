# Retrieval evaluation analysis

- Cases: 120
- Source recall: 0.9306
- Locator recall: 0.9500
- Mean reciprocal rank: 0.6014
- Request errors: 0

## Results by tag

| Tag | Cases | Source recall | Locator recall | MRR | Avg latency, s | Errors |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| comparison | 8 | 1.0000 | 1.0000 | 0.5427 | 9.385 | 0 |
| configuration | 8 | 1.0000 | 1.0000 | 0.7708 | 6.424 | 0 |
| definition | 24 | 0.8958 | 1.0000 | 0.6169 | 7.858 | 0 |
| document-occurrence | 8 | 1.0000 | 1.0000 | 0.4567 | 18.517 | 0 |
| formula | 32 | 0.9271 | 0.8438 | 0.6098 | 11.209 | 0 |
| legislation | 8 | 1.0000 | 1.0000 | 0.5198 | 10.508 | 0 |
| multi-document | 72 | 0.8981 | 0.9306 | 0.5032 | 10.844 | 0 |
| orem | 8 | 1.0000 | 1.0000 | 0.9167 | 9.379 | 0 |
| positive-only | 8 | 1.0000 | 1.0000 | 0.4567 | 18.517 | 0 |
| procedure | 16 | 1.0000 | 0.9375 | 0.6469 | 8.410 | 0 |
| rre | 104 | 0.9199 | 0.9423 | 0.5883 | 9.187 | 0 |
| semantic-precision | 16 | 0.9375 | 1.0000 | 0.6285 | 9.218 | 0 |
| single-document | 8 | 1.0000 | 1.0000 | 0.9167 | 9.379 | 0 |
| system-behavior | 40 | 0.8750 | 1.0000 | 0.5720 | 7.869 | 0 |
| terminology | 8 | 0.8750 | 1.0000 | 0.6562 | 9.340 | 0 |
| typo-tolerance | 16 | 0.9167 | 0.6875 | 0.6892 | 10.842 | 0 |

## Results by question family

| Family | Cases | Source recall | Locator recall | MRR | Avg latency, s | Zero-source cases |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| accrual-types | 8 | 1.0000 | 1.0000 | 0.8542 | 7.764 | 0 |
| advances | 8 | 1.0000 | 0.8750 | 0.8875 | 8.976 | 0 |
| compare-fourth-fifth | 8 | 1.0000 | 1.0000 | 0.5427 | 9.385 | 0 |
| contract-conditions | 8 | 1.0000 | 1.0000 | 0.7708 | 6.424 | 0 |
| electricity-price-composition | 8 | 1.0000 | 1.0000 | 0.6007 | 9.097 | 0 |
| first-category-calculation | 8 | 0.8333 | 0.5000 | 0.4910 | 12.709 | 0 |
| ivo | 8 | 1.0000 | 1.0000 | 0.9167 | 9.379 | 0 |
| price-category-concept | 8 | 1.0000 | 1.0000 | 0.5777 | 7.827 | 0 |
| price-category-documents | 8 | 1.0000 | 1.0000 | 0.4567 | 18.517 | 0 |
| punce | 8 | 0.6875 | 1.0000 | 0.4188 | 7.984 | 1 |
| rre-contract-prerequisites | 8 | 1.0000 | 1.0000 | 0.4062 | 7.844 | 0 |
| second-category-law | 8 | 1.0000 | 1.0000 | 0.5198 | 10.508 | 0 |
| sixth-category-calculation | 8 | 0.8750 | 0.8750 | 0.5118 | 12.239 | 0 |
| sixth-category-services | 8 | 0.8750 | 1.0000 | 0.6562 | 9.340 | 1 |
| transmission-calculation | 8 | 0.6875 | 1.0000 | 0.4099 | 9.330 | 0 |

## Most frequently missing sources

- Методика расчета стоимости 1-6ЦК: 8
- 29.12.2011 N 1179: 8
- 04.05.2012 N 442: 2

## Worst cases

- `punce-07` — source=0.0000, locator=1.0000, MRR=0.0000; missing: 29.12.2011 N 1179, Методика расчета стоимости 1-6ЦК; question: Какие алгоритмы расчёта ПУНЦ реализованы?
- `sixth-category-services-04` — source=0.0000, locator=1.0000, MRR=0.0000; missing: Методика расчета стоимости 1-6ЦК; question: какие услуги входят в расчет шестой цк
- `first-category-calculation-04` — source=0.3333, locator=0.0000, MRR=0.1667; missing: Методика расчета стоимости 1-6ЦК, 29.12.2011 N 1179, Порядок определения стоимости 1 ЦК; question: как щитается первая цк
- `first-category-calculation-03` — source=0.3333, locator=1.0000, MRR=0.3333; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Какие формулы применяются для стоимости по 1 ЦК?
- `sixth-category-calculation-06` — source=0.3333, locator=1.0000, MRR=0.3333; missing: 04.05.2012 N 442, 29.12.2011 N 1179; question: Сравни методику расчёта 6 ЦК с постановлениями 442 и 1179.
- `transmission-calculation-04` — source=0.5000, locator=1.0000, MRR=0.0625; missing: Методика расчета стоимости 1-6ЦК; question: как считается передача по договорам и по цк
- `punce-06` — source=0.5000, locator=1.0000, MRR=0.1000; missing: 29.12.2011 N 1179; question: Сопоставь нормативное определение ПУНЦ и его расчёт в системе.
- `punce-04` — source=0.5000, locator=1.0000, MRR=0.1250; missing: 29.12.2011 N 1179; question: пунцэ что это и как считается
- `transmission-calculation-05` — source=0.5000, locator=1.0000, MRR=0.1667; missing: Методика расчета стоимости 1-6ЦК; question: Какие варианты расчёта стоимости передачи реализованы в системе?
- `punce-01` — source=0.5000, locator=1.0000, MRR=0.5000; missing: 29.12.2011 N 1179; question: Что такое ПУНЦЭ?
- `transmission-calculation-01` — source=0.5000, locator=1.0000, MRR=0.5000; missing: Методика расчета стоимости 1-6ЦК; question: В каких случаях производятся расчёты за услуги по передаче электроэнергии?
- `transmission-calculation-03` — source=0.5000, locator=1.0000, MRR=0.5000; missing: Методика расчета стоимости 1-6ЦК; question: Опиши основания расчёта услуг по передаче ЭЭ.
- `transmission-calculation-06` — source=0.5000, locator=1.0000, MRR=0.5000; missing: Методика расчета стоимости 1-6ЦК; question: От чего зависит расчёт услуг по передаче электроэнергии?
- `sixth-category-calculation-02` — source=0.6667, locator=0.0000, MRR=0.5000; missing: 29.12.2011 N 1179, Порядок определения стоимости и цены для 5 и 6 ЦК; question: Приведи полный порядок определения стоимости шестой ценовой категории.
- `advances-05` — source=1.0000, locator=0.0000, MRR=0.1000; missing: 4.1.2.1 Авансы; question: Из каких шагов состоит формирование авансового начисления?
- `first-category-calculation-02` — source=1.0000, locator=0.0000, MRR=0.5556; missing: Порядок определения стоимости 1 ЦК; question: Приведи полный порядок расчёта первой ценовой категории.
- `first-category-calculation-05` — source=1.0000, locator=0.0000, MRR=0.5833; missing: Порядок определения стоимости 1 ЦК; question: Из каких составляющих формируется стоимость первой ЦК?
- `first-category-calculation-08` — source=1.0000, locator=0.0000, MRR=0.6111; missing: Порядок определения стоимости 1 ЦК; question: Объясни расчёт электроэнергии и передачи для первой ценовой категории.
- `rre-contract-prerequisites-07` — source=1.0000, locator=1.0000, MRR=0.2500; missing: none; question: Дай чек-лист готовности договора РРЭ к расчёту.
- `sixth-category-services-07` — source=1.0000, locator=1.0000, MRR=0.2500; missing: none; question: Отдели услуги по 6 ЦК от составляющих предельной цены.
- `second-category-law-08` — source=1.0000, locator=1.0000, MRR=0.2667; missing: none; question: Какие параметры нужны для определения стоимости по второй ЦК?
- `compare-fourth-fifth-06` — source=1.0000, locator=1.0000, MRR=0.2917; missing: none; question: Сопоставь расчёт электроэнергии, покупной и сетевой мощности для 4 и 5 ЦК.
- `accrual-types-04` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: виды начислений в системе какие есть
- `contract-conditions-01` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Что ведётся в договорных условиях?
- `contract-conditions-02` — source=1.0000, locator=1.0000, MRR=0.3333; missing: none; question: Какие параметры хранятся в модуле Договорные условия РРЭ?

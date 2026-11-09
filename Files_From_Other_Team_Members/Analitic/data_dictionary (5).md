# Data dictionary — кейс 2 «Кредитный конвейер»

Описание 14 колонок `applications.csv` и того, как их обрабатывает прототип 04_прототип.py. Примеры взяты из данных. Коды ошибок соответствуют каталогу `08_reasons.md`.

## Поля

| Поле | Тип | Обяз. | Ограничение | Пример | Код ошибки | Комментарий |
| - | - | - | - | - | - | - |
| app_id | string | да | `app_NNNN`, непустой | app_0001 | VALIDATION_ERROR | Ключ для аудита и поддержки. Прототип проверяет только, что поле заполнено |
| age | integer | да | 18–70; плюс диапазон продукта | 32 | MISSING_AGE, AGE_OUT_OF_RANGE, PRODUCT_NOT_AVAILABLE | Ограничение продукта и признак скоринга: 25–50 → 20, 18–24 и 51–60 → 10, 61–70 → 5 |
| income | integer, ₽/мес | да | > 0; ≥ min_income продукта | 120000 | MISSING_INCOME, VALIDATION_ERROR, PRODUCT_MIN_INCOME | Основа расчёта dti и лимита суммы. Баллы: ≥150k → 30, ≥80k → 20, ≥40k → 10, иначе 0 |
| employment | enum | да | employed, self-employed, student, retired, unemployed | employed | MISSING_EMPLOYMENT, INVALID_EMPLOYMENT, VALIDATION_ERROR | unemployed — отказ. Баллы: 25 / 20 / 5 / 10 / 0 |
| family | enum | да | single, married, children | married | VALIDATION_ERROR | Участвует в скоринге: married и children → 15, single → 10. Противоречит NFR-5, см. 02_правила |
| amount | integer, ₽ | да | 10 000–5 000 000; в диапазоне продукта; ≤ 10 × income | 500000 | VALIDATION_ERROR, PRODUCT_NOT_AVAILABLE, AMOUNT_LIMIT | Запрошенная сумма |
| term | integer, мес | да | 3–60; в диапазоне продукта. В данных: 12, 24, 36, 48, 60 | 24 | VALIDATION_ERROR, PRODUCT_NOT_AVAILABLE | Срок кредита. Влияет на ежемесячный платёж и dti |
| decision | enum | да (сервер) | APPROVED, REFUSED, NEED_MORE_DATA, MANUAL_REVIEW | APPROVED | — | Предварительное решение. MANUAL_REVIEW зарезервирован, прототип его не выдаёт |
| reason | string | да, если decision ≠ APPROVED | UPPER_SNAKE_CASE из каталога; пусто при APPROVED | HIGH_DTI | — | Машинный код причины. Обеспечивает объяснимость (NFR-3) |
| client_id | string | да | `cli_NNNN`, непустой | cli_0001 | VALIDATION_ERROR | Связывает заявки одного человека; в CSV у каждого клиента одна заявка. Ключ для эмулятора СМЭВ |
| passport_hash | string | да | ровно 16 hex-символов | f727a849055f2224 | VALIDATION_ERROR | Усечённый SHA-256 от серии и номера паспорта. Оригинал не хранится. Ключ для эмулятора БКИ |
| dti | decimal(·,2) | да (сервер) | ≥ 0; порог 0.5 | 0.21 | HIGH_DTI | Аннуитетный платёж / income по ставке продукта. Пусто, если конвейер остановился до расчёта |
| bki_score | integer | да (из БКИ) | 300–850; < 500 или просрочка — отказ | 720 | BKI_DELINQUENCY, EXTERNAL_TIMEOUT | Баллы: ≥700 → 10, 600–699 → 5, 500–599 → 0, < 500 → −20. В режиме csv пустое значение даёт EXTERNAL_TIMEOUT |
| smev_status | enum | да (из СМЭВ) | MATCH, MISMATCH, TIMEOUT, NOT_CALLED | MATCH | SMEV_MISMATCH, EXTERNAL_TIMEOUT | MATCH → дальше, MISMATCH → отказ, остальное → EXTERNAL_TIMEOUT. В режиме csv пустое значение считается NOT_CALLED |

## Формула dti

```
r       = ставка_продукта / 12
платёж  = amount × r / (1 − (1 + r)^(−term))
dti     = round(платёж / income, 2)
отказ, если dti > 0.50
```

Эталон applications.csv считает dti по единой ставке 18%: формула воспроизводит dti проверенных заявок с точностью до округления. Прототип берёт ставку продукта из products.csv (12–22%), поэтому для Премиум, Студенческого и Пенсионного его dti ниже эталонного. Пример: app_0002 в эталоне 0.72 (18%), в прототипе 0.66 (Студенческий, 12%).

Сравнение идёт по округлённому значению: у app_0068 точный dti = 0.5001, округлённый 0.50, и HIGH_DTI не сработал.

## Безопасность passport_hash

16 hex-символов — это 64 бита, усечённый SHA-256. Номер паспорта содержит всего 10 цифр (~10¹⁰ вариантов), поэтому хэш без секрета восстанавливается перебором за минуты. Для промышленной версии рекомендуем HMAC-SHA-256 с секретом в хранилище ключей. По данным можно проверить формат и уникальность хэшей, но не алгоритм.

## Качество данных: выявленные расхождения

| app_id | Что в данных | Что ожидается по правилам |
| - | - | - |
| app_0005 | HIGH_DTI при dti = 0.39 | AMOUNT_LIMIT: dti ≤ 0.5, но 800 000 > 10 × 60 000 |
| app_0012 | HIGH_DTI при dti = 0.24 | Одобрение: dti ≤ 0.5, score 640 |
| app_0007 | APPROVED при smev_status = MISMATCH | REFUSED, SMEV_MISMATCH |
| app_0009 | AMOUNT_LIMIT при amount = 8.75 × income | Одобрение: сумма ≤ 10 × income |
| app_0004 | Пустой age, причина только MISSING_INCOME | Прототип тоже останавливается на первой ошибке; клиенту стоит сообщать обо всех пропущенных полях |
| app_0010 / app_0017 | bki_score пуст у одного безработного и заполнен у другого | В прототипе unemployed отсекается на валидации, БКИ не вызывается |
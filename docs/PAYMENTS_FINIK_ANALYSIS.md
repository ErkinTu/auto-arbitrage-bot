# Платежи: переход с FreedomPay на Finik (finik.kg) — анализ и план реализации

Дата: 2026-07-31. **Статус на 2026-08-04: план РЕАЛИЗОВАН в коде** (шаги 1–13
§7), 130 тестов зелёные, локально применено. Не сделано: шаг 0 (аккаунт и ключи
Finik), e2e на beta (§8) и прод-деплой (§9) — они требуют кредов от Finik.
Актуальный статус по пунктам — `TODO.md § Переход на Finik`.

**Решение (принято заказчиком 2026-07-31):** второго провайдера НЕ делаем.
**FreedomPay удаляем из проекта целиком**, вместо него — **Finik Web SDK**.

Это безопасно, и вот чем это подтверждено — **сверено с продом напрямую
2026-07-31** (важно: таблица `TODO.md` со статусами на 2026-07-25 устарела,
код подписки на прод всё-таки уехал спящим, коммит `1080a8d`):

| Проверка на `63.180.133.249` | Факт |
|---|---|
| `app/services/freedompay.py` | на проде **есть** (задеплоен спящим) |
| таблица `payments` | **существует**, колонка `freedompay_payment_id` на месте |
| строк в `payments` | **0** |
| `bot_users` с `access_status='subscriber'` | **0** |
| `bot_users` с непустым `subscription_expires_at` | **0** |
| `FREEDOMPAY_*` / `PUBLIC_BASE_URL` в прод-`.env` | пусты → `subscription_enabled=false` |

То есть: **платежей и подписчиков не существует, терять нечего**, но схема на
проде уже создана — значит переименование колонки требует ручного DDL (alembic
нет). Поскольку данных 0, проще не `ALTER`, а `DROP TABLE payments` — пересоздастся
`init_db()` правильной (шаг 4).

Задачи в бэклоге — `TODO.md § Переход на Finik`. Объяснение потока простым
языком (по схеме `../web-sdk-flow.png`, без кода) — `FINIK_FLOW.md`.

---

## 1. Источники

Всё ниже проверено по первичным источникам, а не по памяти:

| Что | Ссылка |
|---|---|
| Web SDK — начало работы | https://www.finik.kg/documentation/web-sdk/ |
| Web SDK — интеграция (эндпоинты, пример запроса) | https://www.finik.kg/documentation/web-sdk/integration/ |
| Web SDK — вебхуки (payload, ретраи) | https://www.finik.kg/documentation/web-sdk/webhooks/ |
| Web SDK — справочник (ошибки, публичные ключи) | https://www.finik.kg/documentation/web-sdk/reference/ |
| Индекс документации | https://www.finik.kg/documentation/ |
| Payments Gateway API (другой продукт, НЕ наш) | https://www.finik.kg/documentation/payments-gateway/ |
| Возможности и тарифы (2% QR / от 2.9% Visa) | https://www.finik.kg/features/ |
| Для разработчиков (список SDK) | https://www.finik.kg/en/for-developers/ |
| Библиотека подписи (алгоритм) | https://registry.npmjs.org/@mancho.devs/authorizer (v2.12.8) |
| Схема потока | `../web-sdk-flow.png` (сохранена со страницы Web SDK) |
| FreedomPay (для сравнения, движок PayBox) | https://freedompay.kz/docs-en/merchant-api/ |

Поддержка Finik: `finik@quickpay.kg`, Telegram-поддержка, страница онбординга
`finik.kg/onboarding/`.

---

## 2. Аналитика: почему Finik лучше под наш продукт

### 2.1 Кто наш плательщик

Физлицо в Кыргызстане, которое пришло в Telegram-бота за уведомлениями «авто
ниже рынка». Платит **со телефона**, разово, 500 сом за 30 дней, вероятно один
раз в месяц вручную. Из этого следуют приоритеты: **минимум шагов на телефоне**,
**никакого ввода карты**, и **дешёвый онбординг мерчанта** (MVP, не банк).

### 2.2 Ключевое различие — способ оплаты

| | Finik | FreedomPay |
|---|---|---|
| Что видит юзер | ссылка → QR-страница, на телефоне **сама открывает банковское приложение**, там подтверждение оплаты | hosted-форма: номер карты, срок, CVV, часто 3-D Secure с SMS |
| Шагов на телефоне | нажал кнопку → банк-апп → подтвердил | нашёл карту → 16 цифр → срок → CVV → SMS-код |
| Карты | тоже поддерживаются (в webhook приходит `service: {id: "VISA"}`) | основной способ |

В КР оплата из банковского приложения (MBank, O!Bank, Optima, Bakai и т.п.) —
привычная бытовая механика, а ввод карточных данных в мобильном браузере
отпугивает. Это единственный аргумент, который реально влияет на выручку:
**конверсия «нажал оплатить» → «оплатил»**. Всё остальное — эргономика разработки.

### 2.3 Онбординг мерчанта

- **Finik:** ИП — регистрация и **онлайн-идентификация прямо в приложении Finik**;
  организации — через представителей. Затем в кабинете создаётся **API-ключ типа
  «Веб-клиент» (WEB)** с загрузкой нашего публичного ключа.
- **FreedomPay:** заявка (`freedompay.kg/anketa`) → кабинет → сканы учредительных
  документов → **договор с досылкой бумажных оригиналов** в офис (см.
  `../DEPLOY.md § Подключение мерчанта`). Предусловия: юрлицо/ИП + расчётный счёт.

Для MVP это разница между «начали принимать деньги на этой неделе» и «через
две-три недели, когда дойдёт курьер».

### 2.4 Экономика

Finik публикует ставки: **2% на QR, от 2.9% на Visa, без абонплаты и скрытых
комиссий**, платим только с успешных платежей. У FreedomPay KG публичных ставок
нет вовсе — договорные (ориентир рынка 2–4%, см. `../DEPLOY.md § Тарифы FreedomPay`).

При цене 500 сом и периоде 30 дней:

| Ставка | Комиссия с платежа | Остаётся | В год с одного подписчика (12 платежей) |
|---|---:|---:|---:|
| 2% (QR Finik) | 10 сом | 490 сом | комиссия 120 сом |
| 2.9% (Visa Finik) | 14.5 сом | 485.5 сом | комиссия 174 сом |
| 4% (пессимистичный договорной) | 20 сом | 480 сом | комиссия 240 сом |

Вывод: при таком чеке процент не критичен. **Критичен был бы фикс за
транзакцию** — вопрос №6 в §11: если у Finik есть минимальная комиссия
(скажем 5–10 сом), при чеке 500 сом это уже 1–2% сверху, и тогда экономнее
продавать период 90 дней одним платежом.

### 2.5 Безопасность протокола

| | Finik | FreedomPay |
|---|---|---|
| Подпись | RSA-2048, SHA-256, base64. **Мы** держим приватный ключ, Finik — публичный; их webhook подписан **их** приватным ключом | MD5 от конкатенации полей с **общим** `secret_key` |
| Кто может подделать наш запрос | никто без нашего приватного ключа | любой, у кого утёк общий секрет (включая сторону PSP) |
| Что покрывает подпись | метод, path, host, `x-api-*` заголовки, query, тело | значения `pg_*` полей + имя скрипта |
| Хеш | SHA-256 | MD5 (коллизии на произвольных префиксах практичны; для HMAC-подобной схемы не смертельно, но это 2026 год) |

Асимметрия — объективно лучше: **утечка нашего `.env` не даёт возможности
подделать webhook** (для этого нужен приватный ключ Finik), тогда как в схеме
FreedomPay один и тот же `secret_key` и подписывает, и проверяет.

### 2.6 Надёжность доставки — тут Finik слабее, и это надо закрывать кодом

| | Finik | FreedomPay |
|---|---|---|
| Ретраи webhook | at-least-once, задокументированы: первые 5 попыток каждые 30 с, дальше удвоение (60→120→240…) до максимума 24 ч, затем раз в сутки | есть, описаны хуже (~каждые 30 мин в течение 2 ч) |
| Webhook при неуспехе | **не присылается вообще** | присылается (`pg_result=0`) |
| Проверка статуса по запросу | **не задокументирована** для acquiring | `get_status.php` |
| Возвраты (refund) | в доках Web SDK нет | в API PayBox есть |

Три следствия, каждое становится пунктом реализации:

1. Строка `payments` в статусе `pending` **никогда не закроется сама** →
   нужен джанитор в worker (шаг 10).
2. Если webhook не доставится за ~сутки ретраев (наш домен лежал, сертификат
   истёк) — **сверять статус нечем**. Митигация: (а) вопрос №2 к Finik про
   статус-эндпоинт, (б) ручная процедура: найти платёж в кабинете Finik и
   продлить подписку CLI-командой (шаг 11).
3. Возвраты — через кабинет вручную (уточнить, вопрос №4).

### 2.7 Качество документации

Доки Finik тонкие: единицы `Amount` не указаны, лимитов нет, **алгоритм подписи
описан словами** («канонизируем метод, path, host и `x-api-*`»), а точная
каноникализация живёт только в npm-пакете `@mancho.devs/authorizer`. Для Python
готового SDK нет. Это был главный риск оценки — **он снят**: я распаковал пакет,
восстановил алгоритм и сверил Python-порт с настоящей библиотекой байт в байт
(§4). Остаточный риск — Finik молча поменяет каноникализацию в новой версии
библиотеки; страховка — тесты на векторах и понятная ошибка `401` при расхождении.

### 2.8 Итоговая сводка

| Критерий | Finik | FreedomPay | Кто лучше |
|---|---|---|---|
| Конверсия оплаты на телефоне | QR → банк-апп | ввод карты | **Finik** ⭐ |
| Скорость онбординга | ИП онлайн в приложении | договор + бумаги почтой | **Finik** ⭐ |
| Прозрачность тарифов | 2% / 2.9% публично | договорные | **Finik** |
| Криптография подписи | RSA-2048/SHA-256 | MD5 + общий секрет | **Finik** |
| Ретраи webhook | описаны, до суток | описаны хуже | **Finik** |
| Webhook при неуспехе | нет | есть | FreedomPay |
| Проверка статуса по запросу | не задокументирована | есть | FreedomPay |
| Refund через API | нет | есть | FreedomPay |
| Качество доков | тонкие | подробные | FreedomPay |
| Зависимости у нас | +`cryptography`, свой порт подписи | stdlib | FreedomPay |
| Готовность у нас | 0% | 100%, спит, покрыто тестами | FreedomPay |

Плюсы Finik — продуктовые (деньги и время до запуска), минусы — инженерные
(один день работы + два джанитора). Поэтому переход оправдан.

---

## 3. Протокол Finik Web SDK (полная спецификация)

### 3.1 Не перепутать два продукта

| Продукт | Хост | Назначение |
|---|---|---|
| **Web SDK (acquiring)** | `api.acquiring.averspay.kg` | **наш кейс:** принимать деньги |
| Payments Gateway | `api.paymentsgateway.averspay.kg` | платить *за* услуги (`/v2/services`, `/v2/recipient`, `/v2/payment`, `GET /v2/payments/{id}`) |

Общий у них только алгоритм подписи. `GET /v2/payments/{paymentId}` (проверка
статуса) относится к Payments Gateway — **на acquiring он не задокументирован**,
и рассчитывать на него нельзя, пока Finik не подтвердит (вопрос №2).

За Finik стоит Avers/QuickPay (`averspay.kg`); заявлен статус официального Visa
Payment Facilitator.

### 3.2 Среды

| Среда | Эндпоинт |
|---|---|
| Beta | `https://beta.api.acquiring.averspay.kg/v1/payment` |
| Prod | `https://api.acquiring.averspay.kg/v1/payment` |

Публичные ключи Finik (для проверки webhook) **разные по среде**, оба
2048-битные RSA, опубликованы в `§ Справочник`:

```
prod: MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAuF/PUmhMPPidcMxhZBPb…tQIDAQAB
beta: MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAwlrlKz/8gLWd1ARWGA/8…ZwIDAQAB
```

Полные PEM — на странице справочника; в код кладём оба и выбираем по
`FINIK_ENV=beta|prod` (шаг 3).

### 3.3 Поток (соответствует `../web-sdk-flow.png`)

```
1. бот  --POST /v1/payment (x-api-key, x-api-timestamp, signature)-->  Finik
2. бот  <--302 Location: https://qr.finik/<path>---------------------  Finik
3. юзер --жмёт inline-кнопку с этим URL--> страница Finik --> банк-апп --> оплата
4. юзер <--302 Location: RedirectUrl--------  Finik   (возврат «сразу», НЕ подтверждение)
5. api  <--POST webhookUrl (подписан приватным ключом Finik)--  Finik  ← ИСТОЧНИК ИСТИНЫ
6. api  --200 OK-->  Finik
```

Два принципиальных момента, оба прямо прописаны в доках:

- **Шаг 2 нельзя автоследовать.** Ответ 302, ссылка на оплату — в заголовке
  `Location`. В `httpx` следование редиректам по умолчанию выключено
  (`follow_redirects=False`), но это надо закрепить явно, чтобы никто не «починил»
  случайно.
- **Шаг 4 — не подтверждение оплаты.** Юзера возвращают сразу; финализировать
  подписку можно **только по webhook** (шаг 5).

### 3.4 Запрос `POST /v1/payment`

Заголовки:

| Заголовок | Значение |
|---|---|
| `x-api-key` | ключ из кабинета Finik (тип WEB) |
| `x-api-timestamp` | текущее время в **миллисекундах** UNIX; живёт ~10 с |
| `signature` | RSA-SHA256(каноническая строка), base64 |
| `content-type` | `application/json` |

Тело:

| Поле | Тип | Обяз. | Смысл |
|---|---|---|---|
| `Amount` | number | нет | фиксированная сумма; без него сумму вводит клиент — **нам обязательно задавать** |
| `CardType` | string | да | всегда `FINIK_QR` |
| `PaymentId` | string | да | уникальный **UUID**, наш идентификатор платежа |
| `RedirectUrl` | string | да | куда вернуть юзера после оплаты |
| `Data.accountId` | string | да | наш счёт в Finik |
| `Data.name_en` | string | да | имя, которое видит плательщик на QR |
| `Data.webhookUrl` | string | да | **per-payment**, в кабинете настраивать не надо |
| `Data.description` | string | нет | описание на странице оплаты |
| `Data.startDate` / `endDate` | number | нет | срок жизни QR, UNIX ms |
| `Data.additionalData` | array | нет | до 20 своих полей; вернутся в `webhook.fields` |

Ошибки: `400` — кривое тело, невалидный/просроченный `x-api-timestamp`,
`Amount must be greater than 0`; `401` — `An invalid signature is provided`;
`403` — доступ запрещён.

### 3.5 Webhook

```json
{
  "id": "transaction-id-15423_DEBIT",
  "transactionId": "transaction-id-241234",
  "status": "success",
  "amount": 100,
  "fields": {
    "amount": 100,
    "paymentId": "payment-id-241234",
    "fieldId1": "value1",
    "fieldId2": "value2"
  },
  "requestDate": 1737369012345,
  "transactionDate": 1737369012345,
  "service": { "id": "VISA" }
}
```

Что важно:

- **Связь с нашим заказом — `fields.paymentId`** (тот `PaymentId`, что мы
  отправили). На верхнем уровне нашего идентификатора нет — по `id`/`transactionId`
  искать нечего, это идентификаторы Finik.
- Наши `additionalData` возвращаются в том же `fields` по своим `fieldId`.
- `status` проверять **регистронезависимо**.
- Идемпотентность — по `transactionId` (у нас плюс `order_id`).
- Ответ — **`200 OK` сразу**, тяжёлое делать асинхронно.
- `service.id` есть только у карточных платежей (у QR его не будет).
- **Webhook приходит только при успехе.** Неуспех/просрочка — тишина.
- Ретраи: 5 попыток по 30 с, далее удвоение до 24 ч, затем раз в сутки.

---

## 4. Алгоритм подписи: восстановлен и проверен

Доки описывают подпись словами. Точная каноникализация — в
`@mancho.devs/authorizer` (`Signer.getData()`). Я скачал пакет с npm, прочитал
`package/index.js`, портировал на Python и **сверил с настоящей JS-библиотекой:
совпали и канонические строки, и итоговые base64-подписи** на двух кейсах
(процедура сверки — приложение А).

### 4.1 Каноническая строка

Части соединяются через `\n`:

```
1. httpMethod в нижнем регистре                              → "post"
2. decodeURI(path)                                           → "/v1/payment"
3. "host:<Host>" + для каждого x-api-* "&<ключ-в-нижнем>:<значение>",
   ключи отсортированы; ВСЁ остальное (content-type, X-Forwarded-*) НЕ участвует
4. query — ТОЛЬКО если непустой: "k=v" через "&", ключи sort(),
   и ключ, и значение через encodeURI(decodeURI(x))
5. тело: JSON без пробелов, отсортированы ТОЛЬКО ключи верхнего уровня
   (вложенные объекты — в исходном порядке!), юникод НЕ эскейпится;
   если тела нет — пустая строка
```

Подпись = `RSA-SHA256(строка)` → base64, PKCS#1 v1.5 (`openssl dgst -sha256 -sign`).

### 4.2 Проверенный Python-порт

```python
import json, urllib.parse

def canonical(*, method, path, headers, body=None, query=None) -> str:
    parts = [method.lower(), urllib.parse.unquote(path)]

    host = headers.get("Host") or headers.get("host")
    if not host:
        raise ValueError("Header 'Host' is required")
    header_parts = [f"host:{host}"]
    for key in sorted(k for k in headers if k.lower().startswith("x-api-")):
        header_parts.append(f"{key.lower()}:{headers[key]}")
    parts.append("&".join(header_parts))

    if query:
        safe = "!#$&'()*+,/:;=?@[]~-_.!~*'()"  # то, что encodeURI не кодирует
        qs = "&".join(
            f"{urllib.parse.quote(urllib.parse.unquote(k), safe=safe)}="
            f"{urllib.parse.quote(urllib.parse.unquote(query[k] or ''), safe=safe)}"
            for k in sorted(query)
        )
        if qs:
            parts.append(qs)

    parts.append(
        json.dumps({k: body[k] for k in sorted(body)}, separators=(",", ":"), ensure_ascii=False)
        if body else ""
    )
    return "\n".join(parts)
```

### 4.3 Векторы для тестов (получены из настоящей JS-библиотеки)

Ключевая пара — тестовая, из `package/__test__/testData.js` пакета
(`-----BEGIN RSA PRIVATE KEY-----MIIEowIBAAKCAQEAoq/3Uxpz…`, публичный —
`…MIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8AMIIBCgKCAQEAoq/3Uxpz…`).

**Вектор 1** — пример из README библиотеки (юникод в query, лишние заголовки,
которые НЕ должны попасть в подпись):

```
post
/services
host:api.paymentsgateway.averspay.kg&x-api-header:header value&x-api-key:h8z1TDStxu5YY2YuN8jUa9hpzIVbfkLT7kPiPiYj&x-api-timestamp:1636026186643
best=%D1%82%D0%B5%D1%81%D1%82%20%D0%B4%D0%B0%D1%82%D0%B0&from=0&size=10
{"amount":100,"payment":"payment"}
```
подпись: `etPE40DBxzNIXT9UX8St905X2RijGPLGNw68SEn2vPB5aoCnShnVW1vJJGgKX65wio2g3dwZIGME+dKH7UVV0kNdx/0WmRcs21I2/4k/qUskxlDGgytGvRD2Ms+eb8h1VE1j5NZLeGwWDE7unXx3OkkjstDTsbnGK0kw6wuoFJMj60zElREE0853OMLZVsYBDHSDfUhwZopfxnBFl24rZHiN9WKllqiOd0WBm7+0Rtw+EG4/G8N7/XEDfz3KoSoIM4tyBAU+PSEOxy2VeVO4mXDcHBPZJzcNnGcYKawG1vwRId6dGS426a8QkE09ZW19baihMJZmp7LN4UWyACGn+w==`

**Вектор 2** — реальное тело Finik-платежа (вложенный `Data` с намеренно
неотсортированными ключами, кириллица в `name_en`):

```
post
/v1/payment
host:api.acquiring.averspay.kg&x-api-key:test-key&x-api-timestamp:1737369012345
{"Amount":500,"CardType":"FINIK_QR","Data":{"webhookUrl":"https://x.kg/hook","accountId":"acc-1","name_en":"Подписка №1","additionalData":[{"fieldId":"order","value":"abc"}]},"PaymentId":"00000000-0000-0000-0000-000000000000","RedirectUrl":"https://example.com/success"}
```
подпись: `NvYRs9F5V33D9xtLzbAZKCyzSzpsl98TruszP8q+GiG7nTiSjPB6x4txN7mJseRLfPp4MJcgKxt89dfj3F7zlmWgFkQpEpylrEjTRpbbCW8Iu99KNWPM2vuy+rB8tlyBvX/qQmGygpIx+MVWE3C8fR+KHNkbA6dLA+DMWfhmz/3l+2egjSXAtbT2LnyR1WX0qD0T/QAO9ckur1i9lsXbQRnd/no9qHT5DtFW/VAUeHrtNXBBOp1a1lwXfdyvJaHklbjTtRZEY+hmGVwz0EmU8ykN6mfI3ajb4hZ1lUF6kZHY3ADd/y2AjjpZxKxS4WWD/06c0IIAV0KsFaw5h0fE1w==`

Обратите внимание на вектор 2: во вложенном `Data` порядок ключей
`webhookUrl, accountId, name_en, additionalData` **сохранён как есть** —
сортируется только верхний уровень. Тест на этом и должен ловить регрессию.

---

## 5. Что удаляем (инвентарь FreedomPay)

Упоминания есть в 19 файлах. Полностью удаляются:

| Файл | Действие |
|---|---|
| `app/services/freedompay.py` | **удалить** (147 строк) |
| `tests/test_freedompay.py` | **удалить** (60 строк), заменить на `tests/test_finik.py` |

Правятся (убрать FreedomPay-специфику, поставить Finik):

| Файл | Что убрать |
|---|---|
| `app/api/payments.py` | роут `/result` (form-data, `pg_sig`, XML-ответ), `build_callback_response`, `_xml` |
| `app/core/config.py` | `freedompay_merchant_id/secret_key/base_url/testing_mode/payment_lifetime_seconds`, свойства `freedompay_{result,success,failure}_url`, условие в `subscription_enabled` |
| `app/models/entities.py` | `Payment.freedompay_payment_id` → `finik_transaction_id`; докстринги |
| `app/services/subscription.py` | параметр `freedompay_payment_id` → `provider_txn_id`; докстринг |
| `app/bot/handlers.py` | импорт `init_payment` из `services.freedompay` → `services.finik` |
| `app/bot/messages.py:52` | «оплатить через FreedomPay» → «оплатить через Finik (QR / банковское приложение)» |
| `app/bot/keyboards.py:15` | докстринг |
| `app/bot/db.py` | `create_pending_payment`: `uuid4().hex` → `str(uuid.uuid4())` (Finik требует UUID) + докстринг |
| `tests/test_payments_webhook.py` | переписать под Finik (JSON + RSA вместо form-data + MD5) |
| `.env.example` | блок `FREEDOMPAY_*` → `FINIK_*` |
| `DEPLOY.md` | `§ Подписка/FreedomPay` (стр. 267–331) → `§ Подписка/Finik`; тарифы и онбординг переписать |
| `CLAUDE.md` | абзац «Подписка (freedompay.kg…)» в «Ключевых фактах» + строка индекса |
| `docs/TODO.md` | закрыть раздел про FreedomPay, добавить `§ Переход на Finik` |
| `Caddyfile` | только комментарий (пути `/api/v1/payments/*` те же) |
| `docker-compose.yml` | комментарий + **volume с приватным ключом** для `api` и `bot` |
| `pyproject.toml` | **убрать** `python-multipart` (был нужен только для form-data webhook FreedomPay), **добавить** `cryptography` |

Код FreedomPay на проде **есть** (уехал спящим, `1080a8d`), но платежей и
подписчиков — 0, поэтому удаление всё равно чистое: без миграции данных и без
периода совместимости. Единственный ручной DDL — пересоздать `payments` (шаг 4).

---

## 6. Целевая архитектура

Инфраструктурно **не меняется ничего**: Caddy остаётся единственным публичным
входом и пробрасывает только `/api/v1/payments/*`; `api` и `postgres` — на
`127.0.0.1`; бот читает БД напрямую и работает на long-polling.

```
 бот (aiogram)                          api (FastAPI)                worker
   /subscribe                             POST /api/v1/payments/finik   джанитор pending
      │                                        ▲                        напоминания
      │ create_pending_payment (БД)             │ 200 OK
      │ finik.init_payment ──HTTPS──► Finik ────┘ (webhook, подписан Finik)
      │      ◄── 302 Location                        ▲
      └── inline-кнопка с этим URL ──► юзер ─────────┘ оплата в банк-аппе
                                          └──302 RedirectUrl──► t.me/<bot>
```

Кто какой ключ держит:

| Компонент | Нужен | Зачем |
|---|---|---|
| `bot` | **наш приватный** RSA-ключ + `x-api-key` + `accountId` | подписать `POST /v1/payment` |
| `api` | **публичный ключ Finik** (константа в коде по среде) | проверить подпись webhook |
| `worker` | — | джанитор и напоминания в БД, к Finik не ходит |

Приватный ключ — **файлом**, смонтированным в контейнеры (`FINIK_PRIVATE_KEY_PATH`),
а не многострочным значением в `.env`: не утечёт в `docker inspect`/логи и
переносится обычным `scp` с правами `600`.

---

## 7. Шаги реализации

Порядок такой, что после каждого шага проект остаётся зелёным (`pytest`, `ruff`).

### Шаг 0. Аккаунт и ключи (блокирует шаги 5+, делать первым)

```bash
openssl genrsa -out finik_private.pem 2048
openssl rsa -in finik_private.pem -pubout > finik_public.pem
chmod 600 finik_private.pem          # в git НЕ добавлять
```

Затем: корпоративный счёт (ИП — онлайн-идентификация в приложении Finik),
в кабинете — API-ключ типа **«Веб-клиент» (WEB)** с загрузкой `finik_public.pem`.
На выходе нужны три значения: `FINIK_API_KEY`, `FINIK_ACCOUNT_ID` и доступ к beta.
Параллельно отправить вопросы из §11.

### Шаг 1. Зависимости

`pyproject.toml`: `+ "cryptography>=43,<46"`, `- "python-multipart…"`.
Затем `uv lock && uv sync` и пересборка образа (код запечён в образ, bind-mount нет).

### Шаг 2. `app/services/finik.py` — клиент (новый файл, ~170 строк)

Структура (по образцу `services/exchange.py`: синхронный `httpx`, вызывается из
бот-цикла через `asyncio.to_thread`):

```python
"""Клиент Finik Web SDK (acquiring): создание платежа и проверка webhook.

Подпись: RSA-SHA256(каноническая строка), base64 — схема восстановлена из
@mancho.devs/authorizer и сверена с ней на векторах (см.
docs/PAYMENTS_FINIK_ANALYSIS.md §4). Каноническая строка:
  method.lower() \n unquote(path) \n host:<h>&<x-api-*>… \n [query] \n JSON(тело)
Сортируются ТОЛЬКО ключи верхнего уровня тела; вложенные — как есть.
"""

CARD_TYPE_QR = "FINIK_QR"
PAYMENT_PATH = "/v1/payment"

FINIK_PUBLIC_KEYS = {          # из § Справочник, разные по среде
    "prod": "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A…",
    "beta": "-----BEGIN PUBLIC KEY-----\nMIIBIjANBgkqhkiG9w0BAQEFAAOCAQ8A…",
}

def canonical(*, method, path, headers, body=None, query=None) -> str: ...   # §4.2

def js_json(body: Mapping[str, Any]) -> str:
    """JSON как его сериализует JS: верхний уровень отсортирован, целые float → int."""

def sign(data: str, private_key_pem: str) -> str:      # base64
def verify(data: str, signature_b64: str, public_key_pem: str) -> bool

@dataclass(slots=True)
class InitPaymentResult:
    ok: bool
    redirect_url: str | None = None
    status_code: int | None = None
    error: str | None = None

def init_payment(*, order_id: str, amount: Decimal, description: str) -> InitPaymentResult:
    """POST /v1/payment → 302, ссылка на оплату в заголовке Location."""

def verify_webhook(*, method: str, path: str, headers: Mapping[str, str],
                   body: Mapping[str, Any], signature: str) -> bool:
    """Проверяет подпись входящего webhook публичным ключом Finik по среде."""
```

Три места, где легко ошибиться (замеры — §10):

- `js_json` **обязана** нормализовать целые float в int, иначе `amount: 1500.00`
  из webhook даст `1500.0` вместо `1500` и подпись не сойдётся.
- `httpx.post(..., follow_redirects=False)` — задать явно, ссылка нужна из
  `response.headers["location"]`; ожидаемый код `302`.
- `x-api-timestamp` = `str(int(time.time() * 1000))`, считать **прямо перед**
  запросом (окно ~10 с).

Тело запроса собирается так:

```python
body = {
    "Amount": _amount_number(amount),          # см. вопрос №1: сомы или тыйыны
    "CardType": CARD_TYPE_QR,
    "PaymentId": order_id,                     # UUID с дефисами
    "RedirectUrl": settings.finik_redirect_url,
    "Data": {
        "accountId": settings.finik_account_id,
        "name_en": settings.finik_qr_name,      # латиницей, видит плательщик
        "webhookUrl": settings.finik_webhook_url,
        "description": description,
        "endDate": int((time.time() + settings.finik_payment_lifetime_seconds) * 1000),
        "additionalData": [{"fieldId": "orderId", "value": order_id}],
    },
}
```

### Шаг 3. `app/core/config.py` — настройки

Заменить блок FreedomPay на:

```python
# --- Подписка / Finik (finik.kg, Web SDK) ---------------------------------
finik_api_key: str = ""
finik_account_id: str = ""
finik_private_key_path: str = "/run/secrets/finik_private.pem"
finik_env: str = "beta"                 # beta | prod — выбирает хост и публичный ключ
finik_qr_name: str = "Car Analytics Bot"   # name_en, латиницей
finik_payment_lifetime_seconds: int = 3600
public_base_url: str = ""                # остаётся как было
bot_username: str = ""                   # для RedirectUrl прямо в бота
subscription_price_kgs: Decimal = Decimal("500")
subscription_period_days: int = 30
subscription_reminder_days_before: int = 3
payment_pending_ttl_hours: int = 6       # джанитор: pending старше → expired
```

Свойства:

```python
@property
def subscription_enabled(self) -> bool:
    return bool(self.finik_api_key and self.finik_account_id and self.public_base_url)

@property
def finik_base_url(self) -> str:
    return ("https://api.acquiring.averspay.kg" if self.finik_env == "prod"
            else "https://beta.api.acquiring.averspay.kg")

@property
def finik_webhook_url(self) -> str:
    return f"{self.public_base_url.rstrip('/')}/api/v1/payments/finik"

@property
def finik_redirect_url(self) -> str:
    """Вернуть юзера сразу в бота, если известен username; иначе — наша страница."""
    if self.bot_username:
        return f"https://t.me/{self.bot_username.lstrip('@')}"
    return f"{self.public_base_url.rstrip('/')}/api/v1/payments/success"
```

Приватный ключ читать **лениво и кешированно** (`@lru_cache` на функцию-загрузчик
в `finik.py`), а не в `Settings`: пустой/отсутствующий файл не должен ронять
`api`, которому ключ не нужен вовсе.

`subscription_enabled` намеренно НЕ требует приватного ключа: пока
`FINIK_API_KEY`/`FINIK_ACCOUNT_ID`/`PUBLIC_BASE_URL` пусты — подписка спит и
поведение бота ровно как до подписки (гейт по группе). Это свойство сохраняем.

### Шаг 4. Модель `Payment`

```python
order_id: Mapped[str]              # = PaymentId в Finik, UUID с дефисами
finik_transaction_id: Mapped[str | None] = mapped_column(String(64))   # был freedompay_payment_id
status: ...                        # pending | paid | failed | expired
raw_callback: Mapped[dict | None]  # тело webhook как пришло
```

**Схема: DDL руками, alembic нет.** Таблица `payments` **уже создана и на проде,
и локально** (прежний код уехал спящим, коммит `1080a8d`), с колонкой
`freedompay_payment_id`. `create_all` существующую таблицу не меняет — сам он
колонку не переименует. Но строк в ней **0** (сверено с продом 2026-07-31), и
подписчиков тоже 0, поэтому вместо `ALTER TABLE … RENAME COLUMN` проще и честнее
пересоздать: `init_db()`/`create_all` поднимет её из актуальной модели.

Прогнать **и локально, и на проде** (при выключенных `api`/`worker`/`bot`, чтобы
никто не держал таблицу):

```bash
docker compose exec -T postgres psql -U car_user -d car_analytics \
  -c "SELECT count(*) FROM payments;"          # ОБЯЗАТЕЛЬНО: должно быть 0
docker compose exec -T postgres psql -U car_user -d car_analytics \
  -c "DROP TABLE IF EXISTS payments;"
# затем обычный старт: init_db()/create_all создаст payments по новой модели
```

Если к моменту миграции в `payments` уже появятся оплаченные строки (то есть
FreedomPay всё-таки успели запустить) — `DROP` **нельзя**, тогда:

```sql
ALTER TABLE payments RENAME COLUMN freedompay_payment_id TO finik_transaction_id;
```

Колонка `bot_users.renewal_reminded_at` на проде уже есть и остаётся как была —
она провайдеро-независима.

`app/services/subscription.py`: переименовать параметр
`freedompay_payment_id` → `provider_txn_id` и присваивать в
`payment.finik_transaction_id`. Остальная логика (`next_expiry`,
`is_subscription_active`) провайдеро-независима и **не меняется** — это
подтверждает, что швы были выбраны правильно.

### Шаг 5. `app/api/payments.py` — webhook

Полностью заменить `/result` на:

```python
@router.post("/finik")
async def finik_webhook(request: Request, session: Session = Depends(get_session)) -> Response:
    raw = await request.body()
    try:
        payload = json.loads(raw)
    except ValueError:
        return JSONResponse({"error": "bad json"}, status_code=400)

    ok = verify_webhook(
        method="POST",
        path=request.url.path,                       # за Caddy путь не меняется
        headers=dict(request.headers),               # Host + x-api-* берём как пришли
        body=payload,
        signature=request.headers.get("signature", ""),
    )
    if not ok:
        logger.warning("finik webhook: bad signature (payload=%s)", payload)
        return JSONResponse({"error": "invalid signature"}, status_code=401)
    ...
```

Дальше — порядок проверок (каждая на своём месте, и каждая покрыта тестом):

1. `order_id = str(payload.get("fields", {}).get("paymentId", ""))`; нет → `400`.
2. Найти `Payment` по `order_id`; нет → `404` (в лог: чужой/устаревший платёж).
3. **Идемпотентность:** `payment.status == "paid"` → сразу `200 {"status":"ok"}`
   (Finik ретраит, и подпись валидна вечно — это часть безопасности, не оптимизация).
4. `str(payload.get("status", "")).lower() != "success"` → лог + `200` (по докам
   такого быть не должно; молча игнорируем, но не ретраим).
5. **Сверка суммы:** `Decimal(str(payload["amount"])) == payment.amount`, иначе
   `logger.error` + `400`, подписку НЕ продлевать.
6. `apply_paid_payment(session, payment, provider_txn_id=payload.get("transactionId"),
   raw_callback=payload)` → `session.commit()`.
7. Уведомить юзера в Telegram и вернуть `200`.

Про «отвечать 200 сразу, тяжёлое — асинхронно»: у нас тяжёлого нет (одна
транзакция + один `sendMessage`), но `sendMessage` уводим в
`BackgroundTasks`/`asyncio.create_task`, чтобы залипший Telegram не растягивал
ответ webhook. Сейчас `_notify_paid` вызывается инлайн — это единственное
осмысленное изменение в нём.

Коды ответов важны семантически: `401`/`400` — Finik пусть ретраит, мы
подозреваем подделку/рассинхрон; `500` при ошибке БД — **нужно**, чтобы ретрай
пришёл; `200` — только когда подписка реально продлена (или уже была продлена).

Страницы `/success` и `/failure` оставить как фолбэк (если `BOT_USERNAME` не
задан), текст «FreedomPay» из них убрать.

### Шаг 6. `app/bot/db.py` — UUID

```python
order_id = str(uuid.uuid4())      # было uuid.uuid4().hex; Finik требует UUID
```
36 символов, в `String(64)` влезает.

### Шаг 7. `app/bot/handlers.py` + `messages.py` + `keyboards.py`

Функционально `/subscribe` не меняется — меняется импорт и текст:

```python
from app.services.finik import init_payment   # было app.services.freedompay
...
result = await asyncio.to_thread(init_payment, order_id=order_id,
                                amount=settings.subscription_price_kgs,
                                description=f"Подписка на {settings.subscription_period_days} дней")
```

`messages.py`: «Нажмите кнопку ниже, чтобы оплатить через **Finik** — откроется
QR-страница, на телефоне она сама предложит ваше банковское приложение».
Кнопка: `💳 Оплатить {price} сом` — как есть.

### Шаг 8. `docker-compose.yml` — приватный ключ в контейнеры

```yaml
  bot:
    volumes:
      - ./secrets/finik_private.pem:/run/secrets/finik_private.pem:ro
```

Монтировать нужно **только `bot`** (он один подписывает исходящие запросы);
`api` проверяет webhook публичным ключом Finik, который лежит константой в коде.
`./secrets/` — в `.gitignore`.

### Шаг 9. `.env.example` + `DEPLOY.md`

```
# Подписка / Finik (finik.kg, Web SDK). Пока FINIK_API_KEY/FINIK_ACCOUNT_ID или
# PUBLIC_BASE_URL пусты — подписка спит: /subscribe отвечает «недоступно»,
# доступ к боту гейтится только группой (как раньше).
FINIK_API_KEY=
FINIK_ACCOUNT_ID=
FINIK_ENV=beta                   # beta | prod — хост и публичный ключ Finik
FINIK_PRIVATE_KEY_PATH=/run/secrets/finik_private.pem
FINIK_QR_NAME=Car Analytics Bot  # name_en, латиницей: это видит плательщик
FINIK_PAYMENT_LIFETIME_SECONDS=3600
BOT_USERNAME=                    # для возврата из оплаты сразу в бота
PAYMENTS_DOMAIN=
PUBLIC_BASE_URL=
SUBSCRIPTION_PRICE_KGS=500
SUBSCRIPTION_PERIOD_DAYS=30
SUBSCRIPTION_REMINDER_DAYS_BEFORE=3
PAYMENT_PENDING_TTL_HOURS=6
```

`DEPLOY.md § Подписка/FreedomPay` → `§ Подписка/Finik`: домен и Caddy — как было,
дальше генерация ключей, загрузка публичного в кабинет, `scp` приватного в
`./secrets/`, `FINIK_ENV=beta` → обкатка → `prod`. Тарифы переписать на
публичные 2% / 2.9%.

### Шаг 10. Джанитор `pending` (новое, из-за «webhook только на успех»)

В `worker.py` рядом с `send_renewal_reminders_if_due`:

```python
def expire_stale_payments(session) -> int:
    """pending старше payment_pending_ttl_hours → expired: Finik про неуспех молчит."""
```

Запускать в том же суточном/часовом тике, что и напоминания. Без этого таблица
`payments` копит вечные `pending`, и любая аналитика по конверсии врёт.

### Шаг 11. Ручная сверка на случай потерянного webhook

CLI-команда в `app/cli.py`:

```
docker compose exec -T worker uv run python -m app.cli grant-subscription \
  --telegram-id 123456789 --days 30 --note "finik txn 241234, webhook не дошёл"
```

Внутри — тот же `next_expiry` + `access_status='subscriber'`. Это страховка на
отсутствие статус-эндпоинта (§2.6): платёж виден в кабинете Finik, продлеваем
руками, вместо того чтобы объясняться с оплатившим юзером.

### Шаг 12. Тесты

`tests/test_finik.py` (заменяет `test_freedompay.py`):

- `test_canonical_matches_reference_vector_1/2` — обе канонические строки из §4.3
  байт в байт.
- `test_signature_matches_reference_vector_1/2` — подпись тестовым ключом равна
  зафиксированной base64-строке.
- `test_nested_data_keys_are_not_sorted` — во вложенном `Data` порядок сохранён.
- `test_only_host_and_x_api_headers_are_signed` — `content-type`,
  `X-Forwarded-For` не влияют на подпись.
- `test_integral_floats_serialize_without_fraction` — `1500.00 → "1500"` (§10.1).
- `test_top_level_key_sort_is_ascii_for_our_payloads` — фиксирует допущение из §10.2.
- `test_init_payment_reads_location_from_302` — мок `httpx`, редирект не следуется.
- `test_init_payment_returns_error_on_401` — понятная ошибка вместо исключения.

`tests/test_payments_webhook.py` (переписать): валидная подпись → подписка
продлена; битая подпись → `401`, БД не тронута; повтор того же webhook →
второго продления нет; сумма не совпала → `400` и `status != 'paid'`;
`status: "SUCCESS"` (другой регистр) → проходит; `amount: 1500.00` → подпись
проверяется (регрессия §10.1); неизвестный `fields.paymentId` → `404`.

Ожидаемо: было 97 тестов, станет ~105.

---

## 8. Тест-план e2e (beta, до боевого запуска)

Webhook требует публичный HTTPS, поэтому локально — туннель:

```bash
cloudflared tunnel --url http://localhost:8000     # или: ngrok http 8000
# полученный https-URL → PUBLIC_BASE_URL в .env, контейнеры пересоздать
```

Цепочка проверки (`FINIK_ENV=beta`, сумма 1 сом — заодно закрывает вопрос №1 про
единицы `Amount`):

1. `/subscribe` → бот присылает кнопку (значит `init_payment` вернул `Location`).
2. Открыть с телефона → QR-страница Finik → **проверить, что открывается
   банковское приложение** (главная продуктовая гипотеза §2.2).
3. Оплатить → прилетает webhook → в логах `api` подпись валидна, ответ `200`.
4. В БД: `payments.status='paid'`, `finik_transaction_id` заполнен,
   `bot_users.subscription_expires_at` = +30 дней, `access_status='subscriber'`.
5. Юзеру пришло «Оплата получена…», редирект вернул его в бота (`BOT_USERNAME`).
6. `/status` показывает дату; `/add_filter` доступен **не члену группы**.
7. **Идемпотентность:** повторить тот же webhook (`curl` с сохранёнными
   заголовками и телом) → `200`, срок НЕ продлился второй раз.
8. **Битая подпись:** тот же запрос с испорченным `signature` → `401`, в БД ничего.
9. **Джанитор:** сделать `/subscribe` и не платить → через
   `PAYMENT_PENDING_TTL_HOURS` статус `expired`.
10. Напоминание: подписчику с истечением ≤3 дней worker шлёт `/subscribe`.
11. Переключить `FINIK_ENV=prod`, боевые `FINIK_API_KEY`/`FINIK_ACCOUNT_ID`,
    прогнать пункты 1–5 на реальный минимальный чек.

---

## 9. Прод-деплой

Инфраструктура уже описана и не меняется: домен → A-запись на `63.180.133.249`,
порты 80/443 в Security Group, `docker compose --profile payments up -d --build`.

Отличия от прежнего чек-листа:

1. `scp` приватного ключа в `./secrets/finik_private.pem` (`chmod 600`), в git его нет.
2. `.env`: `FINIK_*`, `BOT_USERNAME`, `PAYMENTS_DOMAIN`, `PUBLIC_BASE_URL`.
   **`rsync` строго с `--exclude '.env'`** — иначе затрём боевой
   `LALAFO_PROXY_URL` и lalafo сразу упрётся в Cloudflare-403.
3. **Схема — единственный ручной DDL:** `DROP TABLE payments` (в ней 0 строк,
   сверено 2026-07-31), дальше `init_db()` создаст её по новой модели с
   `finik_transaction_id`. Порядок: остановить `api`/`worker`/`bot` → проверить
   `count(*) = 0` → `DROP` → поднять с новым кодом. `bot_users.renewal_reminded_at`
   на проде уже есть, трогать не надо. **Помнить про стартовый DDL-затор**
   (память `prod-deploy-ddl-lock-race`): `up -d --build` пересоздаёт
   `api`+`worker`+`bot` разом, и DDL может встать на idle-in-transaction сессии
   → лечится `pg_terminate_backend` корневой транзакции.
4. Проверка: `curl https://<домен>/api/v1/payments/finik` → `400/401` (жив, но без
   подписи не работает); любой другой путь наружу → `404`.
5. **Синхронность часов.** `x-api-timestamp` живёт ~10 с → на сервере должен
   работать NTP (`timedatectl status` → `System clock synchronized: yes`).
   Контейнеры в UTC, это нормально: timestamp — epoch, к таймзоне нечувствителен.

---

## 10. Грабли, проверенные экспериментально

### 10.1 Целые float ломают проверку подписи webhook — критично

Проверять подпись входящего webhook приходится по **пересобранному** JSON
(верхний уровень сортируется → сырые байты не подходят). А JS и Python
по-разному печатают целые числа с дробной частью:

```
вход:  {"status":"success","amount":1500.00,"fee":0.1,"big":1e21,"t":1737369012345}
JS  :  {"amount":1500,"big":1e+21,"fee":0.1,"status":"success","t":1737369012345}
PY  :  {"amount":1500.0,…}      ← подпись НЕ сойдётся
PY* :  {"amount":1500,…}        ← после нормализации integral float → int, совпало
```

`1e21` совпадает и так (оба дают `1e+21`). Лечение — в `js_json()`: если
`isinstance(v, float) and v.is_integer()` → `int(v)`. Обязателен тест
(§7 шаг 12), иначе первый же платёж на «ровную» сумму отвалится с `401`, и
искать причину придётся в байтах.

### 10.2 JS сортирует ключи через `localeCompare`, а не по кодпоинтам

```
ключи: ["Amount","accountId","Zebra","_x","name_en","nameEn"]
JS localeCompare: _x, accountId, Amount, name_en, nameEn, Zebra
Python sorted   : Amount, Zebra, _x, accountId, nameEn, name_en
```

**Для наших полезных нагрузок порядки совпадают** — в запросе все ключи
PascalCase ASCII (`Amount, CardType, Data, PaymentId, RedirectUrl`), в webhook
все camelCase (`amount, fields, id, requestDate, service, status,
transactionDate, transactionId`). Поэтому обычный `sorted()` рабочий. Но это
**допущение**, а не свойство: стоит Finik добавить в webhook ключ вроде
`_meta` или `Amount` — и проверка начнёт падать. Отсюда два требования:
(а) тест, фиксирующий допущение явно; (б) в `verify_webhook` при неуспехе
пробовать второй порядок ключей (ICU-подобный) как фолбэк — это ничего не
ослабляет (подпись всё равно должна сойтись) и спасает от тихой поломки.

### 10.3 Подпись действует вечно → идемпотентность обязательна

В теле нет ни nonce, ни срока; `x-api-timestamp` у webhook проверять нельзя
(ретраи Finik приходят часами позже с тем же подписанным телом). Значит
единственная защита от повторного проигрывания — «уже обработано» по
`transactionId`/`order_id`. Это **элемент безопасности**, а не оптимизация;
в коде уже есть правильный паттерн (`status == "paid"` → сразу `ok`).

### 10.4 В подпись входят только `Host` и `x-api-*`

Следствия для нашего деплоя: (а) Caddy должен передавать наверх исходный `Host`
(`reverse_proxy` так делает по умолчанию — не переопределять); (б) заголовки для
каноникализации брать **из фактического запроса**, а не хардкодить список;
(в) `X-Forwarded-*`, добавленные Caddy, безвредны — они не начинаются с `x-api-`.

### 10.5 Единицы `Amount`: сомы (выведено из ошибок валидации, 2026-08-07)

Прямым текстом доки единицы не называют, но список ошибок валидации на странице
«Интеграция» их выдаёт:

> `The fixedAmount must be a decimal number with no more than 2 digits after the decimal point.`
> `The fixedAmount must be a greater than or equal to 0.01.`

Два знака после запятой и минимум `0.01` возможны только в **сомах** (0.01 сома =
1 тыйын); в тыйынах дробная часть не имела бы смысла, а минимум был бы целым `1`.
Значит `Amount: 2000` = 2000 сом, наш `_amount_number()` корректен, а дробные
суммы допустимы (но не более 2 знаков — `Decimal` в конфиге это и даёт).

Косвенное подтверждение — пример из доков `"Amount": 100`: в тыйынах это был бы
платёж на 1 сом, странная величина для примера.

Первый beta-платёж всё равно делаем на 1 сом, но уже как проверку вывода, а не
как способ узнать единицы.

### 10.6 `PaymentId` должен быть UUID

В примерах — канонический UUID с дефисами; у нас `uuid.uuid4().hex` без дефисов.
Скорее всего примут и hex, но проверять на боевых деньгах это не то, чем стоит
рисковать — шаг 6 меняет на `str(uuid.uuid4())`. Косвенно подтверждается ошибкой
валидации `Payment ID must be less than or equal to 36 characters long.` — 36
символов это ровно UUID с дефисами.

### 10.7 `Currency` есть в ошибках, но нет в примере тела (2026-08-07)

В списке ошибок валидации на «Интеграции» три штуки про валюту:

> `Currency must be provided.`
> `An invalid currency is provided.`
> `"FINIK_QR" payment method currently does not support {Currency} currency.`

При этом в официальном примере тела запроса поля `Currency` **нет**, и мы его не
отправляем. Варианты: поле опционально с дефолтом KGS, либо ошибки относятся к
карточному потоку. Ломается это **до денег** — первый же `POST /v1/payment` на
beta вернёт `400 Currency must be provided`, если поле нужно; лечение — одна
строка в теле. Поэтому спрашивать заранее не обязательно, но проверить на первом
beta-прогоне — обязательно.

### 10.8 `Data.Lang` в примере доков, у нас не передаётся

В официальном примере `Data` есть `"Lang": "ru"` — язык страницы оплаты. Мы его
не шлём, значит страница откроется на дефолтном языке Finik. Косметика, но для
кыргызстанских плательщиков `ru` уместнее — добавить вместе с проверкой
`Currency`.

### 10.9 Статус платежа проверить нечем — это подтверждено, а не «не найдено»

Пересмотр всех трёх страниц Web SDK (`reference`, `integration`, `webhooks`,
2026-08-07) дал **ровно один** документированный эндпоинт —
`POST /v1/payment` (`https://api.acquiring.averspay.kg` / `https://beta.api.acquiring.averspay.kg`).
Ни GET по платежу, ни status-check нет нигде; страница вебхуков прямо предписывает
считать webhook источником истины и подтверждает, что при неуспехе он **не
отправляется вообще**. Ретраи: 5 попыток по 30 с, дальше удвоение с потолком в
сутки, затем раз в сутки.

Тот status-check, что встречается в доках Finik, — из **Payments Gateway**
(исходящие платежи), это другой продукт (§2.4).

Практический вывод не меняется, но перестаёт быть гипотезой: сверки по потерянному
webhook не будет никогда, страховки — джанитор `pending` и `cli grant-subscription`.

---

## 11. Вопросы к Finik (`finik@quickpay.kg`) — отправить до шага 2

**Ревизия 2026-08-07:** вопросы 1, 2 и частично 3, 5 закрыты по документации (см.
§10.5, §10.9) — спрашивать их не нужно. Актуальный остаток — ниже, помечен ⬜.

1. ✅ ~~**`Amount` — в сомах или тыйынах?** Допустимы ли дробные суммы?~~ —
   закрыто ошибками валидации: **сомы**, до 2 знаков, минимум `0.01` (§10.5).
2. ✅ ~~**Есть ли эндпоинт проверки статуса платежа для acquiring?**~~ — нет,
   подтверждено обходом всех страниц Web SDK (§10.9). Остаточный риск никуда не
   делся, но закрывается нашими средствами, а не их API.
3. ✅ ~~Присылается ли webhook при неуспехе?~~ — страница вебхуков: только при
   успехе, при неуспехе не отправляется вовсе. ⬜ Открытым остаётся лишь: есть ли
   уведомление об истечении `endDate`?
4. ⬜ **Возвраты (refund/reversal)** — через API или только через кабинет?
5. 🟡 Точный формат `Data.additionalData`. Ошибки валидации подсказывают структуру:
   `"fieldId" not provided in the payment data.`, `"name" not provided in the
   payment data.`, `additionalData exceeds maximum allowed size of 20.` — то есть
   массив объектов с `fieldId` и `name` (не `value`, как мы предполагали). Нам
   сейчас не нужно (связь с заказом держим через `PaymentId`), но если понадобится
   — проверять именно эту форму.
6. **Есть ли фиксированная/минимальная комиссия за транзакцию** помимо 2%?
   При чеке 500 сом фикс важнее процента.
7. Лимиты: rate limit на `POST /v1/payment`, мин/макс сумма, срок жизни ссылки
   по умолчанию (если не задавать `startDate`/`endDate`).
8. Можно ли на один `accountId` использовать разные `webhookUrl` (beta и prod
   одновременно), или нужны разные счета/ключи?
9. Список банков КР, чьи приложения открываются по QR (покрытие плательщиков —
   от него зависит вся продуктовая гипотеза §2.2).
10. Сроки и порядок расчётов (settlement) на расчётный счёт; чек/фискализация для
    физлиц-плательщиков.
11. Есть ли официальный пример подписи на Python, и гарантирована ли стабильность
    каноникализации `@mancho.devs/authorizer` (мы её портировали и сверили —
    хотим подтверждение, что она не поменяется молча)?
12. ⬜ Тестовые данные для beta: `accountId`, способ оплаты в тестовой среде.
13. 🟡 Нужно ли передавать `Currency` в `POST /v1/payment` для `CardType:
    FINIK_QR`? В ошибках валидации поле есть, в примере тела — нет (§10.7).
    Необязательный вопрос: выяснится первым же beta-запросом.

---

## 12. Откат

До боевого включения откат тривиален: `FINIK_API_KEY`/`FINIK_ACCOUNT_ID`/
`PUBLIC_BASE_URL` пустые → `subscription_enabled=false` → `/subscribe` отвечает
«временно недоступно», доступ гейтится группой, Caddy под профилем `payments` не
поднимается. То есть **выключение подписки — это правка `.env`**, а не
откат кода.

После запуска (появятся оплатившие юзеры) откат уже не «выключить» — надо
дожить до конца оплаченных периодов: `subscription_expires_at` в БД, доступ по
ним продолжит работать даже при выключенном провайдере, потому что
`is_subscription_active` смотрит только на дату. Это удобно: провайдера можно
поменять, не отбирая доступ у оплативших.

---

## Приложение А. Как проверялась подпись

Воспроизводимо, ~5 минут:

1. Скачать и распаковать библиотеку, которую рекомендуют доки Finik:
   ```bash
   curl -sL https://registry.npmjs.org/@mancho.devs/authorizer/-/authorizer-2.12.8.tgz | tar xz
   ```
   Алгоритм — в `package/index.js`: `getHttpMethod/getPath/getHeadersData/
   getQueryStringParamsData/getJsonBody/getData/sign/verify`.
2. Библиотека тянет `node-jose`, но в `sign()` он нужен лишь чтобы вернуть тот же
   PEM в `crypto.createSign`. Подменяется заглушкой — и код запускается без сети:
   ```js
   exports.JWK = { createKeyStore: () => ({ add: async (pem) => ({ toPEM: () => pem }) }) };
   ```
3. Подписать два запроса (README-пример и реальное тело Finik) тестовым ключом из
   `package/__test__/testData.js`, напечатать `getData()` и подпись.
4. Собрать те же канонические строки Python-портом (§4.2) и подписать
   `openssl dgst -sha256 -sign priv.pem | base64 -w0`.
5. Сравнить: совпали **и канонические строки, и base64-подписи** для обоих кейсов
   (`canonical match: True, signature match: True`). Векторы — §4.3, они и стали
   тестами шага 12.

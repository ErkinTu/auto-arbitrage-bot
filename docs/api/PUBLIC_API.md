# Внешний API: аудит, защита, контракт для сайта

Ветка `feature/public-api-hardening` (09.11.2026, от `fix/text-card-photos-button`
= прод 02.11.2026). **На проде с 09.11.2026.** Наружу по-прежнему открыты только
`payments/*` и `admin/*` — блок внешнего API в `Caddyfile` закомментирован,
открывать его — решение владельца (§6).

## 1. Что торчит сейчас и что собираемся открыть

| Префикс | Кто ходит | Наружу сейчас | Гейт |
|---|---|---|---|
| `/api/v1/payments/*` | Finik | да, `PAYMENTS_DOMAIN` | RSA-подпись |
| `/api/v1/admin/*` | панель | да, `ADMIN_DOMAIN` | сессия в httpOnly-куке |
| `/api/v1/catalog/*`, `/dashboard/*` | сайт | **нет** | — (публичные данные) |
| `/api/v1/market/vehicle-summary`, `/estimate`, `/price-trend` | сайт | **нет** | — (тяжёлые) |
| `/docs`, `/openapi.json` | разработчики | нет | на проде выключить |

## 2. Аудит (что нашли)

Проверено чтением кода и вживую (uvicorn + временный Postgres).

| # | Серьёзность | Находка | Статус |
|---|---|---|---|
| 1 | **Высокая** | Лимит перебора входа в админку считался по `request.client.host`, а uvicorn запускался без `--proxy-headers`: за Caddy это адрес контейнера Caddy — **один бакет на весь интернет**. 10 мусорных запросов за 5 мин от кого угодно запирали вход всем админам. | исправлено |
| 2 | **Высокая** (при открытии) | Ни у одной публичной ручки нет лимита частоты. `dashboard/summary` — `percentile_cont` по всей `listings`, `vehicle-summary` тянет до 1000 ORM-объектов; база общая с ботом — скрейпер положил бы пуши. | исправлено |
| 3 | Высокая (при открытии) | Настройка `API_CACHE_TTL_SECONDS` объявлена, но **нигде не используется** — кэша ответов не было. | исправлено |
| 4 | Средняя | Нет `statement_timeout`: один медленный запрос снаружи держит соединение базы без предела. | исправлено (10 с, только процесс `api`) |
| 5 | Средняя | `/docs` и `/openapi.json` включены всегда. | выключатель `API_DOCS_ENABLED` |
| 6 | Средняя | Нет потолка тела запроса; на неверной подписи webhook логировал **всё** неподписанное тело. | 64 КБ, лог обрезан до 500 символов |
| 7 | Низкая | CORS: `allow_methods=*`, `allow_headers=*` вместе с `allow_credentials`. Дефолт `CORS_ORIGINS` — localhost. | сужено; на проде задать свой origin |
| 8 | Низкая | Заголовки безопасности ставил только Caddy и только на домене панели. | на всех ответах API |
| 9 | Низкая | `estimate`/`price-trend`: строки марки/модели без ограничения длины (ключи кэша, логи). | `max_length` |
| 10 | Низкая | uvicorn без `--limit-concurrency`/keep-alive таймаута, с заголовком `Server`. | исправлено |

**Открытые (не чинили в этой ветке — нужны решения/замеры):**

- `vehicle_summary`/`estimate`: `LIMIT 1000` **без `ORDER BY`** — у популярных
  когорт выборка недетерминирована (оценка может «плавать» между запросами).
  Правильно — агрегировать в SQL (`percentile_cont`), как уже сделано в
  `dashboard.py`, а не тащить объекты в Python.
- Админка: пагинация `OFFSET` и `ILIKE '%q%'` без `pg_trgm` — на 40k+ строк
  пока терпимо, для ленты сайта не годится (см. §5, курсор).
- Формат ошибок не единый: `detail` строкой у FastAPI, `{detail, retry_after}`
  у 429. Для внешнего контракта — единый конверт (§5).
- Лимитер и кэш — память одного процесса. Как только `api` станет больше
  одного процесса/машины — Redis (§7).

## 3. Что сделано в ветке

| Механизм | Где | Суть |
|---|---|---|
| Лимит частоты | `app/core/ratelimit.py` | Токен-бакет на IP по трём политикам: `public` 120/мин (всплеск 60), `heavy` 30/мин (15) + **общий** потолок 600/мин на всех, `admin` 600/мин (200). `/health` и `payments/*` не режутся (отбитый 429 webhook Finik повторит через сутки = «деньги списаны, доступа нет»). Ответ — 429 + `Retry-After`; на каждом ответе `RateLimit-Limit/Remaining/Reset`. Память ограничена (50k ключей). |
| Реальный IP | `docker-compose.yml`, `Dockerfile` | `uvicorn --proxy-headers --forwarded-allow-ips=*`. Безопасно, т. к. порт 8000 наружу не опубликован, а Caddy ≥2.5 входящий `X-Forwarded-For` от клиента не пропускает. Если `API_HOST` станет `0.0.0.0` — сузить `FORWARDED_ALLOW_IPS`. |
| Кэш ответов | `app/core/cache.py` | TTL 300 с, ≤2000 ключей, LRU, **single-flight** (на промахе считает один поток). Ошибки не кэшируются. Публичные и админские копии делят кэш. |
| HTTP-кэш | `app/core/http.py` | `Cache-Control: public, max-age=60` у `catalog/dashboard/market`, `no-store` у `admin/payments`. |
| Заголовки | `app/core/http.py` | `nosniff`, `X-Frame-Options: DENY`, `Referrer-Policy`, CSP `default-src 'none'` (кроме `/docs`), `X-Request-ID` (свой от клиента — только если похож на id), `Server-Timing`, warning на запросы дольше `API_SLOW_REQUEST_MS`. |
| Потолок тела | `app/core/http.py` | 413 по `Content-Length` и по факту потока (chunked). |
| SQL-таймаут | `app/main.py` | `statement_timeout` 10 с на соединения процесса `api` (бот/worker не затронуты). Проверено: `pg_sleep` дольше лимита → `QueryCanceled`. |
| Readiness | `GET /health/ready` | 503, если база не отвечает (`/health` — liveness, базу не трогает). |
| Swagger | `API_DOCS_ENABLED` | на проде `false`. |

Все настройки — в `.env.example` (секция «Внешний API»). Тесты —
`tests/test_api_hardening.py` (26 шт.), весь набор — 689 зелёных.

## 4. Как это видит клиент (сайт, интеграции)

- Любой ответ несёт `X-Request-ID` — его клиент показывает/логирует при ошибке,
  по нему запрос находится в логах `api`.
- `429` → ждать `Retry-After` секунд. `RateLimit-Remaining` позволяет сбавить
  темп заранее. Фронт панели уже так делает (`analytics-front`, ветка
  `feature/resilient-http`: повтор GET при 429/502-504, таймаут 15 с).
- Данные публичных ручек обновляются раз в цикл бота (10 мин): кэш 5 мин на
  сервере и 1 мин в браузере устаревания не добавляют заметно.

## 5. Контракт для сайта — **реализован** (ветка `feature/public-feed-api`, 09.11.2026)

Роутер `app/api/public/feed.py`, логика `app/services/public_feed.py`, схемы
`schemas.Public*` (из них генерируются TS-типы фронта). Всё под префиксом
`/api/v1/public/` — одна строка в белом списке Caddy, политика лимита `public`
(счётчик фильтра — `heavy`), `Cache-Control: public, max-age=60`, серверный
кэш 30 с (`PUBLIC_FEED_CACHE_SECONDS`).

| Ручка | Назначение |
|---|---|
| `GET /public/feed` | лента; `sort=fresh\|price_asc\|price_desc`, `limit≤48`, `cursor` (keyset, не OFFSET); фильтры: `brand_ids`, `model_id`, `cities`, `source=mashina\|lalafo`, `price_from/to`, `year_from/to`, `mileage_from/to` (NULL-пробег под диапазон не попадает), `seller_type=private\|dealer`, `fuel`, `body_type`, `gearbox`, `drive`, `steering` (коды групп через запятую — `services/specs.py`: sedan, suv, …; automatic, manual, cvt, robot; front, rear, awd; left, right — раскрываются во все написания источников; NULL и «-1» не попадают), `customs=cleared\|not_cleared`, `region` (коды как в боте: `bishkek`, `osh_city`, 7 областей, `foreign`; раскрываются в города по `services/regions.py`, нераспознанные сёла не попадают), `with_photo`, `new_24h`, `price_dropped`, `deal=any\|good\|not_high` |
| `GET /public/feed/histograms` | гистограммы цены (0…50 000+ $, шаг 2000), года (≤1995…текущий), пробега (0…300 000+ км, шаг 10 000) под ползунки; те же фильтры, что у ленты, свой диапазон не применяется, `deal` не учитывается; лимит heavy |
| `GET /public/feed/facets` | значения для чипов фильтра из базы: регионы (с подписью `label`), кузов, КПП, привод, руль, города (≥ 5 объявлений, варианты написания склеены, значения без букв вроде «-1» скрыты), растаможка; кэш справочников |
| `GET /public/feed/count` | «Показать N авто»; при `deal≠any` и >1000 кандидатов — `approximate: true` |
| `GET /public/best` | «Лучшее сейчас»: свежие за 24 ч с фото, наибольшее отклонение вниз |
| `GET /public/listings?ids=` | избранное (до 50), **включая снятые** (`is_active=false`) |
| `GET /public/listings/{id}` | карточка: + галерея `images`, `price_history` |
| `GET /public/listings/{id}/similar` | похожие: та же модель, год ±2, активные с фото, ближе по году и цене (`limit≤12`, по умолчанию 8) |

Аналитика поверх объявления — поле `market`: `avg_price_usd`, `samples`,
`diff_usd`, `diff_percent`, `verdict` (`good|market|high` по порогу
`PUBLIC_DEAL_THRESHOLD_PERCENT=5`). Расчёт — тот же, что у бота (пакетный
`batch_market_estimates`, тест на совпадение `test_batch_estimates_match_bot`);
отклонение больше `BOT_MAX_BELOW_MARKET_PERCENT` — оценки нет (мусор цены).

**Чего в ответе нет никогда:** `seller_phone`, `raw_payloads`, `title`/описание
источника, флаги качества (тест `test_no_personal_data_in_public_schema`).

Тип продавца: заполнен у 100 % активных (копия прода 01.11: lalafo 69k частных /
3,9k дилеров, mashina 35k / 19k) — показываем всегда.

**Замер на копии прода** (307k объявлений, 120k активных; без кэша):

| | было | стало |
|---|---|---|
| лента, 1-я страница | 594 мс | ~110–190 мс |
| основной SQL ленты | 90–200 мс (полный проход) | 0,25 мс (индекс) |
| `deal=good` | 2,3 с | ~0,3 с |
| счётчик `deal=good` | 20 с | ~1 с (выборка) |
| карточка | 91 мс | ~50–95 мс |

Что дало: запрос без JOIN-ов + частичный индекс `ix_listing_feed_fresh`
(условие обязано быть `is_active = true`, не `IS true`, иначе индекс не
сопоставляется); оценка рынка одним запросом с оконным `max()` в 7-дневном окне
вместо 2 запросов на когорту.

**Дальше (когда счётчик станет узким местом):** хранить `market_diff_percent` в
`listings` (считать при синке/пересчёте статистики) — тогда `deal=good` и сортировка
«по выгоде» станут обычным индексируемым SQL.

Правила контракта: версия в пути (`/api/v1`), ломающее — только `/api/v2`;
только `GET`; единый конверт ошибок — следующий шаг (пока `detail` строкой).
Вход на сайт — через Telegram-бота (`PLATFORM_ADR.md`), API-ключи для B2B —
когда появится первый клиент.

### 5.1. Фильтры уведомлений в профиле — `/api/v1/me/filters` (ветка `feature/push-filters`, 12.11.2026)

Не публичное: нужна сессия сайта (кука `sk_session`), Caddy пропускает `/api/v1/me/*`
вместе с `/api/v1/auth/*`. Фильтр уведомлений — это фильтр бота (`user_filters`): сайт
и бот правят одни строки, правила — общие (`bot/db.py`).

| Ручка | Кто | Назначение |
|---|---|---|
| `GET /me/filters` | вошедший | `items`, `used`/`limit` (общий с ботом), `can_edit`, `max_brands`, `market_percents`, `regions` |
| `POST /me/filters` | subscriber | создать; 409 `limit` / `duplicate`, 422 `invalid` + `field` |
| `PUT /me/filters/{id}` | subscriber | правка той же строки; тело + `updated_at` виденной версии → 409 `stale`, если правили в боте |
| `DELETE /me/filters/{id}` | вошедший | удалить (и без подписки — освободить слот) |
| `POST /me/filters/preview` | вошедший | «за 7 дней прислал бы N»: `total`, `per_day_avg`, `by_day[7]`, `without_market`, `samples` (≤ 6 `PublicListing`); лимит heavy, кэш 5 мин |

Тело: `brand_ids` (≤ 10; при 2+ — без модели), `model_id` (только при одной марке),
`year_from`/`year_to` или `max_age_years`, `mileage_max_km`, `price_max_usd`,
`below_market_percent` (5/10/15/20, null — без «ниже рынка»), `region` (коды бота,
null — «Все авто»), `fuel_groups`. Ошибки — `detail: {code, message, …}`, `message`
показывать человеку как есть. Изменения — только со своего `Origin`.

## 6. Порядок открытия наружу (когда будет сайт)

1. Выкатить эту ветку: публичное поведение не меняется, закрываются находки 1,
   4-10. В `.env` прода: `API_DOCS_ENABLED=false`, `CORS_ORIGINS=https://<сайт>`.
2. Неделю смотреть логи `slow request` и 429 у панели (лимит админки щедрый,
   ложных отказов быть не должно).
3. Завести домен, раскомментировать блок `{$PUBLIC_API_DOMAIN}` в `Caddyfile`,
   `PUBLIC_API_DOMAIN=…` в `.env`, `docker compose --profile payments up -d caddy`.
4. Проверка снаружи: белый список отвечает, `admin/*`, `payments/*`, `/docs` —
   404; 70 запросов подряд к `catalog/brands` → 429 с `Retry-After`.
5. Мониторинг: `/health/ready` во внешний пинг (UptimeRobot и т. п.).

## 7. Масштабирование (когда упрёмся)

Признак — CPU `api` > 70 % или p95 > 1 с в `slow request`. Тогда:
`uvicorn --workers N` → лимитер и кэш в Redis (интерфейсы `TokenBucketLimiter.hit`
и `TTLCache.get_or_compute` узкие нарочно — меняется реализация, не вызовы) →
реплика Postgres только на чтение для `api`. Lightsail 2 ГБ — потолок по
памяти раньше, чем по CPU: Redis туда влезет (~50 МБ), вторая машина — уже
решение о бюджете.

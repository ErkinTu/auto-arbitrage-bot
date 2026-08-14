# CLAUDE.md

Точка входа для ИИ-сессий. Здесь — суть проекта, архитектура и операционные
правила, которые НЕ видны из кода. Детали — в тематических доках (см. индекс).

## Что это

MVP для сбора объявлений о продаже авто с **mashina.kg**, хранения истории
цен и рыночной аналитики (оценка цены, графики, «ниже рынка»). Backend —
Python (FastAPI + SQLAlchemy + PostgreSQL), Telegram-бот на aiogram.
Отдельный фронт — Vue/Vite в соседнем каталоге `../analytics-front`.

Стек: Python ≥3.12, FastAPI+uvicorn, SQLAlchemy 2.0, psycopg3, aiogram 3,
httpx, pydantic-settings. Зависимости через **uv**. Линт/тесты: ruff, pytest.

## Архитектура: 4 Docker-сервиса

| Сервис | Что делает | Entrypoint |
|---|---|---|
| `postgres` | БД (volume `postgres_data`) | postgres:16-alpine |
| `api` | FastAPI: оценка/графики/каталог на :8000 | `uvicorn app.main:app` |
| `worker` | full scan по выходным + пересчёт статистики + деактивация «зависших» | `app.worker` |
| `bot` | Telegram: fresh-парс каждые 10 мин + уведомления | `app.bot` |

Ключевые факты (частые источники непонимания):

- **Бот читает БД напрямую** (`app/bot/db.py`, `SessionLocal`), НЕ через HTTP
  API. Поэтому API боту не нужен и закрыт наружу.
- **API без аутентификации.** Порты `api:8000` и `postgres:5432` публикуются
  только на `127.0.0.1` (через `API_HOST`/`POSTGRES_HOST`, дефолт localhost).
  Наружу открывать нельзя без reverse-proxy+auth. См. `DEPLOY.md`.
- **Время.** Контейнеры в UTC; всё расписание код считает сам по
  `SCHEDULER_TIMEZONE` (`Asia/Bishkek`). Контейнерный TZ не трогать.
- **Расписание встроено в сервисы**, cron не нужен (кроме бэкапов).
- **Источника данных два**: mashina.kg (`app/parsers/mashina.py`, full+fresh) и
  lalafo.kg (`app/parsers/lalafo.py`, ТОЛЬКО fresh — без full-scan).
  Реестр источников/фабрика парсеров — `app/services/sources.py`; нормализатор
  диспетчеризуется через `parser.normalize()`. Детали lalafo-объявления
  дотягиваются только для новых/подешевевших (`_enrich_or_skip` в sync).
  Паузы lalafo раздельные: лента 5с, карточки 0.5с с удвоением при 429
  (`lalafo_feed_delay_seconds`/`lalafo_detail_delay_seconds`).
  **На проде lalafo требует `LALAFO_PROXY_URL`** (Cloudflare режет
  датацентровые IP; WARP не помогает). Детали и прод-чеклист — `docs/LALAFO.md`.
- **Свежесть данных.** Бот пушит только объявления, ОПУБЛИКОВАННЫЕ на
  источнике ≤ 12 ч назад (`bot_max_listing_age_hours`; NULL-дата = молчим) —
  VIP-поднятые старые в БД сохраняются, но «новинками» не считаются.
  Вместо full-scan у lalafo — ежесуточное обслуживание в worker: точечный
  валидатор карточек не виденных 3+ дня объявлений (HTTP 410 Gone →
  деактивация reason='card_gone', 200 → продление `last_seen_at`; лента даёт
  только новейшие 50, живое объявление иначе «пропадает» навсегда), затем
  фолбэк — деактивация не подтверждённых дольше 30 дней (`stale_listing_days`,
  reason='stale'). Агрегаты статистики учитывают только виденных за последние
  30 дней (`stats_max_last_seen_days`). Если даже самое старое объявление
  полной fresh-страницы новое (пропуск/простой) — warning + catch-up до
  `bot_catchup_max_pages` страниц.
- **Курс USD/KGS** — НБКР (`app/services/exchange.py`, раз в день, лениво из
  бот-цикла). Цены lalafo в сомах конвертируются в USD только при отсутствии
  собственного USD (флаг `usd_converted`; смена курса ≠ смена цены).
- **Подписка (finik.kg, Web SDK, разовая оплата).** Доступ = член закрытой группы
  (суперюзер, как раньше) ИЛИ активная платная подписка. Пока
  `FINIK_API_KEY`/`FINIK_ACCOUNT_ID`/`PUBLIC_BASE_URL` пусты
  (`settings.subscription_enabled=false`) — подписка спит, доступ гейтится только
  группой. Оплата: бот `/subscribe` → `finik.init_payment` → **302, ссылка на
  страницу оплаты в заголовке `Location`** (редиректы НЕ следуем) → кнопка в
  Telegram; юзер платит по QR из банковского приложения; Finik POST-ит
  `/api/v1/payments/finik` (`api/payments.py`, проверка RSA-подписи их публичным
  ключом) → продление `subscription_expires_at` + `access_status='subscriber'`.
  **Webhook — единственный источник истины**: возврат юзера по `RedirectUrl`
  оплату не подтверждает. Webhook — ЕДИНСТВЕННОЕ, что торчит наружу, через Caddy
  (профиль `payments`, только `/api/v1/payments/*`); `api` остаётся на 127.0.0.1.
  `access_status` синхронизируется в `access.py::resolve_access` (matching
  Telegram не дёргает). Деплой-чеклист — `DEPLOY.md § Подписка/Finik`.
  **Обкатка на проде**: `SUBSCRIPTION_TESTER_IDS` — пока список не пуст,
  `/subscribe` работает только для этих telegram_id (платёж остальным даже не
  заводится, иначе копились бы `pending` и врала бы воронка), а пейвол после
  триала не зовёт в `/subscribe`, а предлагает ручное продление —
  сообщение о конце триала одно на всю жизнь юзера, тратить его на тупик нельзя.
  Существенные особенности Finik (детали — `docs/PAYMENTS_FINIK_ANALYSIS.md`):
  - **Подпись** = RSA-SHA256(канон), base64; в канон входят метод, path, `Host` и
    только заголовки `x-api-*`, query и JSON тела, где отсортированы ТОЛЬКО ключи
    верхнего уровня. Схема восстановлена из npm-пакета `@mancho.devs/authorizer`
    и сверена с ним — векторы в `tests/test_finik.py`, менять `finik.py::canonical`
    без них нельзя. Приватный ключ — файлом в `./secrets/` (volume у `bot`), не в
    `.env`; публичные ключи Finik зашиты в код по среде `FINIK_ENV=beta|prod`.
  - **Целые float ломают проверку webhook**: подпись считается по пересобранному
    JSON, а JS печатает `2000.00` как `2000` (Python — `2000.0`) → нормализация в
    `_js_number`. Есть тест-регрессия.
  - **Webhook приходит только на успех** — про отказы Finik молчит, поэтому
    `pending` гасит `worker.expire_stale_payments` (`PAYMENT_PENDING_TTL_HOURS`).
  - **Статус платежа запросом не проверить** (нет такого эндпоинта у acquiring) →
    страховка на потерянный webhook: `cli.py grant-subscription`.
  - Идемпотентность обязательна: подпись Finik валидна бессрочно, ретраи идут
    сутки; повтор по оплаченному `order_id` отвечает `200 already processed`.
- **Триал, пейвол, рефералка.** Доступ = группа ИЛИ подписка ИЛИ **активный
  триал** (`bot/db.py::decide_access_status`, приоритет в этом же порядке —
  статус нужен честным для метрик и напоминаний). Триал: `TRIAL_DAYS=3` по
  кнопке в онбординге `/start`, **один раз на telegram_id** —
  `trial_started_at` не обнуляется никогда, даже после истечения. Срок лежит в
  ОТДЕЛЬНОЙ `trial_expires_at`, не в `subscription_expires_at`: иначе
  бесплатные дни попадут в напоминание о продлении и в метрики оплат.
  **Пейвол наступает сам** — `matching.py` выбирает только активный доступ, так
  что с истечением триала пуши прекращаются без выключателя; отдельно шлётся
  ровно одно сообщение (`worker.send_trial_paywall_notices`, флаг
  `trial_expired_notified_at` ставится даже при неудачной доставке).
  Рефералка: deep-link `t.me/<bot>?start=ref_<code>` (нужен `BOT_USERNAME`),
  друг засчитывается **только после активации им триала**, за каждые
  `REFERRAL_FRIENDS_PER_BONUS=3` — `+REFERRAL_BONUS_DAYS=7` дней; начисление
  идемпотентно (храним ЧИСЛО выданных бонусов и догоняем до заслуженного),
  самоприглашение и перепривязка запрещены. Бонус выдаётся **один раз на
  пригласившего** (`REFERRAL_MAX_BONUSES=1`): телеграм-ботов middleware
  отсекает (`user.is_bot`), но мультиаккаунтинг живым человеком не ловится
  ничем, а без потолка 30 приглашённых = 70 бесплатных дней. Понижение потолка
  задним числом дни не отбирает. Бонусные дни идут в тот же
  `subscription_expires_at` через `subscription.py::extend_paid_access` (общий
  хелпер для оплаты/бонуса/CLI) — для доступа это одно и то же, а различить
  всегда можно по `user_events`/`payments`. Воронка: журнал `user_events`
  (`start`/`trial_started`/`trial_expired`/`payment_created`/`paid`), отчёт —
  `cli.py funnel --days N`; по `bot_users` её не построить, там только текущее
  состояние. Грабля: сессии бота живут с `autoflush=False`, поэтому
  `grant_due_bonuses` делает `flush()` сам — иначе COUNT не увидит только что
  выданный триал и бонус молча не начислится.
- **Лимит фильтров и чёрный список.** Фильтров на пользователя —
  `BOT_MAX_FILTERS_PER_USER=5` (было 10). Лимит применяется **только на
  создание**: у кого уже больше, старые фильтры работают — молча отключать
  чужую настройку хуже, чем временно нарушенный инвариант (`db.py::filters_quota`,
  `messages.py::filters_limit_text`). Дубликат фильтра не создаётся
  (`DuplicateFilterError`): при лимите 5 слот дорог, а рассылку дубль всё равно
  не удваивает. Чёрный список (`user_blocks`, `services/blocklist.py`) —
  **отдельная сущность, а не поле фильтра**: фильтр отвечает «что показывать»,
  и отрицание пришлось бы дублировать в каждый из пяти. Запрет ГЛОБАЛЬНЫЙ,
  поверх всех фильтров юзера, проверяется в `matching.py` после `filter_matches`
  (одна загрузка на цикл). Гранулярность: марка / марка+модель / город
  (`kind` + нормализованный `value`); продавца заблокировать нельзя — в
  `listings` нет его идентификатора. NULL-поля объявления под запрет не
  попадают: запрет должен быть доказан. Вход — кнопка «🚫 Не показывать такие»
  под карточкой (двухшаговое меню) и `/blacklist`.
- **Группировка уведомлений** (`BOT_GROUP_MIN_LISTINGS`, по умолчанию 0 =
  выключено). Замер на проде за 14 дней: обычный цикл — 5 сообщений на человека
  (p90 12), первый цикл после ночной паузы — в среднем **28**, максимум 55; в
  пиковом цикле 328 отправок. Лимиты Telegram (сверено с core.telegram.org/bots/faq):
  ~30 сообщений/с суммарно, не чаще 1/с в один чат, сверх — только платный Paid
  Broadcasts. При включении авто одной марки уезжают одним списком
  (`notifier.build_outgoing` + `messages.format_digest`), сообщения чередуются
  между чатами, чтобы пауза 1 с/чат не душила рассылку. Цена: у списка нет
  кнопки чёрного списка (она привязана к объявлению).
- **Защита контента от пересылки.** Всем исходящим методам Bot API, у которых
  есть поле `protect_content`, оно проставляется централизованно в
  session-middleware (`bot/protection.py`), а не на каждом `answer()` — иначе
  новый хендлер молча уехал бы без защиты. Решение принимает
  `settings.protect_content_for(chat_id)`: `BOT_PROTECT_CONTENT` + whitelist
  `BOT_SHARE_WHITELIST_IDS` (кому пересылка РАЗРЕШЕНА). Незаданное поле у
  aiogram — не `None`, а sentinel `Default('protect_content')`; явно
  переданное в коде значение middleware не трогает. `services/telegram.py`
  (sync-сендер из worker, минует aiogram) ставит флаг сам по тому же правилу.
  **Границы:** «Переслать»/«Сохранить» убираются везде, скриншот блокирует
  только Android-клиент (iOS/Desktop/Web — нет), ссылку из сообщения юзер
  всё равно может открыть и скопировать. Это барьер от массового репоста,
  не DRM.
- **Предохранители качества «ниже рынка»** (защита от выбросов цены):
  (A) абсолютный потолок `MAX_PLAUSIBLE_PRICE_USD=2_000_000` в нормализаторе →
  флаг `price_implausibly_high` (в `CRITICAL_QUALITY_FLAGS`, `analytics_eligible=false`);
  (B) относительный отсев в `stats.py` — цены вне `[median/K, median*K]`
  (`outlier_price_median_multiple=4.0`) по когорте «модель+год» при
  `≥ outlier_min_cohort=5`, до раскладки по бакетам; (C) при NULL-пробеге
  `bot/matching.py::market_average` агрегирует ВСЕ бакеты модель+года (bucket
  `unknown` у lalafo ~43% и сам по себе не набирает `min_samples`).

## Раскладка кода (`app/`)

- `main.py` — FastAPI-приложение, CORS, роутеры. `cli.py` — все ручные команды.
- `worker.py` — планировщик full-scan/rebuild. `core/config.py` — все настройки (env).
- `api/` — роутеры: `routes.py` (estimate/price-trend), `dashboard.py`,
  `catalog.py`, `market.py`, `payments.py` (webhook Finik — см. выше).
- `services/` — логика: `blocklist.py` (чёрный список), `sync.py` (обход источника), `normalizer.py` +
  `catalog_service.py` + `catalog.py` (нормализация моделей/алиасы),
  `analytics.py`, `stats.py` (агрегаты), `dashboard.py`, `finik.py` +
  `subscription.py` (подписка), `trial.py` + `referral.py` + `events.py`
  (триал, рефералка, журнал воронки), `telegram.py` (sync sendMessage из
  worker/api).
- `bot/` — aiogram: `poller.py` (fresh-циклы), `handlers.py`, `notifier.py`,
  `db.py` (прямой доступ к БД), `access.py` (проверка членства в группе).
- `models/entities.py` — ORM-модели (в т.ч. `UserEvent` — журнал воронки,
  `UserBlock` — чёрный список).
  `parsers/mashina.py` — адаптер источника.

## Запуск и команды

```bash
cp .env.example .env         # заполнить POSTGRES_PASSWORD, TELEGRAM_* и т.д.
docker compose up -d --build
docker compose ps            # 4 сервиса Up; порты 127.0.0.1:...
curl http://127.0.0.1:8000/health
pytest                       # тесты (pythonpath=. в pyproject)
```

Ручные команды CLI (внутри контейнера, код запечён в образ — bind-mount нет):

```bash
docker compose exec -T worker uv run python -m app.cli <cmd>
```

`cmd`: `init-db`, `seed`, `targets`, `add-target`, `sync [--source]`,
`sync-all [--source]`, `rebuild-stats`, `catalog-issues`, `add-model-alias`,
`add-generation-alias`, `reprocess-catalog`, `ensure-self-aliases`,
`refresh-rate`, `grant-subscription --telegram-id --days [--note]` (ручное
продление подписки: потерянный webhook, бонус, компенсация),
`funnel [--days 30]` (воронка: старты → триалы → пейвол → платежи → оплаты).

## Продакшн

AWS Lightsail, проект в `/home/ubuntu/car-analytics`, docker без sudo. Локальная
разработка и сервер запускаются одинаково (`docker compose`). Полный runbook
обновления/бэкапов — `DEPLOY.md`.

**Переезд на свой AWS-аккаунт состоялся 08.08.2026.** Боевой сервер — новый:

| | Адрес | Ключ | Роль |
|---|---|---|---|
| **Боевой** | `ubuntu@63.182.28.250` | `car-analytics-prod-key.pem` | свой аккаунт `279341437912`, обслуживает прод с 08.08.2026 |
| Старый | `ubuntu@63.180.133.249` | `LightsailDefaultKey-eu-central-1.pem` | чужой аккаунт (сервер друга), **остановлен**, держится как путь отката |

Оба — Lightsail 2 ГБ / 2 vCPU / 60 ГБ, Ubuntu 24.04, eu-central-1 (Франкфурт).
Проверка, какой из них живой, — не по докам, а по факту: `docker compose ps` на
новом отвечает, старый не пускает даже по ssh. Иначе и быть не может — токен
один, два бота дали бы `TelegramConflictError` (см. правило ниже).

Сверка данных после переезда и порядок отката — `MIGRATION.md`; там же дедлайны
нового аккаунта: **03.02.2027** — конец free plan (без Paid Plan аккаунт
закрывается с данными), **03.08.2027** — истечение кредитов ($160).

## Операционные правила и грабли (проверено на практике)

- **Один токен — один бот.** С одним `TELEGRAM_BOT_TOKEN` работает только ОДИН
  экземпляр (иначе `TelegramConflictError`). Для локальных тестов — отдельный
  токен и отдельный `TELEGRAM_ACCESS_CHAT_ID`; серверный бот не останавливать.
- **Добавление новой марки — строгий порядок:** `.env` → пересоздать
  контейнеры → full-обход → **подтвердить модели алиасами** →
  `reprocess-catalog` → `rebuild-stats`. `rebuild-stats` до подтверждения
  моделей бесполезен: нераспознанные модели дают `analytics_eligible=false`,
  и марки нет во фронте (`/api/v1/catalog/brands` показывает только марки с
  analytics-eligible объявлениями). Полный runbook — `docs/SYNC_STRATEGY.md §4`.
- **Баг парсера:** в имя модели «протекают» спеки двигателя (`CX-50 2.0 AT`,
  `EZ-60 1.5hyb AT`). Обходится алиасами на чистую каноническую модель;
  настоящий фикс парсера в бэклоге.
- **`reprocess-catalog` тяжёлый** (проход по всем ~40k+ объявлений, коммит в
  конце, несколько минут). Запускать синхронно с `</dev/null`; НЕ фонить через
  `nohup … &` поверх ssh — при обрыве канала процесс умирает без коммита.
- **База — в docker-volume**, не в каталоге проекта. rsync её не переносит;
  для переноса/синхронизации — дамп (`scripts/backup.sh`) + `scripts/restore.sh`.

## Индекс документации

| Файл | О чём |
|---|---|
| `README.md` | Обзор, детальный quick start, сценарии запуска, парсинг, API |
| `DEPLOY.md` | Деплой на сервер, порты/безопасность, rsync/scp, бэкапы+cron |
| `MIGRATION.md` | Перенос на другой сервер/аккаунт: порядок, сверка данных, откат |
| `docs/SYNC_STRATEGY.md` | Расписание сбора; §4 — runbook добавления марки |
| `docs/ANALYTICS_GUIDE.md` | Нормализация, оценка цены, дневные агрегаты, SQL |
| `docs/DB_SCHEMA_FOR_GPT.md` | Схема БД (таблицы/связи) для генерации запросов |
| `docs/DB_QUERIES.md` | Готовые SQL-запросы |
| `docs/FRONTEND_TZ.md` | Тех.задание фронта (`../analytics-front`) |
| `docs/ADMIN_PANEL_PLAN.md` | **План:** фронт → mobile-first админка (вход через бота, админ-API, Caddy, дизайн) |
| `docs/LALAFO.md` | Источник lalafo.kg: API, ловушки данных, прод-чеклист |
| `docs/LALAFO_PROXY.md` | Доступ к lalafo с прода: Cloudflare-блок, эксперименты, варианты выхода |
| `docs/FINIK_FLOW.md` | Finik простым языком: идея, что с чем связано, поток по схеме `web-sdk-flow.png`, что нужно завести |
| `docs/PAYMENTS_FINIK_ANALYSIS.md` | **Провайдер меняется на Finik**: анализ, спецификация протокола, восстановленный алгоритм подписи + векторы, грабли, пошаговый план работ |
| `docs/CHANGELOG.md` | Журнал изменений бота: что сделано, почему так, логи замеров |
| `docs/NOTIFICATIONS.md` | Компоновка сообщений: карточка vs список, включение группировки, логи |
| `docs/TODO.md` | Общий бэклог бота: пределы нагрузки, лимиты юзера, доставка уведомлений |
| `docs/TODO_LALAFO.md` | Согласованные задачи следующей итерации lalafo |

Долгоживущие факты вне репозитория — в авто-памяти (`MEMORY.md`).

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
  Подробности lalafo API и прод-чеклист — `LALAFO.md`.
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

## Раскладка кода (`app/`)

- `main.py` — FastAPI-приложение, CORS, роутеры. `cli.py` — все ручные команды.
- `worker.py` — планировщик full-scan/rebuild. `core/config.py` — все настройки (env).
- `api/` — роутеры: `routes.py` (estimate/price-trend), `dashboard.py`,
  `catalog.py`, `market.py`.
- `services/` — логика: `sync.py` (обход источника), `normalizer.py` +
  `catalog_service.py` + `catalog.py` (нормализация моделей/алиасы),
  `analytics.py`, `stats.py` (агрегаты), `dashboard.py`.
- `bot/` — aiogram: `poller.py` (fresh-циклы), `handlers.py`, `notifier.py`,
  `db.py` (прямой доступ к БД), `access.py` (проверка членства в группе).
- `models/entities.py` — ORM-модели. `parsers/mashina.py` — адаптер источника.

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
`refresh-rate`.

## Продакшн

AWS Lightsail, `ubuntu@63.180.133.249`, проект в `/home/ubuntu/car-analytics`,
docker без sudo. Полный runbook переноса/обновления/бэкапов — в `DEPLOY.md`.
Локальная разработка и сервер запускаются одинаково (`docker compose`).

## Операционные правила и грабли (проверено на практике)

- **Один токен — один бот.** С одним `TELEGRAM_BOT_TOKEN` работает только ОДИН
  экземпляр (иначе `TelegramConflictError`). Для локальных тестов — отдельный
  токен и отдельный `TELEGRAM_ACCESS_CHAT_ID`; серверный бот не останавливать.
- **Добавление новой марки — строгий порядок:** `.env` → пересоздать
  контейнеры → full-обход → **подтвердить модели алиасами** →
  `reprocess-catalog` → `rebuild-stats`. `rebuild-stats` до подтверждения
  моделей бесполезен: нераспознанные модели дают `analytics_eligible=false`,
  и марки нет во фронте (`/api/v1/catalog/brands` показывает только марки с
  analytics-eligible объявлениями). Полный runbook — `SYNC_STRATEGY.md §4`.
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
| `SYNC_STRATEGY.md` | Расписание сбора; §4 — runbook добавления марки |
| `ANALYTICS_GUIDE.md` | Нормализация, оценка цены, дневные агрегаты, SQL |
| `DB_SCHEMA_FOR_GPT.md` | Схема БД (таблицы/связи) для генерации запросов |
| `DB_QUERIES.md` | Готовые SQL-запросы |
| `FRONTEND_TZ.md` | Тех.задание фронта (`../analytics-front`) |
| `LALAFO.md` | Источник lalafo.kg: API, ловушки данных, прод-чеклист |
| `TODO_LALAFO.md` | Согласованные задачи следующей итерации lalafo |

Долгоживущие факты вне репозитория — в авто-памяти (`MEMORY.md`).

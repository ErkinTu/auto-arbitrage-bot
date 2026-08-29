# Sync Strategy

Стратегия сбора и обновления объявлений. Всё время — локальное
(`SCHEDULER_TIMEZONE`, по умолчанию `Asia/Bishkek`). Список марок задается
в `.env` через `ALLOWED_BRANDS` (сейчас 16 марок, через запятую).

Автоматику выполняют два сервиса:

| Сервис | Задача | Когда |
|---|---|---|
| `bot` | fresh-циклы: новые объявления + уведомления в Telegram | ежедневно, каждые 10 минут, 8:00–24:00 |
| `worker` | полная сверка каталога | суббота и воскресенье, с 2:00 |
| `worker` | пересчет `daily_market_stats` | каждые 8 часов: 9:00, 17:00, 01:00 |

## 1. Постоянные обновления (бот, ежедневно)

Каждые `BOT_FRESH_INTERVAL_MINUTES` (10 минут) в окне
`BOT_ACTIVE_START_HOUR`–`BOT_ACTIVE_END_HOUR` (8:00–24:00) бот делает по
**одному запросу на каждую марку** из `ALLOWED_BRANDS`:

> Интервал считается от НАЧАЛА цикла: поллер спит `interval − длительность
> цикла` (`poller.py`, пол — `MIN_PAUSE_SECONDS=60`). До 29.08.2026 он спал
> полный интервал ПОСЛЕ работы, и период был `10 мин + сбор` — на проде 14.5
> мин вместо 10 (замер — `docs/CHANGELOG.md § 2026-08-29`).


- `page=1`, `size=BOT_FRESH_PAGE_SIZE` (20), сортировка `created_at desc`,
  `created_period=1d` — маленькая страница, чтобы не нагружать сайт;
- N марок = N запросов за цикл (по числу в `ALLOWED_BRANDS`, сейчас 16);
- пауза между запросами — `REQUEST_DELAY_SECONDS` (5 секунд), весь цикл ~1.5–2 минуты;
- найденные события `new` сразу матчятся с фильтрами пользователей,
  уведомления уходят в Telegram.

Ночью (00:00–8:00) бот спит. Объявления, созданные ночью, попадают в первый
цикл после 8:00 благодаря `created_period=1d` (окно перекрытия — сутки).

Расписание worker-а на fresh больше не влияет: бот запускает `run_sync` с
`update_target_bookkeeping=False` и не трогает `last_success_at`.

## 2. Полная сверка (worker, выходные)

Полный обход каталога ловит изменения цены/пробега/статуса у старых
объявлений и деактивирует исчезнувшие. Марки делятся пополам
(алфавитно, стабильно):

- **суббота, с 2:00** — первая половина марок;
- **воскресенье, с 2:00** — вторая половина.

Параметры: `page_size=100`, до `FULL_SCAN_MAX_PAGES=100` страниц и
`FULL_SCAN_MAX_ITEMS=10000` карточек на марку; пауза между страницами —
`REQUEST_DELAY_SECONDS` (5 секунд), пауза между марками —
`FULL_SCAN_BRAND_PAUSE_SECONDS` (1 минута).

Повторный запуск в тот же день исключен: worker проверяет `parser_runs`
(mode=`full` за текущие сутки), поэтому рестарт контейнера безопасен.
Деактивация отсутствующих объявлений срабатывает только если обход дошел
до конца каталога, а не остановился по лимиту.

## 3. Пересчет аналитики (worker)

`daily_market_stats` пересчитывается каждые `STATS_REBUILD_INTERVAL_HOURS`
(8 часов) по сетке от `STATS_REBUILD_START_HOUR` (9:00): **9:00, 17:00,
01:00**. Сетка сдвинута на час от полной сверки (2:00), чтобы ночной слот
не совпадал с full scan по выходным. На этой статистике работают графики
и проверка «цена ниже средней» в боте.

Ручной пересчет в любой момент:

```bash
docker compose exec worker uv run python -m app.cli rebuild-stats
```

## 4. Первичная загрузка новой марки (полный runbook)

> Добавляете несколько марок сразу? Сначала `docs/PROGRESS.md § Добавление
> 7–8 новых марок`: там ночное окно, порядок на пачку (один
> `reprocess-catalog` на всех, а не восемь) и что при этом увидят люди.

> **Порядок строгий: `.env` → пересоздать контейнеры → full-обход →
> ПОДТВЕРДИТЬ МОДЕЛИ (алиасы) → `reprocess-catalog` → `rebuild-stats`.**
> `rebuild-stats` до подтверждения моделей бесполезен: у объявлений с
> нераспознанной моделью `analytics_eligible=false`, они не попадают ни в
> статистику, ни во фронт (`/api/v1/catalog/brands` показывает только марки
> с analytics-eligible объявлениями). Это главная причина «залил, а марки
> нет в аналитике».

Все команды — внутри контейнера (код запечён в образ, bind-mount кода нет),
подойдёт `worker` или `api`. На сервере: `cd /home/ubuntu/car-analytics`.

**Шаг 1. Добавить марку в `ALLOWED_BRANDS`** в `.env` (на сервере — в
серверном `.env`). Пример: `...,Zeekr,Mazda`.

**Шаг 2. Пересоздать контейнеры**, чтобы подхватить новый `.env` — при старте
выполнится `seed` и создастся `sync_target` для марки, а бот/worker включат
её в свои циклы:

```bash
docker compose up -d
docker compose exec -T worker uv run python -m app.cli targets | grep -i <Марка>
```

**Шаг 3. Разовый full-обход** (не дожидаясь выходных):

```bash
docker compose exec -T worker uv run python -m app.cli sync \
  --brand Mazda --mode full --page-size 100 --max-pages 100 --max-items 10000
```

В summary должно быть `complete_scan=True` и `errors=0`.

**Шаг 4. Подтвердить модели (категории).** После обхода нераспознанные
модели лежат как `NormalizationIssue` со `status=open` — их объявления
**ещё не** analytics-eligible. Посмотри список:

```bash
docker compose exec -T worker uv run python -m app.cli catalog-issues --brand Mazda --limit 100
```

Для **каждого реального** написания заведи алиас на чистую каноническую
модель. Из-за бага парсера в имя иногда «протекает» спецификация двигателя
(`CX-50 2.0 AT`, `EZ-60 1.5hyb AT`) — канонической моделью бери чистое имя
(`CX-50`, `EZ-60`), а несколько спец-вариантов сводятся к одной модели:

```bash
docker compose exec -T worker uv run python -m app.cli add-model-alias \
  --brand Mazda --model "CX-50" --alias "CX-50 2.0 AT"
docker compose exec -T worker uv run python -m app.cli add-model-alias \
  --brand Mazda --model "CX-50" --alias "CX-50 2.5 AT 4WD"
# ...и так по всем строкам из catalog-issues
```

Много алиасов удобнее одним процессом (без 30+ холодных стартов `uv run`) —
передав скрипт в контейнер через stdin: `docker compose exec -T worker
uv run python - <<'PY'` … в нём цикл по dict `{canonical: [aliases…]}` с
`add_model_alias(session, brand_name=..., canonical_model_name=..., alias=...)`
и `session.commit()`. **Проверь, что все канонические имена — реальные модели
марки**, а не мусор/спеки.

**Шаг 5. `reprocess-catalog`** — применяет алиасы ко всем уже сохранённым
объявлениям и выставляет `analytics_eligible`, ничего не скачивая заново:

```bash
docker compose exec -T worker uv run python -m app.cli reprocess-catalog </dev/null
```

> Прогон идёт по **всем** объявлениям (~40k+), одной транзакцией с коммитом в
> конце, и занимает несколько минут. Запускай его синхронно с `</dev/null`
> (иначе ssh-сессия висит на stdin) и с большим таймаутом. Не «фонить» через
> `nohup … &` поверх ssh — при обрыве канала `docker compose exec` получает
> сигнал и процесс умирает без коммита (проверено). Прерванный прогон
> безопасен: незакоммиченная транзакция откатывается.

**Шаг 6. `rebuild-stats`** — теперь марка попадёт в `daily_market_stats`:

```bash
docker compose exec -T worker uv run python -m app.cli rebuild-stats </dev/null
```

**Шаг 7. Проверка** (всё должно сойтись):

```bash
# open-issues по марке = 0
docker compose exec -T worker uv run python -m app.cli catalog-issues --brand Mazda --status open --limit 100
# марка видна во фронте с active_count
curl -s http://127.0.0.1:8000/api/v1/catalog/brands | python3 -c 'import sys,json;print([b for b in json.load(sys.stdin) if b["name"]=="Mazda"])'
# все listings марки analytics_eligible
docker compose exec -T postgres psql -U car_user -d car_analytics -t -c \
  "SELECT count(*) FILTER (WHERE analytics_eligible), count(*) FROM listings l JOIN brands b ON b.id=l.brand_id WHERE b.name='Mazda';"
```

Пока по марке нет накопленной статистики за несколько дней, фильтры «только
ниже рынка» по ней молчат (недостаточно образцов — уведомление не уходит).

## 5. Total count (идея на будущее)

API возвращает `api_total` — сейчас он только логируется. Полезное
улучшение: хранить `last_api_total` / `last_api_total_at` в `sync_targets`,
чтобы видеть рост выдачи по марке. `api_total` не должен быть единственным
критерием обновления: даже при неизменном total новые объявления вытесняют
старые с первых страниц.

## 6. Запуск

```bash
docker compose up -d --build postgres api worker bot
```

Нагрузка на внешний API за сутки (16 марок):

- будни: 96 циклов × 16 запросов = ~1540 легких запросов (страница из 20
  объявлений = 1 запрос; страницы в 5 раз меньше прежних 100 штук);
- выходные: + полная сверка половины марок ночью (сотни страниц, с паузами 5с).

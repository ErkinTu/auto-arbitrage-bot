# Car Analytics MVP

MVP-сервис для:

- сбора объявлений выбранных марок из внешнего API;
- сохранения текущего состояния объявления;
- неизменяемой истории изменения цены и статуса;
- ежедневной рыночной статистики;
- API оценки цены и графика средней цены.

Марки по умолчанию задаются в `.env` через `ALLOWED_BRANDS`. В `.env.example` это `BMW,Audi,Toyota,Kia`.

> Важно: внешний endpoint может быть доступен из браузера, но это не означает безусловное разрешение на массовый сбор. Проверь правила источника, не обходи защиту, используй умеренную частоту запросов и понятный User-Agent.

## Что хранит база

- `sources` - источники данных.
- `brands`, `car_models`, `generations` - канонический справочник автомобиля.
- `model_aliases`, `generation_aliases` - варианты написания из объявлений и их канонические значения.
- `normalization_issues` - неизвестные модели, которые ждут подтверждения, а не загрязняют справочник.
- `listings` - текущее состояние объявления.
- `listing_price_history` - первая цена и все дальнейшие изменения.
- `listing_status_history` - новое, обновлено, деактивировано, реактивировано.
- `sync_targets` - что, как часто и в каком объеме собирать.
- `parser_runs` - журнал работы парсера.
- `raw_payloads` - безопасный очищенный JSON только для новых/изменившихся карточек.
- `daily_market_stats` - готовая ежедневная статистика для быстрых графиков.

Личные данные продавцов не сохраняются: имя, телефон, `user_id`, аватар, сообщения и счетчики контактов исключены.

## Быстрый старт через Docker

```bash
cp .env.example .env
# Открой .env и измени POSTGRES_PASSWORD.
# DATABASE_URL в .env нужен для запуска с хоста; docker-compose переопределяет его для контейнеров.
docker compose up --build -d
```

### Одноразовый запуск или постоянный контейнер

`docker compose run --rm api ...` создает временный контейнер, выполняет команду и удаляет его. Это удобно для редких ручных команд, но каждый раз есть накладные расходы на старт.

Для частой работы подними `api` один раз:

```bash
docker compose up -d postgres api
```

После этого запускай CLI внутри уже работающего контейнера:

```bash
docker compose exec api uv run python -m app.cli sync --brand BMW --mode full --page-size 100 --max-pages 5 --max-items 500
docker compose exec api uv run python -m app.cli rebuild-stats
docker compose exec api uv run python -m app.cli targets
```

Если нужен временный override, например delay 5 секунд, используй одноразовый запуск:

```bash
docker compose run --rm -e REQUEST_DELAY_SECONDS=5 api uv run python -m app.cli sync --brand BMW --mode full --page-size 100 --max-pages 5 --max-items 500
```

Проверка:

```bash
curl http://localhost:8000/health
docker compose run --rm api uv run python -m app.cli targets
```

`api` при старте сам выполняет `init-db` и `seed`, затем запускает FastAPI. `worker` при старте тоже выполняет `init-db` и `seed`, затем регулярно запускает включенные `sync_targets`.

### Docker-сценарии запуска

Поднять только базу для ручных CLI-команд:

```bash
docker compose up -d postgres
```

Инициализировать схему и стартовые targets:

```bash
docker compose run --rm api sh -c 'uv run python -m app.cli init-db && uv run python -m app.cli seed'
```

Полностью сбросить Docker-базу и начать заново:

```bash
docker compose down -v --remove-orphans
docker compose up -d postgres
docker compose run --rm api sh -c 'uv run python -m app.cli init-db && uv run python -m app.cli seed'
```

Первичная загрузка BMW: 5 страниц по 100 объявлений, максимум 500 карточек, задержка 10 секунд после каждого запроса к Mashina:

```bash
docker compose run --rm -e REQUEST_DELAY_SECONDS=10 api sh -c 'uv run python -m app.cli sync --brand BMW --mode full --page-size 100 --max-pages 5 --max-items 500'
```

Точечный свежий сбор Audi за последние сутки: первые 3 страницы, не более 200 карточек:

```bash
docker compose run --rm -e REQUEST_DELAY_SECONDS=10 api sh -c 'uv run python -m app.cli sync --brand Audi --mode fresh --period 1d --page-size 100 --max-pages 3 --max-items 200'
```

Пересчитать статистику после загрузки:

```bash
docker compose run --rm api uv run python -m app.cli rebuild-stats
```

Посмотреть последние runs и счетчики напрямую в PostgreSQL:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select id, mode, processed_count, new_count, updated_count, skipped_count, error_count, status from parser_runs order by id desc limit 5;"'
```

Проверить количество объявлений:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from listings;"'
```

### Первичное заполнение одной марки

Для первого заполнения BMW запускай **полный обход без `created_period`**. Парсер идет с `page=1` дальше по страницам, пока API не вернет неполную страницу.

```bash
uv run python -m app.cli sync \
  --brand BMW \
  --mode full \
  --page-size 100 \
  --max-pages 100 \
  --max-items 10000
```

Сначала безопаснее проверить на 2 страницах и 200 карточках, затем увеличить лимиты.

Повторный запуск этой же команды не пропускает уже просмотренные страницы. Ручной `sync` каждый раз начинает с `page=1`; дубли не создаются, потому что объявление ищется по `source_id + external_id`. Если карточка не изменилась, она останется текущей; если цена или данные изменились, обновится `listings` и добавится история.

Полная сверка выбранной марки. Деактивация сработает только если обход действительно дошел до конца, а не остановился по лимиту страниц/карточек:

```bash
uv run python -m app.cli sync \
  --brand Audi \
  --mode full \
  --max-pages 100 \
  --max-items 10000
```

Сбор всех включенных марок:

```bash
uv run python -m app.cli sync-all --mode fresh
```

После дневной полной проверки подготовь быстрые данные для графиков:

```bash
uv run python -m app.cli rebuild-stats
```

## Запуск без Docker

Нужен PostgreSQL 16+ и Python 3.12+.

```bash
cp .env.example .env
# Укажи DATABASE_URL для своего PostgreSQL.
uv sync --all-groups
uv run python -m app.cli init-db
uv run python -m app.cli seed
uv run uvicorn app.main:app --reload
```

Отдельный worker для регулярного fresh scan:

```bash
uv run python -m app.worker
```

## Управление тем, что парсить

Создать или изменить постоянное правило для конкретной модели:

```bash
uv run python -m app.cli add-target \
  --brand Toyota \
  --model Camry \
  --period 1d \
  --page-size 100 \
  --max-pages 4 \
  --max-items 350 \
  --interval 30 \
  --priority 10
```

Посмотреть активные правила:

```bash
uv run python -m app.cli targets
```

Параметры означают:

- `period` - окно новых объявлений, например `1d` или `2d`.
- `page-size` - сколько карточек просить у API на одной странице.
- `max-pages` - жесткий лимит страниц за запуск.
- `max-items` - жесткий лимит обработанных карточек.
- `interval` - минимальный промежуток между автоматическими запусками конкретного правила.
- `priority` - меньше число, выше приоритет.

Docker-вариант той же команды:

```bash
docker compose run --rm api uv run python -m app.cli add-target \
  --brand BMW \
  --period 1d \
  --page-size 100 \
  --max-pages 5 \
  --max-items 500 \
  --interval 60 \
  --priority 10
```

После этого `worker` будет запускать rule автоматически, когда пройдет `interval`:

```bash
docker compose up -d worker
```

## Нормализация каталога

Парсер сохраняет исходные значения `source_model_text` и `source_generation_text` отдельно от канонических справочников. Поколение не обязательно: объявление с известной моделью без поколения остается пригодным для аналитики.

Поддерживаются форматы Mashina: римские обозначения (`V`, `VIII (XV70)`), коды в скобках (`(G05)`), периоды выпуска (`2016-2026`, `2016-н.в.`), а также `Рестайлинг` и вариант написания `Рейстайлинг`. Они приводятся к единому виду: `Рейстайлинг (V)` становится `V Рестайлинг`. Период выпуска не ошибочно принимается за год конкретного автомобиля.

Неизвестная модель не создается автоматически в `car_models`. Она сохраняется с `normalization_status=unknown_model` и появляется в очереди:

```bash
uv run python -m app.cli catalog-issues --brand Toyota
```

Добавить ручное соответствие, затем применить его к уже собранным объявлениям без повторного скачивания:

```bash
uv run python -m app.cli add-model-alias \
  --brand Toyota \
  --model "Mark II" \
  --alias "Mark 2"

uv run python -m app.cli add-generation-alias \
  --brand Toyota \
  --model "Mark II" \
  --generation "IV" \
  --alias "4 поколения"

uv run python -m app.cli reprocess-catalog
uv run python -m app.cli rebuild-stats
```

Docker-вариант:

```bash
docker compose run --rm api uv run python -m app.cli catalog-issues --brand Toyota
docker compose run --rm api uv run python -m app.cli add-model-alias --brand Toyota --model "Mark II" --alias "Mark 2"
docker compose run --rm api uv run python -m app.cli reprocess-catalog
docker compose run --rm api uv run python -m app.cli rebuild-stats
```

После обновления старого развертывания достаточно выполнить `docker compose up --build -d`. `init-db` идемпотентно добавит новые поля `listings`; новые таблицы справочника создаст SQLAlchemy. Позже этот механизм стоит заменить миграциями Alembic.

## API аналитики

После накопления данных:

```bash
curl "http://localhost:8000/api/v1/estimate?brand=Audi&model=Q5&generation=II%20%28FY%29&year=2019&mileage_km=119000"
```

График дневной средней цены:

```bash
curl "http://localhost:8000/api/v1/price-trend?brand=Audi&model=Q5&generation=II%20%28FY%29&year=2019&mileage_km=119000&days=365"
```

Оценка строится так:

1. Ищутся активные объявления с теми же маркой, моделью, поколением и годом.
2. Предпочитаются авто с пробегом в пределах ±50 000 км.
3. Считаются средняя, медианная цена и простой линейный эффект пробега.
4. Возвращается оценочная цена, квартильный диапазон, число образцов и уверенность.

Это не ML-модель и не обещание рыночной цены. Это прозрачная MVP-оценка, которую позже можно заменить на более точную модель.

## Синхронизация

### Fresh scan

Каждые 30-60 минут:

```text
created_period=1d + sort_by=created_at desc
```

Назначение: ловить новые объявления с перекрытием в 1-2 дня, чтобы не потерять карточки после сетевой ошибки.

В fresh-режиме параметр `period` передается во внешний API как `created_period`. Для ручного запуска:

```bash
uv run python -m app.cli sync --brand BMW --mode fresh --period 1d --page-size 100 --max-pages 5 --max-items 500
```

### Full scan

Раз в 2-7 дней:

```text
все страницы конкретной марки или модели
```

Назначение: заметить изменения цены, пробега, статуса и исчезнувшие объявления.

Карточка становится `inactive`, только когда отсутствует в нескольких **полных** проверках. Один неудачный запрос ее не деактивирует.

Важно: деактивация выполняется только если full scan считается завершенным, то есть API вернул пустую или неполную страницу. Если запуск остановился по `max-pages` или `max-items`, это лимитированный обход; он обновит найденные объявления, но не будет деактивировать отсутствующие.

## Как работает один запуск sync

1. Создается запись в `parser_runs` со статусом `running`.
2. Парсер запрашивает страницы с `page=1` до `max-pages`.
3. На каждой странице запрашивается `page-size` карточек.
4. После HTTP-запроса Mashina parser делает паузу `REQUEST_DELAY_SECONDS`.
5. Каждая карточка нормализуется и сохраняется по ключу `source_id + external_id`.
6. Новая карточка получает событие `new`; измененная карточка получает `updated`, `price_changed` или `reactivated`.
7. В `raw_payloads` сохраняется очищенный JSON только для новых или измененных карточек.
8. В конце run получает `completed` или `failed`.

`max-items` ограничивает именно обработанные карточки. Например, `--page-size 100 --max-pages 5 --max-items 500` означает максимум 5 HTTP-страниц и максимум 500 сохраненных/обновленных карточек.

## Как заменить парсер

Бизнес-логика не зависит от Mashina.kg. Любой новый адаптер должен реализовать интерфейс из `app/parsers/base.py`:

```python
class NewSourceParser(ParserAdapter):
    source_name = "new-source"

    def fetch_listings(...):
        # Вернуть FetchPage(items=[raw_json, ...], total=...)
        ...
```

Затем добавь отдельный normalizer, который возвращает `NormalizedListing`. Сохранение истории, статусов и статистики менять не нужно.

## Бэкапы

Создание резервной копии:

```bash
./scripts/backup.sh
```

Восстановление:

```bash
./scripts/restore.sh backups/car_analytics_YYYY-MM-DD_HH-MM-SS.sql.gz
```

Рекомендуемый cron на сервере:

```cron
0 3 * * * cd /path/to/car-analytics-mvp && ./scripts/backup.sh >> logs/backup.log 2>&1
```

Сохраняй копии не только на том же VPS. Минимум: 14 дневных копий локально и одна копия на внешнем хранилище.

## Что улучшить после MVP

1. Перейти с временного идемпотентного upgrade-кода на Alembic до следующего заметного изменения схемы.
2. Подтвердить реальные API-параметры фильтра модели в DevTools -> Network.
3. Заменить разбор `title` на официальный атрибут модели/поколения, если API его отдает.
4. Добавить Redis-кэш для публичных аналитических endpoint-ов.
5. Добавить median absolute deviation / IQR для фильтрации выбросов.
6. Добавить раздельные фоновые задачи для fresh scan, full scan и пересчета статистики.

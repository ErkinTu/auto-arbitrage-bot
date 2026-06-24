# Car Analytics MVP

MVP-сервис для:

- сбора объявлений выбранных марок из внешнего API;
- сохранения текущего состояния объявления;
- неизменяемой истории изменения цены и статуса;
- ежедневной рыночной статистики;
- API оценки цены и графика средней цены.

Стартовые марки: **BMW**.

> Важно: внешний endpoint может быть доступен из браузера, но это не означает безусловное разрешение на массовый сбор. Проверь правила источника, не обходи защиту, используй умеренную частоту запросов и понятный User-Agent.

## Что хранит база

- `sources` - источники данных.
- `brands`, `car_models`, `generations` - справочники автомобиля.
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
# Открой .env и измени POSTGRES_PASSWORD / DATABASE_URL.
docker compose up --build -d

# Для первого ручного заполнения только одной марки вместо worker:
# docker compose up --build -d postgres api
```

Проверка:

```bash
curl http://localhost:8000/health
uv run python -m app.cli targets
```

Первый точечный сбор Audi (первые 3 страницы, не более 200 карточек). `created_period` используется только для свежих объявлений:

```bash
uv run python -m app.cli sync \
  --brand Audi \
  --mode fresh \
  --period 1d \
  --max-pages 3 \
  --max-items 200
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
- `max-pages` - жесткий лимит страниц за запуск.
- `max-items` - жесткий лимит обработанных карточек.
- `interval` - минимальный промежуток между автоматическими запусками конкретного правила.
- `priority` - меньше число, выше приоритет.

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

### Full scan

Раз в 2-7 дней:

```text
все страницы конкретной марки или модели
```

Назначение: заметить изменения цены, пробега, статуса и исчезнувшие объявления.

Карточка становится `inactive`, только когда отсутствует в нескольких **полных** проверках. Один неудачный запрос ее не деактивирует.

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

1. Перейти с `create_all()` на Alembic, когда схема начнет меняться.
2. Подтвердить реальные API-параметры фильтра модели в DevTools -> Network.
3. Заменить разбор `title` на официальный атрибут модели/поколения, если API его отдает.
4. Добавить Redis-кэш для публичных аналитических endpoint-ов.
5. Добавить median absolute deviation / IQR для фильтрации выбросов.
6. Добавить раздельные фоновые задачи для fresh scan, full scan и пересчета статистики.

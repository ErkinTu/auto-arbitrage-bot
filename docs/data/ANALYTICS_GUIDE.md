# Analytics Guide

Коротко о частях проекта, которые работают после загрузки объявлений.

## Что есть в базе

- `listings` - текущее состояние каждого объявления.
- `listing_price_history` - история цен.
- `listing_status_history` - история статусов.
- `brands`, `car_models`, `generations` - канонический каталог авто.
- `model_aliases`, `generation_aliases` - соответствия исходных названий каноническим.
- `normalization_issues` - неизвестные модели, которые нужно подтвердить вручную.
- `daily_market_stats` - готовые дневные агрегаты для быстрых графиков.

Главные поля для аналитики в `listings`:

- `current_price_usd` - цена в USD.
- `brand_id`, `car_model_id`, `generation_id`, `year` - признаки похожих авто.
- `mileage_km` - пробег.
- `is_active` - объявление активно.
- `analytics_eligible` - можно использовать в расчетах.
- `quality_flags` - предупреждения по качеству данных.

## Нормализация

После загрузки объявление приводится к каноническому каталогу:

```text
source text -> brand/model/generation -> analytics_eligible
```

Если модель известна, объявление идет в аналитику. Если модель неизвестна, она не создается автоматически, а попадает в `normalization_issues`.

Посмотреть неизвестные модели:

```bash
docker compose exec api uv run python -m app.cli catalog-issues --status open
```

Добавить alias и переобработать уже загруженные объявления:

```bash
docker compose exec api uv run python -m app.cli add-model-alias --brand Toyota --model "Mark II" --alias "Mark 2"
docker compose exec api uv run python -m app.cli reprocess-catalog
```

## Оценка цены

Сервис оценки ищет похожие активные объявления:

```text
brand + model + generation + year
```

Если есть похожие авто с близким пробегом, они получают приоритет. Возвращаются средняя/медианная логика, диапазон и confidence.

API:

```bash
curl "http://localhost:8000/api/v1/estimate?brand=BMW&model=5%20Series&generation=VII%20%28G30%2FG31%29&year=2019&mileage_km=90000"
```

Использовать, когда нужно оценить конкретное авто.

## Дневные агрегаты

`daily_market_stats` хранит готовые статистики:

```text
brand + model + generation + year + mileage_bucket + customs_status
```

Пересчитать после больших загрузок или правок каталога:

```bash
docker compose exec api uv run python -m app.cli rebuild-stats
```

Использовать, когда нужны быстрые графики, тренды и групповые отчеты.

API тренда:

```bash
curl "http://localhost:8000/api/v1/price-trend?brand=BMW&model=5%20Series&year=2019&days=365"
```

## Прямые SQL-запросы

Для ручной аналитики используй:

- `DB_QUERIES.md` - готовые SQL-запросы.
- `DB_SCHEMA_FOR_GPT.md` - схема таблиц, которую можно отправлять в GPT для генерации SQL.

Пример: средняя цена BMW 5 Series по годам:

```sql
select
  l.year,
  count(*) as listings,
  round(avg(l.current_price_usd), 2) as avg_price_usd
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
where
  b.name = 'BMW'
  and cm.name = '5 Series'
  and l.is_active
  and l.analytics_eligible
  and l.current_price_usd is not null
group by l.year
order by l.year desc;
```

## Когда что использовать

- Нужно проверить конкретное авто: `/api/v1/estimate`.
- Нужно построить график или отчет: `rebuild-stats` + `/api/v1/price-trend`.
- Нужно найти аномалии: SQL из `DB_QUERIES.md`.
- Нужно исправить каталог: `catalog-issues`, `add-model-alias`, `reprocess-catalog`.
- Нужно понять структуру БД: `DB_SCHEMA_FOR_GPT.md`.

# Docker DB Queries

Готовые SQL-запросы для проверки данных напрямую в PostgreSQL из Docker.

## Как выполнить SQL

Открыть интерактивный `psql`:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Выполнить один запрос:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from listings;"'
```

Для многострочных запросов удобнее открыть `psql`, вставить SQL и завершить точкой с запятой.

## Быстрые счетчики

Всего объявлений:

```sql
select count(*) as total_listings
from listings;
```

Активные/неактивные объявления:

```sql
select is_active, count(*) as listings
from listings
group by is_active
order by is_active desc;
```

Объявления по статусу нормализации:

```sql
select normalization_status, count(*) as listings
from listings
group by normalization_status
order by listings desc;
```

Последние запуски парсера:

```sql
select
  id,
  mode,
  processed_count,
  new_count,
  updated_count,
  price_changed_count,
  skipped_count,
  error_count,
  status,
  started_at,
  finished_at
from parser_runs
order by id desc
limit 10;
```

## Модели и поколения

Количество разных моделей по брендам:

```sql
select
  b.name as brand,
  count(distinct cm.id) as model_count
from car_models cm
join brands b on b.id = cm.brand_id
group by b.name
order by model_count desc, b.name;
```

Количество объявлений по моделям:

```sql
select
  b.name as brand,
  cm.name as model,
  count(*) as listings,
  count(*) filter (where l.is_active) as active_listings,
  round(avg(l.current_price_usd), 2) as avg_price_usd
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
where l.current_price_usd is not null
group by b.name, cm.name
order by listings desc, b.name, cm.name;
```

Количество объявлений по модели и поколению:

```sql
select
  b.name as brand,
  cm.name as model,
  coalesce(g.name, 'Без поколения') as generation,
  count(*) as listings,
  round(avg(l.current_price_usd), 2) as avg_price_usd
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
left join generations g on g.id = l.generation_id
where l.current_price_usd is not null
group by b.name, cm.name, coalesce(g.name, 'Без поколения')
order by listings desc, b.name, cm.name, generation;
```

## Похожие авто и средняя цена

Похожие авто здесь считаются группой с одинаковыми `brand + model + generation + year`.
Запрос выводит только группы, где похожих объявлений больше 5.

```sql
select
  b.name as brand,
  cm.name as model,
  coalesce(g.name, 'Без поколения') as generation,
  l.year,
  count(*) as similar_count,
  round(avg(l.current_price_usd), 2) as avg_price_usd,
  percentile_cont(0.5) within group (order by l.current_price_usd) as median_price_usd,
  min(l.current_price_usd) as min_price_usd,
  max(l.current_price_usd) as max_price_usd
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
left join generations g on g.id = l.generation_id
where
  l.is_active
  and l.current_price_usd is not null
  and l.analytics_eligible
group by b.name, cm.name, coalesce(g.name, 'Без поколения'), l.year
having count(*) > 5
order by similar_count desc, brand, model, generation, l.year desc;
```

То же самое только для BMW:

```sql
select
  cm.name as model,
  coalesce(g.name, 'Без поколения') as generation,
  l.year,
  count(*) as similar_count,
  round(avg(l.current_price_usd), 2) as avg_price_usd,
  percentile_cont(0.5) within group (order by l.current_price_usd) as median_price_usd
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
left join generations g on g.id = l.generation_id
where
  b.name = 'BMW'
  and l.is_active
  and l.current_price_usd is not null
  and l.analytics_eligible
group by cm.name, coalesce(g.name, 'Без поколения'), l.year
having count(*) > 5
order by similar_count desc, model, generation, l.year desc;
```

Похожие авто с близким пробегом по бакетам:

```sql
select
  b.name as brand,
  cm.name as model,
  coalesce(g.name, 'Без поколения') as generation,
  l.year,
  (l.mileage_km / 50000) * 50000 as mileage_from,
  ((l.mileage_km / 50000) + 1) * 50000 as mileage_to,
  count(*) as similar_count,
  round(avg(l.current_price_usd), 2) as avg_price_usd,
  percentile_cont(0.5) within group (order by l.current_price_usd) as median_price_usd
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
left join generations g on g.id = l.generation_id
where
  l.is_active
  and l.analytics_eligible
  and l.current_price_usd is not null
  and l.mileage_km is not null
group by
  b.name,
  cm.name,
  coalesce(g.name, 'Без поколения'),
  l.year,
  (l.mileage_km / 50000) * 50000,
  ((l.mileage_km / 50000) + 1) * 50000
having count(*) > 5
order by similar_count desc, brand, model, generation, l.year desc;
```

## Цены и выбросы

Самые дорогие активные объявления:

```sql
select
  l.id,
  b.name as brand,
  cm.name as model,
  coalesce(g.name, 'Без поколения') as generation,
  l.year,
  l.mileage_km,
  l.current_price_usd,
  l.city,
  l.title
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
left join generations g on g.id = l.generation_id
where l.is_active and l.current_price_usd is not null
order by l.current_price_usd desc
limit 50;
```

Самые дешевые активные объявления:

```sql
select
  l.id,
  b.name as brand,
  cm.name as model,
  coalesce(g.name, 'Без поколения') as generation,
  l.year,
  l.mileage_km,
  l.current_price_usd,
  l.city,
  l.title
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
left join generations g on g.id = l.generation_id
where l.is_active and l.current_price_usd is not null
order by l.current_price_usd asc
limit 50;
```

Средняя цена по годам:

```sql
select
  b.name as brand,
  cm.name as model,
  l.year,
  count(*) as listings,
  round(avg(l.current_price_usd), 2) as avg_price_usd,
  percentile_cont(0.5) within group (order by l.current_price_usd) as median_price_usd
from listings l
join brands b on b.id = l.brand_id
join car_models cm on cm.id = l.car_model_id
where
  l.is_active
  and l.analytics_eligible
  and l.current_price_usd is not null
  and l.year is not null
group by b.name, cm.name, l.year
order by b.name, cm.name, l.year desc;
```

## География и продавцы

Объявления по городам:

```sql
select
  coalesce(city, 'Не указан') as city,
  count(*) as listings,
  round(avg(current_price_usd), 2) as avg_price_usd
from listings
where is_active and current_price_usd is not null
group by coalesce(city, 'Не указан')
order by listings desc;
```

Объявления по типу продавца:

```sql
select
  coalesce(seller_type, 'unknown') as seller_type,
  count(*) as listings,
  count(*) filter (where is_official_dealer) as official_dealer_listings,
  count(*) filter (where is_verified_seller) as verified_seller_listings
from listings
group by coalesce(seller_type, 'unknown')
order by listings desc;
```

## Качество нормализации

Неизвестные модели, которые нужно вручную сопоставить:

```sql
select
  b.name as brand,
  ni.source_model_text,
  ni.occurrence_count,
  ni.status,
  ni.first_seen_at,
  ni.last_seen_at
from normalization_issues ni
join brands b on b.id = ni.brand_id
where ni.status = 'open'
order by ni.occurrence_count desc, ni.last_seen_at desc;
```

Объявления с флагами качества:

```sql
select
  id,
  title,
  quality_flags,
  current_price_usd,
  year,
  mileage_km
from listings
where quality_flags::text <> '[]'
order by last_seen_at desc
limit 100;
```

Сколько объявлений пригодны для аналитики:

```sql
select
  analytics_eligible,
  count(*) as listings
from listings
group by analytics_eligible
order by analytics_eligible desc;
```

## История

История изменения цены по конкретному объявлению:

```sql
select
  l.id as listing_id,
  l.title,
  ph.event_type,
  ph.price_usd,
  ph.mileage_km_snapshot,
  ph.customs_status_snapshot,
  ph.observed_at
from listing_price_history ph
join listings l on l.id = ph.listing_id
where l.id = 1
order by ph.observed_at;
```

Последние изменения цены:

```sql
select
  l.id as listing_id,
  b.name as brand,
  cm.name as model,
  l.title,
  ph.event_type,
  ph.price_usd,
  ph.observed_at
from listing_price_history ph
join listings l on l.id = ph.listing_id
left join brands b on b.id = l.brand_id
left join car_models cm on cm.id = l.car_model_id
where ph.event_type = 'price_changed'
order by ph.observed_at desc
limit 100;
```

## Daily Market Stats

После `rebuild-stats` посмотреть готовые агрегаты:

```sql
select
  d.stat_date,
  b.name as brand,
  cm.name as model,
  coalesce(g.name, 'Без поколения') as generation,
  d.year,
  d.mileage_bucket,
  d.customs_status,
  d.sample_count,
  d.avg_price_usd,
  d.median_price_usd,
  d.min_price_usd,
  d.max_price_usd
from daily_market_stats d
join brands b on b.id = d.brand_id
join car_models cm on cm.id = d.car_model_id
left join generations g on g.id = d.generation_id
order by d.stat_date desc, d.sample_count desc
limit 100;
```

## Полезные служебные команды

Пересчитать ежедневные агрегаты:

```bash
docker compose run --rm api uv run python -m app.cli rebuild-stats
```

Посмотреть открытые проблемы каталога через CLI:

```bash
docker compose run --rm api uv run python -m app.cli catalog-issues --status open --limit 100
```

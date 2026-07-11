# DB Schema For GPT

Этот файл можно отправлять в GPT, чтобы он генерировал SQL-запросы к базе проекта.

База: PostgreSQL.
Основная таблица с объявлениями: `listings`.
Цены для аналитики: `current_price_usd`.
Главный фильтр для рыночной аналитики: `is_active = true`, `analytics_eligible = true`, `current_price_usd is not null`.

## Как выполнять запросы в Docker

Интерактивный `psql`:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
```

Один SQL-запрос:

```bash
docker compose exec postgres sh -c 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "select count(*) from listings;"'
```

## Основные связи

```text
sources.id              -> listings.source_id
brands.id               -> car_models.brand_id
brands.id               -> listings.brand_id
car_models.id           -> generations.car_model_id
car_models.id           -> listings.car_model_id
generations.id          -> listings.generation_id
listings.id             -> listing_price_history.listing_id
listings.id             -> listing_status_history.listing_id
parser_runs.id          -> raw_payloads.parser_run_id
listings.id             -> raw_payloads.listing_id
brands.id               -> model_aliases.brand_id
car_models.id           -> model_aliases.car_model_id
car_models.id           -> generation_aliases.car_model_id
generations.id          -> generation_aliases.generation_id
brands.id               -> normalization_issues.brand_id
car_models.id           -> normalization_issues.resolved_model_id
brands.id               -> daily_market_stats.brand_id
car_models.id           -> daily_market_stats.car_model_id
generations.id          -> daily_market_stats.generation_id
sources.id              -> sync_targets.source_id
sync_targets.id         -> parser_runs.target_id
```

## Частые JOIN-шаблоны

Объявления с маркой, моделью и поколением:

```sql
from listings l
left join brands b on b.id = l.brand_id
left join car_models cm on cm.id = l.car_model_id
left join generations g on g.id = l.generation_id
```

Только объявления, пригодные для аналитики:

```sql
where
  l.is_active
  and l.analytics_eligible
  and l.current_price_usd is not null
```

Похожие авто обычно группируются по:

```sql
b.name, cm.name, coalesce(g.name, 'Без поколения'), l.year
```

Для учета пробега добавляй бакет:

```sql
(l.mileage_km / 50000) * 50000
```

## Таблицы

### `sources`

Источники данных.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `name` | varchar | unique, например `mashina.kg` |
| `base_url` | text | базовый URL источника |
| `created_at` | timestamptz | дата создания |
| `updated_at` | timestamptz | дата обновления |

### `brands`

Канонические марки.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `name` | varchar | unique, например `BMW` |
| `slug` | varchar | optional |
| `created_at` | timestamptz | дата создания |
| `updated_at` | timestamptz | дата обновления |

### `car_models`

Канонические модели.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `brand_id` | integer | FK -> `brands.id` |
| `name` | varchar | например `5 Series`, `X5`, `X7` |
| `slug` | varchar | optional |
| `created_at` | timestamptz | дата создания |
| `updated_at` | timestamptz | дата обновления |

Unique: `brand_id + name`.

### `generations`

Канонические поколения моделей.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `car_model_id` | integer | FK -> `car_models.id` |
| `name` | varchar | например `VII (G30/G31)` |
| `code` | varchar | optional |
| `is_facelift` | boolean | рестайлинг или нет |
| `created_at` | timestamptz | дата создания |
| `updated_at` | timestamptz | дата обновления |

Unique: `car_model_id + name`.

### `listings`

Главная таблица объявлений. Используй ее для большинства запросов.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `source_id` | integer | FK -> `sources.id` |
| `external_id` | varchar | id объявления во внешнем источнике |
| `brand_id` | integer | FK -> `brands.id`, nullable |
| `car_model_id` | integer | FK -> `car_models.id`, nullable |
| `generation_id` | integer | FK -> `generations.id`, nullable |
| `title` | text | заголовок объявления |
| `source_slug` | varchar | slug/url part источника |
| `source_make_text` | varchar | исходная марка из источника |
| `source_model_text` | varchar | исходная модель из источника |
| `source_generation_text` | varchar | исходное поколение из источника |
| `normalization_status` | varchar | например `resolved`, `resolved_model_only`, `unknown_model` |
| `normalization_confidence` | varchar | например `exact_alias`, `bootstrap_catalog` |
| `quality_flags` | json | список флагов качества |
| `analytics_eligible` | boolean | можно использовать для аналитики |
| `year` | integer | год авто |
| `mileage_km` | integer | пробег в км |
| `body_type` | varchar | кузов |
| `engine_volume` | numeric | объем двигателя |
| `fuel_type` | varchar | топливо |
| `gearbox` | varchar | коробка |
| `drive_type` | varchar | привод |
| `steering_wheel` | varchar | руль |
| `city` | varchar | город |
| `customs_status` | varchar | статус растаможки |
| `seller_type` | varchar | `dealer` или `private` |
| `is_verified_seller` | boolean | проверенный продавец |
| `is_official_dealer` | boolean | официальный дилер |
| `current_price_amount` | numeric | текущая цена в исходной валюте |
| `current_price_currency` | varchar | валюта исходной цены |
| `current_price_kgs` | numeric | текущая цена в KGS |
| `current_price_usd` | numeric | текущая цена в USD |
| `status` | varchar | статус из источника |
| `is_active` | boolean | активно ли объявление |
| `source_created_at` | timestamptz | дата создания в источнике |
| `source_upped_at` | timestamptz | дата поднятия в источнике |
| `first_seen_at` | timestamptz | впервые увидели |
| `last_seen_at` | timestamptz | последний раз увидели |
| `deactivated_at` | timestamptz | когда деактивировано |
| `missing_full_scans` | integer | сколько полных сканов отсутствовало |
| `payload_hash` | varchar | hash очищенного payload |
| `created_at` | timestamptz | дата создания в БД |
| `updated_at` | timestamptz | дата обновления в БД |

Unique: `source_id + external_id`.

### `listing_price_history`

История цен.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `listing_id` | integer | FK -> `listings.id` |
| `event_type` | varchar | `initial`, `price_changed` |
| `price_amount` | numeric | цена в исходной валюте |
| `price_currency` | varchar | валюта |
| `price_kgs` | numeric | цена в KGS |
| `price_usd` | numeric | цена в USD |
| `mileage_km_snapshot` | integer | пробег на момент события |
| `customs_status_snapshot` | varchar | растаможка на момент события |
| `observed_at` | timestamptz | когда событие зафиксировано |

### `listing_status_history`

История статусов.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `listing_id` | integer | FK -> `listings.id` |
| `event_type` | varchar | `new`, `updated`, `deactivated`, `reactivated`, `price_changed` |
| `old_status` | varchar | старый статус |
| `new_status` | varchar | новый статус |
| `details` | json | дополнительные детали |
| `observed_at` | timestamptz | когда событие зафиксировано |

### `model_aliases`

Сопоставление исходных названий моделей с каноническими.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `brand_id` | integer | FK -> `brands.id` |
| `car_model_id` | integer | FK -> `car_models.id` |
| `alias` | varchar | исходное написание |
| `normalized_alias` | varchar | нормализованное написание |
| `source` | varchar | `bootstrap`, `manual`, etc |
| `created_at` | timestamptz | дата создания |
| `updated_at` | timestamptz | дата обновления |

### `generation_aliases`

Сопоставление исходных поколений с каноническими.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `car_model_id` | integer | FK -> `car_models.id` |
| `generation_id` | integer | FK -> `generations.id` |
| `alias` | varchar | исходное написание |
| `normalized_alias` | varchar | нормализованное написание |
| `source` | varchar | `manual`, `source_normalized`, etc |
| `created_at` | timestamptz | дата создания |
| `updated_at` | timestamptz | дата обновления |

### `normalization_issues`

Очередь неизвестных моделей, которые нужно подтвердить вручную.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `brand_id` | integer | FK -> `brands.id` |
| `resolved_model_id` | integer | FK -> `car_models.id`, nullable |
| `source_model_text` | varchar | исходная модель |
| `normalized_source_model` | varchar | нормализованная исходная модель |
| `status` | varchar | `open`, `resolved`, `ignored` |
| `occurrence_count` | integer | сколько раз встретилось |
| `first_seen_at` | timestamptz | когда впервые встретилось |
| `last_seen_at` | timestamptz | когда последний раз встретилось |
| `created_at` | timestamptz | дата создания |
| `updated_at` | timestamptz | дата обновления |

### `sync_targets`

Правила автоматического парсинга.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `source_id` | integer | FK -> `sources.id` |
| `brand_filter` | varchar | марка |
| `model_filter` | varchar | модель или null |
| `generation_filter` | varchar | поколение или null |
| `enabled` | boolean | включено ли правило |
| `priority` | integer | меньше число = выше приоритет |
| `fresh_period` | varchar | например `1d` |
| `page_size` | integer | размер страницы |
| `max_pages` | integer | лимит страниц |
| `max_items` | integer | лимит карточек |
| `sync_interval_minutes` | integer | периодичность |
| `last_success_at` | timestamptz | последний успешный запуск |
| `last_full_scan_at` | timestamptz | последний полный scan |
| `created_at` | timestamptz | дата создания |
| `updated_at` | timestamptz | дата обновления |

### `parser_runs`

Журнал запусков парсера.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `source_id` | integer | FK -> `sources.id` |
| `target_id` | integer | FK -> `sync_targets.id` |
| `mode` | varchar | `fresh` или `full` |
| `started_at` | timestamptz | старт |
| `finished_at` | timestamptz | завершение |
| `status` | varchar | `running`, `completed`, `failed` |
| `new_count` | integer | новых |
| `updated_count` | integer | обновленных |
| `price_changed_count` | integer | смен цены |
| `reactivated_count` | integer | реактиваций |
| `deactivated_count` | integer | деактиваций |
| `skipped_count` | integer | пропущенных |
| `error_count` | integer | ошибок |
| `processed_count` | integer | обработано |
| `error_message` | text | ошибка |

### `raw_payloads`

Очищенный raw JSON для новых/измененных объявлений.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `source_id` | integer | FK -> `sources.id` |
| `listing_id` | integer | FK -> `listings.id` |
| `parser_run_id` | integer | FK -> `parser_runs.id` |
| `external_id` | varchar | id объявления во внешнем источнике |
| `payload_hash` | varchar | hash payload |
| `payload` | json | очищенный JSON |
| `observed_at` | timestamptz | когда сохранено |

### `daily_market_stats`

Готовые агрегаты после `rebuild-stats`.

| column | type | note |
|---|---|---|
| `id` | integer | primary key |
| `stat_date` | date | дата агрегата |
| `brand_id` | integer | FK -> `brands.id` |
| `car_model_id` | integer | FK -> `car_models.id` |
| `generation_id` | integer | FK -> `generations.id`, nullable |
| `year` | integer | год авто |
| `mileage_bucket` | varchar | бакет пробега |
| `customs_status` | varchar | растаможка |
| `avg_price_usd` | numeric | средняя цена |
| `median_price_usd` | numeric | медианная цена |
| `min_price_usd` | numeric | минимум |
| `max_price_usd` | numeric | максимум |
| `sample_count` | integer | количество объявлений |

## Примеры промптов для GPT

```text
Используй схему ниже и сгенерируй PostgreSQL query.
Нужно получить среднюю цену BMW 5 Series по годам.
Фильтры: только активные, analytics_eligible=true, price_usd not null.
Выведи year, count, avg_price_usd, median_price_usd.
```

```text
Сгенерируй SQL для поиска аномально дорогих BMW X7:
сравни цену объявления со средней ценой похожих авто same model + generation + year,
оставь только группы где похожих авто больше 5,
покажи объявления где цена выше средней более чем в 2 раза.
```

```text
Сгенерируй SQL для распределения объявлений по моделям BMW:
model, active_count, avg_price_usd, median_price_usd, min_price_usd, max_price_usd.
```

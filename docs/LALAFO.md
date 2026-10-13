Парсер работает по средством открытия браузера и прямого парсинга без API как в mashina.kg.
Вот ссылки:
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/toyota
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/hyundai-avtomobili-s-probegom
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/kia-avtomobili-s-probegom
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/bmw
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/honda-avtomobili-s-probegom
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/mercedes-benz-avtomobili-s-probegom
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/lexus
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/daewoo-avtomobili-s-probegom
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/volkswagen
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/audi
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/subaru
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/byd
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/geely
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/nissan-avtomobili-s-probegom
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/zeekr

Как ты видишь, некоторые ссылки с дополнительным "-avtomobili-s-probegom". У нас есть список того что мы отслеживаем .env, при необходимости я дам знать какие авто открываются с дополнительным постфиксом.
Нам не нужно собирать полностью все данные как в mashina.kg (синхронизировать базу данных), достаточно собирать свежие авто. Получаем новые авто и сохраняем данные.
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/subaru?sort_by=newest
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/audi?sort_by=newest
https://lalafo.kg/kyrgyzstan/avtomobili-s-probegom/prodazha-avtomobiley/byd?sort_by=newest
Сортировка тоже возвращяет не отсортированный список, авто могут быть за сегодня, вчерашние и другие, каждый запрос - новый беспорядок. Бесконечный нижний скролл. 

Это детали авто, открываются в новой странице, каждая:
https://lalafo.kg/kemin/ads/toyota-avensis-verso-2000-g-miniven-id-114669711?ad_type=1&feed_id=6500&listing_category_id=1608
https://lalafo.kg/bishkek/ads/toyota-altezza-2000-g-25-l-avtomat-benzin-id-111524325?ad_type=1&feed_id=6500&listing_category_id=1608
https://lalafo.kg/bishkek/ads/toyota-camry-2004-g-24-l-avtomat-benzin-sedan-id-114669553?ad_type=1&feed_id=6500&listing_category_id=1608

1. Лента объявлений (поиск карточек)
Сама карточка: ищем тег <article> (или <div>) с классом, содержащим LFAdTileHorizontal.
Оптимальный селектор: article[class*="LFAdTileHorizontal"]
Ссылка на страницу: ищем тег <a>, у которого атрибут href содержит /ads/.
Оптимальный селектор: a[href*="/ads/"]

2. Страница объявления (сбор характеристик и описания)
Главный контейнер характеристик: ищем тег <ul> с классом adDetailParamsList.
Оптимальный селектор: ul.adDetailParamsList
Строка характеристики: ищем тег <li> с классом, содержащим adDetailParamsListItem.
Оптимальный селектор: li[class*="adDetailParamsListItem"]
Название (ключ): ищем тег <p> с классом, содержащим adDetailParamKey.
Оптимальный селектор: p[class*="adDetailParamKey"]
Значение: ищем тег <div> с классом, содержащим adDetailValueWrap.
Оптимальный селектор: div[class*="adDetailValueWrap"]
Контейнер описания: ищем тег <div> с неизменяемым классом descriptionWrap.
Оптимальный селектор: div.descriptionWrap
Текст описания: ищем тег <p> с классом, содержащим adDetailDescription.
Оптимальный селектор: p[class*="adDetailDescription"]

---

## Итоги анализа (2026-07-18): браузер НЕ нужен

Lalafo — Next.js-приложение, его внутренний JSON API доступен обычным httpx
(без cookies/токенов). CSS-селекторы выше — запасной план на случай закрытия API.

**Обязательные заголовки API:** `device: pc`, `country-id: 12`, `language: ru_RU`.

**Лента (свежие первыми):**
`GET https://lalafo.kg/api/search/v3/ads/search?category_id=<id>&expand=url&page=1&per-page=20&sort_by=newest`
Отдаёт id, title, description, price+currency (USD или KGS), national_price (KGS),
city, created_time/updated_time (unix), images, url. Поле `params` в ленте ПУСТОЕ.
На per-page=20 приходит ~24 элемента: VIP-поднятые добавляются сверх страницы,
они той же марки, но могут быть старыми — свежесть проверять по created_time.

**Карточка:** `GET https://lalafo.kg/api/search/v3/feed/details/<id>?expand=url`
`params`: Модель (чистая!), Год, Пробег, Топливо, Кузов, КПП, Руль, Привод,
Объём двигателя, Цвет, Страна учета, Растаможка. Дёргать только для новых id.

**Марка = category_id** (Toyota=1608, BMW=1557, Honda=1570, Mercedes-Benz=1585).
API списка марок нет; id достаётся из HTML страницы марки — regex
`"searchRequestUrlFromServer":"...category_id=(\d+)` по `__NEXT_DATA__`.
Слаг: пробуем `<brand>` и `<brand>-avtomobili-s-probegom` (нужен для honda,
mercedes-benz). ЛОВУШКА: для неизвестного слага lalafo отдаёт 200 с общим
разделом «Продажа авто» — его category_id надо отвергать (см.
`_get_generic_category_id` в `app/parsers/lalafo.py`).

**Реализация:** `app/parsers/lalafo.py` (LalafoParser: resolve_category_id,
fetch_listings, fetch_details). Смоук без БД:
`PYTHONPATH=. uv run python scripts/lalafo_smoke.py Toyota --limit 5 --details`
Аудит данных по всем маркам (read-only):
`PYTHONPATH=. uv run python scripts/lalafo_brand_audit.py --delay 0.8`

---

## Интеграция (2026-07-18, локально; прод — по чеклисту ниже)

- lalafo — второй источник в общем пайплайне: `app/services/sources.py`
  (фабрика), `parser.normalize()` (диспетчеризация), `_enrich_or_skip` в
  `sync.py` (карточка только для новых/подешевевших; повторы с той же ценой —
  только `last_seen_at`). Fresh-only: full-scan и деактивация lalafo не касаются.
- Бот-цикл обходит оба источника (`app/bot/poller.py`), падение одного не
  мешает другому; уведомления source-aware («Открыть на lalafo.kg»).
- Цены в сомах → USD по дневному курсу НБКР (`app/services/exchange.py`,
  таблица `exchange_rates`, лениво раз в день). Флаг `usd_converted`; смена
  курса не создаёт событий price_changed.
- Мусорные цены (< 500 USD за авто: взносы рассрочки, «180 сом») получают
  критический флаг `price_implausibly_low` — вне аналитики и уведомлений.
- `reprocess-catalog` перепарсивает заголовки ТОЛЬКО mashina-объявлений;
  lalafo-объявления лишь пере-резолвятся по алиасам (поля не затираются).
- Ловушки данных: заглушка «Другая модель» → модель пустая; русские имена
  моделей («Спринтер», «Город» = City); кириллическая «Х» у Zeekr.
- Свежесть (итерация 2026-07-18): бот пушит только публикации ≤ 12 ч
  (`bot_max_listing_age_hours`, NULL-дата = молчим); «зависших» без ленты
  > 30 дней worker ежедневно деактивирует (`stale_listing_days`,
  reason='stale'); статистика строится только по виденным ≤ 30 дней
  (`stats_max_last_seen_days`).
- Паузы раздельные: лента 5с (`lalafo_feed_delay_seconds`), карточки 0.5с
  (`lalafo_detail_delay_seconds`); ответ 429 удваивает detail-паузу до конца
  цикла парсера. Обычный цикл ~80–90с, первый прогрев ~10–12 мин.
- Карточка мёртвого объявления отвечает **HTTP 410 Gone** (проверено живьём
  2026-07-18: продано/удалено/не существовало — всегда 410; живая — 200 с
  `status_id: 2`). **Но 200 ≠ живое** (выяснено 2026-09-16): API `feed/details`
  отдаёт 200 и на снятые объявления — `status_id: 8` (архив/снято, ~74%
  «живых» ответов на проде) и `status_id: 3` (заблокировано модерацией,
  спам) — у обоих публичная HTML-страница того же объявления отвечает 410.
  `status_id: 11` — страница 200, семантика не выяснена (~3% карточек),
  считаем живым. На этом построен точечный валидатор (worker, раз в сутки,
  вручную — `cli.py lalafo-maintenance`): не виденные 3+ дня объявления
  проверяются карточкой от самых давних, не больше
  `lalafo_validate_max_per_run` (3000 ≈ 80 мин через прокси, ~25 МБ трафика).
  410 → reason='card_gone', 200 + мёртвый status_id → reason='card_inactive'
  (в details лежит `status_id`), 200 + status 2 → продление `last_seen_at`.
  Это критично: лента — только новейшие 50 на марку, живое объявление со
  временем уходит из нашего поля зрения, и без валидатора 30-дневный фолбэк
  убивал бы живые.
- **Потолок валидатора обязан превышать суточный поток** объявлений,
  доживающих до 30-дневного порога (09.2026, 24 марки: ~2100/сутки). При
  потолке 500 фолбэк ежедневно гасил ~1600 непроверенных, и по выборке треть
  из них была живой (16.09.2026, 20 карточек: 6 status 2, 13 status 8, 1
  status 11). Теперь фолбэк добивает только то, что валидатор в этом прогоне
  **уже пытался** проверить и не смог (`ValidationReport.reached_last_seen_at`);
  непроверенное копится активным, а worker пишет warning «raise
  LALAFO_VALIDATE_MAX_PER_RUN». Метрика в логе: `candidates`/`drained`.
- **Предохранитель на лежащий прокси**: 5 ошибок карточек подряд прерывают
  прогон (`aborted=True`), и фолбэк пропускается — иначе «не видели 30 дней»
  читается как «пропало», хотя мы просто не смогли посмотреть. До фикса при
  `407 TRAFFIC_EXHAUSTED` (баланс DataImpulse кончился 14.09.2026) валидатор
  с 5 ретраями × 500 карточек висел ~10 ч за ночь, блокируя остальные задачи
  worker (пересчёт статистики, пейвол), а фолбэк потом снял 2795 объявлений
  вслепую. Внутри прогона commit каждые 100 карточек — транзакция не висит
  открытой 40 минут.
- Порядок ленты (проверено живьём 2026-07-18): `sort_by=newest` возвращает
  VIP-блок сверху (при per-page=50 приходит ~54 элемента, VIP помечены
  `paid_features`, бывают месячной давности), дальше — строго `created_time`
  по убыванию. Сайт мешает карточки промо-блоками, API — детерминирован.
- Catch-up: признак пропуска — самое старое объявление ПОЛНОЙ страницы
  оказалось новым (сравнение «вся страница новая» не работает: VIP-шапка
  почти всегда известна). Warning в лог + дочитывание до
  `bot_catchup_max_pages` дополнительных страниц.
- Прерванный рестартом full-scan (mashina) не блокирует повтор в тот же день:
  worker при старте помечает осиротевшие `running` full-запуски failed.

## Прод и Cloudflare (проверено 2026-07-18; полный разбор — `LALAFO_PROXY.md`)

- **lalafo.kg блокирует датацентровые IP**: с AWS Lightsail и HTML, и JSON API
  отвечают 403 «Just a moment…» (челлендж Cloudflare). Браузерный User-Agent
  не помогает; **Cloudflare WARP тоже не проходит** (тестировали в proxy-mode —
  403). С резидентного KG-IP всё работает.
- Решение: `LALAFO_PROXY_URL` — прокси только для lalafo-запросов
  (http/socks5, для socks нужен установленный `httpx[socks]` — в зависимостях).
  Варианты выхода: туннель через домашний/мобильный интернет (обратный SSH:
  `ssh -R 1080 сервер` с устройства в КР → `socks5://127.0.0.1:1080`),
  статический резидентный (ISP) прокси ~$3–10/мес.
- Без прокси код деградирует штатно: lalafo-марки логируют
  `LalafoBrandNotFoundError`/403 и скипаются, mashina работает; валидатор
  на не-410 статусы ничего не деактивирует, после 5 ошибок подряд прерывается
  и отключает фолбэк «зависших» на этот прогон.
- **Баланс прокси кончается молча**: признак — `ProxyError('407
  TRAFFIC_EXHAUSTED')` в логах bot/worker, lalafo-цикл выпадает, mashina
  живёт, пользователи ничего не замечают. Расход реальный — 5 ГБ за ~8 недель
  при 24 марках (не 0.5 ГБ/мес из ранних оценок). Мониторинга баланса нет —
  пополнять с запасом.

## Чеклист выката на прод

1. Код + `docker compose up -d --build`.
2. `init-db` (создаст `exchange_rates`), `seed` (источник lalafo.kg + таргеты).
3. `ensure-self-aliases` (самоалиасы каноническим моделям).
4. Ручные алиасы (данные, в дампе локальной БД или командами):
   Mercedes-Benz Спринтер→Sprinter, T1→T1, GLC-Class→GLC; Hyundai Porter→Porter;
   BMW 530→5 Series, X3 M→X3; VW Passat Variant→Passat, Golf Variant→Golf;
   Zeekr Х(кир.)→X; Daewoo Lanos→Lanos (Sens); Geely GX7, Vision X3;
   Nissan 370Z, Cedric, Navara→Navara (Frontier); Honda Orthia→Orthia,
   Город→City; Toyota Mark X ZiO→Mark X Zio, Mark X→Mark X.
5. `refresh-rate` (первый курс), `reprocess-catalog`, `rebuild-stats`.
6. Перезапустить bot (один токен — один бот!).
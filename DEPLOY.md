# Деплой и запуск

## Что вообще едет на сервер (и что нет)

Проект состоит из двух каталогов, и **на сервере живёт только один**:

| Каталог | Что это | На сервере |
|---|---|---|
| `car-analytics-mvp/` | бэкенд: API, бот, воркер, БД | **да** — целиком, в `/home/ubuntu/car-analytics` |
| `../analytics-front/` | Vue-фронт (админ-панель) | **только сборка** — `dist/` лежит в `admin-dist/`, исходников там нет |

Следствия, которые экономят полчаса недоумения:

- **`api` статику не раздаёт** — это чистый JSON на `127.0.0.1:8000`. Страницы
  отдаёт Caddy: он же терминирует HTTPS и он же единственный публичный вход.
- **Наружу открыты два домена и ничего сверх них**: домен платежей (только
  `/api/v1/payments/*`) и домен панели (только `/api/v1/admin/*` плюс статика).
  Публичные ручки API снаружи недоступны ни на одном из них.
- **`analytics-front` не под git** (решение владельца) и на сервере не
  собирается: rsync-ом едет только готовый `dist/`. Исходников на сервере нет.
- Подробности — раздел «Админ-панель» ниже.

Всё работает внутри Docker-контейнеров, **cron не нужен** (кроме бэкапов).
Расписание встроено в сами сервисы (см. `docs/SYNC_STRATEGY.md`):

| Сервис | Что делает |
|---|---|
| `postgres` | база данных |
| `api` | FastAPI (оценка цены, графики) на :8000 |
| `worker` | full scan по выходным (сб/вс с 2:00), пересчет статистики 9:00/17:00/01:00 |
| `bot` | Telegram-бот: fresh-парс каждые 10 мин (8:00–24:00) + уведомления |

`restart: unless-stopped` поднимает контейнеры после падения и перезагрузки
сервера (нужен включенный Docker: `systemctl enable --now docker`).

> **Один токен — один бот.** С одним `TELEGRAM_BOT_TOKEN` может работать
> только ОДИН экземпляр бота во всём мире. Если локальный и серверный боты
> запущены одновременно, Telegram глушит одного:
> `TelegramConflictError: terminated by other getUpdates request`.
> Перед запуском бота на сервере останови локальный:
> `docker compose stop bot` (локально).

## Запуск (локально и на сервере — одинаково)

```bash
cp .env.example .env   # если .env еще нет; заполнить значения ниже
docker compose up -d --build
```

Обязательные значения в `.env`:

- `POSTGRES_PASSWORD` — сильный пароль;
- `TELEGRAM_BOT_TOKEN` — токен от BotFather;
- `TELEGRAM_ACCESS_CHAT_ID` — id закрытой группы (бот добавлен админом);
- `ALLOWED_BRANDS` — список марок через запятую;
- `REQUEST_DELAY_SECONDS=5.0`.

Проверка:

```bash
docker compose ps            # 4 сервиса Up
docker compose logs -f bot   # "bot poller started: every 10 min within 08:00-24:00"
curl http://127.0.0.1:8000/health
```

---

## rsync и scp — что это и как пользоваться

Оба инструмента копируют файлы на удалённый сервер по SSH. Разница:

- **`scp`** (secure copy) — простое «скопировать файл(ы) туда». Копирует
  всё целиком каждый раз. Удобно для разовой отправки одного файла
  (например, дампа базы).
- **`rsync`** — умная синхронизация каталогов: передаёт **только изменённые
  части файлов**, умеет исключать лишнее (`--exclude`), показывать прогресс.
  Идеально для повторной заливки кода после правок — второй прогон быстрый,
  т.к. шлёт только разницу.

Оба ходят по тому же SSH, поэтому им нужен тот же ключ, что и для `ssh`.
Ключ передаётся так:

- `scp -i КЛЮЧ файл user@server:путь`
- `rsync -av -e "ssh -i КЛЮЧ" ЛОКАЛЬНО/ user@server:путь/`
  (у rsync ключ задаётся через `-e "ssh -i КЛЮЧ"`)

Флаги rsync, которые мы используем:

| Флаг | Значение |
|---|---|
| `-a` | archive: копировать рекурсивно, сохранять права/время |
| `-v` | verbose: показывать, что копируется |
| `-e "ssh -i КЛЮЧ"` | каким SSH/ключом ходить |
| `--exclude 'X'` | не копировать X (можно несколько раз) |
| `-n` | dry-run: только показать, что сделал бы, ничего не копируя |

**Важно про завершающий `/`:** `rsync ./ server:dir/` копирует
*содержимое* текущей папки в `dir/`. Без слэша (`rsync . server:dir`)
создастся вложенная папка. Всегда пиши `./` и `dir/` со слэшами.

---

## Перенос на сервер (наш реальный кейс: AWS Lightsail)

Данные вводных:

```bash
# выполняется в ЛОКАЛЬНОМ терминале

# БОЕВОЙ сервер (свой аккаунт 279341437912, прод с 08.08.2026):
KEY=~/Desktop/aws/car-analytics-prod-key.pem
SRV=ubuntu@63.182.28.250

cd ~/Desktop/myskupka/car-analytics-mvp
```

Старый сервер (`ubuntu@63.180.133.249`, ключ `LightsailDefaultKey-eu-central-1.pem`,
чужой аккаунт) остановлен и держится только как путь отката — см. `MIGRATION.md`.

### 1. Docker на сервере (один раз)

```bash
ssh -i "$KEY" "$SRV" 'curl -fsSL https://get.docker.com | sh \
  && sudo usermod -aG docker ubuntu \
  && sudo systemctl enable --now docker'
```

`usermod -aG docker ubuntu` избавляет от `sudo` перед `docker`. Применяется
только при **новом** логине — переподключись и проверь:

```bash
ssh -i "$KEY" "$SRV" 'docker ps >/dev/null 2>&1 && echo "docker без sudo: OK" \
  || echo "нужен sudo или ещё один реконнект"'
```

Если docker всё ещё требует `sudo` — либо переподключись ещё раз, либо во
всех командах ниже пиши `sudo docker compose ...`.

### 2. Каталог проекта (в домашней папке, sudo не нужен)

```bash
ssh -i "$KEY" "$SRV" 'mkdir -p /home/ubuntu/car-analytics/backups'
```

### 3. Код — через rsync

```bash
rsync -av -e "ssh -i $KEY" \
  --exclude '.venv' --exclude '.git' --exclude 'backups' \
  --exclude '.idea' --exclude '.pytest_cache' --exclude '__pycache__' \
  --exclude '*.zip' --exclude '.ruff_cache' \
  ./ "$SRV:/home/ubuntu/car-analytics/"
```

`.env` едет вместе с кодом (в нём рабочий токен и пароль — restore пройдёт
чисто). `API_HOST`/`POSTGRES_HOST` в `.env` не задаём — дефолт `127.0.0.1`,
наружу ничего не открывается.

### 4. База — дамп через scp

```bash
# локально: свежий дамп (если нужен новый)
./scripts/backup.sh
# отправить на сервер
scp -i "$KEY" backups/car_analytics_*.sql.gz "$SRV:/home/ubuntu/car-analytics/backups/"
```

### 5. Запуск на сервере: postgres → restore → всё остальное

```bash
ssh -i "$KEY" "$SRV"          # заходим на сервер
# --- дальше на сервере ---
cd /home/ubuntu/car-analytics

docker compose up -d postgres            # только БД
docker compose ps                        # ждём "healthy" у postgres

chmod +x scripts/*.sh
./scripts/restore.sh backups/car_analytics_<дата>.sql.gz   # restore в пустую БД

docker compose up -d --build             # api + worker + bot

docker compose ps                        # 4 сервиса Up, порты 127.0.0.1:...
curl http://127.0.0.1:8000/health        # {"status":"ok"}
docker compose logs -f bot               # Ctrl+C для выхода
```

Порядок важен: сначала **только** postgres (чтобы `init-db`/`seed` из
остальных сервисов не заполнили базу до restore), потом restore в чистую БД,
потом всё остальное.

> Перед этим шагом останови локальный бот (`docker compose stop bot`
> локально), иначе два бота с одним токеном будут конфликтовать.

---

## Обновление проекта после правок (синхронизация)

Поменял код локально → залей на сервер и пересобери. Тот же rsync, что при
переносе, **но с `--exclude '.env'`** (он пошлёт только изменившиеся файлы):

> ⚠️ **НЕ затирай серверный `.env`.** Серверный `.env` расходится с локальным
> НАМЕРЕННО: в нём боевой `LALAFO_PROXY_URL` (DataImpulse residential KG),
> которого в локальном `.env` нет и быть не должно. Обычный rsync **без**
> `--exclude '.env'` перезапишет серверный `.env` локальным → на проде пропадёт
> прокси → lalafo сразу упрётся в Cloudflare-блок (403). Поэтому при
> ОБНОВЛЕНИИ всегда исключай `.env` (в отличие от первого переноса выше, где
> `.env` едет специально). Правки настроек, которые реально нужны на проде,
> вноси прямо в серверный `.env` (с бэкапом файла перед правкой).

```bash
# локально, из папки проекта
KEY=~/Desktop/aws/car-analytics-prod-key.pem   # боевой (свой аккаунт)
SRV=ubuntu@63.182.28.250

rsync -av -e "ssh -i $KEY" \
  --exclude '.env' --exclude '.venv' --exclude '.git' --exclude 'backups' \
  --exclude '.idea' --exclude '.pytest_cache' --exclude '__pycache__' \
  --exclude '*.zip' --exclude '.ruff_cache' \
  ./ "$SRV:/home/ubuntu/car-analytics/"

# затем на сервере пересобрать и перезапустить
ssh -i "$KEY" "$SRV" 'cd /home/ubuntu/car-analytics && docker compose up -d --build'
```

Полезно перед реальной заливкой прогнать **dry-run**, чтобы увидеть, что
именно уйдёт (ничего не копирует):

```bash
rsync -avn -e "ssh -i $KEY" --exclude '.env' --exclude '.venv' --exclude '.git' \
  --exclude 'backups' --exclude '.idea' --exclude '.pytest_cache' \
  --exclude '__pycache__' --exclude '*.zip' --exclude '.ruff_cache' \
  ./ "$SRV:/home/ubuntu/car-analytics/"
```

Замечания:

- Правки только в `docker-compose.yml`/коде Python подхватываются через
  `up -d --build`.
- Настройки в `.env` меняй **прямо на сервере** (файл исключён из rsync).
  После правки серверного `.env` перезапусти сервисы (`docker compose up -d`),
  чтобы контейнеры пересоздались с новыми значениями. Если добавляешь **новую**
  переменную — синхронизируй её и в локальный `.env.example`, но НЕ заливай
  локальный `.env` на прод.
- База при `up -d --build` **не трогается** (данные в volume `postgres_data`).
- На будущее чище — приватный git-репозиторий и `git pull` на сервере вместо
  rsync (тогда `.env` держат отдельно, а не заливают из локали).

### Затор DDL при `up -d --build` (случается регулярно, лечится одной командой)

`up -d --build` пересоздаёт `api`, `worker` и `bot` одновременно, и **все три
выполняют `init-db`** (плюс `api` делает это ещё раз в своём lifespan). Отсюда
два наблюдаемых сбоя — оба видны только в логах, наружу выглядят как «сервис не
поднялся».

1. **Лок на `listings`.** `ALTER TABLE listings ADD COLUMN IF NOT EXISTS …`
   берёт ACCESS EXCLUSIVE ДО проверки `IF EXISTS`, поэтому даже no-op ALTER
   встаёт в очередь за любой открытой транзакцией по `listings`. Бот и воркер
   уже стартовали и держат сессию `idle in transaction` (у них
   `autoflush=False`, соединение остаётся в транзакции между запросами) →
   `init_db` висит, `api` замирает на `Waiting for application startup.`,
   `/health` не отвечает.
2. **Гонка `CREATE TABLE`.** Когда в релизе есть НОВАЯ таблица, три `create_all`
   стартуют одновременно и двое проигрывают:
   `UniqueViolation: duplicate key value violates unique constraint
   "pg_type_typname_nsp_index"`. Таблица при этом создаётся (победителем), данные
   целы, но в логах остаётся traceback — не пугаться, а проверить, что таблица и
   её индексы на месте.

**Диагностика и лечение** (в 08/2026 понадобилось при выкатке тарифов):

```bash
docker compose exec -T postgres psql -U car_user -d car_analytics -c "
SELECT pid, state, wait_event_type, age(clock_timestamp(), xact_start) AS xact_age,
       pg_blocking_pids(pid) AS blocked_by, left(query, 60) AS q
  FROM pg_stat_activity
 WHERE datname = current_database() AND pid <> pg_backend_pid()
 ORDER BY xact_start NULLS LAST;"
```

Корень — та строка, у которой `state = idle in transaction` и пустой
`blocked_by`; за ней стоят ALTER-ы. Снять её (транзакция читающая, пул
переподключится сам благодаря `pool_pre_ping`):

```bash
docker compose exec -T postgres psql -U car_user -d car_analytics -c \
  "SELECT pg_terminate_backend(<PID>);"
curl -s 127.0.0.1:8000/health      # через несколько секунд {"status":"ok"}
```

Проверять `pg_stat_activity` полезно и ДО деплоя: пустой список означает, что
затора не будет. Настоящее лечение (advisory-lock вокруг `init_db`, чтобы
миграцию выполнял ровно один контейнер) — в бэклоге, `docs/TODO.md`.

### Первая выкладка ветки `dev-admin-panel` (ещё не сделана)

Ветка с админ-панелью на сервер **не заливалась** (статус — в
`docs/ADMIN_PANEL_PLAN.md`). Когда решишь её выложить, вот что произойдёт и что
проверить. Обычный rsync + `up -d --build`, никаких новых шагов.

**Что изменится в API-части:**

1. **Схема БД доедет сама.** `init-db` на старте контейнера выполнит
   идемпотентный `_upgrade_postgres_schema`: добавится колонка
   `bot_users.is_superuser` (`DEFAULT false`), а `create_all` создаст пустые
   `admin_login_tokens` и `admin_sessions`. Ручных ALTER не нужно, данные не
   трогаются.
2. **Ручек `admin/*` не появится** — при пустом `ADMIN_PANEL_URL` роутеры не
   подключаются вовсе. Это и есть спящий режим.
3. **`/api/v1/market/vehicle-summary` начнёт принимать `mileage_km`.** Параметр
   необязательный; без него ответ прежний, плюс новый блок `mileage` в JSON.
   Старые клиенты не ломаются — поле только добавилось.
4. **`/api/v1/dashboard/*` станет быстрее** (0.29с → 0.095с и 0.23с → 0.055с на
   60k объявлений): медианы считает Postgres. Числа в ответах те же, сверялись.
5. **Вебхук Finik начнёт принимать статус `succeeded`**, а не только `success`.
   Это чинит реальный отвалившийся платёж от 14.08.2026.
6. **У бота появится команда `/admin`** — молчащая, пока `ADMIN_PANEL_URL`
   пуст. В меню команд её нет, пользователи её не увидят.

**Проверка после выкладки:**

```bash
ssh -i "$KEY" "$SRV"
cd /home/ubuntu/car-analytics
docker compose ps                                  # 4 сервиса Up (+caddy, если профиль payments)
curl -s 127.0.0.1:8000/health                      # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' \
  127.0.0.1:8000/api/v1/admin/users                # 404 — панель спит, это норма
curl -s '127.0.0.1:8000/api/v1/dashboard/summary'  # цифры на месте
docker compose logs --tail 50 bot                  # поллер жив, TelegramConflictError нет
```

Отдельно стоит убедиться, что **бот не потерял пользователей**: дождаться
ближайшего fresh-цикла и увидеть в логах отправки. Панель к рассылке отношения
не имеет, но выкладка трогает общий образ, а бот — единственное, что видят люди.

**Откат** — как обычно: `git checkout dev` локально, rsync, `up -d --build`.
Схему откатывать не нужно: лишняя колонка со значением `false` и две пустые
таблицы старому коду не мешают.

---

## Бэкапы и cron на сервере

`scripts/backup.sh` делает дамп из контейнера postgres в `backups/*.sql.gz`
и удаляет копии старше `KEEP_DAYS` (по умолчанию 14).

Ежедневный бэкап настроен на **23:00 UTC = 05:00 по Бишкеку** — самое
тихое окно: бот спит (работает 8:00–24:00), пересчёт статистики (01:00)
и выходной full scan (с 02:00, ~1.5 ч) уже завершены. Хост-cron работает
в UTC. Настройка (**на сервере**):

```bash
ssh -i "$KEY" "$SRV"          # зайти на сервер
# --- на сервере ---
cd /home/ubuntu/car-analytics
mkdir -p logs
crontab -e
# добавить строку:
0 23 * * * cd /home/ubuntu/car-analytics && ./scripts/backup.sh >> logs/backup.log 2>&1
```

Проверить, что cron записан, и посмотреть лог после первого срабатывания:

```bash
crontab -l                    # список задач
cat logs/backup.log           # результат последних запусков
ls -lh backups/               # появляются свежие *.sql.gz
```

> Cron на Ubuntu уже включён (`systemctl status cron`). Docker при этом не
> нужен как cron — расписание парсинга встроено в сервисы, cron только для
> бэкапов.

**Забирайте копии с сервера к себе** — бэкап на том же VPS не спасёт от
потери самого VPS:

```bash
# локально: скачать все дампы с сервера в ./backups/
rsync -av -e "ssh -i $KEY" "$SRV:/home/ubuntu/car-analytics/backups/" ./backups/
```

---

## Подписка / Finik (Caddy + публичный webhook)

> **Состояние: подписка ВКЛЮЧЕНА на проде с 14.08.2026** и принимает боевые
> деньги. Домен — `63-182-28-250.sslip.io`, сертификат Let's Encrypt до
> 12.11.2026 (продлевается сам), порты 80/443 в Lightsail открыты,
> `FINIK_ENV=prod`, приватный ключ лежит в `secrets/`. Оплата пока доступна
> только `SUBSCRIPTION_TESTER_IDS`. Что происходило при включении и какой баг
> при этом поймали — `docs/CHANGELOG.md § 2026-08-14/15`.

Подписочная система (finik.kg, Web SDK, разовая оплата) требует ОДНОГО публичного
HTTPS-входа — webhook Finik. Даёт его контейнер **Caddy** (авто-TLS), который
пробрасывает наружу **только** `/api/v1/payments/*`; сам `api` остаётся на
`127.0.0.1`. Пока креды не заданы — подписка спит, всё работает как раньше
(Caddy не запускается: он под compose-профилем `payments`).

Как всё связано и что происходит при оплате — `docs/FINIK_FLOW.md`;
протокол, алгоритм подписи и грабли — `docs/PAYMENTS_FINIK_ANALYSIS.md`.

Чек-лист включения на проде (пройден 14.08.2026 — оставлен как runbook для
повторения на другом сервере/аккаунте):

1. **Порты 80 и 443** в firewall Lightsail (консоль AWS → инстанс → Networking →
   IPv4 Firewall → Add rule: HTTP/TCP/80 и HTTPS/TCP/443, Anywhere IPv4). До
   этого на сервере открыт только 22.
   > ⚠️ **Порты — ПЕРВЫМ шагом, до запуска Caddy.** С закрытыми портами ACME
   > падает на обоих challenge с `Timeout during connect (likely firewall
   > problem)`, а Caddy ретраит раз в минуту и за десять минут сжигает лимит
   > Let's Encrypt (5 неудачных проверок в час на имя) — дальше ждать час уже с
   > открытыми портами. Если Caddy подняли раньше портов — остановите его
   > (`docker compose --profile payments stop caddy`) и поднимите после.
2. **Домен** → A-запись на боевой сервер, **`63.182.28.250`**.
   > **Без регистратора это тоже решается:** `63-182-28-250.sslip.io` — сервис
   > `sslip.io` резолвит IP прямо из имени, DNS-зона не нужна, Let's Encrypt
   > такое имя принимает (на голый IP сертификат не выдаётся). Так и сделано на
   > проде, потому что собственный домен занят другим продуктом. Цена — сторонняя
   > зависимость в критическом пути платежей: ляжет sslip.io → Finik не отрезолвит
   > webhook, оплаты будут проходить, а подписки не открываться. Для боевой
   > работы лучше свой домен; переезд = две строки в `.env`, в кабинете Finik
   > адрес не прописан (`webhookUrl` шлём в каждом платеже).
3. **Аккаунт Finik**: корпоративный счёт (ИП — онлайн-идентификация в приложении
   Finik; организация — через их представителей). На выходе нужен `accountId`.
   > **Какой именно ID брать.** В кабинете Finik, раздел «Счета», нужна колонка
   > **«ID»** той строки, что помечена как корпоративная (UUID, 36 символов) —
   > это и есть `FINIK_ACCOUNT_ID`, счёт, на который приходят деньги. Колонка
   > **«ID пользователя»** НЕ подходит: она одинакова у всех счетов владельца и
   > не говорит, куда зачислять.
4. **Ключи** (генерировать локально, приватный на сервер не по git):
   ```bash
   openssl genrsa -out finik_private.pem 2048
   openssl rsa -in finik_private.pem -pubout > finik_public.pem
   ```
   В кабинете Finik создать API-ключ типа **«Веб-клиент» (WEB)** и загрузить
   `finik_public.pem` → получить `FINIK_API_KEY`.
5. **Приватный ключ на сервер** (каталог `./secrets` смонтирован в контейнер
   `bot` как `/run/secrets`, в git не попадает):
   ```bash
   scp -i "$KEY" finik_private.pem "$SRV:/home/ubuntu/car-analytics/secrets/"
   ssh -i "$KEY" "$SRV" 'chmod 600 /home/ubuntu/car-analytics/secrets/finik_private.pem'
   ```
   Сверить, что на сервер уехал ключ от той же пары, чей публичный загружен в
   кабинет (иначе подпись не сойдётся, а ошибка будет невнятной — `401`):
   ```bash
   openssl rsa -in finik_private.pem -noout -modulus | openssl sha256
   openssl rsa -pubin -in finik_public.pem -noout -modulus | openssl sha256   # хэши должны совпасть
   ```
6. **`.env` на сервере** (не в git; значения секретов сюда не выписываем):
   ```
   FINIK_API_KEY=...                 # из кабинета, ключ типа WEB
   FINIK_ACCOUNT_ID=...              # UUID корпоративного счёта (колонка «ID»)
   FINIK_ENV=prod                    # beta только если реально есть доступ к beta
   FINIK_QR_NAME=Car Analytics Bot   # латиницей: это видит плательщик
   PAYMENTS_DOMAIN=63-182-28-250.sslip.io
   PUBLIC_BASE_URL=https://63-182-28-250.sslip.io
   BOT_USERNAME=sk_hunterbot         # возврат из оплаты прямо в бота
   SUBSCRIPTION_TESTER_IDS=...       # ОБКАТКА: платить могут только эти id
   DELAYED_TIER_LAG_MINUTES=60        # отставание дешёвых тарифов; 0 = выключить
   SUBSCRIPTION_MAX_HORIZON_DAYS=60   # дальше вперёд доступ не продаём
   SUBSCRIPTION_REMINDER_DAYS_BEFORE=1
   TRIAL_DAYS=3                      # 0 = выключить триал и рефералку
   REFERRAL_FRIENDS_PER_BONUS=3
   REFERRAL_BONUS_DAYS=7
   REFERRAL_MAX_BONUSES=1            # бонус за друзей — один раз на человека
   ```
   > **Цен и сроков в `.env` больше нет.** Тарифная сетка (4 плана: месяц
   > 1990/990, неделя 600/300) живёт в `app/services/plans.py`, потому что у
   > плана есть ещё и тексты, а образ всё равно пересобирается на каждом
   > деплое. Менять прайс = правка одного файла + `up -d --build`.
   > `DELAYED_TIER_LAG_MINUTES` обязан быть ≤ `BOT_NEW_LOOKBACK_MINUTES / 2`
   > (сейчас 60 против 180): иначе у отстающих не остаётся циклов, чтобы
   > объявление подхватилось, и они перестают получать что-либо без единой
   > ошибки в логах. На старте про это ругается warning — читать его в
   > `docker compose logs bot | head`.
   > `FINIK_ENV` выбирает и хост acquiring, и публичный ключ, которым мы
   > проверяем подпись webhook. Кабинет боевой → должно быть `prod`, иначе
   > запросы уйдут на beta-хост, а входящие webhook не пройдут проверку.
   > **`SUBSCRIPTION_TESTER_IDS` — главный предохранитель обкатки.** Пока он
   > заполнен, `/subscribe` отвечает «оплата скоро» всем, кроме перечисленных, и
   > платёж им не заводится вовсе. Пейвол после триала в этом режиме тоже не
   > зовёт в `/subscribe`, а предлагает написать в бот (продлеваем через
   > `grant-subscription`): сообщение о конце триала уходит РОВНО ОДИН раз за всю
   > жизнь пользователя, и потратить его на «оплатите» → «оплата недоступна»
   > значит потерять человека молча. Открыть оплату всем = очистить переменную и
   > пересоздать контейнер `bot`.
7. **Схема БД — единственный ручной DDL.** ✅ *На текущем проде НЕ требуется:
   таблица `payments` уже создана под Finik (с `finik_transaction_id`) при
   переезде 08.08.2026 — сверено `\d payments`.* Инструкция ниже нужна, только
   если поднимаете подписку там, где ещё лежит наследие FreedomPay: таблица
   существует со старой колонкой `freedompay_payment_id`, а `create_all` её не
   переименует. При нулевом числе строк:
   ```bash
   docker compose exec -T postgres psql -U car_user -d car_analytics \
     -c "SELECT count(*) FROM payments;"     # ОБЯЗАТЕЛЬНО должно быть 0
   docker compose stop api worker bot
   docker compose exec -T postgres psql -U car_user -d car_analytics \
     -c "DROP TABLE IF EXISTS payments;"
   ```
   Дальше `init_db()` создаст её заново по актуальной модели (с
   `finik_transaction_id`). Если строки НЕ нулевые — вместо DROP:
   ```sql
   ALTER TABLE payments RENAME COLUMN freedompay_payment_id TO finik_transaction_id;
   ```
   Колонка `bot_users.renewal_reminded_at` на проде уже есть, трогать не надо.
   (Грабля стартового DDL-затора — см. память `prod-deploy-ddl-lock-race`.)
8. **Часы.** `x-api-timestamp` живёт ~10 с → NTP обязателен:
   `timedatectl status` → `System clock synchronized: yes`.
9. **Запуск с Caddy**: `docker compose --profile payments up -d --build`
   (без профиля Caddy не поднимается — как локально). Пересборка обязательна:
   добавилась зависимость `cryptography`.
   > ⚠️ **`up -d caddy` пересоздаёт заодно `api` и `postgres`** — у них меняется
   > `env_file`. `bot` и `worker` при этом не трогаются. Простой измеряется
   > секундами, но если вы этого не ждали, выглядит как случайный рестарт базы.
   Убедиться, что сертификат выпустился:
   ```bash
   docker compose logs caddy --tail 30 | grep -Ei "obtain|certificate|challenge|error"
   # ждём: "certificate obtained successfully"
   ```
10. **Проверка снаружи** (с локальной машины, не с сервера — иначе не проверите
    firewall):
    ```bash
    B=https://63-182-28-250.sslip.io
    curl -s -o /dev/null -w "%{http_code}\n" -X POST -H 'content-type: application/json' -d '{}' $B/api/v1/payments/finik   # 401
    curl -s -o /dev/null -w "%{http_code}\n" $B/docs                                                                        # 404
    curl -s -o /dev/null -w "%{http_code}\n" $B/health                                                                      # 404
    curl -s -o /dev/null -w "%{http_code} -> %{redirect_url}\n" http://63-182-28-250.sslip.io/api/v1/payments/finik          # 308 -> https
    ```
    `401` на webhook — это здоровье, а не ошибка: эндпоинт жив и проверяет
    подпись. `404` на всём остальном подтверждает, что наружу торчит только
    `/api/v1/payments/*`. Сертификат отдельно:
    ```bash
    echo | openssl s_client -connect 63-182-28-250.sslip.io:443 \
      -servername 63-182-28-250.sslip.io 2>/dev/null | openssl x509 -noout -subject -issuer -dates
    ```
11. **Боевая проверка под вайтлистом.** Тестировщик платит по-настоящему и
    сверяет всю цепочку. Прогонять надо **все четыре плана** (минимум 300 сом) и
    отдельно сценарий «апгрейд поверх отстающего».
    > Прежнего трюка «поставить цену 1 сом» больше нет: прайс переехал в
    > `app/services/plans.py`. Если совсем нужно — правка файла и пересборка
    > образа, но нормальный путь теперь — недельный план за 300 сом.
    
    ```bash
    docker compose exec -T postgres psql -U car_user -d car_analytics -c \
      "SELECT order_id, status, amount, finik_transaction_id, paid_at FROM payments ORDER BY id DESC LIMIT 3;"
    docker compose exec -T postgres psql -U car_user -d car_analytics -c \
      "SELECT telegram_id, access_status, subscription_expires_at FROM bot_users
       WHERE telegram_id = <TESTER_ID>;"
    docker compose exec -T postgres psql -U car_user -d car_analytics -c \
      "SELECT b.tier, b.starts_at, b.ends_at, b.origin, p.plan_code
         FROM access_blocks b LEFT JOIN payments p ON p.id = b.payment_id
        WHERE b.user_id = (SELECT id FROM bot_users WHERE telegram_id = <TESTER_ID>)
        ORDER BY b.starts_at;"
    docker compose logs api --since 30m | grep -i "webhook\|payments"
    ```
    Ожидаем: `status='paid'`, `paid_at` заполнен, `access_status='subscriber'`,
    срок продлён, в бот пришло «✅ Оплата получена». По тарифам отдельно:
    `plan_code` доехал в платёж, блок создан РОВНО ОДИН, его `tier` совпал с
    планом, `ends_at` последнего блока = `subscription_expires_at`, а даты в
    сообщении бота совпали с расписанием. Повторная доставка того же
    webhook должна отвечать `200 already processed` (Finik ретраит сутки).
    > **Логи `api` смотреть обязательно, а не только базу.** Именно так поймали
    > `succeeded` вместо `success` (§10.10 анализа): webhook дошёл, подпись
    > сошлась, а платёж был отброшен по статусу — в базе это выглядит просто как
    > «остался `pending`», и без строки `finik webhook: non-success status …`
    > причину не найти. Мы отвечаем `200`, поэтому Finik такой платёж больше не
    > повторит: закрывать вручную (см. «Если webhook не дошёл»).
12. **Beta-среда.** Мы её пропустили: доступ у Finik не запрашивали, обкатали
    сразу на `prod` реальными платежами по 1 сом под вайтлистом — минимальная
    сумма у Finik 0.01 сома, так что боевой тест дешевле переписки. Если beta всё
    же есть, порядок обычный: `FINIK_ENV=beta` → прогон → `prod` → пересоздать
    контейнеры.
13. **Открыть всем**: очистить `SUBSCRIPTION_TESTER_IDS`,
    `docker compose up -d bot api worker`.
    > Помните про тех, у кого триал кончился ДО открытия оплаты: пейвольное
    > сообщение уходит ровно один раз за всю жизнь пользователя, второй раз бот
    > его не пришлёт. Этих людей надо добирать отдельной разовой рассылкой —
    > см. `docs/TODO.md § Открытые хвосты`.

### Тарифы: 4 плана вместо одного

Сетка (реестр — `app/services/plans.py`): месяц 1990 (полная скорость) / 990
(отставание ~1 ч), неделя 600 / 300. Модель данных — `access_blocks`: очередь
оплаченных отрезков доступа, потому что купленный «быстрый» тариф встаёт **в
хвост** уже оплаченному «медленному» (доплаты разницы нет), и доступ становится
расписанием, а не одной датой. Подробности и решения — `docs/PRICING_PLAN.md`.

1. **DDL приезжает сам** (`init_db()` + `create_all`), alembic в проекте нет:
   новая таблица `access_blocks` с partial unique index по `payment_id` и колонка
   `payments.plan_code`.
2. **Бэкфилл идёт в том же релизе и идемпотентен** (`MIGRATION_STATEMENTS`):
   `plan_code` NULL → `'month_full'`, а всем, у кого доступ ещё активен,
   создаётся блок `full` до `subscription_expires_at`. Без него заплатившие
   2000 сом за полную скорость наутро получили бы отставание: без блока
   `effective_tier` честно отвечает `delayed` (fail-safe по умолчанию).
   Побочный эффект осознанный: реферальные и CLI-дни, выданные до тарифов, тоже
   станут полными — отбирать выданное задним числом хуже.
3. Прод-DDL лёгкий (`payments` мал, `access_blocks` новая), но порядок деплоя
   прежний — помнить про затор DDL при `up -d --build` (см. § «Затор DDL при
   `up -d --build`»): при выкатке тарифов он сработал оба раза — и гонкой
   `CREATE TABLE access_blocks`, и локом на `listings`.
4. **Проверка после деплоя** — блоки появились у всех, у кого есть доступ:
   ```bash
   docker compose exec -T postgres psql -U car_user -d car_analytics -c \
     "SELECT count(*) FILTER (WHERE subscription_expires_at > now()) AS with_access,
             (SELECT count(*) FROM access_blocks WHERE ends_at > now()) AS with_block
        FROM bot_users;"
   ```
   Числа должны сойтись. Если `with_block` меньше — часть людей поедет на
   `delayed`, и это надо чинить `grant-subscription --tier full` или SQL.
5. **Выключатели.** Подписка целиком — пустые `FINIK_*` (как раньше). Отставание
   отдельно — `DELAYED_TIER_LAG_MINUTES=0`: при нуле фильтрация не применяется
   вовсе, все платные едут как полные. Нужен именно аварийно: если отставание
   начнёт съедать уведомления, чинить надо мгновенно и без релиза.
6. **Ручное продление знает про скорость**:
   `cli.py grant-subscription --telegram-id N --days 7 [--tier full|delayed]`
   (дефолт `full`: ручное продление — это компенсация, то есть извинение).

### Триал, пейвол и рефералка (выкатывается вместе с подпиской)

Отдельного чек-листа не требует, но три вещи проверить:

1. **Ручного DDL нет.** Новые колонки `bot_users` (`trial_*`, `invite_code`,
   `referrer_id`, `referral_bonuses_granted`) добавляются идемпотентными
   `ALTER … IF NOT EXISTS` в `_upgrade_postgres_schema`, таблицы `user_events` и
   `user_blocks` (чёрный список) создаёт `create_all`. Но это те самые ALTER-ы на
   `bot_users`, которые могут застрять при DDL-заторе на старте — если контейнеры
   зависли, искать `idle in transaction` (см. раздел про деплой выше).
2. **`BOT_USERNAME` обязателен для `/invite`**: без него deep-link
   `t.me/<bot>?start=ref_<code>` не собрать, и команда честно отвечает
   «временно недоступно». Он же используется для возврата после оплаты.
3. **Выключатели.** `TRIAL_DAYS=0` — триал и рефералка выключены целиком
   (кнопки в онбординге нет, доступ как раньше: группа ИЛИ подписка).
   Это же и способ отката без выкатывания кода.
   ⚠️ **Дефолт `TRIAL_DAYS` в коде — 3, а не 0.** Переменной нет в прод-`.env`
   → после выката триал включится САМ, и бот из закрытого станет доступен любому
   на 3 дня. Хотите выкатить код без изменения поведения — впишите `TRIAL_DAYS=0`
   явно. Триал одноразовый (`trial_started_at` не обнуляется никогда), поэтому
   «случайно включили и выключили» = у этих людей второй попытки уже не будет.

Проверка после выката: `/start` от нового аккаунта → онбординг с демо-лотом и
кнопкой; `/invite` отдаёт ссылку; `docker compose exec -T worker uv run python
-m app.cli funnel --days 7` печатает воронку.

### Тарифы Finik (что берут с нас)

Публично заявлено на `finik.kg/features`: **2% на QR, от 2.9% на Visa, без
абонплаты и скрытых комиссий** — платим только с успешных платежей.

- Комиссия удерживается **из суммы платежа**: клиент платит 2000 сом → на счёт
  приходит ≈1960 (QR). Процент несём мы, поэтому он уже в цене.
- **Минимальная сумма платежа — 0.01 сома** (из их же ошибок валидации, §10.5
  анализа), поэтому боевая обкатка на 1 сом ничего не нарушает.
- Выплаты (settlement) на расчётный счёт — пакетами по условиям договора.
- **Уточнить у них:** есть ли **фиксированная/минимальная комиссия за
  транзакцию** (при чеке 2000 сом это важнее процента); сроки выплат; список
  банков КР, чьи приложения открываются по QR; возвраты (refund) — API или
  только кабинет. Полный список вопросов — `docs/PAYMENTS_FINIK_ANALYSIS.md §11`.

### Если webhook не дошёл

У Finik нет задокументированного эндпоинта «проверь статус платежа», а webhook
приходит только на успешную оплату. Finik ретраит доставку до суток (5 попыток
по 30 с, далее удвоение), поэтому короткая авария не теряет платёж. Если платёж
виден в кабинете Finik, а подписки нет — продлить вручную:

```bash
docker compose exec -T worker uv run python -m app.cli grant-subscription \
  --telegram-id 123456789 --days 30 --note "finik txn 241234, webhook не дошёл"
```

**Если платёж есть в нашей базе (строка `pending`), а webhook мы отбили сами** —
как было 14.08 с `succeeded` (§10.10 анализа) — правильнее закрыть именно платёж,
а не выдавать дни отдельно: тогда сходятся и статус строки, и доступ, и воронка.

```bash
docker compose exec -T api uv run python -c "
from sqlalchemy import select
from app.db.session import SessionLocal
from app.models import Payment
from app.services.subscription import apply_paid_payment
with SessionLocal() as s:
    p = s.scalar(select(Payment).where(Payment.order_id == 'ORDER-UUID'))
    apply_paid_payment(s, p, provider_txn_id=None,
                       raw_callback={'manual': 'причина'})
    s.commit()
"
```

`apply_paid_payment` ставит `status='paid'`, `paid_at`, продлевает
`subscription_expires_at`, выставляет `access_status='subscriber'` и пишет
событие `paid` в `user_events`. Ручной `UPDATE payments SET status='paid'` не
делает ничего из этого — получится оплаченный платёж без доступа и дыра в
воронке. Минус способа: у такой строки пустой `finik_transaction_id`, а в
`raw_callback` наша заметка — при сверке с кабинетом она будет выглядеть
аномалией. Это нормально, но помнить надо.

### Подключение мерчанта

Предусловия: **ИП или юрлицо в КР** и расчётный счёт. Для ИП Finik заявляет
регистрацию и **онлайн-идентификацию прямо в приложении** — без визитов и
досылки бумаг. Контакты: `finik@quickpay.kg`, Telegram-поддержка, страница
онбординга `finik.kg/onboarding/`.

## Админ-панель (опубликована 15.08.2026)

Панель работает на **`https://admin.63-182-28-250.sslip.io`** — отдельный
поддомен sslip.io, DNS не нужен (имя само резолвится в IP), сертификат
Let's Encrypt Caddy выпускает сам по HTTP-01.

**Нового контейнера не появилось.** Статику отдаёт тот же Caddy, что уже стоял
ради вебхука Finik: в `docker-compose.yml` ему добавлен том
`./admin-dist:/srv/admin:ro`, в `Caddyfile` — второй блок сайта.

### Что открыто наружу, а что нет

| Домен | Путь | Куда идёт |
|---|---|---|
| `63-182-28-250.sslip.io` | `/api/v1/payments/*` | `api:8000` — вебхук Finik |
| | всё остальное | 404 |
| `admin.63-182-28-250.sslip.io` | `/api/v1/admin/*` | `api:8000`, за `current_admin` |
| | `/api/*` прочее | 404 (честный, а не SPA-заглушка) |
| | остальное | статика панели из `/srv/admin` |

Публичные ручки (`/estimate`, `/catalog/*`, `/dashboard/*`, `/docs`) наружу
**не проксируются ни на одном домене**. Панели они тоже не нужны: экран
«Рынок» ходит в их копии под `/api/v1/admin/catalog/*` и
`/api/v1/admin/market/*` — те же сервисы, но за гейтом. Именно поэтому
публикация панели не потащила за собой публикацию всей аналитики.

Проверка границ (после любой правки `Caddyfile`):

```bash
curl -s -o /dev/null -w '%{http_code}\n' https://admin.63-182-28-250.sslip.io/                      # 200 — панель
curl -s -o /dev/null -w '%{http_code}\n' https://admin.63-182-28-250.sslip.io/api/v1/admin/users    # 401 — гейт
curl -s -o /dev/null -w '%{http_code}\n' https://admin.63-182-28-250.sslip.io/api/v1/catalog/brands # 404 — закрыто
curl -s -o /dev/null -w '%{http_code}\n' https://63-182-28-250.sslip.io/api/v1/payments/success     # 200 — платежи целы
```

### Кто может войти

Только те, у кого в БД стоит `is_superuser`. На 15.08.2026 это двое:
`1184591481` (@AccErkin) и `7102547557` (@autotradekg). Выдаётся **только SQL**:

```bash
docker compose exec -T postgres psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c \
  "UPDATE bot_users SET is_superuser = true WHERE telegram_id = <TG_ID>;"
# снять: ... SET is_superuser = false ...
# список: SELECT telegram_id, username FROM bot_users WHERE is_superuser;
```

Членство в закрытой группе доступа к панели **не даёт** — это разные вещи.
Человек должен хотя бы раз написать боту, иначе строки в `bot_users` нет.

### Как входить

Написать боту `/admin` → придёт кнопка со ссылкой (живёт 5 минут, срабатывает
один раз) → откроется панель, сессия в куке на 12 часов. Ни логина, ни пароля
в системе нет.

Страховка, если бот недоступен, — выдать токен руками и открыть ссылку:

```bash
docker compose exec -T worker uv run python -c \
  "from app.bot import db; print(db.issue_admin_login_token(<TG_ID>))"
# открыть https://admin.63-182-28-250.sslip.io/login?t=<токен>
```

### Обновление панели

Фронт не под git и на сервере не собирается — только готовая сборка:

```bash
cd ../analytics-front && pnpm build          # → dist/
rsync -a --delete -e "ssh -i $KEY" dist/ "$SRV:/home/ubuntu/car-analytics/admin-dist/"
```

Перезапускать ничего не нужно: Caddy отдаёт файлы с диска. **Но** после правки
самого `Caddyfile` конфиг надо перечитать — контейнер не пересоздаётся, файл
смонтирован томом:

```bash
docker compose exec -T caddy caddy reload --config /etc/caddy/Caddyfile
```

### Как выключить обратно

Панель гасится одной переменной, без выкладки кода:

```bash
# на сервере, в .env
ADMIN_PANEL_URL=      # роутеры admin/* не подключатся: 404 вместо 401
ADMIN_DOMAIN=         # блок сайта в Caddy не поднимется
docker compose --profile payments up -d
```

Данные при этом не трогаются, платежи продолжают работать.

### Границы, которые надо помнить

- **Auth — единственная защита.** Панель доступна из интернета всем, и между
  чужим человеком и боевой базой стоит только контур входа. Отсюда: ручки
  строго read-only (менять данные из панели нечем), потолок выборки 200 строк,
  ссылка одноразовая, сессия отзывается удалением строки.
- **Домен платежей не трогать.** `Host` входит в подпись вебхука Finik —
  поэтому панель живёт отдельным доменом, а не путём на том же.
- **sslip.io и Let's Encrypt.** Сертификат выдаётся, но лимиты LE общие на весь
  sslip.io. Если упрёмся — в логах Caddy будет rate limit; лечится своим
  доменом.

## Безопасность

- Часовой пояс контейнеров (UTC) не трогать — код считает время сам по
  `SCHEDULER_TIMEZONE=Asia/Bishkek`.
- **Порты API (8000) и БД (5436→5432) по умолчанию публикуются только на
  `127.0.0.1` хоста** — снаружи недоступны, даже если в AWS Security Group
  порт случайно открыт (приложение просто не слушает внешний интерфейс).
  Это задаётся `API_HOST` / `POSTGRES_HOST` в `.env` (дефолт `127.0.0.1`).
  Проверка на сервере:
  ```bash
  docker compose ps                       # порты вида 127.0.0.1:8000->8000
  curl http://127.0.0.1:8000/health       # локально: {"status":"ok"}
  ```
  Проверка снаружи (с локальной машины) — должно быть refused/timeout:
  ```bash
  curl -m 5 http://63.182.28.250:8000/health
  ```
- Бот и worker к API **не обращаются** — работают с БД по внутренней сети
  Docker (`postgres:5432`), поэтому закрытый наружу API их не затрагивает.
- Открывать API наружу (`API_HOST=0.0.0.0`) — только осознанно и обязательно
  за reverse-proxy (nginx/caddy с HTTPS) + auth: у самого API аутентификации
  нет. Дополнительный рубеж — файрвол (`ufw allow ssh && ufw enable`), но
  учти, что публикация портов Docker обходит `ufw`; на AWS полагайся на
  Security Group, а не только на `ufw`.
- `.env` не коммитить (уже в `.gitignore`).

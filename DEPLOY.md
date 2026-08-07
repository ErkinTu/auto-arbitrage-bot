# Деплой и запуск

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

Данные вводных (подставь свои):

```bash
# выполняется в ЛОКАЛЬНОМ терминале
KEY=~/Desktop/aws/LightsailDefaultKey-eu-central-1.pem
SRV=ubuntu@63.180.133.249
cd ~/Desktop/myskupka/car-analytics-mvp
```

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
KEY=~/Desktop/aws/LightsailDefaultKey-eu-central-1.pem
SRV=ubuntu@63.180.133.249

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

Подписочная система (finik.kg, Web SDK, разовая оплата) требует ОДНОГО публичного
HTTPS-входа — webhook Finik. Даёт его контейнер **Caddy** (авто-TLS), который
пробрасывает наружу **только** `/api/v1/payments/*`; сам `api` остаётся на
`127.0.0.1`. Пока креды не заданы — подписка спит, всё работает как раньше
(Caddy не запускается: он под compose-профилем `payments`).

Как всё связано и что происходит при оплате — `docs/FINIK_FLOW.md`;
протокол, алгоритм подписи и грабли — `docs/PAYMENTS_FINIK_ANALYSIS.md`.

Чек-лист включения на проде:

1. **Домен** → A-запись на `63.180.133.249`; открыть в Security Group порты
   80 и 443 (нужны Let's Encrypt).
2. **Аккаунт Finik**: корпоративный счёт (ИП — онлайн-идентификация в приложении
   Finik; организация — через их представителей). На выходе нужен `accountId`.
3. **Ключи** (генерировать локально, приватный на сервер не по git):
   ```bash
   openssl genrsa -out finik_private.pem 2048
   openssl rsa -in finik_private.pem -pubout > finik_public.pem
   ```
   В кабинете Finik создать API-ключ типа **«Веб-клиент» (WEB)** и загрузить
   `finik_public.pem` → получить `FINIK_API_KEY`.
4. **Приватный ключ на сервер** (каталог `./secrets` смонтирован в контейнер
   `bot` как `/run/secrets`, в git не попадает):
   ```bash
   scp -i "$KEY" finik_private.pem "$SRV:/home/ubuntu/car-analytics/secrets/"
   ssh -i "$KEY" "$SRV" 'chmod 600 /home/ubuntu/car-analytics/secrets/finik_private.pem'
   ```
5. **`.env` на сервере** (не в git):
   ```
   FINIK_API_KEY=...
   FINIK_ACCOUNT_ID=...
   FINIK_ENV=beta                    # сначала тестовая среда, потом prod
   FINIK_QR_NAME=Car Analytics Bot   # латиницей: это видит плательщик
   PAYMENTS_DOMAIN=pay.example.com
   PUBLIC_BASE_URL=https://pay.example.com
   BOT_USERNAME=your_bot             # возврат из оплаты прямо в бота
   SUBSCRIPTION_PRICE_KGS=2000
   SUBSCRIPTION_PERIOD_DAYS=30
   SUBSCRIPTION_REMINDER_DAYS_BEFORE=1
   TRIAL_DAYS=3                      # 0 = выключить триал и рефералку
   REFERRAL_FRIENDS_PER_BONUS=3
   REFERRAL_BONUS_DAYS=7
   ```
6. **Схема БД — единственный ручной DDL.** На проде таблица `payments` уже
   существует со старой колонкой `freedompay_payment_id` (спящий код FreedomPay
   уехал коммитом `1080a8d`), а `create_all` её не переименует. Строк в ней 0
   (сверено 2026-07-31), поэтому:
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
7. **Часы.** `x-api-timestamp` живёт ~10 с → NTP обязателен:
   `timedatectl status` → `System clock synchronized: yes`.
8. **Запуск с Caddy**: `docker compose --profile payments up -d --build`
   (без профиля Caddy не поднимается — как локально). Пересборка обязательна:
   добавилась зависимость `cryptography`.
9. **Проверка**: `curl -X POST https://<домен>/api/v1/payments/finik` → `400/401`
   (жив, но без подписи не работает); любой другой путь наружу → 404.
   В боте `/subscribe` → кнопка оплаты; после оплаты приходит webhook и подписка
   активируется автоматически.
10. Обкатав `FINIK_ENV=beta` (первый платёж — на 1 сом, чтобы заодно проверить
    единицы `Amount`), переключить на `prod` и пересоздать контейнеры.

### Триал, пейвол и рефералка (выкатывается вместе с подпиской)

Отдельного чек-листа не требует, но три вещи проверить:

1. **Ручного DDL нет.** Новые колонки `bot_users` (`trial_*`, `invite_code`,
   `referrer_id`, `referral_bonuses_granted`) добавляются идемпотентными
   `ALTER … IF NOT EXISTS` в `_upgrade_postgres_schema`, таблицу `user_events`
   создаёт `create_all`. Но это те самые ALTER-ы на `bot_users`, которые могут
   застрять при DDL-заторе на старте — если контейнеры зависли, искать
   `idle in transaction` (см. раздел про деплой выше).
2. **`BOT_USERNAME` обязателен для `/invite`**: без него deep-link
   `t.me/<bot>?start=ref_<code>` не собрать, и команда честно отвечает
   «временно недоступно». Он же используется для возврата после оплаты.
3. **Выключатели.** `TRIAL_DAYS=0` — триал и рефералка выключены целиком
   (кнопки в онбординге нет, доступ как раньше: группа ИЛИ подписка).
   Это же и способ отката без выкатывания кода.

Проверка после выката: `/start` от нового аккаунта → онбординг с демо-лотом и
кнопкой; `/invite` отдаёт ссылку; `docker compose exec -T worker uv run python
-m app.cli funnel --days 7` печатает воронку.

### Тарифы Finik (что берут с нас)

Публично заявлено на `finik.kg/features`: **2% на QR, от 2.9% на Visa, без
абонплаты и скрытых комиссий** — платим только с успешных платежей.

- Комиссия удерживается **из суммы платежа**: клиент платит 2000 сом → на счёт
  приходит ≈1960 (QR). Процент несём мы, поэтому он уже в цене.
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

### Подключение мерчанта

Предусловия: **ИП или юрлицо в КР** и расчётный счёт. Для ИП Finik заявляет
регистрацию и **онлайн-идентификацию прямо в приложении** — без визитов и
досылки бумаг. Контакты: `finik@quickpay.kg`, Telegram-поддержка, страница
онбординга `finik.kg/onboarding/`.

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
  curl -m 5 http://63.180.133.249:8000/health
  ```
- Бот и worker к API **не обращаются** — работают с БД по внутренней сети
  Docker (`postgres:5432`), поэтому закрытый наружу API их не затрагивает.
- Открывать API наружу (`API_HOST=0.0.0.0`) — только осознанно и обязательно
  за reverse-proxy (nginx/caddy с HTTPS) + auth: у самого API аутентификации
  нет. Дополнительный рубеж — файрвол (`ufw allow ssh && ufw enable`), но
  учти, что публикация портов Docker обходит `ufw`; на AWS полагайся на
  Security Group, а не только на `ufw`.
- `.env` не коммитить (уже в `.gitignore`).

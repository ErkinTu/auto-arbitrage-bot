# MIGRATION.md — перенос проекта на свой сервер

Короткий runbook: боевой бот живёт на **чужом** AWS-аккаунте (сервер друга),
переносим на **свой**. Полное описание деплоя и бэкапов — `DEPLOY.md`, здесь
только последовательность переезда и его специфика.

Обозначения (подставь свои):

```bash
KEY_OLD=~/Desktop/aws/LightsailDefaultKey-eu-central-1.pem
SRV_OLD=ubuntu@63.180.133.249            # сервер друга
KEY_NEW=~/Desktop/aws/<новый-ключ>.pem
SRV_NEW=ubuntu@<IP-нового-сервера>
```

## Что переносится, а что нет

| Что | Как переносится |
|---|---|
| Код | rsync из локального репозитория |
| **База** | **только дампом** — она в docker-volume `postgres_data`, rsync её не видит |
| `.env` | **копировать со СТАРОГО сервера**, не из локального: там боевой `LALAFO_PROXY_URL` (DataImpulse), которого локально нет |
| `secrets/finik_private.pem` | отдельным `scp`, в git его нет (на текущем проде его нет вовсе — оплата не поднималась) |
| `backups/` | не переносим (тяжёлые), но свежий дамп забираем к себе |

Прокси lalafo авторизуется логином/паролем, а не по IP, — с нового IP заработает
без перевыпуска. Telegram и Finik к IP тоже не привязаны.

## С какими файлами работаем

Всё нужное для переезда уже снято в **`migration/<дата>/`** (каталог в
`.gitignore` — внутри боевой токен и пароли, в git он не попадёт никогда).
Опись и «что куда идёт» — в `migration/<дата>/README.md`. Раскладка:

```
migration/2026-08-08/
├── env/
│   ├── env.prod                  ← ЭТО кладём как .env на новый сервер
│   └── .env.bak*                 история конфигурации, только для разбора
├── db/
│   └── car_analytics_*.sql.gz    ТЁПЛЫЙ дамп (страховка, не для переезда)
├── config/                       docker-compose.yml, Caddyfile, Dockerfile… (для сверки)
├── logs/                         backup.log + по 1500 строк логов bot/worker/api/postgres
├── system/server-snapshot.txt    железо, порты, cron, размер БД, СЧЁТЧИКИ ТАБЛИЦ
└── SHA256SUMS                    sha256sum -c SHA256SUMS
```

Три файла, которые реально участвуют в переезде:

1. **`env/env.prod`** → станет `.env` на новом сервере.
2. **холодный дамп** — снимается в шаге 5, ляжет в `migration/<дата>/db/`.
3. **`system/server-snapshot.txt`** — по счётчикам таблиц оттуда сверяем restore.

Тёплый дамп из слепка нужен как страховка: он снят на работающих сервисах и
отстаёт от момента переключения на несколько часов. Восстанавливать из него —
только если холодный дамп не удался.

Обновить слепок (например, если переезд отложился) — повторить `scp` из шага 0.

---

## 0. Снять слепок сервера (если ещё не снят)

```bash
SNAP=migration/$(date +%F); mkdir -p $SNAP/{env,config,logs,system,db}; chmod 700 migration $SNAP

scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/.env"        "$SNAP/env/env.prod"
scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/.env.bak*"   "$SNAP/env/"
scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/logs/*"      "$SNAP/logs/"
for f in docker-compose.yml Caddyfile Dockerfile .dockerignore pyproject.toml uv.lock; do
  scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/$f" "$SNAP/config/"; done
# ключ Finik, если он уже появился на сервере
scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/secrets/*"   "$SNAP/env/" 2>/dev/null

chmod 600 $SNAP/env/*
( cd $SNAP && find . -type f ! -name SHA256SUMS -exec sha256sum {} \; | sort -k2 > SHA256SUMS )
```

Плюс снимок системы и **счётчики таблиц** (без них потом нечем сверять restore):

```bash
ssh -i "$KEY_OLD" "$SRV_OLD" 'cd /home/ubuntu/car-analytics
  lscpu | grep -E "^Model name|^CPU\(s\)"; free -h; df -h /; docker compose ps; crontab -l
  docker compose exec -T postgres psql -U car_user -d car_analytics -c "
    SELECT '"'"'listings'"'"' t, count(*) FROM listings
    UNION ALL SELECT '"'"'bot_users'"'"', count(*) FROM bot_users
    UNION ALL SELECT '"'"'user_filters'"'"', count(*) FROM user_filters
    UNION ALL SELECT '"'"'sent_notifications'"'"', count(*) FROM sent_notifications
    UNION ALL SELECT '"'"'daily_market_stats'"'"', count(*) FROM daily_market_stats
    ORDER BY 1;"' > $SNAP/system/server-snapshot.txt
```

---

## 1. Создать инстанс на своём аккаунте

- **Lightsail, 2 ГБ RAM / 2 vCPU / 60 ГБ SSD — $12/мес.** Это ровно текущая
  конфигурация; 1 ГБ мало (стек уже уходит в swap), 20 ГБ диска мало (образы
  ~6 ГБ + build cache).
- Ubuntu 24.04 LTS.
- **Прикрепить static IP** — иначе после stop/start адрес сменится и утащит за
  собой DNS и webhook Finik.
- Firewall: оставить 22; **80/443 открывать только если поднимаешь оплату**
  (Caddy). Порты API и БД наружу не публикуются, они на `127.0.0.1`.

> Свой аккаунт на бесплатном плане **закрывается сам** через 6 месяцев или по
> исчерпании кредитов, вместе с данными. Прежде чем ставить сюда прод — перевести
> аккаунт на Paid Plan и поставить бюджет-алерт. Кредиты при этом не теряются.

## 2. Подготовить сервер

```bash
ssh -i "$KEY_NEW" "$SRV_NEW" 'curl -fsSL https://get.docker.com | sh \
  && sudo usermod -aG docker ubuntu && sudo systemctl enable --now docker'
# переподключиться, чтобы docker работал без sudo:
ssh -i "$KEY_NEW" "$SRV_NEW" 'docker ps >/dev/null && echo "docker без sudo OK"'

# swap 2 ГБ: 1.9 ГБ RAM для 4 контейнеров впритык, на старом сервере swap занят
ssh -i "$KEY_NEW" "$SRV_NEW" 'sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \
  && sudo mkswap /swapfile && sudo swapon /swapfile \
  && echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab'

# NTP обязателен (подпись Finik живёт ~10 с). TZ контейнеров НЕ трогать.
ssh -i "$KEY_NEW" "$SRV_NEW" 'timedatectl | grep -E "synchronized|Time zone"'

ssh -i "$KEY_NEW" "$SRV_NEW" 'mkdir -p /home/ubuntu/car-analytics/{backups,secrets,logs}'
```

## 3. Залить код, `.env` и ключи (без простоя, заранее)

Всё, кроме базы, переносится до окна простоя — старый сервер продолжает работать.

```bash
cd ~/Desktop/myskupka/car-analytics-mvp
rsync -av -e "ssh -i $KEY_NEW" \
  --exclude '.env' --exclude '.venv' --exclude '.git' --exclude 'backups' \
  --exclude '.idea' --exclude '.pytest_cache' --exclude '__pycache__' \
  --exclude '*.zip' --exclude '.ruff_cache' --exclude 'migration' \
  ./ "$SRV_NEW:/home/ubuntu/car-analytics/"

# .env — из слепка, НЕ локальный
scp -i "$KEY_NEW" migration/2026-08-08/env/env.prod "$SRV_NEW:/home/ubuntu/car-analytics/.env"
# ключ Finik — только если он есть (на текущем проде его нет)
scp -i "$KEY_NEW" migration/2026-08-08/env/finik_private.pem \
  "$SRV_NEW:/home/ubuntu/car-analytics/secrets/" 2>/dev/null || echo "ключа Finik нет — ок"

ssh -i "$KEY_NEW" "$SRV_NEW" 'cd /home/ubuntu/car-analytics && chmod 600 .env secrets/* 2>/dev/null; \
  chmod +x scripts/*.sh'
```

## 4. Собрать образ заранее (тоже без простоя)

Сборка — самая долгая и самая голодная часть. Делаем её до остановки старого
сервера, отдельно от `up`: на 1.9 ГБ RAM `up -d --build` рядом с работающим
postgres может уйти в OOM.

```bash
ssh -i "$KEY_NEW" "$SRV_NEW" 'cd /home/ubuntu/car-analytics && docker compose build'
```

---

# ОКНО ПРОСТОЯ

Дальше идёт единственная часть, когда бот молчит. Порядок: **сохранить базу →
остановить сервисы → снять холодный дамп → перенести → поднять на новом.**
Реально это 10–20 минут, из них большая часть — передача 68 МБ.

## 5. Остановить сервисы и снять холодный дамп

```bash
ssh -i "$KEY_OLD" "$SRV_OLD"
cd /home/ubuntu/car-analytics

# 5.1 страховка ДО остановки — на случай, если что-то пойдёт не так
./scripts/backup.sh

# 5.2 остановить всё, что пишет в базу. postgres НЕ трогаем — из него дампим.
docker compose stop bot worker api
docker compose ps            # bot/worker/api — Exited, postgres — Up (healthy)

# 5.3 холодный дамп: в базу больше никто не пишет, это и есть точка переноса
./scripts/backup.sh
ls -lht backups/ | head -3   # верхний файл — наш
```

Почему именно так: `bot` и `worker` пишут постоянно (парсинг, `sent_notifications`,
статистика). Если дампить на живых сервисах, всё записанное после дампа
потеряется — а `sent_notifications` как раз защищает от повторной отправки
одного объявления. Расхождение = люди получат дубли.

**Бота останавливаем здесь, а не позже, ещё по одной причине:** один токен —
один `getUpdates`. Пока старый бот жив, новый упадёт с `TelegramConflictError`.

## 6. Перенести дамп на новый сервер

```bash
# у себя: забрать холодный дамп в слепок
SNAP=migration/2026-08-08
scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/backups/car_analytics_*.sql.gz" $SNAP/db/
cd $SNAP/db && ls -t | head -1        # запомнить имя свежего файла
gzip -t $(ls -t | head -1) && echo "дамп целый"

# отправить на новый сервер
scp -i "$KEY_NEW" $(ls -t | head -1) "$SRV_NEW:/home/ubuntu/car-analytics/backups/"
```

Быстрее — напрямую между серверами, если у тебя один и тот же ключ имеет доступ
к обоим; иначе гоняем через себя, как выше.

## 7. Restore: только postgres, потом сверка

Порядок критичен: `api`/`worker`/`bot` при старте делают `init-db` + `seed` и
заполнят пустую базу до восстановления.

```bash
ssh -i "$KEY_NEW" "$SRV_NEW"
cd /home/ubuntu/car-analytics

docker compose up -d postgres
docker compose ps                       # ждём healthy

./scripts/restore.sh backups/car_analytics_<дата>.sql.gz
```

Сверить с числами из `migration/<дата>/system/server-snapshot.txt` — они должны
совпасть (кроме `sent_notifications`, которое могло подрасти между слепком и
холодным дампом; расти оно может только вверх):

```bash
docker compose exec -T postgres psql -U car_user -d car_analytics -c "
SELECT 'listings' t, count(*) FROM listings
UNION ALL SELECT 'bot_users', count(*) FROM bot_users
UNION ALL SELECT 'user_filters', count(*) FROM user_filters
UNION ALL SELECT 'sent_notifications', count(*) FROM sent_notifications
UNION ALL SELECT 'daily_market_stats', count(*) FROM daily_market_stats ORDER BY 1;"
```

Эталон на 2026-08-08: `listings` 91 343, `daily_market_stats` 575 745,
`sent_notifications` 55 720, `bot_users` 15, `user_filters` 31, `payments` 0.

**Если числа не совпали — остановиться здесь.** Не поднимать бота, разобраться:
не тот файл дампа, restore упал на середине, или дамп снят до остановки сервисов.

## 8. Поднять всё на новом сервере

```bash
docker compose up -d                    # образ уже собран в шаге 4
docker compose ps                       # 4 сервиса Up, порты 127.0.0.1:...
```

Если переносишь вместе с оплатой (ветка `dev`) — здесь же выполнить ручной DDL
по `payments` из `DEPLOY.md § Подписка/Finik, шаг 6` и проверить `.env`:
`TRIAL_DAYS` (в коде дефолт 3 — впиши явно), `SUBSCRIPTION_TESTER_IDS`,
`BOT_USERNAME`, `PUBLIC_BASE_URL`, `PAYMENTS_DOMAIN`. В слепке `.env` этих
переменных нет вообще — прод их не знал.

## 9. Проверить

```bash
curl http://127.0.0.1:8000/health                     # {"status":"ok"}
docker compose logs --tail=30 bot                     # "bot poller started"
docker compose logs --tail=20 worker
docker compose exec -T worker uv run python -m app.cli funnel --days 7
free -h && df -h /                                    # память и диск после старта
```
В самом Telegram: `/start`, `/filters`, `/status` — и дождаться первого цикла
уведомлений (до 10 минут в окне 8:00–24:00).

Сравнить логи с эталоном из слепка (`migration/<дата>/logs/docker-bot.log`):
должны быть те же строки `bot fresh sync … new=…` по каждой марке и
`notifications: …`. Первый цикл на новом сервере — момент проверки прокси
lalafo: если в логе `403` или `Just a moment`, значит `LALAFO_PROXY_URL` не
доехал в `.env`.

## 10. Домен и webhook (только если оплата уже поднята)

1. A-запись `PAYMENTS_DOMAIN` → **новый static IP**, дождаться обновления DNS.
2. Открыть 80/443 в firewall Lightsail (нужны Let's Encrypt).
3. `docker compose --profile payments up -d` и проверить:
   `curl -X POST https://<домен>/api/v1/payments/finik` → 400/401,
   любой другой путь наружу → 404.
4. В кабинете Finik webhook привязан к домену, не к IP, — менять там ничего не
   нужно, если домен тот же.

## 11. Бэкапы на новом сервере

```bash
crontab -e
# 23:00 UTC = 05:00 по Бишкеку — самое тихое окно
0 23 * * * cd /home/ubuntu/car-analytics && ./scripts/backup.sh >> logs/backup.log 2>&1
```

И забирать копии к себе (бэкап на том же VPS не спасёт от потери VPS):

```bash
rsync -av -e "ssh -i $KEY_NEW" "$SRV_NEW:/home/ubuntu/car-analytics/backups/" ./backups/
```

## 12. Откат

Старый сервер не выключать и не удалять минимум сутки. Если новый не поехал:

```bash
ssh -i "$KEY_NEW" "$SRV_NEW" 'cd /home/ubuntu/car-analytics && docker compose stop bot worker'
ssh -i "$KEY_OLD" "$SRV_OLD" 'cd /home/ubuntu/car-analytics && docker compose start bot worker'
```

Потеряются только уведомления, накопленные за время простоя нового бота: они
всплывут снова в пределах `BOT_NEW_LOOKBACK_MINUTES` (180 мин). Дубликатов не
будет — дедуп по паре «юзер + объявление» в `sent_notifications`.

Откат безопасен, пока на старом сервере жива база: он продолжает с того же
холодного дампа, ничего не потеряв. Но с момента, как **новый** бот отработал
хотя бы один цикл, откат означает потерю того, что новый успел записать
(`sent_notifications` разойдётся → часть людей получит объявление повторно).
Поэтому решение об откате принимаем в первые минуты, а не через день.

Когда новый сервер отработал сутки чисто — на старом
`docker compose down` (volume с базой оставить, пока не убедишься окончательно).

---

## Грабли (проверено на практике)

- **База не переносится rsync-ом.** Она в docker-volume, а не в каталоге
  проекта. Только дамп.
- **Дамп для переезда — холодный.** Снимать ПОСЛЕ `docker compose stop bot
  worker api`. Дамп с работающих сервисов годится как страховка, но всё
  записанное после него потеряется, а расхождение `sent_notifications` = дубли
  уведомлений у людей.
- **Секреты живут только в `migration/<дата>/`** (каталог в `.gitignore`).
  Внутри боевой токен бота, пароль Postgres и креды прокси — не отправлять
  мессенджерами, не класть в облако.
- **`.env` брать со старого сервера.** Локальный `.env` расходится намеренно:
  на проде живёт `LALAFO_PROXY_URL` (DataImpulse), без него lalafo с
  датацентрового IP отдаёт 403.
- **Два бота с одним токеном не живут** — сначала стоп старого, потом старт
  нового.
- **Сначала только postgres, потом restore.** Иначе `init-db`/`seed` из
  api/worker/bot заполнят базу и restore ляжет поверх мусора.
- **`docker compose up -d --build` одной командой на 2 ГБ RAM рискован** —
  сборка рядом с работающим postgres может уйти в OOM. Сначала `build`, потом
  `up`. При зависании смотреть `dmesg | grep -i oom`.
- **`restore.sh` до правки 08.08.2026 падал с кодом 127** на боевом `.env`:
  `source .env` спотыкался о `;` в логине прокси. Если запускаешь скрипт из
  старой копии кода — возьми свежий.
- **Static IP прикрепить сразу.** Без него stop/start сменит адрес и сломает
  DNS/webhook.
- **TZ контейнеров (UTC) не трогать** — расписание считается по
  `SCHEDULER_TIMEZONE=Asia/Bishkek` в коде.

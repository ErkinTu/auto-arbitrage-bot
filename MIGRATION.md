# MIGRATION.md — перенос проекта на свой сервер

Короткий runbook: боевой бот живёт на **чужом** AWS-аккаунте (сервер друга),
переносим на **свой**. Полное описание деплоя и бэкапов — `DEPLOY.md`, здесь
только последовательность переезда и его специфика.

**Переезд совмещён с деплоем ветки `dev`** (решено 08.08.2026): вместе с новым
сервером в прод уезжают триал, рефералка, чёрный список и лимит 5 фильтров; код
Finik уезжает тоже, но оплата остаётся выключенной (`FINIK_*` пусты). Шаги 1 и 2
уже выполнены — сервер создан и настроен, дальше начинается работа с кодом и базой.

Обозначения (боевые значения, не плейсхолдеры):

```bash
KEY_OLD=~/Desktop/aws/LightsailDefaultKey-eu-central-1.pem
SRV_OLD=ubuntu@63.180.133.249                        # сервер друга, пока БОЕВОЙ
KEY_NEW=~/Desktop/aws/car-analytics-prod-key.pem
SRV_NEW=ubuntu@63.182.28.250                         # свой аккаунт, готов к заливке
```

Новый сервер — аккаунт AWS `279341437912`, инстанс `car-analytics-prod`
(Lightsail $12/мес: 2 ГБ RAM, 2 vCPU, 60 ГБ SSD), static IP
`car-analytics-prod-ip` = **63.182.28.250**, регион eu-central-1a (Франкфурт,
как у старого — чтобы не менять IP-репутацию у mashina/lalafo заодно с переездом).

**Приватный ключ `car-analytics-prod-key.pem` скачивается у AWS ровно один раз.**
Копии на стороне Amazon нет: потеряешь файл — доступ по SSH только через
браузерный Lightsail-терминал и заведение нового ключа.

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

1. **`env/env.prod`** → станет `.env` на новом сервере. **Дополнен 08.08.2026**
   блоком переменных под ветку `dev` (`TRIAL_DAYS=3`, `BOT_USERNAME=sk_hunterbot`,
   `SUBSCRIPTION_TESTER_IDS=1184591481`, `BOT_MAX_FILTERS_PER_USER=5`,
   `REFERRAL_*`, пустые `FINIK_*`/`PUBLIC_BASE_URL`/`PAYMENTS_DOMAIN`) — старый
   прод их не знал. Версия до правки лежит рядом:
   `env/env.prod.bak-before-dev-vars`. Пустые `FINIK_API_KEY`/`FINIK_ACCOUNT_ID`/
   `PUBLIC_BASE_URL` — это осознанное состояние: `subscription_enabled=false`,
   `/subscribe` недоступен никому, доступ гейтится группой и триалом.
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

## 1. Создать инстанс на своём аккаунте — ✅ СДЕЛАНО 08.08.2026

Что заведено (повторять не нужно, оставлено для истории и на случай пересоздания):

- **Lightsail $12/мес: 2 ГБ RAM / 2 vCPU / 60 ГБ SSD / 3 ТБ трафика**, dual-stack
  (IPv4+IPv6). Это ровно конфигурация старого сервера; 1 ГБ мало (стек уже
  уходит в swap), 20 ГБ диска мало (образы + build cache). Диск в Lightsail
  отдельно не выбирается — 2 ГБ RAM идут в комплекте с 60 ГБ.
- **Ubuntu 24.04 LTS**, blueprint «Linux OS only», eu-central-1a.
- **Static IP прикреплён** (`car-analytics-prod-ip` = 63.182.28.250) — без него
  stop/start сменил бы адрес и утащил за собой DNS и webhook Finik. Пока IP
  привязан к работающему инстансу, он бесплатен.
- **Firewall: только SSH 22.** Дефолтное правило HTTP 80 удалено — на 80 ничего
  не слушает, откроем 80/443 вместе с Caddy, когда будем поднимать оплату.
  Порты API и БД наружу не публикуются, они на `127.0.0.1`.
- IPv6 у инстанса есть, но dual-stack выбран сознательно: IPv6-only дешевле на
  $2/мес, зато Let's Encrypt, Finik и docker-registry по IPv6-only — отдельная
  морока за экономию $24/год.

### Деньги и два дедлайна (проверено в консоли 08.08.2026)

Аккаунт на **AWS Free Plan**, кредитов **$159.98**: $100 «AWS Free Tier» +
3 × $20 «Explore AWS». **Amazon Lightsail есть в списке applicable products** —
кредиты действительно оплачивают этот сервер.

| Дата | Что происходит |
|---|---|
| **03.02.2027** | кончается free plan. Без перехода на **Paid Plan** аккаунт закрывается ВМЕСТЕ С ДАННЫМИ. Апгрейд требует карты, кредиты при нём не теряются. Делать в январе 2027, не в последнюю неделю. |
| **03.08.2027** | истекают сами кредиты. $12 × 12 ≈ $144 из $160 → примерно год бесплатно, дальше $12/мес живыми деньгами. |

Бюджет-алерты уже стоят (оба на `erkint3120@gmail.com`): `zero-spend-alert`
($1, факт > $0.01 — выстрелит в первый день, когда кредиты кончатся) и
`car-analytics-prod-15usd` ($15/мес, факт > 80% и прогноз > 100%). Бесплатных
бюджетов у AWS два, третий стоил бы ~$0.60/мес.

Мелочь на будущее: настройки «IAM user and role access to Billing information»
в аккаунте больше НЕ существует (AWS перевёл доступ к биллингу на обычные
IAM-права), искать её не надо. У `admin-user` есть `AdministratorAccess`, чего
достаточно; апгрейд плана и страница Credits — под root-логином.

## 2. Подготовить сервер — ✅ СДЕЛАНО 08.08.2026

Итоговое состояние (проверено ПОСЛЕ перезагрузки, а не сразу после установки):
Ubuntu 24.04.4, ядро 6.17.0-1019-aws, hostname `car-analytics-prod`,
Docker **29.7.2** + Compose **v5.4.0**, swap 2 ГБ, `vm.swappiness=10`,
`unattended-upgrades` активен, TZ `Etc/UTC`, NTP синхронизирован, диск 4.6/58 ГБ.

Порядок, если сервер придётся поднимать заново:

```bash
# 0. Пакеты и ребут — ДО всего остального: пока сервер пустой, это бесплатно,
#    после переезда пришлось бы ловить окно простоя под новое ядро.
ssh -i "$KEY_NEW" "$SRV_NEW" 'sudo DEBIAN_FRONTEND=noninteractive apt-get update -qq \
  && sudo DEBIAN_FRONTEND=noninteractive apt-get -y -o Dpkg::Options::="--force-confold" upgrade'
ssh -i "$KEY_NEW" "$SRV_NEW" 'test -f /var/run/reboot-required && sudo systemctl reboot'

# 1. swap 2 ГБ: 1.9 ГБ RAM для 4 контейнеров впритык, на старом сервере swap занят
ssh -i "$KEY_NEW" "$SRV_NEW" 'sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile \
  && sudo mkswap /swapfile && sudo swapon /swapfile \
  && echo "/swapfile none swap sw 0 0" | sudo tee -a /etc/fstab'

# 2. swappiness 10 вместо дефолтных 60: на машине с postgres дефолт гонит в свап
#    то, что полезнее держать в page cache
ssh -i "$KEY_NEW" "$SRV_NEW" 'echo "vm.swappiness=10" | sudo tee /etc/sysctl.d/99-swappiness.conf \
  && sudo sysctl -q -w vm.swappiness=10'

# 3. Docker
ssh -i "$KEY_NEW" "$SRV_NEW" 'curl -fsSL https://get.docker.com | sh \
  && sudo usermod -aG docker ubuntu && sudo systemctl enable --now docker'
# НОВАЯ сессия — группа docker применяется только при логине:
ssh -i "$KEY_NEW" "$SRV_NEW" 'docker ps >/dev/null && echo "docker без sudo OK"'

# 4. Ротация json-логов контейнеров: без неё они растут неограниченно, а диск
#    здесь один под образы, базу и бэкапы
ssh -i "$KEY_NEW" "$SRV_NEW" 'printf "{\n  \"log-driver\": \"json-file\",\n  \"log-opts\": { \"max-size\": \"10m\", \"max-file\": \"5\" }\n}\n" \
  | sudo tee /etc/docker/daemon.json && sudo systemctl restart docker'

# 5. Имя хоста — чтобы в логах и в prompt было видно, где ты
ssh -i "$KEY_NEW" "$SRV_NEW" 'sudo hostnamectl set-hostname car-analytics-prod'

# 6. NTP обязателен (подпись Finik живёт ~10 с). TZ контейнеров НЕ трогать.
ssh -i "$KEY_NEW" "$SRV_NEW" 'timedatectl | grep -E "synchronized|Time zone"'

ssh -i "$KEY_NEW" "$SRV_NEW" 'mkdir -p /home/ubuntu/car-analytics/{backups,secrets,logs} \
  && chmod 700 /home/ubuntu/car-analytics/secrets'
```

Проверка, что всё переживает перезагрузку (иначе опечатка в `fstab` вылезет уже
на проде):

```bash
ssh -i "$KEY_NEW" "$SRV_NEW" 'sudo systemctl reboot'; sleep 30
ssh -i "$KEY_NEW" "$SRV_NEW" 'swapon --show; cat /proc/sys/vm/swappiness; docker ps'
```

## 3. Залить код, `.env` и ключи (без простоя, заранее)

Всё, кроме базы, переносится до окна простоя — старый сервер продолжает работать.

> **РЕШЕНО 08.08.2026: переезжаем сразу с веткой `dev`** — триал, рефералка,
> чёрный список, лимит 5 фильтров, код Finik (сама оплата спит). `rsync` заливает
> ТЕКУЩУЮ локальную ветку, git ему безразличен, поэтому перед заливкой:
>
> ```bash
> git branch --show-current    # должно быть dev
> git status --short           # рабочее дерево чистое
> ```
>
> Из этого следуют два обязательных шага, которых нет при простом переезде:
> **переменные `.env`** (сделано, см. ниже) и **DDL по таблице `payments`**
> (шаг 7.1). Что новый код сделает с существующими юзерами — раздел
> «Новый код и существующие юзеры» перед шагом 8.

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

## 7.1. DDL под ветку `dev`: убрать таблицу `payments` от FreedomPay

Делать **после restore и ДО `docker compose up -d`**. В восстановленной базе лежит
таблица `payments` от отменённого FreedomPay: в ней колонка
`freedompay_payment_id`, а код Finik ждёт `finik_transaction_id`. `create_all`
существующие таблицы не меняет, а ALTER-ов для `payments` в
`app/db/session.py::_upgrade_postgres_schema` нет — то есть таблица молча
останется несовместимой, и первая же попытка оплаты упадёт.

Строк там **0** (проверено на проде 08.08.2026), поэтому просто дропаем — новую
схему `create_all` создаст сам при старте:

```bash
docker compose exec -T postgres psql -U car_user -d car_analytics -c "
SELECT count(*) FROM payments;"            # ОБЯЗАТЕЛЬНО 0, иначе не дропать
docker compose exec -T postgres psql -U car_user -d car_analytics -c "
DROP TABLE IF EXISTS payments;"
```

Если строк вдруг НЕ 0 — не дропать, а переименовать колонку:
`ALTER TABLE payments RENAME COLUMN freedompay_payment_id TO finik_transaction_id;`

Та же процедура описана в `DEPLOY.md § Подписка/Finik` — здесь она вынесена в
отдельный шаг только потому, что в переезде у неё жёсткое место в порядке:
после restore, до первого `up`.

Остальные новые сущности ручного DDL не требуют: колонки `trial_*`, `invite_code`,
`referrer_id`, `referral_bonuses_granted` добавляются идемпотентными
`ADD COLUMN IF NOT EXISTS` при старте, а таблицы `user_events` и `user_blocks`
новые — их создаёт `create_all`.

---

## Новый код и существующие юзеры (проверено по БД, не по памяти)

Состояние боевой базы на 08.08.2026: **11 юзеров `access_status='member'`,
4 `revoked`**, подписок 0. Колонок `trial_started_at`/`trial_expires_at` в проде
нет — `init_db` добавит их как nullable, то есть **NULL у всех 15**.

| Кто | Что с ними будет |
|---|---|
| 11 членов группы (`member`) | Ничего не меняется: `matching.py` пускает `access_status='member'` без всяких дат — бесплатно и бессрочно. На `/start` получат обычный текст, кнопки триала не увидят (`handlers.py`, первая ветка). |
| 4 `revoked` | Не теряют ничего — пушей у них нет и сейчас. Появляется путь: `/start` → онбординг → кнопка «3 дня бесплатно». |
| Триал | **С нуля у каждого, но только по кнопке.** `trial_started_at` NULL → один раз на `telegram_id`; автоматически не выдаётся никому. |

Два следствия, важных для нервов при деплое:

- **Доступ в момент деплоя не теряет никто.** Статусы уже посчитаны в БД, а
  пересчёт (`resolve_access`) происходит только когда юзер сам пишет боту.
- **Рассылок после деплоя не будет.** `trial.paywall_candidates` требует
  `trial_expires_at IS NOT NULL` → кандидатов 0, ни одного сообщения о конце
  триала не уйдёт.

## 8. Поднять всё на новом сервере

```bash
docker compose up -d                    # образ уже собран в шаге 4
docker compose ps                       # 4 сервиса Up, порты 127.0.0.1:...
```

DDL по `payments` к этому моменту уже сделан (шаг 7.1), переменные `.env` уже
лежат в `env.prod` — на этом шаге ничего досыпать не нужно. Проверить, что
контейнеры их увидели:

```bash
docker compose exec -T bot printenv | grep -E "^(TRIAL_DAYS|BOT_USERNAME|BOT_MAX_FILTERS_PER_USER|SUBSCRIPTION_TESTER_IDS)="
docker compose logs bot | grep -iE "trial|referral|subscription" | head
```

Оплату (`--profile payments`, Caddy, домен, ключ Finik) на этом шаге НЕ поднимаем:
`FINIK_*` и `PUBLIC_BASE_URL` пусты специально, `subscription_enabled=false`.
Включение оплаты — отдельная задача после переезда, чек-лист в
`DEPLOY.md § Подписка/Finik` и шаг 10 ниже.

## 9. Проверить

```bash
curl http://127.0.0.1:8000/health                     # {"status":"ok"}
docker compose logs --tail=30 bot                     # "bot poller started"
docker compose logs --tail=20 worker
docker compose exec -T worker uv run python -m app.cli funnel --days 7
free -h && df -h /                                    # память и диск после старта
```
Проверки, специфичные для нового кода (ветка `dev`):

```bash
# 1. Колонки триала/рефералки доехали, таблицы новые созданы
docker compose exec -T postgres psql -U car_user -d car_analytics -c "
SELECT column_name FROM information_schema.columns
WHERE table_name='bot_users' AND column_name LIKE 'trial%' OR column_name IN
('invite_code','referrer_id','referral_bonuses_granted') ORDER BY 1;" -c "\dt user_events" -c "\dt user_blocks"

# 2. payments пересоздана уже под Finik — должна быть finik_transaction_id
docker compose exec -T postgres psql -U car_user -d car_analytics -c "\d payments" | grep finik

# 3. Статусы доступа не поехали: ожидаем 11 member / 4 revoked
docker compose exec -T postgres psql -U car_user -d car_analytics -c "
SELECT access_status, count(*) FROM bot_users GROUP BY 1 ORDER BY 2 DESC;"

# 4. Пейвол никому не грозит: кандидатов должно быть 0
docker compose exec -T postgres psql -U car_user -d car_analytics -c "
SELECT count(*) FROM bot_users WHERE trial_expires_at IS NOT NULL
AND trial_expires_at <= now() AND trial_expired_notified_at IS NULL;"

# 5. Воронка отвечает (таблица user_events пустая — это норма, она новая)
docker compose exec -T worker uv run python -m app.cli funnel --days 7
```

В самом Telegram: `/start`, `/filters`, `/status`, `/blacklist` — и дождаться
первого цикла уведомлений (до 10 минут в окне 8:00–24:00). Под аккаунтом члена
группы кнопки триала быть НЕ должно; чтобы посмотреть онбординг с триалом, нужен
аккаунт вне группы. `/subscribe` сейчас должен отвечать отказом — оплата спит.

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
- **`rsync` заливает текущую ветку, а не «прод».** Git ему безразличен: что
  лежит в рабочем дереве, то и уедет. Проверять `git branch --show-current` и
  `git status --short` перед заливкой.
- **Таблица `payments` от FreedomPay несовместима с кодом Finik**
  (`freedompay_payment_id` vs `finik_transaction_id`), а `create_all` её не
  поправит и ALTER-а для неё в `_upgrade_postgres_schema` нет. Дропнуть при
  `count(*)=0` до старта сервисов — шаг 7.1. Иначе поломка вылезет только при
  первой оплате, то есть через недели.
- **Сначала только postgres, потом restore.** Иначе `init-db`/`seed` из
  api/worker/bot заполнят базу и restore ляжет поверх мусора.
- **`docker compose up -d --build` одной командой на 2 ГБ RAM рискован** —
  сборка рядом с работающим postgres может уйти в OOM. Сначала `build`, потом
  `up`. При зависании смотреть `dmesg | grep -i oom`.
- **`restore.sh` до правки 08.08.2026 падал с кодом 127** на боевом `.env`:
  `source .env` спотыкался о `;` в логине прокси. Если запускаешь скрипт из
  старой копии кода — возьми свежий.
- **Static IP прикрепить сразу.** Без него stop/start сменит адрес и сломает
  DNS/webhook. Привязка static IP МЕНЯЕТ публичный адрес инстанса (динамический
  заменяется на статический), поэтому делать это до любых настроек и DNS.
- **Приватный ключ Lightsail отдаётся один раз.** Скачать `.pem` нужно в момент
  создания ключа; у AWS остаётся только публичная часть. Сразу `chmod 600` —
  с правами 664 ssh откажется его использовать.
- **Группа `docker` применяется только при новом логине.** После
  `usermod -aG docker ubuntu` в ТЕКУЩЕЙ ssh-сессии `docker ps` всё ещё требует
  sudo; надо переподключиться.
- **`apt upgrade` с ребутом делать до переезда.** На пустом сервере это ничего
  не стоит, а после — потребует окна простоя ради нового ядра.
- **TZ контейнеров (UTC) не трогать** — расписание считается по
  `SCHEDULER_TIMEZONE=Asia/Bishkek` в коде.

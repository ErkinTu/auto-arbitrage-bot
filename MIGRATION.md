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
| `secrets/finik_private.pem` | отдельным `scp`, в git его нет |
| `backups/` | не переносим (тяжёлые), но свежий дамп забираем к себе |

Прокси lalafo авторизуется логином/паролем, а не по IP, — с нового IP заработает
без перевыпуска. Telegram и Finik к IP тоже не привязаны.

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

## 3. Забрать со старого сервера дамп, `.env` и ключ Finik

```bash
# свежий дамп на старом сервере (pg_dump, прод не останавливается)
ssh -i "$KEY_OLD" "$SRV_OLD" 'cd /home/ubuntu/car-analytics && ./scripts/backup.sh'

# к себе: дамп + .env + secrets
mkdir -p migration && cd migration
scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/backups/car_analytics_*.sql.gz" .
scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/.env" ./env.prod
scp -i "$KEY_OLD" "$SRV_OLD:/home/ubuntu/car-analytics/secrets/*" ./secrets/ 2>/dev/null || true

gzip -t car_analytics_*.sql.gz && echo "дамп целый"
```

## 4. Залить код и файлы на новый сервер

```bash
cd ~/Desktop/myskupka/car-analytics-mvp
rsync -av -e "ssh -i $KEY_NEW" \
  --exclude '.env' --exclude '.venv' --exclude '.git' --exclude 'backups' \
  --exclude '.idea' --exclude '.pytest_cache' --exclude '__pycache__' \
  --exclude '*.zip' --exclude '.ruff_cache' --exclude 'migration' \
  ./ "$SRV_NEW:/home/ubuntu/car-analytics/"

scp -i "$KEY_NEW" migration/env.prod "$SRV_NEW:/home/ubuntu/car-analytics/.env"
scp -i "$KEY_NEW" migration/car_analytics_*.sql.gz "$SRV_NEW:/home/ubuntu/car-analytics/backups/"
scp -i "$KEY_NEW" migration/secrets/finik_private.pem "$SRV_NEW:/home/ubuntu/car-analytics/secrets/"
ssh -i "$KEY_NEW" "$SRV_NEW" 'chmod 600 /home/ubuntu/car-analytics/secrets/* 2>/dev/null; \
  chmod +x /home/ubuntu/car-analytics/scripts/*.sh'
```

## 5. Только postgres → restore → сверить

Порядок критичен: `api`/`worker`/`bot` при старте делают `init-db` + `seed` и
заполнят пустую базу до восстановления.

```bash
ssh -i "$KEY_NEW" "$SRV_NEW"
cd /home/ubuntu/car-analytics

docker compose up -d postgres
docker compose ps                       # ждём healthy

./scripts/restore.sh backups/car_analytics_<дата>.sql.gz
```

Сверить, что данные доехали (числа должны совпасть со старым сервером):

```bash
docker compose exec -T postgres psql -U car_user -d car_analytics -c "
SELECT 'listings' t, count(*) FROM listings
UNION ALL SELECT 'bot_users', count(*) FROM bot_users
UNION ALL SELECT 'user_filters', count(*) FROM user_filters
UNION ALL SELECT 'sent_notifications', count(*) FROM sent_notifications
UNION ALL SELECT 'daily_market_stats', count(*) FROM daily_market_stats;"
```

## 6. Погасить старого бота и поднять новый

**Один токен — один бот.** Пока не остановлен старый, новый получит
`TelegramConflictError`.

```bash
# на СТАРОМ сервере:
ssh -i "$KEY_OLD" "$SRV_OLD" 'cd /home/ubuntu/car-analytics && docker compose stop bot worker'

# на НОВОМ: сборка отдельно от запуска — 1.9 ГБ RAM, вместе рискуем OOM
docker compose build
docker compose up -d
docker compose ps                       # 4 сервиса Up, порты 127.0.0.1:...
```

Если переносишь вместе с оплатой (ветка `dev`) — здесь же выполнить ручной DDL
по `payments` из `DEPLOY.md § Подписка/Finik, шаг 6` и проверить `.env`:
`TRIAL_DAYS` (в коде дефолт 3 — впиши явно), `SUBSCRIPTION_TESTER_IDS`,
`BOT_USERNAME`, `PUBLIC_BASE_URL`, `PAYMENTS_DOMAIN`.

## 7. Проверить

```bash
curl http://127.0.0.1:8000/health                     # {"status":"ok"}
docker compose logs --tail=30 bot                     # "bot poller started"
docker compose logs --tail=20 worker
docker compose exec -T worker uv run python -m app.cli funnel --days 7
free -h && df -h /                                    # память и диск после старта
```
В самом Telegram: `/start`, `/filters`, `/status` — и дождаться первого цикла
уведомлений (до 10 минут в окне 8:00–24:00).

## 8. Домен и webhook (только если оплата уже поднята)

1. A-запись `PAYMENTS_DOMAIN` → **новый static IP**, дождаться обновления DNS.
2. Открыть 80/443 в firewall Lightsail (нужны Let's Encrypt).
3. `docker compose --profile payments up -d` и проверить:
   `curl -X POST https://<домен>/api/v1/payments/finik` → 400/401,
   любой другой путь наружу → 404.
4. В кабинете Finik webhook привязан к домену, не к IP, — менять там ничего не
   нужно, если домен тот же.

## 9. Бэкапы на новом сервере

```bash
crontab -e
# 23:00 UTC = 05:00 по Бишкеку — самое тихое окно
0 23 * * * cd /home/ubuntu/car-analytics && ./scripts/backup.sh >> logs/backup.log 2>&1
```

И забирать копии к себе (бэкап на том же VPS не спасёт от потери VPS):

```bash
rsync -av -e "ssh -i $KEY_NEW" "$SRV_NEW:/home/ubuntu/car-analytics/backups/" ./backups/
```

## 10. Откат

Старый сервер не выключать и не удалять минимум сутки. Если новый не поехал:

```bash
ssh -i "$KEY_NEW" "$SRV_NEW" 'cd /home/ubuntu/car-analytics && docker compose stop bot worker'
ssh -i "$KEY_OLD" "$SRV_OLD" 'cd /home/ubuntu/car-analytics && docker compose start bot worker'
```

Потеряются только уведомления, накопленные за время простоя нового бота: они
всплывут снова в пределах `BOT_NEW_LOOKBACK_MINUTES` (180 мин). Дубликатов не
будет — дедуп по паре «юзер + объявление» в `sent_notifications`.

Когда новый сервер отработал сутки чисто — на старом
`docker compose down` (volume с базой оставить, пока не убедишься окончательно).

---

## Грабли (проверено на практике)

- **База не переносится rsync-ом.** Она в docker-volume, а не в каталоге
  проекта. Только дамп.
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

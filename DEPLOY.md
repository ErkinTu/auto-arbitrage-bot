# Деплой и запуск

Всё работает внутри Docker-контейнеров, **cron не нужен** (кроме бэкапов).
Расписание встроено в сами сервисы (см. `SYNC_STRATEGY.md`):

| Сервис | Что делает |
|---|---|
| `postgres` | база данных |
| `api` | FastAPI (оценка цены, графики) на :8000 |
| `worker` | full scan по выходным (сб/вс с 2:00), пересчет статистики 9:00/17:00/01:00 |
| `bot` | Telegram-бот: fresh-парс каждые 20 мин (8:00–24:00) + уведомления |

`restart: unless-stopped` поднимает контейнеры после падения и перезагрузки
сервера (нужен включенный Docker: `systemctl enable --now docker`).

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
docker compose logs -f bot   # "bot poller started: every 20 min within 08:00-24:00"
curl http://localhost:8000/health
```

## Перенос на SSH-сервер

1. Поставить Docker (один раз):

```bash
curl -fsSL https://get.docker.com | sh
systemctl enable --now docker
```

2. Скопировать проект:

```bash
rsync -av --exclude '.venv' --exclude '.git' --exclude 'backups' \
  ./ user@server:/opt/car-analytics/
```

(лучше на будущее: приватный git-репозиторий и `git clone` на сервере)

3. Перенести базу:

```bash
# локально: дамп
./scripts/backup.sh
scp backups/car_analytics_*.sql.gz user@server:/opt/car-analytics/backups/

# на сервере: сначала только postgres, затем restore, затем всё остальное
cd /opt/car-analytics
docker compose up -d postgres        # дождаться healthy: docker compose ps
./scripts/restore.sh backups/car_analytics_<дата>.sql.gz
docker compose up -d --build
```

## Обновление кода на сервере

```bash
rsync ... (или git pull)
docker compose up -d --build
```

## Бэкапы (единственный cron)

```bash
mkdir -p logs
crontab -e
# добавить:
0 3 * * * cd /opt/car-analytics && ./scripts/backup.sh >> logs/backup.log 2>&1
```

Периодически забирайте копии с сервера (`scp` к себе): бэкап на том же
VPS не защищает от потери самого VPS.

## Безопасность

- Часовой пояс контейнеров (UTC) не трогать — код считает время сам по
  `SCHEDULER_TIMEZONE=Asia/Bishkek`.
- Порт 8000 наружу не открывать без необходимости: либо файрвол
  (`ufw allow ssh && ufw enable`), либо nginx/caddy с HTTPS перед API.
- `.env` не коммитить (уже в `.gitignore`).

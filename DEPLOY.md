# Деплой и запуск

Всё работает внутри Docker-контейнеров, **cron не нужен** (кроме бэкапов).
Расписание встроено в сами сервисы (см. `SYNC_STRATEGY.md`):

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

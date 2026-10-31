import logging
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    database_url: str = "postgresql+psycopg://car_user:car_password@localhost:5432/car_analytics"
    # Postgres сам рвёт сессию, простоявшую с открытой транзакцией дольше N
    # секунд (страховка от «забытой» транзакции, которая держит DDL при выкатке
    # и весь трафик за ним — 21.10.2026). Не 60, а 300: парсеры между запросами
    # к БД ходят в HTTP с ретраями (до ~3 мин при лежащем прокси), и короткий
    # таймаут ронял бы их прогоны. 0 = выключено.
    db_idle_in_transaction_timeout_seconds: int = 300

    mashina_api_url: str = "https://mashina.kg/api/mbank-proxy/v1/ads/listings"
    lalafo_base_url: str = "https://lalafo.kg"
    # Прокси ТОЛЬКО для lalafo-запросов: Cloudflare lalafo блокирует
    # датацентровые IP (AWS -> 403), нужен резидентный выход. Пусто = напрямую.
    # Форматы: http://user:pass@host:port или socks5://127.0.0.1:1080.
    lalafo_proxy_url: str = ""
    nbkr_rates_url: str = "https://www.nbkr.kg/XML/daily.xml"
    request_timeout_seconds: float = 20.0
    request_delay_seconds: float = 2.0
    request_retry_attempts: int = 5
    request_retry_backoff_seconds: float = 10.0
    user_agent: str = "CarAnalyticsMVP/0.1 (contact: your-email@example.com)"

    allowed_brands: str = "BMW,Audi,Toyota,Kia"
    missing_full_scans_before_deactivation: int = 2
    default_page_size: int = 100
    default_max_pages: int = 5
    default_max_items: int = 500
    default_sync_interval_minutes: int = 60

    api_cache_ttl_seconds: int = 300

    cors_origins: str = "http://localhost:5173,http://localhost:5174,http://localhost:5175"

    telegram_bot_token: str = ""
    telegram_access_chat_id: str = ""  # id закрытой группы/канала вида -100...; бот должен быть админом
    bot_fresh_interval_minutes: int = 10
    bot_fresh_period: str = "1d"
    bot_fresh_page_size: int = 20  # объявлений на марку за цикл (1 страница)
    # lalafo: авто больше, чем на mashina; API отдаёт максимум 50 на страницу
    # (при большем per-page молча ужимает до 50) + VIP-поднятые сверху.
    lalafo_fresh_page_size: int = 50
    # Раздельные паузы lalafo: лента марки — тяжелый запрос (3с; до 31.10.2026
    # было 5с — 2.2 плана PUSH_PIPELINE_PLAN, откат этой же переменной),
    # карточка — лёгкий GET. При 429 парсер сам удваивает detail-паузу до конца цикла.
    lalafo_feed_delay_seconds: float = 3.0
    lalafo_detail_delay_seconds: float = 0.5
    # Catch-up: если ВСЯ fresh-страница оказалась новой, значит между циклами
    # был пропуск (простой) — дочитываем до N дополнительных страниц.
    bot_catchup_max_pages: int = 3
    # Окно, в котором событие new/price_changed ещё считается кандидатом на пуш.
    # Шире = больше попыток сматчиться, пока daily_market_stats догоняет цену
    # (sent_notifications дедуплицирует, повторов не будет).
    bot_new_lookback_minutes: int = 180
    # Уведомлять только о свежих по дате ПУБЛИКАЦИИ на источнике: VIP-поднятые
    # старые объявления всплывают в «свежей» ленте, но «новинками» не являются.
    bot_max_listing_age_hours: int = 12
    bot_below_avg_factor: float = 0.95
    bot_below_avg_min_samples: int = 5
    bot_access_cache_ttl_seconds: int = 600
    # Потолок фильтров на пользователя: ограничивает объём матчинга и флуд.
    # Проверяется в начале визарда (/add_filter) и защитно при создании фильтра.
    # Действует ТОЛЬКО на создание: у кого фильтров уже больше (лимит снижали
    # с 10), они продолжают работать — молча выключать чужую настройку хуже,
    # чем временно нарушенный инвариант. См. db.py::filters_quota.
    bot_max_filters_per_user: int = 5
    # Порог «ниже рынка» базового фильтра (кнопка «триал + фильтр» в онбординге,
    # пресеты «КР»/«Бишкек»). 20% — выбор 52% пользователей и самый тихий вариант;
    # перед сменой — замер объёма: `cli.py preset-volume`.
    bot_base_filter_percent: int = 20
    # Слать в рассылке только объявления с полными данными: марка, модель, год
    # и правдоподобный пробег (`matching.is_complete_listing`). Витрина и превью
    # нового фильтра требуют этого всегда — это первое, что видит человек.
    # Замер 21.10.2026 (локально, новичок с базовым фильтром): из 54 пушей у 35
    # не было пробега — на lalafo он пуст у ~43% объявлений. False — как раньше.
    bot_require_complete_listings: bool = True
    # Скидка к рынку больше этого — почти всегда не находка, а ошибка цены,
    # аренда или «цена за долю» (Lexus ES 2020 за $1800 = «−93%»). Такие не
    # шлём и не показываем нигде. 0 = без отсечки.
    bot_max_below_market_percent: float = 60.0
    # Потолок пушей одному чату за цикл — ПО ТАРИФУ (`plans.effective_tier`):
    # full = группа/триал/платный полный, delayed = платный медленный (и фолбэк).
    # Сверх лимита объявление пропускаем без записи в sent_notifications — придёт
    # в следующем цикле. Кто именно проходит в лимит, решает не порядок прихода,
    # а приоритет фильтра (`matching.filter_priority`: «ниже рынка» → марка/
    # модель → «все авто», внутри уровня — по % ниже рынка). Замер на проде
    # (2026-09-06): плоский потолок 10 упирался у 15 юзеров, у одного из них
    # «все авто» вытеснял «ниже рынка» в ноль. 0 = без ограничения.
    # Старое имя BOT_MAX_NOTIFICATIONS_PER_USER_PER_CYCLE больше не читается.
    bot_max_notifications_per_user_per_cycle_full: int = 50
    bot_max_notifications_per_user_per_cycle_delayed: int = 20
    # Разовая подборка сразу после создания фильтра — противовес точке отсчёта
    # (без неё человек сидит в тишине до первой новой машины).
    # 3, а не 10: превью — доказательство «фильтр работает», а не лента; десять
    # карточек подряд новичку читаются как спам ещё до первой настоящей рассылки.
    bot_filter_preview_limit: int = 3
    bot_filter_preview_max_age_hours: int = 24
    # Группировка: если за цикл одному человеку набирается столько и больше
    # объявлений ОДНОЙ марки, они уезжают одним сообщением-списком вместо N
    # карточек. 0 = выключено (по карточке на объявление, как сейчас).
    # Замер на проде (14 дней): обычный цикл — 5 сообщений на человека (p90 12),
    # а первый цикл после ночной паузы — в среднем 28, максимум 55. Значение 3
    # сжимает утренний залп в несколько раз; лимиты Telegram при этом те же
    # (≈30 сообщений/с суммарно, не чаще 1/с в один чат).
    # С 22.10.2026 (этап 2 плана): если сверх топа с фото набралось столько и
    # больше — всё это уходит ОДНИМ списком с разделами по маркам и кнопкой
    # «📷 Показать с фото» (по 3 карточки, кнопка переезжает вниз); меньше —
    # карточками. У самого списка нет «Не то» — она у карточек.
    # Рекомендация — 3, значение по замеру на проде.
    bot_group_min_listings: int = 0
    # Карточки с фото (план 22.10.2026): сколько лучших объявлений человеку за
    # цикл уходят фото + подписью + кнопками «Не то»/«Открыть»; остальные —
    # текстовыми карточками нового вида. Те же N — у витрины (/best, /start) и
    # превью фильтра. 0 = откат: прежний текст карточки и прежняя кнопка
    # «не показывать такие», без фото (правка .env и рестарт bot).
    bot_rich_cards_per_cycle: int = 3
    # Защита контента: sendMessage с protect_content=true убирает «Переслать»/
    # «Сохранить» и включает блокировку скриншотов там, где клиент это умеет
    # (Android). iOS/Desktop/Web скриншоты НЕ блокируют — гарантии нет, это
    # барьер, а не DRM. Whitelist — те, кому пересылка разрешена (свои id через
    # запятую); для них сообщения уходят без protect_content.
    bot_protect_content: bool = True
    bot_share_whitelist_ids: str = ""

    # --- Подписка / Finik (finik.kg, Web SDK) ---------------------------------
    # Пока finik_api_key/finik_account_id/public_base_url пусты — подписочная
    # логика спит: /subscribe отвечает «временно недоступно», доступ гейтится
    # только группой (как раньше).
    finik_api_key: str = ""
    finik_account_id: str = ""
    # Приватный RSA-ключ ФАЙЛОМ (монтируется в контейнер bot), а не значением в
    # .env: не утечёт в docker inspect/логи.
    finik_private_key_path: str = "/run/secrets/finik_private.pem"
    finik_env: str = "beta"  # beta | prod — выбирает хост и публичный ключ Finik
    # name_en: имя, которое плательщик видит на странице оплаты. Латиницей.
    finik_qr_name: str = "Car Analytics Bot"
    # Срок жизни ссылки на оплату (endDate у QR), секунды.
    finik_payment_lifetime_seconds: int = 3600
    # Публичный HTTPS-адрес (за Caddy), от которого строятся webhook/redirect URL.
    # Напр. https://pay.example.com (без хвостового /).
    public_base_url: str = ""
    # Username бота без @ — чтобы после оплаты возвращать юзера сразу в бота.
    bot_username: str = ""
    # Обкатка оплаты на проде: пока список НЕ пуст, платить может только тот, чей
    # telegram_id в нём есть — остальные получают «оплата скоро», платёж им даже
    # не заводится (иначе в payments копились бы pending, а воронка врала бы).
    # Пусто = оплата открыта всем (боевой режим).
    subscription_tester_ids: str = ""
    # Цена и срок живут не здесь, а в реестре планов (`app/services/plans.py`):
    # у плана есть ещё и скорость с текстами, а два источника правды о цене
    # дают расхождение вида «в кнопке 990, а списали 1990».
    #
    # Отставание «дешёвых» тарифов: пуш уходит, когда с квалифицирующего события
    # (`listing_status_history.observed_at`) прошло столько минут. Отсчёт от
    # события в НАШЕЙ базе, а не от даты публикации: иначе задержка зависела бы
    # от того, как быстро мы сами добрались до объявления, и первый цикл после
    # ночной паузы превратил бы «отставание» в мгновенную рассылку.
    # 0 = АВАРИЙНЫЙ ВЫКЛЮЧАТЕЛЬ: фильтрация не применяется вовсе, все платные
    # едут как полные (нужен без релиза, если отставание начнёт съедать пуши).
    delayed_tier_lag_minutes: int = 60
    # Насколько вперёд разрешено оплачивать доступ. Возвратов у нас нет (Finik —
    # разовые платежи, возврат только руками через их поддержку), а чем дальше
    # горизонт, тем вероятнее просьба вернуть деньги. Два месяца при месячном
    # максимальном плане = продлить заранее можно ровно один раз. 0 = без лимита.
    subscription_max_horizon_days: int = 60
    # За сколько дней до истечения слать напоминание о продлении.
    subscription_reminder_days_before: int = 1
    # Finik сообщает ТОЛЬКО об успешных оплатах, поэтому неоплаченные платежи
    # гасим сами: pending старше этого срока -> expired.
    payment_pending_ttl_hours: int = 6

    # --- Админ-панель ---------------------------------------------------------
    # Публичный адрес панели, напр. https://admin.example.kg (без хвостового /).
    # Пусто = вход выключен целиком: /admin в боте молчит даже для админа. Это
    # выключатель по умолчанию — забытый на чужом стенде код ничего не открывает.
    admin_panel_url: str = ""
    # Срок жизни ссылки входа. Пять минут — компромисс: ссылка живёт в истории
    # чата вечно, но за это время её успевает открыть только тот, кто её и
    # запросил. Одноразовость (used_at) страхует отдельно.
    admin_login_token_ttl_minutes: int = 5
    # Сколько живёт открытая сессия. Сутки — многовато для доступа к боевой базе
    # с телефона, который могут забыть в такси; половина рабочего дня достаточна,
    # а повторный вход стоит одного сообщения боту.
    admin_session_ttl_hours: int = 12

    # --- Триал и рефералка ----------------------------------------------------
    # Бесплатный период по кнопке в /start — выдаётся один раз на telegram_id.
    # 0 = триал выключен (кнопки нет, доступ как раньше: группа ИЛИ подписка).
    trial_days: int = 3
    # «Пригласи N друзей — получи +M дней». Друг засчитывается только после того,
    # как активировал триал: иначе накрутка сводится к созданию пустых аккаунтов.
    referral_friends_per_bonus: int = 3
    referral_bonus_days: int = 7
    # Сколько бонусов максимум может получить ОДИН пригласивший. 1 = «+7 дней
    # один раз в жизни»: телеграм-ботов middleware отсекает (`user.is_bot`), но
    # мультиаккаунтинг живым человеком не ловится ничем, а без потолка 30
    # приглашённых = 70 бесплатных дней. 0 = без ограничения.
    referral_max_bonuses: int = 1

    scheduler_timezone: str = "Asia/Bishkek"
    bot_active_start_hour: int = 8  # fresh-циклы бота только в окне [start, end)
    bot_active_end_hour: int = 24  # 24 = до полуночи
    # Ежедневное напоминание «вы на паузе, включить?» — не раньше этого часа
    # (локальное время, по умолчанию обед), одно в день, пока человек сам не
    # нажмёт /play (решение владельца 31.10.2026).
    bot_pause_reminder_hour: int = 13
    full_scan_hour: int = 2  # полная сверка по выходным не раньше этого часа
    full_scan_brand_pause_seconds: int = 60
    full_scan_page_size: int = 100
    full_scan_max_pages: int = 100
    full_scan_max_items: int = 10000
    # Пересчёт daily_market_stats (только сегодня) каждый час в окне активности
    # бота [bot_active_start_hour, bot_active_end_hour): свежие данные приходят
    # лишь пока идёт fresh-парс, ночью пересчитывать нечего (см. worker.py).
    stats_rebuild_start_hour: int = 8
    stats_rebuild_interval_hours: int = 1
    # У lalafo нет full-scan → проданное авто висело бы is_active навсегда:
    # агрегаты строятся только по недавно виденным объявлениям, а «зависшие»
    # (last_seen_at старше stale_listing_days) деактивируются worker-ом раз в сутки.
    stats_max_last_seen_days: int = 30
    stale_listing_days: int = 30
    # Отсев выбросов цены по когорте «модель+год» при построении агрегатов: в
    # когортах ≥ outlier_min_cohort исключаем цены вне [median/K, median*K],
    # K = outlier_price_median_multiple. K=4 сохраняет легитимный разброс по
    # комплектациям/пробегу, но режет мусор в десятки раз (Accord 2003 за $520k).
    # Когорты меньше порога не трогаем — там граница неосмысленна (полагаемся на
    # абсолютный предохранитель price_implausibly_high в нормализаторе).
    outlier_price_median_multiple: float = 4.0
    outlier_min_cohort: int = 5
    # Точечный валидатор lalafo: мы видим только новейшие 50 на марку, поэтому
    # живое объявление со временем уходит из ленты навсегда. Карточка отвечает
    # 200 живым (продлеваем last_seen_at) и 410 Gone удалённым (деактивация
    # раньше 30-дневного фолбэка); 200 с мёртвым status_id (8, 3) — тоже
    # деактивация. Раз в сутки, не больше max_per_run карточек, от самых давних.
    # Потолок ОБЯЗАН превышать суточный поток объявлений, доживающих до
    # 30-дневного порога (на проде 09.2026 ~2100/сутки при 24 марках): фолбэк
    # гасит только то, что валидатор уже пытался проверить, остальное копится
    # активным (worker пишет warning). 500 было мало: ~1600/сутки уходили
    # вслепую, треть — живые. 3000 карточек ≈ 80 мин через прокси, ~25 МБ трафика.
    lalafo_validate_after_days: int = 3
    lalafo_validate_max_per_run: int = 3000

    @model_validator(mode="after")
    def _warn_if_lag_eats_the_lookback_window(self) -> "Settings":
        """Отставание обязано быть заметно меньше окна кандидатов на пуш.

        Окно `bot_new_lookback_minutes` — единственный шанс объявления попасть в
        рассылку: дозревшее позже уже выпало из выборки. При lag > lookback/2 у
        отстающего почти не остаётся циклов (а часть пушей ещё и откладывает
        потолок на цикл), и он перестаёт получать что-либо — без единой ошибки
        в логах. Поэтому ругаемся на старте, а не выясняем это по жалобам.
        """
        if 0 < self.bot_new_lookback_minutes < self.delayed_tier_lag_minutes * 2:
            logger.warning(
                "DELAYED_TIER_LAG_MINUTES=%s слишком велико при "
                "BOT_NEW_LOOKBACK_MINUTES=%s: отстающие тарифы будут терять "
                "уведомления (нужно lag <= lookback/2)",
                self.delayed_tier_lag_minutes,
                self.bot_new_lookback_minutes,
            )
        return self

    @property
    def allowed_brand_list(self) -> list[str]:
        return [item.strip() for item in self.allowed_brands.split(",") if item.strip()]

    @property
    def cors_allow_origins(self) -> list[str]:
        return [item.strip() for item in self.cors_origins.split(",") if item.strip()]

    @property
    def share_whitelist_id_set(self) -> frozenset[int]:
        """Telegram-id, которым пересылка разрешена; нечисловой мусор игнорируем."""
        ids: set[int] = set()
        for item in self.bot_share_whitelist_ids.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                ids.add(int(item))
            except ValueError:
                logger.warning("BOT_SHARE_WHITELIST_IDS: не число, пропущено: %r", item)
        return frozenset(ids)

    def protect_content_for(self, chat_id: object) -> bool:
        """Нужна ли защита контента для чата: да, если включена и чат не в whitelist."""
        if not self.bot_protect_content:
            return False
        try:
            numeric_id = int(chat_id)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            # chat_id вида "@channel": по id в whitelist не попадает — защищаем.
            return True
        return numeric_id not in self.share_whitelist_id_set

    @property
    def subscription_enabled(self) -> bool:
        """Подписка активна, только когда заданы креды Finik и публичный URL.

        Приватный ключ здесь намеренно НЕ проверяется: он нужен лишь боту, а
        `api` (webhook) обходится публичным ключом Finik.
        """
        return bool(self.finik_api_key and self.finik_account_id and self.public_base_url)

    @property
    def subscription_tester_id_set(self) -> frozenset[int]:
        """Telegram-id тестировщиков оплаты; нечисловой мусор игнорируем."""
        ids: set[int] = set()
        for item in self.subscription_tester_ids.split(","):
            item = item.strip()
            if not item:
                continue
            try:
                ids.add(int(item))
            except ValueError:
                logger.warning("SUBSCRIPTION_TESTER_IDS: не число, пропущено: %r", item)
        return frozenset(ids)

    @property
    def subscription_testing(self) -> bool:
        """Режим обкатки: оплата включена, но доступна не всем."""
        return self.subscription_enabled and bool(self.subscription_tester_id_set)

    def subscription_allowed_for(self, telegram_id: int) -> bool:
        """Может ли этот пользователь начать оплату прямо сейчас."""
        if not self.subscription_enabled:
            return False
        testers = self.subscription_tester_id_set
        return not testers or telegram_id in testers

    @property
    def admin_panel_enabled(self) -> bool:
        """Панель включена, только когда известен её публичный адрес."""
        return bool(self.admin_panel_url)

    def admin_login_url(self, token: str) -> str:
        return f"{self.admin_panel_url.rstrip('/')}/login?t={token}"

    @property
    def trial_enabled(self) -> bool:
        """Триал выдаём, только если он вообще что-то даёт по сроку."""
        return self.trial_days > 0

    @property
    def referral_enabled(self) -> bool:
        """Рефералка работает поверх триала: друг засчитывается по его активации."""
        return self.trial_enabled and self.referral_bonus_days > 0 and self.referral_friends_per_bonus > 0

    @property
    def finik_base_url(self) -> str:
        if self.finik_env == "prod":
            return "https://api.acquiring.averspay.kg"
        return "https://beta.api.acquiring.averspay.kg"

    @property
    def finik_webhook_url(self) -> str:
        return f"{self.public_base_url.rstrip('/')}/api/v1/payments/finik"

    @property
    def finik_redirect_url(self) -> str:
        """Куда Finik вернёт юзера после оплаты: лучше сразу в бота."""
        if self.bot_username:
            return f"https://t.me/{self.bot_username.lstrip('@')}"
        return f"{self.public_base_url.rstrip('/')}/api/v1/payments/success"


@lru_cache
def get_settings() -> Settings:
    return Settings()

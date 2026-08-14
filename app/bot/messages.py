"""Русские HTML-шаблоны сообщений бота."""

from __future__ import annotations

import html

from app.bot.matching import PendingNotification, is_foreign
from app.core.config import get_settings
from app.services.regions import REGION_LABELS


def start_text() -> str:
    """Приветствие. Одна мысль: без фильтра бот молчит.

    Лимит берём из настройки, а не пишем числом в тексте: он уже менялся
    (10 → 5), и разъехавшееся приветствие обещало бы не то, что даёт бот.
    """
    limit = get_settings().bot_max_filters_per_user
    return (
        "👋 Привет! Я слежу за новыми объявлениями о продаже авто в Кыргызстане "
        "и присылаю те, что подходят под ваши фильтры.\n\n"
        "<b>Нет фильтров — нет объявлений.</b>\n"
        f"Создайте фильтр, их может быть до {limit}: /add_filter\n\n"
        "/filters — мои фильтры\n"
        "/help — все команды и как это работает"
    )

HELP_TEXT = (
    "ℹ️ Как это работает\n\n"
    "Вы создаёте фильтр — я каждые 10 минут проверяю новые объявления "
    "и присылаю сюда те, что вам подходят.\n\n"
    "Команды\n"
    "/add_filter — создать фильтр\n"
    "/filters — мои фильтры и удаление\n"
    "/blacklist — что не показывать\n"
    "/invite — пригласить друзей и получить дни бесплатно\n"
    "/status — мой доступ\n"
    "/cancel — отменить текущее действие\n\n"
    "Полезно знать\n"
    "• В фильтре любой шаг можно пропустить — тогда ограничения по нему не будет.\n"
    "• «Только ниже рынка» — пришлю лишь те авто, что заметно дешевле средней цены "
    "по такой же модели, году и пробегу.\n"
    "• «Регион» — область КР, «Зарубежные авто» (Китай, Корея, США) или все сразу. "
    "У зарубежных цена обычно без растаможки и доставки.\n"
    "• Надоела марка, модель или город — нажмите «🚫 Не показывать такие» под "
    "объявлением. Подействует сразу на все ваши фильтры."
)

def access_denied_text(
    *, trial_days: int, trial_available: bool, payments_open: bool
) -> str:
    """Отказ в доступе. Про закрытую группу не говорим намеренно.

    Группа — это то, как доступ ВЫДАЁТСЯ части людей, а не причина, по которой
    его нет у остальных. Человеку, пришедшему по ссылке, «вы не участник
    закрытой группы» читается как «уходите», хотя рядом лежит бесплатный
    триал, который он просто не нажал. Поэтому текст всегда называет следующий
    шаг: сначала триал, потом оплата, а пока оплата не открыта — ручное
    включение.
    """
    if trial_available:
        return (
            "🔒 Чтобы получать объявления, нужно включить доступ.\n\n"
            f"🎁 {trial_days} дня бесплатно — без карты и без оплаты. "
            "Нажмите кнопку ниже."
        )
    if payments_open:
        return (
            "🔒 Доступ закончился — новые объявления я не присылаю.\n\n"
            "Продлить: /subscribe\n"
            "Или получить дни бесплатно за приглашённых друзей: /invite"
        )
    return (
        "🔒 Доступ закончился — новые объявления я не присылаю.\n\n"
        "Оплата подписки открывается со дня на день. Напишите сюда «хочу» — "
        "включим доступ вручную.\n"
        "А пока можно получить дни за приглашённых друзей: /invite"
    )

SUBSCRIPTION_REQUIRED_TEXT = (
    "🔒 Доступ к боту — по подписке.\n"
    "Оформите её командой /subscribe, чтобы получать объявления по вашим фильтрам."
)

SUBSCRIPTION_UNAVAILABLE_TEXT = (
    "⏳ Оплата подписки временно недоступна. Попробуйте позже или напишите администратору."
)


def admin_login_text(ttl_minutes: int) -> str:
    """Сообщение со ссылкой входа в панель.

    Срок и одноразовость называем прямо: человек должен понимать, почему
    вчерашняя ссылка из истории чата не сработает, и не считать это поломкой.
    """
    return (
        "🔐 Вход в админ-панель\n\n"
        f"Ссылка действует {ttl_minutes} мин и открывается один раз.\n"
        "Прошлые ссылки больше не работают."
    )

# Оплата уже работает, но пока обкатывается на тестировщиках
# (SUBSCRIPTION_TESTER_IDS). Обещать «оплатите» человеку, которому кнопка не
# откроется, нельзя — он один раз упрётся и уйдёт.
SUBSCRIPTION_SOON_TEXT = (
    "🛠 Оплата сейчас в последней настройке — откроем со дня на день.\n"
    "Доступ пока не отключаем: напишите сюда, если он закончился, и мы продлим вручную."
)


SUBSCRIPTION_MEMBER_TEXT = (
    "✅ У вас доступ участника закрытой группы — платить не нужно, всё уже открыто."
)


def subscribe_prompt(price_kgs: int, period_days: int, *, trial_expires_at=None) -> str:
    text = (
        f"💳 Подписка на {period_days} дней — {_fmt_int(price_kgs)} сом.\n"
        "Нажмите кнопку ниже — откроется страница оплаты Finik с QR. На телефоне "
        "она сама предложит ваше банковское приложение; вводить номер карты не нужно.\n"
        "Доступ откроется автоматически сразу после оплаты."
    )
    if trial_expires_at is not None:
        # Честно предупреждаем: оплата во время триала «съедает» его остаток —
        # платные дни считаются от сегодня (`next_expiry` смотрит только на
        # subscription_expires_at, бесплатные дни лежат отдельно и не плюсуются).
        text += (
            f"\n\n🎁 Бесплатный период идёт до {trial_expires_at.strftime('%d.%m.%Y')}. "
            f"Если оплатить сейчас, {period_days} дней пойдут с сегодняшнего дня — "
            "оставшиеся бесплатные дни не приплюсуются."
        )
    return text


def subscribe_renew_prompt(price_kgs: int, period_days: int, expires_at, *, now=None) -> str:
    """Оффер продления тому, у кого подписка ещё активна.

    Отдельный текст, а не общий: человек с активной подпиской, увидев обычное
    «оформите подписку», решает, что оплата не прошла, и платит второй раз.
    Главное здесь — снять тревогу («всё в порядке») и сказать, что дни
    складываются, а не сгорают (`next_expiry` продлевает от текущего срока).
    """
    from datetime import datetime, timezone

    now = now or datetime.now(timezone.utc)
    days_left = max((expires_at - now).days, 0)
    left = "меньше суток" if days_left == 0 else f"осталось {days_left} дн."
    return (
        f"✅ <b>Подписка уже активна</b> — до {expires_at.strftime('%d.%m.%Y')} ({left}).\n\n"
        "Платить прямо сейчас не нужно. Но можно продлить заранее — "
        f"<b>{period_days} дней приплюсуются к текущему сроку</b>, ничего не сгорит.\n"
        f"Продление — {_fmt_int(price_kgs)} сом."
    )


def onboarding_text(trial_days: int) -> str:
    """Первое сообщение новичку: приветствие + как работает + оффер триала."""
    return (
        "👋 Привет! Я слежу за объявлениями о продаже авто в Кыргызстане "
        "и присылаю те, что дешевле рынка.\n\n"
        "Как это работает: вы задаёте фильтр (марка, модель, год, пробег, цена, "
        "регион), а я каждые ~10 минут проверяю свежие объявления и сравниваю цену "
        "со средней по таким же машинам. Ниже рынка — сразу пишу вам.\n\n"
        f"🎁 Первые {trial_days} дня — бесплатно, без карты."
    )


DEMO_INTRO_TEXT = "Вот пример объявления, которое я нашёл сегодня:"

DEMO_UNAVAILABLE_TEXT = (
    "Сейчас нет свежего примера под рукой — как только появится машина ниже рынка, "
    "она придёт вам первой."
)


def trial_started_text(expires_at, trial_days: int) -> str:
    return (
        f"🎁 Бесплатный период на {trial_days} дня активирован — до "
        f"{expires_at.strftime('%d.%m.%Y')}.\n\n"
        "Без фильтра объявления не приходят — создайте первый: /add_filter\n"
        "Пригласите друзей и получите ещё дни бесплатно: /invite"
    )


def trial_already_used_text(payments_open: bool = True) -> str:
    """Триал уже израсходован. `payments_open=False` — оплата ещё не открыта.

    Тот же принцип, что и в `trial_paywall_text`: звать в /subscribe, который
    отвечает «скоро», — значит закончить разговор тупиком.
    """
    if not payments_open:
        return (
            "Бесплатный период уже был использован на этом аккаунте.\n"
            "Оплата открывается со дня на день — напишите сюда «хочу», "
            "включим доступ вручную.\n"
            "А пока можно получить дни за приглашённых друзей: /invite"
        )
    return (
        "Бесплатный период уже был использован на этом аккаунте.\n"
        "Оформить подписку: /subscribe\n"
        "Или получить дни бесплатно за приглашённых друзей: /invite"
    )


def trial_paywall_text(price_kgs: int, period_days: int, payments_open: bool = True) -> str:
    """Разовое сообщение после окончания триала (уходит из worker).

    `payments_open=False` — оплата ещё обкатывается (или выключена): звать в
    /subscribe нельзя, там человека ждёт «скоро». Сообщение одно на всю жизнь
    пользователя (`trial_expired_notified_at`), поэтому потратить его на тупик —
    значит потерять человека молча.
    """
    if not payments_open:
        return (
            "⏳ Бесплатный период закончился — новые объявления я пока не присылаю.\n\n"
            "Оплата подписки открывается со дня на день. Напишите сюда «хочу» — "
            "включим доступ вручную, как только всё заработает.\n"
            "А пока можно получить дни за приглашённых друзей: /invite"
        )
    return (
        "⏳ Бесплатный период закончился — новые объявления я больше не присылаю.\n\n"
        f"Подписка — {_fmt_int(price_kgs)} сом за {period_days} дней: оплата по QR "
        "из банковского приложения, без ввода карты.\n"
        "Оформить: /subscribe\n"
        "Или получите дни бесплатно за приглашённых друзей: /invite"
    )


def invite_text(
    *,
    link: str,
    qualified: int,
    friends_to_next_bonus: int,
    bonus_days: int,
    friends_per_bonus: int,
    bonuses_granted: int,
    limit_reached: bool = False,
) -> str:
    """Экран /invite. Потолок бонусов озвучиваем сразу, а не после отказа."""
    if limit_reached:
        return (
            f"🤝 Бонус за друзей уже начислен: +{bonuses_granted * bonus_days} дней.\n\n"
            f"Ваша ссылка (друзья по-прежнему могут заходить по ней):\n{link}\n\n"
            "Бонус даётся один раз, поэтому новых дней за приглашения больше не будет.\n"
            "Продлить доступ: /subscribe"
        )
    granted = (
        f"\nУже начислено бонусов: {bonuses_granted} (+{bonuses_granted * bonus_days} дней)."
        if bonuses_granted
        else ""
    )
    return (
        f"🤝 Пригласите {friends_per_bonus} друзей — получите +{bonus_days} дней бесплатно "
        "(бонус даётся один раз).\n\n"
        f"Ваша ссылка:\n{link}\n\n"
        f"Друг засчитывается, когда перейдёт по ссылке и активирует бесплатный период.\n"
        f"Засчитано друзей: {qualified}. До бонуса: {friends_to_next_bonus}."
        f"{granted}"
    )


def referral_bonus_text(days: int) -> str:
    return (
        f"🎉 Ваши друзья активировали бота — вам начислено +{days} дней доступа.\n"
        "Статус: /status"
    )


def filters_limit_text(used: int, limit: int) -> str:
    """Отказ добавить фильтр. Разводит «занято ровно лимит» и «занято больше».

    Второе — законное наследие прежнего лимита 10: старые фильтры продолжают
    работать, но добавить новый можно, только освободив место.
    """
    if used > limit:
        return (
            f"📋 Сейчас у вас {used} фильтров, а добавлять можно до {limit}.\n"
            f"Старые продолжают работать. Чтобы создать новый, удалите "
            f"{used - limit + 1} в /filters."
        )
    return (
        f"📋 Занято {used} из {limit} фильтров.\n"
        "Удалите ненужный в /filters, чтобы добавить новый."
    )


BLOCK_MENU_TEXT = "Что именно больше не показывать?"

BLOCK_LIST_EMPTY_TEXT = (
    "🚫 Чёрный список пуст.\n\n"
    "Он работает поверх всех ваших фильтров: что сюда попало — не придёт никогда.\n"
    "Добавить проще всего кнопкой «🚫 Не показывать такие» под любым объявлением."
)


def blocks_text(blocks: list) -> str:
    lines = ["🚫 Чёрный список (не приходит ни по одному фильтру):\n"]
    for index, item in enumerate(blocks, start=1):
        lines.append(f"{index}. {item.label}")
    lines.append("\nКнопкой ниже можно вернуть любое обратно.")
    return "\n".join(lines)


def block_added_text(label: str) -> str:
    return f"Больше не покажу: {label}. Вернуть — /blacklist"


def block_limit_text(limit: int) -> str:
    return (
        f"В чёрном списке уже {limit} записей — это потолок.\n"
        "Удалите лишнее в /blacklist или сузьте сам фильтр: длинный чёрный список "
        "обычно значит, что фильтр задан слишком широко."
    )


def duplicate_filter_text() -> str:
    return (
        "🤔 Такой фильтр у вас уже есть — второй такой же ничего не добавит "
        "(объявление всё равно приходит один раз).\n"
        "Ваши фильтры: /filters"
    )


def subscribe_error_text() -> str:
    return (
        "⚠️ Не удалось создать платёж. Попробуйте ещё раз через минуту "
        "командой /subscribe."
    )


def status_text(
    *,
    access_status: str,
    expires_at,
    active: bool,
    trial_active: bool = False,
    trial_expires_at=None,
) -> str:
    if access_status == "member":
        return "✅ У вас доступ участника закрытой группы — подписка не требуется."
    if active and expires_at is not None:
        return f"✅ Подписка активна до {expires_at.strftime('%d.%m.%Y')}.\nПродлить: /subscribe"
    if trial_active and trial_expires_at is not None:
        return (
            f"🎁 Бесплатный период активен до {trial_expires_at.strftime('%d.%m.%Y')}.\n"
            "Оформить подписку заранее: /subscribe · Продлить бесплатно: /invite"
        )
    return "❌ Подписка неактивна. Оформить: /subscribe"


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def format_notification(p: PendingNotification) -> str:
    title = html.escape(p.title) if p.title else "Объявление"
    year = str(p.year) if p.year is not None else "—"
    mileage = f"{_fmt_int(p.mileage_km)} км" if p.mileage_km is not None else "—"
    price = f"${_fmt_int(round(p.price_usd))}" if p.price_usd is not None else "—"
    city = html.escape(p.city) if p.city else "—"

    below = ""
    if p.below_market_percent is not None and p.below_market_percent > 0:
        below = f" · на {p.below_market_percent:g}% ниже средней"

    avg_line = ""
    if p.market_avg_usd is not None:
        # При неизвестном пробеге среднее берётся по всей когорте модель+год
        # (все бакеты пробега) — помечаем, чтобы «ниже средней» не читалось как
        # оценка с учётом пробега.
        scope = " (по всем пробегам)" if p.mileage_km is None else ""
        avg_line = f"Средняя цена по рынку{scope}: ${_fmt_int(round(p.market_avg_usd))}\n"

    foreign_note = ""
    if is_foreign(p.city):
        foreign_note = "🌍 <b>Внимание: авто находится за рубежом</b>\n"

    return (
        "🚗 Новое объявление по вашему фильтру\n\n"
        f"<b>{title}</b>\n"
        f"Год: {year} · Пробег: {mileage}\n"
        f"Цена: <b>{price}</b>{below}\n"
        f"{avg_line}"
        f"Город: {city}\n"
        f"{foreign_note}\n"
        f'<a href="{p.url}">Открыть на {p.source_name}</a>'
    )


def _plural(count: int, one: str, few: str, many: str) -> str:
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def format_digest(brand_name: str, notes: list[PendingNotification]) -> str:
    """Несколько авто одной марки одним сообщением.

    Формат намеренно плотнее карточки: цель — чтобы утренний залп читался
    сверху вниз за один экран, а не 30 отдельными пушами. Превью ссылок у
    такого сообщения нет (Telegram показал бы одно, первое) — поэтому ссылка
    даётся текстом на каждой строке.
    """
    word = _plural(len(notes), "объявление", "объявления", "объявлений")
    lines = [f"🚗 <b>{html.escape(brand_name)}</b> — {len(notes)} {word} по вашим фильтрам\n"]
    for index, note in enumerate(notes, start=1):
        title = html.escape(note.title) if note.title else "Объявление"
        price = f"${_fmt_int(round(note.price_usd))}" if note.price_usd is not None else "—"
        details = [price]
        if note.below_market_percent is not None and note.below_market_percent > 0:
            details.append(f"на {note.below_market_percent:g}% ниже средней")
        if note.mileage_km is not None:
            details.append(f"{_fmt_int(note.mileage_km)} км")
        if note.city:
            details.append(html.escape(note.city))
        if is_foreign(note.city):
            details.append("🌍 за рубежом")
        lines.append(f'{index}. <a href="{note.url}">{title}</a>\n    {" · ".join(details)}')
    return "\n".join(lines)


def format_filter_line(
    *,
    brand_name: str | None,
    model_name: str | None,
    year_from: int | None,
    year_to: int | None,
    mileage_max_km: int | None,
    price_max_usd: float | None,
    below_market_only: bool,
    below_market_percent: int | None = None,
    skip_foreign: bool = False,
    region: str | None = None,
) -> str:
    if brand_name is None:
        parts = ["Все марки"]
    else:
        parts = [f"{brand_name} {model_name}" if model_name else f"{brand_name} (все модели)"]
    if year_from is not None or year_to is not None:
        parts.append(f"{year_from or '…'}–{year_to or '…'} г.")
    if mileage_max_km is not None:
        parts.append(f"до {_fmt_int(mileage_max_km)} км")
    if price_max_usd is not None:
        parts.append(f"до ${_fmt_int(round(price_max_usd))}")
    if below_market_only:
        if below_market_percent is not None:
            parts.append(f"ниже рынка на {below_market_percent}%+")
        else:
            parts.append("только ниже рынка")
    if region is not None and region in REGION_LABELS:
        parts.append(f"регион: {REGION_LABELS[region]}")
    elif skip_foreign:
        # Legacy-фильтры без региона, созданные до региональных фильтров.
        parts.append("без зарубежных")
    return " · ".join(parts)


def preview_intro_text(count: int) -> str:
    """Подводка к разовой подборке после создания фильтра."""
    word = "объявление" if count == 1 else ("объявления" if 2 <= count <= 4 else "объявлений")
    return (
        f"Вот {count} подходящих {word} за последние сутки — чтобы вы сразу "
        "увидели, что фильтр работает. Дальше буду присылать только новые."
    )

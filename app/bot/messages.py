"""Русские HTML-шаблоны сообщений бота."""

from __future__ import annotations

import html

from app.bot.digest import OTHER_BRANDS_LABEL
from app.bot.matching import PendingNotification, is_foreign
from app.core.config import get_settings
from app.services.access_schedule import ScheduleEntry
from app.services.filter_presets import brand_group_label
from app.services.plans import PLANS, TIER_DELAYED, Plan
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
        "/best — лучшие предложения прямо сейчас\n"
        "/help — все команды и как это работает"
    )

HELP_TEXT = (
    "ℹ️ Как это работает\n\n"
    "Вы создаёте фильтр — я каждые 10 минут проверяю новые объявления "
    "и присылаю сюда те, что вам подходят.\n\n"
    "Команды\n"
    "/add_filter — создать фильтр (готовый или свой)\n"
    "/filters — мои фильтры и удаление\n"
    "/best — лучшие предложения прямо сейчас\n"
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


def _d(value) -> str:
    """Дата для человека: 17.09.2026."""
    return value.strftime("%d.%m.%Y") if value is not None else "—"


def _speed_label(delayed: bool) -> str:
    return "с отставанием ~1 ч" if delayed else "полная скорость"


def plan_line(plan: Plan, *, bold: bool = True) -> str:
    """Строка тарифа в сетке.

    `bold=False` — для сообщений, которые уходят из worker-а: `services/telegram.py`
    шлёт БЕЗ `parse_mode` (в отличие от бота с DefaultBotProperties(HTML)), и
    теги там прилетели бы человеку текстом. Пейвол — сообщение одно на всю
    жизнь юзера, испортить его разметкой нельзя.
    """
    speed = "уведомления на ~1 ч позже" if plan.delayed else "уведомления сразу"
    price = f"{plan.period_label} — {_fmt_int(plan.price)} сом"
    if bold:
        price = f"<b>{price}</b>"
    return f"• {price} — {speed}"


# Снимает вопрос «почему ночью тихо» — иначе пауза читается как сбой.
NIGHT_NOTE = "Ночью уведомления не приходят."


def subscribe_menu_text() -> str:
    """Меню /subscribe: сетка «срок — цена — скорость», без пояснений."""
    grid = "\n".join(plan_line(plan) for plan in PLANS)
    return (
        "💳 <b>Тарифы</b>\n\n"
        f"{grid}\n\n"
        f"{NIGHT_NOTE}\n\n"
        "Выберите тариф — покажу точные даты до оплаты."
    )


def plan_confirm_text(plan: Plan, draft) -> str:
    """Экран подтверждения: цена, период и ТОЧНЫЕ даты по расписанию доступа.

    Даты обязательны: купивший полный тариф поверх отстающего иначе первый же
    тихий час прочтёт как обман, а не как «полная скорость начнётся 30.08».
    """
    current = draft.schedule_before[0] if draft.schedule_before else None
    head = (
        f"💳 <b>{plan.title.capitalize()}</b> — {_fmt_int(plan.price)} сом "
        f"за {plan.days} дней.\n\n"
    )

    if draft.starts_now:
        speed = (
            "уведомления всё это время приходят примерно на час позже"
            if plan.delayed
            else "полная скорость всё время"
        )
        body = f"Доступ до <b>{_d(draft.access_until)}</b>, {speed}."
    elif current is not None and current.delayed and not plan.delayed:
        # Апгрейд поверх отстающего: главное сказать, что доплаты разницы нет и
        # ничего не сгорает — иначе это читается как «заплатил дважды».
        body = (
            f"У вас оплачено с отставанием до {_d(current.ends_at)}. "
            "Доплаты разницы нет: "
            f"{plan.days} полных дней встанут следом — <b>полная скорость с "
            f"{_d(draft.starts_at)} по {_d(draft.access_until)}</b>. Ничего не сгорает."
        )
    elif current is not None and not current.delayed and plan.delayed:
        body = (
            f"Полная скорость сохраняется до {_d(current.ends_at)} — понижения "
            f"сейчас не будет. Дальше {plan.days} дней с отставанием, "
            f"до {_d(draft.access_until)}."
        )
    else:
        body = (
            f"Дни приплюсуются к текущему сроку: доступ до "
            f"<b>{_d(draft.access_until)}</b>, ничего не сгорает."
        )

    text = head + body
    if draft.trial_expires_at is not None:
        # Честно предупреждаем: оплата во время триала «съедает» его остаток —
        # платные дни считаются от сегодня (`next_expiry` смотрит только на
        # subscription_expires_at, бесплатные дни лежат отдельно и не плюсуются).
        text += (
            f"\n\n🎁 Бесплатный период идёт до {_d(draft.trial_expires_at)}. "
            f"Если оплатить сейчас, {plan.days} дней пойдут с сегодняшнего дня — "
            "оставшиеся бесплатные дни не приплюсуются."
        )
    text += (
        "\n\nНажмите кнопку ниже — откроется страница оплаты Finik с QR. На телефоне "
        "она сама предложит ваше банковское приложение; вводить номер карты не нужно.\n"
        "Доступ откроется автоматически сразу после оплаты."
    )
    return text


def subscribe_horizon_text(horizon_until, max_horizon_days: int) -> str:
    """Отказ по горизонту — ДО создания платежа.

    Иначе получилось бы «деньги взяли, дни не встали»: дальше горизонта доступ
    мы всё равно не сдвинем, а возвратов у нас нет.
    """
    months = max(max_horizon_days // 30, 1)
    return (
        f"✅ У вас уже оплачено до <b>{_d(horizon_until)}</b> — это максимум "
        f"на {months} мес. вперёд.\n"
        "Продлить можно ближе к сроку: напомню сам за день до окончания."
    )


def onboarding_text(trial_days: int) -> str:
    """Первое сообщение новичку: приветствие + как работает + оффер триала."""
    return (
        "👋 Привет! Я слежу за объявлениями о продаже авто в Кыргызстане "
        "и присылаю те, что дешевле рынка.\n\n"
        "Как это работает: вы задаёте фильтр (марка, модель, год, пробег, цена, "
        "регион), а я каждые ~10 минут проверяю свежие объявления и сравниваю цену "
        "со средней по таким же машинам. Ниже рынка — сразу пишу вам.\n\n"
        f"🎁 Первые {trial_days} дня — бесплатно, без карты и на полной скорости.\n"
        f"Дальше — от {_fmt_int(min(plan.price for plan in PLANS))} сом; тарифы и "
        "условия в /subscribe."
    )


START_SHOWCASE_INTRO_TEXT = "Вот что я нашёл за сутки — дешевле всего относительно рынка:"

START_TRIAL_PROMPT_TEXT = "Хотите получать такие, как только они появятся?"

DEMO_UNAVAILABLE_TEXT = (
    "Сейчас нет свежего примера под рукой — как только появится машина ниже рынка, "
    "она придёт вам первой."
)


def trial_started_text(expires_at, trial_days: int, base_filter: str | None = None) -> str:
    """Триал включён. `base_filter` — описание фильтра, поставленного той же кнопкой.

    None — фильтр не ставили (у человека уже были свои): тогда не обещаем того,
    чего не делали, и про фильтры молчим только если они есть.
    """
    head = (
        f"🎁 Бесплатный период на {trial_days} дня активирован — до "
        f"{expires_at.strftime('%d.%m.%Y')}.\n\n"
    )
    if base_filter:
        body = (
            f"✅ Поставил базовый фильтр: {base_filter}.\n"
            "Изменить: /filters, добавить свой: /add_filter\n\n"
        )
    else:
        body = "Ваши фильтры: /filters\n\n"
    return head + body + "Пригласите друзей и получите ещё дни бесплатно: /invite"


def base_filter_set_text(summary: str) -> str:
    return f"✅ Поставил базовый фильтр: {summary}.\nИзменить: /filters"


PRESETS_PROMPT_TEXT = (
    "Выберите готовый фильтр — или настройте свой по марке, году, пробегу и цене:"
)


def filter_created_text(summary: str) -> str:
    return (
        f"✅ Фильтр создан:\n{summary}\n\n"
        "Как только появится подходящее объявление — пришлю его сюда.\n"
        "/filters — мои фильтры, /add_filter — еще один."
    )


# Показывается под /filters, только когда у человека фильтры разных уровней
# приоритета (`matching.filter_priority`): иначе порядок ему ничего не меняет.
# Слово «приоритет» не используем: работает это только при переполнении
# потолка за цикл, и звучать должно успокаивающе, а не как правило с подвохом.
FILTERS_PRIORITY_NOTE = (
    "ℹ️ Если новых авто сразу много, сначала приходят те, что дешевле рынка, "
    "затем по конкретной марке, затем остальные. Ничего не теряется: что не "
    "поместилось, придёт в следующем цикле."
)


SHOWCASE_INTRO_TEXT = "🔥 Лучшие предложения за сутки — дешевле всего относительно рынка:"

SHOWCASE_EMPTY_TEXT = (
    "Сейчас нет машин заметно ниже рынка за последние сутки — загляните позже."
)


def nudge_trial_text(trial_days: int, step: int) -> str:
    """Напоминание тому, кто нажал /start, но не включил бесплатный период."""
    if step == 1:
        return (
            "Вы ещё не включили бесплатный период 🙂\n\n"
            "Одна кнопка — и я начну присылать машины по Кыргызстану, которые "
            f"дешевле рынка на {get_settings().bot_base_filter_percent}% и больше. "
            f"{trial_days} дня бесплатно, без карты."
        )
    return (
        "Пока вы думаете, машины ниже рынка уходят за часы.\n"
        f"Включите {trial_days} бесплатных дня — фильтр поставлю сам, "
        "поменять его можно в любой момент."
    )


def nudge_filter_text(step: int) -> str:
    """Напоминание тому, у кого доступ есть, а фильтра нет — бот для него молчит."""
    if step == 1:
        return (
            "У вас пока нет ни одного фильтра — поэтому я молчу.\n\n"
            "Поставить базовый: вся КР, ниже рынка на "
            f"{get_settings().bot_base_filter_percent}%? Изменить можно в /filters."
        )
    return (
        "Фильтра всё ещё нет, и новые машины проходят мимо вас.\n"
        "Одна кнопка — и базовый фильтр начнёт работать. Свой: /add_filter"
    )


def trial_summary_text(*, sent: int, below_market: int, best_percent: float | None) -> str:
    """Итоги триала за ~12 ч до конца: что человек уже получил.

    Вызывается только при sent > 0 — нулевой итог («я прислал вам 0 машин»)
    продаёт против нас, вместо него уходит `trial_summary_market_text`.
    """
    lines = [
        "⏳ Бесплатный период заканчивается примерно через 12 часов.\n",
        f"За это время я прислал вам {sent} {_plural(sent, 'машину', 'машины', 'машин')}"
        + (
            f", из них {below_market} — ниже рынка."
            if below_market
            else "."
        ),
    ]
    if best_percent:
        lines.append(f"Лучшая находка — на {_fmt_percent(best_percent)}% дешевле рынка.")
    lines.append(
        "\nЧтобы уведомления не прекратились, продлите доступ: /subscribe\n"
        f"Самый доступный вариант — {_fmt_int(min(plan.price for plan in PLANS))} сом."
    )
    return "\n".join(lines)


def trial_summary_market_text(best: list[PendingNotification]) -> str:
    """Итоги триала, когда по фильтрам ничего не пришло: показываем рынок."""
    lines = [
        "⏳ Бесплатный период заканчивается примерно через 12 часов.\n",
        "По вашим фильтрам за это время подходящих машин не нашлось — возможно, "
        "фильтр слишком узкий. Вот что есть на рынке прямо сейчас:\n",
    ]
    for note in best:
        lines.append(f"• {_digest_line(note)}")
    lines.append(
        "\nРасширить фильтр: /filters · продлить доступ: /subscribe"
    )
    return "\n".join(lines)


def burned_broadcast_text(*, matches: int | None) -> str:
    """Разовая рассылка тем, у кого триал сгорел, а оплату не начинали.

    `matches` — сколько машин ниже рынка было за неделю по фильтрам человека;
    None — фильтров нет, предлагаем базовый (кнопка в сообщении).
    """
    # Самый дешёвый вход — неделя с отставанием (300 сом): разовое «попробуйте»,
    # без скидки — о скидке владелец решает отдельно.
    cheapest = min(PLANS, key=lambda plan: plan.price)
    if matches:
        head = (
            f"🚗 За последнюю неделю по вашему фильтру было {matches} "
            f"{_plural(matches, 'машина', 'машины', 'машин')} ниже рынка — "
            "я не мог их прислать: бесплатный период закончился.\n\n"
        )
    else:
        head = (
            "🚗 Каждый день на рынке Кыргызстана появляются машины на 20% и больше "
            "дешевле рынка — их разбирают за часы.\n\n"
        )
    return (
        head
        + f"Попробуйте: {cheapest.title.lower()} — {_fmt_int(cheapest.price)} сом, "
        "оплата по QR из банковского приложения: /subscribe"
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


def trial_paywall_text(payments_open: bool = True) -> str:
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
    grid = "\n".join(plan_line(plan, bold=False) for plan in PLANS)
    return (
        "⏳ Бесплатный период закончился — новые объявления я больше не присылаю.\n\n"
        f"{grid}\n\n"
        "Оплата по QR из банковского приложения, без ввода карты.\n"
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


def schedule_line(entries: tuple[ScheduleEntry, ...] | list[ScheduleEntry]) -> str:
    """Расписание скорости в одну строку: «с отставанием до 30.08 → полная до 29.09»."""
    return " → ".join(
        f"{_speed_label(entry.delayed)} до {_d(entry.ends_at)}" for entry in entries
    )


def status_text(
    *,
    access_status: str,
    expires_at,
    active: bool,
    trial_active: bool = False,
    trial_expires_at=None,
    schedule: tuple[ScheduleEntry, ...] | list[ScheduleEntry] = (),
    tier: str | None = None,
) -> str:
    """Мой доступ. Подписчику показываем РАСПИСАНИЕ, а не только дату.

    Иначе купивший полный тариф не отличит его от отстающего — а отличие в
    скорости и есть то, за что он доплатил. `tier` — фолбэк для доступа без
    блоков (бонусные дни на базе, где бэкфилл не проходил): скорость назвать
    надо всё равно, иначе строка просто исчезает и выглядит как «неизвестно».
    """
    if access_status == "member":
        return "✅ У вас доступ участника закрытой группы — подписка не требуется."
    if active and expires_at is not None:
        text = f"✅ Подписка активна до {_d(expires_at)}."
        if schedule:
            text += f"\nСкорость: {schedule_line(schedule)}"
        elif tier is not None:
            text += f"\nСкорость: {_speed_label(tier == TIER_DELAYED)}"
        return text + "\nПродлить: /subscribe"
    if trial_active and trial_expires_at is not None:
        return (
            f"🎁 Бесплатный период активен до {_d(trial_expires_at)} — на полной скорости.\n"
            "Оформить подписку заранее: /subscribe · Продлить бесплатно: /invite"
        )
    return "❌ Подписка неактивна. Оформить: /subscribe"


def renewal_reminder_text(expires_at, tier: str) -> str:
    """Напоминание перед истечением: что именно истекает и сколько стоит продлить.

    Обе цены месячных планов рядом: человек продлевает не «подписку вообще», а
    конкретную скорость, и должен видеть, что дешевле есть.
    """
    expiring = _speed_label(tier == TIER_DELAYED)
    prices = " или ".join(
        f"{_fmt_int(plan.price)} сом ({_speed_label(plan.delayed)})"
        for plan in PLANS
        if plan.days >= 30
    )
    return (
        f"⏳ Ваш доступ ({expiring}) истекает {_d(expires_at)}.\n"
        f"Продление на месяц — {prices}.\n"
        "Продлить: /subscribe — чтобы не пропускать выгодные объявления."
    )


def paid_text(plan: Plan, expires_at, full_speed_from=None) -> str:
    """Сообщение после оплаты (шлёт webhook): тариф, срок и дата полной скорости."""
    text = (
        f"✅ Оплата получена: <b>{plan.title}</b>.\n"
        f"Доступ активен до {_d(expires_at)}."
    )
    if full_speed_from is not None:
        # Полная скорость начинается не сегодня (купили поверх отстающего) —
        # молчать об этом нельзя, иначе тишина читается как «оплата не сработала».
        text += f"\nПолная скорость — с {_d(full_speed_from)}."
    return text + "\nДоступ открыт — /add_filter"


def _fmt_int(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def card_title(p: PendingNotification) -> str:
    """«Toyota Camry · 2018»: марка и модель из каталога, иначе заголовок источника.

    Заголовок lalafo — «Toyota Camry: 2016 г., 2.5 л, …», от него берём часть до
    двоеточия. Поколение не добавляем: у mashina в нём спеки двигателя
    («V (XA50) 2.5hyb AT 4WD»), в первой строке пуша это шум.
    """
    if p.brand_name and p.model_name:
        name = f"{p.brand_name} {p.model_name}"
    else:
        name = (p.title or "").split(":", 1)[0].strip() or "Объявление"
    return f"{name} · {p.year}" if p.year is not None else name


def _round_hundred(value: float) -> int:
    return int(round(value / 100.0)) * 100


def format_notification(p: PendingNotification) -> str:
    """Карточка объявления — подпись к фото или отдельное сообщение.

    Название, цена и скидка — в первых строках: только они попадают в
    push-превью, поэтому служебной фразы «Новое объявление…» больше нет.
    Строка без значения не выводится вовсе (никаких «—»). «Похожие» — та же
    модель, год и диапазон пробега (так считает `market_estimate`); при
    неизвестном пробеге — все пробеги, и это сказано прямо.
    """
    lines = [f"<b>{html.escape(card_title(p))}</b>"]

    if p.price_usd is not None:
        price_line = f"💰 ${_fmt_int(round(p.price_usd))}"
        # Целые проценты: десятые в push-превью — шум («на 14.9%» → «на 15%»).
        percent = round(p.below_market_percent) if p.below_market_percent is not None else 0
        if percent > 0:
            price_line += f" · на {percent}% ниже рынка"
            if p.market_avg_usd is not None:
                saving = _round_hundred(p.market_avg_usd - p.price_usd)
                if saving > 0:
                    price_line += f" (выгода ≈ ${_fmt_int(saving)})"
        lines.append(price_line)

    if p.market_avg_usd is not None:
        market_line = f"Рынок: ${_fmt_int(_round_hundred(p.market_avg_usd))}"
        if p.market_samples:
            similar = _plural(p.market_samples, "похожей машине", "похожим машинам", "похожим машинам")
            market_line += f" — по {p.market_samples} {similar}"
        if p.mileage_km is None:
            market_line += " (пробег не указан — по всем пробегам)"
        lines.append(market_line)

    details = []
    if p.mileage_km is not None:
        details.append(f"{_fmt_int(p.mileage_km)} км")
    if p.city:
        details.append(html.escape(p.city))
    if details:
        lines.append(" · ".join(details))

    if is_foreign(p.city):
        lines.append("🌍 <b>Внимание: авто находится за рубежом</b>")

    lines.append(f'<a href="{p.url}">Открыть на {html.escape(p.source_name)}</a>')
    return "\n".join(lines)


def format_notification_legacy(p: PendingNotification) -> str:
    """Карточка до 22.10.2026 — вид при `BOT_RICH_CARDS_PER_CYCLE=0` (откат флагом)."""
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


def _fmt_percent(value: float) -> str:
    return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"


def _digest_line(note: PendingNotification) -> str:
    """Одна строка про машину: ссылка, цена и % ниже рынка."""
    title = html.escape(note.title or "Объявление")
    details = []
    if note.price_usd is not None:
        details.append(f"${_fmt_int(round(note.price_usd))}")
    if note.below_market_percent:
        details.append(f"на {_fmt_percent(note.below_market_percent)}% ниже рынка")
    suffix = f" — {', '.join(details)}" if details else ""
    return f'<a href="{note.url}">{title}</a>{suffix}'


def _plural(count: int, one: str, few: str, many: str) -> str:
    if 11 <= count % 100 <= 14:
        return many
    last = count % 10
    if last == 1:
        return one
    if 2 <= last <= 4:
        return few
    return many


def _digest_item(index: int, note: PendingNotification) -> str:
    details = []
    if note.price_usd is not None:
        details.append(f"💰 ${_fmt_int(round(note.price_usd))}")
    percent = round(note.below_market_percent) if note.below_market_percent is not None else 0
    if percent > 0:
        details.append(f"на {percent}% ниже рынка")
    if note.mileage_km is not None:
        details.append(f"{_fmt_int(note.mileage_km)} км")
    if note.city:
        details.append(html.escape(note.city))
    if is_foreign(note.city):
        details.append("🌍 за рубежом")
    line = f'{index}. <a href="{note.url}">{html.escape(card_title(note))}</a>'
    if details:
        line += f"\n    {' · '.join(details)}"
    return line


def format_digest(
    notes: list[PendingNotification],
    *,
    total: int,
    start_index: int = 1,
    brand_counts: dict[str | None, int] | None = None,
) -> str:
    """Всё, что сверх топа с фото, одним списком — разделами по маркам.

    `notes` — уже упорядочены (`digest.order_digest`); список длиннее
    DIGEST_MAX_ITEMS приходит частями: `start_index` продолжает сквозную
    нумерацию, заголовок «ещё N» — только у первой части, заголовок марки —
    там, где её раздел начинается или продолжается. Строки — в виде новой
    карточки; фото — по кнопке «Показать с фото» под последней частью.
    """
    lines: list[str] = []
    if start_index == 1:
        word = _plural(total, "объявление", "объявления", "объявлений")
        lines.append(f"📋 <b>Ещё {total} {word} по вашим фильтрам</b>")
    current: object = object()
    for index, note in enumerate(notes, start=start_index):
        if note.brand_name != current:
            current = note.brand_name
            label = html.escape(note.brand_name) if note.brand_name else OTHER_BRANDS_LABEL
            count = (brand_counts or {}).get(note.brand_name)
            lines.append(f"\n🚗 <b>{label}</b>" + (f" · {count}" if count else ""))
        lines.append(_digest_item(index, note))
    return "\n".join(lines)


DIGEST_EXPIRED_TEXT = "Список устарел — свежие предложения в «🔥 Лучшее сейчас»"
DIGEST_GONE_TEXT = "Эти объявления уже сняты с продажи"
DIGEST_EMPTY_TEXT = "Больше объявлений в этом списке нет"


def format_digest_legacy(brand_name: str, notes: list[PendingNotification]) -> str:
    """Список до 22.10.2026 — вид при `BOT_RICH_CARDS_PER_CYCLE=0` (откат флагом).

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
    brand_group: str | None = None,
    max_age_years: int | None = None,
) -> str:
    if brand_name is not None:
        parts = [f"{brand_name} {model_name}" if model_name else f"{brand_name} (все модели)"]
    elif brand_group_label(brand_group):
        parts = [brand_group_label(brand_group)]
    else:
        parts = ["Все марки"]
    if year_from is not None or year_to is not None:
        parts.append(f"{year_from or '…'}–{year_to or '…'} г.")
    if max_age_years is not None:
        parts.append(f"не старше {max_age_years} лет")
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

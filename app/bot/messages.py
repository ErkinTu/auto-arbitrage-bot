"""Русские HTML-шаблоны сообщений бота."""

from __future__ import annotations

import html

from app.bot.matching import PendingNotification, is_foreign
from app.services.regions import REGION_LABELS

START_TEXT = (
    "👋 Привет! Я слежу за новыми объявлениями на mashina.kg и присылаю те, "
    "что подходят под ваши фильтры.\n\n"
    "Команды:\n"
    "/add_filter — создать фильтр (марка, модель, год, пробег, цена, регион)\n"
    "/filters — мои фильтры и их удаление\n"
    "/subscribe — оформить/продлить подписку · /status — статус подписки\n"
    "/invite — пригласить друзей и получить дни бесплатно\n"
    "/help — справка"
)

HELP_TEXT = (
    "ℹ️ Как это работает:\n\n"
    "1. Создайте фильтр через /add_filter: марка → модель → годы → пробег → цена → "
    "«только ниже рынка» → регион.\n"
    "2. Каждые ~10 минут я проверяю свежие объявления.\n"
    "3. Как только появляется машина под ваш фильтр — присылаю её сюда со ссылкой.\n\n"
    "Любой шаг фильтра можно пропустить — тогда ограничения по нему не будет.\n"
    "«Только ниже рынка» — присылать лишь авто с ценой заметно ниже средней "
    "по таким же марке/модели/году/пробегу.\n"
    "«Регион» — область/город КР, «Зарубежные авто» (Китай, Корея, США и т.п.) "
    "или «Все авто». У зарубежных цены обычно без растаможки и доставки.\n\n"
    "/filters — посмотреть и удалить фильтры."
)

ACCESS_DENIED_TEXT = (
    "⛔ Доступ только для участников закрытой группы.\n"
    "Если вы считаете, что это ошибка — свяжитесь с администратором."
)

SUBSCRIPTION_REQUIRED_TEXT = (
    "🔒 Доступ к боту — по подписке.\n"
    "Оформите её командой /subscribe, чтобы получать объявления по вашим фильтрам."
)

SUBSCRIPTION_UNAVAILABLE_TEXT = (
    "⏳ Оплата подписки временно недоступна. Попробуйте позже или напишите администратору."
)


def subscribe_prompt(price_kgs: int, period_days: int) -> str:
    return (
        f"💳 Подписка на {period_days} дней — {_fmt_int(price_kgs)} сом.\n"
        "Нажмите кнопку ниже — откроется страница оплаты Finik с QR. На телефоне "
        "она сама предложит ваше банковское приложение; вводить номер карты не нужно.\n"
        "Доступ откроется автоматически сразу после оплаты."
    )


def onboarding_text(trial_days: int) -> str:
    """Первое сообщение новичку: приветствие + как работает + оффер триала."""
    return (
        "👋 Привет! Я слежу за объявлениями о продаже авто на mashina.kg и "
        "lalafo.kg и присылаю те, что дешевле рынка.\n\n"
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
        "Создайте первый фильтр: /add_filter\n"
        "Пригласите друзей и получите ещё дни бесплатно: /invite"
    )


TRIAL_ALREADY_USED_TEXT = (
    "Бесплатный период уже был использован на этом аккаунте.\n"
    "Оформить подписку: /subscribe"
)


def trial_paywall_text(price_kgs: int, period_days: int) -> str:
    """Разовое сообщение после окончания триала (уходит из worker)."""
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
) -> str:
    granted = (
        f"\nУже начислено бонусов: {bonuses_granted} (+{bonuses_granted * bonus_days} дней)."
        if bonuses_granted
        else ""
    )
    return (
        f"🤝 Пригласите {friends_per_bonus} друзей — получите +{bonus_days} дней бесплатно.\n\n"
        f"Ваша ссылка:\n{link}\n\n"
        f"Друг засчитывается, когда перейдёт по ссылке и активирует бесплатный период.\n"
        f"Засчитано друзей: {qualified}. До следующего бонуса: {friends_to_next_bonus}."
        f"{granted}"
    )


def referral_bonus_text(days: int) -> str:
    return (
        f"🎉 Ваши друзья активировали бота — вам начислено +{days} дней доступа.\n"
        "Статус: /status"
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

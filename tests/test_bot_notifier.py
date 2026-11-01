import asyncio

from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter

from app.bot.matching import PendingNotification
from app.bot.keyboards import BlockMenuCB, HideCB
from app.bot.messages import (
    card_heading,
    card_title,
    format_filter_line,
    format_notification,
    format_notification_legacy,
)
from app.bot.digest import LEGACY_DIGEST_MAX_ITEMS as DIGEST_MAX_ITEMS
from app.bot.notifier import (
    CAPTION_MAX_LENGTH,
    build_outgoing,
    send_notifications,
)


def _note(chat_id: int = 42, **overrides) -> PendingNotification:
    values = dict(
        chat_id=chat_id,
        listing_id=1,
        filter_id=1,
        title="Toyota Camry 2019",
        year=2019,
        mileage_km=90_000,
        price_usd=10200.0,
        city="Бишкек",
        url="https://www.mashina.kg/details/toyota-camry-1/",
        below_market_percent=None,
    )
    values.update(overrides)
    return PendingNotification(**values)


class FakeBot:
    def __init__(
        self,
        errors: list[Exception] | None = None,
        photo_errors: list[Exception] | None = None,
    ) -> None:
        self.messages: list[tuple[int, str]] = []
        self.photos: list[tuple[int, str, str]] = []  # (chat_id, photo, caption)
        self._errors = errors or []
        self._photo_errors = photo_errors or []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        if self._errors:
            raise self._errors.pop(0)
        self.messages.append((chat_id, text))

    async def send_photo(self, chat_id: int, photo: str, **kwargs) -> None:
        if self._photo_errors:
            raise self._photo_errors.pop(0)
        self.photos.append((chat_id, photo, kwargs["caption"]))


# --- карточка до 22.10.2026 (откат флагом BOT_RICH_CARDS_PER_CYCLE=0) --------


def test_legacy_notification_full() -> None:
    text = format_notification_legacy(_note(below_market_percent=15.0, market_avg_usd=12000.0))
    assert "<b>Toyota Camry 2019</b>" in text
    assert "Год: 2019 · Пробег: 90 000 км" in text
    assert "Цена: <b>$10 200</b> · на 15% ниже средней" in text
    assert "Средняя цена по рынку: $12 000" in text
    assert "Город: Бишкек" in text
    assert "за рубежом" not in text
    assert '<a href="https://www.mashina.kg/details/toyota-camry-1/">' in text


def test_legacy_notification_null_mileage_marks_avg_as_all_mileages() -> None:
    # Пустой пробег: среднее берётся по всей когорте модель+год -> помечаем.
    text = format_notification_legacy(_note(mileage_km=None, below_market_percent=15.0, market_avg_usd=12000.0))
    assert "Средняя цена по рынку (по всем пробегам): $12 000" in text
    # Известный пробег: пометки нет.
    text_known = format_notification_legacy(_note(mileage_km=90_000, market_avg_usd=12000.0))
    assert "Средняя цена по рынку: $12 000" in text_known
    assert "по всем пробегам" not in text_known


def test_legacy_notification_foreign_listing_highlights_location() -> None:
    text = format_notification_legacy(_note(city="Китай"))
    assert "Город: Китай" in text
    assert "🌍 <b>Внимание: авто находится за рубежом</b>" in text
    assert "растаможк" not in text


def test_legacy_notification_missing_fields_and_escaping() -> None:
    text = format_notification_legacy(
        _note(title="<script>", year=None, mileage_km=None, price_usd=None, city=None)
    )
    assert "&lt;script&gt;" in text
    assert "Год: - · Пробег: -" in text
    assert "Цена: <b>-</b>" in text
    assert "ниже средней" not in text
    assert "Средняя цена" not in text


# --- новая карточка (план 22.10.2026) ------------------------------------------


def _rich_note(**overrides) -> PendingNotification:
    values = dict(
        title="Toyota Camry 70 2.5 AT",
        brand_name="Toyota",
        model_name="Camry",
        year=2018,
        mileage_km=98_000,
        price_usd=15_900.0,
        city="Бишкек",
        below_market_percent=18.0,
        market_avg_usd=19_380.0,
        market_samples=31,
        image_url="https://s3.mashina.kg/images/x_medium.webp",
    )
    values.update(overrides)
    return _note(**values)


def test_format_notification_matches_owner_layout() -> None:
    text = format_notification(_rich_note())
    assert text.split("\n") == [
        "<b>Toyota Camry: 2018 г.</b>",
        "💰 $15 900 · на 18% ниже рынка (выгода ≈ $3 500)",
        "Рынок: $19 400 - по 31 похожей машине",
        "98 000 км · Бишкек",
        "mashina.kg",  # источник простым текстом; ссылка — кнопкой «Открыть ↗»
    ]


def test_format_notification_has_no_placeholders_or_service_phrase() -> None:
    """Строка без значения не выводится; служебной фразы в начале нет."""
    text = format_notification(
        _rich_note(
            mileage_km=None, city=None, below_market_percent=None, market_avg_usd=None
        )
    )
    assert "—" not in text and ": -" not in text
    assert "Новое объявление" not in text
    assert "Рынок" not in text and "ниже рынка" not in text
    assert text == "<b>Toyota Camry: 2018 г.</b>\n💰 $15 900\nmashina.kg"


def test_format_notification_says_ниже_рынка_not_средней() -> None:
    text = format_notification(_rich_note())
    assert "ниже рынка" in text
    assert "средн" not in text


def test_format_notification_samples_plural() -> None:
    assert "по 5 похожим машинам" in format_notification(_rich_note(market_samples=5))
    assert "по 21 похожей машине" in format_notification(_rich_note(market_samples=21))
    assert "по 12 похожим машинам" in format_notification(_rich_note(market_samples=12))
    # Без числа образцов (старые данные) — только цена рынка.
    assert "Рынок: $19 400\n" in format_notification(_rich_note(market_samples=None))


def test_format_notification_unknown_mileage_is_said_plainly() -> None:
    text = format_notification(_rich_note(mileage_km=None))
    assert "пробег не указан - по всем пробегам" in text


def test_format_notification_foreign_and_escaping() -> None:
    text = format_notification(_rich_note(city="Китай"))
    assert "🌍 <b>Внимание: авто находится за рубежом</b>" in text
    escaped = format_notification(_rich_note(brand_name=None, model_name=None, title="<b>x</b>: 2016 г."))
    assert escaped.startswith("<b>&lt;b&gt;x&lt;/b&gt;: 2018 г.</b>")


def test_card_title_falls_back_to_source_title() -> None:
    assert card_title(_rich_note()) == "Toyota Camry · 2018"
    # lalafo: «Марка Модель: год, объём, …» — берём часть до двоеточия.
    lalafo = _rich_note(model_name=None, title="Toyota Camry: 2016 г., 2.5 л, Автомат")
    assert card_title(lalafo) == "Toyota Camry · 2018"
    assert card_title(_rich_note(model_name=None, title=None, year=None)) == "Объявление"


def test_card_heading_has_base_specs() -> None:
    """Первая строка - как у lalafo: «Марка Модель: год, объём, коробка, топливо, кузов»."""
    mashina = _rich_note(engine_volume=2.5, gearbox="автомат", fuel="Гибрид", body_type="седан")
    assert card_heading(mashina) == "Toyota Camry: 2018 г., 2.5 л, Автомат, Гибрид, Седан"
    assert format_notification(mashina).startswith(
        "<b>Toyota Camry: 2018 г., 2.5 л, Автомат, Гибрид, Седан</b>\n"
    )
    bmw = _rich_note(
        brand_name="BMW", model_name="5 series", year=2020, engine_volume=2.0,
        gearbox="Автомат", fuel="Бензин", body_type="Седан",
    )
    assert card_heading(bmw) == "BMW 5 series: 2020 г., 2 л, Автомат, Бензин, Седан"


def test_card_heading_electric_without_volume_and_skips_unknown() -> None:
    electric = _rich_note(engine_volume=0.0, gearbox="Автомат", fuel="Электромобиль", body_type="Седан")
    assert card_heading(electric) == "Toyota Camry: 2018 г., Автомат, Электромобиль, Седан"
    # Неизвестное не выводится, без лишних запятых.
    assert card_heading(_rich_note(engine_volume=1.6, gearbox=None, fuel=None, body_type="")) == (
        "Toyota Camry: 2018 г., 1.6 л"
    )
    # Мусор без букв (у mashina коробка бывает «-1») пропускается.
    assert card_heading(_rich_note(gearbox="-1", fuel=None, body_type="внедорожник 5 дв.")) == (
        "Toyota Camry: 2018 г., Внедорожник 5 дв."
    )
    assert card_heading(_rich_note(model_name=None, title=None, year=None)) == "Объявление"


def test_fuel_display_prefers_source_text() -> None:
    from app.services.fuel import fuel_display

    assert fuel_display("Бензин, Газ", "petrol_gas") == "Бензин, Газ"  # lalafo как есть
    assert fuel_display(None, "electric") == "Электромобиль"  # mashina по группе
    assert fuel_display(None, "diesel") == "Дизель"
    assert fuel_display(None, None) is None


def test_new_card_fits_photo_caption() -> None:
    text = format_notification(_rich_note(title="x" * 300, model_name=None, city="y" * 80))
    assert len(text) <= CAPTION_MAX_LENGTH


def _button_callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row if button.callback_data]


def test_rich_cards_zero_keeps_previous_output() -> None:
    """Флаг 0 — прежний вывод байт в байт: старый текст, старая кнопка, без фото."""
    notes = [_rich_note(listing_id=index) for index in range(5)]
    messages = build_outgoing(notes, rich_cards=0)
    assert [message.text for message in messages] == [format_notification_legacy(n) for n in notes]
    assert all(message.photo_url is None for message in messages)
    assert all(
        _button_callbacks(message.reply_markup) == [BlockMenuCB(listing_id=note.listing_id).pack()]
        for message, note in zip(messages, notes)
    )


def test_rich_cards_top_n_per_chat_get_photo() -> None:
    notes = [_rich_note(chat_id=1, listing_id=index) for index in range(5)] + [
        _rich_note(chat_id=2, listing_id=10, image_url=None)
    ]
    messages = build_outgoing(notes, rich_cards=3)
    chat1 = [message for message in messages if message.chat_id == 1]
    assert [message.photo_url is not None for message in chat1] == [True, True, True, False, False]
    # Без фото у объявления — текстовая карточка даже в топе.
    assert [message.photo_url for message in messages if message.chat_id == 2] == [None]
    # Новый текст и «Не то» — у всех карточек, не только у топа.
    assert all(message.text.startswith("<b>Toyota Camry: 2018 г.</b>") for message in messages)
    assert _button_callbacks(chat1[4].reply_markup) == [HideCB(listing_id=4).pack()]
    urls = [button.url for row in chat1[0].reply_markup.inline_keyboard for button in row if button.url]
    assert urls == [notes[0].url]


def test_rich_top_is_not_swallowed_by_digest() -> None:
    notes = [_rich_note(chat_id=1, listing_id=index, brand_name="Toyota") for index in range(6)]
    messages = build_outgoing(notes, rich_cards=3, group_min=3)
    assert [message.photo_url is not None for message in messages] == [True, True, True, False]
    assert messages[3].listing_id is None  # остальные три — одним списком


def test_send_notifications_sends_photo_for_top() -> None:
    bot = FakeBot()
    sent = asyncio.run(send_notifications(bot, [_rich_note()], rich_cards=3))
    assert sent == 1
    assert bot.messages == []
    assert bot.photos == [(42, _rich_note().image_url, format_notification(_rich_note()))]


def test_send_notifications_photo_rejected_falls_back_to_text() -> None:
    """Битая картинка — не наш баг: карточка доходит текстом."""
    bot = FakeBot(
        photo_errors=[
            TelegramBadRequest(method=None, message="Bad Request: wrong type of the web page content")
        ]
    )
    blocked: list[int] = []

    async def on_blocked(chat_id: int) -> None:
        blocked.append(chat_id)

    sent = asyncio.run(
        send_notifications(bot, [_rich_note()], rich_cards=3, on_blocked=on_blocked)
    )
    assert sent == 1
    assert bot.messages == [(42, format_notification(_rich_note()))]
    assert blocked == []


def test_send_notifications_gone_chat_on_photo_is_not_retried_as_text() -> None:
    bot = FakeBot(photo_errors=[TelegramBadRequest(method=None, message="Bad Request: chat not found")])
    blocked: list[int] = []

    async def on_blocked(chat_id: int) -> None:
        blocked.append(chat_id)

    sent = asyncio.run(
        send_notifications(bot, [_rich_note()], rich_cards=3, on_blocked=on_blocked)
    )
    assert sent == 0
    assert bot.messages == []
    assert blocked == [42]


def test_format_filter_line() -> None:
    line = format_filter_line(
        brand_name="BMW",
        model_name="X5",
        year_from=2015,
        year_to=2020,
        mileage_max_km=150_000,
        price_max_usd=25_000.0,
        below_market_only=True,
        skip_foreign=True,
    )
    assert line == (
        "BMW X5 · 2015-2020 г. · до 150 000 км · до $25 000 · только ниже рынка · без зарубежных"
    )
    assert (
        format_filter_line(
            brand_name="BMW",
            model_name="X5",
            year_from=None,
            year_to=None,
            mileage_max_km=None,
            price_max_usd=None,
            below_market_only=True,
            below_market_percent=10,
        )
        == "BMW X5 · ниже рынка на 10%+"
    )
    assert (
        format_filter_line(
            brand_name="BMW",
            model_name=None,
            year_from=None,
            year_to=None,
            mileage_max_km=None,
            price_max_usd=None,
            below_market_only=False,
        )
        == "BMW (все модели)"
    )
    assert (
        format_filter_line(
            brand_name=None,
            model_name=None,
            year_from=2015,
            year_to=None,
            mileage_max_km=None,
            price_max_usd=15_000.0,
            below_market_only=False,
        )
        == "Все марки · 2015-… г. · до $15 000"
    )


def test_send_notifications_delivers_all() -> None:
    bot = FakeBot()
    pending = [_note(chat_id=1), _note(chat_id=2, listing_id=2)]
    sent = asyncio.run(send_notifications(bot, pending))
    assert sent == 2
    assert [chat_id for chat_id, _ in bot.messages] == [1, 2]


def test_send_notifications_blocked_user_calls_on_blocked() -> None:
    bot = FakeBot(errors=[TelegramForbiddenError(method=None, message="bot was blocked")])
    blocked_ids: list[int] = []

    async def on_blocked(chat_id: int) -> None:
        blocked_ids.append(chat_id)

    pending = [_note(chat_id=7), _note(chat_id=8, listing_id=2)]
    sent = asyncio.run(send_notifications(bot, pending, on_blocked=on_blocked))
    assert sent == 1
    assert blocked_ids == [7]
    assert [chat_id for chat_id, _ in bot.messages] == [8]


def test_send_notifications_deactivates_gone_chat() -> None:
    """Удалённый аккаунт отдаёт BadRequest, а не Forbidden — тоже деактивируем.

    Без этого бот долбился бы в мёртвый чат каждый цикл вечно: на проде один
    такой пользователь давал ~600 неудачных отправок в сутки.
    """
    bot = FakeBot(
        errors=[TelegramBadRequest(method=None, message="Bad Request: chat not found")]
    )
    blocked_ids: list[int] = []

    async def on_blocked(chat_id: int) -> None:
        blocked_ids.append(chat_id)

    pending = [_note(chat_id=7), _note(chat_id=8, listing_id=2)]
    sent = asyncio.run(send_notifications(bot, pending, on_blocked=on_blocked))
    assert sent == 1
    assert blocked_ids == [7]
    assert [chat_id for chat_id, _ in bot.messages] == [8]


def test_send_notifications_keeps_user_on_our_own_bad_request() -> None:
    """Битая разметка — наш баг, а не мёртвый чат: пользователя не трогаем.

    Иначе одна ошибка форматирования молча отписала бы всю базу.
    """
    bot = FakeBot(
        errors=[TelegramBadRequest(method=None, message="Bad Request: can't parse entities")]
    )
    blocked_ids: list[int] = []

    async def on_blocked(chat_id: int) -> None:
        blocked_ids.append(chat_id)

    pending = [_note(chat_id=7), _note(chat_id=8, listing_id=2)]
    sent = asyncio.run(send_notifications(bot, pending, on_blocked=on_blocked))
    assert sent == 1
    assert blocked_ids == []


def test_send_notifications_skips_rest_of_cycle_for_gone_chat() -> None:
    """Отвалившемуся чату не шлём остаток уже собранной очереди.

    Очередь строится до отправки, поэтому иначе мы бы отстояли паузу
    PER_CHAT_DELAY_SECONDS перед каждой оставшейся отправкой в пустоту.
    """
    bot = FakeBot(
        errors=[TelegramBadRequest(method=None, message="Bad Request: chat not found")]
    )
    blocked_ids: list[int] = []

    async def on_blocked(chat_id: int) -> None:
        blocked_ids.append(chat_id)

    pending = [_note(chat_id=7, listing_id=n) for n in range(1, 4)]
    sent = asyncio.run(send_notifications(bot, pending, on_blocked=on_blocked))
    assert sent == 0
    assert bot.messages == []
    # Ровно одна попытка и ровно одна деактивация, а не три.
    assert blocked_ids == [7]


def test_send_notifications_retry_after() -> None:
    bot = FakeBot(errors=[TelegramRetryAfter(method=None, message="flood", retry_after=0)])
    sent = asyncio.run(send_notifications(bot, [_note()]))
    assert sent == 1
    assert len(bot.messages) == 1


def test_send_notifications_unexpected_error_does_not_stop_batch() -> None:
    bot = FakeBot(errors=[RuntimeError("boom")])
    pending = [_note(chat_id=1), _note(chat_id=2, listing_id=2)]
    sent = asyncio.run(send_notifications(bot, pending))
    assert sent == 1
    assert [chat_id for chat_id, _ in bot.messages] == [2]


# --------------------------------------------------------------------------
# Группировка нескольких авто одной марки в одно сообщение
# --------------------------------------------------------------------------


def _brand_note(brand: str, listing_id: int, chat_id: int = 42) -> PendingNotification:
    return _note(
        chat_id=chat_id,
        listing_id=listing_id,
        brand_name=brand,
        title=f"{brand} модель {listing_id}",
    )


def test_grouping_off_by_default_keeps_one_message_per_listing() -> None:
    pending = [_brand_note("Toyota", i) for i in range(4)]
    messages = build_outgoing(pending)
    assert len(messages) == 4
    # У карточки есть кнопка «не показывать такие», у неё же и listing_id.
    assert all(m.reply_markup is not None and m.listing_id is not None for m in messages)


def test_grouping_collapses_same_brand_and_leaves_the_rest_as_cards() -> None:
    pending = [_brand_note("Toyota", i) for i in range(3)]
    pending += [_brand_note("Kia", 100), _brand_note("Kia", 101)]  # меньше порога
    pending.append(_note(chat_id=42, listing_id=200, brand_name=None))  # марка неизвестна

    messages = build_outgoing(pending, group_min=3)

    digests = [m for m in messages if m.reply_markup is None]
    cards = [m for m in messages if m.reply_markup is not None]
    assert len(digests) == 1
    assert "Toyota" in digests[0].text and "3 объявления" in digests[0].text
    # Kia (2 < порога) и объявление без марки уходят карточками, как раньше.
    assert len(cards) == 3
    assert {m.listing_id for m in cards} == {100, 101, 200}


def test_digest_is_split_when_there_are_too_many_items() -> None:
    pending = [_brand_note("Toyota", i) for i in range(DIGEST_MAX_ITEMS + 2)]
    messages = build_outgoing(pending, group_min=3)
    assert len(messages) == 2
    assert all(m.reply_markup is None for m in messages)
    # Ни одно объявление не потеряно и не задвоено: по ссылке на каждое.
    assert sum(m.text.count("<a href=") for m in messages) == DIGEST_MAX_ITEMS + 2


def test_outgoing_alternates_between_chats() -> None:
    """Соседние сообщения — разным людям: иначе пауза 1 с/чат душит рассылку."""
    pending = [_brand_note("Toyota", i, chat_id=1) for i in range(3)]
    pending += [_brand_note("Toyota", 10 + i, chat_id=2) for i in range(3)]

    chats = [m.chat_id for m in build_outgoing(pending)]
    assert chats == [1, 2, 1, 2, 1, 2]


def test_send_notifications_counts_messages_not_listings() -> None:
    bot = FakeBot()
    pending = [_brand_note("Toyota", i) for i in range(5)]
    sent = asyncio.run(send_notifications(bot, pending, group_min=3))
    assert sent == 1
    assert len(bot.messages) == 1
    assert "5 объявлений" in bot.messages[0][1]


def test_format_notification_rounds_percent_to_whole() -> None:
    assert "на 15% ниже рынка" in format_notification(_rich_note(below_market_percent=14.9))
    # Меньше половины процента — скидки в карточке нет вовсе, а не «на 0%».
    tiny = format_notification(_rich_note(below_market_percent=0.4))
    assert "ниже рынка" not in tiny and "выгода" not in tiny

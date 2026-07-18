import asyncio

from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from app.bot.matching import PendingNotification
from app.bot.messages import format_filter_line, format_notification
from app.bot.notifier import send_notifications


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
    def __init__(self, errors: list[Exception] | None = None) -> None:
        self.messages: list[tuple[int, str]] = []
        self._errors = errors or []

    async def send_message(self, chat_id: int, text: str, **kwargs) -> None:
        if self._errors:
            raise self._errors.pop(0)
        self.messages.append((chat_id, text))


def test_format_notification_full() -> None:
    text = format_notification(_note(below_market_percent=15.0, market_avg_usd=12000.0))
    assert "<b>Toyota Camry 2019</b>" in text
    assert "Год: 2019 · Пробег: 90 000 км" in text
    assert "Цена: <b>$10 200</b> · на 15% ниже средней" in text
    assert "Средняя цена по рынку: $12 000" in text
    assert "Город: Бишкек" in text
    assert "за рубежом" not in text
    assert '<a href="https://www.mashina.kg/details/toyota-camry-1/">' in text


def test_format_notification_foreign_listing_highlights_location() -> None:
    text = format_notification(_note(city="Китай"))
    assert "Город: Китай" in text
    assert "🌍 <b>Внимание: авто находится за рубежом</b>" in text
    assert "растаможк" not in text


def test_format_notification_missing_fields_and_escaping() -> None:
    text = format_notification(
        _note(title="<script>", year=None, mileage_km=None, price_usd=None, city=None)
    )
    assert "&lt;script&gt;" in text
    assert "Год: — · Пробег: —" in text
    assert "Цена: <b>—</b>" in text
    assert "ниже средней" not in text
    assert "Средняя цена" not in text


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
        "BMW X5 · 2015–2020 г. · до 150 000 км · до $25 000 · только ниже рынка · без зарубежных"
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
        == "Все марки · 2015–… г. · до $15 000"
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

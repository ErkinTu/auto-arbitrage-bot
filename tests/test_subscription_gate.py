"""Гейт оплаты и гейт скорости: кто может платить и кто как быстро получает пуши.

Смысл вайтлиста — обкатать боевой контур Finik на проде так, чтобы обычные
пользователи не могли заплатить: платёж им даже не заводится (иначе в
`payments` копились бы pending, а воронка врала бы про конверсию).

Вторая половина файла — `effective_tier`: приоритет источников скорости и
fail-safe по умолчанию. Ошибка в нём либо раздаёт полный тариф бесплатно, либо
тормозит того, кто за скорость заплатил.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.core.config import Settings
from app.models import BotUser
from app.services.plans import (
    PLANS,
    TIER_DELAYED,
    TIER_FULL,
    effective_tier,
    find_plan,
    plan_for_code,
)

# Пустой _env_file: иначе pydantic подхватит локальный .env и тест станет
# зависеть от машины разработчика.
CREDENTIALS = dict(finik_api_key="k", finik_account_id="a", public_base_url="https://pay.example")


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)


def test_payments_closed_while_credentials_are_missing() -> None:
    settings = _settings()
    assert settings.subscription_enabled is False
    assert settings.subscription_testing is False
    assert settings.subscription_allowed_for(1) is False


def test_payments_open_to_everyone_without_tester_list() -> None:
    settings = _settings(**CREDENTIALS)
    assert settings.subscription_enabled is True
    assert settings.subscription_testing is False
    assert settings.subscription_allowed_for(777) is True


def test_only_testers_may_pay_while_list_is_set() -> None:
    settings = _settings(**CREDENTIALS, subscription_tester_ids="100, 200 ,не-число")
    assert settings.subscription_testing is True
    assert settings.subscription_allowed_for(100) is True
    assert settings.subscription_allowed_for(200) is True
    assert settings.subscription_allowed_for(300) is False


def test_tester_list_alone_does_not_open_payments() -> None:
    """Без кредов Finik платить нельзя даже тестировщику."""
    settings = _settings(subscription_tester_ids="100")
    assert settings.subscription_testing is False
    assert settings.subscription_allowed_for(100) is False


# --- скорость уведомлений ----------------------------------------------------

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _user(**kwargs) -> BotUser:
    return BotUser(telegram_id=1, access_status=kwargs.pop("access_status", "revoked"), **kwargs)


def test_group_member_is_always_full_speed() -> None:
    """Участники группы — как админы: тариф им не выдаётся и не проверяется."""
    member = _user(access_status="member")
    assert effective_tier(member, None, NOW) == TIER_FULL
    # Даже если ему зачем-то завели отстающий блок.
    assert effective_tier(member, TIER_DELAYED, NOW) == TIER_FULL


def test_trial_is_full_speed() -> None:
    """Триал показывает продукт в лучшем виде: продавать отставание вслепую нельзя."""
    trial = _user(
        access_status="trial",
        trial_started_at=NOW - timedelta(days=1),
        trial_expires_at=NOW + timedelta(days=2),
    )
    assert effective_tier(trial, None, NOW) == TIER_FULL


def test_expired_trial_no_longer_grants_full_speed() -> None:
    expired = _user(
        access_status="subscriber",
        trial_started_at=NOW - timedelta(days=10),
        trial_expires_at=NOW - timedelta(days=7),
    )
    assert effective_tier(expired, TIER_DELAYED, NOW) == TIER_DELAYED
    assert effective_tier(expired, TIER_FULL, NOW) == TIER_FULL


def test_unknown_speed_defaults_to_delayed() -> None:
    """Fail-safe: неизвестность = медленный, но рабочий доступ.

    Обратный дефолт раздавал бы полный тариф бесплатно — и молча, потому что
    жалоб на «слишком быстро» не бывает.
    """
    assert effective_tier(_user(access_status="subscriber"), None, NOW) == TIER_DELAYED


def test_plan_registry_is_self_consistent() -> None:
    """Сетка должна сходиться: месяц дешевле четырёх недель в обеих скоростях."""
    codes = {plan.code for plan in PLANS}
    assert codes == {"month_full", "month_delayed", "week_full", "week_delayed"}
    month_full = find_plan("month_full")
    week_full = find_plan("week_full")
    month_delayed = find_plan("month_delayed")
    week_delayed = find_plan("week_delayed")
    assert month_full.price < week_full.price * 4
    assert month_delayed.price < week_delayed.price * 4
    # «Быстрый» ровно вдвое дороже «медленного» — иначе разница не читается.
    assert week_full.price == week_delayed.price * 2
    assert month_full.tier == TIER_FULL and month_delayed.tier == TIER_DELAYED


def test_missing_and_unknown_plan_codes() -> None:
    """NULL и мусор читаются как исторический месяц; кнопке же нужен строгий отказ."""
    assert plan_for_code(None).code == "month_full"
    assert plan_for_code("no_such_plan").code == "month_full"
    assert find_plan("no_such_plan") is None
    assert find_plan(None) is None

"""Гейт оплаты: кто и когда может начать платёж.

Смысл вайтлиста — обкатать боевой контур Finik на проде так, чтобы обычные
пользователи не могли заплатить: платёж им даже не заводится (иначе в
`payments` копились бы pending, а воронка врала бы про конверсию).
"""

from __future__ import annotations

from app.core.config import Settings

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

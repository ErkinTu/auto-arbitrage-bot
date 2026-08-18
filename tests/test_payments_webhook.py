"""Тесты webhook Finik: подпись, идемпотентность, сверка суммы.

Подписываем payload тестовым ключом и подменяем публичный ключ Finik для среды
`beta` (дефолт в тестах) — так проверяется весь путь целиком, включая
пересборку JSON для канона.
"""

import json
from datetime import datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import AccessBlock, BotUser, Payment
from app.services import finik
from app.services.finik import WEBHOOK_PATH, canonical, sign
from app.services.subscription import is_subscription_active, next_expiry
from tests.test_finik import TEST_PRIVATE_KEY, TEST_PUBLIC_KEY

WEBHOOK_URL = WEBHOOK_PATH
PRICE = "2000.00"


@pytest.fixture(autouse=True)
def _finik_test_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Finik «подписывает» webhook тестовым ключом (env по умолчанию — beta)."""
    monkeypatch.setitem(finik.FINIK_PUBLIC_KEYS, "beta", TEST_PUBLIC_KEY)


def _client() -> tuple[TestClient, sessionmaker]:
    # Модели уже зарегистрированы импортом app.main; отдельный import не нужен
    # (он бы затенил объект FastAPI `app` именем пакета).
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    TestingSession = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)

    def _override():
        with TestingSession() as session:
            yield session

    app.dependency_overrides[get_session] = _override
    return TestClient(app), TestingSession


def _seed_pending_payment(
    SessionMaker: sessionmaker,
    amount: str = PRICE,
    *,
    plan_code: str | None = "month_full",
    period_days: int = 30,
) -> tuple[int, str]:
    with SessionMaker() as session:
        user = BotUser(telegram_id=111, access_status="member")
        session.add(user)
        session.flush()
        payment = Payment(
            user_id=user.id,
            order_id="8e1f6a4c-0000-4000-8000-000000000001",
            amount=Decimal(amount),
            currency="KGS",
            period_days=period_days,
            plan_code=plan_code,
            status="pending",
        )
        session.add(payment)
        session.commit()
        return user.id, payment.order_id


def _signed_webhook(
    order_id: str,
    *,
    amount: float | int = 2000,
    status: str = "success",
    transaction_id: str = "TXN-77",
    drop_payment_id: bool = False,
) -> tuple[bytes, dict[str, str]]:
    """Готовит тело и заголовки так, как их присылает Finik.

    Порядок ключей — как в доках (НЕ отсортированный): подпись считается по
    пересобранному JSON с отсортированным верхним уровнем, и тест на этом ловит
    ошибки пересборки.
    """
    fields: dict[str, object] = {"amount": amount}
    if not drop_payment_id:
        fields["paymentId"] = order_id
    payload: dict[str, object] = {
        "id": "trx-15423_DEBIT",
        "transactionId": transaction_id,
        "status": status,
        "amount": amount,
        "fields": fields,
        "requestDate": 1737369012345,
        "transactionDate": 1737369012345,
    }
    raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    signed_headers = {
        "Host": "testserver",
        "x-api-key": "finik-webhook-key",
        "x-api-timestamp": "1737369012345",
    }
    signature = sign(
        canonical(method="POST", path=WEBHOOK_URL, headers=signed_headers, body=payload),
        TEST_PRIVATE_KEY,
    )
    headers = {k: v for k, v in signed_headers.items() if k != "Host"}
    headers.update({"content-type": "application/json", "signature": signature})
    return raw, headers


def test_next_expiry_extends_from_now_when_expired() -> None:
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    assert next_expiry(None, now, 30) == now + timedelta(days=30)
    past = now - timedelta(days=5)
    assert next_expiry(past, now, 30) == now + timedelta(days=30)


def test_next_expiry_stacks_on_active_subscription() -> None:
    now = datetime(2026, 7, 25, tzinfo=timezone.utc)
    future = now + timedelta(days=10)
    assert next_expiry(future, now, 30) == future + timedelta(days=30)


def test_webhook_success_activates_subscription() -> None:
    client, SessionMaker = _client()
    try:
        user_id, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id)
        resp = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

        with SessionMaker() as session:
            payment = session.get(Payment, 1)
            user = session.get(BotUser, user_id)
            assert payment.status == "paid"
            assert payment.finik_transaction_id == "TXN-77"
            assert payment.raw_callback["transactionId"] == "TXN-77"
            assert user.access_status == "subscriber"
            assert is_subscription_active(user)
    finally:
        app.dependency_overrides.clear()


def test_webhook_accepts_integral_float_amount() -> None:
    """Регрессия: JS печатает 2000.00 как 2000, Python — как 2000.0.

    Без нормализации чисел пересобранный JSON не совпал бы с подписанным и
    первый же платёж на «ровную» сумму отвалился бы с 401.
    """
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id, amount=2000.0)
        assert b'"amount":2000.0' in raw  # на проводе именно дробное представление
        resp = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert resp.status_code == 200
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "paid"
    finally:
        app.dependency_overrides.clear()


def test_webhook_status_check_is_case_insensitive() -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id, status="SUCCESS")
        assert client.post(WEBHOOK_URL, content=raw, headers=headers).status_code == 200
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "paid"
    finally:
        app.dependency_overrides.clear()


def test_webhook_accepts_succeeded_status() -> None:
    """Регрессия: доки Finik обещают "success", боевой Finik шлёт "succeeded".

    На этом отвалилась первая реальная оплата 14.08.2026: webhook пришёл, подпись
    сошлась, а платёж был отброшен как «не успех» — и ретраев не будет, мы же
    ответили 200.
    """
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id, status="succeeded")
        assert client.post(WEBHOOK_URL, content=raw, headers=headers).status_code == 200
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "paid"
    finally:
        app.dependency_overrides.clear()


def test_webhook_is_idempotent() -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id)
        first = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert first.json()["description"] == "subscription activated"
        with SessionMaker() as session:
            user = session.scalar(select(BotUser))
            first_expiry = user.subscription_expires_at
        # Ретрай Finik по тому же платежу не должен продлевать второй раз.
        second = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert second.status_code == 200
        assert second.json()["description"] == "already processed"
        with SessionMaker() as session:
            user = session.scalar(select(BotUser))
            assert user.subscription_expires_at == first_expiry
            assert session.get(Payment, 1).status == "paid"
    finally:
        app.dependency_overrides.clear()


def test_webhook_bad_signature_rejected() -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id)
        headers["signature"] = "AAAA" + headers["signature"][4:]
        resp = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert resp.status_code == 401
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "pending"
            assert session.scalar(select(BotUser)).access_status == "member"
    finally:
        app.dependency_overrides.clear()


def test_webhook_tampered_body_rejected() -> None:
    """Подпись покрывает тело: подмена суммы после подписания не проходит."""
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id)
        tampered = raw.replace(b'"amount":2000', b'"amount":1000')
        resp = client.post(WEBHOOK_URL, content=tampered, headers=headers)
        assert resp.status_code == 401
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "pending"
    finally:
        app.dependency_overrides.clear()


def test_webhook_amount_mismatch_still_grants_access(caplog) -> None:
    """Расхождение суммы — наша рассинхронизация, а не причина отобрать доступ.

    Подпись Finik уже доказала, что тело не подделано, поэтому доступ выдаём по
    `plan_code` и ругаемся в лог. Отказ означал бы «деньги списаны, доступа
    нет» — ровно баг 14.08.2026, повторять его из-за арифметики нельзя.
    """
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id, amount=1)
        with caplog.at_level("WARNING"):
            resp = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert resp.status_code == 200
        assert "amount mismatch" in caplog.text
        with SessionMaker() as session:
            payment = session.get(Payment, 1)
            assert payment.status == "paid"
            assert is_subscription_active(session.scalar(select(BotUser)))
            # Дни и скорость взяты из плана/платежа, а не из суммы в webhook.
            assert session.scalar(select(AccessBlock.tier)) == "full"
    finally:
        app.dependency_overrides.clear()


def test_webhook_non_success_status_does_not_activate() -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id, status="failed")
        resp = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert resp.status_code == 200  # ретраить нечего
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "pending"
            assert session.scalar(select(BotUser)).access_status == "member"
    finally:
        app.dependency_overrides.clear()


def test_webhook_unknown_order_is_404() -> None:
    client, SessionMaker = _client()
    try:
        _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook("00000000-dead-4000-8000-000000000000")
        resp = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert resp.status_code == 404
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "pending"
    finally:
        app.dependency_overrides.clear()


def test_webhook_without_payment_id_is_400() -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        raw, headers = _signed_webhook(order_id, drop_payment_id=True)
        resp = client.post(WEBHOOK_URL, content=raw, headers=headers)
        assert resp.status_code == 400
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "pending"
    finally:
        app.dependency_overrides.clear()


def test_webhook_rejects_non_json_body() -> None:
    client, _ = _client()
    try:
        resp = client.post(
            WEBHOOK_URL,
            content=b"not json",
            headers={"content-type": "application/json", "signature": "x"},
        )
        assert resp.status_code == 400
    finally:
        app.dependency_overrides.clear()


# --- тарифы: блок доступа по плану платежа -----------------------------------
# Webhook приходит через минуты и знает о заказе только строку `payments`,
# поэтому именно `plan_code` решает, какой блок создавать. Ошибка здесь = человек
# заплатил за полную скорость и получил отставание (или наоборот).


@pytest.mark.parametrize(
    ("plan_code", "price", "days", "tier"),
    [
        ("month_full", "1990.00", 30, "full"),
        ("month_delayed", "990.00", 30, "delayed"),
        ("week_full", "600.00", 7, "full"),
        ("week_delayed", "300.00", 7, "delayed"),
    ],
)
def test_webhook_creates_the_block_of_the_paid_plan(
    plan_code: str, price: str, days: int, tier: str
) -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(
            SessionMaker, price, plan_code=plan_code, period_days=days
        )
        raw, headers = _signed_webhook(order_id, amount=float(price))
        assert client.post(WEBHOOK_URL, content=raw, headers=headers).status_code == 200
        with SessionMaker() as session:
            block = session.scalar(select(AccessBlock))
            user = session.scalar(select(BotUser))
            assert block.tier == tier
            assert block.origin == "payment"
            assert block.payment_id == 1
            assert (block.ends_at - block.starts_at).days == days
            # Горизонт доступа = конец последнего блока, иначе доступ и тариф
            # разъедутся: один есть, другого нет.
            assert user.subscription_expires_at == block.ends_at
    finally:
        app.dependency_overrides.clear()


def test_webhook_reads_missing_plan_code_as_the_historical_month() -> None:
    """Платежи до тарифной сетки: NULL = месяц полной скорости (было 2000 сом)."""
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker, plan_code=None)
        raw, headers = _signed_webhook(order_id)
        assert client.post(WEBHOOK_URL, content=raw, headers=headers).status_code == 200
        with SessionMaker() as session:
            block = session.scalar(select(AccessBlock))
            assert block.tier == "full"
            assert (block.ends_at - block.starts_at).days == 30
    finally:
        app.dependency_overrides.clear()


def test_webhook_retry_does_not_create_a_second_block() -> None:
    """Ровно то, ради чего в схеме UNIQUE(payment_id): ретраи Finik идут сутки."""
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker, "1990.00")
        raw, headers = _signed_webhook(order_id, amount=1990.0)
        client.post(WEBHOOK_URL, content=raw, headers=headers)
        client.post(WEBHOOK_URL, content=raw, headers=headers)
        with SessionMaker() as session:
            blocks = session.scalars(select(AccessBlock)).all()
            assert len(blocks) == 1
    finally:
        app.dependency_overrides.clear()


def test_second_payment_queues_after_the_first() -> None:
    """Апгрейд поверх отстающего: полный блок встаёт следом, а не «прямо сейчас».

    Это и есть решение «доплаты разницы нет»: скорость сейчас не меняется, но
    оплаченные дни не сгорают.
    """
    client, SessionMaker = _client()
    try:
        _, first_order = _seed_pending_payment(
            SessionMaker, "300.00", plan_code="week_delayed", period_days=7
        )
        raw, headers = _signed_webhook(first_order, amount=300.0)
        assert client.post(WEBHOOK_URL, content=raw, headers=headers).status_code == 200

        with SessionMaker() as session:
            user = session.scalar(select(BotUser))
            session.add(
                Payment(
                    user_id=user.id,
                    order_id="8e1f6a4c-0000-4000-8000-000000000002",
                    amount=Decimal("1990.00"),
                    currency="KGS",
                    period_days=30,
                    plan_code="month_full",
                    status="pending",
                )
            )
            session.commit()
        raw, headers = _signed_webhook(
            "8e1f6a4c-0000-4000-8000-000000000002", amount=1990.0
        )
        assert client.post(WEBHOOK_URL, content=raw, headers=headers).status_code == 200

        with SessionMaker() as session:
            blocks = session.scalars(select(AccessBlock).order_by(AccessBlock.starts_at)).all()
            assert [block.tier for block in blocks] == ["delayed", "full"]
            assert blocks[1].starts_at == blocks[0].ends_at  # смежно, без дыры
            user = session.scalar(select(BotUser))
            assert user.subscription_expires_at == blocks[1].ends_at
    finally:
        app.dependency_overrides.clear()

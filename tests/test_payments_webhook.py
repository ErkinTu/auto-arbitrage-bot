from datetime import datetime, timedelta, timezone
from decimal import Decimal

from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.session import get_session
from app.main import app
from app.models import BotUser, Payment
from app.services.freedompay import RESULT_SCRIPT, build_signature
from app.services.subscription import is_subscription_active, next_expiry

SECRET = ""  # freedompay_secret_key по умолчанию пуст в тестах


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


def _seed_pending_payment(SessionMaker: sessionmaker, amount: str = "500.00") -> tuple[int, str]:
    with SessionMaker() as session:
        user = BotUser(telegram_id=111, access_status="member")
        session.add(user)
        session.flush()
        payment = Payment(
            user_id=user.id,
            order_id="ORD-1",
            amount=Decimal(amount),
            currency="KGS",
            period_days=30,
            status="pending",
        )
        session.add(payment)
        session.commit()
        return user.id, payment.order_id


def _signed_result(order_id: str, *, result: str = "1", amount: str = "500.00") -> dict:
    params = {
        "pg_order_id": order_id,
        "pg_payment_id": "PAY-9",
        "pg_result": result,
        "pg_amount": amount,
        "pg_currency": "KGS",
        "pg_salt": "salt123",
    }
    params["pg_sig"] = build_signature(RESULT_SCRIPT, params, SECRET)
    return params


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
        resp = client.post("/api/v1/payments/result", data=_signed_result(order_id))
        assert resp.status_code == 200
        assert "<pg_status>ok</pg_status>" in resp.text

        with SessionMaker() as session:
            payment = session.get(Payment, 1)
            user = session.get(BotUser, user_id)
            assert payment.status == "paid"
            assert payment.freedompay_payment_id == "PAY-9"
            assert user.access_status == "subscriber"
            assert is_subscription_active(user)
    finally:
        app.dependency_overrides.clear()


def test_webhook_is_idempotent() -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        first = client.post("/api/v1/payments/result", data=_signed_result(order_id))
        assert "<pg_status>ok</pg_status>" in first.text
        with SessionMaker() as session:
            expires_after_first = session.get(Payment, 1).status
            user = session.scalar(select(BotUser))
            first_expiry = user.subscription_expires_at
        # Повторный вызов не должен продлевать второй раз.
        second = client.post("/api/v1/payments/result", data=_signed_result(order_id))
        assert "already processed" in second.text
        with SessionMaker() as session:
            user = session.scalar(select(BotUser))
            assert user.subscription_expires_at == first_expiry
        assert expires_after_first == "paid"
    finally:
        app.dependency_overrides.clear()


def test_webhook_bad_signature_rejected() -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker)
        params = _signed_result(order_id)
        params["pg_sig"] = "0" * 32
        resp = client.post("/api/v1/payments/result", data=params)
        assert "<pg_status>error</pg_status>" in resp.text
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "pending"
    finally:
        app.dependency_overrides.clear()


def test_webhook_amount_mismatch_rejected() -> None:
    client, SessionMaker = _client()
    try:
        _, order_id = _seed_pending_payment(SessionMaker, amount="500.00")
        # Подписанный, но с суммой 1.00 вместо 500.00.
        resp = client.post("/api/v1/payments/result", data=_signed_result(order_id, amount="1.00"))
        assert "<pg_status>error</pg_status>" in resp.text
        assert "amount mismatch" in resp.text
        with SessionMaker() as session:
            assert session.get(Payment, 1).status == "pending"
    finally:
        app.dependency_overrides.clear()

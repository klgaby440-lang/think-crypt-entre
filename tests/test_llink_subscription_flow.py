import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("LLINK_SSO_SECRET", "test-sso-secret-" + ("x" * 48))

import main


@pytest.fixture
def db_session():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    main.Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        engine.dispose()


def _user(db):
    user = main.ThinkUser(
        think_id="THK-" + uuid4().hex[:12].upper(),
        crypt_id=main._new_crypt_id(),
        email=f"user-{uuid4().hex[:8]}@example.test",
        password_hash="test-hash",
        full_name="Llink Test User",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_llink_sso_ticket_is_short_lived_and_scoped(monkeypatch):
    monkeypatch.setenv("LLINK_SSO_SECRET", "sso-secret-" + ("s" * 48))
    user = SimpleNamespace(
        id=42,
        think_id="THK-0123456789AB",
        crypt_id="INK_ABCDEFGHJKLMNPQ",
        email="tester@example.test",
        full_name="Think Tester",
    )
    result = main.exchange_llink_sso(user=user)
    claims = __import__("jwt").decode(
        result["sso_token"],
        os.environ["LLINK_SSO_SECRET"],
        algorithms=["HS256"],
        audience="llink-api",
        issuer="think-api",
    )
    assert claims["sub"] == "42"
    assert claims["think_id"] == "THK-0123456789AB"
    assert claims["crypt_id"] == "INK_ABCDEFGHJKLMNPQ"
    assert claims["scope"] == "llink"
    assert claims["aud"] == "llink-api"
    assert 0 < claims["exp"] - claims["iat"] <= 120


def test_llink_sso_refuses_weak_secret(monkeypatch):
    monkeypatch.setenv("LLINK_SSO_SECRET", "short")
    user = SimpleNamespace(
        id=42,
        think_id="THK-0123456789AB",
        email="tester@example.test",
        full_name="Think Tester",
    )
    with pytest.raises(HTTPException) as error:
        main.exchange_llink_sso(user=user)
    assert error.value.status_code == 503


def test_llink_plan_prices_validate_all_plans(monkeypatch):
    monkeypatch.setenv(
        "LLINK_PLAN_PRICES_CDF",
        '{"bronze":500,"silver":1500,"gold":3000,"diamant":5000}',
    )
    prices = main._llink_plan_prices_cdf()
    assert prices == {
        "bronze": Decimal("500"),
        "silver": Decimal("1500"),
        "gold": Decimal("3000"),
        "diamant": Decimal("5000"),
    }


@pytest.mark.parametrize(
    "raw",
    [
        '{"bronze":0,"silver":1500,"gold":3000,"diamant":5000}',
        '{"bronze":500,"silver":1500,"gold":3000}',
        '{"bronze":true,"silver":1500,"gold":3000,"diamant":5000}',
        '{"bronze":501,"silver":1500,"gold":3000,"diamant":5000}',
    ],
)
def test_llink_plan_prices_reject_invalid_catalog(monkeypatch, raw):
    monkeypatch.setenv("LLINK_PLAN_PRICES_CDF", raw)
    with pytest.raises(HTTPException) as error:
        main._llink_plan_prices_cdf()
    assert error.value.status_code == 503


def test_cinetpay_success_activates_llink_without_crediting_think_wallet(db_session):
    user = _user(db_session)
    wallet = main.Wallet(
        user_id=user.id,
        currency="CDF",
        available_balance=Decimal("0"),
        reserved_balance=Decimal("0"),
    )
    payment = main.PaymentTransaction(
        public_id="PAY-" + uuid4().hex[:16].upper(),
        user_id=user.id,
        purpose="llink_subscription",
        amount=Decimal("500"),
        currency="CDF",
        provider="cinetpay_v1",
        merchant_transaction_id="THK" + uuid4().hex[:24].upper(),
        provider_transaction_id="provider-llink-123",
        idempotency_key="llink-" + str(user.id) + "-" + uuid4().hex[:16],
        status="pending",
        payment_url="https://checkout.example.test/pay",
        provider_response={"environment": "sandbox"},
    )
    db_session.add_all([wallet, payment])
    db_session.commit()
    db_session.refresh(payment)

    sub = main.LlinkSubscription(
        user_id=user.id,
        checkout_id=payment.idempotency_key,
        plan="bronze",
        status="pending_payment",
        amount=Decimal("500"),
        currency="CDF",
        payment_transaction_id=payment.id,
    )
    db_session.add(sub)
    db_session.commit()
    db_session.refresh(sub)

    provider_status = SimpleNamespace(
        merchant_transaction_id=payment.merchant_transaction_id,
        transaction_id=payment.provider_transaction_id,
        status="SUCCESS",
        amount="500",
        currency="CDF",
    )
    main._sync_cinetpay_payment_status(db_session, payment, provider_status)
    db_session.refresh(sub)
    db_session.refresh(payment)
    db_session.refresh(wallet)

    assert payment.status == "accepted"
    assert sub.status == "active"
    assert sub.starts_at is not None
    assert sub.expires_at == sub.starts_at + timedelta(days=365)
    assert wallet.available_balance == Decimal("0")
    assert db_session.query(main.LedgerEntry).count() == 0

    original_expiry = sub.expires_at
    main._sync_cinetpay_payment_status(db_session, payment, provider_status)
    db_session.refresh(sub)
    assert sub.expires_at == original_expiry
    assert db_session.query(main.Notification).filter_by(user_id=user.id).count() == 1


def test_cinetpay_credit_purchase_is_marked_paid_without_think_wallet_credit(db_session):
    user = _user(db_session)
    wallet = main.Wallet(
        user_id=user.id, currency="CDF",
        available_balance=Decimal("250"), reserved_balance=Decimal("0"),
    )
    payment = main.PaymentTransaction(
        public_id="PAY-" + uuid4().hex[:16].upper(),
        user_id=user.id, purpose="llink_credits",
        amount=Decimal("2800"), currency="CDF", provider="cinetpay_v1",
        merchant_transaction_id="THK" + uuid4().hex[:24].upper(),
        provider_transaction_id="provider-llink-credit-123",
        idempotency_key="llink-credit-" + str(user.id) + "-" + uuid4().hex[:12],
        status="pending", payment_url="https://checkout.example.test/pay",
        provider_response={"environment": "sandbox"},
    )
    db_session.add_all([wallet, payment])
    db_session.commit()
    db_session.refresh(payment)
    purchase = main.LlinkCreditPurchase(
        user_id=user.id, checkout_id=payment.idempotency_key,
        credits=100, wallet_type="online", status="pending_payment",
        amount=Decimal("2800"), currency="CDF", cdf_per_usd=Decimal("2800"),
        payment_transaction_id=payment.id,
    )
    db_session.add(purchase)
    db_session.commit()
    provider_status = SimpleNamespace(
        merchant_transaction_id=payment.merchant_transaction_id,
        transaction_id=payment.provider_transaction_id,
        status="SUCCESS", amount="2800", currency="CDF",
    )

    main._sync_cinetpay_payment_status(db_session, payment, provider_status)
    db_session.refresh(payment)
    db_session.refresh(purchase)
    db_session.refresh(wallet)

    assert payment.status == "accepted"
    assert purchase.status == "paid"
    assert purchase.credits == 100
    assert wallet.available_balance == Decimal("250")
    assert db_session.query(main.LedgerEntry).count() == 0

    main._sync_cinetpay_payment_status(db_session, payment, provider_status)
    db_session.refresh(purchase)
    assert purchase.status == "paid"
    assert db_session.query(main.Notification).filter_by(user_id=user.id).count() == 1


def test_failed_cinetpay_credit_purchase_is_never_marked_paid(db_session):
    user = _user(db_session)
    payment = main.PaymentTransaction(
        public_id="PAY-" + uuid4().hex[:16].upper(),
        user_id=user.id, purpose="llink_credits",
        amount=Decimal("2800"), currency="CDF", provider="cinetpay_v1",
        merchant_transaction_id="THK" + uuid4().hex[:24].upper(),
        provider_transaction_id="provider-llink-credit-failed",
        idempotency_key="llink-credit-" + str(user.id) + "-" + uuid4().hex[:12],
        status="pending", payment_url="https://checkout.example.test/pay",
        provider_response={"environment": "sandbox"},
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)
    purchase = main.LlinkCreditPurchase(
        user_id=user.id, checkout_id=payment.idempotency_key,
        credits=100, wallet_type="online", status="pending_payment",
        amount=Decimal("2800"), currency="CDF", cdf_per_usd=Decimal("2800"),
        payment_transaction_id=payment.id,
    )
    db_session.add(purchase)
    db_session.commit()
    provider_status = SimpleNamespace(
        merchant_transaction_id=payment.merchant_transaction_id,
        transaction_id="provider-llink-credit-failed", status="FAILED", amount="2800", currency="CDF",
    )

    main._sync_cinetpay_payment_status(db_session, payment, provider_status)
    db_session.refresh(payment)
    db_session.refresh(purchase)

    assert payment.status == "failed"
    assert purchase.status == "failed"


def test_second_llink_subscription_starts_after_previous_expiry(db_session):
    user = _user(db_session)
    previous_end = datetime.now(timezone.utc) + timedelta(days=12)
    previous = main.LlinkSubscription(
        user_id=user.id,
        checkout_id="llink-" + str(user.id) + "-previous",
        plan="bronze",
        status="active",
        amount=Decimal("500"),
        currency="CDF",
        starts_at=datetime.now(timezone.utc),
        expires_at=previous_end,
    )
    db_session.add(previous)
    db_session.commit()
    db_session.refresh(previous)

    payment = main.PaymentTransaction(
        public_id="PAY-" + uuid4().hex[:16].upper(),
        user_id=user.id,
        purpose="llink_subscription",
        amount=Decimal("1500"),
        currency="CDF",
        provider="cinetpay_v1",
        merchant_transaction_id="THK" + uuid4().hex[:24].upper(),
        provider_transaction_id="provider-llink-456",
        idempotency_key="llink-" + str(user.id) + "-" + uuid4().hex[:16],
        status="pending",
        payment_url="https://checkout.example.test/pay",
        provider_response={"environment": "sandbox"},
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)
    sub = main.LlinkSubscription(
        user_id=user.id,
        checkout_id=payment.idempotency_key,
        plan="silver",
        status="pending_payment",
        amount=Decimal("1500"),
        currency="CDF",
        payment_transaction_id=payment.id,
    )
    db_session.add(sub)
    db_session.commit()

    provider_status = SimpleNamespace(
        merchant_transaction_id=payment.merchant_transaction_id,
        transaction_id=payment.provider_transaction_id,
        status="SUCCESS",
        amount="1500",
        currency="CDF",
    )
    main._sync_cinetpay_payment_status(db_session, payment, provider_status)
    db_session.refresh(sub)

    assert sub.starts_at.replace(tzinfo=timezone.utc) == previous_end.replace(tzinfo=timezone.utc)


def test_internal_refresh_is_scoped_to_the_think_user_and_subscription(db_session):
    user = _user(db_session)
    payment = main.PaymentTransaction(
        public_id="PAY-" + uuid4().hex[:16].upper(),
        user_id=user.id,
        purpose="llink_subscription",
        amount=Decimal("500"),
        currency="CDF",
        provider="cinetpay_v1",
        merchant_transaction_id="THK" + uuid4().hex[:24].upper(),
        provider_transaction_id="provider-llink-refresh",
        idempotency_key="llink-" + str(user.id) + "-" + uuid4().hex[:16],
        status="accepted",
        payment_url="https://checkout.example.test/pay",
        provider_response={"environment": "sandbox"},
    )
    db_session.add(payment)
    db_session.commit()
    db_session.refresh(payment)
    now = datetime.now(timezone.utc)
    sub = main.LlinkSubscription(
        user_id=user.id,
        checkout_id=payment.idempotency_key,
        plan="gold",
        status="active",
        amount=Decimal("3000"),
        currency="CDF",
        payment_transaction_id=payment.id,
        starts_at=now - timedelta(minutes=1),
        expires_at=now + timedelta(days=365),
    )
    db_session.add(sub)
    db_session.commit()

    result = main.refresh_llink_subscription_payment_internal(
        think_user_id=user.id,
        public_id=payment.public_id,
        _service=None,
        db=db_session,
    )
    assert result["payment_status"] == "accepted"
    assert result["subscription"]["is_active"] is True
    assert result["subscription"]["plan"] == "gold"

    another_user = _user(db_session)
    with pytest.raises(HTTPException) as error:
        main.refresh_llink_subscription_payment_internal(
            think_user_id=another_user.id,
            public_id=payment.public_id,
            _service=None,
            db=db_session,
        )
    assert error.value.status_code == 404


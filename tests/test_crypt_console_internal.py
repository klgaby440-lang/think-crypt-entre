import os
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

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


def test_crypt_console_service_authentication_fails_closed(monkeypatch):
    monkeypatch.delenv("CRYPT_CONSOLE_SERVICE_TOKEN", raising=False)
    with pytest.raises(HTTPException) as error:
        main.require_crypt_console_service("anything")
    assert error.value.status_code == 503


def test_crypt_console_service_authentication_rejects_invalid_token(monkeypatch):
    monkeypatch.setenv("CRYPT_CONSOLE_SERVICE_TOKEN", "console-service-secret-" + ("x" * 40))
    with pytest.raises(HTTPException) as error:
        main.require_crypt_console_service("wrong")
    assert error.value.status_code == 401


def test_crypt_console_overview_reports_identity_and_credit_totals(db_session):
    user = _new_user_for_console(db_session)
    db_session.add(main.Wallet(
        user_id=user.id, currency="CDF",
        available_balance=1234, reserved_balance=50,
    ))
    purchase = main.LlinkCreditPurchase(
        user_id=user.id, checkout_id="console-" + uuid4().hex,
        credits=250, wallet_type="online", status="paid",
        amount=2500, currency="CDF", cdf_per_usd=2800,
    )
    db_session.add(purchase)
    db_session.commit()

    result = main.crypt_console_internal_overview(db=db_session, _service=True)
    assert result["status"] is True
    assert result["data"]["users_total"] == 1
    assert result["data"]["users_active"] == 1
    assert result["data"]["llink_credit_purchases"]["credits_paid"] == 250
    assert result["data"]["wallet_balances"]["CDF"]["available"] == 1234.0


def test_crypt_console_user_lookup_and_status_change(db_session):
    user = _new_user_for_console(db_session)
    found = main.crypt_console_internal_users(
        query=user.crypt_id, limit=10, db=db_session, _service=True,
    )
    assert found["data"][0]["crypt_id"] == user.crypt_id
    assert found["data"][0]["wallets"] == {}

    updated = main.crypt_console_internal_update_user_status(
        crypt_id=user.crypt_id, payload={"is_active": False},
        db=db_session, _service=True,
    )
    db_session.refresh(user)
    assert updated["data"]["is_active"] is False
    assert user.is_active is False


def _new_user_for_console(db):
    user = main.ThinkUser(
        think_id="THK-" + uuid4().hex[:12].upper(),
        crypt_id=main._new_crypt_id(),
        email=f"console-{uuid4().hex[:8]}@example.test",
        password_hash="test-hash",
        full_name="CRYPT Console Test",
        is_active=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)
    return user

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache
from urllib.parse import urlparse
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from pydantic import BaseModel, Field
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    or_,
    create_engine,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from sqlalchemy.exc import IntegrityError
from pwdlib import PasswordHash
from starlette.concurrency import run_in_threadpool
import jwt

import logging
import os
import re
import hashlib
import json
import secrets

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger("think.database")

# ---------------------------------------------------------------------------
# PostgreSQL / SQLAlchemy — Phase 1 and Phase 2
#
# Required:
#   THINK_DATABASE_URL: internal URL using a Think-only Render credential,
#                       pointing to /think_db.
#
# Optional, FIRST BOOTSTRAP ONLY:
#   THINK_DATABASE_ADMIN_URL: same Think-only credential and same PostgreSQL
#                             instance, pointing to /postgres. It is used to
#                             create think_db if it does not exist.
#
# After think_db has been created, remove THINK_DATABASE_ADMIN_URL from
# Render. The application then connects only to THINK_DATABASE_URL.
# ---------------------------------------------------------------------------

Base = declarative_base()
database_engine = None
SessionLocal = None
PASSWORD_HASHER = PasswordHash.recommended()


class ThinkUser(Base):
    __tablename__ = "think_users"

    id = Column(Integer, primary_key=True, index=True)
    think_id = Column(String(32), nullable=False, unique=True, index=True)
    email = Column(String(320), nullable=False, unique=True, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(120), nullable=False)
    phone = Column(String(40), nullable=True)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    wallets = relationship("Wallet", back_populates="user", cascade="all, delete-orphan")
    payments = relationship("PaymentTransaction", back_populates="user")
    withdrawals = relationship("Withdrawal", back_populates="user")
    transfers = relationship("Transfer", back_populates="user")
    subscriptions = relationship("Subscription", back_populates="user")
    notifications = relationship("Notification", back_populates="user", cascade="all, delete-orphan")
    profile = relationship("UserProfile", back_populates="user", uselist=False, cascade="all, delete-orphan")


class UserProfile(Base):
    __tablename__ = "user_profiles"

    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="CASCADE"), primary_key=True)
    username = Column(String(40), nullable=True, unique=True, index=True)
    first_name = Column(String(80), nullable=True)
    last_name = Column(String(80), nullable=True)
    birth_date = Column(String(10), nullable=True)
    country = Column(String(80), nullable=False, default="RDC")
    preferred_currency = Column(String(3), nullable=False, default="CDF")
    address = Column(String(200), nullable=True)
    city = Column(String(100), nullable=True)
    postal_code = Column(String(24), nullable=True)
    preferences = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("ThinkUser", back_populates="profile")


class Wallet(Base):
    __tablename__ = "wallets"
    __table_args__ = (
        UniqueConstraint("user_id", "currency", name="uq_wallet_user_currency"),
        CheckConstraint("available_balance >= 0", name="ck_wallet_available_nonnegative"),
        CheckConstraint("reserved_balance >= 0", name="ck_wallet_reserved_nonnegative"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="CASCADE"), nullable=False, index=True)
    currency = Column(String(3), nullable=False)
    available_balance = Column(Numeric(20, 2), nullable=False, default=0)
    reserved_balance = Column(Numeric(20, 2), nullable=False, default=0)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("ThinkUser", back_populates="wallets")
    ledger_entries = relationship("LedgerEntry", back_populates="wallet")


class LedgerEntry(Base):
    __tablename__ = "ledger_entries"

    id = Column(Integer, primary_key=True, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True)
    entry_type = Column(String(32), nullable=False)
    amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    reference = Column(String(128), nullable=True, unique=True, index=True)
    related_type = Column(String(32), nullable=True)
    related_id = Column(String(64), nullable=True)
    description = Column(Text, nullable=True)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    wallet = relationship("Wallet", back_populates="ledger_entries")


class PaymentTransaction(Base):
    __tablename__ = "payment_transactions"

    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="RESTRICT"), nullable=False, index=True)
    purpose = Column(String(40), nullable=False)
    amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    provider = Column(String(32), nullable=False, default="cinetpay")
    merchant_transaction_id = Column(String(64), nullable=False, unique=True, index=True)
    provider_transaction_id = Column(String(128), nullable=True, index=True)
    idempotency_key = Column(String(128), nullable=True, unique=True)
    status = Column(String(32), nullable=False, default="pending")
    payment_url = Column(Text, nullable=True)
    provider_response = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("ThinkUser", back_populates="payments")


class Withdrawal(Base):
    __tablename__ = "withdrawals"

    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="RESTRICT"), nullable=False, index=True)
    wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount = Column(Numeric(20, 2), nullable=False)
    fee = Column(Numeric(20, 2), nullable=False, default=0)
    net_amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    operator = Column(String(32), nullable=False)
    phone = Column(String(40), nullable=False)
    provider_transaction_id = Column(String(128), nullable=True, index=True)
    status = Column(String(32), nullable=False, default="pending")
    provider_response = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))

    user = relationship("ThinkUser", back_populates="withdrawals")
    wallet = relationship("Wallet")


class MobileMoneyAccount(Base):
    """A user's saved mobile-money destination. Phone data is encrypted at rest."""

    __tablename__ = "mobile_money_accounts"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="CASCADE"), nullable=False, index=True)
    operator = Column(String(24), nullable=False)
    label = Column(String(60), nullable=False, default="Mon numéro")
    phone_ciphertext = Column(String(512), nullable=False)
    phone_masked = Column(String(40), nullable=False)
    is_default = Column(Boolean, nullable=False, default=False)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    updated_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), onupdate=lambda: datetime.now(timezone.utc))


class WithdrawalIdempotency(Base):
    """Durable request-key registry so retries never create a second payout."""

    __tablename__ = "withdrawal_idempotency"
    __table_args__ = (
        UniqueConstraint("user_id", "idempotency_key", name="uq_withdrawal_user_idempotency"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="CASCADE"), nullable=False, index=True)
    idempotency_key = Column(String(128), nullable=False)
    request_fingerprint = Column(String(64), nullable=False)
    withdrawal_id = Column(Integer, ForeignKey("withdrawals.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))


class Transfer(Base):
    __tablename__ = "transfers"

    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String(64), nullable=False, unique=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="RESTRICT"), nullable=False, index=True)
    source_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=True, index=True)
    mode = Column(String(24), nullable=False, default="same")
    agency = Column(String(80), nullable=False)
    recipient = Column(String(80), nullable=True)
    beneficiary = Column(String(120), nullable=True)
    amount = Column(Numeric(20, 2), nullable=False)
    think_fee = Column(Numeric(20, 2), nullable=False, default=0)
    agency_fee = Column(Numeric(20, 2), nullable=False, default=0)
    received_amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(String(32), nullable=False, default="pending")
    idempotency_key = Column(String(128), nullable=True, unique=True)
    reason = Column(Text, nullable=True)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("ThinkUser", back_populates="transfers")
    source_wallet = relationship("Wallet")



class InternalTransfer(Base):
    __tablename__ = "internal_transfers"
    __table_args__ = (
        UniqueConstraint(
            "sender_user_id", "idempotency_key",
            name="uq_internal_transfer_sender_idempotency",
        ),
        CheckConstraint("amount > 0", name="ck_internal_transfer_amount_positive"),
    )

    id = Column(Integer, primary_key=True, index=True)
    public_id = Column(String(64), nullable=False, unique=True, index=True)
    sender_user_id = Column(Integer, ForeignKey("think_users.id", ondelete="RESTRICT"), nullable=False, index=True)
    recipient_user_id = Column(Integer, ForeignKey("think_users.id", ondelete="RESTRICT"), nullable=False, index=True)
    source_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True)
    destination_wallet_id = Column(Integer, ForeignKey("wallets.id", ondelete="RESTRICT"), nullable=False, index=True)
    amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False)
    status = Column(String(32), nullable=False, default="sandbox_completed")
    idempotency_key = Column(String(128), nullable=True)
    reason = Column(Text, nullable=True)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    sender = relationship("ThinkUser", foreign_keys=[sender_user_id])
    recipient = relationship("ThinkUser", foreign_keys=[recipient_user_id])
    source_wallet = relationship("Wallet", foreign_keys=[source_wallet_id])
    destination_wallet = relationship("Wallet", foreign_keys=[destination_wallet_id])


class Subscription(Base):
    __tablename__ = "subscriptions"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan = Column(String(40), nullable=False)
    status = Column(String(24), nullable=False, default="pending")
    amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="EUR")
    payment_transaction_id = Column(Integer, ForeignKey("payment_transactions.id", ondelete="SET NULL"), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("ThinkUser", back_populates="subscriptions")


class LlinkSubscription(Base):
    """Abonnement Llink rattaché à un compte Think et à un paiement CinetPay."""
    __tablename__ = "llink_subscriptions"
    __table_args__ = (
        UniqueConstraint("payment_transaction_id", name="uq_llink_subscription_payment"),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="CASCADE"), nullable=False, index=True)
    plan = Column(String(24), nullable=False)
    status = Column(String(24), nullable=False, default="pending_payment", index=True)
    amount = Column(Numeric(20, 2), nullable=False)
    currency = Column(String(3), nullable=False, default="CDF")
    payment_transaction_id = Column(Integer, ForeignKey("payment_transactions.id", ondelete="SET NULL"), nullable=True)
    starts_at = Column(DateTime(timezone=True), nullable=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("ThinkUser")


class Notification(Base):
    __tablename__ = "notifications"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("think_users.id", ondelete="CASCADE"), nullable=False, index=True)
    title = Column(String(160), nullable=False)
    body = Column(Text, nullable=False)
    category = Column(String(32), nullable=False, default="info")
    is_read = Column(Boolean, nullable=False, default=False)
    extra_data = Column(JSON, nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    user = relationship("ThinkUser", back_populates="notifications")


def _sqlalchemy_url(raw_url: str):
    url = make_url(raw_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    return url


def initialize_database():
    """Create Think's separate database (optional bootstrap) and its ORM tables."""
    think_raw_url = os.getenv("THINK_DATABASE_URL")
    if not think_raw_url:
        raise RuntimeError(
            "THINK_DATABASE_URL is missing. Set it to the Internal Database URL "
            "for the dedicated Think PostgreSQL credential and the think_db database."
        )

    think_url = _sqlalchemy_url(think_raw_url)
    database_name = os.getenv("THINK_DATABASE_NAME", "think_db").strip()
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", database_name):
        raise RuntimeError("THINK_DATABASE_NAME must be a simple PostgreSQL identifier.")

    if think_url.database != database_name:
        raise RuntimeError(
            f"THINK_DATABASE_URL points to database '{think_url.database}', "
            f"but THINK_DATABASE_NAME is '{database_name}'. Use the think_db URL."
        )

    admin_raw_url = os.getenv("THINK_DATABASE_ADMIN_URL")
    if admin_raw_url:
        admin_url = _sqlalchemy_url(admin_raw_url)
        if (admin_url.host, admin_url.port, admin_url.username) != (
            think_url.host, think_url.port, think_url.username
        ):
            raise RuntimeError(
                "THINK_DATABASE_ADMIN_URL and THINK_DATABASE_URL must use the "
                "same Render PostgreSQL instance and the same dedicated Think user."
            )

        # Connect to the maintenance database, not to ClassNet's database.
        admin_url = admin_url.set(database="postgres")
        admin_engine = create_engine(
            admin_url,
            isolation_level="AUTOCOMMIT",
            pool_pre_ping=True,
            pool_size=1,
            max_overflow=0,
            connect_args={"connect_timeout": 10},
        )
        try:
            with admin_engine.connect() as connection:
                exists = connection.execute(
                    text("SELECT 1 FROM pg_database WHERE datname = :database_name"),
                    {"database_name": database_name},
                ).scalar_one_or_none()

                if exists is None:
                    owner_name = connection.execute(text("SELECT current_user")).scalar_one()
                    quoted_database = admin_engine.dialect.identifier_preparer.quote(database_name)
                    quoted_owner = admin_engine.dialect.identifier_preparer.quote(owner_name)
                    connection.exec_driver_sql(
                        f"CREATE DATABASE {quoted_database} OWNER {quoted_owner}"
                    )
                    logger.info("Created Think database '%s' owned by its dedicated role.", database_name)
                else:
                    logger.info("Think database '%s' already exists.", database_name)
        except Exception as exc:
            raise RuntimeError(
                "Unable to bootstrap think_db. Verify that the dedicated Think Render "
                "credential can connect to the 'postgres' maintenance database and has "
                "CREATEDB permission. ClassNet credentials must not be used here."
            ) from exc
        finally:
            admin_engine.dispose()

    think_engine = create_engine(
        think_url,
        pool_pre_ping=True,
        pool_recycle=300,
        pool_size=2,
        max_overflow=0,
        connect_args={"connect_timeout": 10},
    )
    try:
        with think_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
        Base.metadata.create_all(bind=think_engine)
    except Exception:
        think_engine.dispose()
        raise

    logger.info(
        "Think PostgreSQL ready: database='%s', tables=%s",
        database_name,
        ", ".join(sorted(Base.metadata.tables.keys())),
    )
    return think_engine

BASE_DIR = Path(__file__).resolve().parent

app = FastAPI(title="Think API", version="0.2-sandbox")

_cors_origins = [
    origin.strip()
    for origin in os.getenv("CORS_ORIGINS", "*").split(",")
    if origin.strip()
]
_allow_credentials = "*" not in _cors_origins
app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=_allow_credentials,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Idempotency-Key"],
)

# All account, wallet and transaction routes now use SQLAlchemy/PostgreSQL.


@app.on_event("startup")
def startup_database():
    global database_engine, SessionLocal
    database_engine = initialize_database()
    SessionLocal = sessionmaker(
        bind=database_engine,
        autoflush=False,
        autocommit=False,
        expire_on_commit=False,
    )


@app.on_event("shutdown")
def shutdown_database():
    global database_engine
    if database_engine is not None:
        database_engine.dispose()
        database_engine = None


# ---------------------------------------------------------------------------
# Schémas API
# ---------------------------------------------------------------------------

class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)


class RegisterData(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=3, max_length=40)
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=8, max_length=128)
    country: str = Field(default="RDC", max_length=80)
    currency: str = Field(default="CDF", min_length=3, max_length=3)


class ProfilePatch(BaseModel):
    username: str | None = Field(default=None, min_length=3, max_length=40)
    first_name: str | None = Field(default=None, max_length=80)
    last_name: str | None = Field(default=None, max_length=80)
    birth_date: str | None = Field(default=None, max_length=10)
    country: str | None = Field(default=None, max_length=80)
    preferred_currency: str | None = Field(default=None, min_length=3, max_length=3)
    address: str | None = Field(default=None, max_length=200)
    city: str | None = Field(default=None, max_length=100)
    postal_code: str | None = Field(default=None, max_length=24)
    phone: str | None = Field(default=None, max_length=40)
    email: str | None = Field(default=None, min_length=3, max_length=320)
    preferences: dict[str, Any] | None = None


class TransferData(BaseModel):
    mode: str = Field(default="same", max_length=24)
    agency: str = Field(min_length=2, max_length=80)
    agency_slug: str = Field(min_length=2, max_length=40)
    source_agency: str = Field(default="Compte Think", max_length=80)
    source_agency_slug: str | None = Field(default=None, max_length=40)
    recipient: str = Field(min_length=2, max_length=80)
    beneficiary: str = Field(default="Bénéficiaire non renseigné", max_length=120)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    reason: str = Field(default="", max_length=500)



class InternalTransferData(BaseModel):
    recipient_think_id: str = Field(min_length=8, max_length=32)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    reason: str = Field(default="", max_length=500)


class SubscriptionData(BaseModel):
    plan: str = Field(min_length=2, max_length=40)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    method: str = Field(default="mobile", max_length=24)
    agency_slug: str | None = Field(default=None, max_length=40)


class PaymentData(BaseModel):
    purpose: str = Field(default="wallet_deposit", max_length=40)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(default="CDF", min_length=3, max_length=3)
    description: str = Field(default="Dépôt Think", max_length=200)
    payment_method: str | None = Field(default=None, max_length=24)
    mobile_money_account_id: int | None = Field(default=None, gt=0)


class LlinkCheckoutData(BaseModel):
    think_user_id: int = Field(gt=0)
    plan: str = Field(min_length=5, max_length=16)
    payment_method: str = Field(min_length=3, max_length=24)
    checkout_id: str = Field(min_length=8, max_length=64)


class SandboxCreditData(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    reason: str = Field(default="Crédit de test Sandbox", max_length=200)


class WithdrawalData(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    mobile_money_account_id: int | None = Field(default=None, gt=0)
    operator: str | None = Field(default=None, min_length=2, max_length=32)
    phone: str | None = Field(default=None, min_length=6, max_length=40)


class MobileMoneyAccountData(BaseModel):
    operator: str = Field(min_length=2, max_length=24)
    phone: str = Field(min_length=6, max_length=40)
    label: str = Field(default="Mon numéro", min_length=1, max_length=60)
    is_default: bool = False


ALLOWED_CURRENCIES = {
    "CDF", "USD", "EUR", "GBP", "CNY", "INR",
    "BRL", "SAR", "RUB", "BDT", "IDR", "TRY",
}
PLAN_PRICES_EUR = {
    "Standard": Decimal("2.99"),
    "Intermédiaire": Decimal("6.99"),
    "Premium": Decimal("14.99"),
}
THINK_FEE_RATE = Decimal("0.03")
AGENCY_FEE_RATES = {
    "orange": Decimal("0.010"), "airtel": Decimal("0.012"),
    "mpesa": Decimal("0.010"), "mtn": Decimal("0.011"),
    "wise": Decimal("0.008"), "westernunion": Decimal("0.015"),
    "moneygram": Decimal("0.014"), "remitly": Decimal("0.012"),
    "xoom": Decimal("0.012"), "alipay": Decimal("0.008"),
    "wechat": Decimal("0.008"), "paytm": Decimal("0.010"),
}
CENT = Decimal("0.01")


def get_db():
    if SessionLocal is None:
        raise HTTPException(status_code=503, detail="Database is not ready.")
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def _jwt_secret() -> str:
    value = os.getenv("JWT_SECRET", "").strip()
    if len(value) < 32:
        raise HTTPException(status_code=503, detail="JWT_SECRET must be configured in Render (minimum 32 characters).")
    return value


def _llink_sso_secret() -> str:
    value = os.getenv("LLINK_SSO_SECRET", "").strip()
    if len(value) < 32:
        raise HTTPException(
            status_code=503,
            detail="LLINK_SSO_SECRET doit être configuré sur Think et Llink (minimum 32 caractères).",
        )
    return value


def _require_llink_service_token(
    token: str | None = Header(default=None, alias="X-LLINK-SERVICE-TOKEN"),
) -> None:
    expected = os.getenv("LLINK_SERVICE_TOKEN", "").strip()
    if len(expected) < 32:
        raise HTTPException(status_code=503, detail="LLINK_SERVICE_TOKEN n'est pas configuré.")
    if not token or not secrets.compare_digest(token.strip(), expected):
        raise HTTPException(status_code=401, detail="Authentification du service Llink invalide.")


def _llink_plan_prices_cdf() -> dict[str, Decimal]:
    raw = os.getenv("LLINK_PLAN_PRICES_CDF", "").strip()
    if not raw:
        raise HTTPException(
            status_code=503,
            detail="Les tarifs Llink en CDF doivent être configurés dans LLINK_PLAN_PRICES_CDF.",
        )
    try:
        values = json.loads(raw)
    except json.JSONDecodeError:
        raise HTTPException(status_code=503, detail="LLINK_PLAN_PRICES_CDF doit être un objet JSON valide.") from None
    if not isinstance(values, dict):
        raise HTTPException(status_code=503, detail="LLINK_PLAN_PRICES_CDF doit être un objet JSON.")
    required = {"bronze", "silver", "gold", "diamant"}
    if set(values) != required:
        raise HTTPException(
            status_code=503,
            detail="LLINK_PLAN_PRICES_CDF doit définir exactement bronze, silver, gold et diamant.",
        )
    prices: dict[str, Decimal] = {}
    for plan, raw_amount in values.items():
        if isinstance(raw_amount, bool):
            raise HTTPException(status_code=503, detail=f"Tarif Llink invalide pour {plan}.")
        try:
            amount = Decimal(str(raw_amount))
        except Exception:
            raise HTTPException(status_code=503, detail=f"Tarif Llink invalide pour {plan}.") from None
        if not amount.is_finite() or amount != amount.to_integral_value() or amount < 100 or amount > 2_500_000 or int(amount) % 5:
            raise HTTPException(
                status_code=503,
                detail=f"Le tarif CDF de {plan} doit être un entier, multiple de 5, compris entre 100 et 2 500 000.",
            )
        prices[plan] = amount
    return prices


def issue_access_token(user: ThinkUser) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": str(user.id),
        "iat": now,
        "exp": now + timedelta(hours=24),
        "iss": "think-api",
        "aud": "think-app",
    }
    return jwt.encode(payload, _jwt_secret(), algorithm="HS256")


def get_current_user(
    authorization: str | None = Header(default=None),
    db: Session = Depends(get_db),
) -> ThinkUser:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="Authentification requise.")
    token = authorization.split(" ", 1)[1].strip()
    try:
        payload = jwt.decode(
            token, _jwt_secret(), algorithms=["HS256"],
            issuer="think-api", audience="think-app",
            options={"require": ["sub", "exp", "iat"]},
        )
        user_id = int(payload["sub"])
    except (jwt.PyJWTError, KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Session Think invalide ou expirée.")
    user = db.query(ThinkUser).filter(
        ThinkUser.id == user_id, ThinkUser.is_active.is_(True)
    ).first()
    if user is None:
        raise HTTPException(status_code=401, detail="Compte Think introuvable ou désactivé.")
    return user


def notify_user(db: Session, user_id: int, title: str, body: str, category: str = "info"):
    db.add(Notification(user_id=user_id, title=title, body=body, category=category))


def serialize_notification(item: Notification) -> dict[str, Any]:
    return {
        "id": item.id, "title": item.title, "text": item.body,
        "category": item.category, "read": item.is_read,
        "createdAt": item.created_at.isoformat() if item.created_at else None,
    }


def serialize_transfer(item: Transfer) -> dict[str, Any]:
    created = item.created_at.isoformat() if item.created_at else None
    message = (
        f"Demande Sandbox de {item.amount} {item.currency} vers {item.agency}. "
        "Aucun transfert Mobile Money réel n'a été effectué."
    )
    return {
        "id": item.id, "publicId": item.public_id, "status": item.status,
        "mode": item.mode, "agency": item.agency,
        "agencySlug": item.agency.lower().replace(" ", ""),
        "sourceAgency": "Compte Think", "sourceAgencySlug": None,
        "recipient": item.recipient, "beneficiary": item.beneficiary or "",
        "amount": float(item.amount), "currency": item.currency,
        "fee": float(item.think_fee), "agencyFee": float(item.agency_fee),
        "total": float(item.amount), "received": float(item.received_amount),
        "reason": item.reason or "", "createdAt": created,
        "date": item.created_at.strftime("%d/%m/%Y") if item.created_at else "",
        "time": item.created_at.strftime("%H:%M") if item.created_at else "",
        "notificationText": message,
    }



def serialize_internal_transfer(item: InternalTransfer, current_user_id: int) -> dict[str, Any]:
    created = item.created_at.isoformat() if item.created_at else None
    outgoing = item.sender_user_id == current_user_id
    return {
        "id": item.id,
        "public_id": item.public_id,
        "status": item.status,
        "direction": "sent" if outgoing else "received",
        "sender_think_id": item.sender.think_id,
        "sender_name": item.sender.full_name,
        "recipient_think_id": item.recipient.think_id,
        "recipient_name": item.recipient.full_name,
        "amount": float(item.amount),
        "currency": item.currency,
        "fee": 0.0,
        "reason": item.reason or "",
        "created_at": created,
        "no_real_money_moved": True,
    }



def serialize_payment(item: PaymentTransaction) -> dict[str, Any]:
    """Serialize a payment without exposing CinetPay API or notification tokens."""
    created = item.created_at.isoformat() if item.created_at else None
    labels = {
        "accepted": "Paiement confirmé",
        "pending": "Paiement en attente",
        "initializing": "Initialisation du paiement",
        "initialization_unknown": "Confirmation d'initialisation à vérifier",
        "initialization_failed": "Initialisation échouée",
        "failed": "Paiement refusé",
    }
    return {
        "id": item.id,
        "public_id": item.public_id,
        "merchant_transaction_id": item.merchant_transaction_id,
        "provider_transaction_id": item.provider_transaction_id,
        "provider": item.provider,
        "purpose": item.purpose,
        "amount": float(item.amount),
        "currency": item.currency,
        "status": item.status,
        "status_label": labels.get(item.status, item.status),
        "created_at": created,
        "payment_url_available": bool(item.payment_url and item.status in {"pending", "initializing"}),
        "notificationText": f"{labels.get(item.status, item.status)} · {item.amount} {item.currency}",
        "no_real_money": bool((item.provider_response or {}).get("environment") == "sandbox"),
    }


def serialize_user(db: Session, user: ThinkUser) -> dict[str, Any]:
    profile = user.profile
    wallets = db.query(Wallet).filter(Wallet.user_id == user.id).all()
    subscription = (
        db.query(Subscription).filter(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc()).first()
    )
    return {
        "id": user.id, "think_id": user.think_id, "email": user.email,
        "full_name": user.full_name, "phone": user.phone or "",
        "profile": {
            "username": profile.username if profile else "",
            "first_name": profile.first_name if profile else "",
            "last_name": profile.last_name if profile else "",
            "birth_date": profile.birth_date if profile else "",
            "country": profile.country if profile else "RDC",
            "currency": profile.preferred_currency if profile else "CDF",
            "address": profile.address if profile else "",
            "city": profile.city if profile else "",
            "postal_code": profile.postal_code if profile else "",
            "preferences": profile.preferences if profile else {},
        },
        "balances": {w.currency: float(w.available_balance) for w in wallets},
        "reserved_balances": {w.currency: float(w.reserved_balance) for w in wallets},
        "subscription": {
            "plan": subscription.plan if subscription else "",
            "status": subscription.status if subscription else "inactive",
            "expires_at": subscription.expires_at.isoformat() if subscription and subscription.expires_at else None,
            "test": True,
        },
        "created_at": user.created_at.isoformat() if user.created_at else None,
        "test_mode": True,
    }


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def index():
    return FileResponse(BASE_DIR / "app" / "index.html", media_type="text/html")


@app.get("/health")
def health():
    if database_engine is None:
        raise HTTPException(status_code=503, detail="Database not initialized.")
    try:
        with database_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
    except Exception:
        logger.exception("Think database health check failed.")
        raise HTTPException(status_code=503, detail="Database unavailable.")
    return {
        "ok": True,
        "service": "think-api",
        "environment": os.getenv("APP_ENV", "sandbox"),
        "database": "connected",
        "persistence": "PostgreSQL-backed accounts, wallets and sandbox requests",
    }


@app.post("/api/auth/register")
def register(data: RegisterData, db: Session = Depends(get_db)):
    _jwt_secret()  # Fail before creating any rows if the signing secret is missing.
    email = data.email.strip().lower()
    if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
        raise HTTPException(status_code=422, detail="Adresse e-mail invalide.")
    currency = data.currency.upper()
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Devise non prise en charge.")
    if db.query(ThinkUser).filter(ThinkUser.email == email).first():
        raise HTTPException(status_code=409, detail="Un compte existe déjà avec cet e-mail.")

    user = ThinkUser(
        think_id="THK-" + uuid4().hex[:12].upper(),
        email=email,
        password_hash=PASSWORD_HASHER.hash(data.password),
        full_name=data.full_name.strip(),
        phone=data.phone.strip(),
    )
    db.add(user)
    try:
        db.flush()
        db.add(UserProfile(user_id=user.id, country=data.country.strip(), preferred_currency=currency))
        for code in sorted(ALLOWED_CURRENCIES):
            db.add(Wallet(
                user_id=user.id, currency=code,
                available_balance=Decimal("0"), reserved_balance=Decimal("0")
            ))
        notify_user(db, user.id, "Bienvenue sur Think", "Ton compte Sandbox est créé et relié à PostgreSQL.", "welcome")
        db.commit()
        db.refresh(user)
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=409, detail="Adresse e-mail déjà utilisée.")
    return {"access_token": issue_access_token(user), "token_type": "bearer", "user": serialize_user(db, user)}


@app.post("/api/auth/login")
def login(data: Credentials, db: Session = Depends(get_db)):
    _jwt_secret()
    email = data.email.strip().lower()
    user = db.query(ThinkUser).filter(
        ThinkUser.email == email, ThinkUser.is_active.is_(True)
    ).first()
    if user is None or not PASSWORD_HASHER.verify(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="E-mail ou mot de passe incorrect.")
    return {"access_token": issue_access_token(user), "token_type": "bearer", "user": serialize_user(db, user)}


@app.post("/api/auth/logout")
def logout(user: ThinkUser = Depends(get_current_user)):
    # Le client efface son jeton court; aucune opération financière n'est déclenchée ici.
    return {"ok": True}


@app.get("/api/me")
def me(db: Session = Depends(get_db), user: ThinkUser = Depends(get_current_user)):
    return serialize_user(db, user)


@app.post("/api/auth/llink/exchange")
def exchange_llink_sso(user: ThinkUser = Depends(get_current_user)):
    """Émet un jeton SSO à durée courte et limité à Llink après authentification Think."""
    now = datetime.now(timezone.utc)
    token = jwt.encode(
        {
            "sub": str(user.id),
            "think_id": user.think_id,
            "email": user.email,
            "name": user.full_name,
            "scope": "llink",
            "iss": "think-api",
            "aud": "llink-api",
            "iat": now,
            "exp": now + timedelta(minutes=2),
            "jti": uuid4().hex,
        },
        _llink_sso_secret(),
        algorithm="HS256",
    )
    return {
        "sso_token": token,
        "token_type": "bearer",
        "expires_in": 120,
        "user": {"think_id": user.think_id, "email": user.email, "full_name": user.full_name},
    }


@app.patch("/api/profile")
def update_profile(
    data: ProfilePatch,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    values = data.model_dump(exclude_unset=True)
    profile = user.profile
    if profile is None:
        profile = UserProfile(user_id=user.id)
        db.add(profile)
        db.flush()

    phone = values.pop("phone", None)
    if phone is not None:
        user.phone = phone.strip()

    email = values.pop("email", None)
    if email is not None:
        email = email.strip().lower()
        if "@" not in email or "." not in email.rsplit("@", 1)[-1]:
            raise HTTPException(status_code=422, detail="Adresse e-mail invalide.")
        duplicate = db.query(ThinkUser).filter(ThinkUser.email == email, ThinkUser.id != user.id).first()
        if duplicate:
            raise HTTPException(status_code=409, detail="Cette adresse e-mail est déjà utilisée.")
        user.email = email

    preferences = values.pop("preferences", None)
    if preferences is not None:
        profile.preferences = {**(profile.preferences or {}), **preferences}

    for key, value in values.items():
        if hasattr(profile, key):
            setattr(profile, key, value.strip() if isinstance(value, str) else value)

    if profile.preferred_currency:
        profile.preferred_currency = profile.preferred_currency.upper()
        if profile.preferred_currency not in ALLOWED_CURRENCIES:
            raise HTTPException(status_code=422, detail="Devise non prise en charge.")

    if profile.username:
        profile.username = profile.username.strip().lower()

    full_name = " ".join(x for x in [profile.first_name, profile.last_name] if x)
    if full_name:
        user.full_name = full_name[:120]

    db.commit()
    db.refresh(user)
    return serialize_user(db, user)


@app.get("/api/wallet")
def wallet(db: Session = Depends(get_db), user: ThinkUser = Depends(get_current_user)):
    rows = db.query(Wallet).filter(Wallet.user_id == user.id).order_by(Wallet.currency.asc()).all()
    return {
        "balances": {row.currency: float(row.available_balance) for row in rows},
        "reserved_balances": {row.currency: float(row.reserved_balance) for row in rows},
        "currency_count": len(rows),
        "test_mode": True,
    }


@app.post("/api/wallet/sandbox-credit")
def sandbox_credit(
    data: SandboxCreditData,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    if os.getenv("APP_ENV", "sandbox").lower() != "sandbox":
        raise HTTPException(status_code=404, detail="Route disponible uniquement en Sandbox.")
    currency = data.currency.upper()
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Devise non prise en charge.")
    if data.amount > Decimal("100000000"):
        raise HTTPException(status_code=422, detail="Montant de test trop élevé.")
    row = (
        db.query(Wallet)
        .filter(Wallet.user_id == user.id, Wallet.currency == currency)
        .with_for_update()
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="Portefeuille introuvable.")
    row.available_balance = Decimal(row.available_balance) + data.amount
    reference = "SBX-" + uuid4().hex
    db.add(LedgerEntry(
        wallet_id=row.id, entry_type="DEPOSIT", amount=data.amount, currency=currency,
        status="sandbox_confirmed", reference=reference,
        description=data.reason, extra_data={"source": "sandbox_credit", "no_real_money": True},
    ))
    notify_user(
        db, user.id, "Crédit de test ajouté",
        f"{data.amount} {currency} ajoutés au portefeuille Sandbox. Aucun argent réel n'a été reçu.",
        "finance",
    )
    db.commit()
    return {
        "ok": True, "test_mode": True, "no_real_money": True,
        "balance": float(row.available_balance), "currency": currency,
    }


@app.get("/api/notifications")
def notifications(db: Session = Depends(get_db), user: ThinkUser = Depends(get_current_user)):
    rows = (
        db.query(Notification).filter(Notification.user_id == user.id)
        .order_by(Notification.created_at.desc()).limit(100).all()
    )
    return {"items": [serialize_notification(row) for row in rows]}


@app.post("/api/notifications/read-all")
def read_all_notifications(db: Session = Depends(get_db), user: ThinkUser = Depends(get_current_user)):
    db.query(Notification).filter(
        Notification.user_id == user.id, Notification.is_read.is_(False)
    ).update({Notification.is_read: True}, synchronize_session=False)
    db.commit()
    return {"ok": True}


@app.get("/api/mobile-money/accounts")
def list_mobile_money_accounts(
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    rows = (
        db.query(MobileMoneyAccount)
        .filter(MobileMoneyAccount.user_id == user.id, MobileMoneyAccount.is_active.is_(True))
        .order_by(MobileMoneyAccount.is_default.desc(), MobileMoneyAccount.created_at.asc())
        .all()
    )
    return {"items": [_serialize_mobile_money_account(row) for row in rows]}


@app.post("/api/mobile-money/accounts")
def add_mobile_money_account(
    data: MobileMoneyAccountData,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    operator = data.operator.strip().upper()
    if operator not in PAYOUT_OPERATORS_CD:
        raise HTTPException(status_code=422, detail="Opérateur Mobile Money non pris en charge pour les retraits RDC.")
    phone = _normalize_drc_phone(data.phone)
    ciphertext = _encrypt_mobile_phone(phone)
    rows = db.query(MobileMoneyAccount).filter(
        MobileMoneyAccount.user_id == user.id,
        MobileMoneyAccount.is_active.is_(True),
    ).all()
    make_default = data.is_default or not rows
    if make_default:
        for existing in rows:
            existing.is_default = False
    row = MobileMoneyAccount(
        user_id=user.id,
        operator=operator,
        label=data.label.strip() or "Mon numéro",
        phone_ciphertext=ciphertext,
        phone_masked=_mask_mobile_phone(phone),
        is_default=make_default,
        is_active=True,
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {"ok": True, "account": _serialize_mobile_money_account(row)}


@app.post("/api/mobile-money/accounts/{account_id}/default")
def set_default_mobile_money_account(
    account_id: int,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    row = db.query(MobileMoneyAccount).filter(
        MobileMoneyAccount.id == account_id,
        MobileMoneyAccount.user_id == user.id,
        MobileMoneyAccount.is_active.is_(True),
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Numéro Mobile Money introuvable.")
    db.query(MobileMoneyAccount).filter(
        MobileMoneyAccount.user_id == user.id,
        MobileMoneyAccount.is_active.is_(True),
    ).update({MobileMoneyAccount.is_default: False}, synchronize_session=False)
    row.is_default = True
    db.commit()
    return {"ok": True, "account": _serialize_mobile_money_account(row)}


@app.delete("/api/mobile-money/accounts/{account_id}")
def delete_mobile_money_account(
    account_id: int,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    row = db.query(MobileMoneyAccount).filter(
        MobileMoneyAccount.id == account_id,
        MobileMoneyAccount.user_id == user.id,
        MobileMoneyAccount.is_active.is_(True),
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Numéro Mobile Money introuvable.")
    was_default = row.is_default
    row.is_active = False
    row.is_default = False
    if was_default:
        replacement = (
            db.query(MobileMoneyAccount)
            .filter(MobileMoneyAccount.user_id == user.id, MobileMoneyAccount.is_active.is_(True))
            .order_by(MobileMoneyAccount.created_at.asc())
            .first()
        )
        if replacement:
            replacement.is_default = True
    db.commit()
    return {"ok": True, "deleted": True}


@app.get("/api/withdrawals")
def withdrawals(db: Session = Depends(get_db), user: ThinkUser = Depends(get_current_user)):
    rows = (
        db.query(Withdrawal).filter(Withdrawal.user_id == user.id)
        .order_by(Withdrawal.created_at.desc()).limit(100).all()
    )
    return {"items": [_serialize_withdrawal(row) for row in rows]}


@app.post("/api/withdrawals")
def create_withdrawal(
    data: WithdrawalData,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    client = _require_cinetpay_payout_sandbox()
    currency = data.currency.strip().upper()
    if currency != "CDF":
        raise HTTPException(status_code=422, detail="Le PayOut CinetPay configuré pour la RDC utilise actuellement le CDF.")
    amount_decimal = data.amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if amount_decimal != data.amount or amount_decimal != amount_decimal.to_integral_value():
        raise HTTPException(status_code=422, detail="Le montant PayOut CDF doit être un entier.")
    amount = int(amount_decimal)
    if amount < PAYOUT_MIN_CDF or amount > PAYOUT_MAX_CDF:
        raise HTTPException(
            status_code=422,
            detail=f"Le montant doit être un nombre entier compris entre {PAYOUT_MIN_CDF:,} et {PAYOUT_MAX_CDF:,} CDF (plage de validation du SDK installé).",
        )

    mobile_account = None
    if data.mobile_money_account_id:
        mobile_account = db.query(MobileMoneyAccount).filter(
            MobileMoneyAccount.id == data.mobile_money_account_id,
            MobileMoneyAccount.user_id == user.id,
            MobileMoneyAccount.is_active.is_(True),
        ).first()
        if mobile_account is None:
            raise HTTPException(status_code=404, detail="Le numéro Mobile Money sélectionné est introuvable.")
        operator = mobile_account.operator
        phone = _decrypt_mobile_phone(mobile_account.phone_ciphertext)
    else:
        operator = str(data.operator or "").strip().upper()
        if operator not in PAYOUT_OPERATORS_CD:
            raise HTTPException(status_code=422, detail="Choisis Airtel Money, Orange Money ou M-Pesa.")
        phone = _normalize_drc_phone(data.phone or "")

    if operator not in PAYOUT_OPERATORS_CD:
        raise HTTPException(status_code=422, detail="Opérateur PayOut non autorisé pour la RDC.")
    masked_phone = _mask_mobile_phone(phone)

    # Validate the complete SDK request before reserving wallet funds. A local
    # configuration/validation error must never leave a reservation behind.
    public_id = "WDR-" + uuid4().hex[:16].upper()
    try:
        from cinetpay import TransferRequest
        from cinetpay.validation import validate_transfer_request

        notify_url = _cinetpay_payout_notify_url()
        request_data = TransferRequest(
            currency="CDF",
            merchant_transaction_id=public_id,
            phone_number=phone,
            amount=amount,
            payment_method=operator,
            reason="Retrait Think Sandbox",
            notify_url=notify_url,
        )
        validate_transfer_request(request_data)
    except HTTPException:
        raise
    except Exception as exc:
        logger.info("PayOut request rejected locally (%s).", type(exc).__name__)
        raise HTTPException(status_code=422, detail="Paramètres du transfert incompatibles avec le SDK CinetPay installé.") from None

    key = (idempotency_key or ("wdr-" + uuid4().hex)).strip()
    if not key or len(key) > 128:
        raise HTTPException(status_code=422, detail="Clé d'idempotence invalide.")
    fingerprint = hashlib.sha256(
        f"{amount}|{currency}|{operator}|{phone}".encode("utf-8")
    ).hexdigest()
    existing_key = db.query(WithdrawalIdempotency).filter(
        WithdrawalIdempotency.user_id == user.id,
        WithdrawalIdempotency.idempotency_key == key,
    ).first()
    if existing_key:
        if existing_key.request_fingerprint != fingerprint:
            raise HTTPException(status_code=409, detail="Cette clé d'idempotence a déjà été utilisée pour une autre demande.")
        existing_row = db.query(Withdrawal).filter(Withdrawal.id == existing_key.withdrawal_id).first()
        if existing_row is None:
            raise HTTPException(status_code=409, detail="Cette demande est en cours de rapprochement. Consulte l'historique avant de réessayer.")
        return {"ok": True, "withdrawal": _serialize_withdrawal(existing_row), "idempotent_replay": True}

    pending_withdrawal = (
        db.query(Withdrawal)
        .filter(
            Withdrawal.user_id == user.id,
            Withdrawal.status.in_(["payout_initializing", "payout_pending", "payout_unknown"]),
        )
        .order_by(Withdrawal.created_at.desc())
        .first()
    )
    if pending_withdrawal is not None:
        raise HTTPException(
            status_code=409,
            detail=(
                f"Un retrait ({pending_withdrawal.public_id}) est encore en cours de vérification. "
                "Vérifie son statut avant de lancer un nouveau PayOut."
            ),
        )

    wallet_row = (
        db.query(Wallet)
        .filter(Wallet.user_id == user.id, Wallet.currency == currency)
        .with_for_update()
        .first()
    )
    if wallet_row is None:
        raise HTTPException(status_code=404, detail="Portefeuille CDF introuvable.")
    if Decimal(wallet_row.available_balance) < amount_decimal:
        raise HTTPException(
            status_code=409,
            detail=f"Solde insuffisant. Disponible : {wallet_row.available_balance} {currency}.",
        )

    wallet_row.available_balance = Decimal(wallet_row.available_balance) - amount_decimal
    wallet_row.reserved_balance = Decimal(wallet_row.reserved_balance) + amount_decimal
    row = Withdrawal(
        public_id=public_id,
        user_id=user.id,
        wallet_id=wallet_row.id,
        amount=amount_decimal,
        fee=Decimal("0"),
        net_amount=amount_decimal,
        currency=currency,
        operator=operator,
        # Do not keep an unencrypted beneficiary number in the withdrawal row.
        phone=masked_phone,
        status="payout_initializing",
        provider_response={
            "environment": "sandbox",
            "provider": "cinetpay_api_v1",
            "no_real_money": True,
            "recipient_phone_ciphertext": _encrypt_mobile_phone(phone),
            "request_key": key,
        },
    )
    db.add(row)
    try:
        db.flush()
        db.add(LedgerEntry(
            wallet_id=wallet_row.id,
            entry_type="WITHDRAWAL",
            amount=-amount_decimal,
            currency=currency,
            status="reserved",
            reference=public_id,
            related_type="withdrawal",
            related_id=str(row.id),
            description="Montant réservé avant un transfert CinetPay PayOut Sandbox.",
            extra_data={"provider": "cinetpay_api_v1", "test_mode": True, "no_real_money": True},
        ))
        db.add(WithdrawalIdempotency(
            user_id=user.id,
            idempotency_key=key,
            request_fingerprint=fingerprint,
            withdrawal_id=row.id,
        ))
        notify_user(
            db, user.id, "Retrait PayOut en cours",
            f"Le montant de {amount_decimal} CDF est réservé pour le test PayOut vers {operator}. Aucun fonds réel ne sera transféré en Sandbox.",
            "finance",
        )
        db.commit()
        db.refresh(row)
    except IntegrityError:
        db.rollback()
        existing_key = db.query(WithdrawalIdempotency).filter(
            WithdrawalIdempotency.user_id == user.id,
            WithdrawalIdempotency.idempotency_key == key,
        ).first()
        if existing_key and existing_key.request_fingerprint == fingerprint:
            existing_row = db.query(Withdrawal).filter(Withdrawal.id == existing_key.withdrawal_id).first()
            if existing_row:
                return {"ok": True, "withdrawal": _serialize_withdrawal(existing_row), "idempotent_replay": True}
        raise HTTPException(status_code=409, detail="Demande PayOut déjà reçue. Vérifie son statut avant toute nouvelle tentative.") from None

    try:
        provider_response = client.transfer.create(request_data, "CD")
        if provider_response.merchant_transaction_id != row.public_id:
            raise RuntimeError("merchant_transaction_id_mismatch")
        if not provider_response.transaction_id or not provider_response.notify_token:
            raise RuntimeError("missing_provider_transaction_or_notify_token")

        response_data = dict(row.provider_response or {})
        response_data.update({
            "notify_token": provider_response.notify_token,
            "provider_initial_status": str(provider_response.status),
            "provider_initial_code": provider_response.code,
            "provider_fee_amount": str(provider_response.fee_amount or ""),
        })
        row.provider_transaction_id = provider_response.transaction_id
        row.provider_response = response_data
        row.status = "payout_pending"
        db.commit()
        db.refresh(row)

        provider_status = client.transfer.get_status(provider_response.transaction_id, "CD")
        row = _sync_cinetpay_withdrawal_status(db, row, provider_status)
    except HTTPException:
        raise
    except Exception as exc:
        # A timeout may happen after CinetPay has accepted a transfer. Keep funds
        # reserved and require a status lookup by merchant reference; never retry blindly.
        db.refresh(row)
        response_data = dict(row.provider_response or {})
        response_data["initialization_error_type"] = type(exc).__name__
        response_data["last_check_error_at"] = datetime.now(timezone.utc).isoformat()
        row.provider_response = response_data
        if row.status not in {"payout_success", "payout_failed"}:
            row.status = "payout_unknown"
        db.commit()
        logger.warning("CinetPay Sandbox PayOut unresolved for %s (%s).", row.public_id, type(exc).__name__)
        raise HTTPException(
            status_code=502,
            detail=f"CinetPay n'a pas confirmé le retrait. Référence Think : {row.public_id}. Vérifie son statut avant de réessayer.",
        ) from None

    return {"ok": True, "withdrawal": _serialize_withdrawal(row), "test_mode": True, "no_real_money": True}


@app.post("/api/withdrawals/{public_id}/refresh")
def refresh_withdrawal(
    public_id: str,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    row = db.query(Withdrawal).filter(
        Withdrawal.public_id == public_id,
        Withdrawal.user_id == user.id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Demande de retrait introuvable.")
    if row.status in {"payout_success", "payout_failed"}:
        return {"withdrawal": _serialize_withdrawal(row), "final": True}
    client = _require_cinetpay_payout_sandbox()
    identifier = row.provider_transaction_id or row.public_id
    try:
        provider_status = client.transfer.get_status(identifier, "CD")
    except Exception as exc:
        logger.warning("CinetPay PayOut status query unresolved for %s (%s).", row.public_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="Le statut CinetPay reste indisponible. Le montant demeure réservé.") from None
    synced = _sync_cinetpay_withdrawal_status(db, row, provider_status)
    return {"withdrawal": _serialize_withdrawal(synced), "final": synced.status in {"payout_success", "payout_failed"}}


@app.api_route("/api/cinetpay/payout/notify", methods=["GET", "POST"])
async def cinetpay_payout_notify(request: Request, db: Session = Depends(get_db)):
    if request.method == "GET":
        return {"ok": True, "service": "cinetpay-payout-notify"}
    try:
        from cinetpay import parse_notification, verify_notification
        payload = parse_notification(await request.json())
    except ImportError:
        raise HTTPException(status_code=503, detail="Le SDK CinetPay est indisponible.") from None
    except Exception:
        raise HTTPException(status_code=400, detail="Notification PayOut invalide ou incomplète.") from None

    row = db.query(Withdrawal).filter(
        Withdrawal.public_id == payload.merchant_transaction_id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Référence PayOut inconnue.")
    expected_token = str((row.provider_response or {}).get("notify_token") or "")
    if not expected_token or not verify_notification(expected_token, payload.notify_token):
        raise HTTPException(status_code=401, detail="Jeton de notification PayOut invalide.")
    if row.provider_transaction_id and row.provider_transaction_id != payload.transaction_id:
        raise HTTPException(status_code=409, detail="Référence transaction de notification incohérente.")
    if row.status in {"payout_success", "payout_failed"}:
        return {"ok": True, "public_id": row.public_id, "status": row.status, "duplicate": True}

    client = _require_cinetpay_payout_sandbox()
    try:
        provider_status = await run_in_threadpool(client.transfer.get_status, payload.transaction_id, "CD")
    except Exception as exc:
        logger.warning("CinetPay PayOut webhook verification failed for %s (%s).", row.public_id, type(exc).__name__)
        raise HTTPException(status_code=502, detail="La vérification PayOut a échoué. CinetPay pourra renvoyer la notification.") from None
    synced = _sync_cinetpay_withdrawal_status(db, row, provider_status)
    return {"ok": True, "public_id": synced.public_id, "status": synced.status}


@app.get("/api/transfers")
def transfers(db: Session = Depends(get_db), user: ThinkUser = Depends(get_current_user)):
    rows = (
        db.query(Transfer).filter(Transfer.user_id == user.id)
        .order_by(Transfer.created_at.desc()).limit(100).all()
    )
    return {"items": [serialize_transfer(row) for row in rows]}


@app.get("/api/internal-transfers/recipient/{think_id}")
def resolve_internal_transfer_recipient(
    think_id: str,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    """Resolve a public Think ID without exposing private account details."""
    if os.getenv("APP_ENV", "sandbox").lower() != "sandbox":
        raise HTTPException(status_code=404, detail="Les transferts internes Sandbox sont désactivés.")
    normalized = think_id.strip().upper()
    if not re.fullmatch(r"THK-[A-F0-9]{12}", normalized):
        raise HTTPException(status_code=422, detail="Entre un identifiant Think valide (THK-...).")
    recipient = db.query(ThinkUser).filter(
        ThinkUser.think_id == normalized,
        ThinkUser.is_active.is_(True),
    ).first()
    if recipient is None:
        raise HTTPException(status_code=404, detail="Aucun compte Think actif ne correspond à cet identifiant.")
    if recipient.id == user.id:
        raise HTTPException(status_code=400, detail="Tu ne peux pas effectuer un transfert vers ton propre compte.")
    return {
        "recipient": {
            "think_id": recipient.think_id,
            "full_name": recipient.full_name,
        },
        "test_mode": True,
    }


@app.get("/api/internal-transfers")
def internal_transfers(
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    if os.getenv("APP_ENV", "sandbox").lower() != "sandbox":
        return {"items": [], "test_mode": False}
    rows = (
        db.query(InternalTransfer)
        .filter(or_(
            InternalTransfer.sender_user_id == user.id,
            InternalTransfer.recipient_user_id == user.id,
        ))
        .order_by(InternalTransfer.created_at.desc(), InternalTransfer.id.desc())
        .limit(100)
        .all()
    )
    return {
        "items": [serialize_internal_transfer(row, user.id) for row in rows],
        "test_mode": True,
    }


@app.post("/api/internal-transfers")
def create_internal_transfer(
    data: InternalTransferData,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    """Instantly move Sandbox balance between two Think wallets in one DB transaction."""
    if os.getenv("APP_ENV", "sandbox").lower() != "sandbox":
        raise HTTPException(status_code=404, detail="Les transferts internes Sandbox sont désactivés.")

    currency = data.currency.strip().upper()
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Devise non prise en charge.")
    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise HTTPException(status_code=422, detail="Clé d'idempotence invalide.")

    normalized_think_id = data.recipient_think_id.strip().upper()
    if not re.fullmatch(r"THK-[A-F0-9]{12}", normalized_think_id):
        raise HTTPException(status_code=422, detail="Identifiant Think invalide.")

    amount = data.amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if amount < Decimal("1.00"):
        raise HTTPException(status_code=422, detail="Le montant minimum est de 1 unité.")
    if amount > Decimal("100000000"):
        raise HTTPException(status_code=422, detail="Montant Sandbox trop élevé.")
    reason = data.reason.strip()

    def replay_existing(existing: InternalTransfer):
        same_request = (
            existing.recipient.think_id == normalized_think_id
            and Decimal(existing.amount) == amount
            and existing.currency == currency
            and (existing.reason or "") == reason
        )
        if not same_request:
            raise HTTPException(
                status_code=409,
                detail="Cette clé d'idempotence a déjà été utilisée pour une autre demande.",
            )
        return {
            "ok": True,
            "transfer": serialize_internal_transfer(existing, user.id),
            "test_mode": True,
            "idempotent_replay": True,
        }

    if idempotency_key:
        existing = db.query(InternalTransfer).filter(
            InternalTransfer.sender_user_id == user.id,
            InternalTransfer.idempotency_key == idempotency_key,
        ).first()
        if existing:
            return replay_existing(existing)

    recipient = db.query(ThinkUser).filter(
        ThinkUser.think_id == normalized_think_id,
        ThinkUser.is_active.is_(True),
    ).first()
    if recipient is None:
        raise HTTPException(status_code=404, detail="Le bénéficiaire Think est introuvable ou désactivé.")
    if recipient.id == user.id:
        raise HTTPException(status_code=400, detail="Tu ne peux pas effectuer un transfert vers ton propre compte.")

    # Lock both wallet rows in a deterministic order to prevent concurrent
    # transfers from spending the same funds or deadlocking opposite transfers.
    wallet_rows = (
        db.query(Wallet)
        .filter(
            Wallet.user_id.in_([user.id, recipient.id]),
            Wallet.currency == currency,
        )
        .order_by(Wallet.id.asc())
        .with_for_update()
        .all()
    )
    wallets_by_user = {row.user_id: row for row in wallet_rows}
    source_wallet = wallets_by_user.get(user.id)
    destination_wallet = wallets_by_user.get(recipient.id)
    if source_wallet is None or destination_wallet is None:
        raise HTTPException(status_code=404, detail="Portefeuille introuvable pour cette devise.")

    available = Decimal(source_wallet.available_balance)
    if available < amount:
        raise HTTPException(
            status_code=409,
            detail=f"Solde insuffisant. Disponible : {available} {currency}.",
        )

    public_id = "THI-" + uuid4().hex[:16].upper()
    source_wallet.available_balance = available - amount
    destination_wallet.available_balance = Decimal(destination_wallet.available_balance) + amount

    transfer = InternalTransfer(
        public_id=public_id,
        sender_user_id=user.id,
        recipient_user_id=recipient.id,
        source_wallet_id=source_wallet.id,
        destination_wallet_id=destination_wallet.id,
        amount=amount,
        currency=currency,
        status="sandbox_completed",
        idempotency_key=idempotency_key,
        reason=reason,
        extra_data={"test_mode": True, "no_real_money_moved": True},
    )
    db.add(transfer)
    db.flush()

    db.add_all([
        LedgerEntry(
            wallet_id=source_wallet.id,
            entry_type="INTERNAL_TRANSFER_OUT",
            amount=-amount,
            currency=currency,
            status="sandbox_completed",
            reference=public_id + "-OUT",
            related_type="internal_transfer",
            related_id=str(transfer.id),
            description=f"Transfert Sandbox envoyé à {recipient.think_id}.",
            extra_data={"no_real_money_moved": True},
        ),
        LedgerEntry(
            wallet_id=destination_wallet.id,
            entry_type="INTERNAL_TRANSFER_IN",
            amount=amount,
            currency=currency,
            status="sandbox_completed",
            reference=public_id + "-IN",
            related_type="internal_transfer",
            related_id=str(transfer.id),
            description=f"Transfert Sandbox reçu de {user.think_id}.",
            extra_data={"no_real_money_moved": True},
        ),
    ])
    notify_user(
        db, user.id, "Transfert Think envoyé",
        f"Transfert Sandbox {public_id} : {amount} {currency} envoyés à {recipient.full_name} ({recipient.think_id}). Aucun argent réel n'a été déplacé.",
        "finance",
    )
    notify_user(
        db, recipient.id, "Transfert Think reçu",
        f"Tu as reçu {amount} {currency} de {user.full_name} ({user.think_id}) via le Sandbox Think. Aucun argent réel n'a été déplacé.",
        "finance",
    )

    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        if idempotency_key:
            existing = db.query(InternalTransfer).filter(
                InternalTransfer.sender_user_id == user.id,
                InternalTransfer.idempotency_key == idempotency_key,
            ).first()
            if existing:
                return replay_existing(existing)
        raise HTTPException(status_code=409, detail="Le transfert a rencontré un conflit. Vérifie ton historique avant de réessayer.")

    db.refresh(transfer)
    return {
        "ok": True,
        "transfer": serialize_internal_transfer(transfer, user.id),
        "test_mode": True,
        "no_real_money_moved": True,
        "idempotent_replay": False,
    }


@app.post("/api/transfers")
def create_transfer(
    data: TransferData,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    currency = data.currency.upper()
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Devise non prise en charge.")

    if idempotency_key:
        existing = db.query(Transfer).filter(
            Transfer.user_id == user.id, Transfer.idempotency_key == idempotency_key
        ).first()
        if existing:
            return {"ok": True, "transfer": serialize_transfer(existing), "test_mode": True}

    amount = data.amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if amount < Decimal("1.00"):
        raise HTTPException(status_code=422, detail="Le montant minimum est de 1 unité.")

    wallet_row = (
        db.query(Wallet)
        .filter(Wallet.user_id == user.id, Wallet.currency == currency)
        .with_for_update()
        .first()
    )
    if wallet_row is None:
        raise HTTPException(status_code=404, detail="Portefeuille introuvable.")
    if Decimal(wallet_row.available_balance) < amount:
        raise HTTPException(
            status_code=409,
            detail=f"Solde insuffisant. Disponible : {wallet_row.available_balance} {currency}.",
        )

    think_fee = (amount * THINK_FEE_RATE).quantize(CENT, rounding=ROUND_HALF_UP)
    agency_fee = (
        amount * AGENCY_FEE_RATES.get(data.agency_slug.lower(), Decimal("0.01"))
    ).quantize(CENT, rounding=ROUND_HALF_UP)
    received = max(Decimal("0"), amount - think_fee - agency_fee)
    public_id = "THX-" + uuid4().hex[:16].upper()

    # Sandbox request only: reserve the balance, do not claim a Mobile Money payout.
    wallet_row.available_balance = Decimal(wallet_row.available_balance) - amount
    wallet_row.reserved_balance = Decimal(wallet_row.reserved_balance) + amount

    transfer = Transfer(
        public_id=public_id,
        user_id=user.id,
        source_wallet_id=wallet_row.id,
        mode=data.mode,
        agency=data.agency,
        recipient=data.recipient,
        beneficiary=data.beneficiary,
        amount=amount,
        think_fee=think_fee,
        agency_fee=agency_fee,
        received_amount=received,
        currency=currency,
        status="sandbox_pending",
        idempotency_key=idempotency_key,
        reason=data.reason,
        extra_data={"no_real_money_sent": True},
    )
    db.add(transfer)
    db.flush()
    db.add(LedgerEntry(
        wallet_id=wallet_row.id,
        entry_type="TRANSFER_OUT",
        amount=-amount,
        currency=currency,
        status="reserved",
        reference=public_id,
        related_type="transfer",
        related_id=str(transfer.id),
        description="Montant réservé pour une demande de transfert Sandbox.",
        extra_data={"no_real_money_sent": True},
    ))
    notify_user(
        db, user.id, "Demande de transfert préparée",
        f"Demande Sandbox {public_id} : {amount} {currency} vers {data.agency}. "
        "Aucun transfert Mobile Money réel n'a été effectué.",
        "finance",
    )
    db.commit()
    db.refresh(transfer)
    return {
        "ok": True, "transfer": serialize_transfer(transfer),
        "test_mode": True, "no_real_money_sent": True,
    }


@app.get("/api/internal/llink/users/{think_user_id}/subscription")
def get_llink_subscription_internal(
    think_user_id: int,
    _service: None = Depends(_require_llink_service_token),
    db: Session = Depends(get_db),
):
    """Consultation service-à-service des droits Llink ; aucun accès public par numéro seul."""
    user = db.query(ThinkUser).filter(
        ThinkUser.id == think_user_id,
        ThinkUser.is_active.is_(True),
    ).first()
    if user is None:
        raise HTTPException(status_code=404, detail="Compte Think introuvable ou désactivé.")
    now = datetime.now(timezone.utc)
    row = (
        db.query(LlinkSubscription)
        .filter(
            LlinkSubscription.user_id == user.id,
            LlinkSubscription.status == "active",
            LlinkSubscription.expires_at > now,
        )
        .order_by(LlinkSubscription.expires_at.desc(), LlinkSubscription.created_at.desc())
        .first()
    )
    if row is None:
        latest = (
            db.query(LlinkSubscription)
            .filter(LlinkSubscription.user_id == user.id)
            .order_by(LlinkSubscription.created_at.desc())
            .first()
        )
        return {
            "is_active": False,
            "plan": latest.plan if latest else "",
            "status": latest.status if latest else "inactive",
            "expires_at": None,
            "level": "velora",
        }
    level = {"bronze": "satori", "silver": "noetra", "gold": "auren", "diamant": "omnis"}.get(row.plan, "velora")
    return {
        "is_active": True,
        "plan": row.plan,
        "status": row.status,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "level": level,
    }


@app.post("/api/internal/llink/subscriptions/checkout")
def create_llink_subscription_checkout_internal(
    data: LlinkCheckoutData,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    _service: None = Depends(_require_llink_service_token),
    db: Session = Depends(get_db),
):
    """Crée un checkout Llink avec prix calculé côté Think, jamais fourni par le navigateur."""
    user = db.query(ThinkUser).filter(
        ThinkUser.id == data.think_user_id,
        ThinkUser.is_active.is_(True),
    ).first()
    if user is None:
        raise HTTPException(status_code=404, detail="Compte Think introuvable ou désactivé.")
    plan = data.plan.strip().lower()
    prices = _llink_plan_prices_cdf()
    if plan not in prices:
        raise HTTPException(status_code=400, detail="Formule Llink inconnue.")
    payment_method = data.payment_method.strip().upper()
    if payment_method not in {"OM_CD", "AIRTEL_CD", "MPESA_CD", "AFRICELL_CD"}:
        raise HTTPException(status_code=422, detail="Moyen de paiement non pris en charge pour la RDC.")
    checkout_id = data.checkout_id.strip()
    if not re.fullmatch(r"[A-Za-z0-9_-]{8,64}", checkout_id):
        raise HTTPException(status_code=422, detail="Référence de checkout invalide.")
    expected_key = f"llink-{user.id}-{checkout_id}"
    if idempotency_key and idempotency_key.strip() != expected_key:
        raise HTTPException(status_code=409, detail="Clé d'idempotence Llink incohérente.")
    payment_data = PaymentData(
        purpose="llink_subscription",
        amount=prices[plan],
        currency="CDF",
        description=f"Abonnement Llink {plan.title()}",
        payment_method=payment_method,
    )
    result = create_payment(
        payment_data,
        idempotency_key=expected_key,
        db=db,
        user=user,
    )
    payment_row = db.query(PaymentTransaction).filter(
        PaymentTransaction.public_id == result["public_id"],
        PaymentTransaction.user_id == user.id,
    ).first()
    if payment_row is None:
        raise HTTPException(status_code=500, detail="La transaction Think créée est introuvable.")
    sub = db.query(LlinkSubscription).filter(
        LlinkSubscription.payment_transaction_id == payment_row.id
    ).first()
    if sub is None:
        sub = LlinkSubscription(
            user_id=user.id,
            plan=plan,
            status="pending_payment",
            amount=prices[plan],
            currency="CDF",
            payment_transaction_id=payment_row.id,
        )
        db.add(sub)
        db.commit()
    return {
        "ok": True,
        "product": "llink",
        "plan": plan,
        "status": sub.status,
        "subscription_id": sub.id,
        "public_id": payment_row.public_id,
        "merchant_transaction_id": payment_row.merchant_transaction_id,
        "payment_url": result.get("payment_url"),
        "amount": int(prices[plan]),
        "currency": "CDF",
        "expires_at": sub.expires_at.isoformat() if sub.expires_at else None,
        "test_mode": result.get("test_mode", True),
    }


@app.get("/api/subscriptions")
def subscription(db: Session = Depends(get_db), user: ThinkUser = Depends(get_current_user)):
    row = (
        db.query(Subscription).filter(Subscription.user_id == user.id)
        .order_by(Subscription.created_at.desc()).first()
    )
    if row is None:
        return {"plan": "", "status": "inactive", "expires_at": None, "test": True}
    return {
        "plan": row.plan,
        "status": row.status,
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
        "test": True,
    }


@app.post("/api/subscriptions/subscribe")
def subscribe(
    data: SubscriptionData,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    expected = PLAN_PRICES_EUR.get(data.plan)
    if expected is None:
        raise HTTPException(status_code=400, detail="Formule Think inconnue.")
    amount = data.amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if amount != expected:
        raise HTTPException(status_code=400, detail=f"Montant incorrect. Attendu : {expected:.2f} EUR.")
    if os.getenv("APP_ENV", "sandbox").lower() != "sandbox":
        raise HTTPException(status_code=404, detail="Cette simulation est réservée au Sandbox.")

    row = Subscription(
        user_id=user.id,
        plan=data.plan,
        status="pending_payment",
        amount=amount,
        currency="EUR",
    )
    db.add(row)
    notify_user(
        db, user.id, "Abonnement en attente",
        f"La demande {data.plan} est enregistrée. Aucun paiement n'a été encaissé.",
        "account",
    )
    db.commit()
    return {
        "ok": True,
        "status": "pending_payment",
        "subscription": {"plan": data.plan, "status": "pending_payment", "test": True},
        "message": "Demande Sandbox enregistrée. Aucun paiement réel n'a été effectué.",
    }


def _truthy_env(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).strip().lower() in {"1", "true", "yes", "on"}


def _cinetpay_environment_status() -> dict[str, Any]:
    mode = os.getenv("CINETPAY_MODE", "sandbox").strip().lower()
    api_key = os.getenv("CINETPAY_API_KEY_CD", "").strip()
    api_password = os.getenv("CINETPAY_API_PASSWORD_CD", "").strip()
    app_env = os.getenv("APP_ENV", "sandbox").strip().lower()
    enabled = _truthy_env("CINETPAY_ENABLED")
    merchant_verified = _truthy_env("CINETPAY_MERCHANT_VERIFIED")
    payout_enabled = _truthy_env("CINETPAY_PAYOUT_ENABLED")

    ready = enabled and bool(api_key and api_password)
    message = "CinetPay n'est pas encore configuré."
    if not enabled:
        message = "PayIn CinetPay est désactivé. Active CINETPAY_ENABLED après avoir configuré les clés Sandbox."
    elif not api_key or not api_password:
        ready = False
        message = "Les clés API Sandbox du pays CD sont manquantes."
    elif mode == "sandbox":
        if not api_key.startswith("sk_test_"):
            ready = False
            message = "Le mode Sandbox exige une clé CinetPay commençant par sk_test_."
        elif app_env != "sandbox":
            ready = False
            message = "Le Sandbox CinetPay ne peut fonctionner que lorsque APP_ENV=sandbox."
        else:
            message = "PayIn CinetPay Sandbox est prêt."
    elif mode == "production":
        if not merchant_verified:
            ready = False
            message = "La production est bloquée : le compte marchand CRYPT n'est pas encore vérifié."
        elif not _truthy_env("CINETPAY_PRODUCTION_ENABLED"):
            ready = False
            message = "La production est verrouillée. CINETPAY_PRODUCTION_ENABLED doit être activé après validation."
        elif not api_key.startswith("sk_live_"):
            ready = False
            message = "Le mode production exige une clé CinetPay commençant par sk_live_."
        elif app_env != "production":
            ready = False
            message = "La production exige APP_ENV=production."
        else:
            message = "PayIn CinetPay Production est configuré."
    else:
        ready = False
        message = "CINETPAY_MODE doit être sandbox ou production."

    payout_sandbox_enabled = (
        _truthy_env("CINETPAY_PAYOUT_SANDBOX_ENABLED")
        and mode == "sandbox"
        and app_env == "sandbox"
        and api_key.startswith("sk_test_")
        and bool(api_password)
    )

    return {
        "enabled": ready,
        "payin_enabled": ready,
        "payout_enabled": False,  # Real PayOut is never enabled by this sandbox code path.
        "payout_sandbox_enabled": payout_sandbox_enabled,
        "payout_mode": "sandbox_only" if payout_sandbox_enabled else "disabled",
        "mode": mode if mode in {"sandbox", "production"} else "unknown",
        "country": "CD",
        "currency": os.getenv("CINETPAY_CURRENCY", "CDF").strip().upper(),
        "merchant_verified": merchant_verified,
        "payout_authorized": payout_enabled,
        "message": message,
    }


def _require_cinetpay():
    status = _cinetpay_environment_status()
    if not status["payin_enabled"]:
        raise HTTPException(status_code=503, detail=status["message"])
    api_key = os.getenv("CINETPAY_API_KEY_CD", "").strip()
    api_password = os.getenv("CINETPAY_API_PASSWORD_CD", "").strip()
    try:
        from cinetpay import CinetPayClient, ClientConfig, CountryCredentials
    except ImportError:
        raise HTTPException(status_code=503, detail="Le SDK officiel cinetpay-python n'est pas installé.") from None
    return _cached_cinetpay_client(api_key, api_password), status



def _cinetpay_error_log_fields(exc: Exception) -> tuple[str, str, str, str]:
    """Return safe provider error fields without logging credentials or full payloads."""
    error_type = type(exc).__name__
    api_code = getattr(exc, "api_code", "")
    api_status = str(getattr(exc, "api_status", "") or "")[:80]
    description = str(getattr(exc, "description", "") or "")
    description = re.sub(r"\s+", " ", description).strip()
    # AuthenticationError intentionally exposes only a generic SDK message,
    # e.g. "no access_token in response"; include it for diagnosis.
    safe_message = str(exc) if error_type == "AuthenticationError" else ""
    safe_message = re.sub(r"\s+", " ", safe_message).strip()[:180]
    description = re.sub(
        r"(?i)\bsk_(?:test|live)_[A-Za-z0-9_-]+\b",
        "[REDACTED]",
        description,
    )
    description = re.sub(
        r"(?i)\bBearer\s+[^\s,;]+",
        "Bearer [REDACTED]",
        description,
    )
    description = description[:240]
    return error_type, str(api_code)[:32], api_status, description or safe_message


@lru_cache(maxsize=2)
def _cached_cinetpay_client(api_key: str, api_password: str):
    """Keep one SDK client/token cache per credential set for this process."""
    from cinetpay import CinetPayClient, ClientConfig, CountryCredentials

    return CinetPayClient(ClientConfig(
        credentials={"CD": CountryCredentials(api_key=api_key, api_password=api_password)},
        timeout=25.0,
        # Temporary diagnostic logging: the official SDK sanitizes API keys,
        # passwords, and tokens from request logs.
        debug=True,
    ))


def _payment_return_url(public_id: str, result: str) -> str:
    base = os.getenv("THINK_PUBLIC_BASE_URL", "https://think-crypt-entreprise.onrender.com").strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.netloc:
        raise HTTPException(status_code=503, detail="THINK_PUBLIC_BASE_URL doit être une URL HTTPS publique.")
    return f"{base}/api/cinetpay/payin/return?payment={public_id}&result={result}"


def _validate_cinetpay_payment_url(payment_url: str, mode: str) -> None:
    parsed = urlparse(payment_url or "")
    expected_host = "checkout.cinetpay.net" if mode == "sandbox" else "checkout.cinetpay.co"
    if parsed.scheme != "https" or not parsed.hostname or not (
        parsed.hostname == expected_host or parsed.hostname.endswith("." + expected_host)
    ):
        raise HTTPException(status_code=502, detail="CinetPay a retourné une URL de paiement inattendue.")


PAYOUT_OPERATORS_CD = {"AIRTEL_CD", "OM_CD", "MPESA_CD"}
PAYOUT_MIN_CDF = 100
PAYOUT_MAX_CDF = 1_500_000  # Limit imposed by the installed CinetPay Python SDK; confirm merchant-specific cap.


def _mobile_money_fernet() -> Fernet:
    raw_key = os.getenv("MOBILE_MONEY_ENCRYPTION_KEY", "").strip()
    if not raw_key:
        raise HTTPException(
            status_code=503,
            detail="La variable MOBILE_MONEY_ENCRYPTION_KEY doit être configurée pour protéger les numéros Mobile Money.",
        )
    try:
        return Fernet(raw_key.encode("ascii"))
    except (ValueError, UnicodeEncodeError):
        raise HTTPException(status_code=503, detail="MOBILE_MONEY_ENCRYPTION_KEY est invalide.") from None


def _normalize_drc_phone(raw_phone: str) -> str:
    value = re.sub(r"[\s().-]", "", str(raw_phone or ""))
    digits = re.sub(r"\D", "", value)
    if digits.startswith("00243"):
        digits = digits[5:]
    elif digits.startswith("243"):
        digits = digits[3:]
    elif digits.startswith("0"):
        digits = digits[1:]
    if not re.fullmatch(r"\d{9}", digits):
        raise HTTPException(
            status_code=422,
            detail="Numéro RDC invalide. Utilise un numéro à 9 chiffres ou le format international +243…",
        )
    return "+243" + digits


def _mask_mobile_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    return "+243 ••• ••• " + digits[-4:]


def _encrypt_mobile_phone(phone: str) -> str:
    return _mobile_money_fernet().encrypt(phone.encode("utf-8")).decode("ascii")


def _decrypt_mobile_phone(ciphertext: str) -> str:
    try:
        return _mobile_money_fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except (InvalidToken, UnicodeDecodeError, ValueError):
        raise HTTPException(
            status_code=503,
            detail="Impossible de déchiffrer ce numéro Mobile Money. Vérifie la clé MOBILE_MONEY_ENCRYPTION_KEY.",
        ) from None


def _serialize_mobile_money_account(row: MobileMoneyAccount) -> dict[str, Any]:
    return {
        "id": row.id,
        "operator": row.operator,
        "operator_label": {"AIRTEL_CD": "Airtel Money", "OM_CD": "Orange Money", "MPESA_CD": "M-Pesa"}.get(row.operator, row.operator),
        "label": row.label,
        "phone_masked": row.phone_masked,
        "is_default": row.is_default,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _serialize_withdrawal(row: Withdrawal) -> dict[str, Any]:
    state_labels = {
        "payout_initializing": "Initialisation CinetPay",
        "payout_pending": "Transfert en attente",
        "payout_unknown": "Résultat à vérifier",
        "payout_success": "Transfert confirmé (Sandbox)",
        "payout_failed": "Transfert échoué",
        "sandbox_pending": "Ancienne demande simulée",
    }
    phone_display = row.phone or ""
    if phone_display and "•" not in phone_display:
        try:
            phone_display = _mask_mobile_phone(_normalize_drc_phone(phone_display))
        except HTTPException:
            phone_display = "Numéro masqué"
    response_data = row.provider_response or {}
    return {
        "public_id": row.public_id,
        "status": row.status,
        "status_label": state_labels.get(row.status, row.status),
        "operator": row.operator,
        "phone": phone_display,
        "amount": float(row.amount),
        "fee": float(row.fee),
        "provider_fee_amount": str(response_data.get("provider_fee_amount") or ""),
        "net_amount": float(row.net_amount),
        "currency": row.currency,
        "provider_transaction_id": row.provider_transaction_id,
        "test_mode": True,
        "no_real_money": True,
        "created_at": row.created_at.isoformat() if row.created_at else None,
    }


def _require_cinetpay_payout_sandbox():
    status = _cinetpay_environment_status()
    if (
        os.getenv("APP_ENV", "sandbox").strip().lower() != "sandbox"
        or os.getenv("CINETPAY_MODE", "sandbox").strip().lower() != "sandbox"
    ):
        raise HTTPException(status_code=404, detail="PayOut CinetPay réel désactivé : cette route est réservée au Sandbox.")
    if not status.get("payout_sandbox_enabled"):
        raise HTTPException(
            status_code=503,
            detail="PayOut Sandbox désactivé. Configure les clés test puis CINETPAY_PAYOUT_SANDBOX_ENABLED=true.",
        )
    api_key = os.getenv("CINETPAY_API_KEY_CD", "").strip()
    api_password = os.getenv("CINETPAY_API_PASSWORD_CD", "").strip()
    try:
        from cinetpay import CinetPayClient, ClientConfig, CountryCredentials
    except ImportError:
        raise HTTPException(status_code=503, detail="Le SDK CinetPay est indisponible.") from None
    return _cached_cinetpay_client(api_key, api_password)


def _cinetpay_payout_notify_url() -> str:
    base = os.getenv(
        "THINK_PUBLIC_BASE_URL",
        "https://think-crypt-entreprise.onrender.com",
    ).strip().rstrip("/")
    parsed = urlparse(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise HTTPException(status_code=503, detail="THINK_PUBLIC_BASE_URL doit être une URL HTTPS publique valide.")
    url = base + "/api/cinetpay/payout/notify"
    if len(url) > 120:
        raise HTTPException(status_code=503, detail="L'URL de notification PayOut dépasse 120 caractères.")
    return url


def _sync_cinetpay_withdrawal_status(
    db: Session,
    withdrawal: Withdrawal,
    provider_status: Any,
) -> Withdrawal:
    merchant_id = str(getattr(provider_status, "merchant_transaction_id", "") or "")
    provider_id = str(getattr(provider_status, "transaction_id", "") or "")
    provider_state = str(getattr(provider_status, "status", "") or "").upper()

    if merchant_id != withdrawal.public_id:
        raise HTTPException(status_code=409, detail="La référence marchand PayOut ne correspond pas.")
    if not provider_id or (
        withdrawal.provider_transaction_id and provider_id != withdrawal.provider_transaction_id
    ):
        raise HTTPException(status_code=409, detail="La référence transaction PayOut ne correspond pas.")

    provider_amount = str(getattr(provider_status, "amount", "") or "").strip()
    if provider_amount:
        try:
            if Decimal(provider_amount) != Decimal(withdrawal.net_amount):
                raise HTTPException(status_code=409, detail="Le montant retourné par CinetPay ne correspond pas au retrait.")
        except (ValueError, ArithmeticError):
            raise HTTPException(status_code=502, detail="Montant PayOut retourné par CinetPay invalide.") from None

    provider_currency = str(getattr(provider_status, "currency", "") or "").strip().upper()
    if provider_currency and provider_currency != withdrawal.currency:
        raise HTTPException(status_code=409, detail="La devise retournée par CinetPay ne correspond pas au retrait.")

    locked = (
        db.query(Withdrawal)
        .filter(Withdrawal.id == withdrawal.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if locked is None:
        raise HTTPException(status_code=404, detail="Demande de retrait introuvable.")
    if locked.status in {"payout_success", "payout_failed"}:
        return locked

    wallet_row = (
        db.query(Wallet)
        .filter(Wallet.id == locked.wallet_id, Wallet.user_id == locked.user_id)
        .with_for_update()
        .first()
    )
    if wallet_row is None:
        raise HTTPException(status_code=404, detail="Portefeuille de retrait introuvable.")

    response_data = dict(locked.provider_response or {})
    response_data["last_provider_status"] = provider_state
    response_data["last_provider_code"] = getattr(provider_status, "code", None)
    response_data["last_provider_check_at"] = datetime.now(timezone.utc).isoformat()
    response_data["provider_fee_amount"] = str(getattr(provider_status, "fee_amount", "") or "")
    locked.provider_response = response_data
    locked.provider_transaction_id = provider_id

    amount = Decimal(locked.amount)
    if provider_state == "SUCCESS":
        if Decimal(wallet_row.reserved_balance) < amount:
            raise HTTPException(status_code=409, detail="Le solde réservé ne correspond pas à cette demande.")
        wallet_row.reserved_balance = Decimal(wallet_row.reserved_balance) - amount
        ledger = db.query(LedgerEntry).filter(LedgerEntry.reference == locked.public_id).first()
        if ledger is None:
            raise HTTPException(status_code=409, detail="Écriture de réservation du retrait introuvable.")
        ledger.status = "confirmed"
        ledger.description = "Transfert Mobile Money confirmé par CinetPay Sandbox."
        ledger.extra_data = {
            **(ledger.extra_data or {}),
            "provider": "cinetpay_api_v1",
            "provider_transaction_id": provider_id,
            "test_mode": True,
            "no_real_money": True,
        }
        locked.status = "payout_success"
        notify_user(
            db, locked.user_id, "Retrait confirmé (Sandbox)",
            f"Le retrait test {locked.public_id} de {locked.amount} {locked.currency} est confirmé par CinetPay Sandbox. Aucun argent réel n'a été transféré.",
            "finance",
        )
    elif provider_state in {"FAILED", "TRANSACTION_EXIST", "INSUFFICIENT_BALANCE", "USER_NOT_FOUND", "USER_IS_BLOCKED", "EXPIRED", "NOT_ALLOWED"}:
        if Decimal(wallet_row.reserved_balance) < amount:
            raise HTTPException(status_code=409, detail="Le solde réservé ne correspond pas à ce retrait.")
        wallet_row.reserved_balance = Decimal(wallet_row.reserved_balance) - amount
        wallet_row.available_balance = Decimal(wallet_row.available_balance) + amount
        ledger = db.query(LedgerEntry).filter(LedgerEntry.reference == locked.public_id).first()
        if ledger is None:
            raise HTTPException(status_code=409, detail="Écriture de réservation du retrait introuvable.")
        ledger.status = "reversed"
        ledger.description = "Réservation libérée après échec confirmé par CinetPay."
        release_reference = "WDR-REL-" + locked.public_id
        if db.query(LedgerEntry).filter(LedgerEntry.reference == release_reference).first() is None:
            db.add(LedgerEntry(
                wallet_id=wallet_row.id,
                entry_type="WITHDRAWAL_RELEASE",
                amount=amount,
                currency=locked.currency,
                status="confirmed",
                reference=release_reference,
                related_type="withdrawal",
                related_id=str(locked.id),
                description="Montant restitué après échec confirmé du PayOut Sandbox.",
                extra_data={"provider": "cinetpay_api_v1", "no_real_money": True},
            ))
        locked.status = "payout_failed"
        notify_user(
            db, locked.user_id, "Retrait échoué",
            f"Le retrait test {locked.public_id} a échoué. Le montant réservé a été rendu au portefeuille.",
            "finance",
        )
    else:
        locked.status = "payout_pending"

    db.commit()
    db.refresh(locked)
    return locked


@app.get("/api/cinetpay/status")
def cinetpay_status(user: ThinkUser = Depends(get_current_user)):
    return _cinetpay_environment_status()


@app.get("/api/payments")
def list_payments(
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    rows = (
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.user_id == user.id)
        .order_by(PaymentTransaction.created_at.desc())
        .limit(100)
        .all()
    )
    return {"items": [serialize_payment(row) for row in rows]}


def _sync_cinetpay_payment_status(
    db: Session,
    payment: PaymentTransaction,
    provider_status: Any,
) -> PaymentTransaction:
    """Apply only a status fetched directly from CinetPay; never trust webhook status fields."""
    merchant_id = str(getattr(provider_status, "merchant_transaction_id", "") or "")
    provider_id = str(getattr(provider_status, "transaction_id", "") or "")
    status = str(getattr(provider_status, "status", "") or "").upper()

    if merchant_id != payment.merchant_transaction_id:
        raise HTTPException(status_code=409, detail="La référence marchand retournée par CinetPay ne correspond pas.")
    if not provider_id or (
        payment.provider_transaction_id and provider_id != payment.provider_transaction_id
    ):
        raise HTTPException(status_code=409, detail="La référence CinetPay ne correspond pas à cette demande.")

    # Lock and refresh the row: concurrent/repeated webhooks must never credit twice.
    locked = (
        db.query(PaymentTransaction)
        .filter(PaymentTransaction.id == payment.id)
        .with_for_update()
        .populate_existing()
        .first()
    )
    if locked is None:
        raise HTTPException(status_code=404, detail="Transaction Think introuvable.")

    response_data = dict(locked.provider_response or {})
    response_data["last_provider_status"] = status
    response_data["last_provider_check_at"] = datetime.now(timezone.utc).isoformat()
    locked.provider_response = response_data
    locked.provider_transaction_id = provider_id

    if locked.status == "accepted":
        return locked

    if status == "SUCCESS":
        llink_subscription = (
            db.query(LlinkSubscription)
            .filter(LlinkSubscription.payment_transaction_id == locked.id)
            .with_for_update()
            .first()
        )
        if locked.purpose == "llink_subscription":
            if llink_subscription is None:
                raise HTTPException(
                    status_code=409,
                    detail="Abonnement Llink associé à cette transaction introuvable ; aucun crédit portefeuille n'a été effectué.",
                )
            if llink_subscription.status != "active":
                now = datetime.now(timezone.utc)
                previous = (
                    db.query(LlinkSubscription)
                    .filter(
                        LlinkSubscription.user_id == locked.user_id,
                        LlinkSubscription.status == "active",
                        LlinkSubscription.expires_at > now,
                        LlinkSubscription.id != llink_subscription.id,
                    )
                    .order_by(LlinkSubscription.expires_at.desc())
                    .first()
                )
                starts_at = previous.expires_at if previous and previous.expires_at else now
                llink_subscription.status = "active"
                llink_subscription.starts_at = starts_at
                llink_subscription.expires_at = starts_at + timedelta(days=365)
                notify_user(
                    db, locked.user_id, "Abonnement Llink activé",
                    f"Ton abonnement Llink {llink_subscription.plan.title()} est actif jusqu'au {llink_subscription.expires_at.date().isoformat()}.",
                    "account",
                )
            locked.status = "accepted"
        else:
            wallet_row = (
                db.query(Wallet)
                .filter(Wallet.user_id == locked.user_id, Wallet.currency == locked.currency)
                .with_for_update()
                .first()
            )
            if wallet_row is None:
                raise HTTPException(status_code=404, detail="Portefeuille du bénéficiaire introuvable.")
            locked.status = "accepted"
            ledger_reference = "CNPIN-" + locked.public_id
            ledger_exists = db.query(LedgerEntry).filter(LedgerEntry.reference == ledger_reference).first()
            if ledger_exists is None:
                wallet_row.available_balance = Decimal(wallet_row.available_balance) + Decimal(locked.amount)
                db.add(LedgerEntry(
                    wallet_id=wallet_row.id,
                    entry_type="DEPOSIT",
                    amount=Decimal(locked.amount),
                    currency=locked.currency,
                    status="confirmed",
                    reference=ledger_reference,
                    related_type="payment",
                    related_id=str(locked.id),
                    description=f"Dépôt CinetPay confirmé ({locked.merchant_transaction_id}).",
                    extra_data={"provider": "cinetpay_v1", "no_real_money": locked.provider_response.get("environment") == "sandbox"},
                ))
                notify_user(
                    db, locked.user_id, "Dépôt CinetPay confirmé",
                    f"{locked.amount} {locked.currency} ont été ajoutés à ton portefeuille Think.",
                    "finance",
                )
            else:
                locked.status = "accepted"
    elif status in {"FAILED", "TRANSACTION_EXIST", "INSUFFICIENT_BALANCE"}:
        if locked.purpose == "llink_subscription":
            failed_subscription = db.query(LlinkSubscription).filter(
                LlinkSubscription.payment_transaction_id == locked.id
            ).with_for_update().first()
            if failed_subscription is not None and failed_subscription.status != "active":
                failed_subscription.status = "failed"
        locked.status = "failed"
    else:
        # INITIATED/PENDING and unknown transitional statuses remain pending.
        if locked.status not in {"initialization_failed", "failed"}:
            locked.status = "pending"

    db.commit()
    db.refresh(locked)
    return locked


def _fetch_and_sync_cinetpay_payment(db: Session, payment: PaymentTransaction) -> PaymentTransaction:
    client, _ = _require_cinetpay()
    identifier = payment.provider_transaction_id or payment.merchant_transaction_id
    try:
        provider_status = client.payment.get_status(identifier, "CD")
    except Exception as exc:
        error_type, api_code, api_status, description = _cinetpay_error_log_fields(exc)
        logger.warning(
            "CinetPay status check failed for %s: exception_type=%s api_code=%s api_status=%s description=%s",
            payment.public_id,
            error_type,
            api_code or "-",
            api_status or "-",
            description or "-",
        )
        raise HTTPException(status_code=502, detail="Impossible de vérifier le statut CinetPay pour le moment.") from None
    return _sync_cinetpay_payment_status(db, payment, provider_status)


@app.post("/api/payments/create")
def create_payment(
    data: PaymentData,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    client, provider_config = _require_cinetpay()
    currency = data.currency.strip().upper()
    expected_currency = provider_config["currency"]
    if currency != expected_currency or currency != "CDF":
        raise HTTPException(
            status_code=422,
            detail=f"Les dépôts CinetPay pour le compte RDC sont configurés en {expected_currency}.",
        )

    amount_decimal = data.amount.quantize(CENT, rounding=ROUND_HALF_UP)
    if amount_decimal != data.amount or amount_decimal != amount_decimal.to_integral_value():
        raise HTTPException(status_code=422, detail="Le montant CDF doit être un nombre entier.")
    amount = int(amount_decimal)
    if amount < 100 or amount > 2_500_000:
        raise HTTPException(status_code=422, detail="Le montant CinetPay doit être compris entre 100 et 2 500 000 CDF.")
    if amount % 5 != 0:
        raise HTTPException(status_code=422, detail="Le montant CinetPay doit être un multiple de 5.")

    allowed_methods = {"OM_CD", "AIRTEL_CD", "MPESA_CD", "AFRICELL_CD"}
    payment_method = data.payment_method.strip().upper() if data.payment_method else None
    if payment_method and payment_method not in allowed_methods:
        raise HTTPException(status_code=422, detail="Moyen de paiement CinetPay non reconnu pour la RDC.")

    if idempotency_key is not None:
        idempotency_key = idempotency_key.strip()
        if not idempotency_key or len(idempotency_key) > 128:
            raise HTTPException(status_code=422, detail="Clé d'idempotence invalide.")
    else:
        idempotency_key = "payin-" + uuid4().hex

    existing = db.query(PaymentTransaction).filter(
        PaymentTransaction.idempotency_key == idempotency_key
    ).first()
    if existing:
        if existing.user_id != user.id:
            raise HTTPException(status_code=409, detail="Cette clé d'idempotence appartient à une autre session.")
        if (
            Decimal(existing.amount) != amount_decimal
            or existing.currency != currency
            or existing.purpose != data.purpose
        ):
            raise HTTPException(status_code=409, detail="Cette clé d'idempotence a déjà été utilisée pour une autre demande.")
        if existing.payment_url:
            return {
                **serialize_payment(existing),
                "payment_url": existing.payment_url,
                "test_mode": (existing.provider_response or {}).get("environment") == "sandbox",
                "idempotent_replay": True,
            }
        raise HTTPException(
            status_code=409,
            detail="La première initialisation doit être vérifiée avant de créer une nouvelle demande. Utilise une nouvelle clé uniquement après rapprochement.",
        )

    public_id = "PAY-" + uuid4().hex[:16].upper()
    merchant_transaction_id = "THK" + uuid4().hex[:24].upper()
    row = PaymentTransaction(
        public_id=public_id,
        user_id=user.id,
        purpose=data.purpose,
        amount=amount_decimal,
        currency=currency,
        provider="cinetpay_v1",
        merchant_transaction_id=merchant_transaction_id,
        status="initializing",
        idempotency_key=idempotency_key,
        provider_response={"environment": provider_config["mode"]},
    )
    db.add(row)
    db.commit()
    db.refresh(row)

    profile = user.profile
    first_name = (profile.first_name if profile else "") or ""
    last_name = (profile.last_name if profile else "") or ""
    if not first_name.strip() or not last_name.strip():
        parts = user.full_name.strip().split()
        if len(parts) < 2:
            row.status = "initialization_failed"
            row.provider_response = {"environment": provider_config["mode"], "error": "profile_name_incomplete"}
            db.commit()
            raise HTTPException(
                status_code=422,
                detail="Complète ton prénom et ton nom dans ton profil avant de déposer de l'argent avec CinetPay.",
            )
        first_name = first_name.strip() or parts[0]
        last_name = last_name.strip() or " ".join(parts[1:])

    description = re.sub(r"[^A-Za-z0-9À-ÿ .'-]", " ", data.description).strip() or "Depot Think"
    client_phone_number = None
    if data.mobile_money_account_id:
        mobile_account = db.query(MobileMoneyAccount).filter(
            MobileMoneyAccount.id == data.mobile_money_account_id,
            MobileMoneyAccount.user_id == user.id,
            MobileMoneyAccount.is_active.is_(True),
        ).first()
        if mobile_account is None:
            raise HTTPException(status_code=404, detail="Le numéro Mobile Money sélectionné est introuvable.")
        mobile_phone = _decrypt_mobile_phone(mobile_account.phone_ciphertext)
        if payment_method and payment_method != mobile_account.operator:
            raise HTTPException(status_code=422, detail="Le réseau choisi ne correspond pas au numéro Mobile Money sélectionné.")
        payment_method = payment_method or mobile_account.operator
        client_phone_number = mobile_phone
    notify_url = os.getenv(
        "CINETPAY_NOTIFY_URL",
        "https://think-crypt-entreprise.onrender.com/api/cinetpay/payin/notify",
    ).strip()

    try:
        from cinetpay import PaymentRequest

        success_url = _payment_return_url(public_id, "success")
        failed_url = _payment_return_url(public_id, "failed")
        request_data = PaymentRequest(
            currency=currency,
            merchant_transaction_id=merchant_transaction_id,
            amount=amount,
            lang="fr",
            designation=description,
            client_email=user.email,
            client_first_name=first_name.strip(),
            client_last_name=last_name.strip(),
            success_url=success_url,
            failed_url=failed_url,
            notify_url=notify_url,
            channel="PUSH",
            payment_method=payment_method,
            client_phone_number=client_phone_number,
        )
        provider_response = client.payment.initialize(request_data, "CD")
        try:
            _validate_cinetpay_payment_url(provider_response.payment_url, provider_config["mode"])
        except HTTPException:
            # The provider may already have created this payment. Do not allow a blind retry.
            row.status = "initialization_unknown"
            row.provider_response = {
                "environment": provider_config["mode"],
                "error_type": "unexpected_checkout_url",
            }
            db.commit()
            raise
        if not provider_response.notify_token or not provider_response.transaction_id:
            raise RuntimeError("CinetPay response is missing notification or transaction references.")
    except HTTPException as exc:
        if row.status == "initializing":
            row.status = "initialization_failed"
            row.provider_response = {
                "environment": provider_config["mode"],
                "error_type": "local_configuration",
                "error_status": exc.status_code,
            }
            db.commit()
        raise
    except Exception as exc:
        row.status = "initialization_unknown"
        row.provider_response = {
            "environment": provider_config["mode"],
            "error_type": type(exc).__name__,
        }
        db.commit()
        error_type, api_code, api_status, description = _cinetpay_error_log_fields(exc)
        logger.warning(
            "CinetPay payment initialization unresolved for %s: exception_type=%s api_code=%s api_status=%s description=%s",
            public_id,
            error_type,
            api_code or "-",
            api_status or "-",
            description or "-",
        )
        raise HTTPException(
            status_code=502,
            detail=f"CinetPay n'a pas confirmé l'initialisation. Référence Think : {public_id}. Vérifie cette demande avant de réessayer.",
        ) from None

    row.provider_transaction_id = provider_response.transaction_id
    row.payment_url = provider_response.payment_url
    row.status = "pending"
    row.provider_response = {
        "environment": provider_config["mode"],
        "notify_token": provider_response.notify_token,
        "payment_token": provider_response.payment_token,
        "provider_status": provider_response.status,
    }
    db.commit()
    db.refresh(row)
    return {
        **serialize_payment(row),
        "payment_url": row.payment_url,
        "test_mode": provider_config["mode"] == "sandbox",
        "idempotent_replay": False,
    }


@app.get("/api/payments/{public_id}")
def get_payment(
    public_id: str,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    row = db.query(PaymentTransaction).filter(
        PaymentTransaction.public_id == public_id,
        PaymentTransaction.user_id == user.id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Paiement Think introuvable.")
    return serialize_payment(row)


@app.post("/api/payments/{public_id}/refresh")
def refresh_payment(
    public_id: str,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    row = db.query(PaymentTransaction).filter(
        PaymentTransaction.public_id == public_id,
        PaymentTransaction.user_id == user.id,
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Paiement Think introuvable.")
    if row.provider != "cinetpay_v1":
        return serialize_payment(row)
    if row.status not in {"accepted", "failed"}:
        row = _fetch_and_sync_cinetpay_payment(db, row)
    return serialize_payment(row)


@app.api_route("/api/cinetpay/payin/notify", methods=["GET", "POST"])
async def cinetpay_payin_notify(request: Request, db: Session = Depends(get_db)):
    # CinetPay also performs a GET availability ping to the notify URL.
    if request.method == "GET":
        return {"ok": True, "service": "cinetpay-payin-notify"}
    try:
        from cinetpay import parse_notification, verify_notification
        raw_body = await request.json()
        notification = parse_notification(raw_body)
    except ImportError:
        raise HTTPException(status_code=503, detail="Le SDK CinetPay est indisponible.") from None
    except Exception:
        raise HTTPException(status_code=400, detail="Notification CinetPay invalide ou incomplète.") from None

    row = db.query(PaymentTransaction).filter(
        PaymentTransaction.merchant_transaction_id == notification.merchant_transaction_id,
        PaymentTransaction.provider == "cinetpay_v1",
    ).first()
    if row is None:
        raise HTTPException(status_code=404, detail="Référence de paiement Think inconnue.")
    expected_token = str((row.provider_response or {}).get("notify_token") or "")
    if not expected_token or not verify_notification(expected_token, notification.notify_token):
        raise HTTPException(status_code=401, detail="Jeton de notification CinetPay invalide.")
    if row.provider_transaction_id and row.provider_transaction_id != notification.transaction_id:
        raise HTTPException(status_code=409, detail="Référence prestataire incohérente.")
    if row.status == "accepted":
        # A verified payment is immutable; repeated notifications need no provider call.
        return {"ok": True, "public_id": row.public_id, "status": row.status, "duplicate": True}

    client, _ = _require_cinetpay()
    try:
        provider_status = await run_in_threadpool(
            client.payment.get_status, notification.transaction_id, "CD"
        )
    except Exception as exc:
        logger.warning(
            "CinetPay webhook verification failed for %s (%s).",
            row.public_id,
            type(exc).__name__,
        )
        raise HTTPException(status_code=502, detail="La vérification CinetPay a échoué. La notification pourra être renvoyée.") from None
    synced = _sync_cinetpay_payment_status(db, row, provider_status)
    return {"ok": True, "public_id": synced.public_id, "status": synced.status}


@app.get("/api/cinetpay/payin/return")
def cinetpay_payin_return(payment: str, result: str = "returned"):
    # The redirect is informational only; the webhook/provider status check is authoritative.
    safe_result = result if result in {"success", "failed"} else "returned"
    return RedirectResponse(
        url=f"/?cinetpay_payment={payment}&cinetpay_result={safe_result}",
        status_code=303,
    )

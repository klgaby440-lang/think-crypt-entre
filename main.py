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


class SandboxCreditData(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    reason: str = Field(default="Crédit de test Sandbox", max_length=200)


class WithdrawalData(BaseModel):
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(min_length=3, max_length=3)
    operator: str = Field(min_length=2, max_length=32)
    phone: str = Field(min_length=6, max_length=40)


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


@app.get("/api/withdrawals")
def withdrawals(db: Session = Depends(get_db), user: ThinkUser = Depends(get_current_user)):
    rows = (
        db.query(Withdrawal).filter(Withdrawal.user_id == user.id)
        .order_by(Withdrawal.created_at.desc()).limit(100).all()
    )
    return {
        "items": [
            {
                "public_id": row.public_id,
                "status": row.status,
                "operator": row.operator,
                "phone": row.phone,
                "amount": float(row.amount),
                "fee": float(row.fee),
                "net_amount": float(row.net_amount),
                "currency": row.currency,
                "created_at": row.created_at.isoformat() if row.created_at else None,
            }
            for row in rows
        ]
    }


@app.post("/api/withdrawals")
def create_withdrawal(
    data: WithdrawalData,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    if os.getenv("APP_ENV", "sandbox").lower() != "sandbox":
        raise HTTPException(status_code=404, detail="Les demandes de retrait Sandbox sont désactivées hors Sandbox.")

    currency = data.currency.upper()
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Devise non prise en charge.")
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

    fee = (amount * THINK_FEE_RATE).quantize(CENT, rounding=ROUND_HALF_UP)
    net_amount = amount - fee
    public_id = "WDR-" + uuid4().hex[:16].upper()

    wallet_row.available_balance = Decimal(wallet_row.available_balance) - amount
    wallet_row.reserved_balance = Decimal(wallet_row.reserved_balance) + amount
    row = Withdrawal(
        public_id=public_id,
        user_id=user.id,
        wallet_id=wallet_row.id,
        amount=amount,
        fee=fee,
        net_amount=net_amount,
        currency=currency,
        operator=data.operator.strip(),
        phone=data.phone.strip(),
        status="sandbox_pending",
        provider_response={"test_mode": True, "no_real_payout": True},
    )
    db.add(row)
    db.flush()
    db.add(LedgerEntry(
        wallet_id=wallet_row.id,
        entry_type="WITHDRAWAL",
        amount=-amount,
        currency=currency,
        status="reserved",
        reference=public_id,
        related_type="withdrawal",
        related_id=str(row.id),
        description="Montant réservé pour une demande de retrait Sandbox.",
        extra_data={"no_real_payout": True},
    ))
    notify_user(
        db, user.id, "Demande de retrait enregistrée",
        f"Demande Sandbox {public_id} : {amount} {currency} vers {data.operator}. "
        "Aucun paiement Mobile Money réel n'a été envoyé.",
        "finance",
    )
    db.commit()
    db.refresh(row)
    return {
        "ok": True,
        "withdrawal": {
            "public_id": row.public_id,
            "status": row.status,
            "operator": row.operator,
            "phone": row.phone,
            "amount": float(row.amount),
            "fee": float(row.fee),
            "net_amount": float(row.net_amount),
            "currency": row.currency,
        },
        "test_mode": True,
        "no_real_payout": True,
    }


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
    """Expose a safe, secret-free view of CinetPay readiness for Think.

    PayIn can run with test credentials in Sandbox while merchant verification
    is pending. PayOut remains disabled until the merchant and transfer service
    are explicitly confirmed by CinetPay and Think's production gate is opened.
    """
    mode = os.getenv("CINETPAY_MODE", "sandbox").strip().lower()
    api_key = os.getenv("CINETPAY_API_KEY_CD", "").strip()
    api_password = os.getenv("CINETPAY_API_PASSWORD_CD", "").strip()
    app_env = os.getenv("APP_ENV", "sandbox").strip().lower()
    enabled = _truthy_env("CINETPAY_ENABLED")
    production_enabled = _truthy_env("CINETPAY_PRODUCTION_ENABLED")

    # Keep compatibility with the previous boolean settings while exposing
    # the more precise merchant states shown in the CinetPay dashboard.
    merchant_state = os.getenv("CINETPAY_MERCHANT_VERIFICATION_STATUS", "").strip().lower()
    if not merchant_state:
        merchant_state = "verified" if _truthy_env("CINETPAY_MERCHANT_VERIFIED") else "pending"
    if merchant_state not in {"pending", "verified", "rejected", "unknown"}:
        merchant_state = "unknown"
    merchant_verified = merchant_state == "verified"

    payout_state = os.getenv("CINETPAY_PAYOUT_STATUS", "").strip().lower()
    if not payout_state:
        payout_state = "active" if _truthy_env("CINETPAY_PAYOUT_ENABLED") else "unknown"
    if payout_state not in {"unknown", "pending", "active", "inactive", "rejected"}:
        payout_state = "unknown"
    payout_authorized = (
        payout_state == "active" and _truthy_env("CINETPAY_PAYOUT_ENABLED")
    )

    sandbox_state = os.getenv("CINETPAY_SANDBOX_STATUS", "yes").strip().lower()
    sandbox_available = (
        sandbox_state in {"1", "true", "yes", "on"}
        and mode == "sandbox"
        and app_env == "sandbox"
    )

    ready = enabled and bool(api_key and api_password)
    message = "CinetPay n'est pas encore configuré."
    if not enabled:
        message = "PayIn CinetPay est désactivé. Active CINETPAY_ENABLED après avoir configuré les clés Sandbox."
    elif not api_key or not api_password:
        ready = False
        message = "Les clés API CinetPay du pays CD sont manquantes."
    elif mode == "sandbox":
        if not sandbox_available:
            ready = False
            message = "Le Sandbox n'est pas activé ou APP_ENV n'est pas réglé sur sandbox."
        elif not api_key.startswith("sk_test_"):
            ready = False
            message = "Le mode Sandbox exige une clé CinetPay commençant par sk_test_."
        else:
            message = "PayIn CinetPay Sandbox est prêt. La vérification marchande pending ne bloque pas les essais Sandbox."
    elif mode == "production":
        if not merchant_verified:
            ready = False
            message = f"La production est bloquée : vérification marchande {merchant_state}."
        elif not production_enabled:
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

    payout_blockers: list[str] = []
    if not merchant_verified:
        payout_blockers.append(f"Compte marchand non vérifié ({merchant_state}).")
    if not payout_authorized:
        payout_blockers.append(f"Activation PayOut non confirmée ({payout_state}).")
    if mode != "production" or app_env != "production":
        payout_blockers.append("Think est configuré en Sandbox, pas en production.")
    if not production_enabled:
        payout_blockers.append("La bascule de production reste désactivée.")
    if not api_key.startswith("sk_live_"):
        payout_blockers.append("Une clé API de production n'est pas configurée.")
    # Even when CinetPay authorizes transfers, this API deliberately keeps
    # user withdrawals in simulation until a separately reviewed live payout
    # workflow (reservation, provider verification and reversal) is enabled.
    payout_blockers.append("L'exécution PayOut réelle n'est pas activée dans Think.")

    return {
        "enabled": ready,
        "payin_enabled": ready,
        "payout_enabled": False,
        "payout_real_available": False,
        "payout_mode": "simulation_only",
        "payout_status": payout_state,
        "payout_authorized": payout_authorized,
        "payout_blockers": payout_blockers,
        "mode": mode if mode in {"sandbox", "production"} else "unknown",
        "app_environment": app_env,
        "country": "CD",
        "currency": os.getenv("CINETPAY_CURRENCY", "CDF").strip().upper(),
        "merchant_verified": merchant_verified,
        "merchant_verification_status": merchant_state,
        "sandbox_status": "yes" if sandbox_available else "no",
        "sandbox_available": sandbox_available,
        "production_enabled": production_enabled,
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


@lru_cache(maxsize=2)
def _cached_cinetpay_client(api_key: str, api_password: str):
    """Keep one SDK client/token cache per credential set for this process."""
    from cinetpay import CinetPayClient, ClientConfig, CountryCredentials

    return CinetPayClient(ClientConfig(
        credentials={"CD": CountryCredentials(api_key=api_key, api_password=api_password)},
        timeout=25.0,
        debug=False,
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
            # A ledger row already exists: repair the payment status without crediting again.
            locked.status = "accepted"
    elif status in {"FAILED", "TRANSACTION_EXIST", "INSUFFICIENT_BALANCE"}:
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
        logger.warning(
            "CinetPay status check failed for %s (%s).",
            payment.public_id,
            type(exc).__name__,
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
        logger.warning(
            "CinetPay payment initialization unresolved for %s (%s).",
            public_id,
            type(exc).__name__,
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



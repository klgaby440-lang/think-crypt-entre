from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from uuid import uuid4
from pathlib import Path
from secrets import token_urlsafe
from typing import Any

from fastapi import FastAPI, Header, HTTPException, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
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
    create_engine,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, declarative_base, relationship, sessionmaker
from sqlalchemy.exc import IntegrityError
from pwdlib import PasswordHash
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

# ---------------------------------------------------------------------------
# Compatibilité temporaire avec l'ancienne démonstration.
# Les routes qui utilisent USERS/TOKENS ci-dessous restent en mémoire et seront
# migrées vers les modèles SQLAlchemy dans la phase suivante.
# ---------------------------------------------------------------------------

USERS: dict[int, dict[str, Any]] = {}
TOKENS: dict[str, int] = {}
NEXT_USER_ID = 2
NEXT_TRANSFER_ID = 1003
NEXT_PAYMENT_ID = 5001

PLAN_PRICES_EUR = {
    "Standard": 2.99,
    "Intermédiaire": 6.99,
    "Premium": 14.99,
}

THINK_FEE_RATE = 0.03
AGENCY_FEE_RATES = {
    "orange": 0.010,
    "airtel": 0.012,
    "mpesa": 0.010,
    "mtn": 0.011,
    "wise": 0.008,
    "westernunion": 0.015,
    "moneygram": 0.014,
    "remitly": 0.012,
    "xoom": 0.012,
    "alipay": 0.008,
    "wechat": 0.008,
    "paytm": 0.010,
}


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def make_think_id() -> str:
    return "THK-" + token_urlsafe(8).replace("-", "A").replace("_", "B").upper()[:12]


def hashless_test_password(password: str) -> str:
    # Serveur de test uniquement : on ne veut pas introduire de dépendance de
    # hash ici. Ce mécanisme ne doit jamais être repris en production.
    return password


def user_public(user: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": user["id"],
        "think_id": user["think_id"],
        "email": user["email"],
        "full_name": user["full_name"],
        "phone": user.get("phone", ""),
        "balances": dict(user["balances"]),
        "subscription": dict(user["subscription"]),
        "transfers": list(user["transfers"]),
        "notifications": list(user["notifications"]),
        "created_at": user["created_at"],
        "test_mode": True,
    }


def add_notification(user: dict[str, Any], title: str, text: str, category: str = "info") -> dict[str, Any]:
    item = {
        "id": int(datetime.now().timestamp() * 1000),
        "title": title,
        "text": text,
        "category": category,
        "read": False,
        "created_at": now_iso(),
    }
    user["notifications"].append(item)
    user["notifications"] = user["notifications"][-100:]
    return item


def add_transfer(user: dict[str, Any], *, mode: str, agency: str, agency_slug: str,
                 source_agency: str, source_agency_slug: str | None, recipient: str,
                 beneficiary: str, amount: float, currency: str, reason: str) -> dict[str, Any]:
    global NEXT_TRANSFER_ID

    if amount < 1:
        raise HTTPException(400, "Le montant minimum d'un transfert est de 1 unité.")

    available = float(user["balances"].get(currency, 0))
    if amount > available:
        raise HTTPException(400, f"Solde insuffisant en {currency}. Disponible : {available:.2f}.")

    think_fee = round(amount * THINK_FEE_RATE, 2)
    agency_rate = AGENCY_FEE_RATES.get(agency_slug, 0.01)
    agency_fee = round(amount * agency_rate, 2)
    received = round(max(0, amount - think_fee - agency_fee), 2)

    user["balances"][currency] = round(available - amount, 2)
    created = now_iso()
    dt = datetime.fromisoformat(created.replace("Z", "+00:00"))
    date = dt.astimezone().strftime("%d/%m/%Y")
    time = dt.astimezone().strftime("%H:%M")
    target = recipient if recipient != "—" else "numéro non renseigné"
    beneficiary_part = f" ({beneficiary})" if beneficiary and beneficiary != "Bénéficiaire non renseigné" else ""
    notification = (
        f"Vous avez envoyé {received:.2f} {currency} le {date} à {time} à {target}{beneficiary_part}. "
        f"Frais Think : {think_fee:.2f} {currency} · Frais {agency} : {agency_fee:.2f} {currency}."
    )

    record = {
        "id": NEXT_TRANSFER_ID,
        "status": "completed_test",
        "mode": mode,
        "agency": agency,
        "agencySlug": agency_slug,
        "sourceAgency": source_agency,
        "sourceAgencySlug": source_agency_slug,
        "recipient": target,
        "beneficiary": beneficiary or "Bénéficiaire non renseigné",
        "amount": amount,
        "currency": currency,
        "fee": think_fee,
        "agencyFee": agency_fee,
        "total": amount,
        "received": received,
        "reason": reason,
        "createdAt": created,
        "date": date,
        "time": time,
        "notificationText": notification,
    }
    NEXT_TRANSFER_ID += 1
    user["transfers"].append(record)
    user["transfers"] = user["transfers"][-100:]
    add_notification(user, "Transfert effectué", notification, "transfer")
    return record


def seed() -> None:
    demo = {
        "id": 1,
        "think_id": "THK-DEMO000001",
        "email": "demo@think.test",
        "password": hashless_test_password("ThinkDemo123!"),
        "full_name": "Compte Démo Think",
        "phone": "+243900000000",
        "created_at": now_iso(),
        "balances": {
            "CDF": 125000.00,
            "USD": 250.00,
            "EUR": 50.00,
            "GBP": 20.00,
            "CNY": 300.00,
            "INR": 1000.00,
            "BRL": 100.00,
            "SAR": 200.00,
            "RUB": 2000.00,
            "BDT": 5000.00,
            "IDR": 500000.00,
            "TRY": 100.00,
        },
        "subscription": {
            "plan": "Standard",
            "status": "active",
            "expires_at": (datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
            "test": True,
        },
        "transfers": [],
        "notifications": [],
    }
    USERS[1] = demo

    # Données de test visibles immédiatement dans l'historique.
    add_transfer(
        demo, mode="same", agency="Orange Money", agency_slug="orange",
        source_agency="Compte Think", source_agency_slug=None,
        recipient="+243812345678", beneficiary="Jean Test", amount=25000,
        currency="CDF", reason="Test historique"
    )
    add_transfer(
        demo, mode="other", agency="M-Pesa", agency_slug="mpesa",
        source_agency="Orange Money", source_agency_slug="orange",
        recipient="+243898765432", beneficiary="Amina Test", amount=25,
        currency="USD", reason="Test USD"
    )
    # On remet une partie du solde après le seed historique afin que le compte
    # reste confortable pour les tests interactifs.
    demo["balances"]["CDF"] += 25000
    demo["balances"]["USD"] += 25
    add_notification(demo, "Bienvenue sur Think", "Le compte de démonstration est prêt à être utilisé.", "welcome")
    add_notification(demo, "Abonnement actif", "La formule Standard est active dans le serveur de test.", "subscription")


# No demo user or demo balance is seeded; accounts are created through the API.

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


class SubscriptionData(BaseModel):
    plan: str = Field(min_length=2, max_length=40)
    amount: Decimal = Field(gt=0, max_digits=12, decimal_places=2)
    method: str = Field(default="mobile", max_length=24)
    agency_slug: str | None = Field(default=None, max_length=40)


class PaymentData(BaseModel):
    purpose: str = Field(default="wallet_deposit", max_length=40)
    amount: Decimal = Field(gt=0, max_digits=20, decimal_places=2)
    currency: str = Field(default="CDF", min_length=3, max_length=3)
    description: str = Field(default="Paiement Think", max_length=200)


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


@app.post("/api/payments/create")
def create_payment(
    data: PaymentData,
    db: Session = Depends(get_db),
    user: ThinkUser = Depends(get_current_user),
):
    currency = data.currency.upper()
    if currency not in ALLOWED_CURRENCIES:
        raise HTTPException(status_code=422, detail="Devise non prise en charge.")
    row = PaymentTransaction(
        public_id="PAY-" + uuid4().hex[:16].upper(),
        user_id=user.id,
        purpose=data.purpose,
        amount=data.amount.quantize(CENT, rounding=ROUND_HALF_UP),
        currency=currency,
        provider="sandbox",
        merchant_transaction_id="THINK-" + uuid4().hex[:20].upper(),
        status="pending_sandbox",
        provider_response={"test_mode": True, "no_real_payment": True},
    )
    db.add(row)
    db.commit()
    db.refresh(row)
    return {
        "public_id": row.public_id,
        "status": row.status,
        "amount": float(row.amount),
        "currency": row.currency,
        "purpose": row.purpose,
        "description": data.description,
        "payment_url": None,
        "test_mode": True,
        "no_real_payment": True,
        "message": "Demande enregistrée. L'intégration CinetPay PAYIN sera ajoutée dans la phase suivante.",
    }

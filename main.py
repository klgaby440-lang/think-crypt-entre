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


seed()

@app.on_event("startup")
def startup_database():
    global database_engine
    database_engine = initialize_database()


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
    email: str
    password: str


class RegisterData(BaseModel):
    full_name: str = Field(min_length=2, max_length=120)
    phone: str = Field(min_length=3, max_length=40)
    email: str
    password: str = Field(min_length=8, max_length=128)


class TransferData(BaseModel):
    mode: str = "same"
    agency: str
    agency_slug: str
    source_agency: str = "Compte Think"
    source_agency_slug: str | None = None
    recipient: str
    beneficiary: str = "Bénéficiaire non renseigné"
    amount: float
    currency: str
    reason: str = ""


class SubscriptionData(BaseModel):
    plan: str
    amount: float
    method: str = "mobile"
    agency_slug: str | None = None


class PaymentData(BaseModel):
    purpose: str = "subscription"
    amount: float
    currency: str = "EUR"
    description: str = "Paiement Think"


def current_user(authorization: str | None) -> dict[str, Any]:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(401, "Authentification requise.")
    token = authorization.split(" ", 1)[1].strip()
    user_id = TOKENS.get(token)
    if not user_id or user_id not in USERS:
        raise HTTPException(401, "Session Think invalide ou expirée.")
    return USERS[user_id]


def issue_token(user: dict[str, Any]) -> str:
    token = "test_" + token_urlsafe(24)
    TOKENS[token] = user["id"]
    return token


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
        "persistence": "schema-ready; legacy demo routes still use in-memory data",
    }


@app.post("/api/auth/login")
def login(data: Credentials):
    user = next((u for u in USERS.values() if u["email"].lower() == data.email.lower()), None)
    if not user or user["password"] != data.password:
        raise HTTPException(401, "E-mail ou mot de passe incorrect.")
    return {"access_token": issue_token(user), "token_type": "bearer", "think_id": user["think_id"]}


@app.post("/api/auth/register")
def register(data: RegisterData):
    if any(u["email"].lower() == data.email.lower() for u in USERS.values()):
        raise HTTPException(409, "Un compte existe déjà avec cet e-mail.")
    global NEXT_USER_ID
    user = {
        "id": NEXT_USER_ID,
        "think_id": make_think_id(),
        "email": data.email,
        "password": hashless_test_password(data.password),
        "full_name": data.full_name,
        "phone": data.phone,
        "created_at": now_iso(),
        "balances": {"CDF": 0.0, "USD": 0.0, "EUR": 0.0, "GBP": 0.0, "CNY": 0.0, "INR": 0.0, "BRL": 0.0, "SAR": 0.0, "RUB": 0.0, "BDT": 0.0, "IDR": 0.0, "TRY": 0.0},
        "subscription": {"plan": "", "status": "inactive", "expires_at": None, "test": True},
        "transfers": [],
        "notifications": [],
    }
    USERS[NEXT_USER_ID] = user
    NEXT_USER_ID += 1
    add_notification(user, "Bienvenue sur Think", "Ton compte de test a été créé.", "welcome")
    return {"access_token": issue_token(user), "token_type": "bearer", "think_id": user["think_id"]}


@app.get("/api/me")
def me(authorization: str | None = Header(default=None)):
    return user_public(current_user(authorization))


@app.get("/api/wallet")
def wallet(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    return {"balances": dict(user["balances"]), "currency_count": len(user["balances"]), "test_mode": True}


@app.get("/api/transfers")
def transfers(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    return {"items": list(user["transfers"])}


@app.post("/api/transfers")
def create_transfer(data: TransferData, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    transfer = add_transfer(
        user,
        mode=data.mode,
        agency=data.agency,
        agency_slug=data.agency_slug,
        source_agency=data.source_agency,
        source_agency_slug=data.source_agency_slug,
        recipient=data.recipient,
        beneficiary=data.beneficiary,
        amount=data.amount,
        currency=data.currency,
        reason=data.reason,
    )
    return {"ok": True, "transfer": transfer, "user": user_public(user)}


@app.get("/api/subscriptions")
def subscription(authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    return dict(user["subscription"])


@app.post("/api/subscriptions/subscribe")
def subscribe(data: SubscriptionData, authorization: str | None = Header(default=None)):
    user = current_user(authorization)
    expected = PLAN_PRICES_EUR.get(data.plan)
    if expected is None:
        raise HTTPException(400, "Formule Think inconnue.")
    if abs(float(data.amount) - expected) > 0.005:
        raise HTTPException(400, f"Montant incorrect pour {data.plan}. Attendu : {expected:.2f} EUR.")

    expires = datetime.now(timezone.utc) + timedelta(days=30)
    user["subscription"] = {
        "plan": data.plan,
        "status": "active",
        "expires_at": expires.isoformat(),
        "test": True,
        "payment_method": data.method,
        "agency_slug": data.agency_slug,
    }
    add_notification(user, "Abonnement activé", f"La formule {data.plan} a été activée sur le serveur de test.", "subscription")
    return {"ok": True, "payment": {"status": "succeeded_test", "amount": expected, "currency": "EUR"}, "subscription": dict(user["subscription"]), "user": user_public(user)}


@app.post("/api/payments/create")
def create_payment(data: PaymentData, authorization: str | None = Header(default=None)):
    global NEXT_PAYMENT_ID
    user = current_user(authorization)
    payment_id = f"TESTPAY-{NEXT_PAYMENT_ID}"
    NEXT_PAYMENT_ID += 1
    return {
        "public_id": payment_id,
        "status": "pending_test",
        "amount": data.amount,
        "currency": data.currency,
        "purpose": data.purpose,
        "description": data.description,
        "payment_url": f"/test-checkout/{payment_id}",
        "test_mode": True,
        "user": user["think_id"],
    }


@app.get("/test-checkout/{payment_id}", response_class=HTMLResponse)
def fake_checkout(payment_id: str):
    return HTMLResponse(f"""
    <!doctype html><html lang='fr'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>
    <title>Think · Checkout test</title><style>body{{font-family:system-ui;display:grid;place-items:center;min-height:100vh;background:#f5f3fb;color:#17171c}}main{{background:white;padding:30px;border-radius:22px;max-width:440px;box-shadow:0 20px 60px #0001}}code{{background:#f0eef8;padding:4px 7px;border-radius:7px}}</style></head>
    <body><main><h1>Checkout Think — TEST</h1><p>Simulation locale du prestataire.</p><p>Référence : <code>{payment_id}</code></p><p>Aucun argent réel ne sera débité.</p><button onclick="history.back()">Retour à Think</button></main></body></html>
    """)

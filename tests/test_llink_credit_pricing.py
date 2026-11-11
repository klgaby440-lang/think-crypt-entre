import os
from decimal import Decimal

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("THINK_SSO_SECRET", "test-sso-secret-" + ("x" * 48))
os.environ.setdefault("THINK_SERVICE_TOKEN", "test-service-token-" + ("y" * 48))

import pytest
from fastapi import HTTPException

import main


def test_think_credit_price_preserves_one_cent_reference_value():
    assert main._llink_credit_price_cdf(100, Decimal("2800")) == Decimal("2800")


def test_think_credit_purchase_rejects_amount_below_cinetpay_minimum():
    assert main._llink_minimum_credit_purchase(Decimal("2800")) == 4
    for credits in (1, 2, 3):
        with pytest.raises(HTTPException) as exc:
            main._llink_credit_price_cdf(credits, Decimal("2800"))
        assert exc.value.status_code == 422
        assert "au moins 4 crédits" in exc.value.detail


def test_think_credit_purchase_rounds_up_without_artificial_minimum_price():
    # 4 crédits valent 112 CDF au taux de 2 800 CDF/USD ; CinetPay exige
    # un multiple de 5, donc 115 CDF. Le prix n'est pas remplacé par 100 CDF.
    assert main._llink_credit_price_cdf(4, Decimal("2800")) == Decimal("115")
    assert main._llink_credit_price_cdf(5, Decimal("2800")) == Decimal("140")


def test_think_credit_catalog_exposes_only_the_supported_online_wallet(monkeypatch):
    monkeypatch.setenv("LLINK_CDF_PER_USD", "2800")
    catalog = main.list_llink_credit_catalog()
    assert catalog["credit_usd"] == "0.01"
    assert catalog["minimum_credits"] == 4
    assert catalog["maximum_credits"] == 89285
    assert catalog["wallet_types"] == ["online"]
    assert catalog["offline_available"] is False


def test_think_credit_price_rejects_amount_above_cinetpay_limit():
    with pytest.raises(HTTPException) as exc:
        main._llink_credit_price_cdf(500_000, Decimal("2800"))
    assert exc.value.status_code == 422

import os
from decimal import Decimal

os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault("THINK_SSO_SECRET", "test-sso-secret-" + ("x" * 48))
os.environ.setdefault("THINK_SERVICE_TOKEN", "test-service-token-" + ("y" * 48))

import pytest
from fastapi import HTTPException

from main import _llink_credit_price_cdf


def test_think_credit_price_rounds_cdf_to_multiple_of_five():
    assert _llink_credit_price_cdf(100, Decimal("2800")) == Decimal("2800")
    assert _llink_credit_price_cdf(1, Decimal("2800")) == Decimal("100")


def test_think_credit_price_rejects_out_of_range_amount():
    with pytest.raises(HTTPException) as exc:
        _llink_credit_price_cdf(500_000, Decimal("100000"))
    assert exc.value.status_code == 422

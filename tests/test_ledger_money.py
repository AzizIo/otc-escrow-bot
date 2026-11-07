from decimal import Decimal

import pytest

from app.services.ledger import money


def test_money_quantizes_to_cents():
    assert money("12.345") == Decimal("12.35")


def test_money_rejects_zero_and_negative():
    with pytest.raises(ValueError):
        money("0")
    with pytest.raises(ValueError):
        money("-1")

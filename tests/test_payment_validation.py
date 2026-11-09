from decimal import Decimal

import pytest

from app.services.payment_validation import invoice_matches_deposit


def test_accepts_exact_paid_usdt_crypto_invoice():
    invoice = {
        "status": "paid",
        "currency_type": "crypto",
        "asset": "USDT",
        "amount": "25.00",
    }

    assert invoice_matches_deposit(invoice, Decimal("25.00")) is True


@pytest.mark.parametrize(
    "changes",
    [
        {"status": "active"},
        {"currency_type": "fiat"},
        {"asset": "TON"},
        {"amount": "24.99"},
        {"amount": "NaN"},
        {"amount": "Infinity"},
        {"amount": "not-a-number"},
    ],
)
def test_rejects_invoice_with_mismatched_or_invalid_payment_data(changes):
    invoice = {
        "status": "paid",
        "currency_type": "crypto",
        "asset": "USDT",
        "amount": "25.00",
        **changes,
    }

    assert invoice_matches_deposit(invoice, Decimal("25.00")) is False


def test_rejects_missing_payment_fields():
    assert invoice_matches_deposit({}, Decimal("25.00")) is False


def test_rejects_amount_with_extra_precision_even_when_rounded_value_matches():
    invoice = {
        "status": "paid",
        "currency_type": "crypto",
        "asset": "USDT",
        "amount": "25.001",
    }

    assert invoice_matches_deposit(invoice, Decimal("25.00")) is False

from decimal import Decimal


def invoice_matches_deposit(invoice: dict, expected_amount: Decimal) -> bool:
    """Return True only for a paid USDT crypto invoice with the exact expected amount."""
    try:
        received_amount = Decimal(str(invoice.get("amount", "0")))
    except (ArithmeticError, TypeError, ValueError):
        return False

    return (
        invoice.get("status") == "paid"
        and invoice.get("currency_type") == "crypto"
        and invoice.get("asset") == "USDT"
        and received_amount.is_finite()
        and received_amount == expected_amount
    )

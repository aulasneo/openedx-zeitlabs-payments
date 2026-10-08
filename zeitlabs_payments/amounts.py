"""Exact monetary conversions for provider serialization boundaries."""

from decimal import Decimal, InvalidOperation


def quantize_amount(amount: Decimal, decimal_places: int) -> Decimal:
    """
    Set provider precision without changing the monetary value.

    Providers must select the precision from their API/currency contract.
    Reject unsupported precision instead of silently rounding a checkout total.
    """
    if not isinstance(amount, Decimal):
        raise TypeError('Payment amounts must be Decimal values.')
    if not amount.is_finite() or amount < 0:
        raise ValueError('Payment amounts must be finite and non-negative.')
    if isinstance(decimal_places, bool) or not isinstance(decimal_places, int) or decimal_places < 0:
        raise ValueError('Currency decimal places must be a non-negative integer.')
    try:
        quantized = amount.quantize(Decimal(1).scaleb(-decimal_places))
    except InvalidOperation as exc:
        raise ValueError('Payment amount cannot be represented at provider precision.') from exc
    if quantized != amount:
        raise ValueError('Payment amount exceeds provider precision.')
    return quantized


def amount_to_minor_units(amount: Decimal, decimal_places: int) -> int:
    """Convert exact major units to an integer before signing/submitting a payment."""
    return int(quantize_amount(amount, decimal_places).scaleb(decimal_places))

"""Exact monetary conversions for provider serialization boundaries."""

from decimal import Decimal, Inexact, InvalidOperation, Rounded, localcontext


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
        with localcontext() as context:
            context.prec = max(
                context.prec,
                len(amount.as_tuple().digits),
                amount.adjusted() + decimal_places + 1,
                1,
            )
            context.Emax = max(context.Emax, amount.adjusted() + decimal_places + 1)
            context.Emin = min(context.Emin, -decimal_places)
            # Detect lost precision with the value comparison below, independent
            # of the caller's rounding and signal traps.
            context.traps[Inexact] = False
            context.traps[Rounded] = False
            quantum = Decimal((0, (1,), -decimal_places))
            quantized = amount.quantize(quantum)
    except InvalidOperation as exc:
        raise ValueError('Payment amount cannot be represented at provider precision.') from exc
    if quantized != amount:
        raise ValueError('Payment amount exceeds provider precision.')
    return quantized


def amount_to_minor_units(amount: Decimal, decimal_places: int) -> int:
    """Convert exact major units to an integer before signing/submitting a payment."""
    sign, digits, _ = quantize_amount(amount, decimal_places).as_tuple()
    minor_units = int(''.join(str(digit) for digit in digits))
    return -minor_units if sign else minor_units

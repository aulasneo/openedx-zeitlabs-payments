"""Regression tests for exact checkout and provider amounts."""

from decimal import Decimal
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.contrib.sites.models import Site
from django.http import HttpRequest
from django.template.loader import render_to_string

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.amounts import amount_to_minor_units, quantize_amount
from zeitlabs_payments.models import Cart, CatalogueItem
from zeitlabs_payments.providers.manual_payment.processor import ManualPaymentProcessor
from zeitlabs_payments.serializers import CartSerializer


@pytest.mark.parametrize('value,places,formatted,minor', [
    ('99.90', 2, '99.90', 9990),
    ('10.01', 2, '10.01', 1001),
    ('0.50', 2, '0.50', 50),
    ('50', 2, '50.00', 5000),
    ('0', 2, '0.00', 0),
    ('99.9000', 2, '99.90', 9990),
    ('100', 0, '100', 100),
    ('10.001', 3, '10.001', 10001),
])
def test_provider_serialization(value, places, formatted, minor):
    """Provider formats retain fractions and choose their own currency exponent."""
    amount = Decimal(value)
    quantized = quantize_amount(amount, places)
    assert isinstance(quantized, Decimal)
    assert quantized == amount
    assert format(quantized, 'f') == formatted
    submitted = amount_to_minor_units(amount, places)
    assert isinstance(submitted, int)
    assert submitted == minor
    assert Decimal(submitted).scaleb(-places) == amount


@pytest.mark.parametrize('value,places', [
    ('99.901', 2), ('10.01', 0), ('-0.50', 2),
    ('NaN', 2), ('sNaN', 2), ('Infinity', 2), ('-Infinity', 2),
    ('1E100', 2), ('10.01', -1), ('10.01', 2.0), ('10.01', True),
])
@pytest.mark.parametrize('convert', [quantize_amount, amount_to_minor_units])
def test_provider_serialization_rejects_invalid_amounts(value, places, convert):
    """Never silently round, truncate, or submit invalid monetary values."""
    with pytest.raises(ValueError):
        convert(Decimal(value), places)


@pytest.mark.parametrize('value', [99.90, '99.90', 100])
@pytest.mark.parametrize('convert', [quantize_amount, amount_to_minor_units])
def test_provider_serialization_requires_decimal(value, convert):
    """Float conversion must not introduce binary arithmetic into payments."""
    with pytest.raises(TypeError):
        convert(value, 2)


@pytest.fixture
def fractional_cart(request, base_data, settings):  # pylint: disable=unused-argument
    """Persist fractional totals, including a second line and tax/discount adjustments."""
    expected, adjustments = request.param
    settings.ZEITLABS_PAYMENTS_SETTINGS = {'valid_currency': 'BRL', 'invoice_prefix': 'TEST'}
    settings.TEMPLATES = [{
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'OPTIONS': {'loaders': [
            ('django.template.loaders.locmem.Loader', {'main_django.html': '{% block body %}{% endblock %}'}),
            'django.template.loaders.app_directories.Loader',
        ]},
    }]
    catalogue = CatalogueItem.objects.get(sku='custom-sku-1')
    catalogue.currency = 'BRL'
    catalogue.save(update_fields=['currency'])
    cart = Cart.objects.create(user=get_user_model().objects.get(pk=3), status=Cart.Status.PROCESSING)
    total = Decimal(expected)
    discount = Decimal('0.20') if adjustments else Decimal('0.00')
    tax = Decimal('0.10') if adjustments else Decimal('0.00')
    cart.items.create(
        catalogue_item=catalogue,
        original_price=total - Decimal('0.10') + discount - tax,
        discount_amount=discount,
        tax_amount=tax,
        final_price=total - Decimal('0.10'),
    )
    cart.items.create(catalogue_item=catalogue, original_price=Decimal('0.10'), final_price=Decimal('0.10'))
    return cart, total


@pytest.mark.django_db
@pytest.mark.parametrize('fractional_cart', [
    (value, adjustments) for value in ('99.90', '10.01', '0.50') for adjustments in (False, True)
], indirect=True)
@pytest.mark.parametrize('boundary', ['major', 'minor', 'manual'])
def test_checkout_submitted_and_reconciled_amounts_agree(
    fractional_cart, boundary, rf,  # pylint: disable=redefined-outer-name
):
    """Decimal parameters, rendered checkout, recorded payment, and invoice agree."""
    cart, expected = fractional_cart
    request = HttpRequest()
    request.user = cart.user
    request.site = Site.objects.get_current()
    processor = DummyProcessor()
    params = processor.get_transaction_parameters_base(cart, request)
    assert isinstance(params['amount'], Decimal)
    assert params['amount'] == expected == cart.gross_total - cart.discount_total + cart.tax_total
    assert params['currency'] == 'BRL'
    serialized_cart = CartSerializer(cart).data
    assert serialized_cart['total'] == expected
    checkout = render_to_string('zeitlabs_payments/checkout.html', {'cart': serialized_cart})
    assert f'{expected} BRL' in checkout

    if boundary == 'manual':
        ManualPaymentProcessor().process_payment(request, cart, 'fractional-payment', 'success')
        invoice = cart.invoices.get()
    else:
        if boundary == 'minor':
            submitted = amount_to_minor_units(params['amount'], 2)
            confirmed = Decimal(submitted).scaleb(-2)
        else:
            submitted = format(quantize_amount(params['amount'], 2), 'f')
            confirmed = Decimal(submitted)
        assert confirmed == expected
        invoice = processor.process_payment_and_update_records(
            cart=cart, data={'confirmed': True}, request=request,
            transaction_id='fractional-payment', transaction_status='success',
            method='card', amount=str(confirmed), currency=params['currency'], reason='Confirmed payment',
        )
        assert invoice is not None

    invoice.refresh_from_db()
    payment = invoice.related_transaction
    assert payment.amount == invoice.total == expected
    assert payment.currency == invoice.currency == 'BRL'
    assert invoice.gross_total == cart.gross_total
    assert invoice.discount_total == cart.discount_total
    assert invoice.tax_total == cart.tax_total
    assert sum((item.price for item in invoice.items.all()), Decimal('0')) == expected
    with patch('zeitlabs_payments.templatetags.zeitlab_payment_tags.get_current_request', return_value=rf.get('/')):
        receipt = render_to_string('zeitlabs_payments/invoice.html', {'invoice': invoice, 'currency': invoice.currency})
    assert f'data-total-amount="{expected}"' in receipt
    assert f'{expected} BRL' in receipt

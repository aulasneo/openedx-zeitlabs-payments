"""Verify scoped payment identity and rollback-safe uniqueness handling."""

from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import IntegrityError, transaction
from django.http import HttpRequest

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.exceptions import DuplicateTransactionError, InvalidCartError
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, Transaction, WebhookEvent

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('base_data')]


@pytest.fixture
def purchase():
    """A processing cart and a real request user."""
    user = get_user_model().objects.get(pk=3)
    cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
    catalogue = CatalogueItem.objects.get(sku='custom-sku-1')
    cart.items.create(catalogue_item=catalogue, original_price=catalogue.price, final_price=catalogue.price)
    request = HttpRequest()
    request.user = user
    return DummyProcessor(), cart, request


def record_payment(processor, cart, **kwargs):
    """Record a normal payment through the public processor method."""
    return processor.handle_payment(
        cart, cart.user, 'success', 'scoped-payment', 'card', kwargs.get('amount', '50'),
        'SAR', 'confirmed', response={},
    )


def test_constraint_rejects_duplicate_identity_even_for_a_refund(purchase):  # pylint: disable=redefined-outer-name
    """The same external transaction cannot be attached to another ledger record."""
    processor, cart, _ = purchase
    record = record_payment(processor, cart)
    with pytest.raises(IntegrityError):
        with transaction.atomic():
            Transaction.objects.create(
                gateway=record.gateway, gateway_account=record.gateway_account,
                gateway_transaction_id=record.gateway_transaction_id,
                type=Transaction.TransactionType.REFUND, amount=50,
            )
    assert Transaction.objects.count() == 1


@pytest.mark.parametrize('namespace', ['gateway', 'account'])
def test_equal_ids_in_independent_namespaces_are_allowed(purchase, namespace):  # pylint: disable=redefined-outer-name
    """Independent providers or merchant accounts can both issue the same ID."""
    processor, cart, _ = purchase
    first = record_payment(processor, cart)
    other = DummyProcessor()
    if namespace == 'gateway':
        other.SLUG = 'other-gateway'
    else:
        other.TRANSACTION_ACCOUNT = 'merchant-b'
    second_cart = Cart.objects.create(user=cart.user, status=Cart.Status.PROCESSING)
    second = record_payment(other, second_cart)
    assert first.gateway_transaction_id == second.gateway_transaction_id
    assert first.pk != second.pk
    assert Transaction.objects.count() == 2


def test_duplicate_on_another_cart_is_an_already_processed_outcome(purchase):  # pylint: disable=redefined-outer-name
    """The losing cart stays unmodified and gets no invoice or webhook."""
    processor, cart, request = purchase
    record_payment(processor, cart)
    other = Cart.objects.create(user=cart.user, status=Cart.Status.PROCESSING)
    assert processor.process_payment_and_update_records(
        cart=other, request=request, data={}, transaction_id='scoped-payment', transaction_status='success',
        method='card', amount='50', currency='SAR', reason='confirmed',
    ) is None
    other.refresh_from_db()
    assert other.status == Cart.Status.PROCESSING
    assert not Transaction.objects.filter(cart=other).exists()
    assert not Invoice.objects.filter(cart=other).exists()
    assert WebhookEvent.objects.count() == 1
    assert AuditLog.objects.filter(cart=other, action=AuditLog.AuditActions.DUPLICATE_TRANSACTION).count() == 1
    assert not AuditLog.objects.filter(cart=other, action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK).exists()


def test_direct_duplicate_leaves_the_callers_transaction_usable(purchase):  # pylint: disable=redefined-outer-name
    """Catch a unique violation outside its savepoint without poisoning the caller."""
    processor, cart, _ = purchase
    record_payment(processor, cart)
    other = Cart.objects.create(user=cart.user, status=Cart.Status.PROCESSING)
    with transaction.atomic():
        with pytest.raises(DuplicateTransactionError):
            record_payment(processor, other)
        assert Cart.objects.get(pk=other.pk).status == Cart.Status.PROCESSING
    assert Transaction.objects.count() == 1


def test_unrelated_integrity_error_is_not_reported_as_a_duplicate(purchase):  # pylint: disable=redefined-outer-name
    """Missing required amount must keep its original integrity failure."""
    processor, cart, _ = purchase
    with pytest.raises(IntegrityError):
        record_payment(processor, cart, amount=None)
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING
    assert not Transaction.objects.exists()


def test_webhook_failure_rolls_back_direct_recording(purchase):  # pylint: disable=redefined-outer-name
    """The record, cart transition, and webhook are one atomic recording stage."""
    processor, cart, _ = purchase
    with patch.object(WebhookEvent.objects, 'create', side_effect=RuntimeError('webhook unavailable')):
        with pytest.raises(RuntimeError, match='webhook unavailable'):
            record_payment(processor, cart)
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING
    assert not Transaction.objects.exists()
    assert not AuditLog.objects.filter(cart=cart).exists()


@pytest.mark.parametrize('cart_status', [Cart.Status.PAID, Cart.Status.CANCELLED, Cart.Status.REFUNDED])
def test_direct_recording_cannot_restart_a_terminal_cart(purchase, cart_status):  # pylint: disable=redefined-outer-name
    """The low-level recording method also guards cart transitions."""
    processor, cart, _ = purchase
    Cart.objects.filter(pk=cart.pk).update(status=cart_status)
    with pytest.raises(InvalidCartError):
        record_payment(processor, cart)
    cart.refresh_from_db()
    assert cart.status == cart_status
    assert not Transaction.objects.exists()


def test_recovery_cannot_switch_gateway_accounts(purchase):  # pylint: disable=redefined-outer-name
    """The account namespace used for recovery must match the recorded payment."""
    processor, cart, request = purchase
    record = record_payment(processor, cart)
    processor.TRANSACTION_ACCOUNT = 'another-merchant'
    assert processor.get_payment_for_recovery(cart, record.gateway_transaction_id) is None
    with pytest.raises(InvalidCartError, match='does not belong'):
        processor.complete_paid_cart(cart, request, record)
    assert not Invoice.objects.exists()

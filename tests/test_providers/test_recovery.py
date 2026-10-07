"""Regression tests for recovering purchases after recording a payment."""

from importlib import import_module
from unittest.mock import MagicMock, patch

import pytest
from common.djangoapps.student.models import CourseEnrollment
from django.apps import apps
from django.contrib.auth import get_user_model
from django.http import HttpRequest

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.models import (
    AuditLog,
    Cart,
    CartItem,
    CatalogueItem,
    Invoice,
    InvoiceItem,
    Transaction,
    WebhookEvent,
)

pytestmark = pytest.mark.django_db


@pytest.fixture
def payment(base_data):  # pylint: disable=unused-argument
    """A purchase with real database records and the existing course handler."""
    user = get_user_model().objects.get(pk=3)
    cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
    catalogue = CatalogueItem.objects.get(sku='custom-sku-1')
    cart.items.create(catalogue_item=catalogue, original_price=catalogue.price, final_price=catalogue.price)
    request = HttpRequest()
    request.user = user
    return DummyProcessor(), {
        'cart': cart,
        'request': request,
        'data': {'confirmed': True},
        'transaction_id': 'recovery-payment',
        'transaction_status': 'success',
        'method': 'card',
        'amount': str(cart.total),
        'currency': 'SAR',
        'reason': 'Confirmed payment',
    }


def assert_single_purchase(cart):
    """Check that retries created neither extra payments nor extra invoices."""
    assert Transaction.objects.filter(cart=cart).count() == 1
    assert WebhookEvent.objects.filter(related_transaction__cart=cart).count() == 1
    assert Invoice.objects.filter(cart=cart).count() == 1
    assert InvoiceItem.objects.filter(invoice__cart=cart).count() == cart.items.count()
    assert AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.CART_FULFILLED).count() == 1
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PAID
    assert cart.fulfilled_at is not None
    assert not cart.items.filter(fulfilled_at__isnull=True).exists()


def test_retry_after_partial_invoice_creation(payment):  # pylint: disable=redefined-outer-name
    """Invoice line failure rolls back the invoice, but not the recorded payment."""
    processor, params = payment
    cart = params['cart']
    with patch.object(InvoiceItem.objects, 'get_or_create', side_effect=RuntimeError('line failure')):
        assert processor.process_payment_and_update_records(**params) is None

    cart.refresh_from_db()
    assert cart.status == Cart.Status.PAID
    assert cart.fulfilled_at is None
    assert Transaction.objects.filter(cart=cart).count() == 1
    assert not Invoice.objects.filter(cart=cart).exists()
    assert not CourseEnrollment.objects.filter(user=cart.user).exists()

    with patch.object(processor, 'handle_payment', side_effect=AssertionError('must not record again')):
        invoice = processor.process_payment_and_update_records(**params)
    assert invoice is not None
    assert_single_purchase(cart)


def test_retry_after_enrollment_failure_and_repeated_success(payment):  # pylint: disable=redefined-outer-name
    """Reuse the invoice and do not enroll again after recovery has completed."""
    processor, params = payment
    cart = params['cart']
    with patch.object(CourseEnrollment, 'enroll', side_effect=RuntimeError('enrollment unavailable')):
        assert processor.process_payment_and_update_records(**params) is None
    invoice = Invoice.objects.get(cart=cart)
    assert cart.items.get().fulfilled_at is None
    assert Transaction.objects.filter(cart=cart).count() == 1

    # params still contains the original PROCESSING instance: recovery must read
    # the committed state, not trust a stale object supplied by a caller.
    assert cart.status == Cart.Status.PROCESSING
    recovered = processor.process_payment_and_update_records(**params)
    assert recovered.pk == invoice.pk
    assert CourseEnrollment.objects.filter(user=cart.user).count() == 1
    with patch.object(processor, 'fulfill_cart', side_effect=AssertionError('must not fulfill again')):
        assert processor.process_payment_and_update_records(**params).pk == invoice.pk
    assert_single_purchase(cart)


def test_retry_skips_items_already_fulfilled(payment):  # pylint: disable=redefined-outer-name
    """A second item's failure preserves the first item's enrollment and checkpoint."""
    processor, params = payment
    cart = params['cart']
    first = cart.items.get()
    catalogue = CatalogueItem.objects.get(sku='course1-org2-no-id-professional')
    second = cart.items.create(catalogue_item=catalogue, original_price=catalogue.price, final_price=catalogue.price)
    params['amount'] = str(cart.total)
    handler = CART_HANDLER[CatalogueItem.ItemType.PAID_COURSE]
    fulfill = handler.fulfill

    def fail_second(item, gateway):
        if item.pk == second.pk:
            raise RuntimeError('second item failed')
        fulfill(item, gateway)

    with patch.object(handler, 'fulfill', side_effect=fail_second):
        assert processor.process_payment_and_update_records(**params) is None
    first.refresh_from_db()
    second.refresh_from_db()
    assert first.fulfilled_at is not None
    assert second.fulfilled_at is None
    assert CourseEnrollment.objects.filter(user=cart.user).count() == 1

    with patch.object(handler, 'fulfill', wraps=fulfill) as retried:
        assert processor.process_payment_and_update_records(**params) is not None
    assert retried.call_count == 1
    assert retried.call_args.args[0].pk == second.pk
    assert CourseEnrollment.objects.filter(user=cart.user).count() == 2
    assert_single_purchase(cart)


def test_checkpoint_failure_rolls_back_local_enrollment(payment):  # pylint: disable=redefined-outer-name
    """An item checkpoint and its database enrollment must commit together."""
    processor, params = payment
    cart = params['cart']
    with patch.object(CartItem, 'save', side_effect=RuntimeError('checkpoint unavailable')):
        assert processor.process_payment_and_update_records(**params) is None
    assert Transaction.objects.filter(cart=cart).count() == 1
    assert not CourseEnrollment.objects.filter(user=cart.user).exists()
    assert cart.items.get().fulfilled_at is None
    assert processor.process_payment_and_update_records(**params) is not None
    assert_single_purchase(cart)


def test_retry_repairs_legacy_partial_invoice(payment):  # pylint: disable=redefined-outer-name
    """An invoice left without lines by the old code is repaired in place."""
    processor, params = payment
    cart = params['cart']
    with patch.object(processor, 'fulfill_cart', side_effect=RuntimeError('temporary failure')):
        assert processor.process_payment_and_update_records(**params) is None
    invoice = Invoice.objects.get(cart=cart)
    invoice.items.all().delete()
    assert processor.process_payment_and_update_records(**params).pk == invoice.pk
    assert_single_purchase(cart)


@pytest.mark.parametrize('mismatch', ['id', 'gateway', 'cart', 'type'])
def test_paid_cart_requires_matching_recorded_payment(payment, mismatch):  # pylint: disable=redefined-outer-name
    """Recovery cannot attach an unrelated payment to a paid cart."""
    processor, params = payment
    cart = params['cart']
    with patch.object(processor, 'create_invoice', side_effect=RuntimeError('temporary failure')):
        assert processor.process_payment_and_update_records(**params) is None
    record = Transaction.objects.get(cart=cart)
    if mismatch == 'id':
        params['transaction_id'] = 'different-id'
    elif mismatch == 'gateway':
        record.gateway = 'another-provider'
    elif mismatch == 'cart':
        record.cart = Cart.objects.create(user=cart.user, status=Cart.Status.PAID)
    else:
        record.type = Transaction.TransactionType.REFUND
    record.save()
    assert processor.process_payment_and_update_records(**params) is None
    assert not Invoice.objects.filter(cart=cart).exists()
    assert not CourseEnrollment.objects.filter(user=cart.user).exists()
    assert Transaction.objects.filter(gateway_transaction_id='recovery-payment').count() == 1


@pytest.mark.parametrize('status', [Cart.Status.CANCELLED, Cart.Status.REFUND_REQUESTED, Cart.Status.REFUNDED])
def test_recovery_does_not_reactivate_cancelled_or_refunded_purchase(
    payment, status,  # pylint: disable=redefined-outer-name
):
    """A later cancellation/refund blocks fulfillment retries."""
    processor, params = payment
    cart = params['cart']
    with patch.object(processor, 'fulfill_cart', side_effect=RuntimeError('temporary failure')):
        assert processor.process_payment_and_update_records(**params) is None
    Cart.objects.filter(pk=cart.pk).update(status=status)
    assert processor.process_payment_and_update_records(**params) is None
    assert not CourseEnrollment.objects.filter(user=cart.user).exists()
    assert Transaction.objects.filter(cart=cart).count() == 1


def test_retry_after_completion_audit_failure(payment):  # pylint: disable=redefined-outer-name
    """Finished items are not repeated if saving overall completion fails."""
    processor, params = payment
    cart = params['cart']
    log = AuditLog.log

    def fail_completion(action, **kwargs):
        if action == AuditLog.AuditActions.CART_FULFILLED:
            raise RuntimeError('audit unavailable')
        return log(action=action, **kwargs)

    with patch.object(AuditLog, 'log', side_effect=fail_completion):
        assert processor.process_payment_and_update_records(**params) is None
    cart.refresh_from_db()
    assert cart.fulfilled_at is None
    assert cart.items.get().fulfilled_at is not None
    assert CourseEnrollment.objects.filter(user=cart.user).count() == 1

    # Exercise the direct operator/background-job recovery entry point.
    record = Transaction.objects.get(cart=cart)
    with patch.object(CourseEnrollment, 'enroll', side_effect=AssertionError('must not enroll again')):
        assert processor.complete_paid_cart(cart, params['request'], record) is not None
    assert_single_purchase(cart)


def test_migration_marks_only_audited_legacy_completions(payment):  # pylint: disable=redefined-outer-name
    """The migration must not equate a paid cart with a completed purchase."""
    _, params = payment
    completed = params['cart']
    incomplete = Cart.objects.create(user=completed.user, status=Cart.Status.PAID)
    audit = AuditLog.objects.create(cart=completed, action=AuditLog.AuditActions.CART_FULFILLED)
    migration = import_module('zeitlabs_payments.migrations.0003_fulfillment_tracking')
    schema_editor = MagicMock()
    schema_editor.connection.alias = 'default'
    migration.backfill_completed_carts(apps, schema_editor)
    completed.refresh_from_db()
    incomplete.refresh_from_db()
    assert completed.fulfilled_at == audit.created_at
    assert completed.items.get().fulfilled_at == audit.created_at
    assert incomplete.fulfilled_at is None

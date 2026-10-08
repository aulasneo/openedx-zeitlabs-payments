"""Regression tests for recovering purchases after recording a payment."""

from importlib import import_module
from unittest.mock import MagicMock, patch

import pytest
from common.djangoapps.student.models import CourseEnrollment
from django.apps import apps
from django.contrib.auth import get_user_model
from django.http import HttpRequest
from django.urls import reverse
from rest_framework.test import APIClient

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.exceptions import InvalidCartError
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
from zeitlabs_payments.providers.manual_payment.processor import ManualPaymentProcessor

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


@pytest.mark.parametrize('mismatch', ['id', 'gateway', 'cart', 'type', 'ambiguous'])
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
    elif mismatch == 'type':
        record.type = Transaction.TransactionType.REFUND
    if mismatch == 'ambiguous':
        # The database now prevents new duplicates; retain the defensive
        # recovery behavior for a corrupt/legacy lookup result.
        with patch.object(Transaction.objects, 'get', side_effect=Transaction.MultipleObjectsReturned):
            assert processor.process_payment_and_update_records(**params) is None
    else:
        record.save()
        assert processor.process_payment_and_update_records(**params) is None
    assert not Invoice.objects.filter(cart=cart).exists()
    assert not CourseEnrollment.objects.filter(user=cart.user).exists()
    assert Transaction.objects.filter(gateway_transaction_id='recovery-payment').count() == 1
    assert not AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK).exists()
    audit = AuditLog.objects.get(cart=cart, action=AuditLog.AuditActions.RECOVERY_PAYMENT_LOOKUP_FAILED)
    assert ('ambiguous' if mismatch == 'ambiguous' else 'missing') in audit.details


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


def test_manual_payment_preserves_payment_after_invoice_failure(payment):  # pylint: disable=redefined-outer-name
    """A manual retry reuses the payment committed before invoice creation failed."""
    _, params = payment
    processor = ManualPaymentProcessor()
    cart = params['cart']
    args = (params['request'], cart, 'manual-recovery', 'success')
    with patch.object(processor, 'create_invoice', side_effect=RuntimeError('invoice unavailable')):
        with pytest.raises(RuntimeError, match='invoice unavailable'):
            processor.process_payment(*args)
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PAID
    assert Transaction.objects.filter(cart=cart).count() == 1
    assert not Invoice.objects.filter(cart=cart).exists()
    with patch.object(processor, 'handle_payment', side_effect=AssertionError('must reuse payment')):
        result = processor.process_payment(*args)
        assert processor.process_payment(*args) == result
    assert Invoice.objects.filter(cart=cart).count() == 1
    assert CourseEnrollment.objects.filter(user=cart.user).count() == 1
    assert not WebhookEvent.objects.filter(related_transaction__cart=cart).exists()


def test_manual_retry_preserves_completed_items_and_external_effects(payment):  # pylint: disable=redefined-outer-name
    """External effects and checkpoints of earlier items survive a later failure."""
    _, params = payment
    cart = params['cart']
    first = cart.items.get()
    catalogue = CatalogueItem.objects.get(sku='course1-org2-no-id-professional')
    second = cart.items.create(catalogue_item=catalogue, original_price=catalogue.price, final_price=catalogue.price)
    handler = CART_HANDLER[CatalogueItem.ItemType.PAID_COURSE]
    external_effects = []

    def fulfill(item, gateway):  # pylint: disable=unused-argument
        external_effects.append(item.pk)

    def fail_second(item, gateway):
        if item.pk == second.pk:
            raise RuntimeError('second item failed')
        fulfill(item, gateway)

    processor = ManualPaymentProcessor()
    args = (params['request'], cart, 'manual-recovery', 'success')
    with patch.object(handler, 'fulfill', side_effect=fail_second):
        with pytest.raises(RuntimeError, match='second item failed'):
            processor.process_payment(*args)
    first.refresh_from_db()
    assert first.fulfilled_at is not None
    assert external_effects == [first.pk]
    assert Transaction.objects.filter(cart=cart).count() == 1
    with patch.object(handler, 'fulfill', side_effect=fulfill):
        result = processor.process_payment(*args)
        assert processor.process_payment(*args) == result
    assert external_effects == [first.pk, second.pk]
    assert Invoice.objects.filter(cart=cart).count() == 1
    assert not cart.items.filter(fulfilled_at__isnull=True).exists()


@pytest.fixture
def manual_api(base_data):  # pylint: disable=unused-argument
    """An authorized manual-payment request."""
    client = APIClient()
    client.force_authenticate(get_user_model().objects.get(pk=1))
    return client, reverse('zeitlabs_payments:manual-payment'), {
        'user_id': 3,
        'course_key': 'course-v1:org1+1+1',
        'mode': 'no-id-professional',
        'transaction_id': 'manual-api-recovery',
        'transaction_status': 'success',
    }


def test_manual_api_retry_reuses_cart_after_enrollment_succeeds(manual_api):  # pylint: disable=redefined-outer-name
    """API retries must bypass new-enrollment validation for the original purchase."""
    client, url, payload = manual_api
    log = AuditLog.log

    def fail_completion(action, **kwargs):
        if action == AuditLog.AuditActions.CART_FULFILLED:
            raise RuntimeError('completion unavailable')
        return log(action=action, **kwargs)

    with patch.object(AuditLog, 'log', side_effect=fail_completion):
        assert client.post(url, payload).status_code == 400
    record = Transaction.objects.get(gateway_transaction_id=payload['transaction_id'])
    assert CourseEnrollment.objects.filter(user_id=3).count() == 1
    with patch.object(CourseEnrollment, 'enroll', side_effect=AssertionError('must not enroll again')):
        recovered = client.post(url, payload)
        assert recovered.status_code == 201
        assert client.post(url, payload).data == recovered.data
    assert recovered.data['created_cart'] == record.cart_id
    assert Cart.objects.filter(user_id=3).count() == 1
    assert Transaction.objects.filter(cart_id=record.cart_id).count() == 1
    assert Invoice.objects.filter(cart_id=record.cart_id).count() == 1


@pytest.mark.parametrize('mismatch', ['user', 'course', 'ambiguous', 'refunded'])
def test_manual_api_rejects_invalid_recovery(manual_api, mismatch):  # pylint: disable=redefined-outer-name
    """A transaction ID cannot be reused for another purchase or after refund."""
    client, url, payload = manual_api
    assert client.post(url, payload).status_code == 201
    count = Cart.objects.count()
    if mismatch == 'user':
        payload['user_id'] = 4
    elif mismatch == 'course':
        payload['course_key'] = 'course-v1:org2+1+1'
    elif mismatch == 'refunded':
        Cart.objects.filter(user_id=3).update(status=Cart.Status.REFUNDED)
    if mismatch == 'ambiguous':
        with patch.object(Transaction.objects, 'select_related') as lookup:
            lookup.return_value.get.side_effect = Transaction.MultipleObjectsReturned
            assert client.post(url, payload).status_code == 400
    else:
        assert client.post(url, payload).status_code == 400
    assert Cart.objects.count() == count

    if mismatch == 'ambiguous':
        audit = AuditLog.objects.get(action=AuditLog.AuditActions.RECOVERY_PAYMENT_LOOKUP_FAILED)
        assert audit.cart_id is None
        assert audit.gateway == ManualPaymentProcessor.SLUG
        assert audit.details == f"Recovery rejected: recorded payment {payload['transaction_id']} is ambiguous."


@pytest.mark.parametrize('recorded_status', ['failed', 'pending', 'unknown', ''])
@pytest.mark.parametrize('entry_point', ['manual_api', 'direct'])
def test_recovery_rejects_unsuccessful_recorded_payment(
    manual_api, recorded_status, entry_point,  # pylint: disable=redefined-outer-name
):
    """A success in the retry payload cannot override the stored payment result."""
    client, url, payload = manual_api
    with patch.object(ManualPaymentProcessor, 'create_invoice', side_effect=RuntimeError('invoice unavailable')):
        assert client.post(url, payload).status_code == 400
    record = Transaction.objects.get(gateway_transaction_id=payload['transaction_id'])
    record.status = recorded_status
    record.save(update_fields=['status'])
    if entry_point == 'manual_api':
        response = client.post(url, payload)
        assert response.status_code == 400
        assert 'Only successful recorded payments' in response.data['details']
    else:
        request = HttpRequest()
        request.user = record.cart.user
        with pytest.raises(InvalidCartError, match='Only successful recorded payments'):
            ManualPaymentProcessor().complete_paid_cart(record.cart, request, record)
    record.refresh_from_db()
    assert record.status == recorded_status
    assert Cart.objects.filter(user_id=3).count() == 1
    assert Transaction.objects.filter(cart=record.cart).count() == 1
    assert not Invoice.objects.filter(cart=record.cart).exists()
    assert not CourseEnrollment.objects.filter(user_id=3).exists()
    assert record.cart.fulfilled_at is None
    assert not record.cart.items.filter(fulfilled_at__isnull=False).exists()
    audit = AuditLog.objects.get(action=AuditLog.AuditActions.INVALID_TRANSACTION, cart=record.cart)
    assert audit.details == f"Transaction: {payload['transaction_id']} is in invalid state: {recorded_status}."

"""Reject unconfirmed payments before recording, invoicing, or granting access."""

from unittest.mock import patch

import pytest
from common.djangoapps.student.models import CourseEnrollment
from django.contrib.auth import get_user_model
from django.http import HttpRequest
from django.urls import reverse
from rest_framework.test import APIClient

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.cart_handler import CART_HANDLER
from zeitlabs_payments.exceptions import InvalidPaymentStatusError
from zeitlabs_payments.models import AuditLog, Cart, CatalogueItem, Invoice, Transaction, WebhookEvent
from zeitlabs_payments.providers.base import PaymentOutcome
from zeitlabs_payments.providers.manual_payment.processor import ManualPaymentProcessor

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('base_data')]

UNCONFIRMED = ['failed', 'pending', 'unknown', 'paid', 'authorized', '', None, 1, {}, [], 'successful']


class MappedProcessor(DummyProcessor):
    """A provider with explicit external status names."""

    TRANSACTION_STATUS_SUCCESS = 'settled'
    TRANSACTION_STATUS_PENDING = 'awaiting settlement'
    TRANSACTION_STATUS_FAILED = 'declined'


@pytest.fixture
def purchase():
    """A processing purchase with the real enrollment handler and learner."""
    user = get_user_model().objects.get(pk=3)
    cart = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
    item = CatalogueItem.objects.get(sku='custom-sku-1')
    cart.items.create(catalogue_item=item, original_price=item.price, final_price=item.price)
    request = HttpRequest()
    request.user = user
    return {
        'cart': cart, 'request': request, 'data': {}, 'transaction_id': 'status-payment',
        'method': 'card', 'amount': str(cart.total), 'currency': 'SAR', 'reason': 'confirmation',
    }


def assert_no_purchase_effects(cart):
    """Check persisted state, ledger records, invoice, and actual enrollment."""
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING
    assert cart.fulfilled_at is None
    assert not cart.items.filter(fulfilled_at__isnull=False).exists()
    assert not Transaction.objects.exists()
    assert not WebhookEvent.objects.exists()
    assert not Invoice.objects.exists()
    assert not CourseEnrollment.objects.filter(user=cart.user).exists()


@pytest.mark.parametrize('payment_status', UNCONFIRMED)
def test_direct_recording_rejects_unconfirmed_status(purchase, payment_status):  # pylint: disable=redefined-outer-name
    """Callers of the low-level helper cannot bypass status validation."""
    processor = DummyProcessor()
    params = {key: value for key, value in purchase.items() if key not in ('request', 'data')}
    with pytest.raises(InvalidPaymentStatusError):
        processor.handle_payment(user=purchase['request'].user, transaction_status=payment_status, **params)
    assert_no_purchase_effects(purchase['cart'])


@pytest.mark.parametrize('payment_status', UNCONFIRMED)
def test_shared_processing_rejects_unconfirmed_status(purchase, payment_status):  # pylint: disable=redefined-outer-name
    """Reject before invoice and fulfillment, with an invalid-status audit rather than rollback."""
    processor = DummyProcessor()
    assert processor.process_payment_and_update_records(transaction_status=payment_status, **purchase) is None
    assert_no_purchase_effects(purchase['cart'])
    assert AuditLog.objects.filter(cart=purchase['cart'], action=AuditLog.AuditActions.INVALID_TRANSACTION).count() == 1
    assert not AuditLog.objects.filter(action=AuditLog.AuditActions.TRANSACTION_ROLLED_BACK).exists()


@pytest.mark.parametrize('payment_status', ['success', 'SUCCESS', ' Success ', 'settled', ' SETTLED '])
def test_success_is_normalized_and_fulfills_purchase(purchase, payment_status):  # pylint: disable=redefined-outer-name
    """Only canonical or explicitly mapped success produces a paid invoice and enrollment."""
    invoice = MappedProcessor().process_payment_and_update_records(transaction_status=payment_status, **purchase)
    assert invoice is not None
    assert invoice.status == Invoice.InvoiceStatus.PAID
    assert invoice.related_transaction.status == PaymentOutcome.SUCCESS.value
    assert CourseEnrollment.objects.filter(user=purchase['cart'].user).count() == 1
    purchase['cart'].refresh_from_db()
    assert purchase['cart'].fulfilled_at is not None


@pytest.mark.parametrize('payment_status, outcome', [
    ('settled', PaymentOutcome.SUCCESS),
    (' awaiting settlement ', PaymentOutcome.PENDING),
    ('DECLINED', PaymentOutcome.FAILED),
    ('other-provider-success', PaymentOutcome.UNKNOWN),
])
def test_explicit_provider_mapping(payment_status, outcome):
    """Provider success, pending, and failure names map to distinct normalized outcomes."""
    assert MappedProcessor().normalize_payment_status(payment_status) == outcome


@pytest.mark.parametrize('payment_status', ['awaiting settlement', 'declined', 'other-provider-success'])
def test_provider_non_success_does_not_fulfill(purchase, payment_status):  # pylint: disable=redefined-outer-name
    """Mapped pending/failure states remain unconfirmed at the shared boundary."""
    assert MappedProcessor().process_payment_and_update_records(transaction_status=payment_status, **purchase) is None
    assert_no_purchase_effects(purchase['cart'])


@pytest.mark.parametrize('payment_status', ['failed', 'pending', 'unknown'])
def test_recovery_payload_must_also_confirm_success(purchase, payment_status):  # pylint: disable=redefined-outer-name
    """An unsuccessful retry cannot resume incomplete fulfillment of a paid cart."""
    processor = DummyProcessor()
    with patch.object(processor, 'create_invoice', side_effect=RuntimeError('temporary outage')):
        assert processor.process_payment_and_update_records(transaction_status='success', **purchase) is None
    record = Transaction.objects.get()
    assert processor.process_payment_and_update_records(transaction_status=payment_status, **purchase) is None
    record.refresh_from_db()
    assert record.status == 'success'
    assert not Invoice.objects.exists()
    assert not CourseEnrollment.objects.filter(user=purchase['cart'].user).exists()


def test_legacy_mapped_success_remains_recoverable(purchase):  # pylint: disable=redefined-outer-name
    """Historical provider-specific success records remain valid after normalization changes."""
    processor = MappedProcessor()
    with patch.object(processor, 'create_invoice', side_effect=RuntimeError('temporary outage')):
        assert processor.process_payment_and_update_records(transaction_status='settled', **purchase) is None
    Transaction.objects.update(status='SETTLED')
    assert processor.process_payment_and_update_records(transaction_status='settled', **purchase) is not None
    assert Transaction.objects.count() == 1


@pytest.mark.parametrize('payment_status', UNCONFIRMED)
def test_manual_processor_rejects_unconfirmed_status(purchase, payment_status):  # pylint: disable=redefined-outer-name
    """Programmatic manual payments use the same success guard as callbacks."""
    with pytest.raises(InvalidPaymentStatusError):
        ManualPaymentProcessor().process_payment(
            purchase['request'], purchase['cart'], purchase['transaction_id'], payment_status,
        )
    assert_no_purchase_effects(purchase['cart'])


@pytest.mark.parametrize('payment_status', ['failed', 'pending', 'unknown', 'paid', 1, {}, ['success']])
def test_manual_api_rejects_status_before_cart_creation(payment_status):
    """An authenticated staff request cannot create a cart or purchase from an unconfirmed status."""
    client = APIClient()
    client.force_authenticate(get_user_model().objects.get(pk=1))
    handler = CART_HANDLER[CatalogueItem.ItemType.PAID_COURSE]
    with patch.object(handler, 'validate_item_and_create_cart') as create_cart:
        response = client.post(reverse('zeitlabs_payments:manual-payment'), {
            'user_id': 3, 'course_key': 'course-v1:org1+1+1', 'mode': 'no-id-professional',
            'transaction_id': 'manual-status', 'transaction_status': payment_status,
        }, format='json')
        create_cart.assert_not_called()
    assert response.status_code == 400
    assert not Cart.objects.exists()
    assert not Transaction.objects.exists()
    assert not Invoice.objects.exists()
    assert not CourseEnrollment.objects.filter(user_id=3).exists()


def test_manual_api_accepts_normalized_success():
    """Manual status validation and persisted success normalization agree."""
    client = APIClient()
    client.force_authenticate(get_user_model().objects.get(pk=1))
    response = client.post(reverse('zeitlabs_payments:manual-payment'), {
        'user_id': 3, 'course_key': 'course-v1:org1+1+1', 'mode': 'no-id-professional',
        'transaction_id': 'manual-status', 'transaction_status': ' SUCCESS ',
    }, format='json')
    assert response.status_code == 201
    assert Transaction.objects.get().status == 'success'
    assert CourseEnrollment.objects.filter(user_id=3).count() == 1


def test_rejected_payment_does_not_consume_its_identity(purchase):  # pylint: disable=redefined-outer-name
    """A later confirmed callback can record the same ID after an unconfirmed response."""
    processor = DummyProcessor()
    assert processor.process_payment_and_update_records(transaction_status='pending', **purchase) is None
    assert processor.process_payment_and_update_records(transaction_status='success', **purchase) is not None
    assert Transaction.objects.count() == 1
    assert CourseEnrollment.objects.filter(user=purchase['cart'].user).count() == 1


def test_instance_provider_mapping_is_respected(purchase):  # pylint: disable=redefined-outer-name
    """Existing adapters can configure their success token on the processor instance."""
    processor = DummyProcessor()
    processor.TRANSACTION_STATUS_SUCCESS = 'confirmed'
    assert processor.process_payment_and_update_records(transaction_status='confirmed', **purchase) is not None
    assert Transaction.objects.get().status == 'success'

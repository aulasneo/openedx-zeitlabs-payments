"""Prevent duplicate gateway attempts and unsafe checkout state transitions."""

from copy import deepcopy
from unittest.mock import patch

import pytest
from django.contrib.auth import get_user_model
from django.db import transaction
from django.http import HttpResponse
from django.test import Client
from django.urls import resolve, reverse

from test_utils.dummy_processor import DummyProcessor
from zeitlabs_payments.models import AuditLog, Cart
from zeitlabs_payments.providers.base import BaseProcessor

pytestmark = [pytest.mark.django_db, pytest.mark.usefixtures('base_data')]


@pytest.fixture(autouse=True)
def browser_settings(settings):
    """Render actual page bodies and enforce CSRF like the LMS middleware does."""
    templates = deepcopy(settings.TEMPLATES)
    templates[0]['APP_DIRS'] = False
    templates[0]['OPTIONS']['loaders'] = [
        ('django.template.loaders.locmem.Loader', {
            'main_django.html': '<html><body>{% block body %}{% endblock %}</body></html>',
        }),
        'django.template.loaders.filesystem.Loader',
        'django.template.loaders.app_directories.Loader',
    ]
    settings.TEMPLATES = templates
    settings.MIDDLEWARE = [*settings.MIDDLEWARE, 'django.middleware.csrf.CsrfViewMiddleware']


@pytest.fixture
def purchase():
    """A learner's pending purchase and authenticated browser."""
    user = get_user_model().objects.get(pk=3)
    cart = Cart.objects.create(user=user)
    client = Client()
    client.force_login(user)
    url = reverse('zeitlabs_payments:initiate-payment', args=['dummy', cart.pk])
    return client, cart, url


@pytest.mark.parametrize('cart_status', [value for value in Cart.Status.values if value != Cart.Status.PENDING])
def test_non_pending_cart_cannot_start_payment(purchase, cart_status):  # pylint: disable=redefined-outer-name
    """An existing attempt or terminal purchase must never be overwritten."""
    client, cart, url = purchase
    Cart.objects.filter(pk=cart.pk).update(status=cart_status)
    with patch.object(DummyProcessor, 'payment_view') as gateway:
        assert client.post(url).status_code == 409
    gateway.assert_not_called()
    cart.refresh_from_db()
    assert cart.status == cart_status
    assert not AuditLog.objects.filter(cart=cart).exists()


def test_get_is_read_only(purchase):  # pylint: disable=redefined-outer-name
    """Following or prefetching the URL cannot create an order."""
    client, cart, url = purchase
    with patch.object(DummyProcessor, 'payment_view') as gateway:
        assert client.get(url).status_code == 405
    gateway.assert_not_called()
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PENDING


def test_checkout_posts_with_csrf_token(purchase):  # pylint: disable=redefined-outer-name
    """The rendered checkout supplies a working token; missing tokens are rejected."""
    _, cart, url = purchase
    client = Client(enforce_csrf_checks=True)
    client.force_login(cart.user)
    with patch.object(DummyProcessor, 'payment_view', return_value=HttpResponse('gateway')) as gateway:
        assert client.post(url).status_code == 403
        gateway.assert_not_called()
        checkout = client.get(reverse('zeitlabs_payments:checkout'))
        assert checkout.status_code == 200
        assert f'<form action="{url}" method="post">'.encode() in checkout.content
        assert b'name="csrfmiddlewaretoken"' in checkout.content
        token = client.cookies['csrftoken'].value
        assert client.post(url, {'csrfmiddlewaretoken': token}).status_code == 200
        gateway.assert_called_once()


def test_overlapping_and_repeated_requests_call_gateway_once(purchase):  # pylint: disable=redefined-outer-name
    """A second request arriving before the first returns cannot create an order."""
    client, cart, url = purchase

    def initiate(**kwargs):
        assert kwargs['cart'].status == Cart.Status.PROCESSING
        assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PROCESSING
        assert client.post(url).status_code == 409
        return HttpResponse('gateway')

    with patch.object(DummyProcessor, 'payment_view', side_effect=initiate) as gateway:
        assert client.post(url).status_code == 200
        assert client.post(url).status_code == 409
    gateway.assert_called_once()
    assert AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.CART_STATUS_UPDATED).count() == 1
    assert AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.REDIRECT_TO_PAYMENT).count() == 1


def test_stale_cart_cannot_overwrite_a_payment(purchase):  # pylint: disable=redefined-outer-name
    """A competing state transition between lookup and claim must win."""
    client, cart, url = purchase

    def stale_lookup(_cart_id):
        Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.PAID)
        return cart

    with patch.object(DummyProcessor, 'get_cart', side_effect=stale_lookup):
        with patch.object(DummyProcessor, 'payment_view') as gateway:
            assert client.post(url).status_code == 409
    gateway.assert_not_called()
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PAID


@pytest.mark.parametrize('failure', ['parameters', 'timeout', 'error_response', 'rendering'])
def test_initialization_failure_blocks_blind_retries(purchase, failure):  # pylint: disable=redefined-outer-name
    """Both caught and propagated errors retain the claim and avoid a false redirect audit."""
    client, cart, url = purchase
    if failure == 'parameters':
        payment_view = patch.object(DummyProcessor, 'payment_view', BaseProcessor.payment_view)
        failing_stage = patch.object(DummyProcessor, 'get_transaction_parameters', side_effect=ValueError('bad config'))
    elif failure == 'rendering':
        payment_view = patch.object(DummyProcessor, 'payment_view', BaseProcessor.payment_view)
        failing_stage = patch.object(DummyProcessor, 'TEMPLATE_NAME', 'missing-template.html', create=True)
    else:
        kwargs = {'side_effect': TimeoutError('gateway timed out')} if failure == 'timeout' else {
            'return_value': HttpResponse('gateway rejected request', status=502),
        }
        payment_view = patch.object(DummyProcessor, 'payment_view', **kwargs)
        failing_stage = patch.object(DummyProcessor, 'get_transaction_parameters', return_value={})
    with payment_view, failing_stage:
        response = client.post(url)
    assert response.status_code == 502
    if failure != 'error_response':
        assert b'Your payment may have started' in response.content
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PROCESSING
    assert AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.PAYMENT_INITIALIZATION_FAILED).count() == 1
    assert not AuditLog.objects.filter(cart=cart, action=AuditLog.AuditActions.REDIRECT_TO_PAYMENT).exists()
    with patch.object(DummyProcessor, 'payment_view') as gateway:
        assert client.post(url).status_code == 409
    gateway.assert_not_called()


@pytest.mark.parametrize('initialization_status', [200, 502])
def test_callback_status_is_not_overwritten_after_initialization(
    purchase, initialization_status,  # pylint: disable=redefined-outer-name
):
    """A fast callback can finish payment while initialization is still returning."""
    client, cart, url = purchase

    def initiate(**_kwargs):
        Cart.objects.filter(pk=cart.pk).update(status=Cart.Status.PAID)
        return HttpResponse('gateway', status=initialization_status)

    with patch.object(DummyProcessor, 'payment_view', side_effect=initiate):
        assert client.post(url).status_code == initialization_status
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PAID


def test_atomic_requests_does_not_wrap_initiation(purchase):  # pylint: disable=redefined-outer-name
    """Django must commit the claim before an external request even with ATOMIC_REQUESTS enabled."""
    _, _, url = purchase
    assert 'default' in resolve(url).func._non_atomic_requests  # pylint: disable=protected-access


def test_enclosing_transaction_cannot_start_external_payment(purchase):  # pylint: disable=redefined-outer-name
    """Reject a rollbackable claim before creating an irreversible gateway order."""
    client, cart, url = purchase
    with patch.object(DummyProcessor, 'payment_view') as gateway:
        with transaction.atomic():
            with pytest.raises(RuntimeError, match='durable atomic block cannot be nested'):
                client.post(url)
        gateway.assert_not_called()
    cart.refresh_from_db()
    assert cart.status == Cart.Status.PENDING
    assert not AuditLog.objects.filter(cart=cart).exists()

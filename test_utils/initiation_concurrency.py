"""Run a real checkout claim race independently of pytest's transaction wrappers."""
# Django model imports require setup with the isolated database first.
# Each iteration joins both workers before any captured values can change.
# pylint: disable=import-outside-toplevel,cell-var-from-loop

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch

import django
from django.conf import settings


def run_claim_race():  # pylint: disable=too-many-locals
    """Both requests read pending before either may attempt the conditional update."""
    # Configure and migrate a disposable file database before Django opens any
    # connections. A subprocess preserves the suite's session-scoped fixtures.
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'test_settings')
    with TemporaryDirectory(prefix='payment-claim-race-') as directory:
        settings.DATABASES = {'default': {
            'ENGINE': 'django.db.backends.sqlite3',
            'NAME': str(Path(directory) / 'checkout.sqlite3'),
            'ATOMIC_REQUESTS': True,
            'OPTIONS': {'timeout': 10},
        }}
        settings.ALLOWED_HOSTS = ['testserver']
        django.setup()

        from django.contrib.auth import get_user_model
        from django.core.management import call_command
        from django.db import connections
        from django.http import HttpResponse
        from django.test import Client
        from django.urls import reverse

        from test_utils.dummy_processor import DummyProcessor
        from zeitlabs_payments.models import AuditLog, Cart
        from zeitlabs_payments.providers.registry import PROCESSORS

        call_command('migrate', run_syncdb=True, verbosity=0)
        PROCESSORS['dummy'] = DummyProcessor
        user = get_user_model().objects.create(username='concurrent-learner')
        clients = [Client(), Client()]
        for client in clients:
            client.force_login(user)

        original_get_cart = DummyProcessor.get_cart
        try:
            for _ in range(3):
                cart = Cart.objects.create(user=user)
                url = reverse('zeitlabs_payments:initiate-payment', args=['dummy', cart.pk])
                ready = Barrier(2, timeout=10)
                request_connections = []

                def synchronized_lookup(processor, cart_id):
                    pending_cart = original_get_cart(processor, cart_id)
                    assert pending_cart.status == Cart.Status.PENDING
                    connection = connections['default']
                    assert not connection.in_atomic_block
                    request_connections.append(connection.connection)
                    ready.wait()
                    return pending_cart

                def gateway(**_kwargs):
                    # ATOMIC_REQUESTS must not surround the external call: the
                    # winning claim is already committed and visible here.
                    assert not connections['default'].in_atomic_block
                    assert Cart.objects.get(pk=cart.pk).status == Cart.Status.PROCESSING
                    return HttpResponse('gateway')

                def initiate(client):
                    try:
                        return client.post(url).status_code
                    finally:
                        connections.close_all()

                with patch.object(DummyProcessor, 'get_cart', synchronized_lookup):
                    with patch.object(DummyProcessor, 'payment_view', side_effect=gateway) as attempt:
                        with ThreadPoolExecutor(max_workers=2) as executor:
                            responses = list(executor.map(initiate, clients, timeout=15))
                        assert sorted(responses) == [200, 409]
                        attempt.assert_called_once()

                assert len(request_connections) == 2
                assert request_connections[0] is not request_connections[1]
                cart.refresh_from_db()
                assert cart.status == Cart.Status.PROCESSING
                for action in (AuditLog.AuditActions.CART_STATUS_UPDATED, AuditLog.AuditActions.REDIRECT_TO_PAYMENT):
                    assert AuditLog.objects.filter(cart=cart, action=action).count() == 1
        finally:
            connections.close_all()


if __name__ == '__main__':
    run_claim_race()

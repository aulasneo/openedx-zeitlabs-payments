"""Run payment identity migrations and database races in an isolated process."""
# Django models must be imported after configuring the disposable database.
# pylint: disable=import-outside-toplevel,too-many-locals,cell-var-from-loop

import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier
from unittest.mock import patch
from uuid import uuid4

import django
from django.conf import settings


def run_legacy_migration():
    """Verify that duplicate preflight preserves records and reconciled data migrates intact."""
    from django.db import IntegrityError, connection, transaction
    from django.db.migrations.executor import MigrationExecutor

    executor = MigrationExecutor(connection)
    previous = [('zeitlabs_payments', '0003_fulfillment_tracking')]
    target = [('zeitlabs_payments', '0004_transaction_identity')]
    executor.migrate(previous)
    legacy = executor.loader.project_state(previous).apps.get_model('zeitlabs_payments', 'Transaction')
    original = legacy.objects.create(gateway='legacy', gateway_transaction_id='same', amount=50)
    duplicate = legacy.objects.create(gateway='legacy', gateway_transaction_id='same', amount=50)
    executor = MigrationExecutor(connection)
    try:
        executor.migrate(target)
    except RuntimeError as exc:
        assert 'duplicate legacy' in str(exc)
    else:
        raise AssertionError('Duplicate migration unexpectedly succeeded')
    assert legacy.objects.count() == 2
    assert legacy.objects.get(pk=original.pk).gateway == 'legacy'
    with connection.cursor() as cursor:
        fields = connection.introspection.get_table_description(cursor, legacy._meta.db_table)
    assert 'gateway_account' not in {field.name for field in fields}
    # Simulate verified reconciliation, preserving both records and their IDs.
    legacy.objects.filter(pk=duplicate.pk).update(gateway='independent-gateway')
    executor = MigrationExecutor(connection)
    executor.migrate(target)
    current = executor.loader.project_state(target).apps.get_model('zeitlabs_payments', 'Transaction')
    assert current.objects.count() == 2
    assert set(current.objects.values_list('gateway_account', flat=True)) == {''}
    try:
        with transaction.atomic():
            current.objects.create(gateway='legacy', gateway_transaction_id='same', amount=50)
    except IntegrityError:
        pass
    else:
        raise AssertionError('Unique constraint did not reject a duplicate')


def run_constraint_race():
    """Concurrent inserts cannot bypass uniqueness, even without processor checks."""
    from django.core.management import call_command
    from django.db import IntegrityError, connections

    from zeitlabs_payments.models import Transaction

    call_command('migrate', run_syncdb=True, verbosity=0)
    ready = Barrier(2, timeout=10)

    def insert(_worker):
        try:
            connections['default'].ensure_connection()
            ready.wait()
            try:
                Transaction.objects.create(gateway='race', gateway_transaction_id='shared', amount=50)
                return 'recorded'
            except IntegrityError:
                return 'duplicate'
        finally:
            connections.close_all()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert sorted(executor.map(insert, range(2), timeout=15)) == ['duplicate', 'recorded']
    assert Transaction.objects.count() == 1


def run_payment_races():  # pylint: disable=too-many-statements
    """Verify that InnoDB locks and uniqueness serialize payment processing."""
    from django.contrib.auth import get_user_model
    from django.core.management import call_command
    from django.db import connections
    from django.http import HttpRequest

    from test_utils.dummy_processor import DummyProcessor
    from zeitlabs_payments.cart_handler import CART_HANDLER
    from zeitlabs_payments.models import Cart, CatalogueItem, Invoice, Transaction, WebhookEvent

    call_command('migrate', run_syncdb=True, verbosity=0)
    user = get_user_model().objects.create(username='payment-race')
    item = CatalogueItem.objects.create(
        sku='race-course', type=CatalogueItem.ItemType.PAID_COURSE, price=50, currency='SAR',
    )
    handler = CART_HANDLER[CatalogueItem.ItemType.PAID_COURSE]
    for isolation in ('read committed', 'repeatable read'):
        connections.close_all()
        connections['default'].settings_dict['OPTIONS']['isolation_level'] = isolation
        for scenario in ('same_cart', 'different_carts', 'different_ids'):
            first = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
            first.items.create(catalogue_item=item, original_price=50, final_price=50)
            second = first
            if scenario == 'different_carts':
                second = Cart.objects.create(user=user, status=Cart.Status.PROCESSING)
                second.items.create(catalogue_item=item, original_price=50, final_price=50)
            key = f'{isolation}-{scenario}'
            ids = [key, key + '-other' if scenario == 'different_ids' else key]
            ready = Barrier(2, timeout=10)
            inserting = Barrier(2, timeout=10)
            create = Transaction.objects.create

            def simultaneous_insert(**kwargs):
                # Different carts can hold independent row locks. Force both
                # inserts to contend for the exact same external payment key.
                if scenario == 'different_carts':
                    inserting.wait()
                return create(**kwargs)

            def process(worker):
                try:
                    request = HttpRequest()
                    request.user = user
                    ready.wait()
                    invoice = DummyProcessor().process_payment_and_update_records(
                        cart=[first, second][worker], request=request, data={}, transaction_id=ids[worker],
                        transaction_status='success', method='card', amount='50', currency='SAR', reason='confirmed',
                    )
                    return invoice.pk if invoice else None
                finally:
                    connections.close_all()

            with patch.object(Transaction.objects, 'create', side_effect=simultaneous_insert):
                with patch.object(handler, 'fulfill') as fulfillment:
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = list(executor.map(process, range(2), timeout=20))
                    fulfillment.assert_called_once()
            carts = {first.pk, second.pk}
            assert Transaction.objects.filter(cart_id__in=carts).count() == 1
            assert Invoice.objects.filter(cart_id__in=carts).count() == 1
            assert WebhookEvent.objects.filter(related_transaction__cart_id__in=carts).count() == 1
            if scenario == 'same_cart':
                assert results[0] is not None and results[0] == results[1]
            else:
                assert results.count(None) == 1
            paid = Cart.objects.filter(pk__in=carts, status=Cart.Status.PAID)
            assert paid.count() == 1
            assert paid.get().fulfilled_at is not None
            assert not paid.get().items.filter(fulfilled_at__isnull=True).exists()


def run(mode):
    """Configure a disposable SQLite database or a uniquely named MySQL schema."""
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'test_settings')
    with TemporaryDirectory(prefix='payment-record-race-') as directory:
        if mode == 'mysql':
            import MySQLdb
            database = 'payment_race_' + uuid4().hex
            credentials = {
                'host': os.environ.get('PAYMENT_MYSQL_HOST', '127.0.0.1'),
                'port': int(os.environ.get('PAYMENT_MYSQL_PORT', '3306')),
                'user': os.environ.get('PAYMENT_MYSQL_USER', 'root'),
                'passwd': os.environ.get('PAYMENT_MYSQL_PASSWORD', ''),
            }
            admin = MySQLdb.connect(**credentials)
            with admin.cursor() as cursor:
                cursor.execute(f'CREATE DATABASE `{database}` CHARACTER SET utf8mb4')
            settings.DATABASES = {'default': {
                'ENGINE': 'django.db.backends.mysql', 'NAME': database,
                'HOST': credentials['host'], 'PORT': credentials['port'],
                'USER': credentials['user'], 'PASSWORD': credentials['passwd'],
                'OPTIONS': {'isolation_level': 'read committed'},
            }}
        else:
            settings.DATABASES = {'default': {
                'ENGINE': 'django.db.backends.sqlite3', 'NAME': str(Path(directory) / 'payments.sqlite3'),
                'OPTIONS': {'timeout': 10},
            }}
        try:
            django.setup()
            {'legacy': run_legacy_migration, 'sqlite': run_constraint_race, 'mysql': run_payment_races}[mode]()
        finally:
            from django.db import connections
            connections.close_all()
            if mode == 'mysql':
                with admin.cursor() as cursor:
                    cursor.execute(f'DROP DATABASE `{database}`')
                admin.close()


if __name__ == '__main__':
    run(sys.argv[1])

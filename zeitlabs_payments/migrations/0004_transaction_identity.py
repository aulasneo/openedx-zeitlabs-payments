"""Enforce payment identity without discarding legacy financial records."""

from django.db import migrations, models


def check_legacy_duplicates(apps, schema_editor):
    """Stop before any DDL when existing gateway IDs require reconciliation."""
    transaction_model = apps.get_model('zeitlabs_payments', 'Transaction')
    duplicates = list(
        transaction_model.objects.using(schema_editor.connection.alias)
        .values('gateway', 'gateway_transaction_id')
        .annotate(record_count=models.Count('pk'))
        .filter(record_count__gt=1)
        .order_by('gateway', 'gateway_transaction_id')[:10]
    )
    if duplicates:
        raise RuntimeError(
            'Cannot enforce transaction uniqueness: duplicate legacy (gateway, transaction ID) groups exist. '
            'Stop payment writers and reconcile these records with the gateway before retrying migration 0004. '
            f'No records were changed. Up to 10 conflicting groups: {duplicates!r}'
        )


class Migration(migrations.Migration):
    """Preflight legacy identities, then add account scope and database enforcement."""

    dependencies = [('zeitlabs_payments', '0003_fulfillment_tracking')]

    operations = [
        migrations.RunPython(check_legacy_duplicates, migrations.RunPython.noop),
        migrations.AddField(
            model_name='transaction',
            name='gateway_account',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddConstraint(
            model_name='transaction',
            constraint=models.UniqueConstraint(
                fields=('gateway', 'gateway_account', 'gateway_transaction_id'),
                name='unique_gateway_account_transaction',
            ),
        ),
    ]

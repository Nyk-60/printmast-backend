from django.db import migrations


def backfill(apps, schema_editor):
    """Batches finished before per-order label state existed: every order in a
    ready/completed batch went through production, so mark them labeled."""
    BatchOrder = apps.get_model('production', 'BatchOrder')
    BatchOrder.objects.filter(batch__status__in=('ready', 'completed')).update(label_status='labeled')
    BatchOrder.objects.filter(label_purchased=True).update(label_status='labeled')


class Migration(migrations.Migration):

    dependencies = [
        ('production', '0002_alter_batchorder_options_batchorder_error_kind_and_more'),
    ]

    operations = [
        migrations.RunPython(backfill, migrations.RunPython.noop),
    ]

from django.db import migrations


def backfill_store(apps, schema_editor):
    """Infer each existing SkuDesign's store from the orders that actually used it.

    A design created before this field existed has no recorded store. If every
    order that ever auto-matched it belongs to the same store, that's clearly the
    right answer. If it was never used, or (shouldn't happen — checked against the
    live DB before writing this) used by more than one store, it's left NULL rather
    than guessed: it simply won't auto-match anything until the sub-user re-saves it
    for a specific store.
    """
    SkuDesign = apps.get_model('designs', 'SkuDesign')
    OrderItemDesign = apps.get_model('designs', 'OrderItemDesign')

    for sd in SkuDesign.objects.filter(store__isnull=True):
        store_ids = set(
            OrderItemDesign.objects.filter(sku_design=sd)
            .values_list('order_item__order__store_id', flat=True)
        )
        if len(store_ids) == 1:
            sd.store_id = store_ids.pop()
            sd.save(update_fields=['store'])


class Migration(migrations.Migration):

    dependencies = [
        ('designs', '0005_sku_design_store_scope'),
    ]

    operations = [
        migrations.RunPython(backfill_store, migrations.RunPython.noop),
    ]

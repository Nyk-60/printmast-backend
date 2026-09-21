from django.db import migrations

OLD_NAMES = ('Left Sleeve', 'Right Sleeve')


def merge_sleeves(apps, schema_editor):
    """Left/Right Sleeve share one size, so a product keeps a single "Sleeve" area that is
    sized by width x height. The first of the two is renamed, the other one dropped."""
    Area = apps.get_model('catalog', 'PrintCenterPrintArea')
    product_ids = Area.objects.filter(name__in=OLD_NAMES).values_list('pc_product_id', flat=True).distinct()
    for pid in product_ids:
        areas = list(Area.objects.filter(pc_product_id=pid, name__in=OLD_NAMES).order_by('name'))
        if not Area.objects.filter(pc_product_id=pid, name='Sleeve').exists():
            keep, extra = areas[0], areas[1:]
            keep.name = 'Sleeve'
            keep.save(update_fields=['name'])
        else:
            extra = areas
        for area in extra:
            area.delete()
    Area.objects.filter(name__icontains='sleeve').update(size_mode='box')


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0009_print_area_size_mode'),
    ]

    operations = [
        migrations.RunPython(merge_sleeves, migrations.RunPython.noop),
    ]

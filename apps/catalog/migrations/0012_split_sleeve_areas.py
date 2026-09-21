from django.db import migrations


def split_sleeves(apps, schema_editor):
    """Left and right sleeve are chosen separately (each has its own area/icon) but share one
    size grid. 0010 had merged them into "Sleeve": that area becomes "Left Sleeve" and keeps the
    grid, and a "Right Sleeve" area is added that takes its sizes from it."""
    Area = apps.get_model('catalog', 'PrintCenterPrintArea')
    for sleeve in Area.objects.filter(name='Sleeve'):
        if Area.objects.filter(pc_product_id=sleeve.pc_product_id, name__in=('Left Sleeve', 'Right Sleeve')).exists():
            continue
        sleeve.name = 'Left Sleeve'
        sleeve.save(update_fields=['name'])
        Area.objects.create(
            pc_product_id=sleeve.pc_product_id, name='Right Sleeve', is_default=False,
            is_active=sleeve.is_active, width_inches=sleeve.width_inches, height_inches=sleeve.height_inches,
            size_mode=sleeve.size_mode, size_from=sleeve,
        )


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0011_print_area_size_from'),
    ]

    operations = [
        migrations.RunPython(split_sleeves, migrations.RunPython.noop),
    ]

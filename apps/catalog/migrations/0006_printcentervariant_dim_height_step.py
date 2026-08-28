from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0005_printcentervariant_dims_variantdimensionrule'),
    ]

    operations = [
        migrations.AddField(
            model_name='printcentervariant',
            name='dim_height_step',
            field=models.PositiveIntegerField(
                default=1,
                help_text='H increases every N items: 1=every item, 5=every 5 items (e.g. thin DTF prints)',
            ),
        ),
    ]

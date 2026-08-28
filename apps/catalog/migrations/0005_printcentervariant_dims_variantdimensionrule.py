from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0004_remove_base_cost_add_variant_labels'),
    ]

    operations = [
        migrations.AddField(
            model_name='printcentervariant',
            name='dim_length_in',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=6),
        ),
        migrations.AddField(
            model_name='printcentervariant',
            name='dim_width_in',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=6),
        ),
        migrations.AddField(
            model_name='printcentervariant',
            name='dim_height_base_in',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=6),
        ),
        migrations.AddField(
            model_name='printcentervariant',
            name='dim_height_per_unit_in',
            field=models.DecimalField(
                decimal_places=3, default=0, max_digits=5,
                help_text='Extra height added per each additional unit (for stackable items, e.g. t-shirts)',
            ),
        ),
        migrations.AddField(
            model_name='printcentervariant',
            name='is_stackable',
            field=models.BooleanField(
                default=True,
                help_text='If True, height compounds with quantity. If False (e.g. canvas), max height is used.',
            ),
        ),
        migrations.CreateModel(
            name='VariantDimensionRule',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('qty_min', models.PositiveIntegerField()),
                ('qty_max', models.PositiveIntegerField(blank=True, null=True,
                    help_text='Leave blank for no upper limit')),
                ('length_in', models.DecimalField(decimal_places=2, max_digits=6)),
                ('width_in', models.DecimalField(decimal_places=2, max_digits=6)),
                ('height_in', models.DecimalField(decimal_places=2, max_digits=6)),
                ('variant', models.ForeignKey(
                    on_delete=django.db.models.deletion.CASCADE,
                    related_name='dimension_rules',
                    to='catalog.printcentervariant',
                )),
            ],
            options={
                'ordering': ('qty_min',),
            },
        ),
    ]

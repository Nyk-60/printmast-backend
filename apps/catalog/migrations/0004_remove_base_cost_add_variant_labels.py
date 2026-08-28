from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('catalog', '0003_printcentervariant_color_group_and_more'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='product',
            name='base_cost',
        ),
        migrations.AlterField(
            model_name='product',
            name='base_price',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=10),
        ),
        migrations.AddField(
            model_name='product',
            name='variant1_label',
            field=models.CharField(default='Size', max_length=40),
        ),
        migrations.AddField(
            model_name='product',
            name='variant2_label',
            field=models.CharField(default='Color', max_length=40),
        ),
        migrations.AddField(
            model_name='printcenterproduct',
            name='variant1_label',
            field=models.CharField(default='Size', max_length=40),
        ),
        migrations.AddField(
            model_name='printcenterproduct',
            name='variant2_label',
            field=models.CharField(default='Color', max_length=40),
        ),
        migrations.AlterField(
            model_name='productvariant',
            name='cost',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=10),
        ),
        migrations.AlterField(
            model_name='productvariant',
            name='price',
            field=models.DecimalField(decimal_places=2, default=0, max_digits=10),
        ),
    ]

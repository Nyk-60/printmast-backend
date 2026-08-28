from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0004_orderitem_custom_image'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='shipping_override',
            field=models.JSONField(
                blank=True,
                null=True,
                help_text='PC-set override: {weight_oz, length, width, height, note}. Null = use auto-calc.',
            ),
        ),
    ]

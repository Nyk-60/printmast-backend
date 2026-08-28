from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('orders', '0002_order_labeled_at'),
    ]

    operations = [
        migrations.AddField(
            model_name='order',
            name='is_hidden',
            field=models.BooleanField(
                default=False,
                db_index=True,
                help_text='Removed from platform view by the sub-user. Not cancelled in ShipStation.',
            ),
        ),
    ]

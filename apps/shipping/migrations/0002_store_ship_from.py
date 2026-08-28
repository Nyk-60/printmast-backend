from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('shipping', '0001_initial'),
    ]

    operations = [
        migrations.AddField(model_name='store', name='ship_from_name',
            field=models.CharField(blank=True, max_length=200,
                help_text='Sender name on labels for this store. Falls back to PC company name.')),
        migrations.AddField(model_name='store', name='ship_from_company',
            field=models.CharField(blank=True, max_length=200)),
    ]

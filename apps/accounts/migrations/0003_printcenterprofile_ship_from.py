from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0002_printcenterprofile_logo_and_more'),
    ]

    operations = [
        migrations.AddField(model_name='printcenterprofile', name='ship_from_name',
            field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name='printcenterprofile', name='ship_from_phone',
            field=models.CharField(blank=True, max_length=30)),
        migrations.AddField(model_name='printcenterprofile', name='ship_from_street1',
            field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name='printcenterprofile', name='ship_from_street2',
            field=models.CharField(blank=True, max_length=200)),
        migrations.AddField(model_name='printcenterprofile', name='ship_from_city',
            field=models.CharField(blank=True, max_length=120)),
        migrations.AddField(model_name='printcenterprofile', name='ship_from_state',
            field=models.CharField(blank=True, max_length=80)),
        migrations.AddField(model_name='printcenterprofile', name='ship_from_postal_code',
            field=models.CharField(blank=True, max_length=20)),
        migrations.AddField(model_name='printcenterprofile', name='ship_from_country',
            field=models.CharField(blank=True, default='US', max_length=2)),
    ]

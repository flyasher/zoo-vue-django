# Generated by Django 2.0.3 on 2018-03-27 15:33

import django.db.models.deletion
import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [("objectives", "0001_initial")]

    operations = [
        migrations.AlterField(
            model_name="objectivesnapshot",
            name="objective",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name="snapshots",
                to="objectives.Objective",
            ),
        ),
        migrations.AlterField(
            model_name="objectivesnapshot",
            name="timestamp",
            field=models.DateTimeField(default=django.utils.timezone.now),
        ),
    ]

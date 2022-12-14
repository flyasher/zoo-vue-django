# Generated by Django 2.2.19 on 2021-05-09 19:00

from django.db import migrations, models

import zoo.services.constants


class Migration(migrations.Migration):

    dependencies = [
        ("services", "0026_link"),
    ]

    operations = [
        migrations.AddField(
            model_name="environment",
            name="type",
            field=models.CharField(
                choices=[("gitlab", "gitlab"), ("zoo", "zoo")],
                default=zoo.services.constants.EnviromentType("zoo"),
                max_length=100,
            ),
        ),
        migrations.AlterField(
            model_name="environment",
            name="name",
            field=models.CharField(max_length=200),
        ),
        migrations.AlterUniqueTogether(
            name="environment",
            unique_together={("service", "name", "type")},
        ),
    ]

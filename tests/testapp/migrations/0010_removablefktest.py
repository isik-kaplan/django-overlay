import django.db.models.deletion
from django.db import migrations, models

import django_overlay.fields
import django_overlay.operations


class Migration(migrations.Migration):

    dependencies = [
        ('testapp', '0009_filteredsourcetest_uniquetestnosource_and_more'),
    ]

    operations = [
        migrations.CreateModel(
            name='RemovableFkTest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('label', models.CharField(default='x', max_length=100)),
                (
                    'address',
                    django_overlay.fields.OverlayForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='removable_fk_tests',
                        to='testapp.address',
                    ),
                ),
            ],
        ),
        django_overlay.operations.AddOverlayConstraint(
            app_label='testapp',
            model_name='RemovableFkTest',
            field_name='address',
        ),
    ]

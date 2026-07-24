import django_overlay.constraints
import django_overlay.operations
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('testapp', '0011_remove_removablefktest_address'),
        ('testapp_shared', '0008_removableuniquetestsource'),
    ]

    operations = [
        migrations.CreateModel(
            name='RemovableUniqueTest',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ssn', models.CharField(max_length=20)),
            ],
            options={
                'db_table': 'removableuniquetest_view',
                'managed': False,
            },
        ),
        migrations.CreateModel(
            name='RemovableUniqueTestBase',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('ssn', models.CharField(max_length=20)),
            ],
            options={
                'db_table': 'removableuniquetest',
                'default_permissions': (),
                'constraints': [
                    django_overlay.constraints.OverlayUniqueConstraint(
                        fields=('ssn',), name='removableuniquetest_ssn_unique'
                    )
                ],
            },
        ),
        django_overlay.operations.SyncOverlayView(
            app_label='testapp',
            model_name='RemovableUniqueTest',
        ),
        django_overlay.operations.AddOverlayUniqueConstraint(
            app_label='testapp',
            model_name='RemovableUniqueTest',
            constraint_name='removableuniquetest_ssn_unique',
        ),
    ]

from django.db import migrations

import django_overlay.operations


class Migration(migrations.Migration):

    dependencies = [
        ('testapp', '0010_removablefktest'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='RemovableFkTest',
            name='address',
        ),
        django_overlay.operations.RemoveOverlayConstraint(
            app_label='testapp',
            model_name='RemovableFkTest',
            field_name='address',
        ),
    ]

import django_overlay.operations
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('testapp', '0012_removableuniquetest'),
    ]

    operations = [
        migrations.RemoveConstraint(
            model_name='RemovableUniqueTestBase',
            name='removableuniquetest_ssn_unique',
        ),
        django_overlay.operations.RemoveOverlayUniqueConstraint(
            app_label='testapp',
            model_name='RemovableUniqueTest',
            constraint_name='removableuniquetest_ssn_unique',
        ),
    ]

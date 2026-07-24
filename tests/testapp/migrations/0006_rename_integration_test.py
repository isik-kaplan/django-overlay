import django.db.models.deletion
import django_overlay.fields
from django.db import migrations, models


class Migration(migrations.Migration):
    """Deliberately does NOT include SyncOverlayView/AddOverlayConstraint —
    those get bundled into 0007 alongside the rename instead, since both
    operations always read the model as currently defined in code (needed
    for get_source()/OverlayMeta, which a migration's historical frozen
    model doesn't have), not the field names as they stood back when this
    migration was originally written."""

    dependencies = [
        ("testapp", "0005_uniquetestcomposite_uniquetestcompositebase"),
    ]

    operations = [
        migrations.CreateModel(
            name="RenameFieldTest",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_field", models.CharField(max_length=100)),
            ],
            options={
                "db_table": "renamefieldtest_view",
                "managed": False,
            },
        ),
        migrations.CreateModel(
            name="RenameFieldTestBase",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("original_field", models.CharField(max_length=100)),
            ],
            options={
                "db_table": "renamefieldtest",
            },
        ),
        migrations.CreateModel(
            name="RenameFkTest",
            fields=[
                ("id", models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "original_fk",
                    django_overlay.fields.OverlayForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="rename_fk_tests",
                        to="testapp.person",
                    ),
                ),
            ],
        ),
    ]

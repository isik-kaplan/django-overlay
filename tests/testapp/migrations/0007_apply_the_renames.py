import django_overlay.operations
from django.db import migrations


class Migration(migrations.Migration):
    """Hand-written, not autodetector output: exercises the exact scenario
    Fix 4 closes — a genuine RenameField on both a plain overlay field and
    an OverlayForeignKey column, each followed by the resync/re-constraint
    operation the fixed makemigrations command now appends automatically."""

    dependencies = [
        ("testapp", "0006_rename_integration_test"),
    ]

    operations = [
        migrations.RenameField(model_name="RenameFieldTestBase", old_name="original_field", new_name="renamed_field"),
        migrations.RenameField(model_name="RenameFieldTest", old_name="original_field", new_name="renamed_field"),
        django_overlay.operations.SyncOverlayView(
            app_label="testapp",
            model_name="RenameFieldTest",
        ),
        migrations.RenameField(model_name="RenameFkTest", old_name="original_fk", new_name="renamed_fk"),
        django_overlay.operations.AddOverlayConstraint(
            app_label="testapp",
            model_name="RenameFkTest",
            field_name="renamed_fk",
        ),
    ]

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError

from ...sync import resync_view


class Command(BaseCommand):
    help = (
        "Regenerate one or more OverlayModel views + triggers from their "
        "current field list and get_source(), without a migration. Use "
        "this whenever a tenant's resolved source changes without a field "
        "change (e.g. moved from one vendor's table to another's)."
    )

    def add_arguments(self, parser):
        parser.add_argument("models", nargs="+", metavar="app_label.ModelName")
        parser.add_argument("--database", default="default")

    def handle(self, *args, **options):
        for label in options["models"]:
            try:
                app_label, model_name = label.split(".")
            except ValueError:
                raise CommandError(f"Expected app_label.ModelName, got {label!r}") from None
            try:
                model = django_apps.get_model(app_label, model_name)
            except LookupError as exc:
                raise CommandError(str(exc)) from exc
            resync_view(model, using=options["database"])
            self.stdout.write(self.style.SUCCESS(f"Resynced {label}"))

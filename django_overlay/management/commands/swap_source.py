from dataclasses import replace

from django.apps import apps as django_apps
from django.core.management.base import BaseCommand, CommandError

from ...exceptions import OverlaySwapRefused
from ...swaps import swap_source, verify_source_swap


class Command(BaseCommand):
    help = (
        "Cut an OverlayModel over to a new source table, or check whether it "
        "could be. Two modes, matching the two halves of a blue-green "
        "deployment: with --candidate-schema/--candidate-table it verifies a "
        "table against the live source and changes nothing, which is what you "
        "run while the old source is still serving. With neither, it treats "
        "the model's current get_source() as the candidate, checks it against "
        "whatever the deployed view actually reads, and cuts over."
    )

    def add_arguments(self, parser):
        parser.add_argument("model", metavar="app_label.ModelName")
        parser.add_argument("--database", default="default")
        parser.add_argument(
            "--identity-column",
            action="append",
            default=[],
            dest="identity_columns",
            metavar="FIELD",
            help=(
                "A field of the source's natural key. Repeat for a composite one. Without it "
                "nothing checks that the candidate means the same entity by an id as the current "
                "source does, which is the one failure that breaks everything and raises nothing."
            ),
        )
        parser.add_argument("--candidate-schema", help="Verify this table instead of cutting over.")
        parser.add_argument("--candidate-table", help="Verify this table instead of cutting over.")
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Run the full preflight against the configured source and stop before the cutover.",
        )
        parser.add_argument(
            "--allow",
            action="append",
            default=[],
            dest="allow",
            metavar="CODE",
            help="Downgrade one finding code (e.g. S006) from error to warning. Repeatable.",
        )
        parser.add_argument("--lock-timeout", default="5s")
        parser.add_argument("--min-row-ratio", type=float, default=0.9)

    def handle(self, *args, **options):
        model = self._model(options["model"])
        common = dict(
            identity_columns=options["identity_columns"],
            using=options["database"],
            min_row_ratio=options["min_row_ratio"],
        )
        candidate = self._candidate(model, options)

        if candidate is not None:
            # Verify-only. get_source() still names the live table here, which
            # is exactly the state this mode is for: check green while blue is
            # serving, then flip config, then run this again with no
            # --candidate-* to cut over.
            report = verify_source_swap(model, candidate, **common)
            self._write(report)
            if not report.ok:
                raise CommandError("Preflight failed. Nothing was changed.")
            return

        try:
            report = swap_source(
                model,
                dry_run=options["dry_run"],
                lock_timeout=options["lock_timeout"],
                allow=options["allow"],
                **common,
            )
        except OverlaySwapRefused as refused:
            self._write(refused.report)
            raise CommandError("Swap refused. Nothing was changed.") from None

        self._write(report)
        if options["dry_run"]:
            self.stdout.write(self.style.WARNING("Dry run — nothing was changed."))
        else:
            self.stdout.write(self.style.SUCCESS(f"Swapped {options['model']}."))

    def _model(self, label):
        try:
            app_label, model_name = label.split(".")
        except ValueError:
            raise CommandError(f"Expected app_label.ModelName, got {label!r}") from None
        try:
            model = django_apps.get_model(app_label, model_name)
        except LookupError as exc:
            raise CommandError(str(exc)) from exc
        if not getattr(model, "_is_overlay_view_model", False):
            raise CommandError(f"{label} is not an OverlayModel.")
        return model

    def _candidate(self, model, options):
        schema, table = options["candidate_schema"], options["candidate_table"]
        if schema is None and table is None:
            return None
        if schema is None or table is None:
            raise CommandError("--candidate-schema and --candidate-table go together.")
        # Everything but which table is carried over from the configured
        # source: how a source is read is a model-level decision, and changing
        # it at the same time as the table is a different operation.
        return replace(model.get_source(), schema=schema, table=table)

    def _write(self, report):
        self.stdout.write(str(report))

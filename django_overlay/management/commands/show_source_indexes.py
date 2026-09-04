from django.apps import apps as django_apps
from django.core.management.base import BaseCommand
from django.db import connections

from ...introspection import compare_indexes, partition_summary, table_indexes
from ...sync import resolve_schema


class Command(BaseCommand):
    help = (
        "Compare each overlay model's base table indexes against its source table's. "
        "A query that filters and sorts across the view needs the matching index on "
        "both halves of the UNION ALL, or the planner can't merge them cheaply."
    )

    def add_arguments(self, parser):
        parser.add_argument("--database", default="default", help="Database alias to introspect.")
        parser.add_argument("--model", help="Only this model, as app_label.ModelName.")
        parser.add_argument(
            "--missing-only",
            action="store_true",
            help="Skip models whose indexes already line up on both sides.",
        )

    def handle(self, *args, **options):
        connection = connections[options["database"]]
        tenant_schema = resolve_schema(connection)
        models = self._models(options.get("model"))

        if not models:
            self.stdout.write("No overlay models with a source table found.")
            return

        with connection.cursor() as cursor:
            for model in models:
                self._report(cursor, model, tenant_schema, options["missing_only"])

    def _models(self, model_label: str | None):
        if model_label:
            app_label, model_name = model_label.split(".")
            candidates = [django_apps.get_model(app_label, model_name)]
        else:
            candidates = django_apps.get_models()
        return [model for model in candidates if getattr(model, "_is_overlay_view_model", False)]

    def _report(self, cursor, model, tenant_schema: str, missing_only: bool) -> None:
        source = model.get_source()
        base_table = model._base_model._meta.db_table
        source_indexes = table_indexes(cursor, source.schema, source.table)
        base_indexes = table_indexes(cursor, tenant_schema, base_table)
        missing_locally, missing_at_source = compare_indexes(source_indexes, base_indexes)

        if missing_only and not missing_locally and not missing_at_source:
            return

        self.stdout.write(
            self.style.MIGRATE_HEADING(
                f"{model._meta.label}  {tenant_schema}.{base_table}  <-  {source.schema}.{source.table}"
            )
        )
        partitions = partition_summary(cursor, source.schema, source.table)
        if partitions:
            declared = source.partition_key or self.style.WARNING("not declared")
            self.stdout.write(f"  partitioned parent, {partitions['partitions']} partitions, key {declared}")
            if not source.partition_key:
                self.stdout.write(
                    "      every probe this package generates fans out across all of them — "
                    "set SourceTable(partition_key=...)"
                )
            for index in partitions["unattached"]:
                # Half-covered, which parity below cannot see: these live on
                # partitions and are attached to nothing, so the parent reports
                # them as absent. Reported here rather than folded into
                # source_indexes, because "on 3 of 50" is not the same claim as
                # "the source has this index".
                self.stdout.write(
                    self.style.WARNING(
                        f"  UNATTACHED  {index['shape']} — on {index['on_partitions']} of "
                        f"{partitions['partitions']} partitions, not on the parent"
                    )
                )
        if not source_indexes:
            self.stdout.write("  source table has no indexes at all")
        for index in source_indexes:
            self.stdout.write(f"  source  {index['shape']}{' UNIQUE' if index['unique'] else ''}  ({index['name']})")
        for index in base_indexes:
            self.stdout.write(f"  base    {index['shape']}{' UNIQUE' if index['unique'] else ''}  ({index['name']})")

        for index in missing_locally:
            self.stdout.write(self.style.WARNING(f"  MISSING on {base_table}: {index['shape']}"))
            hint = _django_index_hint(index["shape"])
            if hint:
                self.stdout.write(f"      Meta.indexes = [{hint}]")
        for index in missing_at_source:
            self.stdout.write(
                self.style.WARNING(
                    f"  MISSING on {source.table}: {index['shape']} — the source is the big half, "
                    "so this is the expensive gap"
                )
            )
        self.stdout.write("")


def _django_index_hint(shape: str) -> str | None:
    """`models.Index(...)` for a plain btree over bare columns; None for
    anything with an expression, opclass, or non-default access method, where
    guessing the Django equivalent would be wrong more often than right."""
    if not shape.startswith("btree (") or not shape.endswith(")"):
        return None
    columns = [column.strip() for column in shape[len("btree (") : -1].split(",")]
    if any(not column.isidentifier() for column in columns):
        return None
    fields = ", ".join(f'"{column}"' for column in columns)
    return f'models.Index(fields=[{fields}], name="...")'

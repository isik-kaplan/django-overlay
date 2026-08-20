"""Mutation testing for the code mutmut can't reach.

mutmut forks a pre-warmed parent and only sets MUTANT_UNDER_TEST in the child,
so anything whose effect happens at *import* time — the metaclass,
`uniqueness`'s narrowing, every migration operation — has already run against
the original code by the time a mutant is chosen. Those mutants are reported as
survived no matter how good the tests are, which makes mutmut's verdict
worthless there.

This does the same job the crude way: edit the source, run the suite, put it
back. Slower per mutant, but it reaches everything, and the mutations are
chosen for meaning rather than generated, so a survivor here is always worth
reading.

    POSTGRES_USER=postgres uv run python tests/probe_unreachable_mutants.py
    POSTGRES_USER=postgres uv run python tests/probe_unreachable_mutants.py operations

Exit code is the number of survivors, so it can gate CI.
"""

import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent

# (region, label, file, old, new) — the mutation must be killed by the suite.
# A fifth element "equivalent" marks one that provably *can't* be killed because
# the mutated code behaves identically; those are asserted to survive, so a
# surprise kill is reported too (the reasoning would have changed).
#
# `old` must appear exactly once in the file, which is what keeps this list
# honest: a refactor that moves the code reports STALE rather than silently
# testing nothing.
MUTANTS = [
    # ---- operations.py: excluded from mutmut entirely, so otherwise unmeasured
    (
        "operations",
        "SyncOverlayView builds the view from live columns, not historical ones",
        "django_overlay/operations.py",
        '            columns = [column for column in historical_columns if column != "_overlay_deleted"]',
        "            columns = [f.column for f in model._base_model._meta.fields]",
    ),
    (
        "operations",
        "SyncOverlayView resolves the wrong model",
        "django_overlay/operations.py",
        "        def forward(apps, schema_editor):\n            model = django_apps.get_model(app_label, model_name)\n            tenant_schema = _resolve_schema(schema_editor)\n            # Columns from historical state",
        "        def forward(apps, schema_editor):\n            model = None\n            tenant_schema = _resolve_schema(schema_editor)\n            # Columns from historical state",
    ),
    (
        "operations",
        "DropOverlayView drops the base table's name instead of the view's",
        "django_overlay/operations.py",
        '_drop_view(schema_editor, tenant_schema, f"{historical._meta.db_table}_view")',
        "_drop_view(schema_editor, tenant_schema, historical._meta.db_table)",
    ),
    (
        "operations",
        "AddOverlayConstraint hardcodes the referencing pk",
        "django_overlay/operations.py",
        "                    targets,\n                    model._meta.pk.column,",
        '                    targets,\n                    "wrong_pk",',
    ),
    (
        "operations",
        "AddOverlayUniqueConstraint skips the trigger when there *is* a source",
        "django_overlay/operations.py",
        "            if sql:\n                schema_editor.execute(sql)",
        "            if not sql:\n                schema_editor.execute(sql)",
    ),
    (
        "operations",
        "RemoveOverlayConstraint derives the wrong trigger name",
        "django_overlay/operations.py",
        'trigger_name = f"overlayfk_{model._meta.db_table}_{self.column}"[:63]',
        'trigger_name = f"overlayfk_{model._meta.db_table}_{self.column}"[:20]',
    ),
    # ---- uniqueness.py: the import-time half
    (
        "uniqueness",
        "_narrow stops narrowing (soft_delete flag dropped)",
        "django_overlay/uniqueness.py",
        '**{**kwargs, "soft_delete": True}',
        '**{**kwargs, "soft_delete": False}',
    ),
    (
        "uniqueness",
        "_narrow passes every constraint straight through",
        "django_overlay/uniqueness.py",
        "    if isinstance(constraint, OverlayUniqueConstraint):\n        path, args, kwargs = constraint.deconstruct()",
        "    if False:\n        path, args, kwargs = constraint.deconstruct()",
    ),
    # ---- dataclass field defaults: evaluated at import, so mutmut reports
    # "could not find any test case for any mutant" for the whole module.
    (
        "sources",
        "SourceTable stops defaulting its id column to id",
        "django_overlay/sources.py",
        '    id_column: str = "id"',
        '    id_column: str = "XXidXX"',
    ),
    (
        "sources",
        "SourceTable defaults extra_where to a non-empty predicate",
        "django_overlay/sources.py",
        '    extra_where: str = ""',
        '    extra_where: str = "XXXX"',
    ),
    (
        "uniqueness",
        "narrow_for_soft_delete drops every constraint before narrowing it",
        "django_overlay/uniqueness.py",
        '    declared = options.get("constraints") or []',
        '    declared = options.get("constraints") and []',
    ),
    (
        "uniqueness",
        "narrow_for_soft_delete returns the options untouched",
        "django_overlay/uniqueness.py",
        '        options["constraints"] = [_narrow(constraint) for constraint in declared]',
        '        options["constraints"] = list(declared)',
    ),
    (
        "uniqueness",
        "for_validation hands back the narrowed constraints",
        "django_overlay/uniqueness.py",
        "constraint.without_soft_delete_narrowing() if isinstance(constraint, OverlayUniqueConstraint) else constraint",
        "constraint",
    ),
    # ---- the metaclass
    (
        "metaclass",
        "the base model's relation keeps its reverse accessor",
        "django_overlay/fields.py",
        '    field.remote_field.related_name = "+"\n    field._related_name = "+"',
        "    pass",
    ),
    (
        "metaclass",
        "only the live related_name is hidden, not the serialized one",
        "django_overlay/fields.py",
        '    field._related_name = "+"',
        "    pass",
    ),
    (
        "metaclass",
        "a OneToOneField is copied to the base model as-is",
        "django_overlay/fields.py",
        "    collapsed = _WITHOUT_IMPLICIT_UNIQUE.get(type(field))",
        "    collapsed = None",
    ),
    (
        "metaclass",
        "the collapsed copy keeps its cached unique",
        "django_overlay/fields.py",
        '        copied.__dict__.pop("unique", None)',
        "        pass",
    ),
    (
        "metaclass",
        "soft_delete adds no shadow flag",
        "django_overlay/models.py",
        'base_ns["_overlay_deleted"] = models.BooleanField(default=False, editable=False)',
        "pass",
    ),
    (
        "metaclass",
        "constraints go to the view model instead of the base model",
        "django_overlay/models.py",
        '_BASE_ONLY_META_OPTIONS = ("constraints", "indexes", "unique_together", "index_together", "db_table_comment")',
        '_BASE_ONLY_META_OPTIONS = ("indexes", "index_together", "db_table_comment")',
    ),
    (
        "metaclass",
        "the overlay manager clobbers a model's own manager",
        "django_overlay/models.py",
        "        if not any(isinstance(v, models.Manager) for v in rest_items.values()):",
        "        if True:",
    ),
    (
        "metaclass",
        "m2m fields are copied to both models",
        "django_overlay/models.py",
        "        m2m_items = {k: v for k, v in namespace.items() if isinstance(v, models.ManyToManyField)}",
        "        m2m_items = {}",
    ),
    (
        "metaclass",
        "self-referencing updates go back through the view",
        "django_overlay/models.py",
        "        if not any(_reads_own_columns(value, self.model) for value in kwargs.values()):",
        "        if True:",
    ),
    (
        "metaclass",
        "the base-table update stops materialising matched rows first",
        "django_overlay/models.py",
        """            self._copy_matched_rows_to_the_base_table()
            return base_manager.using(self.db).filter(pk__in=self.values("pk")).update(**kwargs)""",
        """            pass
            return base_manager.using(self.db).filter(pk__in=self.values("pk")).update(**kwargs)""",
    ),
    (
        "metaclass",
        "save() stops routing a self-referencing expression around the view",
        "django_overlay/models.py",
        "        if not any(_reads_own_columns(value, self.model) for _, _, value in values):",
        "        if True:",
    ),
    (
        "metaclass",
        "save()'s routed path stops materialising matched rows first",
        "django_overlay/models.py",
        """            self._copy_matched_rows_to_the_base_table()
            matched = base_manager.using(self.db).filter(pk__in=self.values("pk"))""",
        """            pass
            matched = base_manager.using(self.db).filter(pk__in=self.values("pk"))""",
    ),
    (
        "metaclass",
        "save() reports the row count instead of the values it was asked for",
        "django_overlay/models.py",
        "            if not returning_fields:",
        "            if True:",
    ),
    # ---- SQL templates: only ever rendered from a migration
    (
        "sql",
        "the update trigger overwrites unchanged columns",
        "django_overlay/sql_templates/triggers/instead_of_update.sql.j2",
        "{{ c | qi }} = CASE WHEN NEW.{{ c | qi }} IS DISTINCT FROM OLD.{{ c | qi }} THEN {{ proposed_prefix }}.{{ c | qi }} ELSE {{ base_table | qi }}.{{ c | qi }} END",
        "{{ c | qi }} = {{ proposed_prefix }}.{{ c | qi }}",
    ),
    (
        "sql",
        "the view stops masking source rows with a base row",
        "django_overlay/sql_templates/view/view.sql.j2",
        "NOT IN (SELECT {{ pk_column | qi }} FROM {{ tenant_schema | qi }}.{{ base_table | qi }} WHERE {{ pk_column | qi }} IS NOT NULL)",
        "IS NOT NULL AND TRUE",
    ),
    (
        "sql",
        "the view stops hiding soft-deleted rows",
        "django_overlay/sql_templates/view/view.sql.j2",
        "WHERE NOT _overlay_deleted",
        "WHERE TRUE",
    ),
    (
        "sql",
        "the FK trigger accepts NULLs as violations",
        "django_overlay/sql_templates/triggers/constraint_trigger.sql.j2",
        "AND NEW.{{ column | qi }} IS NOT NULL",
        "AND TRUE",
    ),
    (
        "sql",
        "the FK trigger accepts soft-deleted rows as targets",
        "django_overlay/sql_templates/triggers/constraint_trigger.sql.j2",
        "{% if t.soft_delete %} AND NOT _overlay_deleted{% endif %}",
        "",
    ),
    (
        "sql",
        "the FK trigger drops its own existence re-check",
        "django_overlay/sql_templates/triggers/constraint_trigger.sql.j2",
        "  IF NOT EXISTS (\n    SELECT 1 FROM {{ tenant_schema | qi }}.{{ referencing_table | qi }}\n    WHERE {{ referencing_pk | qi }} = NEW.{{ referencing_pk | qi }}\n      AND {{ column | qi }} IS NOT DISTINCT FROM NEW.{{ column | qi }}\n  ) THEN\n    RETURN NULL;\n  END IF;",
        "",
    ),
    (
        "sql",
        "the unique trigger stops excluding the row's own source origin",
        "django_overlay/sql_templates/triggers/unique_constraint_trigger.sql.j2",
        'AND src.{{ source.id_column | qi }} != {{ "-" if negate }}NEW.{{ pk_column | qi }}',
        "",
    ),
    (
        "sql",
        "the unique trigger stops skipping masked source rows",
        "django_overlay/sql_templates/triggers/unique_constraint_trigger.sql.j2",
        "       {%- if soft_delete %}",
        "       {%- if False %}",
    ),
    # "the unique trigger checks NULL columns for collisions" is deliberately
    # absent. The IS NOT NULL guard is a short-circuit, not a correctness rule:
    # with it gone the EXISTS still compares src.col = NEW.col against a NULL,
    # which is never true, so the outcome is identical and only the scan is
    # wasted. Re-tested once tests/testapp NullableUniqueTest gave the source
    # side a genuinely nullable constrained column -- the mutation still
    # survives, which settles it as equivalent rather than untested. The
    # behaviour it guards is pinned by tests/test_unique_constraint.py.
    (
        "sql",
        "the insert trigger stops defaulting the pk",
        "django_overlay/sql_templates/triggers/instead_of_insert.sql.j2",
        "NEW.{{ pk_column | qi }} := COALESCE(NEW.{{ pk_column | qi }}, {{ pk_default_expr }});",
        "",
    ),
    (
        "sql",
        "the unique trigger goes back to deferring to COMMIT",
        "django_overlay/sql_templates/triggers/unique_constraint_trigger.sql.j2",
        "DEFERRABLE INITIALLY IMMEDIATE",
        "DEFERRABLE INITIALLY DEFERRED",
    ),
    (
        "sql",
        "the FK trigger stops deferring, unlike Django's own FK",
        "django_overlay/sql_templates/triggers/constraint_trigger.sql.j2",
        "DEFERRABLE INITIALLY DEFERRED",
        "DEFERRABLE INITIALLY IMMEDIATE",
    ),
    (
        "sql",
        "the delete-side guard stops checking for references",
        "django_overlay/sql_templates/triggers/referenced_row_trigger.sql.j2",
        "    SELECT 1 FROM {{ tenant_schema | qi }}.{{ referencing_table | qi }}\n    WHERE {{ column | qi }} = OLD.{{ target_pk | qi }}",
        "    SELECT 1 FROM {{ tenant_schema | qi }}.{{ referencing_table | qi }}\n    WHERE FALSE",
    ),
    (
        "sql",
        "the delete-side guard stops allowing a row that reverts to source",
        "django_overlay/sql_templates/triggers/referenced_row_trigger.sql.j2",
        "  IF EXISTS (\n    SELECT 1 FROM {{ tenant_schema | qi }}.{{ target_view | qi }}\n    WHERE {{ target_pk | qi }} = OLD.{{ target_pk | qi }}\n  ) THEN\n    RETURN NULL;\n  END IF;",
        "",
    ),
    (
        "sql",
        "the delete-side guard stops watching updates, so soft deletes slip past",
        "django_overlay/sql_templates/triggers/referenced_row_trigger.sql.j2",
        "AFTER DELETE OR UPDATE ON {{ tenant_schema | qi }}.{{ target_table | qi }}",
        "AFTER DELETE ON {{ tenant_schema | qi }}.{{ target_table | qi }}",
    ),
    (
        "sql",
        "soft delete becomes a hard delete",
        "django_overlay/sql.py",
        'template = "triggers/instead_of_delete_soft.sql.j2" if soft_delete else "triggers/instead_of_delete.sql.j2"',
        'template = "triggers/instead_of_delete.sql.j2"',
    ),
    (
        "sql",
        "NEGATIVE_ID stops negating source ids",
        "django_overlay/strategies.py",
        "return strategy is Strategy.NEGATIVE_ID",
        "return False",
    ),
]


def run_suite() -> bool:
    """True if the suite passes (i.e. the mutant survived)."""
    proc = subprocess.run(
        ["uv", "run", "pytest", "-x", "-q", "--no-cov", "-p", "no:cacheprovider"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        env={**os.environ, "POSTGRES_USER": os.environ.get("POSTGRES_USER", "postgres")},
        timeout=1800,
    )
    return proc.returncode == 0


def main(regions) -> int:
    selected = [m for m in MUTANTS if not regions or m[0] in regions]
    if not selected:
        print(f"no mutants for {regions!r}; regions are {sorted({m[0] for m in MUTANTS})}")
        return 1

    unexpected = []
    for mutant in selected:
        region, label, rel_path, old, new = mutant[:5]
        equivalent = len(mutant) > 5
        path = ROOT / rel_path
        original = path.read_text()
        if original.count(old) != 1:
            print(f"STALE    [{region}] {label}")
            print(f"         pattern appears {original.count(old)}x in {rel_path}, expected once")
            unexpected.append(f"{label} (pattern no longer matches)")
            continue
        path.write_text(original.replace(old, new))
        try:
            survived = run_suite()
        finally:
            path.write_text(original)
        if equivalent:
            status = "equivalent" if survived else "SURPRISE  "
            if not survived:
                unexpected.append(f"{label} (marked equivalent but the suite killed it)")
        else:
            status = "SURVIVED  " if survived else "killed    "
            if survived:
                unexpected.append(label)
        print(f"{status} [{region}] {label}")
        sys.stdout.flush()

    print(f"\n{len(selected) - len(unexpected)}/{len(selected)} as expected")
    for label in unexpected:
        print(f"  UNEXPECTED: {label}")
    return len(unexpected)


if __name__ == "__main__":
    sys.exit(main(set(sys.argv[1:])))

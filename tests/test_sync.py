from django_overlay.sync import resolve_schema


def test_resolve_schema_prefers_a_tenant_schema_name_when_present():
    class FakeConnection:
        schema_name = "org_a"

    assert resolve_schema(FakeConnection()) == "org_a"

from django.apps import AppConfig


class TestappSharedConfig(AppConfig):
    name = "tests.testapp_shared"
    label = "testapp_shared"
    default_auto_field = "django.db.models.AutoField"

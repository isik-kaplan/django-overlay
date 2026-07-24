from django.db import models
from django_tenants.models import DomainMixin, TenantMixin


class Client(TenantMixin):
    name = models.CharField(max_length=100)

    auto_create_schema = True

    class Meta:
        app_label = "tenants_app"


class Domain(DomainMixin):
    class Meta:
        app_label = "tenants_app"

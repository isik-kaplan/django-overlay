"""Every route an F() expression can take to the database."""

import threading

import pytest
from django.db import connection, models

from tests.testapp.models import Person
from tests.testapp_shared.models import PersonSource


pytestmark = [pytest.mark.django_db(transaction=True), pytest.mark.concurrency]

THREADS, PER = 4, 40


def hammer(fn):
    def worker():
        for _ in range(PER):
            fn()
        connection.close()

    ts = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout=120)


def result(label, fn):
    Person.objects.all().delete()
    PersonSource.objects.all().delete()
    p = Person.objects.create(first_name="x", age=0)
    hammer(lambda: fn(p.pk))
    got = Person.objects.get(pk=p.pk).age
    print(f"{label:34} {got:4}/{THREADS * PER} {'OK' if got == THREADS * PER else 'LOST UPDATES'}")


def test_all_routes():
    print()
    result("queryset.update(F)", lambda pk: Person.objects.filter(pk=pk).update(age=models.F("age") + 1))

    def via_save(pk):
        obj = Person(pk=pk)
        obj.age = models.F("age") + 1
        obj.save(update_fields=["age"])

    result("instance.save() with F", via_save)

    def via_bulk_update(pk):
        obj = Person.objects.get(pk=pk)
        obj.age = models.F("age") + 1
        Person.objects.bulk_update([obj], ["age"])

    result("bulk_update with F", via_bulk_update)

    Person.objects.all().delete()
    PersonSource.objects.all().delete()

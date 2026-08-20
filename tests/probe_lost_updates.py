"""Is `.update(F(...) + 1)` atomic through the view, as it is on a real table?"""

import threading

import pytest
from django.db import connection, models

from tests.testapp.models import MetaTest, Person
from tests.testapp_shared.models import PersonSource


pytestmark = pytest.mark.django_db(transaction=True)

THREADS, PER_THREAD = 4, 40


def hammer(update):
    def worker():
        for _ in range(PER_THREAD):
            update()
        connection.close()

    threads = [threading.Thread(target=worker) for _ in range(THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)


def test_lost_updates():
    expected = THREADS * PER_THREAD
    print()

    # control: a plain managed table (the hidden base model, used directly)
    Person.objects.all().delete()
    PersonSource.objects.all().delete()
    p = Person.objects.create(first_name="ctl", age=0)
    Base = Person.base_table()
    hammer(lambda: Base.objects.filter(pk=p.pk).update(age=models.F("age") + 1))
    got = Base.objects.get(pk=p.pk).age
    print(f"plain table, F() increment      : {got}/{expected} {'OK' if got == expected else 'LOST UPDATES'}")

    # through the view, organic row
    Person.objects.all().delete()
    p = Person.objects.create(first_name="view", age=0)
    hammer(lambda: Person.objects.filter(pk=p.pk).update(age=models.F("age") + 1))
    got = Person.objects.get(pk=p.pk).age
    print(f"overlay view, F() increment     : {got}/{expected} {'OK' if got == expected else 'LOST UPDATES'}")

    # through the view, source-backed row (copy-on-write path)
    Person.objects.all().delete()
    PersonSource.objects.all().delete()
    src = PersonSource.objects.create(first_name="src", age=0)
    hammer(lambda: Person.objects.filter(pk=-src.id).update(age=models.F("age") + 1))
    got = Person.objects.get(pk=-src.id).age
    print(f"overlay view, source-backed row : {got}/{expected} {'OK' if got == expected else 'LOST UPDATES'}")

    # a source-less overlay model, to rule the source out entirely
    MetaTest.objects.all().delete()
    m = MetaTest.objects.create(name="m")
    hammer(lambda: MetaTest.objects.filter(pk=m.pk).update(name=models.F("name")))
    print("source-less overlay model       : (no numeric field; see above)")

    Person.objects.all().delete()
    PersonSource.objects.all().delete()
    MetaTest.objects.all().delete()

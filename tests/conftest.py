import pytest
from django.db import connection


@pytest.fixture
def db_cursor(db):
    with connection.cursor() as cursor:
        yield cursor

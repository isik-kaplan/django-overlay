import threading
import time

import psycopg
import pytest
from django.conf import settings

from tests.testapp.registry import STRATEGIES


pytestmark = pytest.mark.django_db(transaction=True)


def negates(strategy_name):
    return strategy_name == "negative_id"


def _connect():
    db = settings.DATABASES["default"]
    return psycopg.connect(
        host=db["HOST"],
        port=db["PORT"],
        dbname=db["NAME"],
        user=db["USER"],
        password=db["PASSWORD"] or None,
        autocommit=False,
    )


def _wait_until(predicate, timeout=5.0, description="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"{description} never became true within {timeout}s")


def _run_update(sql, params, state, go_event, errors):
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT pg_backend_pid()")
            state["pid"] = cur.fetchone()[0]
            cur.execute(sql, params)
        state["locked"] = True
        go_event.wait(timeout=5)
        conn.commit()
    except Exception as exc:  # surfaced to the main thread via errors
        errors.append(exc)
    finally:
        conn.close()


def _assert_concurrent_edits_to_different_columns_both_survive(view_table, target_id):
    a_go, b_go = threading.Event(), threading.Event()
    a_state, b_state, errors = {}, {}, []

    a_thread = threading.Thread(
        target=_run_update,
        args=(f'UPDATE "{view_table}" SET first_name = %s WHERE id = %s', ["A-edit", target_id], a_state, a_go, errors),
    )
    a_thread.start()
    _wait_until(lambda: a_state.get("locked"), description="thread A acquiring its lock")

    b_thread = threading.Thread(
        target=_run_update,
        args=(f'UPDATE "{view_table}" SET age = %s WHERE id = %s', [99, target_id], b_state, b_go, errors),
    )
    b_thread.start()
    _wait_until(lambda: "pid" in b_state, description="thread B's connection to start")

    admin_conn = _connect()
    try:
        with admin_conn.cursor() as admin_cur:

            def b_is_blocked():
                admin_cur.execute("SELECT wait_event_type FROM pg_stat_activity WHERE pid = %s", [b_state["pid"]])
                row = admin_cur.fetchone()
                return row is not None and row[0] == "Lock"

            _wait_until(b_is_blocked, description="thread B blocking on thread A's row lock")
    finally:
        admin_conn.close()

    a_go.set()
    a_thread.join(timeout=5)
    b_go.set()
    b_thread.join(timeout=5)

    assert not errors, errors
    return a_thread, b_thread


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_concurrent_edits_to_different_columns_of_an_already_materialized_row_both_survive(strategy_name):
    m = STRATEGIES[strategy_name]
    person = m["Person"].objects.create(first_name="Original", age=1)

    _assert_concurrent_edits_to_different_columns_both_survive(m["Person"]._meta.db_table, person.id)

    person.refresh_from_db()
    assert person.first_name == "A-edit"
    assert person.age == 99


@pytest.mark.parametrize("strategy_name", STRATEGIES.keys())
def test_concurrent_edits_to_different_columns_of_a_source_only_row_both_survive(strategy_name):
    m = STRATEGIES[strategy_name]
    source = m["PersonSource"].objects.create(first_name="Original", age=1)
    view_id = -source.id if negates(strategy_name) else source.id

    _assert_concurrent_edits_to_different_columns_both_survive(m["Person"]._meta.db_table, view_id)

    materialized = m["Person"].objects.get(id=view_id)
    assert materialized.first_name == "A-edit"
    assert materialized.age == 99

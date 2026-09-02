"""Execute every compiled query shape against a real Postgres.

This exists because unit tests cannot catch the whole class of bug it caught. A compiler can
emit SQL with the right placeholder count, the right columns and the right text, and still bind
`tenant_id` to a timestamp because the parameters were collected in a different order from the
one the placeholders appear in. That is invisible until execution.

Skipped when no database is reachable, so the suite still runs on a laptop.
"""

from __future__ import annotations

import os

import pytest

from sanjay.query.filters import compile_sql, parse

from .test_query_filters import ALL_SHAPES, SITE, TENANT

psycopg = pytest.importorskip("psycopg")

DSN = os.environ.get("SANJAY_TEST_DSN", "dbname=sanjay_ci")


@pytest.fixture(scope="module")
def conn():
    try:
        c = psycopg.connect(DSN, connect_timeout=3)
    except Exception as e:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no database at {DSN!r}: {e}")
    try:
        yield c
    finally:
        c.close()


@pytest.mark.parametrize("payload", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
@pytest.mark.parametrize("scoped", [False, True], ids=["all-cameras", "camera-scoped"])
def test_every_shape_executes(conn, payload, scoped):
    cams = ["cccccccc-0000-0000-0000-000000000001"] if scoped else None
    q = compile_sql(parse(payload), tenant_id=TENANT, site_id=SITE, camera_ids=cams)
    try:
        with conn.cursor() as cur:
            cur.execute(q.sql, q.params)
            cur.fetchall()
    except Exception:
        conn.rollback()
        raise


def test_count_query_returns_the_triple_shape(conn):
    """The answer layer unpacks these three columns positionally, so their order is a contract."""
    q = compile_sql(
        parse({"entity": "zone_events", "select": "count",
               "start": "2026-09-01T00:00:00+05:30", "end": "2026-09-02T00:00:00+05:30"}),
        tenant_id=TENANT, site_id=SITE,
    )
    with conn.cursor() as cur:
        cur.execute(q.sql, q.params)
        assert [d.name for d in cur.description] == ["confident", "ambiguous", "total"]


def test_grouped_count_returns_group_columns_first(conn):
    q = compile_sql(
        parse({"entity": "zone_events", "select": "count", "group_by": ["camera_id", "type"],
               "start": "2026-09-01T00:00:00+05:30", "end": "2026-09-02T00:00:00+05:30"}),
        tenant_id=TENANT, site_id=SITE,
    )
    with conn.cursor() as cur:
        cur.execute(q.sql, q.params)
        names = [d.name for d in cur.description]
    assert names == ["group_0", "group_1", "confident", "ambiguous", "total"]

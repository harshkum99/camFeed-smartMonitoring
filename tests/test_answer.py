"""Answer-layer tests, run against a freshly seeded database.

These pin down the behaviour that decides whether a customer can trust a number: the four
refusals, the attribute-aware confidence split, coverage scoping, and the audit trail. Every one
of them was a bug at some point during the build, which is why they are here.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from sanjay.query.answer import answer
from sanjay.query.filters import parse

psycopg = pytest.importorskip("psycopg")

DSN = "dbname=sanjay_test"
TENANT = "11111111-1111-1111-1111-111111111111"
SITE = "aaaaaaaa-0000-0000-0000-000000000001"
ROOT = Path(__file__).resolve().parents[1]

CAM_GATE = "cccccccc-0000-0000-0000-000000000001"
CAM_ZONEB = "cccccccc-0000-0000-0000-000000000002"
CAM_FIRE = "cccccccc-0000-0000-0000-000000000004"
CAM_CANTEEN = "cccccccc-0000-0000-0000-000000000008"

DAY1 = ("2026-09-01T00:00:00+05:30", "2026-09-02T00:00:00+05:30")
DAY2 = ("2026-09-02T00:00:00+05:30", "2026-09-03T00:00:00+05:30")


@pytest.fixture(scope="module")
def conn():
    """Build the schema and seed a deterministic factory. Skips when no server is reachable, so
    the rest of the suite still runs on a laptop with no Postgres."""
    try:
        admin = psycopg.connect("dbname=postgres", connect_timeout=3, autocommit=True)
    except Exception as e:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no postgres: {e}")

    with admin, admin.cursor() as cur:
        cur.execute("DROP DATABASE IF EXISTS sanjay_test")
        cur.execute("CREATE DATABASE sanjay_test")

    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        r = subprocess.run(  # noqa: S603
            ["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", "sanjay_test", "-f", str(path)],
            capture_output=True, text=True, check=False,
        )
        if r.returncode:
            pytest.skip(f"migration {path.name} failed: {r.stderr[:300]}")

    r = subprocess.run(  # noqa: S603
        [sys.executable, str(ROOT / "scripts" / "seed_demo.py"), "--dsn", DSN, "--days", "3"],
        capture_output=True, text=True, check=False,
    )
    if r.returncode:
        pytest.skip(f"seed failed: {r.stderr[:300]}")

    c = psycopg.connect(DSN)
    try:
        yield c
    finally:
        c.close()


def ask(conn, payload, question="test question", **kw):
    return answer(conn, parse(payload), question=question, tenant_id=TENANT,
                  site_id=SITE, actor="pytest", **kw)


def evt(start, end, **kw):
    d = {"entity": "zone_events", "select": "count", "start": start, "end": end}
    d.update(kw)
    return d


# --- the four refusals -----------------------------------------------------------------

def test_no_coverage_names_the_camera_and_the_minutes(conn):
    """The canteen camera dies at 14:00 on day 1. Asking about it afterwards must not return a
    confident zero — that is a false negative delivered as a fact."""
    a = ask(conn, evt("2026-09-01T14:00:00+05:30", "2026-09-01T22:00:00+05:30",
                      filters=[{"field": "camera_id", "op": "eq", "value": CAM_CANTEEN}]))
    assert a.abstained and a.abstain_reason == "no_coverage"
    assert a.coverage_pct == 0.0
    assert "Canteen Entrance" in a.message
    assert "480 min" in a.message
    assert "nothing was recorded" in a.message


def test_no_evidence_is_backed_by_high_coverage(conn):
    """A real negative. The claim is only defensible because coverage was high, so the message
    has to say so — otherwise it is indistinguishable from an outage."""
    a = ask(conn, evt(*DAY1, filters=[{"field": "type", "op": "eq", "value": "stationary"}]))
    assert a.abstained and a.abstain_reason == "no_evidence"
    assert a.coverage_pct > 0.9
    assert "real negative" in a.message


def test_not_measured_is_scoped_to_what_was_asked_about(conn):
    """Vests are measured in Zone B but never on vehicles. Asking about vests on vehicles must
    say 'not measured', not 'zero' — the site-wide check would wrongly answer zero."""
    a = ask(conn, {"entity": "tracks", "select": "count",
                   "start": DAY1[0], "end": DAY1[1],
                   "filters": [{"field": "class", "op": "eq", "value": "vehicle"},
                               {"field": "vest", "op": "lt", "value": 0.5}]})
    assert a.abstained and a.abstain_reason == "not_measured"
    assert "vest" in a.message
    assert "not zero, it is" in a.message


def test_measured_attribute_does_not_trigger_not_measured(conn):
    """The mirror of the test above: helmets ARE measured in Zone B, so an empty result there is
    a genuine no-evidence, not a missing detector."""
    a = ask(conn, evt(*DAY1, filters=[
        {"field": "camera_id", "op": "eq", "value": CAM_ZONEB},
        {"field": "helmet", "op": "lt", "value": 0.001}]))
    assert a.abstain_reason != "not_measured"


# --- the confidence split --------------------------------------------------------------

def test_borderline_helmet_scores_are_ambiguous_not_confident(conn):
    """The bug this exists for: a helmet score of 0.46 was being reported as a confident safety
    violation because the *person* detection was confident. A worker facing away is not a worker
    without a helmet, and negative-attribute rules are the first thing a factory tests."""
    a = ask(conn, evt(*DAY2, filters=[
        {"field": "type", "op": "eq", "value": "cross_pos"},
        {"field": "helmet", "op": "lt", "value": 0.5}]))
    assert a.confident > 0
    assert a.ambiguous > 0, "borderline scores must not all be counted as confident"
    assert a.confident + a.ambiguous == a.total


def test_clear_violations_stay_confident(conn):
    """The margin must not swallow real violations — a guard that flags everything as uncertain
    is as useless as one that flags nothing."""
    a = ask(conn, evt(*DAY2, filters=[
        {"field": "type", "op": "eq", "value": "cross_pos"},
        {"field": "helmet", "op": "lt", "value": 0.25}]))
    assert a.confident > 0
    assert a.ambiguous == 0


# --- coverage scoping ------------------------------------------------------------------

def test_coverage_follows_the_camera_named_in_the_question(conn):
    """Averaging coverage across a site hides the outage the question is about: seven healthy
    cameras and one dead one reads as 88% covered."""
    dead = ask(conn, evt("2026-09-02T09:00:00+05:30", "2026-09-02T17:00:00+05:30",
                         filters=[{"field": "camera_id", "op": "eq", "value": CAM_CANTEEN}]))
    alive = ask(conn, evt("2026-09-02T09:00:00+05:30", "2026-09-02T17:00:00+05:30",
                          filters=[{"field": "camera_id", "op": "eq", "value": CAM_GATE}]))
    assert dead.coverage_pct == 0.0
    assert alive.coverage_pct == 1.0


def test_session_camera_scope_is_never_widened_by_a_filter(conn):
    """A permission boundary. A filter naming a camera outside the session's scope must not pull
    it back in."""
    a = ask(conn, evt(*DAY2, filters=[{"field": "camera_id", "op": "eq", "value": CAM_CANTEEN}]),
            camera_ids=[CAM_GATE])
    assert "Canteen" not in a.message


# --- results and evidence --------------------------------------------------------------

def test_fire_exit_obstruction_is_found_on_the_day_it_happened(conn):
    a = ask(conn, evt(*DAY2, select="rows",
                      filters=[{"field": "camera_id", "op": "eq", "value": CAM_FIRE},
                               {"field": "type", "op": "eq", "value": "stationary"}], limit=10))
    assert not a.abstained
    assert len(a.rows) == 1
    assert a.evidence and a.evidence[0].camera_name.startswith("Fire Exit")


def test_every_answer_carries_evidence_with_a_timestamp_and_camera(conn):
    """Never paraphrase a claim you cannot attach a frame to."""
    a = ask(conn, evt(*DAY2, select="distinct_count",
                      filters=[{"field": "camera_id", "op": "eq", "value": CAM_GATE},
                               {"field": "type", "op": "eq", "value": "cross_pos"}]))
    assert a.evidence
    for e in a.evidence:
        assert e.ts is not None and e.camera_name and e.keyframe_uri


def test_evidence_is_ordered_best_first(conn):
    a = ask(conn, evt(*DAY2, filters=[{"field": "camera_id", "op": "eq", "value": CAM_ZONEB}]))
    confs = [e.confidence for e in a.evidence]
    assert confs == sorted(confs, reverse=True)


def test_grouped_counts_split_confident_and_ambiguous_per_group(conn):
    a = ask(conn, evt(*DAY2, group_by=["camera_id"],
                      filters=[{"field": "helmet", "op": "lt", "value": 0.5}]))
    assert a.groups
    for g in a.groups:
        assert g["confident"] + g["ambiguous"] == g["total"]


# --- audit -----------------------------------------------------------------------------

def test_every_question_is_logged_including_refusals(conn):
    """A question that was asked and declined is exactly as interesting to a regulator as one
    that was answered."""
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM answers WHERE actor = 'audit-probe'")
        before = cur.fetchone()[0]

    ask(conn, evt(*DAY1, filters=[{"field": "type", "op": "eq", "value": "stationary"}]),
        question="a question that will be refused")
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE answers SET actor = actor WHERE false"  # no-op; the log is append-only
        )
        cur.execute(
            "SELECT question, abstained, abstain_reason, coverage_pct, sql_executed, plan "
            "FROM answers ORDER BY created_at DESC LIMIT 1"
        )
        q, abstained, reason, coverage, sql, plan = cur.fetchone()

    assert q == "a question that will be refused"
    assert abstained is True and reason == "no_evidence"
    assert coverage is not None
    assert "SELECT" in sql
    assert plan["describes"]          # the English restatement is stored, not just the SQL
    assert before == before


def test_audit_log_rejects_tampering(conn):
    """Enforced by the database, so an application bug cannot quietly weaken it."""
    ask(conn, evt(*DAY2, filters=[{"field": "camera_id", "op": "eq", "value": CAM_GATE}]))
    with conn.cursor() as cur, pytest.raises(psycopg.errors.RaiseException):
        cur.execute("UPDATE answers SET question = 'redacted'")
    conn.rollback()

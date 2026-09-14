"""Answering questions about a site whose footage was imported from a recording.

A recorded site breaks assumptions a live site never tests: its day is in another timezone (and
may be the day the clocks changed), most of the day has no footage at all, its cameras were graded
against ground truth and some cannot see people, and nobody drew a zone. Each of those, handled
carelessly, turns "we don't know" into a confident zero — so these tests pin down the refusals and
the wording as much as the numbers.
"""

from __future__ import annotations

import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smartcam.query.filters import Entity, Op, Select, parse
from smartcam.query.nl import Catalog, StubProvider, compile_question, named_windows

ROOT = Path(__file__).resolve().parents[1]
DBNAME = "smartcam_answers_rec_test"
DSN = f"dbname={DBNAME}"

# --- nl: windows and compilation (no database) -----------------------------------------------

NOW = datetime(2018, 3, 11, 17, 55, tzinfo=UTC)          # 13:55 EDT, the day clocks went forward
CAT = Catalog(
    cameras={"Admin G326 indoor": "a326", "Admin G329 indoor": "a329",
             "Bus G340 outdoor": "b340"},
    tz="America/New_York",
)


def compile_(q):
    return compile_question(q, CAT, StubProvider(), now=NOW)


def cameras_of(f) -> set[str]:
    out: set[str] = set()
    for p in f.predicates:
        if p.field == "camera_id":
            out.update([p.value] if p.op is Op.EQ else p.value)
    return out


def test_today_on_a_dst_day_is_23_hours_with_an_offset_per_end():
    """Adding 24 hours to local midnight would end "today" an hour into tomorrow."""
    w = named_windows(NOW, "America/New_York")
    assert w["today"] == ("2018-03-11T00:00:00-05:00", "2018-03-12T00:00:00-04:00")


def test_fixed_offset_timedelta_still_accepted():
    """Existing callers pass a timedelta; the IANA change must not break them."""
    w = named_windows(NOW, timedelta(hours=-5))
    assert w["today"] == ("2018-03-11T00:00:00-05:00", "2018-03-12T00:00:00-05:00")


def test_named_camera_with_clock_times_compiles_to_one_camera_and_exact_window():
    """The demo question: one camera, five minutes, people counted once each."""
    c = compile_("How many people were at Admin G329 between 11:55 and 12:00 today?")
    f = c.filter
    assert f is not None, c.unsupported
    assert f.entity is Entity.TRACKS and f.select is Select.DISTINCT_COUNT
    assert any(p.field == "class" and p.op is Op.EQ and p.value == "person"
               for p in f.predicates)
    assert cameras_of(f) == {"a329"}
    assert f.start.isoformat() == "2018-03-11T11:55:00-04:00"
    assert f.end.isoformat() == "2018-03-11T12:00:00-04:00"


def test_shared_word_selects_every_camera_that_has_it():
    """"The admin cameras" names no single camera; picking one would answer a narrower question."""
    c = compile_("How many people were at the admin cameras today?")
    assert cameras_of(c.filter) == {"a326", "a329"}


def test_identifying_word_does_not_swallow_a_separate_group():
    """G340 explains "bus", not "admin" — dropping the admin cameras would be silent narrowing."""
    c = compile_("How many people were at Bus G340 or the admin cameras today?")
    assert cameras_of(c.filter) == {"a326", "a329", "b340"}


def test_after_8pm_runs_to_local_midnight():
    c = compile_("How many people were there after 8pm today?")
    assert c.filter.start.isoformat() == "2018-03-11T20:00:00-04:00"
    assert c.filter.end.isoformat() == "2018-03-12T00:00:00-04:00"


def test_clock_range_that_wraps_ends_the_next_day():
    """"Between 22:00 and 02:00" is a night shift, not an empty or reversed window."""
    now = datetime(2018, 3, 8, 17, 55, tzinfo=UTC)        # an ordinary day, no clock change
    c = compile_question("How many people were there between 22:00 and 02:00 today?", CAT,
                         StubProvider(), now=now)
    assert c.filter is not None, c.unsupported
    assert c.filter.start.isoformat() == "2018-03-08T22:00:00-05:00"
    assert c.filter.end.isoformat() == "2018-03-09T02:00:00-05:00"


def test_clock_range_that_wraps_on_the_dst_day_ends_the_next_day():
    """The wrapped end is on 12 March, when 02:00 exists; only 11 March skipped it."""
    c = compile_("How many people were there between 22:00 and 02:00 today?")
    assert c.filter is not None, c.unsupported
    assert c.filter.start.isoformat() == "2018-03-11T22:00:00-04:00"
    assert c.filter.end.isoformat() == "2018-03-12T02:00:00-04:00"


def test_nonexistent_local_time_is_refused_not_shifted():
    """02:30 did not happen on 11 March 2018 in New York; guessing an offset moves the window."""
    c = compile_("How many people were there between 02:30 and 03:30 today?")
    assert c.filter is None
    assert "clocks" in (c.unsupported or "")


def test_passage_verb_stays_a_zone_question_without_zones():
    """"Entered" is a line crossing; answering from presence would report presence as entries."""
    c = compile_("how many people entered today")
    assert c.filter is not None
    assert c.filter.entity is Entity.ZONE_EVENTS


def test_vehicle_words_filter_class_vehicle():
    c = compile_("how many vehicles were at Bus G340 today")
    assert any(p.field == "class" and p.value == "vehicle" for p in c.filter.predicates)


def test_happened_is_not_ppe():
    """"happened" contains "ppe"; a substring match routed it to a helmet question."""
    c = compile_("what happened today")
    if c.filter is not None:
        assert not any(p.field == "helmet" for p in c.filter.predicates)
    else:
        assert "helmet" not in json.dumps(c.raw)


# --- answer: against a recorded site (database) ----------------------------------------------

psycopg = pytest.importorskip("psycopg")

TENANT = "22222222-2222-2222-2222-222222222222"
SITE = "bbbbbbbb-0000-0000-0000-000000000001"
CAM_A = "dddddddd-0000-0000-0000-00000000000a"
CAM_B = "dddddddd-0000-0000-0000-00000000000b"
CAM_C = "dddddddd-0000-0000-0000-00000000000c"
OTHER_TENANT = "33333333-3333-3333-3333-333333333333"
OTHER_SITE = "bbbbbbbb-0000-0000-0000-000000000002"
OTHER_CAM = "dddddddd-0000-0000-0000-0000000000ff"

CLIP1 = ("2018-03-11T11:55:00-04:00", "2018-03-11T12:00:00-04:00")
CLIP2 = ("2018-03-11T13:50:00-04:00", "2018-03-11T13:55:00-04:00")
HOLE = ("2018-03-11T12:00:00-04:00", "2018-03-11T13:50:00-04:00")

TRACK_A1 = "eeeeeeee-0000-0000-0000-0000000000a1"
TRACK_A2 = "eeeeeeee-0000-0000-0000-0000000000a2"
TRACK_B1 = "eeeeeeee-0000-0000-0000-0000000000b1"
TRACK_B2 = "eeeeeeee-0000-0000-0000-0000000000b2"
TRACK_OTHER = "eeeeeeee-0000-0000-0000-0000000000ff"


@pytest.fixture(scope="module")
def conn():
    """A fresh database with every migration and a small recorded site. Skips with no Postgres."""
    try:
        admin = psycopg.connect("dbname=postgres", connect_timeout=3, autocommit=True)
    except Exception as e:  # noqa: BLE001 - any connection failure means "skip"
        pytest.skip(f"no postgres: {e}")
    with admin, admin.cursor() as cur:
        cur.execute(f"DROP DATABASE IF EXISTS {DBNAME}")
        cur.execute(f"CREATE DATABASE {DBNAME}")

    for path in sorted((ROOT / "db" / "migrations").glob("*.sql")):
        r = subprocess.run(  # noqa: S603
            ["psql", "-q", "-v", "ON_ERROR_STOP=1", "-d", DBNAME, "-f", str(path)],  # noqa: S607
            capture_output=True, text=True, check=False,
        )
        if r.returncode:
            pytest.skip(f"migration {path.name} failed: {r.stderr[:300]}")

    person_ok = {"classes": {"person": {"status": "reliable", "frame_recall": 0.91}}}
    person_bad = {"classes": {"person": {"status": "unreliable", "frame_recall": 0.05}}}
    c = psycopg.connect(DSN)
    with c.cursor() as cur:
        for tenant, site, as_of in ((TENANT, SITE, "2018-03-11T13:55:00-04:00"),
                                    (OTHER_TENANT, OTHER_SITE, None)):
            cur.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
                        (tenant, f"tenant {tenant[:4]}"))
            cur.execute("INSERT INTO sites (site_id, tenant_id, name, tz, as_of) "
                        "VALUES (%s, %s, %s, 'America/New_York', %s)",
                        (site, tenant, f"site {site[-1]}", as_of))
        for cam, name, caps, tenant, site in (
            (CAM_A, "Admin G326 indoor", person_ok, TENANT, SITE),
            (CAM_B, "Admin G329 indoor", person_bad, TENANT, SITE),
            (CAM_C, "Bus G340 outdoor", {"classes": {}}, TENANT, SITE),
            (OTHER_CAM, "Other tenant camera", person_ok, OTHER_TENANT, OTHER_SITE),
        ):
            cur.execute("INSERT INTO cameras (camera_id, tenant_id, site_id, name, capabilities) "
                        "VALUES (%s, %s, %s, %s, %s::jsonb)",
                        (cam, tenant, site, name, json.dumps(caps)))
        cur.execute("SELECT ensure_partitions('2018-03-01', '2018-03-01')")
        for cam in (CAM_A, CAM_B, CAM_C):
            for s, e in (CLIP1, CLIP2):
                cur.execute("INSERT INTO camera_uptime (camera_id, site_id, tenant_id, ts_start, "
                            "ts_end, state, source) VALUES (%s, %s, %s, %s, %s, 'online', "
                            "'recorded_import')", (cam, SITE, TENANT, s, e))
        for tid, cam, tenant, site, conf in (
            (TRACK_A1, CAM_A, TENANT, SITE, 0.9), (TRACK_A2, CAM_A, TENANT, SITE, 0.8),
            (TRACK_B1, CAM_B, TENANT, SITE, 0.9), (TRACK_B2, CAM_B, TENANT, SITE, 0.85),
            (TRACK_OTHER, OTHER_CAM, OTHER_TENANT, OTHER_SITE, 0.9),
        ):
            cur.execute("INSERT INTO tracks (track_id, tenant_id, site_id, camera_id, class, "
                        "ts_start, ts_end, dwell_s, conf_max, conf_mean, n_frames, attrs) "
                        "VALUES (%s, %s, %s, %s, 'person', '2018-03-11T11:56:00-04:00', "
                        "'2018-03-11T11:57:00-04:00', 60, %s, %s, 30, '{}')",
                        (tid, tenant, site, cam, conf, conf))
    c.commit()
    try:
        yield c
    finally:
        c.close()


def ask(conn, payload, question="test question"):
    from smartcam.query.answer import answer
    return answer(conn, parse(payload), question=question, tenant_id=TENANT, site_id=SITE,
                  actor="test")


def tracks(window, *filters, select="distinct_count"):
    return {"entity": "tracks", "select": select, "start": window[0], "end": window[1],
            "filters": list(filters)}


def cam(*ids):
    return ({"field": "camera_id", "op": "eq", "value": ids[0]} if len(ids) == 1
            else {"field": "camera_id", "op": "in", "value": list(ids)})


PERSON = {"field": "class", "op": "eq", "value": "person"}


def logged(conn, question) -> int:
    with conn.cursor() as cur:
        cur.execute("SELECT count(*) FROM answers WHERE question = %s AND tenant_id = %s",
                    (question, TENANT))
        return cur.fetchone()[0]


def test_unreliable_camera_is_never_a_real_negative(conn):
    """A camera that detects 5% of people cannot vouch for an empty result, or for a count."""
    q = "people on B"
    a = ask(conn, tracks(CLIP1, PERSON, cam(CAM_B)), q)
    assert a.abstain_reason != "no_evidence"
    assert a.confident == 0
    assert [(lim.camera_id, lim.status) for lim in a.limits] == [(CAM_B, "unreliable")]
    assert "Admin G329 indoor" in a.message and "5%" in a.message
    assert logged(conn, q) == 1


def test_unmeasured_attribute_refuses_and_leaves_nothing_behind(conn):
    """No helmet detector ran: the answer is unknown, and a count beside it would be believed."""
    q = "no helmet on A"
    a = ask(conn, tracks(CLIP1, PERSON, cam(CAM_A),
                         {"field": "helmet", "op": "lt", "value": 0.5}), q)
    assert a.abstain_reason == "not_measured"
    assert a.total == 0 and a.rows == [] and a.evidence == []
    assert logged(conn, q) == 1


def test_is_null_on_an_unmeasured_attribute_is_not_every_track(conn):
    """helmet IS NULL matches every track when nothing measured helmets — that is not a finding."""
    q = "helmet null rows on A"
    a = ask(conn, tracks(CLIP1, cam(CAM_A), {"field": "helmet", "op": "is_null"},
                         select="rows"), q)
    assert a.abstain_reason == "not_measured"
    assert a.rows == [] and a.evidence == []
    assert logged(conn, q) == 1


def test_zone_question_without_zones_names_the_camera(conn):
    q = "zone events on A"
    a = ask(conn, {"entity": "zone_events", "select": "count", "start": CLIP1[0],
                   "end": CLIP1[1], "filters": [cam(CAM_A)]}, q)
    assert a.abstain_reason == "not_measured"
    assert "Admin G326 indoor" in a.message
    assert logged(conn, q) == 1


def test_class_no_camera_detects_is_not_measured(conn):
    """Silence from cameras with no vehicle detector is not "no vehicles"."""
    q = "vehicles on A and B"
    a = ask(conn, tracks(CLIP1, {"field": "class", "op": "eq", "value": "vehicle"},
                         cam(CAM_A, CAM_B)), q)
    assert a.abstain_reason == "not_measured"
    assert a.total == 0
    assert logged(conn, q) == 1


def test_hole_between_clips_says_no_footage_not_down(conn):
    """The cameras were not down at 12:30; nobody imported that hour. The wording must say so."""
    q = "people in the hole"
    a = ask(conn, tracks(HOLE, PERSON, cam(CAM_A, CAM_B, CAM_C)), q)
    assert a.abstain_reason == "no_coverage"
    assert "has no footage" in a.message
    assert "was down" not in a.message
    assert logged(conn, q) == 1


# --- api: evidence frames and scope (database) -----------------------------------------------

@pytest.fixture
def client(conn, monkeypatch, tmp_path):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from smartcam.api import app as app_module
    from smartcam.api import session

    monkeypatch.setattr(app_module, "DSN", DSN)
    monkeypatch.setattr(session, "DEFAULT_TENANT", TENANT)
    monkeypatch.setattr(session, "DEFAULT_SITE", SITE)
    monkeypatch.setenv("SMARTCAM_DATA_DIR", str(tmp_path))
    return TestClient(app_module.app)


def store_frame(conn, root, track_id, tenant, site, camera, shade):
    np = pytest.importorskip("numpy")
    from smartcam.ingest.keyframes import KeyframeStore

    rgb = np.full((24, 32, 3), shade, dtype=np.uint8)
    frame = KeyframeStore(root).put(rgb, tenant_id=tenant, site_id=site, camera_id=camera)
    with conn.cursor() as cur:
        cur.execute("UPDATE tracks SET best_keyframe_uri = %s, frame_sha256 = %s "
                    "WHERE track_id = %s", (frame.key, frame.sha256, track_id))
    conn.commit()
    return frame


def test_keyframe_is_served_with_its_hash(conn, client, tmp_path):
    frame = store_frame(conn, tmp_path, TRACK_A1, TENANT, SITE, CAM_A, 90)
    r = client.get(f"/api/evidence/{TRACK_A1}/keyframe")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"
    assert r.headers["x-keyframe-sha256"] == frame.sha256


def test_tampered_keyframe_is_not_served(conn, client, tmp_path):
    """A frame whose bytes changed since ingest is no longer evidence of anything."""
    frame = store_frame(conn, tmp_path, TRACK_A2, TENANT, SITE, CAM_A, 160)
    path = tmp_path / frame.key
    path.write_bytes(path.read_bytes() + b"tampered")
    assert client.get(f"/api/evidence/{TRACK_A2}/keyframe").status_code == 422


def test_other_tenants_keyframe_is_404_even_with_their_headers(conn, client, tmp_path):
    """The headers are not a security boundary; real footage must not be one guessed header away."""
    store_frame(conn, tmp_path, TRACK_OTHER, OTHER_TENANT, OTHER_SITE, OTHER_CAM, 40)
    r = client.get(f"/api/evidence/{TRACK_OTHER}/keyframe",
                   headers={"x-smartcam-tenant": OTHER_TENANT, "x-smartcam-site": OTHER_SITE})
    assert r.status_code == 404


def test_non_uuid_track_id_is_rejected(client):
    assert client.get("/api/evidence/not-a-uuid/keyframe").status_code == 422


def test_site_of_another_tenant_is_forbidden(client):
    """Mixing one tenant's id with another's site would pair their camera names with our tracks."""
    r = client.get("/api/cameras",
                   headers={"x-smartcam-tenant": TENANT, "x-smartcam-site": OTHER_SITE})
    assert r.status_code == 403


def test_site_reports_timezone_and_replay(client):
    """The console cannot print a time for a recorded site without both."""
    body = client.get("/api/site").json()
    assert body["tz"] == "America/New_York"
    assert body["replay"] is True


def test_recorded_cameras_have_unchecked_clocks(client):
    """A recording has no live clock to check; unchecked must not be shown as fine."""
    rows = client.get("/api/cameras").json()
    assert {r["name"] for r in rows} == {"Admin G326 indoor", "Admin G329 indoor",
                                         "Bus G340 outdoor"}
    assert all(r["clock_ok"] is None for r in rows)


RELIABLE = {"classes": {"person": {"status": "reliable", "frame_recall": 0.91}}}


def _set_caps(conn, camera_id, caps):
    with conn.cursor() as cur:
        cur.execute("UPDATE cameras SET capabilities = %s::jsonb WHERE camera_id = %s",
                    (json.dumps(caps), camera_id))
    conn.commit()


def test_a_limited_camera_finding_nothing_is_not_a_confirmed_negative(conn):
    """A camera measured to miss half the people it sees can report that it saw nobody, but not
    that nobody was there. The empty case used to lose the caveat the non-empty case kept."""
    _set_caps(conn, CAM_A, {"classes": {"person": {"status": "limited", "frame_recall": 0.45}}})
    try:
        a = ask(conn, tracks(CLIP2, PERSON, cam(CAM_A)), "limited empty")
        assert a.abstain_reason == "no_evidence"
        assert "real negative" not in a.message
        assert "not a confirmed negative" in a.message and "45%" in a.message
    finally:
        _set_caps(conn, CAM_A, RELIABLE)


def test_an_unassessed_camera_does_not_count_towards_coverage(conn):
    """'Only 100% of that period was usably covered' contradicted itself: a camera whose detection
    quality was never measured cannot make a period covered."""
    _set_caps(conn, CAM_A, {"classes": {"person": {"status": "not_assessed"}}})
    try:
        a = ask(conn, tracks(CLIP2, PERSON, cam(CAM_A)), "unassessed empty")
        assert a.coverage_pct == 0.0
        assert a.abstain_reason == "no_coverage"
        assert "not been measured" in a.message
    finally:
        _set_caps(conn, CAM_A, RELIABLE)


def test_unzoned_cameras_in_a_partly_zoned_scope_are_left_out_and_named(conn):
    """One camera with a zone let every unzoned camera in scope count as covered, so their silence
    became a real negative."""
    with conn.cursor() as cur:
        cur.execute("INSERT INTO zones (camera_id, site_id, name, polygon) VALUES "
                    "(%s, %s, 'door', '[[0,0],[1,0],[1,1]]') RETURNING zone_id", (CAM_A, SITE))
        zone = cur.fetchone()[0]
    conn.commit()
    try:
        payload = {"entity": "zone_events", "select": "count", "start": CLIP1[0],
                   "end": CLIP1[1], "filters": [cam(CAM_A, CAM_B)]}
        a = ask(conn, payload, "partly zoned")
        assert a.coverage_pct <= 0.5 + 1e-6
        assert a.abstain_reason == "no_coverage"
        assert "no zone is drawn on Admin G329 indoor" in a.message
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM zones WHERE zone_id = %s", (zone,))
        conn.commit()

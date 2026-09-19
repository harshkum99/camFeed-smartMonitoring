"""Scoring an import against ground truth, and writing an analysed clip into the index.

Scoring is where the product's claims about itself come from, so a parser that silently counts a
vehicle as a person, or a metric that hides a person split across two track ids, would put a false
number in front of an evaluator. Writing is where a re-run could double every count, or a camera
of another site could leak tracks across a tenant boundary. The write tests run against their own
freshly migrated database and skip when no Postgres is reachable.
"""

from __future__ import annotations

import dataclasses
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smartcam.ingest.meva import meva_profile
from smartcam.ingest.score import (
    ClipScore,
    GtBox,
    annotation_files,
    capability,
    load_kpf,
    score_log,
)
from smartcam.ingest.write import (
    CameraRow,
    ClipRow,
    ImportRefused,
    TrackRow,
    bootstrap_site,
    clip_id,
    clip_uri,
    ensure_months,
    set_capability,
    track_id,
    write_clip,
)

ROOT = Path(__file__).resolve().parents[1]
DBNAME = "smartcam_ingest_test"


# --- KPF parsing ------------------------------------------------------------------------------

GEOM = """\
- {'meta': 'geom file'}
- {'geom': {'g0': '980 18 995 50', 'id0': 823, 'id1': 32, 'keyframe': True, 'ts0': 696}}
- {'geom': {'g0': '100 200 300 400', 'id0': 824, 'id1': 40, 'keyframe': True, 'ts0': 696}}
- {"geom": {"ts0": 700, "g0": "192 108 384 216", "id1": 33, "id0": 825}}
"""

TYPES = """\
- {'meta': 'types file'}
- {'types': {'cset3': {'person': 1.0}, 'id1': 32}}
- {'types': {'cset3': {'vehicle': 1.0}, 'id1': 40}}
- {"types": {"cset3": {"person": 0.8, "vehicle": 0.2}, "id1": 33}}
"""


@pytest.fixture
def kpf(tmp_path):
    geom, types = tmp_path / "x.geom.yml", tmp_path / "x.types.yml"
    geom.write_text(GEOM)
    types.write_text(TYPES)
    return geom, types


def test_load_kpf_keeps_people_and_normalises_boxes(kpf):
    """Boxes are compared against our normalised tracks, so pixel boxes would never match."""
    boxes = load_kpf(*kpf, width=1920, height=1080)
    assert boxes[0] == GtBox(32, 696, (980 / 1920, 18 / 1080, 995 / 1920, 50 / 1080))
    assert boxes[1] == GtBox(33, 700, (0.1, 0.1, 0.2, 0.2))
    assert len(boxes) == 2


def test_load_kpf_excludes_vehicles_from_person_ground_truth(kpf):
    """Counting a parked car as an annotated person would make our recall look worse than it is,
    and asking for vehicles must return only the vehicle."""
    assert 40 not in {b.track for b in load_kpf(*kpf, width=1920, height=1080)}
    vehicles = load_kpf(*kpf, width=1920, height=1080, cls="vehicle")
    assert [b.track for b in vehicles] == [40]


def test_load_kpf_accepts_reordered_keys_and_double_quotes(kpf):
    """KPF writers differ; a regex that relies on key order would drop whole tracks silently."""
    assert 33 in {b.track for b in load_kpf(*kpf, width=1920, height=1080)}


def test_annotation_files_match_the_video_stem(tmp_path):
    """Annotations share the video's name up to the camera id, and live in a nested tree."""
    stem = "2018-03-11.11-55-00.12-00-00.admin.G329"
    d = tmp_path / "annotations" / "2018-03-11" / "11"
    d.mkdir(parents=True)
    (d / f"{stem}.geom.yml").write_text("")
    (d / f"{stem}.types.yml").write_text("")
    found = annotation_files(tmp_path / "annotations", f"{stem}.r13.avi")
    assert found == (d / f"{stem}.geom.yml", d / f"{stem}.types.yml")
    assert annotation_files(tmp_path / "annotations", "other.r13.avi") is None


def test_annotation_files_need_the_types_file_too(tmp_path):
    """Geometry without classes cannot be filtered to people, so it is treated as absent."""
    (tmp_path / "v.geom.yml").write_text("")
    assert annotation_files(tmp_path, "v.r13.avi") is None


# --- scoring ----------------------------------------------------------------------------------

A = (0.1, 0.1, 0.2, 0.3)
B = (0.6, 0.6, 0.7, 0.9)


def _log() -> dict:
    """Two people over four frames. Person A is split across ids 'a' and 'b'; the detector misses
    person B on the last frame."""
    frames = []
    for n in range(4):
        dets = [[*A, 0.9]] + ([[*B, 0.8]] if n < 3 else [])
        frames.append({"n": n, "dets": dets})
    return {
        "file": "clip.r13.avi", "camera_key": "admin.G329", "step": 1,
        "width": 1920, "height": 1080, "frames": frames,
        "tracks": {
            "a": [[0, *A, 0.9], [1, *A, 0.9]],
            "b": [[2, *A, 0.9], [3, *A, 0.9]],
            "c": [[n, *B, 0.8] for n in range(4)],
        },
        "written": ["a", "b", "c"],
    }


def _gt() -> list[GtBox]:
    return [GtBox(1, n, A) for n in range(4)] + [GtBox(2, n, B) for n in range(4)]


def test_score_log_reports_a_split_person_as_more_than_one_id_each():
    """Fragmentation is where counting error lives: one person as two ids counts as two people.
    The score must show it rather than report both people simply 'found'."""
    s = score_log(_log(), _gt())
    assert (s.gt_people, s.gt_boxes, s.tracks_written) == (2, 8, 3)
    assert s.people_found == 2
    assert s.ids_per_person == 1.5
    assert s.people_per_id == 1.0
    assert s.box_recall == 0.875
    assert s.span_frames == 4


def test_score_log_ignores_tracks_that_were_not_written():
    """Only tracks that reached the index can answer a question, so only they are scored."""
    log = _log()
    log["written"] = ["a", "c"]
    s = score_log(log, _gt())
    assert s.ids_per_person == 1.0
    assert s.people_found == 2


def _score(recall: float | None, boxes: int) -> ClipScore:
    return ClipScore("f", "k", 1, boxes, 1, 1, 1.0, 1.0, recall, 10)


@pytest.mark.parametrize(("recall", "boxes", "status"), [
    (0.73, 100, "reliable"),
    (0.45, 200, "limited"),
    (0.05, 500, "unreliable"),
    (0.73, 99, "not_assessed"),
])
def test_capability_thresholds(recall, boxes, status):
    """The status is what the answer layer tells a customer; too few boxes is no evidence at all."""
    cap = capability([_score(recall, boxes)], detector_id="det", basis="meva")
    assert cap["status"] == status
    assert cap["gt_boxes"] == boxes
    assert cap["detector"] == "det"


def test_capability_weights_recall_by_boxes():
    """A short clip must not outvote a long one."""
    cap = capability([_score(0.9, 20), _score(0.1, 180)], detector_id="d", basis="b")
    assert cap["frame_recall"] == 0.18
    assert cap["status"] == "unreliable"


def test_capability_with_no_scores_is_not_assessed():
    assert capability([], detector_id="d", basis="b")["status"] == "not_assessed"


# --- writing ----------------------------------------------------------------------------------

psycopg = pytest.importorskip("psycopg")

DETECTOR = "yolo-test"
OTHER_TENANT = "22222222-2222-2222-2222-222222222222"
OTHER_SITE = "bbbbbbbb-0000-0000-0000-000000000001"
OTHER_CAM = "dddddddd-0000-0000-0000-000000000001"


def _t(h: int, m: int, s: int = 0) -> datetime:
    return datetime(2018, 3, 11, h, m, s, tzinfo=UTC)


@pytest.fixture(scope="module")
def db():
    """A fresh database of our own, migrated, with the MEVA site bootstrapped once."""
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
    c = psycopg.connect(f"dbname={DBNAME}", autocommit=True)
    try:
        bootstrap_site(c, meva_profile(), _cams(), DETECTOR)
        ensure_months(c, [_t(15, 55)])
        with c.cursor() as cur:
            cur.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, 'other')",
                        (OTHER_TENANT,))
            cur.execute("INSERT INTO sites (site_id, tenant_id, name) VALUES (%s, %s, 'other')",
                        (OTHER_SITE, OTHER_TENANT))
            cur.execute("INSERT INTO cameras (camera_id, tenant_id, site_id, name) "
                        "VALUES (%s, %s, %s, 'other cam')", (OTHER_CAM, OTHER_TENANT, OTHER_SITE))
        yield c
    finally:
        c.close()


def _cams() -> list[CameraRow]:
    return [CameraRow(cid, key, name, 1920, 1080, 30.0, "h264", "detection", [])
            for key, (cid, name) in meva_profile().cameras.items()]


def _cam(key: str = "admin.G329") -> str:
    return meva_profile().cameras[key][0]


def _one(conn, sql: str, *args):
    with conn.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchone()[0]


def _clip(sha: str, start: datetime, analysed_end: datetime, cam: str | None = None) -> ClipRow:
    cam = cam or _cam()
    p = meva_profile()
    return ClipRow(
        clip_id=clip_id(p.site_id, "admin.G329", sha), camera_id=cam, ts_start=start,
        analysed_end=analysed_end, file_start=start, file_end=start + timedelta(minutes=5),
        keyframe_uris=[], frame_sha256=[], source_uri=f"s3://bucket/{sha[:8]}.avi",
        source_sha256=sha, uptime_detail="test import")


def _track(sha: str, local: int, start: datetime, cam: str | None = None,
           fingerprint: str = "cfg1") -> TrackRow:
    cam = cam or _cam()
    end = start + timedelta(seconds=12)
    box = [0.1, 0.1, 0.2, 0.3]
    return TrackRow(
        track_id=track_id(meva_profile().site_id, "admin.G329", sha, fingerprint, local),
        camera_id=cam, cls="person", ts_start=start, ts_end=end, conf_max=0.9, conf_mean=0.7,
        n_frames=24, bbox_first=box, bbox_last=box, keyframe_key=None, keyframe_sha256=None,
        keyframe_ts=start + timedelta(seconds=3), keyframe_bbox=box, clip_uri=clip_uri(sha),
        model_versions={"detector": DETECTOR})


def test_bootstrap_twice_does_not_duplicate(db):
    """Bootstrap runs at the start of every import; it must converge, not accumulate."""
    p = meva_profile()
    bootstrap_site(db, p, _cams(), DETECTOR)
    assert _one(db, "SELECT count(*) FROM tenants WHERE tenant_id = %s", p.tenant_id) == 1
    assert _one(db, "SELECT count(*) FROM sites WHERE site_id = %s", p.site_id) == 1
    assert _one(db, "SELECT count(*) FROM cameras WHERE site_id = %s", p.site_id) == len(p.cameras)


def test_bootstrap_refuses_a_different_timezone(db):
    """A changed timezone would put every track at the wrong time of day."""
    with pytest.raises(ImportRefused):
        bootstrap_site(db, dataclasses.replace(meva_profile(), tz="UTC"), _cams(), DETECTOR)
    assert _one(db, "SELECT tz FROM sites WHERE site_id = %s",
                meva_profile().site_id) == "America/New_York"


def test_ensure_months_creates_the_partition(db):
    """An insert into a month with no partition fails the whole clip."""
    ensure_months(db, [_t(15, 55)])
    assert _one(db, "SELECT to_regclass('tracks_201803') IS NOT NULL")


def test_reimport_replaces_rather_than_accumulates(db):
    """A second run of the same file must not double every count."""
    sha = "a" * 64
    start = _t(15, 55)
    write_clip(db, meva_profile(), _clip(sha, start, _t(16, 0)),
               [_track(sha, 1, start), _track(sha, 2, start + timedelta(seconds=30))])
    write_clip(db, meva_profile(), _clip(sha, start, _t(15, 58)),
               [_track(sha, 1, start, fingerprint="cfg2")])
    uri = clip_uri(sha)
    assert _one(db, "SELECT count(*) FROM tracks WHERE clip_uri = %s", uri) == 1
    assert _one(db, "SELECT count(*) FROM clips WHERE source_sha256 = %s", sha) == 1
    cid = _clip(sha, start, start).clip_id
    assert _one(db, "SELECT count(*) FROM camera_uptime WHERE clip_id = %s "
                    "AND source = 'recorded_import'", cid) == 1


def test_uptime_ends_where_analysis_ended_and_carries_tenant(db):
    """Uptime past the analysed end would claim coverage for footage nobody looked at."""
    sha = "b" * 64
    start = _t(16, 0)
    clip = _clip(sha, start, _t(16, 2))
    write_clip(db, meva_profile(), clip, [_track(sha, 1, start)])
    with db.cursor() as cur:
        cur.execute("SELECT ts_end, tenant_id FROM camera_uptime WHERE clip_id = %s",
                    (clip.clip_id,))
        ts_end, tenant = cur.fetchone()
    assert ts_end == clip.analysed_end
    assert str(tenant) == meva_profile().tenant_id


def test_track_on_another_sites_camera_is_refused(db):
    """A mis-attributed track is a cross-tenant disclosure; nothing from the clip may land."""
    sha = "c" * 64
    start = _t(16, 5)
    with pytest.raises(ImportRefused):
        write_clip(db, meva_profile(), _clip(sha, start, _t(16, 10)),
                   [_track(sha, 1, start, cam=OTHER_CAM)])
    with pytest.raises(ImportRefused):
        write_clip(db, meva_profile(), _clip(sha, start, _t(16, 10), cam=OTHER_CAM),
                   [_track(sha, 1, start, cam=OTHER_CAM)])
    assert _one(db, "SELECT count(*) FROM tracks WHERE clip_uri = %s", clip_uri(sha)) == 0
    assert _one(db, "SELECT count(*) FROM clips WHERE source_sha256 = %s", sha) == 0


def test_as_of_follows_latest_uptime_and_moves_back(db):
    """'This morning' at a recorded site means relative to its footage; a re-import that analyses
    less must not leave the site answering as of a time we no longer hold."""
    p = meva_profile()
    sha = "d" * 64
    start = _t(20, 0)
    write_clip(db, p, _clip(sha, start, _t(20, 5)), [_track(sha, 1, start)])
    max_end = "SELECT max(ts_end) FROM camera_uptime WHERE site_id = %s"
    as_of = "SELECT as_of FROM sites WHERE site_id = %s"
    assert _one(db, as_of, p.site_id) == _t(20, 5) == _one(db, max_end, p.site_id)
    write_clip(db, p, _clip(sha, start, _t(20, 2)), [_track(sha, 1, start)])
    assert _one(db, as_of, p.site_id) == _t(20, 2) == _one(db, max_end, p.site_id)


def test_written_tracks_carry_no_attributes(db):
    """Recorded import runs no attribute model; any attrs key would be an unmeasured claim."""
    sha = "e" * 64
    write_clip(db, meva_profile(), _clip(sha, _t(16, 20), _t(16, 25)),
               [_track(sha, 1, _t(16, 20))])
    assert _one(db, "SELECT count(*) FROM tracks") > 0
    assert _one(db, "SELECT count(*) FROM tracks WHERE attrs <> '{}'::jsonb") == 0


def test_set_capability_creates_classes_when_missing(db):
    """jsonb_set does not create intermediate keys, so a camera without 'classes' would silently
    keep no capability at all."""
    cam = _cam("bus.G340")
    with db.cursor() as cur:
        cur.execute("UPDATE cameras SET capabilities = %s WHERE camera_id = %s",
                    ('{"detector": "x"}', cam))
    set_capability(db, camera_id=cam, cls="person", value={"status": "unreliable"})
    caps = _one(db, "SELECT capabilities FROM cameras WHERE camera_id = %s", cam)
    assert caps == {"detector": "x", "classes": {"person": {"status": "unreliable"}}}


def test_reimport_of_the_same_footage_under_a_new_hash_replaces_the_old_rows(db):
    """A partial download re-imported once complete has a different hash but the same footage.
    Keying replacement on the hash alone counted everyone in the overlap twice."""
    old, new = "d" * 64, "e" * 64
    start = _t(17, 10)
    write_clip(db, meva_profile(), _clip(old, start, _t(17, 12)), [_track(old, 1, start)])
    write_clip(db, meva_profile(), _clip(new, start, _t(17, 15)),
               [_track(new, 1, start), _track(new, 2, start + timedelta(seconds=90))])
    window = (start - timedelta(minutes=1), start + timedelta(minutes=6))
    assert _one(db, "SELECT count(*) FROM tracks WHERE camera_id = %s AND ts_start >= %s "
                    "AND ts_start < %s", _cam(), *window) == 2
    assert _one(db, "SELECT count(*) FROM clips WHERE camera_id = %s AND ts_start >= %s "
                    "AND ts_start < %s", _cam(), *window) == 1
    assert _one(db, "SELECT count(*) FROM camera_uptime WHERE camera_id = %s "
                    "AND source = 'recorded_import' AND ts_start >= %s AND ts_start < %s",
                _cam(), *window) == 1


def test_clip_whose_uptime_cannot_be_written_writes_nothing(db):
    """Tracks without the uptime row that makes them answerable would sit inside a coverage gap.
    The uptime insert used to be skipped silently while the tracks committed."""
    sha = "f" * 64
    start = _t(17, 20)
    with db.cursor() as cur:
        cur.execute("INSERT INTO camera_uptime (camera_id, ts_start, ts_end, state) "
                    "VALUES (%s, %s, %s, 'online')", (_cam(), start, start + timedelta(hours=1)))
    db.commit()
    with pytest.raises(ImportRefused, match="uptime row already starts"):
        write_clip(db, meva_profile(), _clip(sha, start, _t(17, 25)), [_track(sha, 1, start)])
    db.rollback()
    assert _one(db, "SELECT count(*) FROM tracks WHERE clip_uri = %s", clip_uri(sha)) == 0
    assert _one(db, "SELECT count(*) FROM clips WHERE source_sha256 = %s", sha) == 0


def test_reimporting_a_shorter_file_removes_every_track_of_the_longer_one(db):
    """An hour-long import replaced by a ten-minute re-export used to keep the tracks from the
    rest of the hour, citing a recording row that no longer existed."""
    old, new = "7" * 64, "8" * 64
    start = _t(18, 0)
    long_clip = dataclasses.replace(_clip(old, start, _t(19, 0)),
                                    file_end=start + timedelta(hours=1))
    write_clip(db, meva_profile(), long_clip,
               [_track(old, 1, start), _track(old, 2, start + timedelta(minutes=30))])
    write_clip(db, meva_profile(), _clip(new, start, _t(18, 10)), [_track(new, 1, start)])
    assert _one(db, "SELECT count(*) FROM tracks WHERE clip_uri = %s", clip_uri(old)) == 0
    assert _one(db, "SELECT count(*) FROM tracks WHERE clip_uri = %s", clip_uri(new)) == 1


def test_hash_time_is_when_the_file_was_hashed_and_survives_reimport(db):
    """The hash report states when the hash was taken. That is not when a row was written, and
    re-importing the same bytes does not make them newly received."""
    sha = "9" * 64
    start = _t(18, 30)
    hashed = datetime(2026, 9, 1, 4, 0, tzinfo=UTC)
    write_clip(db, meva_profile(),
               dataclasses.replace(_clip(sha, start, _t(18, 35)), source_hashed_at=hashed),
               [_track(sha, 1, start)])
    later = datetime(2026, 9, 5, 4, 0, tzinfo=UTC)
    write_clip(db, meva_profile(),
               dataclasses.replace(_clip(sha, start, _t(18, 35)), source_hashed_at=later),
               [_track(sha, 1, start)])
    assert _one(db, "SELECT imported_at FROM clips WHERE source_sha256 = %s", sha) == hashed

"""The evidence-bundle routes of the console API: create, list, detail, archive, certificate.

These routes hand out real footage and write a chain of custody, so the tests pin down three
things: scope comes from server configuration and never from x-smartcam-* headers; every access
that matters leaves a custody row; and an archive that is no longer the bytes sealed at creation
is refused, not served.

The certificate renderer is replaced by a stub returning a fixed PDF, so these tests are about
the routes — scope, custody, integrity — and not about the document's layout, which
tests/test_evidence_certificate.py covers with the real renderer.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import types
import uuid
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
psycopg = pytest.importorskip("psycopg")
np = pytest.importorskip("numpy")

from fastapi.testclient import TestClient  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DBNAME = "smartcam_evidence_api_test"
DSN = f"dbname={DBNAME}"

TENANT = "22222222-2222-2222-2222-222222222222"
SITE = "bbbbbbbb-0000-0000-0000-000000000001"
CAM_A = "dddddddd-0000-0000-0000-00000000000a"
OTHER_TENANT = "33333333-3333-3333-3333-333333333333"
OTHER_SITE = "bbbbbbbb-0000-0000-0000-000000000002"
OTHER_CAM = "dddddddd-0000-0000-0000-0000000000ff"

TRACK_1 = "eeeeeeee-0000-0000-0000-0000000000a1"
TRACK_2 = "eeeeeeee-0000-0000-0000-0000000000a2"
TRACK_OTHER = "eeeeeeee-0000-0000-0000-0000000000ff"

CLIP = ("2018-03-11T11:55:00-04:00", "2018-03-11T12:00:00-04:00")
SOURCE_NAME = "2018-03-11.11-55-00.admin.G326.avi"
OTHER_SOURCE_NAME = "other-tenant.avi"
FAKE_PDF = b"%PDF-1.4 fake"
ACTOR = "inspector.rao"


def _fake_certificate_module() -> types.ModuleType:
    mod = types.ModuleType("smartcam.evidence.certificate")

    def render_certificate(manifest, *, draft_id="D1", generated_at=None, custody=None):
        assert manifest["merkle"]["root"] and manifest["manifest_sha256"]
        return FAKE_PDF if draft_id == "D1" else FAKE_PDF + draft_id.encode()

    mod.render_certificate = render_certificate
    return mod


def _create_db() -> None:
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


def _seed(data: Path, footage: Path) -> None:
    from smartcam.ingest.keyframes import KeyframeStore

    footage.mkdir(parents=True, exist_ok=True)
    src = footage / SOURCE_NAME
    src.write_bytes(b"RIFF fake recorder bytes " * 64)
    src_sha = hashlib.sha256(src.read_bytes()).hexdigest()
    other = footage / OTHER_SOURCE_NAME
    other.write_bytes(b"another customer's recording")
    other_sha = hashlib.sha256(other.read_bytes()).hexdigest()

    store = KeyframeStore(data)
    frames = {}
    for i, (tid, cam, tenant, site) in enumerate((
        (TRACK_1, CAM_A, TENANT, SITE), (TRACK_2, CAM_A, TENANT, SITE),
        (TRACK_OTHER, OTHER_CAM, OTHER_TENANT, OTHER_SITE),
    )):
        rgb = np.full((16, 16, 3), 40 * (i + 1), dtype=np.uint8)
        frames[tid] = store.put(rgb, tenant_id=tenant, site_id=site, camera_id=cam)

    c = psycopg.connect(DSN)
    with c, c.cursor() as cur:
        for tenant, site in ((TENANT, SITE), (OTHER_TENANT, OTHER_SITE)):
            cur.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
                        (tenant, f"tenant {tenant[:4]}"))
            cur.execute("INSERT INTO sites (site_id, tenant_id, name, tz) "
                        "VALUES (%s, %s, %s, 'America/New_York')",
                        (site, tenant, f"site {site[-1]}"))
        for cam, name, tenant, site in ((CAM_A, "Admin G326 indoor", TENANT, SITE),
                                        (OTHER_CAM, "Other camera", OTHER_TENANT, OTHER_SITE)):
            cur.execute("INSERT INTO cameras (camera_id, tenant_id, site_id, name) "
                        "VALUES (%s, %s, %s, %s)", (cam, tenant, site, name))
        cur.execute("SELECT ensure_partitions('2018-03-01', '2018-03-01')")
        for cam, tenant, site, name, sha in (
            (CAM_A, TENANT, SITE, SOURCE_NAME, src_sha),
            (OTHER_CAM, OTHER_TENANT, OTHER_SITE, OTHER_SOURCE_NAME, other_sha),
        ):
            cur.execute("INSERT INTO clips (tenant_id, site_id, camera_id, ts_start, ts_end, "
                        "source_uri, source_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        (tenant, site, cam, CLIP[0], CLIP[1], f"file:///dvr/{name}", sha))
            cur.execute("INSERT INTO camera_uptime (camera_id, site_id, tenant_id, ts_start, "
                        "ts_end, state, source) VALUES (%s,%s,%s,%s,%s,'online',"
                        "'recorded_import')", (cam, site, tenant, CLIP[0], CLIP[1]))
        for tid, cam, tenant, site, sha in (
            (TRACK_1, CAM_A, TENANT, SITE, src_sha), (TRACK_2, CAM_A, TENANT, SITE, src_sha),
            (TRACK_OTHER, OTHER_CAM, OTHER_TENANT, OTHER_SITE, other_sha),
        ):
            f = frames[tid]
            cur.execute(
                "INSERT INTO tracks (track_id, tenant_id, site_id, camera_id, class, ts_start, "
                "ts_end, dwell_s, conf_max, conf_mean, n_frames, attrs, best_keyframe_uri, "
                "frame_sha256, keyframe_ts, keyframe_bbox, clip_uri) VALUES (%s,%s,%s,%s,"
                "'person','2018-03-11T11:56:00-04:00','2018-03-11T11:57:00-04:00',60,0.9,0.8,"
                "30,'{}',%s,%s,'2018-03-11T11:56:30-04:00',%s::jsonb,%s)",
                (tid, tenant, site, cam, f.key, f.sha256, json.dumps([0.1, 0.1, 0.3, 0.5]),
                 f"sha256:{sha}"))


@pytest.fixture(scope="module")
def env(tmp_path_factory):
    """A fresh database, footage and keyframes on disk, and the app pointed at all of them."""
    _create_db()
    base = tmp_path_factory.mktemp("evidence_api")
    data, footage = base / "data", base / "footage"
    _seed(data, footage)

    from smartcam.api import app as app_module
    from smartcam.api import session as session_module

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(app_module, "DSN", DSN)
        mp.setattr(session_module, "DEFAULT_TENANT", TENANT)
        mp.setattr(session_module, "DEFAULT_SITE", SITE)
        mp.setenv("SMARTCAM_DATA_DIR", str(data))
        mp.setenv("SMARTCAM_FOOTAGE_DIR", str(footage))
        mp.setitem(sys.modules, "smartcam.evidence.certificate", _fake_certificate_module())
        yield types.SimpleNamespace(client=TestClient(app_module.app), data=data)


def _post(client, track_ids, purpose="Incident review for the site manager", **headers):
    return client.post("/api/evidence/bundles",
                       json={"track_ids": track_ids, "purpose": purpose,
                             "question": "Who was at Admin G326?"},
                       headers=headers or {"x-smartcam-actor": ACTOR})


def _query(sql, args=()):
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute(sql, args)
        return cur.fetchall()


def _custody_actions(bundle_id):
    return [a for (a,) in _query("SELECT action FROM custody_log WHERE bundle_id = %s "
                                 "ORDER BY at, entry_id", (bundle_id,))]


@pytest.fixture(scope="module")
def bundle(env):
    r = _post(env.client, [TRACK_1, TRACK_2])
    assert r.status_code == 200, r.text
    return r.json()


BUG_POST = ("BUG: smartcam/api/app.py:388 create_evidence_bundle never commits; the db() "
            "dependency (app.py:88) closes the connection, so the bundle row and its 'created' "
            "custody entry are rolled back while the archive stays on disk")
BUG_GET = ("BUG: smartcam/api/app.py:436/438/458 log_custody runs inside the implicit transaction "
           "opened by load_bundle's SELECT, becomes a savepoint, and is rolled back when db() "
           "closes the connection without commit: 'exported', 'export_refused' and 'viewed' "
           "custody rows are lost")


@pytest.fixture(scope="module")
def stored(env):
    """A bundle committed through the service directly, so the read routes can be tested
    independently of whether the POST route persists what it builds."""
    from smartcam.evidence import service

    with psycopg.connect(DSN) as c:
        built = service.create_bundle(c, service.BundleRequest(
            tenant_id=TENANT, site_id=SITE, requested_by=ACTOR, purpose="Stored for read tests",
            track_ids=[TRACK_1, TRACK_2]), data_dir=env.data)
        c.commit()
    return built


# --- create -----------------------------------------------------------------------------------

def test_create_returns_sealed_summary_with_source_included(bundle):
    """The operator needs the bundle id and root at once; a source cited by hash only is a warning
    they must see, so the happy path must show the recording was actually included."""
    uuid.UUID(bundle["bundle_id"])
    assert len(bundle["merkle_root"]) == 64
    assert bundle["sources"][0]["file"] == SOURCE_NAME
    assert bundle["sources"][0]["included"] is True
    assert bundle["certificate"] is True
    assert bundle["tracks"] == 2 and bundle["frames"] == 2
    assert bundle["requested_by"] == ACTOR


def test_create_is_persisted_with_actor_and_created_custody_row(env, bundle):
    """The response comes from the request's own connection; only a committed row proves the
    bundle, and its 'created' custody entry, survive the request."""
    rows = _query("SELECT requested_by, archive_sha256 FROM evidence_bundles "
                  "WHERE bundle_id = %s", (bundle["bundle_id"],))
    assert rows == [(ACTOR, bundle["archive_sha256"])]
    custody = _query("SELECT actor, action FROM custody_log WHERE bundle_id = %s",
                     (bundle["bundle_id"],))
    assert (ACTOR, "created") in custody


def test_created_bundle_appears_in_list(env, bundle):
    """What the operator just prepared must be findable again from the console."""
    ids = [b["bundle_id"] for b in env.client.get("/api/evidence/bundles").json()]
    assert bundle["bundle_id"] in ids


def test_foreign_tenant_track_is_409_and_writes_nothing(env):
    """A track id from another customer must not be packaged, and the refusal must leave no
    archive or row behind."""
    before = _query("SELECT count(*) FROM evidence_bundles")[0][0]
    zips_before = set(env.data.rglob("*.zip"))
    for ids in ([TRACK_OTHER], [TRACK_1, TRACK_OTHER]):
        r = _post(env.client, ids)
        assert r.status_code == 409, r.text
    assert _query("SELECT count(*) FROM evidence_bundles")[0][0] == before
    assert set(env.data.rglob("*.zip")) == zips_before


def test_empty_purpose_is_422(env):
    """The purpose is printed on the certificate; a blank one is a validation error."""
    r = _post(env.client, [TRACK_1], purpose="")
    assert r.status_code == 422


def test_whitespace_purpose_is_refused(env):
    """Three spaces pass the length check but say nothing; the service must still refuse."""
    before = _query("SELECT count(*) FROM evidence_bundles")[0][0]
    r = _post(env.client, [TRACK_1], purpose="   ")
    assert 400 <= r.status_code < 500
    assert _query("SELECT count(*) FROM evidence_bundles")[0][0] == before


# --- read -------------------------------------------------------------------------------------

def test_list_returns_the_bundle(env, stored):
    r = env.client.get("/api/evidence/bundles", headers={"x-smartcam-actor": ACTOR})
    assert r.status_code == 200
    listed = {b["bundle_id"]: b for b in r.json()}
    assert listed[stored.bundle_id]["merkle_root"] == stored.merkle_root
    assert listed[stored.bundle_id]["certificate"] is True


def test_detail_includes_custody(env, stored):
    """The detail view is where the chain of custody is read; 'created' must head it."""
    r = env.client.get(f"/api/evidence/bundles/{stored.bundle_id}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["merkle_root"] == stored.merkle_root
    assert body["custody"][0]["action"] == "created"
    assert body["custody"][0]["actor"] == ACTOR


def test_archive_download_matches_stored_hash(env, stored):
    """The header lets the recipient check the bytes they were handed against the record."""
    r = env.client.get(f"/api/evidence/bundles/{stored.bundle_id}/archive",
                       headers={"x-smartcam-actor": "exporter"})
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/zip"
    db_hash = _query("SELECT archive_sha256 FROM evidence_bundles WHERE bundle_id = %s",
                     (stored.bundle_id,))[0][0]
    assert r.headers["x-archive-sha256"] == db_hash == stored.archive_sha256
    assert hashlib.sha256(r.content).hexdigest() == db_hash


def test_archive_download_appends_exported_custody_row(env, stored):
    """Every download is a custody entry; a bundle that leaves without one breaks the chain."""
    r = env.client.get(f"/api/evidence/bundles/{stored.bundle_id}/archive",
                       headers={"x-smartcam-actor": "exporter"})
    assert r.status_code == 200
    assert ("exporter", "exported") in _query(
        "SELECT actor, action FROM custody_log WHERE bundle_id = %s", (stored.bundle_id,))


def test_certificate_is_served_as_pdf(env, stored):
    r = env.client.get(f"/api/evidence/bundles/{stored.bundle_id}/certificate")
    assert r.status_code == 200, r.text
    assert r.headers["content-type"] == "application/pdf"
    assert r.content == FAKE_PDF


def test_certificate_view_appends_viewed_custody_row(env, stored):
    r = env.client.get(f"/api/evidence/bundles/{stored.bundle_id}/certificate")
    assert r.status_code == 200
    assert "viewed" in _custody_actions(stored.bundle_id)


@pytest.fixture(scope="module")
def corrupted(env, stored):
    """A second committed bundle whose zip is then altered on disk."""
    from smartcam.evidence import service

    with psycopg.connect(DSN) as c:
        built = service.create_bundle(c, service.BundleRequest(
            tenant_id=TENANT, site_id=SITE, requested_by=ACTOR, purpose="To be tampered with",
            track_ids=[TRACK_1]), data_dir=env.data)
        c.commit()
    with built.archive.open("ab") as f:
        f.write(b"tampered")
    return built


def test_corrupted_archive_is_refused(env, corrupted):
    """A zip that is no longer the sealed bytes must not leave the system."""
    r = env.client.get(f"/api/evidence/bundles/{corrupted.bundle_id}/archive")
    assert r.status_code == 422
    assert "exported" not in _custody_actions(corrupted.bundle_id)
    r = env.client.get(f"/api/evidence/bundles/{corrupted.bundle_id}/certificate")
    assert r.status_code == 422


def test_corrupted_archive_refusal_appends_export_refused(env, corrupted):
    """The refused attempt is itself part of the chain of custody."""
    r = env.client.get(f"/api/evidence/bundles/{corrupted.bundle_id}/archive")
    assert r.status_code == 422
    assert "export_refused" in _custody_actions(corrupted.bundle_id)


# --- scope ------------------------------------------------------------------------------------

@pytest.fixture(scope="module")
def foreign_bundle(env):
    """Another tenant's bundle, inserted directly: the API must never reach it."""
    bid = str(uuid.uuid4())
    with psycopg.connect(DSN) as c, c.cursor() as cur:
        cur.execute(
            "INSERT INTO evidence_bundles (bundle_id, tenant_id, site_id, requested_by, purpose, "
            "window_start, window_end, cameras, frame_count, merkle_root, archive_sha256, "
            "certificate_sha256) VALUES (%s,%s,%s,'x','other',%s,%s,%s::uuid[],1,%s,%s,%s)",
            (bid, OTHER_TENANT, OTHER_SITE, CLIP[0], CLIP[1], [OTHER_CAM], "0" * 64, "1" * 64,
             "2" * 64))
    return bid


@pytest.mark.parametrize("suffix", ["", "/archive", "/certificate"])
def test_other_tenant_bundle_is_404_even_with_its_headers(env, foreign_bundle, suffix):
    """The scope headers are a convenience for the console, not a key to another customer."""
    headers = {"x-smartcam-tenant": OTHER_TENANT, "x-smartcam-site": OTHER_SITE}
    r = env.client.get(f"/api/evidence/bundles/{foreign_bundle}{suffix}", headers=headers)
    assert r.status_code == 404
    assert _custody_actions(foreign_bundle) == []


def test_list_excludes_other_tenant_bundle(env, foreign_bundle):
    headers = {"x-smartcam-tenant": OTHER_TENANT, "x-smartcam-site": OTHER_SITE}
    ids = [b["bundle_id"] for b in env.client.get("/api/evidence/bundles", headers=headers).json()]
    assert foreign_bundle not in ids


@pytest.mark.parametrize("suffix", ["", "/archive", "/certificate"])
def test_non_uuid_bundle_id_is_422(env, suffix):
    r = env.client.get(f"/api/evidence/bundles/not-a-uuid{suffix}")
    assert r.status_code == 422


def test_each_new_draft_is_numbered_and_logged_with_its_hash(env, stored):
    """s.63(4) asks for a certificate at each instance of submission: every draft handed out is
    distinct, numbered, and recorded before it leaves."""
    url = f"/api/evidence/bundles/{stored.bundle_id}/drafts"
    first = env.client.post(url, headers={"x-smartcam-actor": "clerk"})
    second = env.client.post(url)
    assert first.status_code == second.status_code == 200
    assert first.headers["content-type"] == "application/pdf"
    assert first.headers["cache-control"] == "no-store"
    n1, n2 = first.headers["x-draft-id"], second.headers["x-draft-id"]
    assert n1 != n2 and n1.startswith("D") and int(n2[1:]) == int(n1[1:]) + 1
    logged = _query("SELECT actor, detail FROM custody_log WHERE bundle_id = %s AND "
                    "action = 'draft_generated' ORDER BY at", (stored.bundle_id,))
    by_id = {d["draft_id"]: (a, d["sha256"]) for a, d in logged}
    assert by_id[n1] == ("clerk", hashlib.sha256(first.content).hexdigest())
    assert by_id[n2][1] == hashlib.sha256(second.content).hexdigest()


def test_a_get_never_issues_a_draft(env, stored):
    """A reload, a link preview or a PDF viewer re-fetching must not use up a draft number."""
    before = _custody_actions(stored.bundle_id).count("draft_generated")
    r = env.client.get(f"/api/evidence/bundles/{stored.bundle_id}/drafts")
    assert r.status_code in (404, 405)
    assert _custody_actions(stored.bundle_id).count("draft_generated") == before


def test_concurrent_draft_requests_get_distinct_numbers(env, stored):
    """Two tabs, or a double click, must not both be handed D-something with the same number."""
    from concurrent.futures import ThreadPoolExecutor

    url = f"/api/evidence/bundles/{stored.bundle_id}/drafts"
    with ThreadPoolExecutor(4) as pool:
        responses = list(pool.map(lambda _: env.client.post(url), range(4)))
    ids = [r.headers["x-draft-id"] for r in responses]
    assert all(r.status_code == 200 for r in responses)
    assert len(set(ids)) == 4

"""Evidence bundles, end to end: build, verify by hand, tamper, refuse, record, serve.

A bundle is handed to people who do not trust us, so the tests check it the way they would: by
unzipping it and running `shasum` and the bundled `verify_bundle.py`, not by asking our own code
whether it is happy. The refusals matter as much as the successes — a bundle silently missing a
recording, or citing another customer's track, is worse than no bundle — and every refusal must
leave nothing behind on disk.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
import uuid
import zipfile
from pathlib import Path

import pytest

psycopg = pytest.importorskip("psycopg")
np = pytest.importorskip("numpy")

from smartcam.evidence import cli, service  # noqa: E402
from smartcam.evidence.bundle import (  # noqa: E402
    BundleRefused,
    BundleRequest,
    build_bundle,
)
from smartcam.evidence.merkle import merkle_root  # noqa: E402
from smartcam.ingest.keyframes import KeyframeStore  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
DBNAME = "smartcam_evidence_test"
DSN = f"dbname={DBNAME}"

TENANT = "44444444-4444-4444-4444-444444444444"
SITE = "cccccccc-0000-0000-0000-000000000001"
CAM = "ffffffff-0000-0000-0000-00000000000a"
CAM_NAME = "Admin G329 indoor"
CAM_DIR = "Admin_G329_indoor"                     # the camera name made path-safe
OTHER_TENANT = "55555555-5555-5555-5555-555555555555"
OTHER_SITE = "cccccccc-0000-0000-0000-000000000002"
OTHER_CAM = "ffffffff-0000-0000-0000-0000000000ff"

TRACKS = [f"abababab-0000-0000-0000-00000000000{i}" for i in (1, 2, 3)]
SYNTHETIC = "abababab-0000-0000-0000-0000000000aa"
OTHER_TRACK = "abababab-0000-0000-0000-0000000000ff"
# Two clips from the same camera, an hour apart, both exported as cam.avi (DVR style).
HOURLY = ["abababab-0000-0000-0000-0000000000c1", "abababab-0000-0000-0000-0000000000c2"]

RECORDING = b"RIFF\x00\x00\x00\x00AVI fake recorder bytes " * 64
RECORDING_SHA = hashlib.sha256(RECORDING).hexdigest()
SOURCE_URI = "s3://bucket/drops/2018-03-11/12/cam.avi"
HOUR13 = b"RIFF second hour of recording " * 50
HOUR13_SHA = hashlib.sha256(HOUR13).hexdigest()


def frame_rgb(i: int):
    """A distinct small picture per track, so each evidence frame has its own hash."""
    a = np.zeros((24, 32, 3), dtype=np.uint8)
    a[..., i % 3] = 40 + 60 * i
    return a


def put_frames(data_dir: Path):
    store = KeyframeStore(data_dir)
    return [store.put(frame_rgb(i), tenant_id=TENANT, site_id=SITE, camera_id=CAM)
            for i in range(3)]


@pytest.fixture(scope="module")
def conn(tmp_path_factory):
    """A fresh database with every migration and one recorded clip. Skips with no Postgres."""
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

    frames = put_frames(tmp_path_factory.mktemp("seed-frames"))
    c = psycopg.connect(DSN, autocommit=True)
    with c.cursor() as cur:
        for tenant, site in ((TENANT, SITE), (OTHER_TENANT, OTHER_SITE)):
            cur.execute("INSERT INTO tenants (tenant_id, name) VALUES (%s, %s)",
                        (tenant, f"tenant {tenant[:4]}"))
            cur.execute("INSERT INTO sites (site_id, tenant_id, name, tz) "
                        "VALUES (%s, %s, %s, 'America/New_York')",
                        (site, tenant, f"site {site[-1]}"))
        for cam, name, tenant, site in ((CAM, CAM_NAME, TENANT, SITE),
                                        (OTHER_CAM, "Other camera", OTHER_TENANT, OTHER_SITE)):
            cur.execute("INSERT INTO cameras (camera_id, tenant_id, site_id, name) "
                        "VALUES (%s, %s, %s, %s)", (cam, tenant, site, name))
        cur.execute("SELECT ensure_partitions('2018-03-01', '2018-03-01')")
        cur.execute("INSERT INTO clips (tenant_id, site_id, camera_id, ts_start, ts_end, "
                    "source_uri, source_sha256) VALUES (%s, %s, %s, "
                    "'2018-03-11T11:55:00-04:00', '2018-03-11T12:00:00-04:00', %s, %s)",
                    (TENANT, SITE, CAM, SOURCE_URI, RECORDING_SHA))
        cur.execute("INSERT INTO clips (tenant_id, site_id, camera_id, ts_start, ts_end, "
                    "source_uri, source_sha256) VALUES (%s, %s, %s, "
                    "'2018-03-11T13:00:00-04:00', '2018-03-11T13:05:00-04:00', %s, %s)",
                    (TENANT, SITE, CAM, "s3://bucket/drops/2018-03-11/13/cam.avi", HOUR13_SHA))
        cur.execute("INSERT INTO camera_uptime (camera_id, site_id, tenant_id, ts_start, ts_end, "
                    "state, source) VALUES (%s, %s, %s, '2018-03-11T11:55:00-04:00', "
                    "'2018-03-11T13:05:00-04:00', 'online', 'recorded_import')",
                    (CAM, SITE, TENANT))

        def track(tid, tenant, site, cam, clip_uri, minute, frame=None):
            cur.execute(
                "INSERT INTO tracks (track_id, tenant_id, site_id, camera_id, class, ts_start, "
                "ts_end, dwell_s, conf_max, conf_mean, n_frames, attrs, best_keyframe_uri, "
                "frame_sha256, keyframe_ts, keyframe_bbox, clip_uri) VALUES (%s, %s, %s, %s, "
                "'person', %s, %s, 30, 0.9, 0.8, 20, '{}', %s, %s, %s, %s::jsonb, %s)",
                (tid, tenant, site, cam, f"2018-03-11T{minute}:00-04:00",
                 f"2018-03-11T{minute}:30-04:00", frame.key if frame else None,
                 frame.sha256 if frame else None,
                 f"2018-03-11T{minute}:10-04:00" if frame else None,
                 json.dumps([10, 20, 50, 90]) if frame else None, clip_uri))

        for i, (tid, fr) in enumerate(zip(TRACKS, frames, strict=True)):
            track(tid, TENANT, SITE, CAM, f"sha256:{RECORDING_SHA}", f"11:5{6 + i}", fr)
        track(SYNTHETIC, TENANT, SITE, CAM, None, "11:56")
        track(OTHER_TRACK, OTHER_TENANT, OTHER_SITE, OTHER_CAM, f"sha256:{RECORDING_SHA}",
              "11:56")
        track(HOURLY[0], TENANT, SITE, CAM, f"sha256:{RECORDING_SHA}", "11:57")
        track(HOURLY[1], TENANT, SITE, CAM, f"sha256:{HOUR13_SHA}", "13:01")
    try:
        yield c
    finally:
        c.close()


@pytest.fixture
def env(conn, tmp_path):
    """Per-test data dir holding the keyframes, and a footage dir holding the recording."""
    data_dir = tmp_path / "data"
    put_frames(data_dir)
    footage = tmp_path / "footage"
    src = footage / "nested" / "cam.avi"
    src.parent.mkdir(parents=True)
    src.write_bytes(RECORDING)
    return {"conn": conn, "data": data_dir, "footage": footage, "src": src, "tmp": tmp_path}


def req(tracks=None, tenant=TENANT, site=SITE, purpose="Complaint 42, stairwell"):
    ids = list(TRACKS if tracks is None else tracks)
    return BundleRequest(tenant, site, "tester", purpose, ids, "who was there?")


def build(env, tracks=None, **kw):
    kw.setdefault("footage_root", env["footage"])
    return build_bundle(env["conn"], req(tracks), data_dir=env["data"], **kw)


def extract(archive: Path, dest: Path) -> Path:
    with zipfile.ZipFile(archive) as z:
        z.extractall(dest)
    (top,) = [p for p in dest.iterdir() if p.is_dir()]
    return top


def run_verifier(root: Path):
    return subprocess.run([sys.executable, "verify_bundle.py", "."], cwd=root,  # noqa: S603
                          capture_output=True, text=True, check=False)


def nothing_written(data_dir: Path) -> bool:
    bundles = data_dir / "bundles"
    tmp = data_dir / "tmp"
    no_bundles = not bundles.exists() or not any(p.is_file() for p in bundles.rglob("*"))
    no_tmp = not tmp.exists() or not any(tmp.iterdir())
    return no_bundles and no_tmp


# --- a good bundle ---------------------------------------------------------------------------

@pytest.fixture
def built(env):
    b = build(env)
    return b, extract(b.archive, env["tmp"] / "out")


def test_bundle_contains_every_expected_file(built):
    """The court is handed recordings, frames, index rows and the means to check them."""
    b, root = built
    names = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
    assert f"source/{CAM_DIR}/{RECORDING_SHA[:16]}/cam.avi" in names
    assert (root / "source" / CAM_DIR / RECORDING_SHA[:16] / "cam.avi").read_bytes() == RECORDING
    for t in TRACKS:
        assert f"frames/{t}.jpg" in names
    assert {"records/tracks.json", "records/cameras.json", "records/coverage.json"} <= names
    assert {"manifest.json", "SHA256SUMS", "README.txt", "verify_bundle.py"} <= names
    assert not any(n.startswith("certificate/") for n in names)


def test_manifest_items_carry_all_hashes_and_the_root_recomputes(built):
    """Whichever algorithm the signatory ticks must already be in the manifest."""
    b, root = built
    m = json.loads((root / "manifest.json").read_text())
    assert m == b.manifest
    assert len(m["items"]) == 1 + 3 + 3
    for it in m["items"]:
        data = (root / it["path"]).read_bytes()
        assert it["sha256"] == hashlib.sha256(data).hexdigest()
        assert it["sha1"] == hashlib.sha1(data).hexdigest()  # noqa: S324
        assert it["md5"] == hashlib.md5(data).hexdigest()  # noqa: S324
        assert it["size_bytes"] == len(data)
    expect = merkle_root([(i["sha256"], i["path"]) for i in m["items"]])
    assert m["merkle"]["root"] == expect == b.merkle_root
    assert m["merkle"]["leaves"] == 7
    assert b.manifest_sha256 == hashlib.sha256((root / "manifest.json").read_bytes()).hexdigest()
    assert b.archive_sha256 == hashlib.sha256(b.archive.read_bytes()).hexdigest()


def test_manifest_states_what_it_does_not_claim(built):
    """Import-time hashes and re-encoded frames must be labelled, not passed off as originals."""
    b, _ = built
    m = b.manifest
    assert "does not keep the recordings" in m["hash_provenance"]
    assert "does not by itself establish" in m["hash_provenance"]
    assert m["software"].startswith("Smart Cam Monitoring")
    assert "re-encoded" in m["derived_frames_note"]
    assert m["site"]["timezone"] == "America/New_York"
    src = m["sources"]
    assert len(src) == 1 and src[0]["included"] is True
    assert src[0]["sha256_at_import"] == RECORDING_SHA
    assert all(f["included"] for f in m["frames"])


def test_unchecked_clock_is_flagged(built):
    """A camera whose clock nobody checked has its times reported with a warning."""
    b, _ = built
    (w,) = b.manifest["clock_warnings"]
    assert w["camera_id"] == CAM and w["kind"] == "unchecked"


def test_bundled_verifier_passes_on_an_untouched_bundle(built):
    """The standalone verifier, run with only Python, must agree the bundle is intact."""
    _, root = built
    r = run_verifier(root)
    assert r.returncode == 0, r.stdout + r.stderr
    assert "BUNDLE VERIFIED" in r.stdout


def test_shasum_checks_every_file(built):
    """`shasum -a 256 -c SHA256SUMS` is what the README tells a stranger to run."""
    if shutil.which("shasum") is None:
        pytest.skip("shasum not installed")
    _, root = built
    r = subprocess.run(["shasum", "-a", "256", "-c", "SHA256SUMS"], cwd=root,  # noqa: S607
                       capture_output=True, text=True, check=False)
    assert r.returncode == 0, r.stdout + r.stderr


def test_check_tree_passes_on_an_untouched_bundle(built):
    b, root = built
    ok, manifest_sha, lines = cli.check_tree(root)
    assert ok, lines
    assert manifest_sha == b.manifest_sha256


# --- tampering -------------------------------------------------------------------------------

def test_one_changed_byte_in_the_recording_fails_both_checks(built):
    """The whole point: a single altered byte in the electronic record must be caught."""
    _, root = built
    p = root / "source" / CAM_DIR / RECORDING_SHA[:16] / "cam.avi"
    data = bytearray(p.read_bytes())
    data[100] ^= 0x01
    p.write_bytes(bytes(data))
    r = run_verifier(root)
    assert r.returncode != 0 and "BUNDLE FAILED VERIFICATION" in r.stdout
    ok, _, lines = cli.check_tree(root)
    assert not ok
    assert any(line.startswith("MISMATCH") and "cam.avi" in line for line in lines)


def test_added_file_is_reported_unlisted(built):
    """A file slipped into the bundle afterwards is not part of what was sealed."""
    _, root = built
    (root / "frames" / "extra.jpg").write_bytes(b"planted")
    ok, _, lines = cli.check_tree(root)
    assert not ok
    assert "UNLISTED  frames/extra.jpg" in lines


def test_edited_merkle_root_fails(built):
    _, root = built
    mp = root / "manifest.json"
    m = json.loads(mp.read_text())
    m["merkle"]["root"] = "0" * 64
    mp.write_text(json.dumps(m, indent=1, sort_keys=True) + "\n")
    ok, _, lines = cli.check_tree(root)
    assert not ok
    assert any("DOES NOT MATCH" in line for line in lines)
    assert run_verifier(root).returncode != 0


# --- refusals --------------------------------------------------------------------------------

def test_source_altered_on_disk_is_refused(env):
    """A recording that no longer hashes as it did at import cannot be handed over as original."""
    env["src"].write_bytes(RECORDING + b"x")
    with pytest.raises(BundleRefused, match="no longer matches"):
        build(env)
    assert nothing_written(env["data"])


def test_missing_source_is_refused_when_required(env):
    env["src"].unlink()
    with pytest.raises(BundleRefused, match="not found"):
        build(env, require_sources=True)
    assert nothing_written(env["data"])


@pytest.mark.parametrize("tracks,match", [
    ([TRACKS[0], OTHER_TRACK], "not held for this site"),
    ([SYNTHETIC], "no source recording"),
    ([str(uuid.uuid4()) for _ in range(51)], "at most 50"),
    (["not-a-uuid"], "UUIDs"),
    ([], "no evidence"),
])
def test_bad_selection_is_refused_and_writes_nothing(env, tracks, match):
    """Another tenant's track, a synthetic track, too many or malformed ids: refuse, not drop."""
    with pytest.raises(BundleRefused, match=match):
        build(env, tracks)
    assert nothing_written(env["data"])


def test_empty_purpose_is_refused_by_the_service(env):
    """The purpose is printed on the certificate; a blank one is not a request."""
    with pytest.raises(BundleRefused, match="purpose"):
        service.create_bundle(env["conn"], req(purpose="   "), data_dir=env["data"],
                              footage_root=env["footage"], with_certificate=False)
    assert nothing_written(env["data"])


def test_missing_source_allowed_is_cited_by_hash_with_a_warning(env):
    """When the operator accepts it, the recording is cited by hash and the gap is stated."""
    env["src"].unlink()
    b = build(env, require_sources=False)
    (s,) = b.manifest["sources"]
    assert s["included"] is False and s["not_included_reason"]
    assert s["sha256_at_import"] == RECORDING_SHA
    assert any("cam.avi" in w for w in b.warnings)
    assert b.manifest["warnings"] == b.warnings
    assert not any(i["path"].startswith("source/") for i in b.manifest["items"])


def test_certificate_hook_is_added_outside_the_merkle_items(env):
    """The certificate is about the hashed items, so it is in SHA256SUMS but not a leaf."""
    seen = {}

    def fake(manifest):
        seen.update(manifest)
        return b"%PDF-fake"

    b = build(env, certificate=fake)
    assert seen["manifest_sha256"] == b.manifest_sha256
    assert service.read_member(b.archive, "certificate/certificate-draft.pdf") == b"%PDF-fake"
    assert not any(i["path"].startswith("certificate/") for i in b.manifest["items"])
    root = extract(b.archive, env["tmp"] / "out")
    assert cli.check_tree(root)[0]


def test_same_named_recordings_from_one_camera_are_both_bundled(env):
    """DVRs export one file per hour with the same name; both hours must survive in the bundle."""
    other = env["footage"] / "13" / "cam.avi"
    other.parent.mkdir(parents=True)
    other.write_bytes(HOUR13)
    b = build(env, HOURLY)
    paths = [s["path"] for s in b.manifest["sources"]]
    assert len(paths) == 2 and len(set(paths)) == 2


# --- recording and serving -------------------------------------------------------------------

@pytest.fixture
def created(env):
    b = service.create_bundle(env["conn"], req(), data_dir=env["data"],
                              footage_root=env["footage"], with_certificate=False)
    return env, b


def test_create_records_one_row_and_one_custody_entry(created):
    """The row describing the archive carries exactly the hashes the archive was sealed with."""
    env, b = created
    with env["conn"].cursor() as cur:
        cur.execute("SELECT manifest_sha256, archive_sha256, merkle_root, certificate_sha256, "
                    "frame_count, tenant_id::text FROM evidence_bundles WHERE bundle_id = %s",
                    (b.bundle_id,))
        rows = cur.fetchall()
        cur.execute("SELECT action, actor FROM custody_log WHERE bundle_id = %s", (b.bundle_id,))
        custody = cur.fetchall()
    assert rows == [(b.manifest_sha256, b.archive_sha256, b.merkle_root, None, 3, TENANT)]
    assert custody == [("created", "tester")]


@pytest.mark.parametrize("sql", [
    "UPDATE evidence_bundles SET purpose = 'edited' WHERE bundle_id = %s",
    "DELETE FROM evidence_bundles WHERE bundle_id = %s",
])
def test_bundle_row_is_immutable(created, sql):
    """An evidence record that can be edited has a history that must be taken on trust."""
    env, b = created
    with pytest.raises(psycopg.Error), env["conn"].cursor() as cur:
        cur.execute(sql, (b.bundle_id,))


def test_verified_archive_serves_untouched_and_refuses_altered(created):
    env, b = created
    stored = service.load_bundle(env["conn"], b.bundle_id, tenant_id=TENANT, site_id=SITE,
                                 data_dir=env["data"])
    assert stored is not None
    assert service.verified_archive(stored) == b.archive.resolve()
    with b.archive.open("ab") as f:
        f.write(b"\x00")
    with pytest.raises(service.BundleUnavailable):
        service.verified_archive(stored)


def test_other_tenant_cannot_load_the_bundle(created):
    """A bundle id guessed by another customer must look exactly like one that does not exist."""
    env, b = created
    assert service.load_bundle(env["conn"], b.bundle_id, tenant_id=OTHER_TENANT,
                               site_id=OTHER_SITE, data_dir=env["data"]) is None


# --- cli verify ------------------------------------------------------------------------------

def test_cli_verify_zip_offline_and_against_the_record(created, capsys):
    """Offline verification needs no database; with --dsn it also matches the custody record."""
    _, b = created
    assert cli.main(["verify", str(b.archive)]) == 0
    assert cli.main(["verify", str(b.archive), "--dsn", DSN]) == 0
    out = capsys.readouterr().out
    assert "BUNDLE VERIFIED" in out and "RECORD    manifest: matches" in out


def test_cli_verify_refuses_path_traversal(tmp_path):
    """An archive entry that escapes the extraction folder is refused before anything is written."""
    z = tmp_path / "evil.zip"
    with zipfile.ZipFile(z, "w") as zf:
        zf.writestr("../evil.txt", "x")
    with pytest.raises(SystemExit):
        cli.main(["verify", str(z)])
    assert not (tmp_path.parent / "evil.txt").exists()


def test_real_certificate_draft_is_sealed_in_the_bundle(env):
    """End to end with the real renderer: the draft is inside the archive, its hash is on the
    immutable bundle row, and it names this bundle."""
    import shutil

    from smartcam.evidence import service

    built = service.create_bundle(env["conn"], req(), data_dir=env["data"],
                                  footage_root=env["footage"], with_certificate=True)
    pdf = service.read_member(built.archive, "certificate/certificate-draft.pdf")
    assert pdf is not None and pdf.startswith(b"%PDF")
    with env["conn"].cursor() as cur:
        cur.execute("SELECT certificate_sha256 FROM evidence_bundles WHERE bundle_id = %s",
                    (built.bundle_id,))
        assert cur.fetchone()[0] == hashlib.sha256(pdf).hexdigest()
    if shutil.which("pdftotext"):
        p = env["tmp"] / "c.pdf"
        p.write_bytes(pdf)
        text = subprocess.run(["pdftotext", "-nodiag", str(p), "-"],  # noqa: S603, S607
                              capture_output=True, text=True, check=True).stdout
        assert built.bundle_id in text and "Annexure A" in text


def test_a_planted_file_fails_verification_even_with_regenerated_sums(env, built):
    """SHA256SUMS can be regenerated by anyone, so it cannot be what stops an extra 'original
    recording' being slipped into source/. The manifest's own list has to."""
    from smartcam.evidence.cli import check_tree

    _, root = built
    planted = root / "source" / CAM_DIR / "planted.avi"
    planted.write_bytes(b"not a recording")
    sums = sorted((hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(root).as_posix())
                  for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (root / "SHA256SUMS").write_text("".join(f"{h}  {p}\n" for h, p in sums))
    ok, _, lines = check_tree(root)
    assert not ok and any("UNLISTED" in line and "planted.avi" in line for line in lines)
    r = run_verifier(root)
    assert r.returncode == 1 and "UNLISTED" in r.stdout


def test_a_swapped_verifier_is_caught_against_the_record(env, built):
    """The bundled verifier is not a Merkle leaf; SHA256SUMS covers it, and the record keeps the
    hash of SHA256SUMS, so a replaced verifier with regenerated sums still fails --dsn."""
    from smartcam.evidence import service
    from smartcam.evidence.cli import _check_record, check_tree

    b = service.create_bundle(env["conn"], req(), data_dir=env["data"],
                              footage_root=env["footage"], with_certificate=False)
    root = extract(b.archive, env["tmp"] / "swapped")
    (root / "verify_bundle.py").write_text("print('BUNDLE VERIFIED')\n")
    sums = sorted((hashlib.sha256(p.read_bytes()).hexdigest(), p.relative_to(root).as_posix())
                  for p in root.rglob("*") if p.is_file() and p.name != "SHA256SUMS")
    (root / "SHA256SUMS").write_text("".join(f"{h}  {p}\n" for h, p in sums))
    ok, manifest_sha, _ = check_tree(root)
    assert ok  # offline, this is undetectable — which is why the record keeps the sums hash
    assert not _check_record(DSN, root, manifest_sha, None)


def test_duplicate_track_ids_are_recorded_once(env):
    from smartcam.evidence import service

    b = service.create_bundle(env["conn"], req(tracks=[TRACKS[0], TRACKS[0], TRACKS[0]]),
                              data_dir=env["data"], footage_root=env["footage"],
                              with_certificate=False)
    with env["conn"].cursor() as cur:
        cur.execute("SELECT track_ids FROM evidence_bundles WHERE bundle_id = %s",
                    (b.bundle_id,))
        assert [str(t) for t in cur.fetchone()[0]] == [TRACKS[0]]

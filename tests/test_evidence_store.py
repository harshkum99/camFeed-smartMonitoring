"""Evidence frames on disk: the key shape, the only lookup the API may use, and the writer.

A keyframe is shown to an operator as proof. These tests care about the two ways that proof can be
wrong: a crafted key that reaches another tenant's frame (or anything outside the tree), and a
stored frame whose bytes no longer match the hash recorded at ingest.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import pytest

from smartcam.evidence import store
from smartcam.evidence.store import KEY_RE, keyframe_key, resolve_keyframe
from smartcam.ingest.keyframes import KeyframeStore, encode_jpeg

TENANT = "11111111-1111-4111-8111-111111111111"
SITE = "22222222-2222-4222-8222-222222222222"
CAMERA = "33333333-3333-4333-8333-333333333333"
OTHER = "44444444-4444-4444-8444-444444444444"
DIGEST = hashlib.sha256(b"frame").hexdigest()


def _key(tenant: str = TENANT, site: str = SITE, digest: str = DIGEST) -> str:
    return keyframe_key(tenant_id=tenant, site_id=site, camera_id=CAMERA, sha256=digest)


def _store_file(root: Path, key: str, data: bytes = b"frame") -> Path:
    path = root / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _rgb() -> np.ndarray:
    rng = np.random.default_rng(7)
    return rng.integers(0, 256, size=(24, 32, 3), dtype=np.uint8)


# --- key shape --------------------------------------------------------------------------------

def test_keyframe_key_round_trips_through_key_re():
    """The key names its own tenant and site; the resolver trusts only what the pattern extracts."""
    m = KEY_RE.match(_key())
    assert m is not None
    assert m.groups() == (TENANT, SITE, CAMERA, DIGEST[:2], DIGEST)


def test_keyframe_key_refuses_ids_that_are_not_uuids():
    """A key built from a path fragment must fail at build time, not be stored under a bad path."""
    with pytest.raises(ValueError):
        keyframe_key(tenant_id="../x", site_id=SITE, camera_id=CAMERA, sha256=DIGEST)


# --- resolve_keyframe refusals ----------------------------------------------------------------

@pytest.mark.parametrize("key", [
    None,
    "",
    f"keyframes/{TENANT}/{SITE}/../{OTHER}/{CAMERA}/{DIGEST[:2]}/{DIGEST}.jpg",
    f"../keyframes/{TENANT}/{SITE}/{CAMERA}/{DIGEST[:2]}/{DIGEST}.jpg",
    f"/keyframes/{TENANT}/{SITE}/{CAMERA}/{DIGEST[:2]}/{DIGEST}.jpg",
    f"s3://bucket/keyframes/{TENANT}/{SITE}/{CAMERA}/{DIGEST[:2]}/{DIGEST}.jpg",
], ids=["none", "empty", "dotdot-inside", "dotdot-prefix", "absolute", "s3-uri"])
def test_malformed_keys_are_refused(tmp_path, key):
    """Escapes are refused because they cannot match the one key shape, not by sanitising."""
    _store_file(tmp_path, _key())
    assert resolve_keyframe(tmp_path, key, tenant_id=TENANT, site_id=SITE) is None


def test_another_tenants_key_is_refused_even_when_the_file_exists(tmp_path):
    """A well-formed key for someone else's frame must look exactly like a missing one."""
    key = _key(tenant=OTHER)
    _store_file(tmp_path, key)
    assert resolve_keyframe(tmp_path, key, tenant_id=TENANT, site_id=SITE) is None


def test_another_sites_key_is_refused_even_when_the_file_exists(tmp_path):
    """Site scoping is as strict as tenant scoping: a site user must not see a sibling site."""
    key = _key(site=OTHER)
    _store_file(tmp_path, key)
    assert resolve_keyframe(tmp_path, key, tenant_id=TENANT, site_id=SITE) is None


def test_prefix_directory_that_disagrees_with_the_digest_is_refused(tmp_path):
    """Content addressing means one file per digest; a second path to it is a crafted key."""
    key = f"keyframes/{TENANT}/{SITE}/{CAMERA}/ff/{DIGEST}.jpg"
    assert DIGEST[:2] != "ff"
    assert KEY_RE.match(key)
    _store_file(tmp_path, key)
    assert resolve_keyframe(tmp_path, key, tenant_id=TENANT, site_id=SITE) is None


def test_symlink_inside_the_tree_pointing_outside_is_refused(tmp_path):
    """A link planted in the store must not turn the API into a reader of arbitrary files."""
    root = tmp_path / "root"
    outside = tmp_path / "secret.jpg"
    outside.write_bytes(b"not evidence")
    key = _key()
    link = root / key
    link.parent.mkdir(parents=True)
    link.symlink_to(outside)
    assert link.is_file()
    assert resolve_keyframe(root, key, tenant_id=TENANT, site_id=SITE) is None


def test_missing_file_is_refused(tmp_path):
    """A valid key with nothing behind it answers None, the same as every other refusal."""
    assert resolve_keyframe(tmp_path, _key(), tenant_id=TENANT, site_id=SITE) is None


def test_valid_stored_file_resolves_to_its_path(tmp_path):
    """The happy path must still work, or every refusal test above passes vacuously."""
    key = _key()
    path = _store_file(tmp_path, key)
    assert resolve_keyframe(tmp_path, key, tenant_id=TENANT, site_id=SITE) == path.resolve()


# --- data_dir ---------------------------------------------------------------------------------

def test_relative_data_dir_resolves_against_repo_root_not_cwd(tmp_path, monkeypatch):
    """Ingest and API start from different shells; resolving against cwd would 404 every frame."""
    monkeypatch.setenv("SMARTCAM_DATA_DIR", "var/elsewhere")
    monkeypatch.chdir(tmp_path)
    assert store.data_dir() == (store.REPO_ROOT / "var/elsewhere").resolve()


def test_absolute_data_dir_is_kept(tmp_path, monkeypatch):
    """An operator who points at a mounted volume must get exactly that volume."""
    monkeypatch.setenv("SMARTCAM_DATA_DIR", str(tmp_path))
    monkeypatch.chdir(store.REPO_ROOT)
    assert store.data_dir() == tmp_path.resolve()


# --- KeyframeStore ----------------------------------------------------------------------------

def test_put_writes_a_jpeg_whose_bytes_hash_to_the_returned_digest(tmp_path):
    """The stored digest is what the API re-checks at serve time; it must describe these bytes."""
    stored = KeyframeStore(tmp_path).put(_rgb(), tenant_id=TENANT, site_id=SITE, camera_id=CAMERA)
    data = (tmp_path / stored.key).read_bytes()
    assert data[:2] == b"\xff\xd8"
    assert hashlib.sha256(data).hexdigest() == stored.sha256
    assert stored.size_bytes == len(data)
    assert stored.key == keyframe_key(
        tenant_id=TENANT, site_id=SITE, camera_id=CAMERA, sha256=stored.sha256)
    assert store.sha256_file(tmp_path / stored.key) == stored.sha256


def test_storing_the_same_frame_twice_is_free_and_does_not_rewrite(tmp_path):
    """Content addressing means a repeat is a no-op; rewriting would race a concurrent reader."""
    ks = KeyframeStore(tmp_path)
    first = ks.put(_rgb(), tenant_id=TENANT, site_id=SITE, camera_id=CAMERA)
    path = tmp_path / first.key
    before = path.stat().st_mtime_ns
    second = ks.put(_rgb(), tenant_id=TENANT, site_id=SITE, camera_id=CAMERA)
    assert second == first
    assert path.stat().st_mtime_ns == before


def test_put_leaves_no_temp_files_behind(tmp_path):
    """Temp files are the atomic-write mechanism; any left over would pile up in the store."""
    ks = KeyframeStore(tmp_path)
    ks.put(_rgb(), tenant_id=TENANT, site_id=SITE, camera_id=CAMERA)
    ks.put(_rgb(), tenant_id=TENANT, site_id=SITE, camera_id=CAMERA)
    assert not [p for p in tmp_path.rglob("*") if p.name.startswith(".tmp-")]


def test_encode_jpeg_is_deterministic():
    """Without determinism, the same frame would get a new key and a new file on every ingest."""
    assert encode_jpeg(_rgb()) == encode_jpeg(_rgb())

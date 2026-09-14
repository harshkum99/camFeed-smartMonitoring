"""Where evidence frames live on disk, and the only way the API is allowed to find one.

A keyframe is shown to an operator as proof that something happened, so two properties matter
more than convenience. It must be impossible to fetch another tenant's frame by guessing or
crafting a path, and it must be impossible to serve a frame whose bytes are no longer the bytes we
hashed at ingest.

The first is handled structurally rather than by sanitising input: a key has exactly one shape,
it names its tenant and site inside itself, and anything that does not match that shape in full is
refused before a filesystem call is made. `..`, absolute paths and `s3://` URIs cannot match the
pattern, so there is no escaping to get wrong. The second is the caller's job at serve time —
`sha256_file` is here so ingest and API hash identically.

Stdlib only: the API imports this, and the API must not need numpy to start.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def data_dir() -> Path:
    """Where keyframes and run logs live. One definition shared by ingest and the API.

    A relative SMARTCAM_DATA_DIR is resolved against the repository root, not the working
    directory: ingest and the API are started from different shells, and if they resolved it
    differently every evidence frame would 404 with nothing in the UI saying why.
    """
    raw = Path(os.environ.get("SMARTCAM_DATA_DIR", "var/smartcam")).expanduser()
    return (raw if raw.is_absolute() else REPO_ROOT / raw).resolve()

_UUID = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
KEY_RE = re.compile(
    rf"^keyframes/({_UUID})/({_UUID})/({_UUID})/([0-9a-f]{{2}})/([0-9a-f]{{64}})\.jpg$"
)


def keyframe_key(*, tenant_id: str, site_id: str, camera_id: str, sha256: str) -> str:
    """The storage key for a frame. Content-addressed, so storing the same frame twice is free."""
    key = f"keyframes/{tenant_id}/{site_id}/{camera_id}/{sha256[:2]}/{sha256}.jpg"
    if not KEY_RE.match(key):
        raise ValueError(f"cannot build a keyframe key from {tenant_id}/{site_id}/{camera_id}")
    return key


def resolve_keyframe(root: Path, key: str | None, *, tenant_id: str, site_id: str) -> Path | None:
    """The file for `key`, or None if the key is malformed, belongs to someone else, or is absent.

    Returns None rather than raising for every refusal: the caller answers all of them with the
    same 404, so a probe cannot distinguish "not yours" from "does not exist".
    """
    if not key:
        return None
    m = KEY_RE.match(key)
    if m is None:
        return None
    key_tenant, key_site, _camera, prefix, digest = m.groups()
    if key_tenant != tenant_id or key_site != site_id or prefix != digest[:2]:
        return None
    base = (root.resolve() / "keyframes" / tenant_id / site_id).resolve()
    path = (root.resolve() / key).resolve()
    # resolve() follows symlinks, so a link planted inside the tree that points out of it fails
    # here rather than being served.
    if not path.is_relative_to(base) or not path.is_file():
        return None
    return path


def sha256_file(path: Path, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while block := f.read(chunk):
            h.update(block)
    return h.hexdigest()

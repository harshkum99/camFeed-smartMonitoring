"""Evidence frames on disk: full native-resolution JPEGs, stored under the hash of their bytes.

The frame shown is the original picture with nothing drawn on it. The box is drawn by the console
from `keyframe_bbox`, so the stored image never carries our annotation and can be shown to someone
who disputes it.

**What the hash does and does not prove.** `frame_sha256` is the hash of this JPEG — a derived
artefact, re-encoded from decoded pixels. It proves the image served today is the image we wrote at
ingest. It does not prove anything about the recorder's original bytes; that anchor is the source
file's own hash in `clips.source_sha256`, which is what an evidence certificate must cite.
"""

from __future__ import annotations

import hashlib
import io
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PIL import Image

from smartcam.evidence.store import keyframe_key


@dataclass(frozen=True)
class StoredFrame:
    key: str
    sha256: str
    size_bytes: int


def encode_jpeg(rgb: np.ndarray, quality: int = 90) -> bytes:
    """Deterministic for a given array and Pillow version: no EXIF, no timestamp, no ICC."""
    buf = io.BytesIO()
    Image.fromarray(np.ascontiguousarray(rgb), "RGB").save(
        buf, "JPEG", quality=quality, optimize=False, progressive=False)
    return buf.getvalue()


class KeyframeStore:
    def __init__(self, root: Path):
        self.root = root

    def put(self, rgb: np.ndarray, *, tenant_id: str, site_id: str, camera_id: str) -> StoredFrame:
        data = encode_jpeg(rgb)
        digest = hashlib.sha256(data).hexdigest()
        key = keyframe_key(tenant_id=tenant_id, site_id=site_id, camera_id=camera_id,
                           sha256=digest)
        path = self.root / key
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".jpg")
            try:
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                os.replace(tmp, path)
            except BaseException:
                Path(tmp).unlink(missing_ok=True)
                raise
        return StoredFrame(key, digest, len(data))

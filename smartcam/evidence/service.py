"""Creating, listing and handing out evidence bundles — the one path the CLI and the API share.

Order matters, because the database row is immutable (migration 006): the archive is built and
hashed first, and only then is the row describing it inserted, with every hash already known. If
the insert fails the archive is deleted, so there is never a file on disk that the custody record
does not know about, or a record pointing at a file that was never finished.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import zipfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from smartcam.evidence.bundle import (
    BuiltBundle,
    BundleRefused,
    BundleRequest,
    build_bundle,
    hash_file,
    log_custody,
    record_bundle,
)
from smartcam.evidence.store import REPO_ROOT


def footage_dir() -> Path:
    """Where original recordings are looked up by name, and re-verified by hash, for a bundle."""
    raw = Path(os.environ.get("SMARTCAM_FOOTAGE_DIR", "var/footage")).expanduser()
    return (raw if raw.is_absolute() else REPO_ROOT / raw).resolve()


def create_bundle(conn, req: BundleRequest, *, data_dir: Path, footage_root: Path | None = None,
                  with_certificate: bool = True) -> BuiltBundle:
    if not req.purpose.strip():
        raise BundleRefused("state the purpose of the bundle; it is printed on the certificate")
    renderer = None
    if with_certificate:
        from smartcam.evidence.certificate import render_certificate
        renderer = render_certificate
    built = build_bundle(conn, req, data_dir=data_dir,
                         footage_root=footage_root or footage_dir(), certificate=renderer)
    try:
        record_bundle(conn, req, built)
    except BaseException:
        built.archive.unlink(missing_ok=True)
        raise
    return built


def certificate_sha256(archive: Path) -> str | None:
    data = read_member(archive, "certificate/certificate-draft.pdf")
    return hashlib.sha256(data).hexdigest() if data is not None else None


def read_member(archive, rel: str) -> bytes | None:
    with zipfile.ZipFile(archive) as z:
        for name in z.namelist():
            if name.split("/", 1)[-1] == rel:
                return z.read(name)
    return None


@dataclass(frozen=True)
class StoredBundle:
    row: dict[str, Any]
    archive: Path


class BundleUnavailable(RuntimeError):
    """The bundle exists but cannot be served as recorded: missing, or not the bytes we sealed."""


def load_bundle(conn, bundle_id: str, *, tenant_id: str, site_id: str,
                data_dir: Path) -> StoredBundle | None:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM evidence_bundles WHERE bundle_id = %s AND tenant_id = %s "
                    "AND site_id = %s", (bundle_id, tenant_id, site_id))
        if cur.description is None:
            return None
        cols = [d.name for d in cur.description]
        r = cur.fetchone()
    if r is None:
        return None
    row = dict(zip(cols, r, strict=True))
    expected = (data_dir / "bundles" / tenant_id / site_id / f"{bundle_id}.zip").resolve()
    return StoredBundle(row, expected)


def verified_archive(stored: StoredBundle) -> Path:
    """The archive, after checking it is byte-for-byte the one recorded when the bundle was made."""
    if not stored.archive.is_file():
        raise BundleUnavailable("the archive for this bundle is missing from storage")
    if hash_file(stored.archive).sha256 != stored.row["archive_sha256"]:
        raise BundleUnavailable("the archive no longer matches the hash recorded when it was "
                                "created, and was not served")
    return stored.archive


def fresh_draft(conn, stored: StoredBundle, actor: str) -> tuple[bytes, str]:
    """A new certificate draft for a new submission of the same bundle.

    Section 63(4) asks for a certificate at each instance a record is submitted, so a draft is not
    reused across submissions: each one gets the next number (D2, D3, ...) and its own time, is
    rendered from the sealed bundle — never from the live index, which may have changed — and is
    logged with its hash before it is handed over. The number is taken under a per-bundle lock
    in the same transaction as the custody row, so two requests can never both issue D2.
    """
    from smartcam.evidence.certificate import render_certificate

    bundle_id = str(stored.row["bundle_id"])
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT pg_advisory_xact_lock(hashtextextended(%s, 63))", (bundle_id,))
        cur.execute("SELECT count(*) FROM custody_log WHERE bundle_id = %s "
                    "AND action = 'draft_generated'", (bundle_id,))
        draft_id = f"D{2 + cur.fetchone()[0]}"
        history = custody(conn, bundle_id)
        with tempfile.TemporaryDirectory() as tmp, open_verified(stored) as handle, \
                zipfile.ZipFile(handle) as z:
            for info in z.infolist():
                rel = info.filename.split("/", 1)[-1]
                if rel.startswith(("records/", "frames/")) or rel == "manifest.json":
                    target = Path(tmp) / rel
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(z.read(info))
            manifest_bytes = (Path(tmp) / "manifest.json").read_bytes()
            if hashlib.sha256(manifest_bytes).hexdigest() != stored.row["manifest_sha256"]:
                raise BundleUnavailable("the sealed manifest does not match its recorded hash")
            pdf = render_certificate(
                {**json.loads(manifest_bytes), "manifest_sha256": stored.row["manifest_sha256"],
                 "_work_dir": tmp},
                draft_id=draft_id, generated_at=datetime.now(UTC).isoformat(), custody=history)
        cur.execute("INSERT INTO custody_log (bundle_id, actor, action, detail) "
                    "VALUES (%s, %s, 'draft_generated', %s)",
                    (bundle_id, actor, Jsonb({"draft_id": draft_id,
                                              "sha256": hashlib.sha256(pdf).hexdigest()})))
    return pdf, draft_id


def open_verified(stored: StoredBundle):
    """The archive as one open handle, already hashed and matched against the record.

    Callers read and serve from this same handle. Checking the file by path and then reopening it
    to stream would certify one set of bytes and hand over another if it changed in between.
    """
    if not stored.archive.is_file():
        raise BundleUnavailable("the archive for this bundle is missing from storage")
    f = stored.archive.open("rb")
    try:
        h = hashlib.sha256()
        while block := f.read(1 << 20):
            h.update(block)
        if h.hexdigest() != stored.row["archive_sha256"]:
            raise BundleUnavailable("the archive no longer matches the hash recorded when it was "
                                    "created, and was not served")
        f.seek(0)
        return f
    except BaseException:
        f.close()
        raise


def custody(conn, bundle_id: str) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute("SELECT at, actor, action, detail FROM custody_log WHERE bundle_id = %s "
                    "ORDER BY at, entry_id", (bundle_id,))
        return [{"at": at.isoformat(), "actor": a, "action": act, "detail": d}
                for at, a, act, d in cur.fetchall()]


__all__ = [
    "BundleRefused", "BundleRequest", "BundleUnavailable", "StoredBundle", "create_bundle",
    "custody", "footage_dir", "fresh_draft", "load_bundle", "log_custody", "open_verified",
    "read_member", "verified_archive",
]

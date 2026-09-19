"""Assembling an evidence bundle: the files a court would be handed, and proof they are unchanged.

A bundle answers one question for someone who was not there: *these are the recordings the
answer came from, and they are byte-for-byte what the recorder produced.* Everything in it is
chosen for a reader who does not trust us — an opposing party's expert, a magistrate, a police
examiner — and so nothing in it can be verified only by running our software.

What goes in, and what each item is:

- ``source/`` — the original recorder files the cited tracks came from, copied byte for byte and
  re-hashed on the way in. A file whose hash no longer matches the one recorded at import is
  refused, not included. These are the electronic records; everything else is about them.
- ``frames/`` — the evidence frames shown in the product. They are JPEGs *derived* from the
  recording (decoded and re-encoded), labelled as such, and are illustrations of where to look,
  not evidence in their own right.
- ``records/`` — the index rows the answer rested on: tracks, camera identity, coverage.
- ``manifest.json`` — every item with its hashes, and the Merkle root over them
  (``smartcam.evidence.merkle``, RFC 6962). ``SHA256SUMS`` lets anyone check every file with
  ``shasum -a 256 -c``; ``verify_bundle.py`` recomputes the root with the Python standard library.

**What we do not claim.** For imported recordings the source hash is computed when the file
reaches us, not when the recorder wrote it; the bundle says so. A camera whose clock was never
checked has its times reported as the device recorded them, with a warning. The bundle is
prepared for the person in charge of the recorder and an expert to certify — it certifies
nothing itself.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
import uuid
import zipfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from psycopg.types.json import Jsonb

from smartcam.evidence.merkle import merkle_root
from smartcam.evidence.store import resolve_keyframe
from smartcam.version import software_version

MAX_TRACKS = 50
BUNDLE_FORMAT = "smartcam-evidence-bundle/1"
MERKLE_SCHEME = "rfc6962-sha256; leaf = '<sha256> <path>' utf-8; leaves sorted by path"


class BundleRefused(RuntimeError):
    """A bundle that would be incomplete or misleading. Nothing is written."""


@dataclass(frozen=True)
class BundleRequest:
    tenant_id: str
    site_id: str
    requested_by: str
    purpose: str
    track_ids: list[str]
    question: str | None = None


@dataclass
class Hashes:
    sha256: str
    sha1: str
    md5: str
    size: int


@dataclass
class BuiltBundle:
    bundle_id: str
    archive: Path
    archive_sha256: str
    manifest_sha256: str
    merkle_root: str
    manifest: dict[str, Any]
    warnings: list[str] = field(default_factory=list)
    #: The tracks actually bundled — deduplicated and normalised, never the raw request.
    track_ids: list[str] = field(default_factory=list)
    #: Hash of SHA256SUMS, which covers every file in the archive including the README, the
    #: verifier and the certificate draft. Recorded so none of them can be swapped unnoticed.
    sums_sha256: str | None = None
    certificate_sha256: str | None = None


def hash_file(path: Path, chunk: int = 1 << 20) -> Hashes:
    """SHA-256, SHA-1 and MD5 in one pass. The certificate form lets the signatory choose the
    algorithm; computing all three means whichever they tick is already on the hash report."""
    s256, s1, m5 = hashlib.sha256(), hashlib.sha1(), hashlib.md5()  # noqa: S324 - reported, not relied on
    size = 0
    with path.open("rb") as f:
        while block := f.read(chunk):
            s256.update(block)
            s1.update(block)
            m5.update(block)
            size += len(block)
    return Hashes(s256.hexdigest(), s1.hexdigest(), m5.hexdigest(), size)


def _index_footage(root: Path | None) -> dict[str, list[Path]]:
    by_name: dict[str, list[Path]] = {}
    if root is None or not root.is_dir():
        return by_name
    for p in root.rglob("*"):
        if p.is_file():
            by_name.setdefault(p.name, []).append(p)
    return by_name


def _source_name(source_uri: str | None) -> str | None:
    if not source_uri:
        return None
    tail = source_uri.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
    return tail or None


def _iso(v: datetime | None) -> str | None:
    return v.astimezone(UTC).isoformat() if v else None


def load_evidence(conn, req: BundleRequest) -> dict[str, Any]:
    """Everything the bundle cites, read under the requester's tenant and site.

    Refuses rather than drops: a bundle silently missing one of the tracks it was asked for is a
    bundle that no longer shows what the operator saw.
    """
    ids = list(dict.fromkeys(req.track_ids))
    if not ids:
        raise BundleRefused("no evidence was selected")
    if len(ids) > MAX_TRACKS:
        raise BundleRefused(f"a bundle can cite at most {MAX_TRACKS} tracks; narrow the selection")
    try:
        ids = [str(uuid.UUID(t)) for t in ids]
    except ValueError:
        raise BundleRefused("track ids must be UUIDs") from None

    with conn.cursor() as cur:
        cur.execute(
            "SELECT track_id, camera_id, class, ts_start, ts_end, conf_max, conf_mean, n_frames, "
            "best_keyframe_uri, frame_sha256, keyframe_ts, keyframe_bbox, clip_uri, "
            "model_versions FROM tracks WHERE tenant_id = %s AND site_id = %s "
            "AND track_id = ANY(%s::uuid[]) ORDER BY ts_start",
            (req.tenant_id, req.site_id, ids))
        cols = [d.name for d in cur.description]
        tracks = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
        found = {str(t["track_id"]) for t in tracks}
        missing = [t for t in ids if t not in found]
        if missing:
            raise BundleRefused(f"{len(missing)} selected track(s) are not held for this site")

        cams = sorted({str(t["camera_id"]) for t in tracks})
        cur.execute(
            "SELECT camera_id, name, make, model, serial_no, mac, recorder_id, channel_no, "
            "detect_w, detect_h, detect_fps, codec, clock_offset_ms, clock_checked_at, "
            "grade::text, graded_from, capabilities FROM cameras "
            "WHERE tenant_id = %s AND site_id = %s "
            "AND camera_id = ANY(%s::uuid[])", (req.tenant_id, req.site_id, cams))
        cols = [d.name for d in cur.description]
        cameras = {str(r[0]): dict(zip(cols, r, strict=True)) for r in cur.fetchall()}

        shas = sorted({t["clip_uri"].removeprefix("sha256:") for t in tracks
                       if (t["clip_uri"] or "").startswith("sha256:")})
        cur.execute(
            "SELECT clip_id, camera_id, ts_start, ts_end, source_uri, source_sha256, "
            "imported_at, imported_by FROM clips "
            "WHERE tenant_id = %s AND site_id = %s AND source_sha256 = ANY(%s)",
            (req.tenant_id, req.site_id, shas))
        cols = [d.name for d in cur.description]
        clips = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

        start = min(t["ts_start"] for t in tracks)
        end = max(t["ts_end"] for t in tracks)
        cur.execute(
            "SELECT camera_id, ts_start, ts_end, state, source, detail FROM camera_uptime "
            "WHERE camera_id = ANY(%s::uuid[]) AND ts_start < %s "
            "AND COALESCE(ts_end, 'infinity') > %s ORDER BY camera_id, ts_start",
            (cams, end, start))
        cols = [d.name for d in cur.description]
        uptime = [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]

        cur.execute(
            "SELECT camera_name, gap_start, gap_end FROM coverage_gaps(%s::uuid[], %s, %s)",
            (cams, start, end))
        gaps = [{"camera": n, "from": a, "to": b} for n, a, b in cur.fetchall()]

        cur.execute("SELECT s.name, s.tz, s.data_attribution, t.name FROM sites s "
                    "JOIN tenants t USING (tenant_id) WHERE s.site_id = %s AND s.tenant_id = %s",
                    (req.site_id, req.tenant_id))
        site_name, tz, attribution, tenant_name = cur.fetchone()

    held = {(str(c["camera_id"]), c["source_sha256"]) for c in clips}
    orphaned = [str(t["track_id"]) for t in tracks
                if (t["clip_uri"] or "").startswith("sha256:")
                and (str(t["camera_id"]), t["clip_uri"].removeprefix("sha256:")) not in held]
    if orphaned:
        # The track cites a recording this site no longer has a record of. Bundling it would list
        # the originals as complete while one is silently absent.
        raise BundleRefused(f"{len(orphaned)} selected track(s) cite a recording that is no "
                            f"longer on record for this site")
    untraced = [str(t["track_id"]) for t in tracks
                if not (t["clip_uri"] or "").startswith("sha256:")]
    if untraced:
        # Synthetic or live-only tracks have no source recording to hand over. A bundle of
        # frames with nothing they were cut from is not an evidence bundle.
        raise BundleRefused(f"{len(untraced)} selected track(s) have no source recording on "
                            f"record, so there is nothing to certify for them")
    return {"tracks": tracks, "cameras": cameras, "clips": clips, "uptime": uptime,
            "window": (start, end), "gaps": gaps, "site_name": site_name, "tz": tz,
            "attribution": attribution, "tenant_name": tenant_name}


def clock_warnings(cameras: dict[str, dict]) -> list[dict[str, Any]]:
    out = []
    for cid, c in sorted(cameras.items()):
        if c["clock_offset_ms"] is None:
            out.append({"camera_id": cid, "camera": c["name"], "kind": "unchecked",
                        "message": f"{c['name']}: the recorder's clock was never checked against a "
                                   f"reference, so times are stated as the device recorded them"})
        elif abs(c["clock_offset_ms"]) >= 30_000:
            off = c["clock_offset_ms"] / 1000
            out.append({"camera_id": cid, "camera": c["name"], "kind": "drift",
                        "offset_ms": c["clock_offset_ms"],
                        "message": f"{c['name']}: recorder clock was {off:+.0f} s from reference "
                                   f"when last checked"})
    return out


def build_bundle(conn, req: BundleRequest, *, data_dir: Path, footage_root: Path | None,
                 require_sources: bool = True, now: datetime | None = None,
                 certificate=None) -> BuiltBundle:
    """Build the archive on disk. Does not write to the database — see `record_bundle`.

    `certificate`, if given, is called with the finished manifest and returns PDF bytes, which are
    added to the archive after the Merkle root is fixed: the certificate is a document *about*
    the hashed items, so it cannot be one of them.
    """
    ev = load_evidence(conn, req)
    bundle_id = str(uuid.uuid4())
    created = (now or datetime.now(UTC)).astimezone(UTC)
    footage = _index_footage(footage_root)
    warnings: list[str] = []

    work = Path(tempfile.mkdtemp(prefix="bundle-", dir=_tmp_parent(data_dir)))
    try:
        items: list[dict[str, Any]] = []

        # --- original recordings
        sources = []
        for clip in sorted(ev["clips"], key=lambda c: (str(c["camera_id"]), c["ts_start"])):
            cam = ev["cameras"][str(clip["camera_id"])]
            name = _source_name(clip["source_uri"])
            entry = {"clip_id": str(clip["clip_id"]), "camera_id": str(clip["camera_id"]),
                     "camera": cam["name"], "file_name": name, "source_uri": clip["source_uri"],
                     "sha256_at_import": clip["source_sha256"],
                     "hashed_at_import": _iso(clip.get("imported_at")),
                     "hashed_by": clip.get("imported_by"),
                     "recorded_from": _iso(clip["ts_start"]), "recorded_to": _iso(clip["ts_end"])}
            candidates = footage.get(name or "", [])
            match = None
            for cand in candidates:
                h = hash_file(cand)
                if h.sha256 == clip["source_sha256"]:
                    match = (cand, h)
                    break
            if match is None:
                reason = ("not found under the footage directory" if not candidates else
                          "a file with this name exists but its content no longer matches the "
                          "hash recorded at import")
                if candidates or require_sources:
                    raise BundleRefused(f"source recording {name}: {reason}")
                entry["included"] = False
                entry["not_included_reason"] = reason
                warnings.append(f"{name}: {reason}; cited by hash only")
            else:
                # A folder per recording, named by its hash: DVR exports routinely reuse one file
                # name per hour, and two of them from one camera must not collide.
                path = (f"source/{_safe(cam['name'])}/{clip['source_sha256'][:16]}/"
                        f"{_safe(name or 'recording')}")
                dest = work / path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(match[0], dest)
                copied = hash_file(dest)
                if copied.sha256 != clip["source_sha256"]:
                    raise BundleRefused(f"{name} changed while it was being copied")
                entry.update(included=True, path=path, rehashed_at=created.isoformat())
                items.append({"path": path, "kind": "original_recording",
                              **_hash_fields(copied)})
            sources.append(entry)

        # --- derived evidence frames
        frames = []
        for t in ev["tracks"]:
            kf = resolve_keyframe(data_dir, t["best_keyframe_uri"], tenant_id=req.tenant_id,
                                  site_id=req.site_id)
            if kf is None:
                frames.append({"track_id": str(t["track_id"]), "included": False})
                continue
            path = f"frames/{t['track_id']}.jpg"
            dest = work / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(kf, dest)
            h = hash_file(dest)
            if h.sha256 != t["frame_sha256"]:
                raise BundleRefused(f"evidence frame for track {t['track_id']} failed its "
                                    f"integrity check")
            items.append({"path": path, "kind": "derived_frame", **_hash_fields(h)})
            frames.append({"track_id": str(t["track_id"]), "included": True, "path": path,
                           "captured_at": _iso(t["keyframe_ts"]), "bbox": t["keyframe_bbox"]})

        # --- the index rows the answer rested on
        records = {
            "records/tracks.json": [_track_record(t, ev["cameras"]) for t in ev["tracks"]],
            "records/cameras.json": [_camera_record(c) for c in ev["cameras"].values()],
            "records/coverage.json": [
                {"camera_id": str(u["camera_id"]), "from": _iso(u["ts_start"]),
                 "to": _iso(u["ts_end"]), "state": u["state"], "source": u["source"],
                 "detail": u["detail"]} for u in ev["uptime"]],
        }
        for path, data in records.items():
            dest = work / path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(data, indent=1, sort_keys=True, default=str) + "\n")
            items.append({"path": path, "kind": "index_record", **_hash_fields(hash_file(dest))})

        try:
            root = merkle_root([(i["sha256"], i["path"]) for i in items])
        except ValueError as e:
            raise BundleRefused(f"the bundle's file list is not well formed: {e}") from e
        start, end = ev["window"]
        manifest = {
            "format": BUNDLE_FORMAT,
            "bundle_id": bundle_id,
            "created_at": created.isoformat(),
            "software": software_version(),
            "tenant": {"id": req.tenant_id, "name": ev["tenant_name"]},
            "site": {"id": req.site_id, "name": ev["site_name"], "timezone": ev["tz"]},
            "requested_by": req.requested_by,
            "purpose": req.purpose,
            "question": req.question,
            "window": {"from": _iso(start), "to": _iso(end)},
            "cameras": sorted(ev["cameras"]),
            "merkle": {"scheme": MERKLE_SCHEME, "root": root, "leaves": len(items)},
            "items": sorted(items, key=lambda i: i["path"]),
            "sources": sources,
            "frames": frames,
            "clock_warnings": clock_warnings(ev["cameras"]),
            # Facts the person signing Part A should weigh before affirming that the recorder
            # was "working properly" throughout. Shown to them; never decided for them.
            "coverage_gaps": [{"camera": g["camera"], "from": _iso(g["from"]),
                               "to": _iso(g["to"])} for g in ev["gaps"]],
            "hash_provenance": (
                "Smart Cam Monitoring does not keep the recordings. Each original listed here was "
                "hashed with SHA-256 when it was imported (at the time shown, where that time was "
                "recorded). When this bundle was prepared, a file whose SHA-256 matched that value "
                "was found in the footage location, copied byte for byte, and hashed again; a file "
                "that did not match would have been refused. The value identifies the file as "
                "received and does not by itself establish that it matches the recorder's "
                "internal storage. The copy in this bundle is identical to the file received, so "
                "the same value applies to both."),
            "derived_frames_note": (
                "Files under frames/ are JPEG images decoded and re-encoded from the original "
                "recordings to show where to look. They are not the electronic record; the files "
                "under source/ are."),
            "attribution": ev["attribution"],
            "warnings": warnings,
        }
        manifest_bytes = (json.dumps(manifest, indent=1, sort_keys=True) + "\n").encode()
        (work / "manifest.json").write_bytes(manifest_bytes)
        manifest_sha = hashlib.sha256(manifest_bytes).hexdigest()

        (work / "README.txt").write_text(_readme(manifest, manifest_sha))
        (work / "verify_bundle.py").write_text(_verifier_source())
        cert_sha = None
        if certificate is not None:
            pdf = certificate({**manifest, "manifest_sha256": manifest_sha,
                               "_work_dir": str(work)})
            (work / "certificate").mkdir()
            (work / "certificate" / "certificate-draft.pdf").write_bytes(pdf)
            cert_sha = hashlib.sha256(pdf).hexdigest()

        sums = sorted((hash_file(p).sha256, p.relative_to(work).as_posix())
                      for p in work.rglob("*") if p.is_file())
        sums_bytes = "".join(f"{h}  {p}\n" for h, p in sums).encode()
        (work / "SHA256SUMS").write_bytes(sums_bytes)

        out_dir = data_dir / "bundles" / req.tenant_id / req.site_id
        out_dir.mkdir(parents=True, exist_ok=True)
        archive = out_dir / f"{bundle_id}.zip"
        _zip(work, archive, top=f"evidence-{bundle_id}")
        return BuiltBundle(bundle_id, archive, hash_file(archive).sha256, manifest_sha, root,
                           manifest, warnings, track_ids=[str(t["track_id"]) for t in ev["tracks"]],
                           sums_sha256=hashlib.sha256(sums_bytes).hexdigest(),
                           certificate_sha256=cert_sha)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def record_bundle(conn, req: BundleRequest, built: BuiltBundle) -> None:
    """Insert the bundle and its 'created' custody entry in one transaction.

    The bundle row is immutable once written (migration 006); every later access is a new custody
    row, never an edit.
    """
    m = built.manifest
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO evidence_bundles (bundle_id, tenant_id, site_id, requested_by, purpose, "
            "window_start, window_end, cameras, frame_count, merkle_root, clock_warnings, "
            "certificate_uri, archive_uri, manifest_sha256, archive_sha256, certificate_sha256, "
            "track_ids, source_files, question, sums_sha256) VALUES (%s,%s,%s,%s,%s,%s,%s,"
            "%s::uuid[],%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid[],%s,%s,%s)",
            (built.bundle_id, req.tenant_id, req.site_id, req.requested_by, req.purpose,
             m["window"]["from"], m["window"]["to"], m["cameras"],
             sum(1 for f in m["frames"] if f["included"]), built.merkle_root,
             Jsonb(m["clock_warnings"]),
             "certificate/certificate-draft.pdf" if built.certificate_sha256 else None,
             str(built.archive), built.manifest_sha256, built.archive_sha256,
             built.certificate_sha256, built.track_ids, Jsonb(m["sources"]), req.question,
             built.sums_sha256))
        cur.execute(
            "INSERT INTO custody_log (bundle_id, actor, action, detail) "
            "VALUES (%s,%s,'created',%s)",
            (built.bundle_id, req.requested_by, Jsonb({
                "purpose": req.purpose, "merkle_root": built.merkle_root,
                "manifest_sha256": built.manifest_sha256, "archive_sha256": built.archive_sha256,
                "items": len(m["items"]), "warnings": built.warnings})))


def log_custody(conn, bundle_id: str, actor: str, action: str, detail: dict | None = None) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("INSERT INTO custody_log (bundle_id, actor, action, detail) "
                    "VALUES (%s,%s,%s,%s)", (bundle_id, actor, action, Jsonb(detail or {})))


# --- helpers ---------------------------------------------------------------------------------

def _hash_fields(h: Hashes) -> dict[str, Any]:
    return {"sha256": h.sha256, "sha1": h.sha1, "md5": h.md5, "size_bytes": h.size}


def _safe(name: str) -> str:
    return "".join(c if c.isalnum() or c in "-_." else "_" for c in name).strip("_") or "camera"


def _tmp_parent(data_dir: Path) -> Path:
    p = data_dir / "tmp"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _track_record(t: dict, cameras: dict) -> dict:
    return {"track_id": str(t["track_id"]), "camera_id": str(t["camera_id"]),
            "camera": cameras[str(t["camera_id"])]["name"], "class": t["class"],
            "first_seen": _iso(t["ts_start"]), "last_seen": _iso(t["ts_end"]),
            "confidence_max": t["conf_max"], "confidence_mean": t["conf_mean"],
            "observations": t["n_frames"], "evidence_frame_at": _iso(t["keyframe_ts"]),
            "evidence_frame_bbox": t["keyframe_bbox"], "source_sha256": t["clip_uri"],
            "analysis": t["model_versions"]}


def _camera_record(c: dict) -> dict:
    return {k: (str(v) if k == "camera_id" else _iso(v) if isinstance(v, datetime) else v)
            for k, v in c.items()}


def _zip(src: Path, archive: Path, top: str) -> None:
    tmp = archive.with_suffix(".zip.part")
    try:
        _write_zip(src, tmp, top)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    tmp.replace(archive)


def _write_zip(src: Path, tmp: Path, top: str) -> None:
    with zipfile.ZipFile(tmp, "w") as z:
        for p in sorted(src.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(src).as_posix()
            # Video is already compressed; storing it keeps the archive fast to build and lets
            # a verifier stream it. Text compresses well and is small either way.
            method = zipfile.ZIP_STORED if rel.startswith("source/") or rel.endswith(".jpg") \
                else zipfile.ZIP_DEFLATED
            info = zipfile.ZipInfo(f"{top}/{rel}", date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = method
            info.external_attr = 0o644 << 16
            with p.open("rb") as f, z.open(info, "w", force_zip64=True) as out:
                shutil.copyfileobj(f, out, 1 << 20)


#: Files a bundle may contain besides its manifest items. Anything else fails verification.
SUPPORT_FILES = ("manifest.json", "SHA256SUMS", "README.txt", "verify_bundle.py",
                 "certificate/certificate-draft.pdf")


def _verifier_source() -> str:
    merkle = (Path(__file__).parent / "merkle.py").read_text()
    return merkle + f"\n\nSUPPORT_FILES = {SUPPORT_FILES!r}\n" + VERIFY_MAIN


VERIFY_MAIN = '''

# --- verify_bundle.py: standalone check of an extracted evidence bundle ----------------------

def _main() -> int:
    import json
    import sys
    from pathlib import Path

    root = Path(sys.argv[1] if len(sys.argv) > 1 else ".")
    try:
        manifest = json.loads((root / "manifest.json").read_text())
        paths = {i["path"] for i in manifest["items"]}
    except (OSError, ValueError, KeyError) as e:
        print(f"manifest.json unreadable: {e}")
        print("BUNDLE FAILED VERIFICATION")
        return 1
    ok = True
    # Nothing may sit in the bundle that the manifest does not account for: an extra file under
    # source/ would otherwise pass as an original recording.
    allowed = paths | set(SUPPORT_FILES)
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_file() and rel not in allowed:
            print(f"UNLISTED  {rel}")
            ok = False
    for item in manifest["items"]:
        p = root / item["path"]
        if not p.is_file():
            print(f"MISSING   {item['path']}")
            ok = False
            continue
        h = hashlib.sha256(p.read_bytes()).hexdigest()
        status = "ok" if h == item["sha256"] else "MISMATCH"
        ok &= status == "ok"
        print(f"{status:<9} {item['path']}")
    try:
        recomputed = merkle_root([(i["sha256"], i["path"]) for i in manifest["items"]])
    except ValueError as e:
        print(f"manifest items are malformed: {e}")
        print("BUNDLE FAILED VERIFICATION")
        return 1
    same = recomputed == manifest["merkle"]["root"]
    print(f"merkle root {recomputed} {'matches' if same else 'DOES NOT MATCH'} the manifest")
    ok &= same
    print("BUNDLE VERIFIED" if ok else "BUNDLE FAILED VERIFICATION")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
'''


def _readme(m: dict, manifest_sha: str) -> str:
    def src(s: dict) -> str:
        line = f"  {s['file_name']}\n    SHA-256 {s['sha256_at_import']}"
        if not s.get("included"):
            line += f"  (NOT INCLUDED: {s.get('not_included_reason', '')})"
        return line

    srcs = "\n".join(src(s) for s in m["sources"])
    warn = "\n".join(f"  - {w['message']}" for w in m["clock_warnings"]) or "  none"
    return f"""EVIDENCE BUNDLE {m['bundle_id']}
Prepared by Smart Cam Monitoring on {m['created_at']} for {m['tenant']['name']},
site {m['site']['name']} (local timezone {m['site']['timezone']}).
Requested by: {m['requested_by']}
Purpose: {m['purpose']}

This bundle is PREPARED for certification. It is not itself a certificate and certifies
nothing. Under the Bharatiya Sakshya Adhiniyam, 2023 (section 63), a certificate must be signed
by the person responsible for the device or recording (Part A) and by an expert (Part B). The
draft in certificate/ is pre-filled from the records below for those people to check, complete
and sign.

ORIGINAL RECORDINGS (the electronic records)
{srcs}

{m['hash_provenance']}

{m['derived_frames_note']}

CLOCK
{warn}

HOW TO VERIFY THIS BUNDLE — no Smart Cam Monitoring software is needed
1. Check every file against its hash:
     shasum -a 256 -c SHA256SUMS            (macOS / Linux)
     certutil -hashfile <file> SHA256       (Windows, one file at a time)
2. Recompute the Merkle root over the items in manifest.json:
     python3 verify_bundle.py .
   Scheme: {m['merkle']['scheme']}.
   Expected root: {m['merkle']['root']}
3. manifest.json itself has SHA-256 {manifest_sha}.
   This value is recorded by Smart Cam Monitoring when the bundle is created and printed on the
   certificate draft; a different value means the manifest has been altered.

{('Footage attribution: ' + m['attribution']) if m.get('attribution') else ''}
"""

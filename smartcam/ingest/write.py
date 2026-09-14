"""Writing an analysed clip into the index: one transaction per clip, safe to repeat.

Three rules shape every statement here.

**A clip is all or nothing.** Tracks, the clip row and the uptime row for a clip are written in a
single transaction. Uptime is what makes an empty answer a real negative; writing it for a clip
whose tracks failed to land would turn "we never analysed this" into "nobody was there".

**Re-running replaces, it never accumulates.** Rows are keyed to the source file's hash, not to
where the file happened to sit on disk or to how much of it this run analysed. A second import of
the same file — with a different root, a smoke-test time limit, or a new detector — removes the
previous import's rows for that file before writing its own. Otherwise a re-run doubles every count.

**Scope is checked, not trusted.** The camera must already belong to the tenant and site being
written, verified against the database before any row is inserted; `camera_uptime` additionally
enforces it by trigger (migration 005). Tracks are the rows the keyframe route scopes on, so a
mis-attributed track would be a cross-tenant disclosure, not just a bad count.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from psycopg.types.json import Jsonb

from smartcam.ingest.meva import SiteProfile


class ImportRefused(RuntimeError):
    pass


@dataclass(frozen=True)
class CameraRow:
    camera_id: str
    key: str
    name: str
    width: int
    height: int
    src_fps: float
    codec: str | None
    grade: str
    grade_notes: list[str]


@dataclass(frozen=True)
class TrackRow:
    track_id: str
    camera_id: str
    cls: str
    ts_start: datetime
    ts_end: datetime
    conf_max: float
    conf_mean: float
    n_frames: int
    bbox_first: list[float]
    bbox_last: list[float]
    keyframe_key: str | None
    keyframe_sha256: str | None
    keyframe_ts: datetime
    keyframe_bbox: list[float]
    clip_uri: str
    model_versions: dict


@dataclass(frozen=True)
class ClipRow:
    clip_id: str
    camera_id: str
    ts_start: datetime
    #: End of the footage actually analysed — never the declared end of the file.
    analysed_end: datetime
    #: The file's own extent, used to find previous imports' rows for the same stretch of footage.
    file_start: datetime
    file_end: datetime
    keyframe_uris: list[str]
    frame_sha256: list[str]
    source_uri: str
    source_sha256: str
    uptime_detail: str


def track_id(site_id: str, camera_key: str, source_sha256: str, fingerprint: str,
             local_id: int) -> str:
    return str(uuid.uuid5(uuid.UUID(site_id),
                          f"track:{camera_key}:{source_sha256}:{fingerprint}:{local_id}"))


def clip_id(site_id: str, camera_key: str, source_sha256: str) -> str:
    return str(uuid.uuid5(uuid.UUID(site_id), f"clip:{camera_key}:{source_sha256}"))


def clip_uri(source_sha256: str) -> str:
    """Stable across roots and prefixes: the file's identity, not its location."""
    return f"sha256:{source_sha256}"


def ensure_months(conn, instants: Iterable[datetime]) -> None:
    """Create monthly partitions for every month these instants fall in.

    Months are computed by the database in its own session timezone, because that is the zone
    partition bounds were cast in (003). Computing months in UTC here instead would send an
    instant late on the last UTC day of a month into a partition that was never created.
    """
    values = sorted(set(instants))
    if not values:
        return
    with conn.cursor() as cur:
        cur.execute(
            "SELECT DISTINCT date_trunc('month', t)::date FROM unnest(%s::timestamptz[]) AS t",
            (values,))
        for (month,) in cur.fetchall():
            cur.execute("SELECT ensure_partitions(%s, %s)", (month, month))


def bootstrap_site(conn, p: SiteProfile, cams: list[CameraRow], detector_id: str) -> None:
    """Create or refresh the tenant, site and cameras. Idempotent.

    Refuses when the site already exists under another tenant, or with another timezone: both mean
    this import would attach footage to a place it does not describe.
    """
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "INSERT INTO tenants (tenant_id, name, legal_basis, face_enabled, is_school, "
            "retention_days) VALUES (%s, %s, %s, false, false, 365) "
            "ON CONFLICT (tenant_id) DO UPDATE SET name = EXCLUDED.name",
            (p.tenant_id, p.tenant_name, p.legal_basis))
        cur.execute("SELECT tenant_id, tz FROM sites WHERE site_id = %s", (p.site_id,))
        row = cur.fetchone()
        if row is None:
            cur.execute(
                "INSERT INTO sites (site_id, tenant_id, name, tz, data_attribution, "
                "example_questions) VALUES (%s, %s, %s, %s, %s, %s)",
                (p.site_id, p.tenant_id, p.site_name, p.tz, p.attribution, p.examples))
        else:
            if str(row[0]) != p.tenant_id:
                raise ImportRefused(f"site {p.site_id} belongs to another tenant")
            if row[1] != p.tz:
                raise ImportRefused(
                    f"site timezone is {row[1]}, but this import says {p.tz}. Refusing: every "
                    f"track would land at the wrong time of day.")
            cur.execute(
                "UPDATE sites SET name = %s, data_attribution = %s, example_questions = %s "
                "WHERE site_id = %s", (p.site_name, p.attribution, p.examples, p.site_id))

        for c in cams:
            cur.execute("SELECT tenant_id, site_id, capabilities FROM cameras WHERE camera_id = %s",
                        (c.camera_id,))
            existing = cur.fetchone()
            if existing and (str(existing[0]), str(existing[1])) != (p.tenant_id, p.site_id):
                raise ImportRefused(f"camera {c.camera_id} belongs to another site")
            caps = dict(existing[2]) if existing else {}
            if caps.get("detector") != detector_id:
                # A new detector invalidates any measured capability: the old numbers describe a
                # different model. Back to "not assessed" until it is scored again.
                caps = {"detector": detector_id,
                        "classes": {"person": {"status": "not_assessed"}}}
            cur.execute(
                "INSERT INTO cameras (camera_id, tenant_id, site_id, name, recorder_id, grade, "
                "detect_w, detect_h, detect_fps, codec, grade_notes, graded_from, capabilities) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'recording',%s) "
                "ON CONFLICT (camera_id) DO UPDATE SET name = EXCLUDED.name, "
                "recorder_id = EXCLUDED.recorder_id, grade = EXCLUDED.grade, "
                "detect_w = EXCLUDED.detect_w, detect_h = EXCLUDED.detect_h, "
                "detect_fps = EXCLUDED.detect_fps, codec = EXCLUDED.codec, "
                "grade_notes = EXCLUDED.grade_notes, graded_from = 'recording', "
                "capabilities = EXCLUDED.capabilities",
                (c.camera_id, p.tenant_id, p.site_id, c.name, f"{p.key}:{c.key}", c.grade,
                 c.width, c.height, c.src_fps, c.codec, c.grade_notes, Jsonb(caps)))


def write_clip(conn, p: SiteProfile, clip: ClipRow, tracks: list[TrackRow]) -> None:
    uri = clip_uri(clip.source_sha256)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute("SELECT tenant_id, site_id FROM cameras WHERE camera_id = %s",
                    (clip.camera_id,))
        owner = cur.fetchone()
        if owner is None or (str(owner[0]), str(owner[1])) != (p.tenant_id, p.site_id):
            raise ImportRefused(f"camera {clip.camera_id} is not part of site {p.site_id}")
        for t in tracks:
            if t.camera_id != clip.camera_id:
                raise ImportRefused("a track names a different camera from its clip")

        # Previous imports of this stretch of footage on this camera, whatever their config, time
        # limit or source hash. Keying only on this file's hash would leave behind rows from an
        # earlier import of the same recording — a partial download, a re-export — and every
        # person in the overlap would be counted twice.
        lo = clip.file_start - timedelta(seconds=5)
        hi = clip.file_end + timedelta(seconds=5)
        cur.execute(
            "SELECT clip_id, source_sha256 FROM clips WHERE tenant_id = %s AND site_id = %s "
            "AND camera_id = %s AND ts_start < %s AND ts_end > %s",
            (p.tenant_id, p.site_id, clip.camera_id, hi, lo))
        prior = cur.fetchall()
        uris = [uri] + [clip_uri(sha) for _, sha in prior if sha]
        cur.execute(
            "DELETE FROM tracks WHERE tenant_id = %s AND site_id = %s AND camera_id = %s "
            "AND ts_start >= %s AND ts_start < %s AND clip_uri = ANY(%s)",
            (p.tenant_id, p.site_id, clip.camera_id, lo, hi, uris))
        clip_ids = [clip.clip_id] + [str(c) for c, _ in prior]
        cur.execute("DELETE FROM clips WHERE clip_id = ANY(%s::uuid[])", (clip_ids,))
        cur.execute(
            "DELETE FROM camera_uptime WHERE camera_id = %s AND source = 'recorded_import' "
            "AND (clip_id = ANY(%s::uuid[]) OR (ts_start < %s AND ts_end > %s))",
            (clip.camera_id, clip_ids, hi, lo))

        if tracks:
            cur.executemany(
                "INSERT INTO tracks (track_id, tenant_id, site_id, camera_id, class, ts_start, "
                "ts_end, dwell_s, conf_max, conf_mean, n_frames, bbox_first, bbox_last, attrs, "
                "best_keyframe_uri, frame_sha256, keyframe_ts, keyframe_bbox, clip_uri, "
                "model_versions) VALUES "
                "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'{}'::jsonb,%s,%s,%s,%s,%s,%s)",
                [(t.track_id, p.tenant_id, p.site_id, t.camera_id, t.cls, t.ts_start, t.ts_end,
                  (t.ts_end - t.ts_start).total_seconds(), t.conf_max, t.conf_mean, t.n_frames,
                  Jsonb(t.bbox_first), Jsonb(t.bbox_last), t.keyframe_key, t.keyframe_sha256,
                  t.keyframe_ts, Jsonb(t.keyframe_bbox), t.clip_uri, Jsonb(t.model_versions))
                 for t in tracks])

        cur.execute(
            "INSERT INTO clips (clip_id, tenant_id, site_id, camera_id, ts_start, ts_end, "
            "keyframe_uris, frame_sha256, retained_reason, source_uri, source_sha256) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'backfill',%s,%s)",
            (clip.clip_id, p.tenant_id, p.site_id, clip.camera_id, clip.ts_start,
             clip.analysed_end, clip.keyframe_uris, clip.frame_sha256, clip.source_uri,
             clip.source_sha256))

        # Uptime means footage we analysed, so it ends where analysis ended. A supervisor row
        # starting at the same instant is left alone; the recorded row yields instead.
        cur.execute(
            "INSERT INTO camera_uptime (camera_id, tenant_id, site_id, ts_start, ts_end, state, "
            "detail, source, clip_id) VALUES (%s,%s,%s,%s,%s,'online',%s,'recorded_import',%s) "
            "ON CONFLICT (camera_id, ts_start) DO NOTHING",
            (clip.camera_id, p.tenant_id, p.site_id, clip.ts_start, clip.analysed_end,
             clip.uptime_detail, clip.clip_id))
        if cur.rowcount != 1:
            # A supervisor row already starts at this instant. Writing tracks without the uptime
            # row that makes them answerable would leave people counted inside a gap; refuse the
            # clip instead, which rolls the whole transaction back.
            raise ImportRefused(
                f"an uptime row already starts at {clip.ts_start} for camera {clip.camera_id}; "
                f"refusing to write tracks without coverage")
        refresh_as_of(cur, p.site_id)


def refresh_as_of(cur, site_id: str) -> None:
    """A recorded site is answered as of the end of its latest analysed footage.

    Recomputed rather than ratcheted with GREATEST, so a re-import that analyses less moves it
    back instead of leaving the site "as of" a time we no longer hold footage for.
    """
    cur.execute(
        "UPDATE sites SET as_of = (SELECT max(ts_end) FROM camera_uptime "
        "WHERE site_id = %s AND source = 'recorded_import') WHERE site_id = %s",
        (site_id, site_id))


def set_capability(conn, *, camera_id: str, cls: str, value: dict) -> None:
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(
            "UPDATE cameras SET capabilities = jsonb_set(jsonb_set(capabilities, '{classes}', "
            "COALESCE(capabilities->'classes', '{}'::jsonb)), ARRAY['classes', %s], %s) "
            "WHERE camera_id = %s",
            (cls, Jsonb(value), camera_id))

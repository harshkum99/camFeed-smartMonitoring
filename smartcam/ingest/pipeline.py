"""One recorded clip in, one clip's worth of index rows out.

    probe -> hash source -> decode at 5 fps -> detect -> track -> summarise
          -> grab native-resolution evidence frames -> rows for write.py

Nothing here touches the database, so the whole path is testable with a synthetic frame source
and a fake detector. A frame log is kept beside the rows — every detection and every track
observation per sampled frame — because the scorer needs to compare tracks against ground truth
frame by frame, and re-running a 3-minute detection pass to get that is wasteful.

**A clip whose decode came up short is a failed clip.** It produces no rows at all, so its
footage reads as a coverage gap rather than as a stretch of time in which nobody appeared.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from smartcam.evidence.store import sha256_file
from smartcam.ingest.decode import (
    DecodeReport,
    Spawner,
    VideoInfo,
    _popen,
    grab_frames,
    iter_sampled_frames,
    probe_video,
    sample_step,
)
from smartcam.ingest.detect import Detector
from smartcam.ingest.keyframes import KeyframeStore
from smartcam.ingest.meva import SiteProfile
from smartcam.ingest.recorded import Clip
from smartcam.ingest.summarise import summarise
from smartcam.ingest.track import ByteTracker, TrackerParams
from smartcam.ingest.write import ClipRow, TrackRow, clip_id, clip_uri, track_id
from smartcam.survey.probe import Runner, _run


@dataclass(frozen=True)
class IngestConfig:
    sample_fps: float = 5.0
    tracker: TrackerParams = field(default_factory=TrackerParams)
    limit_seconds: float | None = None
    start_seconds: float = 0.0


@dataclass
class ClipOutcome:
    file: str
    camera_key: str
    ok: bool
    reason: str | None = None
    tracks: int = 0
    sampled: int = 0
    seconds: float = 0.0
    clip: ClipRow | None = None
    rows: list[TrackRow] = field(default_factory=list)
    info: VideoInfo | None = None
    log_path: Path | None = None


def fingerprint(cfg: IngestConfig, detector: Detector, step: int) -> str:
    """Identifies a configuration, so rows from different configs never share ids."""
    blob = json.dumps({
        "detector": detector.identity.id, "fps": cfg.sample_fps, "step": step,
        "tracker": asdict(cfg.tracker),
        # The analysed window is part of the identity: local track ids restart at 1 for every
        # run, so two windows of one file must never share track ids.
        "start_seconds": cfg.start_seconds, "limit_seconds": cfg.limit_seconds,
    }, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def process_clip(
    clip: Clip, *, profile: SiteProfile, detector: Detector, cfg: IngestConfig,
    store: KeyframeStore, runs_dir: Path, source_uri: str,
    spawn: Spawner = _popen, runner: Runner = _run,
    sha256: Callable[[Path], str] = sha256_file, out: Callable[[str], None] = print,
) -> ClipOutcome:
    t0 = time.monotonic()
    result = ClipOutcome(clip.path.name, clip.camera_key, ok=False)
    mapping = profile.cameras.get(clip.camera_key)
    if mapping is None:
        result.reason = "camera not mapped in the site profile"
        return result
    camera_id, _name = mapping
    if clip.measured_seconds is None:
        result.reason = f"duration could not be measured ({clip.error})"
        return result
    if cfg.start_seconds >= clip.measured_seconds:
        result.reason = (f"--start-seconds {cfg.start_seconds:g} is past the end of this "
                         f"{clip.measured_seconds:.0f}s clip")
        return result

    info = probe_video(clip.path, runner=runner)
    result.info = info
    source_sha = sha256(clip.path)
    # The time the hash was taken, not the time a row was later written: the hash report states
    # it, and analysis of a clip can take minutes.
    hashed_at = datetime.now(UTC)
    step = sample_step(info.src_fps, cfg.sample_fps)
    fp = fingerprint(cfg, detector, step)
    tracker = ByteTracker(info.src_fps / step, cfg.tracker, width=info.width, height=info.height)

    report = DecodeReport()
    frame_log: list[dict] = []
    last_progress = time.monotonic()
    for frame in iter_sampled_frames(
        clip.path, info, step=step, report=report, limit_seconds=cfg.limit_seconds,
        start_seconds=cfg.start_seconds, measured_seconds=clip.measured_seconds, spawn=spawn,
    ):
        dets = detector.detect(frame.rgb)
        tracker.update(dets, n=frame.n, step=step)
        frame_log.append({"n": frame.n,
                          "dets": [[*map(lambda v: round(v, 4), d.box), round(d.score, 4)]
                                   for d in dets]})
        if time.monotonic() - last_progress > 30:
            out(f"    … {clip.path.name}: {report.frames}/{report.expected} frames")
            last_progress = time.monotonic()

    result.sampled = report.frames
    if not report.ok:
        result.reason = report.reason
        result.seconds = time.monotonic() - t0
        return result

    history = tracker.tracks_history()
    summaries = summarise(history, min_hits=cfg.tracker.min_hits)
    cid = clip_id(profile.site_id, clip.camera_key, source_sha)

    frames = grab_frames(clip.path, info, [s.best_n for s in summaries], spawn=spawn)
    model_versions = {
        "detector": detector.identity.id, "tracker": "bytetrack-smartcam",
        "tracker_params": asdict(cfg.tracker), "sample_fps": cfg.sample_fps, "step": step,
        "fingerprint": fp, "recorder_tz": profile.tz, "source_sha256": source_sha,
        "source_frame_indexing": "n from clip start; ts = start + n / src_fps",
    }
    rows: list[TrackRow] = []
    keyframe_uris, frame_hashes = [], []
    for s in summaries:
        stored = None
        if s.best_n in frames:
            stored = store.put(frames[s.best_n], tenant_id=profile.tenant_id,
                               site_id=profile.site_id, camera_id=camera_id)
            keyframe_uris.append(stored.key)
            frame_hashes.append(stored.sha256)
        rows.append(TrackRow(
            track_id=track_id(profile.site_id, clip.camera_key, source_sha, fp, s.local_id),
            camera_id=camera_id, cls=s.cls,
            ts_start=s.ts_start(clip.starts_at, info.src_fps),
            ts_end=s.ts_end(clip.starts_at, info.src_fps),
            conf_max=s.conf_max, conf_mean=s.conf_mean, n_frames=s.n_frames,
            bbox_first=s.bbox_first, bbox_last=s.bbox_last,
            keyframe_key=stored.key if stored else None,
            keyframe_sha256=stored.sha256 if stored else None,
            keyframe_ts=s.keyframe_ts(clip.starts_at, info.src_fps),
            keyframe_bbox=s.best_bbox, clip_uri=clip_uri(source_sha), model_versions=model_versions,
        ))

    start = clip.starts_at + timedelta(seconds=cfg.start_seconds)
    analysed_to = min(
        clip.trusted_end,
        clip.starts_at + timedelta(seconds=((report.last_n or 0) + step) / info.src_fps),
    )
    if analysed_to <= start:
        result.reason = "analysed window is empty"
        return result
    result.clip = ClipRow(
        clip_id=cid, camera_id=camera_id, ts_start=start, analysed_end=analysed_to,
        file_start=clip.starts_at, source_hashed_at=hashed_at,
        file_end=max(clip.trusted_end, clip.declared_end or clip.trusted_end),
        keyframe_uris=keyframe_uris, frame_sha256=frame_hashes,
        source_uri=source_uri, source_sha256=source_sha,
        uptime_detail=(f"{clip.path.name}; measured {clip.measured_seconds:.2f}s; analysed "
                       f"{(analysed_to - start).total_seconds():.1f}s at {cfg.sample_fps:g} fps"),
    )
    result.rows = rows
    result.tracks = len(rows)

    runs_dir.mkdir(parents=True, exist_ok=True)
    log_path = runs_dir / f"{cid}.{fp}.json.gz"
    with gzip.open(log_path, "wt", compresslevel=6) as f:
        json.dump({
            "file": clip.path.name, "camera_key": clip.camera_key, "source_sha256": source_sha,
            "width": info.width, "height": info.height, "src_fps": info.src_fps, "step": step,
            "fingerprint": fp, "detector": detector.identity.id,
            "min_hits": cfg.tracker.min_hits, "frames": frame_log,
            "tracks": {str(k): [[o.n, *o.box, o.score] for o in v] for k, v in history.items()},
            "written": [s.local_id for s in summaries],
        }, f)
    result.log_path = log_path
    result.ok = True
    result.seconds = time.monotonic() - t0
    return result

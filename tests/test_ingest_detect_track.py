"""Detection, tracking and track summaries: the path from a frame to a row in `tracks`.

The product counts people by counting track rows, so the failures that matter here are the silent
ones. A preprocessing slip lowers recall without an error; a vehicle written as a person answers a
question nobody measured; a tracker that splits one person into two doubles a count; a summary
whose times go backwards across a DST change puts evidence at the wrong moment. None of these
tests need a model file, a video, or the network.
"""

from __future__ import annotations

import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import numpy as np
import pytest

from smartcam.ingest.detect import (
    Detection,
    Detector,
    ModelError,
    ModelIdentity,
    load_onnx,
    postprocess,
    preprocess,
)
from smartcam.ingest.summarise import best_keyframe, summarise
from smartcam.ingest.track import ByteTracker, TrackerParams, TrackObs

# --- detect -----------------------------------------------------------------------------------


def test_preprocess_scales_to_unit_range_without_mean_std():
    """The benchmark recall was measured on [0, 1] input. ImageNet mean/std would raise no
    error, it would only quietly lose people."""
    x = preprocess(np.full((640, 640, 3), 255, dtype=np.uint8))
    assert x.dtype == np.float32
    assert x.shape == (1, 3, 640, 640)
    assert x.max() == 1.0
    assert x.min() == 1.0


def test_preprocess_rejects_a_frame_that_was_not_resized():
    """Feeding a 1080p frame would be reshaped nonsense, not a smaller detection."""
    with pytest.raises(ValueError):
        preprocess(np.zeros((1080, 1920, 3), dtype=np.uint8))


def _outputs() -> tuple[np.ndarray, np.ndarray]:
    logits = np.full((1, 300, 80), -10.0, dtype=np.float32)
    boxes = np.full((1, 300, 4), 0.5, dtype=np.float32)
    logits[0, 0, 0] = 5.0                       # person, confident
    boxes[0, 0] = (0.05, 0.5, 0.2, 0.4)         # left edge spills past 0
    logits[0, 1, 2] = 5.0                       # car: detected but never written
    boxes[0, 1] = (0.5, 0.5, 0.2, 0.2)
    logits[0, 2, 0] = -1.0                      # person below the 0.4 floor
    boxes[0, 2] = (0.3, 0.3, 0.1, 0.1)
    logits[0, 3, 0] = 5.0                       # person with a zero-width box
    boxes[0, 3] = (0.7, 0.7, 0.0, 0.2)
    return logits, boxes


def _assert_only_the_person(dets: list[Detection]) -> None:
    assert len(dets) == 1
    d = dets[0]
    assert d.cls == "person"
    assert d.score == pytest.approx(1 / (1 + np.exp(-5.0)), rel=1e-5)
    assert d.box == pytest.approx((0.0, 0.3, 0.15, 0.7), abs=1e-6)


def test_postprocess_keeps_only_measured_class_above_floor_with_clipped_xyxy():
    """Vehicles are unmeasured, sub-floor boxes are noise, and a degenerate box cannot be
    evidence. What survives must be in corner form and inside the frame."""
    logits, boxes = _outputs()
    _assert_only_the_person(postprocess(logits, boxes, floor=0.4))


class _FakeSession:
    def __init__(self, outputs):
        self.outputs = outputs
        self.calls: list[tuple] = []

    def run(self, output_names, input_feed):
        self.calls.append((output_names, input_feed))
        return list(self.outputs)


def test_detector_feeds_preprocessed_frame_and_postprocesses_outputs():
    """The input name and output order are the export's contract; getting either wrong would
    fail only against the real model, so pin them here."""
    session = _FakeSession(_outputs())
    det = Detector(session, ModelIdentity("dfine-s", "ab" * 32))
    dets = det.detect(np.zeros((640, 640, 3), dtype=np.uint8))
    _assert_only_the_person(dets)
    names, feed = session.calls[0]
    assert names == ["logits", "pred_boxes"]
    assert feed["pixel_values"].shape == (1, 3, 640, 640)


def _manifest(tmp_path: Path, sha: str) -> Path:
    m = tmp_path / "third_party.toml"
    m.write_text(f'[[model]]\nname = "dfine-s"\nfile = "model.onnx"\nsha256 = "{sha}"\n')
    return m


def test_load_onnx_refuses_hash_mismatch_before_importing_onnxruntime(tmp_path, monkeypatch):
    """The hash check is the licence gate. It must run before the runtime is touched, so an
    undeclared checkpoint is never parsed at all."""
    model = tmp_path / "model.onnx"
    model.write_bytes(b"not the declared weights")
    monkeypatch.setitem(sys.modules, "onnxruntime", None)   # any import would raise ImportError
    with pytest.raises(ModelError, match="does not match the manifest"):
        load_onnx(model, manifest=_manifest(tmp_path, "0" * 64), file_name="model.onnx")


def test_load_onnx_refuses_a_file_not_in_the_manifest(tmp_path, monkeypatch):
    """A checkpoint copied into the directory without a manifest entry has no licence
    evidence, so it is refused by name."""
    model = tmp_path / "other.onnx"
    model.write_bytes(b"x")
    monkeypatch.setitem(sys.modules, "onnxruntime", None)
    with pytest.raises(ModelError, match="not declared"):
        load_onnx(model, manifest=_manifest(tmp_path, "0" * 64), file_name="other.onnx")


# --- track ------------------------------------------------------------------------------------


def _person(x: float, y: float = 0.4, score: float = 0.9, w: float = 0.05,
            h: float = 0.2) -> Detection:
    return Detection((x, y, x + w, y + h), score, "person")


def _tracker(**params) -> ByteTracker:
    return ByteTracker(5, TrackerParams(**params), width=1000, height=1000)


def test_steady_walker_keeps_one_id():
    """One person split into several tracks is several people in a count."""
    tr = _tracker()
    ids = set()
    for n in range(10):
        ids |= {o.local_id for o in tr.update([_person(0.1 + 0.02 * n)], n=n)}
    assert len(ids) == 1
    assert len(tr.tracks_history()) == 1


def test_crossing_people_keep_distinct_ids():
    """Two people passing each other must not swap or merge; the motion model carries them
    through the frames where their boxes overlap."""
    tr = _tracker()
    seen: dict[int, list[float]] = {}
    for n in range(20):
        a = _person(0.2 + 0.025 * n, y=0.40)
        b = _person(0.7 - 0.025 * n, y=0.42)
        for o in tr.update([a, b], n=n):
            seen.setdefault(o.local_id, []).append(o.box[0])
    assert len(seen) == 2
    for xs in seen.values():
        assert len(xs) == 20
        diffs = np.diff(xs)
        assert np.all(diffs > 0) or np.all(diffs < 0)   # nobody reverses direction mid-track


def _gap_run(missing_frames: int) -> dict[int, list[TrackObs]]:
    tr = _tracker(lost_track_s=2.0)
    n = 0
    for _ in range(5):
        tr.update([_person(0.5)], n=n)
        n += 1
    for _ in range(missing_frames):
        tr.update([], n=n)
        n += 1
    for _ in range(5):
        tr.update([_person(0.5)], n=n)
        n += 1
    return tr.tracks_history()


def test_short_occlusion_is_reassociated_to_the_same_id():
    """Someone behind a pillar for a second is still the same person."""
    hist = _gap_run(5)
    assert list(hist) == [1]
    assert [o.n for o in hist[1]] == [0, 1, 2, 3, 4, 10, 11, 12, 13, 14]


def test_absence_longer_than_lost_window_starts_a_new_id():
    """Past `lost_track_s` the old track is closed; a reappearance is a new track."""
    hist = _gap_run(15)
    assert sorted(hist) == [1, 2]
    assert [o.n for o in hist[1]] == [0, 1, 2, 3, 4]
    assert [o.n for o in hist[2]] == [20, 21, 22, 23, 24]


def test_weak_box_continues_a_track_but_never_starts_one():
    """ByteTrack's core rule: low-confidence boxes rescue a half-hidden person but cannot
    conjure one."""
    params = dict(high_thresh=0.5, new_track_thresh=0.6)
    tr = _tracker(**params)
    for n in range(6):
        assert tr.update([_person(0.5, score=0.45)], n=n) == []
    tr2 = _tracker(**params)
    for n in range(6):
        tr2.update([_person(0.5, score=0.55)], n=n)       # confident, but not enough to start
    assert tr.tracks_history() == {} and tr2.tracks_history() == {}

    tr3 = _tracker(**params)
    tr3.update([_person(0.5, score=0.9)], n=0)
    for n in range(1, 6):
        obs = tr3.update([_person(0.5, score=0.45)], n=n)
        assert [o.local_id for o in obs] == [1]
    assert [o.score for o in tr3.tracks_history()[1]] == [0.9] + [0.45] * 5


def test_history_holds_confirmed_tracks_including_their_first_sighting():
    """A track confirmed on its second sighting must still start at its first, or ts_start
    is late; a one-frame flicker must not appear at all."""
    tr = _tracker()
    tr.update([], n=0)
    assert tr.update([_person(0.3)], n=1) == []           # tentative: nothing reported yet
    tr.update([_person(0.3), _person(0.8, y=0.1)], n=2)   # second box is a one-frame flicker
    tr.update([_person(0.3)], n=3)
    tr.update([_person(0.3)], n=4)
    hist = tr.tracks_history()
    assert list(hist) == [1]
    assert [o.n for o in hist[1]] == [1, 2, 3, 4]


# --- summarise --------------------------------------------------------------------------------


def _obs(n: int, score: float, box=(0.2, 0.2, 0.4, 0.6), tid: int = 1) -> TrackObs:
    return TrackObs(tid, n, box, score)


def test_min_hits_drops_short_tracks_and_confidence_is_summarised():
    """Two sightings at 5 fps is a flicker; confidence keeps both peak and mean so one lucky
    frame cannot make a shadow look certain."""
    hist = {
        1: [_obs(12, 0.5), _obs(6, 0.9), _obs(18, 0.7)],
        2: [_obs(0, 0.99, tid=2), _obs(6, 0.99, tid=2)],
    }
    (s,) = summarise(hist, min_hits=3)
    assert s.local_id == 1
    assert (s.n_first, s.n_last, s.n_frames) == (6, 18, 3)
    assert s.conf_max == 0.9
    assert s.conf_mean == pytest.approx(0.7)


def test_times_come_from_frame_indices_and_stay_monotonic_across_dst():
    """06:59Z on 2018-03-11 is 01:59 EST, a minute before US clocks jump. UTC arithmetic keeps
    the track moving forward even though local wall time leaps an hour."""
    start = datetime(2018, 3, 11, 6, 59, tzinfo=UTC)
    hist = {1: [_obs(6, 0.8), _obs(30 * 90, 0.8), _obs(30 * 120, 0.8)]}
    (s,) = summarise(hist)
    t0, t1 = s.ts_start(start, 30.0), s.ts_end(start, 30.0)
    assert t0 - start == timedelta(seconds=0.2)
    assert t1 - start == timedelta(seconds=120)
    assert t0 < t1
    ny = ZoneInfo("America/New_York")
    assert t0.astimezone(ny).hour == 1 and t1.astimezone(ny).hour == 3   # wall clock jumped


def test_best_keyframe_prefers_a_whole_person_over_a_higher_score_at_the_edge():
    """A half-out-of-shot person is poor evidence however confident the detector was."""
    edge = _obs(1, 0.95, box=(0.0, 0.2, 0.2, 0.6))
    whole = _obs(2, 0.6, box=(0.3, 0.2, 0.5, 0.6))
    assert best_keyframe([edge, whole]) is whole


def test_bboxes_rounded_to_four_places_and_clamped():
    """Stored boxes are compared and drawn; float noise and filter overshoot must not leak."""
    hist = {1: [_obs(0, 0.8, box=(-0.01, 0.123456, 0.456789, 1.02)),
                _obs(1, 0.8), _obs(2, 0.8)]}
    (s,) = summarise(hist)
    assert s.bbox_first == [0.0, 0.1235, 0.4568, 1.0]
    for box in (s.bbox_first, s.bbox_last, s.best_bbox):
        assert all(0.0 <= v <= 1.0 and round(v, 4) == v for v in box)

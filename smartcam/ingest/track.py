"""Multi-object tracking: turning per-frame person boxes into tracks, one database row each.

The association scheme is ByteTrack's (Zhang et al., "ByteTrack: Multi-Object Tracking by
Associating Every Detection Box", arXiv:2110.06864; reference implementation
github.com/ifzhang/ByteTrack, MIT licence, notice in THIRD_PARTY_NOTICES.md). Its one idea that
matters here: a low-confidence box is not thrown away, it is used to *continue* a track that
already exists — a person half-hidden by a railing. Low-confidence boxes never *start* a track,
so they cannot conjure people.

This is an independent implementation of that published algorithm. No code was taken from
ByteTrack, and in particular none from its kalman_filter.py, which is widely reported to descend
from GPL-3.0 nwojke/deep_sort. The motion model here deliberately differs from that lineage: the
state is centre, width and height (not aspect ratio), and the noise constants were chosen by
tuning against MEVA ground truth (docs/research/detector-tracker-benchmark.md), not carried over.

Why tracking quality is a counting problem: the product answers "how many people" by counting
rows in `tracks`. One person split into three tracks is three people. The benchmark found tracking
collapses below about 5 fps for people walking indoors, so the sampling rate is a correctness
parameter rather than a cost dial.

**Only association-backed observations are reported.** Between detections the filter predicts
where a person probably is; predictions are never emitted. A track's start, end and evidence
frame are all moments at which the detector actually saw someone.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
from scipy.optimize import linear_sum_assignment

from smartcam.ingest.detect import Detection

Box = tuple[float, float, float, float]


@dataclass(frozen=True)
class TrackerParams:
    """Defaults chosen by grid search against MEVA KPF ground truth on admin.G329 (37 annotated
    people, D-FINE-S detections, 5 fps): 33/37 found, 28 tracks, 1.21 track ids per person. The
    noise constants were insensitive across a 4x3 grid. Those numbers are in-sample — the same clip
    was used to choose them — so the per-clip scores from the real import are the honest check.

    With `high_thresh` equal to the detector floor the weak-box second stage receives nothing.
    Lowering the floor to 0.25 or 0.3 to feed it did not improve any metric on that clip, so the
    stage is kept for fidelity to the algorithm, not because it was shown to help here.
    """

    high_thresh: float = 0.4        # boxes at or above this take part in the first association
    new_track_thresh: float = 0.5   # and must reach this to start a track
    first_iou: float = 0.2          # confident boxes vs tracked and recently lost tracks
    second_iou: float = 0.5         # weak boxes may only continue a currently tracked track
    unconfirmed_iou: float = 0.3
    lost_track_s: float = 2.0       # how long a track may go unseen before it is closed
    process_noise: float = 0.08     # relative to box size, per sampled frame
    measurement_noise: float = 0.04
    min_hits: int = 3               # observations a track needs before it is written


@dataclass(frozen=True)
class TrackObs:
    local_id: int
    n: int              # source frame index
    box: Box            # normalised x1, y1, x2, y2
    score: float


class BoxFilter:
    """Constant-velocity Kalman filter on [cx, cy, w, h] and their rates, in pixels.

    Noise scales with the box's own size, so a distant person and one filling the frame carry the
    same relative uncertainty. Process noise is generous because samples are 200 ms apart and
    people change pace between them.
    """

    def __init__(self, process_noise: float, measurement_noise: float):
        self.F = np.eye(8)
        self.F[:4, 4:] = np.eye(4)
        self.H = np.eye(4, 8)
        self.q = process_noise
        self.r = measurement_noise

    def initiate(self, z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        size = np.array([z[2], z[3], z[2], z[3]])
        cov = np.diag(np.concatenate([(2 * self.r * size) ** 2, (4 * self.q * size) ** 2]))
        return np.concatenate([z, np.zeros(4)]), cov

    def predict(self, mean: np.ndarray, cov: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        size = np.maximum(np.array([mean[2], mean[3], mean[2], mean[3]]), 1.0)
        q = np.diag(np.concatenate([(self.q * size) ** 2, (self.q * size) ** 2]))
        return self.F @ mean, self.F @ cov @ self.F.T + q

    def update(self, mean: np.ndarray, cov: np.ndarray,
               z: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        size = np.maximum(np.array([mean[2], mean[3], mean[2], mean[3]]), 1.0)
        s = self.H @ cov @ self.H.T + np.diag((self.r * size) ** 2)
        gain = np.linalg.solve(s, self.H @ cov).T
        return mean + gain @ (z - self.H @ mean), (np.eye(8) - gain @ self.H) @ cov


def iou_matrix(a: list[Box], b: list[Box]) -> np.ndarray:
    if not a or not b:
        return np.zeros((len(a), len(b)))
    A, B = np.asarray(a, float), np.asarray(b, float)
    lo = np.maximum(A[:, None, :2], B[None, :, :2])
    hi = np.minimum(A[:, None, 2:], B[None, :, 2:])
    inter = np.clip(hi - lo, 0, None).prod(axis=2)
    area_a = (A[:, 2] - A[:, 0]) * (A[:, 3] - A[:, 1])
    area_b = (B[:, 2] - B[:, 0]) * (B[:, 3] - B[:, 1])
    return inter / (area_a[:, None] + area_b[None, :] - inter + 1e-12)


@dataclass(eq=False)
class _Track:
    mean: np.ndarray
    cov: np.ndarray
    last_n: int
    confirmed: bool = False
    lost: bool = False
    local_id: int | None = None
    pending: list[TrackObs] = field(default_factory=list)
    predicted: Box = (0.0, 0.0, 0.0, 0.0)


def _associate(tracks: list[_Track], dets: list[Detection],
               min_iou: float) -> tuple[list[tuple[int, int]], list[int], list[int]]:
    """Hungarian assignment on 1 - IoU, keeping only pairs that clear `min_iou`."""
    if not tracks or not dets:
        return [], list(range(len(tracks))), list(range(len(dets)))
    m = iou_matrix([t.predicted for t in tracks], [d.box for d in dets])
    rows, cols = linear_sum_assignment(1.0 - m)
    pairs = [(int(r), int(c)) for r, c in zip(rows, cols, strict=True) if m[r, c] >= min_iou]
    used_t, used_d = {r for r, _ in pairs}, {c for _, c in pairs}
    return (pairs, [i for i in range(len(tracks)) if i not in used_t],
            [j for j in range(len(dets)) if j not in used_d])


class ByteTracker:
    def __init__(self, frame_rate: float, params: TrackerParams | None = None, *,
                 width: int, height: int):
        """`frame_rate` is the SAMPLED rate (e.g. 5), `width`/`height` the source resolution."""
        self.params = params or TrackerParams()
        self.frame_rate = frame_rate
        self.w, self.h = width, height
        self.kf = BoxFilter(self.params.process_noise, self.params.measurement_noise)
        self.tracks: list[_Track] = []
        self.history: dict[int, list[TrackObs]] = {}
        self._next_id = 1
        self._frames_seen = 0

    # coordinates: tracker state is in source pixels so widths and heights are real
    def _z(self, box: Box) -> np.ndarray:
        x1, y1, x2, y2 = box[0] * self.w, box[1] * self.h, box[2] * self.w, box[3] * self.h
        return np.array([(x1 + x2) / 2, (y1 + y2) / 2, x2 - x1, y2 - y1])

    def _box(self, mean: np.ndarray) -> Box:
        cx, cy, w, h = mean[:4]
        return (float((cx - w / 2) / self.w), float((cy - h / 2) / self.h),
                float((cx + w / 2) / self.w), float((cy + h / 2) / self.h))

    def update(self, dets: list[Detection], *, n: int, step: int = 1) -> list[TrackObs]:
        """Advance one sampled frame. `n` is the source frame index, `step` the sampling stride.

        Returns the observations confirmed tracks made on this frame.
        """
        p = self.params
        first_frame = self._frames_seen == 0
        self._frames_seen += 1

        for t in self.tracks:
            t.mean, t.cov = self.kf.predict(t.mean, t.cov)
            t.predicted = self._box(t.mean)

        high = [d for d in dets if d.score >= p.high_thresh]
        low = [d for d in dets if d.score < p.high_thresh]
        confirmed = [t for t in self.tracks if t.confirmed]
        unconfirmed = [t for t in self.tracks if not t.confirmed]
        out: list[TrackObs] = []

        # 1. confident boxes against every confirmed track, including recently lost ones
        pairs, rest_t, rest_d = _associate(confirmed, high, p.first_iou)
        for ti, di in pairs:
            out.append(self._observe(confirmed[ti], high[di], n))
        leftover_high = [high[j] for j in rest_d]

        # 2. weak boxes may only continue a track that was being followed until now
        still_tracked = [confirmed[i] for i in rest_t if not confirmed[i].lost]
        pairs, unmatched, _ = _associate(still_tracked, low, p.second_iou)
        for ti, di in pairs:
            out.append(self._observe(still_tracked[ti], low[di], n))
        for i in unmatched:
            still_tracked[i].lost = True

        # 3. a tentative track gets exactly one chance to be seen again
        pairs, rest_u, rest_d = _associate(unconfirmed, leftover_high, p.unconfirmed_iou)
        for ti, di in pairs:
            self._confirm(unconfirmed[ti])
            out.append(self._observe(unconfirmed[ti], leftover_high[di], n))
        dead = {id(unconfirmed[i]) for i in rest_u}
        leftover_high = [leftover_high[j] for j in rest_d]

        # 4. new tracks only from confident boxes nothing else explains
        for d in leftover_high:
            if d.score < p.new_track_thresh:
                continue
            mean, cov = self.kf.initiate(self._z(d.box))
            t = _Track(mean, cov, last_n=n, pending=[TrackObs(-1, n, d.box, d.score)])
            if first_frame:
                self._confirm(t)
                out.append(self.history[t.local_id][-1])  # type: ignore[index]
            self.tracks.append(t)

        max_gap = max(1, round(p.lost_track_s * self.frame_rate)) * step
        self.tracks = [t for t in self.tracks
                       if id(t) not in dead and not (t.lost and n - t.last_n > max_gap)]
        return out

    def _observe(self, t: _Track, d: Detection, n: int) -> TrackObs:
        t.mean, t.cov = self.kf.update(t.mean, t.cov, self._z(d.box))
        t.last_n, t.lost = n, False
        assert t.local_id is not None
        obs = TrackObs(t.local_id, n, d.box, d.score)
        self.history[t.local_id].append(obs)
        return obs

    def _confirm(self, t: _Track) -> None:
        """Give a track its permanent id and keep the observation that started it."""
        t.confirmed = True
        t.local_id = self._next_id
        self._next_id += 1
        self.history[t.local_id] = [TrackObs(t.local_id, o.n, o.box, o.score) for o in t.pending]
        t.pending = []

    def tracks_history(self) -> dict[int, list[TrackObs]]:
        return {k: list(v) for k, v in self.history.items()}

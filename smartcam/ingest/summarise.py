"""From a track's observations to the single row the database keeps for it.

Every field here is something a question will be answered from, so each is derived from what the
detector actually saw rather than from the tracker's predictions:

- **Times come from frame indices.** `ts_start = clip start + n_first / source fps`. Arithmetic is
  done on UTC instants, so a clip that straddles a daylight-saving change cannot produce a track
  that ends before it starts.
- **Confidence is summarised, not chosen.** `conf_max` alone would let one lucky frame make a
  track look certain; `conf_mean` is kept beside it so the query layer can tell a person seen
  clearly for ten seconds from a shadow that scored 0.6 once.
- **The evidence frame avoids cut-off people.** A box touching the frame edge is probably a person
  half out of shot, which is poor evidence however confident the detector was.
- **No attributes.** Nothing in this pipeline measures helmets, vests or clothing colour, so no key
  is written — an absent key is how the answer layer knows to say "not measured" instead of zero.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta

from smartcam.ingest.track import Box, TrackObs

EDGE_MARGIN = 0.005


@dataclass(frozen=True)
class TrackSummary:
    local_id: int
    cls: str
    n_first: int
    n_last: int
    n_frames: int
    conf_max: float
    conf_mean: float
    bbox_first: list[float]
    bbox_last: list[float]
    best_n: int
    best_bbox: list[float]
    best_score: float

    def ts_start(self, clip_start: datetime, src_fps: float) -> datetime:
        return clip_start + timedelta(seconds=self.n_first / src_fps)

    def ts_end(self, clip_start: datetime, src_fps: float) -> datetime:
        return clip_start + timedelta(seconds=self.n_last / src_fps)

    def keyframe_ts(self, clip_start: datetime, src_fps: float) -> datetime:
        return clip_start + timedelta(seconds=self.best_n / src_fps)


def _round(box: Box) -> list[float]:
    return [round(min(1.0, max(0.0, v)), 4) for v in box]


def touches_edge(box: Box) -> bool:
    x1, y1, x2, y2 = box
    return x1 <= EDGE_MARGIN or y1 <= EDGE_MARGIN or x2 >= 1 - EDGE_MARGIN or y2 >= 1 - EDGE_MARGIN


def best_keyframe(obs: list[TrackObs]) -> TrackObs:
    """Whole person first, then detector confidence, then size; earliest frame breaks ties."""
    def area(o: TrackObs) -> float:
        return (o.box[2] - o.box[0]) * (o.box[3] - o.box[1])
    return min(obs, key=lambda o: (touches_edge(o.box), -o.score, -area(o), o.n))


def summarise(history: dict[int, list[TrackObs]], *, min_hits: int = 3,
              cls: str = "person") -> list[TrackSummary]:
    """One summary per track that was seen at least `min_hits` times, in order of first sighting.

    Short tracks are dropped because at 5 fps two sightings is 400 ms: long enough for a
    reflection or a detector flicker, too short to count as a person having been there.
    """
    out: list[TrackSummary] = []
    for local_id, raw in history.items():
        obs = sorted(raw, key=lambda o: o.n)
        if len(obs) < min_hits:
            continue
        scores = [o.score for o in obs]
        best = best_keyframe(obs)
        out.append(TrackSummary(
            local_id=local_id, cls=cls,
            n_first=obs[0].n, n_last=obs[-1].n, n_frames=len(obs),
            conf_max=round(max(scores), 4), conf_mean=round(sum(scores) / len(scores), 4),
            bbox_first=_round(obs[0].box), bbox_last=_round(obs[-1].box),
            best_n=best.n, best_bbox=_round(best.box), best_score=round(best.score, 4),
        ))
    out.sort(key=lambda s: (s.n_first, s.local_id))
    return out


def iter_obs(history: dict[int, list[TrackObs]]) -> Iterable[TrackObs]:
    for obs in history.values():
        yield from obs

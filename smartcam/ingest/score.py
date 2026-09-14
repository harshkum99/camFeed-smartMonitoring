"""Scoring an import against MEVA's hand-made ground truth, and turning the score into a claim.

Accuracy numbers in a sales deck are the ones a technical evaluator tries to break first, so the
only numbers this product states about itself are ones produced here: our tracks, frame by frame,
against the KPF annotations Kitware published for the same clips.

Two outputs:

- **A table**, per clip — annotated people, tracks we wrote, how many annotated people we found,
  how many track ids each found person was split across, and per-box detection recall. Printed and
  saved as JSON, so a number quoted later can be traced to the run that produced it.
- **A capability** per camera, per class, written to `cameras.capabilities`. This is what lets the
  answer layer say "Bus G340 cannot reliably detect people" instead of reporting its near-empty
  results as a real negative.

Thresholds are judgement calls, stated like the survey grade thresholds so they can be argued with:
at 0.60 per-box recall a person walking through view for a few seconds is very likely detected on
several frames; below 0.30 they are more likely missed entirely than found.
"""

from __future__ import annotations

import collections
import gzip
import json
import re
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
from scipy.optimize import linear_sum_assignment

from smartcam.ingest.track import iou_matrix

RELIABLE_RECALL = 0.60
LIMITED_RECALL = 0.30
MIN_GT_BOXES = 100
MATCH_IOU = 0.3

_GEOM = re.compile(
    r"""['"]id1['"]:\s*(\d+).*?['"]ts0['"]:\s*(\d+)|['"]ts0['"]:\s*(\d+).*?['"]id1['"]:\s*(\d+)""")
_G0 = re.compile(r"""['"]g0['"]:\s*['"]\s*([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s*['"]""")
_TYPE = re.compile(r"""['"]cset3['"]:\s*\{([^}]*)\}.*?['"]id1['"]:\s*(\d+)""")


@dataclass(frozen=True)
class GtBox:
    track: int
    n: int
    box: tuple[float, float, float, float]


def load_kpf(geom: Path, types: Path, *, width: int, height: int,
             cls: str = "person") -> list[GtBox]:
    """Parse KPF geom/types YAML line by line. No YAML library: the files are one flow mapping per
    line, and the geom files run to several megabytes."""
    classes: dict[int, str] = {}
    for line in types.read_text().splitlines():
        m = _TYPE.search(line)
        if not m:
            continue
        scores = {k.strip(" '\""): float(v) for k, v in
                  (kv.split(":") for kv in m.group(1).split(",") if ":" in kv)}
        if scores:
            classes[int(m.group(2))] = max(scores, key=scores.get)
    out: list[GtBox] = []
    for line in geom.read_text().splitlines():
        g0, ids = _G0.search(line), _GEOM.search(line)
        if not g0 or not ids:
            continue
        tid = int(ids.group(1) or ids.group(4))
        n = int(ids.group(2) or ids.group(3))
        if classes.get(tid) != cls:
            continue
        x1, y1, x2, y2 = (float(v) for v in g0.groups())
        out.append(GtBox(tid, n, (x1 / width, y1 / height, x2 / width, y2 / height)))
    return out


def annotation_files(annotations: Path, video_name: str) -> tuple[Path, Path] | None:
    """KPF files share the video's stem up to the camera id: `<stem>.geom.yml`."""
    stem = video_name.split(".r13.")[0] if ".r13." in video_name else Path(video_name).stem
    geoms = list(annotations.rglob(f"{stem}.geom.yml"))
    if not geoms:
        return None
    types = geoms[0].with_name(f"{stem}.types.yml")
    return (geoms[0], types) if types.exists() else None


@dataclass
class ClipScore:
    file: str
    camera_key: str
    gt_people: int
    gt_boxes: int
    tracks_written: int
    people_found: int
    ids_per_person: float | None
    people_per_id: float | None
    box_recall: float | None
    span_frames: int


def score_log(log: dict, gt: list[GtBox]) -> ClipScore:
    step = int(log["step"])
    sampled = {int(fr["n"]) for fr in log["frames"]}
    lo, hi = (min(sampled), max(sampled)) if sampled else (0, -1)
    by_n: dict[int, list[GtBox]] = collections.defaultdict(list)
    for g in gt:
        if g.n in sampled and g.n % step == (lo % step):
            by_n[g.n].append(g)

    # per-box detection recall, against the detector's raw output
    dets = {int(fr["n"]): [tuple(d[:4]) for d in fr["dets"]] for fr in log["frames"]}
    hit = total = 0
    for n, gts in by_n.items():
        total += len(gts)
        cand = dets.get(n, [])
        if not cand:
            continue
        m = iou_matrix([g.box for g in gts], cand)
        r, c = linear_sum_assignment(-m)
        hit += int(sum(m[i, j] >= MATCH_IOU for i, j in zip(r, c, strict=True)))

    # track identity against the tracks actually written
    written = {str(i) for i in log["written"]}
    obs_by_n: dict[int, list[tuple[str, tuple]]] = collections.defaultdict(list)
    for tid, obs in log["tracks"].items():
        if tid in written:
            for o in obs:
                obs_by_n[int(o[0])].append((tid, tuple(o[1:5])))
    pairs: collections.Counter = collections.Counter()
    for n, gts in by_n.items():
        cand = obs_by_n.get(n, [])
        if not cand:
            continue
        m = iou_matrix([g.box for g in gts], [c[1] for c in cand])
        r, c = linear_sum_assignment(-m)
        for i, j in zip(r, c, strict=True):
            if m[i, j] >= MATCH_IOU:
                pairs[(gts[i].track, cand[j][0])] += 1
    ids_of: dict[int, set] = collections.defaultdict(set)
    people_of: dict[str, set] = collections.defaultdict(set)
    for (g, p), k in pairs.items():
        if k >= 2:
            ids_of[g].add(p)
            people_of[p].add(g)

    return ClipScore(
        file=log["file"], camera_key=log["camera_key"],
        gt_people=len({g.track for gts in by_n.values() for g in gts}), gt_boxes=total,
        tracks_written=len(written), people_found=len(ids_of),
        ids_per_person=_mean_size(ids_of),
        people_per_id=_mean_size(people_of),
        box_recall=round(hit / total, 3) if total else None,
        span_frames=hi - lo + 1 if sampled else 0,
    )


def _mean_size(groups: dict) -> float | None:
    return round(float(np.mean([len(v) for v in groups.values()])), 2) if groups else None


def capability(scores: list[ClipScore], *, detector_id: str, basis: str) -> dict:
    boxes = sum(s.gt_boxes for s in scores)
    hits = sum((s.box_recall or 0.0) * s.gt_boxes for s in scores)
    recall = hits / boxes if boxes else None
    if boxes < MIN_GT_BOXES or recall is None:
        status = "not_assessed"
    elif recall >= RELIABLE_RECALL:
        status = "reliable"
    elif recall >= LIMITED_RECALL:
        status = "limited"
    else:
        status = "unreliable"
    return {"status": status, "frame_recall": round(recall, 3) if recall is not None else None,
            "gt_boxes": boxes, "basis": basis, "detector": detector_id,
            "assessed_at": datetime.now(UTC).isoformat(timespec="seconds")}


def read_log(path: Path) -> dict:
    with gzip.open(path, "rt") as f:
        return json.load(f)


def as_dicts(scores: list[ClipScore]) -> list[dict]:
    return [asdict(s) for s in scores]

"""Person detection with D-FINE-S, run through onnxruntime on CPU.

The choice and its contract were measured, not read off a model card — see
docs/research/detector-tracker-benchmark.md. The short version: 0.70 person recall at IoU 0.5 on
an indoor MEVA camera at 142 ms a frame, Apache-2.0 for code and weights at every size.

**Only the class we benchmarked is written.** COCO has cars, buses and trucks, and D-FINE will
happily report them, but we have measured person recall and nothing else. Writing vehicle tracks
now would let a vehicle question come back with a number whose error we have never looked at;
leaving the class out makes the same question come back "not measured", which is true.

**The weights are checked before they are loaded.** `load_onnx` hashes the file and refuses it
unless the hash matches the entry in `third_party.toml`. The licence gate in CI reads the same
manifest, so replacing the checkpoint — with a quantised variant, or with something whose licence
nobody checked — is a reviewed manifest change rather than a file copied into a directory.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import numpy as np

from smartcam.evidence.store import sha256_file

INPUT_SIZE = (640, 640)

#: COCO-80 indices as emitted by the D-FINE export (person is 0, not 1 as in 91-id COCO).
COCO_TO_CLASS = {0: "person", 1: "vehicle", 2: "vehicle", 3: "vehicle", 5: "vehicle", 7: "vehicle"}
WRITTEN_CLASSES = frozenset({"person"})


class ModelError(RuntimeError):
    pass


@dataclass(frozen=True)
class Detection:
    box: tuple[float, float, float, float]   # normalised x1, y1, x2, y2 in [0, 1]
    score: float
    cls: str


class OnnxSession(Protocol):
    def run(self, output_names: list[str] | None,
            input_feed: dict[str, np.ndarray]) -> list[np.ndarray]: ...


@dataclass(frozen=True)
class ModelIdentity:
    name: str
    sha256: str
    input: str = "640x640-stretch"
    score_floor: float = 0.4

    @property
    def id(self) -> str:
        return f"{self.name}@{self.sha256[:12]}/{self.input}/thr{self.score_floor}"


def preprocess(rgb: np.ndarray) -> np.ndarray:
    """uint8 H×W×3 RGB already at the model's input size -> float32 [1, 3, H, W] in [0, 1].

    No mean/std normalisation: the export's preprocessor config says so, and it is the input the
    benchmark recall was measured with. Getting it wrong raises no error — it only lowers recall.
    """
    if rgb.shape[:2] != (INPUT_SIZE[1], INPUT_SIZE[0]):
        raise ValueError(f"expected a {INPUT_SIZE[0]}x{INPUT_SIZE[1]} frame, got {rgb.shape}")
    return (rgb.astype(np.float32) / 255.0).transpose(2, 0, 1)[None]


def postprocess(logits: np.ndarray, pred_boxes: np.ndarray, *, floor: float,
                classes: frozenset[str] = WRITTEN_CLASSES) -> list[Detection]:
    """DETR-style outputs to detections. One query is one object, so there is no NMS to do."""
    ids = [i for i, c in COCO_TO_CLASS.items() if c in classes]
    if not ids:
        return []
    probs = 1.0 / (1.0 + np.exp(-logits[0][:, ids]))
    best = probs.argmax(axis=1)
    scores = probs[np.arange(len(probs)), best]
    out: list[Detection] = []
    for q in np.flatnonzero(scores >= floor):
        cx, cy, w, h = (float(v) for v in pred_boxes[0][q])
        box = (
            min(1.0, max(0.0, cx - w / 2)), min(1.0, max(0.0, cy - h / 2)),
            min(1.0, max(0.0, cx + w / 2)), min(1.0, max(0.0, cy + h / 2)),
        )
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        out.append(Detection(box, float(scores[q]), COCO_TO_CLASS[ids[best[q]]]))
    out.sort(key=lambda d: (-d.score, d.box))
    return out


class Detector:
    def __init__(self, session: OnnxSession, identity: ModelIdentity):
        self.session = session
        self.identity = identity

    def detect(self, rgb: np.ndarray) -> list[Detection]:
        feed = {"pixel_values": preprocess(rgb)}
        logits, boxes = self.session.run(["logits", "pred_boxes"], feed)
        return postprocess(logits, boxes, floor=self.identity.score_floor)


def manifest_entry(manifest: Path, file_name: str) -> dict:
    data = tomllib.loads(manifest.read_text())
    for entry in data.get("model", []):
        if entry.get("file") == file_name:
            return entry
    raise ModelError(f"{file_name} is not declared in {manifest.name}; add it there, with its "
                     "licence evidence, before it can be loaded")


def load_onnx(path: Path, *, manifest: Path, file_name: str, threads: int | None = None,
              score_floor: float = 0.4) -> Detector:
    entry = manifest_entry(manifest, file_name)
    if not path.is_file():
        raise ModelError(f"model file not found: {path}")
    actual = sha256_file(path)
    if actual != entry["sha256"]:
        raise ModelError(
            f"{path} does not match the manifest (sha256 {actual[:12]}…, expected "
            f"{entry['sha256'][:12]}…). Refusing to load an undeclared checkpoint."
        )
    import onnxruntime as ort  # heavy, and only needed once the file has been verified

    opts = ort.SessionOptions()
    if threads:
        opts.intra_op_num_threads = threads
    session = ort.InferenceSession(str(path), sess_options=opts,
                                   providers=["CPUExecutionProvider"])
    names_in = {i.name for i in session.get_inputs()}
    names_out = {o.name for o in session.get_outputs()}
    if "pixel_values" not in names_in or not {"logits", "pred_boxes"} <= names_out:
        raise ModelError(f"{path.name} has unexpected inputs/outputs {names_in} -> {names_out}")
    return Detector(session, ModelIdentity(entry["name"], actual, score_floor=score_floor))

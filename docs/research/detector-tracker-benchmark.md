# Detector and tracker, measured on MEVA with ground truth

Measured 2026-09-13 on the development laptop (Apple M2, 8 GB, CPU only, onnxruntime 1.30, Python
3.14). Every number below was produced by running the model against real MEVA frames and scoring
it against MEVA's own KPF annotations — none is copied from a model card. Scripts and raw outputs
lived in the session scratchpad; the method is described well enough to repeat.

## Why this was measured rather than looked up

Model cards report COCO AP at a single input size on a GPU. Our deployment is the opposite on every
axis: CPU, surveillance geometry, people who are sometimes 40 pixels tall, and a sampling rate we
choose to fit a budget. The two questions that decide the architecture — *how small a person can we
see* and *how few frames per second can tracking survive* — are not on any model card.

## Detectors

Person recall at IoU ≥ 0.3, 40 frames sampled evenly across the annotated span of each clip.
"ms" is median wall time per frame on CPU, including preprocessing.

### Near field — `admin.G329`, indoor, people 101–708 px tall (like an indoor factory camera)

| Model | Input | Recall @ 0.3 | Recall @ 0.5 | ms/frame | Code / weights licence |
|---|---|---|---|---|---|
| **D-FINE-S** | 640×640 | 0.76 | **0.70** | **142** | Apache-2.0 / Apache-2.0 |
| RF-DETR-N | 384×384 | 0.74 | 0.69 | 138 | Apache-2.0 / Apache-2.0 |
| RF-DETR-S | 512×512 | 0.77 | 0.73 | 246 | Apache-2.0 / Apache-2.0 |
| RF-DETR-N, 2×2 tiles | 384×384 ×4 | 0.83 | 0.81 | 540 | |
| D-FINE-S | 1280×736 | 0.81 | 0.74 | 300 | |
| D-FINE-N | 640×640 | 0.38 | 0.08 | 41 | Apache-2.0 / Apache-2.0 |

### Far field — `bus.G340`, outdoor car park, people 34–52 px tall at 1920×1080

| Model | Input | Recall @ 0.3 | Recall @ 0.5 | ms/frame |
|---|---|---|---|---|
| D-FINE-S | 640×640 | 0.05 | 0.02 | 131 |
| D-FINE-S | 1280×736 | **0.40** | 0.22 | 288 |
| RF-DETR-N, 2×2 tiles | 384×384 ×4 | 0.40 | 0.17 | 568 |
| RF-DETR-S | 512×512 | 0.16 | 0.04 | 256 |

The ground-truth boxes were checked by drawing them on the frame: they are correct. Two people
really do occupy about 40 pixels in the far corner of a car park. **No configuration we can afford on
CPU sees them reliably**, and that is a property of the camera placement, not of the model.

### Decision: D-FINE-S at 640×640

It ties RF-DETR-N on near-field recall at the same cost, the ONNX file is 41 MB against 108 MB,
it accepts non-square input so a far-field camera can be given a larger input without a different
model, and its weights are Apache-2.0 at every size (RF-DETR's XL/2XL checkpoints are PML 1.0,
which is one careless upgrade away from a licence problem).

ONNX contract, verified by running it: input `pixel_values` float32 `[B,3,H,W]`, RGB, resized by
stretching, divided by 255, **no** mean/std normalisation. Outputs `logits [B,300,80]` (apply
sigmoid; COCO order, person = 0, car = 2, motorcycle = 3, bus = 5, truck = 7) and
`pred_boxes [B,300,4]` as normalised centre-x, centre-y, width, height.

### What this means for the product

The survey already grades cameras by what they can support. These numbers say the grade has to
include **distance to subject**, not only resolution: a 1080p camera watching a car park from 60 m
is not a person-counting camera, and a count from it must be reported with low confidence and a
reason rather than as a number. This is the same honesty rule as "not measured" — it just applies
to geometry instead of to a missing detector.

MEVA's 352×240 cameras (G474–G479) are **thermal IR**; the dataset metadata marks them so. One
person was detected across twelve frames. Night-shift factory footage from IR cameras will need its
own measurement before any claim is made about it.

## Trackers

D-FINE-S detections (score > 0.4) over the annotated 126 seconds of `admin.G329`, which contains
37 annotated person tracks. Implementations from the `trackers` 2.6.0 package (Apache-2.0).
Sampling was done by taking every Nth decoded frame from the same detection run, so the detector
output is identical across rows and only the tracker's input rate changes.

| Sampling | Tracker | GT tracks found | Predicted tracks (≥3 hits) | IDs per GT track |
|---|---|---|---|---|
| 5 fps | SORT | 33 / 37 | 31 | 1.27 |
| 5 fps | **ByteTrack** | **33 / 37** | 28 | **1.24** |
| 5 fps | OC-SORT | 31 / 37 | 23 | 1.26 |
| 2.5 fps | SORT | 28 / 37 | 18 | 1.21 |
| 2.5 fps | ByteTrack | 29 / 37 | 16 | 1.17 |
| 2.5 fps | OC-SORT | 23 / 37 | 18 | 1.35 |
| 1.67 fps | ByteTrack | 18 / 37 | 14 | 1.28 |
| 1 fps | ByteTrack | 12 / 37 | 14 | 1.42 |

**Tracking collapses below about 5 fps for people walking through an indoor space.** At one frame a
second a person crossing a stairwell is seen twice, with no overlap between the two boxes, and every
motion-only tracker loses them. Sampling rate is therefore a correctness parameter, not a cost dial:
the product counts *tracks*, so a rate that fragments tracks inflates or deflates every count.

One number is unresolved: each predicted ID matched about 1.6 ground-truth IDs at 5 fps. That is
either our tracker merging people who pass close together, or MEVA assigning a person a new track
id per annotated activity. We have not yet separated the two, and should not quote a counting
accuracy until we have.

### Decision: ByteTrack at 5 fps, implemented from scratch

ByteTrack is best or tied at every rate. The `trackers` package that implements it pulls in
supervision, full opencv-python, matplotlib, PyAV, requests and rich — far too much for an edge
appliance — and ByteTrack's own reference code carries a Kalman filter widely reported to descend
from GPL-3.0 deep_sort. So `smartcam/ingest/track.py` is an independent implementation of the
published association scheme, with its own motion model (centre, width and height rather than
aspect ratio).

Its parameters were chosen by grid search against the same ground truth, on the same detections:

| Configuration | GT tracks found | Predicted tracks | IDs per GT track |
|---|---|---|---|
| `trackers` ByteTrack (reference, table above) | 33 / 37 | 28 | 1.24 |
| Ours, first defaults (new track ≥ 0.6, confident box ≥ 0.5) | 31 / 37 | 22 | 1.19 |
| **Ours, tuned (new track ≥ 0.5, confident box ≥ 0.4)** | **33 / 37** | **28** | **1.21** |

The Kalman noise constants made no measurable difference across a 4×3 grid, so they are set to
round values rather than tuned. Lowering the detector floor to 0.25 or 0.3 to feed ByteTrack's
weak-box second stage did not improve any metric on this clip either. All of these numbers are
in-sample: the parameters were chosen on the clip they are scored on. The per-clip scores from the
full import, on clips that played no part in tuning, are the honest check.

## Cost

At 142 ms per frame, 5 fps costs about 0.71 CPU-seconds per second of footage: a five-minute clip
takes about three and a half minutes unaccelerated. In the annotated `admin.G329` clip only 1,391 of
9,000 frames contain a person, so a motion gate that reuses the previous frame's detections when
nothing has changed should remove most of that cost without starving the tracker. Real-time
ingest at site scale remains cloud GPU work; this is sized for importing recorded footage.

## Decoding

ffmpeg with `select` + `scale=640:640` piped as `rawvideo rgb24` decoded and delivered 632 frames
at 5 fps from a 1080p clip, with the detector accounting for 90 of the 91 seconds — decode is not
the bottleneck. MEVA AVI files report `pts` as N/A after the first few frames and their first
timestamp is 2/30 s, so frame time must be derived from the frame index
(`clip_start + n / fps`), never from `pts`. KPF annotations index frames the same way.

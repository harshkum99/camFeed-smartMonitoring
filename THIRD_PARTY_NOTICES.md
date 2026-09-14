# Third-party notices

Artefacts shipped with, downloaded by, or demonstrated on Smart Cam Monitoring that are not Python
packages. The machine-readable list, which CI checks, is [third_party.toml](third_party.toml).

## dfine-s-coco — D-FINE-S object detector weights

- Source: `onnx-community/dfine_s_coco-ONNX` on Hugging Face, commit `a3cf031`, file
  `onnx/model.onnx`, sha256 `cd8a49a9…ef7238`.
- Derived from the D-FINE-S COCO checkpoint (`ustc-community/dfine-small-coco`) of D-FINE by
  Peng et al. (github.com/Peterande/D-FINE).
- Licence: Apache License 2.0. The ONNX conversion's card declares no licence of its own; we treat
  it as a format conversion of the Apache-2.0 weights.

## bytetrack — tracking algorithm

`smartcam/ingest/track.py` implements the association scheme described in *ByteTrack: Multi-Object
Tracking by Associating Every Detection Box* (Zhang et al., arXiv:2110.06864). No code was copied
from the reference implementation (github.com/ifzhang/ByteTrack, MIT) or from nwojke/deep_sort
(GPL-3.0). The motion model uses a different state representation and noise constants tuned on MEVA
ground truth; the tuning is recorded in `docs/research/detector-tracker-benchmark.md`.

## MEVA — demo footage and ground truth

"Multiview Extended Video with Activities" (MEVA) dataset by Kitware Inc. and the Intelligence
Advanced Research Projects Activity (IARPA), licensed under a Creative Commons Attribution 4.0
International License (https://creativecommons.org/licenses/by/4.0/). Source: https://mevadata.org.
Any demo or screenshot showing MEVA footage must carry this attribution; the console shows it in
the footer whenever a site with attributed data is selected.

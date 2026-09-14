# Smart Cam Monitoring — Decision Log

Running record of decisions taken, who took them, and why. Newest last.
Architecture decisions with real trade-offs get their own file in `docs/adr/`.

---

## D-001 — Product scope comes from the IM + pitch deck (2026-09-02)

Source documents: `Smart Cam Monitoring IM.pdf`, `Smart Cam Monitoring Presentation.pdf` (Karma AI Pvt Ltd).

Five capabilities the platform must deliver:

| # | Capability | One-line definition |
|---|---|---|
| 1 | Talk to CCTV | Plain-language Q&A across all cameras, answered with timestamps + evidence frames |
| 2 | Multi-camera unified query | One question, answered across every connected camera at once |
| 3 | Real-time incident alerts | Custom rules, alert delivered within ~2s of the event |
| 4 | Track & Trace | Upload one photo → full movement path of that person/object across the network |
| 5 | Compliance / SOP monitoring | Customer's checklist enforced continuously, violations alerted, daily reports |

Hard product constraints from the documents:

- **Software only.** No cameras sold, no camera replacement, no forced DVR/NVR swap.
- **Works with what exists** — IP cameras, analog-on-DVR, NVR, any brand, any age.
- **Edge agent** sits on the customer LAN and reaches every camera in the site.
- **Three deployment modes** — cloud, on-premise/air-gapped, hybrid. On-prem is non-negotiable
  for government/defence/banking buyers.
- **Frame-level storage** — keep analysed frames + metadata, let the customer delete raw video.

---

## D-002 — Marketing claims that need reframing before any technical buyer sees them (2026-09-02)

Decided by: Claude, flagged to Harsh, 2026-09-02.

Three claims in the IM are not technically defensible as written. Keeping them invites a credibility
collapse in front of a CTO, a police forensics officer, or a due-diligence technical reviewer.

### C-1 — "Super-pixelation makes faces/plates identifiable on low-res cameras"

**Problem.** Generative upscaling synthesises plausible pixels; it does not recover information absent
from the sensor data. An upscaled face is a *hallucinated* face. Presenting it as identification is
both technically wrong and, in an evidentiary context, dangerous.

**Reframe.** Enhancement is a *legibility aid for human review*, never an identification basis. Identity
claims must come from embeddings computed on the **original** pixels, always ranked with a confidence
score and always human-confirmed. Enhanced frames get watermarked as enhanced and are excluded from the
evidence bundle.

### C-2 — "80–95% video compression with no loss of analytical detail"

**Problem.** CCTV video is already H.264/H.265 — i.e. already compressed 100–200x from raw. A further
90% reduction that preserves faces, plates and fine motion does not exist.

**Reframe.** The 90% saving is real but comes from a different mechanism: **we do not store video at
all.** We store selected keyframes plus structured metadata. That is a stronger story anyway, because it
also removes the customer's storage bill and their retention liability. Sell the outcome, drop the
false mechanism.

### C-3 — "Query a million cameras in under 5 seconds"

**Problem.** True only if it means a database query. It is impossible if it means analysing video at
query time.

**Reframe.** Architecture must make this true *by construction*: all analysis happens at ingest, queries
only ever hit an indexed event store. This is a design requirement, recorded here so it is never
violated.

**Status:** raised with Harsh 2026-09-02. Engineering will build to the reframed versions.

---

## D-003 — PoC parameters, set by Harsh (2026-09-02)

| Decision | Choice | Consequence for engineering |
|---|---|---|
| **Footage source** | Live RTSP from a real site **and** exported DVR recordings | Ingest layer must handle both live streams and batch file import from day one. Backfill of historical footage is a first-class feature, not an afterthought. |
| **Cloud** | Indian provider (E2E / Yotta / Neysa class) | Data stays in India — supports DPDP posture and government/BFSI sales. No dependence on managed services that only exist on AWS/GCP; everything self-hostable. |
| **PoC vertical** | Factory / warehouse | Demo centres on PPE & helmet compliance, restricted-zone entry, loading-dock activity, shift attendance. Buyer persona = Safety Officer / Plant Head. |
| **Face recognition** | In scope for the PoC, full capability | Biometric processing from day one. Requires the DPDP safeguards below to be built in, not bolted on. |

### Face recognition — safeguards that ship with it

Harsh chose full face recognition in the PoC. Accepted. These controls are built alongside it because
they are cheap now and very expensive to retrofit:

- Per-tenant enable flag; **off by default** on a new tenant.
- Face embeddings stored encrypted at rest, separately from general event data, with their own
  retention clock and their own delete path.
- Consent/notice register — who was enrolled, on what basis, when, by which operator.
- Immutable audit log of every face search: who ran it, what image, what results, when.
- Configurable retention with hard auto-purge, defaulting to a short window.
- A single tenant-level kill switch that disables face processing and purges templates.

---

## D-004 — Local machine is a control plane, not a build target (2026-09-02)

Development laptop: Apple M2, 8 GB RAM, ~20 GB free disk, no Docker, Python 3.14 only (too new for the
PyTorch/vision ecosystem, which lags to 3.12/3.13).

Consequence: all GPU and heavy-ingest work runs on the cloud box. The repo is structured so services are
developed locally against small fixtures and deployed to the GPU host for anything real. A cloud
development server is therefore a **blocking dependency** for the PoC, not a nice-to-have.

---

## D-005 — Public footage stands in for the DVR, and it is MEVA (2026-09-08)

DVR credentials for a live site are not going to arrive in time to unblock ingest. Harsh offered to
supply pre-recorded footage instead and asked what public data would do the job. Full survey in
[DATASETS.md](DATASETS.md).

**Chosen: MEVA** (CC BY 4.0, AWS Open Data, unsigned access) as the primary source, with
**NVIDIA PhysicalAI-SmartSpaces** (CC BY 4.0, synthetic warehouses, 3D ground truth) for the
vertical and accuracy scoring, and the **Eskişehir workplace-safety set** (CC BY 4.0, real plant)
for PPE rules.

**Rejected: UCF-Crime / DCSASS**, the datasets you find first on Kaggle. They are research-use-only,
and we already run a licence gate in CI to keep non-commercial artefacts out of a product we intend
to sell; footage would be the same mistake with a worse blast radius, sitting underneath a demo
given to a paying customer. They are also the wrong shape — no camera identity, no wall-clock time,
no continuity.

Consequences for engineering:

- All three sources are CC BY 4.0. **Attribution is now a shipping requirement** for any demo built
  on them, in the same way the licence gate is for dependencies.
- MEVA's filenames carry camera and start/end time, so `smartcam-import` parses filenames as the
  primary source of truth and treats container metadata and mtime as degraded fallbacks that must be
  declared in the report.
- MEVA's collection blocks leave real multi-hour holes (verified: nothing between 12:00 and 16:50 on
  7 March 2018). Those exercise `coverage_gaps` against unfabricated data, which synthetic seeding
  never could.
- MEVA mixes 1920×1072 and 352×240 cameras, so camera grading is exercised for free — several
  cameras are genuinely below recognition grade.
- **Face recognition and ANPR stay blocked on real footage.** No public set gives both the pixels
  (≥64 px IPD, plate-grade optics) and a lawful basis to enrol. We do not fake these in a demo.

---

## D-006 — First real slice: recorded footage through the whole product (2026-09-14)

Six MEVA clips (three cameras × 11:55 and 13:50 on 11 March 2018) go through decode → D-FINE-S →
ByteTrack → tracks, keyframes, clips and uptime → the unchanged `/api/ask` and console. Measurements
behind every choice are in [research/detector-tracker-benchmark.md](research/detector-tracker-benchmark.md).

**Settled here, and why:**

| Decision | Choice | Reason |
|---|---|---|
| Detector | D-FINE-S, 640×640, ONNX on CPU | 0.70 person recall on near-field MEVA at 142 ms/frame; Apache-2.0 at every size. Pinned by sha256 in `third_party.toml`; the loader refuses any other file. |
| Tracker | ByteTrack association, implemented from scratch | Best or tied at every rate. The `trackers` package drags matplotlib, full OpenCV and PyAV onto an edge box; the reference repo's Kalman filter is reported to descend from GPL deep_sort. |
| Sampling | 5 fps, no motion gate yet | Tracking collapses below ~5 fps (33/37 annotated people at 5 fps, 12/37 at 1 fps). The gate saves CPU only; it was cut from this slice so a correctness change does not ship bundled with an optimisation. |
| Time | frame index, never pts | MEVA AVI pts are N/A after a few frames. `ts = clip start + n / fps`, and the KPF ground truth indexes the same way. |
| "Now" for a recording | `sites.as_of` = end of analysed footage | "Today" asked of a 2018 recording must mean that day, not the wall clock. |
| Timezone | per-site IANA zone, per-endpoint offsets | A fixed offset cannot represent DST; 11 March 2018 in New York is 23 hours long. |
| Blind cameras | `cameras.capabilities`, per class, from ground-truth scoring | A 1080p camera watching a car park from 60 m has 5% person recall. Uptime cannot say that; without it the camera's silence became a confident "no one was there". |
| Evidence hash | `frame_sha256` = hash of the derived JPEG; `clips.source_sha256` = hash of the recorder's file | Only the second is an evidentiary anchor. The BSA s.63 certificate must cite it. |
| Keyframe access | server-configured tenant/site only, bytes hashed then served from one read | The x-smartcam-* headers are not a security boundary; on this route they would have been an image exfiltration path. |

**What changed in the query layer's honesty rules:**

- An attribute nobody measured is refused as `not_measured` before the query runs, with no rows or
  evidence attached — including for `is_null` / `not_in` predicates, which used to return every
  unmeasured row as a match. Measured on some cameras only → answered for those, naming the rest.
- A zone question on cameras with no zones drawn is `not_measured`, not `no_evidence`.
- `no_evidence` can never be emitted while a camera in scope is unreliable, unassessed, or has no
  detector for the asked class. Their tracks never count as confident.
- Tracks are confident on mean detection score, not peak: every track that survives tracking has a
  peak above 0.5, which made the ambiguous bucket permanently empty.
- Gaps on recorded cameras read "has no footage for", not "was down for".

**Found along the way:** `.gitignore`'s `*.ts` (meant for MPEG transport streams) had silently kept
every TypeScript source under `console/src/lib` and `vite.config.ts` out of git, so a clean checkout
of `main` could not build the console. Fixed with a scoped negation.

**Open, and deliberately not decided by engineering:**

- MEVA frames show identifiable faces. Whether they are shown unblurred in customer demos is a
  product/legal call. Stored evidence stays original pixels either way.
- The tenant's `legal_basis` is recorded as `consent` (MEVA subjects were recruited participants).
  It is recorded, not relied on.
- **Known hazard, not fixed:** `drop_partitions_before` (003) drops whole months for every tenant.
  A future retention job built on it would delete the 2018-03 MEVA index. Retention must become
  per-tenant before any job calls it.

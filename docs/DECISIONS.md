# Project Sanjay — Decision Log

Running record of decisions taken, who took them, and why. Newest last.
Architecture decisions with real trade-offs get their own file in `docs/adr/`.

---

## D-001 — Product scope comes from the IM + pitch deck (2026-09-02)

Source documents: `Project Sanjay IM.pdf`, `Project Sanjay Presentation.pdf` (Karma AI Pvt Ltd).

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

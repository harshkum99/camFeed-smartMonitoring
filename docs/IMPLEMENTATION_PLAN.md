# Smart Cam Monitoring — PoC Implementation Plan

**Goal:** a proof of concept a factory Safety Officer or Plant Head will believe, running on their own
cameras, in four weeks.

**Scope:** one site, 6–12 cameras, factory/warehouse. All five capabilities demonstrated, each scoped to
what genuinely works, with the numbers measured rather than claimed.

Day counts assume continuous build. **A demoable slice exists at Day 6**; the full PoC at Day 28.
`[GPU]` marks work blocked on the cloud GPU node. `[SITE]` marks work blocked on real camera access.

---

## M0 · Foundations — Days 1–2 · *no cloud needed*

| # | Task | Output |
|---|---|---|
| 0.1 | Monorepo layout, Python 3.12 toolchain, ruff/pytest, CI skeleton | `smartcam/` packages build and test |
| 0.2 | **Licence CI gate** — fail the build on `ultralytics`, `insightface`, DEIMv2, any AGPL/GPL/non-commercial dependency | `scripts/license_gate.py` in CI |
| 0.3 | Postgres schema, migrations 001–00N. `tenant_id`/`site_id` non-null everywhere; `tracks` partitioned by `ts_start` | `db/migrations/` |
| 0.4 | Docker Compose: Postgres 16 + TimescaleDB + pgvector + MinIO + API. **Computed `shm_size`.** | `infra/compose.yaml` |
| 0.5 | Core domain models + repository layer, fully unit-tested against fixtures | `smartcam/core/` |
| 0.6 | Synthetic event generator — realistic tracks/zone_events/embeddings for a fictional plant | Query layer is developable with zero cameras |

**Why 0.6 matters:** it decouples every downstream milestone from site access and GPU availability. The
entire query engine, console and evidence layer can be built and demoed against synthetic data while the
DVR credentials conversation is still happening.

---

## M1 · Site survey + ingest — Days 3–6 · `[SITE]`

The unglamorous milestone that decides whether the PoC happens at all. Research is blunt: credentials,
not technology, are the number one deployment blocker, and 20–40% of Indian sites need a physical visit.

| # | Task | Output |
|---|---|---|
| 1.1 | **`smartcam-survey`** — standalone read-only binary. ONVIF WS-Discovery ∪ TCP sweep (554/80/8000/37777/34567) ∪ credentialed enumeration via Hikvision ISAPI and Dahua/CP-Plus CGI ∪ two-grammar URL fallback | Finds cameras on a real DVR |
| 1.2 | Per-channel **grading**: measured WxH, fps, codec, **measured GOP** (count frames between IDRs over 10 s), face-pixel density, lens type → `recognition` / `detection` / `anpr` / `degraded` / `unservable` | Signed PDF site-survey report |
| 1.3 | **Clock reconciliation** via PRE_AUTH `GetSystemDateAndTime` (needs no password). Report offset per device. Generate UsernameToken `Created` in *device* time | Auth works on drifted DVRs |
| 1.4 | Frigate 0.17.2 on the edge box, per-stream hwaccel preset selected from the probed codec, one go2rtc upstream session per channel | Live detection on real cameras |
| 1.5 | RTSP supervisor: exponential backoff **with jitter**, global in-flight semaphore, per-device session cap; every transition written to `camera_uptime` | Survives a switch reboot |
| 1.6 | Edge sync agent: durable spool (SQLite WAL + disk quota + oldest-first drop) → resumable HTTPS upload, token-bucket rate-limited to ~25% of measured uplink | Survives a 7-day outage |
| 1.7 | **Measure and publish glass-to-glass latency** on the customer's actual cameras. Millisecond clock on a monitor in frame, screenshot the alert, subtract | The single most valuable number in the project |

> **1.2 is secretly the product.** It turns the largest unmodelled commercial risk — "how many of your
> cameras can actually do this?" — into a pre-sales artefact we charge for, and it is the same audit the
> face/ANPR pixel-density grading needs. Build once, use for both.

**Day 6 checkpoint — first demoable slice:** real cameras → real detections → real events in Postgres,
queryable, with measured latency.

---

## M2 · Talk to CCTV — Days 7–10

| # | Task | Output |
|---|---|---|
| 2.1 | Intent router (4 classes: aggregate / lookup / open-vocab / trace). Small cheap classifier, not the big LLM | `smartcam/query/router.py` |
| 2.2 | **Filter compiler** — LLM emits constrained JSON against a ~20-column whitelist; we compile to parameterised SQL. LLM never writes SQL, never does arithmetic | `smartcam/query/compile.py` |
| 2.3 | Semantic search over clip captions with a **mandatory** `(site_id, ts, camera_id)` pre-filter before ANN | |
| 2.4 | `inspect_frames` re-look tool — retained keyframes back to the VLM. Worth ~1.32× on grounding accuracy alone | |
| 2.5 | Evidence assembly: every claim carries `{ts, camera, keyframe_uri, bbox, confidence, track_id}` | |
| 2.6 | **Count triple** (confident / ambiguous / coverage %) + short-gap track stitcher | Counting that survives scrutiny |
| 2.7 | **Four refusal modes**, each distinguishable to the user; `answers` table logs every query | Audit artefact + eval harness |
| 2.8 | Console: ask → answer → evidence filmstrip | |

---

## M3 · Real-time alerts + rules — Days 11–14

| # | Task | Output |
|---|---|---|
| 3.1 | Rule document schema (JSON) — scope / schedule / trigger / **confirmation** / verification / suppression / notify | |
| 3.2 | CEL evaluator on the edge, compiled once and cached; dwell/absence/count state machines outside CEL | Sub-millisecond rule eval |
| 3.3 | **Visual zone editor** — draw polygons on a live frame, normalised 0–1 coords, presence = bottom-centre of box | *This is what makes a non-technical buyer believe* |
| 3.4 | False-alarm stack, cheapest first: motion/zone masks → median-of-≥3 score → zone inertia 3 → size/aspect gates → `lightning_threshold` for IR day/night switching → temporal validation | |
| 3.5 | Suppression: debounce, cooldown, `max_per_hour` governor, `quiet_after_n_unactioned` | |
| 3.6 | NL → rule compiler, **authoring time only**. Renders the rule back to English *and draws it on the frame* for confirmation. Never in the runtime path | |
| 3.7 | Lane B VLM verification on fired alerts only. Verdict shown on the alert card | Highest perceived value per line of code |
| 3.8 | Delivery: WebSocket + FCM high-priority + webhook. **Start DLT registration paperwork today** (10 business days) | |
| 3.9 | One-tap "not an incident" on every alert → `rule.feedback` | The flywheel, and our future training data |

**SLO, set now:** ≤3 unactioned alerts per camera per day at go-live, ≤1 within 30 days. False alarms —
not latency, not accuracy — are what get these products muted in week two.

---

## M4 · Compliance pack + backfill — Days 15–18

| # | Task | Output |
|---|---|---|
| 4.1 | Factory SOP pack: PPE/helmet in zone, restricted-zone entry after hours, fire-exit obstruction, loading-dock dwell, zone occupancy | 5 shipped rules |
| 4.2 | Nightly batch compliance run + one-page 07:00 daily report | The actually-paid product |
| 4.3 | **Backfill importer** — ONVIF `GetReplayUri` + `Rate-Control: no` + `Frames: intra/5000`; fallbacks to Hikvision `/Streaming/tracks/…?starttime=`, Dahua `/cam/playback?…`, then file drop | 30 days of history before demo day |
| 4.4 | Publish **camera-days backfilled per hour per DVR** | The number that decides whether demo day has history |
| 4.5 | `POST /events/external` generic ingest (POS, access control, weighbridge). Demo with one CSV import | Answers "can you tie this to my systems?" live |

> **4.3 is not optional.** Frigate has no historical-ingest path whatsoever, and on demo day the index is
> empty while the buyer's first question is about an incident they personally remember. Naive 1× replay of
> 30 days × 20 cameras is 150 days of wall-clock. The ONVIF replay primitives make it minutes.

---

## M5 · Track & Trace + face — Days 19–22 · `[GPU]`

| # | Task | Output |
|---|---|---|
| 5.1 | CLIP-ReID tracklet embeddings — one per track, weighted over the sharpest crops. **Not per frame** (10–30× cheaper *and* more accurate) | |
| 5.2 | Hand-authored camera topology graph with min/max transit times. 15 minutes of work for 12 cameras | Does more for accuracy than any model swap |
| 5.3 | Topology-gated candidate search: a hit is only shown if reachable in the elapsed time | |
| 5.4 | **Candidate board UI** — ranked sightings, Likely/Possible/Weak badges, per-hop Confirm/Reject. Confirmed hops solid on the floor plan, unconfirmed dashed | |
| 5.5 | Face: SCRFD + AdaFace, enrolled staff gallery, multi-frame track voting, encrypted templates, 112×112 chip retained, immutable search audit log, tenant kill switch | |
| 5.6 | ANPR: fast-alpr + Indian-plate fine-tune (Plate Recognizer free tier as the labelling oracle), BH-series format validation, OCR confusion table | |

**Framing rule, non-negotiable:** Track & Trace ships as *"we narrowed 40,000 person-appearances to 12 for
you to confirm"*, never as an assertion of identity. State-of-the-art cross-camera ReID under clothing
change is ~58% Rank-1. A demo that claims certainty and gets caught on the misses loses the account; one
that says "8 of 11 found, operator confirmed in 20 seconds" wins it.

---

## M6 · Evidence layer + demo — Days 23–28

| # | Task | Output |
|---|---|---|
| 6.1 | SHA-256 at capture; per-camera-hour Merkle hash chain; WORM object lock | |
| 6.2 | ONVIF-scraped device make/model/serial/MAC; NIC/NPL-traceable NTP; append-only chain of custody | |
| 6.3 | **BSA s.63 certificate generator** — one-click PDF: frames, hashes, hash report, Merkle root, device identity, access log, pre-filled Schedule Part A | **The USP made physical** |
| 6.4 | Multi-tenant scoping + per-camera RBAC; Frigate port 5000 bound to loopback | Answers "can my Pune manager see only Pune?" |
| 6.5 | Clip export via the M4 backfill importer (on-demand pull from the customer's DVR by timestamp) | "Send me that clip" — the most common post-search action |
| 6.6 | **Numbers dashboard**: frames ingested, % surviving motion gate, % surviving detector, VLM calls/camera/day, GPU-seconds/camera/day, ₹/camera/month, p50/p95 query latency, measured FAR, bytes kept vs bytes seen | Worth more to the business than the demo |
| 6.7 | Demo script, rehearsed on the customer's own footage 24h before | |

---

## The demo script

Five minutes, in this order. Every question is answered from the index, never from video.

1. **Survey.** Run `smartcam-survey` live on their DVR. Cameras appear, graded. *This single moment sells the
   product more than any accuracy number.*
2. **Alert, with a stopwatch.** Draw a zone on screen. Type *"alert me if anyone enters this zone after
   8 PM"* in plain English. Walk into it. Alert on the console in under a second, with the evidence frame
   and the AI verification verdict.
3. **Talk to CCTV.** *"Was the fire exit blocked at any point today?"* — visually obvious, a real fine, and
   every ops head has been burned by it. Then *"how many workers entered Zone B without a helmet today?"* —
   answered as a count triple with coverage %.
4. **The honest no.** *"Was there a fire in the server room last Tuesday?"* → *"No camera covers the server
   room. Coverage 0% for that period."* Regulated buyers trust a system that refuses.
5. **The certificate.** Select the incident, click once, hand them a printed court-filable
   Bharatiya Sakshya Adhiniyam s.63 certificate. **Nobody else in the world does this.**
6. **The counter.** *"Raw bytes seen: 4.1 TB. Bytes Smart Cam Monitoring kept: 38 GB. Your DVR's oldest frame: 23 days
   ago. Smart Cam Monitoring's oldest frame: day one."*

---

## Deck corrections — before the next customer meeting

Every one of these was independently flagged by three or more research dimensions. Indian buyers use SI
technical evaluators, and unevidenced performance claims are also misleading advertisements under the
Consumer Protection Act 2019, actionable by the CCPA independently of any customer complaint.

| Remove | Replace with |
|---|---|
| "Super-pixelation makes low-res faces identifiable" | *Delete entirely.* Keep enhancement only as a watermarked, non-evidentiary display aid. |
| "80–95% compression with no loss of analytical detail" | "We index instead of archiving — motion-gated frame selection plus embeddings." |
| "90% storage saving by discarding your raw video" | "Your DVR keeps recording exactly as it does today. Smart Cam Monitoring adds 24 months of searchable, hash-sealed memory on top." |
| "Under 5 seconds across a million cameras" | "Under 5 seconds across every camera on your site, over a continuously built index." |
| "Alerts in under 2 seconds" | "Under 2 seconds to your live console; under 5 seconds to your phone, typically." |
| "Trained on 6,000+ incident types" | "Open-vocabulary — describe the incident in plain language and we detect it." (The largest public taxonomies are Kinetics-700 at 700 classes and AVA at 80. The biggest Indian competitor claims 200+. Someone will ask for the list.) |
| "No new hardware" | "One small appliance. We keep every camera you own." |

---

## Definition of done

- [ ] All five capabilities demoed on the customer's own cameras
- [ ] Glass-to-glass alert latency measured and published, not estimated
- [ ] False-alarm rate measured over ≥7 days of real operation
- [ ] `answers` table shows every query, including the refusals
- [ ] A BSA s.63 certificate PDF printed and handed over
- [ ] The numbers dashboard shows real ₹/camera/month COGS
- [ ] Zero AGPL / non-commercial dependencies (CI-enforced)
- [ ] Engineer-hours-per-site instrumented from deployment one — *the single number that tells us whether
      this is a SaaS business or a services business*

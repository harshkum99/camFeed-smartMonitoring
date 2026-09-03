# Smart Cam Monitoring — System Architecture

Version 1.0 · 2026-09-02 · derived from an 18-agent research sweep (see `docs/research/`)

---

## The one invariant

> **All analysis happens at ingest. Queries only ever hit an indexed event store.
> No user query ever causes video to be analysed.**

Every capability in the product is a consequence of this rule, and the deck's "answers in under 5 seconds
across every camera" claim is only true because of it. If a feature ever needs to analyse video at query
time, the feature is designed wrong. NVIDIA's own reference system takes 2.4 seconds for a single agentic
query over 17 clips, and 34 seconds at 50 concurrent — that is what happens when you break this rule.

---

## Three lanes, one event store

The single most important structural decision. The deck describes one pipeline
(compress → extract → enhance → tag → store → answer) and then asks it to deliver 2-second alerts. It
cannot: that is an archival pipeline, and the cloud round trip alone costs more than the budget. So we
split it into three lanes that share an event store but never share a latency budget.

```
                    ┌──────────────── CUSTOMER LAN ────────────────┐
                    │                                              │
  cameras / DVR ───▶│  go2rtc ──▶ decode ──▶ motion gate ──▶ detect│──▶ track
   (RTSP, ONVIF)    │     │                                    │   │      │
                    │     │                                    │   │      ▼
                    │     │                              LANE A │   │  rules (CEL)
                    │     │                              (HOT)  │   │      │
                    │     │                                     │   │      ▼
                    │     │                                     │   │   ALERT ◀── <1.2s
                    │     ▼                                     │   │      │
                    │  keyframes + crops + embeddings ──────────┘   │      │
                    │     │                          LANE C (COLD)  │      │
                    │     ▼                                         │      │
                    │  durable spool (SQLite WAL + disk quota)      │      │
                    └─────┬────────────────────────────────────┬────┘      │
                          │ outbound HTTPS only, rate-limited  │ MQTT/TLS  │
                          ▼                                    ▼           ▼
   ┌────────────────────────── SMART CAM MONITORING CLOUD ────────────────────────────────────┐
   │                                                                            │
   │   ingest API ──▶ EVENT STORE (Postgres + TimescaleDB + pgvector)           │
   │                    tracks · zone_events · clips · camera_uptime · answers  │
   │                        ▲                    │                              │
   │       LANE B (WARM) ───┘                    ▼                              │
   │   VLM verify / caption           query router ──▶ filter compiler ──▶ SQL  │
   │   (Gemini Flash, 0.4–1s)              │                └──▶ vector search   │
   │                                       └──▶ evidence frames + confidence    │
   └────────────────────────────────────────────────────────────────────────────┘
```

| Lane | Runs | Latency budget | Purpose |
|---|---|---|---|
| **A — Hot** | Edge box, on the LAN | **< 1.2 s** to an open console | Detector + tracker + rule engine. Fires alerts. Never calls a VLM. Never waits on the cloud. |
| **B — Warm** | Cloud | 0.4–8 s | VLM second opinion on a *fired* alert. Confirms, downgrades or suppresses it before it earns a WhatsApp message. |
| **C — Cold** | Edge → cloud, async | minutes | Keyframes, embeddings, captions, backfill. Builds the memory that Talk-to-CCTV queries. Its latency does not matter and must never enter the alert path. |

**The alert claim, stated honestly:** *"Under 2 seconds from incident to alert on your live console.
Under 5 seconds to your phone, typically."* WhatsApp (>1–3 s, plus a hard platform limit of one message
per 6 seconds per user) and DLT SMS (3–30 s) can never hit 2 seconds. A customer with a stopwatch and a
phone catches an unqualified claim in the first demo.

---

## Component decisions

Every model and library below was checked for licence. This is a board-level concern, not an engineering
detail: Ultralytics states plainly that SaaS deployment and trained weights are covered by AGPL-3.0, and
discovering that during a bank's due diligence is a deal-killer, not a patch.

| Layer | Pick | Licence | Why |
|---|---|---|---|
| Edge NVR / ingest / decode / motion gate | **Frigate 0.17.2, unforked** | **MIT** ✅ | Ships the boring-but-hard parts already. Verified: the repo LICENSE is MIT; only the *name* and logo are trademarked. |
| RTSP/ONVIF adapter | go2rtc (embedded in Frigate) | MIT | Widest vendor coverage. Pin the version — single maintainer. |
| Object detector | **D-FINE-S** (primary), RF-DETR-S (alternate) | **Apache-2.0** ✅ | 50.6 COCO AP at 3.5 ms on T4, beating YOLO11-S's 44.4 AP. Better *and* licence-clean. |
| ~~Ultralytics YOLO (any version)~~ | **BANNED** | AGPL-3.0 ❌ | Would require open-sourcing all of Smart Cam Monitoring. CI licence gate enforces this. |
| ~~DEIMv2~~ | **BANNED** | custom ❌ | DEIM v1 is Apache; v2 reverted to a commercial-enquiry licence. Easy to grab by accident. |
| Tracking | ByteTrack → BoT-SORT-ReID where identity matters | MIT | ByteTrack 14.4 ms/frame; BoT-SORT-ReID 32.6 ms — budget ~1 CPU core per stream. |
| Frame / appearance embeddings | SigLIP2 or jina-clip-v1 via ONNX Runtime | permissive | Powers semantic search and Track & Trace. ~314 FPS on modest silicon — nearly free. |
| Person ReID | CLIP-ReID (ViT-B/16, 512-d) | — | The only model that stays usable off-domain (16–50% mAP where OSNet gives 1–6%). |
| Face detection | SCRFD-10GF (ONNX) | permissive | |
| Face recognition | **AdaFace IR-101 @ WebFace12M** | **MIT** ✅ | Best low-quality open model that is actually licensable. IJB-S surveillance rank-1 71.35%. |
| ~~InsightFace buffalo_l / antelopev2~~ | **PROTOTYPE ONLY** | non-commercial ❌ | The default every demo reaches for. Must never enter a shipped build. |
| ANPR | fast-alpr + fast-plate-ocr, fine-tuned on Indian plates | MIT | 0.68 ms/plate. Needs Indian fine-tune — global model reads ~60–80% here. |
| Event + vector store | Postgres 16 + TimescaleDB + pgvector | — | One database at PoC scale. Qdrant + ClickHouse at ~100+ cameras. |
| Edge → cloud data plane | Durable local spool → HTTPS, resumable, rate-limited | — | **Not MQTT.** Mosquitto silently drops QoS-0 to a disconnected client and caps queues at 1,000 messages — three hours of outage loses events. |
| Edge → cloud control plane | MQTT/TLS 8883, `max_queued_messages 0`, persistent session | — | Small control and alert messages only. |
| Captioning + alert verification | Gemini 3 Flash / Flash-Lite (API); Qwen3-VL-8B self-hosted as the air-gap switch | — | API wins until ~2,500 cameras. Self-host path built as config, not a rewrite. |
| Query planner | Claude/Gemini emitting a **constrained JSON filter**, never free SQL | — | Shrinks text-to-SQL from Spider-2.0-hard (21% accuracy) to Spider-1.0-easy (~91%). |
| Rules evaluation | CEL (compiled once, cached) | Apache-2.0 | Nanosecond-to-microsecond, sandboxed, non-Turing-complete. |

**What we do NOT deploy:** NVIDIA VSS (128 GB RAM + H100 floor, and its own docs admit no TLS, no
authentication and no rate limiting between components — it is a demo rig). We read its now-Apache-2.0
source and steal the data model. Same for DeepStream and Savant: learn from, don't deploy.

---

## Storage: retention-additive, never retention-replacing

**We reversed the deck's most commercially exciting claim, and the reversal is a better product.**

The deck says: discard the raw video, keep only analysed frames, save 90% of storage. Six independent
research dimensions flagged this as the single most dangerous decision in the project:

- **It is probably unlawful for our best customers.** IT Act s.7(1)(b) requires records be retained in the
  original format or one "demonstrably representing accurately" the original — 1-fps keyframes are not.
  *Paramvir Singh Saini* (SC, 2020) mandates 18 months of audio+video in police stations. Banks, schools
  and insurers have their own expectations. A customer who deletes footage on our advice and then loses a
  case is an existential liability.
- **It destroys our own product.** Every state-of-the-art video-QA system depends on a
  re-look-at-the-frame step. Delete the pixels and any question our ingest schema did not anticipate
  becomes permanently unanswerable, and no model improvement can ever be applied retroactively.
- **It contradicts Track & Trace.** ReID needs many contiguous frames; decimating to 0.05 fps to hit the
  storage number destroys temporal aggregation. You cannot have both on the same site.
- **The stated mechanism is arithmetically wrong anyway.** A 1080p JPEG is ~200 KB; an inter-coded H.265
  frame at 2 Mbps averages 16.7 KB. Storing keyframes at 1 fps *saves 20%, not 90%*. The saving only
  appears at ~0.05 fps — i.e. discarding 99.5% of frames — which must be stated plainly or it is
  mis-selling.

**So the architecture is:**

> The customer's DVR keeps recording exactly as it does today. We never touch it, never delete from it,
> never become the system of record. Smart Cam Monitoring adds a compact, hash-sealed, searchable index on top —
> and keeps that index for far longer than the DVR keeps video.

This is a *stronger* sales position: **"Your DVR keeps 23 days. Smart Cam Monitoring keeps 24 months, hash-sealed and
court-exportable, for less than the cost of one hard disk."** It also removes the customer's biggest
objection instead of creating it, and it is the only version a bank's risk team will sign.

Raw video never leaves the LAN. Only keyframes, crops, embeddings and tags cross the WAN — about
**42 KB per gated event**, or ~31 kbps sustained for a 40-camera site. That number belongs on a slide.

---

## The event schema

Two rules that must be enforced in code review from the first commit, because retrofitting either means
reprocessing the entire corpus:

1. **Store tracks, not per-frame detections.** Per-object rows at 5 fps across 1,000 cameras is 1.3
   billion rows/day. Track-level is ~5 million. This is a 250× difference and it is the easiest way for
   an engineer to accidentally make the system unaffordable.
2. **`tenant_id` and `site_id` are non-null on every table from migration 001.** Retrofitting a tenant
   column across a partitioned event store is a rewrite.

```sql
cameras(camera_id PK, tenant_id, site_id, name, zone_map, lens_type, grade, tz, ...)
  -- lens_type ∈ {rectilinear, fisheye, ptz}   grade ∈ {recognition, detection, anpr, degraded, unservable}

camera_uptime(camera_id, ts_start, ts_end, state)     -- online|offline|degraded|obscured
  -- THE "I don't know" TABLE. This is the liability shield, not a nice-to-have.

tracks(track_id PK, camera_id, tenant_id, site_id, class,
       ts_start, ts_end, dwell_s, conf_max, conf_mean, n_frames,
       bbox_first, bbox_last, path_simplified,
       attrs jsonb,                    -- {helmet:0.93, vest:0.11, upper_colour:"blue"}
       appearance_vec vector(512),
       global_identity_id NULL, identity_conf,   -- nullable ON PURPOSE
       best_keyframe_uri, frame_sha256,
       model_versions jsonb)           -- mandatory: makes re-indexing possible
  PARTITION BY RANGE(ts_start)

zone_events(event_id PK, track_id FK, zone_id, camera_id, type, ts, dwell_start, dwell_end, conf, evidence_uri)
  -- ALL counting questions become COUNT(*) on THIS table. Never on captions.

clips(clip_id PK, camera_id, ts_start, ts_end, caption, caption_vec, frame_vec,
      keyframe_uris[], retained_reason, vlm_version)

rules(rule_id, site_id, name, doc jsonb, severity, active)
alerts(alert_id, rule_id, ts, state, verifier_verdict, evidence_uris[])
answers(answer_id, actor, question, plan, sql_executed, evidence_ids[], coverage_pct, abstained, latency_ms)
  -- eval harness + DPDP Rule 6 audit artefact + anti-stalking defence, all in one table. Build day 1.
external_events(source, site_id, ts, type, amount, actor, payload)
  -- generic inbound endpoint: POS, access control, weighbridge. Camera+time is the join key.
```

---

## The query path

```
question ──▶ ROUTER (small, cheap classifier — not the big LLM)
              │
              ├─ AGGREGATE/COUNT ──▶ parameterised SQL over zone_events. LLM does no arithmetic.
              ├─ LOOKUP/FILTER ────▶ parameterised SQL + evidence frames
              ├─ OPEN-VOCAB ───────▶ vector search over clip captions ──▶ VLM re-look to verify
              └─ TRACE/IDENTITY ───▶ embedding kNN, time+topology gated ──▶ ranked candidates, human confirm
```

**The LLM never computes a number and never writes free SQL.** It emits a JSON filter object against a
whitelisted set of ~20 columns and operators; our code compiles that to parameterised SQL. Router accuracy
is a cost control as much as a quality control — a misrouted count that should cost ₹0.08 in SQL costs
₹8 and 20 seconds down the agentic path.

**Counts are reported as a triple, never a scalar:** *"14 confident, 3 ambiguous, 97% camera coverage."*
A bare "14" is a lie waiting to be caught, because the DB does exact arithmetic over whatever rows exist —
if track fragmentation split one person into five tracks, SQL faithfully returns five. Counting accuracy
lives in a short-gap **track stitcher** (same camera, same zone, gap < 2 s, appearance cosine above
threshold), not in the detector.

**Four distinct honest refusals**, and they must be distinguishable to the user:

| Refusal | Meaning |
|---|---|
| **No coverage** | `camera_uptime` shows a gap, or no camera covers that zone. Name the camera and the minutes. |
| **No evidence found** | Retrieval returned nothing above threshold. Offer to widen the window. |
| **Not measured** | The question needs an attribute we never captured. Offer to enable that detector going forward. |
| **Low confidence** | Evidence exists but is weak. Show candidates, ask the operator to adjudicate. |

A demo should deliberately include one question the system correctly refuses. Buyers in regulated
industries trust a system that says no more than one that always answers.

---

## Face recognition — how we ship what Harsh asked for, lawfully

Harsh chose full face recognition in the PoC. It ships, scoped so that it is lawful **without consent**:

- **Factory + employees only.** DPDP s.7(i) makes processing for "purposes of employment and safeguarding
  the employer from loss or liability" a consent-free legitimate use. This is exactly our PoC vertical —
  the vertical choice and the legal basis line up.
- **Enrolled gallery only.** 20–50 staff enrolled deliberately. Never a gallery auto-built from ambient
  footage — that specific behaviour is a *prohibited practice* in the EU (AI Act Art 5(1)(e)) and has no
  lawful basis in India either.
- **AdaFace (MIT), not InsightFace.** InsightFace weights are non-commercial. A licence CI gate blocks them.
- **Never single-frame.** Aggregate embeddings across a track and vote. This is where real accuracy lives.
- **Store the 112×112 aligned chip alongside the embedding.** Without it, changing the recognition model
  invalidates the entire index with no way to re-embed. ~5–10 KB of insurance.
- **Camera grading is honest.** Recognition needs ≥64 px between eye centres. Typically only 5–15% of a
  customer's cameras qualify. We *measure and tell them which* — turning their oldest weakness into a
  paid consulting output instead of a broken promise.
- **Off by default per tenant, encrypted templates, separate retention clock, immutable search audit log,
  one-switch purge.**

Two things we will not do: no super-resolution in the identification path (see below), and no open-ended
1:N search of the general public.

### Super-pixelation is removed from the pipeline entirely

Not softened — removed. Generative upscaling synthesises plausible pixels conditioned on training priors;
it cannot recover absent information. Delhi Police's own facial recognition system scored 2% accuracy in
2018 and could not reliably distinguish sex — that is the public record an opposing expert will cite.
And it directly contradicts our own headline USP: certifying a hallucinated frame's SHA-256 under a
Bharatiya Sakshya Adhiniyam s.63 attestation is a false attestation, and the person who signs it is
personally on the hook.

Enhancement survives only as a **display aid for human review**, visibly watermarked "enhanced — not
evidentiary", and excluded from every evidence bundle.

---

## The USP: evidence-grade video memory

*(full argument in `docs/USP.md`)*

Natural-language camera search is no longer a differentiator — Eagle Eye gives it away free, Coram and
Spot AI ship it, and open-source Frigate has it. What no competitor will build is India's legal artefact.

**Bharatiya Sakshya Adhiniyam 2023 s.63(4)** and its Schedule (in force 1 July 2024) require every
electronic record tendered in an Indian court to carry a certificate stating SHA-1/SHA-256/MD5 hashes with
a hash report enclosed, with tick-boxes that explicitly name "DVR" and "Cloud" as sources, signed by the
custodian (Part A) and an expert (Part B). Today Indian businesses satisfy this by hand, with a pen drive.

We hash every frame at the moment of capture, chain the hashes per camera-hour, scrape device
make/model/serial/MAC over ONVIF, stamp against an NIC/NPL-traceable NTP source, keep an append-only
chain-of-custody log, and generate the filled Schedule Part A as a PDF on one click.

It is roughly three weeks of work with no ML in it. Verkada and Coram will never build an Indian evidence
certificate generator. Videonetics and Staqu are per-channel detection vendors who have never framed their
product as a legal artefact. And the proof point fits in a meeting: **hand the buyer a printed,
court-filable certificate.**

---

## Engineering hazards that are invisible until they bite

Each of these was found by an adversarial pass and each has a one-line fix that is expensive to retrofit.

| Hazard | Fix |
|---|---|
| **Docker's default 64 MB shm** — 40 cameras at 1080p needs ~2.4 GB. Symptom is a `Bus error` on startup, not slow performance. | Compute `shm_size` from `(w·h·1.5·20 + 270480)/1048576` per camera. One line in compose. |
| **FFmpeg has no RTSP auto-reconnect.** The `reconnect*` options are HTTP-only. | External supervisor: exponential backoff **with jitter** (without it, a switch reboot makes all 40 channels retry in lockstep and the DVR refuses the burst), a global in-flight semaphore, per-device session cap. |
| **Three clocks disagree** — edge box, DVR OSD burn-in, DVR recording index. Our answers use one, the customer's sanity-check uses another, evidence export uses the third. | ONVIF `GetSystemDateAndTime` is **PRE_AUTH** — measure every device's offset before we even have credentials. Alarm above 30 s drift. Refuse evidence export for a camera that drifted during the window. |
| **Clock skew breaks authentication.** WS-UsernameToken validates our timestamp against the *device* clock. A 10-minute-out DVR rejects correct credentials with a generic auth error. | Generate every `Created` value in device time, not host time. Otherwise a field engineer loses a day convinced the password is wrong. |
| **One RTSP session per consumer exhausts the DVR** and degrades the customer's own live view. If Smart Cam Monitoring makes their CCTV worse, the deal is dead regardless of AI quality. | Exactly one upstream session per channel via go2rtc restream; fan out locally. |
| **Frigate's port 5000 bypasses role enforcement entirely** and treats every request as admin. Our sync agent naturally points there. | Bind 5000 to loopback; agent authenticates on 8971. A bank's security review *will* find this. |
| **Fisheye destroys person detection** — a fisheye-fine-tuned model scores 0.36 mAP on pedestrians, roughly a coin flip. And a PTZ that moves silently invalidates every zone and rule drawn on it. | Dewarp fisheye to 2–4 virtual rectilinear cameras before detection. Bind PTZ zones to named presets; suspend rules when off-preset and log it as a coverage gap. |
| **Negative-attribute rules ("no helmet") are the false-alarm disaster.** A person facing away or wearing a white cap fires confident violations. It is also the first rule a factory will test. | `min_frames ≥ 5` and mandatory VLM second opinion on every negative-attribute rule. |
| **Live view has no NAT path.** go2rtc's own docs say to open port 8555 — impossible on Jio/Airtel CGNAT. Relaying via Twilio TURN in Mumbai at $0.60/GB costs more than the subscription. | Edge publishes outbound into a cloud SFU so one publish serves N viewers. Cloudflare TURN ($0.05/GB, 12× cheaper) as fallback. Meter viewer-minutes as a COGS line from day one. |
| **Frigate cannot ingest recorded video at all** — and demo day starts with an empty index, while the buyer's first question is about an incident they remember from last week. | Separate in-house backfill importer using ONVIF `GetReplayUri` with `Rate-Control: no` + `Frames: intra/5000` (both mandatory-to-support), falling back to vendor playback grammars, then to a file drop. Same component serves clip export. |
| **A busy scene inverts the unit economics.** The whole business rests on ~200 gated VLM events/camera/day. A windy outdoor yard produces 20,000. | Hard per-camera daily VLM call caps in code, per-tenant budget alarms, and a gate-quality dashboard from day one. |

---

## What we deliberately do not build in the PoC

Heavy compression. Super-pixelation (permanently killed). A custom RTSP stack. Any trained
action-recognition model. Multi-thousand-camera scale. Kubernetes. An air-gapped build. SMS and WhatsApp
delivery (DLT sender-ID registration alone is 10 business days, and neither channel can hit 2 seconds
anyway — but **start the DLT paperwork on day one** so it is ready for the pilot).

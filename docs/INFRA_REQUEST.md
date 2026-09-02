# Infrastructure Request — Project Sanjay PoC

**For: Harsh.** Everything on this page is a blocking dependency. Ordered by how much it delays the PoC.
Prices are Sept 2026 street prices from the research sweep; re-confirm on quote.

---

## 1. Cloud — Indian provider (BLOCKING, needed first)

You chose an Indian provider. That is the right call and it is worth more than a rupee saving: RBI's
outsourcing Master Direction (RBI/2023-24/102) requires India-only data storage and inspection rights over
us and our sub-processors, so a BFSI or PSU deal is impossible on an offshore GPU. Picking India now
means we never have to re-platform.

**Recommended: E2E Networks** (Indian, self-serve rate card, IndiaAI-empanelled).
Yotta Shakti is the runner-up — cheaper on H100 (₹356/hr) but avoid its managed Kubernetes, whose
platform fee is ₹5.4–9 lakh/month, roughly ten times our entire PoC budget.

### Node A — GPU node

| Spec | Value |
|---|---|
| GPU | 1 × NVIDIA **L4 24GB** (A100 40GB acceptable if L4 unavailable) |
| vCPU / RAM | 8+ vCPU, 32 GB |
| Disk | 500 GB NVMe SSD |
| Region | Any Indian region (Delhi/Mumbai) |
| Indicative cost | ~₹49/GPU-hr → **₹31,000/month** if left on 24×7 |

Runs: object detection benchmarking, SigLIP/CLIP embeddings, person-ReID embeddings, face recognition,
ANPR, and optionally a self-hosted VLM later.

**Cost control:** we do not need this 24×7 in weeks 1–2. If E2E supports stop/start billing, budget
~₹12,000/month for the first fortnight and scale up. Ask their sales team two questions when you call:
1. Stop/start (per-hour) billing on GPU nodes, or is it monthly-committed?
2. The **IndiaAI Mission empanelled subsidised rate card** — we could not verify current subsidised rates
   from any primary source, and if a real subsidy exists it changes our self-host-vs-API break-even.

### Node B — Application / control node

| Spec | Value |
|---|---|
| vCPU / RAM | 8 vCPU, 16 GB |
| Disk | 200 GB SSD |
| Indicative cost | **₹6,000–9,000/month** |

Runs Postgres 16 + TimescaleDB + pgvector, the API, the web console, MinIO (S3-compatible), and the
alert dispatcher. No Kubernetes — plain Docker Compose. Kubernetes at this stage costs engineering weeks
and buys nothing.

### Object storage

Start on Node B's local disk. PoC volume is genuinely tiny — 20 cameras × 30 days of motion-gated frames
is roughly **600 GB**. Move to E2E EOS (₹2–3/GB) or Cloudflare R2 (zero egress) only when we outgrow it.
Do not spend a sprint on storage optimisation; the PoC's cost is entirely GPU hours.

**Cloud subtotal: ₹40,000–45,000/month at full tilt, ~₹20,000/month lean.**

---

## 2. Edge hardware (BLOCKING for the real-camera demo)

Research is unambiguous here and it is counter-intuitive: **do not buy a Jetson.** In India the Jetson tax
is brutal and supply is unreliable — an Orin Nano Super devkit is ₹46,609 incl. GST on pre-order, an Orin
NX 16GB module is ₹1,24,501 and out of stock, a built Jetson box is ₹1.73–2.08 lakh. Meanwhile an Intel
mini-PC does the same job for a fifth of the price, decodes video in hardware, runs a mainline kernel our
field engineers can actually debug remotely, and ships this week.

**Buy two identical Intel mini-PCs:**

| Item | Cost |
|---|---|
| ASUS NUC 14 Essential N150 barebone (or MSI Cubi N ADL N100) | ₹19,635 |
| 16 GB DDR5 SO-DIMM | ~₹3,500 |
| 1 TB NVMe SSD | ~₹5,000 |
| 600VA UPS *(not optional — see below)* | ~₹3,000 |
| **Per box** | **~₹31,000** |
| **Two boxes** | **~₹62,000** |

One goes to the pilot factory. One stays on our desk as the reference/dev box, so we are never blocked on
site access.

The UPS is not a nicety. The edge box holds the site's event spool; an unclean power loss mid-write
corrupts it, and once the raw video has aged out of the customer's DVR there is no way to re-derive it.

**Do not buy yet:** a Jetson, an RK3588 board, a Hailo accelerator, or an edge GPU. An N150 covers 8–16
cameras at a 5 fps detect rate, which is every demo we will give in the next two months. If a site needs
more, the answer is a second box, not a bigger one — consumer GPUs cap at 12 concurrent decode sessions,
so the "one box for 128 cameras" idea in the deck does not survive contact with the hardware.

---

## 3. API keys and accounts

| What | Why | Cost |
|---|---|---|
| **Google AI Studio / Gemini API key** | Frame captioning and alert verification. Gemini 3 Flash / Flash-Lite is dramatically cheaper than self-hosting until ~2,500 cameras. | ~$20–50/month at PoC volume |
| **Plate Recognizer free tier** | 2,500 lookups/month. Used as a *ground-truth oracle* to auto-label Indian plates so we don't hand-label from scratch. | Free |
| A domain + DNS | Console + API + TLS | ~₹1,000/yr |

**Note on Gemini pricing:** the current Flash rate is a published promotional rate that roughly doubles on
1 Jan 2027. We will build the self-hosted path (Qwen3-VL) as a config switch from the start so that is a
pricing decision, not a rewrite. It is also the only path that works for the air-gapped SKU.

---

## 4. What I need from the pilot site (BLOCKING, and the one that always slips)

This is the item that delays deployments more than any technology, so please start it now.

1. **DVR/NVR admin credentials.** In India these are very often held only by the installer who set the
   system up, and they have commercial reasons not to hand them over. Budget a conversation. If the
   password is genuinely lost, we need physical access for a reset.
2. **DVR/NVR make, model, and LAN IP.** Plus whether cameras are IP or analog-on-DVR.
3. **Network:** a LAN port for the edge box, and the site's broadband upload speed. We need **no port
   forwarding and no inbound firewall rules** — the box connects outbound only.
4. **A signed one-paragraph letter from the customer** stating the deployment is for "purposes of
   employment and safeguarding the employer from loss or liability." That is DPDP s.7(i) verbatim, and it
   is what makes employee monitoring — including the face recognition you asked for — lawful **without
   consent**. It costs them nothing and it is our legal foundation.
5. **A rough floor plan or camera map.** Fifteen minutes of someone's time. Hand-drawing which camera can
   reach which will do more for Track & Trace accuracy than any model choice.

---

## 5. Total

| | Monthly | One-time |
|---|---|---|
| Cloud (full tilt) | ₹40,000–45,000 | — |
| Cloud (lean, weeks 1–2) | ₹20,000 | — |
| Edge hardware ×2 | — | ₹62,000 |
| APIs | ₹4,000 | — |
| **PoC total (4 weeks)** | | **≈ ₹1.5–1.9 lakh** |

For comparison, the research put a credible 4-week PoC at ₹78,000–1,02,700 for cloud alone; we are inside
that, and the extra is the second edge box, which buys us independence from site access.

---

## Order of operations

1. **Today:** start the DVR credentials conversation with the pilot site. It has the longest lead time.
2. **Today:** order the two mini-PCs. They ship in 48 hours; a Jetson would have taken three weeks.
3. **This week:** provision Node B (the cheap CPU node). I can build the entire event store, query engine,
   evidence layer and console against it with no GPU at all.
4. **Week 2:** provision Node A (GPU) once there is real footage to run models against. Renting a GPU
   before there is footage to point it at is the most common way to burn ₹2 lakh for nothing.

Node B first, Node A second. That ordering saves roughly ₹15,000 and costs nothing.

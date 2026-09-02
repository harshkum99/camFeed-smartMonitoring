# Project Sanjay — The USP

## The problem with the current positioning

"Ask your cameras a question" is not a differentiator any more. As of September 2026:

- **Eagle Eye Networks gives it away free** in every edition — "All Eagle Eye users can opt in to get this
  powerful AI enhancement for free."
- **Coram AI** advertises "work with any IP camera. No rip-and-replace" — the exact sentence in our deck.
- **Spot AI** ships a conversational agent (Iris) across 1,200 customers and 3 billion video-minutes/month.
- **Rhombus** has had it since April 2025. **Milestone** ships a VLM built with NVIDIA.
- **Open-source Frigate** ships semantic search, GenAI descriptions and a tool-calling chat agent for
  **zero licence cost**.

So if Sanjay's pitch rests on camera-agnostic natural-language search, we are entering the most crowded
camp in the market with a slide that four better-funded competitors already have.

Meanwhile the Indian market is smaller than the deck assumes. The **entire** disclosed FY25 revenue of
India's named video-AI pure-plays — Videonetics ₹79 Cr, Detect ₹51 Cr, Kritikal ₹28 Cr, Staqu ₹20 Cr,
Wobot India ₹13 Cr — is roughly **₹200–250 Cr combined**, against CP Plus alone booking ₹3,112 Cr, 77% of
it from plain cameras and recorders. The software-to-hardware revenue ratio in Indian surveillance is
about **1:14**. And the buyer's existing recurring anchor is an AMC line of **₹500–1,100 per camera per
year** — ₹42–92/month. Any per-camera AI subscription is a 6–20× step-up on a line item they already
resent.

The differentiation therefore cannot be the AI. It has to be something structural.

---

## The USP

> ### Sanjay turns the CCTV you already own into a searchable, court-admissible 24-month memory — for less than what one hard disk costs you.

Three things make this defensible in a way that "better AI" is not.

### 1. It is India's legal system, encoded in software

**Bharatiya Sakshya Adhiniyam 2023, section 63(4)** and its Schedule — in force since 1 July 2024 —
require every electronic record tendered in an Indian court to carry a certificate stating the
SHA-1/SHA-256/MD5 hash **with a hash report enclosed**, identifying the source device (the form has
tick-boxes that explicitly name "DVR" and "Cloud"), signed by the custodian in Part A and by an expert in
Part B, **at each instance of submission**.

Today, Indian businesses satisfy this by hand, with a pen drive and a notary.

We do it at capture: SHA-256 every frame, chain the hashes per camera-hour into a Merkle root, scrape
device make/model/serial/MAC over ONVIF, timestamp against an NIC/NPL-traceable NTP source (which CERT-In
mandates anyway — a compliance chore turned into a feature), keep an append-only chain-of-custody log, and
generate the filled Schedule Part A as a PDF on one click.

**Why it is defensible:** it is not a technical moat, it is a jurisdictional one. Verkada, Coram, Spot AI
and Eagle Eye will never build an Indian evidence-certificate generator — the market is invisible from San
Francisco. Videonetics, Staqu, Awiros and Vehant are per-channel detection vendors who have never framed
their product as a legal artefact. It takes about three weeks to build and contains no machine learning.

**The proof point fits in a meeting:** hand the buyer a printed, court-filable certificate.

### 2. It inverts the storage claim from a liability into the pitch

The deck's "we discard your raw video and save you 90% of storage" is, on the research, unlawful for our
best customers, destructive to our own product, and arithmetically wrong in its stated mechanism. So we
flip it:

> **"Your DVR keeps 23 days. Sanjay keeps 24 months."**

We never touch their recording. We add memory on top of it. This removes the single hardest objection in
every regulated-sector meeting instead of creating it, and it is the only version a bank's risk team signs.

It also happens to be extremely cheap. Raw video never leaves the LAN; only keyframes, crops, embeddings
and tags cross the WAN — about **42 KB per gated event**, roughly **31 kbps sustained for a 40-camera
site**. That is what lets us price at an Indian number while Verkada and Coram must store the video.

### 3. It is priced by an architecture Western competitors cannot match

Verkada's cheapest camera is $699 (₹66,400) against an Indian 4MP dome at ₹3,000–6,000. Rhombus lists
$149/camera/year for software alone. Cloud VMS economics are structurally hostile here: a single 2 Mbps
camera with 30-day cloud retention costs about ₹770/camera/month in raw storage before any compute.

Our unit cost is one to two orders of magnitude lower because of three decisions that are the *same*
decision: detection runs on a customer-premises box, the VLM touches under 0.5% of frames, and raw video
never moves. That is also precisely what makes an air-gapped SKU possible — so the cheap-cloud requirement
and the government/BFSI requirement are one engineering project, not two.

---

## Ranked, with build times

| # | Candidate moat | Defensibility | Build | Verdict |
|---|---|---|---|---|
| 1 | **Evidence-grade hash-sealed memory + BSA s.63 certificate** | **High — structural, jurisdictional** | ~3 weeks, no ML | **The identity** |
| 2 | **India-price architecture** (edge inference, <0.5% VLM, raw stays on LAN) | High but quiet — it is not a slide, it is why our price is legal | It *is* the PoC | **The identity** |
| 3 | Per-site false-alarm flywheel (operator confirm/dismiss trains a per-camera reranker) | Medium-high, compounding | 3–4 weeks for v1 | Roadmap |
| 4 | SOP/compliance vertical packs sold as outcomes | Low technically, medium-high commercially | Ongoing | Best *revenue* wedge, weakest moat — always bundle with #1 |
| 5 | Air-gapped / on-prem SKU | Medium | Falls out of #2 | Market access, not differentiation |
| 6 | Cross-camera identity graph / site memory | Medium-high after 12 months | 4–8 weeks | Highest DPDP risk, slowest payoff |
| 7 | Works-with-any-junk-camera onboarding | Low-medium (Coram claims it too) | Continuous | Necessary to win the demo, not sufficient to win the category |

---

## Pricing

Two-part tariff, deliberately not a flat per-camera number — the moment a buyer computes ₹800 × 4,000
cameras, the deal dies.

| SKU | Price |
|---|---|
| Site platform fee (edge agent, full-site indexing, Talk-to-CCTV, daily report) | **₹4,999 / site / month** |
| Continuous monitoring (only on cameras carrying an active real-time rule — typically 3–6 of 20) | **₹899 / camera / month** |
| Compliance pack (vertical SOP library + auditor export) — *priced against the fine, not the camera* | **₹15,000–40,000 / site / month** |
| Evidence certificate export | 20/month included, then **₹500** per certified bundle |
| Retention depth | 12 months included; +₹149 (24mo), +₹269 (36mo) |
| Track & Trace | +₹200/camera/month, **or ₹5,000 per investigation** |
| Air-gapped / government | ₹4,500/camera one-time perpetual + 18% AMC, min 100 cameras |
| **One-time site onboarding & survey** | **₹15,000–25,000** |

A 20-camera outlet with 5 monitored cameras: ₹9,494/month ≈ ₹475/camera blended. Annual prepay only,
15% discount — Indian mid-market cash collection is the silent killer.

### The onboarding fee is not optional

Research put fully-loaded site onboarding at **₹11,000–17,000** — 51.65% of India's camera estate is
analog-on-DVR with credentials held by an installer who has reasons not to share them, and 20–40% of sites
need a physical visit. Absorbed, that consumes roughly a year of a camera's gross margin and quietly turns
this into a services business with a software attach.

Charged separately it does three things: converts the riskiest COGS line into revenue, maps onto the
installation line the customer already pays their integrator, and acts as a qualification filter — a chain
that will not pay ₹20,000 to onboard a site will not renew at ₹1.14 lakh a year.

### The margin warning

Stacking every cost line the research surfaced against a ₹475 blended ARPU — 25% channel, cloud COGS,
edge-hardware amortisation, onboarding, support, notifications — leaves roughly **₹44/camera/month, a 9%
contribution margin**, before R&D and before re-indexing. Each research dimension reported a healthy
margin because each priced only its own slice.

Four levers fix it, and they are all commercial rather than technical: price the appliance at cost-plus
(it is currently specced *below BOM*), charge onboarding one-time, cut included retention from 12 months
to 3 and sell depth as a properly-priced ladder, and go direct for the first ten logos instead of through a
25% channel. Together those restore ~74%.

**Instrument engineer-hours-per-site from the very first deployment.** Until one paid pilot is sold with
onboarding invoiced separately, the gross margin of this business is unknown to within a factor of five.
It is a spreadsheet column, not an engineering project.

---

## Beachhead

**PoC and first paid pilot: factory / warehouse** (Harsh's call, and the research agrees it is the
runner-up-by-a-hair with a decisive advantage). Three reasons it wins for the PoC specifically:

1. **The legal basis is clean.** DPDP s.7(i) makes processing for "purposes of employment and safeguarding
   the employer from loss or liability" a **consent-free legitimate use**. Factory + employees is the one
   vertical where the face recognition Harsh asked for is lawful without a consent infrastructure we cannot
   build in four weeks. Retail customers, hospital patients and the general public have **no** equivalent
   basis — DPDP has no legitimate-interests ground.
2. **The buyer has budget and a statutory mandate.** Safety Officer / Plant Head, Factories Act exposure,
   and a quantifiable fine to price against.
3. **The detections are the reliable ones.** PPE, zone entry, dwell, loading-dock activity — spatial and
   temporal rules over solid detector output, not open-ended scene comprehension.

**Second vertical, month 6–9: multi-site retail/QSR chains** of 50–500 outlets, sold to VP-Operations or
Head of Loss Prevention. Same edge agent, different rule pack. Larger market, but Wobot.ai owns the
positioning — and its India entity reaching only ₹13.3 Cr while its logo wall filled with US names is a
data point about Indian willingness-to-pay, not an empty field.

**Explicitly not year one:** police and Safe City (GeM/e-tender, L1 price discovery, 18–36 month cycles,
incumbents with 20-year relationships), schools (DPDP s.9(3) is an *absolute* ban on tracking children
with no consent cure and a ₹200 crore ceiling), and BFSI (6–12 month security reviews — year three).

---

## Channel

1. **Months 0–9: founder-led direct** to 8–10 accounts. System integrators cannot sell this — they earn on
   box margin and AMC, have no software demo capability, and will bury a ₹9,494/month SaaS line inside an
   AMC renewal where it dies.
2. **Months 3+: fulfilment partners, not resellers.** Pay the customer's *incumbent* CCTV/AMC vendor
   ₹3,000–5,000 per site for installation, DVR credential recovery and cabling, plus a 10% recurring
   referral. They do the truck rolls in tier-2/3 cities; we keep the customer, the data and the
   relationship.
3. **Months 12–18: OEM/white-label** into CP Plus (1,000+ distributors, 550+ cities, 77% hardware-dependent
   revenue and a post-IPO need for software attach), Prama or Matrix. The only realistic route to
   five-figure camera volume — but it caps realised price at ₹150–300/camera/month and commoditises us. Do
   it only after direct logos give us leverage.

---

## The one-line positioning

> **Sanjay gives the cameras you already own a memory that stands up in court.**

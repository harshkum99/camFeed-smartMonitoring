# Standing in for a DVR: public footage we can build against

Written 2026-09-08, when it became clear DVR credentials for a live site were not going to
arrive in time to unblock ingest.

## What we actually need is not "surveillance video"

Every list of "CCTV datasets" on the internet is a list of **clips**: a few seconds of a fight, a
cropped shoplifting incident, ten thousand frames of a person wearing a hard hat. Those train
detectors. They cannot exercise this product, because this product is not a detector — it is a
searchable timeline. The claims we make in a demo are timeline claims:

> *"Nobody entered the chemical store between 8pm and 6am."*
> *"Coverage was 88% — camera 4 was down from 02:10 to 02:40, so I cannot tell you about that
> half hour."*
> *"Your DVR keeps 23 days. We keep 24 months."*

None of those sentences can be produced from a folder of clips. So the bar a dataset has to clear
for us is higher and stranger than the usual one:

| Requirement | Why it is non-negotiable here |
|---|---|
| **Camera identity** | Every row in `tracks`, `zone_events` and `camera_uptime` is keyed by camera. A dataset that ships `video_0041.mp4` has thrown that away. |
| **Wall-clock timestamps** | "This morning", "after 8pm", "on Tuesday" are compiled into `ts BETWEEN …`. Relative frame numbers cannot answer them. |
| **Continuity, including its absence** | `coverage_pct` and `coverage_gaps` are the honesty machinery — the difference between "nothing happened" and "we could not see". A dataset with no gaps cannot demonstrate the feature that distinguishes us. |
| **Several cameras at the same instant** | Track & Trace, and the multi-camera count, are the same footage seen twice. |
| **Vertical match** | We sell factory/warehouse. Street crime footage demos the wrong product. |

Judged that way, the popular datasets mostly fail, and the ones that pass are not the famous ones.

## The shortlist

| Dataset | Shape | Licence | Verdict |
|---|---|---|---|
| **MEVA** — Multiview Extended Video with Activities | 328 h, 29 ground cameras, 5-minute clips in a `facility/date/hour/` tree, filenames encoding camera and start/end time | **CC BY 4.0** | **Primary.** The only public dataset that is already a DVR. |
| **NVIDIA PhysicalAI-SmartSpaces** (AI City Challenge MTMC) | Synthetic warehouses, 1080p30, dozens of cameras per scene, 3D ground truth + calibration | **CC BY 4.0** | **Secondary.** Our exact vertical, and the only source of ground truth to score our counts against. |
| **Eskişehir workplace-safety video** (Mendeley / *Data in Brief*) | 691 clips, 1920×1080 @ 24 fps, from the security cameras of a real production plant | **CC BY 4.0** | **Third.** Real factory pixels for the PPE and safe-walkway rules. |
| **WiseNET** | 6 indoor cameras, NTP-synchronised, ~1 h, people-tracking annotations | CC BY | Useful only as a small multi-camera sanity check. |
| **VIRAT 2.0** | 8.5 h, 11 outdoor scenes, static cameras | "Video Dataset Protection Agreement" | Skip. MEVA is its successor, is larger, and is properly licensed. |
| **UCF-Crime / DCSASS** | 128 h of untrimmed web video, 13 crime classes | **Research only** | **Reject — see below.** |

### The licence trap, which is the reason this document exists

UCF-Crime is what you find first. It is on Kaggle, it is enormous, it is called "CCTV", and it is
**research-use-only**. We already run a licence gate in CI to keep AGPL and non-commercial model
weights out of a product we intend to sell; letting research-only *footage* in through the side
door would be the same mistake with a worse blast radius, because it would sit underneath a demo
we give to a paying customer. It is also, on inspection, the wrong shape: uncurated web video,
no camera identity, no wall-clock time, no continuity, and a subject matter (arson, shooting,
assault) that demos a public-safety product we are not building.

The three datasets we chose are all CC BY 4.0 — commercial use permitted with attribution. That
is a deliberate constraint, not a coincidence.

## Why MEVA is the one

Everything below was verified against the public bucket, not taken from the paper.

```
drops-123-r13/2018-03-07/16/2018-03-07.16-50-00.16-55-00.admin.G329.r13.avi     143 MB
drops-123-r13/2018-03-07/16/2018-03-07.16-50-00.16-55-00.bus.G331.r13.avi       111 MB
drops-123-r13/2018-03-07/16/2018-03-07.16-50-00.16-55-00.bus.G475.r13.avi         2 MB
drops-123-r13/2018-03-07/16/2018-03-07.16-50-00.16-55-00.school.G299.r13.avi    141 MB
```

Read that filename again: **date, start time, end time, camera**. A directory hierarchy of
facility → date → hour. That is not a dataset layout; that is how a DVR exports footage, which is
why it works for us — the importer written for MEVA is most of the importer we will point at a
customer's real export.

Three properties, each checked:

**About 24 cameras record every window, and they start within seconds of each other.** Hour 10 of
7 March holds 313 clips across 13 five-minute windows. The clips in one window start at 16-50-00,
16-50-01, 16-50-05, 16-50-06, 16-50-07 — roughly synchronised, not exactly, which is the same
few-second skew a real site has and which any multi-camera claim we make has to survive.

**Collection came in blocks, and the holes between them are hours long.** On 7 March there is
continuous footage from 09:10 to 12:00 and from 16:50 to 17:40, and *nothing* from 12:00 to 16:50
or after 17:40. That is a genuine, unfabricated coverage gap of four hours and fifty minutes
across every camera at once — which is precisely the shape our `coverage_gaps` machinery exists
to report, and the one thing we cannot honestly synthesise. Ask this footage "did anyone enter
after 6pm" and the only correct answer is *we could not see*.

**The cameras are not all the same grade, and this is a feature.** `admin.G329` is 1920×1072 @
30 fps. `bus.G475` and the two hospital cameras are **352×240** — the small files are not
truncated, they are simply low-resolution cameras running the full five minutes. So MEVA arrives
pre-loaded with cameras that physically cannot support face recognition, which is exactly the
finding `smartcam-survey` is built to deliver and exactly the awkward conversation a salesperson
needs to have rehearsed before a customer has it for them.

Access needs no account and no credentials — it is on the AWS Open Data registry, so an
unsigned request works:

```bash
aws s3 sync s3://mevadata-public-01/drops-123-r13/2018-03-07 ./footage --no-sign-request
```

One day of one facility is a few tens of GB; a single five-minute window across all cameras is
about 1.2 GB and is enough to develop against.

Annotations (KPF format, ~184 h of the video) live separately at
`gitlab.kitware.com/meva/meva-data-repo` — worth pulling for scoring our own detector, not needed
to run the pipeline.

### What MEVA cannot do for us

It is a staged collection on a US campus in 2018: parking lots, a bus stop, a school entrance,
roughly 100 actors. There is not a forklift or a hard hat in it. So MEVA proves the *machinery* —
timeline, coverage, multi-camera, plain-English queries over real footage — and the other two
datasets carry the *vertical*. Nothing in a customer demo should be built on MEVA pixels alone.

## Division of labour

| Capability we need to show | Source |
|---|---|
| Continuous timeline, real timestamps, honest coverage gaps | MEVA |
| Same person on two cameras (Track & Trace) | MEVA, then PhysicalAI warehouse scenes |
| Counting accuracy scored against truth | PhysicalAI (3D ground truth + calibration) |
| Warehouse geometry, racking, forklifts, aisles | PhysicalAI |
| PPE violations, safe-walkway violations, panel covers, overloaded forklifts | Eskişehir |
| Number plates, enrolled faces | **None of them.** Needs real footage. |

The last row is the honest gap. Face recognition needs ≥64 px inter-pupillary distance and ANPR
needs a plate-grade camera; no public dataset gives us both those pixels *and* a lawful basis to
enrol identities from them. Those two features stay behind real footage from a real site with a
real DPDP s.7(i) notice, and we should not fake them in a demo.

## Which means we still want the customer's footage

A public dataset unblocks engineering. It does not replace an exported DVR reel from an actual
Indian factory, because that is where the unglamorous truths live: a 4-minute clock drift, H.264+
variable GOP, an 8-second keyframe interval, IR switching at dusk, a camera pointed at a wall
since 2019. Every one of those has already shaped the survey tool. So the ask stands, just with
the urgency removed from it — a few hours of export beats live RTSP credentials for now, and
carries far less risk to the customer's own recording.

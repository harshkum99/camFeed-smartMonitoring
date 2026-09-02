"""Channel grading and edge-box sizing.

This is the commercially load-bearing part of the survey. It answers the question every customer
asks and no vendor answers honestly: *which of my cameras can actually do this?*

Grading a customer's estate before quoting converts our single largest unmodelled risk into a
pre-sales artefact we charge for. It is also the only defence against the failure mode where we
promise face recognition on a 4 mm ceiling dome and lose the account in the pilot review.

All thresholds carry their justification. Where a number is a judgement call rather than a
sourced figure, it says so.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import StrEnum


class Grade(StrEnum):
    """What a channel can support. Ordered best to worst."""

    RECOGNITION = "recognition"   # face matching viable: enough pixels between the eyes
    ANPR = "anpr"                 # plate reading viable
    DETECTION = "detection"       # person/vehicle/zone/PPE rules — the bread and butter
    DEGRADED = "degraded"         # works, but only off the main stream: ~2.24x the box budget
    UNSERVABLE = "unservable"     # cannot be used; say so before the contract, not after


# --- Thresholds -------------------------------------------------------------------------
# Detect stream floor. Frigate's own reference detect stream is 1280x720 @ 5 fps with an I-frame
# interval of 5 (a 1-second GOP), and it notes objects should ideally fit within 320x320.
MIN_DETECT_WIDTH = 640
MIN_DETECT_HEIGHT = 360
MIN_DETECT_FPS = 4.0

# GOP is the most overlooked requirement in the whole survey. Decode cannot produce a usable
# frame until the next keyframe, so a long GOP puts a hard floor under alert latency no matter
# how fast the detector is. Indian DVR sub streams commonly ship 50-100 frame GOPs because they
# are tuned for bandwidth, not latency — that alone can add 1.5-3s.
GOOD_GOP_MS = 1_000
MAX_DETECT_GOP_MS = 2_000

# Face recognition. OSAC guidance is 64-128 px between eye centres; NEC recommends 70+ (100
# ideal). Below ~28 px face width, accuracy collapses. There is no upscaling remedy: generative
# super-resolution invents detail, it does not recover it.
MIN_FACE_IPD_PX = 64.0
MIN_FACE_DETECT_IPD_PX = 24.0     # enough to detect a face, nowhere near enough to identify one

# ANPR. Published Indian results cluster at 95-99% for clean near-frontal four-wheeler plates and
# fall hard for two-wheelers. Plate width is the governing dimension.
MIN_PLATE_WIDTH_PX = 90.0

# Decoding a 1080p main stream instead of a 640x360 sub stream costs about 2.24x the shared
# memory and a comparable multiple of decode. Both failure modes arrive together.
DEGRADED_BUDGET_MULTIPLIER = 2.24


@dataclass
class ChannelProbe:
    """What the probe measured on one channel. All fields optional: a channel we could not
    connect to still has to produce a graded row, because 'unservable' is a finding."""

    channel: int
    reachable: bool = False
    width: int | None = None
    height: int | None = None
    fps: float | None = None
    codec: str | None = None
    gop_ms: int | None = None
    stream: str = "sub"                 # which stream these numbers came from
    has_substream: bool = True
    #: Median inter-pupillary distance observed on sampled frames, when a face pass was run.
    face_ipd_px: float | None = None
    #: Median plate width observed, when an ANPR pass was run.
    plate_width_px: float | None = None
    lens: str = "rectilinear"           # rectilinear | fisheye | ptz
    error: str | None = None


@dataclass
class ChannelGrade:
    channel: int
    grade: Grade
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: How much of an edge box this channel consumes, relative to a clean sub-stream channel.
    budget_units: float = 1.0


def grade_channel(p: ChannelProbe) -> ChannelGrade:
    """Grade one channel. Conservative by construction: we would rather under-promise here than
    discover the truth in front of a customer."""
    reasons: list[str] = []
    warnings: list[str] = []

    if not p.reachable:
        return ChannelGrade(
            p.channel, Grade.UNSERVABLE,
            reasons=[p.error or "could not connect to any known stream URL"],
            budget_units=0.0,
        )

    if not p.width or not p.height or not p.fps:
        return ChannelGrade(
            p.channel, Grade.UNSERVABLE,
            reasons=["stream connected but produced no decodable video"],
            budget_units=0.0,
        )

    budget = 1.0
    # Tracked separately from `budget` because a fisheye also costs extra budget, but for a
    # different reason and without the same commercial meaning.
    main_fallback = False

    if not p.has_substream or p.stream == "main":
        main_fallback = True
        budget = DEGRADED_BUDGET_MULTIPLIER
        warnings.append(
            "no usable sub stream; detection must decode the main stream, which costs about "
            f"{DEGRADED_BUDGET_MULTIPLIER:g}x the box budget"
        )

    # Hard floors first. Failing these means the channel cannot carry detection at all.
    too_small = p.width < MIN_DETECT_WIDTH or p.height < MIN_DETECT_HEIGHT
    too_slow = p.fps < MIN_DETECT_FPS
    if too_small and p.stream != "main":
        # A tiny sub stream is not fatal if the main stream can carry detection instead — but we
        # have not probed the main stream here, so this is a plan, not a measurement. Say so.
        warnings.append(
            f"sub stream is only {p.width}x{p.height}, below the "
            f"{MIN_DETECT_WIDTH}x{MIN_DETECT_HEIGHT} floor, so detection must use the main "
            f"stream instead. The main stream was not probed — confirm it before quoting"
        )
        main_fallback = True
        budget = DEGRADED_BUDGET_MULTIPLIER
        too_small = False
    if too_small or too_slow:
        why = []
        if too_small:
            why.append(f"{p.width}x{p.height} is below the {MIN_DETECT_WIDTH}x"
                       f"{MIN_DETECT_HEIGHT} detection floor")
        if too_slow:
            why.append(f"{p.fps:.1f} fps is below the {MIN_DETECT_FPS:g} fps detection floor")
        return ChannelGrade(p.channel, Grade.UNSERVABLE, reasons=why,
                            warnings=warnings, budget_units=0.0)

    # GOP governs alert latency, not detection accuracy, so a long GOP degrades rather than
    # disqualifies. It is worth flagging loudly because it is usually fixable in the recorder's
    # sub-stream settings — and only the sub stream, never the main stream.
    if p.gop_ms is not None:
        if p.gop_ms > MAX_DETECT_GOP_MS:
            warnings.append(
                f"keyframe interval is {p.gop_ms} ms; alerts on this camera will lag by roughly "
                f"that much. Shortening the SUB stream's I-frame interval to ~1 s fixes it"
            )
        elif p.gop_ms > GOOD_GOP_MS:
            warnings.append(f"keyframe interval {p.gop_ms} ms is acceptable but not ideal")

    if p.lens == "fisheye":
        warnings.append(
            "fisheye lens: person detection is roughly a coin flip on raw fisheye frames. "
            "We dewarp to virtual rectilinear views, which costs extra budget"
        )
        budget *= 1.5
    elif p.lens == "ptz":
        warnings.append(
            "PTZ camera: rules bind to named preset positions and are suspended whenever the "
            "camera is off-preset or moving. Off-preset time is reported as a coverage gap"
        )

    if p.codec and p.codec.lower() in {"hevc", "h265"}:
        warnings.append("H.265: verify the edge box can hardware-decode it (Intel gen 8+)")

    # Now the best grade this channel can carry.
    if p.face_ipd_px is not None and p.face_ipd_px >= MIN_FACE_IPD_PX:
        reasons.append(
            f"median {p.face_ipd_px:.0f} px between eye centres, at or above the "
            f"{MIN_FACE_IPD_PX:g} px recognition floor"
        )
        return ChannelGrade(p.channel, Grade.RECOGNITION, reasons, warnings, budget)

    if p.plate_width_px is not None and p.plate_width_px >= MIN_PLATE_WIDTH_PX:
        reasons.append(f"median plate width {p.plate_width_px:.0f} px supports plate reading")
        return ChannelGrade(p.channel, Grade.ANPR, reasons, warnings, budget)

    if p.face_ipd_px is not None and p.face_ipd_px < MIN_FACE_IPD_PX:
        reasons.append(
            f"median {p.face_ipd_px:.0f} px between eye centres is below the "
            f"{MIN_FACE_IPD_PX:g} px floor, so this camera can detect people but cannot "
            f"identify them"
        )
    else:
        reasons.append(f"{p.width}x{p.height} @ {p.fps:.0f} fps supports detection and zone rules")

    # DEGRADED means exactly one thing: this channel costs roughly double because detection has
    # to decode its main stream. It is a capacity statement, so it must track the capacity cost —
    # a channel billed at 2.24x cannot be reported as a clean detection channel.
    if main_fallback:
        return ChannelGrade(p.channel, Grade.DEGRADED, reasons, warnings, budget)
    return ChannelGrade(p.channel, Grade.DETECTION, reasons, warnings, budget)


# --- Edge box sizing --------------------------------------------------------------------
# The research produced four different camera counts for the same N100-class hardware (8, 8-16,
# 30, and "up to 30"). None were derived from the two resources that actually run out first.
# These are, so that a SKU sheet is arithmetic rather than marketing.


@dataclass(frozen=True)
class BoxSpec:
    """An edge box's real capacity limits."""

    name: str
    #: Detector throughput, inferences/second, for the model we actually ship.
    inferences_per_sec: float
    ram_gb: float
    #: Shared memory available to the decode pipeline. Docker's default is 64 MB, which is why
    #: a 40-camera site fails with an opaque `Bus error` rather than degrading gracefully.
    shm_gb: float
    cpu_cores: int


BOXES: dict[str, BoxSpec] = {
    # Intel N100/N150: one detector instance, ~15 ms MobileNetV2 / ~30 ms YOLOv9-s-320 measured.
    "edge-s": BoxSpec("Sanjay Edge S (Intel N150)", inferences_per_sec=33.3,
                      ram_gb=16, shm_gb=4, cpu_cores=4),
    # Core Ultra: NPU + iGPU detector pool.
    "edge-m": BoxSpec("Sanjay Edge M (Core Ultra 5)", inferences_per_sec=90.0,
                      ram_gb=32, shm_gb=8, cpu_cores=12),
    # Same, plus a Hailo-8 M.2 (~7 ms YOLOv6n = ~143 inf/s on its own).
    "edge-m-hailo": BoxSpec("Sanjay Edge M + Hailo-8", inferences_per_sec=200.0,
                            ram_gb=32, shm_gb=8, cpu_cores=12),
}


def shm_bytes_per_camera(width: int, height: int) -> int:
    """Frigate's documented shared-memory requirement for one camera's frame buffers.

    (w * h * 1.5 * 20) + 270480 bytes. At 1080p that is ~59.6 MB per camera, so 40 cameras need
    ~2.4 GB — against a Docker default of 64 MB. The symptom is a `Bus error` on startup, not a
    slow system, which is why this must be computed rather than guessed.
    """
    return int(width * height * 1.5 * 20 + 270_480)


@dataclass
class BoxSizing:
    box: str
    cameras: int
    detect_fps: float
    motion_duty: float
    inferences_required: float
    inferences_available: float
    shm_required_gb: float
    tracker_cores_required: float
    fits: bool
    limits: list[str] = field(default_factory=list)
    headroom_pct: float = 0.0


def size_box(
    grades: list[ChannelGrade],
    probes: dict[int, ChannelProbe],
    *,
    box: str = "edge-s",
    detect_fps: float = 5.0,
    motion_duty: float = 0.30,
    tracker_ms_per_frame: float = 32.6,
    shm_gb_available: float | None = None,
) -> BoxSizing:
    """Can this box carry this estate? Checks the three resources that actually run out.

    `motion_duty` is the fraction of the day a camera sees motion. 0.30 is a reasonable indoor
    default; a busy retail floor or a windy outdoor yard runs near 1.0 all day, and that single
    parameter is the difference between 20 cameras and 11 on the same hardware.

    `tracker_ms_per_frame` defaults to BoT-SORT-ReID, which needs roughly a full CPU core per
    stream. This is the line item that turns a cheap box into an expensive one and it is
    under-budgeted in almost every plan.

    `shm_gb_available` overrides the box's configured shared memory. Pass the container's
    *actual* value to catch the Docker-default case (64 MB = 0.0625) before deployment, rather
    than on site at 2am.
    """
    spec = BOXES[box]
    shm_budget = spec.shm_gb if shm_gb_available is None else shm_gb_available
    servable = [g for g in grades if g.grade is not Grade.UNSERVABLE]

    # Inference load, weighted by each channel's budget cost (a main-stream fallback or a
    # dewarped fisheye costs more than a clean sub stream).
    weighted = sum(g.budget_units for g in servable)
    required = weighted * detect_fps * motion_duty
    available = spec.inferences_per_sec

    # Shared memory, at each channel's actual decode resolution.
    shm_bytes = 0
    for g in servable:
        p = probes.get(g.channel)
        if p and p.width and p.height:
            shm_bytes += shm_bytes_per_camera(p.width, p.height)
        else:
            shm_bytes += shm_bytes_per_camera(1280, 720)
    shm_bytes += 40 * 1024 * 1024  # Frigate's documented logging allowance
    shm_gb = shm_bytes / 1024**3

    cores = len(servable) * (detect_fps * motion_duty) * (tracker_ms_per_frame / 1000.0)

    limits: list[str] = []
    if required > available:
        limits.append(
            f"detector oversubscribed {required / available:.1f}x "
            f"({required:.0f} inf/s needed, {available:.0f} available). "
            f"Fit is {max_cameras(box, detect_fps, motion_duty)} cameras at this motion duty, "
            f"or add a Hailo-8, or ship a second box"
        )
    if shm_gb > shm_budget:
        limits.append(
            f"shared memory short: {shm_gb:.2f} GB needed, {shm_budget:.2f} GB configured. "
            f"Set shm_size to at least {math.ceil(shm_gb * 1.15 * 1024)}m in the compose file, "
            f"or the container dies on startup with an opaque Bus error rather than running slow"
        )
    if cores > spec.cpu_cores:
        limits.append(
            f"tracker needs ~{cores:.1f} CPU cores, box has {spec.cpu_cores}. "
            f"Fall back to ByteTrack on cameras where identity does not matter"
        )

    return BoxSizing(
        box=spec.name,
        cameras=len(servable),
        detect_fps=detect_fps,
        motion_duty=motion_duty,
        inferences_required=required,
        inferences_available=available,
        shm_required_gb=round(shm_gb, 2),
        tracker_cores_required=round(cores, 1),
        fits=not limits,
        limits=limits,
        headroom_pct=round(100 * (1 - required / available), 1) if available else 0.0,
    )


def max_cameras(box: str, detect_fps: float = 5.0, motion_duty: float = 0.30) -> int:
    """How many clean sub-stream channels this box carries. Arithmetic, not a marketing tier."""
    spec = BOXES[box]
    if detect_fps <= 0 or motion_duty <= 0:
        return 0
    return math.floor(spec.inferences_per_sec / (detect_fps * motion_duty))

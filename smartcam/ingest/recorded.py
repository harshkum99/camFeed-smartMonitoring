"""Reading a folder of recorded footage as though it were a DVR.

A customer hands us an export — or, until they do, we point this at MEVA. Either way the input is
the same shape: a pile of files whose only reliable metadata is their own names. This module turns
that pile into a timeline, which is the only thing the rest of the product knows how to consume.

Three decisions here are worth more than the code:

**Filenames are evidence, not decoration.** Recorder exports encode camera and wall-clock time in
the filename because the container often does not. `creation_time` in an mp4 written by a Chinese
DVR is routinely the time the *file was copied to the USB stick*, which is hours or days late. So
a filename we can parse always wins over container metadata, and container metadata always wins
over mtime — and when we are down to mtime we say so rather than quietly asserting a timestamp.

**Declared duration and measured duration are different numbers, and the difference is the
product.** A clip named `16-50-00.16-55-00` claims five minutes. If ffprobe finds 40 seconds in
it, then 4m20s of that window was never recorded. Every other pipeline treats such a file as
corrupt input to be dropped. We treat it as a *coverage gap*, because "the camera was down" and
"nothing happened" are the two answers this product exists to keep apart. Dropping the file
silently converts one into the other.

**Naive timestamps need a declared timezone or they are worse than nothing.** No recorder export
carries a UTC offset. Guessing it wrong shifts every answer by hours, and shifts it invisibly:
"anyone in the chemical store after 8pm" quietly becomes a question about the afternoon. So the
timezone is an argument, it is recorded on every clip, and it appears in the import report.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from smartcam.survey.probe import CompletedRun, Runner, _run

#: Containers a recorder plausibly emits. `.dav` is Dahua's own wrapper; ffmpeg reads it.
VIDEO_SUFFIXES = frozenset({".mp4", ".avi", ".mkv", ".dav", ".ts", ".mov", ".asf", ".264"})

#: Below this, a clip is too short to have been a real recording window — almost always a
#: zero-length placeholder the export left behind.
MIN_USEFUL_SECONDS = 1.0

#: Two clips for one camera that overlap by less than this are the ordinary rounding of a
#: recorder closing one file and opening the next. More than this means something is wrong.
OVERLAP_TOLERANCE_S = 2.0


class ConventionError(ValueError):
    """The filename did not match the convention it was asserted to follow."""


# --- filename conventions ---------------------------------------------------------------------


@dataclass(frozen=True)
class ParsedName:
    camera_key: str
    starts_at: datetime          # naive; localised by the caller
    declared_seconds: float | None
    convention: str


def _meva(path: Path) -> ParsedName | None:
    """`2018-03-07.16-50-00.16-55-00.admin.G329.r13.avi`

    Date, start, end, location, camera. The richest convention we have, and the reason MEVA is
    the dataset we build against: it is the only public collection already shaped like an export.
    """
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2})\.(\d{2}-\d{2}-\d{2})\.(\d{2}-\d{2}-\d{2})\."
        r"(.+?)\.(G\d+)\.",
        path.name,
    )
    if not m:
        return None
    day, start_s, end_s, location, cam = m.groups()
    start = datetime.strptime(f"{day} {start_s}", "%Y-%m-%d %H-%M-%S")
    end = datetime.strptime(f"{day} {end_s}", "%Y-%m-%d %H-%M-%S")
    if end <= start:                      # a window that crosses midnight
        end += timedelta(days=1)
    return ParsedName(f"{location}.{cam}", start, (end - start).total_seconds(), "meva")


def _hikvision(path: Path) -> ParsedName | None:
    """`ch01_20260908103000.mp4`, and the `_00000000123000000.mp4` variant its tools emit.

    Hikvision states the start only. Duration comes from the file itself, which is exactly the
    case where declared and measured cannot be compared — see `Clip.trusted_end`.
    """
    m = re.match(r"^ch0*(\d+)[_-](\d{14})", path.name, re.IGNORECASE)
    if not m:
        return None
    channel, stamp = m.groups()
    return ParsedName(
        f"ch{int(channel):02d}", datetime.strptime(stamp, "%Y%m%d%H%M%S"), None, "hikvision",
    )


def _dahua(path: Path) -> ParsedName | None:
    """`.../<host>/2026-09-08/001/dav/10/10.03.00-10.08.00[M][0@0][0].dav`

    Dahua splits the date into the directory tree and keeps only the clock in the filename, so
    this convention has to read the path, not just the name. The `001` component is the channel.
    """
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{2})-(\d{2})\.(\d{2})\.(\d{2})", path.name)
    if not m:
        return None
    day = next((p for p in path.parts[::-1] if re.fullmatch(r"\d{4}-\d{2}-\d{2}", p)), None)
    if day is None:
        return None
    channel = next((p for p in path.parts[::-1] if re.fullmatch(r"\d{3}", p)), "001")
    h1, m1, s1, h2, m2, s2 = (int(x) for x in m.groups())
    base = datetime.strptime(day, "%Y-%m-%d")
    start = base + timedelta(hours=h1, minutes=m1, seconds=s1)
    end = base + timedelta(hours=h2, minutes=m2, seconds=s2)
    if end <= start:
        end += timedelta(days=1)
    return ParsedName(
        f"ch{int(channel):02d}", start, (end - start).total_seconds(), "dahua",
    )


def _iso_generic(path: Path) -> ParsedName | None:
    """`cam-gate_2026-09-08T10-30-00.mp4` and friends — what our own exports should look like.

    Deliberately last of the real conventions: it is permissive enough to shadow the vendor ones
    if it ran first.
    """
    m = re.match(
        r"^(?P<cam>[A-Za-z0-9][\w.-]*?)[_ ]"
        r"(?P<d>\d{4}-\d{2}-\d{2})[T_ ](?P<t>\d{2}[-:]\d{2}[-:]\d{2})",
        path.name,
    )
    if not m:
        return None
    clock = m.group("t").replace(":", "-")
    return ParsedName(
        m.group("cam"),
        datetime.strptime(f"{m.group('d')} {clock}", "%Y-%m-%d %H-%M-%S"),
        None,
        "iso",
    )


#: Order matters: most specific first, permissive last.
CONVENTIONS: dict[str, Callable[[Path], ParsedName | None]] = {
    "meva": _meva,
    "hikvision": _hikvision,
    "dahua": _dahua,
    "iso": _iso_generic,
}


def parse_name(path: Path, *, convention: str | None = None) -> ParsedName | None:
    """Identify camera and start time from a path. Returns None if nothing matches.

    Passing `convention` asserts one and raises rather than silently falling through to another —
    on a large import, a convention that matches 3% of files and misreads the rest is far more
    damaging than one that matches none.
    """
    if convention is not None:
        try:
            parser = CONVENTIONS[convention]
        except KeyError:
            raise ConventionError(
                f"unknown convention {convention!r}; known: {', '.join(CONVENTIONS)}"
            ) from None
        got = parser(path)
        if got is None:
            raise ConventionError(f"{path.name} does not match the {convention} convention")
        return got
    for parser in CONVENTIONS.values():
        got = parser(path)
        if got is not None:
            return got
    return None


# --- clips ------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Clip:
    path: Path
    camera_key: str
    starts_at: datetime                  # timezone-aware, UTC
    declared_seconds: float | None       # from the filename
    measured_seconds: float | None       # from ffprobe
    size_bytes: int
    convention: str                      # or "mtime" / "container" when the name told us nothing
    error: str | None = None

    @property
    def trusted_end(self) -> datetime:
        """Where this clip's footage actually stops.

        The measured duration, when we have it — a clip that claims five minutes and holds forty
        seconds covers forty seconds. Falling back to the declared window would assert coverage
        for four minutes of footage nobody ever recorded.
        """
        seconds = self.measured_seconds if self.measured_seconds is not None else (
            self.declared_seconds or 0.0
        )
        return self.starts_at + timedelta(seconds=seconds)

    @property
    def declared_end(self) -> datetime | None:
        if self.declared_seconds is None:
            return None
        return self.starts_at + timedelta(seconds=self.declared_seconds)

    @property
    def short_by_seconds(self) -> float:
        """How much of the declared window has no footage in it. Zero when unmeasurable."""
        if self.declared_seconds is None or self.measured_seconds is None:
            return 0.0
        return max(0.0, self.declared_seconds - self.measured_seconds)

    @property
    def usable(self) -> bool:
        return self.error is None and (self.measured_seconds or 0.0) >= MIN_USEFUL_SECONDS


def measure_seconds(path: Path, *, timeout: float = 20.0, runner: Runner = _run) -> tuple[
    float | None, str | None
]:
    """Duration of a file's video stream, and the container's own idea of when it started.

    Asks for the *stream* duration rather than the container's, because a recorder that appends
    to a file across a reboot leaves a container duration covering the wall-clock span while the
    video stream holds only what was recorded.
    """
    run = runner(
        [
            "ffprobe", "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=duration:format=duration,start_time:format_tags=creation_time",
            "-of", "json", str(path),
        ],
        timeout,
    )
    if run.returncode != 0:
        return None, (run.stderr.strip().splitlines() or ["unreadable"])[-1]
    try:
        data = json.loads(run.stdout)
    except json.JSONDecodeError:
        return None, "ffprobe output unparseable"

    created = (data.get("format", {}).get("tags") or {}).get("creation_time")
    streams = data.get("streams") or []
    for candidate in (
        streams[0].get("duration") if streams else None,
        data.get("format", {}).get("duration"),
    ):
        try:
            value = float(candidate)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            continue
        if value > 0:
            return value, created
    return None, created


def _container_start(created: str | None) -> datetime | None:
    if not created:
        return None
    try:
        return datetime.fromisoformat(created.replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def scan(
    root: Path,
    *,
    tz: str = "UTC",
    convention: str | None = None,
    camera_map: dict[str, str] | None = None,
    measure: bool = True,
    runner: Runner = _run,
) -> list[Clip]:
    """Walk a folder of recorded footage and return what it holds, in wall-clock order.

    `tz` names the timezone the *recorder* was set to. It is not cosmetic: every question the
    product answers is a question about local time, and a wrong offset moves every answer
    silently. `camera_map` renames the key a convention produced (`bus.G331`) to our own camera
    id, and is the seam where an import binds to a real site.
    """
    zone = ZoneInfo(tz)
    mapping = camera_map or {}
    clips: list[Clip] = []

    for path in sorted(p for p in root.rglob("*") if p.suffix.lower() in VIDEO_SUFFIXES):
        parsed = parse_name(path, convention=convention)
        measured, created_or_error = (None, None)
        if measure:
            measured, created_or_error = measure_seconds(path, runner=runner)

        error = None
        if parsed is not None:
            starts_at = parsed.starts_at.replace(tzinfo=zone).astimezone(UTC)
            camera_key, declared, source = (
                parsed.camera_key, parsed.declared_seconds, parsed.convention,
            )
        else:
            # Nothing in the name. Fall back, and record which crutch we used so the report can
            # say how much of the timeline rests on a guess.
            container = _container_start(created_or_error) if measured is not None else None
            if container is not None:
                starts_at, source = container, "container"
            else:
                starts_at = datetime.fromtimestamp(path.stat().st_mtime, UTC)
                source = "mtime"
            camera_key, declared = path.parent.name or "unknown", None

        if measured is None and measure:
            error = created_or_error if source in {"mtime", "container"} else created_or_error
            error = error or "duration could not be measured"

        clips.append(
            Clip(
                path=path,
                camera_key=mapping.get(camera_key, camera_key),
                starts_at=starts_at,
                declared_seconds=declared,
                measured_seconds=measured,
                size_bytes=path.stat().st_size,
                convention=source,
                error=error,
            )
        )

    clips.sort(key=lambda c: (c.camera_key, c.starts_at))
    return clips


# --- the timeline -----------------------------------------------------------------------------


@dataclass(frozen=True)
class Gap:
    """A stretch for which we have no footage, and therefore cannot answer questions."""

    camera_key: str
    starts_at: datetime
    ends_at: datetime
    kind: str        # "before" | "between" | "within" | "after" | "unreadable"
    exact: bool      # False when we know the loss but not where inside the window it fell

    @property
    def seconds(self) -> float:
        return (self.ends_at - self.starts_at).total_seconds()


@dataclass
class Timeline:
    clips: list[Clip]
    gaps: list[Gap] = field(default_factory=list)
    overlaps: list[tuple[str, datetime, float]] = field(default_factory=list)
    #: The span the import claims to cover, when the caller named one. Held here rather than
    #: recomputed, so that the gaps and the coverage percentage are measured against the same
    #: ruler — asking about an eight-hour shift and scoring against the one hour of footage that
    #: turned up would report a blind night as fully covered.
    claimed_window: tuple[datetime, datetime] | None = None

    @property
    def cameras(self) -> list[str]:
        return sorted({c.camera_key for c in self.clips})

    @property
    def window(self) -> tuple[datetime, datetime] | None:
        if self.claimed_window is not None:
            return self.claimed_window
        if not self.clips:
            return None
        return (
            min(c.starts_at for c in self.clips),
            max(max(c.trusted_end, c.declared_end or c.trusted_end) for c in self.clips),
        )

    @property
    def recorded_seconds(self) -> float:
        return sum(c.measured_seconds or 0.0 for c in self.clips if c.usable)

    def coverage_pct(self) -> float:
        """Footage we hold, over the span it claims to cover, across all cameras.

        Deliberately computed from measured durations rather than from clip count. A folder of
        1,000 files of which a third are truncated is not 100% covered, and reporting it as such
        is the exact failure the honesty machinery exists to prevent.
        """
        span = self.window
        if span is None:
            return 0.0
        total = (span[1] - span[0]).total_seconds() * max(1, len(self.cameras))
        if total <= 0:
            return 0.0
        return round(min(100.0, 100.0 * self.recorded_seconds / total), 1)


def build_timeline(
    clips: Iterable[Clip], *, window: tuple[datetime, datetime] | None = None
) -> Timeline:
    """Group clips per camera and find every hole between them, inside them, and around them.

    `window` is the span the import is *claiming* to cover — a shift, a night, the whole export.
    Left unset it is taken from the clips themselves, which makes the site-wide span the standard
    every camera is held to. That matters: a camera whose footage stops four hours before its
    neighbours has a four-hour hole, and a per-camera view that only looks between consecutive
    clips will never see it. This is the same trailing-gap blindness that `coverage_gaps` in
    `003_partitions_and_coverage.sql` had to grow a third UNION arm to fix; the two must agree,
    or the importer and the database will report different coverage for the same footage.
    """
    timeline = Timeline(
        clips=sorted(clips, key=lambda c: (c.camera_key, c.starts_at)), claimed_window=window,
    )
    span = timeline.window

    by_camera: dict[str, list[Clip]] = {}
    for clip in timeline.clips:
        by_camera.setdefault(clip.camera_key, []).append(clip)

    for camera, series in by_camera.items():
        previous: Clip | None = None
        first = series[0].starts_at
        if span is not None and (first - span[0]).total_seconds() > MIN_USEFUL_SECONDS:
            timeline.gaps.append(Gap(camera, span[0], first, "before", exact=True))
        for clip in series:
            if clip.error is not None or clip.measured_seconds is None:
                # We have a file and a window but cannot read it. That is a gap we can locate
                # precisely, which makes it more useful than a truncation.
                end = clip.declared_end or clip.starts_at
                if end > clip.starts_at:
                    timeline.gaps.append(
                        Gap(camera, clip.starts_at, end, "unreadable", exact=True)
                    )
            elif clip.short_by_seconds > MIN_USEFUL_SECONDS:
                # Footage is missing from inside the declared window. We know how much; we do not
                # know whether the camera dropped at the start, the middle or the end, so this
                # gap is attributed to the tail and flagged inexact rather than pretending.
                declared_end = clip.declared_end
                assert declared_end is not None
                timeline.gaps.append(
                    Gap(camera, clip.trusted_end, declared_end, "within", exact=False)
                )

            if previous is not None:
                edge = max(previous.trusted_end, previous.declared_end or previous.trusted_end)
                delta = (clip.starts_at - edge).total_seconds()
                if delta > MIN_USEFUL_SECONDS:
                    timeline.gaps.append(Gap(camera, edge, clip.starts_at, "between", exact=True))
                elif delta < -OVERLAP_TOLERANCE_S:
                    timeline.overlaps.append((camera, clip.starts_at, -delta))
            previous = clip

        last = series[-1]
        edge = max(last.trusted_end, last.declared_end or last.trusted_end)
        if span is not None and (span[1] - edge).total_seconds() > MIN_USEFUL_SECONDS:
            timeline.gaps.append(Gap(camera, edge, span[1], "after", exact=True))

    timeline.gaps.sort(key=lambda g: (g.camera_key, g.starts_at))
    return timeline


__all__ = [
    "CONVENTIONS",
    "Clip",
    "CompletedRun",
    "ConventionError",
    "Gap",
    "ParsedName",
    "Runner",
    "Timeline",
    "build_timeline",
    "measure_seconds",
    "parse_name",
    "scan",
]

"""Stream probing: what a channel actually emits, as opposed to what the spec sheet claims.

Everything here shells out to ffprobe. That is deliberate — ffprobe handles the long tail of
malformed Indian DVR streams far better than any Python binding, and it costs us nothing to
depend on a binary that has to be on the edge box anyway.

The measured GOP is the most valuable number this module produces and the one nobody thinks to
collect. Decode cannot yield a usable frame until the next keyframe, so a 4-second GOP puts a
4-second floor under alert latency regardless of how fast the detector is. Indian sub streams
routinely ship 50-100 frame GOPs because they are tuned for bandwidth.
"""

from __future__ import annotations

import contextlib
import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from smartcam.survey.grade import ChannelProbe

#: Injectable so tests never touch a network or a subprocess.
Runner = Callable[[Sequence[str], float], "CompletedRun"]


@dataclass
class CompletedRun:
    returncode: int
    stdout: str
    stderr: str


def _run(cmd: Sequence[str], timeout: float) -> CompletedRun:
    try:
        p = subprocess.run(  # noqa: S603 - argv list, no shell
            list(cmd), capture_output=True, text=True, timeout=timeout, check=False,
        )
        return CompletedRun(p.returncode, p.stdout, p.stderr)
    except subprocess.TimeoutExpired:
        return CompletedRun(124, "", f"timed out after {timeout}s")
    except FileNotFoundError as e:
        return CompletedRun(127, "", str(e))


def ffprobe_available() -> bool:
    return shutil.which("ffprobe") is not None


def _parse_fps(rate: str | None) -> float | None:
    """ffprobe reports frame rates as rationals like '15/1' or, on broken streams, '0/0'."""
    if not rate:
        return None
    try:
        num, _, den = rate.partition("/")
        n, d = float(num), float(den or 1)
        return round(n / d, 2) if d else None
    except ValueError:
        return None


def probe_stream(
    url: str,
    channel: int,
    *,
    stream_kind: str = "sub",
    timeout: float = 12.0,
    gop_sample_frames: int = 120,
    runner: Runner = _run,
) -> ChannelProbe:
    """Probe one RTSP URL. Never raises — an unreachable channel is a finding, not an error."""
    base = [
        "ffprobe", "-v", "error",
        "-rtsp_transport", "tcp",       # UDP loses packets on congested site LANs
        "-rw_timeout", "8000000",       # microseconds; ffprobe has no RTSP reconnect
        "-select_streams", "v:0",
        "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,codec_name",
        "-of", "json", url,
    ]
    res = runner(base, timeout)
    if res.returncode != 0:
        return ChannelProbe(
            channel=channel, reachable=False, stream=stream_kind,
            error=_friendly_error(res.stderr),
        )

    try:
        streams = json.loads(res.stdout or "{}").get("streams") or []
    except json.JSONDecodeError:
        return ChannelProbe(channel=channel, reachable=False, stream=stream_kind,
                            error="ffprobe returned unparseable output")
    if not streams:
        return ChannelProbe(channel=channel, reachable=False, stream=stream_kind,
                            error="connected but the stream carries no video track")

    s = streams[0]
    fps = _parse_fps(s.get("avg_frame_rate")) or _parse_fps(s.get("r_frame_rate"))
    probe = ChannelProbe(
        channel=channel,
        reachable=True,
        width=s.get("width"),
        height=s.get("height"),
        fps=fps,
        codec=s.get("codec_name"),
        stream=stream_kind,
    )
    probe.gop_ms = measure_gop_ms(url, fps=fps, sample_frames=gop_sample_frames,
                                  timeout=timeout, runner=runner)
    return probe


def measure_gop_ms(
    url: str,
    *,
    fps: float | None,
    sample_frames: int = 120,
    timeout: float = 12.0,
    runner: Runner = _run,
) -> int | None:
    """Measure the real keyframe interval by counting frames between keyframes.

    We read a bounded number of frames and take the *median* gap rather than the mean: a stream
    that opens on a keyframe and a stream with one anomalous long gap would both skew a mean,
    and vendors with variable-GOP "smart" encoders (Hikvision H.264+, Dahua Smart H.264) produce
    exactly that shape.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-rtsp_transport", "tcp",
        "-rw_timeout", "8000000",
        "-select_streams", "v:0",
        "-show_entries", "frame=key_frame,best_effort_timestamp_time",
        "-read_intervals", f"%+#{sample_frames}",
        "-of", "csv=p=0", url,
    ]
    res = runner(cmd, timeout)
    if res.returncode != 0 or not res.stdout.strip():
        return None

    key_times: list[float] = []
    index_of_keys: list[int] = []
    for i, line in enumerate(res.stdout.strip().splitlines()):
        parts = line.split(",")
        if not parts or parts[0].strip() not in {"0", "1"}:
            continue
        if parts[0].strip() == "1":
            index_of_keys.append(i)
            if len(parts) > 1:
                with contextlib.suppress(ValueError):
                    key_times.append(float(parts[1]))

    # Prefer real timestamps; they account for a stream whose actual fps differs from what it
    # advertises, which is common on cheap encoders.
    if len(key_times) >= 2:
        gaps = [b - a for a, b in zip(key_times, key_times[1:], strict=False) if b > a]
        if gaps:
            return int(round(_median(gaps) * 1000))

    # Fall back to counting frames between keyframes.
    if len(index_of_keys) >= 2 and fps:
        gaps = [b - a for a, b in zip(index_of_keys, index_of_keys[1:], strict=False)]
        if gaps:
            return int(round(_median([g / fps for g in gaps]) * 1000))

    # Exactly one keyframe in `sample_frames` means the GOP is at least that long — which is
    # itself an important finding, not a missing measurement.
    if len(index_of_keys) == 1 and fps:
        return int(round((sample_frames / fps) * 1000))
    return None


def _median(xs: list[float]) -> float:
    ys = sorted(xs)
    n = len(ys)
    mid = n // 2
    return ys[mid] if n % 2 else (ys[mid - 1] + ys[mid]) / 2


def _friendly_error(stderr: str) -> str:
    """Turn ffprobe's stderr into something a field engineer can act on.

    Worth the effort: the difference between '401 Unauthorized' and 'connection refused' is the
    difference between a five-minute fix and a site visit, and the raw text buries it.
    """
    low = (stderr or "").lower()
    if "401" in low or "unauthorized" in low:
        return ("authentication rejected. Check credentials — and check the recorder's clock: "
                "devices using WS-UsernameToken validate our timestamp against THEIR clock, so "
                "a drifted DVR rejects correct passwords with this same error")
    if "404" in low or "not found" in low:
        return "URL path not recognised by this device; trying other vendor grammars"
    if "connection refused" in low:
        return "connection refused — RTSP may be disabled on the device"
    if "timed out" in low or "timeout" in low:
        return "timed out — host unreachable, firewalled, or the recorder is out of sessions"
    if "no route to host" in low:
        return "no route to host — check the edge box is on the camera VLAN"
    if "immediate exit requested" in low or "invalid data" in low:
        return "stream opened but produced no decodable video"
    return (stderr or "unknown probe failure").strip().splitlines()[-1][:200]

"""Turning a recording into the frames the detector sees, indexed by frame number.

ffmpeg does the decoding and the resizing and hands us raw RGB over a pipe. Measured on a 1080p
MEVA clip, that pipe delivered 632 frames at 5 fps in 91 seconds of which the detector took 90, so
decode is not where the time goes and there is no case for a Python video binding.

**Time comes from the frame index, never from pts.** MEVA's AVI files report `pts` as N/A after the
first few frames, and the first timestamp they do report is 2/30 s rather than zero. A pipeline
that trusted pts would either crash or quietly shift every track by 67 ms — or by far more on a
recorder whose timestamps wrap. The k-th frame out of `select=not(mod(n,step))` is source frame
`k * step`, and its wall-clock time is `clip start + n / fps`. The KPF ground truth indexes frames
the same way, which is what makes scoring possible at all.

**A short decode is a failed clip, not a short clip.** If ffmpeg stops early we do not know
whether the recording ends there or the file is damaged, and writing tracks plus an uptime row
for the decoded part would assert coverage for footage nobody analysed.
"""

from __future__ import annotations

import json
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

import numpy as np

from smartcam.survey.probe import Runner, _parse_fps, _run

#: Below this fraction of the expected frame count, a decode is treated as a failure.
MIN_DECODED_FRACTION = 0.9


@dataclass(frozen=True)
class VideoInfo:
    width: int
    height: int
    src_fps: float
    codec: str | None
    duration_s: float | None


class PipeProcess(Protocol):
    stdout: IO[bytes] | None
    returncode: int | None

    def wait(self, timeout: float | None = None) -> int: ...
    def terminate(self) -> None: ...
    def kill(self) -> None: ...


#: Injectable so tests feed synthetic frames without ffmpeg.
Spawner = Callable[[Sequence[str], IO[bytes]], PipeProcess]


def _popen(cmd: Sequence[str], stderr: IO[bytes]) -> PipeProcess:
    return subprocess.Popen(  # noqa: S603 - argv list, no shell
        list(cmd), stdout=subprocess.PIPE, stderr=stderr, stdin=subprocess.DEVNULL,
        bufsize=0,
    )


class DecodeError(RuntimeError):
    pass


def probe_video(path: Path, *, runner: Runner = _run, timeout: float = 20.0) -> VideoInfo:
    run = runner(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height,avg_frame_rate,r_frame_rate,codec_name,duration",
            "-of", "json", str(path),
        ],
        timeout,
    )
    if run.returncode != 0:
        raise DecodeError(f"ffprobe failed on {path.name}: {run.stderr.strip()[-200:]}")
    try:
        stream = (json.loads(run.stdout).get("streams") or [])[0]
    except (json.JSONDecodeError, IndexError) as e:
        raise DecodeError(f"{path.name} has no readable video stream") from e
    fps = _parse_fps(stream.get("avg_frame_rate")) or _parse_fps(stream.get("r_frame_rate"))
    if not fps or not stream.get("width") or not stream.get("height"):
        raise DecodeError(f"{path.name}: frame size or rate unreadable")
    try:
        duration = float(stream["duration"])
    except (KeyError, TypeError, ValueError):
        duration = None
    return VideoInfo(int(stream["width"]), int(stream["height"]), fps,
                     stream.get("codec_name"), duration)


def sample_step(src_fps: float, target_fps: float) -> int:
    """Every how many source frames to take one. 30 fps at a 5 fps target is every 6th."""
    return max(1, round(src_fps / target_fps))


def sampled_command(path: Path, step: int, size: tuple[int, int] = (640, 640),
                    start_frame: int = 0) -> list[str]:
    w, h = size
    select = f"not(mod(n\\,{step}))" if not start_frame else \
        f"gte(n\\,{start_frame})*not(mod(n-{start_frame}\\,{step}))"
    return [
        "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-i", str(path), "-map", "0:v:0", "-an", "-sn", "-dn",
        "-vf", f"select={select},scale={w}:{h}",
        "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1",
    ]


@dataclass(frozen=True)
class SampledFrame:
    n: int              # source frame index
    rgb: np.ndarray     # uint8 H x W x 3


@dataclass
class DecodeReport:
    frames: int = 0
    expected: int = 0
    returncode: int | None = None
    stopped_early_by_us: bool = False
    stderr_tail: str = ""
    last_n: int | None = None

    @property
    def ok(self) -> bool:
        if self.returncode not in (0, None) and not self.stopped_early_by_us:
            return False
        # Nothing decoded is never a success, even when nothing was expected: a zero-length
        # analysis window would otherwise write a coverage row that ends before it starts.
        if self.expected <= 0 or self.frames <= 0:
            return False
        return self.frames >= MIN_DECODED_FRACTION * self.expected

    @property
    def reason(self) -> str | None:
        if self.ok:
            return None
        if self.returncode not in (0, None) and not self.stopped_early_by_us:
            return f"ffmpeg exited {self.returncode}: {self.stderr_tail}"
        if self.expected <= 0:
            return "nothing to analyse in the requested window"
        return f"decoded {self.frames} of ~{self.expected} expected frames"


def iter_sampled_frames(
    path: Path, info: VideoInfo, *, step: int, report: DecodeReport,
    size: tuple[int, int] = (640, 640), limit_seconds: float | None = None,
    start_seconds: float = 0.0, measured_seconds: float | None = None,
    spawn: Spawner = _popen,
) -> Iterator[SampledFrame]:
    """Yield every `step`-th source frame, resized, with its true source index.

    `report` is filled in as frames arrive and finalised when the generator is exhausted, so the
    caller can refuse to write a clip whose decode came up short.
    """
    w, h = size
    frame_bytes = w * h * 3
    start_frame = round(start_seconds * info.src_fps)
    span = measured_seconds if measured_seconds is not None else (info.duration_s or 0.0)
    span = max(0.0, span - start_seconds)
    if limit_seconds is not None:
        span = min(span, limit_seconds)
    report.expected = int(span * info.src_fps // step)
    stop_after_n = start_frame + span * info.src_fps if limit_seconds is not None else None

    with tempfile.TemporaryFile() as err:
        proc = spawn(sampled_command(path, step, size, start_frame), err)
        assert proc.stdout is not None
        k = 0
        drained = False
        try:
            while True:
                buf = _read_exact(proc.stdout, frame_bytes)
                if buf is None:
                    drained = True
                    break
                n = start_frame + k * step
                if stop_after_n is not None and n >= stop_after_n:
                    report.stopped_early_by_us = True
                    break
                k += 1
                report.frames = k
                report.last_n = n
                yield SampledFrame(n, np.frombuffer(buf, np.uint8).reshape(h, w, 3))
        finally:
            # Only interrupt ffmpeg if we walked away from it — early stop, or the consumer closed
            # the generator. After a natural end of stream its exit code is the verdict.
            if not drained:
                _stop(proc)
                report.stopped_early_by_us = report.stopped_early_by_us or report.frames > 0
            report.returncode = proc.wait()
            err.seek(0)
            report.stderr_tail = err.read()[-400:].decode("utf-8", "replace").strip()


def grab_frames(path: Path, info: VideoInfo, indices: Sequence[int], *,
                spawn: Spawner = _popen, chunk: int = 64) -> dict[int, np.ndarray]:
    """Native-resolution frames at the given source indices, for evidence.

    Evidence is the original pixels at the original size. The 640×640 detector input is a
    stretched, downscaled copy and would be misleading to show anyone.
    """
    wanted = sorted(set(indices))
    out: dict[int, np.ndarray] = {}
    frame_bytes = info.width * info.height * 3
    for i in range(0, len(wanted), chunk):
        part = wanted[i:i + chunk]
        select = "+".join(f"eq(n\\,{n})" for n in part)
        cmd = ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error", "-i", str(path),
               "-map", "0:v:0", "-an", "-sn", "-dn", "-vf", f"select={select}",
               "-fps_mode", "passthrough", "-f", "rawvideo", "-pix_fmt", "rgb24", "pipe:1"]
        with tempfile.TemporaryFile() as err:
            proc = spawn(cmd, err)
            assert proc.stdout is not None
            for n in part:
                buf = _read_exact(proc.stdout, frame_bytes)
                if buf is None:
                    break
                out[n] = np.frombuffer(buf, np.uint8).reshape(info.height, info.width, 3)
            _stop(proc)
            proc.wait()
    return out


def _read_exact(stream: IO[bytes], size: int) -> bytes | None:
    chunks, got = [], 0
    while got < size:
        b = stream.read(size - got)
        if not b:
            return None
        chunks.append(b)
        got += len(b)
    return b"".join(chunks)


def _stop(proc: PipeProcess) -> None:
    if proc.returncode is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


__all__ = [
    "DecodeError", "DecodeReport", "PipeProcess", "SampledFrame", "Spawner", "VideoInfo",
    "grab_frames", "iter_sampled_frames", "probe_video", "sample_step",
    "sampled_command",
]

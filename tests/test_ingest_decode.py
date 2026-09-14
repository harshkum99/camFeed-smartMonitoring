"""Decoding a recording into sampled frames, indexed by source frame number.

The decoder has two jobs whose failure is silent. It must attach the right source index to each
frame, because every track time is derived from it rather than from pts. And it must say when a
decode came up short, because writing results for a half-decoded clip claims coverage for footage
nobody analysed. The fake spawner tests pin both without ffmpeg; one real-ffmpeg test proves the
index mapping on actual pixels.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from smartcam.ingest.decode import (
    DecodeError,
    DecodeReport,
    VideoInfo,
    grab_frames,
    iter_sampled_frames,
    probe_video,
    sample_step,
    sampled_command,
)
from smartcam.survey.probe import CompletedRun

SIZE = (8, 8)
FRAME_BYTES = 8 * 8 * 3  # 192


# --- sampling arithmetic ----------------------------------------------------------------------

@pytest.mark.parametrize(("src", "step"), [(30, 6), (25, 5), (29.97, 6), (15, 3), (4, 1)])
def test_sample_step_at_five_fps(src, step):
    """A wrong step shifts every frame's time; a zero step would divide by zero downstream."""
    assert sample_step(src, 5) == step


def test_sampled_command_selects_by_index_and_keeps_frames_unretimed():
    """passthrough stops ffmpeg duplicating or dropping frames, which would break n = k * step."""
    cmd = sampled_command(Path("clip.avi"), 6)
    joined = " ".join(cmd)
    assert "select=not(mod(n\\,6)),scale=640:640" in joined
    assert "-fps_mode" in cmd and "passthrough" in cmd
    assert "rgb24" in cmd


def test_sampled_command_with_start_frame_offsets_the_selection():
    """Resuming mid-clip must still land on the start frame, not the next multiple of step."""
    joined = " ".join(sampled_command(Path("clip.avi"), 6, start_frame=90))
    assert "gte(n\\,90)" in joined
    assert "mod(n-90\\,6)" in joined


# --- probe ------------------------------------------------------------------------------------

def _runner(returncode=0, stdout="", stderr=""):
    def run(cmd, timeout):
        return CompletedRun(returncode, stdout, stderr)
    return run


def test_probe_video_parses_the_stream():
    """Width, height and fps size the pipe reads; getting any wrong garbles every frame."""
    payload = json.dumps({"streams": [{
        "width": 1920, "height": 1080, "avg_frame_rate": "30000/1001",
        "r_frame_rate": "30/1", "codec_name": "h264", "duration": "300.5",
    }]})
    info = probe_video(Path("x.avi"), runner=_runner(stdout=payload))
    assert (info.width, info.height, info.codec) == (1920, 1080, "h264")
    assert info.src_fps == pytest.approx(29.97, abs=0.01)
    assert info.duration_s == pytest.approx(300.5)


def test_probe_video_raises_on_ffprobe_failure():
    """An unreadable file must fail loudly, not decode as zero frames."""
    with pytest.raises(DecodeError):
        probe_video(Path("x.avi"), runner=_runner(returncode=1, stderr="bad"))


def test_probe_video_raises_when_there_is_no_video_stream():
    """An audio-only or empty container has nothing to analyse."""
    with pytest.raises(DecodeError):
        probe_video(Path("x.avi"), runner=_runner(stdout=json.dumps({"streams": []})))


# --- iter_sampled_frames with a fake ffmpeg ---------------------------------------------------

class FakeProc:
    def __init__(self, frames: int, partial: int = 0, exit_code: int = 0):
        data = b"".join(bytes([i % 256]) * FRAME_BYTES for i in range(frames))
        self.stdout = io.BytesIO(data + b"\x00" * partial)
        self.returncode: int | None = None
        self._exit = exit_code
        self.terminated = False

    def wait(self, timeout=None):
        if self.returncode is None:
            self.returncode = -15 if self.terminated else self._exit
        return self.returncode

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.terminated = True


def _spawner(proc):
    def spawn(cmd, stderr):
        return proc
    return spawn


def _info(duration=10.0):
    return VideoInfo(8, 8, 30.0, "h264", duration)


def _run_decode(proc, **kw):
    report = DecodeReport()
    frames = list(iter_sampled_frames(Path("clip.avi"), _info(), step=6, report=report,
                                      size=SIZE, spawn=_spawner(proc), **kw))
    return frames, report


def test_frames_carry_source_indices_and_the_report_counts_them():
    """The k-th frame out is source frame k * step; that is the whole time model."""
    frames, report = _run_decode(FakeProc(50, partial=10), measured_seconds=10.0)
    assert [f.n for f in frames] == [6 * k for k in range(50)]
    assert frames[3].rgb.shape == (8, 8, 3) and int(frames[3].rgb[0, 0, 0]) == 3
    assert report.frames == 50 and report.expected == 50 and report.last_n == 294
    assert report.ok and report.reason is None


def test_short_decode_is_a_failure_with_a_reason():
    """Stopping at 40 of 50 frames means unknown damage; the clip must not be written."""
    _, report = _run_decode(FakeProc(40), measured_seconds=10.0)
    assert report.frames == 40
    assert not report.ok
    assert "40" in report.reason and "50" in report.reason


def test_limit_seconds_stops_early_and_stays_ok():
    """A deliberate limit is not damage: the verdict is judged against the limited span."""
    proc = FakeProc(50)
    frames, report = _run_decode(proc, measured_seconds=10.0, limit_seconds=2.0)
    assert [f.n for f in frames] == [0, 6, 12, 18, 24, 30, 36, 42, 48, 54]
    assert report.expected == 10
    assert report.stopped_early_by_us and proc.terminated
    assert report.ok


def test_nonzero_exit_after_natural_end_fails():
    """ffmpeg's exit code is the verdict when it ran to the end; a full frame count can lie."""
    _, report = _run_decode(FakeProc(50, exit_code=1), measured_seconds=10.0)
    assert report.frames == 50
    assert not report.ok
    assert "exited 1" in report.reason


# --- real ffmpeg ------------------------------------------------------------------------------

needs_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


@pytest.fixture
def index_clip(tmp_path):
    """Two seconds at 30 fps whose frame N has grey level 4 * N."""
    path = tmp_path / "clip.mkv"
    subprocess.run(  # noqa: S603, S607 - fixed argv
        ["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi",
         "-i", "color=c=black:s=64x48:r=30:d=2",
         "-vf", "format=gray,geq=lum='N*4'", "-c:v", "ffv1", str(path)],
        check=True, capture_output=True, timeout=60,
    )
    return path


@needs_ffmpeg
def test_real_ffmpeg_frame_index_matches_pixels(index_clip):
    """Proves n = k * step against pixels that encode their own index, with no pts involved."""
    info = VideoInfo(64, 48, 30.0, "ffv1", 2.0)
    report = DecodeReport()
    frames = list(iter_sampled_frames(index_clip, info, step=6, report=report, size=(64, 48)))
    assert [f.n for f in frames] == list(range(0, 60, 6))
    for f in frames:
        assert float(np.mean(f.rgb)) == pytest.approx(4 * f.n, abs=2)
    assert report.ok, report.reason


@needs_ffmpeg
def test_real_ffmpeg_grab_frames_returns_requested_indices(index_clip):
    """Evidence stills must be the frames the detections came from, not neighbours."""
    info = VideoInfo(64, 48, 30.0, None, 2.0)
    out = grab_frames(index_clip, info, [12, 30])
    assert sorted(out) == [12, 30]
    for n, rgb in out.items():
        assert rgb.shape == (48, 64, 3)
        assert float(np.mean(rgb)) == pytest.approx(4 * n, abs=2)


def test_a_decode_with_nothing_to_analyse_is_not_a_success():
    """Zero frames against zero expected used to pass, and a start offset past the end of a clip
    then wrote a coverage row that ended before it began."""
    from smartcam.ingest.decode import DecodeReport
    report = DecodeReport(frames=0, expected=0, returncode=0)
    assert not report.ok
    assert "nothing to analyse" in report.reason

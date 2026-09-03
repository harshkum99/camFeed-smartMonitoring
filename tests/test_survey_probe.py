"""Grammar construction and stream probing.

The probe tests inject a fake ffprobe runner, so they exercise the real parsing logic without a
network, a camera, or a subprocess.
"""

from __future__ import annotations

from smartcam.survey.grammars import (
    Vendor,
    candidates_for,
    fingerprint_http,
    fingerprint_ports,
)
from smartcam.survey.probe import CompletedRun, measure_gop_ms, probe_stream

# --- grammars --------------------------------------------------------------------------


def test_hikvision_channel_arithmetic():
    """101 = ch1 main, 102 = ch1 sub, 201 = ch2 main. Getting this wrong silently probes the
    wrong camera, which is worse than failing."""
    c = candidates_for("10.0.0.5", Vendor.HIKVISION, channels=2, include_fallbacks=False)
    urls = {x.url for x in c}
    assert "rtsp://10.0.0.5:554/Streaming/Channels/101" in urls
    assert "rtsp://10.0.0.5:554/Streaming/Channels/102" in urls
    assert "rtsp://10.0.0.5:554/Streaming/Channels/201" in urls


def test_dahua_subtype_arithmetic():
    c = candidates_for("10.0.0.6", Vendor.DAHUA, channels=1, include_fallbacks=False)
    urls = {x.url for x in c}
    assert "rtsp://10.0.0.6:554/cam/realmonitor?channel=1&subtype=0" in urls
    assert "rtsp://10.0.0.6:554/cam/realmonitor?channel=1&subtype=1" in urls


def test_substream_is_tried_before_mainstream():
    """Detection runs on the sub stream. Ordering is a cost decision, not a preference:
    decoding main streams for detection costs ~2.24x for no accuracy gain."""
    c = candidates_for("10.0.0.5", Vendor.HIKVISION, channels=1, include_fallbacks=False)
    assert c[0].stream == "sub"
    assert [x.stream for x in c].index("sub") < [x.stream for x in c].index("main")


def test_credentials_go_in_userinfo_for_most_vendors():
    c = candidates_for("10.0.0.5", Vendor.HIKVISION, channels=1,
                       user="admin", password="p@ss", include_fallbacks=False)
    assert c[0].url.startswith("rtsp://admin:p@ss@10.0.0.5:554/")


def test_credentials_go_in_the_path_for_xiongmai():
    """XiongMai boards take credentials as path parameters. Putting them in the userinfo
    silently fails to authenticate, which reads as a wrong password."""
    c = candidates_for("10.0.0.9", Vendor.XIONGMAI, channels=1,
                       user="admin", password="1234", include_fallbacks=False)
    main = next(x for x in c if x.stream == "main")
    assert "user=admin&password=1234" in main.url
    assert "admin:1234@" not in main.url


def test_no_credentials_produces_clean_urls():
    c = candidates_for("10.0.0.5", Vendor.HIKVISION, channels=1, include_fallbacks=False)
    assert "@" not in c[0].url


def test_redaction_hides_credentials_for_logging():
    """Survey output is shown to customers and written to logs. Passwords must not be in it."""
    c = candidates_for("10.0.0.5", Vendor.HIKVISION, channels=1,
                       user="admin", password="hunter2", include_fallbacks=False)
    assert "hunter2" not in c[0].redacted
    assert "10.0.0.5" in c[0].redacted


def test_reolink_zero_padded_channels():
    c = candidates_for("10.0.0.7", Vendor.REOLINK, channels=1, include_fallbacks=False)
    assert any("h264Preview_01_sub" in x.url for x in c)


def test_fallbacks_are_tried_last():
    c = candidates_for("10.0.0.5", Vendor.HIKVISION, channels=1, include_fallbacks=True)
    assert c[-1].stream == "fallback"
    assert c[0].confidence > c[-1].confidence


def test_unknown_vendor_still_yields_fallback_candidates():
    """A device we cannot fingerprint is not a device we give up on."""
    c = candidates_for("10.0.0.8", Vendor.UNKNOWN, channels=1)
    assert c and all(x.stream == "fallback" for x in c)


def test_http_fingerprints():
    assert fingerprint_http("Server: Hikvision-Webs") is Vendor.HIKVISION
    assert fingerprint_http("<title>CP PLUS Login</title>") is Vendor.DAHUA
    assert fingerprint_http("WWW-Authenticate: Digest realm=\"UNV\"") is Vendor.UNIVIEW
    assert fingerprint_http("nothing useful here") is Vendor.UNKNOWN


def test_port_fingerprints_survive_a_disabled_web_ui():
    """Hardened firmware often disables the web UI but keeps the proprietary SDK port open."""
    assert fingerprint_ports({554, 8000}) is Vendor.HIKVISION
    assert fingerprint_ports({554, 37777}) is Vendor.DAHUA
    assert fingerprint_ports({554, 34567}) is Vendor.XIONGMAI
    assert fingerprint_ports({554, 80}) is Vendor.UNKNOWN


# --- probing ---------------------------------------------------------------------------

STREAM_JSON = (
    '{"streams":[{"width":1280,"height":720,"avg_frame_rate":"15/1",'
    '"r_frame_rate":"15/1","codec_name":"h264"}]}'
)


def fake_runner(stream_out: str, frames_out: str = "", rc: int = 0, frames_rc: int = 0):
    def run(cmd, timeout):
        is_frames = "frame=key_frame,best_effort_timestamp_time" in " ".join(cmd)
        if is_frames:
            return CompletedRun(frames_rc, frames_out, "" if frames_rc == 0 else "boom")
        return CompletedRun(rc, stream_out, "" if rc == 0 else "boom")
    return run


def test_probe_reads_stream_properties():
    p = probe_stream("rtsp://x/1", 1, runner=fake_runner(STREAM_JSON))
    assert p.reachable and p.width == 1280 and p.height == 720
    assert p.fps == 15.0 and p.codec == "h264"


def test_probe_survives_a_broken_frame_rate():
    """Cheap encoders report 0/0. That must not crash the survey."""
    bad = '{"streams":[{"width":640,"height":360,"avg_frame_rate":"0/0","codec_name":"h264"}]}'
    p = probe_stream("rtsp://x/1", 1, runner=fake_runner(bad))
    assert p.reachable and p.fps is None


def test_probe_reports_auth_failure_with_the_clock_hint():
    """A drifted DVR rejects correct credentials with a 401, because WS-UsernameToken validates
    our timestamp against the device's clock. Without this hint an engineer loses a day."""
    def run(cmd, timeout):
        return CompletedRun(1, "", "401 Unauthorized")
    p = probe_stream("rtsp://x/1", 1, runner=run)
    assert not p.reachable
    assert "clock" in p.error


def test_probe_distinguishes_refused_from_timeout():
    for stderr, needle in (
        ("Connection refused", "RTSP may be disabled"),
        ("Operation timed out", "out of sessions"),
        ("No route to host", "camera VLAN"),
    ):
        p = probe_stream("rtsp://x/1", 1, runner=lambda c, t, s=stderr: CompletedRun(1, "", s))
        assert needle in p.error, stderr


def test_probe_handles_video_less_stream():
    p = probe_stream("rtsp://x/1", 1, runner=fake_runner('{"streams":[]}'))
    assert not p.reachable and "no video track" in p.error


def test_probe_handles_unparseable_output():
    p = probe_stream("rtsp://x/1", 1, runner=fake_runner("not json"))
    assert not p.reachable and "unparseable" in p.error


def test_gop_measured_from_timestamps():
    """Keyframes one second apart -> 1000 ms."""
    frames = "\n".join(
        f"{1 if i % 15 == 0 else 0},{i / 15:.4f}" for i in range(60)
    )
    assert measure_gop_ms("rtsp://x", fps=15.0, runner=fake_runner("", frames)) == 1000


def test_gop_uses_median_not_mean():
    """Variable-GOP 'smart' encoders (Hikvision H.264+, Dahua Smart H.264) emit one anomalous
    long gap. A mean would be dragged by it; the median reports what the stream mostly does."""
    times = [0.0, 1.0, 2.0, 3.0, 30.0]      # four gaps: 1, 1, 1, 27
    frames = "\n".join(f"1,{t:.4f}" for t in times)
    assert measure_gop_ms("rtsp://x", fps=15.0, runner=fake_runner("", frames)) == 1000


def test_gop_falls_back_to_frame_counting_without_timestamps():
    frames = "\n".join("1" if i % 30 == 0 else "0" for i in range(90))
    assert measure_gop_ms("rtsp://x", fps=15.0, runner=fake_runner("", frames)) == 2000


def test_single_keyframe_in_sample_reports_a_lower_bound():
    """One keyframe in 120 frames at 15fps means the GOP is at least 8s. That is a finding
    worth surfacing, not a measurement to discard — it is a latency disaster."""
    frames = "\n".join("1" if i == 0 else "0" for i in range(120))
    got = measure_gop_ms("rtsp://x", fps=15.0, sample_frames=120, runner=fake_runner("", frames))
    assert got == 8000


def test_gop_returns_none_when_it_cannot_be_measured():
    assert measure_gop_ms("rtsp://x", fps=15.0, runner=fake_runner("", "", frames_rc=1)) is None

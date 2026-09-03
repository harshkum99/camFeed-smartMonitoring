"""Grading and box-sizing tests.

These encode commercial promises, not just code behaviour: if `grade_channel` says
RECOGNITION, a salesperson will quote face recognition on that camera.
"""

from __future__ import annotations

from smartcam.survey.grade import (
    BOXES,
    ChannelProbe,
    Grade,
    grade_channel,
    max_cameras,
    shm_bytes_per_camera,
    size_box,
)


def probe(**kw) -> ChannelProbe:
    base = dict(channel=1, reachable=True, width=1280, height=720, fps=15.0,
                codec="h264", gop_ms=1000, stream="sub", has_substream=True)
    base.update(kw)
    return ChannelProbe(**base)


# --- grading ---------------------------------------------------------------------------

def test_healthy_substream_is_detection_grade():
    g = grade_channel(probe())
    assert g.grade is Grade.DETECTION
    assert g.budget_units == 1.0
    assert not g.warnings


def test_unreachable_channel_is_a_finding_not_an_exception():
    g = grade_channel(probe(reachable=False, error="connection refused"))
    assert g.grade is Grade.UNSERVABLE
    assert "connection refused" in g.reasons[0]
    assert g.budget_units == 0.0


def test_connected_but_undecodable_is_unservable():
    g = grade_channel(probe(width=None, height=None, fps=None))
    assert g.grade is Grade.UNSERVABLE


def test_face_pixels_above_floor_earn_recognition_grade():
    g = grade_channel(probe(face_ipd_px=80.0))
    assert g.grade is Grade.RECOGNITION


def test_face_pixels_below_floor_do_not_earn_recognition_grade():
    """The whole point of grading. 40px between the eyes cannot identify anyone, and no amount
    of upscaling changes that."""
    g = grade_channel(probe(face_ipd_px=40.0))
    assert g.grade is Grade.DETECTION
    assert any("cannot" in r and "identify" in r for r in g.reasons)


def test_plate_width_above_floor_earns_anpr_grade():
    g = grade_channel(probe(plate_width_px=120.0))
    assert g.grade is Grade.ANPR


def test_long_gop_warns_but_does_not_disqualify():
    """A 4s keyframe interval puts a 4s floor under alert latency — but it is usually fixable
    in the recorder's sub-stream settings, so it degrades rather than disqualifies."""
    g = grade_channel(probe(gop_ms=4000))
    assert g.grade is Grade.DETECTION
    assert any("keyframe interval" in w for w in g.warnings)
    assert any("SUB stream" in w for w in g.warnings)


def test_tiny_substream_falls_back_to_main_and_is_graded_degraded():
    """DEGRADED is a capacity statement, so it must track the capacity cost. A channel billed at
    2.24x cannot be reported as a clean detection channel — that is how a 20-camera quote turns
    into a box that cannot carry the site."""
    g = grade_channel(probe(width=352, height=288))
    assert g.grade is Grade.DEGRADED
    assert g.budget_units == 2.24
    assert any("main stream" in w for w in g.warnings)


def test_main_stream_fallback_admits_it_was_not_measured():
    """We infer the main stream will work; we did not probe it. Saying so is the difference
    between a defensible quote and an apology."""
    g = grade_channel(probe(width=352, height=288))
    assert any("not probed" in w for w in g.warnings)


def test_fisheye_costs_budget_without_being_graded_degraded():
    """A fisheye costs extra for dewarping, not because its stream is unusable. Conflating the
    two would misreport a perfectly good camera."""
    g = grade_channel(probe(lens="fisheye"))
    assert g.grade is Grade.DETECTION
    assert g.budget_units > 1.0


def test_tiny_main_stream_is_genuinely_unservable():
    """No fallback left when the main stream itself is below the floor."""
    g = grade_channel(probe(width=352, height=288, stream="main"))
    assert g.grade is Grade.UNSERVABLE


def test_slow_stream_is_unservable():
    g = grade_channel(probe(fps=1.0))
    assert g.grade is Grade.UNSERVABLE
    assert any("fps" in r for r in g.reasons)


def test_no_substream_is_degraded_not_detection():
    g = grade_channel(probe(has_substream=False, stream="main", width=1920, height=1080))
    assert g.grade is Grade.DEGRADED
    assert g.budget_units == 2.24


def test_fisheye_costs_extra_budget_and_says_why():
    g = grade_channel(probe(lens="fisheye"))
    assert g.budget_units > 1.0
    assert any("fisheye" in w for w in g.warnings)


def test_ptz_warns_about_preset_binding():
    g = grade_channel(probe(lens="ptz"))
    assert any("preset" in w for w in g.warnings)


def test_h265_warns_about_hardware_decode():
    g = grade_channel(probe(codec="hevc"))
    assert any("H.265" in w for w in g.warnings)


# --- box sizing ------------------------------------------------------------------------

def test_shm_matches_frigate_formula_at_1080p():
    """~59.6 MB/camera at 1080p. 40 cameras is ~2.4 GB against a Docker default of 64 MB."""
    mb = shm_bytes_per_camera(1920, 1080) / 1024**2
    assert 59.0 < mb < 60.5
    forty = shm_bytes_per_camera(1920, 1080) * 40 / 1024**3
    assert 2.3 < forty < 2.5


def test_small_estate_fits_the_cheap_box():
    probes = {i: probe(channel=i) for i in range(1, 9)}
    grades = [grade_channel(p) for p in probes.values()]
    s = size_box(grades, probes, box="edge-s")
    assert s.fits
    assert s.cameras == 8
    assert s.headroom_pct > 0


def test_forty_cameras_do_not_fit_the_cheap_box_and_it_says_so_concretely():
    """The research produced four contradictory camera counts for this hardware. This is the
    arithmetic one, and the failure message has to be actionable — a field engineer reads it."""
    probes = {i: probe(channel=i, width=1920, height=1080) for i in range(1, 41)}
    grades = [grade_channel(p) for p in probes.values()]
    s = size_box(grades, probes, box="edge-s")
    assert not s.fits
    joined = " ".join(s.limits)
    assert "oversubscribed" in joined
    assert "Hailo" in joined or "second box" in joined
    # The detector is the binding constraint here, not memory: 40x1080p needs ~2.4 GB of shm
    # and this box budgets 4 GB. Both numbers still have to be right.
    assert 2.3 < s.shm_required_gb < 2.5
    assert not any("shared memory" in limit for limit in s.limits)


def test_docker_default_shm_is_caught_before_it_becomes_a_bus_error():
    """The real-world failure this check exists for. Docker's default shm is 64 MB; a
    40-camera site needs ~2.4 GB and dies on startup with an opaque `Bus error`, not with
    degraded performance. We want it caught at survey time, on a laptop, with a fix attached."""
    probes = {i: probe(channel=i, width=1920, height=1080) for i in range(1, 41)}
    grades = [grade_channel(p) for p in probes.values()]
    s = size_box(grades, probes, box="edge-s", shm_gb_available=0.0625)
    assert not s.fits
    shm_limits = [x for x in s.limits if "shared memory" in x]
    assert shm_limits, s.limits
    assert "shm_size" in shm_limits[0] and "Bus error" in shm_limits[0]


def test_motion_duty_dominates_capacity():
    """A busy outdoor yard at 100% duty carries roughly a third of what a quiet indoor site does.
    This single parameter is the difference between a working SKU and an undersized one."""
    quiet = max_cameras("edge-s", motion_duty=0.30)
    busy = max_cameras("edge-s", motion_duty=1.0)
    assert quiet > busy
    assert busy == 6 and quiet == 22


def test_hailo_upgrade_raises_the_ceiling():
    assert max_cameras("edge-m-hailo") > max_cameras("edge-m") > max_cameras("edge-s")


def test_unservable_channels_consume_no_budget():
    probes = {1: probe(channel=1), 2: probe(channel=2, reachable=False)}
    grades = [grade_channel(p) for p in probes.values()]
    s = size_box(grades, probes, box="edge-s")
    assert s.cameras == 1


def test_degraded_channels_cost_more_than_clean_ones():
    clean = {i: probe(channel=i) for i in range(1, 6)}
    degraded = {i: probe(channel=i, has_substream=False, stream="main",
                         width=1920, height=1080) for i in range(1, 6)}
    a = size_box([grade_channel(p) for p in clean.values()], clean, box="edge-s")
    b = size_box([grade_channel(p) for p in degraded.values()], degraded, box="edge-s")
    assert b.inferences_required > a.inferences_required
    assert b.shm_required_gb > a.shm_required_gb


def test_every_box_spec_is_self_consistent():
    for key, spec in BOXES.items():
        assert spec.inferences_per_sec > 0, key
        assert spec.shm_gb > 0 and spec.ram_gb >= spec.shm_gb, key

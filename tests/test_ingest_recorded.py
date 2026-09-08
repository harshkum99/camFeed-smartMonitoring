"""Importing recorded footage: filename conventions, and the gaps between and inside clips.

These tests care most about the two ways an importer can lie. It can put footage at the wrong
time — a timezone the exporter never declared — and it can claim coverage for a window whose file
is truncated. Both produce a system that answers confidently and wrongly, which is worse than one
that declines.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from smartcam.ingest.recorded import (
    Clip,
    ConventionError,
    build_timeline,
    measure_seconds,
    parse_name,
    scan,
)
from smartcam.survey.probe import CompletedRun

MEVA = "2018-03-07.16-50-00.16-55-00.admin.G329.r13.avi"


# --- filename conventions ---------------------------------------------------------------------

def test_meva_names_yield_camera_and_both_ends_of_the_window():
    p = parse_name(Path(MEVA))
    assert p.camera_key == "admin.G329"
    assert p.starts_at == datetime(2018, 3, 7, 16, 50)
    assert p.declared_seconds == 300.0
    assert p.convention == "meva"


def test_meva_window_crossing_midnight_does_not_go_backwards():
    """23-58 to 00-03 is five minutes forward, not twenty-three hours and fifty-five back."""
    p = parse_name(Path("2018-03-07.23-58-00.00-03-00.bus.G331.r13.avi"))
    assert p.declared_seconds == 300.0


def test_hikvision_export_names():
    p = parse_name(Path("ch01_20260908103000.mp4"))
    assert (p.camera_key, p.convention) == ("ch01", "hikvision")
    assert p.starts_at == datetime(2026, 9, 8, 10, 30)
    assert p.declared_seconds is None      # Hikvision states a start, never an end


def test_dahua_takes_its_date_from_the_directory_tree():
    """Dahua keeps only the clock in the filename. Reading the name alone dates every clip to
    1900 and silently collapses a month of footage onto one day."""
    p = parse_name(Path("dvr/2026-09-08/003/dav/10/10.03.00-10.08.00[M][0@0][0].dav"))
    assert p.camera_key == "ch03"
    assert p.starts_at == datetime(2026, 9, 8, 10, 3)
    assert p.declared_seconds == 300.0


def test_iso_convention_for_our_own_exports():
    p = parse_name(Path("cam-gate_2026-09-08T10-30-00.mp4"))
    assert (p.camera_key, p.starts_at) == ("cam-gate", datetime(2026, 9, 8, 10, 30))


def test_unmatched_name_returns_none_rather_than_guessing():
    assert parse_name(Path("VIDEO_0041.mp4")) is None


def test_asserting_a_convention_raises_when_it_does_not_match():
    """A convention that matches 3% of an import and misreads the rest is worse than one that
    matches nothing, because the 97% still lands in the database."""
    with pytest.raises(ConventionError, match="does not match"):
        parse_name(Path("ch01_20260908103000.mp4"), convention="meva")


def test_unknown_convention_names_the_ones_we_have():
    with pytest.raises(ConventionError, match="hikvision"):
        parse_name(Path(MEVA), convention="panasonic")


# --- probing ----------------------------------------------------------------------------------

def runner_for(payload: str, rc: int = 0, stderr: str = ""):
    return lambda cmd, timeout: CompletedRun(rc, payload, stderr)


def test_duration_prefers_the_video_stream_over_the_container():
    """A recorder that appends across a reboot leaves a container duration spanning the wall
    clock while the video stream holds only what was written."""
    payload = '{"streams":[{"duration":"40.0"}],"format":{"duration":"300.0"}}'
    assert measure_seconds(Path("x.mp4"), runner=runner_for(payload))[0] == 40.0


def test_duration_falls_back_to_the_container_when_the_stream_omits_it():
    payload = '{"streams":[{}],"format":{"duration":"120.5"}}'
    assert measure_seconds(Path("x.mp4"), runner=runner_for(payload))[0] == 120.5


def test_unreadable_file_reports_the_reason_not_a_zero():
    broken = runner_for("", rc=1, stderr="moov atom not found")
    got, why = measure_seconds(Path("x.mp4"), runner=broken)
    assert got is None and "moov atom" in why


# --- scanning ---------------------------------------------------------------------------------

def make(root: Path, name: str, size: int = 1024) -> Path:
    p = root / name
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"\0" * size)
    return p


def test_recorder_timezone_places_the_footage(tmp_path):
    """The single most consequential argument in this module. No export declares an offset, and
    getting it wrong turns 'after 8pm' into a question about the afternoon."""
    make(tmp_path, "cam-gate_2026-09-08T20-30-00.mp4")
    probe = runner_for('{"streams":[{"duration":"300"}]}')

    ist = scan(tmp_path, tz="Asia/Kolkata", runner=probe)[0]
    utc = scan(tmp_path, tz="UTC", runner=probe)[0]

    assert ist.starts_at == datetime(2026, 9, 8, 15, 0, tzinfo=UTC)
    assert utc.starts_at == datetime(2026, 9, 8, 20, 30, tzinfo=UTC)


def test_camera_map_binds_dataset_keys_to_our_camera_ids(tmp_path):
    make(tmp_path, MEVA)
    clip = scan(tmp_path, camera_map={"admin.G329": "cam-gate"},
                runner=runner_for('{"streams":[{"duration":"300"}]}'))[0]
    assert clip.camera_key == "cam-gate"


def test_unparseable_name_falls_back_and_admits_which_crutch_it_used(tmp_path):
    """Landing on mtime is allowed. Landing on it silently is not — the report has to be able to
    say how much of the timeline rests on a guess."""
    make(tmp_path / "cam4", "VIDEO_0041.mp4")
    clip = scan(tmp_path, runner=runner_for('{"streams":[{"duration":"12"}]}'))[0]
    assert clip.convention == "mtime"
    assert clip.camera_key == "cam4"           # the folder is the only hint left


def test_container_creation_time_beats_mtime_when_the_name_is_useless(tmp_path):
    make(tmp_path, "VIDEO_0041.mp4")
    payload = ('{"streams":[{"duration":"12"}],'
               '"format":{"tags":{"creation_time":"2026-09-08T10:30:00.000000Z"}}}')
    clip = scan(tmp_path, runner=runner_for(payload))[0]
    assert clip.convention == "container"
    assert clip.starts_at == datetime(2026, 9, 8, 10, 30, tzinfo=UTC)


def test_non_video_files_are_ignored(tmp_path):
    make(tmp_path, "notes.txt")
    make(tmp_path, MEVA)
    assert len(scan(tmp_path, runner=runner_for('{"streams":[{"duration":"300"}]}'))) == 1


# --- the timeline -----------------------------------------------------------------------------

T0 = datetime(2026, 9, 8, 10, 0, tzinfo=UTC)


def clip(minute: int, *, declared: float | None = 300.0, measured: float | None = 300.0,
         camera: str = "cam-gate", error: str | None = None) -> Clip:
    return Clip(path=Path(f"{minute}.mp4"), camera_key=camera,
                starts_at=T0 + timedelta(minutes=minute), declared_seconds=declared,
                measured_seconds=measured, size_bytes=1024, convention="iso", error=error)


def test_contiguous_clips_leave_no_gap():
    t = build_timeline([clip(0), clip(5), clip(10)])
    assert t.gaps == []
    assert t.coverage_pct() == 100.0


def test_a_missing_clip_is_a_gap_between():
    t = build_timeline([clip(0), clip(10)])
    (gap,) = t.gaps
    assert gap.kind == "between" and gap.seconds == 300.0 and gap.exact


def test_a_truncated_clip_is_a_gap_inside_its_own_window():
    """The 2 MB clip sitting among 140 MB siblings. Every other pipeline drops it as corrupt,
    which silently converts 'the camera was down' into 'nothing happened'."""
    t = build_timeline([clip(0, measured=40.0), clip(5)])
    (gap,) = t.gaps
    assert gap.kind == "within"
    assert gap.seconds == pytest.approx(260.0)


def test_a_truncated_gap_admits_it_does_not_know_where_the_loss_fell():
    """We know 260 seconds are missing. We do not know whether the camera dropped at the start,
    the middle or the end, and the report must not imply we do."""
    t = build_timeline([clip(0, measured=40.0)])
    assert t.gaps[0].exact is False


def test_an_unreadable_file_is_an_exact_gap_over_its_declared_window():
    t = build_timeline([clip(0, measured=None, error="moov atom not found"), clip(5)])
    (gap,) = t.gaps
    assert gap.kind == "unreadable" and gap.seconds == 300.0 and gap.exact


def test_a_camera_that_stops_early_gets_a_trailing_gap():
    """The blindness a per-camera view misses if it only looks between consecutive clips: camera
    b has no second clip, so there is no pair to compare, and its missing five minutes vanish.
    `coverage_gaps` in the schema needed a third UNION arm for exactly this; the two must agree."""
    t = build_timeline([clip(0, camera="a"), clip(5, camera="a"), clip(0, camera="b")])
    assert [(g.camera_key, g.kind, g.seconds) for g in t.gaps] == [("b", "after", 300.0)]
    assert t.cameras == ["a", "b"]


def test_a_camera_that_starts_late_gets_a_leading_gap():
    t = build_timeline([clip(0, camera="a"), clip(5, camera="a"), clip(5, camera="b")])
    assert [(g.camera_key, g.kind) for g in t.gaps] == [("b", "before")]


def test_gap_total_reconciles_with_the_coverage_figure():
    """If the gaps a report lists do not add up to the coverage it quotes, one of the two is
    lying, and an operator will find the discrepancy before we do."""
    clips = [clip(0, camera="a"), clip(5, camera="a"), clip(0, camera="b", measured=100.0)]
    t = build_timeline(clips)
    span = t.window
    total = (span[1] - span[0]).total_seconds() * len(t.cameras)
    missing = sum(g.seconds for g in t.gaps)
    assert t.coverage_pct() == pytest.approx(100.0 * (total - missing) / total, abs=0.1)


def test_an_explicit_window_holds_every_camera_to_the_shift_not_to_the_footage():
    """Asked about an eight-hour night shift, a folder holding ten minutes of it is 2% covered.
    Deriving the window from the clips instead would call the same blind night 100%."""
    shift = (T0, T0 + timedelta(hours=8))
    t = build_timeline([clip(0), clip(5)], window=shift)
    assert [g.kind for g in t.gaps] == ["after"]
    assert t.coverage_pct() == pytest.approx(600 / 28800 * 100, abs=0.1)


def test_overlapping_clips_are_reported_rather_than_double_counted():
    """Two files covering one window means a duplicated export or a misread convention. Summing
    them inflates coverage past what was recorded."""
    t = build_timeline([clip(0), clip(2)])
    assert t.overlaps and t.overlaps[0][0] == "cam-gate"
    assert t.coverage_pct() <= 100.0


def test_coverage_is_computed_from_measured_footage_not_from_file_count():
    """A thousand files of which a third are truncated is not a fully covered site."""
    t = build_timeline([clip(0, measured=100.0), clip(5, measured=100.0)])
    assert t.coverage_pct() == pytest.approx(33.3, abs=0.2)


def test_an_empty_import_is_zero_coverage_not_a_crash():
    t = build_timeline([])
    assert t.coverage_pct() == 0.0 and t.window is None


def test_clip_end_trusts_the_measurement_over_the_claim():
    c = clip(0, declared=300.0, measured=40.0)
    assert c.trusted_end == T0 + timedelta(seconds=40)
    assert c.declared_end == T0 + timedelta(seconds=300)
    assert c.short_by_seconds == 260.0


def test_short_by_is_zero_when_there_is_nothing_to_compare():
    """Hikvision states no end time. Absence of a declared duration is not evidence of loss."""
    assert clip(0, declared=None, measured=40.0).short_by_seconds == 0.0

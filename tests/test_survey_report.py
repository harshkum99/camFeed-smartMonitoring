"""Report rendering and discovery parsing.

The report is customer-facing and is used to price a deal, so its claims are tested like code.
"""

from __future__ import annotations

import json

import pytest

from sanjay.survey.cli import build_parser
from sanjay.survey.discovery import Device, _first_xaddr, expand_cidr, parse_device_time
from sanjay.survey.grade import ChannelProbe, Grade, grade_channel, size_box
from sanjay.survey.grammars import Vendor
from sanjay.survey.report import SurveyResult, now_iso, to_json, to_markdown


def make_result(probes: dict[int, ChannelProbe], devices=None) -> SurveyResult:
    grades = [grade_channel(p) for p in probes.values()]
    return SurveyResult(
        site_name="Acme Plant 2", surveyed_at=now_iso(), surveyor="tester",
        devices=devices or [], probes=probes, grades=grades,
        sizing=size_box(grades, probes, box="edge-s"),
    )


def probe(**kw) -> ChannelProbe:
    base = dict(channel=1, reachable=True, width=1280, height=720, fps=15.0,
                codec="h264", gop_ms=1000, stream="sub", has_substream=True)
    base.update(kw)
    return ChannelProbe(**base)


# --- report ----------------------------------------------------------------------------

def test_report_states_how_many_channels_are_usable():
    probes = {1: probe(channel=1), 2: probe(channel=2), 3: probe(channel=3, fps=1.0)}
    md = to_markdown(make_result(probes))
    assert "**2 of 3 channels are usable.**" in md


def test_report_warns_loudly_when_no_camera_can_do_face_recognition():
    """The most important honest finding the tool produces. If a salesperson reads this report
    they must not come away thinking face matching is available."""
    probes = {1: probe(channel=1, face_ipd_px=30.0)}
    md = to_markdown(make_result(probes))
    assert "No camera on this site is recognition-grade" in md
    assert "64 pixels" in md
    assert "upscaling invents it" in md


def test_report_omits_the_face_warning_when_a_camera_qualifies():
    probes = {1: probe(channel=1, face_ipd_px=90.0)}
    md = to_markdown(make_result(probes))
    assert "No camera on this site is recognition-grade" not in md
    assert "Recognition-grade" in md


def test_report_surfaces_channel_warnings_with_the_fix():
    probes = {1: probe(channel=1, gop_ms=5000)}
    md = to_markdown(make_result(probes))
    assert "Channels needing attention" in md
    assert "keyframe interval" in md


def test_report_promises_we_changed_nothing_and_means_it():
    """Our answer to the hardest question in any regulated-sector meeting. The report must not
    accidentally imply we touched the main stream."""
    md = to_markdown(make_result({1: probe()}))
    assert "no device settings were changed" in md.lower()
    assert "never to the main stream" in md


def test_report_shows_clock_drift_in_human_units():
    dev = Device(host="10.0.0.64", open_ports={554, 8000}, vendor=Vendor.HIKVISION,
                 clock_offset_ms=-252_000, notes=["clock is 4m behind ours"])
    md = to_markdown(make_result({1: probe()}, devices=[dev]))
    assert "4m behind" in md
    assert "10.0.0.64" in md


def test_report_explains_a_box_that_does_not_fit():
    probes = {i: probe(channel=i, width=1920, height=1080) for i in range(1, 41)}
    md = to_markdown(make_result(probes))
    assert "does not fit as specified" in md
    assert "oversubscribed" in md


def test_report_handles_a_site_where_nothing_was_found():
    """The credentials-failure case, which is the single most common outcome of a first visit."""
    md = to_markdown(SurveyResult(site_name="Acme", surveyed_at=now_iso(), surveyor="t"))
    assert "No camera channels could be enumerated" in md
    assert "credentials" in md


def test_report_lists_unreachable_channels_separately():
    r = make_result({1: probe()})
    r.unreachable = [(7, "authentication rejected")]
    md = to_markdown(r)
    assert "could not reach" in md
    assert "Channel 7" in md


def test_json_output_is_valid_and_complete():
    dev = Device(host="10.0.0.64", open_ports={554, 8000}, vendor=Vendor.HIKVISION)
    data = json.loads(to_json(make_result({1: probe()}, devices=[dev])))
    assert data["site"] == "Acme Plant 2"
    assert data["summary"][Grade.DETECTION.value] == 1
    assert data["devices"][0]["open_ports"] == [554, 8000]   # sets serialise as sorted lists
    assert data["sizing"]["cameras"] == 1
    assert data["channels"][0]["probe"]["width"] == 1280


# --- discovery parsing -----------------------------------------------------------------

def test_parse_device_time_prefers_utc():
    xml = """<Envelope><Body><GetSystemDateAndTimeResponse><SystemDateAndTime>
      <TimeZone><TZ>GMT+00:00</TZ></TimeZone>
      <UTCDateTime><Time><Hour>10</Hour><Minute>30</Minute><Second>15</Second></Time>
      <Date><Year>2026</Year><Month>9</Month><Day>2</Day></Date></UTCDateTime>
      <LocalDateTime><Time><Hour>16</Hour><Minute>0</Minute><Second>15</Second></Time>
      <Date><Year>2026</Year><Month>9</Month><Day>2</Day></Date></LocalDateTime>
    </SystemDateAndTime></GetSystemDateAndTimeResponse></Body></Envelope>"""
    dt, tz = parse_device_time(xml)
    assert dt is not None
    assert (dt.year, dt.month, dt.day, dt.hour, dt.minute) == (2026, 9, 2, 10, 30)
    assert tz == "GMT+00:00"


def test_parse_device_time_survives_a_partial_response():
    dt, tz = parse_device_time("<Envelope><Body>nothing useful</Body></Envelope>")
    assert dt is None and tz is None


def test_parse_device_time_rejects_an_impossible_date():
    """Devices with a dead RTC report month 0 or day 0. That must not raise."""
    xml = """<UTCDateTime><Time><Hour>0</Hour><Minute>0</Minute><Second>0</Second></Time>
             <Date><Year>0</Year><Month>0</Month><Day>0</Day></Date></UTCDateTime>"""
    dt, _ = parse_device_time(xml)
    assert dt is None


def test_first_xaddr_extraction():
    xml = ('<d:ProbeMatch><d:XAddrs>http://10.0.0.64/onvif/device_service '
           'http://[fe80::1]/onvif/device_service</d:XAddrs></d:ProbeMatch>')
    assert _first_xaddr(xml) == "http://10.0.0.64/onvif/device_service"
    assert _first_xaddr("<d:ProbeMatch/>") is None


def test_cidr_expansion():
    assert len(expand_cidr("192.168.1.0/29")) == 6


def test_cidr_expansion_refuses_to_sweep_the_whole_network():
    """Sweeping a customer's entire network unasked lands us in their SOC incident report."""
    with pytest.raises(ValueError, match="refusing to sweep"):
        expand_cidr("10.0.0.0/16")


# --- cli -------------------------------------------------------------------------------

def test_cli_parses_all_three_subcommands():
    p = build_parser()
    assert p.parse_args(["scan", "--cidr", "192.168.1.0/24"]).cmd == "scan"
    assert p.parse_args(["probe", "--host", "10.0.0.1"]).cmd == "probe"
    assert p.parse_args(["run", "--host", "10.0.0.1", "--channels", "8"]).channels == 8


def test_cli_defaults_to_low_rtsp_concurrency():
    """Opening many simultaneous sessions exhausts a recorder and degrades the customer's own
    live view. If Sanjay makes their CCTV worse, the deal is dead regardless of AI quality."""
    assert build_parser().parse_args(["probe", "--host", "x"]).concurrency <= 4


def test_cli_takes_no_password_argument():
    """Passwords must come from a prompt or the environment, never from argv, where they land
    in shell history and in every process listing on the box."""
    p = build_parser()
    with pytest.raises(SystemExit):
        p.parse_args(["probe", "--host", "x", "--password", "hunter2"])

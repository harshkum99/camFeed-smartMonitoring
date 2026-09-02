"""Vendor knowledge base: how to talk to the recorders that actually exist in India.

Roughly 80-95% of the Indian installed base is Hikvision- or Dahua-derived. CP Plus (Aditya
Infotech) is the Dahua-lineage domestic champion; Prama is the Hikvision JV; Secureye, Zicom and
Godrej are largely rebadged Chinese OEM boards. That concentration is good news: two RTSP URL
grammars and two HTTP control APIs cover the overwhelming majority of sites.

This module is deliberately data-heavy and logic-light. It is the file that accumulates device
quirks over time, and that accumulated long tail is a real (if unglamorous) moat — no open-source
project will do this work for the Indian market.

Everything here is passive knowledge. Nothing in this module performs I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class Vendor(StrEnum):
    HIKVISION = "hikvision"     # incl. Prama (the Hikvision India JV)
    DAHUA = "dahua"             # incl. CP Plus, the Dahua-lineage domestic champion
    UNIVIEW = "uniview"
    XIONGMAI = "xiongmai"       # the generic white-label board behind many cheap Indian DVRs
    AXIS = "axis"
    TPLINK = "tplink"           # VIGI
    REOLINK = "reolink"
    UNKNOWN = "unknown"


# Ports worth sweeping. 554 is RTSP; 8000 and 37777 are the Hikvision and Dahua proprietary SDK
# ports and are the single most reliable vendor fingerprint when HTTP is locked down; 34567 is
# the XiongMai/"Sofia" port that betrays a white-label board.
SCAN_PORTS: tuple[int, ...] = (554, 80, 8000, 37777, 34567, 8899, 443, 8080)

# Ports that identify a vendor on their own.
PORT_FINGERPRINTS: dict[int, Vendor] = {
    8000: Vendor.HIKVISION,
    37777: Vendor.DAHUA,
    34567: Vendor.XIONGMAI,
}


@dataclass(frozen=True)
class StreamGrammar:
    """How to construct an RTSP URL for one vendor family.

    `main` and `sub` are format strings taking `channel` (1-indexed) and, where the vendor
    encodes it that way, a derived stream id.
    """

    vendor: Vendor
    main: str
    sub: str
    #: Some recorders expose a usable mid-resolution third stream, but typically only on
    #: higher-end models. Worth probing: it is often the sweet spot for detection.
    third: str | None = None
    #: Recorded-footage replay, used by the backfill importer and by clip export. Takes
    #: `channel`, `start` and `end` (vendor-specific time format, see `playback_time_format`).
    playback: str | None = None
    playback_time_format: str = "%Y%m%dT%H%M%SZ"
    default_port: int = 554
    notes: str = ""


GRAMMARS: dict[Vendor, StreamGrammar] = {
    # Channel id arithmetic: channel * 100 + stream. 101 = ch1 main, 102 = ch1 sub, 103 = ch1
    # third. The sub stream is documented as "typically low resolution" — on the HD-analog DVR
    # estate it is the phone-app stream, and its GOP is tuned for bandwidth, not latency.
    Vendor.HIKVISION: StreamGrammar(
        vendor=Vendor.HIKVISION,
        main="rtsp://{host}:{port}/Streaming/Channels/{channel}01",
        sub="rtsp://{host}:{port}/Streaming/Channels/{channel}02",
        third="rtsp://{host}:{port}/Streaming/Channels/{channel}03",
        playback=(
            "rtsp://{host}:{port}/Streaming/tracks/{channel}01"
            "?starttime={start}&endtime={end}"
        ),
        notes="Newer firmware requires digest auth and may disable RTSP by default.",
    ),
    # subtype 0 = main, 1 = extra stream 1, 2 = extra stream 2. Channels are 1-indexed.
    Vendor.DAHUA: StreamGrammar(
        vendor=Vendor.DAHUA,
        main="rtsp://{host}:{port}/cam/realmonitor?channel={channel}&subtype=0",
        sub="rtsp://{host}:{port}/cam/realmonitor?channel={channel}&subtype=1",
        third="rtsp://{host}:{port}/cam/realmonitor?channel={channel}&subtype=2",
        playback=(
            "rtsp://{host}:{port}/cam/playback?channel={channel}"
            "&starttime={start}&endtime={end}"
        ),
        playback_time_format="%Y_%m_%d_%H_%M_%S",
        notes="CP Plus shares this grammar. Some CP-NC models use /VideoInput/1/mpeg4/1.",
    ),
    Vendor.UNIVIEW: StreamGrammar(
        vendor=Vendor.UNIVIEW,
        main="rtsp://{host}:{port}/unicast/c{channel}/s0/live",
        sub="rtsp://{host}:{port}/unicast/c{channel}/s1/live",
        notes="Older UNV firmware uses /media/video{channel}.",
    ),
    Vendor.XIONGMAI: StreamGrammar(
        vendor=Vendor.XIONGMAI,
        main="rtsp://{host}:{port}/user={user}&password={password}"
             "&channel={channel}&stream=0.sdp?real_stream",
        sub="rtsp://{host}:{port}/user={user}&password={password}"
            "&channel={channel}&stream=1.sdp?real_stream",
        notes="Credentials go in the PATH, not the userinfo. Frequently no sub stream at all.",
    ),
    Vendor.AXIS: StreamGrammar(
        vendor=Vendor.AXIS,
        main="rtsp://{host}:{port}/axis-media/media.amp?camera={channel}",
        sub="rtsp://{host}:{port}/axis-media/media.amp?camera={channel}&resolution=640x360",
    ),
    Vendor.TPLINK: StreamGrammar(
        vendor=Vendor.TPLINK,
        main="rtsp://{host}:{port}/stream1",
        sub="rtsp://{host}:{port}/stream2",
        notes="VIGI needs Smart Coding OFF and H.264, or recorded playback misbehaves.",
    ),
    Vendor.REOLINK: StreamGrammar(
        vendor=Vendor.REOLINK,
        main="rtsp://{host}:{port}/h264Preview_{channel:02d}_main",
        sub="rtsp://{host}:{port}/h264Preview_{channel:02d}_sub",
        notes="Prefers CBR ('fluency first') and interframe space 1x.",
    ),
}

# Extra paths worth trying when the primary grammar fails. Cheap to attempt, and each one
# represents a real family of devices somebody has already lost an afternoon to.
FALLBACK_PATHS: tuple[str, ...] = (
    "/VideoInput/1/mpeg4/1",      # CP Plus CP-NC series
    "/media/video{channel}",      # older Uniview
    "/live/ch{channel}",
    "/ch{channel}/0",
    "/11",                        # some XiongMai boards
    "/live",
    "/stream1",
)


@dataclass(frozen=True)
class ControlApi:
    """Vendor HTTP API for enumerating channels without ONVIF.

    ONVIF is a convenience, not a dependency: WS-Discovery is UDP multicast with TTL 1, so it
    never crosses a subnet, and Profile S conformance ends 31 March 2027. These two APIs are
    what actually work.
    """

    vendor: Vendor
    channel_list: str
    device_info: str
    encoder_config: str | None = None
    auth: str = "digest"


CONTROL_APIS: dict[Vendor, ControlApi] = {
    Vendor.HIKVISION: ControlApi(
        vendor=Vendor.HIKVISION,
        channel_list="/ISAPI/Streaming/channels",
        device_info="/ISAPI/System/deviceInfo",
        encoder_config="/ISAPI/Streaming/channels/{channel}01",
    ),
    Vendor.DAHUA: ControlApi(
        vendor=Vendor.DAHUA,
        channel_list="/cgi-bin/magicBox.cgi?action=getProductDefinition&name=MaxRemoteInputChannels",
        device_info="/cgi-bin/magicBox.cgi?action=getSystemInfo",
        encoder_config="/cgi-bin/configManager.cgi?action=getConfig&name=Encode",
    ),
}

# HTTP response substrings that identify a vendor. Ordered most- to least-specific; the first
# match wins.
HTTP_FINGERPRINTS: tuple[tuple[str, Vendor], ...] = (
    ("hikvision", Vendor.HIKVISION),
    ("prama", Vendor.HIKVISION),
    ("webs\r\nserver", Vendor.HIKVISION),   # Hikvision's terse embedded server banner
    ("dahua", Vendor.DAHUA),
    ("cp plus", Vendor.DAHUA),
    ("cpplus", Vendor.DAHUA),
    ("uniview", Vendor.UNIVIEW),
    ("unv", Vendor.UNIVIEW),
    ("xiongmai", Vendor.XIONGMAI),
    ("netsurveillance", Vendor.XIONGMAI),
    ("axis", Vendor.AXIS),
    ("vigi", Vendor.TPLINK),
    ("tp-link", Vendor.TPLINK),
    ("reolink", Vendor.REOLINK),
)


@dataclass
class Candidate:
    """One RTSP URL worth trying, with why we think it might work."""

    url: str
    vendor: Vendor
    channel: int
    stream: str                       # 'main' | 'sub' | 'third' | 'fallback'
    confidence: float                 # ordering hint, not a probability
    redacted: str = field(default="", repr=False)


def _redact(url: str) -> str:
    """Strip credentials so a URL can be safely logged or shown to a customer."""
    if "@" not in url:
        return url
    scheme, _, rest = url.partition("://")
    _, _, hostpart = rest.rpartition("@")
    return f"{scheme}://***:***@{hostpart}"


def candidates_for(
    host: str,
    vendor: Vendor,
    channels: int,
    *,
    port: int = 554,
    user: str | None = None,
    password: str | None = None,
    include_fallbacks: bool = True,
) -> list[Candidate]:
    """Build the ordered list of RTSP URLs to try for a device.

    Sub streams come first, deliberately. Detection runs on the sub stream — it is what every
    NVR already uses for its live wall, it is already low-bitrate, and pulling main streams for
    detection multiplies decode cost by roughly 2.24x for no accuracy gain at CCTV distances.
    The main stream is pulled on demand, for evidence frames only.
    """
    grammar = GRAMMARS.get(vendor)
    out: list[Candidate] = []

    def build(tmpl: str, channel: int) -> str:
        """Render one template, putting credentials wherever that vendor expects them.

        Most vendors take credentials in the RTSP userinfo (rtsp://user:pass@host/...).
        XiongMai boards take them as path parameters instead, which the template signals by
        containing a {user} placeholder.
        """
        url = tmpl.format(
            host=host, port=port, channel=channel,
            user=user or "", password=password or "",
        )
        creds_in_path = "{user}" in tmpl
        if user is None or creds_in_path:
            return url
        scheme, _, rest = url.partition("://")
        return f"{scheme}://{user}:{password or ''}@{rest}"

    if grammar is not None:
        # Sub stream first, deliberately — see the docstring.
        for stream, tmpl, conf in (
            ("sub", grammar.sub, 0.95),
            ("main", grammar.main, 0.90),
            ("third", grammar.third, 0.55),
        ):
            if tmpl is None:
                continue
            for ch in range(1, channels + 1):
                url = build(tmpl, ch)
                out.append(Candidate(url, vendor, ch, stream, conf, _redact(url)))

    if include_fallbacks:
        for ch in range(1, channels + 1):
            for path in FALLBACK_PATHS:
                url = build("rtsp://{host}:{port}" + path, ch)
                out.append(Candidate(url, vendor, ch, "fallback", 0.20, _redact(url)))

    out.sort(key=lambda c: (-c.confidence, c.channel, c.stream))
    return out


def fingerprint_http(banner: str) -> Vendor:
    """Identify a vendor from an HTTP response banner or body fragment."""
    low = banner.lower()
    for needle, vendor in HTTP_FINGERPRINTS:
        if needle in low:
            return vendor
    return Vendor.UNKNOWN


def fingerprint_ports(open_ports: set[int]) -> Vendor:
    """Identify a vendor from the set of open TCP ports.

    More reliable than HTTP on hardened firmware, where the web UI may be disabled but the
    proprietary SDK port is still listening.
    """
    for port, vendor in PORT_FINGERPRINTS.items():
        if port in open_ports:
            return vendor
    return Vendor.UNKNOWN

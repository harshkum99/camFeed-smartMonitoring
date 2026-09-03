"""Finding devices on a customer LAN, and measuring their clocks before we have credentials.

Discovery uses four independent methods because no single one is reliable on a real site:

  1. ONVIF WS-Discovery. Convenient when it works, but it is UDP multicast to 239.255.255.250
     with TTL 1 — it never crosses a subnet, is routinely blocked on managed switches, and
     cameras behind an NVR's PoE ports are invisible to it. ONVIF also announced that Profile S
     conformance ends 31 March 2027, so this is a convenience, never a dependency.
  2. TCP sweep of the ports that matter.
  3. HTTP banner fingerprinting.
  4. Port fingerprinting, which survives hardened firmware where the web UI is off but the
     vendor's proprietary SDK port is still listening.

The clock check deserves special mention. ONVIF `GetSystemDateAndTime` has access class PRE_AUTH
— it requires no credentials at all. That gives us every device's clock offset during discovery,
before anyone has found the DVR password, and it pre-empts the single most confusing failure in
the field: devices using WS-UsernameToken validate our request timestamp against *their* clock,
so a DVR that is ten minutes out rejects perfectly correct credentials with a generic auth error,
and an engineer spends the afternoon convinced the password is wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
import socket
import struct
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

from smartcam.survey.grammars import SCAN_PORTS, Vendor, fingerprint_http, fingerprint_ports

WS_DISCOVERY_ADDR = "239.255.255.250"
WS_DISCOVERY_PORT = 3702

_PROBE = """<?xml version="1.0" encoding="UTF-8"?>
<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope"
            xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing"
            xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery"
            xmlns:dn="http://www.onvif.org/ver10/network/wsdl">
  <e:Header>
    <w:MessageID>uuid:{mid}</w:MessageID>
    <w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>
    <w:Action e:mustUnderstand="true">
      http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>
  </e:Header>
  <e:Body><d:Probe><d:Types>dn:NetworkVideoTransmitter</d:Types></d:Probe></e:Body>
</e:Envelope>"""

_GET_TIME = """<?xml version="1.0" encoding="UTF-8"?>
<s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope">
  <s:Body><GetSystemDateAndTime xmlns="http://www.onvif.org/ver10/device/wsdl"/></s:Body>
</s:Envelope>"""


@dataclass
class Device:
    host: str
    open_ports: set[int] = field(default_factory=set)
    vendor: Vendor = Vendor.UNKNOWN
    onvif_xaddr: str | None = None
    banner: str = ""
    #: How far this device's clock is from ours, in milliseconds. Positive = device is ahead.
    clock_offset_ms: int | None = None
    clock_source: str | None = None
    timezone: str | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def clock_ok(self) -> bool:
        return self.clock_offset_ms is not None and abs(self.clock_offset_ms) < 30_000


async def ws_discover(timeout: float = 4.0) -> list[tuple[str, str]]:
    """Multicast probe. Returns (host, xaddr) pairs. Best-effort by design — an empty result
    means nothing, which is why the TCP sweep runs regardless."""
    loop = asyncio.get_running_loop()
    found: dict[str, str] = {}

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, struct.pack("b", 1))
    sock.setblocking(False)
    try:
        sock.bind(("", 0))
        msg = _PROBE.format(mid=uuid.uuid4()).encode()
        # Send several probes: single UDP probes are lost often enough to matter.
        for _ in range(3):
            try:
                sock.sendto(msg, (WS_DISCOVERY_ADDR, WS_DISCOVERY_PORT))
            except OSError:
                break
            await asyncio.sleep(0.15)

        deadline = loop.time() + timeout
        while loop.time() < deadline:
            try:
                data, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(sock, 65535), timeout=max(0.1, deadline - loop.time())
                )
            except (TimeoutError, OSError):
                break
            xaddr = _first_xaddr(data.decode("utf-8", "replace"))
            if addr[0] not in found:
                found[addr[0]] = xaddr or f"http://{addr[0]}/onvif/device_service"
    finally:
        sock.close()
    return sorted(found.items())


def _first_xaddr(xml: str) -> str | None:
    m = re.search(r"<[^>]*XAddrs[^>]*>([^<]+)<", xml, re.IGNORECASE)
    if not m:
        return None
    return m.group(1).split()[0].strip() or None


async def tcp_sweep(
    hosts: list[str],
    ports: tuple[int, ...] = SCAN_PORTS,
    *,
    timeout: float = 1.0,
    concurrency: int = 256,
) -> dict[str, set[int]]:
    """Connect-scan a list of hosts. Bounded concurrency so we do not melt a site's switch or
    look like a port scan to their IDS."""
    sem = asyncio.Semaphore(concurrency)
    results: dict[str, set[int]] = {h: set() for h in hosts}

    async def probe(host: str, port: int) -> None:
        async with sem:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=timeout
                )
                results[host].add(port)
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
            except (TimeoutError, OSError):
                pass

    await asyncio.gather(*(probe(h, p) for h in hosts for p in ports))
    return {h: p for h, p in results.items() if p}


async def http_banner(host: str, port: int = 80, *, timeout: float = 3.0) -> str:
    """Fetch enough of the device's HTTP response to fingerprint it. Raw sockets rather than an
    HTTP client, because a surprising number of these devices are not quite HTTP/1.1."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(host, port), timeout=timeout
        )
    except (TimeoutError, OSError):
        return ""
    try:
        writer.write(f"GET / HTTP/1.0\r\nHost: {host}\r\nUser-Agent: SmartCam-Survey\r\n\r\n"
                     .encode())
        await writer.drain()
        data = await asyncio.wait_for(reader.read(4096), timeout=timeout)
        return data.decode("utf-8", "replace")
    except (TimeoutError, OSError):
        return ""
    finally:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()


def parse_device_time(xml: str) -> tuple[datetime | None, str | None]:
    """Pull UTCDateTime out of a GetSystemDateAndTime response.

    Prefers UTCDateTime over LocalDateTime: the local value depends on the device's timezone
    setting, which on Indian sites is very often still UTC+00:00 from the factory even though
    the on-screen clock shows IST.
    """
    tz = None
    tzm = re.search(r"<[^>]*TZ>([^<]+)<", xml)
    if tzm:
        tz = tzm.group(1).strip()

    block = re.search(r"<[^>]*UTCDateTime>(.*?)</[^>]*UTCDateTime>", xml, re.DOTALL)
    if not block:
        return None, tz
    body = block.group(1)

    def get(tag: str) -> int | None:
        m = re.search(rf"<[^>]*{tag}>\s*(\d+)\s*<", body)
        return int(m.group(1)) if m else None

    y, mo, d = get("Year"), get("Month"), get("Day")
    h, mi, s = get("Hour"), get("Minute"), get("Second")
    if None in (y, mo, d, h, mi, s):
        return None, tz
    try:
        return datetime(y, mo, d, h, mi, s, tzinfo=UTC), tz
    except ValueError:
        return None, tz


async def onvif_clock(
    host: str,
    xaddr: str | None = None,
    *,
    timeout: float = 4.0,
) -> tuple[int | None, str | None]:
    """Measure a device's clock offset in milliseconds. Requires NO credentials.

    Returns (offset_ms, timezone). Positive offset means the device is ahead of us.
    """
    import httpx

    paths = [xaddr] if xaddr else []
    paths += [f"http://{host}/onvif/device_service", f"http://{host}:8000/onvif/device_service"]

    headers = {
        "Content-Type": 'application/soap+xml; charset=utf-8; '
                        'action="http://www.onvif.org/ver10/device/wsdl/GetSystemDateAndTime"',
    }
    async with httpx.AsyncClient(timeout=timeout, verify=False) as client:  # noqa: S501
        for url in paths:
            if not url:
                continue
            try:
                sent = datetime.now(UTC)
                r = await client.post(url, content=_GET_TIME, headers=headers)
                recv = datetime.now(UTC)
            except Exception:  # noqa: BLE001 - any transport failure means "try the next path"
                continue
            if r.status_code != 200:
                continue
            device_time, tz = parse_device_time(r.text)
            if device_time is None:
                continue
            # Compare against the midpoint of our request window, so network latency does not
            # masquerade as clock drift.
            ours = sent + (recv - sent) / 2
            return int((device_time - ours).total_seconds() * 1000), tz
    return None, None


async def discover(
    hosts: list[str],
    *,
    use_ws_discovery: bool = True,
    check_clocks: bool = True,
    timeout: float = 1.0,
) -> list[Device]:
    """Full discovery pass over a set of hosts. Never raises; unreachable is a result."""
    ws: dict[str, str] = {}
    if use_ws_discovery:
        try:
            ws = dict(await ws_discover())
        except OSError:
            ws = {}

    all_hosts = sorted(set(hosts) | set(ws))
    open_ports = await tcp_sweep(all_hosts, timeout=timeout)

    devices: list[Device] = []
    for host in all_hosts:
        ports = open_ports.get(host, set())
        if not ports and host not in ws:
            continue
        dev = Device(host=host, open_ports=ports, onvif_xaddr=ws.get(host))

        dev.vendor = fingerprint_ports(ports)
        if dev.vendor is Vendor.UNKNOWN:
            for p in (80, 8080, 443):
                if p in ports:
                    dev.banner = await http_banner(host, p)
                    dev.vendor = fingerprint_http(dev.banner)
                    if dev.vendor is not Vendor.UNKNOWN:
                        break

        if check_clocks:
            offset, tz = await onvif_clock(host, dev.onvif_xaddr)
            dev.clock_offset_ms, dev.timezone = offset, tz
            if offset is None:
                dev.notes.append(
                    "clock not readable over ONVIF; timestamps will rely on our own clock and "
                    "cannot be cross-checked against the recorder's on-screen time"
                )
            elif abs(offset) >= 30_000:
                dev.notes.append(
                    f"clock is {_humanise(offset)} — this WILL corrupt time-range answers, and "
                    f"on devices using WS-UsernameToken it also causes correct credentials to "
                    f"be rejected with a generic authentication error"
                )
            if tz and "00:00" in tz:
                dev.notes.append(
                    f"timezone reported as {tz}; if the on-screen clock shows IST this device "
                    f"is displaying local time without a timezone set"
                )
        devices.append(dev)
    return devices


def _humanise(offset_ms: int) -> str:
    direction = "ahead of" if offset_ms > 0 else "behind"
    secs = abs(offset_ms) / 1000
    if secs < 90:
        return f"{secs:.0f}s {direction} ours"
    if secs < 5400:
        return f"{secs / 60:.0f}m {direction} ours"
    return f"{secs / 3600:.1f}h {direction} ours"


def expand_cidr(cidr: str) -> list[str]:
    """Expand a CIDR into host addresses. Refuses anything larger than a /22, because sweeping a
    customer's whole network unasked is how you end up in their SOC's incident report."""
    import ipaddress

    net = ipaddress.ip_network(cidr, strict=False)
    if net.num_addresses > 1024:
        raise ValueError(
            f"{cidr} covers {net.num_addresses} addresses; refusing to sweep more than 1024. "
            f"Narrow the range to the camera VLAN."
        )
    return [str(h) for h in net.hosts()]

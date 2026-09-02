"""`sanjay-survey` — read-only site survey.

Run this on a customer's network before quoting. It changes nothing: it discovers recorders,
measures each channel's real resolution, frame rate, codec and keyframe interval, grades what
each camera can actually support, and sizes the appliance.

    sanjay-survey scan   --cidr 192.168.1.0/24
    sanjay-survey probe  --host 192.168.1.64 --vendor hikvision --channels 16 --user admin
    sanjay-survey run    --cidr 192.168.1.0/24 --channels 16 --user admin --out survey

Credentials are read from a prompt or the SANJAY_CAM_PASSWORD environment variable, never from
an argument, so they do not end up in shell history or a process list.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from pathlib import Path

from sanjay.survey.discovery import Device, discover, expand_cidr
from sanjay.survey.grade import ChannelGrade, ChannelProbe, grade_channel, size_box
from sanjay.survey.grammars import GRAMMARS, Vendor, candidates_for
from sanjay.survey.probe import ffprobe_available, probe_stream
from sanjay.survey.report import SurveyResult, now_iso, to_json, to_markdown


def _password(explicit: bool) -> str | None:
    env = os.environ.get("SANJAY_CAM_PASSWORD")
    if env:
        return env
    if explicit:
        return getpass.getpass("Camera/DVR password: ")
    return None


async def _probe_channels(
    host: str,
    vendor: Vendor,
    channels: int,
    *,
    user: str | None,
    password: str | None,
    port: int,
    concurrency: int,
    timeout: float,
) -> tuple[dict[int, ChannelProbe], list[tuple[int, str]]]:
    """Find one working stream per channel, preferring the sub stream.

    Concurrency is capped hard. Opening one RTSP session per channel per consumer is how you
    exhaust a recorder's session limit and degrade the customer's own live view — and if Sanjay
    makes their existing CCTV worse, the deal is dead regardless of how good the AI is.
    """
    cands = candidates_for(host, vendor, channels, port=port, user=user, password=password)
    by_channel: dict[int, list] = {}
    for c in cands:
        by_channel.setdefault(c.channel, []).append(c)

    sem = asyncio.Semaphore(concurrency)
    probes: dict[int, ChannelProbe] = {}
    failures: list[tuple[int, str]] = []

    async def one(channel: int, options: list) -> None:
        last_error = "no candidate URL responded"
        async with sem:
            for cand in options:
                p = await asyncio.to_thread(
                    probe_stream, cand.url, channel,
                    stream_kind=cand.stream, timeout=timeout,
                )
                if p.reachable:
                    p.has_substream = cand.stream in {"sub", "third"}
                    probes[channel] = p
                    return
                last_error = p.error or last_error
                # An auth failure will fail identically on every other URL for this device, so
                # stop rather than hammering the recorder with 40 more doomed attempts.
                if p.error and "authentication" in p.error:
                    break
        failures.append((channel, last_error))

    await asyncio.gather(*(one(ch, opts) for ch, opts in by_channel.items()))
    return probes, failures


def _resolve_vendor(name: str | None, devices: list[Device]) -> Vendor:
    if name:
        try:
            return Vendor(name.lower())
        except ValueError:
            sys.exit(f"unknown vendor '{name}'. Known: {', '.join(v.value for v in GRAMMARS)}")
    for d in devices:
        if d.vendor is not Vendor.UNKNOWN:
            return d.vendor
    return Vendor.UNKNOWN


def _emit(result: SurveyResult, out: str | None) -> None:
    md = to_markdown(result)
    if out:
        Path(f"{out}.md").write_text(md, encoding="utf-8")
        Path(f"{out}.json").write_text(to_json(result), encoding="utf-8")
        print(f"wrote {out}.md and {out}.json", file=sys.stderr)
    print(md)


async def cmd_scan(a: argparse.Namespace) -> int:
    hosts = list(a.host or [])
    if a.cidr:
        try:
            hosts += expand_cidr(a.cidr)
        except ValueError as e:
            sys.exit(str(e))
    if not hosts:
        sys.exit("give --cidr or at least one --host")

    print(f"sweeping {len(hosts)} addresses…", file=sys.stderr)
    devices = await discover(hosts, use_ws_discovery=not a.no_discovery,
                             check_clocks=not a.no_clock, timeout=a.timeout)
    result = SurveyResult(site_name=a.site, surveyed_at=now_iso(), surveyor=a.surveyor,
                          devices=devices)
    _emit(result, a.out)
    return 0 if devices else 1


async def cmd_probe(a: argparse.Namespace) -> int:
    if not ffprobe_available():
        sys.exit("ffprobe not found on PATH. Install ffmpeg.")
    vendor = _resolve_vendor(a.vendor, [])
    password = _password(a.ask_password)
    probes, failures = await _probe_channels(
        a.host, vendor, a.channels, user=a.user, password=password,
        port=a.port, concurrency=a.concurrency, timeout=a.timeout,
    )
    grades = [grade_channel(p) for p in probes.values()]
    sizing = size_box(grades, probes, box=a.box, motion_duty=a.motion_duty)
    result = SurveyResult(site_name=a.site, surveyed_at=now_iso(), surveyor=a.surveyor,
                          probes=probes, grades=grades, sizing=sizing, unreachable=failures)
    _emit(result, a.out)
    return 0 if probes else 1


async def cmd_run(a: argparse.Namespace) -> int:
    if not ffprobe_available():
        sys.exit("ffprobe not found on PATH. Install ffmpeg.")
    hosts = list(a.host or [])
    if a.cidr:
        try:
            hosts += expand_cidr(a.cidr)
        except ValueError as e:
            sys.exit(str(e))
    if not hosts:
        sys.exit("give --cidr or at least one --host")

    print(f"sweeping {len(hosts)} addresses…", file=sys.stderr)
    devices = await discover(hosts, use_ws_discovery=not a.no_discovery,
                             check_clocks=not a.no_clock, timeout=a.timeout)
    if not devices:
        result = SurveyResult(site_name=a.site, surveyed_at=now_iso(), surveyor=a.surveyor)
        _emit(result, a.out)
        return 1

    print(f"found {len(devices)} device(s); probing channels…", file=sys.stderr)
    password = _password(a.ask_password)

    all_probes: dict[int, ChannelProbe] = {}
    all_failures: list[tuple[int, str]] = []
    offset = 0
    for dev in devices:
        vendor = _resolve_vendor(a.vendor, [dev])
        probes, failures = await _probe_channels(
            dev.host, vendor, a.channels, user=a.user, password=password,
            port=a.port, concurrency=a.concurrency, timeout=a.timeout,
        )
        # Multiple recorders on one site share a channel-number space, so offset them.
        for ch, p in probes.items():
            p.channel = ch + offset
            all_probes[p.channel] = p
        all_failures += [(ch + offset, why) for ch, why in failures]
        offset += a.channels

    grades: list[ChannelGrade] = [grade_channel(p) for p in all_probes.values()]
    sizing = size_box(grades, all_probes, box=a.box, motion_duty=a.motion_duty)
    result = SurveyResult(site_name=a.site, surveyed_at=now_iso(), surveyor=a.surveyor,
                          devices=devices, probes=all_probes, grades=grades,
                          sizing=sizing, unreachable=all_failures)
    _emit(result, a.out)
    return 0 if all_probes else 1


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sanjay-survey",
        description="Read-only CCTV site survey. Changes nothing on the customer's equipment.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    def common(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--site", default="Unnamed site")
        sp.add_argument("--surveyor", default=os.environ.get("USER", "unknown"))
        sp.add_argument("--out", help="write <out>.md and <out>.json")
        sp.add_argument("--timeout", type=float, default=1.0)

    def probing(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("--vendor", help="force a vendor grammar instead of fingerprinting")
        sp.add_argument("--channels", type=int, default=16)
        sp.add_argument("--port", type=int, default=554)
        sp.add_argument("--user")
        sp.add_argument("--ask-password", action="store_true",
                        help="prompt for the password (or set SANJAY_CAM_PASSWORD)")
        sp.add_argument("--concurrency", type=int, default=4,
                        help="max simultaneous RTSP sessions; keep low to avoid exhausting "
                             "the recorder and degrading the customer's own live view")
        sp.add_argument("--box", default="edge-s", help="appliance to size against")
        sp.add_argument("--motion-duty", type=float, default=0.30,
                        help="fraction of the day cameras see motion; the dominant capacity "
                             "variable. Use 1.0 for a busy outdoor site")

    s = sub.add_parser("scan", help="discover recorders and check their clocks")
    common(s)
    s.add_argument("--cidr")
    s.add_argument("--host", action="append")
    s.add_argument("--no-discovery", action="store_true", help="skip ONVIF WS-Discovery")
    s.add_argument("--no-clock", action="store_true", help="skip the ONVIF clock check")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("probe", help="probe and grade one recorder's channels")
    common(s)
    probing(s)
    s.add_argument("--host", required=True)
    s.set_defaults(fn=cmd_probe)

    s = sub.add_parser("run", help="discover, probe, grade and size in one pass")
    common(s)
    probing(s)
    s.add_argument("--cidr")
    s.add_argument("--host", action="append")
    s.add_argument("--no-discovery", action="store_true")
    s.add_argument("--no-clock", action="store_true")
    s.set_defaults(fn=cmd_run)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())

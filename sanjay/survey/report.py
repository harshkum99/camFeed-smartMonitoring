"""The site survey report — the customer-facing output, and a pre-sales artefact we charge for.

This document answers the question no surveillance vendor answers honestly: which of your
cameras can actually do this? Handing a prospect a graded, per-channel assessment before quoting
does three things at once — it prices the deal from evidence rather than from a camera count, it
sets expectations we can meet, and it turns their oldest, worst cameras from a broken promise
into a consulting finding.

Output is deliberately plain: it gets read on a phone in a plant office.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime

from sanjay.survey.discovery import Device
from sanjay.survey.grade import BOXES, BoxSizing, ChannelGrade, ChannelProbe, Grade, max_cameras

GRADE_LABEL: dict[Grade, str] = {
    Grade.RECOGNITION: "Recognition-grade",
    Grade.ANPR:        "Plate-reading",
    Grade.DETECTION:   "Detection-grade",
    Grade.DEGRADED:    "Degraded",
    Grade.UNSERVABLE:  "Not usable",
}

GRADE_MEANING: dict[Grade, str] = {
    Grade.RECOGNITION: "enough facial detail to match an enrolled person",
    Grade.ANPR:        "enough plate detail to read number plates",
    Grade.DETECTION:   "people, vehicles, zones, dwell time and PPE rules",
    Grade.DEGRADED:    "usable, but consumes roughly twice the appliance capacity",
    Grade.UNSERVABLE:  "cannot be used as configured",
}


@dataclass
class SurveyResult:
    site_name: str
    surveyed_at: str
    surveyor: str
    devices: list[Device] = field(default_factory=list)
    probes: dict[int, ChannelProbe] = field(default_factory=dict)
    grades: list[ChannelGrade] = field(default_factory=list)
    sizing: BoxSizing | None = None
    #: Channels we could not reach at all, with the reason. Distinct from graded-unservable:
    #: these usually mean a credentials problem, which is a commercial issue, not a technical one.
    unreachable: list[tuple[int, str]] = field(default_factory=list)

    def counts(self) -> dict[str, int]:
        out = {g.value: 0 for g in Grade}
        for g in self.grades:
            out[g.grade.value] += 1
        return out


def to_json(r: SurveyResult) -> str:
    def enc(o):
        if isinstance(o, set):
            return sorted(o)
        if hasattr(o, "value"):
            return o.value
        return str(o)

    payload = {
        "site": r.site_name,
        "surveyed_at": r.surveyed_at,
        "surveyor": r.surveyor,
        "summary": r.counts(),
        "devices": [asdict(d) for d in r.devices],
        "channels": [
            {**asdict(g), "probe": asdict(r.probes[g.channel]) if g.channel in r.probes else None}
            for g in r.grades
        ],
        "sizing": asdict(r.sizing) if r.sizing else None,
        "unreachable": r.unreachable,
    }
    return json.dumps(payload, indent=2, default=enc)


def to_markdown(r: SurveyResult) -> str:
    c = r.counts()
    servable = sum(v for k, v in c.items() if k != Grade.UNSERVABLE.value)
    total = sum(c.values())
    L: list[str] = []
    add = L.append

    add(f"# Site survey — {r.site_name}")
    add("")
    add(f"Surveyed {r.surveyed_at} by {r.surveyor}. Read-only: no device settings were changed.")
    add("")

    # --- headline -----------------------------------------------------------------------
    add("## What we found")
    add("")
    if total == 0:
        add("No camera channels could be enumerated. See *Devices* below — this is almost")
        add("always a credentials issue rather than a technical one.")
    else:
        add(f"**{servable} of {total} channels are usable.**")
        add("")
        add("| Capability | Channels | What it means |")
        add("|---|---:|---|")
        for g in Grade:
            if c[g.value]:
                add(f"| {GRADE_LABEL[g]} | {c[g.value]} | {GRADE_MEANING[g]} |")
        add("")
        if c[Grade.RECOGNITION.value] == 0:
            add("> **No camera on this site is recognition-grade.** Face matching needs at least")
            add("> 64 pixels between the eye centres. Nothing recovers detail a camera never")
            add("> captured — upscaling invents it, which is worse than useless as evidence.")
            add("> Face recognition would need one repositioned or replaced camera at each")
            add("> entry point; everything else on this list works today.")
            add("")

    # --- devices ------------------------------------------------------------------------
    if r.devices:
        add("## Devices")
        add("")
        add("| Address | Identified as | Open ports | Clock |")
        add("|---|---|---|---|")
        for d in r.devices:
            ports = ", ".join(str(p) for p in sorted(d.open_ports)) or "—"
            if d.clock_offset_ms is None:
                clock = "not readable"
            elif abs(d.clock_offset_ms) < 2_000:
                clock = "correct"
            else:
                clock = _drift(d.clock_offset_ms)
            add(f"| `{d.host}` | {d.vendor.value} | {ports} | {clock} |")
        add("")
        notes = [(d.host, n) for d in r.devices for n in d.notes]
        if notes:
            add("**Notes**")
            add("")
            for host, note in notes:
                add(f"- `{host}` — {note}")
            add("")

    # --- channels -----------------------------------------------------------------------
    if r.grades:
        add("## Channel detail")
        add("")
        add("| Ch | Stream | Resolution | FPS | Codec | Keyframe | Grade |")
        add("|---:|---|---|---:|---|---|---|")
        for g in sorted(r.grades, key=lambda x: x.channel):
            p = r.probes.get(g.channel)
            res = f"{p.width}x{p.height}" if p and p.width else "—"
            fps = f"{p.fps:.0f}" if p and p.fps else "—"
            gop = f"{p.gop_ms} ms" if p and p.gop_ms else "—"
            add(f"| {g.channel} | {p.stream if p else '—'} | {res} | {fps} | "
                f"{(p.codec if p else None) or '—'} | {gop} | {GRADE_LABEL[g.grade]} |")
        add("")

        flagged = [g for g in r.grades if g.warnings or g.grade is Grade.UNSERVABLE]
        if flagged:
            add("### Channels needing attention")
            add("")
            for g in sorted(flagged, key=lambda x: x.channel):
                add(f"**Channel {g.channel} — {GRADE_LABEL[g.grade]}**")
                add("")
                for reason in g.reasons:
                    add(f"- {reason}")
                for w in g.warnings:
                    add(f"- {w}")
                add("")

    # --- appliance ----------------------------------------------------------------------
    if r.sizing:
        s = r.sizing
        add("## Appliance sizing")
        add("")
        add(f"Sized against **{s.box}** at a {s.detect_fps:.0f} fps analysis rate and "
            f"{s.motion_duty * 100:.0f}% motion duty.")
        add("")
        add("| | |")
        add("|---|---|")
        add(f"| Channels carried | {s.cameras} |")
        add(f"| Detector load | {s.inferences_required:.0f} of "
            f"{s.inferences_available:.0f} inferences/sec |")
        add(f"| Shared memory required | {s.shm_required_gb:.2f} GB |")
        add(f"| Tracker CPU required | {s.tracker_cores_required:.1f} cores |")
        add("")
        if s.fits:
            add(f"**This fits, with {s.headroom_pct:.0f}% detector headroom.**")
        else:
            add("**This does not fit as specified.**")
            add("")
            for limit in s.limits:
                add(f"- {limit}")
        add("")
        add("Motion duty is the dominant variable and it is site-specific. For reference, "
            f"{BOXES['edge-s'].name} carries {max_cameras('edge-s', motion_duty=0.3)} channels "
            f"at a quiet indoor 30% duty and only {max_cameras('edge-s', motion_duty=1.0)} on a "
            "busy outdoor yard that sees motion all day.")
        add("")

    # --- unreachable --------------------------------------------------------------------
    if r.unreachable:
        add("## Channels we could not reach")
        add("")
        add("These are usually a credentials or configuration issue rather than a hardware one, "
            "and they are the most common reason a deployment slips.")
        add("")
        for ch, why in sorted(r.unreachable):
            add(f"- **Channel {ch}** — {why}")
        add("")

    add("---")
    add("")
    add("*Read-only survey. No recorder settings were modified. Where this report recommends a "
        "change, it is to a secondary (sub) stream only — never to the main stream your "
        "recorder writes to disk, so your recording quality and retention are unaffected.*")
    return "\n".join(L)


def _drift(ms: int) -> str:
    direction = "ahead" if ms > 0 else "behind"
    s = abs(ms) / 1000
    if s < 90:
        return f"{s:.0f}s {direction}"
    if s < 5400:
        return f"{s / 60:.0f}m {direction}"
    return f"{s / 3600:.1f}h {direction}"


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")

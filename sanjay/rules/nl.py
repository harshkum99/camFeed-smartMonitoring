"""Authoring a rule in plain English — and reading it back before it goes live.

The model runs at AUTHORING time only. It never touches the hot path: once a rule is saved it is
a JSON document evaluated by a deterministic engine, so a model that has a bad day cannot change
what a live site alerts on.

The read-back is the load-bearing half. An operator types "alert me if anyone enters the
restricted zone after 8pm", and before it is saved we render the compiled rule back into English
and show the zone drawn on a live frame. That round trip catches the failure this feature is
otherwise prone to: a rule that looks plausible, is saved, and quietly watches the wrong zone or
the wrong hours for a month.

`explain` is deliberately blunt about what a rule will cost the operator, because the numbers
that matter are not the ones they typed — how soon it can fire, how noisy it is likely to be, and
what happens when nobody acts on it.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sanjay.query.nl import ModelError, Provider, _loads, _resolve
from sanjay.rules.schema import (
    ATTRIBUTES,
    CLASSES,
    TRIGGERS,
    Rule,
    RuleError,
    parse_rule,
)

MAX_INSTRUCTION_CHARS = 400

SYSTEM = """You turn a plain-English monitoring instruction into a JSON rule document. You never \
evaluate anything and never invent places that were not listed.

Emit ONLY a JSON object:

  name        a short label an operator will recognise in an alert list
  severity    "info", "warn" or "critical"
  cameras     list of camera NAMES from the list given. Empty if no place is named.
  trigger     {
                type            one of: zone_entry, zone_exit, line_cross, dwell, absence,
                                count, attribute
                object_class    person, vehicle, bag, forklift, pallet, animal
                zone            a zone NAME from the list given
                direction       into, out_of, any        (line_cross only)
                dwell_seconds   integer                   (dwell and absence only, required)
                count_threshold integer                   (count only)
                attributes      list of {key, op, value}, e.g.
                                [{"key":"helmet","op":"lt","value":0.5}]
              }
  schedule    { "windows": [{"from":"HH:MM","to":"HH:MM"}], "days": ["mon", ...] }

Rules that matter:
  - Attributes are CONFIDENCES from 0 to 1, never true/false. "without a helmet" is
    {"key":"helmet","op":"lt","value":0.5}. "wearing a helmet" uses "gt".
  - "after 8pm" is a window from 20:00 to 06:00. A window that ends before it starts wraps
    midnight, which is correct and expected.
  - "blocked for more than N seconds" is type "dwell" with dwell_seconds N.
  - "nobody in X for N minutes" is type "absence".
  - Use ONLY zone and camera names from the lists. If the instruction names a place that is not
    listed, put it in "unknown_place" and still emit your best rule.
  - If the instruction cannot be expressed with these triggers, emit
    {"unsupported": "<short reason>"}. That is a correct answer, not a failure.

Return the JSON object and nothing else."""


@dataclass
class CompiledRule:
    rule: Rule | None
    raw: dict[str, Any]
    unsupported: str | None = None
    unknown_place: str | None = None
    unresolved: list[str] = field(default_factory=list)


def compile_rule(
    instruction: str,
    catalog,                       # sanjay.query.nl.Catalog
    provider: Provider,
    *,
    site_id: str,
    rule_id: str = "",
) -> CompiledRule:
    """Turn an instruction into a validated rule. One repair attempt, then decline.

    A rule we cannot compile is one the operator should rewrite, not one we should guess at —
    a wrong rule runs unattended for weeks.
    """
    instruction = (instruction or "").strip()
    if not instruction:
        return CompiledRule(None, {}, unsupported="empty instruction")
    if len(instruction) > MAX_INSTRUCTION_CHARS:
        return CompiledRule(None, {}, unsupported="instruction is too long to interpret reliably")

    user = _prompt(instruction, catalog)
    raw = _ask(provider, user)
    if isinstance(raw.get("unsupported"), str):
        return CompiledRule(None, raw, unsupported=raw["unsupported"])

    payload, unresolved = _resolve_names(raw, catalog)
    try:
        return CompiledRule(parse_rule(payload, rule_id=rule_id, site_id=site_id), raw,
                            unknown_place=raw.get("unknown_place"), unresolved=unresolved)
    except RuleError as first:
        repair = (f"{user}\n\nYour previous answer was rejected: {first}\n"
                  f"Previous answer: {json.dumps(raw)[:600]}\n"
                  f"Emit a corrected JSON object, or {{\"unsupported\": \"...\"}}.")
        raw2 = _ask(provider, repair)
        if isinstance(raw2.get("unsupported"), str):
            return CompiledRule(None, raw2, unsupported=raw2["unsupported"])
        payload2, unresolved2 = _resolve_names(raw2, catalog)
        try:
            return CompiledRule(parse_rule(payload2, rule_id=rule_id, site_id=site_id), raw2,
                                unknown_place=raw2.get("unknown_place"), unresolved=unresolved2)
        except RuleError as second:
            return CompiledRule(None, raw2, unsupported=str(second))


def _ask(provider: Provider, user: str) -> dict[str, Any]:
    try:
        out = provider.complete(SYSTEM, user)
    except Exception as e:  # noqa: BLE001
        raise ModelError(str(e)) from e
    if not isinstance(out, dict):
        raise ModelError(f"provider returned {type(out).__name__}, expected a JSON object")
    return out


def _prompt(instruction: str, catalog) -> str:
    lines = [f"Instruction: {instruction}", "", "Cameras at this site:"]
    lines += [f"  {n}" for n in sorted(catalog.cameras)]
    lines += ["", "Zones and lines:"]
    lines += [f"  {n}" for n in sorted(catalog.zones)] or ["  (none defined yet)"]
    lines += ["", f"Trigger types: {', '.join(sorted(TRIGGERS))}"]
    lines += [f"Object classes: {', '.join(sorted(CLASSES))}"]
    lines += [f"Attributes: {', '.join(sorted(ATTRIBUTES))}"]
    return "\n".join(lines)


def _resolve_names(raw: dict[str, Any], catalog) -> tuple[dict[str, Any], list[str]]:
    """Zones stay as names in the rule document — the engine matches on the zone names the
    detector emits — but camera names still resolve to ids, and anything unresolvable is
    reported rather than dropped."""
    payload = dict(raw)
    unresolved: list[str] = []

    ids = []
    for name in raw.get("cameras") or []:
        if not isinstance(name, str):
            continue
        cid = _resolve(name, catalog.cameras)
        (ids.append(cid) if cid else unresolved.append(f"camera '{name}'"))
    payload["cameras"] = ids

    trigger = dict(raw.get("trigger") or {})
    zone = trigger.get("zone")
    if isinstance(zone, str) and zone and catalog.zones and not _resolve(zone, catalog.zones):
        unresolved.append(f"zone '{zone}'")
    payload["trigger"] = trigger
    return payload, unresolved


# --- read-back -------------------------------------------------------------------------------

def explain(rule: Rule, camera_names: dict[str, str] | None = None) -> str:
    """Render a compiled rule back into English for the operator to confirm before it goes live.

    This is shown next to the zone drawn on a live frame. Between the sentence and the drawing,
    an operator catches a misread instruction in seconds — which is the only reason it is safe
    to let a model author rules at all.
    """
    names = camera_names or {}
    t = rule.trigger
    who = t.object_class
    if t.attributes:
        bits = []
        for a in t.attributes:
            state = "without a" if a["op"] in ("lt", "lte") else "wearing a"
            bits.append(f"{state} {a['key']}")
        who = f"a {who} {' and '.join(bits)}"
    else:
        who = f"a {who}"

    # Prepositions differ per trigger: you enter a zone but stay *in* one. Getting this wrong
    # produces "enters in Chemical store", and this sentence is the safety mechanism — an
    # operator who is squinting at broken grammar is not checking whether the rule is right.
    zone = t.zone or ""
    action = {
        "zone_entry": f"{who} enters {zone}".rstrip(),
        "zone_exit": f"{who} leaves {zone}".rstrip(),
        "line_cross": f"{who} crosses {zone}"
                      + ("" if t.direction == "any" else f" ({t.direction.replace('_', ' ')})"),
        "dwell": f"{who} stays in {zone} for more than {t.dwell_seconds} seconds",
        "absence": f"{zone} stays empty for more than {t.dwell_seconds} seconds",
        "count": f"{t.count_threshold} or more {t.object_class}s are in {zone} at once",
        "attribute": (f"{who} is seen in {zone}" if zone else f"{who} is seen"),
    }[t.type]

    sentence = f"Alert when {action}"
    if rule.schedule.windows:
        parts = [f"between {a} and {b}" for a, b in rule.schedule.windows]
        sentence += ", " + " or ".join(parts)
    if rule.schedule.days:
        sentence += ", on " + ", ".join(d.capitalize() for d in rule.schedule.days)
    sentence += "."

    where_cams = (", ".join(names.get(c, c) for c in rule.cameras)
                  if rule.cameras else "every camera at this site")
    lines = [
        sentence,
        f"Watching: {where_cams}.",
        f"Severity: {rule.severity}.",
    ]

    # The numbers that actually matter to the person who has to live with this rule.
    lines.append(
        f"Confirmation: {rule.confirmation.min_frames} frames must agree before it fires"
        + (", and an AI second opinion checks each one" if rule.verification.mode != "none"
           else "") + "."
    )
    lines.append(
        f"Limits: at most one alert per object every {rule.suppression.debounce_seconds}s, "
        f"{rule.suppression.max_per_hour} per hour per camera, and the rule mutes itself after "
        f"{rule.suppression.quiet_after_n_unactioned} alerts nobody acts on."
    )
    if rule.trigger.type in ("dwell", "absence"):
        lines.append(
            f"Soonest this can alert: {rule.min_alert_latency_s:.0f}s after the situation "
            f"begins — the {rule.trigger.dwell_seconds}s wait is part of the rule."
        )
    else:
        lines.append("Soonest this can alert: about 1 second, on your live console.")

    for note in rule.notes:
        lines.append(f"Note: {note}")
    return "\n".join(lines)


class StubRuleProvider:
    """Deterministic authoring for tests and the offline demo.

    Handles the rule shapes a factory actually writes first, and declines everything else — a
    stub that always succeeds would hide the decline path it exists to exercise.
    """

    def complete(self, system: str, user: str) -> dict[str, Any]:
        text = user.split("\n", 1)[0].removeprefix("Instruction:").strip().lower()
        zones = _listed(user, "Zones and lines:")
        cams = _listed(user, "Cameras at this site:")

        zone = next((z for z in zones if _tokens(z) & _tokens(text)), None)
        cameras = [c for c in cams if _tokens(c) & _tokens(text)]

        schedule: dict[str, Any] = {}
        if "after 8" in text or "after 20" in text or "night" in text:
            schedule = {"windows": [{"from": "20:00", "to": "06:00"}]}
        elif "weekend" in text:
            schedule = {"days": ["sat", "sun"]}

        secs = _seconds(text)

        if "helmet" in text or "ppe" in text:
            return {"name": "PPE — helmet", "severity": "warn", "cameras": cameras,
                    "schedule": schedule,
                    "trigger": {"type": "zone_entry", "object_class": "person", "zone": zone,
                                "attributes": [{"key": "helmet", "op": "lt", "value": 0.5}]}}
        if "block" in text and secs:
            return {"name": "Obstruction", "severity": "critical", "cameras": cameras,
                    "schedule": schedule,
                    "trigger": {"type": "dwell", "object_class": "pallet", "zone": zone,
                                "dwell_seconds": secs}}
        if "empty" in text or "unattended" in text:
            return {"name": "Unattended", "severity": "warn", "cameras": cameras,
                    "schedule": schedule,
                    "trigger": {"type": "absence", "object_class": "person", "zone": zone,
                                "dwell_seconds": secs or 300}}
        if "enter" in text or "restricted" in text or "anyone" in text:
            return {"name": "Restricted zone", "severity": "critical", "cameras": cameras,
                    "schedule": schedule,
                    "trigger": {"type": "zone_entry", "object_class": "person", "zone": zone}}
        return {"unsupported": "this instruction is outside what the stub provider handles"}


def _listed(user: str, header: str) -> list[str]:
    out, grab = [], False
    for line in user.splitlines():
        if line.strip() == header:
            grab = True
            continue
        if grab:
            if not line.startswith("  ") or not line.strip():
                break
            v = line.strip()
            if not v.startswith("("):
                out.append(v)
    return out


def _tokens(s: str) -> set[str]:
    import re
    return {w for w in re.sub(r"[^a-z0-9]+", " ", s.lower()).split() if len(w) > 2}


def _seconds(text: str) -> int:
    import re
    m = re.search(r"(\d+)\s*(second|sec|minute|min|hour)", text)
    if not m:
        return 0
    n, unit = int(m.group(1)), m.group(2)
    return n * {"second": 1, "sec": 1, "minute": 60, "min": 60, "hour": 3600}[unit]


__all__ = ["CompiledRule", "StubRuleProvider", "compile_rule", "explain"]


def loads(text: str) -> dict[str, Any]:
    """Re-exported so a provider implementation can share the fence-tolerant JSON parsing."""
    return _loads(text)

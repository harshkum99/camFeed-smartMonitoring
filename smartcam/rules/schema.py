"""The rule document: what an operator can express, and what we refuse to let them express.

A rule is data, not code. It is stored as JSON, validated here, and evaluated by a small
deterministic engine — no expression language, no eval, nothing a rule author can write that
reaches the host. That is partly a security position and mostly a portability one: the same
document has to produce the same alerts in the browser preview, on the edge agent, and in the
cloud, and the only way to guarantee that is to keep it declarative.

The shape comes from what operators actually ask for, which is five clauses:

    WHO       an object class, optionally with attributes    person, no helmet
    WHERE     a zone or a line, on named cameras             Zone B, the fire exit
    WHEN      time windows and weekdays                      after 20:00, weekends
    HOW LONG  a dwell or absence threshold                   for more than 60 seconds
    HOW MANY  a count threshold                              more than 4 people

That covers the overwhelming majority of real requests, and it is a form rather than a language,
which is what makes a visual editor possible.

The two blocks that matter most are `confirmation` and `suppression`. They are not tuning knobs —
they are the product. The industry anchor for alarm systems is that 94-98% of activations are
false, and an operator who is shown that mutes the app in week two. So the defaults here are
deliberately conservative, and validation refuses a few combinations outright.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# --- vocabulary --------------------------------------------------------------------------

TRIGGERS = frozenset({
    "zone_entry",      # crossed into an area
    "zone_exit",
    "line_cross",      # crossed a directed line
    "dwell",           # stayed in a zone longer than N seconds
    "absence",         # zone was empty for longer than N seconds
    "count",           # more than N objects present at once
    "attribute",       # an attribute condition on any object present
})

SEVERITIES = ("info", "warn", "critical")
DIRECTIONS = ("into", "out_of", "any")
CLASSES = frozenset({"person", "vehicle", "bag", "forklift", "pallet", "animal"})

#: Attributes we can test. Stored as confidences 0-1, never booleans — "no helmet" is a
#: threshold, because a person facing away is not a person without a helmet.
ATTRIBUTES = frozenset({"helmet", "vest", "mask", "gloves"})

#: Triggers whose condition is defined by elapsed time. The 2-second alert SLA applies to the
#: moment the condition is MET, not to the moment the situation began — a "blocked for 60
#: seconds" rule cannot, by its own definition, alert in under 60 seconds. Sales needs to know
#: this; the validator records it so the UI can say it.
TIME_BASED = frozenset({"dwell", "absence"})


class RuleError(ValueError):
    """A rule we refuse to accept. The message is shown to the operator, so it says what to do."""


# --- document ----------------------------------------------------------------------------

@dataclass
class Confirmation:
    """False-alarm control. The single highest-value block in the document.

    Every frame of confirmation costs roughly 125 ms at an 8 fps analytic rate, so three frames
    is about a third of the sub-2-second budget. There will be pressure to cut this to win a
    latency demo. Don't: the trade destroys the product in production, where alert fatigue —
    not latency — is what gets it switched off.
    """

    #: Median-of-N detection scores must cross the threshold, not just one lucky frame.
    min_frames: int = 3
    #: Frames an object must be consistently inside a zone before entry counts. Kills the
    #: bounding-box jitter that otherwise makes an object flap across a zone edge.
    zone_inertia_frames: int = 3
    #: A track younger than this is usually a detector artefact rather than a person.
    min_track_age_ms: int = 400
    min_confidence: float = 0.55
    #: Box area as a percentage of frame. Kills birds and insects on the lens at one end, and
    #: whole-frame lighting changes at the other.
    min_box_area_pct: float = 0.4
    max_box_area_pct: float = 40.0
    #: Standing people are taller than wide. Rejects most spurious wide boxes.
    aspect_ratio_min: float = 0.15
    aspect_ratio_max: float = 1.4
    require_motion: bool = True
    #: Regions to ignore entirely: a road behind a fence, a TV screen, the timestamp overlay.
    exclusion_masks: list[str] = field(default_factory=list)
    #: Ignore frames where the whole scene changed brightness at once — IR day/night switching
    #: and PTZ moves are a top false-alarm source on Indian installs.
    lightning_threshold: float = 0.8


@dataclass
class Verification:
    """Lane B: a second opinion on an alert that already fired.

    Never in the detection path. The verifier looks at a handful of crops after the fact, so it
    costs about a second and cannot delay the alert that reaches the console.
    """

    mode: str = "none"                      # none | vlm_second_opinion | human_in_loop
    prompt: str = ""
    frames: int = 3
    on_disagree: str = "downgrade"          # suppress | downgrade | escalate
    max_wait_ms: int = 1500


@dataclass
class Suppression:
    """Governors. Without these a single stuck object generates alerts until someone mutes it."""

    #: No second alert for the SAME tracked object within this window.
    debounce_seconds: int = 30
    #: No second alert for the same rule on the same camera within this window.
    cooldown_seconds: int = 120
    #: Hard ceiling. A rule that wants to fire more often than this is misconfigured, and the
    #: right response is to stop shouting rather than to keep shouting accurately.
    max_per_hour: int = 6
    #: After this many alerts nobody acted on, mute and flag for retuning. The alternative is
    #: the operator muting the entire product, which we never find out about.
    quiet_after_n_unactioned: int = 10


@dataclass
class Schedule:
    tz: str = "Asia/Kolkata"
    #: Windows as (from, to) in HH:MM. A window whose end is before its start wraps midnight,
    #: which is the common case: "after 8pm" means 20:00-06:00.
    windows: list[tuple[str, str]] = field(default_factory=list)
    days: list[str] = field(default_factory=list)          # empty = every day
    holiday_calendar: str | None = None


@dataclass
class Trigger:
    type: str
    object_class: str = "person"
    attributes: list[dict[str, Any]] = field(default_factory=list)
    zone: str | None = None
    direction: str = "any"
    dwell_seconds: int = 0
    count_threshold: int = 1
    count_op: str = ">="


@dataclass
class Rule:
    rule_id: str
    site_id: str
    name: str
    trigger: Trigger
    cameras: list[str] = field(default_factory=list)
    severity: str = "warn"
    schedule: Schedule = field(default_factory=Schedule)
    confirmation: Confirmation = field(default_factory=Confirmation)
    verification: Verification = field(default_factory=Verification)
    suppression: Suppression = field(default_factory=Suppression)
    notify: list[dict[str, Any]] = field(default_factory=list)
    enabled: bool = True
    version: int = 1
    #: Populated by the validator, shown in the UI. Not errors — things the operator should know
    #: before they rely on the rule.
    notes: list[str] = field(default_factory=list)

    @property
    def is_negative_attribute(self) -> bool:
        """True when the rule fires on the ABSENCE of an attribute.

        These are the highest false-alarm class in the product and the first thing a factory
        tests, because a person facing away, a white cap, or a small head crop all produce
        confident-looking violations.
        """
        return any(a.get("op") in ("lt", "lte") for a in self.trigger.attributes)

    @property
    def min_alert_latency_s(self) -> float:
        """The soonest this rule can possibly fire, by its own definition.

        A "blocked for 60 seconds" rule cannot alert in under 60 seconds. Quoting a 2-second SLA
        against it is a claim a customer with a stopwatch will disprove.
        """
        base = 1.2                                    # measured hot-path budget
        if self.trigger.type in TIME_BASED:
            return self.trigger.dwell_seconds + base
        return base


# --- validation ---------------------------------------------------------------------------

def parse_rule(payload: dict[str, Any], *, rule_id: str = "", site_id: str = "") -> Rule:
    """Validate an operator- or model-authored rule document.

    Raises RuleError with a message written for the person who wrote the rule. Several checks
    are policy rather than syntax, and they are enforced here rather than documented because a
    rule that quietly floods an operator is indistinguishable from a broken product.
    """
    if not isinstance(payload, dict):
        raise RuleError("a rule must be a JSON object")

    name = str(payload.get("name") or "").strip()
    if not name:
        raise RuleError("every rule needs a name — operators triage by name, not by id")

    t = payload.get("trigger")
    if not isinstance(t, dict):
        raise RuleError("a rule needs a 'trigger' object")

    ttype = t.get("type")
    if ttype not in TRIGGERS:
        raise RuleError(f"unknown trigger '{ttype}'. Available: {', '.join(sorted(TRIGGERS))}")

    obj = t.get("object_class", "person")
    if obj not in CLASSES:
        raise RuleError(f"unknown object class '{obj}'. Available: {', '.join(sorted(CLASSES))}")

    attrs = t.get("attributes") or []
    if not isinstance(attrs, list):
        raise RuleError("'attributes' must be a list")
    for a in attrs:
        if not isinstance(a, dict) or a.get("key") not in ATTRIBUTES:
            raise RuleError(
                f"unknown attribute {a.get('key') if isinstance(a, dict) else a!r}. "
                f"Available: {', '.join(sorted(ATTRIBUTES))}"
            )
        if a.get("op") not in ("lt", "lte", "gt", "gte"):
            raise RuleError(
                f"attribute '{a['key']}' needs a threshold operator (lt/lte/gt/gte). "
                f"Attributes are confidences from 0 to 1, not true/false — a person facing "
                f"away is not a person without a helmet."
            )
        v = a.get("value")
        if not isinstance(v, int | float) or isinstance(v, bool) or not 0.0 <= v <= 1.0:
            raise RuleError(f"attribute '{a['key']}' needs a value between 0 and 1")

    direction = t.get("direction", "any")
    if direction not in DIRECTIONS:
        raise RuleError(f"direction must be one of {', '.join(DIRECTIONS)}")

    if ttype in ("zone_entry", "zone_exit", "dwell", "absence", "count") and not t.get("zone"):
        raise RuleError(f"a '{ttype}' trigger needs a zone")
    if ttype == "line_cross" and not t.get("zone"):
        raise RuleError("a 'line_cross' trigger needs the name of a line")

    dwell = t.get("dwell_seconds", 0)
    if not isinstance(dwell, int) or isinstance(dwell, bool) or dwell < 0:
        raise RuleError("'dwell_seconds' must be a non-negative integer")
    if ttype in TIME_BASED and dwell <= 0:
        raise RuleError(f"a '{ttype}' trigger needs a positive 'dwell_seconds'")

    trigger = Trigger(
        type=ttype, object_class=obj, attributes=attrs, zone=t.get("zone"),
        direction=direction, dwell_seconds=dwell,
        count_threshold=int(t.get("count_threshold", 1)),
        count_op=str(t.get("count_op", ">=")),
    )

    severity = payload.get("severity", "warn")
    if severity not in SEVERITIES:
        raise RuleError(f"severity must be one of {', '.join(SEVERITIES)}")

    rule = Rule(
        rule_id=rule_id or str(payload.get("rule_id") or ""),
        site_id=site_id or str(payload.get("site_id") or ""),
        name=name,
        trigger=trigger,
        cameras=[str(c) for c in (payload.get("cameras") or [])],
        severity=severity,
        schedule=_schedule(payload.get("schedule") or {}),
        confirmation=_confirmation(payload.get("confirmation") or {}),
        verification=_verification(payload.get("verification") or {}),
        suppression=_suppression(payload.get("suppression") or {}),
        notify=list(payload.get("notify") or []),
        enabled=bool(payload.get("enabled", True)),
    )

    _apply_policy(rule)
    return rule


def _apply_policy(rule: Rule) -> None:
    """Checks that are judgement rather than syntax.

    These exist because the failure they prevent is invisible at authoring time and expensive in
    production: the rule looks right, and then floods an operator for a week.
    """
    c = rule.confirmation

    if rule.is_negative_attribute:
        # Detecting the ABSENCE of something is far noisier than detecting its presence, and
        # this is the first rule a factory writes. Raise the bar rather than let it flood.
        if c.min_frames < 5:
            c.min_frames = 5
            rule.notes.append(
                "This rule fires on the absence of an attribute, which is the noisiest kind. "
                "Raised the confirmation to 5 frames."
            )
        if rule.verification.mode == "none":
            rule.verification.mode = "vlm_second_opinion"
            rule.verification.prompt = rule.verification.prompt or _default_prompt(rule)
            rule.notes.append(
                "Enabled AI second-opinion verification, because absence rules produce "
                "confident-looking false violations (a worker facing away, a white cap, a "
                "small head crop)."
            )

    if c.min_frames < 1:
        raise RuleError("'min_frames' must be at least 1")
    if not 0.0 <= c.min_confidence <= 1.0:
        raise RuleError("'min_confidence' must be between 0 and 1")
    if c.min_box_area_pct >= c.max_box_area_pct:
        raise RuleError("'min_box_area_pct' must be below 'max_box_area_pct'")
    if c.aspect_ratio_min >= c.aspect_ratio_max:
        raise RuleError("'aspect_ratio_min' must be below 'aspect_ratio_max'")

    s = rule.suppression
    if s.max_per_hour < 1:
        raise RuleError("'max_per_hour' must be at least 1 — use enabled:false to switch a "
                        "rule off, rather than throttling it to zero")
    if s.max_per_hour > 60:
        rule.notes.append(
            f"{s.max_per_hour} alerts per hour on one rule is far above the go-live target of "
            f"3 unactioned alerts per camera per day. Expect this rule to be muted."
        )
    if s.debounce_seconds < 1:
        raise RuleError("'debounce_seconds' must be at least 1, or one stuck object will alert "
                        "on every frame")

    if rule.trigger.type in TIME_BASED:
        rule.notes.append(
            f"By its own definition this rule cannot alert sooner than "
            f"{rule.trigger.dwell_seconds}s after the situation begins. The 2-second target "
            f"applies from the moment the condition is met, not from when it started."
        )

    if rule.verification.mode != "none" and not rule.verification.prompt:
        rule.verification.prompt = _default_prompt(rule)


def _default_prompt(rule: Rule) -> str:
    """A yes/no question for the verifier. Deliberately narrow: an open-ended prompt invites a
    narrative, and we only need one bit back."""
    t = rule.trigger
    if t.attributes:
        a = t.attributes[0]
        state = "without" if a["op"] in ("lt", "lte") else "wearing"
        return (f"Is there a {t.object_class} {state} a {a['key']} in this image? "
                f"Answer only yes or no.")
    if t.type == "absence":
        return f"Is the area '{t.zone}' completely clear and unobstructed? Answer only yes or no."
    return (f"Is there a {t.object_class} inside the highlighted area '{t.zone}'? "
            f"Answer only yes or no.")


def _schedule(d: dict[str, Any]) -> Schedule:
    windows: list[tuple[str, str]] = []
    for w in d.get("windows") or []:
        if isinstance(w, dict):
            a, b = w.get("from"), w.get("to")
        elif isinstance(w, list | tuple) and len(w) == 2:
            a, b = w
        else:
            raise RuleError("each schedule window needs 'from' and 'to' as HH:MM")
        for x in (a, b):
            if not isinstance(x, str) or not _is_hhmm(x):
                raise RuleError(f"'{x}' is not a valid HH:MM time")
        windows.append((a, b))
    return Schedule(tz=str(d.get("tz") or "Asia/Kolkata"), windows=windows,
                    days=[str(x).lower()[:3] for x in (d.get("days") or [])],
                    holiday_calendar=d.get("holiday_calendar"))


def _is_hhmm(s: str) -> bool:
    parts = s.split(":")
    if len(parts) != 2 or not all(p.isdigit() for p in parts):
        return False
    h, m = int(parts[0]), int(parts[1])
    return 0 <= h <= 23 and 0 <= m <= 59


def _confirmation(d: dict[str, Any]) -> Confirmation:
    c = Confirmation()
    for k, v in d.items():
        if hasattr(c, k):
            setattr(c, k, v)
    return c


def _verification(d: dict[str, Any]) -> Verification:
    v = Verification()
    for k, val in d.items():
        if hasattr(v, k):
            setattr(v, k, val)
    if v.mode not in ("none", "vlm_second_opinion", "human_in_loop"):
        raise RuleError(f"unknown verification mode '{v.mode}'")
    if v.on_disagree not in ("suppress", "downgrade", "escalate"):
        raise RuleError(f"unknown on_disagree '{v.on_disagree}'")
    return v


def _suppression(d: dict[str, Any]) -> Suppression:
    s = Suppression()
    for k, v in d.items():
        if hasattr(s, k):
            setattr(s, k, v)
    return s


def to_json(rule: Rule) -> dict[str, Any]:
    """Serialise back to the stored form. Round-trips through parse_rule unchanged."""
    return {
        "rule_id": rule.rule_id, "site_id": rule.site_id, "name": rule.name,
        "severity": rule.severity, "enabled": rule.enabled, "version": rule.version,
        "cameras": rule.cameras,
        "trigger": {
            "type": rule.trigger.type, "object_class": rule.trigger.object_class,
            "attributes": rule.trigger.attributes, "zone": rule.trigger.zone,
            "direction": rule.trigger.direction, "dwell_seconds": rule.trigger.dwell_seconds,
            "count_threshold": rule.trigger.count_threshold, "count_op": rule.trigger.count_op,
        },
        "schedule": {"tz": rule.schedule.tz,
                     "windows": [{"from": a, "to": b} for a, b in rule.schedule.windows],
                     "days": rule.schedule.days,
                     "holiday_calendar": rule.schedule.holiday_calendar},
        "confirmation": vars(rule.confirmation),
        "verification": vars(rule.verification),
        "suppression": vars(rule.suppression),
        "notify": rule.notify,
    }

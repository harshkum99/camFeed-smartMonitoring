"""Rule evaluation: turning a stream of detections into as few alerts as possible.

"As few as possible" is the design goal, not a side effect. The industry anchor is that 94-98%
of alarm activations are false, and the operator's response to a noisy product is not to complain
— it is to mute it, silently, and never mention it again. So most of this file is about NOT
firing: median-of-N confirmation, zone inertia, size and shape gates, scene-change rejection,
per-track debounce, per-rule cooldown, an hourly governor, and an auto-mute when nobody acts on
what we send.

Everything here is deterministic and in-process. It runs on the edge box, in the hot path, with
no network call and no model — which is the only way the sub-2-second budget survives.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from statistics import median

from sanjay.rules.schema import Rule

#: How long per-track state is kept after we stop seeing an object. Long enough that a person
#: briefly occluded is not treated as a new arrival, short enough not to leak memory on a busy
#: site.
STATE_TTL = timedelta(minutes=10)


@dataclass
class Detection:
    """One observation of one tracked object in one frame."""

    track_id: str
    camera_id: str
    ts: datetime
    cls: str
    confidence: float
    #: Normalised (x1, y1, x2, y2), 0-1, so gates survive a resolution change.
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.1, 0.2)
    #: Zones the object's ground-contact point is currently inside.
    zones: frozenset[str] = frozenset()
    attrs: dict[str, float] = field(default_factory=dict)
    moving: bool = True
    #: How much the whole frame changed since the last one, 0-1. IR day/night switching and PTZ
    #: moves spike this, and they are a top false-alarm source on Indian installs.
    scene_change: float = 0.0
    track_age_ms: int = 5_000
    masked: bool = False

    @property
    def area_pct(self) -> float:
        x1, y1, x2, y2 = self.bbox
        return max(0.0, (x2 - x1)) * max(0.0, (y2 - y1)) * 100.0

    @property
    def aspect(self) -> float:
        x1, y1, x2, y2 = self.bbox
        h = max(1e-6, y2 - y1)
        return (x2 - x1) / h


@dataclass
class Alert:
    rule_id: str
    rule_name: str
    camera_id: str
    track_id: str | None
    ts: datetime
    severity: str
    zone: str | None
    reason: str
    confidence: float
    attrs: dict[str, float] = field(default_factory=dict)
    #: Set when the rule asked for a second opinion. The alert is delivered either way; the
    #: verdict arrives later and can downgrade or suppress it.
    needs_verification: bool = False
    verification_prompt: str = ""
    #: False when the attribute that triggered this sat inside the detector's uncertainty band —
    #: a helmet score of 0.44 fires the same rule as 0.08, but means something quite different
    #: (a worker facing away, a white cap, a small head crop). A borderline alert still reaches
    #: the live console, because a human can glance at it; what it must NOT do is go straight to
    #: someone's phone as a confident safety violation.
    attr_decisive: bool = True

    @property
    def push_worthy(self) -> bool:
        """Whether this should interrupt someone, as opposed to appearing on the console."""
        return self.attr_decisive and not self.needs_verification

    @property
    def dedupe_key(self) -> str:
        return f"{self.rule_id}|{self.camera_id}|{self.track_id or self.zone}"


@dataclass
class _TrackState:
    scores: deque[float] = field(default_factory=lambda: deque(maxlen=15))
    frames_in_zone: int = 0
    frames_out_of_zone: int = 0
    entered_at: datetime | None = None
    was_inside: bool = False
    fired_at: datetime | None = None
    last_seen: datetime | None = None


@dataclass
class _ZoneState:
    occupied_since: datetime | None = None
    empty_since: datetime | None = None
    present: set[str] = field(default_factory=set)
    fired_at: datetime | None = None
    last_seen: datetime | None = None


class Suppressor:
    """Governors, applied after a rule has decided to fire.

    Kept separate from the rule logic because these are about the operator's attention rather
    than about the scene, and because a rule author should not be able to switch them off.
    """

    def __init__(self) -> None:
        self._last_by_track: dict[str, datetime] = {}
        self._last_by_rule_cam: dict[str, datetime] = {}
        self._hourly: dict[str, deque[datetime]] = {}
        self._unactioned: dict[str, int] = {}
        self._muted: set[str] = set()

    def allow(self, alert: Alert, rule: Rule) -> tuple[bool, str]:
        s = rule.suppression
        now = alert.ts

        if rule.rule_id in self._muted:
            return False, "rule auto-muted after too many unactioned alerts"

        if alert.track_id:
            key = f"{rule.rule_id}|{alert.track_id}"
            last = self._last_by_track.get(key)
            if last and (now - last).total_seconds() < s.debounce_seconds:
                return False, f"same object alerted {int((now - last).total_seconds())}s ago"

        cam_key = f"{rule.rule_id}|{alert.camera_id}"
        last = self._last_by_rule_cam.get(cam_key)
        if last and (now - last).total_seconds() < s.cooldown_seconds:
            return False, f"rule cooling down on this camera ({s.cooldown_seconds}s)"

        window = self._hourly.setdefault(cam_key, deque())
        cutoff = now - timedelta(hours=1)
        while window and window[0] < cutoff:
            window.popleft()
        if len(window) >= s.max_per_hour:
            return False, f"hourly cap of {s.max_per_hour} reached on this camera"

        if alert.track_id:
            self._last_by_track[f"{rule.rule_id}|{alert.track_id}"] = now
        self._last_by_rule_cam[cam_key] = now
        window.append(now)
        return True, ""

    def record_feedback(self, rule: Rule, actioned: bool) -> bool:
        """One-tap feedback from the alert card. Returns True if the rule just got muted.

        Auto-muting a rule is not a failure mode, it is the alternative to the operator muting
        the entire product — which we would never find out about.
        """
        if actioned:
            self._unactioned[rule.rule_id] = 0
            return False
        n = self._unactioned.get(rule.rule_id, 0) + 1
        self._unactioned[rule.rule_id] = n
        if n >= rule.suppression.quiet_after_n_unactioned:
            self._muted.add(rule.rule_id)
            return True
        return False

    def unmute(self, rule_id: str) -> None:
        self._muted.discard(rule_id)
        self._unactioned[rule_id] = 0

    @property
    def muted(self) -> set[str]:
        return set(self._muted)


class RuleEngine:
    """Evaluates detections against a set of rules.

    One instance per edge agent. `observe` is called for every detection on every analytic
    frame and must stay cheap — it is inside the latency budget.
    """

    def __init__(self, rules: list[Rule], suppressor: Suppressor | None = None) -> None:
        self.rules = [r for r in rules if r.enabled]
        self.suppressor = suppressor or Suppressor()
        self._tracks: dict[str, _TrackState] = {}
        self._zones: dict[str, _ZoneState] = {}
        #: Why the last detection did not fire, per rule. Powers the tuning UI: an operator
        #: asking "why didn't it alert?" gets an answer instead of a shrug.
        self.last_reject: dict[str, str] = {}

    def observe(self, d: Detection) -> list[Alert]:
        out: list[Alert] = []
        for rule in self.rules:
            if rule.cameras and d.camera_id not in rule.cameras:
                continue
            if not in_schedule(rule, d.ts):
                self.last_reject[rule.rule_id] = "outside the rule's schedule"
                continue
            alert = self._eval(rule, d)
            if alert is None:
                continue
            ok, why = self.suppressor.allow(alert, rule)
            if not ok:
                self.last_reject[rule.rule_id] = why
                continue
            out.append(alert)
        self._expire(d.ts)
        return out

    # --- per-rule evaluation ------------------------------------------------------------

    def _eval(self, rule: Rule, d: Detection) -> Alert | None:
        t = rule.trigger
        if t.type in ("absence", "count"):
            return self._eval_zone(rule, d)

        if d.cls != t.object_class:
            return None

        key = f"{rule.rule_id}|{d.track_id}"
        st = self._tracks.setdefault(key, _TrackState())
        st.last_seen = d.ts
        st.scores.append(d.confidence)

        gate = self._gates(rule, d, st)
        if gate:
            self.last_reject[rule.rule_id] = gate
            # A rejected frame still updates zone membership, or an object that fails one frame's
            # size gate would appear to leave and re-enter the zone.
            self._track_zone(t, d, st, count_it=False)
            return None

        inside = self._track_zone(t, d, st, count_it=True)

        if t.type == "zone_entry":
            if inside and st.frames_in_zone == rule.confirmation.zone_inertia_frames:
                return self._alert(rule, d, f"entered {t.zone}")
            return None

        if t.type == "zone_exit":
            if st.was_inside and st.frames_out_of_zone == rule.confirmation.zone_inertia_frames:
                st.was_inside = False
                return self._alert(rule, d, f"left {t.zone}")
            return None

        if t.type == "line_cross":
            if inside and st.frames_in_zone == rule.confirmation.zone_inertia_frames:
                return self._alert(rule, d, f"crossed {t.zone}")
            return None

        if t.type == "dwell":
            if inside and st.entered_at:
                held = (d.ts - st.entered_at).total_seconds()
                if held >= t.dwell_seconds and st.fired_at is None:
                    st.fired_at = d.ts
                    return self._alert(rule, d, f"in {t.zone} for {int(held)}s")
            return None

        if t.type == "attribute":
            return self._alert(rule, d, _attr_reason(rule, d)) if inside or not t.zone else None

        return None

    def _eval_zone(self, rule: Rule, d: Detection) -> Alert | None:
        """Zone-level triggers, whose state belongs to the zone rather than to any one object."""
        t = rule.trigger
        key = f"{rule.rule_id}|{d.camera_id}|{t.zone}"
        zs = self._zones.setdefault(key, _ZoneState())
        zs.last_seen = d.ts

        inside = t.zone in d.zones and d.cls == t.object_class
        if inside:
            zs.present.add(d.track_id)
            zs.empty_since = None
            zs.occupied_since = zs.occupied_since or d.ts
        else:
            zs.present.discard(d.track_id)

        if not zs.present:
            zs.occupied_since = None
            zs.empty_since = zs.empty_since or d.ts

        if t.type == "count":
            n = len(zs.present)
            if _cmp(n, t.count_op, t.count_threshold) and zs.fired_at is None:
                zs.fired_at = d.ts
                return self._alert(rule, d, f"{n} {t.object_class}(s) in {t.zone}", track=None)
            if not _cmp(n, t.count_op, t.count_threshold):
                zs.fired_at = None
            return None

        # absence: the zone has been empty for longer than the threshold.
        if zs.empty_since and not zs.present:
            empty = (d.ts - zs.empty_since).total_seconds()
            if empty >= t.dwell_seconds and zs.fired_at is None:
                zs.fired_at = d.ts
                return self._alert(rule, d, f"{t.zone} empty for {int(empty)}s", track=None)
        elif zs.present:
            zs.fired_at = None
        return None

    # --- gates ---------------------------------------------------------------------------

    def _gates(self, rule: Rule, d: Detection, st: _TrackState) -> str | None:
        """The cheap rejections, ordered cheapest first. Returns a reason, or None to proceed."""
        c = rule.confirmation

        if d.masked:
            return "inside an excluded region"
        if d.scene_change >= c.lightning_threshold:
            return "whole-frame lighting change (IR switch or camera movement)"
        if d.track_age_ms < c.min_track_age_ms:
            return f"track too new ({d.track_age_ms}ms)"
        if c.require_motion and not d.moving:
            return "no motion"
        if not c.min_box_area_pct <= d.area_pct <= c.max_box_area_pct:
            return f"box area {d.area_pct:.1f}% outside {c.min_box_area_pct}-{c.max_box_area_pct}%"
        if not c.aspect_ratio_min <= d.aspect <= c.aspect_ratio_max:
            return f"aspect ratio {d.aspect:.2f} outside expected shape"

        # Median rather than latest: one lucky frame is not a detection, and one unlucky frame
        # should not lose a real one.
        if len(st.scores) < c.min_frames:
            return f"only {len(st.scores)} of {c.min_frames} confirmation frames"
        if median(list(st.scores)[-c.min_frames:]) < c.min_confidence:
            return f"median confidence below {c.min_confidence}"

        if not _attrs_match(rule, d):
            return "attribute condition not met"
        return None

    def _track_zone(self, t, d: Detection, st: _TrackState, *, count_it: bool) -> bool:
        inside = (t.zone in d.zones) if t.zone else True
        if not count_it:
            return inside
        if inside:
            st.frames_in_zone += 1
            st.frames_out_of_zone = 0
            st.entered_at = st.entered_at or d.ts
            st.was_inside = True
        else:
            st.frames_out_of_zone += 1
            st.frames_in_zone = 0
            st.entered_at = None
            st.fired_at = None
        return inside

    def _alert(self, rule: Rule, d: Detection, reason: str, track: str | None = ...) -> Alert:
        return Alert(
            rule_id=rule.rule_id, rule_name=rule.name, camera_id=d.camera_id,
            track_id=d.track_id if track is ... else track,
            ts=d.ts, severity=rule.severity, zone=rule.trigger.zone, reason=reason,
            confidence=round(d.confidence, 3),
            attrs=dict(d.attrs),
            needs_verification=rule.verification.mode == "vlm_second_opinion",
            verification_prompt=rule.verification.prompt,
            attr_decisive=_attrs_decisive(rule, d),
        )

    def _expire(self, now: datetime) -> None:
        cutoff = now - STATE_TTL
        for store in (self._tracks, self._zones):
            for k in [k for k, v in store.items() if v.last_seen and v.last_seen < cutoff]:
                del store[k]


# --- helpers ---------------------------------------------------------------------------

def _attrs_match(rule: Rule, d: Detection) -> bool:
    for a in rule.trigger.attributes:
        v = d.attrs.get(a["key"])
        if v is None:
            # Never measured. Treating that as a violation is how a system reports safety
            # breaches for workers a detector never looked at.
            return False
        if not _cmp(v, {"lt": "<", "lte": "<=", "gt": ">", "gte": ">="}[a["op"]], a["value"]):
            return False
    return True


#: Where the attribute detector is genuinely unsure. Matches the band the query layer uses, so a
#: violation counted in a report and a violation that raised an alert mean the same thing — a
#: number in a compliance report that disagrees with the alerts the operator saw is worse than
#: either being slightly wrong.
ATTR_AMBIGUOUS_LOW = 0.35
ATTR_AMBIGUOUS_HIGH = 0.65


def _attrs_decisive(rule: Rule, d: Detection) -> bool:
    """Were the attributes that fired this rule clear-cut, or inside the uncertainty band?"""
    for a in rule.trigger.attributes:
        v = d.attrs.get(a["key"])
        if v is None or ATTR_AMBIGUOUS_LOW <= v <= ATTR_AMBIGUOUS_HIGH:
            return False
    return True


def _attr_reason(rule: Rule, d: Detection) -> str:
    bits = []
    for a in rule.trigger.attributes:
        state = "no" if a["op"] in ("lt", "lte") else "has"
        bits.append(f"{state} {a['key']} ({d.attrs.get(a['key'], 0):.2f})")
    return ", ".join(bits) or "attribute match"


def _cmp(left: float, op: str, right: float) -> bool:
    return {
        ">=": left >= right, ">": left > right, "<=": left <= right,
        "<": left < right, "==": left == right, "!=": left != right,
    }[op]


def in_schedule(rule: Rule, ts: datetime, tz_offset: timedelta | None = None) -> bool:
    """Is this moment inside the rule's schedule?

    Windows are site-local. A window whose end is before its start wraps midnight, which is the
    common case rather than the exception: "alert me after 8pm" is 20:00-06:00, and an
    implementation that compares start <= t <= end silently never fires.
    """
    if not rule.schedule.windows and not rule.schedule.days:
        return True

    off = tz_offset if tz_offset is not None else timedelta(hours=5, minutes=30)
    local = ts + off

    if rule.schedule.days and local.strftime("%a").lower() not in rule.schedule.days:
        return False

    if not rule.schedule.windows:
        return True

    minutes = local.hour * 60 + local.minute
    for a, b in rule.schedule.windows:
        start = _mins(a)
        end = _mins(b)
        if start <= end:
            if start <= minutes < end:
                return True
        elif minutes >= start or minutes < end:      # wraps midnight
            return True
    return False


def _mins(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)

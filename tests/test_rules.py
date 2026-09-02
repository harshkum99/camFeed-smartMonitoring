"""Rules engine tests.

These are mostly about NOT firing. The industry anchor is that 94-98% of alarm activations are
false, and an operator's response to a noisy product is to mute it silently. Every gate here
exists because of a specific way a camera lies.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sanjay.rules.engine import Detection, RuleEngine, Suppressor, in_schedule
from sanjay.rules.schema import RuleError, parse_rule, to_json

T0 = datetime(2026, 9, 3, 6, 0, tzinfo=UTC)      # 11:30 IST
CAM = "cam-1"


def rule(**kw):
    payload = {
        "name": "Restricted zone", "cameras": [CAM],
        "trigger": {"type": "zone_entry", "object_class": "person", "zone": "restricted"},
    }
    payload.update(kw)
    return parse_rule(payload, rule_id="r1", site_id="s1")


def det(i=0, **kw):
    base = dict(
        track_id="t1", camera_id=CAM, ts=T0 + timedelta(seconds=i), cls="person",
        confidence=0.9, bbox=(0.4, 0.4, 0.5, 0.75), zones=frozenset({"restricted"}),
        attrs={}, moving=True, scene_change=0.0, track_age_ms=5000,
    )
    base.update(kw)
    return Detection(**base)


def feed(engine, n, **kw):
    """Push n consecutive frames and return every alert produced."""
    out = []
    for i in range(n):
        out += engine.observe(det(i, **kw))
    return out


# --- schema ------------------------------------------------------------------------------

def test_attributes_must_be_thresholds_not_booleans():
    """A person facing away is not a person without a helmet."""
    with pytest.raises(RuleError, match="confidences from 0 to 1"):
        rule(trigger={"type": "zone_entry", "zone": "z", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "eq", "value": False}]})


def test_unknown_trigger_lists_the_available_ones():
    with pytest.raises(RuleError, match="unknown trigger"):
        rule(trigger={"type": "telepathy", "zone": "z"})


def test_dwell_rule_needs_a_duration():
    with pytest.raises(RuleError, match="positive 'dwell_seconds'"):
        rule(trigger={"type": "dwell", "zone": "z", "object_class": "person"})


def test_rule_needs_a_name_because_operators_triage_by_name():
    with pytest.raises(RuleError, match="needs a name"):
        parse_rule({"trigger": {"type": "zone_entry", "zone": "z"}})


def test_max_per_hour_cannot_be_zero():
    with pytest.raises(RuleError, match="enabled:false"):
        rule(suppression={"max_per_hour": 0})


def test_negative_attribute_rules_are_hardened_automatically():
    """The noisiest class of rule and the first one a factory writes. Left as authored it floods
    the operator, so the policy layer raises the bar and says why."""
    r = rule(trigger={"type": "zone_entry", "zone": "z", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "lt", "value": 0.5}]})
    assert r.is_negative_attribute
    assert r.confirmation.min_frames >= 5
    assert r.verification.mode == "vlm_second_opinion"
    assert r.verification.prompt
    assert any("absence" in n for n in r.notes)


def test_positive_attribute_rules_are_left_alone():
    r = rule(trigger={"type": "zone_entry", "zone": "z", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "gt", "value": 0.8}]})
    assert not r.is_negative_attribute
    assert r.confirmation.min_frames == 3
    assert r.verification.mode == "none"


def test_time_based_rule_states_its_own_latency_floor():
    """A 'blocked for 60 seconds' rule cannot alert in under 60 seconds. Quoting a 2-second SLA
    against it is a claim a customer with a stopwatch disproves."""
    r = rule(trigger={"type": "dwell", "zone": "z", "object_class": "pallet",
                      "dwell_seconds": 60})
    assert r.min_alert_latency_s > 60
    assert any("cannot alert sooner" in n for n in r.notes)


def test_rule_round_trips_through_json():
    r = rule(schedule={"windows": [{"from": "20:00", "to": "06:00"}], "days": ["mon", "tue"]})
    again = parse_rule(to_json(r), rule_id="r1", site_id="s1")
    assert to_json(again) == to_json(r)


# --- schedules ----------------------------------------------------------------------------

def test_after_8pm_wraps_midnight():
    """The canonical rule, and the classic bug: comparing start <= t <= end means a 20:00-06:00
    window never fires at all."""
    r = rule(schedule={"windows": [{"from": "20:00", "to": "06:00"}]})
    ist = timedelta(hours=5, minutes=30)
    assert in_schedule(r, datetime(2026, 9, 3, 16, 0, tzinfo=UTC), ist)      # 21:30 IST
    assert in_schedule(r, datetime(2026, 9, 3, 20, 0, tzinfo=UTC), ist)      # 01:30 IST
    assert not in_schedule(r, datetime(2026, 9, 3, 6, 0, tzinfo=UTC), ist)   # 11:30 IST


def test_daytime_window_does_not_wrap():
    r = rule(schedule={"windows": [{"from": "09:00", "to": "17:00"}]})
    ist = timedelta(hours=5, minutes=30)
    assert in_schedule(r, datetime(2026, 9, 3, 6, 0, tzinfo=UTC), ist)
    assert not in_schedule(r, datetime(2026, 9, 3, 20, 0, tzinfo=UTC), ist)


def test_weekday_restriction():
    r = rule(schedule={"days": ["sat", "sun"]})
    ist = timedelta(hours=5, minutes=30)
    assert not in_schedule(r, datetime(2026, 9, 3, 6, 0, tzinfo=UTC), ist)   # Thursday
    assert in_schedule(r, datetime(2026, 9, 5, 6, 0, tzinfo=UTC), ist)       # Saturday


def test_no_schedule_means_always():
    assert in_schedule(rule(), T0)


def test_engine_respects_the_schedule():
    e = RuleEngine([rule(schedule={"windows": [{"from": "20:00", "to": "06:00"}]})])
    assert feed(e, 6) == []
    assert "schedule" in e.last_reject["r1"]


# --- confirmation gates ---------------------------------------------------------------------

def test_a_single_frame_never_fires():
    """One lucky frame is not a detection."""
    e = RuleEngine([rule()])
    assert e.observe(det(0)) == []


def test_fires_once_confirmation_frames_are_met():
    e = RuleEngine([rule()])
    alerts = feed(e, 6)
    assert len(alerts) == 1
    assert alerts[0].reason == "entered restricted"


def test_median_rejects_a_single_high_frame_among_low_ones():
    """Median rather than max: a detector that flickers to 0.95 once has not seen anything."""
    e = RuleEngine([rule()])
    out = []
    for i, c in enumerate([0.2, 0.2, 0.95, 0.2, 0.2, 0.2]):
        out += e.observe(det(i, confidence=c))
    assert out == []
    assert "median confidence" in e.last_reject["r1"]


def test_tiny_boxes_are_rejected():
    """Birds, and insects on the lens."""
    e = RuleEngine([rule()])
    assert feed(e, 6, bbox=(0.4, 0.4, 0.402, 0.404)) == []
    assert "box area" in e.last_reject["r1"]


def test_full_frame_boxes_are_rejected():
    e = RuleEngine([rule()])
    assert feed(e, 6, bbox=(0.0, 0.0, 1.0, 1.0)) == []
    assert "box area" in e.last_reject["r1"]


def test_wide_flat_boxes_are_rejected_for_people():
    """Standing people are taller than wide."""
    e = RuleEngine([rule()])
    assert feed(e, 6, bbox=(0.1, 0.5, 0.9, 0.56)) == []
    assert "aspect ratio" in e.last_reject["r1"]


def test_scene_wide_lighting_change_is_rejected():
    """IR day/night switching and PTZ moves are a top false-alarm source on Indian installs."""
    e = RuleEngine([rule()])
    assert feed(e, 6, scene_change=0.9) == []
    assert "lighting change" in e.last_reject["r1"]


def test_brand_new_tracks_are_rejected():
    e = RuleEngine([rule()])
    assert feed(e, 6, track_age_ms=100) == []
    assert "too new" in e.last_reject["r1"]


def test_masked_regions_are_rejected():
    e = RuleEngine([rule()])
    assert feed(e, 6, masked=True) == []
    assert "excluded region" in e.last_reject["r1"]


def test_stationary_objects_rejected_when_motion_required():
    e = RuleEngine([rule()])
    assert feed(e, 6, moving=False) == []


def test_wrong_object_class_never_fires():
    e = RuleEngine([rule()])
    assert feed(e, 6, cls="vehicle") == []


def test_object_outside_the_zone_never_fires():
    e = RuleEngine([rule()])
    assert feed(e, 6, zones=frozenset({"somewhere-else"})) == []


def test_zone_inertia_requires_consecutive_frames():
    """Bounding-box jitter makes an object flap across a zone edge. Without inertia that is an
    alert per flap."""
    e = RuleEngine([rule()])
    out = []
    for i in range(8):
        inside = frozenset({"restricted"}) if i % 2 == 0 else frozenset()
        out += e.observe(det(i, zones=inside))
    assert out == []


# --- attributes ------------------------------------------------------------------------------

def test_missing_attribute_never_counts_as_a_violation():
    """Treating 'never measured' as a violation reports safety breaches for workers a detector
    never looked at."""
    r = rule(trigger={"type": "zone_entry", "zone": "restricted", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "lt", "value": 0.5}]})
    e = RuleEngine([r])
    assert feed(e, 8, attrs={}) == []
    assert "attribute" in e.last_reject["r1"]


def test_no_helmet_fires_and_carries_the_score():
    r = rule(trigger={"type": "zone_entry", "zone": "restricted", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "lt", "value": 0.5}]})
    e = RuleEngine([r])
    alerts = feed(e, 8, attrs={"helmet": 0.1})
    assert len(alerts) == 1
    assert alerts[0].attrs["helmet"] == 0.1
    assert alerts[0].needs_verification              # hardened automatically
    assert "yes or no" in alerts[0].verification_prompt


def test_a_clear_violation_is_decisive_and_push_worthy():
    r = rule(trigger={"type": "zone_entry", "zone": "restricted", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "lt", "value": 0.5}]},
             verification={"mode": "none"})
    r.verification.mode = "none"        # opt out of the automatic hardening for this test
    a = feed(RuleEngine([r]), 8, attrs={"helmet": 0.05})[0]
    assert a.attr_decisive and a.push_worthy


def test_a_borderline_violation_reaches_the_console_but_not_someone_s_phone():
    """0.44 fires the same rule as 0.08 but usually means a worker facing away or a white cap.
    It is worth a glance, not an interruption."""
    r = rule(trigger={"type": "zone_entry", "zone": "restricted", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "lt", "value": 0.5}]})
    r.verification.mode = "none"
    a = feed(RuleEngine([r]), 8, attrs={"helmet": 0.44})[0]
    assert not a.attr_decisive
    assert not a.push_worthy


def test_verification_pending_also_holds_back_a_push():
    """An alert awaiting a second opinion has not been confirmed yet."""
    r = rule(trigger={"type": "zone_entry", "zone": "restricted", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "lt", "value": 0.5}]})
    a = feed(RuleEngine([r]), 8, attrs={"helmet": 0.05})[0]
    assert a.needs_verification and not a.push_worthy


def test_a_rule_with_no_attributes_is_always_decisive():
    a = feed(RuleEngine([rule()]), 6)[0]
    assert a.attr_decisive and a.push_worthy


def test_helmet_present_does_not_fire_an_absence_rule():
    r = rule(trigger={"type": "zone_entry", "zone": "restricted", "object_class": "person",
                      "attributes": [{"key": "helmet", "op": "lt", "value": 0.5}]})
    e = RuleEngine([r])
    assert feed(e, 8, attrs={"helmet": 0.95}) == []


# --- dwell, count, absence ---------------------------------------------------------------------

def test_dwell_fires_only_after_the_threshold():
    r = rule(trigger={"type": "dwell", "zone": "restricted", "object_class": "person",
                      "dwell_seconds": 30})
    e = RuleEngine([r])
    assert feed(e, 20) == []                      # 20 seconds
    later = [a for i in range(20, 40) for a in e.observe(det(i))]
    assert len(later) == 1
    assert "30s" in later[0].reason or "31s" in later[0].reason


def test_count_fires_when_the_threshold_is_crossed():
    r = rule(trigger={"type": "count", "zone": "restricted", "object_class": "person",
                      "count_threshold": 3, "count_op": ">="},
             suppression={"debounce_seconds": 1, "cooldown_seconds": 1})
    e = RuleEngine([r])
    out = []
    for i, tid in enumerate(["a", "b", "c"]):
        out += e.observe(det(i, track_id=tid))
    assert len(out) == 1
    assert "3 person(s)" in out[0].reason


def test_absence_fires_when_a_zone_stays_empty():
    r = rule(trigger={"type": "absence", "zone": "restricted", "object_class": "person",
                      "dwell_seconds": 20})
    e = RuleEngine([r])
    out = [a for i in range(40) for a in e.observe(det(i, zones=frozenset()))]
    assert len(out) == 1
    assert "empty" in out[0].reason


# --- suppression --------------------------------------------------------------------------------

def test_debounce_stops_the_same_object_realerting():
    e = RuleEngine([rule()])
    first = feed(e, 6)
    again = [a for i in range(6, 20) for a in e.observe(det(i, zones=frozenset()))]
    again += [a for i in range(20, 30) for a in e.observe(det(i))]
    assert len(first) == 1
    assert again == []
    assert "alerted" in e.last_reject["r1"]


def test_cooldown_stops_a_different_object_on_the_same_camera():
    e = RuleEngine([rule()])
    feed(e, 6)
    other = [a for i in range(10, 20) for a in e.observe(det(i, track_id="t2"))]
    assert other == []
    assert "cooling down" in e.last_reject["r1"]


def test_hourly_cap_is_a_hard_ceiling():
    """A rule that wants to fire more often than this is misconfigured, and the right response
    is to stop shouting rather than to keep shouting accurately."""
    r = rule(suppression={"max_per_hour": 2, "debounce_seconds": 1, "cooldown_seconds": 1})
    e = RuleEngine([r])
    fired = 0
    for n in range(6):
        base = n * 100
        for i in range(base, base + 6):
            fired += len(e.observe(det(i, track_id=f"t{n}")))
    assert fired == 2
    assert "hourly cap" in e.last_reject["r1"]


def test_rule_auto_mutes_after_unactioned_alerts():
    """Muting one rule is the alternative to the operator muting the whole product, which we
    would never find out about."""
    r = rule(suppression={"quiet_after_n_unactioned": 3})
    s = Suppressor()
    for _ in range(2):
        assert s.record_feedback(r, actioned=False) is False
    assert s.record_feedback(r, actioned=False) is True
    assert r.rule_id in s.muted

    e = RuleEngine([r], suppressor=s)
    assert feed(e, 6) == []
    assert "auto-muted" in e.last_reject["r1"]


def test_acting_on_an_alert_resets_the_counter():
    r = rule(suppression={"quiet_after_n_unactioned": 3})
    s = Suppressor()
    s.record_feedback(r, actioned=False)
    s.record_feedback(r, actioned=False)
    s.record_feedback(r, actioned=True)
    assert s.record_feedback(r, actioned=False) is False
    assert not s.muted


def test_unmute_restores_a_rule():
    r = rule(suppression={"quiet_after_n_unactioned": 1})
    s = Suppressor()
    s.record_feedback(r, actioned=False)
    s.unmute(r.rule_id)
    assert not s.muted
    assert len(feed(RuleEngine([r], suppressor=s), 6)) == 1


# --- explainability ------------------------------------------------------------------------------

def test_every_rejection_records_a_readable_reason():
    """An operator asking 'why didn't it alert?' gets an answer instead of a shrug."""
    e = RuleEngine([rule()])
    feed(e, 6, bbox=(0.4, 0.4, 0.402, 0.404))
    reason = e.last_reject["r1"]
    assert reason and not reason.startswith("<")
    assert "%" in reason           # the actual measurement, not just a verdict


def test_state_is_expired_so_memory_does_not_grow():
    e = RuleEngine([rule()])
    feed(e, 6)
    assert e._tracks
    e.observe(det(0, ts=T0 + timedelta(hours=2), track_id="later"))
    assert all("t1" not in k for k in e._tracks)

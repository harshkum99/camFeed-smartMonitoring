"""Rule authoring from plain English, and the read-back that makes it safe.

A wrong rule runs unattended for weeks, so these tests care more about what the operator is
shown before saving than about the compilation itself.
"""

from __future__ import annotations

import pytest

from sanjay.query.nl import Catalog, ModelError
from sanjay.rules.nl import StubRuleProvider, compile_rule, explain

CAT = Catalog(
    cameras={"Gate 3 — Main Entry": "cam-gate", "Zone B — Press Shop": "cam-zoneb",
             "Fire Exit — East": "cam-fire"},
    zones={"Chemical store": "z-chem", "Zone B floor": "z-b", "Fire exit clearance": "z-fire"},
)


class Canned:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, system, user):
        self.prompts.append(user)
        return self.responses.pop(0) if self.responses else {"unsupported": "exhausted"}


def author(text, provider=None):
    return compile_rule(text, CAT, provider or StubRuleProvider(), site_id="s1", rule_id="r1")


# --- compilation ----------------------------------------------------------------------------

def test_the_canonical_rule_compiles():
    c = author("alert me if anyone enters the chemical store after 8pm")
    assert c.rule is not None
    assert c.rule.trigger.type == "zone_entry"
    assert c.rule.trigger.zone == "Chemical store"
    assert c.rule.schedule.windows == [("20:00", "06:00")]


def test_camera_names_resolve_to_ids():
    c = author("alert if anyone enters the chemical store after 8pm")
    assert all(not x.startswith("Gate") for x in c.rule.cameras)


def test_helmet_rule_becomes_a_threshold_not_a_boolean():
    c = author("tell me when someone is in zone b floor without a helmet")
    a = c.rule.trigger.attributes[0]
    assert (a["key"], a["op"]) == ("helmet", "lt")
    assert 0.0 < a["value"] < 1.0


def test_obstruction_rule_carries_its_duration():
    c = author("alert if the fire exit clearance is blocked for more than 60 seconds")
    assert c.rule.trigger.type == "dwell"
    assert c.rule.trigger.dwell_seconds == 60


def test_minutes_are_converted_to_seconds():
    c = author("alert if zone b floor is empty for more than 5 minutes")
    assert c.rule.trigger.dwell_seconds == 300


def test_unsupported_instruction_declines_rather_than_guessing():
    """A rule we cannot compile is one the operator should rewrite. Guessing produces something
    that runs unattended for weeks."""
    c = author("make the factory more profitable")
    assert c.rule is None and c.unsupported


def test_invalid_model_output_gets_one_repair_then_declines():
    bad = {"name": "x", "trigger": {"type": "nonsense", "zone": "z"}}
    p = Canned(bad, bad)
    c = author("something", p)
    assert c.rule is None
    assert len(p.prompts) == 2
    assert "unknown trigger" in p.prompts[1]        # the error is fed back


def test_repair_succeeds_on_the_second_attempt():
    bad = {"name": "x", "trigger": {"type": "dwell", "zone": "Chemical store"}}
    good = {"name": "x", "trigger": {"type": "dwell", "zone": "Chemical store",
                                     "object_class": "person", "dwell_seconds": 30}}
    c = author("something", Canned(bad, good))
    assert c.rule is not None and c.rule.trigger.dwell_seconds == 30


def test_unknown_zone_is_reported_not_silently_accepted():
    c = author("x", Canned({"name": "x", "cameras": [],
                            "trigger": {"type": "zone_entry", "object_class": "person",
                                        "zone": "The Roof"}}))
    assert "zone 'The Roof'" in c.unresolved


def test_unknown_camera_is_reported():
    c = author("x", Canned({"name": "x", "cameras": ["Basement Cam"],
                            "trigger": {"type": "zone_entry", "object_class": "person",
                                        "zone": "Chemical store"}}))
    assert "camera 'Basement Cam'" in c.unresolved


def test_provider_failure_is_raised():
    class Broken:
        def complete(self, system, user):
            raise RuntimeError("timeout")

    with pytest.raises(ModelError, match="timeout"):
        author("anything", Broken())


def test_empty_instruction_does_not_call_the_model():
    p = Canned()
    assert author("").unsupported == "empty instruction"
    assert p.prompts == []


# --- read-back, which is the point ------------------------------------------------------------

def test_explain_states_the_rule_in_plain_english():
    c = author("alert me if anyone enters the chemical store after 8pm")
    text = explain(c.rule, {"cam-gate": "Gate 3 — Main Entry"})
    assert "Alert when a person enters in Chemical store" in text
    assert "between 20:00 and 06:00" in text


def test_explain_reads_back_a_helmet_rule_as_absence():
    c = author("tell me when someone is in zone b floor without a helmet")
    assert "without a helmet" in explain(c.rule)


def test_explain_warns_that_a_dwell_rule_cannot_be_fast():
    """Quoting a 2-second SLA against a 60-second rule is a claim a customer with a stopwatch
    disproves in the first demo."""
    c = author("alert if the fire exit clearance is blocked for more than 60 seconds")
    text = explain(c.rule)
    assert "Soonest this can alert: 61s" in text
    assert "part of the rule" in text


def test_explain_states_the_fast_path_for_instant_rules():
    c = author("alert me if anyone enters the chemical store after 8pm")
    assert "about 1 second" in explain(c.rule)


def test_explain_shows_the_limits_the_operator_will_live_with():
    c = author("alert me if anyone enters the chemical store")
    text = explain(c.rule)
    assert "per hour per camera" in text
    assert "mutes itself" in text


def test_explain_surfaces_the_automatic_hardening_of_absence_rules():
    """The operator must be told we changed their rule, and why."""
    c = author("tell me when someone is in zone b floor without a helmet")
    text = explain(c.rule)
    assert "AI second opinion" in text
    assert any("absence" in line for line in text.splitlines())


def test_explain_names_the_cameras_being_watched():
    c = author("alert me if anyone enters the chemical store after 8pm")
    c.rule.cameras = ["cam-fire"]
    assert "Fire Exit — East" in explain(c.rule, {"cam-fire": "Fire Exit — East"})


def test_explain_says_so_when_no_camera_is_named():
    c = author("alert me if anyone enters the chemical store")
    c.rule.cameras = []
    assert "every camera at this site" in explain(c.rule)

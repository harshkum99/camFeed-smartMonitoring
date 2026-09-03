"""Natural-language compilation tests.

The model's output is untrusted input. These tests are mostly about what happens when it is
wrong, malicious, or confidently mistaken — because that is the normal case, not the exception.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from smartcam.query.filters import Entity, Select
from smartcam.query.nl import (
    Catalog,
    ModelError,
    StubProvider,
    build_user_prompt,
    compile_question,
    named_windows,
)

NOW = datetime(2026, 9, 3, 6, 30, tzinfo=UTC)      # 12:00 IST on Thursday 3 Sept
CAT = Catalog(
    cameras={
        "Gate 3 — Main Entry": "cccccccc-0000-0000-0000-000000000001",
        "Zone B — Press Shop": "cccccccc-0000-0000-0000-000000000002",
        "Fire Exit — East": "cccccccc-0000-0000-0000-000000000004",
    },
    zones={"Zone B floor": "22220000-0000-0000-0002-000000000001"},
)


class Canned:
    """Returns whatever it was given, so a specific model answer can be tested."""

    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts: list[str] = []

    def complete(self, system, user):
        self.prompts.append(user)
        return self.responses.pop(0) if self.responses else {"unsupported": "exhausted"}


class Broken:
    def complete(self, system, user):
        raise RuntimeError("connection reset")


def compile_(q, provider=None, now=NOW):
    return compile_question(q, CAT, provider or StubProvider(), now=now)


# --- time windows ------------------------------------------------------------------------

def test_windows_are_computed_not_left_to_the_model():
    """Date arithmetic is the single most common source of a confidently wrong answer — a real
    observation attached to the wrong day."""
    w = named_windows(NOW, timedelta(hours=5, minutes=30))
    assert w["today"] == ("2026-09-03T00:00:00+05:30", "2026-09-04T00:00:00+05:30")
    assert w["yesterday"] == ("2026-09-02T00:00:00+05:30", "2026-09-03T00:00:00+05:30")


def test_weekday_names_resolve_to_the_most_recent_one():
    w = named_windows(NOW, timedelta(hours=5, minutes=30))
    assert w["wednesday"][0].startswith("2026-09-02")
    assert w["monday"][0].startswith("2026-08-31")


def test_windows_carry_the_site_offset_not_utc():
    """A naive or UTC timestamp silently shifts a shift boundary by five and a half hours."""
    for start, end in named_windows(NOW, timedelta(hours=5, minutes=30)).values():
        assert start.endswith("+05:30") and end.endswith("+05:30")


def test_window_selection_from_the_question():
    c = compile_("how many people came through yesterday")
    assert c.filter is not None
    assert c.filter.start.isoformat().startswith("2026-09-02")


# --- name resolution ---------------------------------------------------------------------

def test_camera_names_resolve_to_ids_so_the_model_never_writes_a_uuid():
    """Asking a model to copy a 36-character identifier invites a transposed digit that silently
    queries the wrong camera."""
    c = compile_("was anything blocking the fire exit today",
                 Canned({"entity": "zone_events", "select": "rows", "window": "today",
                         "cameras": ["Fire Exit — East"],
                         "filters": [{"field": "type", "op": "eq", "value": "stationary"}]}))
    ids = [p.value for p in c.filter.predicates if p.field == "camera_id"]
    assert ids == ["cccccccc-0000-0000-0000-000000000004"]


def test_partial_camera_names_resolve():
    c = compile_("check zone b today",
                 Canned({"entity": "zone_events", "select": "count", "window": "today",
                         "cameras": ["Zone B"],
                         "filters": [{"field": "type", "op": "eq", "value": "enter"}]}))
    ids = [p.value for p in c.filter.predicates if p.field == "camera_id"]
    assert ids == ["cccccccc-0000-0000-0000-000000000002"]


def test_ambiguous_camera_name_is_reported_not_guessed():
    """Two plausible cameras means we would be guessing, and a query silently pointed at the
    wrong camera is worse than one that fails."""
    cat = Catalog(cameras={"Gate 1": "id-1", "Gate 2": "id-2"})
    c = compile_question("check the gate today", cat,
                         Canned({"entity": "zone_events", "select": "count", "window": "today",
                                 "cameras": ["Gate"],
                                 "filters": [{"field": "type", "op": "eq", "value": "enter"}]}),
                         now=NOW)
    assert "camera 'Gate'" in c.unresolved
    assert not any(p.field == "camera_id" for p in c.filter.predicates)


def test_invented_camera_name_does_not_silently_vanish():
    """Dropping a camera the operator named answers a different question from the one asked."""
    c = compile_("anything at the roof camera today",
                 Canned({"entity": "zone_events", "select": "count", "window": "today",
                         "cameras": ["Roof Camera"],
                         "filters": [{"field": "type", "op": "eq", "value": "enter"}]}))
    assert c.unresolved == ["camera 'Roof Camera'"]


def test_a_name_from_another_site_cannot_be_reached():
    c = compile_("check Warehouse 9 today",
                 Canned({"entity": "zone_events", "select": "count", "window": "today",
                         "cameras": ["Warehouse 9 — Bay 2"],
                         "filters": [{"field": "type", "op": "eq", "value": "enter"}]}))
    assert not any(p.field == "camera_id" for p in c.filter.predicates)


# --- untrusted model output ----------------------------------------------------------------

def test_unknown_field_from_the_model_triggers_one_repair():
    bad = {"entity": "zone_events", "select": "count", "window": "today",
           "filters": [{"field": "salary", "op": "eq", "value": "x"}]}
    good = {"entity": "zone_events", "select": "count", "window": "today", "filters": []}
    p = Canned(bad, good)
    c = compile_("something", p)
    assert c.filter is not None and c.repaired
    assert "unknown field" in p.prompts[1]        # the error is fed back, not just retried


def test_two_failures_decline_rather_than_loop():
    """Retrying a third time spends money on the same answer. A question we cannot compile is a
    question we should decline."""
    bad = {"entity": "zone_events", "select": "count", "window": "today",
           "filters": [{"field": "salary", "op": "eq", "value": "x"}]}
    p = Canned(bad, bad)
    c = compile_("something", p)
    assert c.filter is None
    assert c.unsupported and "could not interpret" in c.unsupported
    assert len(p.prompts) == 2


def test_model_declining_is_a_valid_answer_not_an_error():
    c = compile_("what is the meaning of life", Canned({"unsupported": "not a CCTV question"}))
    assert c.filter is None and c.unsupported == "not a CCTV question"


def test_sql_injection_in_a_model_value_is_bound_not_executed():
    evil = "'; DROP TABLE tracks; --"
    c = compile_("x", Canned({"entity": "tracks", "select": "rows", "window": "today",
                              "filters": [{"field": "upper_colour", "op": "eq",
                                           "value": evil}]}))
    from smartcam.query.filters import compile_sql
    q = compile_sql(c.filter, tenant_id="t", site_id="s")
    assert evil not in q.sql and evil in q.params


def test_model_cannot_set_the_tenant():
    c = compile_("x", Canned({"entity": "zone_events", "select": "count", "window": "today",
                              "tenant_id": "someone-else", "site_id": "elsewhere",
                              "filters": []}))
    from smartcam.query.filters import compile_sql
    q = compile_sql(c.filter, tenant_id="mine", site_id="my-site")
    assert "someone-else" not in q.params and "elsewhere" not in q.params


def test_provider_failure_is_raised_not_swallowed():
    with pytest.raises(ModelError, match="connection reset"):
        compile_("anything", Broken())


def test_non_dict_response_is_rejected():
    class Weird:
        def complete(self, system, user):
            return ["not", "an", "object"]

    with pytest.raises(ModelError, match="expected a JSON object"):
        compile_("anything", Weird())


def test_empty_and_overlong_questions_are_declined_without_calling_the_model():
    p = Canned()
    assert compile_("", p).unsupported == "empty question"
    assert "too long" in compile_("x" * 600, p).unsupported
    assert p.prompts == []


# --- prompt construction --------------------------------------------------------------------

def test_prompt_lists_the_site_cameras_and_windows():
    u = build_user_prompt("test", CAT, NOW)
    assert "Gate 3 — Main Entry" in u
    assert "yesterday" in u and "today" in u
    assert "helmet" in u                              # the field schema is included
    assert "2026-09-03 12:00 (Thursday)" in u         # site-local now, so relative words anchor


def test_prompt_does_not_leak_ids_to_the_model():
    u = build_user_prompt("test", CAT, NOW)
    assert "cccccccc-0000-0000-0000-000000000001" not in u


# --- the stub, which the offline demo depends on ---------------------------------------------

def test_stub_handles_the_demo_questions():
    helmet = compile_("how many workers entered without a helmet today")
    assert helmet.filter.entity is Entity.ZONE_EVENTS
    assert helmet.filter.select is Select.DISTINCT_COUNT
    assert any(p.field == "helmet" for p in helmet.filter.predicates)

    fire = compile_("was the fire exit blocked at any point today")
    assert fire.filter.select is Select.ROWS


def test_stub_declines_what_it_cannot_handle():
    """A stub that always succeeds would hide the refusal path it exists to exercise."""
    assert compile_("what is our quarterly revenue").filter is None


def test_counting_people_uses_distinct_count():
    """One person crossing a line five times is five events but one person."""
    c = compile_("how many people came through the gate today")
    assert c.filter.select is Select.DISTINCT_COUNT


def test_stub_resolves_weekday_names():
    """'On Tuesday' silently became 'today' in an earlier version — a real observation reported
    against the wrong day, which is the worst failure this system has."""
    c = compile_("was the fire exit blocked on Tuesday")
    assert c.filter.start.isoformat().startswith("2026-09-01")     # Tuesday, not Thursday


def test_stub_matches_camera_names_on_whole_words_only():
    """'Was the fire exit BLOCKED' must not select a camera named '... Admin BLOCK'. Substring
    matching produced a plausible-looking, completely wrong answer."""
    cat = Catalog(cameras={"Fire Exit — East": "fire-id",
                           "Corridor — Admin Block": "corridor-id"})
    c = compile_question("was the fire exit blocked yesterday", cat, StubProvider(), now=NOW)
    ids = [p.value for p in c.filter.predicates if p.field == "camera_id"]
    assert ids == ["fire-id"]


def test_generic_words_in_camera_names_do_not_select_cameras():
    """A question that names no place must leave the camera set empty rather than guessing."""
    cat = Catalog(cameras={"Gate 3 — Main Entry": "gate", "Zone B — Press Shop": "zoneb"})
    c = compile_question("how many people entered today", cat, StubProvider(), now=NOW)
    assert not any(p.field == "camera_id" for p in c.filter.predicates)


def test_named_place_still_matches_after_the_stopword_trim():
    """The mirror of the test above. An over-long stopword list is its own failure: an earlier
    version listed 'zone' and 'shop', which stopped 'Zone B' matching anything at all."""
    c = compile_("how many workers entered Zone B without a helmet yesterday")
    ids = [p.value for p in c.filter.predicates if p.field == "camera_id"]
    assert ids == ["cccccccc-0000-0000-0000-000000000002"]


def test_restatement_shows_camera_names_not_identifiers():
    """The restatement only works as a correctness control if an operator can read it."""
    from smartcam.query.ask import _humanise
    text = "where camera is cccccccc-0000-0000-0000-000000000001, between ..."
    assert _humanise(text, CAT) == "where camera is Gate 3 — Main Entry, between ..."

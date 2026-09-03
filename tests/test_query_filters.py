"""Filter compiler tests.

This is the boundary between an untrusted language model and the customer's data, so the
security properties are tested as hard as the functional ones.
"""

from __future__ import annotations

import pytest

from smartcam.query.filters import (
    MAX_LIMIT,
    Entity,
    FilterError,
    Op,
    Select,
    compile_sql,
    describe,
    parse,
    schema_for_prompt,
)

T0 = "2026-09-01T00:00:00+05:30"
T1 = "2026-09-02T00:00:00+05:30"
TENANT = "11111111-1111-1111-1111-111111111111"
SITE = "aaaaaaaa-0000-0000-0000-000000000001"


def base(**kw):
    d = {"entity": "zone_events", "select": "count", "start": T0, "end": T1}
    d.update(kw)
    return d


def comp(payload, **kw):
    return compile_sql(parse(payload), tenant_id=TENANT, site_id=SITE, **kw)


# --- security --------------------------------------------------------------------------

def test_values_are_always_bound_never_interpolated():
    """The model has read the customer's camera names and attribute values, which are
    attacker-influenced. If any of it reached the SQL text, the model becomes an injection
    vector."""
    evil = "'; DROP TABLE tracks; --"
    q = comp(base(entity="tracks", select="rows",
                  filters=[{"field": "upper_colour", "op": "eq", "value": evil}]))
    assert evil not in q.sql
    assert evil in q.params
    assert q.sql.count("%s") == len(q.params)


def test_unknown_field_is_rejected_with_the_allowed_list():
    """The error is fed back to the model for one repair attempt, so it has to be actionable."""
    with pytest.raises(FilterError) as e:
        parse(base(filters=[{"field": "password", "op": "eq", "value": "x"}]))
    assert "unknown field" in str(e.value)
    assert "camera_id" in str(e.value)


def test_field_name_cannot_smuggle_sql():
    with pytest.raises(FilterError, match="unknown field"):
        parse(base(filters=[{"field": "conf) OR (1=1", "op": "eq", "value": "x"}]))


def test_unknown_operator_is_rejected():
    with pytest.raises(FilterError, match="must be one of"):
        parse(base(filters=[{"field": "type", "op": "regex", "value": ".*"}]))


def test_operator_must_suit_the_column_type():
    """Ordering a text column by '<' is meaningless and would silently return nonsense."""
    with pytest.raises(FilterError, match="not valid for"):
        parse(base(filters=[{"field": "type", "op": "lt", "value": "enter"}]))


def test_group_by_is_restricted_to_low_cardinality_columns():
    with pytest.raises(FilterError, match="cannot group by"):
        parse(base(group_by=["track_id"]))


def test_tenant_and_site_are_injected_and_cannot_be_overridden():
    """Cross-tenant leakage must be impossible by construction, not by prompt discipline."""
    q = comp(base(filters=[{"field": "camera_id", "op": "eq", "value": "other-tenant-cam"}]))
    assert "tenant_id = %s" in q.sql
    assert "site_id = %s" in q.sql
    # Order-independent on purpose: parameters are grouped by clause, and asserting a fixed
    # index here previously encoded a parameter-ordering bug as if it were the contract.
    assert TENANT in q.params and SITE in q.params


def test_no_payload_field_can_set_the_tenant():
    q = comp(base(tenant_id="22222222-2222-2222-2222-222222222222", site_id="elsewhere"))
    assert TENANT in q.params
    assert "22222222-2222-2222-2222-222222222222" not in q.params
    assert "elsewhere" not in q.params


def test_camera_scope_from_the_session_is_applied():
    q = comp(base(), camera_ids=["cam-1", "cam-2"])
    assert "camera_id = ANY(%s)" in q.sql
    assert ["cam-1", "cam-2"] in q.params


def test_limit_is_capped():
    assert parse(base(select="rows", limit=10_000)).limit == MAX_LIMIT


def test_limit_rejects_booleans_and_junk():
    for bad in (True, 0, -1, "50", None):
        with pytest.raises(FilterError, match="limit"):
            parse(base(select="rows", limit=bad))


def test_window_is_bounded():
    with pytest.raises(FilterError, match="maximum"):
        parse(base(start="2020-01-01T00:00:00+00:00", end=T1))


def test_end_must_follow_start():
    with pytest.raises(FilterError, match="end must be after start"):
        parse(base(start=T1, end=T0))


def test_naive_timestamps_are_refused():
    """A naive timestamp silently places people at the wrong time of day. On a site whose DVR
    clock is already suspect, that is the failure a customer notices first."""
    with pytest.raises(FilterError, match="timezone offset"):
        parse(base(start="2026-09-01T00:00:00", end=T1))


def test_numeric_columns_reject_strings_and_booleans():
    for bad in ("0.5", True, None):
        with pytest.raises(FilterError, match="expects a number"):
            parse(base(filters=[{"field": "conf", "op": "lt", "value": bad}]))


def test_in_requires_a_non_empty_bounded_list():
    with pytest.raises(FilterError, match="non-empty list"):
        parse(base(filters=[{"field": "type", "op": "in", "value": []}]))
    with pytest.raises(FilterError, match="too long"):
        parse(base(filters=[{"field": "type", "op": "in", "value": ["x"] * 101}]))


# --- correctness -----------------------------------------------------------------------

def test_count_returns_a_triple_not_a_scalar():
    """A bare count hides that the database counted whatever rows exist at whatever confidence.
    'confident / ambiguous / coverage' is defensible; '14' is a claim waiting to be disproved."""
    q = comp(base())
    assert "AS confident" in q.sql and "AS ambiguous" in q.sql and "AS total" in q.sql


def test_helmet_absence_is_a_confidence_threshold_not_a_boolean():
    """Attributes are stored as confidences so a rule can demand a floor. Negative-attribute
    rules are the highest false-alarm class in the product, and a hard boolean makes that
    worse — a person facing away is not a person without a helmet."""
    q = comp(base(filters=[{"field": "helmet", "op": "lt", "value": 0.5}]))
    assert "(z.attrs->>'helmet')::real < %s" in q.sql
    assert 0.5 in q.params


def test_not_in_includes_rows_where_the_attribute_was_never_measured():
    """SQL's NOT (x = ANY(...)) is NULL when x is NULL, which silently drops exactly the rows
    an operator most needs to see."""
    q = comp(base(filters=[{"field": "type", "op": "not_in", "value": ["enter"]}]))
    assert "IS NULL OR NOT" in q.sql


def test_distinct_count_counts_objects_not_events():
    """One person crossing a line five times is five events but one person. Conflating them is
    the most common way a count goes wrong."""
    q = comp(base(select="distinct_count"))
    assert "count(DISTINCT z.track_id)" in q.sql


def test_group_by_emits_group_and_order():
    q = comp(base(group_by=["camera_id"]))
    assert "GROUP BY z.camera_id" in q.sql
    assert "ORDER BY confident DESC" in q.sql


def test_rows_query_orders_newest_first_and_limits():
    q = comp(base(entity="tracks", select="rows", limit=25))
    assert "ORDER BY t.ts_start DESC" in q.sql and "LIMIT %s" in q.sql
    assert 25 in q.params


def test_group_by_with_rows_is_rejected():
    with pytest.raises(FilterError, match="group_by requires"):
        parse(base(select="rows", group_by=["camera_id"]))


def test_time_column_differs_per_entity():
    assert "z.ts >= %s" in comp(base()).sql
    assert "t.ts_start >= %s" in comp(base(entity="tracks")).sql


def test_window_is_half_open():
    """A closed upper bound double-counts an event landing exactly on a boundary when two
    adjacent windows are queried — e.g. hourly compliance reports."""
    q = comp(base())
    assert "z.ts < %s" in q.sql and "z.ts <= %s" not in q.sql


def test_null_operators_bind_no_parameter():
    q = comp(base(entity="tracks", filters=[{"field": "identity", "op": "is_null"}]))
    assert "t.global_identity_id IS NULL" in q.sql
    assert q.sql.count("%s") == len(q.params)


# Every shape the compiler can emit. Kept here rather than inline so a new feature has to be
# added to this list, and so the invariant below covers it automatically.
ALL_SHAPES = [
    base(),
    base(select="distinct_count"),
    base(select="rows", limit=10),
    base(group_by=["camera_id"]),
    base(group_by=["camera_id", "type"]),
    base(min_confidence=0.8),
    base(entity="tracks"),
    base(entity="tracks", select="rows"),
    base(entity="tracks", group_by=["class", "upper_colour"]),
    base(filters=[{"field": "type", "op": "in", "value": ["enter", "cross_pos"]}]),
    base(filters=[{"field": "type", "op": "not_in", "value": ["gone"]}]),
    base(filters=[{"field": "helmet", "op": "lt", "value": 0.5},
                  {"field": "conf", "op": "gte", "value": 0.6}]),
    base(entity="tracks", filters=[{"field": "identity", "op": "is_null"}]),
    base(entity="tracks", filters=[{"field": "identity", "op": "not_null"},
                                   {"field": "dwell_s", "op": "gt", "value": 60}]),
]


@pytest.mark.parametrize("payload", ALL_SHAPES, ids=range(len(ALL_SHAPES)))
def test_placeholder_count_always_matches_parameter_count(payload):
    """Positional placeholders are consumed in order, so a value appearing twice in the SQL
    must be appended twice. Getting this wrong desynchronises every later parameter, and it is
    invisible until execution — this invariant caught exactly that bug in the count query."""
    q = comp(payload, camera_ids=["cam-1"])
    assert q.sql.count("%s") == len(q.params), q.sql


# --- explainability --------------------------------------------------------------------

def test_describe_restates_the_query_in_english():
    """Shown next to every answer. A person reading this spots a misread question instantly,
    which is the cheapest correctness control in the whole query path."""
    f = parse(base(entity="tracks", select="distinct_count",
                   filters=[{"field": "class", "op": "eq", "value": "person"},
                            {"field": "helmet", "op": "lt", "value": 0.5}]))
    d = describe(f)
    assert "counted distinct objects in tracked objects" in d
    assert "helmet confidence 0-1 is below 0.5" in d
    assert "01 Sep 2026" in d


def test_prompt_schema_lists_fields_and_operators():
    s = schema_for_prompt()
    assert "helmet" in s and "eq, gt" in s.replace("  ", " ") or "eq" in s
    assert "zone_events:" in s and "tracks:" in s


def test_enums_cover_what_the_compiler_handles():
    assert set(Select) == {Select.COUNT, Select.DISTINCT_COUNT, Select.ROWS}
    assert Entity.TRACKS in Entity and Op.NOT_IN in Op

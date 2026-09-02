"""Turning a compiled filter into an answer a customer can act on — or an honest refusal.

The hard part of this layer is not retrieval, it is knowing when to say no. A surveillance system
that answers "no events found" when a camera was simply offline has delivered a false negative as
a fact, and the customer has no way to tell the difference. So every answer carries a coverage
percentage, every count is split into confident and ambiguous, and there are four distinct
refusals that must be distinguishable to the person reading them:

    no coverage   — the cameras were down, or none watches that place. Name which, and when.
    no evidence   — we looked, and there was nothing. Offer to widen the window.
    not measured  — nobody ever ran a detector for that. Offer to enable it going forward.
    low confidence — there is something, but it is weak. Show it and let a human adjudicate.

Buyers in regulated industries trust a system that refuses more than one that always answers, and
a demo should deliberately include a question the system correctly declines.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from sanjay.query.filters import (
    SCHEMA,
    TABLE_ALIAS,
    TIME_COLUMN,
    CompiledQuery,
    Entity,
    Filter,
    Op,
    Select,
    compile_sql,
    describe,
)

#: Below this, we lead with the coverage problem rather than the result. A number computed over
#: two-thirds of a shift is not wrong, but presenting it without saying so is.
COVERAGE_FLOOR = 0.85

#: Attributes whose absence means "we never ran that detector here" rather than "it wasn't true".
#: Distinguishing those two is the whole point of the `not_measured` refusal.
MEASURED_ATTRS = {"helmet", "vest", "upper_colour"}


@dataclass
class Evidence:
    """One frame backing one claim. Every assertion in an answer must carry at least one."""

    track_id: str
    camera_id: str
    camera_name: str
    ts: datetime
    confidence: float
    keyframe_uri: str | None
    frame_sha256: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)


@dataclass
class Gap:
    camera_id: str
    camera_name: str
    start: datetime
    end: datetime

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)


@dataclass
class Answer:
    question: str
    describes: str
    #: The count triple. `confident` is what a customer should act on; `ambiguous` is what a human
    #: should look at. Never collapse these into one number.
    confident: int = 0
    ambiguous: int = 0
    total: int = 0
    rows: list[dict[str, Any]] = field(default_factory=list)
    groups: list[dict[str, Any]] = field(default_factory=list)
    evidence: list[Evidence] = field(default_factory=list)
    coverage_pct: float = 0.0
    gaps: list[Gap] = field(default_factory=list)
    abstained: bool = False
    abstain_reason: str | None = None
    message: str = ""
    latency_ms: int = 0
    sql: str = ""

    @property
    def is_count(self) -> bool:
        return not self.rows and not self.groups


def answer(
    conn,
    f: Filter,
    *,
    question: str,
    tenant_id: str,
    site_id: str,
    actor: str,
    camera_ids: list[str] | None = None,
    coverage_floor: float = COVERAGE_FLOOR,
    evidence_limit: int = 12,
) -> Answer:
    """Execute a filter and build an answer, refusing honestly where we should.

    `conn` is a live psycopg connection. Every call writes a row to `answers`, including the
    refusals — that table is simultaneously the audit trail, the eval harness, and the defence
    the first time somebody uses the query box on a colleague.
    """
    t0 = time.monotonic()
    q = compile_sql(f, tenant_id=tenant_id, site_id=site_id, camera_ids=camera_ids)
    a = Answer(question=question, describes=q.describes, sql=q.sql)

    scope = _coverage_scope(conn, f, site_id, camera_ids)
    a.coverage_pct = _coverage(conn, scope, f.start, f.end)
    a.gaps = _gaps(conn, scope, f.start, f.end)

    _execute(conn, q, f, a)

    if not a.rows and not a.groups and a.total == 0:
        _explain_emptiness(conn, f, a, tenant_id, site_id, scope, coverage_floor)
    elif a.is_count and a.confident == 0 and a.ambiguous > 0:
        a.abstained = True
        a.abstain_reason = "low_confidence"
        a.message = (
            f"Found {a.ambiguous} possible match(es), but none above the "
            f"{f.min_confidence:.0%} confidence threshold. Showing them for you to confirm."
        )
    elif a.coverage_pct < coverage_floor:
        # Not a refusal: we did find something. But the number is incomplete and saying so is
        # the difference between a defensible answer and a mis-sell.
        a.message = (
            f"{_fmt_count(a)} — but only {a.coverage_pct:.0%} of the requested period was "
            f"covered by these cameras. {_gap_phrase(a.gaps)}"
        )
    else:
        a.message = _fmt_count(a)

    if not a.evidence and (a.rows or a.total):
        a.evidence = _fetch_evidence(conn, f, tenant_id, site_id, camera_ids, evidence_limit)

    a.latency_ms = int((time.monotonic() - t0) * 1000)
    _log(conn, a, f, q, tenant_id, site_id, actor, scope)
    return a


# --- execution -------------------------------------------------------------------------

def _execute(conn, q: CompiledQuery, f: Filter, a: Answer) -> None:
    with conn.cursor() as cur:
        cur.execute(q.sql, q.params)
        names = [d.name for d in cur.description]
        fetched = cur.fetchall()

    if f.select is Select.ROWS:
        a.rows = [dict(zip(names, r, strict=True)) for r in fetched]
        a.total = len(a.rows)
        a.confident = sum(1 for r in a.rows if (r.get("conf_max") or r.get("conf") or 0)
                          >= f.min_confidence)
        a.ambiguous = a.total - a.confident
        return

    if f.group_by:
        for r in fetched:
            row = dict(zip(names, r, strict=True))
            group = {g: row[f"group_{i}"] for i, g in enumerate(f.group_by)}
            group.update(confident=row["confident"], ambiguous=row["ambiguous"],
                         total=row["total"])
            a.groups.append(group)
        a.confident = sum(g["confident"] for g in a.groups)
        a.ambiguous = sum(g["ambiguous"] for g in a.groups)
        a.total = sum(g["total"] for g in a.groups)
        return

    row = dict(zip(names, fetched[0], strict=True)) if fetched else {}
    a.confident = row.get("confident") or 0
    a.ambiguous = row.get("ambiguous") or 0
    a.total = row.get("total") or 0


def _explain_emptiness(conn, f: Filter, a: Answer, tenant_id, site_id, scope, floor) -> None:
    """Nothing came back. Work out *why*, because the three reasons need different actions from
    the customer and lumping them together is how a false negative gets delivered as a fact."""
    a.abstained = True

    unmeasured = _unmeasured_attrs(conn, f, tenant_id, site_id)
    if unmeasured:
        a.abstain_reason = "not_measured"
        names = ", ".join(sorted(unmeasured))
        a.message = (
            f"No detector for '{names}' was running on these cameras during that period, so "
            f"this question cannot be answered from the record — the answer is not zero, it is "
            f"unknown. I can enable it going forward."
        )
        return

    if a.coverage_pct < floor:
        a.abstain_reason = "no_coverage"
        a.message = (
            f"Only {a.coverage_pct:.0%} of that period was covered. {_gap_phrase(a.gaps)} "
            f"I can't tell you nothing happened — I can only tell you nothing was recorded."
        )
        return

    a.abstain_reason = "no_evidence"
    a.message = (
        f"No matching events found, and camera coverage was {a.coverage_pct:.0%} for that "
        f"period, so this is a real negative rather than a gap. Try widening the time window."
    )


def _unmeasured_attrs(conn, f: Filter, tenant_id: str, site_id: str) -> set[str]:
    """Which attributes the filter relies on were never populated *for the things it asked about*.

    A helmet predicate returning nothing means one of two very different things: nobody was
    without a helmet, or nobody ever ran a helmet detector there. Only the database knows which.

    The scoping matters more than it looks. Asking "is `vest` measured anywhere on this site?"
    answers yes as soon as one camera runs a vest detector — so a question about hi-vis vests on
    *vehicles* comes back as a confident zero instead of "we never measured that". So the check
    keeps the filter's own non-attribute predicates (camera, class, event type) and drops only
    the attribute tests: the question becomes "was this attribute ever recorded on the subset you
    asked about", which is the one a customer actually means.
    """
    wanted = {p.field for p in f.predicates} & MEASURED_ATTRS
    if not wanted:
        return set()

    cols = SCHEMA[f.entity]
    alias = "t" if f.entity is Entity.TRACKS else "z"
    tcol = TIME_COLUMN[f.entity]

    where = [f"{alias}.tenant_id = %s", f"{alias}.site_id = %s",
             f"{tcol} >= %s", f"{tcol} < %s"]
    params: list[Any] = [tenant_id, site_id, f.start, f.end]

    for p in f.predicates:
        if p.field in MEASURED_ATTRS:
            continue                       # the tests we are deliberately relaxing
        col = cols[p.field]
        if p.op is Op.EQ:
            where.append(f"{col.sql} = %s")
            params.append(p.value)
        elif p.op is Op.IN:
            where.append(f"{col.sql} = ANY(%s)")
            params.append(list(p.value))
        # Other operators are left out on purpose: narrowing further can only make the subset
        # smaller, and a false "not measured" is worse than a false "no evidence" — it tells the
        # customer to go and enable a detector that is already running.

    missing: set[str] = set()
    with conn.cursor() as cur:
        for attr in sorted(wanted):
            cur.execute(
                f"SELECT 1 FROM {TABLE_ALIAS[f.entity]} "  # noqa: S608 - fixed identifiers only
                f"WHERE {' AND '.join(where)} AND {alias}.attrs ? %s LIMIT 1",
                (*params, attr),
            )
            if cur.fetchone() is None:
                missing.add(attr)
    return missing


# --- coverage --------------------------------------------------------------------------

def _coverage_scope(conn, f: Filter, site_id: str, camera_ids: list[str] | None) -> list[str]:
    """Which cameras the coverage figure should be computed over.

    If the question names cameras — "was anyone at the canteen door" — coverage must be that
    camera's uptime, not the site average. Averaging across a site hides exactly the outage the
    question is about: seven healthy cameras and one dead one reads as 88% covered, and the
    answer for the dead one comes back a confident zero.

    Precedence: the session's camera scope (a permission boundary) narrows first, then the
    question's own camera predicates narrow further.
    """
    scope = camera_ids or _cameras_in_scope(conn, site_id)
    named: set[str] = set()
    for p in f.predicates:
        if p.field != "camera_id":
            continue
        if p.op is Op.EQ:
            named.add(str(p.value))
        elif p.op is Op.IN:
            named.update(str(v) for v in p.value)
    if named:
        # Intersect, never widen: a filter must not reach past the session's permitted cameras.
        narrowed = [c for c in scope if c in named]
        return narrowed or scope
    return scope


def _cameras_in_scope(conn, site_id: str) -> list[str]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT camera_id FROM cameras WHERE site_id = %s AND enabled AND NOT excluded",
            (site_id,),
        )
        return [str(r[0]) for r in cur.fetchall()]


def _coverage(conn, cameras: list[str], start: datetime, end: datetime) -> float:
    if not cameras:
        return 0.0
    with conn.cursor() as cur:
        cur.execute("SELECT coverage_pct(%s::uuid[], %s, %s)", (cameras, start, end))
        return float(cur.fetchone()[0] or 0.0)


def _gaps(conn, cameras: list[str], start: datetime, end: datetime) -> list[Gap]:
    if not cameras:
        return []
    with conn.cursor() as cur:
        cur.execute(
            "SELECT camera_id, camera_name, gap_start, gap_end "
            "FROM coverage_gaps(%s::uuid[], %s, %s)", (cameras, start, end),
        )
        return [Gap(str(c), n, s, e) for c, n, s, e in cur.fetchall()]


def _gap_phrase(gaps: list[Gap]) -> str:
    if not gaps:
        return ""
    worst = sorted(gaps, key=lambda g: -g.minutes)[:3]
    parts = [f"{g.camera_name} was down for {g.minutes} min" for g in worst]
    extra = f", and {len(gaps) - 3} other gap(s)" if len(gaps) > 3 else ""
    return "; ".join(parts) + extra + "."


# --- evidence --------------------------------------------------------------------------

def _fetch_evidence(conn, f: Filter, tenant_id, site_id, camera_ids, limit) -> list[Evidence]:
    """Pull the frames that back the answer.

    Never paraphrase a claim you cannot attach a frame to. Ordered by confidence rather than
    time, because an operator scanning a filmstrip wants the clearest example first.
    """
    ev = Filter(entity=f.entity, select=Select.ROWS, start=f.start, end=f.end,
                predicates=f.predicates, limit=limit, min_confidence=f.min_confidence)
    q = compile_sql(ev, tenant_id=tenant_id, site_id=site_id, camera_ids=camera_ids)

    with conn.cursor() as cur:
        cur.execute(q.sql, q.params)
        names = [d.name for d in cur.description]
        rows = [dict(zip(names, r, strict=True)) for r in cur.fetchall()]
        cam_names = _camera_names(cur, site_id)

    out: list[Evidence] = []
    for r in rows:
        cid = str(r["camera_id"])
        out.append(Evidence(
            track_id=str(r.get("track_id") or r.get("event_id")),
            camera_id=cid,
            camera_name=cam_names.get(cid, "unknown camera"),
            ts=r.get("ts_start") or r.get("ts"),
            confidence=float(r.get("conf_max") or r.get("conf") or 0.0),
            keyframe_uri=r.get("best_keyframe_uri") or r.get("evidence_uri"),
            frame_sha256=r.get("frame_sha256"),
            attrs=r.get("attrs") or {},
        ))
    out.sort(key=lambda e: -e.confidence)
    return out


def _camera_names(cur, site_id: str) -> dict[str, str]:
    cur.execute("SELECT camera_id, name FROM cameras WHERE site_id = %s", (site_id,))
    return {str(c): n for c, n in cur.fetchall()}


# --- presentation & audit --------------------------------------------------------------

def _fmt_count(a: Answer) -> str:
    if a.rows:
        return f"{len(a.rows)} result(s)."
    if a.groups:
        return f"{len(a.groups)} group(s), {a.confident} confident event(s) in total."
    if a.ambiguous:
        return (f"{a.confident} confident, {a.ambiguous} ambiguous "
                f"({a.coverage_pct:.0%} camera coverage).")
    return f"{a.confident} ({a.coverage_pct:.0%} camera coverage)."


def _log(conn, a: Answer, f: Filter, q: CompiledQuery, tenant_id, site_id, actor, scope) -> None:
    """Append to the audit trail. This runs for refusals too — a question that was asked and
    declined is exactly as interesting to a regulator as one that was answered."""
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO answers (tenant_id, site_id, actor, question, intent, plan, "
            "sql_executed, cameras_touched, window_start, window_end, rows_returned, "
            "coverage_pct, abstained, abstain_reason, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s::uuid[],%s,%s,%s,%s,%s,%s,%s)",
            (tenant_id, site_id, actor, a.question, f.entity.value,
             _plan_json(f), q.sql, scope, f.start, f.end, a.total,
             a.coverage_pct, a.abstained, a.abstain_reason, a.latency_ms),
        )
    conn.commit()


def log_uninterpreted(
    conn, *, question: str, tenant_id: str, site_id: str, actor: str,
    reason: str, latency_ms: int = 0,
) -> None:
    """Record a question that never reached the database.

    Easy to overlook, and a real gap when it is: the audit trail is the anti-stalking control
    and the artefact a security review asks for, so the *attempt* is the event worth recording —
    not the answer. A question that failed to compile leaves no other trace, which means someone
    probing the system, or an employee looking up a colleague, would be invisible purely because
    they phrased it badly.
    """
    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO answers (tenant_id, site_id, actor, question, intent, "
            "rows_returned, abstained, abstain_reason, latency_ms) "
            "VALUES (%s,%s,%s,%s,%s,0,true,'not_measured',%s)",
            (tenant_id, site_id, actor, question, f"uninterpreted: {reason}"[:200], latency_ms),
        )
    conn.commit()


def _plan_json(f: Filter):
    from psycopg.types.json import Jsonb
    return Jsonb({
        "entity": f.entity.value,
        "select": f.select.value,
        "filters": [{"field": p.field, "op": p.op.value, "value": p.value} for p in f.predicates],
        "group_by": f.group_by,
        "min_confidence": f.min_confidence,
        "describes": describe(f),
    })

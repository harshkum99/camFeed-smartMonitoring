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

from smartcam.query.filters import (
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
    #: When the frame shown was captured — the track's best frame, not necessarily its first.
    ts: datetime
    confidence: float
    keyframe_uri: str | None
    frame_sha256: str | None = None
    attrs: dict[str, Any] = field(default_factory=dict)
    bbox: list[float] | None = None
    track_start: datetime | None = None


@dataclass
class Gap:
    camera_id: str
    camera_name: str
    start: datetime
    end: datetime
    #: True when the camera's footage came from an imported recording. A hole there means "no
    #: footage was imported for this time", not "the camera was down", and must be worded so.
    recorded: bool = False

    @property
    def minutes(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def seconds(self) -> float:
        return (self.end - self.start).total_seconds()


@dataclass
class CameraLimit:
    """A camera in scope that cannot vouch for the asked class, and why."""

    camera_id: str
    camera_name: str
    cls: str
    status: str               # unreliable | limited | not_assessed | absent
    frame_recall: float | None = None

    def phrase(self) -> str:
        recall = (f" (measured detection recall {self.frame_recall:.0%} against ground truth)"
                  if self.frame_recall is not None else "")
        things = _plural(self.cls)
        return {
            "unreliable": f"{self.camera_name} cannot reliably detect {things}{recall}, so it "
                          f"does not count towards coverage",
            "limited": f"{self.camera_name} may miss {things}{recall}",
            "not_assessed": f"{self.camera_name}'s ability to detect {things} has not been "
                            f"measured yet, so a result there cannot be treated as complete",
            "absent": f"no {self.cls} detector ran on {self.camera_name}",
        }[self.status]


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
    limits: list[CameraLimit] = field(default_factory=list)
    #: Qualifications that are not about a camera's detector, e.g. an attribute measured on only
    #: some of the cameras in scope.
    notes: list[str] = field(default_factory=list)
    scope_cameras: list[str] = field(default_factory=list)
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
    scope = _coverage_scope(conn, f, site_id, camera_ids)
    a = Answer(question=question, describes="", sql="", scope_cameras=list(scope))
    a.limits = _limits(conn, f, scope)
    # Tracks from these cameras never count as confident: they cannot vouch for the asked class.
    unreliable = [lim.camera_id for lim in a.limits if lim.status in ("unreliable", "absent")]

    q = compile_sql(f, tenant_id=tenant_id, site_id=site_id, camera_ids=camera_ids,
                    unreliable_camera_ids=unreliable)
    a.describes, a.sql = q.describes, q.sql

    # Cameras that cannot answer this particular question: blind or unassessed for the class, or
    # missing the attribute or the zone it turns on. Each is named in the answer, and none counts
    # towards coverage — however long it was online, its silence is not evidence.
    unusable: set[str] = {lim.camera_id for lim in a.limits
                          if lim.status in ("unreliable", "absent", "not_assessed")}
    refusal = _refuse_before_querying(conn, f, a, tenant_id, site_id, scope, unusable)

    usable = [c for c in scope if c not in unusable]
    a.coverage_pct = (_coverage(conn, usable, f.start, f.end) * len(usable) / len(scope)
                      if scope and usable else 0.0)
    a.gaps = _gaps(conn, scope, f.start, f.end)

    if refusal is None:
        _execute(conn, q, f, a, unreliable)
        _explain(conn, f, a, coverage_floor, bool(unusable))
        if not a.evidence and (a.rows or a.total):
            a.evidence = _fetch_evidence(conn, f, tenant_id, site_id, camera_ids, evidence_limit)

    a.latency_ms = int((time.monotonic() - t0) * 1000)
    _log(conn, a, f, q, tenant_id, site_id, actor, scope)
    return a


def _explain(conn, f: Filter, a: Answer, floor: float, some_unusable: bool) -> None:
    limit_note = _limits_phrase(a.limits) + "".join(f"{n}. " for n in a.notes)
    qualified = a.coverage_pct < floor or some_unusable or bool(a.limits) or bool(a.notes)

    if not a.rows and not a.groups and a.total == 0:
        a.abstained = True
        if a.coverage_pct < floor or some_unusable:
            # Explicit rather than falling out of the percentage: one blind camera among eight
            # still leaves 87% coverage, above the floor, and its silence would otherwise be
            # reported as "a real negative".
            a.abstain_reason = "no_coverage"
            a.message = (
                f"{_only(a.coverage_pct)} of that period was usably covered. "
                f"{_gap_phrase(a.gaps)} {limit_note}"
                f"I can't tell you nothing happened — I can only tell you nothing was recorded."
            ).replace("  ", " ")
            return
        a.abstain_reason = "no_evidence"
        if a.limits:
            # Covered, and nothing found — but by cameras measured to miss some of what they
            # look at. That is evidence of absence, not proof of it.
            a.message = (
                f"No matching events found with {_pct(a.coverage_pct)} coverage, but {limit_note}"
                f"so this is not a confirmed negative.").replace("  ", " ")
            return
        a.message = (
            f"No matching events found, and camera coverage was {_pct(a.coverage_pct)} for that "
            f"period, so this is a real negative rather than a gap. Try widening the time window."
        )
        return

    if a.is_count and a.confident == 0 and a.ambiguous > 0:
        a.abstained = True
        a.abstain_reason = "low_confidence"
        a.message = (
            f"Found {a.ambiguous} possible match(es), but none above the "
            f"{f.min_confidence:.0%} confidence threshold. Showing them for you to confirm."
        )
        if qualified:
            a.message += (f" Coverage was {_pct(a.coverage_pct)}. "
                          f"{_gap_phrase(a.gaps)} {limit_note}").rstrip()
        return

    if a.coverage_pct < floor or some_unusable:
        # Not a refusal: we did find something. But the number is incomplete and saying so is
        # the difference between a defensible answer and a mis-sell.
        a.message = (
            f"{_fmt_count(a, f).rstrip('.')} — but only {_pct(a.coverage_pct)} of the requested "
            f"period was usably covered by these cameras. {_gap_phrase(a.gaps)} {limit_note}"
        ).replace("  ", " ").rstrip()
        return
    a.message = " ".join(
        f"{_fmt_count(a, f)} {limit_note}{_track_count_caveat(conn, f, a)}".split())


def _track_count_caveat(conn, f: Filter, a: Answer) -> str:
    """What a count of tracks is, stated beside the number, for cameras with measured recall.

    The product counts tracks. A person who leaves view and comes back is two tracks, and a
    detector that finds three people in four misses the fourth — both are true of a camera rated
    reliable, and a number shown without them reads as a headcount.
    """
    if f.entity is not Entity.TRACKS or f.select is Select.ROWS or not a.total:
        return ""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT name, (capabilities->'classes'->'person'->>'frame_recall')::real FROM cameras "
            "WHERE camera_id = ANY(%s::uuid[]) AND capabilities->'classes'->'person'->>'status' "
            "= 'reliable' ORDER BY name", (list(a.scope_cameras),))
        measured = [(n, r) for n, r in cur.fetchall() if r is not None]
    if not measured:
        return ""
    recall = ", ".join(f"{n} {r:.0%}" for n, r in measured)
    return (f" Counted as tracks, not identified individuals: someone who leaves view and returns "
            f"counts again, and measured detection recall is {recall}.")


def _refuse_before_querying(conn, f: Filter, a: Answer, tenant_id, site_id,
                            scope: list[str], unusable: set[str]) -> str | None:
    """Refusals that do not depend on what the query would return.

    Run first, and they leave no rows, counts or evidence behind: a "not measured" answer shown
    beside a list of results is a contradiction the operator has to resolve, and they will
    resolve it by believing the results. Cameras that can only partly answer are added to
    `unusable` and named in a note instead.
    """
    nowhere, partly = _unmeasured_attrs(conn, f, tenant_id, site_id, scope)
    if nowhere:
        names = ", ".join(sorted(nowhere))
        return _refuse(a, "not_measured",
            f"No detector for '{names}' was running on these cameras during that period, so this "
            f"question cannot be answered from the record — the answer is not zero, it is "
            f"unknown. I can enable it going forward.")
    for attr, cams in sorted(partly.items()):
        # Measured on some cameras, not on others. The count can only ever include the cameras
        # that measured it, so say which ones did not rather than letting their silence count.
        unusable.update(cams)
        a.notes.append(f"'{attr}' was not measured on {_names(conn, cams)}, so those cameras are "
                       f"not included in this answer")

    if f.entity is Entity.ZONE_EVENTS and scope:
        zoned = _cameras_with_zones(conn, scope)
        unzoned = [c for c in scope if c not in zoned]
        if not zoned:
            return _refuse(a, "not_measured",
                f"No zone or line is drawn on {_names(conn, scope)}, so zone events were never "
                f"evaluated there — the answer is unknown, not zero. Draw a zone to start "
                f"measuring it.")
        if unzoned:
            unusable.update(unzoned)
            a.notes.append(f"no zone is drawn on {_names(conn, unzoned)}, so those cameras are "
                           f"not included in this answer")

    absent = [lim for lim in a.limits if lim.status == "absent"]
    if scope and absent and len(absent) == len(scope):
        cls = absent[0].cls
        return _refuse(a, "not_measured",
            f"No {cls} detector ran on {', '.join(lim.camera_name for lim in absent)}, so this "
            f"cannot be answered from the record — it is unknown, not zero.")
    return None


def _refuse(a: Answer, reason: str, message: str) -> str:
    a.abstained, a.abstain_reason, a.message = True, reason, message
    a.rows, a.groups, a.evidence = [], [], []
    a.confident = a.ambiguous = a.total = 0
    return reason


# --- execution -------------------------------------------------------------------------

def _execute(conn, q: CompiledQuery, f: Filter, a: Answer, unreliable: list[str]) -> None:
    with conn.cursor() as cur:
        cur.execute(q.sql, q.params)
        names = [d.name for d in cur.description]
        fetched = cur.fetchall()

    if f.select is Select.ROWS:
        a.rows = [dict(zip(names, r, strict=True)) for r in fetched]
        a.total = len(a.rows)
        a.confident = sum(
            1 for r in a.rows
            if (r.get("conf_mean") if f.entity is Entity.TRACKS else r.get("conf")) is not None
            and float(r.get("conf_mean") if f.entity is Entity.TRACKS else r.get("conf"))
            >= f.min_confidence
            and str(r.get("camera_id")) not in unreliable)
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


def _unmeasured_attrs(conn, f: Filter, tenant_id: str, site_id: str,
                      scope: list[str] | None = None) -> tuple[set[str], dict[str, list[str]]]:
    """Which attributes the filter relies on were never populated *for the things it asked about*.

    A helmet predicate returning nothing means one of two very different things: nobody was
    without a helmet, or nobody ever ran a helmet detector there. Only the database knows which.

    Two scopings matter. The check keeps the filter's own non-attribute predicates (camera, class,
    event type) and drops only the attribute tests, so hi-vis vests on *vehicles* are not declared
    measured because vests on people were. And it is evaluated per camera in scope: if any camera
    with matching objects never recorded the attribute, the question is not answerable for that
    camera — a site-wide "yes, measured somewhere" would turn that camera's silence into a zero.

    Returns the attributes measured nowhere in scope (a refusal) and, separately, those measured
    on some cameras but not others (a qualification naming the cameras left out).
    """
    wanted = {p.field for p in f.predicates} & MEASURED_ATTRS
    if not wanted:
        return set(), {}

    cols = SCHEMA[f.entity]
    alias = "t" if f.entity is Entity.TRACKS else "z"
    tcol = TIME_COLUMN[f.entity]

    where = [f"{alias}.tenant_id = %s", f"{alias}.site_id = %s",
             f"{tcol} >= %s", f"{tcol} < %s"]
    params: list[Any] = [tenant_id, site_id, f.start, f.end]
    if scope:
        where.append(f"{alias}.camera_id = ANY(%s::uuid[])")
        params.append(list(scope))

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

    nowhere: set[str] = set()
    partly: dict[str, list[str]] = {}
    with conn.cursor() as cur:
        for attr in sorted(wanted):
            # Per camera: did this camera record the attribute on any matching object? A camera
            # with no matching objects at all says nothing either way and is not counted.
            cur.execute(
                f"SELECT {alias}.camera_id, bool_or({alias}.attrs ? %s) "  # noqa: S608
                f"FROM {TABLE_ALIAS[f.entity]} WHERE {' AND '.join(where)} "
                f"GROUP BY {alias}.camera_id",
                (attr, *params),
            )
            per_camera = cur.fetchall()
            lacking = [str(c) for c, measured in per_camera if not measured]
            if not per_camera or len(lacking) == len(per_camera):
                nowhere.add(attr)
            elif lacking:
                partly[attr] = lacking
    return nowhere, partly


def _cameras_with_zones(conn, scope: list[str]) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT DISTINCT camera_id FROM zones WHERE camera_id = ANY(%s::uuid[])",
                    (list(scope),))
        return {str(r[0]) for r in cur.fetchall()}


def _names(conn, cameras: list[str]) -> str:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM cameras WHERE camera_id = ANY(%s::uuid[]) ORDER BY name",
                    (list(cameras),))
        return ", ".join(r[0] for r in cur.fetchall()) or "these cameras"


def _limits(conn, f: Filter, scope: list[str]) -> list[CameraLimit]:
    """Cameras in scope whose measured capability for the asked class qualifies the answer.

    Only cameras that carry a capability record are judged. Cameras without one predate
    measurement and are treated as before, so the synthetic seed site is unaffected.
    """
    if not scope:
        return []
    classes = [p.value for p in f.predicates if p.field == "class" and p.op is Op.EQ]
    classes += [v for p in f.predicates if p.field == "class" and p.op is Op.IN for v in p.value]
    # No class named: people are what the product counts, and a presence question without a class
    # is overwhelmingly about people. Judging nothing would silently skip the check entirely.
    cls = str(classes[0]) if len(set(classes)) == 1 else "person"

    with conn.cursor() as cur:
        cur.execute("SELECT camera_id, name, capabilities FROM cameras "
                    "WHERE camera_id = ANY(%s::uuid[]) ORDER BY name", (list(scope),))
        rows = cur.fetchall()
    out: list[CameraLimit] = []
    for cid, name, caps in rows:
        if not isinstance(caps, dict) or "classes" not in caps:
            continue
        entry = caps["classes"].get(cls)
        if entry is None:
            out.append(CameraLimit(str(cid), name, cls, "absent"))
            continue
        status = entry.get("status", "not_assessed")
        if status != "reliable":
            out.append(CameraLimit(str(cid), name, cls, status, entry.get("frame_recall")))
    return out


def _plural(cls: str) -> str:
    return {"person": "people", "vehicle": "vehicles"}.get(cls, f"{cls}s")


def _limits_phrase(limits: list[CameraLimit]) -> str:
    return ("; ".join(lim.phrase() for lim in limits) + ". ") if limits else ""


def _only(v: float) -> str:
    pct = _pct(v)
    return "Under 0.1%" if pct == "under 0.1%" else f"Only {pct}"


def _pct(v: float) -> str:
    if 0 < v < 0.001:
        return "under 0.1%"
    return f"{v:.1%}" if 0 < v < 0.1 else f"{v:.0%}"


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
            "SELECT g.camera_id, g.camera_name, g.gap_start, g.gap_end, "
            "EXISTS (SELECT 1 FROM camera_uptime u WHERE u.camera_id = g.camera_id "
            "        AND u.source = 'recorded_import') "
            "FROM coverage_gaps(%s::uuid[], %s, %s) g", (cameras, start, end),
        )
        return [Gap(str(c), n, s, e, bool(r)) for c, n, s, e, r in cur.fetchall()]


def _gap_phrase(gaps: list[Gap]) -> str:
    if not gaps:
        return ""
    # Recorded cameras are summarised per camera: an import with footage at 11:55 and 13:50
    # produces three separate holes in a day, and listing the three largest reads as if the camera
    # failed three times. Supervised cameras keep one entry per outage, because each is an event.
    per_camera: dict[str, float] = {}
    live: list[Gap] = []
    names: dict[str, str] = {}
    for g in gaps:
        if g.recorded:
            per_camera[g.camera_id] = per_camera.get(g.camera_id, 0.0) + g.seconds
            names[g.camera_id] = g.camera_name
        else:
            live.append(g)
    # Recorders start each file a few seconds apart, so a query window rarely lines up with a clip
    # to the second. Holes under a minute on a recorded camera are that stagger, not a finding,
    # and still count against the percentage — they are only left out of the sentence.
    parts = [(secs, f"{names[c]} has no footage for {_duration(secs)}")
             for c, secs in per_camera.items() if secs >= 60]
    parts += [(g.seconds, f"{g.camera_name} was down for {g.minutes} min") for g in live]
    parts.sort(key=lambda x: -x[0])
    if not parts:
        return ""
    extra = f", and {len(parts) - 3} other gap(s)" if len(parts) > 3 else ""
    return "; ".join(p for _, p in parts[:3]) + extra + "."


def _duration(seconds: float) -> str:
    minutes = int(seconds // 60)
    if minutes < 1:
        return "under a minute"
    if minutes < 60:
        return f"{minutes} min"
    h, m = divmod(minutes, 60)
    return f"{h} h {m} min" if m else f"{h} h"


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
            ts=r.get("keyframe_ts") or r.get("ts_start") or r.get("ts"),
            confidence=float(r.get("conf_max") or r.get("conf") or 0.0),
            keyframe_uri=r.get("best_keyframe_uri") or r.get("evidence_uri"),
            frame_sha256=r.get("frame_sha256"),
            attrs=r.get("attrs") or {},
            bbox=r.get("keyframe_bbox"),
            track_start=r.get("ts_start"),
        ))
    out.sort(key=lambda e: -e.confidence)
    return out


def _camera_names(cur, site_id: str) -> dict[str, str]:
    cur.execute("SELECT camera_id, name FROM cameras WHERE site_id = %s", (site_id,))
    return {str(c): n for c, n in cur.fetchall()}


# --- presentation & audit --------------------------------------------------------------

def _fmt_count(a: Answer, f: Filter | None = None) -> str:
    if a.rows:
        if f is not None and len(a.rows) >= f.limit:
            return f"Showing the first {len(a.rows)} result(s); there may be more."
        return f"{len(a.rows)} result(s)."
    if a.groups:
        return f"{len(a.groups)} group(s), {a.confident} confident event(s) in total."
    if a.ambiguous:
        return (f"{a.confident} confident, {a.ambiguous} ambiguous "
                f"({_pct(a.coverage_pct)} camera coverage).")
    return f"{a.confident} ({_pct(a.coverage_pct)} camera coverage)."


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
            "VALUES (%s,%s,%s,%s,%s,0,true,NULL,%s)",
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

"""The filter compiler — the boundary between the language model and the database.

The model never writes SQL and never computes a number. It emits a JSON filter object against a
small whitelist of fields and operators, and this module compiles that to parameterised SQL. The
database does all arithmetic.

That design is not defensive pedantry, it is what makes the product work:

  * Free-form text-to-SQL over a wide schema measures around 21% execution accuracy on Spider
    2.0. Constraining the model to filling parameters against ~20 well-named columns turns the
    same task into Spider-1.0-easy, which is around 91%.
  * Language models are demonstrably bad at counting — accuracy falls from roughly 0.60 to 0.45
    as compositional depth grows. Every count in Sanjay is a SQL COUNT over `zone_events`, so
    the model's arithmetic ability never enters the answer.
  * Values are always bound parameters, never interpolated. A model that has read a customer's
    camera names — which are attacker-influenced data — cannot construct a query.
  * `tenant_id` and `site_id` are injected by us from the authenticated session and can never be
    supplied by the model. Cross-tenant leakage is impossible by construction rather than by
    prompt discipline.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

MAX_LIMIT = 500
DEFAULT_LIMIT = 100
#: A query with no upper bound on its time range scans every partition. Windows are also how a
#: coverage percentage is computed, so an unbounded query could not be honestly answered anyway.
MAX_WINDOW = timedelta(days=400)


class Entity(StrEnum):
    """The two tables a question may be compiled against.

    ZONE_EVENTS answers "how many" and "who entered". TRACKS answers "who was present" and
    "how long". Counting a *person* is a zone-event question; counting *presence* is a track
    question, and conflating them is the most common way to get a count wrong.
    """

    TRACKS = "tracks"
    ZONE_EVENTS = "zone_events"


class Select(StrEnum):
    COUNT = "count"
    DISTINCT_COUNT = "distinct_count"
    ROWS = "rows"


class Op(StrEnum):
    EQ = "eq"
    NE = "ne"
    IN = "in"
    NOT_IN = "not_in"
    LT = "lt"
    LTE = "lte"
    GT = "gt"
    GTE = "gte"
    IS_NULL = "is_null"
    NOT_NULL = "not_null"


_SQL_OP = {
    Op.EQ: "=", Op.NE: "<>", Op.LT: "<", Op.LTE: "<=", Op.GT: ">", Op.GTE: ">=",
}


@dataclass(frozen=True)
class Column:
    """A queryable column. `sql` is emitted verbatim, so it must never contain user input."""

    sql: str
    kind: str                       # text | number | timestamp | uuid | jsonb_number
    ops: frozenset[Op]
    describe: str


_NUM_OPS = frozenset({Op.EQ, Op.NE, Op.LT, Op.LTE, Op.GT, Op.GTE, Op.IS_NULL, Op.NOT_NULL})
_TXT_OPS = frozenset({Op.EQ, Op.NE, Op.IN, Op.NOT_IN, Op.IS_NULL, Op.NOT_NULL})

# The whitelist. Adding a column here is a deliberate act: it widens what the model can ask for,
# and every addition makes the compile step measurably less reliable.
SCHEMA: dict[Entity, dict[str, Column]] = {
    Entity.TRACKS: {
        "camera_id":   Column("t.camera_id", "uuid", _TXT_OPS, "camera"),
        "class":       Column("t.class", "text", _TXT_OPS, "object class"),
        "ts_start":    Column("t.ts_start", "timestamp", _NUM_OPS,
                              "when the object first appeared"),
        "ts_end":      Column("t.ts_end", "timestamp", _NUM_OPS, "when it was last seen"),
        "dwell_s":     Column("t.dwell_s", "number", _NUM_OPS, "seconds present, for loitering"),
        "conf_max":    Column("t.conf_max", "number", _NUM_OPS, "peak detection confidence"),
        "n_frames":    Column("t.n_frames", "number", _NUM_OPS, "frames the object was tracked"),
        "identity":    Column("t.global_identity_id", "uuid", _TXT_OPS, "cross-camera identity"),
        # Attributes live in jsonb and are stored as confidences, not booleans, so a rule can
        # demand a confidence floor. "no helmet" is helmet < 0.5, never helmet IS FALSE.
        "helmet":      Column("(t.attrs->>'helmet')::real", "jsonb_number", _NUM_OPS,
                              "helmet confidence 0-1"),
        "vest":        Column("(t.attrs->>'vest')::real", "jsonb_number", _NUM_OPS,
                              "hi-vis vest confidence 0-1"),
        "upper_colour": Column("t.attrs->>'upper_colour'", "text", _TXT_OPS, "clothing colour"),
    },
    Entity.ZONE_EVENTS: {
        "camera_id":   Column("z.camera_id", "uuid", _TXT_OPS, "camera"),
        "zone_id":     Column("z.zone_id", "uuid", _TXT_OPS, "which zone"),
        "type":        Column("z.type::text", "text", _TXT_OPS, "event type"),
        "ts":          Column("z.ts", "timestamp", _NUM_OPS, "when it happened"),
        "conf":        Column("z.conf", "number", _NUM_OPS, "confidence 0-1"),
        "track_id":    Column("z.track_id", "uuid", _TXT_OPS, "the object that triggered it"),
        "helmet":      Column("(z.attrs->>'helmet')::real", "jsonb_number", _NUM_OPS,
                              "helmet confidence 0-1"),
    },
}

TIME_COLUMN = {Entity.TRACKS: "t.ts_start", Entity.ZONE_EVENTS: "z.ts"}
TABLE_ALIAS = {Entity.TRACKS: "tracks t", Entity.ZONE_EVENTS: "zone_events z"}

# Attribute predicates need their own ambiguity band, separate from detection confidence.
#
# A rule like "helmet < 0.5" fires on a score of 0.46 exactly as it fires on 0.04, and if the
# person detection was confident, both get reported as confident violations. They are not the
# same thing: 0.46 usually means the head crop was small, or the worker was facing away, or the
# cap was white. Negative-attribute rules are the highest false-alarm class in the product and
# the first one a factory tests.
#
# The band is a fixed property of the DETECTOR, not of the query. That distinction matters: an
# earlier version subtracted a margin from whatever threshold the question used, so asking for
# "helmet < 0.25" narrowed the confident bucket to "< 0.05" and emptied it — punishing the
# operator for asking a stricter question. Scores in this range are where the model is genuinely
# unsure; anything outside it is decisive however the question was phrased.
ATTR_AMBIGUOUS_LOW = 0.35
ATTR_AMBIGUOUS_HIGH = 0.65

#: Attributes stored as jsonb confidences, for which the margin above applies.
CONFIDENCE_ATTRS = {"helmet", "vest"}

#: Group-by is restricted to low-cardinality dimensions. Grouping by track_id would return a row
#: per object and defeat the point of an aggregate.
GROUPABLE = {
    Entity.TRACKS: {"camera_id", "class", "upper_colour"},
    Entity.ZONE_EVENTS: {"camera_id", "zone_id", "type"},
}


class FilterError(ValueError):
    """A filter the model produced that we refuse to compile.

    The message is fed back to the model for one repair attempt, so it must say what is allowed
    rather than merely that something was wrong.
    """


@dataclass
class Predicate:
    field: str
    op: Op
    value: Any = None


@dataclass
class Filter:
    """The only thing a model is allowed to emit."""

    entity: Entity
    select: Select
    start: datetime
    end: datetime
    predicates: list[Predicate] = field(default_factory=list)
    group_by: list[str] = field(default_factory=list)
    limit: int = DEFAULT_LIMIT
    #: Rows below this confidence are excluded from the confident count and reported separately
    #: as ambiguous. Surfaced to the user, because a threshold that is not shown is a lie.
    min_confidence: float = 0.5


@dataclass
class CompiledQuery:
    sql: str
    params: list[Any]
    entity: Entity
    select: Select
    describes: str


def parse(payload: dict[str, Any]) -> Filter:
    """Validate a model-produced JSON object into a Filter. Raises FilterError, never anything
    else, so the caller can hand the message back for a repair attempt."""
    if not isinstance(payload, dict):
        raise FilterError("filter must be a JSON object")

    entity = _enum(Entity, payload.get("entity"), "entity")
    select = _enum(Select, payload.get("select"), "select")
    cols = SCHEMA[entity]

    start = _time(payload.get("start"), "start")
    end = _time(payload.get("end"), "end")
    if end <= start:
        raise FilterError("end must be after start")
    if end - start > MAX_WINDOW:
        raise FilterError(
            f"time window is {(end - start).days} days; the maximum is {MAX_WINDOW.days}. "
            f"Narrow the window."
        )

    preds: list[Predicate] = []
    raw = payload.get("filters") or []
    if not isinstance(raw, list):
        raise FilterError("'filters' must be a list")
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise FilterError(f"filters[{i}] must be an object")
        name = item.get("field")
        if name not in cols:
            raise FilterError(
                f"unknown field '{name}' for {entity}. Available: {', '.join(sorted(cols))}"
            )
        col = cols[name]
        op = _enum(Op, item.get("op"), f"filters[{i}].op")
        if op not in col.ops:
            raise FilterError(
                f"operator '{op}' is not valid for '{name}' ({col.kind}). "
                f"Valid: {', '.join(sorted(o.value for o in col.ops))}"
            )
        value = item.get("value")
        _check_value(name, col, op, value)
        preds.append(Predicate(name, op, value))

    group_by = payload.get("group_by") or []
    if not isinstance(group_by, list):
        raise FilterError("'group_by' must be a list")
    for g in group_by:
        if g not in GROUPABLE[entity]:
            raise FilterError(
                f"cannot group by '{g}'. Groupable: {', '.join(sorted(GROUPABLE[entity]))}"
            )
    if group_by and select is Select.ROWS:
        raise FilterError("group_by requires select 'count' or 'distinct_count'")

    limit = payload.get("limit", DEFAULT_LIMIT)
    if not isinstance(limit, int) or isinstance(limit, bool) or limit < 1:
        raise FilterError("'limit' must be a positive integer")

    conf = payload.get("min_confidence", 0.5)
    if not isinstance(conf, int | float) or isinstance(conf, bool) or not 0.0 <= conf <= 1.0:
        raise FilterError("'min_confidence' must be a number between 0 and 1")

    return Filter(entity, select, start, end, preds, list(group_by),
                  min(int(limit), MAX_LIMIT), float(conf))


def _enum(cls, value, label):
    try:
        return cls(value)
    except (ValueError, KeyError):
        raise FilterError(
            f"'{label}' must be one of: {', '.join(m.value for m in cls)} (got {value!r})"
        ) from None


def _time(value, label) -> datetime:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str):
        raise FilterError(f"'{label}' must be an ISO-8601 timestamp string")
    try:
        dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise FilterError(f"'{label}' is not a valid ISO-8601 timestamp: {value!r}") from None
    if dt.tzinfo is None:
        raise FilterError(
            f"'{label}' must carry a timezone offset. Naive timestamps silently place people "
            f"at the wrong time of day, which is the failure a customer notices first."
        )
    return dt


def _check_value(name: str, col: Column, op: Op, value: Any) -> None:
    if op in (Op.IS_NULL, Op.NOT_NULL):
        return
    if op in (Op.IN, Op.NOT_IN):
        if not isinstance(value, list) or not value:
            raise FilterError(f"'{name}' with '{op}' needs a non-empty list")
        if len(value) > 100:
            raise FilterError(f"'{name}' list is too long (max 100)")
        for v in value:
            _scalar(name, col, v)
        return
    _scalar(name, col, value)


def _scalar(name: str, col: Column, value: Any) -> None:
    if col.kind in ("number", "jsonb_number"):
        if isinstance(value, bool) or not isinstance(value, int | float):
            raise FilterError(f"'{name}' expects a number, got {type(value).__name__}")
    elif col.kind == "timestamp":
        if not isinstance(value, str | datetime):
            raise FilterError(f"'{name}' expects an ISO-8601 timestamp")
    elif not isinstance(value, str):
        raise FilterError(f"'{name}' expects a string, got {type(value).__name__}")


def compile_sql(f: Filter, *, tenant_id: str, site_id: str,
                camera_ids: list[str] | None = None) -> CompiledQuery:
    """Compile a validated Filter to parameterised SQL.

    `tenant_id`, `site_id` and `camera_ids` come from the authenticated session and are never
    model-supplied. They are appended after the model's predicates so no filter can widen scope
    beyond what the caller is entitled to see.
    """
    cols = SCHEMA[f.entity]
    alias = "t" if f.entity is Entity.TRACKS else "z"
    tcol = TIME_COLUMN[f.entity]
    conf_col = "t.conf_max" if f.entity is Entity.TRACKS else "z.conf"

    # Placeholders are POSITIONAL, so parameters must be collected in the order the
    # placeholders appear in the finished SQL text — SELECT, then WHERE, then trailing clauses.
    # Collecting them per-clause and concatenating at the end makes that ordering structural.
    # A single running list shared across clauses that are built out of textual order mis-binds
    # every parameter, and the SQL still reads correctly on inspection: the placeholder count
    # matches, the types are wrong, and it only fails at execution.
    select_params: list[Any] = []
    where_params: list[Any] = []
    tail_params: list[Any] = []

    def binder(target: list[Any]):
        """Every model-supplied value goes through one of these. Nothing is ever interpolated
        into the SQL text, so camera names and attribute values — attacker-influenced data the
        model has read — cannot construct a query."""
        def bind(v: Any) -> str:
            target.append(v)
            return "%s"
        return bind

    bsel, bwhere, btail = binder(select_params), binder(where_params), binder(tail_params)

    # --- WHERE. Scope first, so it is impossible to read the query and miss it.
    where: list[str] = [
        f"{alias}.tenant_id = {bwhere(tenant_id)}",
        f"{alias}.site_id = {bwhere(site_id)}",
    ]
    if camera_ids:
        where.append(f"{alias}.camera_id = ANY({bwhere(camera_ids)})")
    where.append(f"{tcol} >= {bwhere(f.start)}")
    where.append(f"{tcol} < {bwhere(f.end)}")

    for p in f.predicates:
        col = cols[p.field]
        if p.op is Op.IS_NULL:
            where.append(f"{col.sql} IS NULL")
        elif p.op is Op.NOT_NULL:
            where.append(f"{col.sql} IS NOT NULL")
        elif p.op is Op.IN:
            where.append(f"{col.sql} = ANY({bwhere(list(p.value))})")
        elif p.op is Op.NOT_IN:
            # NOT (x = ANY(...)) is NULL when x is NULL, which silently drops rows whose
            # attribute was never measured. Those rows are exactly the ones an operator needs
            # to see, so make the NULL case explicit.
            where.append(f"({col.sql} IS NULL OR NOT ({col.sql} = ANY({bwhere(list(p.value))})))")
        else:
            where.append(f"{col.sql} {_SQL_OP[p.op]} {bwhere(p.value)}")

    # --- SELECT and trailing clauses.
    group_sql = [cols[g].sql for g in f.group_by]
    if f.select is Select.ROWS:
        select_sql = f"{alias}.*"
        tail = f" ORDER BY {tcol} DESC LIMIT {btail(f.limit)}"
    else:
        counted = f"DISTINCT {alias}.track_id" if f.select is Select.DISTINCT_COUNT else "*"

        # A row is confident only if BOTH the detection was confident AND — where the question
        # turns on a measured attribute — that attribute is clear of its threshold. Without the
        # second half, a helmet score of 0.46 is reported as a confident safety violation purely
        # because we were sure it was a person.
        def decisive(bind) -> str:
            clauses = [f"{conf_col} >= {bind(f.min_confidence)}"]
            for p in f.predicates:
                extra = _attr_margin_clause(cols, p, bind)
                if extra:
                    clauses.append(extra)
            return " AND ".join(clauses)

        # Bound once per occurrence: a value appearing twice in the SQL must be appended twice.
        select_sql = (
            f"count(*) FILTER (WHERE {decisive(bsel)}) AS confident, "
            f"count(*) FILTER (WHERE NOT ({decisive(bsel)})) AS ambiguous, "
            f"count({counted}) AS total"
        )
        tail = ""
        if group_sql:
            cols_sql = ", ".join(f"{g} AS group_{i}" for i, g in enumerate(group_sql))
            select_sql = f"{cols_sql}, {select_sql}"
            tail = (" GROUP BY " + ", ".join(group_sql)
                    + " ORDER BY confident DESC LIMIT " + btail(f.limit))

    sql = (f"SELECT {select_sql} FROM {TABLE_ALIAS[f.entity]} "
           f"WHERE {' AND '.join(where)}{tail}")
    return CompiledQuery(sql, select_params + where_params + tail_params,
                         f.entity, f.select, describe(f))


def _attr_margin_clause(cols: dict[str, Column], p: Predicate, bind) -> str | None:
    """Require a confidence-valued attribute to sit outside the detector's uncertainty band
    before the row counts as confident.

    A score inside [ATTR_AMBIGUOUS_LOW, ATTR_AMBIGUOUS_HIGH] is one the model could not call, so
    it belongs in the ambiguous bucket for a human to adjudicate — regardless of how strict the
    question's own threshold was. Returns None for anything that is not a numeric comparison on
    such an attribute; equality and set membership have no band to apply.
    """
    if p.field not in CONFIDENCE_ATTRS or p.op not in (Op.LT, Op.LTE, Op.GT, Op.GTE):
        return None
    if not isinstance(p.value, int | float) or isinstance(p.value, bool):
        return None

    col = cols[p.field].sql
    if p.op in (Op.LT, Op.LTE):
        # Testing for absence: the score must be decisively low.
        return f"{col} < {bind(ATTR_AMBIGUOUS_LOW)}"
    # Testing for presence: the score must be decisively high.
    return f"{col} > {bind(ATTR_AMBIGUOUS_HIGH)}"


def describe(f: Filter) -> str:
    """Plain-English restatement, shown to the operator next to the answer.

    A model can silently misread a question; a person reading "counted zone entries where helmet
    confidence is below 0.5" spots it immediately. This is the cheapest correctness control in
    the whole query path.
    """
    what = {
        Select.COUNT: "counted",
        Select.DISTINCT_COUNT: "counted distinct objects in",
        Select.ROWS: "listed",
    }[f.select]
    noun = "tracked objects" if f.entity is Entity.TRACKS else "zone events"
    parts = [f"{what} {noun}"]
    for p in f.predicates:
        col = SCHEMA[f.entity][p.field]
        if p.op is Op.IS_NULL:
            parts.append(f"where {col.describe} is missing")
        elif p.op is Op.NOT_NULL:
            parts.append(f"where {col.describe} is present")
        elif p.op in (Op.IN, Op.NOT_IN):
            neg = "not " if p.op is Op.NOT_IN else ""
            parts.append(f"where {col.describe} is {neg}one of {', '.join(map(str, p.value))}")
        else:
            word = {Op.EQ: "is", Op.NE: "is not", Op.LT: "is below", Op.LTE: "is at most",
                    Op.GT: "is above", Op.GTE: "is at least"}[p.op]
            parts.append(f"where {col.describe} {word} {p.value}")
    parts.append(f"between {f.start:%d %b %Y %H:%M} and {f.end:%d %b %Y %H:%M}")
    if f.group_by:
        parts.append("grouped by " + ", ".join(f.group_by))
    return ", ".join(parts)


def schema_for_prompt() -> str:
    """The field list handed to the model. Deliberately small — every column added here costs
    accuracy at the compile step."""
    out: list[str] = []
    for entity, cols in SCHEMA.items():
        out.append(f"{entity}:")
        for name, col in cols.items():
            ops = ", ".join(sorted(o.value for o in col.ops))
            out.append(f"  {name} ({col.kind}) — {col.describe}. ops: {ops}")
    return "\n".join(out)

"""Turning a plain-language question into a validated filter.

This is the only place a language model touches the query path, and its output is treated as
untrusted throughout: it is validated by `filters.parse`, which rejects unknown fields, unknown
operators, and anything that would widen scope. A model that has read the customer's camera names
— attacker-influenced data — cannot construct a query, only fill in a form.

Two things are deliberately kept away from the model, because they are exactly what models get
wrong:

  * **UUIDs.** The model names cameras and zones the way a person does ("Gate 3", "the fire
    exit"), and we resolve those to ids. Asking a model to copy a 36-character identifier is
    inviting a transposed digit that silently queries the wrong camera.
  * **Date arithmetic.** "Yesterday", "last Tuesday" and "this shift" are computed here and
    offered as named windows for the model to pick from. Left to the model, this is the single
    most common source of a confidently wrong answer — a real observation attached to the wrong
    day.

If the model produces something invalid, it gets exactly one repair attempt with the validation
error fed back, because those errors are written to be actionable. A second failure is reported
to the operator rather than retried: a question we cannot compile is a question we should decline.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta, timezone, tzinfo
from typing import Any, Protocol
from zoneinfo import ZoneInfo

from smartcam.query.filters import Filter, FilterError, parse, schema_for_prompt

MAX_QUESTION_CHARS = 500


class ModelError(RuntimeError):
    """The provider failed or returned something unusable."""


class Provider(Protocol):
    """Anything that can turn a prompt into a JSON object.

    Deliberately minimal so the self-hosted path is a config change rather than a rewrite —
    the API is cheaper until roughly 2,500 cameras, and an on-premise deployment cannot use it
    at all, so both have to work from day one.
    """

    def complete(self, system: str, user: str) -> dict[str, Any]: ...


# --- catalogue -------------------------------------------------------------------------

@dataclass
class Catalog:
    """What exists at this site, in the words a person would use.

    Built from the database per request. It is also a scope boundary: a name the model invents
    resolves to nothing rather than to some other tenant's camera.

    `tz` is the site's IANA timezone, not a fixed offset: a fixed offset cannot represent daylight
    saving, and on the day clocks change "today" is 23 or 25 hours long. `as_of` is set for sites
    whose footage was imported from a recording — relative words like "this morning" then mean the
    morning of the recording, not of the day someone happens to ask.
    """

    cameras: dict[str, str] = field(default_factory=dict)      # name -> camera_id
    zones: dict[str, str] = field(default_factory=dict)        # name -> zone_id
    tz: str = "Asia/Kolkata"
    as_of: datetime | None = None
    measured_attrs: set[str] = field(default_factory=set)

    @property
    def zone(self) -> tzinfo:
        return ZoneInfo(self.tz)

    @property
    def tz_offset(self) -> timedelta:
        """The site's current UTC offset. Kept for callers that only need a label."""
        ref = self.as_of or datetime.now(UTC)
        return ref.astimezone(self.zone).utcoffset() or timedelta(0)

    @classmethod
    def load(cls, conn, site_id: str) -> Catalog:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT camera_id, name FROM cameras "
                "WHERE site_id = %s AND enabled AND NOT excluded", (site_id,))
            cameras = {n: str(c) for c, n in cur.fetchall()}
            cur.execute("SELECT zone_id, name FROM zones WHERE site_id = %s", (site_id,))
            zones = {n: str(z) for z, n in cur.fetchall()}
            cur.execute("SELECT tz, as_of FROM sites WHERE site_id = %s", (site_id,))
            row = cur.fetchone()
        tz, as_of = (row[0], row[1]) if row else ("Asia/Kolkata", None)
        return cls(cameras=cameras, zones=zones, tz=tz, as_of=as_of)

    def resolve_camera(self, name: str) -> str | None:
        return _resolve(name, self.cameras)

    def resolve_zone(self, name: str) -> str | None:
        return _resolve(name, self.zones)


def _resolve(name: str, table: dict[str, str]) -> str | None:
    """Match a human name to an id, tolerantly but never creatively.

    Exact, then case-insensitive, then a containment match — but a containment match is only
    accepted when exactly one candidate matches. Two plausible cameras means we would be
    guessing, and a query silently pointed at the wrong camera is worse than one that fails.
    """
    if not name:
        return None
    if name in table:
        return table[name]

    want = _norm(name)
    exact = [v for k, v in table.items() if _norm(k) == want]
    if len(exact) == 1:
        return exact[0]

    partial = [v for k, v in table.items() if want and (want in _norm(k) or _norm(k) in want)]
    return partial[0] if len(partial) == 1 else None


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


# --- time windows -----------------------------------------------------------------------

def named_windows(now: datetime, tz: str | tzinfo | timedelta) -> dict[str, tuple[str, str]]:
    """Precompute the windows people actually ask about, as ISO strings with the site's offset.

    The model picks a name; it never does the arithmetic. `now` is passed in rather than read
    from the clock so this is testable and so a demo can be replayed.

    Each endpoint carries its own offset. On a daylight-saving day the two ends of "today" have
    different offsets, and a window built by adding 24 hours to local midnight would end an hour
    into tomorrow — or an hour before today is over.
    """
    zone = _zone(tz)
    d = now.astimezone(zone).date()

    def at(day: date, hour: int = 0) -> datetime:
        return datetime.combine(day, time(hour), tzinfo=zone)

    def w(a: datetime, b: datetime) -> tuple[str, str]:
        return (a.isoformat(timespec="seconds"), b.isoformat(timespec="seconds"))

    one = timedelta(days=1)
    out = {
        "today": w(at(d), at(d + one)),
        "yesterday": w(at(d - one), at(d)),
        "this_week": w(at(d - timedelta(days=d.weekday())), at(d + one)),
        "last_7_days": w(at(d - timedelta(days=7)), at(d + one)),
        "last_30_days": w(at(d - timedelta(days=30)), at(d + one)),
        "this_morning": w(at(d, 6), at(d, 12)),
        "last_night": w(at(d - one, 20), at(d, 6)),
    }
    for i in range(1, 8):
        day = d - timedelta(days=i)
        out[day.strftime("%A").lower()] = w(at(day), at(day + one))
    return out


#: Windows that name a single calendar day, to which a clock time like "after 8pm" can apply.
SINGLE_DAY_WINDOWS = frozenset({"today", "yesterday", "monday", "tuesday", "wednesday",
                                "thursday", "friday", "saturday", "sunday"})


def _zone(tz: str | tzinfo | timedelta) -> tzinfo:
    if isinstance(tz, timedelta):
        return timezone(tz)
    if isinstance(tz, str):
        return ZoneInfo(tz)
    return tz


def clock_window(window: tuple[str, str], zone: tzinfo, from_time: str | None,
                 to_time: str | None) -> tuple[str, str]:
    """Narrow a single-day window to clock times, in the site's own timezone.

    Refuses a local time that does not exist — 02:30 on the morning clocks go forward — rather
    than letting Python quietly pick an offset for it and shift the window by an hour.
    """
    day = datetime.fromisoformat(window[0]).astimezone(zone).date()
    start = _clock(day, from_time, zone) if from_time else datetime.fromisoformat(window[0])
    if to_time:
        # "between 22:00 and 02:00" ends the next day. Decide that from the clock readings before
        # building the end instant: building 02:00 on the start day first would refuse a time that
        # only fails to exist on that day — which on a clocks-forward day it does.
        wraps = bool(from_time) and _hhmm(to_time) <= _hhmm(from_time)
        end = _clock(day + timedelta(days=1) if wraps else day, to_time, zone)
    else:
        end = datetime.fromisoformat(window[1])
    return start.isoformat(timespec="seconds"), end.isoformat(timespec="seconds")


def _hhmm(text: str | None) -> tuple[int, int]:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", (text or "").strip())
    return (int(m.group(1)), int(m.group(2))) if m else (-1, -1)


def _clock(day: date, hhmm: str, zone: tzinfo) -> datetime:
    m = re.fullmatch(r"(\d{1,2}):(\d{2})", hhmm.strip())
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        raise FilterError(f"clock time must be HH:MM in 24-hour form, got {hhmm!r}")
    naive = datetime.combine(day, time(int(m.group(1)), int(m.group(2))))
    aware = naive.replace(tzinfo=zone)
    if aware.astimezone(UTC).astimezone(zone).replace(tzinfo=None) != naive:
        raise FilterError(
            f"{hhmm} did not exist on {day:%d %b %Y} at this site (the clocks changed); "
            f"choose a time outside the daylight-saving jump")
    return aware


def _offset_str(off: timedelta) -> str:
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{sign}{total // 3600:02d}:{(total % 3600) // 60:02d}"


# --- prompt ------------------------------------------------------------------------------

SYSTEM = """You translate questions about CCTV footage into a JSON filter. You never answer the \
question yourself, never count anything, and never invent data.

Emit ONLY a JSON object with these keys:

  entity           "zone_events" (things that happened at a zone or line) or "tracks" (objects
                   that were present). Counting PEOPLE PASSING a point is zone_events. Counting
                   objects PRESENT, or asking how long something stayed, is tracks.
  select           "count", "distinct_count" or "rows".
                     distinct_count when the question counts PEOPLE or VEHICLES — one person
                     crossing a line five times is five events but one person.
                     rows when the question asks what/which/show me.
  window           the name of one of the time windows listed below.
  filters          list of {field, op, value}. Use ONLY the listed fields and operators.
  cameras          optional list of camera NAMES from the list below. Use when the question
                   names a place. Never invent a name; never write an id.
  zones            optional list of zone NAMES from the list below.
  group_by         optional, e.g. ["camera_id"] when the question says "by camera" or
                   "which outlet/area".
  limit            optional, for "rows".
  from_time        optional "HH:MM" (24-hour, site-local) for "after 8pm", "between 11:55 and
  to_time          12:00". Only with a single-day window (today, yesterday, a weekday name).

Rules that matter:
  - Attributes are CONFIDENCES from 0 to 1, never true/false. "without a helmet" is
    {"field":"helmet","op":"lt","value":0.5}. "wearing a helmet" is op "gt".
  - If the question does not name a place, leave cameras empty rather than guessing one.
  - Object classes are "person" and "vehicle". Presence questions on tracks should filter class.
  - If the site lists no zones, zone_events holds nothing: answer presence questions from tracks.
  - If the question cannot be expressed with the listed fields, emit
    {"unsupported": "<short reason>"} instead of forcing it. That is a correct answer.

Return the JSON object and nothing else."""


def build_user_prompt(question: str, catalog: Catalog, now: datetime) -> str:
    windows = named_windows(now, catalog.zone)
    local = now.astimezone(catalog.zone)
    replay = " — recorded footage; questions are answered as of this time" if catalog.as_of else ""
    lines = [
        f"Question: {question}",
        "",
        f"Site local time now: {local:%Y-%m-%d %H:%M} ({local:%A}) in {catalog.tz}{replay}.",
        "",
        "Time windows (use the NAME in the `window` field):",
    ]
    lines += [f"  {name}" for name in windows]
    lines += ["", "Cameras at this site:"]
    lines += [f"  {n}" for n in sorted(catalog.cameras)]
    if catalog.zones:
        lines += ["", "Zones:"]
        lines += [f"  {n}" for n in sorted(catalog.zones)]
    else:
        lines += ["", "No zones are drawn at this site, so zone_events holds no data."]
    lines += ["", "Fields:", schema_for_prompt()]
    return "\n".join(lines)


# --- compilation --------------------------------------------------------------------------

@dataclass
class Compiled:
    filter: Filter | None
    raw: dict[str, Any]
    unsupported: str | None = None
    repaired: bool = False
    unresolved: list[str] = field(default_factory=list)


def compile_question(
    question: str,
    catalog: Catalog,
    provider: Provider,
    *,
    now: datetime,
) -> Compiled:
    """Ask the model for a filter, validate it, and allow exactly one repair.

    Raises ModelError only when the provider itself fails. A question the model declines, or one
    that will not validate twice, comes back as `unsupported` — which the answer layer surfaces
    as a refusal rather than as an error.
    """
    question = (question or "").strip()
    if not question:
        return Compiled(None, {}, unsupported="empty question")
    if len(question) > MAX_QUESTION_CHARS:
        return Compiled(None, {}, unsupported="question is too long to interpret reliably")

    user = build_user_prompt(question, catalog, now)
    raw = _ask(provider, SYSTEM, user)

    if isinstance(raw.get("unsupported"), str):
        return Compiled(None, raw, unsupported=raw["unsupported"])

    try:
        payload, unresolved = _to_filter_payload(raw, catalog, now)
        return Compiled(parse(payload), raw, unresolved=unresolved)
    except FilterError as first:
        repair = (
            f"{user}\n\n"
            f"Your previous answer was rejected: {first}\n"
            f"Previous answer: {json.dumps(raw)[:800]}\n"
            f"Emit a corrected JSON object, or {{\"unsupported\": \"...\"}} if it cannot be done."
        )
        raw2 = _ask(provider, SYSTEM, repair)
        if isinstance(raw2.get("unsupported"), str):
            return Compiled(None, raw2, unsupported=raw2["unsupported"], repaired=True)
        try:
            payload2, unresolved2 = _to_filter_payload(raw2, catalog, now)
            return Compiled(parse(payload2), raw2, repaired=True, unresolved=unresolved2)
        except FilterError as second:
            # Two failures means the question is outside what the schema can express. Declining
            # is the right outcome — retrying a third time just spends money on the same answer.
            return Compiled(None, raw2, unsupported=f"could not interpret the question: {second}",
                            repaired=True)


def _ask(provider: Provider, system: str, user: str) -> dict[str, Any]:
    try:
        out = provider.complete(system, user)
    except Exception as e:  # noqa: BLE001 - provider failures are reported, never swallowed
        raise ModelError(str(e)) from e
    if not isinstance(out, dict):
        raise ModelError(f"provider returned {type(out).__name__}, expected a JSON object")
    return out


def _to_filter_payload(raw: dict[str, Any], catalog: Catalog,
                       now: datetime) -> tuple[dict[str, Any], list[str]]:
    """Turn the model's answer into something `filters.parse` can validate.

    Resolves names to ids and the window name to timestamps. Anything unresolvable is reported
    rather than dropped: silently ignoring a camera the operator named would answer a different
    question from the one they asked.
    """
    windows = named_windows(now, catalog.zone)
    name = str(raw.get("window", "")).strip().lower()
    win = windows.get(name)
    from_time, to_time = raw.get("from_time"), raw.get("to_time")
    if (from_time or to_time) and win:
        if name not in SINGLE_DAY_WINDOWS:
            raise FilterError(
                f"from_time/to_time only apply to a single-day window "
                f"({', '.join(sorted(SINGLE_DAY_WINDOWS))}), not '{name}'")
        win = clock_window(win, catalog.zone, from_time and str(from_time),
                           to_time and str(to_time))
    start, end = win if win else (raw.get("start"), raw.get("end"))

    payload: dict[str, Any] = {
        "entity": raw.get("entity"),
        "select": raw.get("select"),
        "start": start,
        "end": end,
        "filters": list(raw.get("filters") or []),
        "group_by": raw.get("group_by") or [],
    }
    if isinstance(raw.get("limit"), int):
        payload["limit"] = raw["limit"]

    unresolved: list[str] = []
    cams = [c for c in (raw.get("cameras") or []) if isinstance(c, str)]
    ids = []
    for name in cams:
        cid = catalog.resolve_camera(name)
        (ids.append(cid) if cid else unresolved.append(f"camera '{name}'"))
    if ids:
        payload["filters"].append(
            {"field": "camera_id", "op": "in" if len(ids) > 1 else "eq",
             "value": ids if len(ids) > 1 else ids[0]})

    zone_names = [z for z in (raw.get("zones") or []) if isinstance(z, str)]
    zids = []
    for name in zone_names:
        zid = catalog.resolve_zone(name)
        (zids.append(zid) if zid else unresolved.append(f"zone '{name}'"))
    if zids and payload["entity"] == "zone_events":
        payload["filters"].append(
            {"field": "zone_id", "op": "in" if len(zids) > 1 else "eq",
             "value": zids if len(zids) > 1 else zids[0]})

    return payload, unresolved


# --- providers ----------------------------------------------------------------------------

class StubProvider:
    """A deterministic provider for tests and offline demos.

    Not a fake that always succeeds — it pattern-matches a small set of real question shapes and
    returns `unsupported` for anything else, so the refusal path is exercised without an API key.
    """

    #: Words that appear in camera names but carry no locating information on their own.
    #: Deliberately short: whole-word matching already fixes the bug this was written for
    #: ("blocked" no longer matches a camera named "... Admin Block", because "block" and
    #: "blocked" are different tokens). An over-long list is its own failure — an earlier
    #: version included "zone" and "shop", which stopped "Zone B" from matching at all.
    STOPWORDS = frozenset({"camera", "main", "area", "line", "the"})

    VEHICLE_WORDS = frozenset({"vehicle", "vehicles", "car", "cars", "truck", "trucks",
                               "van", "vans"})
    PASSAGE_WORDS = frozenset({"entered", "enter", "entering", "crossed", "passed", "through"})
    _T = r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?"

    def complete(self, system: str, user: str) -> dict[str, Any]:
        q = user.split("\n", 1)[0].removeprefix("Question:").strip().lower()
        tokens = set(_norm(q).split())
        has_zones = "\nZones:" in user

        window = "today"
        # Weekday names first: they are the most specific, and an earlier version omitted them
        # entirely so "on Tuesday" silently became "today" — a real observation reported against
        # the wrong day, which is the worst failure this system has.
        weekdays = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
        for name in (*weekdays, "yesterday", "this morning", "last night",
                     "this week", "last 7 days", "last 30 days"):
            if name in q:
                window = name.replace(" ", "_")
                break

        cameras = self._cameras(_prompt_names(user, "Cameras at this site:"), q, tokens)
        base: dict[str, Any] = {"window": window, "cameras": cameras}
        base.update(self._clock(q))

        cls = "vehicle" if tokens & self.VEHICLE_WORDS else "person"
        presence = [{"field": "class", "op": "eq", "value": cls}]

        if "helmet" in q or "ppe" in tokens:
            if not has_zones:
                return {**base, "entity": "tracks", "select": "distinct_count",
                        "filters": [*presence, {"field": "helmet", "op": "lt", "value": 0.5}]}
            return {**base, "entity": "zone_events", "select": "distinct_count",
                    "filters": [{"field": "helmet", "op": "lt", "value": 0.5}]}
        if "block" in q or "obstruct" in q:
            return {**base, "entity": "zone_events", "select": "rows", "limit": 20,
                    "filters": [{"field": "type", "op": "eq", "value": "stationary"}]}
        # Passing a point is a zone question even where no zone is drawn: the answer layer then
        # says the line was never drawn, which is true, instead of counting presence and
        # reporting it as a count of entries.
        passage = bool(tokens & self.PASSAGE_WORDS)
        if "how many" in q or "count" in q:
            if has_zones or passage:
                return {**base, "entity": "zone_events", "select": "distinct_count",
                        "filters": [{"field": "type", "op": "eq", "value": "cross_pos"}]}
            return {**base, "entity": "tracks", "select": "distinct_count", "filters": presence}
        if "who" in tokens or "anyone" in q or "show me" in q:
            if has_zones:
                return {**base, "entity": "zone_events", "select": "rows", "limit": 20,
                        "filters": [{"field": "type", "op": "in", "value": ["enter", "loiter"]}]}
            return {**base, "entity": "tracks", "select": "rows", "limit": 20, "filters": presence}
        return {"unsupported": "this question is outside what the stub provider handles"}

    def _cameras(self, names: list[str], q: str, tokens: set[str]) -> list[str]:
        """Whole-word matching, preferring words that identify one camera.

        "Admin G329" selects G329 only: "g329" names one camera, and "admin", which two cameras
        share, is already explained by it. "The admin cameras" selects both, because no
        identifying word was given. "Bus G340 or the admin cameras" selects all three, because
        "admin" is not explained by G340 — dropping it would quietly answer a narrower question.
        """
        words = {n: {w for w in _norm(n).split() if len(w) > 2} - self.STOPWORDS for n in names}
        counts: dict[str, int] = {}
        for ws in words.values():
            for w in ws:
                counts[w] = counts.get(w, 0) + 1
        selected = [n for n in names
                    if _norm(n) in _norm(q) or any(counts[w] == 1 for w in words[n] & tokens)]
        covered = set().union(*(words[n] for n in selected)) if selected else set()
        for w in sorted(tokens - covered):
            if counts.get(w, 0) > 1:
                selected += [n for n in names if w in words[n] and n not in selected]
        return selected

    def _clock(self, q: str) -> dict[str, str]:
        t = self._T
        for pattern, keys in ((rf"between {t} and {t}", ("from_time", "to_time")),
                              (rf"from {t} (?:to|until) {t}", ("from_time", "to_time")),
                              (rf"after {t}", ("from_time",)),
                              (rf"before {t}", ("to_time",))):
            m = re.search(pattern, q)
            if not m:
                continue
            groups = m.groups()
            out = {}
            for i, key in enumerate(keys):
                h, mi, ap = groups[3 * i: 3 * i + 3]
                if mi is None and ap is None:
                    return {}          # "after 5" is a count, not a time
                hour = int(h) % 12 + (12 if ap == "pm" else 0) if ap else int(h)
                out[key] = f"{hour:02d}:{int(mi or 0):02d}"
            return out
        return {}


def _prompt_names(user: str, header: str) -> list[str]:
    out, grab = [], False
    for line in user.splitlines():
        if line.strip() == header:
            grab = True
            continue
        if grab:
            if not line.startswith("  ") or not line.strip():
                break
            out.append(line.strip())
    return out


class GeminiProvider:
    """Gemini via the REST API, with response_mime_type forcing JSON.

    Chosen for the query layer because it is dramatically cheaper than self-hosting until
    roughly 2,500 cameras and has no idle cost. The published rate is promotional and steps up,
    so the provider seam exists to make swapping to a self-hosted model a config change — and an
    air-gapped deployment cannot call an API at all.
    """

    def __init__(self, api_key: str, model: str = "gemini-3-flash", timeout: float = 20.0):
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def complete(self, system: str, user: str) -> dict[str, Any]:
        import httpx

        url = (f"https://generativelanguage.googleapis.com/v1beta/models/"
               f"{self.model}:generateContent")
        body = {
            "systemInstruction": {"parts": [{"text": system}]},
            "contents": [{"role": "user", "parts": [{"text": user}]}],
            "generationConfig": {"response_mime_type": "application/json", "temperature": 0.0},
        }
        r = httpx.post(url, json=body, timeout=self.timeout,
                       headers={"x-goog-api-key": self.api_key})
        r.raise_for_status()
        data = r.json()
        try:
            text = data["candidates"][0]["content"]["parts"][0]["text"]
        except (KeyError, IndexError) as e:
            raise ModelError(f"unexpected response shape: {json.dumps(data)[:300]}") from e
        return _loads(text)


def _loads(text: str) -> dict[str, Any]:
    """Parse the model's JSON, tolerating a code fence."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t).strip()
    try:
        out = json.loads(t)
    except json.JSONDecodeError as e:
        raise ModelError(f"model did not return valid JSON: {t[:200]}") from e
    if not isinstance(out, dict):
        raise ModelError("model returned JSON that is not an object")
    return out

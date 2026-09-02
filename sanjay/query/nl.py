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
from datetime import datetime, timedelta
from typing import Any, Protocol

from sanjay.query.filters import Filter, FilterError, parse, schema_for_prompt

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
    """

    cameras: dict[str, str] = field(default_factory=dict)      # name -> camera_id
    zones: dict[str, str] = field(default_factory=dict)        # name -> zone_id
    tz_offset: timedelta = timedelta(hours=5, minutes=30)
    measured_attrs: set[str] = field(default_factory=set)

    @classmethod
    def load(cls, conn, site_id: str) -> Catalog:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT camera_id, name FROM cameras "
                "WHERE site_id = %s AND enabled AND NOT excluded", (site_id,))
            cameras = {n: str(c) for c, n in cur.fetchall()}
            cur.execute("SELECT zone_id, name FROM zones WHERE site_id = %s", (site_id,))
            zones = {n: str(z) for z, n in cur.fetchall()}
        return cls(cameras=cameras, zones=zones)

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

def named_windows(now: datetime, tz_offset: timedelta) -> dict[str, tuple[str, str]]:
    """Precompute the windows people actually ask about, as ISO strings with the site's offset.

    The model picks a name; it never does the arithmetic. `now` is passed in rather than read
    from the clock so this is testable and so a demo can be replayed.
    """
    local = now + tz_offset
    midnight = local.replace(hour=0, minute=0, second=0, microsecond=0)
    tzs = _offset_str(tz_offset)

    def w(a: datetime, b: datetime) -> tuple[str, str]:
        return (a.strftime("%Y-%m-%dT%H:%M:%S") + tzs, b.strftime("%Y-%m-%dT%H:%M:%S") + tzs)

    out = {
        "today": w(midnight, midnight + timedelta(days=1)),
        "yesterday": w(midnight - timedelta(days=1), midnight),
        "this_week": w(midnight - timedelta(days=local.weekday()), midnight + timedelta(days=1)),
        "last_7_days": w(midnight - timedelta(days=7), midnight + timedelta(days=1)),
        "last_30_days": w(midnight - timedelta(days=30), midnight + timedelta(days=1)),
        "this_morning": w(midnight + timedelta(hours=6), midnight + timedelta(hours=12)),
        "last_night": w(midnight - timedelta(hours=4), midnight + timedelta(hours=6)),
    }
    for i in range(1, 8):
        d = midnight - timedelta(days=i)
        out[d.strftime("%A").lower()] = w(d, d + timedelta(days=1))
    return out


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

Rules that matter:
  - Attributes are CONFIDENCES from 0 to 1, never true/false. "without a helmet" is
    {"field":"helmet","op":"lt","value":0.5}. "wearing a helmet" is op "gt".
  - If the question does not name a place, leave cameras empty rather than guessing one.
  - If the question cannot be expressed with the listed fields, emit
    {"unsupported": "<short reason>"} instead of forcing it. That is a correct answer.

Return the JSON object and nothing else."""


def build_user_prompt(question: str, catalog: Catalog, now: datetime) -> str:
    windows = named_windows(now, catalog.tz_offset)
    lines = [
        f"Question: {question}",
        "",
        f"Site local time now: {(now + catalog.tz_offset):%Y-%m-%d %H:%M} "
        f"({(now + catalog.tz_offset):%A}).",
        "",
        "Time windows (use the NAME in the `window` field):",
    ]
    lines += [f"  {name}" for name in windows]
    lines += ["", "Cameras at this site:"]
    lines += [f"  {n}" for n in sorted(catalog.cameras)]
    if catalog.zones:
        lines += ["", "Zones:"]
        lines += [f"  {n}" for n in sorted(catalog.zones)]
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

    payload, unresolved = _to_filter_payload(raw, catalog, now)
    try:
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
        payload2, unresolved2 = _to_filter_payload(raw2, catalog, now)
        try:
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
    windows = named_windows(now, catalog.tz_offset)
    win = windows.get(str(raw.get("window", "")).strip().lower())
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

    def complete(self, system: str, user: str) -> dict[str, Any]:
        q = user.split("\n", 1)[0].removeprefix("Question:").strip().lower()
        tokens = set(_norm(q).split())

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

        # Whole-word matching only. Substring matching is what let "blocked" select a camera
        # whose name happens to contain "Block".
        cameras = []
        for n in _prompt_names(user, "Cameras at this site:"):
            words = {w for w in _norm(n).split() if len(w) > 2} - self.STOPWORDS
            if _norm(n) in _norm(q) or (words and words & tokens):
                cameras.append(n)

        if "helmet" in q or "ppe" in q:
            return {"entity": "zone_events", "select": "distinct_count", "window": window,
                    "cameras": cameras,
                    "filters": [{"field": "helmet", "op": "lt", "value": 0.5}]}
        if "block" in q or "obstruct" in q:
            return {"entity": "zone_events", "select": "rows", "window": window,
                    "cameras": cameras, "limit": 20,
                    "filters": [{"field": "type", "op": "eq", "value": "stationary"}]}
        if "how many" in q or "count" in q:
            return {"entity": "zone_events", "select": "distinct_count", "window": window,
                    "cameras": cameras,
                    "filters": [{"field": "type", "op": "eq", "value": "cross_pos"}]}
        if "who" in q or "anyone" in q or "show me" in q:
            return {"entity": "zone_events", "select": "rows", "window": window,
                    "cameras": cameras, "limit": 20,
                    "filters": [{"field": "type", "op": "in", "value": ["enter", "loiter"]}]}
        return {"unsupported": "this question is outside what the stub provider handles"}


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

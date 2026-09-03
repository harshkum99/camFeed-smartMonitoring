"""The Smart Cam Monitoring console API.

Thin by design. Every endpoint here is a translation layer over logic that is already tested
somewhere else — the filter compiler, the answer layer, the rules engine. Nothing decides
anything on its own, because an HTTP handler is the worst place to put a decision you later need
to run identically on an edge box with no network.

Two things this layer is genuinely responsible for:

  * scope — tenant, site and the permitted camera list come from the session, never the request;
  * honesty — a refusal is a 200 with a reason, not a 404. "No results" and "the camera was
    offline" are different answers to the operator, and an HTTP status code cannot carry that
    difference.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import psycopg
from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from psycopg.types.json import Jsonb
from pydantic import BaseModel, Field

from smartcam.api.session import Session, current_session
from smartcam.query.ask import ask
from smartcam.query.nl import Catalog, GeminiProvider, ModelError, StubProvider
from smartcam.rules.nl import StubRuleProvider, compile_rule, explain
from smartcam.rules.schema import RuleError, parse_rule, to_json

DSN = os.environ.get("SMARTCAM_DSN", "dbname=smartcam_dev")
#: The built console. Vite compiles to plain static files, so the deployed artefact still needs
#: no toolchain and no network — which is what the air-gapped edge SKU requires.
WEB_DIR = Path(__file__).resolve().parents[2] / "console" / "dist"


def _provider():
    """Gemini when a key is present, the offline stub otherwise.

    The stub is not a placeholder to be embarrassed about — it is what lets the console be
    demonstrated on a laptop with no network, and it declines questions it cannot handle rather
    than inventing answers.
    """
    key = os.environ.get("SMARTCAM_GEMINI_KEY")
    return GeminiProvider(key) if key else StubProvider()


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.pool = None
    yield


app = FastAPI(title="Smart Cam Monitoring", version="0.1.0", lifespan=lifespan)

# Cross-origin access, for when the console is hosted separately from the API (a CDN deploy, a
# Vercel preview) while the backend stays on a server with the database.
#
# Deliberately an explicit allow-list rather than "*": this API answers questions about a
# customer's premises, and a wildcard would let any page a logged-in operator visits query it
# from their browser. Set SMARTCAM_ALLOWED_ORIGINS to a comma-separated list of exact origins.
_origins = [
    o.strip() for o in os.environ.get("SMARTCAM_ALLOWED_ORIGINS", "").split(",") if o.strip()
]
if _origins:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_origins,
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "DELETE"],
        allow_headers=["content-type", "x-smartcam-actor", "x-smartcam-tenant",
                       "x-smartcam-site", "x-smartcam-roles"],
    )


def db():
    """One short-lived connection per request. A pool belongs here eventually; at PoC scale it
    would be optimising something that is not slow."""
    conn = psycopg.connect(DSN)
    try:
        yield conn
    finally:
        conn.close()


# --- models --------------------------------------------------------------------------------

class AskBody(BaseModel):
    question: str = Field(min_length=1, max_length=500)


class RuleDraftBody(BaseModel):
    instruction: str = Field(min_length=1, max_length=400)


class ZoneBody(BaseModel):
    name: str
    kind: str = "area"
    #: Normalised 0-1 points, so a zone survives a resolution change on the camera.
    polygon: list[list[float]]
    direction: str | None = None


class FeedbackBody(BaseModel):
    actioned: bool
    note: str | None = None


# --- health & catalogue ----------------------------------------------------------------------

@app.get("/api/health")
def health(conn=Depends(db)) -> dict[str, Any]:
    with conn.cursor() as cur:
        cur.execute("SELECT 1")
        cur.fetchone()
    return {"ok": True, "interpreter": type(_provider()).__name__}


@app.get("/api/cameras")
def cameras(s: Session = Depends(current_session), conn=Depends(db)) -> list[dict[str, Any]]:
    """The camera list, with each one's grade — what it can actually support.

    The grade is surfaced in the UI rather than hidden in a report, because it is the honest
    answer to "why can't I do face matching on that camera" and the operator will ask.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT camera_id, name, grade::text, lens::text, detect_w, detect_h, detect_fps, "
            "       gop_ms, clock_offset_ms "
            "FROM cameras WHERE site_id = %s AND enabled AND NOT excluded ORDER BY name",
            (s.site_id,),
        )
        rows = cur.fetchall()
    return [
        {"camera_id": str(c), "name": n, "grade": g, "lens": lens,
         "resolution": f"{w}x{h}" if w else None, "fps": fps, "gop_ms": gop,
         "clock_offset_ms": drift,
         "clock_ok": drift is None or abs(drift) < 30_000}
        for c, n, g, lens, w, h, fps, gop, drift in rows
    ]


@app.get("/api/coverage")
def coverage(hours: int = 24, s: Session = Depends(current_session),
             conn=Depends(db)) -> dict[str, Any]:
    """Recent camera coverage. Shown on the console's front page on purpose: an operator who
    cannot see that a camera has been dark since Tuesday will read every empty answer about it
    as good news."""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT camera_id FROM cameras WHERE site_id = %s AND enabled AND NOT excluded",
            (s.site_id,))
        cams = [str(r[0]) for r in cur.fetchall()]
        if not cams:
            return {"coverage_pct": 0.0, "gaps": [], "cameras": 0}
        cur.execute(
            "SELECT coverage_pct(%s::uuid[], now() - make_interval(hours => %s), now())",
            (cams, hours))
        pct = float(cur.fetchone()[0] or 0.0)
        cur.execute(
            "SELECT camera_name, gap_start, gap_end FROM coverage_gaps("
            "%s::uuid[], now() - make_interval(hours => %s), now())", (cams, hours))
        gaps = [{"camera": n, "from": a.isoformat(), "to": b.isoformat(),
                 "minutes": int((b - a).total_seconds() // 60)} for n, a, b in cur.fetchall()]
    return {"coverage_pct": pct, "gaps": gaps, "cameras": len(cams), "hours": hours}


@app.get("/api/activity")
def activity(hours: int = 48, s: Session = Depends(current_session),
             conn=Depends(db)) -> list[dict[str, Any]]:
    """Hourly event volume across the site.

    Worth charting because it has real shape — shift changes, breaks, and the dead hours are
    all visible, so an operator spots an abnormal night at a glance. A flat or missing band is
    itself the finding: it usually means a camera stopped, not that nothing happened.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT date_trunc('hour', ts) AS h, count(*) "
            "FROM zone_events WHERE site_id = %s "
            "  AND ts >= (SELECT max(ts) FROM zone_events WHERE site_id = %s) "
            "          - make_interval(hours => %s) "
            "GROUP BY h ORDER BY h",
            (s.site_id, s.site_id, min(max(hours, 1), 24 * 14)),
        )
        return [{"hour": h.isoformat(), "events": n} for h, n in cur.fetchall()]


# --- ask ------------------------------------------------------------------------------------

@app.post("/api/ask")
def ask_question(body: AskBody, s: Session = Depends(current_session),
                 conn=Depends(db)) -> dict[str, Any]:
    """Answer a question, or explain honestly why we cannot.

    Always 200. A refusal is an answer — encoding it as a 404 would make "nothing happened" and
    "we could not look" indistinguishable to the client, which is the exact failure this whole
    layer exists to avoid.
    """
    catalog = Catalog.load(conn, s.site_id)
    try:
        r = ask(conn, body.question, tenant_id=s.tenant_id, site_id=s.site_id, actor=s.actor,
                provider=_provider(), catalog=catalog, camera_ids=s.camera_ids)
    except ModelError as e:
        raise HTTPException(503, f"the interpreter is unavailable: {e}") from e

    a = r.answer
    return {
        "question": r.question,
        "message": r.message,
        "refused": r.refused is not None,
        "abstain_reason": a.abstain_reason if a else None,
        "describes": a.describes if a else None,
        "confident": a.confident if a else 0,
        "ambiguous": a.ambiguous if a else 0,
        "total": a.total if a else 0,
        "coverage_pct": a.coverage_pct if a else 0.0,
        "gaps": [{"camera": g.camera_name, "minutes": g.minutes} for g in (a.gaps if a else [])],
        "unresolved": r.compiled.unresolved if r.compiled else [],
        "evidence": [
            {"track_id": e.track_id, "camera": e.camera_name, "ts": e.ts.isoformat(),
             "confidence": e.confidence, "keyframe_uri": e.keyframe_uri, "attrs": e.attrs}
            for e in (a.evidence if a else [])
        ],
        "latency_ms": r.latency_ms,
    }


# --- zones ------------------------------------------------------------------------------------

@app.get("/api/cameras/{camera_id}/zones")
def get_zones(camera_id: str, s: Session = Depends(current_session),
              conn=Depends(db)) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT zone_id, name, kind, polygon, direction FROM zones "
            "WHERE camera_id = %s AND site_id = %s ORDER BY name", (camera_id, s.site_id))
        return [{"zone_id": str(z), "name": n, "kind": k, "polygon": p, "direction": d}
                for z, n, k, p, d in cur.fetchall()]


@app.put("/api/cameras/{camera_id}/zones")
def put_zones(camera_id: str, zones: list[ZoneBody],
              s: Session = Depends(current_session), conn=Depends(db)) -> dict[str, Any]:
    """Replace a camera's zones. Coordinates are normalised 0-1 and validated here, because a
    zone stored in pixels silently moves the moment a camera's resolution changes."""
    s.require("admin")
    for z in zones:
        if len(z.polygon) < 2:
            raise HTTPException(400, f"zone '{z.name}' needs at least two points")
        if z.kind == "area" and len(z.polygon) < 3:
            raise HTTPException(400, f"area '{z.name}' needs at least three points")
        for pt in z.polygon:
            if len(pt) != 2 or not all(0.0 <= v <= 1.0 for v in pt):
                raise HTTPException(
                    400, f"zone '{z.name}' has a point outside the frame. Coordinates are "
                         f"normalised 0-1 so zones survive a resolution change.")

    with conn.cursor() as cur:
        cur.execute("SELECT 1 FROM cameras WHERE camera_id = %s AND site_id = %s",
                    (camera_id, s.site_id))
        if cur.fetchone() is None:
            raise HTTPException(404, "no such camera at this site")
        cur.execute("DELETE FROM zones WHERE camera_id = %s AND site_id = %s",
                    (camera_id, s.site_id))
        for z in zones:
            cur.execute(
                "INSERT INTO zones (camera_id, site_id, name, kind, polygon, direction) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (camera_id, s.site_id, z.name, z.kind, Jsonb(z.polygon), z.direction))
    conn.commit()
    return {"saved": len(zones)}


# --- rules ---------------------------------------------------------------------------------

@app.post("/api/rules/draft")
def draft_rule(body: RuleDraftBody, s: Session = Depends(current_session),
               conn=Depends(db)) -> dict[str, Any]:
    """Compile an instruction into a rule and hand back the English read-back — WITHOUT saving.

    The two-step is the point. An operator confirms the sentence and the drawn zone before
    anything goes live, which is what catches a plausible-looking rule that watches the wrong
    place for a month.
    """
    catalog = Catalog.load(conn, s.site_id)
    try:
        c = compile_rule(body.instruction, catalog, StubRuleProvider(), site_id=s.site_id)
    except ModelError as e:
        raise HTTPException(503, f"the interpreter is unavailable: {e}") from e

    if c.rule is None:
        return {"ok": False, "reason": c.unsupported, "unresolved": c.unresolved}

    names = {v: k for k, v in catalog.cameras.items()}
    return {
        "ok": True,
        "explain": explain(c.rule, names),
        "rule": to_json(c.rule),
        "notes": c.rule.notes,
        "unresolved": c.unresolved,
        "min_alert_latency_s": c.rule.min_alert_latency_s,
        "needs_verification": c.rule.verification.mode != "none",
    }


@app.post("/api/rules")
def save_rule(payload: dict[str, Any], s: Session = Depends(current_session),
              conn=Depends(db)) -> dict[str, Any]:
    s.require("admin")
    try:
        rule = parse_rule(payload, site_id=s.site_id)
    except RuleError as e:
        raise HTTPException(400, str(e)) from e

    with conn.cursor() as cur:
        cur.execute(
            "INSERT INTO rules (tenant_id, site_id, name, severity, doc, enabled) "
            "VALUES (%s,%s,%s,%s,%s,%s) RETURNING rule_id",
            (s.tenant_id, s.site_id, rule.name, rule.severity, Jsonb(to_json(rule)),
             rule.enabled))
        rule_id = str(cur.fetchone()[0])
    conn.commit()
    return {"rule_id": rule_id, "name": rule.name, "notes": rule.notes}


@app.get("/api/rules")
def list_rules(s: Session = Depends(current_session), conn=Depends(db)) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT rule_id, name, severity, enabled, doc, true_positives, false_positives "
            "FROM rules WHERE site_id = %s ORDER BY created_at DESC", (s.site_id,))
        rows = cur.fetchall()
    out = []
    for rid, name, sev, enabled, doc, tp, fp in rows:
        total = tp + fp
        out.append({
            "rule_id": str(rid), "name": name, "severity": sev, "enabled": enabled,
            "trigger": (doc or {}).get("trigger", {}),
            "true_positives": tp, "false_positives": fp,
            # Shown in the list because a rule whose precision is falling is the one about to
            # get the whole product muted.
            "precision": round(tp / total, 2) if total else None,
        })
    return out


@app.delete("/api/rules/{rule_id}")
def disable_rule(rule_id: str, s: Session = Depends(current_session),
                 conn=Depends(db)) -> dict[str, Any]:
    """Disable rather than delete. A rule that fired is part of the record of why an alert
    happened, and deleting it orphans every alert it raised."""
    s.require("admin")
    with conn.cursor() as cur:
        cur.execute("UPDATE rules SET enabled = false WHERE rule_id = %s AND site_id = %s",
                    (rule_id, s.site_id))
        if cur.rowcount == 0:
            raise HTTPException(404, "no such rule at this site")
    conn.commit()
    return {"disabled": rule_id}


# --- alerts -----------------------------------------------------------------------------------

@app.get("/api/alerts")
def list_alerts(limit: int = 50, s: Session = Depends(current_session),
                conn=Depends(db)) -> list[dict[str, Any]]:
    with conn.cursor() as cur:
        cur.execute(
            "SELECT a.alert_id, a.ts, a.severity, a.state::text, a.camera_id, c.name, "
            "       r.name, a.verifier_verdict, a.evidence_uris, a.operator_feedback "
            "FROM alerts a "
            "LEFT JOIN cameras c ON c.camera_id = a.camera_id "
            "LEFT JOIN rules r ON r.rule_id = a.rule_id "
            "WHERE a.site_id = %s ORDER BY a.ts DESC LIMIT %s",
            (s.site_id, min(limit, 200)))
        rows = cur.fetchall()
    return [
        {"alert_id": str(aid), "ts": ts.isoformat(), "severity": sev, "state": state,
         "camera": cam_name or str(cam), "rule": rule_name,
         "verified": verdict, "evidence": list(ev or []), "feedback": fb}
        for aid, ts, sev, state, cam, cam_name, rule_name, verdict, ev, fb in rows
    ]


@app.post("/api/alerts/{alert_id}/feedback")
def alert_feedback(alert_id: str, body: FeedbackBody,
                   s: Session = Depends(current_session), conn=Depends(db)) -> dict[str, Any]:
    """The one-tap 'not an incident' button.

    This is the most valuable button in the product. It is the only per-site labelled data we
    will ever get for free, and it is what drives a rule to mute itself before an operator mutes
    the whole application.
    """
    verdict = "true_positive" if body.actioned else "false_positive"
    with conn.cursor() as cur:
        cur.execute(
            "UPDATE alerts SET operator_feedback = %s, feedback_by = %s, feedback_at = now(), "
            "state = %s WHERE alert_id = %s AND site_id = %s RETURNING rule_id",
            (verdict, s.actor, "actioned" if body.actioned else "dismissed",
             alert_id, s.site_id))
        row = cur.fetchone()
        if row is None:
            raise HTTPException(404, "no such alert at this site")
        rule_id = row[0]
        col = "true_positives" if body.actioned else "false_positives"
        cur.execute(
            f"UPDATE rules SET {col} = {col} + 1 WHERE rule_id = %s "  # noqa: S608 - fixed name
            "RETURNING true_positives, false_positives, "
            "(doc->'suppression'->>'quiet_after_n_unactioned')::int",
            (rule_id,))
        tp, fp, quiet_after = cur.fetchone() or (0, 0, 10)
    conn.commit()

    muted = not body.actioned and fp >= (quiet_after or 10)
    return {
        "recorded": verdict, "true_positives": tp, "false_positives": fp,
        "rule_muted": muted,
        "message": ("This rule has been muted and flagged for retuning."
                    if muted else "Thanks — this tunes the rule.")
    }


# --- audit --------------------------------------------------------------------------------

@app.get("/api/audit")
def audit(limit: int = 50, s: Session = Depends(current_session),
          conn=Depends(db)) -> list[dict[str, Any]]:
    """Every question asked of this site, including the refused ones.

    Surfaced in the console rather than buried, because a query box over every camera in a
    building is a stalking tool without it, and because a customer's security review will ask.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT created_at, actor, question, abstained, abstain_reason, coverage_pct, "
            "       rows_returned, latency_ms "
            "FROM answers WHERE site_id = %s ORDER BY created_at DESC LIMIT %s",
            (s.site_id, min(limit, 200)))
        rows = cur.fetchall()
    return [
        {"at": at.isoformat(), "actor": actor, "question": q, "refused": ab,
         "reason": reason, "coverage_pct": cov, "rows": n, "latency_ms": ms}
        for at, actor, q, ab, reason, cov, n, ms in rows
    ]


# --- static console ---------------------------------------------------------------------------
#
# Mounted last so /api/* always wins. The console is a client-side-routed single page, so any
# path that is not an API route and not a real file falls back to index.html — otherwise a
# refresh on /alerts would 404, which is the classic way a router-based console breaks only
# after someone bookmarks a page.

if WEB_DIR.is_dir():
    app.mount("/assets", StaticFiles(directory=WEB_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> FileResponse:
        if full_path.startswith("api/"):
            raise HTTPException(404, "no such endpoint")
        candidate = (WEB_DIR / full_path).resolve()
        # resolve() then is_relative_to() blocks path traversal via ../ in the URL.
        if full_path and candidate.is_file() and candidate.is_relative_to(WEB_DIR.resolve()):
            return FileResponse(candidate)
        return FileResponse(WEB_DIR / "index.html")

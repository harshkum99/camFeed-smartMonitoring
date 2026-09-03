"""The one entry point: a plain-language question in, a grounded answer or an honest refusal out.

Everything the operator sees comes through here. It joins the two halves — the model that
interprets the question, and the database that answers it — and keeps the seam visible, because
the two fail in completely different ways and a customer needs to be told which happened.

A question we could not *interpret* is not the same as a question with no *answer*, and both are
different again from a question we could not answer because a camera was down. Collapsing them
into "no results" is how a surveillance system quietly lies.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import UTC, datetime

from smartcam.query.answer import Answer, answer, log_uninterpreted
from smartcam.query.nl import Catalog, Compiled, ModelError, Provider, compile_question


@dataclass
class AskResult:
    question: str
    answer: Answer | None
    compiled: Compiled | None
    #: Set when the question never reached the database at all.
    refused: str | None = None
    latency_ms: int = 0

    @property
    def message(self) -> str:
        if self.refused:
            return self.refused
        return self.answer.message if self.answer else ""


def ask(
    conn,
    question: str,
    *,
    tenant_id: str,
    site_id: str,
    actor: str,
    provider: Provider,
    catalog: Catalog | None = None,
    camera_ids: list[str] | None = None,
    now: datetime | None = None,
) -> AskResult:
    """Interpret, execute, and answer — or explain why we cannot.

    `camera_ids` is the session's permission scope. It is passed to the answer layer, which can
    narrow further from the question but never widen past it, so a question naming a camera the
    operator may not see cannot reach it.
    """
    t0 = time.monotonic()
    now = now or datetime.now(UTC)
    catalog = catalog if catalog is not None else Catalog.load(conn, site_id)

    try:
        compiled = compile_question(question, catalog, provider, now=now)
    except ModelError as e:
        # The interpreter is down. Say so plainly rather than reporting an empty result, which
        # would read as "nothing happened" — the most dangerous thing this product can say.
        _audit(conn, question, tenant_id, site_id, actor, f"interpreter unavailable: {e}", t0)
        return AskResult(question, None, None,
                         refused=f"I could not interpret that question just now ({e}). "
                                 f"The camera record is unaffected — please try again.",
                         latency_ms=_ms(t0))

    if compiled.filter is None:
        # Logged even though it never reached the database: the audit trail records that someone
        # asked, which is the point of having one.
        _audit(conn, question, tenant_id, site_id, actor,
               compiled.unsupported or "could not interpret", t0)
        return AskResult(question, None, compiled,
                         refused=_cannot_interpret(compiled.unsupported),
                         latency_ms=_ms(t0))

    a = answer(conn, compiled.filter, question=question, tenant_id=tenant_id,
               site_id=site_id, actor=actor, camera_ids=camera_ids)

    # The restatement is the cheapest correctness control in the whole query path — a person
    # reading it spots a misread question instantly. That only works if they can read it, so
    # identifiers are put back into the words the operator used.
    a.describes = _humanise(a.describes, catalog)

    # A name we could not resolve changes what the question means, so it is prepended to the
    # answer rather than logged and forgotten. The operator asked about a specific place.
    if compiled.unresolved:
        names = ", ".join(compiled.unresolved)
        a.message = (f"I could not identify {names} at this site, so this covers "
                     f"everything else you can see. {a.message}")

    return AskResult(question, a, compiled, latency_ms=_ms(t0))


def _audit(conn, question, tenant_id, site_id, actor, reason, t0) -> None:
    """Best-effort audit write. A failure here must not swallow the operator's answer, but it is
    surfaced rather than silently dropped — an audit trail with silent holes is worse than none."""
    try:
        log_uninterpreted(conn, question=question, tenant_id=tenant_id, site_id=site_id,
                          actor=actor, reason=reason, latency_ms=_ms(t0))
    except Exception:  # noqa: BLE001
        conn.rollback()
        logging.getLogger(__name__).exception(
            "failed to audit an uninterpreted question from %s", actor)


def _humanise(text: str, catalog: Catalog) -> str:
    """Swap camera and zone ids in the restatement back for their names."""
    for name, ident in (*catalog.cameras.items(), *catalog.zones.items()):
        text = text.replace(ident, name)
    return text


def _cannot_interpret(reason: str | None) -> str:
    return (
        f"I couldn't turn that into a search of the camera record"
        f"{f' — {reason}' if reason else ''}. "
        f"This means I did not understand the question, not that nothing happened. "
        f"Try naming a camera, a time period, and what you are looking for."
    )


def _ms(t0: float) -> int:
    return int((time.monotonic() - t0) * 1000)

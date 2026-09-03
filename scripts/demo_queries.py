#!/usr/bin/env python3
"""Run the demo questions against the seeded factory and print what a customer would see.

This is the rehearsal for the meeting. It deliberately includes questions the system should
refuse, because that is the part buyers in regulated industries actually test.

    python scripts/demo_queries.py --dsn dbname=smartcam_dev
"""

from __future__ import annotations

import argparse

import psycopg

from smartcam.query.answer import answer
from smartcam.query.filters import parse

TENANT = "11111111-1111-1111-1111-111111111111"
SITE = "aaaaaaaa-0000-0000-0000-000000000001"

# Camera ids, in the order scripts/seed_demo.py declares them. A question about "the gate" that
# carries no camera predicate silently counts the whole site — which is exactly the mistake the
# "Understood as" line printed below is there to make visible.
CAM = {
    "gate3":    "cccccccc-0000-0000-0000-000000000001",
    "zoneb":    "cccccccc-0000-0000-0000-000000000002",
    "dock":     "cccccccc-0000-0000-0000-000000000003",
    "fireexit": "cccccccc-0000-0000-0000-000000000004",
    "restrict": "cccccccc-0000-0000-0000-000000000005",
    "yard":     "cccccccc-0000-0000-0000-000000000006",
    "corridor": "cccccccc-0000-0000-0000-000000000007",
    "canteen":  "cccccccc-0000-0000-0000-000000000008",
}

# Site-local times, carried as +05:30 offsets. Day 1 = 2026-09-01.
QUESTIONS: list[tuple[str, dict]] = [
    (
        "How many workers entered Zone B without a helmet on 2 September?",
        {"entity": "zone_events", "select": "distinct_count",
         "start": "2026-09-02T00:00:00+05:30", "end": "2026-09-03T00:00:00+05:30",
         "filters": [{"field": "type", "op": "eq", "value": "cross_pos"},
                     {"field": "helmet", "op": "lt", "value": 0.5}]},
    ),
    (
        "Break that down by camera.",
        {"entity": "zone_events", "select": "count", "group_by": ["camera_id"],
         "start": "2026-09-02T00:00:00+05:30", "end": "2026-09-03T00:00:00+05:30",
         "filters": [{"field": "helmet", "op": "lt", "value": 0.5}]},
    ),
    (
        "Was anything left blocking the fire exit on 2 September?",
        {"entity": "zone_events", "select": "rows",
         "start": "2026-09-02T00:00:00+05:30", "end": "2026-09-03T00:00:00+05:30",
         "filters": [{"field": "type", "op": "eq", "value": "stationary"}], "limit": 10},
    ),
    (
        "Same question for 1 September.",
        {"entity": "zone_events", "select": "rows",
         "start": "2026-09-01T00:00:00+05:30", "end": "2026-09-02T00:00:00+05:30",
         "filters": [{"field": "type", "op": "eq", "value": "stationary"}], "limit": 10},
    ),
    (
        "Did anyone enter the chemical store after 8pm on 3 September?",
        {"entity": "zone_events", "select": "rows",
         "start": "2026-09-03T20:00:00+05:30", "end": "2026-09-03T23:59:00+05:30",
         "filters": [{"field": "camera_id", "op": "eq", "value": CAM["restrict"]},
                     {"field": "type", "op": "in", "value": ["enter", "loiter"]}], "limit": 10},
    ),
    (
        "How many people came through Gate 3 on the morning of 1 September?",
        {"entity": "zone_events", "select": "distinct_count",
         "start": "2026-09-01T07:00:00+05:30", "end": "2026-09-01T12:00:00+05:30",
         "filters": [{"field": "camera_id", "op": "eq", "value": CAM["gate3"]},
                     {"field": "type", "op": "eq", "value": "cross_pos"}]},
    ),
    (
        "Was anyone at the canteen door on the evening of 1 September?",
        {"entity": "zone_events", "select": "count",
         "start": "2026-09-01T14:00:00+05:30", "end": "2026-09-01T22:00:00+05:30",
         "filters": [{"field": "camera_id", "op": "eq", "value": CAM["canteen"]},
                     {"field": "type", "op": "eq", "value": "cross_pos"}]},
    ),
    (
        "Were any workers not wearing a hi-vis vest at the main gate on 1 September?",
        {"entity": "tracks", "select": "count",
         "start": "2026-09-01T00:00:00+05:30", "end": "2026-09-02T00:00:00+05:30",
         "filters": [{"field": "class", "op": "eq", "value": "vehicle"},
                     {"field": "vest", "op": "lt", "value": 0.5}]},
    ),
]

REFUSAL_LABEL = {
    "no_coverage": "NO COVERAGE",
    "no_evidence": "NO EVIDENCE",
    "not_measured": "NOT MEASURED",
    "low_confidence": "LOW CONFIDENCE",
}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default="dbname=smartcam_dev")
    args = ap.parse_args()

    with psycopg.connect(args.dsn) as conn:
        for i, (question, payload) in enumerate(QUESTIONS, 1):
            f = parse(payload)
            a = answer(conn, f, question=question, tenant_id=TENANT, site_id=SITE,
                       actor="demo@karma.ai")

            print(f"\n{'=' * 78}")
            print(f"Q{i}. {question}")
            print("-" * 78)
            tag = f"  [{REFUSAL_LABEL[a.abstain_reason]}]" if a.abstain_reason else ""
            print(f"  {a.message}{tag}")

            if a.groups:
                names = _camera_names(conn, SITE)
                for g in a.groups[:6]:
                    label = names.get(str(g.get("camera_id")), str(g.get("camera_id")))
                    print(f"     {label:<34} {g['confident']:>3} confident, "
                          f"{g['ambiguous']:>3} ambiguous")

            if a.evidence:
                print(f"\n  Evidence ({len(a.evidence)} frame(s), best first):")
                for e in a.evidence[:4]:
                    extra = ""
                    if "helmet" in e.attrs:
                        extra = f"  helmet={e.attrs['helmet']}"
                    print(f"     {e.ts:%d %b %H:%M:%S}  {e.camera_name:<30} "
                          f"conf {e.confidence:.2f}{extra}")

            print(f"\n  Understood as: {a.describes}")
            print(f"  {a.latency_ms} ms")

        print(f"\n{'=' * 78}")
        with conn.cursor() as cur:
            cur.execute("SELECT count(*), count(*) FILTER (WHERE abstained) FROM answers")
            total, refused = cur.fetchone()
        print(f"{total} question(s) logged to the audit trail, {refused} refused.")
    return 0


def _camera_names(conn, site_id: str) -> dict[str, str]:
    with conn.cursor() as cur:
        cur.execute("SELECT camera_id, name FROM cameras WHERE site_id = %s", (site_id,))
        return {str(c): n for c, n in cur.fetchall()}


if __name__ == "__main__":
    raise SystemExit(main())

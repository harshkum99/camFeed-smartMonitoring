#!/usr/bin/env python3
"""The demo, driven entirely by plain English.

Runs with the offline stub by default, so it works with no API key. Pass --gemini to use the
real model:

    python scripts/demo_ask.py --dsn dbname=smartcam_dev
    SMARTCAM_GEMINI_KEY=... python scripts/demo_ask.py --gemini
"""

from __future__ import annotations

import argparse
import os
from datetime import UTC, datetime

import psycopg

from smartcam.query.ask import ask
from smartcam.query.nl import Catalog, GeminiProvider, StubProvider

TENANT = "11111111-1111-1111-1111-111111111111"
SITE = "aaaaaaaa-0000-0000-0000-000000000001"

# "Now" is pinned so the demo is reproducible and the relative words in the questions below
# resolve against the seeded data rather than against the wall clock.
NOW = datetime(2026, 9, 3, 6, 30, tzinfo=UTC)      # 12:00 IST, Thursday 3 September

QUESTIONS = [
    "How many workers entered Zone B without a helmet yesterday?",
    "Was the fire exit blocked at any point yesterday?",
    "Was the fire exit blocked on Tuesday?",
    "How many people came through Gate 3 this morning?",
    "Show me anyone at the canteen door on Tuesday.",
    "What is our quarterly revenue?",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default="dbname=smartcam_dev")
    ap.add_argument("--gemini", action="store_true", help="use the real model")
    args = ap.parse_args()

    if args.gemini:
        key = os.environ.get("SMARTCAM_GEMINI_KEY")
        if not key:
            raise SystemExit("set SMARTCAM_GEMINI_KEY to use --gemini")
        provider = GeminiProvider(key)
        label = "Gemini"
    else:
        provider = StubProvider()
        label = "offline stub"

    with psycopg.connect(args.dsn) as conn:
        catalog = Catalog.load(conn, SITE)
        print(f"Site: {len(catalog.cameras)} cameras · interpreter: {label} · "
              f"site time {(NOW + catalog.tz_offset):%a %d %b %H:%M}\n")

        for i, question in enumerate(QUESTIONS, 1):
            r = ask(conn, question, tenant_id=TENANT, site_id=SITE, actor="demo@karma.ai",
                    provider=provider, catalog=catalog, now=NOW)

            print("=" * 78)
            print(f"Q{i}. {question}")
            print("-" * 78)

            tag = ""
            if r.answer and r.answer.abstain_reason:
                tag = f"   [{r.answer.abstain_reason.upper().replace('_', ' ')}]"
            elif r.refused:
                tag = "   [CANNOT INTERPRET]"
            print(f"  {r.message}{tag}")

            if r.answer and r.answer.evidence:
                print(f"\n  Evidence ({len(r.answer.evidence)} frames):")
                for e in r.answer.evidence[:3]:
                    extra = f"  helmet={e.attrs['helmet']}" if "helmet" in e.attrs else ""
                    print(f"     {e.ts:%a %d %b %H:%M:%S}  {e.camera_name:<28} "
                          f"conf {e.confidence:.2f}{extra}")

            if r.answer:
                print(f"\n  Understood as: {r.answer.describes}")
            print(f"  {r.latency_ms} ms total\n")

        with conn.cursor() as cur:
            cur.execute("SELECT count(*) FILTER (WHERE abstained), count(*) FROM answers")
            refused, total = cur.fetchone()
    print("=" * 78)
    print(f"{total} question(s) in the audit trail, {refused} answered with a refusal.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

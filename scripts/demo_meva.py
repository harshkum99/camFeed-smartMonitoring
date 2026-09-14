#!/usr/bin/env python3
"""The first demo on real footage: plain-English questions about imported MEVA video.

Everything printed comes from the same `ask()` path the console uses, against tracks produced by
running the detector and tracker over the recordings — no synthetic rows. Run after:

    smartcam-ingest run <footage dir> --site meva --tz America/New_York
    smartcam-ingest score --site meva --annotations <KPF dir> --write-capabilities

    python scripts/demo_meva.py
"""

from __future__ import annotations

import argparse
from zoneinfo import ZoneInfo

import psycopg

from smartcam.ingest.meva import MEVA_SITE, MEVA_TENANT, meva_profile
from smartcam.query.ask import ask
from smartcam.query.nl import Catalog, StubProvider

QUESTIONS = [
    *meva_profile().examples,
    "Show me vehicles at Bus G340 today",
    "How many people entered the admin building today?",
    "Was anything blocking the doorway at Admin G326 today?",
    "How many people were at Admin G329 yesterday?",
]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default="dbname=smartcam_dev")
    args = ap.parse_args()

    with psycopg.connect(args.dsn) as conn:
        catalog = Catalog.load(conn, MEVA_SITE)
        if catalog.as_of is None:
            print("The MEVA site has no imported footage yet. Run smartcam-ingest first.")
            return 1
        local = catalog.as_of.astimezone(ZoneInfo(catalog.tz))
        print(f"MEVA Muscatatuck · {len(catalog.cameras)} cameras · answered as of "
              f"{local:%a %d %b %Y %H:%M %Z} · offline interpreter\n")

        for q in QUESTIONS:
            r = ask(conn, q, tenant_id=MEVA_TENANT, site_id=MEVA_SITE, actor="demo",
                    provider=StubProvider(), catalog=catalog)
            a = r.answer
            print(f"Q  {q}")
            if a is None:
                print(f"A  [could not interpret] {r.message}\n")
                continue
            tag = f"[{a.abstain_reason}] " if a.abstain_reason else ""
            print(f"A  {tag}{a.message}")
            if not a.abstained or a.total:
                print(f"   confident {a.confident} · ambiguous {a.ambiguous} · "
                      f"coverage {a.coverage_pct:.1%} · {len(a.evidence)} evidence frame(s)")
            if a.evidence:
                e = a.evidence[0]
                when = e.ts.astimezone(ZoneInfo(catalog.tz))
                print(f"   best frame: {e.camera_name} at {when:%H:%M:%S %Z}, "
                      f"conf {e.confidence:.2f}")
            print(f"   understood as: {a.describes}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

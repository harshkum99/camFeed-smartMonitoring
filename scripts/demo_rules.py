#!/usr/bin/env python3
"""The alerting demo: author rules in English, then run a shift past them.

Shows the two things a buyer actually needs to see — that a rule they typed does what they meant,
and that it does not bury them. The second half deliberately feeds noise (birds, IR switching,
box jitter, one stuck pallet) to show what the system declines to alert on.

    python scripts/demo_rules.py
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime, timedelta

from sanjay.query.nl import Catalog
from sanjay.rules.engine import Detection, RuleEngine
from sanjay.rules.nl import StubRuleProvider, compile_rule, explain

SITE = "aaaaaaaa-0000-0000-0000-000000000001"
CAT = Catalog(
    cameras={"Gate 3 — Main Entry": "cam-gate", "Zone B — Press Shop": "cam-zoneb",
             "Restricted — Chemical Store": "cam-restrict", "Fire Exit — East": "cam-fire"},
    zones={"Chemical store": "z-chem", "Zone B floor": "z-b", "Fire exit clearance": "z-fire"},
)
NAMES = {v: k for k, v in CAT.cameras.items()}

INSTRUCTIONS = [
    "Alert me if anyone enters the chemical store after 8pm",
    "Tell me when someone is on the zone b floor without a helmet",
    "Alert if the fire exit clearance is blocked for more than 60 seconds",
]

T0 = datetime(2026, 9, 3, 16, 0, tzinfo=UTC)      # 21:30 IST — after hours


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.parse_args()

    print("AUTHORING\n" + "=" * 78)
    rules = []
    for i, text in enumerate(INSTRUCTIONS, 1):
        c = compile_rule(text, CAT, StubRuleProvider(), site_id=SITE, rule_id=f"r{i}")
        print(f'\n{i}. Operator typed: "{text}"\n')
        if c.rule is None:
            print(f"   Declined: {c.unsupported}")
            continue
        for line in explain(c.rule, NAMES).splitlines():
            print(f"   {line}")
        if c.unresolved:
            print(f"   ⚠ could not identify: {', '.join(c.unresolved)}")
        rules.append(c.rule)

    # Everything the rules should watch is on one camera for the demo.
    for r in rules:
        r.cameras = ["cam-restrict"]

    print("\n\nA SHIFT, WITH NOISE\n" + "=" * 78)
    engine = RuleEngine(rules)
    fired, rejected = [], []

    for d in _shift():
        alerts = engine.observe(d)
        fired += [(d.ts, a) for a in alerts]
        if not alerts:
            for rid, why in engine.last_reject.items():
                rejected.append((d.ts, rid, why))

    print(f"\n{len(_shift())} detections observed · {len(fired)} alert(s) delivered\n")
    for ts, a in fired:
        lag = (ts - a.ts).total_seconds()
        verify = "  · AI second opinion pending" if a.needs_verification else ""
        print(f"  {ts:%H:%M:%S}  [{a.severity.upper():8}] {a.rule_name}: {a.reason}{verify}")
        print(f"             {NAMES.get(a.camera_id, a.camera_id)} · "
              f"confidence {a.confidence:.2f} · fired {lag:.1f}s after detection")

    print("\n  Declined, with reasons (this is the product working):")
    seen = set()
    for _, rid, why in rejected:
        if (rid, why) in seen:
            continue
        seen.add((rid, why))
        name = next((r.name for r in rules if r.rule_id == rid), rid)
        print(f"    {name:<24} {why}")

    print("\n  Operator taps 'not an incident' three times on the helmet rule…")
    helmet = next((r for r in rules if "helmet" in r.name.lower()), None)
    if helmet:
        helmet.suppression.quiet_after_n_unactioned = 3
        for _ in range(3):
            muted = engine.suppressor.record_feedback(helmet, actioned=False)
        print(f"    rule auto-muted: {muted} — flagged for retuning rather than left shouting")
    return 0


def _shift() -> list[Detection]:
    """A plausible mix: real incidents buried in the noise a camera actually produces."""
    out: list[Detection] = []
    t = T0

    def add(n, **kw):
        nonlocal t
        for _ in range(n):
            base = dict(track_id="x", camera_id="cam-restrict", ts=t, cls="person",
                        confidence=0.9, bbox=(0.4, 0.4, 0.5, 0.75),
                        zones=frozenset({"Chemical store"}), attrs={}, moving=True,
                        track_age_ms=5000)
            base.update(kw)
            out.append(Detection(**base))
            t += timedelta(seconds=1)

    add(8, track_id="bird", bbox=(0.5, 0.2, 0.505, 0.21))          # too small
    add(8, track_id="irswitch", scene_change=0.95)                  # IR day/night switch
    add(8, track_id="shadow", bbox=(0.05, 0.5, 0.95, 0.56))         # wide, flat
    add(8, track_id="intruder")                                     # the real one
    add(8, track_id="worker", zones=frozenset({"Zone B floor"}),
        attrs={"helmet": 0.08})                                     # real PPE violation
    add(8, track_id="facing-away", zones=frozenset({"Zone B floor"}),
        attrs={"helmet": 0.44})                                     # borderline — still fires,
    add(8, track_id="intruder2")                                    # cooldown should hold
    return out


if __name__ == "__main__":
    raise SystemExit(main())

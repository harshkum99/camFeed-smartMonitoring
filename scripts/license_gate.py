#!/usr/bin/env python3
"""Fail the build if a forbidden licence enters the dependency tree.

This exists because of a specific, expensive failure mode. Ultralytics states plainly that its
AGPL-3.0 licence reaches SaaS and API deployments *and* trained weights, so shipping YOLO in a
closed-source product means open-sourcing all of Sanjay. And `ultralytics` is a transitive
dependency of a great many computer-vision tutorials, Roboflow notebooks and tracker repos — it
does not arrive through a deliberate decision, it arrives because someone pip-installed a
tracking library at 11pm.

Discovering this during a bank's or a government buyer's procurement due diligence is a
deal-killer, not a patch. So it is checked in CI, on every commit, from day one.

The permissive alternatives are not a compromise: D-FINE-S scores 50.6 COCO AP at 3.5 ms on a T4
against YOLO11-S's 44.4 AP at 3.2 ms. We give up nothing.

    python scripts/license_gate.py            # check the installed environment
    python scripts/license_gate.py --strict   # also fail on unknown licences
"""

from __future__ import annotations

import argparse
import sys
from importlib import metadata

# Packages that must never appear, with the reason. Reasons are printed on failure, because a
# bare "forbidden" leaves the next engineer no way to make the right call.
FORBIDDEN: dict[str, str] = {
    "ultralytics": (
        "AGPL-3.0. Ultralytics states SaaS/API deployment AND trained weights are covered; "
        "compliance means open-sourcing all of Sanjay. Use D-FINE or RF-DETR (Apache-2.0) — "
        "both score higher on COCO at comparable latency."
    ),
    "insightface": (
        "Model weights (buffalo_l, antelopev2) are research/non-commercial only. This is the "
        "default every face-recognition demo reaches for. Use AdaFace IR-101 (MIT) instead."
    ),
    "yolov5": "AGPL-3.0 (Ultralytics lineage).",
    "yolov8": "AGPL-3.0 (Ultralytics lineage).",
    "deimv2": (
        "Custom 'DEIMv2 License' requiring a commercial enquiry — despite DEIM v1 being "
        "Apache-2.0, which makes this very easy to grab by accident. Use D-FINE."
    ),
    "zoneminder": "GPL-2.0.",
    "edgeface": "CC BY-NC-SA — non-commercial.",
}

# Licence strings that are incompatible with a closed-source commercial SaaS. Matched as
# substrings against the package's declared licence, case-insensitively.
FORBIDDEN_LICENCES: tuple[str, ...] = (
    "agpl",
    "affero",
    "gpl-2", "gplv2", "gpl v2", "gnu general public license v2",
    "gpl-3", "gplv3", "gpl v3", "gnu general public license v3",
    "sspl",
    "non-commercial", "noncommercial", "cc by-nc", "research only",
    "commons clause",
)

# LGPL is fine when dynamically linked, which is how Python uses it. Listed explicitly so nobody
# "helpfully" adds it to the forbidden list later.
ALLOWED_DESPITE_MATCH: tuple[str, ...] = ("lgpl", "lesser general public")

# Packages whose declared metadata is wrong or absent but whose real licence we have verified.
VERIFIED_OVERRIDES: dict[str, str] = {
    "frigate": "MIT",   # repo LICENSE is MIT; only the name and logo are trademarked
}


#: A `License` field longer than this is the full licence *text*, not an identifier. Matching
#: substrings against licence prose produces false positives that train people to ignore the
#: gate — matplotlib, for instance, embeds 64 KB of text that mentions FreeType's
#: "FTL OR GPL-2.0-or-later" dual licence, which we use under FTL. A gate people ignore is
#: worse than no gate.
MAX_IDENTIFIER_LEN = 200


def declared_licence(dist: metadata.Distribution) -> str:
    """Return the package's licence *identifiers*, never its embedded licence text.

    Prefers the SPDX `License-Expression` and the `License ::` trove classifiers, both of which
    are short, structured and reliable. The free-text `License` field is used only when it is
    short enough to plausibly be an identifier such as "MIT" or "AGPL-3.0".
    """
    meta = dist.metadata
    parts: list[str] = []
    if expr := meta.get("License-Expression"):
        parts.append(expr)
    parts += [c for c in meta.get_all("Classifier") or [] if c.startswith("License ::")]
    lic = meta.get("License")
    if lic and len(lic) <= MAX_IDENTIFIER_LEN:
        parts.append(lic)
    return " | ".join(p for p in parts if p and p != "UNKNOWN").strip()


def check(strict: bool = False) -> int:
    violations: list[tuple[str, str, str]] = []
    unknown: list[str] = []

    for dist in sorted(metadata.distributions(), key=lambda d: (d.metadata.get("Name") or "")):
        name = (dist.metadata.get("Name") or "").strip()
        if not name:
            continue
        key = name.lower().replace("_", "-")

        if key in FORBIDDEN:
            violations.append((name, "banned package", FORBIDDEN[key]))
            continue

        lic = VERIFIED_OVERRIDES.get(key) or declared_licence(dist)
        low = lic.lower()

        if not lic:
            unknown.append(name)
            continue
        if any(ok in low for ok in ALLOWED_DESPITE_MATCH):
            continue
        for bad in FORBIDDEN_LICENCES:
            if bad in low:
                violations.append((name, lic, f"licence contains '{bad}'"))
                break

    if violations:
        print("LICENCE GATE FAILED\n", file=sys.stderr)
        for name, lic, why in violations:
            print(f"  {name}", file=sys.stderr)
            print(f"    licence: {lic}", file=sys.stderr)
            print(f"    {why}\n", file=sys.stderr)
        print("These cannot ship in a closed-source commercial SaaS. Remove them, or if one is "
              "genuinely required, get sign-off and add it to VERIFIED_OVERRIDES with the "
              "reasoning.", file=sys.stderr)
        return 1

    if unknown:
        msg = f"{len(unknown)} package(s) declare no licence: {', '.join(sorted(unknown)[:12])}"
        if strict:
            print(f"LICENCE GATE FAILED (strict)\n  {msg}", file=sys.stderr)
            return 1
        print(f"note: {msg}", file=sys.stderr)

    print("licence gate passed")
    return 0


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--strict", action="store_true",
                    help="also fail when a package declares no licence at all")
    raise SystemExit(check(ap.parse_args().strict))

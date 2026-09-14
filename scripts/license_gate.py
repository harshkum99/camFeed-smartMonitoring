#!/usr/bin/env python3
"""Fail the build if a forbidden licence enters the dependency tree.

This exists because of a specific, expensive failure mode. Ultralytics states plainly that its
AGPL-3.0 licence reaches SaaS and API deployments *and* trained weights, so shipping YOLO in a
closed-source product means open-sourcing the whole platform. And `ultralytics` is a transitive
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
import ast
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Packages that must never appear, with the reason. Reasons are printed on failure, because a
# bare "forbidden" leaves the next engineer no way to make the right call.
FORBIDDEN: dict[str, str] = {
    "ultralytics": (
        "AGPL-3.0. Ultralytics states SaaS/API deployment AND trained weights are covered; "
        "compliance means open-sourcing the whole platform. Use D-FINE or RF-DETR (Apache-2.0) — "
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

# Licence strings that are incompatible with a closed-source commercial SaaS. Matched against the
# declared licence after normalisation: lower case, '-' and '_' become spaces. Normalising matters
# more than it looks — the SPDX id `CC-BY-NC-4.0`, which is exactly how model cards write it,
# slipped past an earlier pattern written as "cc by-nc".
FORBIDDEN_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bagpl", "AGPL"),
    (r"\baffero\b", "Affero GPL"),
    (r"\bgpl\s*v?\s*[23]", "GPL"),
    (r"\bgpl\b", "GPL"),                  # bare "GPL", as older packages declare it
    (r"\bgnu gpl\b", "GPL"),
    (r"gnu general public licen[cs]e", "GPL"),
    (r"\bsspl\b", "SSPL"),
    (r"\bnon\s*commercial\b", "non-commercial"),
    (r"\bcc by nc\b", "non-commercial Creative Commons"),
    (r"\bresearch (only|use)\b", "research-only"),
    (r"\bcommons clause\b", "Commons Clause"),
    (r"\bopenrail\b", "OpenRAIL use restrictions"),
)

# LGPL is fine when dynamically linked, which is how Python uses it. Its tokens are REMOVED before
# matching rather than exempting the whole string, so "LGPL-3.0 OR GPL-3.0" still fails on the
# GPL half instead of passing because the word LGPL appears somewhere in it.
LGPL_TOKENS = (
    r"gnu lesser general public licen[cs]e(\s*v?\s*[\d.]+)?(\s*or later)?(\s*\(lgplv?[\d.+]*\))?",
    r"lesser general public licen[cs]e(\s*v?\s*[\d.]+)?",
    r"\blgpl\s*v?\s*[\d.]*\s*(only|or later|\+)?",
)


def normalise(licence: str) -> str:
    low = re.sub(r"[-_]", " ", licence.lower())
    return re.sub(r"\s+", " ", low).strip()


def forbidden_reason(licence: str) -> str | None:
    text = normalise(licence)
    for tok in LGPL_TOKENS:
        text = re.sub(tok, " ", text)
    for pattern, label in FORBIDDEN_PATTERNS:
        if re.search(pattern, text):
            return f"licence is {label}"
    return None


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


# --- artefacts that are not Python packages -------------------------------------------------
#
# Model weights arrive as files, not as pip packages, so the loop above never sees them — and
# weights are exactly where non-commercial licences hide (InsightFace's buffalo_l, Ultralytics'
# .pt files). third_party.toml declares every weights file, independently implemented algorithm
# and dataset; the ingest loader refuses weights whose hash is not declared there, and this checks
# the declarations themselves.

ALLOWED_ARTEFACT_LICENCES = {"apache 2.0", "mit", "bsd 2 clause", "bsd 3 clause", "cc by 4.0",
                             "cc0 1.0"}
FORBIDDEN_MODELS = ("ultralytics", "yolov5", "yolov8", "yolo11", "yolo12", "insightface",
                    "buffalo_l", "antelopev2", "deimv2", "edgeface")
WEIGHT_SUFFIXES = (".onnx", ".pt", ".pth", ".safetensors", ".engine", ".tflite")


def check_manifest(root: Path) -> list[tuple[str, str, str]]:
    path = root / "third_party.toml"
    if not path.exists():
        return [("third_party.toml", "-", "manifest missing")]
    data = tomllib.loads(path.read_text())
    notices = (root / "THIRD_PARTY_NOTICES.md").read_text().lower() \
        if (root / "THIRD_PARTY_NOTICES.md").exists() else ""
    out: list[tuple[str, str, str]] = []

    def bad(name: str, lic: str, why: str) -> None:
        out.append((f"third_party.toml: {name}", lic, why))

    for kind in ("model", "algorithm", "dataset"):
        for entry in data.get(kind, []):
            name = entry.get("name", f"<unnamed {kind}>")
            lic = entry.get("licence", "")
            if normalise(lic) not in ALLOWED_ARTEFACT_LICENCES:
                bad(name, lic, f"{kind} licence is not on the allow-list for shipped artefacts")
            if name.lower() not in notices:
                bad(name, lic, "not listed in THIRD_PARTY_NOTICES.md")

    declared_files = set()
    for m in data.get("model", []):
        name = m.get("name", "?")
        declared_files.add(Path(m.get("file", "")).name)
        if not re.fullmatch(r"[0-9a-f]{64}", str(m.get("sha256", ""))):
            bad(name, m.get("licence", ""), "sha256 missing or malformed")
        url = str(m.get("source_url", ""))
        if not re.search(r"/(resolve|blob|raw)/[0-9a-f]{40}/", url):
            bad(name, m.get("licence", ""),
                "source_url must be pinned to a commit, not a branch such as main")
        hay = f"{name} {url} {m.get('file', '')}".lower()
        for banned in FORBIDDEN_MODELS:
            if banned in hay:
                bad(name, m.get("licence", ""), f"'{banned}' weights are banned (see FORBIDDEN)")

    for a in data.get("algorithm", []):
        target = root / a.get("path", "")
        if not target.is_file():
            bad(a.get("name", "?"), a.get("licence", ""), f"path {a.get('path')} does not exist")
            continue
        text = target.read_text()
        for needle in a.get("header_must_contain", []):
            if needle not in text:
                bad(a.get("name", "?"), a.get("licence", ""),
                    f"{a.get('path')} no longer states its provenance ('{needle}' missing)")

    for file, literal in weight_literals(root / "smartcam"):
        if Path(literal).name not in declared_files:
            bad(literal, "-", f"weights file referenced in {file} is not declared in the manifest")
    return out


def weight_literals(package: Path) -> list[tuple[str, str]]:
    """String constants in the package that name a weights file.

    Only whole path-like literals count — no spaces, ending in a weights suffix — and docstrings
    are skipped, so explaining in prose that the loader refuses an undeclared .onnx file does not
    itself fail the gate.
    """
    found = []
    for py in sorted(package.rglob("*.py")):
        tree = ast.parse(py.read_text())
        docstrings = {id(n.body[0].value) for n in ast.walk(tree)
                      if isinstance(n, ast.Module | ast.ClassDef | ast.FunctionDef |
                                    ast.AsyncFunctionDef)
                      and n.body and isinstance(n.body[0], ast.Expr)
                      and isinstance(n.body[0].value, ast.Constant)}
        joined = {id(v) for n in ast.walk(tree) if isinstance(n, ast.JoinedStr) for v in n.values}
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings and id(node) not in joined
                    and " " not in node.value and node.value.lower().endswith(WEIGHT_SUFFIXES)):
                found.append((str(py.relative_to(package.parent)), node.value))
    return found


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
        if not lic:
            unknown.append(name)
            continue
        if why := forbidden_reason(lic):
            violations.append((name, lic, why))

    violations += check_manifest(ROOT)

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

"""`smartcam-evidence` — prepare an evidence bundle, and verify one.

    smartcam-evidence create --site meva --purpose "Complaint 42, stairwell, 11 Mar" \\
        --question "How many people were at Admin G329 between 11:55 and 12:00 today?"
    smartcam-evidence create --site meva --purpose "..." --tracks <uuid>,<uuid>
    smartcam-evidence verify evidence-<id>.zip [--dsn dbname=smartcam_dev]

`create` bundles the evidence behind an answer: the same tracks whose frames the console showed.
`verify` needs no database — every check it runs, a court's expert can run with `shasum` and
Python — but with `--dsn` it also confirms the bundle is the one this system recorded.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

from smartcam.evidence.merkle import merkle_root


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smartcam-evidence", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("create", help="prepare a bundle from an answer's evidence or track ids")
    c.add_argument("--site", required=True)
    c.add_argument("--purpose", required=True)
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--question")
    g.add_argument("--tracks", help="comma-separated track ids")
    c.add_argument("--actor", default=os.environ.get("USER", "cli"))
    c.add_argument("--footage-dir", type=Path, default=None)
    c.add_argument("--no-certificate", action="store_true")
    c.add_argument("--dsn", default=os.environ.get("SMARTCAM_DSN", "dbname=smartcam_dev"))
    v = sub.add_parser("verify", help="verify a bundle archive or extracted folder")
    v.add_argument("path", type=Path)
    v.add_argument("--dsn", default=None)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return _create(args) if args.cmd == "create" else _verify(args)


def _create(args) -> int:
    import psycopg

    from smartcam.evidence.service import BundleRefused, BundleRequest, create_bundle
    from smartcam.evidence.store import data_dir
    from smartcam.ingest.meva import PROFILES

    if args.site not in PROFILES:
        print(f"unknown site {args.site!r}; known: {', '.join(PROFILES)}", file=sys.stderr)
        return 2
    prof = PROFILES[args.site]()
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        if args.question:
            from smartcam.query.ask import ask
            from smartcam.query.nl import StubProvider
            r = ask(conn, args.question, tenant_id=prof.tenant_id, site_id=prof.site_id,
                    actor=args.actor, provider=StubProvider())
            if r.answer is None or not r.answer.evidence:
                print(f"that question produced no evidence to bundle: {r.message}",
                      file=sys.stderr)
                return 3
            print(f"answer: {r.answer.message}")
            tracks = [e.track_id for e in r.answer.evidence]
        else:
            tracks = [t.strip() for t in args.tracks.split(",") if t.strip()]
        try:
            built = create_bundle(conn, BundleRequest(
                prof.tenant_id, prof.site_id, args.actor, args.purpose, tracks, args.question),
                data_dir=data_dir(), footage_root=args.footage_dir,
                with_certificate=not args.no_certificate)
        except BundleRefused as e:
            print(f"refused: {e}", file=sys.stderr)
            return 3
    print(f"bundle {built.bundle_id}\n  archive  {built.archive}\n"
          f"  sha256   {built.archive_sha256}\n  merkle   {built.merkle_root}\n"
          f"  manifest {built.manifest_sha256}")
    for w in built.warnings:
        print(f"  warning: {w}")
    return 0


def _verify(args) -> int:
    archive_sha = None
    with tempfile.TemporaryDirectory() as tmp:
        root = args.path
        if root.is_file():
            archive_sha = hashlib.sha256(root.read_bytes()).hexdigest()
            with zipfile.ZipFile(root) as z:
                _safe_extract(z, Path(tmp))
            tops = [p for p in Path(tmp).iterdir() if p.is_dir()]
            root = tops[0] if len(tops) == 1 else Path(tmp)
        ok, manifest_sha, lines = check_tree(root)
        for line in lines:
            print(line)
        if args.dsn and manifest_sha:
            ok &= _check_record(args.dsn, root, manifest_sha, archive_sha)
    print("BUNDLE VERIFIED" if ok else "BUNDLE FAILED VERIFICATION")
    return 0 if ok else 1


def check_tree(root: Path) -> tuple[bool, str | None, list[str]]:
    """The same checks a court's expert would run by hand: SHA256SUMS, then the Merkle root —
    plus one they might not think of: nothing is in the bundle that the manifest does not list."""
    from smartcam.evidence.bundle import SUPPORT_FILES

    lines: list[str] = []
    ok = True
    sums = root / "SHA256SUMS"
    if not sums.is_file() or not (root / "manifest.json").is_file():
        return False, None, ["not an evidence bundle: SHA256SUMS or manifest.json missing"]
    try:
        manifest_bytes = (root / "manifest.json").read_bytes()
        manifest = json.loads(manifest_bytes)
        paths = {i["path"] for i in manifest["items"]}
        root_hex = merkle_root([(i["sha256"], i["path"]) for i in manifest["items"]])
    except (ValueError, KeyError, TypeError) as e:
        return False, None, [f"manifest.json is malformed: {e}"]

    listed = set()
    for line in sums.read_text().splitlines():
        want, sep, rel = line.partition("  ")
        if not sep or len(want) != 64:
            ok = False
            lines.append(f"MALFORMED SHA256SUMS line: {line!r}")
            continue
        listed.add(rel)
        p = root / rel
        got = hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else None
        if got != want:
            ok = False
            lines.append(f"{'MISSING' if got is None else 'MISMATCH':<9} {rel}")
    allowed = paths | set(SUPPORT_FILES)
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root).as_posix()
        if p.is_symlink():
            ok = False
            lines.append(f"SYMLINK   {rel}")
        elif p.is_file() and (rel not in allowed or (rel not in listed and rel != "SHA256SUMS")):
            ok = False
            lines.append(f"UNLISTED  {rel}")
    lines.append(f"{len(listed)} file(s) checked against SHA256SUMS")
    for item in manifest["items"]:
        p = root / item["path"]
        if not p.is_file() or hashlib.sha256(p.read_bytes()).hexdigest() != item["sha256"]:
            ok = False
            lines.append(f"MANIFEST  {item['path']} does not match its manifest hash")
    same = root_hex == manifest["merkle"]["root"]
    ok &= same
    lines.append(f"merkle root {root_hex} {'matches' if same else 'DOES NOT MATCH'} the manifest")
    return ok, hashlib.sha256(manifest_bytes).hexdigest(), lines


def _check_record(dsn: str, root: Path, manifest_sha: str, archive_sha: str | None) -> bool:
    """Against the record made when the bundle was created: manifest, root, the SHA256SUMS that
    covers the README, verifier and certificate, and the archive itself when one was given."""
    import psycopg

    manifest = json.loads((root / "manifest.json").read_text())
    with psycopg.connect(dsn) as conn, conn.cursor() as cur:
        cur.execute("SELECT manifest_sha256, merkle_root, certificate_sha256, sums_sha256, "
                    "archive_sha256 FROM evidence_bundles WHERE bundle_id = %s",
                    (manifest["bundle_id"],))
        row = cur.fetchone()
    if row is None:
        print(f"RECORD    no bundle {manifest['bundle_id']} is recorded in this system")
        return False
    want_manifest, want_root, want_cert, want_sums, want_archive = row
    checks = {
        "manifest": want_manifest == manifest_sha,
        "merkle root": want_root == manifest["merkle"]["root"],
        "SHA256SUMS": want_sums is None or want_sums == hashlib.sha256(
            (root / "SHA256SUMS").read_bytes()).hexdigest(),
    }
    cert = root / "certificate" / "certificate-draft.pdf"
    if want_cert:
        checks["certificate"] = cert.is_file() and \
            hashlib.sha256(cert.read_bytes()).hexdigest() == want_cert
    else:
        checks["no certificate"] = not cert.exists()
    if archive_sha is not None:
        checks["archive"] = archive_sha == want_archive
    for name, good in checks.items():
        print(f"RECORD    {name}: {'matches' if good else 'DOES NOT MATCH'} the record made at "
              f"creation")
    return all(checks.values())


def _safe_extract(z: zipfile.ZipFile, dest: Path) -> None:
    base = dest.resolve()
    for info in z.infolist():
        target = (dest / info.filename).resolve()
        if not target.is_relative_to(base):
            raise SystemExit(f"refusing archive entry outside the bundle: {info.filename}")
    z.extractall(dest)


if __name__ == "__main__":
    raise SystemExit(main())

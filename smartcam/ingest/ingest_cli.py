"""`smartcam-ingest` — analyse recorded footage into the index, and score it against ground truth.

    smartcam-ingest run FOOTAGE_DIR --site meva --tz America/New_York
    smartcam-ingest score --site meva --annotations ANNOTATIONS_DIR --write-capabilities

`run` needs the ingest extra (`pip install -e '.[ingest]'`), ffmpeg, and the model file declared in
third_party.toml. Expect about 3.5 minutes of CPU per 5-minute 1080p clip at 5 fps on a laptop.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from smartcam.evidence.store import REPO_ROOT, data_dir

EXIT_OK, EXIT_ARGS, EXIT_REFUSED, EXIT_CLIP_FAILED = 0, 2, 3, 5
MODEL_FILE = "dfine_s_coco/model.onnx"


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smartcam-ingest", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    run = sub.add_parser("run", help="analyse a folder of recorded footage into the index")
    run.add_argument("root", type=Path)
    run.add_argument("--site", required=True, help="site profile key, e.g. meva")
    run.add_argument("--tz", required=True,
                     help="timezone the RECORDER was set to. Required: a wrong one moves every "
                          "track by hours with no visible symptom.")
    run.add_argument("--convention", default=None)
    run.add_argument("--fps", type=float, default=5.0,
                     help="sampling rate. Below ~5 fps tracking fragments and counts go wrong.")
    run.add_argument("--only", default=None, help="glob on file names to import a subset")
    run.add_argument("--limit-seconds", type=float, default=None)
    run.add_argument("--start-seconds", type=float, default=0.0)
    run.add_argument("--threads", type=int, default=None)
    _common(run)

    score = sub.add_parser("score", help="score imported clips against MEVA KPF annotations")
    score.add_argument("--site", required=True)
    score.add_argument("--annotations", type=Path, required=True)
    score.add_argument("--write-capabilities", action="store_true")
    _common(score)
    return p


def _common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--dsn", default=os.environ.get("SMARTCAM_DSN", "dbname=smartcam_dev"))
    p.add_argument("--model-dir", type=Path,
                   default=Path(os.environ.get("SMARTCAM_MODEL_DIR", REPO_ROOT / "var/models")))
    p.add_argument("--manifest", type=Path, default=REPO_ROOT / "third_party.toml")


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    from smartcam.ingest.meva import PROFILES
    if args.site not in PROFILES:
        print(f"unknown site profile {args.site!r}; known: {', '.join(PROFILES)}",
              file=sys.stderr)
        return EXIT_ARGS
    profile = PROFILES[args.site]()
    return _run(args, profile) if args.cmd == "run" else _score(args, profile)


def _run(args, profile) -> int:
    try:
        import psycopg

        from smartcam.ingest.detect import ModelError, load_onnx
        from smartcam.ingest.keyframes import KeyframeStore
        from smartcam.ingest.pipeline import IngestConfig, process_clip
        from smartcam.ingest.recorded import scan
        from smartcam.ingest.write import (
            CameraRow,
            ImportRefused,
            bootstrap_site,
            ensure_months,
            write_clip,
        )
    except ImportError as e:
        print(f"missing dependency ({e}). Install with: pip install -e '.[ingest]'",
              file=sys.stderr)
        return EXIT_ARGS

    if args.tz != profile.tz:
        print(f"refusing: --tz {args.tz} but site {profile.key} records in {profile.tz}",
              file=sys.stderr)
        return EXIT_REFUSED
    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return EXIT_ARGS
    root = args.root.resolve()
    ddir = data_dir()
    if ddir.is_relative_to(root) or root.is_relative_to(ddir):
        print("refusing: the data directory and the footage directory must not contain each "
              "other — source footage is never written to", file=sys.stderr)
        return EXIT_REFUSED

    try:
        detector = load_onnx(args.model_dir / MODEL_FILE, manifest=args.manifest,
                             file_name=MODEL_FILE, threads=args.threads)
    except ModelError as e:
        print(f"model refused: {e}", file=sys.stderr)
        return EXIT_ARGS

    clips = [c for c in scan(root, tz=args.tz, convention=args.convention)
             if not args.only or fnmatch.fnmatch(c.path.name, args.only)]
    mapped = [c for c in clips if c.camera_key in profile.cameras]
    for c in clips:
        if c.camera_key not in profile.cameras:
            print(f"  skip {c.path.name}: camera {c.camera_key} is not mapped for this site")
    if not mapped:
        print("nothing to import")
        return EXIT_ARGS

    print(f"{len(mapped)} clip(s) · detector {detector.identity.id} · {args.fps:g} fps · "
          f"data dir {ddir}")
    cfg = IngestConfig(sample_fps=args.fps, limit_seconds=args.limit_seconds,
                       start_seconds=args.start_seconds)
    store = KeyframeStore(ddir)
    runs_dir = ddir / "runs" / profile.site_id
    failed = 0

    # autocommit: every conn.transaction() below is then a real, top-level transaction. Without it
    # the first bare query opens one transaction for the whole run, every clip becomes a
    # savepoint inside it, and a crash on clip six discards clips one to five.
    with psycopg.connect(args.dsn, autocommit=True) as conn:
        from smartcam.ingest.decode import DecodeError, probe_video
        cams, seen = [], set()
        for c in mapped:
            if c.camera_key in seen:
                continue
            seen.add(c.camera_key)
            info = probe_video(c.path)
            usable = info.width >= 640 and info.height >= 360 and info.src_fps >= 4
            cams.append(CameraRow(
                camera_id=profile.cameras[c.camera_key][0], key=c.camera_key,
                name=profile.cameras[c.camera_key][1], width=info.width, height=info.height,
                src_fps=info.src_fps, codec=info.codec,
                grade="detection" if usable else "unservable",
                grade_notes=["graded from a recording: resolution and frame rate only; the live "
                             "recorder's sub stream and keyframe interval were not observed"]))
        try:
            bootstrap_site(conn, profile, cams, detector.identity.id)
            with conn.transaction():
                ensure_months(conn, [c.starts_at for c in mapped] +
                              [c.trusted_end for c in mapped])
        except ImportRefused as e:
            print(f"refused: {e}", file=sys.stderr)
            return EXIT_REFUSED

        for i, c in enumerate(mapped, 1):
            rel = c.path.relative_to(root).as_posix()
            source_uri = f"file:{rel}"
            if profile.source_prefix and "drops-123-r13" in c.path.parts:
                # Only when the full public key can be derived; otherwise record what is known.
                tail = c.path.parts[c.path.parts.index("drops-123-r13") + 1:]
                source_uri = profile.source_prefix + "/".join(tail)
            print(f"[{i}/{len(mapped)}] {rel}")
            try:
                outcome = process_clip(c, profile=profile, detector=detector, cfg=cfg,
                                       store=store, runs_dir=runs_dir, source_uri=source_uri)
            except (DecodeError, OSError, RuntimeError, ValueError) as e:
                failed += 1
                print(f"    FAILED: {e} — nothing written for this clip")
                continue
            if not outcome.ok or outcome.clip is None:
                failed += 1
                print(f"    FAILED: {outcome.reason} — nothing written for this clip")
                continue
            try:
                write_clip(conn, profile, outcome.clip, outcome.rows)
            except ImportRefused as e:
                failed += 1
                print(f"    REFUSED: {e} — nothing written for this clip")
                continue
            print(f"    {outcome.tracks} track(s) from {outcome.sampled} frames in "
                  f"{outcome.seconds:.0f}s")

        with conn.cursor() as cur:
            cur.execute("SELECT as_of FROM sites WHERE site_id = %s", (profile.site_id,))
            as_of = cur.fetchone()[0]
    print(f"\nsite answers as of {as_of}. Start the API for this site with:\n"
          f"  SMARTCAM_TENANT={profile.tenant_id} SMARTCAM_SITE={profile.site_id} "
          f".venv/bin/uvicorn smartcam.api.app:app --port 8420")
    return EXIT_CLIP_FAILED if failed else EXIT_OK


def _score(args, profile) -> int:
    import psycopg

    from smartcam.ingest.score import (
        annotation_files,
        as_dicts,
        capability,
        load_kpf,
        read_log,
        score_log,
    )
    from smartcam.ingest.write import set_capability

    ddir = data_dir()
    runs = ddir / "runs" / profile.site_id
    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        # Every imported clip, including those that produced no tracks at all. Those are the clips
        # where detection did worst, and leaving them out would inflate the camera's capability.
        cur.execute("SELECT clip_id, camera_id FROM clips WHERE site_id = %s", (profile.site_id,))
        current = cur.fetchall()
        cur.execute("SELECT camera_id, capabilities->>'detector' FROM cameras WHERE site_id = %s",
                    (profile.site_id,))
        camera_detector = {str(c): d for c, d in cur.fetchall()}
    scores, by_camera = [], {}
    for cid, camera_id in current:
        logs = sorted(runs.glob(f"{cid}.*.json.gz"), key=lambda p: p.stat().st_mtime)
        if not logs:
            print(f"  no run log for clip {cid}; re-run the import to score it")
            continue
        log = read_log(logs[-1])
        if log.get("detector") != camera_detector.get(str(camera_id)):
            print(f"  {log['file']}: run used {log.get('detector')}, camera now runs "
                  f"{camera_detector.get(str(camera_id))}; not scored")
            continue
        files = annotation_files(args.annotations, log["file"])
        if files is None:
            print(f"  {log['file']}: no annotations, not scored")
            continue
        gt = load_kpf(*files, width=log["width"], height=log["height"])
        s = score_log(log, gt)
        scores.append(s)
        by_camera.setdefault(str(camera_id), []).append((s, log["detector"]))

    if not scores:
        print("nothing to score")
        return EXIT_ARGS
    print(f"\n{'clip':<52}{'people':>7}{'found':>7}{'tracks':>8}{'ids/person':>12}"
          f"{'box recall':>12}")
    for s in sorted(scores, key=lambda s: s.file):
        print(f"{s.file[:51]:<52}{s.gt_people:>7}{s.people_found:>7}{s.tracks_written:>8}"
              f"{s.ids_per_person if s.ids_per_person is not None else '-':>12}"
              f"{s.box_recall if s.box_recall is not None else '-':>12}")

    out_dir = ddir / "scores" / profile.site_id
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    (out_dir / f"{stamp}.json").write_text(json.dumps(as_dicts(scores), indent=1))
    print(f"\nsaved {out_dir / (stamp + '.json')}")

    if args.write_capabilities:
        with psycopg.connect(args.dsn, autocommit=True) as conn:
            for camera_id, entries in by_camera.items():
                cs = [s for s, _ in entries]
                cap = capability(cs, detector_id=entries[0][1], basis=(
                    f"MEVA KPF ground truth, {len(cs)} clip(s), per-box recall at IoU>="
                    f"0.3 on sampled frames"))
                set_capability(conn, camera_id=camera_id, cls="person", value=cap)
                print(f"  {cs[0].camera_key}: person {cap['status']} "
                      f"(recall {cap['frame_recall']}, {cap['gt_boxes']} boxes)")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())

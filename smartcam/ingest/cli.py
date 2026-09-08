"""`smartcam-import` — read a folder of recorded footage and report what it actually holds.

Run before importing anything, because the interesting output is the part a customer disputes:
how much of the window has no footage in it, and how much of the timeline rests on a guessed
timestamp. Both are cheaper to argue about now than after an answer has been given.

    smartcam-import ./footage --tz Asia/Kolkata
    smartcam-import ./footage --convention meva --tz UTC --window 2018-03-07T16:00,2018-03-07T18:00
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from smartcam.ingest.recorded import CONVENTIONS, ConventionError, build_timeline, scan
from smartcam.survey.probe import ffprobe_available


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="smartcam-import", description=__doc__)
    p.add_argument("root", type=Path, help="folder of recorded footage")
    p.add_argument(
        "--tz", default="UTC",
        help="timezone the RECORDER was set to (e.g. Asia/Kolkata). Not the timezone you are in. "
             "Getting this wrong shifts every answer by hours, invisibly.",
    )
    p.add_argument(
        "--convention", choices=sorted(CONVENTIONS),
        help="assert one filename convention instead of trying each. Fails loudly on a file that "
             "does not match, which is what you want on a large import.",
    )
    p.add_argument(
        "--window", metavar="START,END",
        help="the span this import claims to cover, as two ISO timestamps in the recorder's "
             "timezone. Without it, the footage is scored against itself.",
    )
    p.add_argument("--no-measure", action="store_true",
                   help="trust filenames, skip ffprobe. Fast, and blind to truncated clips.")
    return p


def _parse_window(text: str, tz: str):
    try:
        start, _, end = text.partition(",")
        zone = ZoneInfo(tz)
        return (
            datetime.fromisoformat(start.strip()).replace(tzinfo=zone),
            datetime.fromisoformat(end.strip()).replace(tzinfo=zone),
        )
    except ValueError as e:
        raise SystemExit(f"--window must be two ISO timestamps separated by a comma: {e}") from e


def _hms(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}h {s % 3600 // 60:02d}m" if s >= 3600 else f"{s // 60}m {s % 60:02d}s"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.root.is_dir():
        print(f"not a directory: {args.root}", file=sys.stderr)
        return 2
    measure = not args.no_measure
    if measure and not ffprobe_available():
        print("ffprobe not found — install ffmpeg, or pass --no-measure and accept that "
              "truncated clips will be counted as full coverage.", file=sys.stderr)
        return 2

    try:
        clips = scan(args.root, tz=args.tz, convention=args.convention, measure=measure)
    except ConventionError as e:
        print(f"{e}", file=sys.stderr)
        return 3
    if not clips:
        print(f"No video files under {args.root}.")
        return 1

    window = _parse_window(args.window, args.tz) if args.window else None
    timeline = build_timeline(clips, window=window)
    span = timeline.window
    assert span is not None

    print(f"\n{len(clips)} file(s) · {len(timeline.cameras)} camera(s) · recorder timezone "
          f"{args.tz}")
    print(f"{span[0]:%Y-%m-%d %H:%M} to {span[1]:%Y-%m-%d %H:%M} UTC "
          f"· {_hms(timeline.recorded_seconds)} of footage · {timeline.coverage_pct()}% coverage\n")

    print(f"{'camera':<22}{'clips':>7}{'footage':>12}{'gaps':>7}{'missing':>12}")
    print("-" * 60)
    for camera in timeline.cameras:
        own = [c for c in timeline.clips if c.camera_key == camera]
        holes = [g for g in timeline.gaps if g.camera_key == camera]
        print(f"{camera[:21]:<22}{len(own):>7}"
              f"{_hms(sum(c.measured_seconds or 0 for c in own)):>12}"
              f"{len(holes):>7}{_hms(sum(g.seconds for g in holes)):>12}")

    if timeline.gaps:
        print("\nGaps — windows we hold no footage for, and therefore cannot answer about:")
        for gap in timeline.gaps[:20]:
            about = "" if gap.exact else "  (somewhere in this window; the clip is short)"
            print(f"  {gap.camera_key:<20} {gap.starts_at:%d %b %H:%M} → "
                  f"{gap.ends_at:%H:%M}  {_hms(gap.seconds):>9}  {gap.kind}{about}")
        if len(timeline.gaps) > 20:
            print(f"  … and {len(timeline.gaps) - 20} more")

    guessed = [c for c in clips if c.convention in {"mtime", "container"}]
    unreadable = [c for c in clips if c.error]
    if guessed or unreadable or timeline.overlaps:
        print("\nBefore you trust this timeline:")
    if guessed:
        print(f"  · {len(guessed)} clip(s) had no parseable filename, so their position on the "
              f"timeline came from file metadata rather than from the recorder. Treat any answer "
              f"that depends on them as approximate.")
    if unreadable:
        print(f"  · {len(unreadable)} clip(s) could not be decoded: "
              f"{unreadable[0].error}. Counted as gaps, not as silence.")
    if timeline.overlaps:
        print(f"  · {len(timeline.overlaps)} overlapping clip(s) — a duplicated export, or the "
              f"wrong convention matched. Coverage is capped rather than double-counted.")
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

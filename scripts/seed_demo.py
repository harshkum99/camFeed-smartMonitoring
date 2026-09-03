#!/usr/bin/env python3
"""Generate a realistic day at a factory, so the query layer can be built and demoed before a
single camera is connected.

This decouples every downstream milestone from site access and GPU availability, which matters
because DVR credentials are the longest-lead item in the whole project.

The data is deliberately awkward in the ways real sites are awkward, because a query layer that
only works on tidy data is not finished:

  * One camera goes offline mid-shift and never comes back, so `coverage_pct` has something real
    to report and the "no coverage" refusal has a case to fire on.
  * The server room has no camera at all, so there is a question the system must honestly refuse.
  * Helmet confidence is a continuous value with a genuine ambiguous band, not a boolean, so the
    confident/ambiguous split is exercised.
  * A few tracks are fragmented — one person split across several rows — because that, not model
    accuracy, is where counting error actually lives.

Everything is seeded from a fixed constant: two runs produce identical data, so a test can assert
an exact count and a demo shows the same numbers twice.

    python scripts/seed_demo.py --dsn dbname=smartcam_dev --days 3
"""

from __future__ import annotations

import argparse
import random
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg

SEED = 20260902
IST = timedelta(hours=5, minutes=30)

TENANT = uuid.UUID("11111111-1111-1111-1111-111111111111")
SITE = uuid.UUID("aaaaaaaa-0000-0000-0000-000000000001")


@dataclass(frozen=True)
class Cam:
    key: str
    name: str
    grade: str
    lens: str
    w: int
    h: int
    fps: float
    gop: int
    zones: tuple[tuple[str, str], ...]      # (zone name, kind)


# A plausible mid-size plant. Note what is NOT here: the server room. That absence is the point —
# it is what lets the demo include a question the system correctly refuses.
CAMERAS: tuple[Cam, ...] = (
    Cam("gate3",   "Gate 3 — Main Entry",     "recognition", "rectilinear", 1280, 720, 15, 1000,
        (("Gate 3 entry line", "line"), ("Gate 3 approach", "area"))),
    Cam("zoneb",   "Zone B — Press Shop",     "detection",   "rectilinear", 1280, 720, 15, 1000,
        (("Zone B floor", "area"), ("Zone B PPE checkpoint", "line"))),
    Cam("dock",    "Loading Dock A",          "anpr",        "rectilinear", 1920, 1080, 15, 2000,
        (("Dock A bay", "area"), ("Dock A gate", "line"))),
    Cam("fireexit", "Fire Exit — East",       "detection",   "rectilinear", 1280, 720, 15, 1000,
        (("Fire exit clearance", "area"),)),
    Cam("restrict", "Restricted — Chemical Store", "detection", "rectilinear", 1280, 720, 12, 1500,
        (("Chemical store", "area"),)),
    Cam("yard",    "Yard — North",            "detection",   "fisheye",     1280, 720, 10, 1000,
        (("Yard north", "area"),)),
    Cam("corridor", "Corridor — Admin Block", "detection",   "rectilinear",  704, 576, 12, 4200,
        (("Corridor", "area"),)),
    # This one dies at 14:00 on day 0 and never returns.
    Cam("canteen", "Canteen Entrance",        "detection",   "rectilinear", 1280, 720, 15, 1000,
        (("Canteen door", "line"),)),
)

UPPER_COLOURS = ("blue", "grey", "orange", "white", "navy", "green")


def ist(day: datetime, hour: int, minute: int = 0) -> datetime:
    """A wall-clock time at the site, stored as UTC. Timestamps are carried end-to-end in UTC
    with the site's timezone applied at the edges; mixing the two is how a system confidently
    places someone at a gate on the wrong day."""
    return (day.replace(hour=0, minute=0, second=0, microsecond=0)
            + timedelta(hours=hour, minutes=minute) - IST)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dsn", default="dbname=smartcam_dev")
    ap.add_argument("--days", type=int, default=3)
    ap.add_argument("--start", default="2026-09-01", help="first day, YYYY-MM-DD (site local)")
    args = ap.parse_args()

    rng = random.Random(SEED)
    day0 = datetime.fromisoformat(args.start).replace(tzinfo=UTC)

    with psycopg.connect(args.dsn) as conn, conn.cursor() as cur:
        _reset(cur)
        cam_ids, zone_ids = _cameras(cur)
        _uptime(cur, cam_ids, day0, args.days)

        tracks: list[tuple] = []
        events: list[tuple] = []
        clips: list[tuple] = []

        for d in range(args.days):
            day = day0 + timedelta(days=d)
            _shift_change(rng, day, cam_ids, zone_ids, tracks, events)
            _press_shop(rng, day, cam_ids, zone_ids, tracks, events)
            _loading_dock(rng, day, cam_ids, zone_ids, tracks, events)
            _fire_exit(rng, day, d, cam_ids, zone_ids, tracks, events, clips)
            _after_hours(rng, day, d, cam_ids, zone_ids, tracks, events)

        _insert_tracks(cur, tracks)
        _insert_events(cur, events)
        _insert_clips(cur, clips)
        conn.commit()

        cur.execute("SELECT count(*) FROM tracks")
        n_tracks = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM zone_events")
        n_events = cur.fetchone()[0]

    print(f"seeded {args.days} day(s): {len(CAMERAS)} cameras, {n_tracks} tracks, "
          f"{n_events} zone events, {len(clips)} clips")
    print("note: no camera covers the server room, and 'Canteen Entrance' dies at 14:00 on day 1 "
          "— both are deliberate, so the refusal paths have something real to fire on")
    return 0


def _reset(cur) -> None:
    cur.execute("DELETE FROM zone_events WHERE tenant_id = %s", (TENANT,))
    cur.execute("DELETE FROM clips WHERE tenant_id = %s", (TENANT,))
    cur.execute("DELETE FROM tracks WHERE tenant_id = %s", (TENANT,))
    cur.execute("DELETE FROM camera_uptime WHERE site_id = %s", (SITE,))
    cur.execute("DELETE FROM zones WHERE site_id = %s", (SITE,))
    cur.execute("DELETE FROM cameras WHERE site_id = %s", (SITE,))
    cur.execute("DELETE FROM sites WHERE site_id = %s", (SITE,))
    cur.execute("DELETE FROM tenants WHERE tenant_id = %s", (TENANT,))

    cur.execute(
        "INSERT INTO tenants (tenant_id, name, legal_basis, face_enabled, retention_days) "
        "VALUES (%s, %s, 'employment', true, 365)",
        (TENANT, "Acme Manufacturing Pvt Ltd"),
    )
    cur.execute(
        "INSERT INTO sites (site_id, tenant_id, name, tz) VALUES (%s, %s, %s, 'Asia/Kolkata')",
        (SITE, TENANT, "Plant 2, Bhiwadi"),
    )


def _cameras(cur) -> tuple[dict[str, uuid.UUID], dict[str, uuid.UUID]]:
    cam_ids: dict[str, uuid.UUID] = {}
    zone_ids: dict[str, uuid.UUID] = {}
    for i, c in enumerate(CAMERAS):
        cid = uuid.UUID(f"cccccccc-0000-0000-0000-{i + 1:012d}")
        cam_ids[c.key] = cid
        cur.execute(
            "INSERT INTO cameras (camera_id, tenant_id, site_id, name, grade, lens, "
            "detect_w, detect_h, detect_fps, codec, gop_ms, make, model) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'h264',%s,'CP Plus','CP-UNC-TA21PL3C')",
            (cid, TENANT, SITE, c.name, c.grade, c.lens, c.w, c.h, c.fps, c.gop),
        )
        for j, (zname, kind) in enumerate(c.zones):
            zid = uuid.UUID(f"22220000-0000-0000-{i + 1:04d}-{j + 1:012d}")
            zone_ids[zname] = zid
            cur.execute(
                "INSERT INTO zones (zone_id, camera_id, site_id, name, kind, polygon) "
                "VALUES (%s,%s,%s,%s,%s,%s)",
                (zid, cid, SITE, zname, kind,
                 '[[0.1,0.4],[0.9,0.4],[0.9,0.95],[0.1,0.95]]'),
            )
    return cam_ids, zone_ids


def _uptime(cur, cam_ids, day0: datetime, days: int) -> None:
    """Every camera online for the window, except the canteen camera which dies at 14:00 on day 1
    and never returns. That single gap is what makes `coverage_pct` and the "no coverage" refusal
    testable against something real."""
    start = ist(day0, 0)
    end = ist(day0 + timedelta(days=days), 0)
    death = ist(day0, 14)

    for key, cid in cam_ids.items():
        if key == "canteen":
            cur.execute(
                "INSERT INTO camera_uptime (camera_id, site_id, ts_start, ts_end, state) "
                "VALUES (%s,%s,%s,%s,'online')", (cid, SITE, start, death))
            cur.execute(
                "INSERT INTO camera_uptime (camera_id, site_id, ts_start, ts_end, state, detail) "
                "VALUES (%s,%s,%s,%s,'offline','stream unreachable — PoE switch port down')",
                (cid, SITE, death, end))
        else:
            cur.execute(
                "INSERT INTO camera_uptime (camera_id, site_id, ts_start, ts_end, state) "
                "VALUES (%s,%s,%s,%s,'online')", (cid, SITE, start, end))


def _track(rng, cam_id, cls, t0, dur_s, attrs, conf=None) -> tuple:
    conf = conf if conf is not None else round(rng.uniform(0.62, 0.96), 3)
    return (
        uuid.uuid4(), TENANT, SITE, cam_id, cls, t0, t0 + timedelta(seconds=dur_s),
        float(dur_s), conf, round(conf - rng.uniform(0.02, 0.10), 3),
        int(dur_s * 5), psycopg.types.json.Jsonb(attrs),
        f"s3://smartcam-frames/{cam_id}/{int(t0.timestamp())}.webp",
        f"{uuid.uuid4().hex}{uuid.uuid4().hex}"[:64],
        psycopg.types.json.Jsonb({"detector": "d-fine-s@2026.09", "reid": "clip-reid@2026.09"}),
    )


def _event(cam_id, zone_id, track_id, etype, ts, conf, attrs=None) -> tuple:
    return (uuid.uuid4(), TENANT, SITE, cam_id, zone_id, track_id, etype, ts, conf,
            psycopg.types.json.Jsonb(attrs or {}),
            f"s3://smartcam-frames/{cam_id}/{int(ts.timestamp())}_ev.webp")


def _shift_change(rng, day, cams, zones, tracks, events) -> None:
    """Morning shift arriving through Gate 3. A handful of tracks are deliberately fragmented —
    one person emitted as two rows — because occlusion does that constantly, and it is the real
    source of counting error."""
    cam, zone = cams["gate3"], zones["Gate 3 entry line"]
    for i in range(48):
        t0 = ist(day, 8, 0) + timedelta(seconds=rng.randint(0, 2400))
        tr = _track(rng, cam, "person", t0, rng.randint(4, 14),
                    {"upper_colour": rng.choice(UPPER_COLOURS),
                     "helmet": round(rng.uniform(0.7, 0.98), 2)})
        tracks.append(tr)
        events.append(_event(cam, zone, tr[0], "cross_pos", t0 + timedelta(seconds=2), tr[8]))
        # ~1 in 8 fragments into a second track a few seconds later.
        if i % 8 == 0:
            t1 = t0 + timedelta(seconds=rng.randint(3, 9))
            tr2 = _track(rng, cam, "person", t1, rng.randint(3, 8),
                         {"upper_colour": tr[11].obj["upper_colour"], "fragment_of": str(tr[0])})
            tracks.append(tr2)
            events.append(_event(cam, zone, tr2[0], "cross_pos", t1, tr2[8]))


def _press_shop(rng, day, cams, zones, tracks, events) -> None:
    """Zone B, where the helmet rule lives. Confidence is continuous with a real ambiguous band
    (0.4-0.7): a worker facing away is not a worker without a helmet, and the answer layer has to
    be able to say so rather than assert a violation."""
    cam = cams["zoneb"]
    floor, checkpoint = zones["Zone B floor"], zones["Zone B PPE checkpoint"]
    for _ in range(64):
        t0 = ist(day, 9, 0) + timedelta(seconds=rng.randint(0, 8 * 3600))
        roll = rng.random()
        if roll < 0.72:
            helmet = round(rng.uniform(0.82, 0.99), 2)      # clearly wearing one
        elif roll < 0.88:
            helmet = round(rng.uniform(0.40, 0.70), 2)      # genuinely ambiguous
        else:
            helmet = round(rng.uniform(0.02, 0.30), 2)      # clearly not
        tr = _track(rng, cam, "person", t0, rng.randint(20, 400),
                    {"helmet": helmet, "vest": round(rng.uniform(0.5, 0.98), 2),
                     "upper_colour": rng.choice(UPPER_COLOURS)})
        tracks.append(tr)
        events.append(_event(cam, checkpoint, tr[0], "cross_pos", t0, tr[8], {"helmet": helmet}))
        events.append(_event(cam, floor, tr[0], "enter", t0 + timedelta(seconds=3), tr[8],
                             {"helmet": helmet}))


def _loading_dock(rng, day, cams, zones, tracks, events) -> None:
    cam, bay, gate = cams["dock"], zones["Dock A bay"], zones["Dock A gate"]
    for _ in range(11):
        t0 = ist(day, 7, 0) + timedelta(seconds=rng.randint(0, 11 * 3600))
        dwell = rng.randint(600, 4200)
        tr = _track(rng, cam, "vehicle", t0, dwell,
                    {"vehicle_type": rng.choice(("truck", "tempo", "forklift")),
                     "plate_text": f"RJ{rng.randint(10, 45)}{rng.choice('ABCGH')}"
                                   f"{rng.choice('ABPQR')}{rng.randint(1000, 9999)}"})
        tracks.append(tr)
        events.append(_event(cam, gate, tr[0], "cross_pos", t0, tr[8]))
        events.append(_event(cam, bay, tr[0], "dwell", t0 + timedelta(seconds=30), tr[8]))


def _fire_exit(rng, day, d, cams, zones, tracks, events, clips) -> None:
    """The demo question: 'was the fire exit blocked at any point today?'

    Blocked only on day 1, by a pallet left for 47 minutes. Answering it correctly on the other
    days matters as much as catching it on day 1 — a system that always finds something is as
    useless as one that never does.
    """
    cam, zone = cams["fireexit"], zones["Fire exit clearance"]
    if d == 1:
        t0 = ist(day, 11, 12)
        tr = _track(rng, cam, "pallet", t0, 47 * 60, {"static": True}, conf=0.88)
        tracks.append(tr)
        events.append(_event(cam, zone, tr[0], "stationary", t0, 0.88, {"obstruction": True}))
        clips.append((uuid.uuid4(), TENANT, SITE, cam, t0, t0 + timedelta(seconds=10),
                      "A wooden pallet loaded with cartons is left directly in front of the "
                      "east fire exit, partially blocking the doorway. No person is present.",
                      "alert"))
    else:
        t0 = ist(day, 15, 30)
        clips.append((uuid.uuid4(), TENANT, SITE, cam, t0, t0 + timedelta(seconds=10),
                      "The east fire exit doorway is clear and unobstructed. A worker walks "
                      "past without stopping.", "sampled"))


def _after_hours(rng, day, d, cams, zones, tracks, events) -> None:
    """Restricted-zone entry after 20:00 — the alert rule. Happens on day 2 only."""
    if d != 2:
        return
    cam, zone = cams["restrict"], zones["Chemical store"]
    t0 = ist(day, 21, 47)
    tr = _track(rng, cam, "person", t0, 214,
                {"upper_colour": "grey", "helmet": 0.12}, conf=0.91)
    tracks.append(tr)
    events.append(_event(cam, zone, tr[0], "enter", t0, 0.91))
    events.append(_event(cam, zone, tr[0], "loiter", t0 + timedelta(seconds=60), 0.91))


def _insert_tracks(cur, rows) -> None:
    cur.executemany(
        "INSERT INTO tracks (track_id, tenant_id, site_id, camera_id, class, ts_start, ts_end, "
        "dwell_s, conf_max, conf_mean, n_frames, attrs, best_keyframe_uri, frame_sha256, "
        "model_versions) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)


def _insert_events(cur, rows) -> None:
    cur.executemany(
        "INSERT INTO zone_events (event_id, tenant_id, site_id, camera_id, zone_id, track_id, "
        "type, ts, conf, attrs, evidence_uri) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)", rows)


def _insert_clips(cur, rows) -> None:
    cur.executemany(
        "INSERT INTO clips (clip_id, tenant_id, site_id, camera_id, ts_start, ts_end, caption, "
        "retained_reason, vlm_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'gemini-3-flash@2026.09')",
        rows)


if __name__ == "__main__":
    raise SystemExit(main())

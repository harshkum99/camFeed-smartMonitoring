-- Smart Cam Monitoring — what the first real, recorded footage needs from the schema.
--
-- Until now every row in the index was synthetic. Recorded footage breaks four assumptions the
-- synthetic data never tested, and each gets the smallest change that makes it honest:
--
--   1. "Now" is not the wall clock. A recording from 2018 answered "as of today" has 0% coverage
--      for every question anyone would ask about it. A recorded site carries `as_of`: the end of
--      its latest analysed footage, which is what relative words like "this morning" mean there.
--   2. A camera can be online and still blind to a class. A 1080p camera watching a car park from
--      60 m sees people 40 px tall, and measured person recall there is 0.05. Uptime cannot say
--      that; `capabilities` can, per class, with the measurement it rests on.
--   3. Evidence needs a time and a place in the frame. The frame shown is the track's best frame,
--      not its first, so it needs its own timestamp and its own box.
--   4. An imported clip must be traceable to the exact source file, and re-importable without
--      leaving duplicates behind.

-- ---------------------------------------------------------------------------
-- 1. Replay time and attribution
-- ---------------------------------------------------------------------------

ALTER TABLE sites
    ADD COLUMN IF NOT EXISTS as_of             TIMESTAMPTZ,
    -- Licensed footage (MEVA is CC BY 4.0) must carry its attribution wherever it is shown.
    ADD COLUMN IF NOT EXISTS data_attribution  TEXT,
    ADD COLUMN IF NOT EXISTS example_questions TEXT[] NOT NULL DEFAULT '{}';

-- ---------------------------------------------------------------------------
-- 2. What each camera can actually detect
--
-- Shape: {"detector": "<model id>",
--         "classes": {"person": {"status": "reliable|limited|unreliable|not_assessed",
--                                "frame_recall": 0.70, "basis": "...", "assessed_at": "..."}}}
-- A class absent from "classes" was never detected on this camera at all. That is a different
-- statement from "detected badly", and the answer layer treats the two differently.
-- ---------------------------------------------------------------------------

ALTER TABLE cameras
    ADD COLUMN IF NOT EXISTS capabilities JSONB  NOT NULL DEFAULT '{}'::jsonb,
    ADD COLUMN IF NOT EXISTS grade_notes  TEXT[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS graded_from  TEXT
        CHECK (graded_from IN ('live_probe', 'recording', 'manual'));

-- ---------------------------------------------------------------------------
-- 3. Evidence time and position
-- ---------------------------------------------------------------------------

ALTER TABLE tracks
    ADD COLUMN IF NOT EXISTS keyframe_ts   TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS keyframe_bbox JSONB;

-- ---------------------------------------------------------------------------
-- 4. Provenance and re-import
--
-- `source_sha256` is the evidentiary anchor for recorded footage: the hash of the recorder's own
-- file. The keyframe hash in `tracks.frame_sha256` is the hash of a JPEG we derived from it,
-- which proves the served image is unchanged since ingest and nothing more.
-- ---------------------------------------------------------------------------

ALTER TABLE clips
    ADD COLUMN IF NOT EXISTS source_uri    TEXT,
    ADD COLUMN IF NOT EXISTS source_sha256 TEXT
        CHECK (source_sha256 IS NULL OR source_sha256 ~ '^[0-9a-f]{64}$');

ALTER TABLE camera_uptime
    ADD COLUMN IF NOT EXISTS tenant_id UUID REFERENCES tenants ON DELETE CASCADE,
    ADD COLUMN IF NOT EXISTS source    TEXT NOT NULL DEFAULT 'supervisor'
        CHECK (source IN ('supervisor', 'recorded_import')),
    -- The clip an imported uptime row came from, so a re-import replaces exactly its own rows
    -- instead of guessing from timestamps that a parser change could move.
    ADD COLUMN IF NOT EXISTS clip_id   UUID;

UPDATE camera_uptime u SET tenant_id = c.tenant_id
  FROM cameras c WHERE c.camera_id = u.camera_id AND u.tenant_id IS NULL;

-- tenant_id and site_id are derived from the camera, and a writer that names the wrong ones is
-- refused. Filling rather than requiring keeps every existing writer working unchanged, and the
-- NOT NULL invariant from 001 finally holds for this table too.
CREATE OR REPLACE FUNCTION enforce_camera_scope() RETURNS trigger AS $$
DECLARE
    cam_tenant UUID;
    cam_site   UUID;
BEGIN
    SELECT tenant_id, site_id INTO cam_tenant, cam_site
      FROM cameras WHERE camera_id = NEW.camera_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION '% row names camera %, which does not exist', TG_TABLE_NAME, NEW.camera_id;
    END IF;
    NEW.tenant_id := COALESCE(NEW.tenant_id, cam_tenant);
    NEW.site_id   := COALESCE(NEW.site_id, cam_site);
    IF NEW.tenant_id <> cam_tenant OR NEW.site_id <> cam_site THEN
        RAISE EXCEPTION '% row for camera % names tenant %/site %, but the camera belongs to %/%',
            TG_TABLE_NAME, NEW.camera_id, NEW.tenant_id, NEW.site_id, cam_tenant, cam_site;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS camera_uptime_scope ON camera_uptime;
CREATE TRIGGER camera_uptime_scope BEFORE INSERT OR UPDATE ON camera_uptime
    FOR EACH ROW EXECUTE FUNCTION enforce_camera_scope();

ALTER TABLE camera_uptime ALTER COLUMN tenant_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS camera_uptime_clip_idx ON camera_uptime (clip_id) WHERE clip_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Coverage merges overlapping intervals.
--
-- The 003 version summed online seconds row by row and capped the site total at 100%, while
-- coverage_gaps merged overlaps. Two overlapping rows on one camera could therefore hide another
-- camera's outage in the percentage while the gap list still reported it: the two functions
-- disagreed about the same footage. Merging here, with the same running maximum coverage_gaps
-- uses, makes that disagreement impossible. Accumulators are double precision because a REAL
-- loses whole seconds over a multi-camera month.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION coverage_pct(
    p_cameras UUID[],
    p_start   TIMESTAMPTZ,
    p_end     TIMESTAMPTZ
) RETURNS REAL AS $$
DECLARE
    total_s   DOUBLE PRECISION;
    online_s  DOUBLE PRECISION;
    n_cameras INT;
BEGIN
    n_cameras := COALESCE(array_length(p_cameras, 1), 0);
    IF n_cameras = 0 OR p_end <= p_start THEN
        RETURN 0.0;
    END IF;

    total_s := EXTRACT(EPOCH FROM (p_end - p_start)) * n_cameras;

    WITH ivals AS (
        SELECT u.camera_id AS cam,
               GREATEST(u.ts_start, p_start)           AS s,
               LEAST(COALESCE(u.ts_end, p_end), p_end) AS e
        FROM camera_uptime u
        WHERE u.camera_id = ANY(p_cameras)
          AND u.state = 'online'
          AND u.ts_start < p_end
          AND COALESCE(u.ts_end, p_end) > p_start
    ),
    running AS (
        SELECT s, e,
               MAX(e) OVER (PARTITION BY cam ORDER BY s, e
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_e
        FROM ivals
    )
    SELECT COALESCE(SUM(GREATEST(0, EXTRACT(EPOCH FROM (e - GREATEST(s, COALESCE(prev_e, s)))))), 0)
      INTO online_s
      FROM running;

    RETURN LEAST(1.0, GREATEST(0.0, online_s / NULLIF(total_s, 0)))::REAL;
END;
$$ LANGUAGE plpgsql STABLE;

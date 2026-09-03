-- Smart Cam Monitoring — partition management, vector indexes, and the coverage function.

-- ---------------------------------------------------------------------------
-- Monthly partitions.
--
-- Native declarative partitioning, not TimescaleDB. It carries us well past PoC scale, keeps
-- the on-prem/air-gapped SKU to a stock Postgres (one fewer extension for a bank's security
-- review to object to), and retention becomes DROP PARTITION — instant, and it actually
-- reclaims disk, unlike DELETE.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION ensure_partition(tbl TEXT, month_start DATE)
RETURNS TEXT AS $$
DECLARE
    part_name TEXT;
    month_end DATE;
BEGIN
    month_end := (month_start + INTERVAL '1 month')::DATE;
    part_name := format('%s_%s', tbl, to_char(month_start, 'YYYYMM'));

    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = part_name) THEN
        EXECUTE format(
            'CREATE TABLE %I PARTITION OF %I FOR VALUES FROM (%L) TO (%L)',
            part_name, tbl, month_start, month_end
        );
    END IF;
    RETURN part_name;
END;
$$ LANGUAGE plpgsql;

-- Create partitions spanning a range. Called by the scheduler a month ahead; an INSERT into a
-- missing partition is a hard error, and discovering that at midnight on the 1st is avoidable.
CREATE OR REPLACE FUNCTION ensure_partitions(from_month DATE, to_month DATE)
RETURNS INT AS $$
DECLARE
    m DATE := date_trunc('month', from_month)::DATE;
    n INT := 0;
    tbl TEXT;
BEGIN
    WHILE m <= to_month LOOP
        FOREACH tbl IN ARRAY ARRAY['tracks', 'zone_events', 'clips', 'alerts', 'external_events'] LOOP
            PERFORM ensure_partition(tbl, m);
            n := n + 1;
        END LOOP;
        m := (m + INTERVAL '1 month')::DATE;
    END LOOP;
    RETURN n;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Vector indexes.
--
-- HNSW on the partitioned parent is not supported, so indexes are created per partition by the
-- same helper. At PoC scale (single-digit millions of vectors) this is comfortably fast.
--
-- IMPORTANT: every query MUST pre-filter on (site_id, camera_id, ts) before the ANN step.
-- Unfiltered ANN benchmarks are irrelevant to us — 100% of our queries are filtered by camera
-- and time, and pgvector-HNSW falls back to a brute-force scan under tight filters, spiking p95
-- to 200-300ms. When that becomes the bottleneck the answer is Qdrant with shard keys on
-- (site, month), so camera+time becomes shard SELECTION rather than filtering.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION ensure_vector_indexes(month_start DATE)
RETURNS VOID AS $$
DECLARE
    suffix TEXT := to_char(month_start, 'YYYYMM');
BEGIN
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'tracks_' || suffix) THEN
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I USING hnsw (appearance_vec vector_cosine_ops)',
            'tracks_' || suffix || '_appearance_hnsw', 'tracks_' || suffix
        );
    END IF;
    IF EXISTS (SELECT 1 FROM pg_class WHERE relname = 'clips_' || suffix) THEN
        EXECUTE format(
            'CREATE INDEX IF NOT EXISTS %I ON %I USING hnsw (caption_vec vector_cosine_ops)',
            'clips_' || suffix || '_caption_hnsw', 'clips_' || suffix
        );
    END IF;
END;
$$ LANGUAGE plpgsql;

-- ---------------------------------------------------------------------------
-- Coverage.
--
-- The single most important function in the schema. Every answer we give is qualified by it.
-- Without it, "no events found" and "the camera was offline" are indistinguishable to the user,
-- and we deliver a false negative as a fact.
--
-- Returns the fraction of the requested window during which the requested cameras were online.
-- A camera with NO uptime rows at all is treated as 0% covered, not 100% — absence of evidence
-- is not evidence of uptime.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION coverage_pct(
    p_cameras UUID[],
    p_start   TIMESTAMPTZ,
    p_end     TIMESTAMPTZ
) RETURNS REAL AS $$
DECLARE
    total_s   REAL;
    online_s  REAL;
    n_cameras INT;
BEGIN
    n_cameras := COALESCE(array_length(p_cameras, 1), 0);
    IF n_cameras = 0 OR p_end <= p_start THEN
        RETURN 0.0;
    END IF;

    total_s := EXTRACT(EPOCH FROM (p_end - p_start)) * n_cameras;

    SELECT COALESCE(SUM(
        EXTRACT(EPOCH FROM (
            LEAST(COALESCE(u.ts_end, p_end), p_end) - GREATEST(u.ts_start, p_start)
        ))
    ), 0)
    INTO online_s
    FROM camera_uptime u
    WHERE u.camera_id = ANY(p_cameras)
      AND u.state = 'online'
      AND u.ts_start < p_end
      AND COALESCE(u.ts_end, p_end) > p_start;

    RETURN LEAST(1.0, GREATEST(0.0, online_s / NULLIF(total_s, 0)))::REAL;
END;
$$ LANGUAGE plpgsql STABLE;

-- Which cameras had gaps, and when. Powers the "no coverage" refusal, which must name the
-- camera and the missing minutes rather than shrugging.
-- Three distinct kinds of gap, and the obvious implementation misses two of them:
--   (a) before an online interval,
--   (b) AFTER the last online interval — a camera that dies mid-window and never returns,
--       which is precisely the case that matters operationally,
--   (c) a camera with no uptime rows at all — the whole window is a gap.
-- Overlapping intervals are merged via a running max, so duplicate or nested uptime rows
-- (which the supervisor can legitimately produce on a flapping link) do not invent gaps.
CREATE OR REPLACE FUNCTION coverage_gaps(
    p_cameras UUID[],
    p_start   TIMESTAMPTZ,
    p_end     TIMESTAMPTZ
) RETURNS TABLE (camera_id UUID, camera_name TEXT, gap_start TIMESTAMPTZ, gap_end TIMESTAMPTZ) AS $$
    WITH ivals AS (
        SELECT c.camera_id AS cam, c.name AS nm,
               GREATEST(u.ts_start, p_start)             AS s,
               LEAST(COALESCE(u.ts_end, p_end), p_end)   AS e
        FROM cameras c
        JOIN camera_uptime u
          ON u.camera_id = c.camera_id
         AND u.state = 'online'
         AND u.ts_start < p_end
         AND COALESCE(u.ts_end, p_end) > p_start
        WHERE c.camera_id = ANY(p_cameras)
    ),
    running AS (
        SELECT cam, nm, s, e,
               MAX(e) OVER (PARTITION BY cam ORDER BY s
                            ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS prev_max_e
        FROM ivals
    )
    -- (a) gaps preceding an online interval
    SELECT cam, nm, COALESCE(prev_max_e, p_start), s
    FROM running
    WHERE s > COALESCE(prev_max_e, p_start)
    UNION ALL
    -- (b) trailing gap after the last online interval
    SELECT cam, nm, MAX(e), p_end
    FROM ivals GROUP BY cam, nm HAVING MAX(e) < p_end
    UNION ALL
    -- (c) never online at all during the window
    SELECT c.camera_id, c.name, p_start, p_end
    FROM cameras c
    WHERE c.camera_id = ANY(p_cameras)
      AND NOT EXISTS (SELECT 1 FROM ivals i WHERE i.cam = c.camera_id)
    ORDER BY 1, 3
$$ LANGUAGE sql STABLE;

-- ---------------------------------------------------------------------------
-- Retention. DROP PARTITION, not DELETE.
--
-- Retention is configurable per tenant but has a FLOOR, never only a ceiling:
--   - DPDP Rule 6(1)(e) / Rule 8(3): processing and access logs, minimum 365 days
--   - CERT-In direction (iv): minimum 180 days, held within Indian jurisdiction
--   - Paramvir Singh Saini (SC 2020): 18 months for police-station deployments
-- The application refuses to configure a tenant below its floor.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION drop_partitions_before(tbl TEXT, cutoff DATE)
RETURNS TABLE (dropped TEXT) AS $$
DECLARE
    r RECORD;
BEGIN
    FOR r IN
        SELECT c.relname
        FROM pg_class c
        JOIN pg_inherits i ON i.inhrelid = c.oid
        JOIN pg_class p ON p.oid = i.inhparent
        WHERE p.relname = tbl
          AND c.relname ~ '_[0-9]{6}$'
          AND to_date(right(c.relname, 6), 'YYYYMM') < date_trunc('month', cutoff)
    LOOP
        EXECUTE format('DROP TABLE %I', r.relname);
        dropped := r.relname;
        RETURN NEXT;
    END LOOP;
END;
$$ LANGUAGE plpgsql;

-- Bootstrap: current month plus three ahead.
SELECT ensure_partitions(CURRENT_DATE, (CURRENT_DATE + INTERVAL '3 months')::DATE);
SELECT ensure_vector_indexes(date_trunc('month', CURRENT_DATE)::DATE);

-- Project Sanjay — core schema
--
-- TWO RULES ENFORCED HERE. Both are near-impossible to retrofit; violating either means
-- reprocessing the entire corpus.
--
--   1. We store TRACKS, not per-frame detections. Per-object rows at 5 fps across 1000 cameras
--      is ~1.3 billion rows/day. Track-level is ~5 million. A 250x difference.
--
--   2. tenant_id and site_id are NOT NULL on every table, from migration 001. Retrofitting a
--      tenant column across a partitioned event store is a rewrite, not a migration.
--
-- Targets Postgres 16 + pgvector. TimescaleDB is optional (see 003) — native declarative
-- partitioning carries us to well past PoC scale without it.

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS vector;

-- ---------------------------------------------------------------------------
-- Tenancy
-- ---------------------------------------------------------------------------

CREATE TABLE tenants (
    tenant_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    name            TEXT        NOT NULL,
    -- Legal basis under DPDP. 'employment' = s.7(i), consent-free, and the ONLY basis under
    -- which face recognition is lawful for us today. A tenant whose basis is not 'employment'
    -- cannot enable biometric features; the API enforces this, not a policy PDF.
    legal_basis     TEXT        NOT NULL DEFAULT 'employment'
                    CHECK (legal_basis IN ('employment', 'consent', 'public_interest')),
    -- Hard blocks. Both default to the safe value.
    face_enabled    BOOLEAN     NOT NULL DEFAULT FALSE,
    is_school       BOOLEAN     NOT NULL DEFAULT FALSE,  -- DPDP s.9(3): absolute ban on tracking
                                                          -- children, no consent cure, Rs200cr ceiling
    retention_days  INT         NOT NULL DEFAULT 365,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE sites (
    site_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    name            TEXT        NOT NULL,
    tz              TEXT        NOT NULL DEFAULT 'Asia/Kolkata',
    floor_plan_uri  TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON sites (tenant_id);

-- ---------------------------------------------------------------------------
-- Cameras
--
-- `grade` is the output of the site survey and it is load-bearing commercially: it is how we
-- tell a customer which of their cameras can support which capability, instead of promising
-- everything everywhere and failing. Face recognition needs >=64px between eye centres, which
-- is typically only 5-15% of an estate.
-- ---------------------------------------------------------------------------

CREATE TYPE camera_grade AS ENUM ('recognition', 'detection', 'anpr', 'degraded', 'unservable');
CREATE TYPE lens_type    AS ENUM ('rectilinear', 'fisheye', 'ptz');

CREATE TABLE cameras (
    camera_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    site_id         UUID        NOT NULL REFERENCES sites   ON DELETE CASCADE,
    name            TEXT        NOT NULL,

    -- Device identity. Scraped over ONVIF at onboarding; all of it is required by the
    -- Bharatiya Sakshya Adhiniyam s.63 Schedule certificate, so we capture it up front
    -- rather than chasing it when a customer needs an evidence pack.
    make            TEXT,
    model           TEXT,
    serial_no       TEXT,
    mac             TEXT,
    recorder_id     TEXT,                       -- which DVR/NVR fronts this channel
    channel_no      INT,

    -- Survey output
    grade           camera_grade NOT NULL DEFAULT 'detection',
    lens            lens_type    NOT NULL DEFAULT 'rectilinear',
    detect_w        INT,
    detect_h        INT,
    detect_fps      REAL,
    codec           TEXT,
    gop_ms          INT,                        -- MEASURED, not claimed. >2000ms wrecks alert latency.
    face_px_p50     REAL,                       -- median inter-pupillary distance observed
    substream_ok    BOOLEAN,

    -- Clock health. Three clocks must be reconciled: ours, the DVR's on-screen burn-in, and the
    -- DVR's recording index. ONVIF GetSystemDateAndTime is PRE_AUTH so we can measure the offset
    -- before we even have credentials.
    clock_offset_ms BIGINT,
    clock_checked_at TIMESTAMPTZ,

    zone_map        JSONB       NOT NULL DEFAULT '{}'::jsonb,
    ptz_preset      TEXT,                       -- rules bind to a PTZ PRESET, never to the camera
    enabled         BOOLEAN     NOT NULL DEFAULT TRUE,
    -- Hard-excluded areas. IT Act s.66E is a criminal provision with 3-year exposure and no
    -- data-protection defence. "Auto-discover and ingest every camera on the LAN" is the most
    -- dangerous sentence in the pitch deck; classification is a mandatory onboarding step.
    excluded        BOOLEAN     NOT NULL DEFAULT FALSE,
    excluded_reason TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON cameras (site_id);
CREATE INDEX ON cameras (tenant_id, site_id) WHERE enabled AND NOT excluded;

-- Camera adjacency, hand-drawn at commissioning (15 minutes for 12 cameras). This does more for
-- Track & Trace precision than any model swap: a candidate hit is only shown if it is physically
-- reachable in the elapsed time.
CREATE TABLE camera_topology (
    site_id         UUID NOT NULL REFERENCES sites ON DELETE CASCADE,
    from_camera     UUID NOT NULL REFERENCES cameras ON DELETE CASCADE,
    to_camera       UUID NOT NULL REFERENCES cameras ON DELETE CASCADE,
    min_transit_s   REAL NOT NULL,
    max_transit_s   REAL NOT NULL,
    PRIMARY KEY (from_camera, to_camera)
);

CREATE TABLE zones (
    zone_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    camera_id   UUID NOT NULL REFERENCES cameras ON DELETE CASCADE,
    site_id     UUID NOT NULL REFERENCES sites   ON DELETE CASCADE,
    name        TEXT NOT NULL,
    kind        TEXT NOT NULL DEFAULT 'area' CHECK (kind IN ('area', 'line', 'door')),
    -- Normalised 0.0-1.0 coordinates so zones survive a resolution change.
    polygon     JSONB NOT NULL,
    direction   TEXT CHECK (direction IN ('into', 'out_of', 'any'))   -- lines only
);
CREATE INDEX ON zones (camera_id);

-- ---------------------------------------------------------------------------
-- Coverage — the "I don't know" table
--
-- This is the liability shield, not a nice-to-have. Without it the system confidently answers
-- "no events found" when a camera was simply offline, and we have delivered a false negative as
-- a fact to a customer. Every answer carries a coverage percentage derived from this table.
-- The RTSP supervisor writes it; the supervisor IS the coverage feature.
-- ---------------------------------------------------------------------------

CREATE TYPE camera_state AS ENUM ('online', 'offline', 'degraded', 'obscured', 'off_preset');

CREATE TABLE camera_uptime (
    camera_id   UUID        NOT NULL REFERENCES cameras ON DELETE CASCADE,
    site_id     UUID        NOT NULL REFERENCES sites   ON DELETE CASCADE,
    ts_start    TIMESTAMPTZ NOT NULL,
    ts_end      TIMESTAMPTZ,
    state       camera_state NOT NULL,
    detail      TEXT,
    PRIMARY KEY (camera_id, ts_start)
);
CREATE INDEX ON camera_uptime (site_id, ts_start DESC);

-- ---------------------------------------------------------------------------
-- Tracks — the workhorse. ONE ROW PER TRACKED OBJECT, never per frame.
-- ---------------------------------------------------------------------------

CREATE TABLE tracks (
    track_id            UUID        NOT NULL DEFAULT uuid_generate_v4(),
    tenant_id           UUID        NOT NULL,
    site_id             UUID        NOT NULL,
    camera_id           UUID        NOT NULL,

    class               TEXT        NOT NULL,          -- person | vehicle | bag | forklift | ...
    ts_start            TIMESTAMPTZ NOT NULL,
    ts_end              TIMESTAMPTZ NOT NULL,
    dwell_s             REAL        NOT NULL DEFAULT 0,

    conf_max            REAL        NOT NULL,
    conf_mean           REAL        NOT NULL,
    n_frames            INT         NOT NULL,

    bbox_first          JSONB,
    bbox_last           JSONB,
    path_simplified     JSONB,                          -- Douglas-Peucker'd

    attrs               JSONB       NOT NULL DEFAULT '{}'::jsonb,
                        -- {helmet: 0.93, vest: 0.11, upper_colour: "blue"}

    appearance_vec      vector(512),                    -- ReID, aggregated over the sharpest crops
                                                        -- per TRACK, never per frame: 10-30x cheaper
                                                        -- AND more accurate

    -- Cross-camera identity link. NULLABLE ON PURPOSE — an unlinked track is the honest default,
    -- and identity_conf is always surfaced to the operator rather than hidden.
    global_identity_id  UUID,
    identity_conf       REAL,

    best_keyframe_uri   TEXT,
    frame_sha256        TEXT,                           -- hashed at capture; feeds the BSA s.63 pack
    clip_uri            TEXT,

    -- Makes re-indexing possible. Without it, a model upgrade orphans the entire corpus.
    model_versions      JSONB       NOT NULL DEFAULT '{}'::jsonb,

    -- Track stitching: counting error lives here, not in the detector. If fragmentation splits
    -- one person into five tracks, SQL faithfully returns five.
    stitched_from       UUID[],

    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (track_id, ts_start)
) PARTITION BY RANGE (ts_start);

CREATE INDEX ON tracks (site_id, ts_start DESC);
CREATE INDEX ON tracks (camera_id, ts_start DESC);
CREATE INDEX ON tracks (class, ts_start DESC);
CREATE INDEX ON tracks (global_identity_id) WHERE global_identity_id IS NOT NULL;

-- ---------------------------------------------------------------------------
-- Zone events — EVERY counting question is a COUNT(*) on this table.
-- Never on captions, never by an LLM.
-- ---------------------------------------------------------------------------

CREATE TYPE zone_event_type AS ENUM
    ('enter', 'exit', 'cross_pos', 'cross_neg', 'dwell', 'loiter', 'stationary', 'gone');

CREATE TABLE zone_events (
    event_id        UUID        NOT NULL DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL,
    site_id         UUID        NOT NULL,
    camera_id       UUID        NOT NULL,
    zone_id         UUID        NOT NULL,
    track_id        UUID        NOT NULL,
    type            zone_event_type NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    dwell_start     TIMESTAMPTZ,
    dwell_end       TIMESTAMPTZ,
    conf            REAL        NOT NULL,
    attrs           JSONB       NOT NULL DEFAULT '{}'::jsonb,
    evidence_uri    TEXT,
    PRIMARY KEY (event_id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX ON zone_events (site_id, ts DESC);
CREATE INDEX ON zone_events (zone_id, ts DESC);
CREATE INDEX ON zone_events (camera_id, type, ts DESC);
CREATE INDEX ON zone_events (track_id);

-- ---------------------------------------------------------------------------
-- Clips — motion/scene-gated windows carrying a VLM caption. Open-vocabulary questions
-- ("was the fire exit blocked?") are answered from here; counts never are.
-- ---------------------------------------------------------------------------

CREATE TABLE clips (
    clip_id         UUID        NOT NULL DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL,
    site_id         UUID        NOT NULL,
    camera_id       UUID        NOT NULL,
    ts_start        TIMESTAMPTZ NOT NULL,
    ts_end          TIMESTAMPTZ NOT NULL,
    caption         TEXT,
    caption_vec     vector(1024),
    frame_vec       vector(512),
    keyframe_uris   TEXT[]      NOT NULL DEFAULT '{}',
    frame_sha256    TEXT[]      NOT NULL DEFAULT '{}',
    retained_reason TEXT        NOT NULL DEFAULT 'motion'
                    CHECK (retained_reason IN ('motion', 'scene_change', 'alert', 'sampled', 'backfill')),
    vlm_version     TEXT,
    PRIMARY KEY (clip_id, ts_start)
) PARTITION BY RANGE (ts_start);

CREATE INDEX ON clips (site_id, ts_start DESC);
CREATE INDEX ON clips (camera_id, ts_start DESC);

-- ---------------------------------------------------------------------------
-- External events — POS, access control, weighbridge, alarm panel.
--
-- One generic endpoint means every integration is a per-customer adapter rather than a schema
-- change. For a factory the highest-value adapter is the access-control turnstile: joined to
-- gate-camera tracks it gives tailgating detection, contractor-vs-employee classification and
-- shift headcount with ZERO face recognition and zero biometric exposure.
-- ---------------------------------------------------------------------------

CREATE TABLE external_events (
    ext_id      UUID        NOT NULL DEFAULT uuid_generate_v4(),
    tenant_id   UUID        NOT NULL,
    site_id     UUID        NOT NULL,
    source      TEXT        NOT NULL,        -- 'access_control' | 'pos' | 'weighbridge' | ...
    ts          TIMESTAMPTZ NOT NULL,
    type        TEXT        NOT NULL,
    amount      NUMERIC,
    actor       TEXT,
    camera_hint UUID,
    payload     JSONB       NOT NULL DEFAULT '{}'::jsonb,
    PRIMARY KEY (ext_id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX ON external_events (site_id, source, ts DESC);

-- ---------------------------------------------------------------------------
-- Answers — the audit trail, the eval harness and the anti-stalking defence, in one table.
--
-- A plain-language query box over every camera in a building is a stalking tool. DPDP Rule
-- 6(1)(c) wants visibility; Puttaswamy's fourth prong wants procedural safeguards; RBI wants
-- audit rights. This table is all three, and it is also how we measure our own accuracy.
-- Append-only, enforced by trigger in 002.
-- ---------------------------------------------------------------------------

CREATE TABLE answers (
    answer_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL,
    site_id         UUID        NOT NULL,
    actor           TEXT        NOT NULL,        -- who asked
    question        TEXT        NOT NULL,
    intent          TEXT,                        -- router classification
    plan            JSONB,                       -- the compiled filter
    sql_executed    TEXT,
    cameras_touched UUID[],
    window_start    TIMESTAMPTZ,
    window_end      TIMESTAMPTZ,
    rows_returned   INT,
    evidence_ids    UUID[],
    coverage_pct    REAL,
    abstained       BOOLEAN     NOT NULL DEFAULT FALSE,
    abstain_reason  TEXT CHECK (abstain_reason IN
                        ('no_coverage', 'no_evidence', 'not_measured', 'low_confidence')),
    exported        BOOLEAN     NOT NULL DEFAULT FALSE,
    latency_ms      INT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON answers (tenant_id, created_at DESC);
CREATE INDEX ON answers (actor, created_at DESC);

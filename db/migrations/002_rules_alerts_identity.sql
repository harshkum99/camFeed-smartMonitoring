-- Project Sanjay — rules, alerts, identity, evidence
--
-- Biometric data lives in its OWN tables with its own retention clock and its own delete path,
-- deliberately not mixed into the general event store. That separation is what makes a tenant
-- kill switch and a DPDP erasure request a bounded operation instead of a corpus-wide hunt.

-- ---------------------------------------------------------------------------
-- Rules
--
-- The rule document is JSON with a fixed shape (see sanjay/rules/schema.py). The same document
-- is evaluated identically in the browser preview, on the edge agent and in the cloud.
-- ---------------------------------------------------------------------------

CREATE TABLE rules (
    rule_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id   UUID        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    site_id     UUID        NOT NULL REFERENCES sites   ON DELETE CASCADE,
    name        TEXT        NOT NULL,
    version     INT         NOT NULL DEFAULT 1,
    severity    TEXT        NOT NULL DEFAULT 'warn' CHECK (severity IN ('info', 'warn', 'critical')),
    doc         JSONB       NOT NULL,
    enabled     BOOLEAN     NOT NULL DEFAULT TRUE,

    -- The flywheel. Every alert card carries a one-tap "not an incident"; the counts land here
    -- and above a threshold we auto-propose a tightened confirmation block. This is also the
    -- only per-site labelled dataset we will ever get for free.
    true_positives  INT NOT NULL DEFAULT 0,
    false_positives INT NOT NULL DEFAULT 0,
    last_tuned      TIMESTAMPTZ,

    created_at  TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON rules (site_id) WHERE enabled;

CREATE TYPE alert_state AS ENUM ('raised', 'verifying', 'confirmed', 'suppressed', 'dismissed', 'actioned');

CREATE TABLE alerts (
    alert_id        UUID        NOT NULL DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL,
    site_id         UUID        NOT NULL,
    camera_id       UUID        NOT NULL,
    rule_id         UUID        NOT NULL,
    track_id        UUID,
    zone_event_id   UUID,
    ts              TIMESTAMPTZ NOT NULL,
    state           alert_state NOT NULL DEFAULT 'raised',
    severity        TEXT        NOT NULL,

    -- Lane B. The verifier runs on a FIRED alert, never in the detection path.
    verifier_model  TEXT,
    verifier_verdict BOOLEAN,
    verifier_conf   REAL,
    verifier_reason TEXT,
    verifier_ms     INT,

    evidence_uris   TEXT[]      NOT NULL DEFAULT '{}',

    -- Latency accounting, so the "<2s" claim is measured rather than asserted.
    detected_at     TIMESTAMPTZ,     -- edge: frame timestamp
    fired_at        TIMESTAMPTZ,     -- edge: rule fired
    delivered_at    TIMESTAMPTZ,     -- cloud: pushed to an open console
    dedupe_key      TEXT        NOT NULL,

    operator_feedback TEXT CHECK (operator_feedback IN ('true_positive', 'false_positive')),
    feedback_by     TEXT,
    feedback_at     TIMESTAMPTZ,
    PRIMARY KEY (alert_id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX ON alerts (site_id, ts DESC);
CREATE INDEX ON alerts (rule_id, ts DESC);
CREATE INDEX ON alerts (dedupe_key, ts DESC);

-- ---------------------------------------------------------------------------
-- Identity — cross-camera. Ranked candidates requiring human confirmation, never an assertion.
--
-- State-of-the-art cross-camera ReID under clothing change is ~58% Rank-1. The product must
-- carry that uncertainty in its data model, not paper over it in the UI.
-- ---------------------------------------------------------------------------

CREATE TABLE identities (
    identity_id     UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    site_id         UUID        NOT NULL REFERENCES sites   ON DELETE CASCADE,
    label           TEXT,                        -- operator-assigned, e.g. "Contractor, blue shirt"
    kind            TEXT        NOT NULL DEFAULT 'anonymous'
                    CHECK (kind IN ('anonymous', 'employee', 'contractor', 'visitor')),
    first_seen      TIMESTAMPTZ,
    last_seen       TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- Every hop in a Track & Trace result. Unconfirmed hops render dashed; confirmed render solid.
CREATE TABLE identity_links (
    link_id         UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    identity_id     UUID        NOT NULL REFERENCES identities ON DELETE CASCADE,
    track_id        UUID        NOT NULL,
    camera_id       UUID        NOT NULL,
    ts              TIMESTAMPTZ NOT NULL,
    similarity      REAL        NOT NULL,
    tier            TEXT        NOT NULL CHECK (tier IN ('likely', 'possible', 'weak')),
    topology_ok     BOOLEAN     NOT NULL DEFAULT TRUE,   -- physically reachable in the elapsed time?
    confirmed_by    TEXT,
    confirmed_at    TIMESTAMPTZ,
    rejected        BOOLEAN     NOT NULL DEFAULT FALSE
);
CREATE INDEX ON identity_links (identity_id, ts);

-- ---------------------------------------------------------------------------
-- Face — separate tables, separate retention clock, separate delete path.
--
-- Lawful basis: DPDP s.7(i), employment. Enrolled gallery only — NEVER a gallery auto-built
-- from ambient footage, which is a prohibited practice in the EU (AI Act Art 5(1)(e)) and has
-- no lawful basis in India either.
-- ---------------------------------------------------------------------------

CREATE TABLE face_enrolments (
    person_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    site_id         UUID        NOT NULL REFERENCES sites   ON DELETE CASCADE,
    display_name    TEXT        NOT NULL,
    employee_ref    TEXT,

    -- Consent/notice register: who enrolled them, on what basis, when.
    legal_basis     TEXT        NOT NULL DEFAULT 'employment'
                    CHECK (legal_basis IN ('employment', 'consent')),
    enrolled_by     TEXT        NOT NULL,
    enrolled_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    notice_ref      TEXT,

    -- Encrypted at rest by the application (envelope encryption); the DB never sees plaintext.
    template_enc    BYTEA       NOT NULL,
    template_model  TEXT        NOT NULL,        -- e.g. 'adaface_ir101_webface12m'
    -- The 112x112 aligned chip is the ONLY insurance against a model change orphaning the index.
    -- ~5-10 KB. Without it, upgrading the recogniser means the gallery must be rebuilt by hand.
    chip_uri        TEXT,

    expires_at      TIMESTAMPTZ NOT NULL,        -- hard auto-purge; own clock, not the tenant's
    revoked_at      TIMESTAMPTZ
);
CREATE INDEX ON face_enrolments (tenant_id) WHERE revoked_at IS NULL;

-- Immutable log of every face search. Append-only (trigger below). This is what we hand a
-- regulator, and what defends us the first time a guard runs a search on a colleague.
CREATE TABLE face_search_audit (
    audit_id        UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL,
    site_id         UUID        NOT NULL,
    actor           TEXT        NOT NULL,
    probe_sha256    TEXT        NOT NULL,        -- hash of the query image, not the image
    window_start    TIMESTAMPTZ,
    window_end      TIMESTAMPTZ,
    cameras         UUID[],
    n_results       INT         NOT NULL,
    top_similarity  REAL,
    justification   TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON face_search_audit (tenant_id, created_at DESC);

-- ---------------------------------------------------------------------------
-- Evidence — the USP, in tables.
--
-- Bharatiya Sakshya Adhiniyam 2023 s.63(4) + Schedule (in force 1 July 2024) requires a
-- certificate stating SHA-1/SHA-256/MD5 with a hash report enclosed, identifying the source
-- device, signed by the custodian (Part A) and an expert (Part B), at EACH instance of
-- submission. We produce Part A pre-filled.
-- ---------------------------------------------------------------------------

-- One row per camera-hour. Chaining hashes makes tampering detectable rather than merely
-- discouraged: altering any frame breaks every subsequent root.
CREATE TABLE hash_chain (
    camera_id       UUID        NOT NULL,
    hour_start      TIMESTAMPTZ NOT NULL,
    tenant_id       UUID        NOT NULL,
    site_id         UUID        NOT NULL,
    n_frames        INT         NOT NULL DEFAULT 0,
    merkle_root     TEXT        NOT NULL,
    prev_root       TEXT,
    sealed_at       TIMESTAMPTZ,
    ntp_source      TEXT,                        -- NIC/NPL-traceable; also a CERT-In obligation
    clock_offset_ms BIGINT,
    PRIMARY KEY (camera_id, hour_start)
);
CREATE INDEX ON hash_chain (site_id, hour_start DESC);

CREATE TABLE evidence_bundles (
    bundle_id       UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id       UUID        NOT NULL,
    site_id         UUID        NOT NULL,
    requested_by    TEXT        NOT NULL,
    purpose         TEXT,
    window_start    TIMESTAMPTZ NOT NULL,
    window_end      TIMESTAMPTZ NOT NULL,
    cameras         UUID[]      NOT NULL,
    frame_count     INT         NOT NULL,
    merkle_root     TEXT        NOT NULL,
    -- A camera whose clock drifted during the window cannot produce a trustworthy certificate.
    -- We say so on the document rather than silently printing the wrong time.
    clock_warnings  JSONB       NOT NULL DEFAULT '[]'::jsonb,
    certificate_uri TEXT,
    archive_uri     TEXT,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON evidence_bundles (tenant_id, created_at DESC);

-- Append-only chain of custody: every access to an evidence bundle.
CREATE TABLE custody_log (
    entry_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    bundle_id   UUID        NOT NULL REFERENCES evidence_bundles ON DELETE RESTRICT,
    actor       TEXT        NOT NULL,
    action      TEXT        NOT NULL,       -- created | viewed | exported | certified
    detail      JSONB       NOT NULL DEFAULT '{}'::jsonb,
    at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON custody_log (bundle_id, at);

-- ---------------------------------------------------------------------------
-- Append-only enforcement.
--
-- An audit log that can be edited is not an audit log. Enforced in the database so that an
-- application bug, or a future engineer in a hurry, cannot quietly weaken it.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION deny_mutation() RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'append-only table: % on % is not permitted', TG_OP, TG_TABLE_NAME;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER answers_append_only
    BEFORE UPDATE OR DELETE ON answers
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER face_audit_append_only
    BEFORE UPDATE OR DELETE ON face_search_audit
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

CREATE TRIGGER custody_append_only
    BEFORE UPDATE OR DELETE ON custody_log
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

-- A sealed hash-chain hour is immutable. Re-sealing would defeat the entire point.
CREATE OR REPLACE FUNCTION deny_sealed_update() RETURNS TRIGGER AS $$
BEGIN
    IF OLD.sealed_at IS NOT NULL THEN
        RAISE EXCEPTION 'hash_chain hour % for camera % is sealed and cannot be modified',
            OLD.hour_start, OLD.camera_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER hash_chain_seal
    BEFORE UPDATE OR DELETE ON hash_chain
    FOR EACH ROW EXECUTE FUNCTION deny_sealed_update();

-- ---------------------------------------------------------------------------
-- Biometric guard rails, enforced in the database rather than in a policy document.
-- ---------------------------------------------------------------------------

CREATE OR REPLACE FUNCTION check_face_allowed() RETURNS TRIGGER AS $$
DECLARE t RECORD;
BEGIN
    SELECT face_enabled, is_school, legal_basis INTO t FROM tenants WHERE tenant_id = NEW.tenant_id;
    IF NOT t.face_enabled THEN
        RAISE EXCEPTION 'face recognition is not enabled for this tenant';
    END IF;
    -- DPDP s.9(3) is an absolute prohibition on tracking or behavioural monitoring of children,
    -- with no consent cure and a Rs200 crore ceiling. There is no configuration that permits it.
    IF t.is_school THEN
        RAISE EXCEPTION 'face recognition is prohibited for school tenants (DPDP s.9(3))';
    END IF;
    IF t.legal_basis NOT IN ('employment', 'consent') THEN
        RAISE EXCEPTION 'no lawful basis for biometric processing on this tenant';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER face_enrolment_guard
    BEFORE INSERT ON face_enrolments
    FOR EACH ROW EXECUTE FUNCTION check_face_allowed();

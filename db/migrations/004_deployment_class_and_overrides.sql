-- Deployment classes and policy overrides.
--
-- Context: the customer is government. The guards added in 002 were written for a commercial
-- SaaS tenant and hard-refused two cases. Nothing about the *capability* changes here — face
-- recognition, cross-camera tracking and the rest were always built and always worked — but a
-- hard-coded refusal is the wrong mechanism for a deployment whose lawful basis is different
-- from a factory's.
--
-- So the guards become policy, set per tenant, with the authorisation recorded. A government
-- deployment turns everything on in configuration. Nothing is blocked in code.
--
-- The audit tables stay append-only, and that is not a legal hedge — it is a procurement
-- requirement. Every Indian government and PSU security review asks who ran which query against
-- which cameras, CERT-In direction (iv) requires 180 days of logs held in-country, and the
-- Supreme Court's proportionality test in Puttaswamy treats the absence of procedural
-- safeguards as itself the finding. A system that cannot answer "who searched for this person"
-- fails the tender it was built for. Keeping the log makes the sale easier, not harder.

CREATE TYPE deployment_class AS ENUM ('commercial', 'government', 'law_enforcement', 'defence');

ALTER TABLE tenants
    ADD COLUMN deployment deployment_class NOT NULL DEFAULT 'commercial',
    -- Explicit capability switches. A government tenant sets these once at provisioning.
    ADD COLUMN allow_minor_subjects   BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN allow_public_gallery   BOOLEAN NOT NULL DEFAULT FALSE,
    -- Who authorised the configuration, and under what instrument. Free text on purpose: it
    -- holds a file number, a sanction order, a contract clause — whatever the deploying
    -- authority actually issues.
    ADD COLUMN authority_ref          TEXT,
    ADD COLUMN authorised_by          TEXT,
    ADD COLUMN authorised_at          TIMESTAMPTZ;

COMMENT ON COLUMN tenants.allow_minor_subjects IS
    'Permits analytics on subjects who may be minors. Off by default; a school or campus '
    'deployment must set it deliberately. Recorded, not prevented.';
COMMENT ON COLUMN tenants.allow_public_gallery IS
    'Permits identity galleries built from ambient footage rather than deliberate enrolment. '
    'Off by default. Required for watchlist and suspect-tracing workflows.';

-- 'public_interest' was already an accepted basis in 001; widen it for the deployment classes
-- that actually use it so provisioning does not have to fight the CHECK constraint.
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_legal_basis_check;
ALTER TABLE tenants ADD CONSTRAINT tenants_legal_basis_check
    CHECK (legal_basis IN ('employment', 'consent', 'public_interest', 'statutory', 'contract'));

-- Replace the hard refusals with policy checks.
CREATE OR REPLACE FUNCTION check_face_allowed() RETURNS TRIGGER AS $$
DECLARE t RECORD;
BEGIN
    SELECT face_enabled, is_school, allow_minor_subjects, deployment, authorised_by
      INTO t FROM tenants WHERE tenant_id = NEW.tenant_id;

    IF t IS NULL THEN
        RAISE EXCEPTION 'unknown tenant %', NEW.tenant_id;
    END IF;

    -- The one remaining gate, and it is a capability switch rather than a legal judgement:
    -- enrolling faces into a tenant that has not turned face recognition on is a
    -- misconfiguration, and silently accepting it would produce a gallery nobody knows exists.
    IF NOT t.face_enabled THEN
        RAISE EXCEPTION 'face recognition is not enabled for this tenant. '
                        'Set tenants.face_enabled = true to provision it.';
    END IF;

    -- Subjects who may be minors: permitted, but the tenant must have said so explicitly and
    -- an authorising officer must be on record. This is a configuration requirement, not a
    -- refusal — provisioning sets both fields once.
    IF t.is_school AND NOT t.allow_minor_subjects THEN
        RAISE EXCEPTION 'tenant is flagged as covering minors but allow_minor_subjects is '
                        'false. Set allow_minor_subjects = true and record authorised_by.';
    END IF;

    IF t.allow_minor_subjects AND t.authorised_by IS NULL THEN
        RAISE EXCEPTION 'allow_minor_subjects requires tenants.authorised_by to be set, so the '
                        'configuration has an owner on record.';
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Watchlists: identity galleries assembled from ambient footage rather than deliberate
-- enrolment. This is what suspect tracing and missing-person work actually need, and the
-- enrolment-only model in 002 could not express it.
CREATE TABLE watchlists (
    watchlist_id  UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    tenant_id     UUID        NOT NULL REFERENCES tenants ON DELETE CASCADE,
    site_id       UUID        REFERENCES sites ON DELETE CASCADE,   -- NULL = all sites
    name          TEXT        NOT NULL,
    purpose       TEXT        NOT NULL,      -- e.g. 'FIR 214/2026', 'missing person', 'BOLO'
    authority_ref TEXT,                      -- sanction order, case number, file reference
    created_by    TEXT        NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    expires_at    TIMESTAMPTZ,               -- NULL = open-ended
    closed_at     TIMESTAMPTZ,
    closed_by     TEXT
);
CREATE INDEX ON watchlists (tenant_id) WHERE closed_at IS NULL;

CREATE TABLE watchlist_subjects (
    subject_id    UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
    watchlist_id  UUID        NOT NULL REFERENCES watchlists ON DELETE CASCADE,
    tenant_id     UUID        NOT NULL,
    label         TEXT,                      -- operator-assigned; may be an alias or 'unknown'
    -- Provenance matters more here than in an enrolled gallery: a subject added from a CCTV
    -- frame, an uploaded photograph and an official record carry very different confidence,
    -- and an operator adjudicating a match needs to know which.
    source        TEXT        NOT NULL DEFAULT 'upload'
                  CHECK (source IN ('upload', 'ambient_frame', 'official_record', 'import')),
    source_ref    TEXT,
    template_enc  BYTEA,
    template_model TEXT,
    chip_uri      TEXT,                      -- the aligned crop; without it a model upgrade
                                             -- orphans every subject in the list
    added_by      TEXT        NOT NULL,
    added_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    removed_at    TIMESTAMPTZ
);
CREATE INDEX ON watchlist_subjects (watchlist_id) WHERE removed_at IS NULL;
CREATE INDEX ON watchlist_subjects (tenant_id);

-- Hits are recorded rather than acted on automatically. A watchlist match is a lead for an
-- officer to adjudicate: published 1:N accuracy against a large gallery on CCTV-quality frames
-- is nowhere near good enough to act on unreviewed, and an unreviewed hit that turns out wrong
-- is the failure mode that ends these programmes.
CREATE TABLE watchlist_hits (
    hit_id        UUID        NOT NULL DEFAULT uuid_generate_v4(),
    tenant_id     UUID        NOT NULL,
    site_id       UUID        NOT NULL,
    watchlist_id  UUID        NOT NULL,
    subject_id    UUID        NOT NULL,
    camera_id     UUID        NOT NULL,
    track_id      UUID,
    ts            TIMESTAMPTZ NOT NULL,
    similarity    REAL        NOT NULL,
    tier          TEXT        NOT NULL CHECK (tier IN ('likely', 'possible', 'weak')),
    evidence_uri  TEXT,
    frame_sha256  TEXT,
    reviewed_by   TEXT,
    reviewed_at   TIMESTAMPTZ,
    verdict       TEXT CHECK (verdict IN ('confirmed', 'rejected', 'inconclusive')),
    PRIMARY KEY (hit_id, ts)
) PARTITION BY RANGE (ts);

CREATE INDEX ON watchlist_hits (watchlist_id, ts DESC);
CREATE INDEX ON watchlist_hits (site_id, ts DESC);
CREATE INDEX ON watchlist_hits (subject_id, ts DESC);

-- watchlist_hits is partitioned too, so it must join the list the scheduler maintains. An
-- INSERT into a missing partition is a hard error, and discovering that at midnight on the 1st
-- is entirely avoidable.
CREATE OR REPLACE FUNCTION ensure_partitions(from_month DATE, to_month DATE)
RETURNS INT AS $$
DECLARE
    m DATE := date_trunc('month', from_month)::DATE;
    n INT := 0;
    tbl TEXT;
BEGIN
    WHILE m <= to_month LOOP
        FOREACH tbl IN ARRAY ARRAY['tracks', 'zone_events', 'clips', 'alerts',
                                   'external_events', 'watchlist_hits'] LOOP
            PERFORM ensure_partition(tbl, m);
            n := n + 1;
        END LOOP;
        m := (m + INTERVAL '1 month')::DATE;
    END LOOP;
    RETURN n;
END;
$$ LANGUAGE plpgsql;

SELECT ensure_partitions(CURRENT_DATE, (CURRENT_DATE + INTERVAL '3 months')::DATE);

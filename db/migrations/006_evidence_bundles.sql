-- Smart Cam Monitoring — evidence bundles become records that cannot be edited.
--
-- 002 created evidence_bundles with certificate_uri and archive_uri left NULL to be filled in
-- later. That shape invites an UPDATE, and an evidence record that can be updated is one whose
-- history has to be taken on trust. So a bundle is now written once, complete, and then frozen:
-- the archive is built and hashed first, and the row that describes it is inserted afterwards
-- with every hash already known. Anything that happens to the bundle later — viewed, exported,
-- certified — is a new custody_log row (already append-only in 002), never an edit.

ALTER TABLE evidence_bundles
    ADD COLUMN IF NOT EXISTS manifest_sha256    TEXT CHECK (manifest_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN IF NOT EXISTS archive_sha256     TEXT CHECK (archive_sha256 ~ '^[0-9a-f]{64}$'),
    ADD COLUMN IF NOT EXISTS certificate_sha256 TEXT
        CHECK (certificate_sha256 IS NULL OR certificate_sha256 ~ '^[0-9a-f]{64}$'),
    -- The tracks the operator selected, and the recordings they came from, as cited.
    ADD COLUMN IF NOT EXISTS track_ids          UUID[] NOT NULL DEFAULT '{}',
    ADD COLUMN IF NOT EXISTS source_files       JSONB  NOT NULL DEFAULT '[]'::jsonb,
    ADD COLUMN IF NOT EXISTS question           TEXT,
    -- SHA256SUMS covers every file in the archive, including the README, the bundled verifier
    -- and the certificate draft, none of which is a Merkle leaf. Recording its hash means none
    -- of them can be swapped without the record noticing.
    ADD COLUMN IF NOT EXISTS sums_sha256        TEXT
        CHECK (sums_sha256 IS NULL OR sums_sha256 ~ '^[0-9a-f]{64}$');

DROP TRIGGER IF EXISTS evidence_bundles_immutable ON evidence_bundles;
CREATE TRIGGER evidence_bundles_immutable
    BEFORE UPDATE OR DELETE ON evidence_bundles
    FOR EACH ROW EXECUTE FUNCTION deny_mutation();

-- A bundle names its tenant and site directly, and every read of it is scoped by them. A pair
-- that does not belong together would file one customer's footage under another's name.
CREATE OR REPLACE FUNCTION enforce_bundle_scope() RETURNS trigger AS $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM sites WHERE site_id = NEW.site_id AND tenant_id = NEW.tenant_id) THEN
        RAISE EXCEPTION 'evidence bundle names site % under tenant %, which do not belong together',
            NEW.site_id, NEW.tenant_id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS evidence_bundles_scope ON evidence_bundles;
CREATE TRIGGER evidence_bundles_scope
    BEFORE INSERT ON evidence_bundles
    FOR EACH ROW EXECUTE FUNCTION enforce_bundle_scope();

CREATE INDEX IF NOT EXISTS evidence_bundles_site_idx ON evidence_bundles (site_id, created_at DESC);

-- When, and by which software, each imported recording was hashed. The hash report must say so:
-- a hash computed on receipt identifies the file as received, and nothing earlier, and a reader
-- needs the time and the tool to weigh it. No default, deliberately — rows imported before this
-- column existed have no honest value to give it, and are re-imported to acquire one.
ALTER TABLE clips
    ADD COLUMN IF NOT EXISTS imported_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS imported_by TEXT;

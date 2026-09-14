-- Guard-rail assertions, run in CI against a fresh database.
--
-- These are DPDP and evidentiary controls enforced in the database rather than in a policy
-- document, precisely so that an application bug or an engineer in a hurry cannot quietly
-- weaken them. This file exists so a migration that drops one fails the build.
--
-- Each assertion inverts the usual shape: it attempts the forbidden operation and RAISES if the
-- operation SUCCEEDS. Run with psql -v ON_ERROR_STOP=1.

\set QUIET on
SET client_min_messages TO NOTICE;

BEGIN;

INSERT INTO tenants (tenant_id, name, legal_basis, face_enabled) VALUES
  ('11111111-1111-1111-1111-111111111111', 'Acme Plant', 'employment', false),
  ('22222222-2222-2222-2222-222222222222', 'Test School', 'employment', true),
  ('33333333-3333-3333-3333-333333333333', 'Acme Plant FR', 'employment', true);
UPDATE tenants SET is_school = true WHERE tenant_id = '22222222-2222-2222-2222-222222222222';

INSERT INTO sites (site_id, tenant_id, name) VALUES
  ('aaaaaaaa-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111', 'Plant 2'),
  ('aaaaaaaa-0000-0000-0000-000000000002', '22222222-2222-2222-2222-222222222222', 'Campus'),
  ('aaaaaaaa-0000-0000-0000-000000000003', '33333333-3333-3333-3333-333333333333', 'Plant 3');

INSERT INTO cameras (camera_id, tenant_id, site_id, name) VALUES
  ('cccccccc-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
   'aaaaaaaa-0000-0000-0000-000000000001', 'Gate 3'),
  ('cccccccc-0000-0000-0000-000000000002', '11111111-1111-1111-1111-111111111111',
   'aaaaaaaa-0000-0000-0000-000000000001', 'Zone B'),
  ('cccccccc-0000-0000-0000-000000000003', '11111111-1111-1111-1111-111111111111',
   'aaaaaaaa-0000-0000-0000-000000000001', 'Dock A');

\set QUIET off

-- G1 -- face enrolment is refused when the tenant has not enabled it (off by default).
DO $$ BEGIN
  BEGIN
    INSERT INTO face_enrolments (tenant_id, site_id, display_name, enrolled_by,
                                 template_enc, template_model, expires_at)
    VALUES ('11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000001',
            'Ravi', 'admin', '\x00', 'adaface', now() + interval '90 days');
    RAISE EXCEPTION 'G1 FAILED: face enrolment succeeded on a tenant with face_enabled = false';
  EXCEPTION WHEN sqlstate 'P0001' THEN
    IF SQLERRM LIKE 'G1 FAILED%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'G1 ok  - face enrolment blocked when not enabled';
END $$;

-- G2 -- a tenant covering minors must enable the capability deliberately and name an
-- authorising officer. This is a configuration gate, not a refusal: provisioning sets both
-- fields once and everything works. It exists so a gallery of minors cannot come into
-- existence through a default nobody chose.
DO $$ BEGIN
  BEGIN
    INSERT INTO face_enrolments (tenant_id, site_id, display_name, enrolled_by,
                                 template_enc, template_model, expires_at)
    VALUES ('22222222-2222-2222-2222-222222222222', 'aaaaaaaa-0000-0000-0000-000000000002',
            'Subject', 'admin', '\x00', 'adaface', now() + interval '90 days');
    RAISE EXCEPTION 'G2 FAILED: minor-subject enrolment succeeded without being enabled';
  EXCEPTION WHEN sqlstate 'P0001' THEN
    IF SQLERRM LIKE 'G2 FAILED%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'G2 ok  - minor-subject enrolment requires explicit configuration';
END $$;

-- G3 -- a properly configured tenant CAN enrol. A guard that blocks everything is not a guard.
INSERT INTO face_enrolments (tenant_id, site_id, display_name, enrolled_by,
                             template_enc, template_model, expires_at)
VALUES ('33333333-3333-3333-3333-333333333333', 'aaaaaaaa-0000-0000-0000-000000000003',
        'Enrolled Staff', 'admin', '\x00', 'adaface_ir101_webface12m',
        now() + interval '90 days');
DO $$ BEGIN RAISE NOTICE 'G3 ok  - enrolment permitted for an enabled employment-basis tenant'; END $$;

-- G3b -- an authorised deployment CAN enrol minor subjects. Nothing is blocked in code; the
-- configuration is simply on the record, with an owner. A guard that cannot be turned off is
-- not a guard, it is a missing feature.
UPDATE tenants SET deployment = 'law_enforcement', allow_minor_subjects = true,
                   authorised_by = 'SP (Ops), file 214/2026', authorised_at = now(),
                   legal_basis = 'statutory'
 WHERE tenant_id = '22222222-2222-2222-2222-222222222222';
INSERT INTO face_enrolments (tenant_id, site_id, display_name, enrolled_by,
                             template_enc, template_model, expires_at)
VALUES ('22222222-2222-2222-2222-222222222222', 'aaaaaaaa-0000-0000-0000-000000000002',
        'Subject A', 'officer', '\x00', 'adaface_ir101_webface12m', now() + interval '1 year');
DO $$ BEGIN RAISE NOTICE 'G3b ok - authorised deployment can enrol minor subjects'; END $$;

-- G4 -- the query audit log is append-only. An audit log that can be edited is not an audit log.
INSERT INTO answers (answer_id, tenant_id, site_id, actor, question)
VALUES ('dddddddd-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
        'aaaaaaaa-0000-0000-0000-000000000001', 'guard1', 'who entered gate 3 last night');
DO $$ BEGIN
  BEGIN
    UPDATE answers SET question = 'redacted' WHERE actor = 'guard1';
    RAISE EXCEPTION 'G4 FAILED: answers row was updated';
  EXCEPTION WHEN sqlstate 'P0001' THEN
    IF SQLERRM LIKE 'G4 FAILED%' THEN RAISE; END IF;
  END;
  BEGIN
    DELETE FROM answers WHERE actor = 'guard1';
    RAISE EXCEPTION 'G4 FAILED: answers row was deleted';
  EXCEPTION WHEN sqlstate 'P0001' THEN
    IF SQLERRM LIKE 'G4 FAILED%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'G4 ok  - query audit log rejects UPDATE and DELETE';
END $$;

-- G5 -- face searches are logged immutably. This is the anti-stalking control: a plain-language
-- query box over every camera in a building is a stalking tool without it.
INSERT INTO face_search_audit (tenant_id, site_id, actor, probe_sha256, n_results)
VALUES ('33333333-3333-3333-3333-333333333333', 'aaaaaaaa-0000-0000-0000-000000000003',
        'guard1', repeat('a', 64), 3);
DO $$ BEGIN
  BEGIN
    DELETE FROM face_search_audit WHERE actor = 'guard1';
    RAISE EXCEPTION 'G5 FAILED: face search audit row was deleted';
  EXCEPTION WHEN sqlstate 'P0001' THEN
    IF SQLERRM LIKE 'G5 FAILED%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'G5 ok  - face search audit rejects DELETE';
END $$;

-- G6 -- a sealed hash-chain hour is immutable. Re-sealing would defeat the entire point of the
-- Bharatiya Sakshya Adhiniyam s.63 evidence story.
INSERT INTO hash_chain (camera_id, hour_start, tenant_id, site_id, n_frames, merkle_root, sealed_at)
VALUES ('cccccccc-0000-0000-0000-000000000001', '2026-09-01 10:00+00',
        '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000001',
        120, 'abc123', now());
DO $$ BEGIN
  BEGIN
    UPDATE hash_chain SET merkle_root = 'tampered' WHERE merkle_root = 'abc123';
    RAISE EXCEPTION 'G6 FAILED: a sealed hash-chain hour was modified';
  EXCEPTION WHEN sqlstate 'P0001' THEN
    IF SQLERRM LIKE 'G6 FAILED%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'G6 ok  - sealed hash-chain hour is immutable';
END $$;

-- G7 -- an UNSEALED hour is still writable, otherwise the chain could never be built.
INSERT INTO hash_chain (camera_id, hour_start, tenant_id, site_id, n_frames, merkle_root)
VALUES ('cccccccc-0000-0000-0000-000000000002', '2026-09-01 11:00+00',
        '11111111-1111-1111-1111-111111111111', 'aaaaaaaa-0000-0000-0000-000000000001',
        10, 'partial');
UPDATE hash_chain SET n_frames = 20, merkle_root = 'partial2'
 WHERE camera_id = 'cccccccc-0000-0000-0000-000000000002';
DO $$ BEGIN RAISE NOTICE 'G7 ok  - unsealed hash-chain hour is still writable'; END $$;

-- G8 -- coverage arithmetic. One camera online throughout, one online for half the window, one
-- with no uptime rows at all. Absence of evidence is not evidence of uptime, so the third
-- camera must count as 0%, giving (1.0 + 0.5 + 0.0) / 3 = 0.5.
INSERT INTO camera_uptime (camera_id, site_id, ts_start, ts_end, state) VALUES
  ('cccccccc-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
   '2026-09-01 10:00+00', '2026-09-01 11:00+00', 'online'),
  ('cccccccc-0000-0000-0000-000000000002', 'aaaaaaaa-0000-0000-0000-000000000001',
   '2026-09-01 10:00+00', '2026-09-01 10:30+00', 'online');
DO $$
DECLARE cams UUID[] := ARRAY['cccccccc-0000-0000-0000-000000000001',
                             'cccccccc-0000-0000-0000-000000000002',
                             'cccccccc-0000-0000-0000-000000000003']::UUID[];
        pct REAL; gaps INT;
BEGIN
  pct := coverage_pct(cams, '2026-09-01 10:00+00', '2026-09-01 11:00+00');
  IF abs(pct - 0.5) > 0.001 THEN
    RAISE EXCEPTION 'G8 FAILED: coverage_pct returned %, expected 0.5', pct;
  END IF;

  -- Two gaps: Zone B's trailing gap after it went offline and never returned, and Dock A's
  -- entire window. The trailing case is the one a naive implementation misses, and it is
  -- precisely the operationally important one.
  SELECT count(*) INTO gaps FROM coverage_gaps(cams, '2026-09-01 10:00+00', '2026-09-01 11:00+00');
  IF gaps <> 2 THEN
    RAISE EXCEPTION 'G8 FAILED: coverage_gaps returned % rows, expected 2', gaps;
  END IF;

  PERFORM 1 FROM coverage_gaps(cams, '2026-09-01 10:00+00', '2026-09-01 11:00+00')
   WHERE camera_name = 'Zone B' AND gap_start = '2026-09-01 10:30+00';
  IF NOT FOUND THEN
    RAISE EXCEPTION 'G8 FAILED: trailing gap for a camera that went offline was not detected';
  END IF;
  RAISE NOTICE 'G8 ok  - coverage arithmetic and both gap kinds are correct';
END $$;

-- G9 -- partitions exist for the current month, or the first INSERT of the month fails hard.
DO $$
DECLARE n INT;
BEGIN
  SELECT count(*) INTO n FROM pg_tables
   WHERE schemaname = 'public' AND tablename ~ '^tracks_[0-9]{6}$';
  IF n = 0 THEN RAISE EXCEPTION 'G9 FAILED: no tracks partitions were created'; END IF;
  RAISE NOTICE 'G9 ok  - % tracks partition(s) present', n;
END $$;

-- G10 -- uptime rows take their tenant and site from the camera, and a row naming someone
-- else's site is refused. Without this, an importer bug writes coverage under the wrong customer
-- and nothing notices, because camera_uptime has no other link to a tenant.
DO $$
DECLARE t UUID;
BEGIN
  INSERT INTO camera_uptime (camera_id, site_id, ts_start, ts_end, state)
  VALUES ('cccccccc-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
          '2026-09-02 10:00+00', '2026-09-02 11:00+00', 'online');
  SELECT tenant_id INTO t FROM camera_uptime
   WHERE camera_id = 'cccccccc-0000-0000-0000-000000000001' AND ts_start = '2026-09-02 10:00+00';
  IF t IS DISTINCT FROM '11111111-1111-1111-1111-111111111111' THEN
    RAISE EXCEPTION 'G10 FAILED: uptime tenant_id was not filled from the camera (got %)', t;
  END IF;
  BEGIN
    INSERT INTO camera_uptime (camera_id, tenant_id, site_id, ts_start, ts_end, state)
    VALUES ('cccccccc-0000-0000-0000-000000000001', '11111111-1111-1111-1111-111111111111',
            gen_random_uuid(), '2026-09-02 12:00+00', '2026-09-02 13:00+00', 'online');
    RAISE EXCEPTION 'G10 FAILED: uptime row naming a foreign site was accepted';
  EXCEPTION WHEN raise_exception THEN
    IF SQLERRM LIKE 'G10 FAILED%' THEN RAISE; END IF;
  END;
  RAISE NOTICE 'G10 ok - uptime scope is derived from the camera and cannot be misattributed';
END $$;

-- G11 -- overlapping uptime rows are merged, not double-counted. Two rows covering the same hour
-- on one camera, next to a camera that was dark for the whole hour, must read 50% — the old
-- summing version read 100% and hid the dark camera entirely.
DO $$
DECLARE cams UUID[] := ARRAY['cccccccc-0000-0000-0000-000000000001',
                             'cccccccc-0000-0000-0000-000000000003']::UUID[];
        pct REAL;
BEGIN
  INSERT INTO camera_uptime (camera_id, site_id, ts_start, ts_end, state) VALUES
    ('cccccccc-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
     '2026-09-03 10:00+00', '2026-09-03 11:00+00', 'online'),
    ('cccccccc-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
     '2026-09-03 10:00:01+00', '2026-09-03 11:00+00', 'online'),
    ('cccccccc-0000-0000-0000-000000000001', 'aaaaaaaa-0000-0000-0000-000000000001',
     '2026-09-03 10:15+00', '2026-09-03 10:45+00', 'online');
  pct := coverage_pct(cams, '2026-09-03 10:00+00', '2026-09-03 11:00+00');
  IF abs(pct - 0.5) > 0.001 THEN
    RAISE EXCEPTION 'G11 FAILED: overlapping rows gave coverage %, expected 0.5', pct;
  END IF;
  RAISE NOTICE 'G11 ok - overlapping uptime is merged before coverage is computed';
END $$;

ROLLBACK;

\echo 'all guard-rail assertions passed'

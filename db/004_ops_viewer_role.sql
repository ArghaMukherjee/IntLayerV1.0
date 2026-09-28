-- =============================================================================
-- Read-only role for the operations UI: sees every request, log and report,
-- changes nothing. Actions (submit, cancel, replay) go through the IL API.
-- =============================================================================
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'ops_viewer') THEN
        CREATE ROLE ops_viewer NOLOGIN;
    END IF;
END $$;

GRANT USAGE ON SCHEMA integration, dwh TO ops_viewer;
GRANT SELECT ON ALL TABLES IN SCHEMA integration TO ops_viewer;
GRANT SELECT ON ALL TABLES IN SCHEMA dwh TO ops_viewer;

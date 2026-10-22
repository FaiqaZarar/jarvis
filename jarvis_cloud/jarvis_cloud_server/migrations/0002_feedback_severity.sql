-- Severity, matching the three levels in the approved Figma mockup
-- (issue #145): minor / annoying / blocking. A CHECK constraint, not a
-- separate lookup table - a fixed, small, unlikely-to-grow set of
-- values, the same reasoning migration 0001 already applies elsewhere
-- (no enum table for a 2-3 value domain).
ALTER TABLE feedback ADD COLUMN severity TEXT NOT NULL DEFAULT 'annoying'
    CHECK (severity IN ('minor', 'annoying', 'blocking'));

-- The default above exists only to satisfy NOT NULL for any pre-existing
-- rows from before this column existed; every future insert always
-- passes a real value explicitly (see internal/db/feedback.py) - no code
-- should rely on this default applying going forward.
ALTER TABLE feedback ALTER COLUMN severity DROP DEFAULT;

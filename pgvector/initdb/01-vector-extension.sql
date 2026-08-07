-- Runs ONCE, by the pgvector image's entrypoint, only on an empty data directory.
--
-- The image ships the extension's files but does NOT enable it in any database —
-- that is always an explicit CREATE EXTENSION. Doing it here rather than as a manual
-- psql step means a volume rebuild or a disaster-recovery restore produces an
-- identical database with no remembered follow-up command.
--
-- IF NOT EXISTS so re-running by hand is harmless.
CREATE EXTENSION IF NOT EXISTS vector;

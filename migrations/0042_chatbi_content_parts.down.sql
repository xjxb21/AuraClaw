BEGIN;

ALTER TABLE projection.task_view
    DROP COLUMN IF EXISTS content_parts;

COMMIT;

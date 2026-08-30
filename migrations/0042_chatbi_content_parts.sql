BEGIN;

ALTER TABLE projection.task_view
    ADD COLUMN IF NOT EXISTS content_parts jsonb NOT NULL DEFAULT '[]'::jsonb;

COMMIT;

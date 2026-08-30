BEGIN;

ALTER TABLE `projection_task_view`
    ADD COLUMN `content_parts` json NOT NULL DEFAULT (CAST('[]' AS JSON));

COMMIT;

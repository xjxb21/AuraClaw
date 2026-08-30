BEGIN;

ALTER TABLE `projection_task_view`
    DROP COLUMN IF EXISTS `content_parts`;

COMMIT;

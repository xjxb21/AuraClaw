from datetime import UTC, datetime
from pathlib import Path

import pytest

from auraclaw.infrastructure.projection.postgres_task_store import (
    PostgresTaskProjection,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        (
            "migrations/0042_chatbi_content_parts.sql",
            "content_parts jsonb NOT NULL DEFAULT '[]'::jsonb",
        ),
        (
            "migrations/0042_chatbi_content_parts.down.sql",
            "DROP COLUMN IF EXISTS content_parts",
        ),
        (
            "migrations/mysql/0023_chatbi_content_parts.sql",
            "`content_parts` json NOT NULL DEFAULT (CAST('[]' AS JSON))",
        ),
        (
            "migrations/mysql/0023_chatbi_content_parts.down.sql",
            "DROP COLUMN IF EXISTS `content_parts`",
        ),
    ],
)
def test_chatbi_content_parts_migrations_exist(
    relative_path: str, expected: str
) -> None:
    assert expected in ROOT.joinpath(relative_path).read_text(encoding="utf-8")


def test_postgres_task_row_restores_content_parts() -> None:
    row = {
        "tenant_id": "tenant-1",
        "session_id": "ses-1",
        "root_session_id": "ses-1",
        "run_id": "run-1",
        "status": "ready",
        "run_status": "completed",
        "goal": "build a chart",
        "source": "chat",
        "schedule_id": None,
        "occurrence_id": None,
        "role": "root",
        "parent_session_id": None,
        "progress": 1.0,
        "current_stage": "completed",
        "result_summary": "图表已生成。",
        "content_parts": '[{"type":"text","text":"图表已生成。"}]',
        "result_ref": "null",
        "artifact_refs": "[]",
        "error": "null",
        "delivery_status": None,
        "delivery_id": None,
        "delivery_attempt_count": 0,
        "delivery_response_summary": None,
        "skill_activations": "[]",
        "source_version": 2,
        "projected_at": datetime.now(UTC),
    }

    view = PostgresTaskProjection._task_from_row(row)

    assert view["content_parts"] == [{"type": "text", "text": "图表已生成。"}]

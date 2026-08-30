import asyncio

import pytest

from auraclaw.api.models import TaskView
from auraclaw.contracts.commands import CommandContext
from auraclaw.contracts.events import Actor, CanonicalEvent, NewEvent, utc_now
from auraclaw.contracts.state import Visibility
from auraclaw.gateways.query.reader import TaskQueryService
from auraclaw.gateways.task.admission import AllowAllAdmissionController
from auraclaw.infrastructure.persistence.memory_event_store import InMemoryEventStore
from auraclaw.projection.maintenance import ProjectionMaintenanceService
from auraclaw.projection.relay import OutboxRelay
from auraclaw.projection.task.projector import (
    InMemoryTaskProjection,
    ProjectionGapError,
    UnsupportedEventError,
)
from auraclaw.session.task_service import TaskService


def _context(command_id: str, *, operation: str = "create_task") -> CommandContext:
    return CommandContext(
        command_id=command_id,
        tenant_id="tenant-1",
        actor=Actor(type="user", id="user-1"),
        correlation_id="corr-1",
        expected_version=0,
        operation=operation,
    )


def _service() -> tuple[TaskService, InMemoryEventStore, InMemoryTaskProjection]:
    store = InMemoryEventStore()
    projection = InMemoryTaskProjection()
    service = TaskService(
        event_store=store,
        relay=OutboxRelay(store, projection),
        reader=projection,
        admission=AllowAllAdmissionController(),
    )
    return service, store, projection


def test_100_concurrent_idempotent_creates_produce_one_root_session() -> None:
    async def scenario() -> None:
        service, store, _ = _service()
        responses = await asyncio.gather(
            *(service.create_task(goal="same task", context=_context("stable")) for _ in range(100))
        )

        assert len({response["session_id"] for response in responses}) == 1
        events = await store.load_all("tenant-1")
        assert [event.type for event in events] == ["session.created", "run.requested"]
        assert await store.pending_outbox() == []

    asyncio.run(scenario())


def test_snapshot_restores_session_and_projection_rebuild_is_deterministic() -> None:
    async def scenario() -> None:
        service, store, projection = _service()
        created = await service.create_task(goal="rebuild me", context=_context("create"))
        session_id = str(created["session_id"])
        snapshot = await store.get_snapshot("tenant-1", session_id)
        assert snapshot is not None
        assert snapshot.aggregate_version == 2

        before = await projection.get_task("tenant-1", session_id)
        await projection.clear()
        assert await projection.get_task("tenant-1", session_id) is None
        rebuilt = await ProjectionMaintenanceService(store, projection).rebuild_tasks("tenant-1")
        after = await projection.get_task("tenant-1", session_id)

        assert rebuilt == 2
        assert before is not None and after is not None
        for field in ("session_id", "status", "goal", "projection_version"):
            assert before[field] == after[field]

    asyncio.run(scenario())


def test_content_parts_survive_projection_and_result_api() -> None:
    async def scenario() -> None:
        parts = [
            {"type": "text", "text": "图表已生成。"},
            {"type": "chatbi_chart", "componentKey": "metric-comparison"},
        ]
        common = dict(
            tenant_id="tenant-1",
            root_session_id="ses-1",
            session_id="ses-1",
            run_id="run-1",
            occurred_at=utc_now(),
            actor=Actor(type="user", id="user-1"),
            correlation_id="corr-1",
            causation_id="cmd-1",
            visibility=Visibility.USER,
            schema_version=1,
        )
        projection = InMemoryTaskProjection()
        await projection.project(
            [
                CanonicalEvent(
                    event_id="evt-1",
                    aggregate_version=1,
                    type="session.created",
                    payload={"goal": "build a chart", "role": "root"},
                    **common,
                ),
                CanonicalEvent(
                    event_id="evt-2",
                    aggregate_version=2,
                    type="run.completed",
                    payload={"result_summary": "图表已生成。", "content_parts": parts},
                    **common,
                ),
            ]
        )

        view = await projection.get_task("tenant-1", "ses-1")
        assert view is not None
        assert TaskView.model_validate(view).content_parts == parts
        query = TaskQueryService(projection, object(), object())  # type: ignore[arg-type]
        result = await query.get_result("tenant-1", "ses-1")
        assert result["content_parts"] == parts

        legacy_projection = InMemoryTaskProjection()
        await legacy_projection.project(
            [
                CanonicalEvent(
                    event_id="legacy-evt-1",
                    aggregate_version=1,
                    type="session.created",
                    payload={"goal": "legacy chart", "role": "root"},
                    **common,
                ),
                CanonicalEvent(
                    event_id="legacy-evt-2",
                    aggregate_version=2,
                    type="run.completed",
                    payload={"result_summary": "旧事件文字结果"},
                    **common,
                ),
            ]
        )
        legacy_view = await legacy_projection.get_task("tenant-1", "ses-1")
        assert legacy_view is not None
        assert legacy_view["content_parts"] == [
            {"type": "text", "text": "旧事件文字结果"}
        ]

    asyncio.run(scenario())


def test_event_and_outbox_are_committed_together() -> None:
    async def scenario() -> None:
        store = InMemoryEventStore()
        result = await store.append(
            root_session_id="ses-1",
            session_id="ses-1",
            run_id="run-1",
            context=_context("atomic"),
            events=[NewEvent(type="session.created", payload={"goal": "atomic"})],
            command_result={"session_id": "ses-1"},
        )
        outbox = await store.pending_outbox()
        assert len(result.events) == len(outbox) == 1
        assert result.events[0].event_id == outbox[0].event_id

    asyncio.run(scenario())


def test_projector_rejects_gap_and_unknown_critical_event() -> None:
    async def scenario() -> None:
        projection = InMemoryTaskProjection()
        base = dict(
            event_id="evt-1",
            tenant_id="tenant-1",
            root_session_id="ses-1",
            session_id="ses-1",
            run_id=None,
            occurred_at=utc_now(),
            actor=Actor(type="user", id="user-1"),
            correlation_id="corr-1",
            causation_id="cmd-1",
            visibility=Visibility.INTERNAL,
            schema_version=1,
            payload={},
        )
        with pytest.raises(ProjectionGapError):
            await projection.project(
                [CanonicalEvent(aggregate_version=2, type="run.requested", **base)]
            )
        with pytest.raises(UnsupportedEventError):
            await projection.project(
                [CanonicalEvent(aggregate_version=1, type="future.critical", **base)]
            )

    asyncio.run(scenario())

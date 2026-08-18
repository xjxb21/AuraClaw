from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx
import pytest
from pydantic import ValidationError

from auraclaw.config import Settings
from auraclaw.contracts.auth import AgentSessionAuthRequest, AgentSessionBinding
from auraclaw.contracts.commands import CommandContext
from auraclaw.contracts.errors import AuthorizationError, ReauthorizationRequiredError
from auraclaw.contracts.events import Actor
from auraclaw.contracts.tools import (
    RiskLevel,
    ToolCapability,
    ToolInvocation,
    ToolPermission,
)
from auraclaw.control.runnable_feed import RunnableFeedConsumer
from auraclaw.gateways.task.admission import AllowAllAdmissionController
from auraclaw.infrastructure.clients.agent_runtime_auth import (
    JavaAgentRuntimeAuthClient,
    tool_request_hash,
)
from auraclaw.infrastructure.clients.java_price_insight import JavaPriceInsightToolExecutor
from auraclaw.infrastructure.persistence.memory_event_store import InMemoryEventStore
from auraclaw.projection.relay import OutboxRelay
from auraclaw.projection.task.projector import InMemoryTaskProjection
from auraclaw.session.task_service import TaskService


class _FakeAuth:
    def __init__(self) -> None:
        self.requests: list[dict[str, str]] = []

    async def issue_tool_assertion(
        self,
        *,
        agent_session_id: str,
        conversation_id: str,
        tool_code: str,
        invocation_id: str,
        request_hash: str,
    ) -> str:
        self.requests.append(
            {
                "agent_session_id": agent_session_id,
                "conversation_id": conversation_id,
                "tool_code": tool_code,
                "invocation_id": invocation_id,
                "request_hash": request_hash,
            }
        )
        return f"assertion-{len(self.requests)}"


class _FakeAuthorizer:
    """Consumes sensitive API proof and returns only the persistable binding."""

    async def bind(
        self,
        request: AgentSessionAuthRequest,
        *,
        default_conversation_id: str,
    ) -> AgentSessionBinding:
        assert request.access_token == "page-access-token"
        assert request.handoff_code is None
        return AgentSessionBinding(
            agent_session_id="agent-session-from-java",
            conversation_id=request.conversation_id or default_conversation_id,
            resolved_by="resolve",
        )


async def _java_executor(
    handler: Callable[[httpx.Request], Awaitable[httpx.Response]],
    *,
    assertion_retry_count: int = 1,
) -> tuple[JavaPriceInsightToolExecutor, _FakeAuth]:
    auth = _FakeAuth()
    executor = JavaPriceInsightToolExecutor(
        auth=auth,  # type: ignore[arg-type]
        base_url="http://java-agent-runtime",
        assertion_retry_count=assertion_retry_count,
    )
    await executor._client.aclose()
    object.__setattr__(
        executor,
        "_client",
        httpx.AsyncClient(
            base_url="http://java-agent-runtime",
            transport=httpx.MockTransport(handler),
        ),
    )
    return executor, auth


def _capability(name: str) -> ToolCapability:
    return ToolCapability(
        name=name,
        version="1.0.0",
        description="test",
        input_schema={"type": "object"},
        output_schema={"type": "object"},
        permission=ToolPermission.READ_ONLY,
        risk_level=RiskLevel.LOW,
    )


def _invocation(name: str) -> ToolInvocation:
    return ToolInvocation(
        tool_invocation_id="inv-price-1",
        tenant_id="tenant-a",
        root_session_id="root-a",
        session_id="session-a",
        run_id="run-a",
        tool_name=name,
        tool_version="1.0.0",
        arguments={
            "filter": {
                "period_from": "2026-01",
                "period_to": "2026-03",
            }
        },
        expected_side_effect="read",
        idempotency_key="inv-price-1",
        deadline=None,
        fencing_token=1,
        actor_id="runtime-a",
        agent_auth=AgentSessionBinding(
            agent_session_id="agent-session-1",
            conversation_id="conversation-1",
        ),
    )


def test_request_hash_is_stable_for_canonical_json_order() -> None:
    first = tool_request_hash(
        "POST",
        "/rpc-api/agent-runtime/price-insight/tools/dataset/profile",
        body={"b": 2, "a": {"y": 1, "x": 0}},
        invocation_id="inv-1",
    )
    second = tool_request_hash(
        "POST",
        "/rpc-api/agent-runtime/price-insight/tools/dataset/profile",
        body={"a": {"x": 0, "y": 1}, "b": 2},
        invocation_id="inv-1",
    )

    assert first == second
    assert first.startswith("sha256:")


def test_request_hash_matches_java_shared_vector() -> None:
    """Guard Python hashing against Java's literal CT-TOOL-REQUEST-V2 vector."""
    result = tool_request_hash(
        "POST",
        "/rpc-api/agent-runtime/price-insight/tools/dataset/profile",
        body={"b": 2, "a": 1},
        invocation_id="inv-001",
    )

    assert result == (
        "sha256:437817097ddfafeff42e6023c2106c634b19b11d28e07b34439a6ee3dba989c4"
    )


def test_runnable_feed_propagates_agent_auth_to_required_capability() -> None:
    event = _event(
        "run.requested",
        {
            "run_id": "run-a",
            "auth": {
                "agent_session_id": "agent-session-1",
                "conversation_id": "conversation-1",
            },
        },
    )
    item = RunnableFeedConsumer._derive_from_record(_record(event))

    assert item is not None
    assert item.required_capability["agent_auth"] == {
        "agent_session_id": "agent-session-1",
        "conversation_id": "conversation-1",
    }


def test_access_token_is_consumed_before_session_events_are_persisted() -> None:
    async def scenario() -> None:
        event_store = InMemoryEventStore()
        projection = InMemoryTaskProjection()
        service = TaskService(
            event_store=event_store,
            relay=OutboxRelay(event_store, projection),
            reader=projection,
            admission=AllowAllAdmissionController(),
            agent_session_authorizer=_FakeAuthorizer(),
        )
        created = await service.create_task(
            goal="analyze governed prices",
            context=_command_context(),
            agent_auth=AgentSessionAuthRequest(
                conversation_id="conversation-1",
                access_token="page-access-token",
            ),
        )

        events = await event_store.load("tenant-a", str(created["session_id"]))
        serialized = repr([event.as_dict() for event in events])
        assert "agent-session-from-java" in serialized
        assert "page-access-token" not in serialized

    asyncio.run(scenario())


def test_bare_agent_session_id_is_rejected_before_event_persistence() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            return httpx.Response(500)

        http_client = httpx.AsyncClient(
            base_url="http://java-agent-runtime",
            transport=httpx.MockTransport(handler),
        )
        authorizer = JavaAgentRuntimeAuthClient(
            "http://java-agent-runtime",
            "test-workload-token-with-32-characters-minimum",
            client=http_client,
        )
        event_store = InMemoryEventStore()
        projection = InMemoryTaskProjection()
        service = TaskService(
            event_store=event_store,
            relay=OutboxRelay(event_store, projection),
            reader=projection,
            admission=AllowAllAdmissionController(),
            agent_session_authorizer=authorizer,
        )

        with pytest.raises(AuthorizationError, match="handoffCode"):
            await service.create_task(
                goal="reject an unproven AgentSession",
                context=_command_context(),
                agent_auth=AgentSessionAuthRequest(
                    agent_session_id="agent-session-without-proof",
                    conversation_id="conversation-1",
                ),
            )
        await authorizer.aclose()

        assert requests == []
        assert await event_store.load_all() == []

    asyncio.run(scenario())


def test_java_auth_client_uses_shared_workload_token_and_unwraps_common_result() -> None:
    async def scenario() -> None:
        requests: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            requests.append(request)
            body = json.loads(request.content.decode())
            assert body == {
                "conversationId": "conversation-1",
                "handoffCode": "one-time-handoff",
            }
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "",
                    "data": {
                        "agentSessionId": "agent-session-1",
                        "conversationId": "conversation-1",
                    },
                },
            )

        http_client = httpx.AsyncClient(
            base_url="http://java-agent-runtime",
            transport=httpx.MockTransport(handler),
        )
        client = JavaAgentRuntimeAuthClient(
            "http://java-agent-runtime",
            "test-workload-token-with-32-characters-minimum",
            client=http_client,
        )
        binding = await client.bind(
            AgentSessionAuthRequest(
                agent_session_id="agent-session-1",
                conversation_id="conversation-1",
                handoff_code="one-time-handoff",
            ),
            default_conversation_id="unused",
        )
        await client.aclose()

        assert binding.agent_session_id == "agent-session-1"
        assert binding.resolved_by == "claim"
        assert requests[0].url.path.endswith("/agent-session-1/claim")
        assert requests[0].headers["Authorization"] == (
            "Bearer test-workload-token-with-32-characters-minimum"
        )

    asyncio.run(scenario())


def test_java_auth_client_rejects_short_workload_token() -> None:
    # Reject a deployment placeholder during composition instead of waiting
    # for the first Java authorization request to fail remotely.
    with pytest.raises(ValueError, match="too short"):
        JavaAgentRuntimeAuthClient("http://java-agent-runtime", "short")


@pytest.mark.parametrize(
    "java_code",
    (1_013_000_000, 1_013_000_001, 1_013_000_009, 1_013_000_010),
)
def test_java_auth_client_maps_invalid_session_or_grant_to_reauthorization(
    java_code: int,
) -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"code": java_code, "msg": "authorization is no longer active"},
            )

        client = JavaAgentRuntimeAuthClient(
            "http://java-agent-runtime",
            "test-workload-token-with-32-characters-minimum",
            client=httpx.AsyncClient(
                base_url="http://java-agent-runtime",
                transport=httpx.MockTransport(handler),
            ),
        )

        with pytest.raises(ReauthorizationRequiredError, match="no longer active"):
            await client.issue_tool_assertion(
                agent_session_id="agent-session-1",
                conversation_id="conversation-1",
                tool_code="POST /tools/example",
                invocation_id="invocation-1",
                request_hash="sha256:test",
            )
        await client.aclose()

    asyncio.run(scenario())


def test_settings_rejects_more_than_one_assertion_retry() -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, java_tool_assertion_retry_count=2)


def test_price_insight_tool_backend_defaults_to_java(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("AURACLAW_PRICE_INSIGHT_TOOL_BACKEND", raising=False)
    settings = Settings(_env_file=None)
    assert settings.price_insight_tool_backend == "java"
    assert settings.java_price_insight_configured is False


def test_java_price_insight_executor_issues_assertion_and_calls_java_tool() -> None:
    async def scenario() -> None:
        calls: list[httpx.Request] = []

        async def handler(request: httpx.Request) -> httpx.Response:
            calls.append(request)
            assert request.headers["X-CT-Tool-Assertion"] == "assertion-1"
            assert request.headers["X-CT-Invocation-Id"] == "inv-price-1"
            body = json.loads(request.content.decode())
            assert body["user_id"] == 100
            assert body["filter"]["period_from"] == "2026-01"
            return httpx.Response(
                200,
                json={
                    "code": 0,
                    "msg": "",
                    "data": {
                        "operation": "scope.profile",
                        "source_revision": "java-price-v1",
                        "records": 1,
                        "comparisons": 0,
                        "benchmarks": 0,
                        "rules": 0,
                        "effectiveRule": {"rule_code": "RULE-001"},
                        "eligibleRecords": 1,
                        "periods": ["2026-01"],
                        "tablesRead": ["dwd_pr_price_event_detail_di"],
                    },
                },
            )

        executor, auth = await _java_executor(handler)

        result = await executor.execute(
            _invocation("procurement.price.dataset.profile"),
            _capability("procurement.price.dataset.profile"),
        )

        assert result["source_revision"] == "java-price-v1"
        assert result["eligible_records"] == 1
        assert result["tables_read"] == ["dwd_pr_price_event_detail_di"]
        assert result["effective_rule"] == {"rule_code": "RULE-001"}
        assert len(auth.requests) == 1
        assert auth.requests[0]["agent_session_id"] == "agent-session-1"
        assert auth.requests[0]["conversation_id"] == "conversation-1"
        assert (
            auth.requests[0]["tool_code"]
            == "POST /rpc-api/agent-runtime/price-insight/tools/dataset/profile"
        )
        assert len(calls) == 1
        await executor.aclose()

    asyncio.run(scenario())


def test_java_body_preserves_omitted_threshold() -> None:
    """An omitted threshold must remain absent so Java may select a DWD rule."""
    from auraclaw.infrastructure.clients.java_price_insight import _java_body

    body = _java_body(
        _invocation("procurement.price.dataset.profile"),
        user_id=100,
    )

    assert "deviation_threshold_pct" not in body["filter"]


def test_java_executor_reissues_assertion_once_after_assertion_expired() -> None:
    async def scenario() -> None:
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return httpx.Response(
                    200,
                    json={"code": 1_013_000_013, "msg": "Tool Assertion expired"},
                )
            return httpx.Response(
                200,
                json={"code": 0, "msg": "", "data": {"operation": "scope.profile"}},
            )

        executor, auth = await _java_executor(handler)

        result = await executor.execute(
            _invocation("procurement.price.dataset.profile"),
            _capability("procurement.price.dataset.profile"),
        )
        await executor.aclose()

        assert result["operation"] == "scope.profile"
        assert call_count == 2
        assert len(auth.requests) == 2

    asyncio.run(scenario())


def test_java_executor_does_not_retry_transport_authorization_failure() -> None:
    async def scenario() -> None:
        call_count = 0

        async def handler(request: httpx.Request) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            return httpx.Response(401, json={"code": 401, "msg": "unauthorized"})

        executor, auth = await _java_executor(handler)
        with pytest.raises(AuthorizationError, match="authorization was rejected"):
            await executor.execute(
                _invocation("procurement.price.dataset.profile"),
                _capability("procurement.price.dataset.profile"),
            )
        await executor.aclose()

        assert call_count == 1
        assert len(auth.requests) == 1

    asyncio.run(scenario())


def test_java_executor_maps_revoked_grant_to_reauthorization_required() -> None:
    async def scenario() -> None:
        async def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                json={"code": 1_013_000_010, "msg": "Agent Grant revoked"},
            )

        executor, auth = await _java_executor(handler)
        with pytest.raises(ReauthorizationRequiredError, match="Grant revoked"):
            await executor.execute(
                _invocation("procurement.price.dataset.profile"),
                _capability("procurement.price.dataset.profile"),
            )
        await executor.aclose()

        assert len(auth.requests) == 1

    asyncio.run(scenario())


def test_java_executor_rejects_more_than_one_assertion_retry() -> None:
    with pytest.raises(ValueError, match="zero or one"):
        JavaPriceInsightToolExecutor(
            auth=_FakeAuth(),  # type: ignore[arg-type]
            base_url="http://java-agent-runtime",
            assertion_retry_count=2,
        )


def _command_context() -> CommandContext:
    return CommandContext(
        command_id="cmd-agent-auth",
        tenant_id="tenant-a",
        actor=Actor(type="user", id="user-a"),
        correlation_id="corr-agent-auth",
        expected_version=0,
        operation="create_task",
    )


def _event(event_type: str, payload: dict[str, Any]) -> Any:
    from auraclaw.contracts.events import Actor, CanonicalEvent
    from auraclaw.contracts.state import Visibility

    return CanonicalEvent(
        event_id=f"evt-{event_type}",
        tenant_id="tenant-a",
        root_session_id="root-a",
        session_id="session-a",
        run_id=payload.get("run_id"),
        aggregate_version=2,
        type=event_type,
        occurred_at=datetime(2026, 8, 16, tzinfo=UTC),
        actor=Actor(type="user", id="user-a"),
        correlation_id="corr-a",
        causation_id="cmd-a",
        visibility=Visibility.USER,
        schema_version=1,
        payload=payload,
    )


def _record(event: Any) -> Any:
    from auraclaw.session.ports import ClaimedOutboxRecord

    return ClaimedOutboxRecord(
        outbox_id="outbox-a",
        event_id=event.event_id,
        event=event,
        claim_token="claim-a",
        attempt=1,
    )

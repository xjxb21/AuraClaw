from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from auraclaw.contracts.errors import AuraClawError, LeaseConflictError, ModelProviderError
from auraclaw.contracts.internal import (
    InternalRequestContext,
    ModelGenerateRequest,
    ModelGenerateResponse,
    ModelStreamEvent,
    ServiceIdentity,
)
from auraclaw.infrastructure.clients.model import RemoteModelClient
from auraclaw.internal.http import create_contract_app, stream_contract_route
from auraclaw.model_gateway.internal_service import ModelGatewayInternalService
from auraclaw.model_gateway.ports import ModelCallReservation
from auraclaw.runtime.ports import ModelPolicy, ModelRequest, ModelResponse, ModelStreamChunk


class _StreamingModel:
    async def generate_stream(self, request: Any):
        del request
        yield ModelStreamChunk(kind="delta", delta="hi")
        yield ModelStreamChunk(
            kind="completed",
            response=ModelResponse(
                model_call_id="mdl_1",
                provider="test",
                model="test",
                completed_output="hi",
                deltas=("hi",),
                tool_calls=(),
                finish_reason="stop",
                usage={"input_tokens": 1, "output_tokens": 1},
            ),
        )


class _OrderedState:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.complete_started = asyncio.Event()
        self.allow_complete = asyncio.Event()

    async def reserve(self, **kwargs: Any) -> ModelCallReservation:
        del kwargs
        return ModelCallReservation(status="reserved")

    async def fail(self, **kwargs: Any) -> None:
        del kwargs

    async def complete(self, **kwargs: Any) -> None:
        del kwargs
        self.events.append("complete_started")
        self.complete_started.set()
        await self.allow_complete.wait()
        self.events.append("complete_finished")


def _gateway_request() -> ModelGenerateRequest:
    return ModelGenerateRequest(
        context=InternalRequestContext(
            tenant_id="t1",
            service_identity=ServiceIdentity.AGENT_RUNTIME,
            request_id="req_1",
            correlation_id="run_1",
            causation_id="req_1",
        ),
        model_call_id="mdl_1",
        run_id="run_1",
        messages=[{"role": "user", "content": "hi"}],
        tools=(),
        capability="general",
        preferred_model=None,
        allowed_providers=(),
        data_classification="internal",
        max_output_tokens=64,
    )


@pytest.mark.asyncio
async def test_gateway_yields_completed_before_persisting() -> None:
    state = _OrderedState()
    service = ModelGatewayInternalService(_StreamingModel(), state=state)
    events: list[str] = []

    async def consume() -> None:
        async for event in service.generate_stream(_gateway_request()):
            events.append(event.type)
            if event.type == "completed":
                assert "complete_started" not in state.events
                state.allow_complete.set()

    await consume()
    assert events == ["delta", "completed"]
    assert state.events == ["complete_started", "complete_finished"]


@pytest.mark.asyncio
async def test_gateway_still_returns_completed_when_persist_fails() -> None:
    class _FailState:
        async def reserve(self, **kwargs: Any) -> ModelCallReservation:
            del kwargs
            return ModelCallReservation(status="reserved")

        async def fail(self, **kwargs: Any) -> None:
            del kwargs

        async def complete(self, **kwargs: Any) -> None:
            del kwargs
            raise RuntimeError("db unavailable")

    service = ModelGatewayInternalService(_StreamingModel(), state=_FailState())
    events = [event async for event in service.generate_stream(_gateway_request())]
    assert [event.type for event in events] == ["delta", "completed"]
    assert events[-1].payload["completed_output"] == "hi"


@pytest.mark.asyncio
async def test_remote_client_reconnects_once_for_missing_completed() -> None:
    completed = ModelGenerateResponse(
        model_call_id="mdl_1",
        provider="test",
        model="test",
        completed_output="hello",
        deltas=("hello",),
        tool_calls=(),
        finish_reason="stop",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    calls = {"n": 0}

    async def fake_stream(
        path: str,
        request: Any,
        event_model: type[ModelStreamEvent],
    ):
        del path, request, event_model
        calls["n"] += 1
        if calls["n"] == 1:
            yield ModelStreamEvent(
                model_call_id="mdl_1",
                sequence=1,
                type="delta",
                payload={"delta": "hel"},
            )
            return
        yield ModelStreamEvent(
            model_call_id="mdl_1",
            sequence=1,
            type="delta",
            payload={"delta": "hello"},
        )
        yield ModelStreamEvent(
            model_call_id="mdl_1",
            sequence=2,
            type="completed",
            payload=completed.model_dump(mode="json"),
        )

    client = RemoteModelClient("http://model.test", bearer_token="token")
    client._contract.stream = fake_stream  # type: ignore[method-assign]
    chunks = [
        chunk
        async for chunk in client.generate_stream(
            ModelRequest(
                model_call_id="mdl_1",
                tenant_id="t1",
                run_id="run_1",
                messages=({"role": "user", "content": "hi"},),
                policy=ModelPolicy(),
            )
        )
    ]
    await client.aclose()
    assert calls["n"] == 2
    assert [chunk.kind for chunk in chunks] == ["delta", "completed"]
    assert chunks[0].delta == "hel"
    assert chunks[1].response is not None
    assert chunks[1].response.completed_output == "hello"


@pytest.mark.asyncio
async def test_remote_client_waits_for_in_progress_call_after_peer_closed_stream(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = ModelGenerateResponse(
        model_call_id="mdl_1",
        provider="test",
        model="test",
        completed_output="hello",
        deltas=("hello",),
        tool_calls=(),
        finish_reason="stop",
        usage={"input_tokens": 1, "output_tokens": 1},
    )
    calls = {"n": 0}

    async def fake_stream(
        path: str,
        request: Any,
        event_model: type[ModelStreamEvent],
    ):
        del path, request, event_model
        calls["n"] += 1
        if calls["n"] == 1:
            yield ModelStreamEvent(
                model_call_id="mdl_1",
                sequence=1,
                type="delta",
                payload={"delta": "hel"},
            )
            raise AuraClawError(
                "internal stream was closed by the peer",
                detail="ReadError",
            )
        if calls["n"] < 4:
            raise LeaseConflictError("model call is already in progress")
        yield ModelStreamEvent(
            model_call_id="mdl_1",
            sequence=2,
            type="completed",
            payload=completed.model_dump(mode="json"),
        )

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    client = RemoteModelClient("http://model.test", bearer_token="token")
    client._contract.stream = fake_stream  # type: ignore[method-assign]
    chunks = [
        chunk
        async for chunk in client.generate_stream(
            ModelRequest(
                model_call_id="mdl_1",
                tenant_id="t1",
                run_id="run_1",
                messages=({"role": "user", "content": "hi"},),
                policy=ModelPolicy(),
            )
        )
    ]
    await client.aclose()
    assert calls["n"] == 4
    assert [chunk.kind for chunk in chunks] == ["delta", "completed"]
    assert chunks[1].response is not None
    assert chunks[1].response.completed_output == "hello"


@pytest.mark.asyncio
async def test_stream_handler_error_is_sent_to_remote_model_client() -> None:
    async def failing_stream(_request: ModelGenerateRequest):
        raise ModelProviderError("model provider request failed")
        yield  # pragma: no cover

    app = create_contract_app(
        "model-gateway",
        {},
        stream_routes={
            "/internal/v1/model/stream": stream_contract_route(
                ModelGenerateRequest,
                ModelStreamEvent,
                failing_stream,
            )
        },
    )
    client = RemoteModelClient(
        "http://model.test",
        bearer_token="token",
        transport=httpx.ASGITransport(app=app),
    )
    with pytest.raises(RuntimeError, match="model provider request failed"):
        async for _chunk in client.generate_stream(
            ModelRequest(
                model_call_id="mdl_1",
                tenant_id="t1",
                run_id="run_1",
                messages=({"role": "user", "content": "hi"},),
                policy=ModelPolicy(),
            )
        ):
            pass
    await client.aclose()


def _completed_response() -> ModelGenerateResponse:
    return ModelGenerateResponse(
        model_call_id="mdl_1",
        provider="test",
        model="test",
        completed_output="hello",
        deltas=("hello",),
        tool_calls=(),
        finish_reason="stop",
        usage={"input_tokens": 1, "output_tokens": 1},
    )


@pytest.mark.asyncio
async def test_gateway_waits_for_inflight_and_replays_cache() -> None:
    cached = _completed_response()

    class _MustNotCall:
        async def generate_stream(self, request: Any):
            del request
            raise AssertionError("in-flight call should be replayed from cache")
            yield  # pragma: no cover

    class _InflightThenComplete:
        def __init__(self) -> None:
            self.reserves = 0

        async def reserve(self, **kwargs: Any) -> ModelCallReservation:
            del kwargs
            self.reserves += 1
            if self.reserves == 1:
                return ModelCallReservation("in_progress")
            return ModelCallReservation("completed", cached)

        async def fail(self, **kwargs: Any) -> None:
            del kwargs

        async def complete(self, **kwargs: Any) -> None:
            del kwargs

    state = _InflightThenComplete()
    service = ModelGatewayInternalService(
        _MustNotCall(),
        state=state,
        inflight_wait_seconds=2.0,
        inflight_poll_seconds=0.01,
    )
    events = [event async for event in service.generate_stream(_gateway_request())]
    assert [event.type for event in events] == ["delta", "completed"]
    assert events[-1].payload["completed_output"] == "hello"
    assert state.reserves >= 2


@pytest.mark.asyncio
async def test_gateway_releases_reservation_when_stream_is_cancelled() -> None:
    class _HangingModel:
        def __init__(self) -> None:
            self.started = asyncio.Event()

        async def generate_stream(self, request: Any):
            del request
            self.started.set()
            yield ModelStreamChunk(kind="delta", delta="hi")
            await asyncio.Event().wait()
            yield ModelStreamChunk(
                kind="completed",
                response=ModelResponse(
                    model_call_id="mdl_1",
                    provider="test",
                    model="test",
                    completed_output="hi",
                    deltas=("hi",),
                    tool_calls=(),
                    finish_reason="stop",
                    usage={},
                ),
            )

    class _TrackFail:
        failed_with: str | None = None

        async def reserve(self, **kwargs: Any) -> ModelCallReservation:
            del kwargs
            return ModelCallReservation("reserved")

        async def fail(self, **kwargs: Any) -> None:
            self.failed_with = str(kwargs.get("error_code"))

        async def complete(self, **kwargs: Any) -> None:
            del kwargs

    model = _HangingModel()
    state = _TrackFail()
    service = ModelGatewayInternalService(model, state=state)
    events: list[str] = []

    async def consume() -> None:
        async for event in service.generate_stream(_gateway_request()):
            events.append(event.type)

    task = asyncio.create_task(consume())
    await asyncio.wait_for(model.started.wait(), timeout=2)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert events == ["delta"]
    assert state.failed_with == "CancelledError"


@pytest.mark.asyncio
async def test_remote_client_retries_in_progress_error_event(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed = _completed_response()
    calls = {"n": 0}

    async def fake_stream(
        path: str,
        request: Any,
        event_model: type[ModelStreamEvent],
    ):
        del path, request, event_model
        calls["n"] += 1
        if calls["n"] == 1:
            yield ModelStreamEvent(
                model_call_id="mdl_1",
                sequence=1,
                type="error",
                payload={"message": "model call is already in progress"},
            )
            return
        yield ModelStreamEvent(
            model_call_id="mdl_1",
            sequence=2,
            type="completed",
            payload=completed.model_dump(mode="json"),
        )

    async def no_delay(_seconds: float) -> None:
        return None

    monkeypatch.setattr(asyncio, "sleep", no_delay)
    client = RemoteModelClient("http://model.test", bearer_token="token")
    client._contract.stream = fake_stream  # type: ignore[method-assign]
    chunks = [
        chunk
        async for chunk in client.generate_stream(
            ModelRequest(
                model_call_id="mdl_1",
                tenant_id="t1",
                run_id="run_1",
                messages=({"role": "user", "content": "hi"},),
                policy=ModelPolicy(),
            )
        )
    ]
    await client.aclose()
    assert calls["n"] == 2
    assert [chunk.kind for chunk in chunks] == ["completed"]
    assert chunks[0].response is not None
    assert chunks[0].response.completed_output == "hello"

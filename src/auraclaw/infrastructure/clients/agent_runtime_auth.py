from __future__ import annotations

import hashlib
from collections.abc import Sequence
from typing import Any
from urllib.parse import quote

import httpx
import rfc8785

from auraclaw.contracts.auth import AgentSessionAuthRequest, AgentSessionBinding
from auraclaw.contracts.errors import AuthorizationError, ReauthorizationRequiredError

_CONTENT_TYPE = "application/json"
_HASH_VERSION = "CT-TOOL-REQUEST-V2"
TOOL_ASSERTION_EXPIRED_CODE = 1_013_000_013
REAUTHORIZATION_REQUIRED_CODES = frozenset(
    {
        1_013_000_000,  # AGENT_SESSION_NOT_FOUND
        1_013_000_001,  # AGENT_SESSION_NOT_ACTIVE
        1_013_000_009,  # AGENT_GRANT_EXPIRED
        1_013_000_010,  # AGENT_GRANT_REVOKED
    }
)


class JavaAgentRuntimeError(RuntimeError):
    def __init__(self, java_code: int, message: str) -> None:
        super().__init__(message)
        self.java_code = java_code
        self.message = message


class JavaAgentRuntimeAuthClient:
    """HTTP adapter for Java AgentSession claim/resolve and Tool Assertion APIs."""

    def __init__(
        self,
        base_url: str,
        workload_token: str,
        *,
        client: httpx.AsyncClient | None = None,
        timeout_seconds: float = 30.0,
    ) -> None:
        # Reject weak or accidentally empty deployment values before any task
        # can be accepted. Java applies the same minimum and compares the token
        # in constant time.
        if len(workload_token) < 32:
            raise ValueError("Java Agent Runtime workload token is too short")
        self._workload_token = workload_token
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout_seconds,
        )

    async def bind(
        self,
        request: AgentSessionAuthRequest,
        *,
        default_conversation_id: str,
    ) -> AgentSessionBinding:
        conversation_id = request.conversation_id or default_conversation_id
        if request.agent_session_id and request.handoff_code and not request.access_token:
            return await self.claim_session(
                agent_session_id=request.agent_session_id,
                conversation_id=conversation_id,
                handoff_code=request.handoff_code,
            )
        if request.access_token and not request.agent_session_id and not request.handoff_code:
            return await self.resolve_session(
                conversation_id=conversation_id,
                access_token=request.access_token,
            )
        raise AuthorizationError(
            "agentSessionId with handoffCode or accessToken without agentSessionId is required"
        )

    async def claim_session(
        self,
        *,
        agent_session_id: str,
        conversation_id: str,
        handoff_code: str,
    ) -> AgentSessionBinding:
        data = await self._post_common(
            f"/rpc-api/agent-runtime/auth/agent-sessions/{agent_session_id}/claim",
            {"conversationId": conversation_id, "handoffCode": handoff_code},
        )
        return AgentSessionBinding(
            agent_session_id=str(data.get("agentSessionId", agent_session_id)),
            conversation_id=str(data.get("conversationId", conversation_id)),
            resolved_by="claim",
        )

    async def resolve_session(
        self,
        *,
        conversation_id: str,
        access_token: str,
    ) -> AgentSessionBinding:
        data = await self._post_common(
            "/rpc-api/agent-runtime/auth/agent-sessions/resolve",
            {"conversationId": conversation_id, "accessToken": access_token},
        )
        agent_session_id = data.get("agentSessionId")
        if not agent_session_id:
            raise AuthorizationError("Java Agent Runtime did not return agentSessionId")
        return AgentSessionBinding(
            agent_session_id=str(agent_session_id),
            conversation_id=str(data.get("conversationId", conversation_id)),
            resolved_by="resolve",
        )

    async def issue_tool_assertion(
        self,
        *,
        agent_session_id: str,
        conversation_id: str,
        tool_code: str,
        invocation_id: str,
        request_hash: str,
    ) -> str:
        data = await self._post_common(
            f"/rpc-api/agent-runtime/auth/agent-sessions/{agent_session_id}/tool-assertions",
            {
                "conversationId": conversation_id,
                "toolCode": tool_code,
                "invocationId": invocation_id,
                "requestHash": request_hash,
            },
        )
        assertion = data.get("assertion")
        if not assertion:
            raise AuthorizationError("Java Agent Runtime did not return a Tool Assertion")
        return str(assertion)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _post_common(
        self,
        path: str,
        body: dict[str, Any],
    ) -> dict[str, Any]:
        try:
            response = await self._client.post(
                path,
                json=body,
                headers={
                    # The shared token authenticates the Python workload only
                    # on AgentSession/Assertion endpoints. Business Tool calls
                    # continue to use the short-lived one-time Assertion.
                    "Authorization": f"Bearer {self._workload_token}",
                    "Content-Type": _CONTENT_TYPE,
                },
            )
        except httpx.HTTPError as exc:
            raise AuthorizationError("Java Agent Runtime authorization call failed") from exc
        if response.status_code in {401, 403}:
            raise AuthorizationError("Java Agent Runtime rejected workload authorization")
        if response.status_code >= 400:
            raise AuthorizationError("Java Agent Runtime authorization endpoint failed")
        try:
            return unwrap_common_result(response.json())
        except JavaAgentRuntimeError as exc:
            if exc.java_code in REAUTHORIZATION_REQUIRED_CODES:
                raise ReauthorizationRequiredError(exc.message) from exc
            raise AuthorizationError(exc.message) from exc


def tool_code(method: str, route: str) -> str:
    return f"{method.upper()} {route}"


def tool_request_hash(
    method: str,
    route: str,
    *,
    body: object | None,
    invocation_id: str,
    query_items: Sequence[tuple[object, object]] | None = None,
    content_type: str = _CONTENT_TYPE,
) -> str:
    body_canonical = "null" if body is None else _canonical_json(body)
    envelope = {
        "bodySha256": _sha256_hex(body_canonical),
        "contentType": content_type or "",
        "invocationId": invocation_id or "",
        "method": method or "",
        "query": _canonical_query(query_items),
        "route": route or "",
        "version": _HASH_VERSION,
    }
    return "sha256:" + _sha256_hex(_canonical_json(envelope))


def unwrap_common_result(payload: object) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise RuntimeError("Java Agent Runtime returned a non-object response")
    code = payload.get("code")
    if code != 0:
        message = str(payload.get("msg") or "Java Agent Runtime returned an error")
        raise JavaAgentRuntimeError(_java_error_code(code), message)
    data = payload.get("data")
    return dict(data) if isinstance(data, dict) else {}


def _java_error_code(value: object) -> int:
    if isinstance(value, int):
        return value
    if not isinstance(value, str):
        return -1
    try:
        return int(value)
    except ValueError:
        return -1


def _canonical_json(value: Any) -> str:
    encoded = rfc8785.dumps(value)
    return encoded.decode("utf-8") if isinstance(encoded, bytes) else encoded


def _canonical_query(query_items: Sequence[tuple[object, object]] | None) -> str:
    if not query_items:
        return ""
    pairs: list[tuple[str, str]] = []
    for name, value in query_items:
        encoded_name = quote("" if name is None else str(name), safe="-._~")
        encoded_value = quote("" if value is None else str(value), safe="-._~")
        pairs.append((encoded_name, encoded_value))
    pairs.sort(key=lambda item: (item[0], item[1]))
    return "&".join(f"{name}={value}" for name, value in pairs)


def _sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

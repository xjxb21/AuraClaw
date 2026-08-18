from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

import httpx

from auraclaw.contracts.errors import AuthorizationError, ReauthorizationRequiredError
from auraclaw.contracts.price_insight import PriceInsightFilter
from auraclaw.contracts.tools import ToolCapability, ToolInvocation
from auraclaw.infrastructure.clients.agent_runtime_auth import (
    REAUTHORIZATION_REQUIRED_CODES,
    TOOL_ASSERTION_EXPIRED_CODE,
    JavaAgentRuntimeAuthClient,
    JavaAgentRuntimeError,
    tool_code,
    tool_request_hash,
    unwrap_common_result,
)

_CONTENT_TYPE = "application/json"
_POST = "POST"

# Java DTOs use camelCase for these fields while the existing Python Tool
# capabilities expose snake_case. Keeping the translation here preserves the
# Skill contract and prevents backend selection from changing model-visible data.
_JAVA_RESPONSE_KEY_ALIASES = {
    "affectedCount": "affected_count",
    "comparisonCount": "comparison_count",
    "effectiveRule": "effective_rule",
    "eligibleRecords": "eligible_records",
    "eventCount": "event_count",
    "tablesRead": "tables_read",
}

_DATASET_PROFILE_TOOL = "procurement.price.dataset.profile"
_DATASET_QUALITY_CHECK_TOOL = "procurement.price.dataset.quality.check"
_METRIC_EVIDENCE_LIST_TOOL = "procurement.price.metric.evidence.list"
_LEGACY_SCOPE_PROFILE_TOOL = "procurement.price_insight.scope.profile"
_LEGACY_QUALITY_CHECK_TOOL = "procurement.price_insight.quality.check"
_LEGACY_METRIC_COMPUTE_TOOL = "procurement.price_insight.metric.compute"
_LEGACY_EVIDENCE_LIST_TOOL = "procurement.price_insight.evidence.list"
_LEGACY_DATA_QUALITY_TOOL = "procurement.price_insight.data_quality"
_LEGACY_DRILLDOWN_TOOL = "procurement.price_insight.drilldown"

_METRIC_TOOL_TO_ROUTE = {
    "procurement.price.metric.history-deviation.compute": (
        "history_dev_pct",
        "/rpc-api/agent-runtime/price-insight/tools/metric/history-deviation/compute",
    ),
    "procurement.price.metric.region-max-gap.compute": (
        "region_gap_max",
        "/rpc-api/agent-runtime/price-insight/tools/metric/region-max-gap/compute",
    ),
    "procurement.price.metric.market-deviation.compute": (
        "market_dev_pct",
        "/rpc-api/agent-runtime/price-insight/tools/metric/market-deviation/compute",
    ),
    "procurement.price.metric.positive-impact-amount.compute": (
        "impact_amount",
        "/rpc-api/agent-runtime/price-insight/tools/metric/positive-impact-amount/compute",
    ),
    "procurement.price.metric.negative-impact-amount.compute": (
        "impact_neg_amount",
        "/rpc-api/agent-runtime/price-insight/tools/metric/negative-impact-amount/compute",
    ),
    "procurement.price.metric.positive-impact-share.compute": (
        "impact_share_pct",
        "/rpc-api/agent-runtime/price-insight/tools/metric/positive-impact-share/compute",
    ),
    "procurement.price.metric.negative-impact-share.compute": (
        "impact_neg_share_pct",
        "/rpc-api/agent-runtime/price-insight/tools/metric/negative-impact-share/compute",
    ),
    "procurement.price.metric.market-deviation-count.compute": (
        "deviation_cnt",
        "/rpc-api/agent-runtime/price-insight/tools/metric/market-deviation-count/compute",
    ),
}
_METRIC_KEY_TO_ROUTE = {metric_key: route for metric_key, route in _METRIC_TOOL_TO_ROUTE.values()}

_TOOL_ROUTES = {
    _DATASET_PROFILE_TOOL: "/rpc-api/agent-runtime/price-insight/tools/dataset/profile",
    _DATASET_QUALITY_CHECK_TOOL: (
        "/rpc-api/agent-runtime/price-insight/tools/dataset/quality-check"
    ),
    _METRIC_EVIDENCE_LIST_TOOL: (
        "/rpc-api/agent-runtime/price-insight/tools/metric/evidence/list"
    ),
    _LEGACY_SCOPE_PROFILE_TOOL: "/rpc-api/agent-runtime/price-insight/tools/dataset/profile",
    _LEGACY_QUALITY_CHECK_TOOL: (
        "/rpc-api/agent-runtime/price-insight/tools/dataset/quality-check"
    ),
    _LEGACY_DATA_QUALITY_TOOL: (
        "/rpc-api/agent-runtime/price-insight/tools/dataset/quality-check"
    ),
    _LEGACY_EVIDENCE_LIST_TOOL: (
        "/rpc-api/agent-runtime/price-insight/tools/metric/evidence/list"
    ),
    _LEGACY_DRILLDOWN_TOOL: "/rpc-api/agent-runtime/price-insight/tools/metric/evidence/list",
}


@dataclass(frozen=True)
class JavaPriceInsightToolExecutor:
    """Runs Price Insight ToolCapabilities through Java Agent Runtime Tool APIs."""

    auth: JavaAgentRuntimeAuthClient
    base_url: str
    user_id: int = 100
    timeout_seconds: float = 30.0
    assertion_retry_count: int = 1
    _client: httpx.AsyncClient = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not 0 <= self.assertion_retry_count <= 1:
            raise ValueError("Tool Assertion retry count must be zero or one")
        object.__setattr__(
            self,
            "_client",
            httpx.AsyncClient(
                base_url=self.base_url.rstrip("/"),
                timeout=self.timeout_seconds,
            ),
        )

    async def execute(
        self,
        invocation: ToolInvocation,
        capability: ToolCapability,
    ) -> dict[str, Any]:
        if invocation.agent_auth is None:
            raise AuthorizationError("Java Price Insight Tool requires agentSessionId")
        route = _route_for(invocation)
        body = _java_body(invocation, user_id=self.user_id)
        request_hash = tool_request_hash(
            _POST,
            route,
            body=body,
            invocation_id=invocation.tool_invocation_id,
        )
        for attempt in range(self.assertion_retry_count + 1):
            assertion = await self.auth.issue_tool_assertion(
                agent_session_id=invocation.agent_auth.agent_session_id,
                conversation_id=invocation.agent_auth.conversation_id,
                tool_code=tool_code(_POST, route),
                invocation_id=invocation.tool_invocation_id,
                request_hash=request_hash,
            )
            response = await self._call_java_tool(
                route,
                body,
                assertion=assertion,
                invocation_id=invocation.tool_invocation_id,
            )
            if response.status_code in {401, 403}:
                raise AuthorizationError("Java Price Insight Tool authorization was rejected")
            if response.status_code >= 400:
                raise RuntimeError("Java Price Insight Tool endpoint failed")
            try:
                data = unwrap_common_result(response.json())
            except JavaAgentRuntimeError as exc:
                if (
                    exc.java_code == TOOL_ASSERTION_EXPIRED_CODE
                    and attempt < self.assertion_retry_count
                ):
                    continue
                if exc.java_code in REAUTHORIZATION_REQUIRED_CODES:
                    raise ReauthorizationRequiredError(exc.message) from exc
                raise RuntimeError(exc.message) from exc
            return _normalize_java_result(data)
        raise RuntimeError("Java Price Insight Tool Assertion remained expired after retry")

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call_java_tool(
        self,
        route: str,
        body: dict[str, Any],
        *,
        assertion: str,
        invocation_id: str,
    ) -> httpx.Response:
        # The business Tool request deliberately carries only the one-time
        # Assertion and invocation id; workload Token and tenant/user tokens stay
        # out of the Java Tool execution endpoint.
        return await self._client.post(
            route,
            json=body,
            headers={
                "X-CT-Tool-Assertion": assertion,
                "X-CT-Invocation-Id": invocation_id,
                "Content-Type": _CONTENT_TYPE,
            },
        )


def _route_for(invocation: ToolInvocation) -> str:
    if invocation.tool_name == _LEGACY_METRIC_COMPUTE_TOOL:
        metric_key = str(invocation.arguments["metric_key"])
        try:
            return _METRIC_KEY_TO_ROUTE[metric_key]
        except KeyError as exc:
            raise ValueError(f"Unsupported Price Insight metric key: {metric_key}") from exc
    route = _TOOL_ROUTES.get(invocation.tool_name)
    if route is not None:
        return route
    metric_route = _METRIC_TOOL_TO_ROUTE.get(invocation.tool_name)
    if metric_route is not None:
        return metric_route[1]
    raise ValueError(f"Unsupported Java Price Insight Tool: {invocation.tool_name}")


def _java_body(invocation: ToolInvocation, *, user_id: int) -> dict[str, Any]:
    filters = PriceInsightFilter.model_validate(invocation.arguments["filter"])
    body: dict[str, Any] = {
        "user_id": user_id,
        # Java distinguishes an omitted deviation threshold from an explicit 8:
        # omission allows the uniquely matched governed rule to supply it. Using
        # exclude_unset preserves that distinction after Pydantic validation.
        "filter": _json_ready(filters.model_dump(mode="json", exclude_unset=True)),
    }
    if invocation.tool_name in {
        _METRIC_EVIDENCE_LIST_TOOL,
        _LEGACY_EVIDENCE_LIST_TOOL,
        _LEGACY_DRILLDOWN_TOOL,
    }:
        body.update(
            {
                "metric_key": str(invocation.arguments["metric_key"]),
                "offset": int(invocation.arguments.get("offset", 0)),
                "limit": int(invocation.arguments.get("limit", 50)),
            }
        )
    return body


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return int(value) if value == value.to_integral() else float(value)
    if isinstance(value, dict):
        return {str(key): _json_ready(child) for key, child in value.items()}
    if isinstance(value, list | tuple):
        return [_json_ready(child) for child in value]
    return value


def _normalize_java_result(data: object) -> dict[str, Any]:
    normalized = _normalize_java_response(data)
    return dict(normalized) if isinstance(normalized, dict) else {}


def _normalize_java_response(value: Any) -> Any:
    """Translate only the known Java DTO aliases into the stable Tool schema."""
    if isinstance(value, dict):
        return {
            _JAVA_RESPONSE_KEY_ALIASES.get(str(key), str(key)): _normalize_java_response(
                child
            )
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [_normalize_java_response(child) for child in value]
    return value

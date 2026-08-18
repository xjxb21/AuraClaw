from __future__ import annotations

from auraclaw.config import Settings
from auraclaw.infrastructure.clients.agent_runtime_auth import (
    JavaAgentRuntimeAuthClient,
)
from auraclaw.infrastructure.clients.java_price_insight import (
    JavaPriceInsightToolExecutor,
)


def build_java_agent_runtime_auth_client(
    settings: Settings,
) -> JavaAgentRuntimeAuthClient:
    """Build the shared Java AgentSession authorization adapter from Settings.

    The checks stay in composition so infrastructure clients never read process
    configuration themselves. The shared workload token is unwrapped only at
    this boundary and is never propagated through Session or Tool metadata.
    """
    if not settings.java_price_insight_configured:
        raise ValueError("Java Agent Runtime authorization configuration is incomplete")
    assert settings.java_agent_runtime_base_url is not None
    assert settings.java_agent_runtime_workload_token is not None
    return JavaAgentRuntimeAuthClient(
        settings.java_agent_runtime_base_url,
        settings.java_agent_runtime_workload_token.get_secret_value(),
        timeout_seconds=settings.java_tool_timeout_seconds,
    )


def build_java_price_insight_executor(
    settings: Settings,
    auth_client: JavaAgentRuntimeAuthClient,
) -> JavaPriceInsightToolExecutor:
    """Build the Java-backed executor while preserving Price Insight Tool names."""
    assert settings.java_agent_runtime_base_url is not None
    return JavaPriceInsightToolExecutor(
        auth=auth_client,
        base_url=settings.java_agent_runtime_base_url,
        user_id=settings.java_price_insight_user_id,
        timeout_seconds=settings.java_tool_timeout_seconds,
        assertion_retry_count=settings.java_tool_assertion_retry_count,
    )

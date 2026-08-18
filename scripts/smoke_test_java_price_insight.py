#!/usr/bin/env python3
"""Run the real Java Price Insight authorization and atomic Tool chain."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import os
import sys
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from auraclaw.action.price_insight import (
    PRICE_DATASET_PROFILE_TOOL,
    PRICE_DATASET_QUALITY_CHECK_TOOL,
    PRICE_INSIGHT_METRIC_KEYS,
    PRICE_METRIC_EVIDENCE_LIST_TOOL,
    PRICE_METRIC_TOOLS,
    price_insight_tools,
)
from auraclaw.composition.java_price_insight import (
    build_java_agent_runtime_auth_client,
    build_java_price_insight_executor,
)
from auraclaw.config import Settings, load_secret_files
from auraclaw.contracts.auth import AgentSessionAuthRequest, AgentSessionBinding
from auraclaw.contracts.tools import ToolCapability, ToolInvocation
from auraclaw.infrastructure.clients.agent_runtime_auth import JavaAgentRuntimeAuthClient

_DEFAULT_BASE_URL = "http://192.168.0.100:48080"
_SESSION_ID_ENV = "AURACLAW_JAVA_PRICE_INSIGHT_AGENT_SESSION_ID"
_CONVERSATION_ID_ENV = "AURACLAW_JAVA_PRICE_INSIGHT_CONVERSATION_ID"
_HANDOFF_CODE_ENV = "AURACLAW_JAVA_PRICE_INSIGHT_HANDOFF_CODE"
_ACCESS_TOKEN_ENV = "AURACLAW_JAVA_PRICE_INSIGHT_ACCESS_TOKEN"


@dataclass(frozen=True)
class SmokeOptions:
    """Validated non-secret inputs shared by all eleven Tool calls."""

    base_url: str
    auth_mode: str
    agent_session_id: str | None
    conversation_id: str
    filters: dict[str, Any]
    evidence_metric_key: str
    evidence_limit: int


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Call the Java Agent Runtime Price Insight Tools through the real "
            "shared workload Token and one-time Tool Assertion flow."
        )
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("AURACLAW_JAVA_AGENT_RUNTIME_BASE_URL", _DEFAULT_BASE_URL),
        help=f"Java Agent Runtime base URL (default: {_DEFAULT_BASE_URL})",
    )
    parser.add_argument(
        "--auth-mode",
        choices=("claim", "existing", "resolve"),
        default="claim",
        help="claim a fresh session, reuse an ACTIVE binding, or resolve by access token",
    )
    parser.add_argument(
        "--agent-session-id",
        default=os.environ.get(_SESSION_ID_ENV),
        help=f"AgentSession id; may also be supplied through {_SESSION_ID_ENV}",
    )
    parser.add_argument(
        "--conversation-id",
        default=os.environ.get(_CONVERSATION_ID_ENV),
        help=f"conversation id; may also be supplied through {_CONVERSATION_ID_ENV}",
    )
    parser.add_argument("--period-from", default="2026-01")
    parser.add_argument("--period-to", default="2026-03")
    parser.add_argument("--org-codes", default="", help="comma-separated organization codes")
    parser.add_argument("--region-codes", default="", help="comma-separated region codes")
    parser.add_argument("--category-codes", default="", help="comma-separated category codes")
    parser.add_argument("--material-codes", default="", help="comma-separated material codes")
    parser.add_argument(
        "--evidence-metric-key",
        choices=PRICE_INSIGHT_METRIC_KEYS,
        default="market_dev_pct",
    )
    parser.add_argument("--evidence-limit", type=int, default=10, metavar="1..200")
    return parser


def _parse_options(argv: list[str] | None = None) -> SmokeOptions:
    args = _parser().parse_args(argv)
    if not args.conversation_id:
        raise ValueError("--conversation-id is required")
    if args.auth_mode in {"claim", "existing"} and not args.agent_session_id:
        raise ValueError(f"--agent-session-id is required for {args.auth_mode} mode")
    if not 1 <= args.evidence_limit <= 200:
        raise ValueError("--evidence-limit must be between 1 and 200")
    return SmokeOptions(
        base_url=str(args.base_url).rstrip("/"),
        auth_mode=str(args.auth_mode),
        agent_session_id=(str(args.agent_session_id) if args.agent_session_id else None),
        conversation_id=str(args.conversation_id),
        filters={
            "period_from": str(args.period_from),
            "period_to": str(args.period_to),
            "org_codes": _csv(args.org_codes),
            "region_codes": _csv(args.region_codes),
            "category_codes": _csv(args.category_codes),
            "material_codes": _csv(args.material_codes),
        },
        evidence_metric_key=str(args.evidence_metric_key),
        evidence_limit=int(args.evidence_limit),
    )


def _csv(value: str) -> list[str]:
    return [item.strip() for item in value.split(",") if item.strip()]


def _required_secret(environment_name: str, prompt: str) -> str:
    """Read ephemeral proof without placing it in command-line process metadata."""
    value = os.environ.get(environment_name, "").strip()
    if value:
        return value
    if not sys.stdin.isatty():
        raise ValueError(
            f"{environment_name} is required when no interactive terminal is available"
        )
    value = getpass.getpass(prompt).strip()
    if not value:
        raise ValueError(f"{environment_name} cannot be empty")
    return value


async def _bind_session(
    auth: JavaAgentRuntimeAuthClient,
    options: SmokeOptions,
) -> AgentSessionBinding:
    """Consume sensitive proof once, then retain only the safe AgentSession binding."""
    if options.auth_mode == "existing":
        if options.agent_session_id is None:
            raise ValueError("--agent-session-id is required for existing mode")
        return AgentSessionBinding(
            agent_session_id=options.agent_session_id,
            conversation_id=options.conversation_id,
            resolved_by="existing",
        )
    if options.auth_mode == "claim":
        request = AgentSessionAuthRequest(
            agent_session_id=options.agent_session_id,
            conversation_id=options.conversation_id,
            handoff_code=_required_secret(_HANDOFF_CODE_ENV, "One-time handoff code: "),
        )
    else:
        request = AgentSessionAuthRequest(
            conversation_id=options.conversation_id,
            access_token=_required_secret(_ACCESS_TOKEN_ENV, "Page access token: "),
        )
    return await auth.bind(request, default_conversation_id=options.conversation_id)


def _ordered_capabilities() -> tuple[ToolCapability, ...]:
    """Select only the eleven current atomic Tools, excluding legacy aliases."""
    by_name = {capability.name: capability for capability in price_insight_tools()}
    names = (
        PRICE_DATASET_PROFILE_TOOL,
        PRICE_DATASET_QUALITY_CHECK_TOOL,
        *PRICE_METRIC_TOOLS.values(),
        PRICE_METRIC_EVIDENCE_LIST_TOOL,
    )
    return tuple(by_name[name] for name in names)


def _invocation(
    capability: ToolCapability,
    binding: AgentSessionBinding,
    options: SmokeOptions,
) -> ToolInvocation:
    invocation_id = f"price-insight-smoke-{uuid4().hex}"
    arguments: dict[str, Any] = {"filter": options.filters}
    if capability.name == PRICE_METRIC_EVIDENCE_LIST_TOOL:
        arguments.update(
            {
                "metric_key": options.evidence_metric_key,
                "offset": 0,
                "limit": options.evidence_limit,
            }
        )
    return ToolInvocation(
        tool_invocation_id=invocation_id,
        tenant_id="java-assertion",
        root_session_id=binding.agent_session_id,
        session_id=binding.agent_session_id,
        run_id=invocation_id,
        tool_name=capability.name,
        tool_version=capability.version,
        arguments=arguments,
        expected_side_effect="read",
        idempotency_key=invocation_id,
        deadline=None,
        fencing_token=1,
        actor_id="price-insight-smoke",
        agent_auth=binding,
    )


def _source_revision(tool_name: str, result: dict[str, Any]) -> str:
    revision = result.get("source_revision")
    if not isinstance(revision, str) or not revision:
        raise RuntimeError(f"{tool_name} did not return source_revision")
    return revision


async def _run(options: SmokeOptions) -> None:
    # Settings owns secret-file loading and composition owns client construction;
    # the smoke script therefore exercises the same adapters as the application.
    load_secret_files()
    settings = Settings(
        price_insight_tool_backend="java",
        java_agent_runtime_base_url=options.base_url,
    )
    if not settings.java_price_insight_configured:
        raise ValueError(
            "Java workload configuration is incomplete: configure a workload token "
            "containing at least 32 characters"
        )
    auth = build_java_agent_runtime_auth_client(settings)
    executor = build_java_price_insight_executor(settings, auth)
    try:
        binding = await _bind_session(auth, options)
        revisions: set[str] = set()
        for capability in _ordered_capabilities():
            result = await executor.execute(
                _invocation(capability, binding, options), capability
            )
            revision = _source_revision(capability.name, result)
            revisions.add(revision)
            print(f"PASS {capability.name} source_revision={revision}")
        if len(revisions) != 1:
            raise RuntimeError(
                "Java Price Insight Tools returned inconsistent source revisions"
            )
        print(f"PASS complete tools=11 source_revision={next(iter(revisions))}")
    finally:
        # Both clients own independent connection pools and must be closed even
        # when claim, Assertion issuance, or a business Tool fails.
        await executor.aclose()
        await auth.aclose()


def main(argv: list[str] | None = None) -> int:
    try:
        options = _parse_options(argv)
        asyncio.run(_run(options))
    except (KeyboardInterrupt, EOFError):
        print("ERROR smoke test cancelled", file=sys.stderr)
        return 130
    except Exception as exc:
        # Client exceptions are deliberately sanitized and never contain the
        # workload Token, handoff code, access token, or one-time Assertion.
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

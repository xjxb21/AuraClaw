from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


def _load_script() -> ModuleType:
    path = Path(__file__).parents[2] / "scripts" / "smoke_test_java_price_insight.py"
    spec = importlib.util.spec_from_file_location("smoke_test_java_price_insight", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # Dataclasses resolve postponed annotations through sys.modules while the
    # script is executed, matching Python's normal import behavior.
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_smoke_selects_exactly_eleven_current_atomic_tools() -> None:
    script = _load_script()

    capabilities = script._ordered_capabilities()

    assert len(capabilities) == 11
    assert len({capability.name for capability in capabilities}) == 11
    assert all("price_insight" not in capability.name for capability in capabilities)


def test_smoke_requires_agent_session_for_claim_mode() -> None:
    script = _load_script()

    with pytest.raises(ValueError, match="agent-session-id"):
        script._parse_options(
            ["--auth-mode", "claim", "--conversation-id", "conversation-1"]
        )


def test_smoke_existing_mode_builds_trusted_binding_without_inbound_authorizer() -> None:
    script = _load_script()

    class RejectingAuthorizer:
        async def bind(self, *_args: object, **_kwargs: object) -> None:
            raise AssertionError("existing smoke mode must not call inbound bind")

    options = script._parse_options(
        [
            "--auth-mode",
            "existing",
            "--agent-session-id",
            "agent-session-1",
            "--conversation-id",
            "conversation-1",
        ]
    )

    binding = asyncio.run(script._bind_session(RejectingAuthorizer(), options))

    assert binding.agent_session_id == "agent-session-1"
    assert binding.conversation_id == "conversation-1"
    assert binding.resolved_by == "existing"


def test_smoke_rejects_missing_source_revision() -> None:
    script = _load_script()

    with pytest.raises(RuntimeError, match="source_revision"):
        script._source_revision("procurement.price.dataset.profile", {})

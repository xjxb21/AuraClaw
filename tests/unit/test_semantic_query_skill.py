from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from auraclaw.action.capability_catalog import InMemoryCapabilityCatalogStore
from auraclaw.action.skill_packages import (
    HmacSkillSignatureVerifier,
    SkillPackageRegistry,
    SkillResolver,
)
from auraclaw.composition.business_skills import signed_semantic_query_package
from auraclaw.contracts.capabilities import (
    CapabilityDescriptor,
    CapabilityKind,
    CapabilityStatus,
    CapabilityTrustLevel,
    McpServerDefinition,
)
from auraclaw.infrastructure.artifacts.store import ArtifactStore, InMemoryObjectStorage


def _semantic_tool(name: str) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=f"cap_{name.replace('.', '_')}",
        kind=CapabilityKind.TOOL,
        server_id="java-agent-runtime-mcp",
        canonical_name=name,
        version="1.0.0",
        content_digest="sha256:" + "1" * 64,
        title=name,
        description=name,
        tags=("语义问数",),
        tenant_id="1",
        trust_level=CapabilityTrustLevel.TENANT_VERIFIED,
        classification="internal",
        permission="read-only",
        risk_level="low",
        status=CapabilityStatus.ACTIVE,
        source_revision="test",
        updated_at=datetime.now(UTC),
    )


def test_semantic_query_skill_resolves_java_mcp_tools() -> None:
    async def scenario() -> None:
        tenant_id = "1"
        signer = HmacSkillSignatureVerifier({"platform": b"test-platform-key"})
        registry = SkillPackageRegistry(
            artifacts=ArtifactStore(
                InMemoryObjectStorage(),
                signing_key=b"test-artifact-key",
            ),
            signature_verifier=signer,
        )
        await registry.publish(tenant_id, signed_semantic_query_package(signer))
        catalog = InMemoryCapabilityCatalogStore()
        await catalog.upsert_server(
            McpServerDefinition(
                server_id="java-agent-runtime-mcp",
                tenant_id=tenant_id,
                title="Java Agent Runtime MCP",
                endpoint="http://127.0.0.1:48090/rpc-api/agent-runtime/mcp",
                allowed_private_hosts=("127.0.0.1",),
                status=CapabilityStatus.ACTIVE,
                enabled=True,
            )
        )
        await catalog.replace_capabilities(
            "java-agent-runtime-mcp",
            tuple(
                _semantic_tool(name)
                for name in (
                    "semantic.meta.context",
                    "semantic.query.compile",
                    "semantic.query.execute",
                )
            ),
        )

        binding = await SkillResolver(registry, catalog).resolve(
            tenant_id=tenant_id,
            name="semantic.query.answer",
            version="1.0.0",
            publisher="platform",
            role="root",
            policy_version="test",
        )

        assert binding.skill_name == "semantic.query.answer"
        assert {tool.canonical_name for tool in binding.resolved_tools} == {
            "semantic.meta.context",
            "semantic.query.compile",
            "semantic.query.execute",
        }

    asyncio.run(scenario())

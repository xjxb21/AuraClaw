from __future__ import annotations

import asyncio

from auraclaw.action.capability_catalog import (
    CapabilityCatalog,
    InMemoryCapabilityCatalogStore,
)
from auraclaw.action.mcp_primitives import HandsResourceRegistry, RegisteredResource
from auraclaw.action.skill_packages import (
    HmacSkillSignatureVerifier,
    SkillPackageRegistry,
)
from auraclaw.composition.business_skills import price_insight_resources
from auraclaw.composition.services import (
    _bootstrap_price_insight_capabilities,
    _ensure_price_insight_hands_resources,
    _price_insight_publication_tenants,
)
from auraclaw.config import Settings
from auraclaw.contracts.hands import HandsResourceContent
from auraclaw.infrastructure.artifacts.store import ArtifactStore, InMemoryObjectStorage

_METRIC_URI = "repo://business-skills/price-insight/metric-definitions/1.0.0"


def _skill_registry() -> SkillPackageRegistry:
    key = b"auraclaw-development-platform-skill-key"
    return SkillPackageRegistry(
        artifacts=ArtifactStore(InMemoryObjectStorage(), signing_key=key),
        signature_verifier=HmacSkillSignatureVerifier({"platform": key}),
        resources=HandsResourceRegistry(),
    )


def test_hands_resources_register_without_catalog_io() -> None:
    settings = Settings(
        _env_file=None,
        price_insight_source="fixture",
        deployment_profile="development",
    )
    resources = HandsResourceRegistry()
    assert _ensure_price_insight_hands_resources(settings, resources) > 0
    contents = resources.read("local-org", _METRIC_URI)
    assert contents[0].text


def test_ensure_registers_docs_when_price_insight_source_is_disabled() -> None:
    settings = Settings(
        _env_file=None,
        price_insight_source="disabled",
        deployment_profile="development",
    )
    resources = HandsResourceRegistry()
    assert _ensure_price_insight_hands_resources(settings, resources) > 0
    assert resources.read("local-org", _METRIC_URI)[0].text


def test_hands_gateway_fills_missing_price_insight_resource() -> None:
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    from auraclaw.action.hands import HandsGateway
    from auraclaw.contracts.hands import HandsTrustedContext

    settings = Settings(_env_file=None, price_insight_source="disabled")
    resources = HandsResourceRegistry()

    def fill(uri: str) -> None:
        del uri
        _ensure_price_insight_hands_resources(settings, resources)

    gateway = HandsGateway(
        registry=Mock(),
        gateway=Mock(),
        resources=resources,
        on_missing_resource=fill,
    )
    trusted = HandsTrustedContext(
        tenant_id="local-org",
        root_session_id="root",
        session_id="session",
        run_id="run",
        runtime_id="runtime",
        lease_id="lease",
        fencing_token=1,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    async def scenario() -> None:
        contents = await gateway.read_resource(trusted, _METRIC_URI)
        assert contents[0].text

    asyncio.run(scenario())


def test_publication_tenants_include_workbench_local_org() -> None:
    settings = Settings(
        _env_file=None,
        price_insight_source="fixture",
        price_insight_target_tenant_id="development",
        model_skill_source_tenant_id=1,
        model_skill_target_tenant_id="development",
        price_insight_extra_tenant_ids="acme",
        deployment_profile="development",
    )
    tenants = _price_insight_publication_tenants(settings)
    assert "1" in tenants
    assert "development" in tenants
    assert "local-org" in tenants
    assert "acme" in tenants


def test_hands_bootstrap_publishes_price_insight_resources_for_zhwen_tenant() -> None:
    async def scenario() -> None:
        settings = Settings(
            _env_file=None,
            price_insight_source="fixture",
            price_insight_target_tenant_id="development",
            model_skill_source_tenant_id=1,
            model_skill_target_tenant_id="development",
        )
        skills = _skill_registry()
        resources = skills.resources or HandsResourceRegistry()
        catalog = CapabilityCatalog(InMemoryCapabilityCatalogStore())
        assert await _bootstrap_price_insight_capabilities(
            settings=settings,
            catalog=catalog,
            skill_registry=skills,
            resources=resources,
        )
        for tenant_id in ("1", "development", "local-org"):
            contents = resources.read(tenant_id, _METRIC_URI)
            assert contents[0].text
            assert skills.candidates(tenant_id, "procurement.price-insight.generate")

    asyncio.run(scenario())


def test_ensure_overwrites_tenant_scoped_stale_resource() -> None:
    settings = Settings(_env_file=None, price_insight_source="disabled")
    stale = price_insight_resources(tenant_id="development")[0]
    resources = HandsResourceRegistry()
    resources.register_resource(
        RegisteredResource(
            descriptor=stale.descriptor.model_copy(
                update={"content_digest": "sha256:stale"}
            ),
            contents=(
                HandsResourceContent(
                    uri=_METRIC_URI,
                    mime_type="text/markdown",
                    text="stale",
                ),
            ),
            tenant_ids=("development",),
        )
    )
    assert _ensure_price_insight_hands_resources(settings, resources) > 0
    contents = resources.read("local-org", _METRIC_URI)
    assert contents[0].text
    assert contents[0].text != "stale"


def test_hands_gateway_fills_through_resource_reader() -> None:
    from datetime import UTC, datetime, timedelta
    from unittest.mock import Mock

    from auraclaw.action.hands import HandsGateway
    from auraclaw.action.resource_gateway import ManagedResourceGateway
    from auraclaw.contracts.hands import HandsTrustedContext

    settings = Settings(_env_file=None, price_insight_source="disabled")
    resources = HandsResourceRegistry()
    reader = ManagedResourceGateway(
        resources,
        artifacts=ArtifactStore(
            InMemoryObjectStorage(),
            signing_key=b"auraclaw-development-platform-skill-key",
        ),
    )

    def fill(uri: str) -> None:
        del uri
        _ensure_price_insight_hands_resources(settings, resources)

    gateway = HandsGateway(
        registry=Mock(),
        gateway=Mock(),
        resources=resources,
        resource_reader=reader,
        on_missing_resource=fill,
    )
    trusted = HandsTrustedContext(
        tenant_id="local-org",
        root_session_id="root",
        session_id="session",
        run_id="run",
        runtime_id="runtime",
        lease_id="lease",
        fencing_token=1,
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )

    async def scenario() -> None:
        contents = await gateway.read_resource(trusted, _METRIC_URI)
        assert contents[0].text

    asyncio.run(scenario())

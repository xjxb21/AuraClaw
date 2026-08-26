from __future__ import annotations

import asyncio
from pathlib import Path

from auraclaw.action.capability_catalog import CapabilityCatalog
from auraclaw.config import Settings, get_settings
from auraclaw.infrastructure.persistence.postgres_capability_catalog import (
    PostgresCapabilityCatalogStore,
)


async def main() -> None:
    get_settings.cache_clear()
    settings = Settings(_env_file=str(Path(".env.debug.example").resolve()))
    print(
        "backend",
        settings.storage_backend,
        "source",
        settings.resolved_price_insight_source,
        "dialect",
        settings.resolved_db_dialect,
    )
    store = PostgresCapabilityCatalogStore(settings.resolved_database_url)
    catalog = CapabilityCatalog(store)
    try:
        for tenant in ("1", "development"):
            caps = await catalog.list_capabilities(tenant)
            interesting = sorted(
                c.canonical_name
                for c in caps
                if "price" in c.canonical_name.lower()
                or c.canonical_name.startswith("repo://")
            )
            print(f"tenant={tenant} total={len(caps)} interesting={len(interesting)}")
            for name in interesting[:40]:
                print(" ", name)
    finally:
        close = getattr(store, "close", None)
        if close is not None:
            await close()


if __name__ == "__main__":
    asyncio.run(main())

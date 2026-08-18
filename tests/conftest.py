"""Keep the test suite offline unless a test opts into Java Tool credentials."""

from __future__ import annotations

import pytest

from auraclaw.config import get_settings


@pytest.fixture(autouse=True)
def _pin_python_price_insight_backend_for_offline_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AURACLAW_PRICE_INSIGHT_TOOL_BACKEND", "python")
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()

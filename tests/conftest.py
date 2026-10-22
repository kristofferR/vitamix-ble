"""Shared fixtures."""

from __future__ import annotations

import pytest

from vitamix_ble import client as client_module


@pytest.fixture(autouse=True)
def fake_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    """Reconnecting to a fake device hands back the same fake."""
    monkeypatch.setattr(client_module, "BleakClient", lambda device, **_kwargs: device)

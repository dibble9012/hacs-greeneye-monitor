"""Common fixtures for testing greeneye_monitor.

These tests run the real greeneye library and talk to it over TCP the same way a
monitor does, using packets captured from real monitors.
"""
from __future__ import annotations

import asyncio
import socket
from collections.abc import AsyncGenerator
from collections.abc import Callable
from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest
import siobrultech_protocols.gem.api
from custom_components.greeneye_monitor.const import CONF_MONITORS
from custom_components.greeneye_monitor.const import CONF_SEND_PACKET_DELAY
from custom_components.greeneye_monitor.const import CONFIG_ENTRY_TITLE
from custom_components.greeneye_monitor.const import DOMAIN
from homeassistant.const import CONF_PORT
from homeassistant.core import HomeAssistant
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

pytest_plugins = "pytest_homeassistant_custom_component"

FIXTURES = Path(__file__).parent / "fixtures"

# Serial numbers of the monitors that sent the captured packets
GEM_SERIAL_NUMBER = 1100603
ECM_SERIAL_NUMBER = 500434


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Allow custom integrations to load."""


@pytest.fixture(autouse=True)
def fast_api_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Don't wait 15 seconds for our fake monitors to answer API calls."""
    monkeypatch.setattr(
        siobrultech_protocols.gem.api, "TIMEOUT", timedelta(milliseconds=100)
    )


@pytest.fixture
def port(socket_enabled: None) -> int:
    """Return a TCP port that nothing is listening on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture
async def setup_components(hass: HomeAssistant) -> None:
    """Set up the components that serve the flow and diagnostics HTTP APIs."""
    assert await async_setup_component(hass, "config", {})
    assert await async_setup_component(hass, "diagnostics", {})


def make_entry(
    port: int,
    monitors: list[dict[str, Any]] | None = None,
    options: dict[str, Any] | None = None,
) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title=CONFIG_ENTRY_TITLE,
        unique_id=DOMAIN,
        data={CONF_PORT: port, CONF_MONITORS: monitors or []},
        options=options or {CONF_SEND_PACKET_DELAY: False, CONF_MONITORS: []},
    )


class FakeMonitor:
    """Sends captured packets to the integration like a real monitor would."""

    def __init__(self, port: int) -> None:
        self._port = port
        self._writer: asyncio.StreamWriter | None = None

    async def send(self, fixture: str) -> None:
        if self._writer is None or self._writer.is_closing():
            _, self._writer = await asyncio.open_connection("127.0.0.1", self._port)
        self._writer.write((FIXTURES / fixture).read_bytes())
        await self._writer.drain()

    async def disconnect(self) -> None:
        if self._writer is not None:
            self._writer.close()
            try:
                await self._writer.wait_closed()
            except ConnectionError:
                pass
            self._writer = None


@pytest.fixture
async def fake_monitor(port: int) -> AsyncGenerator[FakeMonitor]:
    monitor = FakeMonitor(port)
    yield monitor
    await monitor.disconnect()


async def wait_for(condition: Callable[[], Any], timeout: float = 5) -> None:
    """Wait for something the library does on its own task to happen."""
    async with asyncio.timeout(timeout):
        while not condition():
            await asyncio.sleep(0.02)

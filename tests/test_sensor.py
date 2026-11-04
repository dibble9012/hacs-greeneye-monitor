"""Tests for the sensor platform."""
from __future__ import annotations

import pytest
from custom_components.greeneye_monitor.const import CONF_ENERGY_UPDATE_INTERVAL
from custom_components.greeneye_monitor.const import CONF_MONITORS
from homeassistant.core import HomeAssistant

from .conftest import FakeMonitor
from .conftest import GEM_SERIAL_NUMBER
from .conftest import make_entry
from .conftest import wait_for
from .test_flows import _setup_entry
from .test_flows import _unload
from .test_flows import _wait_for_entities
from .test_flows import LEGACY_GEM_DATA
from .test_flows import LEGACY_GEM_OPTIONS

ENERGY = "sensor.gem_1100603_channel_1_energy"


@pytest.mark.parametrize(
    ("interval", "writes_again"),
    [(0, True), (300, False)],
)
async def test_energy_update_interval(
    hass: HomeAssistant,
    port: int,
    fake_monitor: FakeMonitor,
    interval: int,
    writes_again: bool,
) -> None:
    """Energy sensors write at most once per configured interval."""
    entry = make_entry(
        port,
        [LEGACY_GEM_DATA],
        {**LEGACY_GEM_OPTIONS, CONF_ENERGY_UPDATE_INTERVAL: interval},
    )
    await _setup_entry(hass, entry)

    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, ENERGY)
    # HA updates last_reported in place when the state doesn't change
    first_reported = hass.states.get(ENERGY).last_reported

    # A second packet updates the channel (the library computes power from the
    # difference between packets)
    await fake_monitor.send("BIN48-NET-TIME.bin")
    channel = entry.runtime_data.monitors[GEM_SERIAL_NUMBER].channels[0]
    await wait_for(lambda: channel.watts is not None)
    await hass.async_block_till_done()

    assert (hass.states.get(ENERGY).last_reported > first_reported) is writes_again

    await _unload(hass, entry)


async def test_unconfigured_pulse_counter_is_skipped(
    hass: HomeAssistant,
    port: int,
    fake_monitor: FakeMonitor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing pulse counter config used to crash setup with an assertion."""
    data = {**LEGACY_GEM_DATA, "pulse_counters": LEGACY_GEM_DATA["pulse_counters"][:2]}
    options = {**LEGACY_GEM_OPTIONS, CONF_MONITORS: []}
    entry = make_entry(port, [data], options)
    await _setup_entry(hass, entry)

    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, ENERGY)
    assert hass.states.get("sensor.gem_1100603_pulse_counter_2") is not None
    assert hass.states.get("sensor.gem_1100603_pulse_counter_3") is None
    assert "Pulse counter 3 on monitor 1100603 is not configured" in caplog.text

    await _unload(hass, entry)

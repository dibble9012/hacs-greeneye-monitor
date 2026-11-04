"""Tests for setup, discovery, the options flow and diagnostics.

Flows are driven through the same HTTP API the frontend uses, so these tests
also catch schemas that can't be serialized for the UI.
"""
from __future__ import annotations

import socket
from http import HTTPStatus
from typing import Any

import pytest
from aiohttp.test_utils import TestClient
from custom_components.greeneye_monitor.const import CONF_ENERGY_UPDATE_INTERVAL
from custom_components.greeneye_monitor.const import CONF_MONITORS
from custom_components.greeneye_monitor.const import CONF_SEND_PACKET_DELAY
from custom_components.greeneye_monitor.const import DOMAIN
from homeassistant.config_entries import ConfigEntryState
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.const import STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM
from pytest_homeassistant_custom_component.common import MockConfigEntry

from .conftest import ECM_SERIAL_NUMBER
from .conftest import FakeMonitor
from .conftest import GEM_SERIAL_NUMBER
from .conftest import make_entry
from .conftest import wait_for

FLOW_URL = "/api/config/config_entries/flow"
OPTIONS_FLOW_URL = "/api/config/config_entries/options/flow"

GEM_MONITOR_FIELDS = {
    "net_metering",
    "pulse_counters",
    "temperature_sensors",
    "temperature_unit",
}

PULSE_COUNTER_DEFAULTS = {
    "counted_quantity": "pulses",
    "counted_quantity_per_pulse": 1,
    "time_unit": "s",
}

# What a GEM entry created by the previous version of this integration looks like
LEGACY_GEM_DATA = {
    "serial_number": GEM_SERIAL_NUMBER,
    "aux5_type": None,
    "net_metering": [],
    "temperature_unit": "°C",
    "pulse_counters": [
        {
            "number": n,
            "is_aux": False,
            "counted_quantity": "pulses",
            "counted_quantity_per_pulse": 1.0,
            "device_class": None,
        }
        for n in range(4)
    ],
}
LEGACY_GEM_OPTIONS = {
    "send_packet_delay": False,
    "monitors": [
        {
            "serial_number": GEM_SERIAL_NUMBER,
            "pulse_counters": [{"number": n, "time_unit": "s"} for n in range(4)],
        }
    ],
}


async def _get(client: TestClient, url: str) -> dict[str, Any]:
    resp = await client.get(url)
    assert resp.status == HTTPStatus.OK, await resp.text()
    return await resp.json()


async def _post(client: TestClient, url: str, data: dict[str, Any]) -> dict[str, Any]:
    resp = await client.post(url, json=data)
    assert resp.status == HTTPStatus.OK, await resp.text()
    return await resp.json()


async def _setup_entry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED


async def _unload(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def _wait_for_discovery(hass: HomeAssistant) -> dict[str, Any]:
    await wait_for(lambda: hass.config_entries.flow.async_progress_by_handler(DOMAIN))
    (flow,) = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    return flow


async def _wait_for_entities(hass: HomeAssistant, entity_id: str) -> None:
    """Wait for an entity to be added (again, after a reload)."""

    def available() -> bool:
        state = hass.states.get(entity_id)
        return state is not None and state.state != STATE_UNAVAILABLE

    await wait_for(available)


async def test_user_flow(
    hass: HomeAssistant, setup_components: None, hass_client, port: int
) -> None:
    client = await hass_client()
    result = await _post(client, FLOW_URL, {"handler": DOMAIN})
    assert result["type"] == "form"
    assert result["step_id"] == "user"

    # Something is already listening on the port
    with socket.socket() as blocker:
        blocker.bind(("", port))
        blocker.listen()
        result = await _post(client, f"{FLOW_URL}/{result['flow_id']}", {"port": port})
    assert result["type"] == "form"
    assert result["errors"] == {"port": "port_in_use"}

    result = await _post(client, f"{FLOW_URL}/{result['flow_id']}", {"port": port})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert entry.unique_id == DOMAIN
    assert entry.data == {"port": port, "monitors": []}
    assert entry.options[CONF_ENERGY_UPDATE_INTERVAL] == 300
    assert entry.state is ConfigEntryState.LOADED

    # Only one hub is allowed
    result = await _post(client, FLOW_URL, {"handler": DOMAIN})
    assert result["type"] == "abort"
    assert result["reason"] == "already_configured"

    await _unload(hass, entry)


async def test_discover_gem(
    hass: HomeAssistant,
    setup_components: None,
    hass_client,
    port: int,
    fake_monitor: FakeMonitor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A new GEM is offered for setup, and gets entities once configured."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = make_entry(port)
    await _setup_entry(hass, entry)
    client = await hass_client()

    await fake_monitor.send("BIN48-NET-TIME.bin")
    flow = await _wait_for_discovery(hass)
    assert flow["context"]["unique_id"] == f"{DOMAIN}_{GEM_SERIAL_NUMBER}"
    url = f"{FLOW_URL}/{flow['flow_id']}"

    result = await _get(client, url)
    assert result["step_id"] == "monitor"
    fields = {field["name"] for field in result["data_schema"]}
    assert fields == GEM_MONITOR_FIELDS

    # Temperature sensors need a unit
    result = await _post(client, url, {"temperature_sensors": ["0"]})
    assert result["step_id"] == "monitor"
    assert result["errors"] == {"temperature_unit": "temperature_unit_required"}

    result = await _post(
        client,
        url,
        {
            "net_metering": ["2", "0"],
            "temperature_unit": "°F",
            "temperature_sensors": ["1"],
            "pulse_counters": ["0", "1", "2", "3"],
        },
    )
    assert result["step_id"] == "pulse_counter"
    assert result["description_placeholders"] == {"pulse_counter_number": "1"}

    # Units are checked against the device class
    result = await _post(
        client, url, {**PULSE_COUNTER_DEFAULTS, "device_class": "energy"}
    )
    assert result["errors"] == {"device_class": "unit_not_valid_for_device_class"}
    assert result["description_placeholders"] == {"pulse_counter_number": "1"}

    result = await _post(
        client,
        url,
        {
            "counted_quantity": "gal",
            "counted_quantity_per_pulse": 0.5,
            "device_class": "water",
            "time_unit": "min",
        },
    )
    for number in (2, 3, 4):
        assert result["description_placeholders"] == {
            "pulse_counter_number": str(number)
        }
        result = await _post(client, url, PULSE_COUNTER_DEFAULTS)

    assert result["type"] == "abort"
    assert result["reason"] == "success"
    await hass.async_block_till_done()

    monitor_data = entry.data[CONF_MONITORS][0]
    assert monitor_data["serial_number"] == GEM_SERIAL_NUMBER
    assert monitor_data["net_metering"] == ["2", "0"]
    assert monitor_data["temperature_unit"] == "°F"
    assert monitor_data["pulse_counters"][0] == {
        "number": 0,
        "is_aux": False,
        "counted_quantity": "gal",
        "counted_quantity_per_pulse": 0.5,
        "device_class": "water",
    }
    assert entry.options[CONF_MONITORS][0]["pulse_counters"][0] == {
        "number": 0,
        "time_unit": "min",
    }

    # The entry reloaded, which dropped the connection. Once the monitor sends
    # another packet, its entities appear.
    assert entry.state is ConfigEntryState.LOADED
    await fake_monitor.disconnect()
    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, "sensor.gem_1100603_channel_1_energy")

    state = hass.states.get("sensor.gem_1100603_channel_1_energy")
    assert float(state.state) == pytest.approx(2973101 / 3600 / 1000)
    assert state.attributes["unit_of_measurement"] == "kWh"
    assert state.attributes["state_class"] == "total"

    state = hass.states.get("sensor.gem_1100603_pulse_counter_1")
    assert state.attributes["unit_of_measurement"] == "gal"
    assert state.attributes["device_class"] == "water"

    # Chatty sensors are disabled by default
    entity_registry = er.async_get(hass)
    # A temperature sensor the user chose is enabled, and the others don't exist
    assert hass.states.get("sensor.gem_1100603_temperature_2") is not None
    assert entity_registry.async_get("sensor.gem_1100603_temperature_1") is None
    power = entity_registry.async_get("sensor.gem_1100603_channel_1")
    assert power.unique_id == f"{GEM_SERIAL_NUMBER}-current-1"
    assert power.disabled_by is er.RegistryEntryDisabler.INTEGRATION

    # Per-input devices hang off the monitor device
    device_registry = dr.async_get(hass)
    (monitor_device,) = device_registry.async_get_devices(
        identifiers={(DOMAIN, str(GEM_SERIAL_NUMBER))}, config_entry_id=entry.entry_id
    )
    assert monitor_device.manufacturer == "Brultech"
    assert monitor_device.serial_number == str(GEM_SERIAL_NUMBER)
    (channel_device,) = device_registry.async_get_devices(
        identifiers={(DOMAIN, f"{GEM_SERIAL_NUMBER}-channel-1")},
        config_entry_id=entry.entry_id,
    )
    assert channel_device.via_device_id == monitor_device.id
    assert "via_device" not in caplog.text

    await _unload(hass, entry)


async def test_discover_energy_only_gem(
    hass: HomeAssistant,
    setup_components: None,
    hass_client,
    port: int,
    fake_monitor: FakeMonitor,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A GEM with only CTs takes one form and gets no other input entities."""
    entry = make_entry(port)
    await _setup_entry(hass, entry)
    client = await hass_client()

    await fake_monitor.send("BIN48-NET-TIME.bin")
    flow = await _wait_for_discovery(hass)
    url = f"{FLOW_URL}/{flow['flow_id']}"

    # Accepting the form as the frontend prefills it finishes setup
    result = await _get(client, url)
    suggested = {
        field["name"]: field["description"]["suggested_value"]
        for field in result["data_schema"]
        if "description" in field
    }
    assert suggested == {"temperature_unit": "°C", "net_metering": []}
    result = await _post(client, url, suggested)
    assert result["type"] == "abort"
    assert result["reason"] == "success"
    await hass.async_block_till_done()

    monitor_data = entry.data[CONF_MONITORS][0]
    assert monitor_data["pulse_counters"] == []
    assert monitor_data["temperature_sensors"] == []
    assert entry.options[CONF_MONITORS][0]["pulse_counters"] == []

    await fake_monitor.disconnect()
    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, "sensor.gem_1100603_channel_48_energy")

    entity_registry = er.async_get(hass)
    unique_ids = {
        registry_entry.unique_id
        for registry_entry in er.async_entries_for_config_entry(
            entity_registry, entry.entry_id
        )
    }
    assert f"{GEM_SERIAL_NUMBER}-energy-48" in unique_ids
    assert not [
        unique_id
        for unique_id in unique_ids
        if "-pulse-" in unique_id or "-count-" in unique_id or "-temp-" in unique_id
    ]
    device_identifiers = {
        identifier
        for device in dr.async_entries_for_config_entry(
            dr.async_get(hass), entry.entry_id
        )
        for _, identifier in device.identifiers
    }
    assert not [
        identifier
        for identifier in device_identifiers
        if "pulse counter" in identifier or "temperature" in identifier
    ]
    assert "greeneye_monitor" not in {
        record.name.split(".")[1]
        for record in caplog.records
        if record.levelname == "WARNING" and record.name.startswith("custom_components.")
    }

    # Options still work, and there's nothing to edit for pulse counters
    result = await _post(client, OPTIONS_FLOW_URL, {"handler": entry.entry_id})
    assert result["menu_options"] == ["global_options", "monitor_settings"]

    await _unload(hass, entry)


async def test_change_inputs_in_use(
    hass: HomeAssistant,
    setup_components: None,
    hass_client,
    port: int,
    fake_monitor: FakeMonitor,
) -> None:
    """Clearing inputs on an existing monitor removes them; selecting adds them."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    data = {
        **LEGACY_GEM_DATA,
        "pulse_counters": LEGACY_GEM_DATA["pulse_counters"][:3],
    }
    options = {
        **LEGACY_GEM_OPTIONS,
        "monitors": [
            {
                "serial_number": GEM_SERIAL_NUMBER,
                "pulse_counters": LEGACY_GEM_OPTIONS["monitors"][0]["pulse_counters"][:3],
            }
        ],
    }
    entry = make_entry(port, [data], options)
    await _setup_entry(hass, entry)
    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, "sensor.gem_1100603_pulse_counter_2")

    entity_registry = er.async_get(hass)
    device_registry = dr.async_get(hass)
    assert entity_registry.async_get("sensor.gem_1100603_temperature_3") is not None

    client = await hass_client()
    result = await _post(client, OPTIONS_FLOW_URL, {"handler": entry.entry_id})
    url = f"{OPTIONS_FLOW_URL}/{result['flow_id']}"
    result = await _post(client, url, {"next_step_id": "monitor_settings"})

    # What's in use now is preselected: an old entry has all temperature sensors
    suggested = {
        field["name"]: field["description"]["suggested_value"]
        for field in result["data_schema"]
        if "description" in field
    }
    assert suggested["pulse_counters"] == ["0", "1", "2"]
    assert suggested["temperature_sensors"] == [str(n) for n in range(8)]

    # Keep pulse counter 1, drop 2 and 3, add 4, and keep only temperature sensor 2
    result = await _post(
        client,
        url,
        {
            "temperature_unit": "°C",
            "pulse_counters": ["0", "3"],
            "temperature_sensors": ["1"],
        },
    )
    assert result["step_id"] == "new_pulse_counter"
    assert result["description_placeholders"] == {"pulse_counter_number": "4"}

    # Nothing is saved or removed until the flow finishes
    assert len(entry.data[CONF_MONITORS][0]["pulse_counters"]) == 3
    assert entity_registry.async_get("sensor.gem_1100603_pulse_counter_2") is not None

    result = await _post(
        client,
        url,
        {
            "counted_quantity": "gal",
            "counted_quantity_per_pulse": 0.5,
            "device_class": "water",
            "time_unit": "min",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()

    monitor_data = entry.data[CONF_MONITORS][0]
    assert [pc["number"] for pc in monitor_data["pulse_counters"]] == [0, 3]
    assert monitor_data["temperature_sensors"] == ["1"]
    assert [
        pc["number"] for pc in entry.options[CONF_MONITORS][0]["pulse_counters"]
    ] == [0, 3]

    for removed in (
        "sensor.gem_1100603_pulse_counter_2",
        "sensor.gem_1100603_pulse_counter_2_rate",
        "sensor.gem_1100603_pulse_counter_3",
        "sensor.gem_1100603_temperature_1",
        "sensor.gem_1100603_temperature_3",
    ):
        assert entity_registry.async_get(removed) is None, removed
    for identifier in (f"{GEM_SERIAL_NUMBER}-pulse counter-2", f"{GEM_SERIAL_NUMBER}-temperature-1"):
        assert not device_registry.async_get_devices(
            identifiers={(DOMAIN, identifier)}, config_entry_id=entry.entry_id
        )
    # Inputs that stayed are untouched
    assert entity_registry.async_get("sensor.gem_1100603_pulse_counter_1") is not None
    assert entity_registry.async_get("sensor.gem_1100603_temperature_2") is not None

    await fake_monitor.disconnect()
    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, "sensor.gem_1100603_pulse_counter_4")
    await _wait_for_entities(hass, "sensor.gem_1100603_pulse_counter_1")
    assert hass.states.get("sensor.gem_1100603_pulse_counter_4").attributes[
        "device_class"
    ] == "water"
    assert entity_registry.async_get("sensor.gem_1100603_pulse_counter_2") is None

    await _unload(hass, entry)


async def test_custom_entity_ids_survive_removing_the_entry(
    hass: HomeAssistant,
    setup_components: None,
    hass_client,
    port: int,
    fake_monitor: FakeMonitor,
) -> None:
    """Deleting the entry and setting up again restores renamed entities."""
    entry = make_entry(port, [LEGACY_GEM_DATA], LEGACY_GEM_OPTIONS)
    await _setup_entry(hass, entry)
    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, "sensor.gem_1100603_channel_5_energy")

    entity_registry = er.async_get(hass)
    entity_registry.async_update_entity(
        "sensor.gem_1100603_channel_5_energy",
        new_entity_id="sensor.garage_cb_05_energy",
        name="CB 05",
    )
    await hass.async_block_till_done()
    await fake_monitor.disconnect()

    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert entity_registry.async_get("sensor.garage_cb_05_energy") is None

    # Set up again from scratch, energy sensors only
    client = await hass_client()
    result = await _post(client, FLOW_URL, {"handler": DOMAIN})
    result = await _post(client, f"{FLOW_URL}/{result['flow_id']}", {"port": port})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    new_entry = hass.config_entries.async_entries(DOMAIN)[0]
    assert new_entry.entry_id != entry.entry_id

    await fake_monitor.send("BIN48-NET-TIME.bin")
    flow = await _wait_for_discovery(hass)
    result = await _post(client, f"{FLOW_URL}/{flow['flow_id']}", {})
    assert result["reason"] == "success"
    await hass.async_block_till_done()
    await fake_monitor.disconnect()
    await fake_monitor.send("BIN48-NET-TIME.bin")

    await _wait_for_entities(hass, "sensor.garage_cb_05_energy")
    restored = entity_registry.async_get("sensor.garage_cb_05_energy")
    assert restored.unique_id == f"{GEM_SERIAL_NUMBER}-energy-5"
    assert restored.name == "CB 05"
    assert restored.config_entry_id == new_entry.entry_id
    # No duplicate was made under the default ID
    assert entity_registry.async_get("sensor.gem_1100603_channel_5_energy") is None
    # Inputs not selected this time don't come back
    assert entity_registry.async_get("sensor.gem_1100603_pulse_counter_1") is None

    await _unload(hass, new_entry)


async def test_discover_ecm_1240(
    hass: HomeAssistant,
    setup_components: None,
    hass_client,
    port: int,
    fake_monitor: FakeMonitor,
) -> None:
    """ECM-1240s ask about Aux 5 instead of temperature."""
    entry = make_entry(port)
    await _setup_entry(hass, entry)
    client = await hass_client()

    await fake_monitor.send("ECM-1240.bin")
    flow = await _wait_for_discovery(hass)
    url = f"{FLOW_URL}/{flow['flow_id']}"

    result = await _get(client, url)
    fields = {field["name"] for field in result["data_schema"]}
    assert fields == {"net_metering", "aux5_type"}

    result = await _post(
        client, url, {"net_metering": ["0"], "aux5_type": "pulse_counter"}
    )
    assert result["description_placeholders"] == {"pulse_counter_number": "Aux 5"}
    result = await _post(client, url, PULSE_COUNTER_DEFAULTS)
    assert result["reason"] == "success"
    await hass.async_block_till_done()

    assert entry.data[CONF_MONITORS][0]["pulse_counters"] == [
        {
            "number": 4,
            "is_aux": True,
            "counted_quantity": "pulses",
            "counted_quantity_per_pulse": 1.0,
            "device_class": None,
        }
    ]

    await fake_monitor.disconnect()
    await fake_monitor.send("ECM-1240.bin")
    await _wait_for_entities(hass, "sensor.ecm_500434_channel_1_energy")
    # Channel 1 is net metered, so the net energy is reported as positive
    assert float(hass.states.get("sensor.ecm_500434_channel_1_energy").state) > 0
    assert hass.states.get("sensor.ecm_500434_aux_5") is not None
    assert hass.states.get("sensor.ecm_500434_aux_1_energy") is not None

    await _unload(hass, entry)


async def test_ignoring_a_monitor(
    hass: HomeAssistant,
    setup_components: None,
    port: int,
    fake_monitor: FakeMonitor,
) -> None:
    """Ignoring a monitor must not create a second entry with the hub's unique ID."""
    entry = make_entry(port)
    await _setup_entry(hass, entry)

    await fake_monitor.send("BIN48-NET-TIME.bin")
    flow = await _wait_for_discovery(hass)
    await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IGNORE},
        data={"unique_id": flow["context"]["unique_id"], "title": "GEM"},
    )
    await hass.async_block_till_done()

    unique_ids = [
        e.unique_id for e in hass.config_entries.async_entries(DOMAIN, include_ignore=True)
    ]
    assert sorted(unique_ids) == sorted([DOMAIN, f"{DOMAIN}_{GEM_SERIAL_NUMBER}"])
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    # After a reload the monitor is seen again, but stays ignored
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await fake_monitor.disconnect()
    await fake_monitor.send("BIN48-NET-TIME.bin")
    await wait_for(lambda: entry.runtime_data.monitors)
    await hass.async_block_till_done()
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    await _unload(hass, entry)


async def test_entry_from_previous_version(
    hass: HomeAssistant,
    setup_components: None,
    hass_client,
    port: int,
    fake_monitor: FakeMonitor,
) -> None:
    """Entries and entities created by the old version keep working and are editable."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    entry = make_entry(port, [LEGACY_GEM_DATA], LEGACY_GEM_OPTIONS)
    # Leftovers from the old version
    legacy_ignored = MockConfigEntry(domain=DOMAIN, source=SOURCE_IGNORE, unique_id=DOMAIN)
    legacy_ignored.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        "sensor.gem_1100603_channel_1_energy_excluded",
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="entity_excluded",
    )
    # An entity the old version registered, which must be reused
    entity_registry = er.async_get(hass)
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{GEM_SERIAL_NUMBER}-energy-1",
        suggested_object_id="my_custom_energy_entity",
    )

    await _setup_entry(hass, entry)
    assert hass.config_entries.async_get_entry(legacy_ignored.entry_id) is None
    assert not ir.async_get(hass).issues

    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, "sensor.my_custom_energy_entity")
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)

    client = await hass_client()

    # Options menu (this used to fail with "doesn't support step options_menu"
    # and, on HA 2025.12+, "property 'config_entry' ... has no setter")
    result = await _post(client, OPTIONS_FLOW_URL, {"handler": entry.entry_id})
    assert result["type"] == "menu"
    assert result["step_id"] == "init"
    assert result["menu_options"] == [
        "global_options",
        "monitor_settings",
        "pulse_counter_settings",
    ]
    url = f"{OPTIONS_FLOW_URL}/{result['flow_id']}"

    # Only one monitor, so it's chosen automatically
    result = await _post(client, url, {"next_step_id": "monitor_settings"})
    assert result["step_id"] == "monitor"
    fields = {field["name"] for field in result["data_schema"]}
    assert fields == GEM_MONITOR_FIELDS
    result = await _post(
        client,
        url,
        {
            "net_metering": ["5", "1"],
            "temperature_unit": "°F",
            "pulse_counters": ["0", "1", "2", "3"],
            "temperature_sensors": [str(n) for n in range(8)],
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.data[CONF_MONITORS][0]["net_metering"] == ["1", "5"]
    assert entry.data[CONF_MONITORS][0]["temperature_unit"] == "°F"
    assert entry.state is ConfigEntryState.LOADED

    # Pulse counter settings
    result = await _post(client, OPTIONS_FLOW_URL, {"handler": entry.entry_id})
    url = f"{OPTIONS_FLOW_URL}/{result['flow_id']}"
    result = await _post(client, url, {"next_step_id": "pulse_counter_settings"})
    assert result["step_id"] == "choose_pulse_counter"
    result = await _post(client, url, {"number": "2"})
    assert result["step_id"] == "pulse_counter"
    assert result["description_placeholders"]["pulse_counter_number"] == "3"
    result = await _post(
        client,
        url,
        {
            "counted_quantity": "ft³",
            "counted_quantity_per_pulse": 0.1,
            "device_class": "gas",
            "time_unit": "h",
        },
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.data[CONF_MONITORS][0]["pulse_counters"][2] == {
        "number": 2,
        "is_aux": False,
        "counted_quantity": "ft³",
        "counted_quantity_per_pulse": 0.1,
        "device_class": "gas",
    }
    assert entry.options[CONF_MONITORS][0]["pulse_counters"][2] == {
        "number": 2,
        "time_unit": "h",
    }

    # Global options
    result = await _post(client, OPTIONS_FLOW_URL, {"handler": entry.entry_id})
    url = f"{OPTIONS_FLOW_URL}/{result['flow_id']}"
    result = await _post(client, url, {"next_step_id": "global_options"})
    assert result["step_id"] == "global_options"
    result = await _post(
        client, url, {CONF_ENERGY_UPDATE_INTERVAL: 60, CONF_SEND_PACKET_DELAY: False}
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.options[CONF_ENERGY_UPDATE_INTERVAL] == 60
    # Per-monitor options are preserved when global options change
    assert entry.options[CONF_MONITORS][0]["pulse_counters"][2] == {
        "number": 2,
        "time_unit": "h",
    }

    # The new settings are used once the monitor reports in again
    await fake_monitor.disconnect()
    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, "sensor.gem_1100603_pulse_counter_3")
    state = hass.states.get("sensor.gem_1100603_pulse_counter_3")
    assert state.attributes["unit_of_measurement"] == "ft³"
    assert state.attributes["device_class"] == "gas"

    await _unload(hass, entry)


async def test_options_with_no_monitors(
    hass: HomeAssistant, setup_components: None, hass_client, port: int
) -> None:
    entry = make_entry(port)
    await _setup_entry(hass, entry)
    client = await hass_client()
    result = await _post(client, OPTIONS_FLOW_URL, {"handler": entry.entry_id})
    assert result["menu_options"] == ["global_options"]
    await _unload(hass, entry)


async def test_diagnostics(
    hass: HomeAssistant,
    setup_components: None,
    hass_client,
    port: int,
    fake_monitor: FakeMonitor,
) -> None:
    entry = make_entry(port, [LEGACY_GEM_DATA], LEGACY_GEM_OPTIONS)
    await _setup_entry(hass, entry)
    await fake_monitor.send("BIN48-NET-TIME.bin")
    await _wait_for_entities(hass, "sensor.gem_1100603_channel_1_energy")

    client = await hass_client()
    result = await _get(client, f"/api/diagnostics/config_entry/{entry.entry_id}")
    data = result["data"]
    monitor = data["monitors"][str(GEM_SERIAL_NUMBER)]
    assert monitor["type"] == "GEM"
    assert monitor["packet_format"] == "BIN48_NET_TIME"
    assert len(monitor["channels"]) == 48
    assert monitor["channels"][0]["absolute_watt_seconds"] == 2973101
    assert "sensor.gem_1100603_channel_1_energy" in data["entities"]
    assert data["devices"]
    assert data["config_entry"]["data"]["monitors"][0]["serial_number"] == (
        GEM_SERIAL_NUMBER
    )

    # Diagnostics still work when the entry isn't loaded
    await _unload(hass, entry)
    result = await _get(client, f"/api/diagnostics/config_entry/{entry.entry_id}")
    assert result["data"]["monitors"] is None


async def test_port_in_use_retries_setup(
    hass: HomeAssistant, port: int
) -> None:
    entry = make_entry(port)
    with socket.socket() as blocker:
        blocker.bind(("", port))
        blocker.listen()
        entry.add_to_hass(hass)
        await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_RETRY


async def test_ecm_options_shows_aux5(
    hass: HomeAssistant,
    setup_components: None,
    hass_client,
    port: int,
) -> None:
    """Monitor settings work for a monitor that hasn't reported since startup."""
    ecm = {
        "serial_number": ECM_SERIAL_NUMBER,
        "aux5_type": "ct",
        "net_metering": [],
        "pulse_counters": [],
    }
    entry = make_entry(port, [ecm], {"monitors": []})
    await _setup_entry(hass, entry)
    client = await hass_client()

    result = await _post(client, OPTIONS_FLOW_URL, {"handler": entry.entry_id})
    assert result["menu_options"] == ["global_options", "monitor_settings"]
    url = f"{OPTIONS_FLOW_URL}/{result['flow_id']}"
    result = await _post(client, url, {"next_step_id": "monitor_settings"})
    fields = {field["name"] for field in result["data_schema"]}
    assert fields == {"net_metering", "aux5_type"}

    # Switching Aux 5 to a pulse counter gives it a default configuration
    result = await _post(client, url, {"aux5_type": "pulse_counter"})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.data[CONF_MONITORS][0]["pulse_counters"][0]["number"] == 4
    assert entry.options[CONF_MONITORS] == [
        {"serial_number": ECM_SERIAL_NUMBER, "pulse_counters": [{"number": 4, "time_unit": "s"}]}
    ]

    await _unload(hass, entry)

"""Diagnostics support for GreenEye Monitor.

Only public Home Assistant APIs are used here, so that a change to Home
Assistant internals can't break downloading diagnostics.
"""
from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from enum import Enum
from typing import Any

from greeneye.monitor import Aux
from greeneye.monitor import Channel
from greeneye.monitor import GemSettings
from greeneye.monitor import Monitor
from greeneye.monitor import PulseCounter
from greeneye.monitor import TemperatureSensor
from greeneye.monitor import VoltageSensor
from homeassistant.config_entries import ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .const import GreeneyeConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: GreeneyeConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    monitors: dict[str, Any] | None = None
    if entry.state is ConfigEntryState.LOADED:
        monitors = {
            str(serial_number): monitor_as_dict(monitor)
            for serial_number, monitor in entry.runtime_data.monitors.items()
        }

    return {
        "current_time": dt_util.now().isoformat(),
        "config_entry": entry.as_dict(),
        "monitors": monitors,
        "entities": entities_as_dict(hass, entry),
        "devices": devices_as_list(hass, entry),
        "issues": issues_as_list(hass),
    }


def _jsonable(value: Any) -> Any:
    """Convert library types that the diagnostics JSON encoder doesn't know."""
    if isinstance(value, Enum):
        return value.name
    if isinstance(value, timedelta):
        return value.total_seconds()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _listener_count(source: Any) -> int | None:
    listeners = getattr(source, "_listeners", None)
    return len(listeners) if listeners is not None else None


def entities_as_dict(
    hass: HomeAssistant, entry: GreeneyeConfigEntry
) -> dict[str, Any]:
    registry = er.async_get(hass)
    result: dict[str, Any] = {}
    for registry_entry in er.async_entries_for_config_entry(registry, entry.entry_id):
        state = hass.states.get(registry_entry.entity_id)
        result[registry_entry.entity_id] = {
            "unique_id": registry_entry.unique_id,
            "name": registry_entry.name,
            "original_name": registry_entry.original_name,
            "disabled_by": registry_entry.disabled_by,
            "device_id": registry_entry.device_id,
            "unit_of_measurement": registry_entry.unit_of_measurement,
            "state": state.as_dict() if state else None,
        }
    return result


def devices_as_list(
    hass: HomeAssistant, entry: GreeneyeConfigEntry
) -> list[dict[str, Any]]:
    registry = dr.async_get(hass)
    return [
        {
            "id": device.id,
            "name": device.name,
            "name_by_user": device.name_by_user,
            "identifiers": sorted(list(identifier) for identifier in device.identifiers),
            "via_device_id": device.via_device_id,
            "disabled_by": device.disabled_by,
        }
        for device in dr.async_entries_for_config_entry(registry, entry.entry_id)
    ]


def issues_as_list(hass: HomeAssistant) -> list[dict[str, Any]]:
    registry = ir.async_get(hass)
    return [
        issue.to_json()
        for (domain, _), issue in registry.issues.items()
        if domain == DOMAIN and issue.active
    ]


def monitor_as_dict(monitor: Monitor) -> dict[str, Any]:
    return {
        "serial_number": monitor.serial_number,
        "type": _jsonable(monitor.type),
        "packet_format": _jsonable(monitor.packet_format),
        "packet_send_interval": _jsonable(monitor.packet_send_interval),
        "has_control": monitor.control is not None,
        "settings": settings_as_dict(monitor.settings),
        "aux": [aux_as_dict(aux) for aux in monitor.aux],
        "channels": [channel_as_dict(channel) for channel in monitor.channels],
        "pulse_counters": [
            pulse_counter_as_dict(pulse_counter)
            for pulse_counter in monitor.pulse_counters
        ],
        "temperature_sensors": [
            temperature_sensor_as_dict(temperature_sensor)
            for temperature_sensor in monitor.temperature_sensors
        ],
        "voltage_sensor": voltage_sensor_as_dict(monitor.voltage_sensor),
        "listeners": _listener_count(monitor),
    }


def aux_as_dict(aux: Channel | Aux) -> dict[str, Any]:
    if isinstance(aux, Channel):
        return channel_as_dict(aux)
    return {
        "channel": channel_as_dict(aux.channel),
        "pulse_counter": pulse_counter_as_dict(aux.pulse_counter),
    }


def channel_as_dict(channel: Channel) -> dict[str, Any]:
    return {
        "number": channel.number,
        "ct_type": channel.ct_type,
        "ct_range": channel.ct_range,
        "is_aux": channel.is_aux,
        "net_metering": channel.net_metering,
        "timestamp": _jsonable(channel.timestamp),
        "seconds": channel.seconds,
        "amps": channel.amps,
        "absolute_watt_seconds": channel.absolute_watt_seconds,
        "polarized_watt_seconds": channel.polarized_watt_seconds,
        "watts": channel.watts,
        "listeners": _listener_count(channel),
    }


def pulse_counter_as_dict(pulse_counter: PulseCounter) -> dict[str, Any]:
    return {
        "number": pulse_counter.number,
        "pulses": pulse_counter.pulses,
        "pulses_per_second": pulse_counter.pulses_per_second,
        "seconds": pulse_counter.seconds,
        "is_aux": pulse_counter.is_aux,
        "listeners": _listener_count(pulse_counter),
    }


def settings_as_dict(settings: GemSettings | None) -> dict[str, Any] | None:
    if settings is None:
        return None
    return {
        "num_channels": settings.num_channels,
        "channel_net_metering": settings.channel_net_metering,
        "ct_ranges": settings.ct_ranges,
        "ct_types": settings.ct_types,
        "packet_format": _jsonable(settings.packet_format),
        "packet_send_interval": _jsonable(settings.packet_send_interval),
        "temperature_unit": _jsonable(settings.temperature_unit),
    }


def temperature_sensor_as_dict(temperature_sensor: TemperatureSensor) -> dict[str, Any]:
    return {
        "number": temperature_sensor.number,
        "temperature": temperature_sensor.temperature,
        "unit": _jsonable(temperature_sensor.unit),
        "listeners": _listener_count(temperature_sensor),
    }


def voltage_sensor_as_dict(voltage_sensor: VoltageSensor) -> dict[str, Any]:
    return {
        "voltage": voltage_sensor.voltage,
        "listeners": _listener_count(voltage_sensor),
    }

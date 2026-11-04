"""Constants for the greeneye_monitor component."""
from __future__ import annotations

from datetime import timedelta

from greeneye import Monitors
from greeneye.monitor import Monitor
from greeneye.monitor import MonitorType
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo

type GreeneyeConfigEntry = ConfigEntry[Monitors]

AUX5_TYPE_CT = "ct"
AUX5_TYPE_PULSE_COUNTER = "pulse_counter"

CONF_AUX5_TYPE = "aux5_type"
CONF_CHANNELS = "channels"
CONF_COUNTED_QUANTITY = "counted_quantity"
CONF_COUNTED_QUANTITY_PER_PULSE = "counted_quantity_per_pulse"
CONF_DEVICE_CLASS = "device_class"
CONF_ENERGY_UPDATE_INTERVAL = "energy_update_interval"
CONF_IS_AUX = "is_aux"
CONF_MONITORS = "monitors"
CONF_NET_METERING = "net_metering"
CONF_NUMBER = "number"
CONF_PULSE_COUNTERS = "pulse_counters"
CONF_SEND_PACKET_DELAY = "send_packet_delay"
CONF_SERIAL_NUMBER = "serial_number"
CONF_TEMPERATURE_SENSORS = "temperature_sensors"
CONF_TIME_UNIT = "time_unit"
CONF_VOLTAGE_SENSORS = "voltage"

CONFIG_ENTRY_TITLE = "GreenEye Monitor (GEM)"

# How often the cumulative (energy and pulse count) sensors write their state.
# The monitor sends a new total in every packet, but recording every packet for
# every channel bloats the recorder database. Five minutes matches the
# granularity of the recorder's short-term statistics, so the Energy dashboard
# stays accurate.
DEFAULT_ENERGY_UPDATE_INTERVAL = timedelta(minutes=5)
MAX_ENERGY_UPDATE_INTERVAL = timedelta(hours=1)

DEVICE_TYPE_AUX = "aux"
DEVICE_TYPE_CURRENT_TRANSFORMER = "channel"
DEVICE_TYPE_PULSE_COUNTER = "pulse counter"
DEVICE_TYPE_TEMPERATURE_SENSOR = "temperature"
DEVICE_TYPE_VOLTAGE_SENSOR = "voltage"
DOMAIN = "greeneye_monitor"

TEMPERATURE_UNIT_CELSIUS = "C"


def signal_new_monitor(entry_id: str) -> str:
    """Dispatcher signal sent when a configured monitor first reports in."""
    return f"{DOMAIN}_{entry_id}_new_monitor"


def get_monitor_type_short_name(monitor: Monitor) -> str:
    if monitor.type == MonitorType.GEM:
        return "GEM"
    return "ECM"


def get_monitor_type_long_name(monitor: Monitor) -> str:
    if monitor.type == MonitorType.ECM_1240:
        return "ECM-1240"
    if monitor.type == MonitorType.ECM_1220:
        return "ECM-1220/1240"
    return "GreenEye Monitor"


def make_monitor_device_info(monitor: Monitor) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, f"{monitor.serial_number}")},
        manufacturer="Brultech",
        name=f"{get_monitor_type_short_name(monitor)} {monitor.serial_number}",
        model=get_monitor_type_long_name(monitor),
        serial_number=f"{monitor.serial_number}",
    )


def make_device_info(
    monitor: Monitor, monitor_device_id: str, device_type: str, number: int
) -> DeviceInfo:
    """Device info for one input (channel, pulse counter, ...) of a monitor.

    monitor_device_id is the device registry ID of the monitor itself, which is
    registered before any of its entities are added.
    """
    monitor_type_short_name = get_monitor_type_short_name(monitor)
    return DeviceInfo(
        identifiers={(DOMAIN, f"{monitor.serial_number}-{device_type}-{number + 1}")},
        name=f"{monitor_type_short_name} {monitor.serial_number} {device_type} {number + 1}",
        via_device_id=monitor_device_id,
    )

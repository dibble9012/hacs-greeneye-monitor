"""Support for monitoring a GreenEye Monitor energy monitor."""
from __future__ import annotations

import logging

import greeneye
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from homeassistant.config_entries import SOURCE_IGNORE
from homeassistant.config_entries import SOURCE_IMPORT
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.const import CONF_NAME
from homeassistant.const import CONF_PORT
from homeassistant.const import CONF_SENSORS
from homeassistant.const import CONF_TEMPERATURE_UNIT
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.const import Platform
from homeassistant.const import UnitOfTime
from homeassistant.core import Event
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import discovery_flow
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.dispatcher import async_dispatcher_send
from homeassistant.helpers.issue_registry import IssueSeverity
from homeassistant.helpers.typing import ConfigType

from . import config_validation as gem_cv
from .const import CONF_CHANNELS
from .const import CONF_COUNTED_QUANTITY
from .const import CONF_COUNTED_QUANTITY_PER_PULSE
from .const import CONF_DEVICE_CLASS
from .const import CONF_MONITORS
from .const import CONF_NET_METERING
from .const import CONF_NUMBER
from .const import CONF_PULSE_COUNTERS
from .const import CONF_SEND_PACKET_DELAY
from .const import CONF_SERIAL_NUMBER
from .const import CONF_TEMPERATURE_SENSORS
from .const import CONF_TIME_UNIT
from .const import CONF_VOLTAGE_SENSORS
from .const import DOMAIN
from .const import GreeneyeConfigEntry
from .const import make_monitor_device_info
from .const import signal_new_monitor
from .const import TEMPERATURE_UNIT_CELSIUS

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.NUMBER, Platform.SENSOR]

TEMPERATURE_SENSOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NUMBER): gem_cv.temperatureSensorNumber,
        vol.Required(CONF_NAME): cv.string,
    }
)

TEMPERATURE_SENSORS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TEMPERATURE_UNIT): cv.temperature_unit,
        vol.Required(CONF_SENSORS): vol.All(
            cv.ensure_list, [TEMPERATURE_SENSOR_SCHEMA]
        ),
    }
)

VOLTAGE_SENSOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NUMBER): gem_cv.channelNumber,
        vol.Required(CONF_NAME): cv.string,
    }
)

VOLTAGE_SENSORS_SCHEMA = vol.All(cv.ensure_list, [VOLTAGE_SENSOR_SCHEMA])

PULSE_COUNTER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NUMBER): gem_cv.pulseCounterNumber,
        vol.Required(CONF_NAME): cv.string,
        vol.Required(CONF_COUNTED_QUANTITY): cv.string,
        vol.Optional(CONF_DEVICE_CLASS, default=None): gem_cv.deviceClass,
        vol.Optional(CONF_COUNTED_QUANTITY_PER_PULSE, default=1.0): vol.Coerce(float),
        vol.Optional(CONF_TIME_UNIT, default=UnitOfTime.SECONDS.value): vol.Any(
            UnitOfTime.SECONDS.value, UnitOfTime.MINUTES.value, UnitOfTime.HOURS.value
        ),
    }
)

PULSE_COUNTERS_SCHEMA = vol.All(cv.ensure_list, [PULSE_COUNTER_SCHEMA])

CHANNEL_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NUMBER): gem_cv.channelNumber,
        vol.Required(CONF_NAME): cv.string,
        vol.Optional(CONF_NET_METERING, default=False): cv.boolean,
    }
)

CHANNELS_SCHEMA = vol.All(cv.ensure_list, [CHANNEL_SCHEMA])

MONITOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SERIAL_NUMBER): gem_cv.serialNumber,
        vol.Optional(CONF_CHANNELS, default=[]): CHANNELS_SCHEMA,
        vol.Optional(
            CONF_TEMPERATURE_SENSORS,
            default={CONF_TEMPERATURE_UNIT: TEMPERATURE_UNIT_CELSIUS, CONF_SENSORS: []},
        ): TEMPERATURE_SENSORS_SCHEMA,
        vol.Optional(CONF_PULSE_COUNTERS, default=[]): PULSE_COUNTERS_SCHEMA,
        vol.Optional(CONF_VOLTAGE_SENSORS, default=[]): VOLTAGE_SENSORS_SCHEMA,
    }
)

MONITORS_SCHEMA = vol.All(cv.ensure_list, [MONITOR_SCHEMA])

COMPONENT_SCHEMA = vol.Schema(
    {vol.Required(CONF_PORT): cv.port, vol.Required(CONF_MONITORS): MONITORS_SCHEMA}
)

CONFIG_SCHEMA = vol.Schema({DOMAIN: COMPONENT_SCHEMA}, extra=vol.ALLOW_EXTRA)


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Copy the YAML configuration to the config entry."""
    if server_config := config.get(DOMAIN):
        ir.async_create_issue(
            hass,
            DOMAIN,
            "remove_yaml",
            is_fixable=False,
            severity=IssueSeverity.WARNING,
            translation_key="remove_yaml",
        )

        hass.async_create_task(
            hass.config_entries.flow.async_init(
                DOMAIN, context={"source": SOURCE_IMPORT}, data=server_config
            )
        )

    return True


async def async_setup_entry(hass: HomeAssistant, entry: GreeneyeConfigEntry) -> bool:
    """Set up the GreenEye Monitor component from a config entry."""
    monitors = greeneye.Monitors(
        send_packet_delay=entry.options.get(CONF_SEND_PACKET_DELAY, False)
    )
    port = entry.data[CONF_PORT]
    try:
        await monitors.start_server(port)
    except OSError as err:
        await _async_discard_monitors(monitors)
        raise ConfigEntryNotReady(
            f"Unable to listen for monitor packets on port {port}: {err}"
        ) from err

    entry.runtime_data = monitors
    _async_remove_legacy_leftovers(hass)

    async def close_monitors(event: Event) -> None:
        """Stop listening so that shutdown is not held up by open connections."""
        await monitors.close()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, close_monitors)
    )

    # The sensor and number platforms subscribe to the dispatcher signal, so
    # set them up before we start telling them about monitors.
    try:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except BaseException:
        # Free the port, or every retry would fail to listen on it
        await monitors.close()
        raise

    async def on_new_monitor(monitor: greeneye.monitor.Monitor) -> None:
        _async_handle_monitor(hass, entry, monitor)

    monitors.add_listener(on_new_monitor)
    entry.async_on_unload(lambda: monitors.remove_listener(on_new_monitor))
    for monitor in list(monitors.monitors.values()):
        _async_handle_monitor(hass, entry, monitor)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    return True


def _async_remove_legacy_leftovers(hass: HomeAssistant) -> None:
    """Clean up things that older versions of this integration left behind."""
    # Older versions raised a repair issue for each energy sensor excluded from
    # the recorder, by peeking at logbook internals. That check is gone.
    issue_registry = ir.async_get(hass)
    for domain, issue_id in list(issue_registry.issues):
        if domain == DOMAIN and issue_id.endswith("_excluded"):
            ir.async_delete_issue(hass, DOMAIN, issue_id)

    # Older versions gave every monitor discovery the hub's unique ID, so
    # ignoring a discovered monitor created a second entry with that ID and HA
    # raised a "multiple config entries with same unique ID" repair.
    for other in hass.config_entries.async_entries(DOMAIN, include_ignore=True):
        if other.source == SOURCE_IGNORE and other.unique_id == DOMAIN:
            hass.async_create_task(hass.config_entries.async_remove(other.entry_id))


def _async_handle_monitor(
    hass: HomeAssistant, entry: GreeneyeConfigEntry, monitor: greeneye.monitor.Monitor
) -> None:
    """Create entities for a configured monitor, or offer to configure it."""
    serial_number = monitor.serial_number
    if any(
        config[CONF_SERIAL_NUMBER] == serial_number
        for config in entry.data[CONF_MONITORS]
    ):
        device = dr.async_get(hass).async_get_or_create(
            config_entry_id=entry.entry_id, **make_monitor_device_info(monitor)
        )
        async_dispatcher_send(
            hass, signal_new_monitor(entry.entry_id), monitor, device.id
        )
        _LOGGER.info("Monitor %d is reporting", serial_number)
        return

    _LOGGER.info("Discovered unconfigured monitor %d", serial_number)
    discovery_flow.async_create_flow(
        hass,
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_SERIAL_NUMBER: serial_number},
    )


async def _async_reload_entry(hass: HomeAssistant, entry: GreeneyeConfigEntry) -> None:
    hass.config_entries.async_schedule_reload(entry.entry_id)


async def _async_discard_monitors(monitors: greeneye.Monitors) -> None:
    """Clean up a Monitors object whose server never started.

    Monitors.close() only tears things down once the server is listening, which
    would leave the packet consumer task running forever on every retry.
    """
    processor = getattr(monitors, "_processor", None)
    task = getattr(processor, "_consumer_task", None)
    if task is not None:
        task.cancel()
    await monitors.close()


async def async_unload_entry(hass: HomeAssistant, entry: GreeneyeConfigEntry) -> bool:
    """Unload the GreenEye Monitor config entry."""
    if unload_ok := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.close()
    return unload_ok

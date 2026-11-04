"""Config flows for greeneye_monitor."""
from __future__ import annotations

import socket
from copy import deepcopy
from typing import Any

import greeneye
import homeassistant.helpers.config_validation as cv
import voluptuous as vol
from greeneye.api import TemperatureUnit
from greeneye.monitor import MonitorType
from homeassistant.components.sensor import DEVICE_CLASS_UNITS
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.config_entries import ConfigEntryState
from homeassistant.config_entries import ConfigFlow
from homeassistant.config_entries import ConfigFlowResult
from homeassistant.config_entries import OptionsFlow
from homeassistant.const import CONF_PORT
from homeassistant.const import CONF_TEMPERATURE_UNIT
from homeassistant.const import UnitOfEnergy
from homeassistant.const import UnitOfInformation
from homeassistant.const import UnitOfLength
from homeassistant.const import UnitOfPrecipitationDepth
from homeassistant.const import UnitOfTemperature
from homeassistant.const import UnitOfTime
from homeassistant.const import UnitOfVolume
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector
from homeassistant.helpers.typing import DiscoveryInfoType

from .const import AUX5_TYPE_CT
from .const import AUX5_TYPE_PULSE_COUNTER
from .const import CONF_AUX5_TYPE
from .const import CONF_CHANNELS
from .const import CONF_COUNTED_QUANTITY
from .const import CONF_COUNTED_QUANTITY_PER_PULSE
from .const import CONF_DEVICE_CLASS
from .const import CONF_ENERGY_UPDATE_INTERVAL
from .const import CONF_IS_AUX
from .const import CONF_MONITORS
from .const import CONF_NET_METERING
from .const import CONF_NUMBER
from .const import CONF_PULSE_COUNTERS
from .const import CONF_SEND_PACKET_DELAY
from .const import CONF_SERIAL_NUMBER
from .const import CONF_TEMPERATURE_SENSORS
from .const import CONF_TIME_UNIT
from .const import CONFIG_ENTRY_TITLE
from .const import DEFAULT_ENERGY_UPDATE_INTERVAL
from .const import DOMAIN
from .const import get_monitor_type_long_name
from .const import get_monitor_type_short_name
from .const import GreeneyeConfigEntry
from .const import MAX_ENERGY_UPDATE_INTERVAL

AUX5_PULSE_COUNTER_NUMBER = 4
DEFAULT_NUM_CHANNELS = 48
DEFAULT_PORT = 8000

COUNTED_QUANTITY_OPTIONS = sorted(
    {
        *[i.value for i in UnitOfVolume],
        *[i.value for i in UnitOfEnergy],
        *[i.value for i in UnitOfTime],
        *[i.value for i in UnitOfLength],
        *[i.value for i in UnitOfPrecipitationDepth],
        *[i.value for i in UnitOfInformation],
        "pulses",
    }
)

TIME_UNIT_OPTIONS = [
    UnitOfTime.SECONDS.value,
    UnitOfTime.MINUTES.value,
    UnitOfTime.HOURS.value,
]

# Device classes that make sense for a cumulative pulse count
PULSE_COUNTER_DEVICE_CLASS_OPTIONS = sorted(
    device_class.value
    for device_class in SensorDeviceClass
    if device_class
    not in (
        SensorDeviceClass.DATE,
        SensorDeviceClass.ENUM,
        SensorDeviceClass.TIMESTAMP,
    )
)

# Schemas that validate stored config entry data. They are also used to fill in
# defaults when building that data, but never shown in the UI. Unknown keys are
# kept so that data written by other versions never blocks saving.
PULSE_COUNTER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NUMBER): cv.positive_int,
        vol.Optional(CONF_IS_AUX, default=False): bool,
        vol.Required(CONF_COUNTED_QUANTITY, default="pulses"): cv.string,
        vol.Required(CONF_COUNTED_QUANTITY_PER_PULSE, default=1.0): vol.Coerce(float),
        vol.Optional(CONF_DEVICE_CLASS, default=None): vol.Maybe(
            vol.Coerce(SensorDeviceClass)
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

MONITOR_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SERIAL_NUMBER): cv.positive_int,
        vol.Optional(CONF_NET_METERING, default=[]): [str],
        vol.Optional(CONF_TEMPERATURE_UNIT): vol.Coerce(UnitOfTemperature),
        vol.Optional(CONF_AUX5_TYPE, default=None): vol.Maybe(
            vol.In([AUX5_TYPE_CT, AUX5_TYPE_PULSE_COUNTER])
        ),
        vol.Optional(CONF_PULSE_COUNTERS, default=[]): [PULSE_COUNTER_SCHEMA],
    },
    extra=vol.ALLOW_EXTRA,
)

CONFIG_ENTRY_DATA_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORT): cv.port,
        vol.Optional(CONF_MONITORS, default=[]): [MONITOR_SCHEMA],
    },
    extra=vol.ALLOW_EXTRA,
)

PULSE_COUNTER_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_NUMBER): cv.positive_int,
        vol.Optional(CONF_TIME_UNIT, default=UnitOfTime.SECONDS.value): vol.In(
            TIME_UNIT_OPTIONS
        ),
    },
    extra=vol.ALLOW_EXTRA,
)

MONITOR_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_SERIAL_NUMBER): cv.positive_int,
        vol.Optional(CONF_PULSE_COUNTERS, default=[]): [PULSE_COUNTER_OPTIONS_SCHEMA],
    },
    extra=vol.ALLOW_EXTRA,
)

CONFIG_ENTRY_OPTIONS_SCHEMA = vol.Schema(
    {
        vol.Optional(CONF_SEND_PACKET_DELAY, default=False): bool,
        vol.Optional(
            CONF_ENERGY_UPDATE_INTERVAL,
            default=int(DEFAULT_ENERGY_UPDATE_INTERVAL.total_seconds()),
        ): vol.All(
            vol.Coerce(int),
            vol.Range(min=0, max=int(MAX_ENERGY_UPDATE_INTERVAL.total_seconds())),
        ),
        vol.Optional(CONF_MONITORS, default=[]): [MONITOR_OPTIONS_SCHEMA],
    },
    extra=vol.ALLOW_EXTRA,
)

# Schemas shown in the UI.
PORT_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_PORT, default=DEFAULT_PORT): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=1, max=65535, step=1, mode=selector.NumberSelectorMode.BOX
            )
        ),
    }
)

PULSE_COUNTER_UI_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_COUNTED_QUANTITY): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=COUNTED_QUANTITY_OPTIONS,
                custom_value=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_COUNTED_QUANTITY_PER_PULSE): selector.NumberSelector(
            selector.NumberSelectorConfig(
                min=0, step="any", mode=selector.NumberSelectorMode.BOX
            )
        ),
        vol.Optional(CONF_DEVICE_CLASS): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=PULSE_COUNTER_DEVICE_CLASS_OPTIONS,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
        vol.Required(CONF_TIME_UNIT): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=TIME_UNIT_OPTIONS, translation_key="time_unit"
            )
        ),
    }
)

PULSE_COUNTER_UI_DEFAULTS = {
    CONF_COUNTED_QUANTITY: "pulses",
    CONF_COUNTED_QUANTITY_PER_PULSE: 1.0,
    CONF_TIME_UNIT: UnitOfTime.SECONDS.value,
}


def _find(items: list[dict[str, Any]], key: str, value: Any) -> dict[str, Any] | None:
    return next((item for item in items if item[key] == value), None)


def _pulse_counter_label(pulse_counter: dict[str, Any]) -> str:
    if pulse_counter.get(CONF_IS_AUX):
        return "Aux 5"
    return str(pulse_counter[CONF_NUMBER] + 1)


def _validate_pulse_counter(user_input: dict[str, Any]) -> dict[str, str]:
    """Check that the unit is one Home Assistant accepts for the device class."""
    device_class = user_input.get(CONF_DEVICE_CLASS)
    if not device_class:
        return {}
    units = DEVICE_CLASS_UNITS.get(SensorDeviceClass(device_class))
    if units is not None and user_input[CONF_COUNTED_QUANTITY] not in units:
        return {CONF_DEVICE_CLASS: "unit_not_valid_for_device_class"}
    return {}


def _pulse_counter_ui_to_config(
    number: int, is_aux: bool, user_input: dict[str, Any]
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Split the pulse counter form into config entry data and options."""
    data = PULSE_COUNTER_SCHEMA(
        {
            CONF_NUMBER: number,
            CONF_IS_AUX: is_aux,
            CONF_COUNTED_QUANTITY: user_input[CONF_COUNTED_QUANTITY],
            CONF_COUNTED_QUANTITY_PER_PULSE: user_input[
                CONF_COUNTED_QUANTITY_PER_PULSE
            ],
            CONF_DEVICE_CLASS: user_input.get(CONF_DEVICE_CLASS) or None,
        }
    )
    options = PULSE_COUNTER_OPTIONS_SCHEMA(
        {CONF_NUMBER: number, CONF_TIME_UNIT: user_input[CONF_TIME_UNIT]}
    )
    return data, options


def make_monitor_settings_schema(
    monitor: greeneye.monitor.Monitor | None,
    monitor_config: dict[str, Any] | None = None,
) -> vol.Schema:
    """Build the form for per-monitor settings.

    Fields are chosen based on what kind of monitor this is. When editing a
    monitor that hasn't reported in since Home Assistant started, we fall back to
    whatever fields were configured previously.
    """
    num_channels = DEFAULT_NUM_CHANNELS
    if monitor and monitor.channels:
        num_channels = len(monitor.channels)
    if monitor_config:
        # Never hide a channel that is already selected
        for channel in monitor_config[CONF_NET_METERING]:
            num_channels = max(num_channels, int(channel) + 1)

    schema: dict[Any, Any] = {
        vol.Optional(CONF_NET_METERING): selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    selector.SelectOptionDict(value=str(i), label=str(i + 1))
                    for i in range(num_channels)
                ],
                multiple=True,
                mode=selector.SelectSelectorMode.DROPDOWN,
            )
        ),
    }

    if monitor is not None:
        show_temperature = monitor.type == MonitorType.GEM
        show_aux5 = monitor.type == MonitorType.ECM_1240
    else:
        assert monitor_config is not None
        show_temperature = CONF_TEMPERATURE_UNIT in monitor_config
        show_aux5 = monitor_config.get(CONF_AUX5_TYPE) is not None

    if show_temperature:
        schema[vol.Required(CONF_TEMPERATURE_UNIT)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[UnitOfTemperature.CELSIUS, UnitOfTemperature.FAHRENHEIT]
            )
        )
    if show_aux5:
        schema[vol.Required(CONF_AUX5_TYPE)] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[AUX5_TYPE_CT, AUX5_TYPE_PULSE_COUNTER],
                translation_key="aux5_type",
            )
        )

    return vol.Schema(schema)


def _monitor_settings_defaults(
    monitor: greeneye.monitor.Monitor | None,
) -> dict[str, Any]:
    """Suggest settings for a newly discovered monitor based on what it reports."""
    defaults: dict[str, Any] = {
        CONF_TEMPERATURE_UNIT: UnitOfTemperature.CELSIUS,
        CONF_AUX5_TYPE: AUX5_TYPE_CT,
        CONF_NET_METERING: [],
    }
    if monitor is None:
        return defaults
    if monitor.temperature_sensors:
        if monitor.temperature_sensors[0].unit == TemperatureUnit.FAHRENHEIT:
            defaults[CONF_TEMPERATURE_UNIT] = UnitOfTemperature.FAHRENHEIT
    defaults[CONF_NET_METERING] = [
        str(channel.number) for channel in monitor.channels if channel.net_metering
    ]
    return defaults


def _can_listen_on_port(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("", port))
        except OSError:
            return False
    return True


class GreeneyeMonitorConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for greeneye_monitor."""

    VERSION = 1

    def __init__(self) -> None:
        self._entry: GreeneyeConfigEntry | None = None
        self._monitor: greeneye.monitor.Monitor | None = None
        self._monitor_data: dict[str, Any] = {}
        self._pulse_counter_data: list[dict[str, Any]] = []
        self._pulse_counter_options: list[dict[str, Any]] = []
        self._pulse_counters_to_configure: list[tuple[int, bool]] = []

    @staticmethod
    @callback
    def async_get_options_flow(entry: GreeneyeConfigEntry) -> OptionsFlow:
        """Create the options flow."""
        return GreeneyeMonitorOptionsFlow()

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()

        errors: dict[str, str] = {}
        if user_input is not None:
            port = int(user_input[CONF_PORT])
            if await self.hass.async_add_executor_job(_can_listen_on_port, port):
                return self.async_create_entry(
                    title=CONFIG_ENTRY_TITLE,
                    data=CONFIG_ENTRY_DATA_SCHEMA({CONF_PORT: port}),
                    options=CONFIG_ENTRY_OPTIONS_SCHEMA({}),
                )
            errors[CONF_PORT] = "port_in_use"

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(PORT_SCHEMA, user_input),
            errors=errors,
        )

    async def async_step_import(
        self, import_data: DiscoveryInfoType
    ) -> ConfigFlowResult:
        """Create a config entry from YAML configuration."""
        data, options = yaml_to_config_entry(import_data)

        if entry := await self.async_set_unique_id(DOMAIN):
            yaml_serial_numbers = {
                monitor[CONF_SERIAL_NUMBER] for monitor in data[CONF_MONITORS]
            }

            data[CONF_MONITORS].extend(
                monitor
                for monitor in entry.data[CONF_MONITORS]
                if monitor[CONF_SERIAL_NUMBER] not in yaml_serial_numbers
            )
            options[CONF_MONITORS].extend(
                monitor
                for monitor in entry.options[CONF_MONITORS]
                if monitor[CONF_SERIAL_NUMBER] not in yaml_serial_numbers
            )
            options[CONF_SEND_PACKET_DELAY] = entry.options.get(
                CONF_SEND_PACKET_DELAY, False
            )
            if CONF_ENERGY_UPDATE_INTERVAL in entry.options:
                options[CONF_ENERGY_UPDATE_INTERVAL] = entry.options[
                    CONF_ENERGY_UPDATE_INTERVAL
                ]

            self.hass.config_entries.async_update_entry(
                entry, data=data, options=options
            )
            self._abort_if_unique_id_configured()

        # This integration used to name entities based on what the user had put in the YAML.
        # Now it uses standardized entity names. If the user hasn't renamed an entity via the UI,
        # if we do nothing, the entity name will change from something they had specified in the YAML
        # to something generic. To avoid this, we look for entities that weren't renamed and we copy their
        # old integration-defined names to user-defined ones.
        entity_registry = er.async_get(self.hass)
        for registry_entry in list(entity_registry.entities.values()):
            if registry_entry.platform != DOMAIN:
                continue

            if registry_entry.name is None:
                entity_registry.async_update_entity(
                    registry_entry.entity_id, name=registry_entry.original_name
                )

        return self.async_create_entry(
            title=CONFIG_ENTRY_TITLE, data=data, options=options
        )

    async def async_step_integration_discovery(
        self, discovery_info: DiscoveryInfoType
    ) -> ConfigFlowResult:
        """Handle a monitor that has started sending packets but isn't configured."""
        serial_number: int = discovery_info[CONF_SERIAL_NUMBER]

        # Each monitor gets its own unique ID so that ignoring one monitor
        # doesn't create a second entry with the hub's unique ID.
        await self.async_set_unique_id(f"{DOMAIN}_{serial_number}")
        self._abort_if_unique_id_configured()

        entry = self._async_get_hub_entry()
        if entry is None or entry.state is not ConfigEntryState.LOADED:
            return self.async_abort(reason="not_loaded")
        if _find(entry.data[CONF_MONITORS], CONF_SERIAL_NUMBER, serial_number):
            return self.async_abort(reason="already_configured")
        monitor = entry.runtime_data.monitors.get(serial_number)
        if monitor is None:
            return self.async_abort(reason="not_loaded")

        self._entry = entry
        self._monitor = monitor
        self.context["title_placeholders"] = {
            "device_name": f"{get_monitor_type_long_name(monitor)} {serial_number}",
            "serial_number": f"{serial_number}",
        }
        return await self.async_step_monitor()

    @callback
    def _async_get_hub_entry(self) -> GreeneyeConfigEntry | None:
        return next(
            (
                entry
                for entry in self._async_current_entries(include_ignore=False)
                if entry.unique_id == DOMAIN
            ),
            None,
        )

    async def async_step_monitor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask how the newly discovered monitor is set up."""
        monitor = self._monitor
        assert monitor is not None

        if user_input is not None:
            aux5_type = user_input.get(CONF_AUX5_TYPE)
            self._monitor_data = {
                CONF_SERIAL_NUMBER: monitor.serial_number,
                CONF_NET_METERING: user_input.get(CONF_NET_METERING, []),
                CONF_AUX5_TYPE: aux5_type,
            }
            if CONF_TEMPERATURE_UNIT in user_input:
                self._monitor_data[CONF_TEMPERATURE_UNIT] = user_input[
                    CONF_TEMPERATURE_UNIT
                ]

            if aux5_type == AUX5_TYPE_PULSE_COUNTER:
                self._pulse_counters_to_configure = [
                    (AUX5_PULSE_COUNTER_NUMBER, True)
                ]
            else:
                self._pulse_counters_to_configure = [
                    (pulse_counter.number, False)
                    for pulse_counter in monitor.pulse_counters
                ]
            return await self.async_step_pulse_counter()

        return self.async_show_form(
            step_id="monitor",
            data_schema=self.add_suggested_values_to_schema(
                make_monitor_settings_schema(monitor),
                _monitor_settings_defaults(monitor),
            ),
            description_placeholders={
                "serial_number": f"{monitor.serial_number}",
                "device_name": (
                    f"{get_monitor_type_short_name(monitor)} {monitor.serial_number}"
                ),
            },
        )

    async def async_step_pulse_counter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask what each of the monitor's pulse counters is counting."""
        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_pulse_counter(user_input)
            if not errors:
                number, is_aux = self._pulse_counters_to_configure.pop(0)
                data, options = _pulse_counter_ui_to_config(number, is_aux, user_input)
                self._pulse_counter_data.append(data)
                self._pulse_counter_options.append(options)

        if not self._pulse_counters_to_configure:
            return self._async_add_monitor_to_entry()

        number, is_aux = self._pulse_counters_to_configure[0]
        return self.async_show_form(
            step_id="pulse_counter",
            data_schema=self.add_suggested_values_to_schema(
                PULSE_COUNTER_UI_SCHEMA, user_input or PULSE_COUNTER_UI_DEFAULTS
            ),
            errors=errors,
            description_placeholders={
                "pulse_counter_number": "Aux 5" if is_aux else f"{number + 1}"
            },
        )

    @callback
    def _async_add_monitor_to_entry(self) -> ConfigFlowResult:
        assert self._entry is not None
        assert self._monitor is not None
        serial_number = self._monitor.serial_number
        # entry.data is read here, not at the start of the flow, so that monitors
        # configured while this flow was waiting for input are kept.
        entry = self._entry
        if _find(entry.data[CONF_MONITORS], CONF_SERIAL_NUMBER, serial_number):
            return self.async_abort(reason="already_configured")

        new_data = deepcopy(dict(entry.data))
        new_data[CONF_MONITORS].append(
            MONITOR_SCHEMA(
                {**self._monitor_data, CONF_PULSE_COUNTERS: self._pulse_counter_data}
            )
        )

        new_options = CONFIG_ENTRY_OPTIONS_SCHEMA(deepcopy(dict(entry.options)))
        new_options[CONF_MONITORS] = [
            monitor
            for monitor in new_options[CONF_MONITORS]
            if monitor[CONF_SERIAL_NUMBER] != serial_number
        ]
        new_options[CONF_MONITORS].append(
            MONITOR_OPTIONS_SCHEMA(
                {
                    CONF_SERIAL_NUMBER: serial_number,
                    CONF_PULSE_COUNTERS: self._pulse_counter_options,
                }
            )
        )

        # The entry's update listener reloads it, which creates the entities.
        self.hass.config_entries.async_update_entry(
            entry, data=new_data, options=new_options
        )
        return self.async_abort(
            reason="success",
            description_placeholders={"serial_number": f"{serial_number}"},
        )


def yaml_to_config_entry(
    yaml: DiscoveryInfoType,
) -> tuple[dict[str, Any], dict[str, Any]]:
    data = CONFIG_ENTRY_DATA_SCHEMA(
        {
            CONF_PORT: yaml[CONF_PORT],
            CONF_MONITORS: [
                {
                    CONF_SERIAL_NUMBER: monitor[CONF_SERIAL_NUMBER],
                    CONF_TEMPERATURE_UNIT: monitor[CONF_TEMPERATURE_SENSORS][
                        CONF_TEMPERATURE_UNIT
                    ],
                    CONF_NET_METERING: [
                        str(channel[CONF_NUMBER] - 1)
                        for channel in monitor[CONF_CHANNELS]
                        if channel[CONF_NET_METERING]
                    ],
                    CONF_PULSE_COUNTERS: [
                        {
                            CONF_NUMBER: pulse_counter[CONF_NUMBER] - 1,
                            CONF_DEVICE_CLASS: pulse_counter[CONF_DEVICE_CLASS],
                            CONF_COUNTED_QUANTITY: pulse_counter[CONF_COUNTED_QUANTITY],
                            CONF_COUNTED_QUANTITY_PER_PULSE: pulse_counter[
                                CONF_COUNTED_QUANTITY_PER_PULSE
                            ],
                        }
                        for pulse_counter in monitor[CONF_PULSE_COUNTERS]
                    ],
                }
                for monitor in yaml[CONF_MONITORS]
            ],
        }
    )
    options = CONFIG_ENTRY_OPTIONS_SCHEMA(
        {
            CONF_MONITORS: [
                {
                    CONF_SERIAL_NUMBER: monitor[CONF_SERIAL_NUMBER],
                    CONF_PULSE_COUNTERS: [
                        {
                            CONF_NUMBER: pulse_counter[CONF_NUMBER] - 1,
                            CONF_TIME_UNIT: pulse_counter[CONF_TIME_UNIT],
                        }
                        for pulse_counter in monitor[CONF_PULSE_COUNTERS]
                    ],
                }
                for monitor in yaml[CONF_MONITORS]
            ]
        }
    )

    return data, options


class GreeneyeMonitorOptionsFlow(OptionsFlow):
    """Edit settings for the hub and for each monitor."""

    def __init__(self) -> None:
        self._serial_number: int | None = None
        self._pulse_counter_number: int | None = None
        self._after_choose_monitor: str = ""

    @property
    def _monitor_configs(self) -> list[dict[str, Any]]:
        return self.config_entry.data[CONF_MONITORS]

    def _running_monitor(self, serial_number: int) -> greeneye.monitor.Monitor | None:
        """Return the live monitor object, if it has reported since startup."""
        if self.config_entry.state is not ConfigEntryState.LOADED:
            return None
        return self.config_entry.runtime_data.monitors.get(serial_number)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the options menu."""
        menu_options = ["global_options"]
        if self._monitor_configs:
            menu_options.append("monitor_settings")
        if any(config[CONF_PULSE_COUNTERS] for config in self._monitor_configs):
            menu_options.append("pulse_counter_settings")
        return self.async_show_menu(step_id="init", menu_options=menu_options)

    async def async_step_global_options(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            options = CONFIG_ENTRY_OPTIONS_SCHEMA(deepcopy(dict(self.config_entry.options)))
            options[CONF_SEND_PACKET_DELAY] = user_input[CONF_SEND_PACKET_DELAY]
            options[CONF_ENERGY_UPDATE_INTERVAL] = int(
                user_input[CONF_ENERGY_UPDATE_INTERVAL]
            )
            return self.async_create_entry(data=options)

        current = CONFIG_ENTRY_OPTIONS_SCHEMA(deepcopy(dict(self.config_entry.options)))
        schema = vol.Schema(
            {
                vol.Required(CONF_ENERGY_UPDATE_INTERVAL): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=0,
                        max=int(MAX_ENERGY_UPDATE_INTERVAL.total_seconds()),
                        step=1,
                        unit_of_measurement="s",
                        mode=selector.NumberSelectorMode.BOX,
                    )
                ),
                vol.Required(CONF_SEND_PACKET_DELAY): selector.BooleanSelector(),
            }
        )
        return self.async_show_form(
            step_id="global_options",
            data_schema=self.add_suggested_values_to_schema(schema, current),
        )

    async def async_step_monitor_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._after_choose_monitor = "monitor"
        return await self.async_step_choose_monitor()

    async def async_step_pulse_counter_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        self._after_choose_monitor = "choose_pulse_counter"
        return await self.async_step_choose_monitor()

    async def async_step_choose_monitor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        serial_numbers = [
            str(config[CONF_SERIAL_NUMBER])
            for config in self._monitor_configs
            if self._after_choose_monitor != "choose_pulse_counter"
            or config[CONF_PULSE_COUNTERS]
        ]

        if user_input is not None or len(serial_numbers) == 1:
            serial_number = (
                user_input[CONF_SERIAL_NUMBER] if user_input else serial_numbers[0]
            )
            self._serial_number = int(serial_number)
            return await getattr(self, f"async_step_{self._after_choose_monitor}")()

        return self.async_show_form(
            step_id="choose_monitor",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_SERIAL_NUMBER): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=serial_numbers,
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    async def async_step_monitor(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit net metering, temperature unit and Aux 5 mode for one monitor."""
        assert self._serial_number is not None
        data = deepcopy(dict(self.config_entry.data))
        options = CONFIG_ENTRY_OPTIONS_SCHEMA(deepcopy(dict(self.config_entry.options)))
        monitor_config = _find(data[CONF_MONITORS], CONF_SERIAL_NUMBER, self._serial_number)
        assert monitor_config is not None

        if user_input is not None:
            monitor_config[CONF_NET_METERING] = sorted(
                user_input.get(CONF_NET_METERING, []), key=int
            )
            if CONF_TEMPERATURE_UNIT in user_input:
                monitor_config[CONF_TEMPERATURE_UNIT] = user_input[
                    CONF_TEMPERATURE_UNIT
                ]
            if CONF_AUX5_TYPE in user_input:
                monitor_config[CONF_AUX5_TYPE] = user_input[CONF_AUX5_TYPE]
                if user_input[CONF_AUX5_TYPE] == AUX5_TYPE_PULSE_COUNTER:
                    _ensure_aux5_pulse_counter(monitor_config, options, self._serial_number)
            return self._async_save(data, options)

        monitor = self._running_monitor(self._serial_number)
        return self.async_show_form(
            step_id="monitor",
            data_schema=self.add_suggested_values_to_schema(
                make_monitor_settings_schema(monitor, monitor_config), monitor_config
            ),
            description_placeholders={"serial_number": str(self._serial_number)},
        )

    async def async_step_choose_pulse_counter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._serial_number is not None
        monitor_config = _find(
            self._monitor_configs, CONF_SERIAL_NUMBER, self._serial_number
        )
        assert monitor_config is not None
        pulse_counters = monitor_config[CONF_PULSE_COUNTERS]

        if user_input is not None or len(pulse_counters) == 1:
            number = (
                user_input[CONF_NUMBER]
                if user_input
                else pulse_counters[0][CONF_NUMBER]
            )
            self._pulse_counter_number = int(number)
            return await self.async_step_pulse_counter()

        return self.async_show_form(
            step_id="choose_pulse_counter",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_NUMBER): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=str(pulse_counter[CONF_NUMBER]),
                                    label=_pulse_counter_label(pulse_counter),
                                )
                                for pulse_counter in pulse_counters
                            ],
                            mode=selector.SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
            description_placeholders={CONF_SERIAL_NUMBER: str(self._serial_number)},
        )

    async def async_step_pulse_counter(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Edit what a pulse counter is counting and how its rate is shown."""
        assert self._serial_number is not None
        assert self._pulse_counter_number is not None
        data = deepcopy(dict(self.config_entry.data))
        options = CONFIG_ENTRY_OPTIONS_SCHEMA(deepcopy(dict(self.config_entry.options)))
        monitor_config = _find(data[CONF_MONITORS], CONF_SERIAL_NUMBER, self._serial_number)
        assert monitor_config is not None
        pulse_counter_config = _find(
            monitor_config[CONF_PULSE_COUNTERS], CONF_NUMBER, self._pulse_counter_number
        )
        assert pulse_counter_config is not None
        monitor_options = _get_or_add_monitor_options(options, self._serial_number)
        pulse_counter_options = _find(
            monitor_options[CONF_PULSE_COUNTERS], CONF_NUMBER, self._pulse_counter_number
        )
        if pulse_counter_options is None:
            pulse_counter_options = PULSE_COUNTER_OPTIONS_SCHEMA(
                {CONF_NUMBER: self._pulse_counter_number}
            )
            monitor_options[CONF_PULSE_COUNTERS].append(pulse_counter_options)

        errors: dict[str, str] = {}
        if user_input is not None:
            errors = _validate_pulse_counter(user_input)
            if not errors:
                new_config, new_options = _pulse_counter_ui_to_config(
                    self._pulse_counter_number,
                    pulse_counter_config.get(CONF_IS_AUX, False),
                    user_input,
                )
                pulse_counter_config.update(new_config)
                pulse_counter_options.update(new_options)
                return self._async_save(data, options)

        current = {**pulse_counter_config, **pulse_counter_options}
        if current.get(CONF_DEVICE_CLASS) is None:
            current.pop(CONF_DEVICE_CLASS, None)
        return self.async_show_form(
            step_id="pulse_counter",
            data_schema=self.add_suggested_values_to_schema(
                PULSE_COUNTER_UI_SCHEMA, user_input or current
            ),
            errors=errors,
            description_placeholders={
                "pulse_counter_number": _pulse_counter_label(pulse_counter_config),
                "serial_number": str(self._serial_number),
            },
        )

    @callback
    def _async_save(
        self, data: dict[str, Any], options: dict[str, Any]
    ) -> ConfigFlowResult:
        """Save data and options together so the entry only reloads once."""
        self.hass.config_entries.async_update_entry(
            self.config_entry, data=CONFIG_ENTRY_DATA_SCHEMA(data), options=options
        )
        return self.async_create_entry(data=options)


def _get_or_add_monitor_options(
    options: dict[str, Any], serial_number: int
) -> dict[str, Any]:
    monitor_options = _find(options[CONF_MONITORS], CONF_SERIAL_NUMBER, serial_number)
    if monitor_options is None:
        monitor_options = MONITOR_OPTIONS_SCHEMA({CONF_SERIAL_NUMBER: serial_number})
        options[CONF_MONITORS].append(monitor_options)
    return monitor_options


def _ensure_aux5_pulse_counter(
    monitor_config: dict[str, Any], options: dict[str, Any], serial_number: int
) -> None:
    """Give a newly enabled Aux 5 pulse counter a default configuration."""
    if _find(monitor_config[CONF_PULSE_COUNTERS], CONF_NUMBER, AUX5_PULSE_COUNTER_NUMBER):
        return
    data, pulse_options = _pulse_counter_ui_to_config(
        AUX5_PULSE_COUNTER_NUMBER, True, PULSE_COUNTER_UI_DEFAULTS
    )
    monitor_config[CONF_PULSE_COUNTERS].append(data)
    _get_or_add_monitor_options(options, serial_number)[CONF_PULSE_COUNTERS].append(
        pulse_options
    )

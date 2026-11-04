"""Support for the sensors in a Brultech energy monitor."""
from __future__ import annotations

import logging
import time
from datetime import timedelta
from typing import Any

import greeneye
from homeassistant.components.sensor import SensorDeviceClass
from homeassistant.components.sensor import SensorEntity
from homeassistant.components.sensor import SensorStateClass
from homeassistant.const import CONF_TEMPERATURE_UNIT
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.const import UnitOfElectricPotential
from homeassistant.const import UnitOfEnergy
from homeassistant.const import UnitOfPower
from homeassistant.const import UnitOfTime
from homeassistant.core import callback
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import AUX5_TYPE_PULSE_COUNTER
from .const import CONF_AUX5_TYPE
from .const import CONF_COUNTED_QUANTITY
from .const import CONF_COUNTED_QUANTITY_PER_PULSE
from .const import CONF_DEVICE_CLASS
from .const import CONF_ENERGY_UPDATE_INTERVAL
from .const import CONF_MONITORS
from .const import CONF_NET_METERING
from .const import CONF_NUMBER
from .const import CONF_PULSE_COUNTERS
from .const import CONF_SERIAL_NUMBER
from .const import CONF_TIME_UNIT
from .const import DEFAULT_ENERGY_UPDATE_INTERVAL
from .const import DEVICE_TYPE_AUX
from .const import DEVICE_TYPE_CURRENT_TRANSFORMER
from .const import DEVICE_TYPE_PULSE_COUNTER
from .const import DEVICE_TYPE_TEMPERATURE_SENSOR
from .const import DEVICE_TYPE_VOLTAGE_SENSOR
from .const import GreeneyeConfigEntry
from .const import make_device_info
from .const import signal_new_monitor

DATA_PULSES = "pulses"
DATA_WATT_SECONDS = "watt_seconds"

COUNTER_ICON = "mdi:counter"

SECONDS_PER_TIME_UNIT = {
    UnitOfTime.SECONDS.value: 1,
    UnitOfTime.MINUTES.value: 60,
    UnitOfTime.HOURS.value: 3600,
}

_LOGGER = logging.getLogger(__name__)


def _find(items: list[dict[str, Any]], key: str, value: Any) -> dict[str, Any] | None:
    return next((item for item in items if item[key] == value), None)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GreeneyeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Brultech energy monitor sensors from the config entry"""
    energy_update_interval = timedelta(
        seconds=entry.options.get(
            CONF_ENERGY_UPDATE_INTERVAL,
            DEFAULT_ENERGY_UPDATE_INTERVAL.total_seconds(),
        )
    )
    added_monitors: set[int] = set()

    @callback
    def on_new_monitor(monitor: greeneye.monitor.Monitor, device_id: str) -> None:
        if monitor.serial_number in added_monitors:
            return
        monitor_config = _find(
            entry.data[CONF_MONITORS], CONF_SERIAL_NUMBER, monitor.serial_number
        )
        if monitor_config is None:
            return
        monitor_option = _find(
            entry.options.get(CONF_MONITORS, []),
            CONF_SERIAL_NUMBER,
            monitor.serial_number,
        ) or {CONF_PULSE_COUNTERS: []}

        added_monitors.add(monitor.serial_number)
        async_add_entities(
            _make_entities(
                monitor, device_id, monitor_config, monitor_option, energy_update_interval
            )
        )
        _LOGGER.info("Set up sensors for monitor %d", monitor.serial_number)

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_monitor(entry.entry_id), on_new_monitor)
    )


def _make_entities(
    monitor: greeneye.monitor.Monitor,
    device_id: str,
    monitor_config: dict[str, Any],
    monitor_option: dict[str, Any],
    energy_update_interval: timedelta,
) -> list[Entity]:
    entities: list[Entity] = []

    net_metering = set(monitor_config.get(CONF_NET_METERING, []))
    for channel in monitor.channels:
        channel_net_metered = str(channel.number) in net_metering
        entities.append(PowerSensor(monitor, device_id, channel, channel_net_metered))
        entities.append(CurrentSensor(monitor, device_id, channel))
        entities.append(
            EnergySensor(
                monitor, device_id, channel, channel_net_metered, energy_update_interval
            )
        )

    def add_pulse_counter(pulse_counter: greeneye.monitor.PulseCounter) -> None:
        config = _find(
            monitor_config.get(CONF_PULSE_COUNTERS, []), CONF_NUMBER, pulse_counter.number
        )
        if config is None:
            _LOGGER.warning(
                "Pulse counter %s on monitor %d is not configured; skipping it",
                "Aux 5" if pulse_counter.is_aux else pulse_counter.number + 1,
                monitor.serial_number,
            )
            return
        options = _find(
            monitor_option[CONF_PULSE_COUNTERS], CONF_NUMBER, pulse_counter.number
        ) or {CONF_TIME_UNIT: UnitOfTime.SECONDS.value}
        device_class = config.get(CONF_DEVICE_CLASS)
        entities.append(
            PulseRateSensor(
                monitor,
                device_id,
                pulse_counter,
                config[CONF_COUNTED_QUANTITY],
                options[CONF_TIME_UNIT],
                config[CONF_COUNTED_QUANTITY_PER_PULSE],
            )
        )
        entities.append(
            PulseCountSensor(
                monitor,
                device_id,
                pulse_counter,
                SensorDeviceClass(device_class) if device_class else None,
                config[CONF_COUNTED_QUANTITY],
                config[CONF_COUNTED_QUANTITY_PER_PULSE],
                energy_update_interval,
            )
        )

    for pulse_counter in monitor.pulse_counters:
        add_pulse_counter(pulse_counter)

    if temperature_unit := monitor_config.get(CONF_TEMPERATURE_UNIT):
        for temperature_sensor in monitor.temperature_sensors:
            entities.append(
                TemperatureSensor(monitor, device_id, temperature_sensor, temperature_unit)
            )

    if monitor.voltage_sensor:
        entities.append(VoltageSensor(monitor, device_id))

    for aux in monitor.aux:
        if isinstance(aux, greeneye.monitor.Channel):
            channel = aux
        elif monitor_config.get(CONF_AUX5_TYPE) == AUX5_TYPE_PULSE_COUNTER:
            add_pulse_counter(aux.pulse_counter)
            continue
        else:
            channel = aux.channel

        entities.append(PowerSensor(monitor, device_id, channel, False))
        entities.append(
            EnergySensor(monitor, device_id, channel, False, energy_update_interval)
        )

    return entities


UnderlyingSensorType = (
    greeneye.monitor.Channel
    | greeneye.monitor.PulseCounter
    | greeneye.monitor.TemperatureSensor
    | greeneye.monitor.VoltageSensor
)


class MonitorSensor(SensorEntity):
    """Base class for Brultech energy monitor sensors."""

    _attr_entity_registry_enabled_default = False
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_suggested_display_precision = 0

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        device_type: str,
        sensor_type: str,
        sensor: UnderlyingSensorType,
        number: int,
        update_interval: timedelta | None = None,
    ) -> None:
        """Construct the entity."""
        self._monitor = monitor
        self._sensor: UnderlyingSensorType = sensor
        self._attr_unique_id = f"{monitor.serial_number}-{sensor_type}-{number + 1}"
        self._attr_device_info: DeviceInfo = make_device_info(
            monitor, monitor_device_id, device_type, number
        )
        self._update_interval = (
            update_interval.total_seconds() if update_interval else 0
        )
        self._last_write = 0.0

    async def async_added_to_hass(self) -> None:
        """Connect to the sensor."""
        # Home Assistant writes the initial state once this returns
        self._last_write = time.monotonic()
        self._sensor.add_listener(self._on_sensor_update)
        self.async_on_remove(
            lambda: self._sensor.remove_listener(self._on_sensor_update)
        )

    @callback
    def _on_sensor_update(self) -> None:
        """Write the new state, at most once per update interval."""
        now = time.monotonic()
        if self._update_interval and now - self._last_write < self._update_interval:
            return
        self._last_write = now
        self.async_write_ha_state()


class PowerSensor(MonitorSensor):
    """Entity showing power usage on one channel of the monitor."""

    _attr_native_unit_of_measurement = UnitOfPower.WATT
    _attr_device_class = SensorDeviceClass.POWER
    _attr_name = None
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        sensor: greeneye.monitor.Channel,
        net_metering: bool,
    ) -> None:
        """Construct the entity."""
        super().__init__(
            monitor,
            monitor_device_id,
            DEVICE_TYPE_CURRENT_TRANSFORMER if not sensor.is_aux else DEVICE_TYPE_AUX,
            "current" if not sensor.is_aux else "aux_current",
            sensor,
            sensor.number,
        )
        self._sensor: greeneye.monitor.Channel = sensor
        self._net_metering = net_metering

    @property
    def native_value(self) -> float | None:
        """Return the current number of watts being used by the channel."""
        return self._sensor.watts

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Return total wattseconds in the state dictionary."""
        watt_seconds = self._sensor.watt_seconds
        if self._net_metering and watt_seconds:
            watt_seconds = abs(watt_seconds)

        return {DATA_WATT_SECONDS: watt_seconds}


class CurrentSensor(MonitorSensor):
    """Entity showing current on one channel of the monitor."""

    _attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
    _attr_device_class = SensorDeviceClass.CURRENT
    _attr_name = "current"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        sensor: greeneye.monitor.Channel,
    ) -> None:
        """Construct the entity."""
        super().__init__(
            monitor,
            monitor_device_id,
            DEVICE_TYPE_CURRENT_TRANSFORMER,
            "amps",
            sensor,
            sensor.number,
        )
        self._sensor: greeneye.monitor.Channel = sensor

    @property
    def native_value(self) -> float | None:
        """Return the current number of amps being used by the channel."""
        return self._sensor.amps


class EnergySensor(MonitorSensor):
    """Entity showing energy usage on one channel of the monitor."""

    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_name = "energy"
    _attr_state_class = SensorStateClass.TOTAL
    _attr_entity_registry_enabled_default = True

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        sensor: greeneye.monitor.Channel,
        net_metering: bool,
        update_interval: timedelta,
    ) -> None:
        """Construct the entity."""
        super().__init__(
            monitor,
            monitor_device_id,
            DEVICE_TYPE_CURRENT_TRANSFORMER if not sensor.is_aux else DEVICE_TYPE_AUX,
            "energy" if not sensor.is_aux else "aux_energy",
            sensor,
            sensor.number,
            update_interval=update_interval,
        )
        self._sensor: greeneye.monitor.Channel = sensor
        self._net_metering = net_metering

    @property
    def native_value(self) -> float | None:
        """Return the total number of kilowatt hours measured by this channel."""
        kwh = self._sensor.kilowatt_hours
        if self._net_metering and kwh:
            kwh = abs(kwh)
        return kwh


class PulseRateSensor(MonitorSensor):
    """Entity showing rate of change in one pulse counter of the monitor."""

    _attr_icon = COUNTER_ICON
    _attr_name = "rate"
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        sensor: greeneye.monitor.PulseCounter,
        counted_quantity: str,
        time_unit: str,
        counted_quantity_per_pulse: float,
    ) -> None:
        """Construct the entity."""
        super().__init__(
            monitor,
            monitor_device_id,
            DEVICE_TYPE_PULSE_COUNTER if not sensor.is_aux else DEVICE_TYPE_AUX,
            "pulse" if not sensor.is_aux else "aux_pulse",
            sensor,
            sensor.number,
        )
        self._sensor: greeneye.monitor.PulseCounter = sensor
        self._counted_quantity_per_pulse = counted_quantity_per_pulse
        self._seconds_per_time_unit = SECONDS_PER_TIME_UNIT[time_unit]
        self._attr_native_unit_of_measurement = f"{counted_quantity}/{time_unit}"

    @property
    def native_value(self) -> float | None:
        """Return the current rate of change for the given pulse counter."""
        if self._sensor.pulses_per_second is None:
            return None

        return (
            self._sensor.pulses_per_second
            * self._counted_quantity_per_pulse
            * self._seconds_per_time_unit
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return total pulses in the data dictionary."""
        return {DATA_PULSES: self._sensor.pulses}


class PulseCountSensor(MonitorSensor):
    """Entity showing pulse counts."""

    _attr_entity_registry_enabled_default = True
    _attr_name = None
    _attr_state_class = SensorStateClass.TOTAL

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        sensor: greeneye.monitor.PulseCounter,
        device_class: SensorDeviceClass | None,
        counted_quantity: str,
        counted_quantity_per_pulse: float,
        update_interval: timedelta,
    ) -> None:
        """Construct the entity."""
        super().__init__(
            monitor,
            monitor_device_id,
            DEVICE_TYPE_PULSE_COUNTER if not sensor.is_aux else DEVICE_TYPE_AUX,
            "count" if not sensor.is_aux else "aux_count",
            sensor,
            sensor.number,
            update_interval=update_interval,
        )
        self._sensor: greeneye.monitor.PulseCounter = sensor
        self._counted_quantity_per_pulse = counted_quantity_per_pulse
        self._attr_native_unit_of_measurement = counted_quantity
        self._attr_device_class = device_class

    @property
    def native_value(self) -> float | None:
        if self._sensor.pulses is None:
            return None

        return self._sensor.pulses * self._counted_quantity_per_pulse


class TemperatureSensor(MonitorSensor):
    """Entity showing temperature from one temperature sensor."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_name = None
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        sensor: greeneye.monitor.TemperatureSensor,
        unit: str,
    ) -> None:
        """Construct the entity."""
        super().__init__(
            monitor,
            monitor_device_id,
            DEVICE_TYPE_TEMPERATURE_SENSOR,
            "temp",
            sensor,
            sensor.number,
        )
        self._sensor: greeneye.monitor.TemperatureSensor = sensor
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self) -> float | None:
        """Return the current temperature being reported by this sensor."""
        return self._sensor.temperature


class VoltageSensor(MonitorSensor):
    """Entity showing voltage."""

    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, monitor: greeneye.monitor.Monitor, monitor_device_id: str) -> None:
        """Construct the entity."""
        super().__init__(
            monitor,
            monitor_device_id,
            DEVICE_TYPE_VOLTAGE_SENSOR,
            "volts",
            monitor.voltage_sensor,
            0,
        )
        self._sensor: greeneye.monitor.VoltageSensor = monitor.voltage_sensor

    @property
    def native_value(self) -> float | None:
        """Return the current voltage being reported by this sensor."""
        return self._sensor.voltage

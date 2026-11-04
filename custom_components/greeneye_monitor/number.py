"""Support for configuration entities that are numbers."""
from __future__ import annotations

import logging

import greeneye
from homeassistant.components.number import NumberEntity
from homeassistant.components.number import NumberMode
from homeassistant.const import EntityCategory
from homeassistant.const import UnitOfTime
from homeassistant.core import callback
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DEVICE_TYPE_CURRENT_TRANSFORMER
from .const import DOMAIN
from .const import GreeneyeConfigEntry
from .const import make_device_info
from .const import signal_new_monitor


_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: GreeneyeConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up configuration entities for Brultech energy monitors"""
    added_monitors: set[int] = set()

    @callback
    def on_new_monitor(monitor: greeneye.monitor.Monitor, device_id: str) -> None:
        if monitor.serial_number in added_monitors:
            return
        added_monitors.add(monitor.serial_number)

        entities: list[Entity] = []
        for channel in monitor.channels:
            if channel.ct_type is not None:
                entities.append(ChannelTypeEntity(monitor, device_id, channel))
            if channel.ct_range is not None:
                entities.append(ChannelRangeEntity(monitor, device_id, channel))

        if monitor.control is not None:
            entities.append(PacketIntervalEntity(monitor))

        async_add_entities(entities)
        _LOGGER.info(
            "Added configuration entities for monitor %d", monitor.serial_number
        )

    entry.async_on_unload(
        async_dispatcher_connect(hass, signal_new_monitor(entry.entry_id), on_new_monitor)
    )


class MonitorNumberEntity(NumberEntity):
    """Base class for monitor settings that are numbers."""

    _attr_entity_category = EntityCategory.CONFIG
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        source: greeneye.monitor.Channel | greeneye.monitor.Monitor,
    ) -> None:
        self._source = source
        self._written_value: float | None = None

    async def async_added_to_hass(self) -> None:
        """Connect to the channel or monitor."""
        self._source.add_listener(self._on_source_update)
        self.async_on_remove(
            lambda: self._source.remove_listener(self._on_source_update)
        )

    @callback
    def _on_source_update(self) -> None:
        # The source notifies on every packet, but these settings rarely change.
        if self.native_value == self._written_value:
            return
        self._written_value = self.native_value
        self.async_write_ha_state()


class ChannelTypeEntity(MonitorNumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_name = "CT type"
    _attr_native_step = 1.0
    _attr_native_min_value = 0
    _attr_native_max_value = 255

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        channel: greeneye.monitor.Channel,
    ) -> None:
        super().__init__(channel)
        self._channel = channel
        self._attr_unique_id = f"{monitor.serial_number}-ct_type-{channel.number + 1}"
        self._attr_device_info = make_device_info(
            monitor, monitor_device_id, DEVICE_TYPE_CURRENT_TRANSFORMER, channel.number
        )

    @property
    def native_value(self) -> float | None:
        return self._channel.ct_type

    async def async_set_native_value(self, value: float) -> None:
        await self._channel.set_ct_type(int(value))


class ChannelRangeEntity(MonitorNumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_name = "CT range"
    _attr_native_step = 1.0
    _attr_native_min_value = 0
    _attr_native_max_value = 15

    def __init__(
        self,
        monitor: greeneye.monitor.Monitor,
        monitor_device_id: str,
        channel: greeneye.monitor.Channel,
    ) -> None:
        super().__init__(channel)
        self._channel = channel
        self._attr_unique_id = f"{monitor.serial_number}-ct_range-{channel.number + 1}"
        self._attr_device_info = make_device_info(
            monitor, monitor_device_id, DEVICE_TYPE_CURRENT_TRANSFORMER, channel.number
        )

    @property
    def native_value(self) -> float | None:
        return self._channel.ct_range

    async def async_set_native_value(self, value: float) -> None:
        await self._channel.set_ct_range(int(value))


class PacketIntervalEntity(MonitorNumberEntity):
    _attr_mode = NumberMode.SLIDER
    _attr_name = "packet interval"
    _attr_native_step = 1.0
    _attr_native_min_value = 1
    _attr_native_max_value = 255
    _attr_native_unit_of_measurement = UnitOfTime.SECONDS

    def __init__(self, monitor: greeneye.monitor.Monitor) -> None:
        super().__init__(monitor)
        self._monitor = monitor
        self._attr_unique_id = f"{monitor.serial_number}-packet_send_interval"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, f"{monitor.serial_number}")}
        )

    @property
    def native_value(self) -> float | None:
        return self._monitor.packet_send_interval.total_seconds()

    async def async_set_native_value(self, value: float) -> None:
        await self._monitor.set_packet_send_interval(seconds=int(value))

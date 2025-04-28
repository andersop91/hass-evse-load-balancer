"""Main coordinator for load balacer."""

import logging
from datetime import datetime, timedelta
from math import floor
from time import time
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import ATTR_DEVICE_ID
from homeassistant.core import CALLBACK_TYPE, HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.event import async_track_time_interval

from . import config_flow as cf
from . import options_flow as of
from .balancers.optimised_load_balancer import (
    OptimisedLoadBalancer,
)
from .chargers.charger import Charger
from .const import (
    COORDINATOR_STATE_AWAITING_CHARGER,
    COORDINATOR_STATE_MONITORING_LOAD,
    DOMAIN,
    EVENT_ACTION_NEW_CHARGER_LIMITS,
    EVENT_ATTR_ACTION,
    EVENT_ATTR_NEW_LIMITS,
    EVSE_LOAD_BALANCER_COORDINATOR_EVENT,
)
from .meters.meter import Meter, Phase

if TYPE_CHECKING:
    from homeassistant.helpers.device_registry import DeviceEntry

_LOGGER = logging.getLogger(__name__)

# Number of seconds between each check cycle
EXECUTION_CYCLE_DELAY: int = 1

# Number of seconds between each charger update. This setting
# makes sure that the charger is not updated too frequently and
# allows a change of the charger's limit to actually take affect
MIN_CHARGER_UPDATE_DELAY: int = 30


class EVSELoadBalancerCoordinator:
    """Coordinator for the EVSE Load Balancer."""

    _last_check_timestamp: str = None

    _last_charger_target_update: tuple[dict[Phase, int], int] | None = None

    def __init__(
        self,
        hass: HomeAssistant,
        config_entry: ConfigEntry,
        meter: Meter,
        charger: Charger,
    ) -> None:
        """Initialize the coordinator."""
        self.hass: HomeAssistant = hass
        self.config_entry: ConfigEntry = config_entry

        self._unsub: list[CALLBACK_TYPE] = []
        self._sensors: list[SensorEntity] = []

        self._fuse_size = config_entry.data.get(cf.CONF_FUSE_SIZE, 0)

        self._meter: Meter = meter
        self._charger: Charger = charger

        device_registry = dr.async_get(self.hass)
        self._device: DeviceEntry = device_registry.async_get_device(
            identifiers={(DOMAIN, self.config_entry.entry_id)}
        )

    async def async_setup(self) -> None:
        """Set up the coordinator."""
        tracked_entities = self._meter.get_tracking_entities()
        _LOGGER.debug("Tracking entities: %s", tracked_entities)
        self._unsub.append(
            async_track_time_interval(
                self.hass,
                self._execute_update_cycle,
                timedelta(seconds=EXECUTION_CYCLE_DELAY),
            )
        )
        self._unsub.append(
            self.config_entry.add_update_listener(self._handle_options_update)
        )

        self._balancer_algo = OptimisedLoadBalancer(
            recovery_window=of.EvseLoadBalancerOptionsFlow.get_option_value(
                self.config_entry, of.OPTION_CHARGE_LIMIT_HYSTERESIS
            )
            * 60
        )

    async def async_unload(self) -> None:
        """Unload the coordinator."""
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    async def _handle_options_update(
        self, hass: HomeAssistant, entry: ConfigEntry
    ) -> None:
        await hass.config_entries.async_reload(entry.entry_id)

    def register_sensor(self, sensor: SensorEntity) -> None:
        """Register a sensor to be updated."""
        self._sensors.append(sensor)

    def unregister_sensor(self, sensor: SensorEntity) -> None:
        """Unregister a sensor."""
        self._sensors.remove(sensor)

    def get_available_current_for_phase(self, phase: Phase) -> int | None:
        """Get the available current for a given phase."""
        active_current = self._meter.get_active_phase_current(phase)
        return (
            min(self._fuse_size, floor(self.fuse_size - active_current))
            if active_current is not None
            else None
        )

    def _get_available_currents(self) -> dict[Phase, int] | None:
        """Check all phases and return the available current."""
        available_currents = {
            phase: self.get_available_current_for_phase(phase) for phase in Phase
        }
        if None in available_currents.values():
            _LOGGER.error(
                "One of the available currents is None: %s.", available_currents
            )
            return None
        return available_currents

    @property
    def fuse_size(self) -> float:
        """Get the fuse size."""
        return self.config_entry.data.get(cf.CONF_FUSE_SIZE, 0)

    @property
    def get_load_balancing_state(self) -> str:
        """Get the load balancing state."""
        if self._should_check_charger():
            return COORDINATOR_STATE_MONITORING_LOAD
        return COORDINATOR_STATE_AWAITING_CHARGER

    @property
    def get_last_check_timestamp(self) -> str:
        """Get the last check timestamp."""
        return self._last_check_timestamp

    @callback
    def _execute_update_cycle(self, now: datetime) -> None:
        """Execute the update cycle for the charger."""
        self._last_check_timestamp = datetime.now().astimezone()
        available_currents = self._get_available_currents()
        current_charger_setting = self._charger.get_current_limit()
        max_charger_current = self._charger.get_max_current_limit()

        if available_currents is None:
            _LOGGER.warning("Available current unknown. Cannot adjust limit.")
            return

        if current_charger_setting is None:
            _LOGGER.warning(
                "Current charger current limit is not available. Cannot adjust limit."
            )
            return

        if max_charger_current is None:
            _LOGGER.warning(
                "Max charger current is not available. Cannot adjust limit."
            )
            return

        # making data available to sensors
        self._async_update_sensors()

        # Run the actual charger update
        if self._should_check_charger():
            new_charger_settings = self._balancer_algo.compute_new_limits(
                current_limits=current_charger_setting,
                available_currents=available_currents,
                max_limits=max_charger_current,
                now=now.timestamp(),
            )
            # Update the charger with the new settings
            has_changed_values = any(
                new_charger_settings[phase] is not current_charger_setting[phase]
                for phase in new_charger_settings
            )
            if has_changed_values and self._may_update_charger_settings():
                self._update_charger_settings(new_charger_settings)

    def _async_update_sensors(self) -> None:
        for sensor in self._sensors:
            if sensor.enabled:
                sensor.async_write_ha_state()

    def _should_check_charger(self) -> bool:
        """Check if the charger should be checked for current limit changes."""
        return True  # self._charger.can_charge()

    def _may_update_charger_settings(self) -> bool:
        """Check if the charger settings haven't been updated too recently."""
        if self._last_charger_target_update is None:
            return True

        last_update_time = self._last_charger_target_update[1]
        if int(time()) - last_update_time > MIN_CHARGER_UPDATE_DELAY:
            return True

        _LOGGER.debug(
            "Charger settings was updated too recently. "
            "Last update: %s, current time: %s",
            last_update_time,
            int(time()),
        )
        return False

    def _update_charger_settings(self, new_limits: dict[Phase, int]) -> None:
        _LOGGER.debug("New charger settings: %s", new_limits)
        self._last_charger_target_update = (
            new_limits,
            int(time()),
        )
        self._emit_charger_event(EVENT_ACTION_NEW_CHARGER_LIMITS, new_limits)
        self.hass.async_create_task(self._charger.set_current_limit(new_limits))

    def _emit_charger_event(self, action: str, new_limits: dict[Phase, int]) -> None:
        """Emit an event to Home Assistant's device event log."""
        self.hass.bus.async_fire(
            EVSE_LOAD_BALANCER_COORDINATOR_EVENT,
            {
                ATTR_DEVICE_ID: self._device.id,
                EVENT_ATTR_ACTION: action,
                EVENT_ATTR_NEW_LIMITS: new_limits,
            },
        )
        _LOGGER.info(
            "Emitted charger event: action=%s, new_limits=%s", action, new_limits
        )

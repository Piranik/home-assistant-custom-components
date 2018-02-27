"""
Allows the creation of a sensor that filters state property.

For more details about this platform, please refer to the documentation at
https://home-assistant.io/components/sensor.filter/
"""
import logging
import statistics
from collections import deque, Counter

import voluptuous as vol

from homeassistant.util import slugify
from homeassistant.core import callback
from homeassistant.components.sensor import PLATFORM_SCHEMA
from homeassistant.const import (
    CONF_NAME, CONF_ENTITY_ID, ATTR_UNIT_OF_MEASUREMENT, ATTR_ENTITY_ID,
    ATTR_ICON, STATE_UNKNOWN, STATE_UNAVAILABLE)
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.event import async_track_state_change

_LOGGER = logging.getLogger(__name__)

FILTER_NAME_LOWPASS = 'lowpass'
FILTER_NAME_OUTLIER = 'outlier'
FILTER_NAME_THROTTLE = 'throttle'

CONF_FILTERS = 'filters'
CONF_FILTER_NAME = 'filter'
CONF_FILTER_WINDOW_SIZE = 'window_size'
CONF_FILTER_PRECISION = 'precision'
CONF_FILTER_RADIUS = 'radius'
CONF_FILTER_TIME_CONSTANT = 'time_constant'

DEFAULT_FILTER_RADIUS = 2.0
DEFAULT_FILTER_TIME_CONSTANT = 10

NAME_TEMPLATE = "{} filter"
ICON = 'mdi:chart-line-variant'

FILTER_SCHEMA = vol.Schema({
    vol.Optional(CONF_FILTER_WINDOW_SIZE): vol.Coerce(int),
    vol.Optional(CONF_FILTER_PRECISION): vol.Coerce(int),
})

FILTER_OUTLIER_SCHEMA = FILTER_SCHEMA.extend({
    vol.Required(CONF_FILTER_NAME): FILTER_NAME_OUTLIER,
    vol.Optional(CONF_FILTER_RADIUS,
                 default=DEFAULT_FILTER_RADIUS): vol.Coerce(float),
})

FILTER_LOWPASS_SCHEMA = FILTER_SCHEMA.extend({
    vol.Required(CONF_FILTER_NAME): FILTER_NAME_LOWPASS,
    vol.Optional(CONF_FILTER_TIME_CONSTANT,
                 default=DEFAULT_FILTER_TIME_CONSTANT): vol.Coerce(int),
})

FILTER_THROTTLE_SCHEMA = FILTER_SCHEMA.extend({
    vol.Required(CONF_FILTER_NAME): FILTER_NAME_THROTTLE,
})

PLATFORM_SCHEMA = PLATFORM_SCHEMA.extend({
    vol.Required(CONF_ENTITY_ID): cv.entity_id,
    vol.Optional(CONF_NAME): cv.string,
    vol.Required(CONF_FILTERS): vol.All(cv.ensure_list,
                                        [vol.Any(FILTER_OUTLIER_SCHEMA,
                                                 FILTER_LOWPASS_SCHEMA,
                                                 FILTER_THROTTLE_SCHEMA)])
})


async def async_setup_platform(hass, config, async_add_devices,
                               discovery_info=None):
    """Set up the template sensors."""
    name = config.get(CONF_NAME)
    entity_id = config.get(CONF_ENTITY_ID)
    filters = []

    for _filter in config[CONF_FILTERS]:
        window_size = _filter.get(CONF_FILTER_WINDOW_SIZE)
        precision = _filter.get(CONF_FILTER_PRECISION)

        if _filter[CONF_FILTER_NAME] == FILTER_NAME_OUTLIER:
            radius = _filter.get(CONF_FILTER_RADIUS)
            filters.append(OutlierFilter(window_size=window_size,
                                         precision=precision,
                                         entity=entity_id,
                                         radius=radius))
        elif _filter[CONF_FILTER_NAME] == FILTER_NAME_LOWPASS:
            time_constant = _filter.get(CONF_FILTER_TIME_CONSTANT)
            filters.append(LowPassFilter(window_size=window_size,
                                         precision=precision,
                                         entity=entity_id,
                                         time_constant=time_constant))
        elif _filter[CONF_FILTER_NAME] == FILTER_NAME_THROTTLE:
            filters.append(ThrottleFilter(window_size=window_size,
                                          precision=precision,
                                          entity=entity_id))

    async_add_devices([SensorFilter(name, entity_id, filters)])


class SensorFilter(Entity):
    """Representation of a Filter Sensor."""

    def __init__(self, name, entity_id, filters):
        """Initialize the sensor."""
        self._name = name
        self._entity = entity_id
        self._unit_of_measurement = None
        self._state = None
        self._filters = filters
        self._icon = ICON

    async def async_added_to_hass(self):
        """Register callbacks."""
        @callback
        def filter_sensor_state_listener(entity, old_state, new_state):
            """Handle device state changes."""
            self._unit_of_measurement = new_state.attributes.get(
                ATTR_UNIT_OF_MEASUREMENT)
            self._icon = new_state.attributes.get(ATTR_ICON, self._icon)

            self._state = new_state.state

            if self._state in [STATE_UNKNOWN, STATE_UNAVAILABLE]:
                return

            for filt in self._filters:
                try:
                    filtered_state = filt.filter_state(self._state)
                    _LOGGER.debug("%s(%s, %s) -> %s", filt.name,
                                  self._entity,
                                  self._state,
                                  "skip" if filt.skip else filtered_state)
                    if filt.skip:
                        return
                except ValueError:
                    _LOGGER.warning("Could not convert state: %s to number",
                                    self._state)
                finally:
                    self._state = filtered_state

            self.async_schedule_update_ha_state()

        async_track_state_change(
            self.hass, self._entity, filter_sensor_state_listener)

    @property
    def name(self):
        """Return the name of the sensor."""
        return self._name

    @property
    def state(self):
        """Return the state of the sensor."""
        return self._state

    @property
    def icon(self):
        """Return the icon to use in the frontend, if any."""
        return self._icon

    @property
    def unit_of_measurement(self):
        """Return the unit_of_measurement of the device."""
        return self._unit_of_measurement

    @property
    def should_poll(self):
        """No polling needed."""
        return False

    @property
    def device_state_attributes(self):
        """Return the state attributes of the sensor."""
        state_attr = {
            ATTR_ENTITY_ID: self._entity
        }
        for filt in self._filters:
            for filt_stat_key, filt_stat_value in filt.stats.items():
                filt_stat = slugify("{} {}".format(filt.name, filt_stat_key))
                _LOGGER.debug("stats(%s): %s: %s", self._entity,
                              filt_stat, filt_stat_value)
                state_attr.update({
                    filt_stat: filt_stat_value
                })

        return state_attr


class Filter(object):
    """Filter skeleton.

    Args:
        window_size (int): size of the sliding window that holds previous
                                values
        precision (int): round filtered value to precision value
        entity (string): used for debugging only
    """

    def __init__(self, name, window_size=1, precision=None, entity=None):
        """Initialize common attributes."""
        self.states = deque(maxlen=window_size)
        self.precision = precision
        self._stats = {}
        self._name = name
        self._entity = entity
        self._skip = False

    @property
    def name(self):
        """Return filter name."""
        return self._name

    @property
    def skip(self):
        """Return wether the current filter_state should be skipped."""
        return self._skip

    @property
    def stats(self):
        """Return statistics of the filter."""
        return self._stats

    def _filter_state(self, new_state):
        """Implement filter."""
        raise NotImplementedError()

    def filter_state(self, new_state):
        """Implement a common interface for filters."""
        filtered = self._filter_state(new_state)
        if self.precision is not None:
            filtered = round(filtered, self.precision)
        self.states.append(filtered)
        return filtered


class OutlierFilter(Filter):
    """BASIC outlier filter.

    Determines if new state is in a band around the median.

    Args:
        radius (float): band radius
    """

    def __init__(self, window_size, precision, entity, radius):
        """Initialize Filter."""
        super().__init__(FILTER_NAME_OUTLIER, window_size, precision, entity)
        self._radius = radius
        self._stats_internal = Counter()

    def _filter_state(self, new_state):
        """Implement the outlier filter."""
        self._stats_internal['total_filtered'] += 1
        new_state = float(new_state)

        self._stats['erasures'] = "{0:.2f}%".format(
            100 * self._stats_internal['erasures']
            / self._stats_internal['total_filtered']
        )

        if (len(self.states) > 1 and
                abs(new_state - statistics.median(self.states))
                > self._radius):

            self._stats_internal['erasures'] += 1

            _LOGGER.debug("Outlier in %s: %s", self._entity, new_state)
            return self.states[-1]
        return new_state


class LowPassFilter(Filter):
    """BASIC Low Pass Filter.

    Args:
        time_constant (int): time constant.
    """

    def __init__(self, window_size, precision, entity, time_constant):
        """Initialize Filter."""
        super().__init__(FILTER_NAME_LOWPASS, window_size, precision, entity)
        self._time_constant = time_constant

    def _filter_state(self, new_state):
        """Implement the low pass filter."""
        new_state = float(new_state)

        if not self.states:
            return new_state

        new_weight = 1.0 / self._time_constant
        prev_weight = 1.0 - new_weight
        filtered = prev_weight * self.states[-1] + new_weight * new_state

        return filtered


class ThrottleFilter(Filter):
    """Throttle Filter.

    One sample per window.
    """

    def __init__(self, window_size, precision, entity):
        """Initialize Filter."""
        super().__init__(FILTER_NAME_THROTTLE, window_size, precision, entity)

    def _filter_state(self, new_state):
        """Implement the throttle filter."""
        if not self.states or len(self.states) == self.states.maxlen:
            self.states.clear()
            self._skip = False
        else:
            self._skip = True

        return new_state
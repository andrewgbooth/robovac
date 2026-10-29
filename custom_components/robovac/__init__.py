# Copyright 2022 Brendan McCluskey
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#    https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.


"""The Eufy Robovac integration."""
from __future__ import annotations
import logging
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any, Dict, Optional

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_IP_ADDRESS,
    CONF_MAC,
    EVENT_HOMEASSISTANT_STOP,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.event import async_track_time_interval
from .const import CONF_AUTODISCOVERY, CONF_VACS, DOMAIN

from .mac_discovery import async_find_ips_by_mac
from .tuyalocaldiscovery import TuyaLocalDiscovery

PLATFORMS = [Platform.VACUUM, Platform.SENSOR, Platform.SELECT, Platform.SWITCH]
MAC_LOOKUP_INTERVAL = timedelta(minutes=10)
_LOGGER = logging.getLogger(__name__)


async def async_setup(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the Eufy Robovac component.

    This function initializes the component and sets up the discovery service.

    Args:
        hass: The Home Assistant instance.
        entry: The config entry.

    Returns:
        True if setup was successful.
    """
    hass.data.setdefault(DOMAIN, {CONF_VACS: {}})

    async def update_device(device: Dict[str, Any]) -> None:
        entry = async_get_config_entry_for_device(hass, device["gwId"])

        if entry is None:
            return

        if not entry.state.recoverable:
            return

        hass_data = entry.data.copy()
        if (
            device["gwId"] in hass_data[CONF_VACS]
            and device.get("ip") is not None
            and hass_data[CONF_VACS][device["gwId"]].get("autodiscovery", True)
        ):
            if hass_data[CONF_VACS][device["gwId"]][CONF_IP_ADDRESS] != device["ip"]:
                hass_data[CONF_VACS][device["gwId"]][CONF_IP_ADDRESS] = device["ip"]
                hass.config_entries.async_update_entry(entry, data=hass_data)
                await hass.config_entries.async_reload(entry.entry_id)
                _LOGGER.debug(
                    "Updated ip address of {} to {}".format(
                        device["gwId"], device["ip"]
                    )
                )

    tuyalocaldiscovery = TuyaLocalDiscovery(update_device)
    try:
        await tuyalocaldiscovery.start()
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, tuyalocaldiscovery.close)
    except Exception:
        _LOGGER.exception("failed to set up discovery")

    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Eufy Robovac from a config entry."""
    # Fill in missing IPs before the update listener exists, so the entry
    # update does not trigger a reload of an entry that is still starting.
    await _async_fill_missing_ips(hass, entry)

    entry.async_on_unload(entry.add_update_listener(update_listener))

    async def _retry_missing_ips(_now: datetime) -> None:
        await _async_fill_missing_ips(hass, entry)

    entry.async_on_unload(
        async_track_time_interval(hass, _retry_missing_ips, MAC_LOOKUP_INTERVAL)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok: bool = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    return unload_ok


async def _async_fill_missing_ips(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Locate vacuums with no IP address by their MAC address on the LAN."""
    vacuums = entry.data[CONF_VACS]
    missing = {
        vacuum_id: vacuum[CONF_MAC]
        for vacuum_id, vacuum in vacuums.items()
        if not vacuum.get(CONF_IP_ADDRESS)
        and vacuum.get(CONF_MAC)
        and vacuum.get(CONF_AUTODISCOVERY, True)
    }
    if not missing:
        return

    try:
        found = await async_find_ips_by_mac(hass, missing.values())
    except Exception:  # pylint: disable=broad-except
        _LOGGER.exception("MAC address lookup failed")
        return

    updated = deepcopy(dict(entry.data))
    changed = False
    for vacuum_id, mac in missing.items():
        if mac in found:
            updated[CONF_VACS][vacuum_id][CONF_IP_ADDRESS] = found[mac]
            changed = True
            _LOGGER.info(
                "Found %s at %s by MAC address %s", vacuum_id, found[mac], mac
            )
        else:
            _LOGGER.debug("Could not find %s (MAC %s) on the local network", vacuum_id, mac)
    if changed:
        hass.config_entries.async_update_entry(entry, data=updated)


async def update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Handle options update."""
    await hass.config_entries.async_reload(entry.entry_id)


def async_get_config_entry_for_device(
    hass: HomeAssistant, device_id: str
) -> Optional[ConfigEntry]:
    """Find the config entry for a specific device ID.

    Args:
        hass: The Home Assistant instance.
        device_id: The device ID to find.

    Returns:
        The config entry if found, None otherwise.
    """
    current_entries = hass.config_entries.async_entries(DOMAIN)
    for entry in current_entries:
        if device_id in entry.data[CONF_VACS]:
            return entry
    return None

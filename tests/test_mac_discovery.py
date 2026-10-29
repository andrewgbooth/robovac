"""Tests for locating vacuums on the LAN by MAC address."""

import ipaddress
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from homeassistant.const import CONF_ACCESS_TOKEN, CONF_ID, CONF_IP_ADDRESS, CONF_MAC
from homeassistant.core import HomeAssistant

from custom_components.robovac import _async_fill_missing_ips
from custom_components.robovac import mac_discovery
from custom_components.robovac.const import CONF_AUTODISCOVERY, CONF_VACS
from custom_components.robovac.mac_discovery import (
    async_find_ips_by_mac,
    normalize_mac,
    parse_arp_table,
    probe_hosts,
)

ARP_TABLE = """\
IP address       HW type     Flags       HW address            Mask     Device
192.168.1.20     0x1         0x2         AA:BB:CC:DD:EE:FF     *        eth0
192.168.1.21     0x1         0x0         11:22:33:44:55:66     *        eth0
192.168.1.22     0x1         0x2         00:00:00:00:00:00     *        eth0
"""


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("AA:BB:CC:DD:EE:FF", "aa:bb:cc:dd:ee:ff"),
        ("aa-bb-cc-dd-ee-ff", "aa:bb:cc:dd:ee:ff"),
        ("aabbccddeeff", "aa:bb:cc:dd:ee:ff"),
        ("aa:bb:cc", None),
        ("", None),
        (None, None),
    ],
)
def test_normalize_mac(raw: str | None, expected: str | None) -> None:
    assert normalize_mac(raw) == expected


def test_parse_arp_table_skips_incomplete_and_empty_entries() -> None:
    assert parse_arp_table(ARP_TABLE) == {"aa:bb:cc:dd:ee:ff": "192.168.1.20"}


def test_probe_hosts_skips_own_address_and_loopback() -> None:
    hosts = probe_hosts(
        [
            ipaddress.IPv4Interface("192.168.1.5/30"),
            ipaddress.IPv4Interface("127.0.0.1/8"),
            ipaddress.IPv4Interface("169.254.1.1/16"),
        ]
    )
    assert hosts == ["192.168.1.6"]


def test_probe_hosts_limits_large_subnets() -> None:
    hosts = probe_hosts([ipaddress.IPv4Interface("10.1.2.3/16")])
    assert len(hosts) == 1022 - 1  # a /22 minus our own address
    assert "10.1.2.3" not in hosts


async def test_find_ips_uses_arp_table_without_probing(hass: HomeAssistant) -> None:
    with (
        patch.object(
            mac_discovery, "_read_arp_table", return_value={"aa:bb:cc:dd:ee:ff": "192.168.1.20"}
        ),
        patch.object(mac_discovery, "_probe", new=AsyncMock()) as probe,
    ):
        found = await async_find_ips_by_mac(hass, ["AA:BB:CC:DD:EE:FF"])

    assert found == {"AA:BB:CC:DD:EE:FF": "192.168.1.20"}
    probe.assert_not_called()


async def test_find_ips_probes_subnet_then_rereads_arp(hass: HomeAssistant) -> None:
    arp_reads = [{}, {"aa:bb:cc:dd:ee:ff": "192.168.1.6"}]
    with (
        patch.object(mac_discovery, "_read_arp_table", side_effect=arp_reads),
        patch.object(
            mac_discovery,
            "_local_interfaces",
            AsyncMock(return_value=[ipaddress.IPv4Interface("192.168.1.5/30")]),
        ),
        patch.object(mac_discovery, "_probe", new=AsyncMock()) as probe,
    ):
        found = await async_find_ips_by_mac(hass, ["AA:BB:CC:DD:EE:FF", "not-a-mac"])

    assert found == {"AA:BB:CC:DD:EE:FF": "192.168.1.6"}
    assert [call.args[0] for call in probe.call_args_list] == ["192.168.1.6"]


async def test_find_ips_returns_nothing_for_invalid_macs(hass: HomeAssistant) -> None:
    assert await async_find_ips_by_mac(hass, ["", "bad"]) == {}


def _entry(vacuums: dict[str, dict[str, Any]]) -> MagicMock:
    entry = MagicMock()
    entry.data = {CONF_VACS: vacuums}
    return entry


def _vacuum(**overrides: Any) -> dict[str, Any]:
    vacuum = {
        CONF_ID: "dev1",
        CONF_MAC: "AA:BB:CC:DD:EE:FF",
        CONF_IP_ADDRESS: "",
        CONF_AUTODISCOVERY: True,
        CONF_ACCESS_TOKEN: "key",
    }
    vacuum.update(overrides)
    return vacuum


async def test_fill_missing_ips_updates_entry(hass: HomeAssistant) -> None:
    entry = _entry({"dev1": _vacuum()})
    hass.config_entries.async_update_entry = MagicMock()  # type: ignore[method-assign]

    with patch(
        "custom_components.robovac.async_find_ips_by_mac",
        AsyncMock(return_value={"AA:BB:CC:DD:EE:FF": "192.168.1.20"}),
    ):
        await _async_fill_missing_ips(hass, entry)

    data = hass.config_entries.async_update_entry.call_args.kwargs["data"]
    assert data[CONF_VACS]["dev1"][CONF_IP_ADDRESS] == "192.168.1.20"
    # The original entry data must not be mutated in place.
    assert entry.data[CONF_VACS]["dev1"][CONF_IP_ADDRESS] == ""


@pytest.mark.parametrize(
    "vacuum",
    [
        _vacuum(**{CONF_IP_ADDRESS: "192.168.1.9"}),
        _vacuum(**{CONF_MAC: ""}),
        _vacuum(**{CONF_AUTODISCOVERY: False}),
    ],
)
async def test_fill_missing_ips_skips_ineligible_vacuums(
    hass: HomeAssistant, vacuum: dict[str, Any]
) -> None:
    hass.config_entries.async_update_entry = MagicMock()  # type: ignore[method-assign]
    with patch(
        "custom_components.robovac.async_find_ips_by_mac", AsyncMock()
    ) as finder:
        await _async_fill_missing_ips(hass, _entry({"dev1": vacuum}))

    finder.assert_not_called()
    hass.config_entries.async_update_entry.assert_not_called()


async def test_fill_missing_ips_leaves_entry_alone_when_not_found(
    hass: HomeAssistant,
) -> None:
    hass.config_entries.async_update_entry = MagicMock()  # type: ignore[method-assign]
    with patch(
        "custom_components.robovac.async_find_ips_by_mac", AsyncMock(return_value={})
    ):
        await _async_fill_missing_ips(hass, _entry({"dev1": _vacuum()}))

    hass.config_entries.async_update_entry.assert_not_called()


async def test_fill_missing_ips_survives_lookup_errors(hass: HomeAssistant) -> None:
    hass.config_entries.async_update_entry = MagicMock()  # type: ignore[method-assign]
    with patch(
        "custom_components.robovac.async_find_ips_by_mac",
        AsyncMock(side_effect=RuntimeError("boom")),
    ):
        await _async_fill_missing_ips(hass, _entry({"dev1": _vacuum()}))

    hass.config_entries.async_update_entry.assert_not_called()

"""Find vacuums on the local network by MAC address.

Tuya devices normally announce themselves with a UDP broadcast, which does
not always reach Home Assistant. As a fallback the Wi-Fi MAC address reported
by the Eufy cloud is looked up in the host's ARP table, after nudging the ARP
table with a TCP connection attempt to each host on the local subnets.
Only the local network is touched; the vacuum needs no extra internet access.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
from typing import Iterable

from homeassistant.components import network
from homeassistant.core import HomeAssistant

_LOGGER = logging.getLogger(__name__)

ARP_TABLE_PATH = "/proc/net/arp"
TUYA_LOCAL_PORT = 6668
PROBE_TIMEOUT = 0.5
PROBE_CONCURRENCY = 128
MAX_PROBE_HOSTS = 1022

_MAC_RE = re.compile(r"^[0-9a-f]{12}$")


def normalize_mac(mac: str | None) -> str | None:
    """Return a MAC as lowercase colon-separated hex, or None if invalid."""
    if not mac:
        return None
    digits = re.sub(r"[^0-9a-fA-F]", "", mac).lower()
    if not _MAC_RE.match(digits):
        return None
    return ":".join(re.findall("..", digits))


def parse_arp_table(text: str) -> dict[str, str]:
    """Parse the contents of /proc/net/arp into a MAC -> IP mapping."""
    entries: dict[str, str] = {}
    for line in text.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4:
            continue
        ip, _hw_type, flags, mac = fields[:4]
        # Flag 0x0 marks an incomplete entry.
        if flags == "0x0":
            continue
        normalized = normalize_mac(mac)
        if normalized and normalized != "00:00:00:00:00:00":
            entries[normalized] = ip
    return entries


def _read_arp_table() -> dict[str, str]:
    try:
        with open(ARP_TABLE_PATH, encoding="utf-8") as arp_file:
            return parse_arp_table(arp_file.read())
    except OSError as err:
        _LOGGER.debug("Cannot read %s: %s", ARP_TABLE_PATH, err)
        return {}


def probe_hosts(
    interfaces: Iterable[ipaddress.IPv4Interface],
) -> list[str]:
    """Return the hosts worth probing for the given local interfaces."""
    hosts: list[str] = []
    for interface in interfaces:
        if interface.ip.is_loopback or interface.ip.is_link_local:
            continue
        subnet: ipaddress.IPv4Network = interface.network
        if subnet.num_addresses - 2 > MAX_PROBE_HOSTS:
            # Very large subnet: only scan the /22 around our own address.
            subnet = ipaddress.IPv4Network(f"{interface.ip}/22", strict=False)
        hosts.extend(str(host) for host in subnet.hosts() if host != interface.ip)
    return hosts


async def _local_interfaces(hass: HomeAssistant) -> list[ipaddress.IPv4Interface]:
    interfaces: list[ipaddress.IPv4Interface] = []
    for adapter in await network.async_get_adapters(hass):
        if not adapter["enabled"]:
            continue
        for ipv4 in adapter["ipv4"]:
            try:
                interfaces.append(
                    ipaddress.IPv4Interface(
                        f"{ipv4['address']}/{ipv4['network_prefix']}"
                    )
                )
            except ValueError:
                continue
    return interfaces


async def _probe(host: str, semaphore: asyncio.Semaphore) -> None:
    """Try to connect to a host so the kernel resolves its MAC via ARP."""
    async with semaphore:
        try:
            _reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, TUYA_LOCAL_PORT), PROBE_TIMEOUT
            )
        except (OSError, asyncio.TimeoutError):
            return
        writer.close()
        try:
            await writer.wait_closed()
        except OSError:
            pass


async def async_find_ips_by_mac(
    hass: HomeAssistant, macs: Iterable[str]
) -> dict[str, str]:
    """Look up local IP addresses for the given MACs.

    Returns a mapping of the MACs as given (not normalized) to IPs; MACs that
    cannot be found are left out.
    """
    wanted = {normalize_mac(mac): mac for mac in macs}
    wanted.pop(None, None)
    if not wanted:
        return {}

    def _matches(arp: dict[str, str]) -> dict[str, str]:
        return {original: arp[mac] for mac, original in wanted.items() if mac in arp}

    found = _matches(await hass.async_add_executor_job(_read_arp_table))
    if len(found) == len(wanted):
        return found

    hosts = probe_hosts(await _local_interfaces(hass))
    _LOGGER.debug("Probing %d local hosts to locate MACs %s", len(hosts), list(wanted))
    semaphore = asyncio.Semaphore(PROBE_CONCURRENCY)
    await asyncio.gather(*(_probe(host, semaphore) for host in hosts))

    return _matches(await hass.async_add_executor_job(_read_arp_table))

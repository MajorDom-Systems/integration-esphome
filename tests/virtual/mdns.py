"""Zeroconf without a network: the SDK's real `ZeroconfDiscoveryService` runs over a stubbed zeroconf.

Same approach as the homekit integration's tests: the browser and service-info classes are replaced, so
announcements are delivered on demand and nothing is ever sent to (or heard from) the LAN.
"""

import asyncio
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

MDNS_TYPE = "_esphomelib._tcp.local."
NOISE_PROTOCOL = "Noise_NNpsk0_25519_ChaChaPoly_SHA256"
SDK_MODULE = "majordom_integration_sdk.discovery.zeroconf_discovery"


@dataclass
class Advert:
    """What a real ESPHome node announces (TXT keys as published by ESPHome's mdns component)."""

    name: str = "test_node"
    mac: str | None = "983569abf679"
    server: str | None = "localhost."  # a real node announces `<name>.local.`
    addresses: list[str] = field(default_factory=lambda: ["127.0.0.1"])
    port: int = 6053
    encrypted: bool = False
    friendly_name: str | None = None

    @property
    def fqdn(self) -> str:
        return f"{self.name}.{MDNS_TYPE}"

    @property
    def properties(self) -> dict[bytes, bytes | None]:
        properties: dict[bytes, bytes | None] = {b"version": b"2026.3.3", b"board": b"host"}
        if self.mac:
            properties[b"mac"] = self.mac.encode()
        if self.friendly_name:
            properties[b"friendly_name"] = self.friendly_name.encode()
        properties[b"api_encryption" if self.encrypted else b"api_encryption_supported"] = NOISE_PROTOCOL.encode()
        return properties


class _BrowserStub:
    def __init__(self, zeroconf: Any, types: list[str], listener: Any) -> None:
        self.types = types
        self.listener = listener
        self.cancelled = False

    async def async_cancel(self) -> None:
        self.cancelled = True


class FakeMDNS:
    def __init__(self) -> None:
        self.adverts: dict[str, Advert] = {}
        self.browsers: list[_BrowserStub] = []
        self._zeroconf = MagicMock(name="zeroconf")

    def _browser(self, zeroconf: Any, types: list[str], listener: Any) -> _BrowserStub:
        browser = _BrowserStub(zeroconf, types, listener)
        self.browsers.append(browser)
        return browser

    def _service_info(self, type_: str, name: str) -> Any:
        adverts = self.adverts

        class ServiceInfoStub:
            def __init__(self, type_: str, name: str) -> None:
                self.type, self.name = type_, name
                self.weight = self.priority = 0
                self.host_ttl, self.other_ttl, self.interface_index, self.text = 120, 4500, None, b""

            def load_from_cache(self, zeroconf: Any) -> bool:
                advert = adverts.get(self.name)
                if advert is None:
                    return False
                self.port, self.server = advert.port, advert.server
                self.properties = advert.properties
                self.decoded_properties = {k.decode(): v.decode() if v else None for k, v in advert.properties.items()}
                self._addresses = advert.addresses
                return True

            async def async_request(self, zeroconf: Any, timeout: float) -> bool:
                return self.load_from_cache(zeroconf)

            @property
            def addresses(self) -> list[bytes]:
                return [socket.inet_pton(socket.AF_INET6 if ":" in a else socket.AF_INET, a) for a in self._addresses]

            def parsed_addresses(self) -> list[str]:
                return list(self._addresses)

        return ServiceInfoStub(type_, name)

    async def announce(self, advert: Advert) -> None:
        """The node appears, or announces itself again with changed records."""
        known = advert.fqdn in self.adverts
        self.adverts[advert.fqdn] = advert
        for browser in self._listening():
            if known:
                browser.listener.update_service(self._zeroconf, MDNS_TYPE, advert.fqdn)
            else:
                browser.listener.add_service(self._zeroconf, MDNS_TYPE, advert.fqdn)
        await settle()

    async def goodbye(self, advert: Advert) -> None:
        self.adverts.pop(advert.fqdn, None)
        for browser in self._listening():
            browser.listener.remove_service(self._zeroconf, MDNS_TYPE, advert.fqdn)
        await settle()

    def _listening(self) -> list[_BrowserStub]:
        return [b for b in self.browsers if not b.cancelled and MDNS_TYPE in b.types]


async def settle() -> None:
    """Let the SDK resolve, deliver and the controller handle an announcement: no I/O is involved."""
    for _ in range(20):
        await asyncio.sleep(0)


@contextmanager
def fake_mdns() -> Iterator[FakeMDNS]:
    fake = FakeMDNS()
    async_zeroconf = MagicMock(name="AsyncZeroconf")
    async_zeroconf.return_value.zeroconf = fake._zeroconf
    async_zeroconf.return_value.async_close = AsyncMock()
    with (
        patch(f"{SDK_MODULE}.AsyncServiceBrowser", fake._browser),
        patch(f"{SDK_MODULE}.AsyncZeroconf", async_zeroconf),
        patch(f"{SDK_MODULE}.AsyncServiceInfo", fake._service_info),
        patch(f"{SDK_MODULE}._RESOLVE_DELAY", 0),
    ):
        yield fake

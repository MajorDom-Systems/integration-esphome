"""ESPHome nodes with ``bluetooth_proxy`` as Bluetooth sources: the hub hears and reaches BLE devices
through them as if through its own adapter.

A node serves one API connection at a time (the newest subscriber wins), so it can't serve this hub
and another one together.
"""

import contextlib
from collections.abc import Callable

from aioesphomeapi import APIClient, APIConnectionError, BluetoothLERawAdvertisementsResponse, BluetoothProxyFeature
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bluetooth_data_tools import int_to_bluetooth_address, parse_advertisement_data_bytes
from majordom_integration_sdk.discovery import GattCharacteristic, GattService
from majordom_integration_sdk.discovery.ble_discovery import OnAdvertisement

_PROPERTIES = {0x02: "read", 0x04: "write-without-response", 0x08: "write", 0x10: "notify", 0x20: "indicate"}
_CCCD = "00002902-0000-1000-8000-00805f9b34fb"
_CONNECTS = BluetoothProxyFeature.ACTIVE_CONNECTIONS | BluetoothProxyFeature.REMOTE_CACHING
_REMEMBERED = 1024  # devices kept merged; phones rotate their address, so the oldest are dropped


class ESPhomeBLESource:
    """An ESPHome node lends its Bluetooth radio to the hub: what it hears and the connections it makes become one
    more source of the hub's `BLEService`."""

    def __init__(self, client: APIClient, name: str, features: BluetoothProxyFeature):
        self.name = f"esphome:{name}"
        self._client, self._features = client, features
        self._unsubscribe: Callable[[], None] | None = None
        self._heard: dict[str, tuple[AdvertisementData, int]] = {}  # address -> merged advertisement, address type

    async def start(self, on_advertisement: OnAdvertisement):
        def on_advertisements(response: BluetoothLERawAdvertisementsResponse):
            advertisements = response.advertisements
            for i in range(len(advertisements)):  # iterating the protobuf directly costs a debug log per batch
                raw = advertisements[i]
                address = int_to_bluetooth_address(raw.address)
                device, adv = self._merge(address, raw.address_type, raw.rssi, raw.data)
                on_advertisement(device, adv)

        self._unsubscribe = self._client.subscribe_bluetooth_le_raw_advertisements(on_advertisements)

    def _merge(self, address: str, address_type: int, rssi: int, data: bytes) -> tuple[BLEDevice, AdvertisementData]:
        # packets and scan responses arrive apart; merged like BlueZ does, so they don't read as changes
        name, uuids, service_data, manufacturer_data, tx_power = parse_advertisement_data_bytes(data)
        if previous := self._heard.pop(address, None):  # re-added last: the dict stays oldest first
            before = previous[0]
            name = name or before.local_name
            uuids = sorted({*before.service_uuids, *uuids})
            service_data = before.service_data | service_data
            manufacturer_data = before.manufacturer_data | manufacturer_data
        adv = AdvertisementData(name, dict(manufacturer_data), dict(service_data), list(uuids), tx_power, rssi, ())
        self._heard[address] = adv, address_type
        if len(self._heard) > _REMEMBERED:
            del self._heard[next(iter(self._heard))]
        return BLEDevice(address, name, {"source": self.name}), adv

    async def stop(self):
        if unsubscribe := self._unsubscribe:
            self._unsubscribe = None
            with contextlib.suppress(APIConnectionError):
                unsubscribe()

    async def connect(self, address: str, on_disconnect: Callable[[], None]) -> "ESPhomeBLEConnection":
        if self._features & _CONNECTS != _CONNECTS:
            raise ConnectionError(f"{self.name} only listens")
        handle = int(address.replace(":", ""), 16)
        states: list[tuple[bool, int, int]] = []

        def on_state(connected: bool, mtu: int, error: int):
            states.append((connected, mtu, error))
            if len(states) > 1 and not connected:
                on_disconnect()

        unsubscribe = await self._client.bluetooth_device_connect(
            handle, on_state, feature_flags=self._features, address_type=self._heard.get(address, (None, 0))[1]
        )  # returns once the proxy answered: connected or not
        connected, mtu, error = states[0]
        if not connected:
            unsubscribe()
            raise ConnectionError(f"{self.name} can't connect to {address} (error {error})")
        return ESPhomeBLEConnection(self._client, handle, mtu, unsubscribe)


class ESPhomeBLEConnection:
    def __init__(self, client: APIClient, address: int, mtu: int, unsubscribe: Callable[[], None]):
        self._client, self._address, self.mtu, self._unsubscribe = client, address, mtu or 23, unsubscribe
        self._services = None
        self._notifying: dict[int, Callable] = {}  # handle -> stop

    async def services(self) -> list[GattService]:
        if self._services is None:  # the proxy forgets them once sent
            self._services = (await self._client.bluetooth_gatt_get_services(self._address)).services
        return [
            GattService(s.uuid, s.handle, tuple(
                GattCharacteristic(c.uuid, c.handle, tuple(n for bit, n in _PROPERTIES.items() if c.properties & bit))
                for c in s.characteristics
            ))
            for s in self._services
        ]  # fmt: skip

    async def read(self, handle: int) -> bytes:
        return bytes(await self._client.bluetooth_gatt_read(self._address, handle))

    async def write(self, handle: int, data: bytes, response: bool = True):
        await self._client.bluetooth_gatt_write(self._address, handle, data, response)

    async def start_notify(self, handle: int, callback: Callable[[bytes], None]):
        await self.services()
        characteristic = next(c for s in self._services for c in s.characteristics if c.handle == handle)
        stop, _ = await self._client.bluetooth_gatt_start_notify(self._address, handle, lambda _, d: callback(bytes(d)))
        self._notifying[handle] = stop
        # the proxy leaves the client characteristic configuration to the client
        if cccd := next((d.handle for d in characteristic.descriptors if d.uuid == _CCCD), None):
            indicate = characteristic.properties & 0x20 and not characteristic.properties & 0x10
            await self._client.bluetooth_gatt_write_descriptor(
                self._address, cccd, b"\x02\x00" if indicate else b"\x01\x00"
            )

    async def stop_notify(self, handle: int):
        if stop := self._notifying.pop(handle, None):
            await stop()

    async def disconnect(self):
        self._unsubscribe()
        await self._client.bluetooth_device_disconnect(self._address)


async def ble_source_of(client: APIClient) -> ESPhomeBLESource | None:
    """The node's source, if it lends Bluetooth this hub can use."""
    info = await client.device_info()
    features = BluetoothProxyFeature(info.bluetooth_proxy_feature_flags_compat(client.api_version))
    if not features & BluetoothProxyFeature.RAW_ADVERTISEMENTS:  # firmware before 2023.? or no proxy
        return None
    return ESPhomeBLESource(client, info.bluetooth_mac_address or info.mac_address, features)

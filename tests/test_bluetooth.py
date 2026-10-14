"""ESPHome nodes lending their Bluetooth as sources of the hub's BLEService."""

import asyncio
from uuid import UUID

import pytest
from aioesphomeapi import BluetoothLERawAdvertisementsResponse, BluetoothProxyFeature, DeviceInfo
from aioesphomeapi.model import (
    APIVersion,
    BluetoothGATTCharacteristic,
    BluetoothGATTDescriptor,
    BluetoothGATTService,
    ESPHomeBluetoothGATTServices,
)
from majordom_integration_sdk.discovery import BLEService

from majordom_esphome.bluetooth import ESPhomeBLESource

PROXY = (
    BluetoothProxyFeature.PASSIVE_SCAN
    | BluetoothProxyFeature.ACTIVE_CONNECTIONS
    | BluetoothProxyFeature.REMOTE_CACHING
    | BluetoothProxyFeature.RAW_ADVERTISEMENTS
)
MATTER = "0000fff6-0000-1000-8000-00805f9b34fb"
DEVICE = 0xAABBCCDDEEFF
MATTER_ADV = bytes.fromhex("0b16f6ff00000fa1f7ff0180")  # service data: a commissionable Matter device
NAME = bytes.fromhex("0509") + b"Bulb"  # scan response: complete local name


class FakeAPIClient:
    api_version = APIVersion(1, 12)

    def __init__(self, features: int = PROXY, answers=(True, 247, 0)):
        self.features, self.answers = features, answers
        self.on_advertisements = None
        self.writes: list[tuple[int, bytes]] = []
        self.descriptors: list[tuple[int, bytes]] = []
        self.on_notify = None

    async def device_info(self):
        return DeviceInfo(mac_address="11:22:33:44:55:66", bluetooth_proxy_feature_flags=self.features)

    def subscribe_bluetooth_le_raw_advertisements(self, callback):
        self.on_advertisements = callback
        return lambda: setattr(self, "on_advertisements", None)

    def advertise(self, data: bytes, rssi: int = -60):
        response = BluetoothLERawAdvertisementsResponse()
        response.advertisements.add(address=DEVICE, rssi=rssi, address_type=1, data=data)
        self.on_advertisements(response)

    async def bluetooth_device_connect(self, address, on_state, feature_flags=0, address_type=None, **kwargs):
        self.connected = (address, address_type)
        on_state(*self.answers)
        return lambda: None

    async def bluetooth_gatt_get_services(self, address):
        cccd = BluetoothGATTDescriptor(uuid="00002902-0000-1000-8000-00805f9b34fb", handle=6)
        characteristics = [
            BluetoothGATTCharacteristic(uuid="c1", handle=3, properties=0x08),
            BluetoothGATTCharacteristic(uuid="c2", handle=5, properties=0x20, descriptors=[cccd]),
        ]
        return ESPHomeBluetoothGATTServices(address, [BluetoothGATTService(MATTER, 1, characteristics)])

    async def bluetooth_gatt_write(self, address, handle, data, response):
        self.writes.append((handle, data))

    async def bluetooth_gatt_start_notify(self, address, handle, callback):
        self.on_notify = callback
        return (lambda: asyncio.sleep(0)), (lambda: None)

    async def bluetooth_gatt_write_descriptor(self, address, handle, data):
        self.descriptors.append((handle, data))

    async def bluetooth_device_disconnect(self, address): ...


class Listener:
    def __init__(self):
        self.heard = []

    async def ble_did_discover_device(self, ble, info):
        self.heard.append(info)

    ble_did_update_device = ble_did_discover_device

    async def ble_did_remove_device(self, ble, info): ...


@pytest.fixture
async def ble_and_node():
    client = FakeAPIClient()
    ble = BLEService(sources=[])
    await ble.start()
    await ble.add_source(await ESPhomeBLESource.of(client))
    yield ble, client
    await ble.stop()


async def test_only_nodes_streaming_raw_advertisements_are_sources():
    assert await ESPhomeBLESource.of(FakeAPIClient(features=0)) is None
    assert (await ESPhomeBLESource.of(FakeAPIClient())).name == "esphome:11:22:33:44:55:66"


async def test_advertisements_and_scan_responses_merge_into_one_device(ble_and_node):
    ble, client = ble_and_node
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})

    client.advertise(MATTER_ADV)
    client.advertise(NAME)
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)

    first, second = listener.heard  # found, then named; the repeat changed nothing
    assert first.device.address == "AA:BB:CC:DD:EE:FF" and first.source == "esphome:11:22:33:44:55:66"
    assert second.advertisement.local_name == "Bulb"
    assert second.advertisement.service_data == {MATTER: bytes.fromhex("00000fa1f7ff0180")}


async def test_connects_and_speaks_gatt_by_handle(ble_and_node):
    ble, client = ble_and_node
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)

    connection = await ble.connect("AA:BB:CC:DD:EE:FF")
    assert client.connected == (DEVICE, 1) and connection.mtu == 247
    services = await connection.services()
    assert [(c.uuid, c.properties) for c in services[0].characteristics] == [("c1", ("write",)), ("c2", ("indicate",))]

    await connection.write(3, b"hs")
    notes = []
    await connection.start_notify(5, notes.append)
    client.on_notify(5, bytearray(b"ok"))
    assert client.writes == [(3, b"hs")] and client.descriptors == [(6, b"\x02\x00")] and notes == [b"ok"]


async def test_a_refused_connection_is_an_error(ble_and_node):
    ble, client = ble_and_node
    client.answers = (False, 0, 133)
    with pytest.raises(ConnectionError, match="error 133"):
        await ble.connect("AA:BB:CC:DD:EE:FF")


async def test_rotating_addresses_dont_grow_the_memory(ble_and_node, monkeypatch):
    ble, client = ble_and_node
    monkeypatch.setattr("majordom_esphome.bluetooth._REMEMBERED", 2)
    source = ble._sources["esphome:11:22:33:44:55:66"]
    for address in range(3):
        response = BluetoothLERawAdvertisementsResponse()
        response.advertisements.add(address=address, rssi=-60, address_type=1, data=MATTER_ADV)
        client.on_advertisements(response)
    assert list(source._heard) == ["00:00:00:00:00:01", "00:00:00:00:00:02"]

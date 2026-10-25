"""ESPHome nodes lending their Bluetooth as sources of the hub's Bluetooth router."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID

import pytest
from aioesphomeapi import (
    APIClient,
    APIConnectionError,
    BluetoothLERawAdvertisementsResponse,
    BluetoothProxyFeature,
    DeviceInfo,
)
from aioesphomeapi.model import (
    APIVersion,
    BluetoothGATTCharacteristic,
    BluetoothGATTDescriptor,
    BluetoothGATTService,
    ESPHomeBluetoothGATTServices,
)
from majordom_integration_sdk.controller import Platform
from majordom_integration_sdk.peripherals import Peripherals
from majordom_integration_sdk.runtime import BLEProxy, PeripheralState, Runtime
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.runtime.peripherals.providers.zeroconf import ZeroconfDiscoveryService
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.bluetooth import ESPhomeBLESource, ble_source_of
from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import hub_creates_device, leaked_tasks, wait_until
from tests.virtual.mdns import Advert, FakeMDNS

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
NODES = {"kitchen": "11:22:33:44:55:01", "garage": "11:22:33:44:55:02"}  # node name -> its Bluetooth MAC


class FakeAPIClient:
    api_version = APIVersion(1, 12)

    def __init__(
        self,
        features: int = PROXY,
        answers=(True, 247, 0),
        mac: str = "11:22:33:44:55:66",
        device_info_error: Exception | None = None,
        unsubscribe_error: Exception | None = None,
    ):
        self.features, self.answers, self.mac = features, answers, mac
        self.device_info_error, self.unsubscribe_error = device_info_error, unsubscribe_error
        self.on_advertisements: Callable[[BluetoothLERawAdvertisementsResponse], None] | None = None
        self.on_stop: Callable[[bool], Awaitable[None]] | None = None
        self.disconnected = False
        self.writes: list[tuple[int, bytes]] = []
        self.descriptors: list[tuple[int, bytes]] = []
        self.on_notify = None
        self.on_slots: Callable[[int, int, list[int]], None] | None = None
        self.unsubscribed = 0

    async def device_info(self):
        if self.device_info_error:
            raise self.device_info_error
        return DeviceInfo(mac_address=self.mac, bluetooth_proxy_feature_flags=self.features)

    def subscribe_bluetooth_le_raw_advertisements(self, callback):
        self.on_advertisements = callback

        def unsubscribe():
            self.on_advertisements = None
            self.unsubscribed += 1
            if self.unsubscribe_error:
                raise self.unsubscribe_error

        return unsubscribe

    def subscribe_bluetooth_connections_free(self, callback):
        self.on_slots = callback

        def unsubscribe():
            self.on_slots = None

        return unsubscribe

    # what the device connection asks of a node besides Bluetooth
    async def connect(self, login, on_stop):
        self.on_stop = on_stop

    async def list_entities_services(self):
        return [], []

    def subscribe_states(self, callback): ...

    async def disconnect(self):
        self.disconnected = True

    async def drop(self):
        assert self.on_stop
        await self.on_stop(False)

    def advertise(self, data: bytes, rssi: int = -60):
        response = BluetoothLERawAdvertisementsResponse()
        response.advertisements.add(address=DEVICE, rssi=rssi, address_type=1, data=data)
        assert self.on_advertisements
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


async def source_of(client: FakeAPIClient) -> ESPhomeBLESource | None:
    return await ble_source_of(cast(APIClient, client))


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
    source = await source_of(client)
    assert source
    await ble.add_source(source)
    yield ble, client
    await ble.stop()


async def test_only_nodes_streaming_raw_advertisements_are_sources():
    assert await source_of(FakeAPIClient(features=0)) is None
    source = await source_of(FakeAPIClient())
    assert source and source.name == "esphome:11:22:33:44:55:66"


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


async def test_a_leased_node_stays_subscribed_but_mutes_its_advertisements(ble_and_node):
    ble, client = ble_and_node
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)

    async with ble.exclusive("AA:BB:CC:DD:EE:FF", owner="matter") as lease:
        assert client.on_advertisements, "the node answers connects only to its subscriber"
        client.advertise(NAME)
        await asyncio.sleep(0.01)
        assert len(listener.heard) == 1
        await lease.connect()
        assert client.connected == (DEVICE, 1)
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)
    assert listener.heard[-1].advertisement.local_name == "Bulb"  # heard again, the muted name kept


async def test_a_lease_mutes_a_lent_node_and_resumes_it_without_unsubscribing():
    """A node lent through its proxy, as the runtime binds it: a lease pauses it (it stays subscribed, or it would
    refuse the leaseholder's connect) and the release resumes it."""
    client = FakeAPIClient()
    source = await source_of(client)
    assert source
    ble = BLEService()
    ble.peripherals = Peripherals({"ble": [*BLEProxy(source).provides]})
    await ble.start()
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)
    subscribed = client.on_advertisements

    async with ble.exclusive("AA:BB:CC:DD:EE:FF", owner="matter") as lease:
        assert lease.source == source.name
        client.advertise(MATTER_ADV, rssi=-30)
        await asyncio.sleep(0.01)
        assert len(listener.heard) == 1  # muted
        assert client.on_advertisements is subscribed
    client.advertise(NAME)
    await asyncio.sleep(0.01)
    assert len(listener.heard) == 2  # resumed
    assert client.on_advertisements is subscribed and client.unsubscribed == 0
    await ble.stop()


async def test_a_node_says_how_many_connections_it_can_still_open(ble_and_node):
    _, client = ble_and_node
    source = await source_of(client)
    assert source
    await source.start(lambda device, adv: None)
    assert source.free_slots is None  # until the node says
    assert client.on_slots
    client.on_slots(1, 3, [0xAABBCCDDEEFF, 0x112233445566])
    assert source.free_slots == 1
    await source.stop()
    assert client.on_slots is None


async def test_a_node_that_only_listens_has_no_slots_to_report():
    client = FakeAPIClient(features=BluetoothProxyFeature.PASSIVE_SCAN | BluetoothProxyFeature.RAW_ADVERTISEMENTS)
    source = await source_of(client)
    assert source
    await source.start(lambda device, adv: None)
    assert client.on_slots is None


async def test_rotating_addresses_dont_grow_the_memory(ble_and_node, monkeypatch):
    ble, client = ble_and_node
    monkeypatch.setattr("majordom_esphome.bluetooth._REMEMBERED", 2)
    source = ble._sources["esphome:11:22:33:44:55:66"]
    for address in range(3):
        response = BluetoothLERawAdvertisementsResponse()
        response.advertisements.add(address=address, rssi=-60, address_type=1, data=MATTER_ADV)
        client.on_advertisements(response)
    assert list(source._heard) == ["00:00:00:00:00:01", "00:00:00:00:00:02"]


class Nodes:
    """Stands for `APIClient` where the device connections open one: each node the hub connects to is a fake."""

    def __init__(self):
        self.clients: list[FakeAPIClient] = []
        self.options: dict[str, Any] = {}  # what the nodes are like from now on: features, device_info_error, …

    def __call__(self, address: str, port: int, password: str | None, noise_psk: str | None = None) -> FakeAPIClient:
        self.clients.append(FakeAPIClient(mac=NODES[address.removesuffix(".local")], **self.options))
        return self.clients[-1]

    def connection_of(self, node: str) -> FakeAPIClient:
        """The newest connection to the node."""
        return next(client for client in reversed(self.clients) if client.mac == NODES[node])


def lent(node: str) -> str:
    return f"esphome:{NODES[node]}"


def sources(ble: BLEService) -> list[str]:
    return sorted(ble._sources)


def proxies(runtime: Runtime) -> list[str]:
    return [p.key for p in runtime.peripherals if isinstance(p, BLEProxy)]


@pytest.fixture
def ble() -> BLEService:
    """The Hub's Bluetooth router: the runtime runs it while ESPHome binds it."""
    return BLEService()


@pytest.fixture
def nodes(monkeypatch: pytest.MonkeyPatch) -> Nodes:
    nodes = Nodes()
    monkeypatch.setattr("majordom_esphome.connection.APIClient", nodes)
    monkeypatch.setattr("majordom_esphome.connection.RECONNECT_DELAY_FIRST", 0.3)
    return nodes


@pytest.fixture
def settings() -> ESPhomeController.Settings:
    return ESPhomeController.Settings()


@pytest.fixture
async def runtime(
    deps: Platform, settings: ESPhomeController.Settings, ble: BLEService, nodes: Nodes, mdns: FakeMDNS, tmp_path
) -> AsyncIterator[Runtime]:
    """ESPHome run as by the Hub, beside the Bluetooth router and DNS-SD it binds."""
    runtime = Runtime(tmp_path / "data", [ble, ZeroconfDiscoveryService(), ESPhomeController(deps, settings=settings)])
    await runtime.start()
    await wait_until(lambda: all(runtime.status(p).state == PeripheralState.RUNNING for p in runtime.peripherals))
    yield runtime
    await runtime.stop()


@pytest.fixture
def esphome(runtime: Runtime) -> ESPhomeController:
    (controller,) = [p for p in runtime.peripherals if isinstance(p, ESPhomeController)]
    return controller


async def announce(controller: ESPhomeController, mdns: FakeMDNS, node: str) -> Discovery:
    mac = NODES[node].replace(":", "")
    await mdns.announce(Advert(name=node, mac=mac, server=f"{node}.local.", addresses=[]))
    return controller.discoveries[controller.device_uuid(mac)]


async def pair(controller: ESPhomeController, mdns: FakeMDNS, node: str) -> ESPhomeDevice:
    discovery = await announce(controller, mdns, node)
    await hub_creates_device(controller.platform, discovery, name=node)
    await controller.pair_device(discovery, None)
    async with controller.device_repository() as repo:
        device = await repo.get(discovery.id, as_=ESPhomeDevice)
    assert device is not None
    return device


async def test_a_paired_node_with_a_proxy_lends_its_radio(esphome, mdns, nodes, ble):
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})

    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")
    nodes.connection_of("kitchen").advertise(MATTER_ADV)
    await asyncio.sleep(0.01)

    assert [info.source for info in listener.heard] == [lent("kitchen")]


async def test_a_lent_radio_is_a_proxy_esphome_brings_along(runtime: Runtime, esphome, mdns, nodes):
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: len(esphome.provides) == 1, message="the node to be lent")

    proxy = runtime.peripheral(f"esphome/{lent('kitchen')}")
    assert isinstance(proxy, BLEProxy) and proxy.contributor is esphome
    assert runtime.status(proxy).state == PeripheralState.RUNNING


def test_the_setting_is_on_by_default():
    assert ESPhomeController.Settings().is_bluetooth_proxy_enabled is True


async def test_a_node_without_a_proxy_lends_nothing(esphome, mdns, nodes, ble):
    nodes.options["features"] = 0

    await pair(esphome, mdns, "kitchen")
    await asyncio.sleep(0.05)

    assert sources(ble) == []


@pytest.mark.parametrize("settings", [ESPhomeController.Settings(is_bluetooth_proxy_enabled=False)])
async def test_nothing_is_lent_while_the_setting_is_off(esphome, mdns, nodes, ble):
    await pair(esphome, mdns, "kitchen")
    await asyncio.sleep(0.05)

    assert sources(ble) == [] and esphome.provides == []


async def test_unpairing_a_node_takes_back_only_its_radio(esphome, mdns, nodes, ble):
    kitchen = await pair(esphome, mdns, "kitchen")
    await pair(esphome, mdns, "garage")
    await wait_until(lambda: sources(ble) == [lent("kitchen"), lent("garage")], message="both nodes to be lent")

    await esphome.unpair(kitchen)

    assert sources(ble) == [lent("garage")]
    assert nodes.connection_of("kitchen").on_advertisements is None


async def test_a_node_that_drops_is_taken_back_and_lent_once_when_it_returns(esphome, mdns, nodes, ble):
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")
    dropped = nodes.connection_of("kitchen")

    await dropped.drop()
    await wait_until(lambda: sources(ble) == [], message="the dropped node to be taken back")
    assert dropped.on_advertisements is None

    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent again")
    assert nodes.connection_of("kitchen") is not dropped and dropped.on_advertisements is None


async def test_stop_takes_back_every_radio(runtime: Runtime, esphome, mdns, nodes, ble):
    await pair(esphome, mdns, "kitchen")
    await pair(esphome, mdns, "garage")
    await wait_until(lambda: len(sources(ble)) == 2, message="both nodes to be lent")

    await runtime.disable(esphome)

    assert proxies(runtime) == [] and esphome.provides == []
    assert all(client.on_advertisements is None and client.disconnected for client in nodes.clients)
    assert leaked_tasks() == []


async def test_a_restarted_controller_lends_each_node_once(runtime: Runtime, esphome, mdns, nodes, ble):
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")
    first = nodes.connection_of("kitchen")

    await runtime.restart(esphome)

    await wait_until(lambda: nodes.connection_of("kitchen") is not first, message="the node to reconnect")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent again")
    nodes.connection_of("kitchen").advertise(MATTER_ADV)
    await asyncio.sleep(0.01)
    assert first.on_advertisements is None and len(listener.heard) == 1


async def test_a_failed_pairing_leaves_no_radio_lent(
    esphome, mdns, nodes, ble, output: RecordingControllerOutput, monkeypatch
):
    discovery = await announce(esphome, mdns, "kitchen")
    await hub_creates_device(esphome.platform, discovery, name="kitchen")
    # the pairing fails after connecting, once the node is lent
    monkeypatch.setattr(
        output, "controller_did_connect_device", AsyncMock(side_effect=RuntimeError("the Hub went away"))
    )

    with pytest.raises(RuntimeError):
        await esphome.pair_device(discovery, None)

    assert sources(ble) == []
    assert nodes.connection_of("kitchen").on_advertisements is None
    assert leaked_tasks() == []


async def test_a_node_whose_bluetooth_fails_to_set_up_stays_connected(
    esphome, mdns, nodes, ble, output: RecordingControllerOutput
):
    nodes.options["device_info_error"] = APIConnectionError("the node went quiet")

    device = await pair(esphome, mdns, "kitchen")
    await asyncio.sleep(0.05)

    assert device.available and output.lost_devices == []
    assert sources(ble) == []


async def test_stop_finishes_when_a_radio_cannot_be_taken_back(runtime: Runtime, esphome, mdns, nodes, ble):
    nodes.options["unsubscribe_error"] = RuntimeError("the node is gone")
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")

    await runtime.disable(esphome)

    assert nodes.connection_of("kitchen").disconnected
    assert proxies(runtime) == [] and leaked_tasks() == []

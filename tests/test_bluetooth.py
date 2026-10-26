"""ESPHome nodes lending their Bluetooth as sources of the hub's Bluetooth router."""

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
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
from aioesphomeapi.model import APIVersion, BluetoothGATTService, ESPHomeBluetoothGATTServices
from majordom_integration_sdk import Endpoint, Platform
from majordom_integration_sdk.capabilities import BLEDiscoveryInfo, BLERouter
from majordom_integration_sdk.peripherals import BLEProxy, Peripherals
from majordom_integration_sdk.repository.memory import DeviceRepositoryMemory
from majordom_integration_sdk.runtime import PeripheralState, Runtime
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.runtime.peripherals.providers.zeroconf import ZeroconfDiscoveryService
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import FakePeripheral, RecordingControllerOutput

from majordom_esphome.bluetooth import ESPhomeBLEConnection, ESPhomeBLESource, ble_source_of
from majordom_esphome.connection import WAIT_READY_TIMEOUT, ESPhomeDeviceConnection
from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import assert_not_paired, hub_creates_device, leaked_tasks, wait_until
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
TX_POWER = bytes.fromhex("020a08")  # 8 dBm
NODES = {"kitchen": "11:22:33:44:55:01", "garage": "11:22:33:44:55:02"}  # node name -> its Bluetooth MAC


class FakeAPIClient:
    api_version: APIVersion | None = APIVersion(1, 12)

    def __init__(
        self,
        features: int = PROXY,
        answers: tuple[bool, int, int] = (True, 247, 0),
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
        self.on_notify: Callable[[int, bytearray], None] | None = None
        self.on_slots: Callable[[int, int, list[int]], None] | None = None
        self.unsubscribed = 0
        self.connected: tuple[int, int | None] | None = None  # the device it connected to, and its address type
        self.on_state: Callable[[bool, int, int], None] | None = None  # of the device it connected to
        self.notifying: set[int] = set()
        self.disconnects: list[int] = []

    async def device_info(self) -> DeviceInfo:
        if self.device_info_error:
            raise self.device_info_error
        return DeviceInfo.from_dict({"mac_address": self.mac, "bluetooth_proxy_feature_flags": self.features})

    def subscribe_bluetooth_le_raw_advertisements(
        self, callback: Callable[[BluetoothLERawAdvertisementsResponse], None]
    ) -> Callable[[], None]:
        self.on_advertisements = callback

        def unsubscribe():
            self.on_advertisements = None
            self.unsubscribed += 1
            if self.unsubscribe_error:
                raise self.unsubscribe_error

        return unsubscribe

    def subscribe_bluetooth_connections_free(
        self, callback: Callable[[int, int, list[int]], None]
    ) -> Callable[[], None]:
        self.on_slots = callback

        def unsubscribe():
            self.on_slots = None

        return unsubscribe

    # what the device connection asks of a node besides Bluetooth
    async def connect(self, login: bool, on_stop: Callable[[bool], Awaitable[None]]):
        self.on_stop = on_stop

    async def list_entities_services(self) -> tuple[list[Any], list[Any]]:
        return [], []

    def subscribe_states(self, callback: Callable[[Any], None]): ...

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

    async def bluetooth_device_connect(
        self,
        address: int,
        on_state: Callable[[bool, int, int], None],
        feature_flags: int = 0,
        address_type: int | None = None,
        **kwargs: Any,
    ) -> Callable[[], None]:
        self.connected = (address, address_type)
        self.on_state = on_state
        on_state(*self.answers)

        def unsubscribe():
            self.on_state = None

        return unsubscribe

    async def bluetooth_gatt_get_services(self, address: int) -> ESPHomeBluetoothGATTServices:
        cccd = {"uuid": "00002902-0000-1000-8000-00805f9b34fb", "handle": 6}
        characteristics = [
            {"uuid": "c1", "handle": 3, "properties": 0x08},
            {"uuid": "c2", "handle": 5, "properties": 0x20, "descriptors": [cccd]},
        ]
        service = BluetoothGATTService.from_dict({"uuid": MATTER, "handle": 1, "characteristics": characteristics})
        # a dataclass made by functools.partial, which ty can't see into
        return ESPHomeBluetoothGATTServices(address, [service])  # ty: ignore[too-many-positional-arguments]

    async def bluetooth_gatt_write(self, address: int, handle: int, data: bytes, response: bool):
        self.writes.append((handle, data))

    async def bluetooth_gatt_read(self, address: int, handle: int) -> bytearray:
        return bytearray(b"value")

    async def bluetooth_gatt_start_notify(
        self, address: int, handle: int, callback: Callable[[int, bytearray], None]
    ) -> tuple[Callable[[], Awaitable[None]], Callable[[], None]]:
        self.on_notify = callback
        self.notifying.add(handle)

        async def stop():
            self.notifying.discard(handle)

        return stop, (lambda: None)

    def bluetooth_gatt_stop_notify_for_address(self, address: int): ...

    async def bluetooth_gatt_write_descriptor(self, address: int, handle: int, data: bytes):
        self.descriptors.append((handle, data))

    async def bluetooth_device_disconnect(self, address: int):
        self.disconnects.append(address)


async def source_of(client: FakeAPIClient) -> ESPhomeBLESource | None:
    return await ble_source_of(cast(APIClient, client))


class Listener:
    def __init__(self):
        self.heard: list[BLEDiscoveryInfo] = []

    async def ble_did_discover_device(self, ble: BLERouter, info: BLEDiscoveryInfo):
        self.heard.append(info)

    ble_did_update_device = ble_did_discover_device

    async def ble_did_remove_device(self, ble: BLERouter, info: BLEDiscoveryInfo): ...


@pytest.fixture
async def ble_and_node() -> AsyncIterator[tuple[BLEService, FakeAPIClient]]:
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


async def test_a_node_that_closed_its_api_connection_meanwhile_lends_nothing():
    client = FakeAPIClient()
    client.api_version = None  # what the client knows once the connection closed

    assert await source_of(client) is None


async def test_advertisements_and_scan_responses_merge_into_one_device(ble_and_node: tuple[BLEService, FakeAPIClient]):
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


async def test_a_scan_response_keeps_the_tx_power_its_advertisement_carried(
    ble_and_node: tuple[BLEService, FakeAPIClient],
):
    ble, client = ble_and_node
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})

    client.advertise(MATTER_ADV + TX_POWER)
    client.advertise(NAME)
    await asyncio.sleep(0.01)

    assert [info.advertisement.tx_power for info in listener.heard] == [8, 8]


async def test_connects_and_speaks_gatt_by_handle(ble_and_node: tuple[BLEService, FakeAPIClient]):
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
    assert client.on_notify
    client.on_notify(5, bytearray(b"ok"))
    assert client.writes == [(3, b"hs")] and client.descriptors == [(6, b"\x02\x00")] and notes == [b"ok"]


async def test_a_closed_connection_leaves_no_notify_handler_on_the_node():
    """The library cleans a device's notify handlers up only once told it disconnected, which the connection stops
    listening for as it disconnects: a handler left behind would feed a later connection to the same handle."""
    handlers: list[Callable[..., None]] = []

    class NodeConnection:  # what the real client talks through
        is_connected = True

        def add_message_callback(self, callback: Callable[..., None], types: Any) -> Callable[[], None]:
            handlers.append(callback)
            return lambda: handlers.remove(callback)

        def send_message(self, message: Any): ...

    class Client(APIClient):
        def _get_connection(self) -> Any:
            return NodeConnection()

    client = Client("node", 6053, None)
    client._send_bluetooth_message_await_response = AsyncMock()
    client._bluetooth_device_request = AsyncMock()
    connection = ESPhomeBLEConnection(client, DEVICE, 247, lambda: None)
    characteristic = {"uuid": "c", "handle": 5}
    connection._services = [
        BluetoothGATTService.from_dict({"uuid": MATTER, "handle": 1, "characteristics": [characteristic]})
    ]
    await connection.start_notify(5, lambda data: None)

    await connection.disconnect()

    assert handlers == []


async def test_a_proxied_connection_reads_stops_notifying_and_disconnects(
    ble_and_node: tuple[BLEService, FakeAPIClient],
):
    ble, client = ble_and_node
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)
    connection = await ble.connect("AA:BB:CC:DD:EE:FF")

    assert await connection.read(3) == b"value"
    await connection.start_notify(5, lambda data: None)
    await connection.stop_notify(5)
    assert client.notifying == set()
    await connection.disconnect()
    assert client.on_state is None and client.disconnects == [DEVICE]


async def test_a_device_dropping_its_proxied_connection_is_told(ble_and_node: tuple[BLEService, FakeAPIClient]):
    ble, client = ble_and_node
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)
    dropped: list[bool] = []
    await ble.connect("AA:BB:CC:DD:EE:FF", on_disconnect=lambda: dropped.append(True))

    assert client.on_state
    client.on_state(False, 0, 0x13)  # the device went away

    assert dropped == [True]


async def test_notifying_an_unknown_handle_is_a_lookup_error(ble_and_node: tuple[BLEService, FakeAPIClient]):
    ble, client = ble_and_node
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)
    connection = await ble.connect("AA:BB:CC:DD:EE:FF")

    with pytest.raises(LookupError):
        await connection.start_notify(99, lambda data: None)


async def test_a_refused_connection_is_an_error(ble_and_node: tuple[BLEService, FakeAPIClient]):
    ble, client = ble_and_node
    client.answers = (False, 0, 133)
    with pytest.raises(ConnectionError, match="error 133"):
        await ble.connect("AA:BB:CC:DD:EE:FF")


async def test_a_leased_node_stays_subscribed_but_mutes_its_advertisements(
    ble_and_node: tuple[BLEService, FakeAPIClient],
):
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


async def test_a_node_hears_again_after_a_router_restart_cut_its_lease_short():
    """The router stops while the node is leased (a restart, a failed hook): the release can't resume it then."""
    client = FakeAPIClient()
    source = await source_of(client)
    assert source
    ble = BLEService(sources=[source])
    await ble.start()
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})

    async with ble.exclusive("AA:BB:CC:DD:EE:FF", owner="esphome-improv"):
        await ble.stop()
    await ble.start()
    client.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)

    assert len(listener.heard) == 1, "a started source hears"
    await ble.stop()


async def test_a_node_reconnecting_while_its_radio_is_leased_hears_once_released():
    """Its API connection drops during the lease (Matter commissioning through it): ESPHome takes the radio back, then
    lends the one of the new connection under the same name."""
    first = FakeAPIClient()
    old = await source_of(first)
    assert old
    ble = BLEService(sources=[old])
    await ble.start()
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})
    first.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)

    async with ble.exclusive("AA:BB:CC:DD:EE:FF", owner="matter"):
        await ble.remove_source(old.name)
        second = FakeAPIClient()
        new = await source_of(second)
        assert new
        await ble.add_source(new)
        assert second.on_advertisements, "the node answers the lease's connects only to its subscriber"
        second.advertise(NAME)
        await asyncio.sleep(0.01)
        assert len(listener.heard) == 1, "muted while leased"
    second.advertise(MATTER_ADV)
    await asyncio.sleep(0.01)

    assert len(listener.heard) == 2
    await ble.stop()


async def test_a_quiet_node_stays_subscribed(ble_and_node: tuple[BLEService, FakeAPIClient]):
    """A node in a quiet place hears no one for a while; its loss shows through its API connection, and resubscribing
    it would drop the replies of the connections it proxies."""
    ble, client = ble_and_node
    subscribed = client.on_advertisements
    ble._silence_sec = 0.01
    await asyncio.sleep(0.02)

    await ble._revive_silent()

    assert client.on_advertisements is subscribed and client.unsubscribed == 0


async def test_a_node_says_how_many_connections_it_can_still_open(ble_and_node: tuple[BLEService, FakeAPIClient]):
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


async def test_rotating_addresses_dont_grow_the_memory(
    ble_and_node: tuple[BLEService, FakeAPIClient], monkeypatch: pytest.MonkeyPatch
):
    ble, client = ble_and_node
    monkeypatch.setattr("majordom_esphome.bluetooth._REMEMBERED", 2)
    source = ble._sources["esphome:11:22:33:44:55:66"]
    assert isinstance(source, ESPhomeBLESource) and client.on_advertisements
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


def zeroconf_of(runtime: Runtime) -> ZeroconfDiscoveryService:
    (zeroconf,) = [p for p in runtime.peripherals if isinstance(p, ZeroconfDiscoveryService)]
    return zeroconf


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
    platform: Platform,
    settings: ESPhomeController.Settings,
    ble: BLEService,
    nodes: Nodes,
    mdns: FakeMDNS,
    output: RecordingControllerOutput,
    tmp_path: Path,
) -> AsyncIterator[Runtime]:
    """ESPHome run as by the Hub, beside the Bluetooth router and DNS-SD it binds."""
    peripherals = [ble, ZeroconfDiscoveryService(), ESPhomeController(platform, settings=settings)]
    runtime = Runtime(tmp_path / "data", peripherals, platform=platform, controller_output=output)
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
    async with controller.platform.device_repository(controller) as repo:
        device = await repo.get(discovery.id, as_=ESPhomeDevice)
    assert device is not None
    return device


async def test_a_paired_node_with_a_proxy_lends_its_radio(
    esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    listener = Listener()
    ble.register(listener, {UUID(MATTER)})

    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")
    nodes.connection_of("kitchen").advertise(MATTER_ADV)
    await asyncio.sleep(0.01)

    assert [info.source for info in listener.heard] == [lent("kitchen")]


async def test_a_lent_radio_is_a_proxy_esphome_brings_along(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes
):
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: len(esphome.provides) == 1, message="the node to be lent")

    proxy = runtime.peripheral(f"esphome/{NODES['kitchen']}")  # known by its Bluetooth MAC
    assert isinstance(proxy, BLEProxy) and proxy.contributor is esphome
    assert runtime.status(proxy).state == PeripheralState.RUNNING


def test_the_setting_is_on_by_default():
    assert ESPhomeController.Settings().is_bluetooth_proxy_enabled is True


async def test_a_node_without_a_proxy_lends_nothing(
    esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    nodes.options["features"] = 0

    await pair(esphome, mdns, "kitchen")
    await asyncio.sleep(0.05)

    assert sources(ble) == []


@pytest.mark.parametrize("settings", [ESPhomeController.Settings(is_bluetooth_proxy_enabled=False)])
async def test_nothing_is_lent_while_the_setting_is_off(
    esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    await pair(esphome, mdns, "kitchen")
    await asyncio.sleep(0.05)

    assert sources(ble) == [] and esphome.provides == []


async def test_turning_the_proxies_off_restarts_it_and_takes_every_radio_back(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")
    first = nodes.connection_of("kitchen")

    await runtime.update_settings(esphome, {"is_bluetooth_proxy_enabled": False})

    await wait_until(lambda: nodes.connection_of("kitchen") is not first, message="the node to reconnect")
    await asyncio.sleep(0.05)
    assert sources(ble) == [] and proxies(runtime) == []


async def test_unpairing_a_node_takes_back_only_its_radio(
    esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    kitchen = await pair(esphome, mdns, "kitchen")
    await pair(esphome, mdns, "garage")
    await wait_until(lambda: sources(ble) == [lent("kitchen"), lent("garage")], message="both nodes to be lent")

    await esphome.unpair(kitchen)

    assert sources(ble) == [lent("garage")]
    assert nodes.connection_of("kitchen").on_advertisements is None


async def test_a_node_that_drops_is_taken_back_and_lent_once_when_it_returns(
    esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")
    dropped = nodes.connection_of("kitchen")

    await dropped.drop()
    await wait_until(lambda: sources(ble) == [], message="the dropped node to be taken back")
    assert dropped.on_advertisements is None

    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent again")
    assert nodes.connection_of("kitchen") is not dropped and dropped.on_advertisements is None


async def test_stop_takes_back_every_radio(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    await pair(esphome, mdns, "kitchen")
    await pair(esphome, mdns, "garage")
    await wait_until(lambda: len(sources(ble)) == 2, message="both nodes to be lent")

    await runtime.disable(esphome)

    assert proxies(runtime) == [] and esphome.provides == []
    assert all(client.on_advertisements is None and client.disconnected for client in nodes.clients)
    assert leaked_tasks() == []


async def test_a_restarted_controller_lends_each_node_once(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
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
    esphome: ESPhomeController,
    mdns: FakeMDNS,
    nodes: Nodes,
    ble: BLEService,
    output: RecordingControllerOutput,
    monkeypatch: pytest.MonkeyPatch,
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


async def test_a_pairing_that_failed_once_connected_can_be_tried_again(
    esphome: ESPhomeController,
    mdns: FakeMDNS,
    nodes: Nodes,
    output: RecordingControllerOutput,
    monkeypatch: pytest.MonkeyPatch,
):
    discovery = await announce(esphome, mdns, "kitchen")
    await hub_creates_device(esphome.platform, discovery, name="kitchen")
    with monkeypatch.context() as hub:
        hub.setattr(output, "controller_did_connect_device", AsyncMock(side_effect=RuntimeError("the Hub went away")))
        with pytest.raises(RuntimeError):
            await esphome.pair_device(discovery, None)

    await esphome.pair_device(esphome.discoveries[discovery.id], None)

    assert output.connected_devices == [discovery.id]
    assert [client.disconnected for client in nodes.clients] == [True, False]


async def test_a_node_whose_bluetooth_fails_to_set_up_stays_connected(
    esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService, output: RecordingControllerOutput
):
    nodes.options["device_info_error"] = APIConnectionError("the node went quiet")

    device = await pair(esphome, mdns, "kitchen")
    await asyncio.sleep(0.05)

    assert device.available and output.lost_devices == []
    assert sources(ble) == []


async def test_stop_finishes_when_a_radio_cannot_be_taken_back(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    nodes.options["unsubscribe_error"] = RuntimeError("the node is gone")
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")

    await runtime.disable(esphome)

    assert nodes.connection_of("kitchen").disconnected
    assert proxies(runtime) == [] and leaked_tasks() == []


# A stop meeting a pairing in flight


@pytest.fixture
def slow_nodes(monkeypatch: pytest.MonkeyPatch) -> asyncio.Event:
    """The nodes list their entities only once it is set: a pairing waits for its node meanwhile."""
    answered = asyncio.Event()

    async def list_entities_services(self: FakeAPIClient) -> tuple[list[Any], list[Any]]:
        await answered.wait()
        return [], []

    monkeypatch.setattr(FakeAPIClient, "list_entities_services", list_entities_services)
    return answered


async def start_pairing(
    controller: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, node: str
) -> tuple[Discovery, asyncio.Task[UUID | None]]:
    """The user pairs the node; returns once the pairing waits for it."""
    discovery = await announce(controller, mdns, node)
    await hub_creates_device(controller.platform, discovery, name=node)
    pairing = asyncio.create_task(controller.pair_device(discovery, None))
    await wait_until(lambda: nodes.clients and nodes.clients[-1].on_stop, message="the node to answer the connect")
    return discovery, pairing


async def test_a_pairing_that_outlives_a_stop_is_refused_and_leaves_nothing_behind(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, slow_nodes: asyncio.Event
):
    discovery, pairing = await start_pairing(esphome, mdns, nodes, "kitchen")

    await runtime.disable(esphome)
    slow_nodes.set()

    with pytest.raises(ConnectionError, match="stopped"):
        await pairing
    assert esphome._connections == {} and nodes.connection_of("kitchen").disconnected
    await assert_not_paired(esphome.platform, discovery.id)
    assert leaked_tasks() == []


async def test_a_pairing_across_a_restart_is_refused_and_lends_nothing(
    runtime: Runtime,
    esphome: ESPhomeController,
    mdns: FakeMDNS,
    nodes: Nodes,
    ble: BLEService,
    slow_nodes: asyncio.Event,
):
    """The user turns the proxies off (another hub needs the node's Bluetooth) while the node is paired: the pairing,
    made with the proxies on, finishes once ESPHome runs again."""
    discovery, pairing = await start_pairing(esphome, mdns, nodes, "kitchen")

    await runtime.update_settings(esphome, {"is_bluetooth_proxy_enabled": False})
    await wait_until(lambda: runtime.status(esphome).state == PeripheralState.RUNNING, message="ESPHome to run")
    slow_nodes.set()

    with pytest.raises(ConnectionError, match="stopped"):
        await pairing
    await asyncio.sleep(0.05)
    assert sources(ble) == [] and esphome.provides == []
    assert esphome._connections == {} and nodes.connection_of("kitchen").disconnected
    await assert_not_paired(esphome.platform, discovery.id)


async def test_a_stop_while_the_device_is_saved_refuses_the_pairing(
    runtime: Runtime,
    esphome: ESPhomeController,
    mdns: FakeMDNS,
    nodes: Nodes,
    output: RecordingControllerOutput,
    monkeypatch: pytest.MonkeyPatch,
):
    discovery = await announce(esphome, mdns, "kitchen")
    await hub_creates_device(esphome.platform, discovery, name="kitchen")
    save = DeviceRepositoryMemory.save

    async def save_then_stop(self: DeviceRepositoryMemory, device: Any, previous_id: UUID | None = None):
        await save(self, device, previous_id)
        if isinstance(device, ESPhomeDevice) and device.parameters is not None and device.available:
            await runtime.disable(esphome)  # the pairing's own save: the device is complete

    monkeypatch.setattr(DeviceRepositoryMemory, "save", save_then_stop)

    with pytest.raises(ConnectionError, match="stopped"):
        await esphome.pair_device(discovery, None)
    assert esphome._connections == {} and nodes.connection_of("kitchen").disconnected
    assert output.connected_devices == [] and leaked_tasks() == []


async def test_a_stop_reaching_a_node_connection_from_inside_it_ends_it(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    """A node drops while the runtime is busy: its connection task tells the runtime its radio is gone and waits for
    the runtime to act, which stops ESPHome (the Hub removed DNS-SD meanwhile). That stop ends the waiting task too."""
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")
    opened = len(nodes.clients)

    async with runtime._lock:  # the runtime is busy
        await nodes.connection_of("kitchen").drop()
        await asyncio.sleep(0.05)
        removing = asyncio.create_task(runtime.remove(zeroconf_of(runtime)))
        await asyncio.sleep(0.05)
    await removing

    await wait_until(lambda: runtime.status(esphome).state == PeripheralState.WAITING, message="ESPHome to stop")
    await asyncio.sleep(1)  # past the reconnect delay
    assert esphome._connections == {} and len(nodes.clients) == opened, "a stopped controller doesn't reconnect"
    assert leaked_tasks() == []


async def test_a_node_lends_its_radio_only_once_it_is_paired(
    runtime: Runtime,
    esphome: ESPhomeController,
    mdns: FakeMDNS,
    nodes: Nodes,
    ble: BLEService,
    monkeypatch: pytest.MonkeyPatch,
):
    held = asyncio.Event()
    wait_ready = ESPhomeDeviceConnection.wait_ready

    async def wait_ready_then_hold(self: ESPhomeDeviceConnection, timeout: float = WAIT_READY_TIMEOUT):
        await wait_ready(self, timeout)
        await held.wait()  # the node is connected, its pairing not done yet

    monkeypatch.setattr(ESPhomeDeviceConnection, "wait_ready", wait_ready_then_hold)
    discovery = await announce(esphome, mdns, "kitchen")
    await hub_creates_device(esphome.platform, discovery, name="kitchen")
    pairing = asyncio.create_task(esphome.pair_device(discovery, None))
    await wait_until(lambda: nodes.clients and nodes.clients[-1].on_stop, message="the node to answer the connect")
    await asyncio.sleep(0.1)

    assert sources(ble) == [] and esphome.provides == [], "a node being paired serves no one yet"
    held.set()
    await pairing
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the paired node to be lent")


async def test_a_cancelled_pairing_lets_go_of_the_node(
    runtime: Runtime,
    esphome: ESPhomeController,
    mdns: FakeMDNS,
    nodes: Nodes,
    ble: BLEService,
    slow_nodes: asyncio.Event,
):
    discovery, pairing = await start_pairing(esphome, mdns, nodes, "kitchen")

    pairing.cancel()  # the Hub's request went away
    with pytest.raises(asyncio.CancelledError):
        await pairing
    slow_nodes.set()
    await asyncio.sleep(0.1)

    assert nodes.connection_of("kitchen").disconnected and sources(ble) == []
    await assert_not_paired(esphome.platform, discovery.id)
    assert leaked_tasks() == []


# DNS-SD going away or restarting under it


async def test_dns_sd_removed_first_still_lets_it_stop_cleanly(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    await pair(esphome, mdns, "kitchen")
    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent")

    await runtime.remove(zeroconf_of(runtime))  # stopped before ESPHome, its registration gone with it

    await wait_until(lambda: runtime.status(esphome).state == PeripheralState.WAITING, message="ESPHome to stop")
    assert esphome._connections == {} and nodes.connection_of("kitchen").disconnected
    assert leaked_tasks() == []


async def test_nodes_are_heard_after_dns_sd_restarts(runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS):
    await runtime.restart(zeroconf_of(runtime))
    await wait_until(lambda: runtime.status(esphome).state == PeripheralState.RUNNING, message="ESPHome to run")

    assert await announce(esphome, mdns, "kitchen"), "a node announced after the restart is discovered"


# Discoveries across a stop


async def test_a_stopped_controller_offers_nothing_to_pair(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes
):
    discovery = await announce(esphome, mdns, "kitchen")
    await hub_creates_device(esphome.platform, discovery, name="kitchen")

    await runtime.disable(esphome)

    assert esphome.discoveries == {}
    with pytest.raises(ValueError, match="no longer discovered"):
        await esphome.pair_device(discovery, None)
    assert esphome._connections == {} and nodes.clients == []


async def test_a_restarted_controller_reports_what_it_hears_again(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, output: RecordingControllerOutput
):
    await announce(esphome, mdns, "kitchen")

    await runtime.restart(esphome)
    await wait_until(lambda: runtime.status(esphome).state == PeripheralState.RUNNING, message="ESPHome to run")
    await announce(esphome, mdns, "kitchen")  # heard anew

    assert len(output.received_discoveries) == 2


async def test_a_paired_node_heard_while_it_starts_is_no_discovery(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, monkeypatch: pytest.MonkeyPatch
):
    """On a restart DNS-SD's cache is warm: it tells the nodes it knows at once, maybe before the paired devices are
    read (a busy database at boot)."""
    await pair(esphome, mdns, "kitchen")
    get_all = DeviceRepositoryMemory.get_all

    async def slow_get_all(self: DeviceRepositoryMemory, as_: Any = None) -> Any:
        await asyncio.sleep(0.3)
        return await get_all(self, as_)

    monkeypatch.setattr(DeviceRepositoryMemory, "get_all", slow_get_all)
    kitchen = Advert(name="kitchen", mac=NODES["kitchen"].replace(":", ""), server="kitchen.local.", addresses=[])

    restarting = asyncio.create_task(runtime.restart(esphome))
    await asyncio.sleep(0.1)
    await mdns.announce(kitchen)
    await restarting
    await mdns.announce(kitchen)

    assert esphome.discoveries == {}, "picking it would open a second connection to the node"


# Routers coming later


async def test_a_bluetooth_router_starting_later_is_taken_in_place(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes, ble: BLEService
):
    await runtime.disable(ble)
    await pair(esphome, mdns, "kitchen")
    opened = len(nodes.clients)

    await runtime.enable(ble)

    await wait_until(lambda: sources(ble) == [lent("kitchen")], message="the node to be lent to it")
    assert runtime.status(esphome).state == PeripheralState.RUNNING
    assert len(nodes.clients) == opened, "ESPHome kept its node connections"


async def test_a_border_router_coming_later_is_taken_in_place(
    runtime: Runtime, esphome: ESPhomeController, mdns: FakeMDNS, nodes: Nodes
):
    await pair(esphome, mdns, "kitchen")
    opened = len(nodes.clients)

    await runtime.add(FakePeripheral("otbr", provides=[Endpoint("thread", "otbr-rest", "http://otbr:8081")]))

    await wait_until(lambda: "thread" in esphome.peripherals.bound, message="the border router to be handed over")
    assert runtime.status(esphome).state == PeripheralState.RUNNING
    assert len(nodes.clients) == opened, "ESPHome kept its node connections"

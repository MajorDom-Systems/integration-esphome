"""Unprovisioned nodes over Improv BLE: found by their advert, given the Hub's Wi-Fi, then paired over the network.

ESPHome host nodes have no BLE, so the node's Improv GATT service is scripted below as the connection a Bluetooth source
hands out; the SDK's real Bluetooth router routes its adverts and connections. Once the node "joins" Wi-Fi, the compiled
keyless virtual node appears over mDNS.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable

import pytest
import pytest_asyncio
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from improv_ble_client import SERVICE_DATA_UUID, SERVICE_UUID, Error, State
from improv_ble_client.protocol import (
    CHARACTERISTIC_UUID_CAPABILITIES,
    CHARACTERISTIC_UUID_ERROR,
    CHARACTERISTIC_UUID_RPC_COMMAND,
    CHARACTERISTIC_UUID_RPC_RESULT,
    CHARACTERISTIC_UUID_STATE,
    IMPROV_CHARACTERISTICS,
    WiFiSettingsCmd,
    WiFiSettingsRes,
)
from majordom_integration_sdk import MissingCredentialsError, WifiCredentials
from majordom_integration_sdk.capabilities import BLEDiscoveryInfo, GattCharacteristic, GattService
from majordom_integration_sdk.controller import Context, Platform
from majordom_integration_sdk.peripherals import Peripherals
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.schemas import CredentialsType
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import FakeBLESource, RecordingControllerOutput

from majordom_esphome import controller as controller_module
from majordom_esphome import improv
from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import (
    assert_not_paired,
    hub_creates_device,
    leaked_tasks,
    wait_until,
)
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import KEYLESS, VirtualDevice

pytestmark = pytest.mark.timeout(60)


HOME = WifiCredentials("Home", "correct horse battery")
HANDLES = {uuid: 0x11 + i for i, uuid in enumerate(IMPROV_CHARACTERISTICS)}


class FakeImprovNode:
    """An unprovisioned ESPHome node's Improv GATT service, as the `BLEConnection` a Bluetooth source hands out."""

    def __init__(self, name: str = KEYLESS.name, address: str = "AA:BB:CC:DD:EE:01") -> None:
        self.name, self.address = name, address
        self.state = State.AUTHORIZED
        self.password = HOME.password  # of the network it can join
        self.fails_with: Error | None = None
        self.received: list[WifiCredentials] = []
        self.on_joined: Callable[[], Awaitable[None]] | None = None  # it shows up on the network
        self._callbacks: dict[int, Callable[[bytes], None]] = {}

    def advertisement(self, named: bool = True) -> AdvertisementData:
        return AdvertisementData(
            local_name=self.name if named else None,
            manufacturer_data={},
            service_data={SERVICE_DATA_UUID: self._service_data()},
            service_uuids=[SERVICE_UUID],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        )

    def info(self, named: bool = True) -> BLEDiscoveryInfo:
        return BLEDiscoveryInfo(BLEDevice(self.address, None, None), self.advertisement(named))

    async def advertise(self, source: FakeBLESource, named: bool = True, rssi: int = -60) -> None:
        """Heard by ``source``; returns once the SDK has passed the advert on."""
        name = self.name if named else None
        source.advertise(self.address, rssi, name, [SERVICE_UUID], {SERVICE_DATA_UUID: self._service_data()})
        await asyncio.sleep(0.05)

    async def press_button(self) -> None:
        self.state = State.AUTHORIZED
        self._notify(CHARACTERISTIC_UUID_STATE, self.state)

    def _service_data(self) -> bytes:
        return bytes([self.state, 0, 0, 0, 0, 0])

    # BLEConnection

    @property
    def mtu(self) -> int:
        return 247

    async def services(self) -> list[GattService]:
        characteristics = tuple(GattCharacteristic(uuid, handle, ("read",)) for uuid, handle in HANDLES.items())
        return [GattService(SERVICE_UUID, 0x10, characteristics)]

    async def read(self, handle: int) -> bytes:
        values = {HANDLES[CHARACTERISTIC_UUID_STATE]: self.state, HANDLES[CHARACTERISTIC_UUID_CAPABILITIES]: 0}
        return bytes([values.get(handle, Error.NO_ERROR)])

    async def write(self, handle: int, data: bytes, is_response_needed: bool = True) -> None:
        assert handle == HANDLES[CHARACTERISTIC_UUID_RPC_COMMAND]
        command = WiFiSettingsCmd.from_bytes(data)
        asyncio.get_running_loop().create_task(
            self._join(WifiCredentials(command.ssid.decode(), command.password.decode()))
        )

    async def start_notify(self, handle: int, callback: Callable[[bytes], None]) -> None:
        self._callbacks[handle] = callback

    async def stop_notify(self, handle: int) -> None:
        self._callbacks.pop(handle, None)

    async def disconnect(self) -> None:
        self._callbacks.clear()

    async def _join(self, wifi: WifiCredentials) -> None:
        if self.state == State.AUTHORIZATION_REQUIRED:
            self._notify(CHARACTERISTIC_UUID_ERROR, Error.NOT_AUTHORIZED)
            return
        self.received.append(wifi)
        self._notify(CHARACTERISTIC_UUID_ERROR, Error.NO_ERROR)
        self._notify(CHARACTERISTIC_UUID_STATE, State.PROVISIONING)
        error = self.fails_with or (Error.UNABLE_TO_CONNECT if wifi.password != self.password else None)
        if error is not None:
            self._notify(CHARACTERISTIC_UUID_ERROR, error)
            self._notify(CHARACTERISTIC_UUID_STATE, State.AUTHORIZED)
            return
        self.state = State.PROVISIONED
        self._notify(CHARACTERISTIC_UUID_STATE, self.state)
        if callback := self._callbacks.get(HANDLES[CHARACTERISTIC_UUID_RPC_RESULT]):
            callback(WiFiSettingsRes(b"http://127.0.0.1", []).as_bytes())
        if self.on_joined is not None:
            await self.on_joined()

    def _notify(self, uuid: str, value: int) -> None:
        if callback := self._callbacks.get(HANDLES[uuid]):
            callback(bytes([value]))


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeImprovNode:
    monkeypatch.setattr(improv, "AUTHORIZATION_TIMEOUT", 1.0)
    monkeypatch.setattr(improv, "LEASE_TIMEOUT", 0.5)
    monkeypatch.setattr(controller_module, "NODE_APPEAR_TIMEOUT", 1.0)
    return FakeImprovNode()


@pytest.fixture
def radio(node: FakeImprovNode) -> FakeBLESource:
    """The Hub's own Bluetooth adapter."""
    return FakeBLESource("hci0", connection=node)


@pytest.fixture
def deps(deps: Platform) -> Platform:
    return deps.copy(context=Context(wifi=HOME))


@pytest_asyncio.fixture
async def router(radio: FakeBLESource) -> AsyncIterator[BLEService]:
    ble = BLEService(sources=[radio])
    await ble.start()
    yield ble
    await ble.stop()


@pytest.fixture
def peripherals(peripherals: Peripherals, router: BLEService) -> Peripherals:
    return Peripherals(peripherals | {"ble": router.endpoint})


@pytest_asyncio.fixture
async def ble_discovery(controller: ESPhomeController, node: FakeImprovNode, radio: FakeBLESource) -> Discovery:
    await node.advertise(radio)
    (found,) = controller.discoveries.values()
    await hub_creates_device(controller.platform, found)
    return found


@pytest_asyncio.fixture
async def keyless_device() -> AsyncIterator[VirtualDevice]:
    virtual = VirtualDevice(KEYLESS)
    await virtual.start()
    yield virtual
    await virtual.stop()


def joins_as_keyless_device(node: FakeImprovNode, mdns: FakeMDNS) -> None:
    async def appear() -> None:
        await mdns.announce(Advert(name=KEYLESS.name, mac=KEYLESS.mac, port=KEYLESS.port, keyless=True))

    node.on_joined = appear


async def last_error(output: RecordingControllerOutput, discovery: Discovery) -> str:
    return next((d.last_error or "" for d in reversed(output.updated_discoveries) if d.id == discovery.id), "")


# Discovery


async def test_a_node_waiting_for_wifi_is_discovered_by_its_name(
    controller: ESPhomeController, node: FakeImprovNode, radio: FakeBLESource, output: RecordingControllerOutput
):
    await node.advertise(radio)

    (discovery,) = output.received_discoveries
    assert discovery.id == controller.device_uuid(f"improv:{node.name}")
    assert (discovery.transport, discovery.device_name) == ("ble", node.name)
    assert discovery.expected_credentials_options == [CredentialsType.none], "the Hub gives it its own Wi-Fi"


async def test_the_discovery_id_follows_the_name_not_the_ble_address(
    controller: ESPhomeController, node: FakeImprovNode, radio: FakeBLESource
):
    await node.advertise(radio)
    first = set(controller.discoveries)
    await controller.ble_did_remove_device(controller.peripherals.instance("ble"), node.info())
    node.address = "AA:BB:CC:DD:EE:02"  # a random (resolvable private) address changes

    await node.advertise(radio)

    assert set(controller.discoveries) == first


async def test_the_name_is_remembered_for_the_adverts_that_do_not_carry_it(
    controller: ESPhomeController, node: FakeImprovNode, radio: FakeBLESource, output: RecordingControllerOutput
):
    await node.advertise(radio, named=False)
    assert controller.discoveries == {}, "without its name the node has no stable identity yet"

    await node.advertise(radio)
    node.state = State.AUTHORIZATION_REQUIRED
    await node.advertise(radio, named=False)

    assert len(output.received_discoveries) == 1
    assert list(controller.discoveries) == [output.received_discoveries[0].id]


@pytest.mark.parametrize("state", [State.PROVISIONING, State.PROVISIONED])
async def test_a_node_that_already_has_wifi_is_not_offered(
    controller: ESPhomeController, node: FakeImprovNode, radio: FakeBLESource, state: State
):
    node.state = state

    await node.advertise(radio)

    assert controller.discoveries == {}


async def test_a_node_that_stops_advertising_is_lost(
    controller: ESPhomeController, node: FakeImprovNode, radio: FakeBLESource, output: RecordingControllerOutput
):
    await node.advertise(radio)

    await controller.ble_did_remove_device(controller.peripherals.instance("ble"), node.info(named=False))

    assert controller.discoveries == {}
    assert output.lost_discoveries == [output.received_discoveries[0].id]


async def test_ble_discovery_stops_with_the_controller(
    controller: ESPhomeController, node: FakeImprovNode, radio: FakeBLESource
):
    await controller.stop()

    await node.advertise(radio)

    assert controller.discoveries == {}


# Pairing: Wi-Fi over BLE, then the node over the network


async def test_pairing_gives_the_node_the_hubs_wifi_then_pairs_it_with_a_key_of_its_own(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeImprovNode,
    mdns: FakeMDNS,
    keyless_device: VirtualDevice,
    output: RecordingControllerOutput,
):
    joins_as_keyless_device(node, mdns)

    device_id = await controller.pair_device(ble_discovery, credentials=None)

    assert node.received == [HOME]
    assert device_id is not None
    assert device_id == controller.device_uuid(KEYLESS.mac), "the device takes the node's own identity"
    async with controller.platform.make_device_repository() as repo:
        device = await repo.get(device_id, as_=ESPhomeDevice)
        assert await repo.get(ble_discovery.id) is None, "the Hub's device moved to the node's identity"
    assert device is not None
    assert device.name == "Kitchen relay", "what the user chose is kept"
    assert device.integration_data.encryption_key and device.parameters
    assert (device.available, device.last_error) == (True, None)
    assert output.connected_devices == [device_id]
    assert controller.discoveries == {}
    assert device_id in output.lost_discoveries, "the node's mDNS discovery was paired through its BLE one"


async def test_a_node_waiting_for_its_button_tells_the_user_then_carries_on(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeImprovNode,
    output: RecordingControllerOutput,
):
    node.state = State.AUTHORIZATION_REQUIRED
    pairing = asyncio.create_task(controller.pair_device(ble_discovery, credentials=None))
    await wait_until(
        lambda: "button" in (controller.discoveries[ble_discovery.id].last_error or ""), 5, "the instruction"
    )

    await node.press_button()

    with pytest.raises(TimeoutError):  # the node is given Wi-Fi, but never shows up in this test
        await pairing
    assert node.received == [HOME]


async def test_a_button_never_pressed_fails_with_the_instruction(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeImprovNode, output: RecordingControllerOutput
):
    node.state = State.AUTHORIZATION_REQUIRED

    with pytest.raises(improv.SetupError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert node.received == []
    assert "press the button" in (await last_error(output, ble_discovery)).lower()
    await assert_not_paired(controller.platform, ble_discovery.id)
    assert leaked_tasks() == []


@pytest.mark.parametrize(
    ("error", "told"),
    [
        (Error.UNABLE_TO_CONNECT, "could not join the wi-fi network home"),
        (Error.NOT_AUTHORIZED, "press the button"),
        (Error.UNKNOWN_ERROR, "could not be set up"),
    ],
)
async def test_an_improv_error_fails_pairing_with_a_clear_reason(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeImprovNode,
    output: RecordingControllerOutput,
    error: Error,
    told: str,
):
    node.fails_with = error

    with pytest.raises(improv.SetupError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert told in (await last_error(output, ble_discovery)).lower()
    await assert_not_paired(controller.platform, ble_discovery.id)
    assert ble_discovery.id in controller.discoveries, "the user can try again"


async def test_a_wrong_wifi_password_is_reported_and_never_logged(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeImprovNode,
    output: RecordingControllerOutput,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG)
    node.password = "another password"

    with pytest.raises(improv.SetupError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert "wi-fi network home" in (await last_error(output, ble_discovery)).lower()
    assert HOME.password not in caplog.text
    assert HOME.password.encode().hex() not in caplog.text


async def test_a_node_out_of_ble_range_fails_with_a_clear_reason(
    controller: ESPhomeController, ble_discovery: Discovery, radio: FakeBLESource, output: RecordingControllerOutput
):
    radio.connection = None

    with pytest.raises(improv.SetupError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert "bluetooth" in (await last_error(output, ble_discovery)).lower()


async def test_bluetooth_held_by_another_integration_fails_with_a_clear_reason(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeImprovNode, output: RecordingControllerOutput
):
    async with controller.peripherals.instance("ble").exclusive(node.address, owner="matter"):
        with pytest.raises(improv.SetupError):
            await controller.pair_device(ble_discovery, credentials=None)

    assert node.received == []
    assert "busy" in (await last_error(output, ble_discovery)).lower()


async def test_a_node_heard_best_through_another_esphome_node_is_set_up_through_it(
    controller: ESPhomeController,
    node: FakeImprovNode,
    radio: FakeBLESource,
    mdns: FakeMDNS,
    keyless_device: VirtualDevice,
):
    proxy = FakeBLESource("esphome:a0b1c2d3e4f5", connection=node)
    router: BLEService = controller.peripherals.instance("ble")
    await router.add_source(proxy)
    radio.connection = None  # the Hub's adapter barely hears it: only the ESPHome node can connect
    await node.advertise(radio, rssi=-95)
    await node.advertise(proxy, rssi=-50)
    (discovery,) = controller.discoveries.values()
    await hub_creates_device(controller.platform, discovery)
    joins_as_keyless_device(node, mdns)

    assert await controller.pair_device(discovery, credentials=None) == controller.device_uuid(KEYLESS.mac)

    assert node.received == [HOME]
    assert (proxy.connected, radio.connected) == ([node.address], [])


async def test_a_node_that_never_shows_up_on_the_network_times_out_with_a_clear_reason(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeImprovNode, output: RecordingControllerOutput
):
    with pytest.raises(TimeoutError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert node.received == [HOME]
    assert "did not show up on the network" in await last_error(output, ble_discovery)
    await assert_not_paired(controller.platform, ble_discovery.id)
    assert leaked_tasks() == []


async def test_a_hub_without_wifi_cannot_set_up_the_node(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeImprovNode,
):
    """A Hub on Ethernet: told apart from any other failure, before the node is touched, so the app can ask for the
    Wi-Fi the node should join (the Hub saves it for the next attempt). The Hub returns the error to the app's pairing
    request, so the discovery's last_error is left as it was."""
    controller.platform = controller.platform.copy(context=Context())
    node.reachable = False  # any BLE step would fail with another error

    with pytest.raises(MissingCredentialsError) as raised:
        await controller.pair_device(ble_discovery, credentials=None)

    assert raised.value.type == MissingCredentialsError.WIFI
    assert node.received == []
    assert controller.discoveries[ble_discovery.id].last_error is None
    await assert_not_paired(controller.platform, ble_discovery.id)


async def test_the_reason_stays_on_the_discovery_while_the_node_keeps_advertising(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeImprovNode, radio: FakeBLESource
):
    node.fails_with = Error.UNKNOWN_ERROR
    with pytest.raises(improv.SetupError):
        await controller.pair_device(ble_discovery, credentials=None)

    await node.advertise(radio, named=False)  # a changed advert: the SDK passes it on

    assert controller.discoveries[ble_discovery.id].last_error


@pytest.mark.parametrize(
    ("ble_name", "node_name", "same"),
    [
        ("kitchen-relay", "kitchen-relay", True),
        ("kitchen-relay", "kitchen-relay-2", False),
        ("a-very-long-node-nam", "a-very-long-node-name", True),  # cut to 20 characters
        ("a-very-long-n-a1b2c3", "a-very-long-node-name-a1b2c3", True),  # cut, keeping the MAC suffix
        ("a-very-long-node-nam", "a-very-long-node-name-a1b2c3", True),  # cut without keeping the suffix
        ("a-very-long-n-ffffff", "a-very-long-node-name-a1b2c3", False),
    ],
)
def test_a_node_is_recognised_on_the_network_by_the_name_it_advertised(ble_name: str, node_name: str, same: bool):
    assert improv.is_node(ble_name, node_name) == same

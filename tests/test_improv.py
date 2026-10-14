"""Unprovisioned nodes over Improv BLE: found by their advert, given the Hub's Wi-Fi, then paired over the network.

ESPHome host nodes have no BLE, so the node's Improv GATT service is scripted below, behind bleak's client: the real
`py-improv-ble-client` runs against it. Once the node "joins" Wi-Fi, the compiled keyless virtual node appears over
mDNS.
"""

import asyncio
import dataclasses
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

import pytest
import pytest_asyncio
from bleak.backends.device import BLEDevice
from bleak.backends.scanner import AdvertisementData
from bleak.exc import BleakError
from improv_ble_client import SERVICE_DATA_UUID, SERVICE_UUID, Error, State
from improv_ble_client.protocol import (
    CHARACTERISTIC_UUID_CAPABILITIES,
    CHARACTERISTIC_UUID_ERROR,
    CHARACTERISTIC_UUID_RPC_RESULT,
    CHARACTERISTIC_UUID_STATE,
    WiFiSettingsCmd,
    WiFiSettingsRes,
)
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.discovery.ble_discovery import BLEDiscoveryInfo
from majordom_integration_sdk.schemas import CredentialsType
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome import controller as controller_module
from majordom_esphome import improv
from majordom_esphome.controller import NO_WIFI_CREDENTIALS, ESPhomeController, NoWiFiCredentialsError
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import (
    HubDependencies,
    WiFi,
    assert_not_paired,
    hub_creates_device,
    leaked_tasks,
    wait_until,
    with_wifi,
)
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import KEYLESS, VirtualDevice

pytestmark = pytest.mark.timeout(60)


HOME = WiFi("Home", "correct horse battery")


class FakeImprovNode:
    """An unprovisioned ESPHome node's Improv service, as py-improv-ble-client sees it through a bleak client."""

    def __init__(self, name: str = KEYLESS.name, address: str = "AA:BB:CC:DD:EE:01") -> None:
        self.name, self.address = name, address
        self.state = State.AUTHORIZED
        self.password = HOME.password  # of the network it can join
        self.fails_with: Error | None = None
        self.reachable = True
        self.received: list[WiFi] = []
        self.on_joined: Callable[[], Awaitable[None]] | None = None  # it shows up on the network
        self._handlers: dict[str, Callable[[Any, bytearray], Awaitable[None]]] = {}

    def advertisement(self, named: bool = True) -> AdvertisementData:
        return AdvertisementData(
            local_name=self.name if named else None,
            manufacturer_data={},
            service_data={SERVICE_DATA_UUID: bytes([self.state, 0, 0, 0, 0, 0])},
            service_uuids=[SERVICE_UUID],
            tx_power=None,
            rssi=-60,
            platform_data=(),
        )

    def info(self, named: bool = True) -> BLEDiscoveryInfo:
        return BLEDiscoveryInfo(BLEDevice(self.address, None, None), self.advertisement(named))

    async def press_button(self) -> None:
        self.state = State.AUTHORIZED
        await self._notify(CHARACTERISTIC_UUID_STATE, self.state)

    # bleak's client, as bleak-retry-connector's `establish_connection` returns it

    async def establish_connection(self, *args: Any, **kwargs: Any) -> "FakeImprovNode":
        if not self.reachable:
            raise BleakError("out of range")
        return self

    is_connected = True
    services = SimpleNamespace(get_characteristic=lambda uuid: SimpleNamespace(uuid=uuid))

    async def start_notify(self, uuid: str, handler: Callable[[Any, bytearray], Awaitable[None]]) -> None:
        self._handlers[uuid] = handler

    async def read_gatt_char(self, uuid: str) -> bytearray:
        values = {CHARACTERISTIC_UUID_STATE: self.state, CHARACTERISTIC_UUID_CAPABILITIES: 0}
        return bytearray([values.get(uuid, Error.NO_ERROR)])

    async def write_gatt_char(self, uuid: str, data: bytes, response: bool = True) -> None:
        command = WiFiSettingsCmd.from_bytes(data)
        asyncio.get_running_loop().create_task(self._join(WiFi(command.ssid.decode(), command.password.decode())))

    async def disconnect(self) -> None:
        pass

    async def clear_cache(self) -> None:
        pass

    async def _join(self, wifi: WiFi) -> None:
        if self.state == State.AUTHORIZATION_REQUIRED:
            await self._notify(CHARACTERISTIC_UUID_ERROR, Error.NOT_AUTHORIZED)
            return
        self.received.append(wifi)
        await self._notify(CHARACTERISTIC_UUID_STATE, State.PROVISIONING)
        error = self.fails_with or (Error.UNABLE_TO_CONNECT if wifi.password != self.password else None)
        if error is not None:
            await self._notify(CHARACTERISTIC_UUID_ERROR, error)
            await self._notify(CHARACTERISTIC_UUID_STATE, State.AUTHORIZED)
            return
        self.state = State.PROVISIONED
        await self._notify(CHARACTERISTIC_UUID_STATE, self.state)
        await self._handlers[CHARACTERISTIC_UUID_RPC_RESULT](
            SimpleNamespace(uuid=CHARACTERISTIC_UUID_RPC_RESULT),
            bytearray(WiFiSettingsRes(b"http://127.0.0.1", []).as_bytes()),
        )
        if self.on_joined is not None:
            await self.on_joined()

    async def _notify(self, uuid: str, value: int) -> None:
        if handler := self._handlers.get(uuid):
            await handler(SimpleNamespace(uuid=uuid), bytearray([value]))


@pytest.fixture
def hub_wifi() -> list[WiFi | None]:
    return [HOME]


@pytest_asyncio.fixture
async def deps(deps: AbstractController.Dependencies, hub_wifi: list[WiFi | None]) -> HubDependencies:
    async def wifi_credentials() -> WiFi | None:
        return hub_wifi[0]

    return with_wifi(deps, wifi_credentials)


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeImprovNode:
    fake = FakeImprovNode()
    monkeypatch.setattr("improv_ble_client.client.establish_connection", fake.establish_connection)
    monkeypatch.setattr(improv, "AUTHORIZATION_TIMEOUT", 1.0)
    monkeypatch.setattr(controller_module, "NODE_APPEAR_TIMEOUT", 1.0)
    return fake


async def advertise(controller: ESPhomeController, info: BLEDiscoveryInfo) -> None:
    """What the SDK's BLE scanner does with an advert: it reaches the listeners registered for its services."""
    await controller.dependencies.ble_discovery_service._handle_advertisement(info.device, info.advertisement)


@pytest_asyncio.fixture
async def ble_discovery(controller: ESPhomeController, node: FakeImprovNode) -> Discovery:
    await advertise(controller, node.info())
    (found,) = controller.discoveries.values()
    await hub_creates_device(controller.dependencies, found)
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
    controller: ESPhomeController, node: FakeImprovNode, output: RecordingControllerOutput
):
    await advertise(controller, node.info())

    (discovery,) = output.received_discoveries
    assert discovery.id == controller.device_uuid(f"improv:{node.name}")
    assert (discovery.transport, discovery.device_name) == ("ble", node.name)
    assert discovery.expected_credentials_options == [CredentialsType.none], "the Hub gives it its own Wi-Fi"


async def test_the_discovery_id_follows_the_name_not_the_ble_address(
    controller: ESPhomeController, node: FakeImprovNode
):
    await advertise(controller, node.info())
    first = set(controller.discoveries)
    await controller.ble_did_remove_device(controller.dependencies.ble_discovery_service, node.info())
    node.address = "AA:BB:CC:DD:EE:02"  # a random (resolvable private) address changes

    await advertise(controller, node.info())

    assert set(controller.discoveries) == first


async def test_the_name_is_remembered_for_the_adverts_that_do_not_carry_it(
    controller: ESPhomeController, node: FakeImprovNode, output: RecordingControllerOutput
):
    await advertise(controller, node.info(named=False))
    assert controller.discoveries == {}, "without its name the node has no stable identity yet"

    await advertise(controller, node.info())
    node.state = State.AUTHORIZATION_REQUIRED
    await advertise(controller, node.info(named=False))

    assert len(output.received_discoveries) == 1
    assert list(controller.discoveries) == [output.received_discoveries[0].id]


@pytest.mark.parametrize("state", [State.PROVISIONING, State.PROVISIONED])
async def test_a_node_that_already_has_wifi_is_not_offered(
    controller: ESPhomeController, node: FakeImprovNode, state: State
):
    node.state = state

    await advertise(controller, node.info())

    assert controller.discoveries == {}


async def test_a_node_that_stops_advertising_is_lost(
    controller: ESPhomeController, node: FakeImprovNode, output: RecordingControllerOutput
):
    await advertise(controller, node.info())

    await controller.ble_did_remove_device(controller.dependencies.ble_discovery_service, node.info(named=False))

    assert controller.discoveries == {}
    assert output.lost_discoveries == [output.received_discoveries[0].id]


async def test_ble_discovery_stops_with_the_controller(controller: ESPhomeController, node: FakeImprovNode):
    await controller.stop()

    await advertise(controller, node.info())

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
    async with controller.dependencies.make_device_repository() as repo:
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
    await assert_not_paired(controller.dependencies, ble_discovery.id)
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
    await assert_not_paired(controller.dependencies, ble_discovery.id)
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
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeImprovNode, output: RecordingControllerOutput
):
    node.reachable = False

    with pytest.raises(improv.SetupError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert "bluetooth" in (await last_error(output, ble_discovery)).lower()


async def test_a_node_that_never_shows_up_on_the_network_times_out_with_a_clear_reason(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeImprovNode, output: RecordingControllerOutput
):
    with pytest.raises(TimeoutError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert node.received == [HOME]
    assert "did not show up on the network" in await last_error(output, ble_discovery)
    await assert_not_paired(controller.dependencies, ble_discovery.id)
    assert leaked_tasks() == []


async def test_a_hub_without_wifi_cannot_set_up_the_node(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeImprovNode,
    output: RecordingControllerOutput,
    hub_wifi: list[WiFi | None],
):
    """A Hub on Ethernet: told apart from any other failure, before the node is touched, so the app can ask for the
    Wi-Fi the node should join (the Hub saves it for the next attempt)."""
    hub_wifi[0] = None
    node.reachable = False  # any BLE step would fail with another error

    with pytest.raises(NoWiFiCredentialsError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert node.received == []
    assert await last_error(output, ble_discovery) == NO_WIFI_CREDENTIALS
    await assert_not_paired(controller.dependencies, ble_discovery.id)


async def test_a_hub_whose_sdk_has_no_wifi_credentials_cannot_set_up_the_node_either(
    deps: HubDependencies, node: FakeImprovNode, output: RecordingControllerOutput
):
    controller = ESPhomeController(dataclasses.replace(deps, wifi_credentials=None))
    await controller.start()
    try:
        await advertise(controller, node.info())
        (found,) = controller.discoveries.values()
        await hub_creates_device(controller.dependencies, found)

        with pytest.raises(NoWiFiCredentialsError):
            await controller.pair_device(found, credentials=None)

        assert node.received == []
        assert await last_error(output, found) == NO_WIFI_CREDENTIALS
    finally:
        await controller.stop()


def test_no_wifi_credentials_is_a_lookup_error_the_hub_already_handles():
    assert issubclass(NoWiFiCredentialsError, LookupError)
    assert str(NoWiFiCredentialsError()) == NO_WIFI_CREDENTIALS
    assert NO_WIFI_CREDENTIALS.startswith("No Wi-Fi credentials found")


async def test_the_reason_stays_on_the_discovery_while_the_node_keeps_advertising(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeImprovNode
):
    node.fails_with = Error.UNKNOWN_ERROR
    with pytest.raises(improv.SetupError):
        await controller.pair_device(ble_discovery, credentials=None)

    await advertise(controller, node.info(named=False))  # a changed advert: the SDK passes it on

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

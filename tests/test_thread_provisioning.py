"""Unprovisioned Thread nodes over BLE: found by the `thread_provisioning` service, given the Hub's Thread dataset,
then paired over the network (Thread, through the border router) like any other node.

The node's GATT service is scripted below as the connection a Bluetooth source hands out; the SDK's real Bluetooth
router routes its adverts and connections. Once the node "attaches", the compiled keyless virtual node appears over
mDNS.
"""

import asyncio
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import replace

import pytest
import pytest_asyncio
from majordom_integration_sdk import MissingCredentialsError, Platform
from majordom_integration_sdk.capabilities import GattCharacteristic, GattService
from majordom_integration_sdk.peripherals import Peripherals
from majordom_integration_sdk.runtime.peripherals.providers.ble.router import BLEService
from majordom_integration_sdk.schemas import CredentialsType
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import FakeBLESource, RecordingControllerOutput

from majordom_esphome import controller as controller_module
from majordom_esphome import improv, thread_provisioning
from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from majordom_esphome.thread_provisioning import (
    ERROR_UUID,
    RESULT_UUID,
    RPC_UUID,
    SERVICE_UUID,
    STATE_UUID,
    Error,
    State,
)
from tests.helpers import assert_not_paired, hub_creates_device, leaked_tasks, wait_until
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import KEYLESS, VirtualDevice

pytestmark = pytest.mark.timeout(60)

NETWORK_KEY = "00112233445566778899aabbccddeeff"  # made up; what a real one must never be: logged
DATASET = (
    "0e080000000000010000"  # active timestamp
    "000300000f"  # channel 15
    "0102abcd"  # PAN ID
    "0208dead00beef00cafe"  # extended PAN ID
    "0708fd11ec6ad2330001"  # mesh-local prefix
    f"0510{NETWORK_KEY}"
    "030a4d616a6f72446f6d31"  # network name
)
HANDLES = {uuid: 0x21 + i for i, uuid in enumerate((STATE_UUID, ERROR_UUID, RPC_UUID, RESULT_UUID))}


class FakeThreadNode:
    """An unprovisioned ESPHome node's `thread_provisioning` GATT service, as the `BLEConnection` a source hands out."""

    def __init__(self, name: str = KEYLESS.name, address: str = "AA:BB:CC:DD:EE:03"):
        self.name, self.address = name, address
        self.state = State.AUTHORIZED
        self.mtu = 247
        self.fails_with: Error | None = None
        self.writes: list[bytes] = []
        self.received: list[str] = []  # datasets, hex
        self.on_attached: Callable[[], Awaitable[None]] | None = None  # it shows up on the network
        self._incoming = b""
        self._callbacks: dict[int, Callable[[bytes], None]] = {}

    async def advertise(self, source: FakeBLESource, named: bool = True, rssi: int = -60):
        source.advertise(self.address, rssi, self.name if named else None, [SERVICE_UUID])
        await asyncio.sleep(0.05)

    async def press_button(self):
        self.state = State.AUTHORIZED
        self._notify(STATE_UUID, self.state)

    # BLEConnection

    async def services(self) -> list[GattService]:
        characteristics = tuple(GattCharacteristic(uuid, handle, ("read",)) for uuid, handle in HANDLES.items())
        return [GattService(SERVICE_UUID, 0x20, characteristics)]

    async def read(self, handle: int) -> bytes:
        return bytes([self.state if handle == HANDLES[STATE_UUID] else Error.NONE])

    async def write(self, handle: int, data: bytes, is_response_needed: bool = True):
        assert handle == HANDLES[RPC_UUID] and is_response_needed
        assert len(data) <= self.mtu - 3, "a write must fit in one ATT packet"
        self.writes.append(data)
        self._incoming += data
        if len(self._incoming) < 3 or len(self._incoming) < self._incoming[1] + 3:
            return  # more chunks to come
        frame, self._incoming = self._incoming, b""
        assert frame[0] == thread_provisioning.SET_DATASET and sum(frame[:-1]) & 0xFF == frame[-1]
        asyncio.get_running_loop().create_task(self._attach(frame[2:-1].hex()))

    async def start_notify(self, handle: int, callback: Callable[[bytes], None]):
        self._callbacks[handle] = callback

    async def stop_notify(self, handle: int):
        self._callbacks.pop(handle, None)

    async def disconnect(self):
        self._callbacks.clear()

    async def _attach(self, dataset: str):
        if self.state == State.AWAITING_AUTHORIZATION:
            self._notify(ERROR_UUID, Error.NOT_AUTHORIZED)
            return
        self.received.append(dataset)
        self._notify(ERROR_UUID, Error.NONE)
        self._notify(STATE_UUID, State.ATTACHING)
        if self.fails_with is not None:
            self._notify(ERROR_UUID, self.fails_with)
            self._notify(STATE_UUID, State.AUTHORIZED)
            return
        self.state = State.ATTACHED
        self._notify(STATE_UUID, self.state)
        if callback := self._callbacks.get(HANDLES[RESULT_UUID]):
            callback(bytes([thread_provisioning.SET_DATASET, 0, thread_provisioning.SET_DATASET]))
        if self.on_attached is not None:
            await self.on_attached()

    def _notify(self, uuid: str, value: int):
        if callback := self._callbacks.get(HANDLES[uuid]):
            callback(bytes([value]))


@pytest.fixture
def node(monkeypatch: pytest.MonkeyPatch) -> FakeThreadNode:
    monkeypatch.setattr(thread_provisioning, "AUTHORIZATION_TIMEOUT", 1.0)
    monkeypatch.setattr(thread_provisioning, "LEASE_TIMEOUT", 0.5)
    monkeypatch.setattr(controller_module, "NODE_APPEAR_TIMEOUT", 1.0)
    return FakeThreadNode()


@pytest.fixture
def radio(node: FakeThreadNode) -> FakeBLESource:
    return FakeBLESource("hci0", connection=node)


@pytest.fixture
def platform(platform: Platform) -> Platform:
    return replace(platform, thread_dataset=DATASET)


@pytest_asyncio.fixture
async def router(radio: FakeBLESource) -> AsyncIterator[BLEService]:
    ble = BLEService(sources=[radio])
    await ble.start()
    yield ble
    await ble.stop()


@pytest.fixture
def peripherals(peripherals: Peripherals, router: BLEService) -> Peripherals:
    return Peripherals({**peripherals.bound, "ble": router.endpoint})


@pytest_asyncio.fixture
async def ble_discovery(controller: ESPhomeController, node: FakeThreadNode, radio: FakeBLESource) -> Discovery:
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


def attaches_as_keyless_device(node: FakeThreadNode, mdns: FakeMDNS):
    async def appear():
        await mdns.announce(Advert(name=KEYLESS.name, mac=KEYLESS.mac, port=KEYLESS.port, keyless=True))

    node.on_attached = appear


async def last_error(output: RecordingControllerOutput, discovery: Discovery) -> str:
    return next((d.last_error or "" for d in reversed(output.updated_discoveries) if d.id == discovery.id), "")


def test_a_command_is_an_improv_style_frame():
    assert thread_provisioning.frame(0x01, b"\x0e\x08") == bytes([0x01, 0x02, 0x0E, 0x08, 0x19])


async def test_a_node_waiting_for_a_thread_network_is_discovered_by_its_name(
    controller: ESPhomeController, node: FakeThreadNode, radio: FakeBLESource, output: RecordingControllerOutput
):
    await node.advertise(radio)

    (discovery,) = output.received_discoveries
    assert (discovery.transport, discovery.device_name) == ("ble", node.name)
    assert discovery.expected_credentials_options == [CredentialsType.none], "the Hub gives it its own network"


async def test_pairing_gives_the_node_the_hubs_thread_network_then_pairs_it_with_a_key_of_its_own(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeThreadNode,
    mdns: FakeMDNS,
    keyless_device: VirtualDevice,
    output: RecordingControllerOutput,
):
    attaches_as_keyless_device(node, mdns)

    device_id = await controller.pair_device(ble_discovery, credentials=None)

    assert node.received == [DATASET]
    assert device_id is not None
    assert device_id == controller.device_uuid(KEYLESS.mac), "the device takes the node's own identity"
    async with controller.platform.device_repository(controller) as repo:
        device = await repo.get(device_id, as_=ESPhomeDevice)
    assert device is not None and device.integration_data.encryption_key and device.parameters
    assert (device.available, device.last_error) == (True, None)
    assert output.connected_devices == [device_id]
    assert controller.discoveries == {}


async def test_the_dataset_is_written_in_chunks_that_fit_the_connection(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeThreadNode
):
    node.mtu = 23  # the BLE minimum: 20 bytes per write

    with pytest.raises(TimeoutError):  # attached, but never shows up in this test
        await controller.pair_device(ble_discovery, credentials=None)

    assert node.received == [DATASET]
    assert len(node.writes) > 1


async def test_a_hub_without_a_thread_network_asks_for_one_before_touching_the_node(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeThreadNode,
    radio: FakeBLESource,
):
    controller.platform = replace(controller.platform, thread_dataset=None)

    with pytest.raises(MissingCredentialsError) as raised:
        await controller.pair_device(ble_discovery, credentials=None)

    assert raised.value.type == MissingCredentialsError.THREAD
    assert radio.connected == [] and node.received == []


async def test_a_node_waiting_for_its_button_tells_the_user_then_carries_on(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeThreadNode
):
    node.state = State.AWAITING_AUTHORIZATION
    pairing = asyncio.create_task(controller.pair_device(ble_discovery, credentials=None))
    await wait_until(
        lambda: "button" in (controller.discoveries[ble_discovery.id].last_error or ""), 5, "the instruction"
    )

    await node.press_button()

    with pytest.raises(TimeoutError):
        await pairing
    assert node.received == [DATASET]


@pytest.mark.parametrize(
    ("error", "told"),
    [
        (Error.UNABLE_TO_ATTACH, "could not join the hub's thread network"),
        (Error.INVALID_DATASET, "refused the hub's thread network"),
        (Error.NOT_AUTHORIZED, "press the button"),
        (Error.UNKNOWN, "could not be set up"),
    ],
)
async def test_a_node_error_fails_pairing_with_a_clear_reason(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeThreadNode,
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
    assert leaked_tasks() == []


async def test_a_node_that_never_shows_up_on_the_network_times_out_with_a_clear_reason(
    controller: ESPhomeController, ble_discovery: Discovery, node: FakeThreadNode, output: RecordingControllerOutput
):
    with pytest.raises(TimeoutError):
        await controller.pair_device(ble_discovery, credentials=None)

    assert "joined the hub's thread network but did not show up" in (await last_error(output, ble_discovery)).lower()
    await assert_not_paired(controller.platform, ble_discovery.id)


async def test_the_network_key_is_never_logged(
    controller: ESPhomeController,
    ble_discovery: Discovery,
    node: FakeThreadNode,
    mdns: FakeMDNS,
    keyless_device: VirtualDevice,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG)
    attaches_as_keyless_device(node, mdns)

    await controller.pair_device(ble_discovery, credentials=None)

    assert NETWORK_KEY not in caplog.text.lower()
    assert DATASET not in caplog.text.lower()

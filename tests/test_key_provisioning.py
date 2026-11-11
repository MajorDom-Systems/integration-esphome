"""Keyless nodes (`api: encryption:` without a key) are given a key of the integration's own at pairing.

The key is generated, kept, then set over a Noise session with the well-known all-zeros PSK; from then on the node
requires it. The user never types (or sees) a key.
"""

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import UUID

import pytest
import pytest_asyncio
from aioesphomeapi import (
    ZERO_NOISE_PSK,
    APIClient,
    APIConnectionError,
    ButtonInfo,
    EntityInfo,
    InvalidEncryptionKeyAPIError,
)
from majordom_integration_sdk import Platform
from majordom_integration_sdk.schemas import CredentialsType, ProvidedCredentials
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import RecordingControllerOutput, build_test_platform

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import (
    assert_no_side_storage,
    assert_not_paired,
    draft_of,
    hub_pairs,
    leaked_tasks,
    param,
    user_picks,
    wait_for_value,
    wait_until,
)
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import KEYLESS, VirtualDevice

pytestmark = pytest.mark.timeout(60)

NEW_KEY = base64.b64encode(bytes(range(32))).decode()  # set in the YAML of a reflashed node
FACTORY_RESET_KEY = 77
button: Any = ButtonInfo  # its dataclass constructor is not typed
FACTORY_RESET: EntityInfo = button(object_id="factory_reset", key=FACTORY_RESET_KEY, name="Factory Reset")
KEYLESS_ADVERT = Advert(name=KEYLESS.name, mac=KEYLESS.mac, port=KEYLESS.port, keyless=True)


@pytest.fixture
def store() -> dict[UUID, dict[str, Any]]:
    """The Hub's device records: a test deletes one as the Hub does."""
    return {}


@pytest.fixture
def platform(store: dict[UUID, dict[str, Any]], tmp_path: Path) -> Platform:
    return build_test_platform(tmp_path, store=store)


@pytest_asyncio.fixture
async def keyless_device() -> AsyncIterator[VirtualDevice]:
    virtual = VirtualDevice(KEYLESS)
    await virtual.start()
    yield virtual
    await virtual.stop()


async def discover(controller: ESPhomeController, mdns: FakeMDNS, advert: Advert) -> Discovery:
    await mdns.announce(advert)
    (found,) = controller.discoveries.values()
    return found


@pytest_asyncio.fixture
async def keyless_discovery(controller: ESPhomeController, mdns: FakeMDNS, keyless_device: VirtualDevice) -> Discovery:
    found = await discover(controller, mdns, KEYLESS_ADVERT)
    user_picks(found)
    return found


async def stored(controller: ESPhomeController, device_id: Any) -> ESPhomeDevice:
    async with controller.platform.device_repository(controller) as repo:
        device = await repo.get(physical_id=device_id, as_=ESPhomeDevice)
    assert device is not None
    return device


def assert_is_a_key(key: str | None):
    assert key is not None and key != ZERO_NOISE_PSK
    assert len(base64.b64decode(key, validate=True)) == 32


async def connects(device: VirtualDevice, psk: str | None) -> bool:
    client = APIClient(device.address, device.port, "", noise_psk=psk)
    try:
        async with asyncio.timeout(5):
            await client.connect(login=True)
    except (APIConnectionError, TimeoutError):
        return False
    finally:
        await client.disconnect()
    return True


# Against the compiled keyless node


async def test_keyless_node_needs_no_credentials(keyless_discovery: Discovery):
    assert keyless_discovery.expected_credentials_options == [CredentialsType.none]


async def test_pairing_gives_the_node_a_key_it_requires_from_then_on(
    controller: ESPhomeController,
    keyless_discovery: Discovery,
    keyless_device: VirtualDevice,
    output: RecordingControllerOutput,
):
    assert await connects(keyless_device, None), "a keyless node accepts plaintext until it has a key"

    await hub_pairs(controller, keyless_discovery, credentials=None)

    device = await stored(controller, keyless_discovery.id)
    key = device.integration_data.encryption_key
    assert_is_a_key(key)
    assert (device.available, device.last_error) == (True, None)
    await wait_for_value(output, param(device, "Temperature"), lambda v: v == 21.5)
    assert not await connects(keyless_device, None), "plaintext must be refused once the node has its key"
    assert not await connects(keyless_device, ZERO_NOISE_PSK), "the zero PSK must be refused once the node has its key"
    assert await connects(keyless_device, key)


async def test_the_node_keeps_its_key_across_its_restart_and_the_hubs(
    controller: ESPhomeController,
    keyless_discovery: Discovery,
    keyless_device: VirtualDevice,
    output: RecordingControllerOutput,
):
    await hub_pairs(controller, keyless_discovery, credentials=None)
    await keyless_device.restart()
    await wait_until(lambda: output.connected_devices.count(keyless_discovery.id) == 2, 15, "the node to reconnect")

    await controller.stop()
    output.connected_devices.clear()
    restarted = ESPhomeController(controller.platform)
    restarted.peripherals = controller.peripherals
    restarted.output = controller.output
    await restarted.start()
    try:
        await wait_until(lambda: keyless_discovery.id in output.connected_devices, 15, "the Hub to reconnect")
    finally:
        await restarted.stop()


async def test_the_generated_key_is_never_logged(
    controller: ESPhomeController, keyless_discovery: Discovery, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    await hub_pairs(controller, keyless_discovery, credentials=None)

    key = (await stored(controller, keyless_discovery.id)).integration_data.encryption_key
    assert key and key not in caplog.text


async def test_a_node_without_encryption_still_pairs_in_plaintext_with_a_warning(
    controller: ESPhomeController, discovery: Discovery, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.WARNING)

    await hub_pairs(controller, discovery, credentials=None)

    assert (await stored(controller, discovery.id)).integration_data.encryption_key is None
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert any("api: encryption:" in w for w in warnings), "the node's owner should be told how to encrypt it"


# Failure paths, against a scripted node behind the library's client


@dataclass
class FakeNode:
    """Just enough of a keyless node's native API to be given a key, refuse it, or be unreachable afterwards."""

    accepts_key: bool = True
    reachable_with_key: bool = True
    key: str | None = None
    set_key_sessions: list[str | None] = field(default_factory=list)  # the PSK of every session a key was sent over
    failed_disconnect_requests: int = 0  # sent over a session the node already dropped: aioesphomeapi logs an ERROR
    fails_listing: int = 0  # entity listings that fail before one succeeds
    resets: int = 0

    def client(self, address: str, port: int, password: str | None, noise_psk: str | None = None) -> "FakeClient":
        return FakeClient(self, noise_psk)


class FakeClient:
    def __init__(self, node: FakeNode, psk: str | None):
        self.node, self.psk = node, psk
        self.on_stop: Callable[[bool], Coroutine[Any, Any, None]] | None = None
        self.dropped = False

    async def connect(self, login: bool = False, on_stop: Callable[[bool], Coroutine[Any, Any, None]] | None = None):
        self.on_stop = on_stop
        if self.psk != self.node.key and not (self.node.key is None and self.psk == ZERO_NOISE_PSK):
            raise InvalidEncryptionKeyAPIError("handshake failed")
        if self.node.key is not None and not self.node.reachable_with_key:
            raise APIConnectionError("connection refused")

    async def noise_encryption_set_key(self, key: bytes) -> bool:
        self.node.set_key_sessions.append(self.psk)
        if self.node.accepts_key:
            self.node.key = key.decode()
            self.dropped = True
            if self.on_stop is not None:  # like a real node: it drops every session once it uses the new key
                asyncio.get_running_loop().call_later(0.1, asyncio.ensure_future, self.on_stop(False))
        return self.node.accepts_key

    async def list_entities_services(self) -> tuple[list[EntityInfo], list[Any]]:
        if self.node.fails_listing:
            self.node.fails_listing -= 1
            raise APIConnectionError("listing failed")
        return [FACTORY_RESET], []

    def button_command(self, key: int):
        assert key == FACTORY_RESET_KEY
        self.node.key = None
        self.node.resets += 1

    def subscribe_states(self, on_state: Callable[..., None]):
        pass

    async def disconnect(self):
        if self.dropped:
            self.node.failed_disconnect_requests += 1

    def force_disconnect(self):
        pass


@pytest.fixture
def node() -> Any:
    fake = FakeNode()
    with patch("majordom_esphome.connection.APIClient", fake.client):
        yield fake


async def test_older_firmware_is_given_its_key_in_plaintext_newer_over_the_zero_psk(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode
):
    newer = await discover(controller, mdns, Advert(keyless=True))
    user_picks(newer)
    await hub_pairs(controller, newer, credentials=None)
    await controller.unpair(await stored(controller, newer.id))
    node.key = None
    older = await discover(controller, mdns, Advert(keyless=True, zero_psk=False, addresses=["127.0.0.2"]))

    await hub_pairs(controller, older, credentials=None)

    assert node.set_key_sessions == [ZERO_NOISE_PSK, None]


async def test_the_session_the_node_drops_after_taking_its_key_is_closed_without_a_disconnect_request(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode
):
    found = await discover(controller, mdns, Advert(keyless=True))
    user_picks(found)

    await hub_pairs(controller, found, credentials=None)

    assert node.key is not None
    assert node.failed_disconnect_requests == 0


async def test_a_refused_key_fails_pairing_cleanly(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode, output: RecordingControllerOutput
):
    node.accepts_key = False
    found = await discover(controller, mdns, Advert(keyless=True))
    user_picks(found)

    with pytest.raises(ConnectionError):
        await hub_pairs(controller, found, credentials=None)

    assert node.key is None
    await assert_not_paired(controller.platform, found.id)
    assert output.connected_devices == []
    assert "key" in (output.updated_discoveries[-1].last_error or "").lower(), "the user must be told why"
    assert found.id in controller.discoveries
    assert leaked_tasks() == []


async def test_a_pairing_failing_after_the_key_is_set_is_rolled_back_by_a_factory_reset(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode
):
    """The key lives only in the draft the Hub drops: the node must pair again keyless."""
    node.fails_listing = 2  # at the hostname, then at the IP
    found = await discover(controller, mdns, Advert(keyless=True))
    user_picks(found)

    with pytest.raises(ConnectionError):
        await hub_pairs(controller, found, credentials=None)

    assert (node.key, node.resets) == (None, 1)
    await assert_not_paired(controller.platform, found.id)
    reset = await discover(controller, mdns, Advert(keyless=True, addresses=["127.0.0.2"]))  # announced anew
    await hub_pairs(controller, reset, credentials=None)
    assert node.set_key_sessions == [ZERO_NOISE_PSK, ZERO_NOISE_PSK]


async def test_a_pairing_the_hub_fails_to_save_is_rolled_back_by_a_factory_reset(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode
):
    found = await discover(controller, mdns, Advert(keyless=True))
    user_picks(found)
    draft = await controller.pair_device(found, draft_of(found), None)
    assert node.key is not None

    await controller.unpair(draft)

    assert (node.key, node.resets) == (None, 1)
    assert controller._just_paired == {}


async def test_a_keyed_node_the_hub_never_saves_is_factory_reset_when_let_go(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr("majordom_esphome.controller.UNSAVED_PAIRING_TIMEOUT", 0.01)
    found = await discover(controller, mdns, Advert(keyless=True))
    await controller.pair_device(found, draft_of(found), None)

    await wait_until(lambda: controller._just_paired == {}, 5, "the node to be let go")

    assert (node.key, node.resets) == (None, 1)


async def test_a_paired_node_keeps_its_key_in_its_record_alone(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode
):
    found = await discover(controller, mdns, Advert(keyless=True))
    user_picks(found)

    await hub_pairs(controller, found, credentials=None)

    assert (await stored(controller, found.id)).integration_data.encryption_key == node.key
    assert node.resets == 0
    assert_no_side_storage(controller.platform)


async def test_unpairing_a_saved_node_factory_resets_it(controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode):
    found = await discover(controller, mdns, Advert(keyless=True))
    user_picks(found)
    await hub_pairs(controller, found, credentials=None)

    await controller.unpair(await stored(controller, found.id))

    assert (node.key, node.resets) == (None, 1)


async def test_a_node_reflashed_with_a_key_of_its_own_after_the_hub_kept_one_can_be_paired_with_the_new_key(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode, output: RecordingControllerOutput
):
    """Paired with the Hub's key, unpaired (the Hub keeps the device record, and so the key), then reflashed with a key
    in its YAML: the kept key is refused, so the user must be able to enter the new one."""
    found = await discover(controller, mdns, Advert(keyless=True))
    user_picks(found)
    await hub_pairs(controller, found, credentials=None)
    kept = (await stored(controller, found.id)).integration_data.encryption_key
    await controller.unpair(await stored(controller, found.id))
    node.key = NEW_KEY

    again = await discover(controller, mdns, Advert(encrypted=True))

    assert again.expected_credentials_options == [CredentialsType.none, CredentialsType.secret]
    with pytest.raises(ConnectionError):  # the kept key is the wrong one now
        await hub_pairs(controller, again, credentials=None)
    assert "key" in (output.updated_discoveries[-1].last_error or "").lower()
    await hub_pairs(
        controller, controller.discoveries[again.id], ProvidedCredentials(type=CredentialsType.secret, value=NEW_KEY)
    )
    device = await stored(controller, found.id)
    assert device.integration_data.encryption_key == NEW_KEY != kept
    assert (device.available, device.last_error) == (True, None)
    assert node.set_key_sessions == [ZERO_NOISE_PSK], "a node with a key of its own is not given ours"

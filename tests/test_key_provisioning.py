"""Keyless nodes (`api: encryption:` without a key) are given a key of the integration's own at pairing.

The key is generated, kept, then set over a Noise session with the well-known all-zeros PSK; from then on the node
requires it. The user never types (or sees) a key.
"""

import asyncio
import base64
import logging
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest
import pytest_asyncio
from aioesphomeapi import (
    ZERO_NOISE_PSK,
    APIClient,
    APIConnectionError,
    EntityInfo,
    InvalidEncryptionKeyAPIError,
)
from majordom_integration_sdk.schemas import CredentialsType, ProvidedCredentials
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import assert_not_paired, hub_creates_device, leaked_tasks, param, wait_for_value, wait_until
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import KEYLESS, VirtualDevice

pytestmark = pytest.mark.timeout(60)

NEW_KEY = base64.b64encode(bytes(range(32))).decode()  # set in the YAML of a reflashed node
KEYLESS_ADVERT = Advert(name=KEYLESS.name, mac=KEYLESS.mac, port=KEYLESS.port, keyless=True)


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
    await hub_creates_device(controller.platform, found)
    return found


async def stored(controller: ESPhomeController, device_id: Any) -> ESPhomeDevice:
    async with controller.platform.make_device_repository() as repo:
        device = await repo.get(device_id, as_=ESPhomeDevice)
    assert device is not None
    return device


def assert_is_a_key(key: str | None) -> None:
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

    await controller.pair_device(keyless_discovery, credentials=None)

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
    await controller.pair_device(keyless_discovery, credentials=None)
    await keyless_device.restart()
    await wait_until(lambda: output.connected_devices.count(keyless_discovery.id) == 2, 15, "the node to reconnect")

    await controller.stop()
    output.connected_devices.clear()
    restarted = ESPhomeController(controller.platform, controller.peripherals)
    await restarted.start()
    try:
        await wait_until(lambda: keyless_discovery.id in output.connected_devices, 15, "the Hub to reconnect")
    finally:
        await restarted.stop()


async def test_the_generated_key_is_never_logged(
    controller: ESPhomeController, keyless_discovery: Discovery, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    await controller.pair_device(keyless_discovery, credentials=None)

    key = (await stored(controller, keyless_discovery.id)).integration_data.encryption_key
    assert key and key not in caplog.text


async def test_a_node_without_encryption_still_pairs_in_plaintext_with_a_warning(
    controller: ESPhomeController, discovery: Discovery, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.WARNING)

    await controller.pair_device(discovery, credentials=None)

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

    def client(self, address: str, port: int, password: str | None, noise_psk: str | None = None) -> "FakeClient":
        return FakeClient(self, noise_psk)


class FakeClient:
    def __init__(self, node: FakeNode, psk: str | None) -> None:
        self.node, self.psk = node, psk
        self.on_stop: Callable[[bool], Coroutine[Any, Any, None]] | None = None

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
            if self.on_stop is not None:  # like a real node: it drops every session once it uses the new key
                asyncio.get_running_loop().call_later(0.1, asyncio.ensure_future, self.on_stop(False))
        return self.node.accepts_key

    async def list_entities_services(self) -> tuple[list[EntityInfo], list[Any]]:
        return [], []

    def subscribe_states(self, on_state: Callable[..., None]) -> None:
        pass

    async def disconnect(self) -> None:
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
    await hub_creates_device(controller.platform, newer)
    await controller.pair_device(newer, credentials=None)
    await controller.unpair(await stored(controller, newer.id))
    node.key = None
    older = await discover(controller, mdns, Advert(keyless=True, zero_psk=False, addresses=["127.0.0.2"]))

    await controller.pair_device(older, credentials=None)

    assert node.set_key_sessions == [ZERO_NOISE_PSK, None]


async def test_a_refused_key_fails_pairing_cleanly(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode, output: RecordingControllerOutput
):
    node.accepts_key = False
    found = await discover(controller, mdns, Advert(keyless=True))
    await hub_creates_device(controller.platform, found)

    with pytest.raises(ConnectionError):
        await controller.pair_device(found, credentials=None)

    assert node.key is None
    await assert_not_paired(controller.platform, found.id, kept_key=True)
    assert output.connected_devices == []
    assert "key" in (output.updated_discoveries[-1].last_error or "").lower(), "the user must be told why"
    assert found.id in controller.discoveries
    assert leaked_tasks() == []


async def test_a_key_set_before_a_failed_reconnect_is_kept_for_the_retry_even_across_a_hub_restart(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode, output: RecordingControllerOutput
):
    node.reachable_with_key = False
    found = await discover(controller, mdns, Advert(keyless=True))
    await hub_creates_device(controller.platform, found)
    with pytest.raises(ConnectionError):
        await controller.pair_device(found, credentials=None)
    await assert_not_paired(controller.platform, found.id, kept_key=True)
    assert (await stored(controller, found.id)).integration_data.encryption_key == node.key
    assert output.updated_discoveries[-1].last_error
    await controller.stop()
    restarted = ESPhomeController(controller.platform, controller.peripherals)
    await restarted.start()
    try:
        node.reachable_with_key = True
        # the node now announces a key: most likely ours, so the user need not type one (but may: see below)
        again = await discover(restarted, mdns, Advert(encrypted=True))
        assert again.expected_credentials_options == [CredentialsType.none, CredentialsType.secret]

        await restarted.pair_device(again, credentials=None)

        device = await stored(restarted, found.id)
        assert device.integration_data.encryption_key == node.key
        assert (device.available, device.last_error) == (True, None)
        assert node.set_key_sessions == [ZERO_NOISE_PSK], "the node already has the key: it is not set twice"
        assert restarted.discoveries == {}
    finally:
        await restarted.stop()


async def test_a_retry_before_the_node_announces_its_key_does_not_set_it_again(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode
):
    node.reachable_with_key = False
    found = await discover(controller, mdns, Advert(keyless=True))
    await hub_creates_device(controller.platform, found)
    with pytest.raises(ConnectionError):
        await controller.pair_device(found, credentials=None)
    node.reachable_with_key = True

    await controller.pair_device(controller.discoveries[found.id], credentials=None)  # mDNS still says keyless

    assert node.set_key_sessions == [ZERO_NOISE_PSK]
    assert (await stored(controller, found.id)).integration_data.encryption_key == node.key


async def test_a_node_reflashed_with_a_key_of_its_own_after_the_hub_kept_one_can_be_paired_with_the_new_key(
    controller: ESPhomeController, mdns: FakeMDNS, node: FakeNode, output: RecordingControllerOutput
):
    """Paired with the Hub's key, unpaired (the Hub keeps the device record, and so the key), then reflashed with a key
    in its YAML: the kept key is refused, so the user must be able to enter the new one."""
    found = await discover(controller, mdns, Advert(keyless=True))
    await hub_creates_device(controller.platform, found)
    await controller.pair_device(found, credentials=None)
    kept = (await stored(controller, found.id)).integration_data.encryption_key
    await controller.unpair(await stored(controller, found.id))
    node.key = NEW_KEY

    again = await discover(controller, mdns, Advert(encrypted=True))

    assert again.expected_credentials_options == [CredentialsType.none, CredentialsType.secret]
    with pytest.raises(ConnectionError):  # the kept key is the wrong one now
        await controller.pair_device(again, credentials=None)
    assert "key" in (output.updated_discoveries[-1].last_error or "").lower()
    await controller.pair_device(
        controller.discoveries[again.id], ProvidedCredentials(type=CredentialsType.secret, value=NEW_KEY)
    )
    device = await stored(controller, found.id)
    assert device.integration_data.encryption_key == NEW_KEY != kept
    assert (device.available, device.last_error) == (True, None)
    assert node.set_key_sessions == [ZERO_NOISE_PSK], "a node with a key of its own is not given ours"

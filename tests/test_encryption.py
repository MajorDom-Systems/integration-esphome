"""Devices with Noise API encryption: the key is requested, validated, used, kept and never leaked."""

import logging

import pytest
from majordom_integration_sdk.schemas import CredentialsType, ProvidedCredentials
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import assert_not_paired, hub_creates_device, leaked_tasks, param, wait_for_value, wait_until
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import ENCRYPTION_KEY as KEY
from tests.virtual.runner import VirtualDevice

WRONG_KEY = "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA="


@pytest.fixture
async def encrypted_discovery(
    controller: ESPhomeController, mdns: FakeMDNS, encrypted_device: VirtualDevice
) -> Discovery:
    await mdns.announce(Advert(name="test_node_enc", port=encrypted_device.port, encrypted=True))
    (found,) = controller.discoveries.values()
    await hub_creates_device(controller.dependencies, found)
    return found


def secret(value: str | None) -> ProvidedCredentials:
    return ProvidedCredentials(type=CredentialsType.secret, value=value)


async def test_discovery_asks_for_the_key(encrypted_discovery: Discovery):
    assert encrypted_discovery.expected_credentials_options == [CredentialsType.secret]


@pytest.mark.parametrize("credentials", [None, ProvidedCredentials(type=CredentialsType.none)])
async def test_pairing_without_the_key_is_refused(
    controller: ESPhomeController, encrypted_discovery: Discovery, credentials: ProvidedCredentials | None
):
    with pytest.raises(ValueError):
        await controller.pair_device(encrypted_discovery, credentials)

    await assert_not_paired(controller.dependencies, encrypted_discovery.id)


async def test_pairing_with_a_code_instead_of_the_key_is_refused(
    controller: ESPhomeController, encrypted_discovery: Discovery
):
    with pytest.raises(ValueError):
        await controller.pair_device(encrypted_discovery, ProvidedCredentials(type=CredentialsType.code, value=KEY))


@pytest.mark.timeout(60)
async def test_pairing_with_a_wrong_key_fails_cleanly(
    controller: ESPhomeController, encrypted_discovery: Discovery, output: RecordingControllerOutput
):
    with pytest.raises((ConnectionError, TimeoutError, ValueError)):
        await controller.pair_device(encrypted_discovery, secret(WRONG_KEY))

    await assert_not_paired(controller.dependencies, encrypted_discovery.id)
    assert output.connected_devices == []
    assert "key" in (output.updated_discoveries[-1].last_error or "").lower(), (
        "the user must be told the key was rejected"
    )
    assert leaked_tasks() == []


async def test_pairing_with_the_key_works_like_a_plain_device(
    controller: ESPhomeController, encrypted_discovery: Discovery, output: RecordingControllerOutput
):
    await controller.pair_device(encrypted_discovery, secret(KEY))

    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(encrypted_discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    assert len(stored.parameters) == 33
    assert stored.integration_data.encryption_key == KEY
    await wait_for_value(output, param(stored, "Temperature"), lambda v: v == 21.5)


@pytest.mark.timeout(60)
async def test_encrypted_device_reconnects_after_hub_restart(
    controller: ESPhomeController, encrypted_discovery: Discovery, output: RecordingControllerOutput
):
    await controller.pair_device(encrypted_discovery, secret(KEY))
    await controller.stop()
    output.connected_devices.clear()
    restarted = ESPhomeController(controller.dependencies)

    await restarted.start()
    try:
        await wait_until(
            lambda: encrypted_discovery.id in output.connected_devices, 15, "the encrypted device to reconnect"
        )
    finally:
        await restarted.stop()


async def test_the_key_is_never_logged(
    controller: ESPhomeController, encrypted_discovery: Discovery, caplog: pytest.LogCaptureFixture
):
    caplog.set_level(logging.DEBUG)

    await controller.pair_device(encrypted_discovery, secret(KEY))
    with pytest.raises((ConnectionError, TimeoutError, ValueError)):
        await controller.pair_device(encrypted_discovery, secret(WRONG_KEY))

    assert KEY not in caplog.text
    assert WRONG_KEY not in caplog.text

"""Controller behavior with the device connection mocked: pairing, commands, unpairing, error paths.

Entities are real `aioesphomeapi` descriptions (what a device actually reports), not hand-made mocks.
"""

from typing import Any
from uuid import UUID

import pytest
from aioesphomeapi import CameraInfo, LightInfo, SwitchInfo
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.parameter import ParameterDataType, ParameterRole
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import assert_not_paired, hub_creates_device, param
from tests.virtual.mdns import Advert, FakeMDNS

SWITCH_KEY = 7  # deliberately different from every other number in the tests


@pytest.fixture
async def mock_discovery(controller: ESPhomeController, mdns: FakeMDNS) -> Discovery:
    await mdns.announce(Advert())
    (found,) = controller.discoveries.values()
    await hub_creates_device(controller.dependencies, found)
    return found


@pytest.fixture
async def mock_paired(controller: ESPhomeController, mock_discovery: Discovery, mock_connection: Any, make_entity: Any):
    _, connection = mock_connection
    connection.get_entities.return_value = {
        SWITCH_KEY: make_entity(SwitchInfo, SWITCH_KEY, "Relay"),
        11: make_entity(LightInfo, 11, "Lamp", supported_color_modes=[3]),
    }
    await controller.pair_device(mock_discovery, credentials=None)
    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(mock_discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    return stored


async def test_pairing_stores_a_parameter_per_entity_capability(
    mock_paired: ESPhomeDevice, mock_connection: Any, output: RecordingControllerOutput, controller: ESPhomeController
):
    _, connection = mock_connection

    relay = param(mock_paired, "Relay")
    assert (relay.data_type, relay.role) == (ParameterDataType.bool, ParameterRole.control)
    assert param(mock_paired, "Lamp", "state").data_type == ParameterDataType.bool
    assert param(mock_paired, "Lamp", "brightness").data_type == ParameterDataType.decimal
    assert mock_paired.id in output.connected_devices
    assert mock_paired.id not in controller.discoveries
    connection.start.assert_awaited_once()
    connection.wait_ready.assert_awaited_once()


async def test_pairing_skips_entities_it_cannot_map(
    controller: ESPhomeController, mock_discovery: Discovery, mock_connection: Any, make_entity: Any
):
    _, connection = mock_connection
    connection.get_entities.return_value = {
        1: make_entity(CameraInfo, 1, "Cam"),
        2: make_entity(SwitchInfo, 2, "Relay"),
    }

    await controller.pair_device(mock_discovery, credentials=None)

    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(mock_discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    assert {p.integration_data.entity_name for p in stored.parameters} == {"Relay"}


async def test_failed_pairing_is_cleaned_up_and_reported(
    controller: ESPhomeController,
    mock_discovery: Discovery,
    mock_connection: Any,
    output: RecordingControllerOutput,
):
    _, connection = mock_connection
    connection.wait_ready.side_effect = TimeoutError("no answer")

    with pytest.raises(TimeoutError):
        await controller.pair_device(mock_discovery, credentials=None)

    connection.stop.assert_awaited_once()
    assert mock_discovery.id in controller.discoveries
    await assert_not_paired(controller.dependencies, mock_discovery.id)
    assert output.updated_discoveries[-1].last_error, "the user must see why pairing failed"
    assert output.connected_devices == []


async def test_command_reaches_the_connection_with_the_entity_key(
    controller: ESPhomeController, mock_paired: ESPhomeDevice, mock_connection: Any
):
    _, connection = mock_connection
    relay = param(mock_paired, "Relay")

    await controller.send_command(
        DeviceCommand(device_id=mock_paired.id, parameter_id=relay.id, value=True), mock_paired, relay
    )

    connection.send_command.assert_awaited_once_with(SWITCH_KEY, "switch", {"key": SWITCH_KEY, "state": True})


async def test_command_to_disconnected_device_fails_visibly(
    controller: ESPhomeController, mock_paired: ESPhomeDevice, mock_connection: Any
):
    await controller.unpair(mock_paired)
    relay = param(mock_paired, "Relay")

    with pytest.raises(ConnectionError):
        await controller.send_command(
            DeviceCommand(device_id=mock_paired.id, parameter_id=relay.id, value=True), mock_paired, relay
        )

    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(mock_paired.id, as_=ESPhomeDevice)
    assert stored is not None
    assert stored.last_error, "a failed command must leave a user-readable last_error on the device"


async def test_command_on_read_only_parameter_is_rejected(
    controller: ESPhomeController, mock_discovery: Discovery, mock_connection: Any, make_entity: Any
):
    from aioesphomeapi import SensorInfo

    _, connection = mock_connection
    connection.get_entities.return_value = {3: make_entity(SensorInfo, 3, "Temperature")}
    await controller.pair_device(mock_discovery, credentials=None)
    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(mock_discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    temperature = param(stored, "Temperature")

    with pytest.raises(ValueError, match="read-only"):
        await controller.send_command(
            DeviceCommand(device_id=stored.id, parameter_id=temperature.id, value=1.0), stored, temperature
        )

    connection.send_command.assert_not_awaited()


async def test_unpair_closes_the_connection(
    controller: ESPhomeController, mock_paired: ESPhomeDevice, mock_connection: Any
):
    _, connection = mock_connection

    await controller.unpair(mock_paired)

    connection.stop.assert_awaited_once()
    with pytest.raises(ConnectionError):
        await controller.fetch(mock_paired)


async def test_identify_keeps_the_connection(
    controller: ESPhomeController, mock_paired: ESPhomeDevice, mock_connection: Any
):
    _, connection = mock_connection

    await controller.identify(mock_paired)  # ESPHome has no identify action: nothing to send, nothing may break

    connection.stop.assert_not_awaited()
    await controller.fetch(mock_paired)


async def test_fetch_of_unknown_device_raises(controller: ESPhomeController, mock_paired: ESPhomeDevice):
    stranger = mock_paired.model_copy(update={"id": UUID(int=1)})

    with pytest.raises(ConnectionError):
        await controller.fetch(stranger)

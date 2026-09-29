"""Integration tests for the ESPHome controller."""

from unittest.mock import MagicMock
from uuid import NAMESPACE_DNS, UUID, uuid5

import pytest
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterVisibility,
)

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import (
    ESPhomeComponentType,
    ESPhomeDevice,
    ESPhomeParameter,
    ESPhomeParameterIntegrationData,
    ESPhomeParameterType,
)


@pytest.mark.asyncio
async def test_pairs_a_discovered_device(
    controller: ESPhomeController,
    deps,
    sample_discovery,
    mock_connection,
    fake_entity,
):
    discovery = sample_discovery
    device_id = discovery.id

    controller._discoveries[device_id] = discovery
    controller._discovery_data[device_id] = {
        "address": "127.0.0.1",
        "port": 6053,
        "requires_encryption": False,
    }

    mock_cls, mock_inst = mock_connection
    ent1 = fake_entity(key=1, name="switch1", component_type="switch")
    ent2 = fake_entity(key=2, name="light1", component_type="light")
    mock_inst.get_entities.return_value = {1: ent1, 2: ent2}

    await controller.pair_device(discovery, credentials=None)

    assert device_id in deps.output.connected_devices

    async with deps.make_device_repository() as repo:
        stored = await repo.get(device_id, as_=ESPhomeDevice)
    assert stored is not None
    assert stored.integration_data.address == "127.0.0.1"
    assert len(stored.parameters) > 0
    assert any(p.role == ParameterRole.control for p in stored.parameters)

    mock_inst.start.assert_awaited_once()
    mock_inst.wait_ready.assert_awaited_once()
    assert device_id not in controller._discoveries


@pytest.mark.asyncio
async def test_fetches_state(
    controller: ESPhomeController,
    deps,
    paired_device,
    mock_connection,
):
    device = paired_device
    device_id = device.id

    _, mock_inst = mock_connection
    async with controller._lock:
        controller._connections[device_id] = mock_inst

    state_obj = MagicMock()
    state_obj.state = True
    await controller._on_state(
        device_id=device_id,
        entity_name="switch1",
        component_type="switch",
        state_obj=state_obj,
    )

    assert len(deps.output.events) > 0
    event = deps.output.events[0]
    assert event.device_id == device_id
    assert event.value is True

    cache = controller._state_cache.get(device_id, {})
    assert cache.get("switch1_state") is True


@pytest.mark.asyncio
async def test_sends_a_command(
    controller: ESPhomeController,
    deps,
    paired_device,
    mock_connection,
):
    device = paired_device
    device_id = device.id

    param_id = uuid5(NAMESPACE_DNS, f"{device_id}_switch1_state")
    parameter = ESPhomeParameter(
        id=param_id,
        name="switch1_state",
        data_type=ParameterDataType.bool,
        role=ParameterRole.control,
        visibility=ParameterVisibility.user,
        integration_data=ESPhomeParameterIntegrationData(
            entity_name="switch1",
            component_type=ESPhomeComponentType.SWITCH,
            parameter_type=ESPhomeParameterType.STATE,
            service_key=1,
            sub_field="state",
        ),
    )
    async with deps.make_device_repository() as repo:
        stored = await repo.get(device_id, as_=ESPhomeDevice)
        stored.parameters = [parameter]
        await repo.save(stored)

    _, mock_inst = mock_connection
    async with controller._lock:
        controller._connections[device_id] = mock_inst

    command = DeviceCommand(
        device_id=device_id,
        parameter_id=param_id,
        value=True,
    )

    await controller.send_command(command, stored, parameter)

    mock_inst.send_command.assert_awaited_once()
    call_args = mock_inst.send_command.call_args[0]
    assert call_args[0] == 1
    assert call_args[1] == "switch"
    assert call_args[2] == {"key": 1, "state": True}


@pytest.mark.asyncio
async def test_identifies(
    controller: ESPhomeController,
    paired_device,
):
    await controller.identify(paired_device)


@pytest.mark.asyncio
async def test_unpairs(
    controller: ESPhomeController,
    deps,
    paired_device,
    mock_connection,
):
    device = paired_device
    device_id = device.id

    _, mock_inst = mock_connection
    async with controller._lock:
        controller._connections[device_id] = mock_inst
    controller._state_cache[device_id] = {"some": "state"}

    await controller.unpair(device)

    mock_inst.stop.assert_awaited_once()

    assert device_id not in controller._connections
    assert device_id not in controller._state_cache


@pytest.mark.asyncio
async def test_incoming_events_from_device(
    controller: ESPhomeController,
    deps,
    paired_device,
    mock_connection,
):
    device = paired_device
    device_id = device.id

    _, mock_inst = mock_connection
    async with controller._lock:
        controller._connections[device_id] = mock_inst

    state_obj = MagicMock()
    state_obj.state = True

    await controller._on_state(
        device_id=device_id,
        entity_name="switch1",
        component_type="switch",
        state_obj=state_obj,
    )

    assert len(deps.output.events) >= 1
    event = deps.output.events[-1]
    assert event.device_id == device_id
    assert event.value is True

    cache = controller._state_cache.get(device_id, {})
    assert cache.get("switch1_state") is True
    assert isinstance(event.parameter_id, UUID)

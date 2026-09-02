"""Integration tests for ESPHome controller.

These tests verify that the controller correctly handles:
- pairing, fetching state, sending commands, identifying, unpairing,
- and receiving asynchronous state updates from the device.
"""

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, uuid5, NAMESPACE_DNS

import pytest
import pytest_asyncio

from majordom_integration_sdk.testing import build_test_dependencies
from majordom_hub.schemas.command import DeviceCommand
from majordom_hub.schemas.device import Discovery, CredentialsValue
from majordom_hub.schemas.parameter import ParameterDataType, ParameterRole, ParameterVisibility

from majordom_hub.services.controller.esphome.controller import ESPhomeController
from majordom_hub.services.controller.esphome.models import (
    ESPhomeDevice,
    ESPhomeDeviceIntegrationData,
    ESPhomeParameter,
    ESPhomeParameterIntegrationData,
    ESPhomeComponentType,
    ESPhomeParameterType,
)



@pytest.fixture
def deps():
    return build_test_dependencies()


@pytest_asyncio.fixture
async def controller(deps):
    ctrl = ESPhomeController(deps)
    await ctrl.start()
    yield ctrl
    await ctrl.stop()


@pytest.fixture
def mock_connection():
    with patch(
        "majordom_hub.services.controller.esphome.controller.ESPhomeDeviceConnection"
    ) as MockConn:
        instance = MagicMock()
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.wait_ready = AsyncMock()
        instance.send_command = AsyncMock()
        instance.get_entities = MagicMock(return_value={})
        MockConn.return_value = instance
        yield MockConn, instance


@pytest.fixture
def fake_entity():
    def _make(key: int, name: str, component_type: str, **kwargs):
        ent = MagicMock()
        ent.key = key
        ent.name = name
        ent.type = component_type
        for k, v in kwargs.items():
            setattr(ent, k, v)
        return ent
    return _make


@pytest.fixture
def sample_discovery():
    device_id = uuid5(NAMESPACE_DNS, "esphome_device_test_123")
    discovery = MagicMock(spec=Discovery)
    discovery.id = device_id
    discovery.name = "test_device.local."
    discovery.integration = "esphome"
    discovery.credentials = "none"
    discovery.transport = "tcp"
    discovery.device_manufacturer = "esphome"
    discovery.device_name = "test_device"
    discovery.device_category = "light"
    discovery.device_icon = ""
    discovery.integration_data = {"address": "127.0.0.1", "port": 6053, "requires_encryption": False}
    return discovery


@pytest_asyncio.fixture
async def paired_device(controller, deps):
    device_id = uuid5(NAMESPACE_DNS, "esphome_device_test_123")
    integration_data = ESPhomeDeviceIntegrationData(
        device_name="test_device",
        address="127.0.0.1",
        port=6053,
        encryption_key=None,
    )
    device = ESPhomeDevice(
        id=device_id,
        name="test_device",
        integration="esphome",
        available=True,
        parameters=[],
        integration_data=integration_data,
        room_id=uuid5(NAMESPACE_DNS, "test_room"),
        transport="tcp",
        manufacturer="esphome",
    )
    async with deps.make_device_repository() as repo:
        await repo.save(device)
    return device



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

    mock_cls, mock_inst = mock_connection
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

    mock_cls, mock_inst = mock_connection
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
    entity_key = call_args[0]
    component_type = call_args[1]
    command_args = call_args[2]
    assert entity_key == 1
    assert component_type == "switch"
    assert command_args == {"key": 1, "state": True}



@pytest.mark.asyncio
async def test_identifies(
    controller: ESPhomeController,
    paired_device,
):
    device = paired_device
    await controller.identify(device)




@pytest.mark.asyncio
async def test_unpairs(
    controller: ESPhomeController,
    deps,
    paired_device,
    mock_connection,
):
    device = paired_device
    device_id = device.id

    mock_cls, mock_inst = mock_connection
    async with controller._lock:
        controller._connections[device_id] = mock_inst
    controller._state_cache[device_id] = {"some": "state"}

    await controller.unpair(device)

    mock_inst.stop.assert_awaited_once()

    async with deps.make_device_repository() as repo:
        deleted = await repo.get(device_id, as_=ESPhomeDevice)
    assert deleted is None

    assert device_id not in controller._connections
    assert device_id not in controller._state_cache



@pytest.mark.asyncio
async def test_incoming_events_from_device(
    controller: ESPhomeController,
    deps,
    paired_device,
    mock_connection,
):
    """
    Test that the controller correctly handles incoming state updates from the device.
    Simulates a real device sending a state change (e.g., someone manually turned on a light).
    Checks that the event is propagated to the Hub via deps.output.events.
    """
    device = paired_device
    device_id = device.id

    mock_cls, mock_inst = mock_connection
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

   
    assert len(deps.output.events) >= 1, "No events received from device"
    event = deps.output.events[-1]  
    assert event.device_id == device_id
    assert event.value is True

    cache = controller._state_cache.get(device_id, {})
    assert cache.get("switch1_state") is True
    assert isinstance(event.parameter_id, UUID)
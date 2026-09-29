"""Shared pytest fixtures for the MajorDom ESPHome integration."""

from unittest.mock import AsyncMock, MagicMock, patch
from uuid import NAMESPACE_DNS, uuid5

import pytest
import pytest_asyncio
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.testing import build_test_dependencies

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import (
    ESPhomeDevice,
    ESPhomeDeviceIntegrationData,
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
    """Patch ESPhomeDeviceConnection where it is used (in the controller module)."""
    with patch("majordom_esphome.controller.ESPhomeDeviceConnection", autospec=True) as MockConn:
        instance = MockConn.return_value
        instance.start = AsyncMock(return_value=None)
        instance.stop = AsyncMock(return_value=None)
        instance.wait_ready = AsyncMock(return_value=None)
        instance.send_command = AsyncMock(return_value=None)
        instance.get_entities = MagicMock(return_value={})
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
    return Discovery(
        id=device_id,
        integration="esphome",
        transport="tcp",
        device_manufacturer="esphome",
        device_name="test_device",
        device_category="light",
        device_icon="",
        expected_credentials_options=[],
    )


@pytest_asyncio.fixture
async def paired_device(deps):
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

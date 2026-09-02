import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import UUID, NAMESPACE_DNS, uuid5

from majordom_integration_sdk.testing import build_test_dependencies
from majordom_hub.services.controller.esphome.controller import ESPhomeController


def stable_uuid(id_string: str) -> UUID:
    return uuid5(NAMESPACE_DNS, id_string)


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
        instance = AsyncMock()
        instance.start = AsyncMock()
        instance.stop = AsyncMock()
        instance.wait_ready = AsyncMock()
        instance.send_command = AsyncMock()
        instance.get_entities.return_value = {}
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

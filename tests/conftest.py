"""Shared pytest fixtures for the MajorDom ESPHome integration."""

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aioesphomeapi import EntityInfo
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.discovery.zeroconf_discovery import ZeroconfDiscoveryService
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.parameter import ParameterRole
from majordom_integration_sdk.testing import RecordingControllerOutput, build_test_dependencies

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import hub_creates_device
from tests.virtual.mdns import Advert, FakeMDNS, fake_mdns
from tests.virtual.runner import ENCRYPTED, PLAIN, SKETCHES, VirtualDevice, build


def pytest_collection_finish(session: pytest.Session) -> None:
    """Build the virtual devices before the first test that needs one, outside any test timeout."""
    if any(
        {"device", "encrypted_device", "extras_device"} & set(getattr(item, "fixturenames", ()))
        for item in session.items
    ):
        for sketch in SKETCHES:
            if not sketch.binary.exists():
                build(sketch)


@pytest.fixture
def mdns() -> Iterator[FakeMDNS]:
    """Zeroconf stubbed out: tests announce nodes explicitly, and nothing touches the network."""
    with fake_mdns() as fake:
        yield fake


@pytest_asyncio.fixture
async def deps(mdns: FakeMDNS) -> AsyncIterator[AbstractController.Dependencies]:
    """Test dependencies with the SDK's real zeroconf discovery service, running over the stubbed zeroconf."""
    service = ZeroconfDiscoveryService()
    await service.start()
    yield build_test_dependencies().copy(zeroconf_discovery_service=service)
    await service.stop()


@pytest.fixture
def output(deps: AbstractController.Dependencies) -> RecordingControllerOutput:
    assert isinstance(deps.output, RecordingControllerOutput)
    return deps.output


@pytest_asyncio.fixture
async def controller(deps: AbstractController.Dependencies) -> AsyncIterator[ESPhomeController]:
    ctrl = ESPhomeController(deps)
    await ctrl.start()
    yield ctrl
    await ctrl.stop()


@pytest.fixture
def mock_connection() -> Any:
    """Patch ESPhomeDeviceConnection where it is used (in the controller module)."""
    with patch("majordom_esphome.controller.ESPhomeDeviceConnection", autospec=True) as MockConn:
        instance = MockConn.return_value
        instance.start = AsyncMock(return_value=None)
        instance.stop = AsyncMock(return_value=None)
        instance.wait_ready = AsyncMock(return_value=None)
        instance.send_command = AsyncMock(return_value=None)
        instance.get_entities = MagicMock(return_value={})
        instance.addresses = ["127.0.0.1"]
        yield MockConn, instance


@pytest.fixture
def make_entity() -> Callable[..., EntityInfo]:
    """Build a real `aioesphomeapi` entity description, i.e. exactly what a device reports."""

    def _make(info_type: Any, key: int, name: str, **kwargs: Any) -> EntityInfo:
        return info_type(object_id=name.lower().replace(" ", "_"), key=key, name=name, **kwargs)

    return _make


@pytest_asyncio.fixture
async def device() -> AsyncIterator[VirtualDevice]:
    virtual = VirtualDevice(PLAIN)
    await virtual.start()
    yield virtual
    await virtual.stop()


@pytest_asyncio.fixture
async def encrypted_device() -> AsyncIterator[VirtualDevice]:
    virtual = VirtualDevice(ENCRYPTED)
    await virtual.start()
    yield virtual
    await virtual.stop()


@pytest_asyncio.fixture
async def discovery(controller: ESPhomeController, mdns: FakeMDNS, device: VirtualDevice) -> Discovery:
    """The plain virtual device, discovered over mDNS and picked by the user (so the Hub has created the device)."""
    await mdns.announce(Advert(port=device.port))
    (found,) = controller.discoveries.values()
    await hub_creates_device(controller.dependencies, found)
    return found


@pytest_asyncio.fixture
async def paired(controller: ESPhomeController, discovery: Discovery) -> ESPhomeDevice:
    """The plain virtual device, paired and connected."""
    await controller.pair_device(discovery, credentials=None)
    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    return stored


def controls(device: ESPhomeDevice) -> list[Any]:
    return [p for p in device.parameters if p.role == ParameterRole.control]

"""Shared pytest fixtures for the MajorDom ESPHome integration."""

from collections.abc import AsyncIterator, Callable, Iterator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from aioesphomeapi import EntityInfo
from majordom_integration_sdk.controller import Platform
from majordom_integration_sdk.peripherals import Peripherals
from majordom_integration_sdk.runtime.peripherals.providers.zeroconf import ZeroconfDiscoveryService
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.parameter import ParameterRole
from majordom_integration_sdk.testing import RecordingControllerOutput, build_test_platform

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import hub_creates_device
from tests.virtual import catalogue
from tests.virtual.mdns import Advert, FakeMDNS, fake_mdns
from tests.virtual.runner import ENCRYPTED, PLAIN, SKETCHES, VirtualDevice, build


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "catalogue: sweep over one virtual node per entity kind (run with -m catalogue)")


@pytest.fixture(scope="session")
def build_errors() -> dict[str, str | None]:
    """Why a catalogue node could not be built (kind -> error), filled in by the collection hook."""
    return _BUILD_ERRORS


_BUILD_ERRORS: dict[str, str | None] = {}


def pytest_collection_finish(session: pytest.Session) -> None:
    """Build the virtual devices before the first test that needs one, outside any test timeout."""
    if any(
        {"device", "encrypted_device", "extras_device", "keyless_device"} & set(getattr(item, "fixturenames", ()))
        for item in session.items
    ):
        for sketch in SKETCHES:
            if not sketch.binary.exists():
                build(sketch)
    if any(item.get_closest_marker("catalogue") for item in session.items):
        _BUILD_ERRORS.update(catalogue.build_all())


@pytest.fixture
def mdns() -> Iterator[FakeMDNS]:
    """Zeroconf stubbed out: tests announce nodes explicitly, and nothing touches the network."""
    with fake_mdns() as fake:
        yield fake


@pytest.fixture
def deps() -> Platform:
    return build_test_platform()


@pytest_asyncio.fixture
async def zeroconf(mdns: FakeMDNS) -> AsyncIterator[ZeroconfDiscoveryService]:
    """The SDK's real DNS-SD provider, running over the stubbed zeroconf."""
    service = ZeroconfDiscoveryService()
    await service.start()
    yield service
    await service.stop()


@pytest.fixture
def peripherals(zeroconf: ZeroconfDiscoveryService) -> Peripherals:
    return Peripherals({"mdns": zeroconf.endpoint})


@pytest.fixture
def output(deps: Platform) -> RecordingControllerOutput:
    assert isinstance(deps.output, RecordingControllerOutput)
    return deps.output


@pytest_asyncio.fixture
async def controller(deps: Platform, peripherals: Peripherals) -> AsyncIterator[ESPhomeController]:
    ctrl = ESPhomeController(deps)
    ctrl.peripherals = peripherals
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
    await hub_creates_device(controller.platform, found)
    return found


@pytest_asyncio.fixture
async def paired(controller: ESPhomeController, discovery: Discovery) -> ESPhomeDevice:
    """The plain virtual device, paired and connected."""
    await controller.pair_device(discovery, credentials=None)
    async with controller.device_repository() as repo:
        stored = await repo.get(discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    return stored


def controls(device: ESPhomeDevice) -> list[Any]:
    return [p for p in device.parameters if p.role == ParameterRole.control]

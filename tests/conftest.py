"""Shared pytest fixtures for the whole suite live here.

conftest.py is auto-discovered by pytest — no import needed.

The fixtures wire the integration's Controller with the SDK's test doubles (a recording
`ControllerOutput`, an in-memory device repository, and fake discovery services) and build
sample `Discovery`/`DeviceState` objects, so tests can drive the controller exactly as the
Hub would — without a running Hub.

**Device persistence is the Hub's core business logic, not the integration's.** The Hub
creates and stores device rows (name/room mapping, id assignment, the provisional→final id
reconciliation on pairing); an integration only reads through the injected repository and
writes parameter *state*. These fixtures therefore seed the repository the way the Hub
would, so the controller sees realistic state — the tests never make the integration
persist a device itself.
"""

from uuid import uuid4

import pytest
from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.schemas.device import CredentialsType, Device, DeviceState, Discovery
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType,
    ParameterRole,
    ParameterState,
    ParameterVisibility,
)
from majordom_integration_sdk.testing import build_test_dependencies

from integration_template import ExampleController

INTEGRATION = "example"


@pytest.fixture
def deps() -> AbstractController.Dependencies:
    """SDK-provided test dependencies (recording output + in-memory repo + fake discovery)."""
    return build_test_dependencies(integration=INTEGRATION)


@pytest.fixture
def controller(deps: AbstractController.Dependencies) -> ExampleController:
    return ExampleController(deps)


# A virtual / simulated device — a fake endpoint that speaks your protocol in-process (no
# hardware, no network) so tests are deterministic and can assert what actually reached the
# device. Stand one up here and have the controller talk to it (e.g. via a base URL / mock
# transport injected through the controller). Tests then assert on both the controller's
# reports (deps.output) and this object's observed state.
#
# @pytest.fixture
# def virtual_device():
#     dev = VirtualExampleDevice()  # your in-process fake
#     dev.start()
#     yield dev
#     dev.stop()


async def _seed(deps: AbstractController.Dependencies, device: DeviceState) -> None:
    """Persist a device the way the Hub would, before handing control to the integration."""
    async with deps.make_device_repository() as repo:
        await repo.save(device)


@pytest.fixture
def discovery() -> Discovery:
    """A sample discovered-but-unpaired device (the controller surfaced this)."""
    return Discovery(
        id=uuid4(),
        integration=INTEGRATION,
        expected_credentials_options=[CredentialsType.none],
        transport="wifi",
        device_manufacturer="ACME",
        device_name="Example Lamp",
        device_category=None,
        device_icon=None,
    )


@pytest.fixture
async def provisional_device(deps: AbstractController.Dependencies, discovery: Discovery) -> DeviceState:
    """The device as it exists when the Hub calls `pair_device`.

    Before pairing, the Hub has already created the row from the user's input: name and room
    mapped, and its id set to the discovery's (still provisional) id — but no parameters yet;
    the controller discovers/maps those while pairing. After pairing the controller reports
    the real device id and the Hub reconciles the row. We seed the repository to match.
    """
    device = DeviceState(
        id=discovery.id,  # provisional id == discovery id, until pairing assigns the real one
        name="Living Room Lamp",  # mapped by the Hub from the user's input
        room_id=uuid4(),
        transport=discovery.transport,
        integration=INTEGRATION,
        manufacturer=discovery.device_manufacturer,
        parameters=[],
    )
    await _seed(deps, device)
    return device


@pytest.fixture
async def device_state(deps: AbstractController.Dependencies) -> DeviceState:
    """An already-paired device with one on/off parameter, as the Hub *stores* it (the full
    state, with parameters). Seeded into the repository."""
    power = ParameterState(
        id=uuid4(),
        name="Power",
        data_type=ParameterDataType.bool,
        role=ParameterRole.control,
        visibility=ParameterVisibility.user,
        integration_data=None,
        value=True,
    )
    state = DeviceState(
        id=uuid4(),
        name="Living Room Lamp",
        room_id=uuid4(),
        transport="wifi",
        integration=INTEGRATION,
        manufacturer="ACME",
        parameters=[power],
    )
    await _seed(deps, state)
    return state


@pytest.fixture
def device(device_state: DeviceState) -> Device:
    """The device as the Hub *passes* it to `fetch`/`identify`/`unpair`/`send_command` — a
    `Device` (info + integration_data), without the parameter list."""
    return Device.model_validate(device_state.model_dump())


@pytest.fixture
def parameter(device_state: DeviceState) -> ParameterState:
    """The target parameter for `send_command`, paired with `device`."""
    return device_state.parameters[0]

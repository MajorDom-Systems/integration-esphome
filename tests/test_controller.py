"""Tests that drive the controller through its lifecycle.

These are **prefilled to fail** against the unimplemented skeleton: every test below calls a
method that still raises `NotImplementedError`, so CI stays red until you implement the
controller.

Run these against a **virtual / simulated device** — a fake endpoint that speaks your
protocol in-process (no real hardware, no network), so CI is deterministic. Add a fixture
for it in `conftest.py` and have the controller talk to it. Each test should then assert on
**both sides**: what the controller reported back to the Hub (via `deps.output`) *and* what
actually landed on the device (the virtual device observed the command / changed state) —
don't just call the method and assume it worked.

Persistence is the Hub's job (see `conftest.py`): the fixtures seed the repository the way
the Hub would, so assert on the controller's protocol behaviour and reports — not that the
integration created a device row.

(The template's own CI excludes this file so the template repo stays green — see
`.github/workflows/test.yml`. Your integration repo runs it in full, and it's red until you
implement the controller.)
"""

from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery
from majordom_integration_sdk.schemas.parameter import ParameterState

from integration_template import ExampleController


async def test_pairs_a_discovered_device(
    controller: ExampleController,
    deps: AbstractController.Dependencies,
    discovery: Discovery,
    provisional_device: Device,  # creates a device with discovery.id and saves it to the db
) -> None:
    # The Hub has already stored `provisional_device` (id == discovery.id) before this call.
    await controller.pair_device(discovery, credentials=None)
    # TODO: verify pairing actually completed — the controller established the session and
    #   reported back (it does not persist the device itself):
    #   - Hub side: `discovery.id` is in `deps.output.connected_devices`.
    #   - Device side: the virtual device accepted the credentials / opened a session.
    #   - Parameters: if you map parameters during pairing, assert their initial states were
    #     written via the repository (`repo.save_parameter_state`).


async def test_fetches_state(
    controller: ExampleController,
    deps: AbstractController.Dependencies,
    device: Device,
) -> None:
    await controller.fetch(device)
    # TODO: verify the refresh reached the device and propagated back:
    #   - Set a known state on the virtual device first, then assert `deps.output.events`
    #     (or the repository's parameter states) now reflect exactly that state — not stale
    #     or default values.


async def test_sends_a_command(controller: ExampleController, device: Device, parameter: ParameterState) -> None:
    command = DeviceCommand(device_id=device.id, parameter_id=parameter.id, value=False)
    await controller.send_command(command, device, parameter)
    # TODO: verify the command actually applied on the device, not just that the call
    #   returned: assert the virtual device received this exact write and its state changed
    #   to `value` (read it back). A no-op that silently succeeds must fail this test.


async def test_identifies(controller: ExampleController, device: Device) -> None:
    await controller.identify(device)
    # TODO: assert the virtual device recorded an identify request (e.g. its identify counter
    #   incremented / the identify command was received).


async def test_unpairs(controller: ExampleController, device: Device) -> None:
    await controller.unpair(device)
    # TODO: verify removal took effect:
    #   - Device side: the virtual device saw the session torn down / was de-registered.
    #   - Controller: the device is gone — no more connections or commands should be accepted.

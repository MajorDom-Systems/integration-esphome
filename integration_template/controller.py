"""A minimal MajorDom integration controller.

This is a worked skeleton — replace the TODO bodies with real protocol calls. It shows the
full lifecycle the Hub drives: discovery -> pairing -> commands -> teardown.

See the full guide at https://docs.majordom.io/device-integration.

A Controller bridges one external protocol/platform into the MajorDom language. The Hub
instantiates it with injected `Dependencies` and calls the methods below; the controller
reports back to the Hub through `dependencies.output` (a `ControllerOutput`).
"""

from __future__ import annotations

from uuid import UUID

from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.device import Device, Discovery, Parameter, ProvidedCredentials

# Output methods to call (see the docs):
# async def controller_did_receive_discovery(self, controller: AbstractController, discovery: Discovery): ...
# async def controller_did_update_discovery(self, controller: AbstractController, discovery: Discovery): ...
# async def controller_did_lose_discovery(self, controller: AbstractController, discovery_id: UUID): ...
# async def controller_did_connect_device(self, controller: AbstractController, device_id: UUID): ...
# async def controller_did_lose_device(self, controller: AbstractController, device_id: UUID): ...
# async def controller_did_receive_events(self, controller: AbstractController, events: Iterable[Event]): ...


class ExampleController(AbstractController[Device, Parameter]):
    """Bridges the Example protocol into MajorDom."""

    def __init__(self, dependencies: AbstractController.Dependencies):
        super().__init__(dependencies)
        self._discoveries: dict[UUID, Discovery] = {}

    name = "Example"

    @property
    def discoveries(self) -> dict[UUID, Discovery]:
        # Return the cached snapshot only — never scan here.
        return self._discoveries

    # Lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        # Register discovery services, subscribe to protocol events, and reconcile the
        # state of already-paired devices here.
        # e.g. self.dependencies.zeroconf_discovery_service.add_listener("_example._tcp.local.", self)
        ...

    async def stop(self) -> None:
        # Cancel tasks and release every held resource.
        ...

    # Hub -> device ---------------------------------------------------------

    async def pair_device(self, discovery: Discovery, credentials: ProvidedCredentials | None) -> None:
        # Establish a session with the device, then report success to the Hub:
        # await self.dependencies.output.controller_did_connect_device(self, self.device_uuid(...))
        raise NotImplementedError

    async def unpair(self, device: Device) -> None:
        raise NotImplementedError

    async def identify(self, device: Device) -> None:
        # Ask the device to blink/beep so the user can locate it.
        raise NotImplementedError

    async def fetch(self, device: Device) -> None:
        # Refresh the device and its parameters, reporting changes via
        # self.dependencies.output.controller_did_receive_events(self, [...]).
        raise NotImplementedError

    async def send_command(self, command: DeviceCommand, device: Device, parameter: Parameter) -> None:
        # Translate the MajorDom command into a protocol-level write.
        raise NotImplementedError

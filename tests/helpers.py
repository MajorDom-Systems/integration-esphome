"""Test helpers shared by the unit and virtual-device suites."""

import asyncio
import contextlib
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID, uuid4

from majordom_integration_sdk import DeviceIdTakenError, Platform
from majordom_integration_sdk.schemas import DeviceParameterChange, ProvidedCredentials
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.parameter import ParameterDataType
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice, ESPhomeDeviceState, ESPhomeParameter

ROOM = UUID(int=42)


_forms: dict[UUID, ESPhomeDeviceState] = {}


def user_picks(discovery: Discovery, name: str = "Kitchen relay", room: UUID = ROOM):
    """What the user fills in when picking a discovery: the Hub builds the pairing draft from it."""
    _forms[discovery.id] = draft_of(discovery, name, room)


def draft_of(discovery: Discovery, name: str = "Kitchen relay", room: UUID = ROOM) -> ESPhomeDeviceState:
    return ESPhomeDeviceState(
        id=uuid4(),
        physical_id=discovery.id,
        name=name,
        room_id=room,
        transport=discovery.transport,
        integration=discovery.integration,
        manufacturer=discovery.device_manufacturer,
        parameters=[],
    )


async def hub_pairs(
    controller: ESPhomeController,
    discovery: Discovery,
    credentials: ProvidedCredentials | None = None,
    draft: ESPhomeDeviceState | None = None,
) -> UUID:
    """What the Hub does (`DeviceProvider`): pair into a draft, roll back with `unpair` on a failure, else save the
    device, report it connected and fetch it."""
    draft = draft or _forms.get(discovery.id, draft_of(discovery)).model_copy(deep=True)
    try:
        device = await controller.pair_device(discovery, draft, credentials)
    except DeviceIdTakenError:
        raise
    except BaseException:
        await controller.unpair(draft)
        raise
    async with controller.platform.device_repository(controller) as repo:
        await repo.save(ESPhomeDevice.model_validate(device.model_dump()))
    if device.available:
        await controller.output.controller_did_connect_device(controller, physical_id=device.physical_id)
    with contextlib.suppress(Exception):
        await controller.fetch(ESPhomeDevice.model_validate(device.model_dump()))
    return device.physical_id


async def wait_until(predicate: Callable[[], Any], timeout: float = 10.0, message: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError(f"timed out after {timeout}s waiting for {message}")


def param(device: ESPhomeDevice, entity: str, sub_field: str = "state") -> ESPhomeParameter:
    matches = [
        p
        for p in device.parameters
        if p.integration_data.entity_name == entity and p.integration_data.sub_field == sub_field
    ]
    assert len(matches) == 1, f"expected one parameter for {entity}.{sub_field}, got {len(matches)}"
    return matches[0]


def changes(output: RecordingControllerOutput) -> list[DeviceParameterChange]:
    return [e for e in output.events if isinstance(e, DeviceParameterChange)]


def values(output: RecordingControllerOutput, parameter_id: UUID) -> list[Any]:
    return [e.value for e in changes(output) if e.parameter_id == parameter_id]


async def wait_for_value(
    output: RecordingControllerOutput,
    parameter: ESPhomeParameter,
    predicate: Callable[[Any], bool] = lambda _: True,
    timeout: float = 5.0,
) -> Any:
    """Wait for a state event of `parameter` that satisfies `predicate`; returns its value."""
    await wait_until(
        lambda: any(predicate(v) for v in values(output, parameter.id)),
        timeout,
        f"event of {parameter.name} (seen: {values(output, parameter.id)})",
    )
    return next(v for v in reversed(values(output, parameter.id)) if predicate(v))


def assert_values_match_parameters(output: RecordingControllerOutput, device: ESPhomeDevice):
    """Every reported value has exactly the plain type its parameter declares (no IntEnums, protobuf objects, ...).

    A `struct` value is a dict keyed by its sub-parameter ids (as the SDK specifies), each value of its sub-type.
    """
    parameters = {p.id: p for p in device.parameters}
    for event in changes(output):
        assert event.parameter_id in parameters, f"event for unknown parameter {event.parameter_id}"
        _assert_value_type(parameters[event.parameter_id], event.value)


def _assert_value_type(parameter: ESPhomeParameter, value: Any):
    expected: dict[ParameterDataType, tuple[type, ...]] = {
        ParameterDataType.bool: (bool,),
        ParameterDataType.integer: (int,),
        ParameterDataType.decimal: (float,),
        ParameterDataType.enum: (int,),
        ParameterDataType.string: (str,),
        ParameterDataType.struct: (dict,),
    }
    assert type(value) in expected[parameter.data_type], (
        f"{parameter.name}: {type(value).__name__} {value!r} is not {parameter.data_type}"
    )
    if parameter.data_type == ParameterDataType.enum:
        assert parameter.valid_values and value in parameter.valid_values, (
            f"{parameter.name}: {value!r} not in {parameter.valid_values}"
        )
    if parameter.data_type == ParameterDataType.struct:
        subs = {str(sub.id): sub for sub in (ESPhomeParameter.model_validate(f) for f in parameter.fields or [])}
        assert set(value) == set(subs), f"{parameter.name}: keys {sorted(value)} are not its sub-parameter ids"
        for key, item in value.items():
            _assert_value_type(subs[key], item)


def leaked_tasks() -> list[asyncio.Task[Any]]:
    """Still-running tasks started by this package: after `stop()`/failed pairing there must be none."""
    leaked = []
    for task in asyncio.all_tasks():
        coro = task.get_coro()
        code = getattr(coro, "cr_code", None)
        if task is not asyncio.current_task() and code is not None and "majordom_esphome" in code.co_filename:
            leaked.append(task)
    return leaked


async def assert_not_paired(platform: Platform, device_id: UUID):
    """A failed pairing leaves no device (the Hub saves only what pairing returned) and nothing kept on the side."""
    async with platform.make_device_repository(ESPhomeController.slug()) as repo:
        assert await repo.get(physical_id=device_id) is None
    assert_no_side_storage(platform)


def assert_no_side_storage(platform: Platform):
    """Whatever the integration keeps about a device lives in its record (`integration_data`), never in a file."""
    folder = platform.documents_root / ESPhomeController.slug()
    assert not folder.exists() or not any(folder.iterdir())

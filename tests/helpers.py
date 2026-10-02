"""Test helpers shared by the unit and virtual-device suites."""

import asyncio
import time
from collections.abc import Callable
from typing import Any
from uuid import UUID

from majordom_integration_sdk.controller import AbstractController
from majordom_integration_sdk.schemas import DeviceParameterChange
from majordom_integration_sdk.schemas.device import Device, Discovery
from majordom_integration_sdk.schemas.parameter import ParameterDataType
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.models import ESPhomeDevice, ESPhomeDeviceIntegrationData, ESPhomeParameter

ROOM = UUID(int=42)


async def hub_creates_device(
    deps: AbstractController.Dependencies, discovery: Discovery, name: str = "Kitchen relay", room: UUID = ROOM
) -> None:
    """What the Hub does when the user picks a discovery: it creates the device (name, room) *before* asking the
    integration to pair (`DeviceProvider.create_device`). The integration completes that device."""
    async with deps.make_device_repository() as repo:
        await repo.save(
            Device(
                id=discovery.id,
                name=name,
                room_id=room,
                transport=discovery.transport,
                integration=discovery.integration,
                manufacturer=discovery.device_manufacturer,
            )
        )


async def wait_until(predicate: Callable[[], Any], timeout: float = 10.0, message: str = "condition") -> None:
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


def assert_values_match_parameters(output: RecordingControllerOutput, device: ESPhomeDevice) -> None:
    """Every reported value has exactly the plain type its parameter declares (no IntEnums, protobuf objects, ...).

    A `struct` value is a dict keyed by its sub-parameter ids (as the SDK specifies), each value of its sub-type.
    """
    parameters = {p.id: p for p in device.parameters}
    for event in changes(output):
        assert event.parameter_id in parameters, f"event for unknown parameter {event.parameter_id}"
        _assert_value_type(parameters[event.parameter_id], event.value)


def _assert_value_type(parameter: ESPhomeParameter, value: Any) -> None:
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


async def assert_not_paired(deps: AbstractController.Dependencies, device_id: UUID) -> None:
    """A failed pairing leaves the device as the Hub created it: no parameters, no connection data."""
    async with deps.make_device_repository() as repo:
        device = await repo.get(device_id, as_=ESPhomeDevice)
    assert device is not None, "the device the Hub created must not be deleted by the integration"
    assert device.parameters == []
    assert device.integration_data == ESPhomeDeviceIntegrationData()
    assert not device.available

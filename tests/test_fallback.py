"""Entities without a hand-written mapping (lock, valve, text, ...) are mapped generically from the library's types.

The virtual node `extras.yaml` hosts them. What has no meaningful mapping (events, multi-argument commands) is skipped
without breaking pairing.
"""

from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.parameter import ParameterDataType as T
from majordom_integration_sdk.schemas.parameter import ParameterRole as R
from majordom_integration_sdk.schemas.parameter import ParameterUnit

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import assert_values_match_parameters, hub_creates_device, param, wait_for_value
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import EXTRAS, VirtualDevice

pytestmark = pytest.mark.timeout(60)


@pytest_asyncio.fixture
async def extras_device() -> AsyncIterator[VirtualDevice]:
    virtual = VirtualDevice(EXTRAS)
    await virtual.start()
    yield virtual
    await virtual.stop()


@pytest_asyncio.fixture
async def extras(controller: ESPhomeController, mdns: FakeMDNS, extras_device: VirtualDevice) -> ESPhomeDevice:
    await mdns.announce(Advert(name=EXTRAS.name, mac=EXTRAS.mac, port=EXTRAS.port))
    (discovery,) = controller.discoveries.values()
    await hub_creates_device(controller.dependencies, discovery)
    await controller.pair_device(discovery, credentials=None)
    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    return stored


async def send(controller: ESPhomeController, device: ESPhomeDevice, entity: str, sub_field: str, value) -> None:
    target = param(device, entity, sub_field)
    await controller.send_command(
        DeviceCommand(device_id=device.id, parameter_id=target.id, value=value), device, target
    )


async def test_unmapped_entities_get_parameters_from_the_library_types(extras: ESPhomeDevice):
    actual = {
        (p.integration_data.entity_name, p.integration_data.sub_field): (p.data_type, p.role) for p in extras.parameters
    }

    assert actual == {
        ("Door", "state"): (T.enum, R.sensor),
        ("Door", "command"): (T.enum, R.control),
        ("Garden Valve", "position"): (T.decimal, R.control),
        ("Garden Valve", "current_operation"): (T.enum, R.sensor),
        ("Garden Valve", "stop"): (T.none, R.control),  # a flag that only means "do it": a button
        ("Label", "state"): (T.string, R.control),
        ("House Alarm", "state"): (T.enum, R.sensor),
        ("House Alarm", "command"): (T.enum, R.control),
        ("House Alarm", "command_with_code"): (T.none, R.control),  # the panel requires a code
        ("Date", "date"): (T.struct, R.control),  # year, month and day as its fields
    }  # the doorbell (an event) is skipped


async def test_enums_are_integers_labelled_with_the_library_enum_names(extras: ESPhomeDevice):
    assert param(extras, "Door", "command").valid_values == {0: "UNLOCK", 1: "LOCK", 2: "OPEN"}
    assert param(extras, "Door", "state").valid_values == {
        0: "NONE",
        1: "LOCKED",
        2: "UNLOCKED",
        3: "JAMMED",
        4: "LOCKING",
        5: "UNLOCKING",
        6: "OPENING",
        7: "OPEN",
    }
    assert param(extras, "Garden Valve", "current_operation").valid_values == {
        0: "IDLE",
        1: "IS_OPENING",
        2: "IS_CLOSING",
    }


async def test_fractions_are_percentages_like_in_the_hand_written_mapping(extras: ESPhomeDevice):
    position = param(extras, "Garden Valve", "position")

    assert (position.unit, position.min_value, position.max_value) == (ParameterUnit.percentage, 0, 100)


async def test_states_are_reported_with_the_declared_types(extras: ESPhomeDevice, output):
    await wait_for_value(output, param(extras, "Door", "state"))
    await wait_for_value(output, param(extras, "Garden Valve", "position"), lambda v: v == 100)

    assert_values_match_parameters(output, extras)


async def test_text_is_set(controller: ESPhomeController, extras: ESPhomeDevice, output):
    await send(controller, extras, "Label", "state", "hello")

    await wait_for_value(output, param(extras, "Label"), lambda v: v == "hello")


async def test_lock_is_locked_by_an_enum_command(controller: ESPhomeController, extras: ESPhomeDevice, output):
    await send(controller, extras, "Door", "command", 1)  # LOCK

    await wait_for_value(output, param(extras, "Door", "state"), lambda v: v == 1)  # LOCKED


async def test_valve_moves_to_a_position_in_percent(controller: ESPhomeController, extras: ESPhomeDevice, output):
    await send(controller, extras, "Garden Valve", "position", 40)

    await wait_for_value(output, param(extras, "Garden Valve", "position"), lambda v: v == pytest.approx(40, abs=1))


async def test_a_flag_command_is_a_button(
    controller: ESPhomeController, extras: ESPhomeDevice, extras_device: VirtualDevice
):
    await send(controller, extras, "Garden Valve", "stop", None)

    await extras_device.wait_log("Valve stop")


@pytest.mark.parametrize(("entity", "sub_field", "value"), [("Door", "state", 1), ("Door", "command", 9)])
async def test_invalid_generic_commands_are_rejected(
    controller: ESPhomeController, extras: ESPhomeDevice, entity: str, sub_field: str, value: int
):
    with pytest.raises(ValueError):
        await send(controller, extras, entity, sub_field, value)


def subs(parameter) -> dict[str, Any]:
    """Sub-parameters by name."""
    return {
        sub.integration_data.sub_field: sub for sub in (type(parameter).model_validate(f) for f in parameter.fields)
    }


async def test_a_date_is_one_struct_value_set_and_reported_by_its_fields(
    controller: ESPhomeController, extras: ESPhomeDevice, output
):
    date = param(extras, "Date", "date")
    fields = subs(date)
    value = {str(fields["year"].id): 2026, str(fields["month"].id): 10, str(fields["day"].id): 2}

    await send(controller, extras, "Date", "date", value)

    await wait_for_value(output, date, lambda v: v == value)
    assert (fields["month"].min_value, fields["month"].max_value) == (1, 12)


async def test_a_command_that_needs_a_code_takes_it_as_an_argument(
    controller: ESPhomeController, extras: ESPhomeDevice, output
):
    command = param(extras, "House Alarm", "command_with_code")
    fields = subs(command)

    await send(
        controller,
        extras,
        "House Alarm",
        "command_with_code",
        {
            str(fields["command"].id): 1,  # ARM_AWAY
            str(fields["code"].id): "1234",
        },
    )

    await wait_for_value(output, param(extras, "House Alarm", "state"), lambda v: v != 0)  # no longer DISARMED

"""Hub -> device: commands sent through the controller reach a virtual ESPHome device and take effect."""

from typing import Any

import pytest
from majordom_integration_sdk.schemas.command import DeviceCommand
from majordom_integration_sdk.schemas.parameter import ParameterVisibility
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import param, values, wait_for_value, wait_until
from tests.virtual.runner import VirtualDevice


async def command(
    controller: ESPhomeController, device: ESPhomeDevice, entity: str, sub_field: str, value: Any
) -> None:
    target = param(device, entity, sub_field)
    await controller.send_command(
        DeviceCommand(device_id=device.id, parameter_id=target.id, value=value), device, target
    )


async def test_switch_turns_on_and_off(controller: ESPhomeController, paired: ESPhomeDevice, output):
    relay = param(paired, "Relay")

    await command(controller, paired, "Relay", "state", True)
    await wait_for_value(output, relay, lambda v: v is True)
    await command(controller, paired, "Relay", "state", False)
    await wait_for_value(output, relay, lambda v: v is False)


async def test_fan_turns_on(controller: ESPhomeController, paired: ESPhomeDevice, output):
    await command(controller, paired, "Virtual Fan", "state", True)

    await wait_for_value(output, param(paired, "Virtual Fan"), lambda v: v is True)


async def test_light_switches_and_dims(controller: ESPhomeController, paired: ESPhomeDevice, output):
    await command(controller, paired, "Virtual Light", "state", True)
    await wait_for_value(output, param(paired, "Virtual Light"), lambda v: v is True)

    await command(controller, paired, "Virtual Light", "brightness", 0.5)

    await wait_for_value(
        output, param(paired, "Virtual Light", "brightness"), lambda v: v == pytest.approx(0.5, abs=0.02)
    )


async def test_changing_the_hue_keeps_the_saturation(controller: ESPhomeController, paired: ESPhomeDevice, output):
    await command(controller, paired, "RGB Light", "state", True)
    await command(controller, paired, "RGB Light", "color_saturation", 100)
    await wait_for_value(
        output, param(paired, "RGB Light", "color_saturation"), lambda v: v == pytest.approx(100, abs=1)
    )

    await command(controller, paired, "RGB Light", "color_hue", 120)  # green

    await wait_for_value(output, param(paired, "RGB Light", "color_hue"), lambda v: v == pytest.approx(120, abs=2))
    latest = values(output, param(paired, "RGB Light", "color_saturation").id)[-1]
    assert latest == pytest.approx(100, abs=1), "setting the hue must not reset the saturation"


async def test_cover_moves_to_a_position(controller: ESPhomeController, paired: ESPhomeDevice, output):
    await command(controller, paired, "Blinds", "position", 30)

    await wait_for_value(output, param(paired, "Blinds", "position"), lambda v: v == pytest.approx(30, abs=1))


async def test_number_is_set(controller: ESPhomeController, paired: ESPhomeDevice, output):
    await command(controller, paired, "Target Temp", "state", 20.5)

    await wait_for_value(output, param(paired, "Target Temp"), lambda v: v == 20.5)


async def test_select_is_set_by_option_index(controller: ESPhomeController, paired: ESPhomeDevice, output):
    await command(controller, paired, "Mode", "state", 1)

    await wait_for_value(output, param(paired, "Mode"), lambda v: v == 1)


async def test_button_is_pressed(controller: ESPhomeController, paired: ESPhomeDevice, device: VirtualDevice):
    await command(controller, paired, "Reboot", "state", None)

    await device.wait_log("Reboot pressed")


async def test_climate_mode_and_targets_are_set(controller: ESPhomeController, paired: ESPhomeDevice, output):
    await command(controller, paired, "HVAC", "mode", 3)  # heat
    await wait_for_value(output, param(paired, "HVAC", "mode"), lambda v: v == 3)

    await command(controller, paired, "HVAC", "target_temperature_low", 19.0)

    await wait_for_value(output, param(paired, "HVAC", "target_temperature_low"), lambda v: v == 19.0)


@pytest.mark.parametrize(
    ("entity", "sub_field", "value"),
    [
        ("Temperature", "state", 5.0),  # sensors are read-only
        ("Mode", "state", 7),  # no such option
        ("Target Temp", "state", 99.0),  # outside the device's 10..30
        ("Virtual Light", "brightness", 150),  # a percentage
        ("RGB Light", "color_hue", 400),  # degrees
    ],
)
async def test_invalid_commands_are_rejected_before_reaching_the_device(
    controller: ESPhomeController, paired: ESPhomeDevice, entity: str, sub_field: str, value: Any
):
    with pytest.raises(ValueError):
        await command(controller, paired, entity, sub_field, value)


@pytest.mark.timeout(60)
async def test_command_to_an_offline_device_fails_visibly(
    controller: ESPhomeController, paired: ESPhomeDevice, device: VirtualDevice, output: RecordingControllerOutput
):
    await device.stop()
    await wait_until(lambda: paired.id in output.lost_devices, 15, "the device to be reported lost")

    with pytest.raises(ConnectionError):
        await command(controller, paired, "Relay", "state", True)

    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(paired.id, as_=ESPhomeDevice)
    assert stored is not None
    assert stored.last_error


async def test_the_transition_is_a_setting_kept_and_added_to_later_light_commands(
    controller: ESPhomeController, paired: ESPhomeDevice, device: VirtualDevice, output
):
    transition = param(paired, "Virtual Light", "transition_length")
    assert transition.visibility == ParameterVisibility.setting

    await command(controller, paired, "Virtual Light", "transition_length", 1.5)
    await wait_for_value(output, transition, lambda v: v == 1.5)  # echoed, so the Hub stores it
    await command(controller, paired, "Virtual Light", "state", True)

    await device.wait_log("Transition length: 1.5s")
    output.events.clear()
    await controller.fetch(paired)
    await wait_for_value(output, transition, lambda v: v == 1.5)  # kept across fetches


async def test_the_flash_command_takes_its_length_as_an_argument(
    controller: ESPhomeController, paired: ESPhomeDevice, device: VirtualDevice
):
    flash = param(paired, "Virtual Light", "flash")
    (length,) = [type(flash).model_validate(f) for f in flash.fields or []]

    await command(controller, paired, "Virtual Light", "flash", {str(length.id): 1.0})

    await device.wait_log("Flash length: 1.0s")

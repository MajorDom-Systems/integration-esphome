"""Pairing against a virtual ESPHome device: what gets stored, and how pairing fails."""

import asyncio

import pytest
from majordom_integration_sdk.schemas import CredentialsType, ProvidedCredentials
from majordom_integration_sdk.schemas.device import Discovery
from majordom_integration_sdk.schemas.parameter import (
    ParameterDataType as T,
)
from majordom_integration_sdk.schemas.parameter import (
    ParameterRole as R,
)
from majordom_integration_sdk.schemas.parameter import (
    ParameterUnit,
    ParameterVisibility,
)
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import ROOM, assert_not_paired, leaked_tasks, param, wait_for_value
from tests.virtual.mdns import Advert, FakeMDNS
from tests.virtual.runner import VirtualDevice

# (entity name, sub-field) -> (data type, role): the parameters the Hub must see for the virtual device
EXPECTED_PARAMETERS: dict[tuple[str, str], tuple[T, R]] = {
    ("Relay", "state"): (T.bool, R.control),
    ("Virtual Fan", "state"): (T.bool, R.control),
    ("Virtual Light", "state"): (T.bool, R.control),
    ("Virtual Light", "brightness"): (T.decimal, R.control),  # brightness-only light: no colour channels
    ("RGB Light", "state"): (T.bool, R.control),
    ("RGB Light", "brightness"): (T.decimal, R.control),
    ("RGB Light", "color_hue"): (T.decimal, R.control),  # colour as hue and saturation, like Matter and Zigbee
    ("RGB Light", "color_saturation"): (T.decimal, R.control),
    ("Virtual Light", "transition_length"): (T.decimal, R.control),  # a setting kept by the integration
    ("Virtual Light", "flash"): (T.none, R.control),  # a command with its length as argument
    ("RGB Light", "transition_length"): (T.decimal, R.control),
    ("RGB Light", "flash"): (T.none, R.control),
    ("Blinds", "position"): (T.decimal, R.control),
    ("Blinds", "operation"): (T.enum, R.sensor),
    ("Blinds", "stop"): (T.none, R.control),
    ("Virtual Fan", "speed_level"): (T.integer, R.control),
    ("Virtual Fan", "oscillating"): (T.bool, R.control),
    ("Virtual Fan", "direction"): (T.enum, R.control),
    ("HVAC", "action"): (T.enum, R.sensor),
    ("HVAC", "custom_preset"): (T.enum, R.control),
    ("HVAC", "mode"): (T.enum, R.control),
    ("HVAC", "current_temperature"): (T.decimal, R.sensor),
    ("HVAC", "target_temperature_low"): (T.decimal, R.control),  # the thermostat is two-point
    ("HVAC", "target_temperature_high"): (T.decimal, R.control),
    ("Target Temp", "state"): (T.decimal, R.control),
    ("Mode", "state"): (T.enum, R.control),
    ("Reboot", "state"): (T.none, R.control),
    ("Temperature", "state"): (T.decimal, R.sensor),
    ("Humidity", "state"): (T.decimal, R.sensor),
    ("Dust", "state"): (T.decimal, R.sensor),
    ("Distance", "state"): (T.decimal, R.sensor),
    ("Motion", "state"): (T.bool, R.sensor),
    ("Status", "state"): (T.string, R.sensor),
}


async def test_pairing_exposes_exactly_the_capabilities_of_each_entity(paired: ESPhomeDevice):
    actual = {
        (p.integration_data.entity_name, p.integration_data.sub_field): (p.data_type, p.role) for p in paired.parameters
    }

    assert actual == EXPECTED_PARAMETERS


async def test_parameter_metadata_comes_from_the_device(paired: ESPhomeDevice):
    target = param(paired, "Target Temp")
    assert (target.min_value, target.max_value, target.min_step) == (10, 30, 0.5)
    assert param(paired, "Temperature").unit == ParameterUnit.celsius
    assert param(paired, "Humidity").unit == ParameterUnit.percentage
    assert param(paired, "Dust").unit == ParameterUnit.ugm3
    assert param(paired, "Distance").unit == ParameterUnit.meters
    # enums are integers with string labels (`valid_values`), labelled by the enum names like the other integrations
    assert param(paired, "Mode").valid_values == {0: "AUTO", 1: "MANUAL"}
    assert param(paired, "Blinds", "operation").valid_values == {0: "IDLE", 1: "IS_OPENING", 2: "IS_CLOSING"}
    assert param(paired, "HVAC", "mode").valid_values == {0: "OFF", 2: "COOL", 3: "HEAT"}  # the modes it supports
    # levels, positions and colour channels are percentages, as everywhere in MajorDom (ESPHome reports 0-1)
    for entity, sub_field in [
        ("Virtual Light", "brightness"),
        ("RGB Light", "color_saturation"),
        ("Blinds", "position"),
    ]:
        level = param(paired, entity, sub_field)
        assert (level.unit, level.min_value, level.max_value) == (ParameterUnit.percentage, 0, 100), entity
    hue = param(paired, "RGB Light", "color_hue")
    assert (hue.unit, hue.min_value, hue.max_value) == (ParameterUnit.arcdegree, 0, 360)


async def test_the_remaining_fields_are_settings_with_the_limits_the_entity_reports(paired: ESPhomeDevice):
    speed = param(paired, "Virtual Fan", "speed_level")
    assert (speed.min_value, speed.max_value) == (0, 3)  # speed_count: 3
    assert param(paired, "Virtual Fan", "direction").valid_values == {0: "FORWARD", 1: "REVERSE"}
    assert param(paired, "HVAC", "custom_preset").valid_values == {0: "Default"}  # the presets the thermostat has
    extras = [("Blinds", "stop"), ("Virtual Fan", "oscillating"), ("HVAC", "action"), ("Virtual Fan", "direction")]
    for entity, sub_field in extras:
        assert param(paired, entity, sub_field).visibility == ParameterVisibility.setting, (entity, sub_field)
    assert param(paired, "Relay").visibility == ParameterVisibility.user  # the hand-written parameters stay on top
    assert not [p for p in paired.parameters if p.integration_data.sub_field == "tilt"]  # the blinds cannot tilt


async def test_identity_is_derived_through_the_sdk_helpers(controller: ESPhomeController, paired: ESPhomeDevice):
    assert paired.id == controller.device_uuid("983569abf679")
    for parameter in paired.parameters:
        key = (
            f"{parameter.integration_data.entity_name.lower().replace(' ', '_')}_{parameter.integration_data.sub_field}"
        )
        assert parameter.id == controller.parameter_uuid(paired.id, key)


async def test_device_has_a_one_tap_main_parameter(paired: ESPhomeDevice):
    main = next((p for p in paired.parameters if p.id == paired.main_parameter), None)

    assert main is not None, "main_parameter must reference one of the device's parameters"
    assert main.can_be_main_parameter


async def test_pairing_keeps_what_the_hub_already_knows_about_the_device(
    controller: ESPhomeController, discovery: Discovery
):
    await controller.pair_device(discovery, credentials=None)

    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    assert (stored.room_id, stored.name) == (ROOM, "Kitchen relay")  # the user's choices, saved by the Hub


async def test_pairing_needs_the_device_the_hub_creates(controller: ESPhomeController, mdns: FakeMDNS, device):
    await mdns.announce(Advert(port=device.port))
    (discovery,) = controller.discoveries.values()

    with pytest.raises(LookupError):
        await controller.pair_device(discovery, credentials=None)

    async with controller.dependencies.make_device_repository() as repo:
        assert await repo.get(discovery.id) is None  # the integration must not invent the device


async def test_pairing_reports_the_device_and_forgets_the_discovery(
    controller: ESPhomeController, discovery: Discovery, output: RecordingControllerOutput
):
    await controller.pair_device(discovery, credentials=None)

    assert output.connected_devices == [discovery.id]
    assert controller.discoveries == {}


async def test_pairing_accepts_explicit_no_credentials(controller: ESPhomeController, discovery: Discovery):
    await controller.pair_device(discovery, ProvidedCredentials(type=CredentialsType.none))


async def test_pairing_rejects_credentials_the_discovery_did_not_ask_for(
    controller: ESPhomeController, discovery: Discovery
):
    with pytest.raises(ValueError):
        await controller.pair_device(discovery, ProvidedCredentials(type=CredentialsType.secret, value="a-key"))

    assert discovery.id in controller.discoveries


async def test_device_can_be_paired_again_after_unpairing(
    controller: ESPhomeController, mdns: FakeMDNS, device: VirtualDevice, paired: ESPhomeDevice
):
    await controller.unpair(paired)
    await mdns.announce(Advert(name="test_node", port=device.port, addresses=["127.0.0.1", "::1"]))
    (discovery,) = controller.discoveries.values()

    await controller.pair_device(discovery, credentials=None)

    async with controller.dependencies.make_device_repository() as repo:
        again = await repo.get(discovery.id, as_=ESPhomeDevice)
    assert again is not None
    assert [p.id for p in again.parameters] == [p.id for p in paired.parameters]


@pytest.mark.timeout(60)
async def test_pairing_an_unreachable_device_fails_fast_and_cleanly(
    controller: ESPhomeController, discovery: Discovery, device: VirtualDevice, output: RecordingControllerOutput
):
    await device.stop()

    async with asyncio.timeout(25):
        with pytest.raises((ConnectionError, TimeoutError)):
            await controller.pair_device(discovery, credentials=None)

    await assert_not_paired(controller.dependencies, discovery.id)
    assert output.connected_devices == []
    assert output.updated_discoveries[-1].last_error, "the user must see why pairing failed"
    assert discovery.id in controller.discoveries
    await asyncio.sleep(0.1)
    assert leaked_tasks() == []


async def test_pairing_is_retryable_after_the_device_comes_back(
    controller: ESPhomeController, discovery: Discovery, device: VirtualDevice, output: RecordingControllerOutput
):
    await device.stop()
    with pytest.raises((ConnectionError, TimeoutError)):
        await controller.pair_device(discovery, credentials=None)
    await device.start()

    await controller.pair_device(output.updated_discoveries[-1], credentials=None)

    async with controller.dependencies.make_device_repository() as repo:
        stored = await repo.get(discovery.id, as_=ESPhomeDevice)
    assert stored is not None
    assert stored.last_error is None
    await wait_for_value(output, param(stored, "Temperature"))

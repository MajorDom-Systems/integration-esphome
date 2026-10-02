"""Device -> Hub: states pushed by a virtual ESPHome device become typed parameter events."""

import pytest
from aioesphomeapi import APIClient
from majordom_integration_sdk.testing import RecordingControllerOutput

from majordom_esphome.controller import ESPhomeController
from majordom_esphome.models import ESPhomeDevice
from tests.helpers import assert_values_match_parameters, changes, param, values, wait_for_value, wait_until
from tests.virtual.runner import VirtualDevice


@pytest.fixture
async def settled(paired: ESPhomeDevice, output: RecordingControllerOutput) -> ESPhomeDevice:
    """Paired device whose initial states have all arrived."""
    for entity in ("Temperature", "Humidity", "Dust", "Distance", "Motion", "Status", "Target Temp", "Mode"):
        await wait_for_value(output, param(paired, entity))
    return paired


async def test_initial_states_are_reported_with_device_values(
    settled: ESPhomeDevice, output: RecordingControllerOutput
):
    expected = {"Temperature": 21.5, "Humidity": 55.0, "Dust": 12.0, "Distance": 1.5, "Motion": True, "Status": "OK"}

    for entity, value in expected.items():
        assert values(output, param(settled, entity).id)[-1] == value, entity
    assert values(output, param(settled, "Target Temp").id)[-1] == 10.0
    assert values(output, param(settled, "Mode").id)[-1] == 0  # "AUTO": enums are reported as their index


async def test_every_value_has_the_type_its_parameter_declares(
    settled: ESPhomeDevice, output: RecordingControllerOutput
):
    await wait_for_value(output, param(settled, "HVAC", "mode"))
    await wait_for_value(output, param(settled, "Blinds", "operation"))

    assert_values_match_parameters(output, settled)


async def test_two_point_climate_reports_both_targets(settled: ESPhomeDevice, output: RecordingControllerOutput):
    low = await wait_for_value(output, param(settled, "HVAC", "target_temperature_low"))
    high = await wait_for_value(output, param(settled, "HVAC", "target_temperature_high"))

    assert low < high


async def test_changes_made_on_the_device_are_pushed(
    settled: ESPhomeDevice, output: RecordingControllerOutput, device: VirtualDevice
):
    relay = param(settled, "Relay")
    client = APIClient(device.address, device.port, "")
    await client.connect(login=True)
    try:
        entities, _ = await client.list_entities_services()
        relay_key = next(e.key for e in entities if e.name == "Relay")

        client.switch_command(relay_key, True)

        assert await wait_for_value(output, relay, lambda v: v is True)
    finally:
        await client.disconnect()


async def test_fetch_reports_the_current_value_of_every_readable_parameter(
    controller: ESPhomeController, settled: ESPhomeDevice, output: RecordingControllerOutput
):
    output.events.clear()

    await controller.fetch(settled)

    reported = {e.parameter_id for e in changes(output)}
    for entity in ("Temperature", "Humidity", "Dust", "Distance", "Motion", "Status", "Target Temp", "Mode"):
        assert param(settled, entity).id in reported, entity
    assert_values_match_parameters(output, settled)


async def test_fetch_reports_in_one_batch(controller: ESPhomeController, settled: ESPhomeDevice, output):
    batches: list[int] = []
    original = output.controller_did_receive_events

    async def spy(controller, events):
        events = list(events)
        batches.append(len(events))
        await original(controller, events)

    output.controller_did_receive_events = spy  # type: ignore[method-assign]
    await wait_until(lambda: True)
    batches.clear()

    await controller.fetch(settled)

    assert len(batches) == 1 and batches[0] >= 8
